"""Docker container lifecycle management for Docker-based MCPs.

Provides start/stop/restart/status operations for MCP containers
defined via docker-compose.yml in their manifest.
"""

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import selectors
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import yaml

import config
from core.config import deployment
from services.mcp import mcp_manifest_types as _mt

logger = logging.getLogger("otodock.docker-manager")

# The container status vocabulary, named once (core-seams phase 8; the
# dashboard mirror is ``lib/status/docker.ts``): the six words
# ``get_container_status`` returns, plus the two the admin list route
# stamps on a manifest it did not probe (``not_checked`` — the MCP is not
# enabled; ``unknown`` — the probe raised). The enable route's own
# ``docker_status`` answer (``started`` / ``failed``) is beside them.
RUNNING = "running"
UNHEALTHY = "unhealthy"
STARTING = "starting"
STOPPED = "stopped"
NOT_FOUND = "not_found"
ERROR = "error"
NOT_CHECKED = "not_checked"
UNKNOWN = "unknown"
STATUSES: frozenset[str] = frozenset({RUNNING, UNHEALTHY, STARTING, STOPPED, NOT_FOUND, ERROR,
                                      NOT_CHECKED, UNKNOWN})
#: A container process exists (healthy or not): a recreate reaches it.
PRESENT: frozenset[str] = frozenset({RUNNING, STARTING, UNHEALTHY})
#: Running or coming up: no start is needed.
STARTED: frozenset[str] = frozenset({RUNNING, STARTING})
ENABLE_STARTED = "started"
ENABLE_FAILED = "failed"

# First boot pulls multi-GB images (e.g. Collabora for file-tools) — surface
# that instead of sitting silent for minutes. Lines matching this get logged
# (throttled) while a compose subprocess streams.
_PROGRESS_RE = re.compile(
    r"\b(Pulling|Pulled|Pull complete|Downloading|Download complete|"
    r"Extracting|Building|Built|Waiting)\b",
    re.IGNORECASE,
)

_DOCKER_PERM_HINT = (
    "cannot access the Docker daemon socket (permission denied). The user "
    "running the proxy is not in the 'docker' group — after dev-setup.sh adds "
    "you, log out and back in (or `newgrp docker`), then restart the proxy. "
    "Until then the proxy runs WITHOUT its Docker MCPs (file-tools etc.)."
)
_docker_perm_logged = False


def _is_docker_perm_denied(stderr: str) -> bool:
    low = (stderr or "").lower()
    return "permission denied" in low and (
        "docker.sock" in low or "docker daemon socket" in low
    )


def _log_docker_perm_denied(context: str) -> None:
    """ERROR once per boot — this used to be an easy-to-miss warning while the
    proxy silently booted without its Docker MCPs."""
    global _docker_perm_logged
    if _docker_perm_logged:
        return
    _docker_perm_logged = True
    logger.error("%s: %s", context, _DOCKER_PERM_HINT)


class ComposeStalled(subprocess.TimeoutExpired):
    """A build that printed nothing for its stall bound (the buildkit session
    that died with compose still waiting on it)."""


class ComposeEnded(subprocess.TimeoutExpired):
    """A run the proxy's shutdown ended, or one it refused to start."""


@dataclasses.dataclass
class _LiveRun:
    proc: subprocess.Popen
    #: compose reached the containers (a recreate stops the old one, creates
    #: the new one under a temporary name, renames it): cut there, a
    #: container is left under that name.
    containers: bool = False


# A compose run is its own process group (compose and its buildx child); the
# live ones are ended at shutdown, so a stop never waits on a build. Once
# the shutdown set ``_stopping`` (under the lock, so a run either registered
# before it or sees it) no run starts and no stalled build is retried.
_live_groups: dict[int, _LiveRun] = {}
_live_lock = threading.Lock()
_stopping = threading.Event()
# How long a group gets between SIGTERM and SIGKILL, and the reader's poll.
_TERM_GRACE_S = 5.0
_READ_POLL_S = 1.0
# How long the shutdown lets a run that reached the containers finish.
_RECREATE_GRACE_S = 30.0
# Compose's line for a step on a container ("Container <name>  Recreate",
# a mark in front of it on a terminal).
_CONTAINER_STEP_RE = re.compile(
    rb"^[^A-Za-z]*Container\s+\S+\s+(Recreat|Creat|Start|Stop|Remov|Renam)", re.IGNORECASE)
# A container a cut recreate left under compose's temporary name.
_TEMP_NAME_RE = re.compile(r"^[0-9a-f]{12}_(.+)$")


def _exited(proc: subprocess.Popen) -> bool:
    """Whether the leader exited, WITHOUT reaping it: while its zombie holds
    the pid, the group id cannot be reused by another process."""
    if proc.returncode is not None:
        return True
    try:
        return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _end_group(proc: subprocess.Popen) -> None:
    """SIGTERM to the compose run's process group, SIGKILL after the grace
    (to the rest of the group too: a buildx child that outlived compose),
    then the leader reaped."""
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_S
    while time.monotonic() < deadline and not _exited(proc):
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_TERM_GRACE_S)


def _end_at_stop(run: _LiveRun) -> None:
    """A run that reached the containers gets ``_RECREATE_GRACE_S`` to finish
    (a recreate cut there leaves a container under a temporary name); a
    build is ended at once."""
    if run.containers:
        deadline = time.monotonic() + _RECREATE_GRACE_S
        while time.monotonic() < deadline and not _exited(run.proc):
            time.sleep(0.2)
    _end_group(run.proc)


def end_running_compose() -> int:
    """Start no compose run any more and end every one still going (the
    proxy's shutdown): a project the boot has not reached returns at once, a
    stalled build is not started again, a run recreating containers gets a
    short grace. Returns how many were ended."""
    with _live_lock:
        _stopping.set()
        runs = list(_live_groups.values())
    if runs:
        with ThreadPoolExecutor(max_workers=len(runs)) as pool:
            list(pool.map(_end_at_stop, runs))
    return len(runs)


