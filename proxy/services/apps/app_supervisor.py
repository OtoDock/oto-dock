"""One process per app server (APPS.md "The supervisor").

The registry lives in this process only: port, pid, launch token, activity,
open bridged sockets and backoff per ``(row id, instance)``, where the
instance is ``live`` (the release the row serves) or ``preview`` (the
working tree copied for the owner or an editor, with its own data). The
proxy's death kills every child (pasta dies with the proxy, bwrap with
pasta), so the table is legitimately empty at boot.

Rules: wake on demand, only for an approved manifest; stdout and stderr in
one pipe the supervisor owns, written to ``app.log`` in the release
directory (outside the app's mounts) and rotated here; health is any HTTP
answer below 500 to ``GET /_health`` within 20 s; idle stop after ten
minutes without a request or an open bridged socket; a crash restarts with
backoff (1 s doubling to 60 s) on the next request, the routes answering
503 meanwhile; a crash into backoff notifies the owner or every editor once
per episode; the idle sweep also stops any entry whose row is gone or
is a personal app whose owner lost the agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from services.apps import app_sandbox, app_tokens, releases
from services.scripts.runner import scrub
from storage import database as task_store
from auth import roles

logger = logging.getLogger("claude-proxy.apps")

HEALTH_TIMEOUT_S = 20.0
IDLE_STOP_S = 10 * 60
SWEEP_S = 30.0
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
HEALTHY_RESET_S = 10 * 60
DRAIN_S = 5.0
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_TAIL_LINES = 200
# The pipe reader's line limit (asyncio's default is 64 KB).
PIPE_LINE_LIMIT = 1024 * 1024
BIND_RETRIES = 3
# Marks in the last log lines that classify an exit as the bucket being
# full rather than a bug.
QUOTA_MARKS = ("EDQUOT", "Disk quota exceeded", "SQLITE_FULL", "database or disk is full")

# The server state vocabulary, named once (core-seams phase 8; the dashboard
# mirror is ``lib/status/appServer.ts``). ``Instance.state`` takes the first
# six; ``AppUnavailable.state`` adds the two holds. The state rides
# ``X-OtoDock-Server`` on a 503, the app shape's ``server``, the logs and
# deploy-status routes, and the runtime shim posts it into the iframe as
# ``server_status.state`` — with its own ``failed`` (``SHIM_FAILED``) for a
# server that stayed 503 past the shim's wait, never an ``Instance.state``.
STOPPED = "stopped"
STARTING = "starting"
UP = "up"
BACKOFF = "backoff"
QUOTA_FULL = "quota_full"
STATIC = "static"
UNAPPROVED = "unapproved"
SECRETS = "secrets"
STATES: frozenset[str] = frozenset({STOPPED, STARTING, UP, BACKOFF, QUOTA_FULL, STATIC, UNAPPROVED, SECRETS})
#: Routes to now: a healthy server, or a client-only app with no server.
SERVING: frozenset[str] = frozenset({UP, STATIC})
#: Held back with a ``last_error`` and a wait before the next start.
HELD: frozenset[str] = frozenset({BACKOFF, QUOTA_FULL})
SHIM_FAILED = "failed"


class AppUnavailable(Exception):
    """The server is not up and will not be for ``retry_after`` seconds."""

    def __init__(self, reason: str, retry_after: int = 5, state: str = BACKOFF) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after
        self.state = state


@dataclass
class Instance:
    row_id: str
    name: str                     # "live" | "preview" | "check"
    row: dict
    release_dir: Path
    data_dir: Path
    host_port: int = 0
    token: str = ""
    entry: str = ""
    proc: asyncio.subprocess.Process | None = None
    state: str = STOPPED          # STATES minus the two holds (AppUnavailable carries those)
    started_at: float = 0.0
    last_activity: float = field(default_factory=time.monotonic)
    healthy_since: float = 0.0
    ws_count: int = 0
    backoff_s: float = BACKOFF_MIN_S
    next_start_at: float = 0.0
    last_error: str = ""
    notified: bool = False
    stop_requested: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    _pump: asyncio.Task | None = None
    _watch: asyncio.Task | None = None
    _tail: list[str] = field(default_factory=list)
    # APPS.md "Secrets": the values the child was handed (its `env`
    # secrets and the launch token), replaced by "[redacted]" in every
    # line the pump sees, so nothing the server prints keeps one.
    scrub: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.row_id, self.name)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.host_port}"

    def touch(self) -> None:
        self.last_activity = time.monotonic()


_instances: dict[tuple[str, str], Instance] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(key: tuple[str, str]) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


def get(row_id: str, name: str = "live") -> Instance | None:
    return _instances.get((row_id, name))


def instances() -> list[Instance]:
    return list(_instances.values())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _allow_hosts(row: dict) -> list[str]:
    """The approved egress hosts of the row resolved to addresses; an
    unresolvable name is skipped (logged), never a wildcard. Public
    addresses only: a carve is a /32 that outranks the private-range
    blackholes, so a name its author points at a LAN or container address
    would open that address to the server."""
    from api.apps import manifest as _mf
    from services.infra.outbound_url import ip_blocked
    out: list[str] = []
    for host in _mf.parse_egress(row):
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            logger.warning("App %s: egress host %s did not resolve", row.get("slug"), host)
            continue
        for info in infos:
            addr = info[4][0]
            if ip_blocked(addr):
                logger.warning("App %s: egress host %s resolved to %s, not a public address; "
                               "not opened", row.get("slug"), host, addr)
                continue
            if addr not in out:
                out.append(addr)
    return out


async def _write_log(inst: Instance, line: str) -> None:
    await append_log(inst.row, line)


async def append_log(row: dict, line: str) -> None:
    """One line into the app's log (``app.log``, rotated at 5 MB): the
    server's pump and a step's trailer (APPS.md "Steps") share it."""
    path = releases.log_path(row)

    def _sync() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.is_file() and path.stat().st_size >= LOG_MAX_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(line)

    await asyncio.to_thread(_sync)


# What the network launcher prints on every start when the host has no
# IPv6 route, a loopback stub resolver and no syslog — four lines of pasta
# that mean nothing to an app's author and buried every real line of
# app_logs on the internal install. Dropped from the log and the tail; a
# line that is not exactly one of these is kept.
_LAUNCHER_NOISE = (
    "Failed to send ", " bytes to syslog",
    "No external routable interface for IPv6",
    "Couldn't get any nameserver address",
)


def is_launcher_noise(line: str) -> bool:
    """True for pasta's start-up chatter (see ``_LAUNCHER_NOISE``)."""
    s = line.strip()
    if not s:
        return False
    if s.startswith("Failed to send ") and s.endswith(" bytes to syslog"):
        return True
    return s in _LAUNCHER_NOISE[2:]


