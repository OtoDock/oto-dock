"""App steps on a paired machine (proxy APPS.md "Steps", satellite 0.5.122).

The proxy sends one ``step_run`` frame per delivery: the script's text and
its sha256, the folder to run in (relative to the synced agent folder), the
environment, the payload and the timeout. This module writes the script and
the payload into the satellite's own private directory (never the synced
workspace), checks the hash of what it wrote, runs the interpreter the
script's first line names (no mode bit is needed — a synced file carries
none), streams what the script prints as ``step_output`` frames, caps what
it keeps, kills the process tree at the timeout or when the link drops,
and answers the ack with the exit code and the first 32 KB of output.

No user switching: the script runs as the user the satellite runs as, like
every session on this machine. Only the script travels — a sibling file
of the app folder is not here; a step keeps its data in the workspace or
inside itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shlex
import signal
import time
from pathlib import Path

from ..config import force_rmtree, kill_process_tree, otodock_dir
from ..host.auth_paths import is_safe_slug
from ..host.env_hygiene import curate_satellite_env
from .. import config
from .._vendored import layout

logger = logging.getLogger("satellite")

STEPS_DIR_NAME = "steps"
OUTPUT_KEEP_BYTES = 32 * 1024
SCRIPT_MAX_BYTES = 256 * 1024
TIMEOUT_MAX_S = 7200
# Output frames: coalesced so a chatty script does not flood the lane.
_FLUSH_BYTES = 4096
_FLUSH_S = 0.3
# After the script exits: how long its output may keep draining.
_DRAIN_S = 5.0
_DELIVERY_RE = re.compile(r"^[0-9a-f-]{36}$")
_REL_RE = re.compile(
    rf"^({layout.WORKSPACE}|{layout.USERS}/[a-z0-9][a-z0-9_.-]{{0,63}}/{layout.WORKSPACE}|{layout.KNOWLEDGE})$"
)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class StepRefused(Exception):
    """The frame cannot be honoured; the ack says why."""


def steps_root() -> Path:
    """The private directory the scripts run from: outside ``agents_dir``,
    so the sync never sees it; wiped whole at satellite start."""
    return otodock_dir() / STEPS_DIR_NAME


def wipe_steps_root() -> None:
    root = steps_root()
    if root.exists():
        try:
            force_rmtree(root)
        except Exception:
            logger.warning("steps: could not wipe %s", root, exc_info=True)


def argv_for(script: bytes, path: str) -> list[str]:
    """The proxy's rule, verbatim: a ``#!`` line names the interpreter,
    otherwise ``/bin/sh``."""
    first = script.split(b"\n", 1)[0].strip()
    if first.startswith(b"#!"):
        words = shlex.split(first[2:].decode("utf-8", "replace"))
        if words:
            return [*words, path]
    return ["/bin/sh", path]


def _resolve_inside(agent_dir: Path, rel: str) -> Path:
    """``agent_dir/rel`` resolved, refused when it leaves the agent folder."""
    base = agent_dir.resolve()
    target = (agent_dir / rel).resolve()
    if not target.is_relative_to(base):
        raise StepRefused("the folder leaves the agent's tree")
    return target


def _validate(msg: dict, agents_dir: Path) -> dict:
    delivery = str(msg.get("delivery_id") or "")
    if not _DELIVERY_RE.match(delivery):
        raise StepRefused("delivery id is not a uuid")
    slug = str(msg.get("agent_slug") or "")
    if not is_safe_slug(slug):
        raise StepRefused("agent slug is not safe")
    script = msg.get("script")
    if not isinstance(script, str) or not script:
        raise StepRefused("no script")
    data = script.encode("utf-8")
    if len(data) > SCRIPT_MAX_BYTES:
        raise StepRefused("the script is larger than 256 KB")
    sha = str(msg.get("sha256") or "")
    if hashlib.sha256(data).hexdigest() != sha:
        raise StepRefused("the script does not match its hash")
    run = str(msg.get("run") or "script.sh")
    name = run.rsplit("/", 1)[-1]
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$", name):
        raise StepRefused("the script's name is not plain")
    ws_rel = str(msg.get("workspace_relative") or "")
    if not _REL_RE.match(ws_rel):
        raise StepRefused("the working folder is not a workspace")
    kn_rel = str(msg.get("knowledge_relative") or "")
    if kn_rel and kn_rel != layout.KNOWLEDGE:
        raise StepRefused("the knowledge folder is not the agent's")
    timeout = msg.get("timeout", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= TIMEOUT_MAX_S:
        raise StepRefused("the timeout is out of bounds")
    env = msg.get("env") or {}
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                        for k, v in env.items()):
        raise StepRefused("the environment is not a map of strings")
    # 0.5.123 (proxy CHECKS.md): extra env names that point at the payload
    # file, and a session's working folder outside the synced tree.
    payload_env = msg.get("payload_env") or []
    if (not isinstance(payload_env, list) or len(payload_env) > 4
            or any(not isinstance(n, str) or not _ENV_NAME_RE.match(n) for n in payload_env)):
        raise StepRefused("payload_env is not a short list of env names")
    agent_dir = agents_dir / slug
    cwd_abs = msg.get("cwd_absolute") or ""
    if cwd_abs:
        if not isinstance(cwd_abs, str) or not Path(cwd_abs).is_absolute():
            raise StepRefused("cwd_absolute is not an absolute folder")
        from ..terminal.pty_session_base import resolve_work_cwd
        try:
            cwd = resolve_work_cwd(cwd_abs, agent_dir, ws_rel)
        except Exception as e:  # noqa: BLE001 — the guard's own words are the reason
            raise StepRefused(f"the working folder is refused: {e}")
    else:
        cwd = _resolve_inside(agent_dir, ws_rel)
        if not cwd.is_dir():
            raise StepRefused(f"the synced folder {ws_rel} is missing on this machine")
    knowledge = _resolve_inside(agent_dir, kn_rel) if kn_rel else None
    payload = msg.get("payload_json")
    if not isinstance(payload, str):
        payload = "{}"
    return {"delivery": delivery, "slug": slug, "data": data, "name": name, "cwd": cwd,
            "knowledge": knowledge, "timeout": int(timeout), "env": env, "payload": payload,
            "payload_env": list(payload_env)}


class _Capture:
    def __init__(self) -> None:
        self.head = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if len(self.head) < OUTPUT_KEEP_BYTES:
            self.head.extend(chunk[:OUTPUT_KEEP_BYTES - len(self.head)])

    def text(self) -> str:
        out = self.head.decode("utf-8", "replace")
        if self.total > len(self.head):
            out += f"\n… [{self.total - len(self.head)} more bytes not kept]"
        return out


async def run_step(msg: dict, ws, agents_dir: Path, local_tunnel_port: int,
                   running: dict[str, asyncio.subprocess.Process]) -> None:
    """The ``step_run`` handler: validate, write, run, stream, ack."""
    command_id = str(msg.get("command_id") or "")
    acked = False

    async def _ack(**fields) -> None:
        nonlocal acked
        acked = True
        await ws.enqueue_send({"type": "ack", "command_id": command_id, **fields})

    try:
        plan = _validate(msg, agents_dir)
    except StepRefused as e:
        await _ack(status="error", error=str(e))
        return
    if plan["delivery"] in running:
        await _ack(status="error", error="this delivery is already running here")
        return
    step_dir = steps_root() / plan["delivery"]
    try:
        try:
            step_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            force_rmtree(step_dir)
            step_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        await _ack(status="error", error=f"the step could not run: {e}")
        return
    with contextlib.suppress(OSError):
        os.chmod(step_dir, 0o700)
    script_path = step_dir / plan["name"]
    payload_path = step_dir / "payload.json"
    try:
        script_path.write_bytes(plan["data"])
        payload_path.write_text(plan["payload"], "utf-8")
        for p in (script_path, payload_path):
            with contextlib.suppress(OSError):
                os.chmod(p, 0o600)
        if hashlib.sha256(script_path.read_bytes()).hexdigest() != str(msg.get("sha256") or ""):
            await _ack(status="error", error="the script did not land intact")
            return
        env = curate_satellite_env(os.environ)
        env.update(plan["env"])
        env["OTODOCK_PROXY_URL"] = f"http://127.0.0.1:{local_tunnel_port}"
        env["OTODOCK_STEP_PAYLOAD"] = str(payload_path)
        for name in plan["payload_env"]:
            env[name] = str(payload_path)
        env["OTODOCK_STEP_SCRIPT"] = str(script_path)
        env["OTODOCK_WORKSPACE_DIR"] = str(plan["cwd"])
        env["OTODOCK_KNOWLEDGE_DIR"] = str(plan["knowledge"]) if plan["knowledge"] else ""
        argv = argv_for(plan["data"], str(script_path))
        spawn_kwargs: dict = {"start_new_session": True} if config.HOST.posix else {}
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(plan["cwd"]), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=env, **spawn_kwargs)
        except (OSError, ValueError) as e:
            await _ack(status="error", error=f"the script could not start: {e}")
            return
        running[plan["delivery"]] = proc
        cap = _Capture()
        pending = bytearray()
        last_flush = time.monotonic()

        async def _flush(force: bool = False) -> None:
            nonlocal pending, last_flush
            if not pending:
                return
            if force or len(pending) >= _FLUSH_BYTES or time.monotonic() - last_flush >= _FLUSH_S:
                text = bytes(pending).decode("utf-8", "replace")
                pending = bytearray()
                last_flush = time.monotonic()
                await ws.enqueue_send({"type": "step_output", "command_id": command_id,
                                       "delivery_id": plan["delivery"], "text": text})

        async def _pump() -> None:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                cap.add(chunk)
                pending.extend(chunk)
                await _flush()
            await _flush(force=True)

        def _kill_group() -> None:
            # What the script left behind dies with it, as the local run's
            # pid namespace does; a child that kept the pipe open would
            # otherwise hold the run to its timeout. The tree walk alone
            # finds nothing once the script itself has exited.
            if spawn_kwargs:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)

        async def _exited() -> None:
            # The return code, not wait(): wait() also waits for the pipe,
            # which a child the script left behind can hold open.
            while proc.returncode is None:
                await asyncio.sleep(0.1)

        timed_out = False
        pump = asyncio.create_task(_pump())
        try:
            await asyncio.wait_for(_exited(), timeout=plan["timeout"])
        except asyncio.TimeoutError:
            timed_out = True
            kill_process_tree(proc.pid, timeout=5.0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_exited(), timeout=10.0)
        try:
            try:
                await asyncio.wait_for(asyncio.shield(pump), timeout=_DRAIN_S)
            except asyncio.TimeoutError:
                _kill_group()
                try:
                    await asyncio.wait_for(pump, timeout=_DRAIN_S)
                except asyncio.TimeoutError:
                    pump.cancel()
            _kill_group()
        finally:
            running.pop(plan["delivery"], None)
        seconds = time.monotonic() - started
        logger.info("step %s (%s): exit=%s timed_out=%s %.1fs", plan["delivery"][:8], plan["name"],
                    proc.returncode, timed_out, seconds)
        await _ack(status="ok", exit_code=None if timed_out else proc.returncode,
                   output=cap.text(), timed_out=timed_out, seconds=round(seconds, 1))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("step %s: failed before its verdict", plan["delivery"][:8], exc_info=True)
        if not acked:
            with contextlib.suppress(Exception):
                await _ack(status="error", error=f"the step could not run: {e}")
    finally:
        try:
            force_rmtree(step_dir)
        except Exception:
            logger.warning("step %s: scratch dir not removed", plan["delivery"][:8], exc_info=True)


def kill_running(running: dict[str, asyncio.subprocess.Process], reason: str) -> int:
    """Every running step's tree killed (the link dropped, the satellite
    stops); the runner's wait sees the exit and the ack, if it can still be
    sent, says so."""
    n = 0
    for delivery, proc in list(running.items()):
        if proc.returncode is None:
            try:
                kill_process_tree(proc.pid, timeout=5.0)
                n += 1
            except Exception:
                logger.warning("step %s: kill failed (%s)", delivery[:8], reason, exc_info=True)
    if n:
        logger.info("steps: killed %d running script(s): %s", n, reason)
    return n