def _run_compose_streaming(
    manifest, cmd: list[str], *, timeout: int, cwd: str | None = None,
    stall: float | None = None,
) -> tuple[int, str]:
    """Run a compose command, streaming progress lines into the log.

    Replaces ``subprocess.run(capture_output=True)`` for the long compose ops:
    that swallowed pull/build progress entirely, so a fresh install read as a
    hang. stderr (compose writes progress AND errors there) is buffered in
    full for the callers' overlap/permission checks; progress-shaped lines log
    at INFO throttled to ~one per 5s. stdout is discarded — no caller of the
    long ops reads it.

    The run is its own process group, read through a poll on the descriptor
    (a child that keeps the pipe open never blocks the reader). At
    ``timeout``, or after ``stall`` seconds without a byte of output, the
    group is ended and ``subprocess.TimeoutExpired`` (``ComposeStalled`` for
    a stall) is raised, matching the old ``subprocess.run`` contract; a run
    the shutdown ended, or would have started after it, raises
    ``ComposeEnded``. The exception's ``recreating`` says whether compose had
    reached the containers.
    """
    with _live_lock:
        if _stopping.is_set():
            raise ComposeEnded(cmd, 0)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            cwd=cwd, env=_compose_env(), start_new_session=True,
        )
        run = _LiveRun(proc)
        _live_groups[proc.pid] = run
    assert proc.stderr is not None
    fd = proc.stderr.fileno()
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    buf = bytearray()
    pending = b""
    started = last_byte = time.monotonic()
    last_log = 0.0
    ended: type[subprocess.TimeoutExpired] | None = None
    try:
        while True:
            now = time.monotonic()
            if now - started >= timeout:
                ended = subprocess.TimeoutExpired
            elif stall is not None and now - last_byte >= stall:
                ended = ComposeStalled
            if ended is not None:
                _end_group(proc)
                break
            if not sel.select(_READ_POLL_S):
                if proc.poll() is not None and not sel.select(0):
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            last_byte = time.monotonic()
            *done, pending = (pending + chunk).replace(b"\r", b"\n").split(b"\n")
            for raw in done:
                if not run.containers and _CONTAINER_STEP_RE.search(raw):
                    run.containers = True
                stripped = raw.decode("utf-8", "replace").strip()
                if stripped and _PROGRESS_RE.search(stripped) and last_byte - last_log >= 5.0:
                    logger.info("%s: %s", manifest.name, stripped[:200])
                    last_log = last_byte
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_TERM_GRACE_S)
    finally:
        sel.close()
        proc.stderr.close()
        with _live_lock:
            _live_groups.pop(proc.pid, None)
    if ended is None and _stopping.is_set() and proc.returncode != 0:
        ended = ComposeEnded
    if ended is not None:
        exc = ended(cmd, stall if ended is ComposeStalled and stall is not None else timeout)
        exc.recreating = run.containers  # type: ignore[attr-defined]  # read by _compose_up
        raise exc
    return proc.returncode if proc.returncode is not None else -1, buf.decode("utf-8", "replace")


def _project_name(manifest) -> str:
    """Namespaced compose project for a Docker MCP — ``otodock-<install_id>-mcp-
    <name>``. The install-id keeps it unique vs the operator's OWN compose
    projects AND vs a second OtoDock install on the same daemon; without it
    Compose derives the project from the folder name (a bare ``camoufox``), which
    collides. Used for every compose op so start/stop/restart/down/ps all target
    the same containers.
    """
    return f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}".lower()


def _compose_cmd(manifest, *args: str) -> list[str]:
    """Build docker compose command for a manifest's compose file under the
    namespaced project (see ``_project_name``).

    On bare-metal (T1) a generated ``docker-compose.override.yml`` (subnet pin +
    namespaced container_name + image tag — see
    ``compose_rewrite.ensure_t1_override``) is merged in with a second ``-f`` when
    present, so every op (up/down/ps/pull/restart) sees the same effective config.
    """
    compose_file = manifest.mcp_dir / manifest.server.docker_compose
    cmd = [
        "docker", "compose", "-p", _project_name(manifest), "-f", str(compose_file),
    ]
    override = manifest.mcp_dir / "docker-compose.override.yml"
    if override.is_file():
        cmd += ["-f", str(override)]
    return [*cmd, *args]


def _compose_env() -> dict[str, str]:
    """Environment for ``docker compose`` subprocesses.

    Bare-metal (T1): the current environment unchanged — ``docker compose``
    talks to the host's local ``/var/run/docker.sock``. Docker-Compose (T2):
    the same environment plus ``DOCKER_HOST=tcp://docker-socket-proxy:2375`` so
    the containerised proxy drives the daemon through the read-restricted
    socket-proxy (it has no local socket). ``deployment.docker_subprocess_env``
    returns ``{}`` on bare-metal, so this is a no-op there.
    """
    return {**os.environ, **deployment.docker_subprocess_env()}


def get_container_status(manifest) -> str:
    """Check Docker container status.

    Returns: 'running', 'unhealthy', 'starting', 'stopped', 'not_found', 'error'.
    A container that is up but failing its healthcheck reports 'unhealthy' (e.g. a
    wedged camoufox whose HTTP is up but `page.goto` hangs) so the dashboard shows
    it amber instead of green — the proxy never auto-restarts Docker MCPs.
    """
    return container_state(manifest)[0]


def normalize_image(ref: str) -> str:
    """An image reference as Docker resolves it, for comparing two of them: an
    implicit ``:latest`` added, the Docker Hub registry and ``library/``
    prefixes dropped."""
    ref = (ref or "").strip()
    if "@" not in ref and ":" not in ref.rsplit("/", 1)[-1]:
        ref += ":latest"
    for prefix in ("docker.io/", "index.docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    if ref.startswith("library/"):
        ref = ref[len("library/"):]
    return ref


def image_stale(manifest, running_images: list[str]) -> bool:
    """Whether the running container serves another image than the one the
    manifest names (an update whose recreate did not happen): a restart
    switches it. No image named, or nothing running: never stale."""
    want = getattr(getattr(manifest, "server", None), "image", "") or ""
    if not want or not running_images:
        return False
    return normalize_image(want) not in {normalize_image(i) for i in running_images}


def container_state(manifest) -> tuple[str, list[str]]:
    """``get_container_status``'s answer and the images of the MCP's running
    containers (one ``compose ps`` call). The MCP's own service decides
    (``_target_service``): a sidecar's health check never does (Collabora's
    start period would read as the file tools starting), but a sidecar that is
    down and did not finish cleanly reads the project as stopped, so the
    boot's ``up -d`` brings it back. A project whose own service cannot be
    named is judged by its first container."""
    try:
        result = subprocess.run(
            _compose_cmd(manifest, "ps", "--format", "json", "-a"),
            capture_output=True, text=True, timeout=15,
            env=_compose_env(),
        )
        if result.returncode != 0:
            if _is_docker_perm_denied(result.stderr):
                _log_docker_perm_denied(
                    f"Docker status check for {manifest.name} failed"
                )
            return NOT_FOUND, []

        output = result.stdout.strip()
        if not output:
            return NOT_FOUND, []

        # docker compose ps --format json outputs one JSON object per line
        # (the State / Health words are Docker's, not ours).
        import json
        rows = []
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        target = _target_service(manifest)
        if target is None or not all("Service" in c for c in rows):
            status, images = _judge_rows(rows)
            return status or NOT_FOUND, images
        status, images = _judge_rows([c for c in rows if c.get("Service") == target])
        if status in (RUNNING, STARTING, UNHEALTHY) and any(
                _sidecar_down(c) for c in rows if c.get("Service") != target):
            status = STOPPED
        return status or NOT_FOUND, images
    except subprocess.TimeoutExpired:
        logger.warning("Timeout checking Docker status for %s", manifest.name)
        return ERROR, []
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Error checking Docker status for %s: %s", manifest.name, e)
        return ERROR, []


def _judge_rows(rows: list) -> tuple[str | None, list[str]]:
    """The first running container's state (its health word when the image
    declares a check), or stopped when one before it is down; and the images
    of the running ones. ``None`` when no row says either."""
    status = None
    images: list[str] = []
    for container in rows:
        state = container.get("State", "").lower()
        if state == "running":
            if container.get("Image"):
                images.append(str(container["Image"]))
            if status is None:
                # `Health` is "" when the image declares no healthcheck.
                health = (container.get("Health") or "").lower()
                status = (UNHEALTHY if health == "unhealthy"
                          else STARTING if health == "starting" else RUNNING)
        elif state in ("exited", "dead", "created") and status is None:
            status = STOPPED
    return status, images


def _sidecar_down(container: dict) -> bool:
    """A container of another service of the project that is not running and
    did not end cleanly: a one-shot that exited 0 has done its work."""
    if _judge_rows([container])[0] != STOPPED:
        return False
    return bool(container.get("ExitCode")) or container.get("State", "").lower() != "exited"


def _target_service(manifest) -> str | None:
    """The compose service the MCP itself runs as, the one the platform's
    compose handling picks: on bare metal the base compose's target (the
    T1 override's pick), under Docker-Compose the pull form's. ``None`` when
    the compose cannot be read or names no single one."""
    from services.mcp import compose_rewrite
    try:
        data = yaml.safe_load((manifest.mcp_dir / manifest.server.docker_compose).read_text()) or {}
        services = data.get("services")
        if not isinstance(services, dict) or not services:
            return None
        if deployment.in_docker_compose():
            return compose_rewrite._pick_pull_target(services, deployment.mcp_service_name(manifest))
        return compose_rewrite._pick_target_service(
            services, manifest.server.service_name or manifest.name)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return None


# A volume ``subpath`` mount (one file of a volume) needs Docker Engine 26,
# API 1.45: an older engine mounts the whole volume, and the service misses
# the file it expects (the file tools' Collabora runs without its WOPI key).
_SUBPATH_MIN_API = (1, 45)


def _compose_mounts_subpath(manifest) -> bool:
    """Whether the MCP's compose mounts one file of a volume."""
    try:
        data = yaml.safe_load((manifest.mcp_dir / manifest.server.docker_compose).read_text()) or {}
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return False
    services = data.get("services") if isinstance(data, dict) else None
    for svc in (services.values() if isinstance(services, dict) else ()):
        for vol in ((svc.get("volumes") or []) if isinstance(svc, dict) else ()):
            spec = vol.get("volume") if isinstance(vol, dict) else None
            if isinstance(spec, dict) and spec.get("subpath"):
                return True
    return False


def _engine_api_version() -> tuple[int, ...] | None:
    """The Docker Engine's API version, ``None`` when it cannot be read."""
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.APIVersion}}"],
            capture_output=True, text=True, timeout=15, env=_compose_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return tuple(int(part) for part in result.stdout.strip().split("."))
    except ValueError:
        return None