async def _pump(inst: Instance) -> None:
    """Own the child's one pipe: every line goes to the log file and into
    the short in-memory tail the crash classifier and the tools read."""
    proc = inst.proc
    if proc is None or proc.stdout is None:
        return
    stamp = f"[{inst.name}]"
    try:
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                # A line past the reader's limit (asyncio drops what it
                # held): keep reading, or the pipe fills and the child
                # blocks on its next write for good.
                raw = f"[a line longer than {PIPE_LINE_LIMIT} bytes was not kept]\n".encode()
            if not raw:
                return
            text = raw.decode("utf-8", "replace")
            if is_launcher_noise(text):
                continue
            if inst.scrub:
                text = scrub(text, inst.scrub)
            inst._tail.append(text.rstrip("\n"))
            del inst._tail[:-LOG_TAIL_LINES]
            await _write_log(inst, f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {stamp} {text}")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("App %s: log pump failed", inst.row.get("slug"))


async def _watch(inst: Instance) -> None:
    """Wait for the child; a death nobody asked for goes to backoff."""
    proc = inst.proc
    if proc is None:
        return
    code = await proc.wait()
    if inst._pump is not None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(inst._pump, timeout=2)
    if inst.stop_requested or _instances.get(inst.key) is not inst:
        inst.state = STOPPED
        inst.closed.set()
        return
    tail = "\n".join(inst._tail[-5:])
    inst.last_error = f"exited with code {code}"
    if any(mark in tail for mark in QUOTA_MARKS):
        inst.state = QUOTA_FULL
        inst.last_error = "the storage quota is full"
    else:
        inst.state = BACKOFF
    inst.next_start_at = time.monotonic() + inst.backoff_s
    inst.backoff_s = min(inst.backoff_s * 2, BACKOFF_MAX_S)
    inst.closed.set()
    logger.warning("App %s (%s) %s; next start in %.0fs", inst.row.get("slug"),
                   inst.name, inst.last_error, inst.next_start_at - time.monotonic())
    if not inst.notified and inst.name == "live":
        inst.notified = True
        try:
            await _notify_crash(inst)
        except Exception:
            logger.exception("App %s: crash notification failed", inst.row.get("slug"))


async def _notify_crash(inst: Instance) -> None:
    """Tell the owner (personal app) or every editor and manager (shared
    app) once per backoff episode."""
    from services.notifications.notification_manager import fire_notification
    from storage.identity import db_users
    row = inst.row
    if row.get("username"):
        targets = [row.get("owner_sub") or ""]
    else:
        targets = [u["sub"] for u in db_users.get_agent_users(row["agent"])
                   if roles.can_edit(u.get("agent_role"))]
    title = f"{row.get('title') or row.get('slug')} stopped"
    body = (f"The app's server {inst.last_error}. It restarts when someone opens it; "
            "the menu's Logs entry shows why.")
    for sub in [t for t in targets if t]:
        await fire_notification(
            title, body, severity="warning", scope="user", target=sub,
            source="app", source_id=row["id"], agent_slug=row["agent"],
            href=f"/apps/{row['id']}",
        )


async def _health(inst: Instance) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    async with httpx.AsyncClient(timeout=1.5) as client:
        while time.monotonic() < deadline:
            proc = inst.proc
            if proc is None or proc.returncode is not None:
                return False
            try:
                r = await client.get(f"{inst.base_url}/_health")
                if r.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    return False


async def _launch(inst: Instance, *, preview: bool, allow_hosts: list[str] | None = None,
                  secrets: dict[str, str] | None = None) -> None:
    """Spawn the child on a fresh host port (a pasta bind failure within a
    second retries with another port) and pump its pipe. ``allow_hosts``
    overrides the row's approved egress (the render of an unapproved
    manifest runs with none); ``secrets`` are the app's ``env`` secrets,
    merged for the live instance only: a preview or a check runs code
    nobody approved and never receives a value (APPS.md "Secrets")."""
    row = inst.row
    allow = allow_hosts if allow_hosts is not None else await asyncio.to_thread(_allow_hosts, row)
    if inst.name != "live":
        secrets = None
    last = ""
    for _attempt in range(BIND_RETRIES):
        inst.host_port = _free_port()
        argv = app_sandbox.build_command(row, inst.release_dir, inst.data_dir,
                                         inst.host_port, inst.entry, allow)
        env = app_sandbox.build_env(row, inst.token, app_tokens.public_key_b64(row["id"]),
                                    preview=preview, secrets=secrets)
        inst.proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, limit=PIPE_LINE_LIMIT,
        )
        inst._tail = []
        inst._pump = asyncio.create_task(_pump(inst))
        await asyncio.sleep(1.0)
        if inst.proc.returncode is None:
            return
        # The reason the agent reads: the last lines that say something (a
        # runtime's blank line and version banner would crowd out the error).
        said = [ln for ln in inst._tail[-12:] if ln.strip() and not ln.strip().startswith("Bun v")]
        last = "\n".join(said[-4:])
        if "bind" not in last.lower() and "address" not in last.lower():
            break
        logger.info("App %s: port %d was taken at launch, retrying", row.get("slug"),
                    inst.host_port)
    raise app_sandbox.AppStartError(last or "the server exited at once")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=DRAIN_S)
        except asyncio.TimeoutError:
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
    except ProcessLookupError:
        pass