def _check_engine_for_subpath(manifest, engine_api: list) -> None:
    """Log the cause when the MCP's compose mounts a volume subpath and the
    engine is older than Docker Engine 26. The engine is asked once per boot
    (``engine_api`` holds its answer) and only for such a compose."""
    if not _compose_mounts_subpath(manifest):
        return
    if not engine_api:
        engine_api.append(_engine_api_version())
    api = engine_api[0]
    if api is not None and api < _SUBPATH_MIN_API:
        logger.warning(
            "Docker MCP %s: its compose mounts one file of a volume, which needs "
            "Docker Engine 26 or later (API 1.45); this engine speaks API %s and "
            "mounts the whole volume instead. Update Docker, then restart OtoDock.",
            manifest.name, ".".join(str(part) for part in api),
        )


def _odd_backslash_run_before_quote(value: str) -> bool:
    """Whether an odd run of backslashes ends ``value`` or precedes a ``'``
    in it (one pass; a regex over a long run of backslashes backtracks)."""
    run = 0
    for ch in value:
        if ch == "\\":
            run += 1
            continue
        if ch == "'" and run % 2:
            return True
        run = 0
    return bool(run % 2)


def dotenv_quote(value: str) -> str | None:
    """``value`` as a single-quoted ``.env`` value Compose's dotenv reader
    hands the container literally: no ``$`` interpolation, backslashes kept,
    each ``'`` written ``\\'``. ``None`` for a value that form cannot carry: a
    control character (a newline ends the line), or an odd run of
    backslashes right before a ``'`` or at the end (Compose reads the quote
    after an odd run as escaped)."""
    if any(ord(c) < 0x20 or c == "\x7f" for c in value):
        return None
    if _odd_backslash_run_before_quote(value):
        return None
    return "'" + value.replace("'", "\\'") + "'"


def _inject_mcp_env(manifest) -> bool:
    """Generate Docker MCP `.env` from manifest declarations + admin secrets.

    Returns True if the .env file content changed (or was created/deleted),
    False if no change. Callers can use this to force-recreate the container
    so it picks up the new env (compose otherwise leaves a running container
    untouched on `up -d` when only env values change).

    Four sources merged in priority order (latest wins):
      1. Existing non-declared keys in `.env` (escape hatch for compose-time
         one-offs — keys not declared anywhere else stay put).
      2. ``manifest.env`` + ``manifest.agent_env`` resolved via the shared
         template resolver (``${platform.*}`` tokens, etc.).
      3. The ``config`` rows (``mcp_registry.effective_config``: saved, else
         the manifest's default), single-quoted.
      4. ``infra_credentials`` rows for this MCP — admin-set secrets like
         ESPOCRM_API_KEY land here so Docker containers receive them at
         startup without hand-editing ``.env``.

    Called on every proxy startup AND every container start (and on install/
    update via the API). Idempotent.
    """
    from services.mcp import mcp_registry
    from storage.identity import credential_store

    env_path = manifest.mcp_dir / ".env"

    # Preserve any existing non-declared keys (escape hatch).
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, v = stripped.split("=", 1)
                existing[k] = v

    # Resolve manifest-declared values via the shared template resolver. We
    # merge `env` and `agent_env` — for Docker MCPs, both end up in `.env`
    # because containers don't have per-session context (they serve many
    # sessions; per-call session_id flows via HTTP query string instead).
    # Secret template tokens (_SECRET_TEMPLATE_TOKENS — empty today) must NEVER
    # be baked into a Docker MCP's .env: that file lives in the container's
    # context and would leak the secret to a (possibly untrusted) container.
    # The per-session credential broker is the only path for secrets — Docker
    # callbacks authenticate with the per-session JWT, not a baked secret.
    def _safe_resolve(key: str, raw: str) -> str | None:
        if any(tok in str(raw) for tok in mcp_registry._SECRET_TEMPLATE_TOKENS):
            logger.warning(
                "Refusing to write secret template %s into Docker .env for %s",
                key, manifest.name,
            )
            return None
        return mcp_registry._resolve_template(raw, manifest)

    declared: dict[str, str] = {}
    for k, v in (manifest.env or {}).items():
        resolved = _safe_resolve(k, v)
        if resolved is not None:
            declared[k] = resolved
    for k, v in (manifest.agent_env or {}).items():
        resolved = _safe_resolve(k, v)
        if resolved is not None:
            declared[k] = resolved

    # Admin-set infra credentials (Docker MCPs only — stdio MCPs get these
    # via env_builder per session). The credential keys are declared in
    # ``manifest.credentials.fields`` so the admin UI knows what to ask for;
    # at startup we pull the saved values out of the encrypted store.
    infra_creds: dict[str, str] = {}
    if manifest.credentials.type == "infra":
        try:
            infra_creds = credential_store.get_infra_credentials(manifest.name) or {}
        except psycopg.Error as e:
            logger.warning(
                "Failed to load infra credentials for %s: %s",
                manifest.name, e,
            )

    # The MCP's config rows (the admin's saved value, else the manifest's
    # default), single-quoted for Compose's dotenv reader: an admin's text is
    # handed over as typed. A value that form cannot carry is left out.
    config_vals: dict[str, str] = {}
    for key, value in mcp_registry.effective_config(manifest).items():
        quoted = dotenv_quote(value) if value else ""
        if quoted is None:
            logger.warning(
                "Config %s of %s holds a character its .env cannot carry; left out",
                key, manifest.name,
            )
            existing.pop(key, None)  # never the value an earlier write left
            continue
        config_vals[key] = quoted

    # Priority order: existing < manifest-declared < config < infra credentials.
    # Admin secrets win over declarations (so an ESPOCRM_URL admin set in the
    # dashboard overrides any default the manifest happens to ship), as an
    # admin's config value wins over a manifest env default.
    final = {**existing, **declared, **config_vals, **infra_creds}

    # Generic tool filter: for MCPs that declare `tool_filter`, two variables.
    # `regex_env_var_name` (default ENABLED_TOOLS_REGEX) holds the admin's
    # regex bare, single-quoted for Compose's dotenv reader so the container
    # sees it byte for byte: a server reads it from its env, or a shell
    # entrypoint passes "$ENABLED_TOOLS_REGEX" quoted. `env_var_name`
    # (default ENABLED_TOOLS_FLAG) keeps the composed flag
    # (`--enabled-tools '<regex>'`) for an entrypoint that expands it; an
    # unquoted expansion keeps those quotes, so a new compose reads the bare
    # one. Both are emptied with no filter, so a cleared regex leaves no stale
    # flag. A regex the dotenv form cannot carry (a control character, a
    # trailing backslash; the save refuses both) empties both.
    if manifest.tool_filter is not None:
        tf = mcp_registry.get_tool_filter(manifest.name)
        regex = tf[1] if tf is not None else ""
        quoted = dotenv_quote(regex) if regex else ""
        if quoted is None:
            logger.warning(
                "The tool filter of %s holds a character its .env cannot carry; "
                "the MCP runs unfiltered until the regex is saved again", manifest.name,
            )
            regex, quoted = "", ""
        final[manifest.tool_filter.env_var_name] = (
            f"{tf[0]} '{regex}'" if regex and tf is not None else "")
        final[manifest.tool_filter.regex_env_var_name] = quoted

    if not final:
        # No declarations + no preserved keys: don't litter the MCP folder with
        # an empty .env (community MCPs that need no platform vars stay clean).
        if env_path.exists():
            env_path.unlink()
            logger.debug("No env declared for %s — .env removed", manifest.name)
            return True
        logger.debug("No env declared for %s — .env not created", manifest.name)
        return False

    new_content = "\n".join(f"{k}={v}" for k, v in final.items()) + "\n"
    old_content = env_path.read_text() if env_path.exists() else None
    if old_content == new_content:
        return False
    env_path.write_text(new_content)
    logger.debug(
        "Wrote %d declared + %d infra keys + preserved %d existing keys to %s/.env",
        len(declared),
        len(infra_creds),
        len(set(existing) - set(declared) - set(infra_creds)),
        manifest.name,
    )
    return True


# T1 recreate-trigger state: the compose-config hash the running container
# was (re)created from, stored beside the generated `.env` (gitignored).
_COMPOSE_HASH_NAME = ".otodock-compose-hash"


def _compose_config_hash(manifest) -> str | None:
    """sha256 over the on-disk compose config (base + generated override).

    Raw file text, not ``docker compose config``: the interpolation inputs
    that matter live in the generated ``.env``, whose changes the existing
    ``env_changed`` trigger already catches, and default-bound changes
    (``OTODOCK_MCP_DEFAULT_MEM_LIMIT``) materialize in the override TEXT via
    ``ensure_t1_override``. The one blind spot — interpolation straight from
    the proxy's process env — is accepted (documented in
    DOCKER-DEPLOYMENT.md). Filenames are part of the digest, so a renamed
    compose file forces one recreate (harmless). Returns None when the base
    compose is missing/unreadable — callers degrade to the plain skip,
    never raise.
    """
    try:
        base = manifest.mcp_dir / manifest.server.docker_compose
        if not base.is_file():
            return None
        h = hashlib.sha256()
        for p in (base, manifest.mcp_dir / "docker-compose.override.yml"):
            if p.is_file():
                h.update(p.name.encode())
                h.update(b"\0")
                h.update(p.read_bytes())
                h.update(b"\0")
        return h.hexdigest()
    except OSError as e:
        logger.debug("Compose hash for %s unavailable: %s", manifest.name, e)
        return None


def _read_hash_file(manifest) -> tuple[str | None, dict | None]:
    """The hash file's two lines: the compose hash, and the build record
    (JSON) a successful ``up --build`` wrote. A file from before the build
    record has one line."""
    try:
        lines = (manifest.mcp_dir / _COMPOSE_HASH_NAME).read_text().splitlines()
    except OSError:
        return None, None
    compose = lines[0].strip() if lines else ""
    record = None
    if len(lines) > 1:
        with contextlib.suppress(ValueError):
            parsed = json.loads(lines[1])
            record = parsed if isinstance(parsed, dict) else None
    return compose or None, record


def _write_hash_file(manifest, *, compose: str | None = None, record: dict | None = None) -> None:
    """Rewrite the hash file, keeping the line the call does not set."""
    old_compose, old_record = _read_hash_file(manifest)
    compose = compose or old_compose or ""
    record = record if record is not None else old_record
    text = compose + "\n" + (json.dumps(record, sort_keys=True) + "\n" if record else "")
    (manifest.mcp_dir / _COMPOSE_HASH_NAME).write_text(text)


def _read_recorded_compose_hash(manifest) -> str | None:
    return _read_hash_file(manifest)[0]


def _record_compose_hash(manifest) -> None:
    """Record the CURRENT on-disk config hash — recomputed here, never a
    precomputed value: the subnet-TOCTOU retry rewrites the override
    mid-``compose up``, and recording a stale hash would force a spurious
    recreate on the next boot. T1-only: ``start_container`` also runs on T2
    (community install / admin API paths) where the file would be litter in
    dirs nothing ever reads it from. Never raises — recording is
    bookkeeping, and an IO error here must not turn a successful start into
    a failure."""
    if deployment.current_mode() != deployment.MANAGED_LOCAL:
        return
    try:
        digest = _compose_config_hash(manifest)
        if digest:
            _write_hash_file(manifest, compose=digest)
    except OSError as e:
        logger.warning("Could not record compose hash for %s: %s", manifest.name, e)