async def start(row: dict, name: str = "live", *, release_dir: Path | None = None,
                data_dir: Path | None = None, allow_hosts: list[str] | None = None) -> Instance:
    """Start (or return the running) instance of ``row``. Raises
    ``AppStartError`` when it cannot come up; the previous instance of the
    same key, if any, is replaced only after the new one is healthy when
    the caller passes a new release directory (blue/green: see
    ``app_deploy``), else stopped first. ``allow_hosts`` overrides the
    approved egress for a ``check`` instance (none while unapproved)."""
    key = (row["id"], name)
    async with _lock(key):
        existing = _instances.get(key)
        if existing and existing.state in SERVING and release_dir is None:
            existing.touch()
            return existing
        if name != "check" and not task_store.app_actions_approved(row):
            raise AppUnavailable("the app is waiting for approval", 30, state=UNAPPROVED)
        # A personal app whose owner lost the agent never starts, whichever
        # route asked (APPS.md "Lifecycle").
        if name != "check" and await asyncio.to_thread(task_store.personal_row_dormant, row):
            raise app_sandbox.AppStartError("the owner no longer has access to this agent")
        # APPS.md "Secrets": a required secret without a value keeps the
        # live server down the way a waiting approval does; a preview or a
        # check runs without any value and so waits for none.
        secrets: dict[str, str] = {}
        if name == "live":
            from services.apps import app_secrets
            missing = await asyncio.to_thread(app_secrets.missing_required, row)
            if missing:
                raise AppUnavailable("waiting for a secret to be set: " + ", ".join(missing),
                                     30, state=SECRETS)
            secrets = await asyncio.to_thread(app_secrets.env_values, row)
        if existing and existing.state == BACKOFF and release_dir is None \
                and time.monotonic() < existing.next_start_at:
            raise AppUnavailable(existing.last_error or "starting soon",
                                 int(existing.next_start_at - time.monotonic()) + 1)
        if release_dir is None:
            if name == "preview":
                release_dir = releases.preview_dir(row)
            else:
                release_dir = releases.live_release_dir(row)
            if release_dir is None:
                raise app_sandbox.AppStartError("the app has no release to run")
        if data_dir is None:
            data_dir = (release_dir / "data") if name == "preview" else releases.app_data_dir(row)
        entry = app_sandbox.server_entry(release_dir)
        inst = Instance(row_id=row["id"], name=name, row=row, release_dir=release_dir,
                        data_dir=data_dir, entry=entry)
        if existing is not None:
            inst.backoff_s = existing.backoff_s
            inst.notified = existing.notified
        if not entry:
            inst.state = STATIC
            if existing is not None and existing.proc is not None:
                await stop(row["id"], name)
            _instances[key] = inst
            return inst
        await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
        # The data mount point must exist under the read-only /app (a
        # release carries one; a working tree run by the smoke may not).
        with contextlib.suppress(OSError):
            await asyncio.to_thread((release_dir / "data").mkdir, exist_ok=True)
        inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                     {"sub": f"app:{row['id']}", "instance": name},
                                     app_tokens.LAUNCH_TTL_S)
        inst.state = STARTING
        inst.started_at = time.monotonic()
        inst.scrub = [v for v in [inst.token, *secrets.values()] if isinstance(v, str) and len(v) >= 8]
        try:
            await _launch(inst, preview=(name == "preview"), allow_hosts=allow_hosts,
                          secrets=secrets or None)
            if not await _health(inst):
                tail = "\n".join(inst._tail[-5:])
                raise app_sandbox.AppStartError(
                    f"the server did not answer /_health within {int(HEALTH_TIMEOUT_S)}s"
                    + (f": {tail}" if tail else ""))
        except asyncio.CancelledError:
            # The caller gave up (a check's budget, a request that went
            # away): nothing may keep running unregistered, holding a port.
            if inst.proc is not None and inst.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(inst.proc.pid), signal.SIGKILL)
            if inst._pump is not None:
                inst._pump.cancel()
            inst.state = STOPPED
            raise
        except Exception as e:
            if inst.proc is not None:
                await _kill(inst.proc)
            if inst._pump is not None:
                inst._pump.cancel()
            inst.state = BACKOFF
            inst.last_error = str(e)
            inst.next_start_at = time.monotonic() + inst.backoff_s
            inst.backoff_s = min(inst.backoff_s * 2, BACKOFF_MAX_S)
            if existing is None or existing.state != UP:
                _instances[key] = inst
            logger.warning("App %s (%s) failed to start: %s", row.get("slug"), name, e)
            raise app_sandbox.AppStartError(str(e)) from e
        inst.state = UP
        inst.healthy_since = time.monotonic()
        inst.backoff_s = BACKOFF_MIN_S
        inst.touch()
        inst._watch = asyncio.create_task(_watch(inst))
        old = _instances.get(key)
        _instances[key] = inst
        if old is not None and old is not inst and old.proc is not None:
            await _drain_and_stop(old)
        logger.info("App %s (%s) up on 127.0.0.1:%d (pid %s, release %s)", row.get("slug"),
                    name, inst.host_port, inst.proc.pid if inst.proc else "-",
                    inst.release_dir.name)
        return inst