# What a build context's hash leaves out: folders the platform or a tool
# makes there, and the files the platform writes beside the compose.
_BUILD_SKIP_DIRS = frozenset({"venv", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".git"})
_BUILD_SKIP_FILES = frozenset({".env", _COMPOSE_HASH_NAME, "docker-compose.override.yml"})
# A context larger than this is not hashed: its builds always run.
_BUILD_HASH_MAX_BYTES = 256 * 1024 * 1024
# A build up that prints nothing this long has stalled (the file tools'
# quietest step, a 176 MB model download, takes minutes on a slow link).
_BUILD_STALL_S = 600.0


def _hash_tree(h, root: str, budget: list[int]) -> bool:
    """Feed every regular file under ``root`` (sorted, the skipped names
    left out, links by their target text) into ``h``. False once the bytes
    read pass ``budget``."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _BUILD_SKIP_DIRS)
        for name in sorted(filenames):
            if name in _BUILD_SKIP_FILES:
                continue
            path = os.path.join(dirpath, name)
            h.update(os.path.relpath(path, root).encode("utf-8", "surrogateescape") + b"\0")
            if os.path.islink(path):
                h.update(b"link:" + os.readlink(path).encode("utf-8", "surrogateescape") + b"\0")
                continue
            size = os.path.getsize(path)
            if size > budget[0]:
                return False
            budget[0] -= size
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            h.update(b"\0")
    return True


def _build_state(manifest) -> tuple[str, list[str]] | None:
    """The hash of what the MCP's builds are made from and the images they
    make: every ``build:`` service of the resolved compose (``docker compose
    config``, so the context, the dockerfile and the args are what compose
    uses), its context's files and its dockerfile. None when a build cannot
    be judged from local files (a git context, ``additional_contexts``, an
    inline dockerfile, a context past the size cap) or compose cannot say."""
    try:
        result = subprocess.run(
            _compose_cmd(manifest, "config", "--format", "json"),
            capture_output=True, text=True, timeout=30,
            cwd=str(manifest.mcp_dir), env=_compose_env(),
        )
        if result.returncode != 0:
            return None
        services = (json.loads(result.stdout) or {}).get("services") or {}
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None
    if not isinstance(services, dict):
        return None
    h = hashlib.sha256()
    images: list[str] = []
    budget = [_BUILD_HASH_MAX_BYTES]
    for name in sorted(services):
        svc = services[name]
        build = svc.get("build") if isinstance(svc, dict) else None
        if not isinstance(build, dict):
            continue
        context = str(build.get("context") or "")
        if (not context or not os.path.isdir(context) or build.get("additional_contexts")
                or build.get("dockerfile_inline")):
            return None
        h.update(name.encode() + b"\0" + json.dumps(build, sort_keys=True).encode() + b"\0")
        try:
            if not _hash_tree(h, context, budget):
                return None
            dockerfile = os.path.join(context, str(build.get("dockerfile") or "Dockerfile"))
            if not os.path.realpath(dockerfile).startswith(os.path.realpath(context) + os.sep):
                with open(dockerfile, "rb") as f:
                    h.update(b"dockerfile\0" + f.read() + b"\0")
        except OSError:
            return None
        images.append(str(svc.get("image") or f"{_project_name(manifest)}-{name}"))
    if not images:
        return None
    return h.hexdigest(), images


def _image_id(image: str) -> str | None:
    """The local image's ID, None when the daemon has no such image."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=30, env=_compose_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _build_is_current(manifest, state: tuple[str, list[str]] | None) -> bool:
    """Whether the images the last ``up --build`` recorded were built from
    what the build would read today and are still the images on this host
    (an image rebuilt or retagged by hand has another ID)."""
    if state is None:
        return False
    record = _read_hash_file(manifest)[1]
    if not record or record.get("hash") != state[0]:
        return False
    recorded = record.get("images")
    if not isinstance(recorded, dict) or sorted(recorded) != sorted(state[1]):
        return False
    return all(_image_id(image) == recorded[image] for image in state[1])


def _record_build(manifest, state: tuple[str, list[str]] | None) -> None:
    """After a successful ``up --build``: the hash computed BEFORE the build
    (a file changed during it reads as changed next time) and the images'
    IDs. T1-only, never raises."""
    if state is None or deployment.current_mode() != deployment.MANAGED_LOCAL:
        return
    ids = {image: _image_id(image) for image in state[1]}
    if not all(ids.values()):
        return
    try:
        _write_hash_file(manifest, record={"hash": state[0], "images": ids})
    except OSError as e:
        logger.warning("Could not record the build of %s: %s", manifest.name, e)


def start_container(manifest, *, force_recreate: bool = False) -> bool:
    """Start Docker container via docker compose up -d.

    When ``force_recreate`` is True, passes ``--force-recreate`` so a running
    container is rebuilt with the current ``.env``. Compose's plain ``up -d``
    leaves a running container untouched when only env values change, so
    callers that just wrote a new ``.env`` should pass force_recreate=True.

    The image is actively refreshed before (re)creating — a plain ``up -d``
    is not enough after an update: it only fetches/builds a MISSING image, so
    a same-tag rebuild or a moved tag would keep the stale local image.
    Topology-aware:
      * T2 (pull-form compose): ``docker compose pull`` the (possibly retagged)
        ``server.image`` first.
      * T1 (build-form compose): pull the canonical prebuilt image, falling
        back to ``--build`` from the Dockerfile when the image is absent or
        the dev build-local flag is set (only the bare-metal local daemon can
        build; the T2 socket-proxy blocks it).
    """
    try:
        _inject_mcp_env(manifest)
        from services.mcp import compose_rewrite
        in_t2 = deployment.in_docker_compose()

        if in_t2:
            # T2: rewrite the catalog's build-from-context compose to pull the
            # pre-built image (the socket-proxy blocks builds). Raises with an
            # actionable message if the MCP has no `server.image` to pull — fail
            # rather than let `compose up` attempt a build the socket-proxy 403s.
            try:
                compose_rewrite.ensure_pull_compose(manifest)
            except ValueError as e:
                logger.error("Cannot start Docker MCP %s: %s", manifest.name, e)
                return False
        else:
            # T1: generate/refresh the override (subnet pin + namespaced
            # container_name + image tag) BEFORE any compose op, so the bridge is
            # pinned to our pool and can never auto-grab a 192.168.x LAN overlap.
            # Best-effort: a parse failure just degrades to docker's auto-bridge.
            compose_rewrite.ensure_t1_override(manifest)

        args = ["up", "-d"]
        if force_recreate:
            args.append("--force-recreate")

        # ---- T2: always pull (build blocked), then up (fast). ----
        if in_t2:
            pull_rc, pull_err = _run_compose_streaming(
                manifest, _compose_cmd(manifest, "pull"),
                timeout=900, cwd=str(manifest.mcp_dir),
            )
            if pull_rc != 0:
                logger.warning(
                    "compose pull for %s failed (cached image if present): %s",
                    manifest.name, pull_err[:300],
                )
            return _compose_up(manifest, args, timeout=120)

        # ---- T1: prefer the canonical prebuilt image; build is the fallback. ----
        srv_image = getattr(manifest.server, "image", "") or ""
        build_local = config.OTODOCK_MCP_BUILD_LOCAL or not srv_image
        if build_local:
            # No published image (or the dev build-local flag) → build from the
            # Dockerfile when its sources changed.
            return _build_up(manifest, args)

        # Pull the canonical image; if it isn't present afterwards (unpublished
        # or a failed pull), self-heal by building locally (the base keeps
        # `build:`). An update re-pulls a moved same-tag digest for free.
        pull_rc, pull_err = _run_compose_streaming(
            manifest, _compose_cmd(manifest, "pull"),
            timeout=900, cwd=str(manifest.mcp_dir),
        )
        if pull_rc != 0:
            logger.warning("compose pull for %s failed: %s", manifest.name, pull_err[:300])
        present = subprocess.run(
            ["docker", "image", "inspect", srv_image],
            capture_output=True, text=True, timeout=30, env=_compose_env(),
        ).returncode == 0
        if not present:
            logger.info(
                "%s: image %s absent after pull — building locally", manifest.name, srv_image,
            )
            return _build_up(manifest, args)
        return _compose_up(manifest, args, timeout=120, t1_retry=True)
    except ComposeEnded:
        logger.info("Docker MCP %s: not started, the proxy is stopping", manifest.name)
        return False
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError, psycopg.Error) as e:
        logger.error("Error starting Docker MCP %s: %s", manifest.name, e)
        return False


def _build_up(manifest, args: list[str]) -> bool:
    """An up for an MCP whose image compose builds here: with ``--no-build``
    when the last build is still current (the hash of its sources and the
    image's ID decide, so a boot never builds for nothing), else
    ``--build``, recorded once it succeeded."""
    state = _build_state(manifest)
    if _build_is_current(manifest, state):
        logger.info("%s: its image is current, no build", manifest.name)
        return _compose_up(manifest, [*args, "--no-build"], timeout=120, t1_retry=True)
    ok = _compose_up(manifest, [*args, "--build"], timeout=900, t1_retry=True)
    if ok:
        _record_build(manifest, state)
    return ok


def _run_up(manifest, args: list[str], timeout: int) -> tuple[int, str]:
    """One ``compose up``. A build that stalls (``_BUILD_STALL_S`` without
    output) or runs past ``timeout`` is ended with its process group and
    started once more, in a new build session: by hand that worked every
    time the boot's build hung. A second one raises."""
    cmd = _compose_cmd(manifest, *args)
    building = "--build" in args
    stall = _BUILD_STALL_S if building else None
    first = ""
    for attempt in (1, 2):
        try:
            return _run_compose_streaming(
                manifest, cmd, timeout=timeout, cwd=str(manifest.mcp_dir), stall=stall)
        except subprocess.TimeoutExpired as e:
            if attempt == 2:
                e.prior = first  # type: ignore[attr-defined]  # read by _compose_up
            if (not building or attempt == 2 or isinstance(e, ComposeEnded) or _stopping.is_set()
                    or getattr(e, "recreating", False)):
                raise
            first = _ended(e)
            logger.warning("%s: the build %s; starting it once more", manifest.name, first)
    raise AssertionError("unreachable")


def _ended(e: subprocess.TimeoutExpired) -> str:
    if isinstance(e, ComposeStalled):
        return f"printed nothing for {e.timeout:.0f} s"
    return f"ran past {e.timeout:.0f} s"


def _settle_interrupted_recreate(manifest) -> str:
    """A recreate cut short leaves the new container under compose's
    temporary name ``<id>_<name>``: renamed back when the old one is gone,
    else the way out named (the old container still holds the name).
    Returns the sentence for the log."""
    project = _project_name(manifest)
    try:
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15, env=_compose_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return "its containers could not be listed"
    names = set(listed.stdout.split()) if listed.returncode == 0 else set()
    notes: list[str] = []
    for name in sorted(names):
        m = _TEMP_NAME_RE.match(name)
        if not m:
            continue
        real = m.group(1)
        if real not in names:
            try:
                renamed = subprocess.run(["docker", "rename", name, real], capture_output=True,
                                         text=True, timeout=15, env=_compose_env()).returncode == 0
            except (OSError, subprocess.SubprocessError):
                renamed = False
            if renamed:
                notes.append(f"{name} renamed back to {real}")
                continue
        notes.append(f"{name} is left beside {real}: docker stop {real}; docker rm {real}; "
                     f"docker rename {name} {real}")
    return "; ".join(notes) or "no container is left under a temporary name"


def _compose_up(manifest, args: list[str], *, timeout: int, t1_retry: bool = False) -> bool:
    """Run ``compose up`` (with the given args), logging the outcome.

    ``t1_retry`` enables the T1 subnet TOCTOU recovery: if the create fails
    because another stack grabbed our pinned /24 between pick-time and
    create-time (stderr mentions "overlap"), reallocate a fresh free /24 into the
    override and retry once. Still failing → return False (the admin sees it).
    A run ended before compose reached the containers (a hung build) leaves
    the running ones as they were; one cut while it recreated them gets its
    temporary-named container renamed back, or the way out logged
    (``_settle_interrupted_recreate``).
    """
    try:
        return _compose_up_attempts(manifest, args, timeout=timeout, t1_retry=t1_retry)
    except ComposeEnded as e:
        if getattr(e, "recreating", False):
            logger.warning("Docker MCP %s: the proxy stopped while compose recreated its "
                           "containers: %s", manifest.name, _settle_interrupted_recreate(manifest))
        else:
            logger.info("Docker MCP %s: not started, the proxy is stopping", manifest.name)
        return False
    except subprocess.TimeoutExpired as e:
        by_hand = " ".join(shlex.quote(part) for part in _compose_cmd(manifest, *args))
        prior = getattr(e, "prior", "")
        if getattr(e, "recreating", False):
            what = (f"compose up {_ended(e)} while it recreated the containers "
                    f"({_settle_interrupted_recreate(manifest)})")
        elif "--build" in args:
            causes = f"{prior}, then {_ended(e)}" if prior and prior != _ended(e) else (
                f"{_ended(e)} twice" if prior else _ended(e))
            what = f"its build {causes}; its running containers are unchanged"
        else:
            what = f"compose up {_ended(e)}"
        logger.error(
            "Failed to start Docker MCP %s: %s, and the next start tries again. "
            "By hand: cd %s && %s",
            manifest.name, what, shlex.quote(str(manifest.mcp_dir)), by_hand,
        )
        return False