async def _drain_and_stop(old: Instance) -> None:
    """Give an instance that a newer one replaced a moment to finish what
    is in flight, then stop it (its bridged sockets close with 1012 and
    the runtime reconnects to the new instance)."""
    old.stop_requested = True
    await asyncio.sleep(min(DRAIN_S, 1.0))
    if old.proc is not None:
        await _kill(old.proc)
    old.state = STOPPED
    old.closed.set()


async def stop(row_id: str, name: str | None = None) -> int:
    """Stop the live instance, the preview, or both (``name=None``).
    Returns how many processes were stopped."""
    names = [name] if name else ["live", "preview", "check"]
    count = 0
    for n in names:
        inst = _instances.pop((row_id, n), None)
        if inst is None:
            continue
        inst.stop_requested = True
        if inst.proc is not None and inst.proc.returncode is None:
            await _kill(inst.proc)
            count += 1
        if inst._pump is not None:
            inst._pump.cancel()
        inst.state = STOPPED
        inst.closed.set()
    return count


async def ensure_up(row: dict, name: str = "live") -> Instance:
    """The instance to route to, started if needed. Raises
    ``AppUnavailable`` (503 with Retry-After for the routes) when the
    server is in backoff, unapproved, or fails to start now."""
    inst = _instances.get((row["id"], name))
    if inst is not None and inst.state in SERVING:
        inst.touch()
        return inst
    if inst is not None and inst.state in HELD:
        wait = inst.next_start_at - time.monotonic()
        if wait > 0 or inst.state == QUOTA_FULL:
            raise AppUnavailable(inst.last_error or "starting soon",
                                 max(1, int(wait) + 1), state=inst.state)
    try:
        return await start(row, name)
    except app_sandbox.AppStartError as e:
        fresh = _instances.get((row["id"], name))
        wait = (fresh.next_start_at - time.monotonic()) if fresh else BACKOFF_MIN_S
        raise AppUnavailable(str(e), max(1, int(wait) + 1)) from e