def _compose_up_attempts(manifest, args: list[str], *, timeout: int, t1_retry: bool) -> bool:
    logger.info(
        "%s: compose up%s (first boot may pull/build images — can take minutes "
        "on slow links; progress is logged)",
        manifest.name, " --build" if "--build" in args else "",
    )
    rc, stderr = _run_up(manifest, args, timeout)
    if rc == 0:
        logger.info(
            "Started Docker MCP: %s%s",
            manifest.name, " (force-recreated)" if "--force-recreate" in args else "",
        )
        _record_compose_hash(manifest)
        return True

    if t1_retry and "overlap" in stderr.lower():
        logger.warning(
            "%s: subnet overlap on network create — reallocating + retrying: %s",
            manifest.name, stderr[:200],
        )
        from services.mcp import compose_rewrite
        compose_rewrite.ensure_t1_override(manifest, force_realloc=True)
        rc, retry_err = _run_up(manifest, args, timeout)
        if rc == 0:
            logger.info("Started Docker MCP %s after subnet reallocation", manifest.name)
            _record_compose_hash(manifest)
            return True
        stderr = retry_err

    # Name-conflict self-heal: a container holding OUR exact target name is
    # by definition ours (the name embeds this install's id — see
    # _project_name), left behind by a previous deployment generation that
    # compose can no longer see (project labels changed). Remove it and retry
    # once. Containers of a DIFFERENT install-id are never touched — a second
    # OtoDock install on the same daemon legitimately owns those.
    expected_name = f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}".lower()
    if "already in use" in stderr.lower() and expected_name in stderr.lower():
        logger.warning(
            "%s: container name %s is held by a previous deployment "
            "generation — removing it and retrying",
            manifest.name, expected_name,
        )
        try:
            rm = subprocess.run(
                ["docker", "rm", "-f", expected_name],
                capture_output=True, text=True, timeout=60,
                env=deployment.docker_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            rm = None
            logger.warning("%s: stale-container removal failed: %s", manifest.name, e)
        if rm is not None and rm.returncode == 0:
            rc, retry_err = _run_up(manifest, args, timeout)
            if rc == 0:
                logger.info(
                    "Started Docker MCP %s after removing the previous "
                    "generation's container", manifest.name,
                )
                _record_compose_hash(manifest)
                return True
            stderr = retry_err
        elif rm is not None:
            logger.warning(
                "%s: could not remove stale container %s: %s",
                manifest.name, expected_name, (rm.stderr or "")[:200],
            )

    if _is_docker_perm_denied(stderr):
        _log_docker_perm_denied(f"compose up for {manifest.name} failed")
    logger.error("Failed to start Docker MCP %s: %s", manifest.name, stderr[:500])
    return False


def _warn_foreign_mcp_leftovers() -> None:
    """Log MCP containers whose name carries a DIFFERENT install identity.

    Container/project names embed the install-id (``otodock-<id>-mcp-<name>``,
    see ``_project_name``). The id lives in config.env, so a recreated
    config.env (install moved to a new folder) rotates it — the previous
    generation's containers keep running with stale config, invisible to
    every compose op here. They must NOT be auto-removed: a second OtoDock
    install on the same daemon legitimately owns identically-shaped names.
    Name them loudly with the removal command and let the operator decide.
    Best-effort — any failure is silent (T3 has no daemon at all).
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
            env=deployment.docker_subprocess_env(),
        )
        if result.returncode != 0:
            return
        ours = f"otodock-{config.INSTALL_ID}-mcp-"
        foreign = re.compile(r"^otodock-(?:[0-9a-f]+-)?mcp-")
        stale = [
            n for n in result.stdout.split()
            if foreign.match(n) and not n.startswith(ours)
        ]
        if stale:
            logger.warning(
                "%d MCP container(s) carry another OtoDock install identity: "
                "%s. If this daemon hosts ONLY this install, they are "
                "leftovers from a previous config.env (rotated install-id) "
                "still running with stale config — remove them with: "
                "docker rm -f %s",
                len(stale), ", ".join(stale), " ".join(stale),
            )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("foreign-MCP-leftover scan skipped: %s", e)


def stop_container(manifest) -> bool:
    """Stop Docker container via docker compose down."""
    try:
        result = subprocess.run(
            _compose_cmd(manifest, "down"),
            capture_output=True, text=True, timeout=60,
            cwd=str(manifest.mcp_dir),
            env=_compose_env(),
        )
        if result.returncode == 0:
            logger.info("Stopped Docker MCP: %s", manifest.name)
            return True
        logger.error(
            "Failed to stop Docker MCP %s: %s", manifest.name, result.stderr[:500]
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("Error stopping Docker MCP %s: %s", manifest.name, e)
        return False


def remove_image(image: str) -> bool:
    """Best-effort ``docker rmi`` of an image tag — opportunistic disk hygiene
    after a delete (reclaim the MCP's image) or an update (reclaim the previous
    tag the new one replaced, so the daemon doesn't accumulate one stale image
    per update). Never raises: the image may be shared, still in use, or already
    absent, none of which should fail the caller.
    """
    if not image:
        return False
    try:
        r = subprocess.run(
            ["docker", "rmi", image],
            capture_output=True, text=True, timeout=60, env=_compose_env(),
        )
        if r.returncode == 0:
            logger.info("Removed image %s", image)
            return True
        logger.info(
            "Image %s not removed (shared/in-use/absent): %s",
            image, r.stderr.strip()[:200],
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        logger.info("Best-effort rmi of %s skipped: %s", image, e)
        return False


def remove_container(manifest, *, prune_image: bool = True) -> bool:
    """Tear a Docker MCP down completely — used when an MCP is DELETED (not just
    stopped). ``compose down --volumes --remove-orphans`` stops and removes the
    container, its per-MCP named volumes (the rewritten T2 compose mounts data
    in ``otodock-mcp-<name>-*`` volumes) and any orphaned siblings; then a
    best-effort ``docker rmi`` of ``server.image`` reclaims the image layer.

    Self-host only (T1 bare-metal + T2 socket-proxy). On cloud (external-pool)
    there is no per-install container, so ``delete_mcp`` never calls this.

    Returns True when the ``down`` succeeds. Image removal is best-effort and
    never fails the call — the image may be shared with another MCP or still in
    use, and reclaiming the container + volumes is what "delete" must guarantee.
    """
    try:
        # ``--rmi local`` reclaims an image-less MCP's locally-built image (tagged
        # ``<project>-<service>``, which the separate ``remove_image`` below can't
        # target). It does NOT touch a custom-tagged image (``server.image``) —
        # that's what ``remove_image`` reclaims — so the two together cover both
        # the T1 build-fallback and the pull/T2 cases.
        result = subprocess.run(
            _compose_cmd(manifest, "down", "--rmi", "local", "--volumes", "--remove-orphans"),
            capture_output=True, text=True, timeout=300,
            cwd=str(manifest.mcp_dir),
            env=_compose_env(),
        )
        ok = result.returncode == 0
        if ok:
            logger.info("Removed Docker MCP container + volumes: %s", manifest.name)
        else:
            logger.warning(
                "compose down for %s exited %s: %s",
                manifest.name, result.returncode, result.stderr[:500],
            )

        if prune_image:
            image = getattr(getattr(manifest, "server", None), "image", "") or ""
            remove_image(image)
        return ok
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("Error removing Docker MCP %s: %s", manifest.name, e)
        return False


def restart_container(manifest, *, start_exited: bool = False) -> bool:
    """Recreate a Docker MCP's container from the current ``.env`` and the
    image the manifest names.

    ``compose restart`` keeps the environment a container was created with and
    the image it runs, so neither a saved tool filter nor an update whose
    recreate did not happen would ever reach it: the restart re-injects
    ``.env`` and recreates. Only an enabled MCP's container that is up
    (running, unhealthy, starting) is recreated, and an exited one too when
    ``start_exited`` (the admin's Restart button): a disabled or stopped MCP is
    never started here (its new ``.env`` applies at its next start). The image
    must already be on this host (``--pull never``, ``--no-build``): an Update
    fetches it."""
    from storage.mcp import mcp_store
    try:
        _inject_mcp_env(manifest)
        state = mcp_store.get_mcp_state(manifest.name) or {}
        status = get_container_status(manifest)
        allowed = PRESENT | ({STOPPED} if start_exited else set())
        if not state.get("enabled") or status not in allowed:
            logger.info(
                "%s: no container to recreate (%s%s); the current settings apply "
                "at its next start", manifest.name, status,
                "" if state.get("enabled") else ", disabled",
            )
            return False
        from services.mcp import compose_rewrite
        if deployment.in_docker_compose():
            compose_rewrite.ensure_pull_compose(manifest)
        else:
            compose_rewrite.ensure_t1_override(manifest)
        return _compose_up(
            manifest,
            ["up", "-d", "--force-recreate", "--no-build", "--pull", "never"],
            timeout=120, t1_retry=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError, psycopg.Error) as e:
        logger.error("Error restarting Docker MCP %s: %s", manifest.name, e)
        return False


def startup_docker_mcps() -> None:
    """Called on proxy startup. Start all enabled Docker MCPs that aren't running.

    Always re-injects the platform `.env` (PROXY_URL, HOST_AGENTS_DIR,
    WOPI_BASE_URL, COLLABORA_SERVICE_ROOT) so
    containers see the current platform config. Note: NO master key — Docker
    MCPs that call back authenticate with a per-session JWT (Authorization
    header injected per session via server.proxy_callbacks), not PROXY_API_KEY.
    When the `.env` content actually changed, the affected container is
    force-recreated so it picks up the new values — without this, plain
    `up -d` would leave a running container untouched and env-only changes
    (Collabora deployment shape, WOPI_BASE_URL, infra credentials, etc.)
    would silently fail until manual recreate.
    """
    from services.mcp import mcp_registry
    from storage.mcp import mcp_store

    # Every topology with a local daemon: name (but never touch) MCP
    # containers left behind by a previous install identity.
    _warn_foreign_mcp_leftovers()

    # T2 (Docker-Compose): Docker MCPs are NOT started from their per-MCP
    # build-context compose files here. The core file-tools runs as an
    # operator-managed compose *sibling* (brought up by the platform compose,
    # reached by service-DNS), and community Docker MCPs are managed via the
    # pull-based socket-proxy path — never `docker compose build`,
    # which the socket-proxy blocks by design. Starting them from here would
    # both fail (403 on build) and fight the operator over the sibling's
    # lifecycle. Discovery (scan_manifests) + service-DNS routing already
    # happened; nothing to start. No-op on bare-metal (T1), where the proxy
    # owns the local-daemon lifecycle exactly as before.
    # Only bare-metal T1 (managed-local) drives per-MCP container lifecycle from
    # here. T2 Docker-Compose: community Docker MCPs are pull-managed siblings
    # (started at install via the socket-proxy); starting them here would
    # 403 on build and fight the operator. T3 cloud (external-pool): there is no
    # local daemon — Docker MCPs are connections to the OtoDock-hosted central
    # pool, never started locally. Both skip; only T1 proceeds.
    if deployment.current_mode() != deployment.MANAGED_LOCAL:
        logger.info(
            "%s: skipping per-MCP docker startup — Docker MCPs are not "
            "locally-managed in this topology",
            deployment.current_mode(),
        )
        return

    states = mcp_store.get_all_mcp_states()
    manifests = mcp_registry.get_all_manifests()
    engine_api: list = []
    enabled = [(name, manifest) for name, manifest in manifests.items()
               if _mt.is_container(manifest.server) and states.get(name, False)]
    for _name, manifest in enabled:
        _check_engine_for_subpath(manifest, engine_api)

    # One worker per project, a few at once: a build that runs long (or
    # stalls until its bound) never holds the next project's start.
    with ThreadPoolExecutor(max_workers=_BRINGUP_WORKERS, thread_name_prefix="docker-mcp") as pool:
        futures = {pool.submit(_bring_up, name, manifest): name for name, manifest in enabled}
        for future, name in futures.items():
            exc = future.exception()
            if exc is not None:
                logger.error("Docker MCP %s: bring-up failed: %r", name, exc, exc_info=exc)


# How many Docker MCP projects the boot brings up at once.
_BRINGUP_WORKERS = 4


def _bring_up(name: str, manifest) -> None:
    """One enabled Docker MCP at boot: its ``.env`` refreshed, then started,
    or recreated when its ``.env`` or its compose config changed. Nothing
    once the proxy is stopping (the pool's queued projects)."""
    if _stopping.is_set():
        return
    env_changed = False
    try:
        env_changed = _inject_mcp_env(manifest)
    except (OSError, ValueError, psycopg.Error) as e:
        logger.warning("Failed to refresh .env for %s: %s", name, e)

    status = get_container_status(manifest)
    if status != RUNNING:
        logger.info("Docker MCP %s: status=%s, starting...", name, status)
        start_container(manifest)
        return
    # Compose-config drift check: the skip path historically ran
    # NOTHING, so base-compose/override changes (a new mem_limit
    # default, an image retag) never reached a running container
    # until something else happened to recreate it. Hash the on-disk
    # config BEFORE refreshing the override (the pre-refresh text is
    # a faithful baseline for what the running container was created
    # from — only start_container ever writes the override), refresh
    # it, re-hash, and recreate on drift. Any failure degrades to
    # the plain skip this branch always was.
    compose_changed = False
    try:
        from services.mcp import compose_rewrite

        pre = _compose_config_hash(manifest)
        compose_rewrite.ensure_t1_override(manifest)
        post = _compose_config_hash(manifest)
        recorded = _read_recorded_compose_hash(manifest)
        if post is not None:
            if recorded is not None:
                compose_changed = post != recorded
            else:
                # First boot with this feature: recreate only on
                # REAL pending drift (the refresh changed the
                # effective config); otherwise seed quietly so
                # unchanged installs see no recreate storm.
                compose_changed = pre is not None and post != pre
                if not compose_changed:
                    _record_compose_hash(manifest)
    except (OSError, ValueError) as e:
        logger.warning(
            "Compose drift check for %s failed (skipping): %s", name, e,
        )

    if env_changed:
        logger.info(
            "Docker MCP %s: .env changed, force-recreating to pick up new values",
            name,
        )
        start_container(manifest, force_recreate=True)
    elif compose_changed:
        logger.info(
            "Docker MCP %s: compose config changed, force-recreating to pick it up",
            name,
        )
        start_container(manifest, force_recreate=True)
    else:
        logger.info("Docker MCP %s: already running", name)