def touch(row_id: str, name: str = "live") -> None:
    inst = _instances.get((row_id, name))
    if inst is not None:
        inst.touch()


def ws_opened(row_id: str, name: str = "live") -> None:
    inst = _instances.get((row_id, name))
    if inst is not None:
        inst.ws_count += 1
        inst.touch()


def ws_closed(row_id: str, name: str = "live") -> None:
    inst = _instances.get((row_id, name))
    if inst is not None:
        inst.ws_count = max(0, inst.ws_count - 1)
        inst.touch()


def status(row_id: str) -> dict:
    """What the tools and the dashboard show about the server."""
    inst = _instances.get((row_id, "live"))
    preview = _instances.get((row_id, "preview"))
    out = {"server": inst.state if inst else STOPPED,
           "error": (inst.last_error if inst and inst.state in HELD else ""),
           "retry_after": (max(0, int(inst.next_start_at - time.monotonic()))
                           if inst and inst.state == BACKOFF else 0),
           "preview": preview.state if preview else STOPPED}
    return out


def read_log_tail(row: dict, lines: int = LOG_TAIL_LINES) -> str:
    """The last ``lines`` of the app's log (both rotation halves)."""
    path = releases.log_path(row)
    out: list[str] = []
    for p in (path.with_suffix(".log.1"), path):
        try:
            out.extend(p.read_text("utf-8", "replace").splitlines())
        except OSError:
            continue
    return "\n".join(out[-max(1, min(lines, 2000)):])


async def smoke(row: dict, tree_dir: Path) -> dict:
    """``check_app``'s smoke: start the working tree's server on a scratch
    port with a scratch data directory, wait for health, stop. Never touches
    the live database."""
    import tempfile
    entry = app_sandbox.server_entry(tree_dir)
    if not entry:
        return {"ok": True, "server": "none"}
    with tempfile.TemporaryDirectory(prefix="otodock-app-check-") as scratch:
        key = (row["id"], "check")
        # The declared hosts open only once a person approved the manifest
        # (the rendered check's rule); a smoke of an unapproved manifest
        # runs with none.
        allow = None if task_store.app_actions_approved(row) else []
        try:
            inst = await start(row, "check", release_dir=tree_dir, data_dir=Path(scratch),
                               allow_hosts=allow)
        except (app_sandbox.AppStartError, AppUnavailable) as e:
            inst = _instances.pop(key, None)
            tail = "\n".join(inst._tail[-20:]) if inst else ""
            return {"ok": False, "server": "failed", "reason": str(e), "log": tail}
        tail = "\n".join(inst._tail[-20:])
        await stop(row["id"], "check")
        return {"ok": True, "server": "up", "log": tail}


async def sweep_once() -> None:
    # Due handler deliveries wake their apps first (APPS.md "Handlers"); a
    # draining app is never idle-stopped under its drain.
    from services.apps import app_handlers
    try:
        await app_handlers.sweep()
    except Exception:
        logger.exception("App handler drain failed (continuing)")
    now = time.monotonic()
    for inst in list(_instances.values()):
        row, dormant = await asyncio.to_thread(_row_and_dormancy, inst.row_id)
        # A dormant personal app's server stops here too: the offboarding
        # event is sent once, and a request in flight at the removal could
        # have started it again just after.
        if row is None or dormant or (row.get("hidden") and inst.name == "live"):
            await stop(inst.row_id)
            continue
        if inst.state == UP and inst.ws_count == 0 and now - inst.last_activity > IDLE_STOP_S \
                and not app_handlers.draining(inst.row_id):
            logger.info("App %s (%s) idle for %ds, stopping", inst.row.get("slug"),
                        inst.name, int(now - inst.last_activity))
            await stop(inst.row_id, inst.name)
            continue
        if inst.state == UP and inst.notified and now - inst.healthy_since > HEALTHY_RESET_S:
            inst.notified = False
        if inst.state == STOPPED and inst.name == "check":
            _instances.pop(inst.key, None)


def _row_and_dormancy(row_id: str) -> tuple[dict | None, bool]:
    row = task_store.get_app(row_id)
    return row, bool(row) and task_store.personal_row_dormant(row)


async def sweep_loop() -> None:
    while True:
        try:
            await asyncio.sleep(SWEEP_S)
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("App supervisor sweep failed (continuing)")


async def stop_all() -> None:
    # A drain cancelled here leaves its row in flight for the boot reset.
    from services.apps import app_handlers
    await app_handlers.cancel_drains()
    for key in list(_instances):
        await stop(key[0], key[1])
