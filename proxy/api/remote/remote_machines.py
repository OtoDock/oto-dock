"""Remote Machine Management REST API.

Admin-only endpoints for managing satellite daemon connections:
- Pairing (generate token, exchange for secret)
- CRUD on remote machines
- Agent-to-machine targeting

Also serves the satellite installer payload (install.sh + satellite tarball)
so users can curl-pipe the install command without checking out the repo.
These two routes are intentionally unauthenticated — the pairing token in
the install command is the auth.
"""

import contextlib
import hashlib
import io
import logging
import tarfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_auth
from services.remote.remote_status import get_live_machine_status
from storage import remote_store, agent_store


def _merge_live_status(machine: dict) -> dict:
    """Overlay in-memory WS status over the DB row so clients see reality.

    The DB `status` column is updated on lifecycle events only and lags the
    live connection state. Callers should never return the raw DB `status`
    to API clients; use this wrapper instead.
    """
    live = get_live_machine_status(machine["id"])
    machine["status"] = live["state"]
    machine["last_heartbeat_age_s"] = live["last_heartbeat_age_s"]
    machine["reachable"] = live["reachable"]
    if live["last_seen_iso"]:
        machine["last_seen"] = live["last_seen_iso"]
    return machine

logger = logging.getLogger("claude-proxy.remote-machines")
router = APIRouter()


def _kick_presync(machine_id: str, agent_slug: str) -> None:
    """W3 (sync-performance): fire-and-forget workspace warm for a freshly
    ENABLED (machine, agent) pairing, so the first chat starts against a
    warm (or in-progress) tree instead of paying the whole initial transfer
    inline. Harmless + idempotent (per-(machine, agent) sync lock inside);
    offline machines no-op (the reconnect catch-up covers them)."""
    import asyncio

    async def _run() -> None:
        try:
            from core.remote.remote_execution import get_remote_layer
            await get_remote_layer().presync_machine_agent(machine_id, agent_slug)
        except Exception:
            logger.exception(
                "enable pre-sync failed for %s/%s", machine_id[:8], agent_slug,
            )

    asyncio.create_task(_run())


# --- Satellite installer assets ---


# Repo-root path to the `satellite/` package — built from this file's
# location so the proxy works under any deployment layout (dev tree, /opt
# install, container). This file lives at proxy/api/remote/, so the repo root
# (which contains both proxy/ and satellite/) is four parents up.
_SATELLITE_DIR = Path(__file__).resolve().parents[3] / "satellite"

# In-process tarball cache. Built once on first request, refreshed if the
# satellite/ tree is newer. Small (~50 KB) so memory is a non-issue.
# The `sha256` is included so we don't recompute it on every update-push.
_TARBALL_CACHE: dict = {"bytes": b"", "built_at": 0.0, "sha256": ""}

# Top-level files (no `satellite/` prefix) that land alongside the
# package after extraction. The .sh files are used by Linux/macOS
# installs; .ps1 files by Windows; bin/ holds the tray + ARP icon.
# Bundling all in one tarball wastes a few KB but keeps the build code
# simple — each platform only invokes its own. Missing extras are skipped
# (see `if p.exists()` below), so bin/otodock.ico absent in a dev checkout
# is harmless.
_TARBALL_EXTRAS = (
    "requirements.txt",
    "uninstall.sh",       # Linux/macOS
    "uninstall.ps1",      # Windows
    "runner.ps1",         # Windows logon-task entry point + auto-update swap
    "bin/otodock.ico",    # Windows tray + Add/Remove-Programs icon
    "bin/otodock",        # otodock-CLI PATH wrapper (Unix/macOS)
    "bin/otodock.cmd",    # otodock-CLI PATH wrapper (Windows)
    "bin/README.md",      # bin/ notes
)


# Subdirectories under ``satellite/`` that are NOT part of the shipped
# Python package and must never land in the tarball.
_SATELLITE_PKG_SKIP_DIRS = frozenset({"__pycache__", "venv", "tests"})


def _discover_satellite_package_files() -> tuple[str, ...]:
    """Every ``*.py`` under ``satellite/`` (recursively) is part of the package
    tarball. The satellite is a package with subpackages (``transport/``,
    ``sessions/``, ``terminal/``, ``host/``, ``_vendored/``) plus the root
    ``__init__``/``__main__``/``config`` — ALL of them must ship.

    Auto-discovery (rather than a hardcoded allowlist) ensures new modules ship
    with the next auto-update push without anyone having to remember to add them
    to a list — a foot-gun that previously bricked satellites when a new module
    was added without updating the list. **It is also recursive on purpose**:
    an earlier ``iterdir()`` (top-level only) version silently dropped every
    subpackage module the moment the flat layout was split into subpackages,
    bricking satellites with a 3-file tarball whose ``python -m satellite``
    died on ``from .transport... import``. Walk the whole tree.

    Skipped: hidden files/dirs (``.something``), ``__pycache__/``, the satellite
    dev ``venv/``, the ``tests/`` tree (never ships), and ``*_spike.py`` (dev
    exploration scripts). Returns POSIX-relative paths under ``satellite/`` (e.g.
    ``"config.py"``, ``"transport/ws_client.py"``). The trust boundary is who can
    write to this directory in the source tree; the file list is config, not
    security.
    """
    out: list[str] = []
    for p in _SATELLITE_DIR.rglob("*.py"):
        rel = p.relative_to(_SATELLITE_DIR)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if _SATELLITE_PKG_SKIP_DIRS.intersection(rel.parts):
            continue
        if p.stem.endswith("_spike"):
            continue
        if not p.is_file():
            continue
        out.append(rel.as_posix())
    return tuple(sorted(out))


def _build_satellite_tarball() -> bytes:
    """Pack the `satellite/` Python package + cross-platform extras into a
    gzipped tarball ready for the installer to extract.

    Layout after extraction (Linux example, ``-C $SATELLITE_DIR``):
        $SATELLITE_DIR/
            satellite/__init__.py, __main__.py, config.py
            satellite/transport/…, sessions/…, terminal/…, host/…, _vendored/…
            requirements.txt
            uninstall.sh, uninstall.ps1, runner.ps1
            bin/otodock.ico, bin/README.md

    Package contents are discovered via ``_discover_satellite_package_files``
    (every ``*.py`` under ``satellite/``, recursively, incl. subpackages) —
    adding a new module file to the source tree automatically ships in the next
    push, no allowlist to maintain. Rebuilds when any source file in the tree
    (or any package dir, so adds/removes count) is newer than the cached blob's
    ``built_at``. Threaded callers see consistent bytes — we build locally then
    atomically swap.
    """
    package_files = _discover_satellite_package_files()
    # Find the newest mtime across files we'd include. We also factor in the
    # mtime of the satellite/ dir AND every package subdir, so that adding or
    # removing a module file (which bumps its containing dir's mtime) — even
    # inside a subpackage — invalidates the cache without a proxy restart.
    newest = _SATELLITE_DIR.stat().st_mtime
    _pkg_dirs = {_SATELLITE_DIR} | {
        (_SATELLITE_DIR / name).parent for name in package_files
    }
    for d in _pkg_dirs:
        newest = max(newest, d.stat().st_mtime)
    for name in package_files:
        p = _SATELLITE_DIR / name
        newest = max(newest, p.stat().st_mtime)
    for extra in _TARBALL_EXTRAS:
        p = _SATELLITE_DIR / extra
        if p.exists():
            newest = max(newest, p.stat().st_mtime)

    if _TARBALL_CACHE["bytes"] and newest <= _TARBALL_CACHE["built_at"]:
        return _TARBALL_CACHE["bytes"]

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in package_files:
            tar.add(_SATELLITE_DIR / name, arcname=f"satellite/{name}")
        for extra in _TARBALL_EXTRAS:
            p = _SATELLITE_DIR / extra
            if p.exists():
                tar.add(p, arcname=extra)

    data = buf.getvalue()
    _TARBALL_CACHE["bytes"] = data
    _TARBALL_CACHE["built_at"] = newest
    _TARBALL_CACHE["sha256"] = hashlib.sha256(data).hexdigest()
    return data


def get_satellite_tarball_with_hash() -> tuple[bytes, str]:
    """Return (tarball_bytes, sha256_hex). Used by the auth-time auto-update
    push (proxy/ws/satellite.py) — the satellite verifies the hash before
    applying the tarball, so we hash exactly once per rebuild.
    """
    data = _build_satellite_tarball()
    return data, _TARBALL_CACHE["sha256"]


def _require_satellite_source() -> None:
    """Refuse feature entry points when the satellite source tree is not
    part of this build (see ws/satellite.py::satellite_source_available).

    Guards pairing, the bootstrap-installer download, and the update push —
    the operations that need the tree on disk. Management endpoints for
    already-paired machines stay functional so a DB migrated from a full
    build degrades cleanly.
    """
    from ws.satellite import satellite_source_available
    if not satellite_source_available():
        raise HTTPException(
            status_code=404,
            detail="Remote-machine support is not included in this build.",
        )


# Supported `?os=` values. `linux` and `macos` both produce a bash
# bootstrap (install.sh auto-detects via uname). `windows` produces a
# PowerShell bootstrap that runs install.ps1.
_BOOTSTRAP_OS_BASH = ("linux", "macos", "unix")
_BOOTSTRAP_OS_WINDOWS = ("windows", "win", "win32")


@router.get("/v1/satellite/bootstrap")
async def serve_bootstrap(request: Request):
    """Single-shot satellite bootstrap endpoint.

    Reads ``X-Pairing-Token`` from request headers (not URL path or query —
    headers aren't logged by default nginx config, so the token stays out
    of access logs). Atomically exchanges the token for a machine_secret
    then returns a self-extracting script with everything embedded:

      - The satellite Python package + extras (base64 tarball)
      - The baseline dev-tooling installer (base64 script)
      - The platform-appropriate install template body
      - The machine_id, machine_secret, and platform_url

    The ``?os=`` query param selects the script flavor:
      - ``linux`` / ``macos`` (default): bash + install.sh + .sh baseline
      - ``windows``: PowerShell + install.ps1 + .ps1 baseline

    End-users run one of:

        # Linux/macOS:
        bash <(curl -sL -H "X-Pairing-Token: <tok>" \\
            https://<platform>/v1/satellite/bootstrap?os=linux)

        # Windows:
        powershell -ExecutionPolicy Bypass -Command "& { $r = iwr \\
            -Headers @{'X-Pairing-Token'='<tok>'} \\
            'https://<platform>/v1/satellite/bootstrap?os=windows'; \\
            iex $r.Content }"
    """
    _require_satellite_source()
    token = request.headers.get("X-Pairing-Token", "").strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Pairing-Token header",
        )

    os_hint = request.query_params.get("os", "linux").lower()
    if os_hint in _BOOTSTRAP_OS_WINDOWS:
        os_hint = "windows"
    elif os_hint in _BOOTSTRAP_OS_BASH:
        os_hint = "linux"
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported os: {os_hint!r}. Expected linux, macos, or windows.",
        )

    # Atomic exchange: validates token, returns machine_secret, clears
    # the pairing token from the DB (one-time use). Failure modes:
    # invalid token, expired token, already-exchanged token.
    try:
        import hashlib as _h
        token_hash = _h.sha256(token.encode()).hexdigest()
        with __import__("storage.pg", fromlist=["get_conn"]).get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM remote_machines WHERE pairing_token_hash = %s",
                (token_hash,),
            ).fetchone()
        if not row:
            raise HTTPException(
                status_code=401,
                detail="Invalid or already-exchanged pairing token",
            )
        machine_id = row["id"]
        machine_secret = remote_store.exchange_pairing_token(
            machine_id=machine_id, pairing_token=token,
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    import config as _cfg
    public_host = _cfg.get_platform_public_url()
    if not public_host:
        raise HTTPException(
            status_code=400,
            detail=(
                "platform_public_url is not configured — admin must set "
                "it before satellites can pair."
            ),
        )
    platform_url = f"wss://{public_host}/v1/satellite"

    body = await __import__("asyncio").to_thread(
        _build_bootstrap_script,
        machine_id, machine_secret, platform_url, os_hint,
    )
    media_type = (
        "text/plain"
        if os_hint == "windows"
        else "text/x-shellscript"
    )
    return Response(
        content=body,
        media_type=media_type,
        headers={"Cache-Control": "no-cache"},
    )


def _build_bootstrap_script(
    machine_id: str, machine_secret: str, platform_url: str,
    os_hint: str = "linux",
) -> bytes:
    """Generate the self-extracting bootstrap script body for the given OS.

    Layout (both flavors):
      1. Shebang + env-var exports (MACHINE_ID, MACHINE_SECRET, PLATFORM_URL)
      2. Two base64 payloads: baseline-tools script + satellite tarball
      3. The static install template body (install.sh or install.ps1)

    ``os_hint`` is either ``"linux"`` (covers macOS too — install.sh
    auto-detects via uname) or ``"windows"``.
    """
    import base64 as _b64

    tarball = _build_satellite_tarball()
    tarball_b64 = _b64.b64encode(tarball).decode("ascii")

    scripts_dir = _SATELLITE_DIR.parent / "scripts"

    if os_hint == "windows":
        baseline_path = scripts_dir / "install-baseline-tools.ps1"
        template_path = _SATELLITE_DIR / "install.ps1"
    else:
        baseline_path = scripts_dir / "install-baseline-tools.sh"
        template_path = _SATELLITE_DIR / "install.sh"

    baseline_b64 = (
        _b64.b64encode(baseline_path.read_bytes()).decode("ascii")
        if baseline_path.exists() else ""
    )

    install_template = template_path.read_text()
    # Drop the template's shebang/header — we replace it with our own.
    if install_template.startswith("#!"):
        install_template = install_template.split("\n", 1)[1]

    if os_hint == "windows":
        return _build_powershell_header(
            machine_id, machine_secret, platform_url,
            baseline_b64, tarball_b64,
        ).encode() + install_template.encode()

    # The git credential helper rides as a third payload on Linux/macOS —
    # the baseline script looks for it NEXT TO ITSELF (repo-local layout),
    # so install.sh materializes both into one temp dir. Without this every
    # bootstrap install warned "credential helper source not found" and
    # GH_TOKEN-driven `git push` from sandboxed agents stayed unwired.
    helper_path = scripts_dir / "oto-git-credential-helper"
    helper_b64 = (
        _b64.b64encode(helper_path.read_bytes()).decode("ascii")
        if helper_path.exists() else ""
    )

    return _build_bash_header(
        machine_id, machine_secret, platform_url,
        baseline_b64, tarball_b64, helper_b64,
    ).encode() + install_template.encode()


def _build_bash_header(
    machine_id: str, machine_secret: str, platform_url: str,
    baseline_b64: str, tarball_b64: str, helper_b64: str = "",
) -> str:
    def _esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return (
        "#!/bin/bash\n"
        "# Auto-generated by /v1/satellite/bootstrap. Do not edit.\n"
        "set -euo pipefail\n"
        "\n"
        f'export MACHINE_ID="{_esc(machine_id)}"\n'
        f'export MACHINE_SECRET="{_esc(machine_secret)}"\n'
        f'export PLATFORM_URL="{_esc(platform_url)}"\n'
        "\n"
        # The two base64 payloads are PLAIN (unexported) shell variables, not
        # `export`ed. The Linux kernel caps any single ENVIRONMENT string at
        # MAX_ARG_STRLEN (128 KiB = 32 × page); the satellite tarball base64 is
        # ~140 KiB. Exporting it makes the FIRST external command install.sh
        # runs (`uname`) die with "Argument list too long" — execve must copy
        # the oversized env into the child. A plain assignment lives only in
        # this bash process (the whole bootstrap runs in one `bash <(...)`
        # process) and the install.sh body reads it via `${VAR:-}` in that same
        # process, so export was never needed. Windows dodges the same limit
        # via PowerShell script variables (see _build_powershell_header).
        "# Embedded payloads (base64). Read by install.sh below (same process).\n"
        f'BASELINE_TOOLS_B64="{baseline_b64}"\n'
        f'CREDENTIAL_HELPER_B64="{helper_b64}"\n'
        f'SATELLITE_TARBALL_B64="{tarball_b64}"\n'
        "\n"
        "# --- install.sh template body ---\n"
    )


def _build_install_commands(
    public_host: str, pairing_token: str,
) -> dict[str, str]:
    """Build the per-OS install commands shown in the pairing modal.

    Returns ``{linux, macos, windows}``. linux and macos use the same
    bash bootstrap (install.sh auto-detects via uname); windows uses
    PowerShell with execution-policy bypass scoped to the invocation.

    ``bash <(curl ...)`` (process substitution) keeps the script's stdin
    connected to the terminal — essential for ``sudo -v`` prompts inside
    install.sh. ``iex $r.Content`` is the PowerShell idiom for
    ``curl | bash``.
    """
    bootstrap_url = f"https://{public_host}/v1/satellite/bootstrap"
    bash_cmd = (
        f'bash <(curl -sL -H "X-Pairing-Token: {pairing_token}" '
        f'"{bootstrap_url}?os=linux")'
    )
    # Windows command: PowerShell. -ExecutionPolicy Bypass is scoped to
    # this invocation only. Runs in a NORMAL (non-admin) PowerShell — the
    # per-user install registers a logon Scheduled Task, no service/HKLM.
    #
    # We use the parenthesized `iex (iwr ...).Content` form rather than
    # `$r = iwr ...; iex $r.Content` because when the user pastes the
    # one-liner into PowerShell, the OUTER shell would expand `$r` to
    # empty before passing the script to the inner shell, producing
    # `= iwr ...` and `.Content` errors. No `$` in this form = no
    # outer-shell interpolation problem.
    ps_inner = (
        f"iex (iwr -Headers @{{'X-Pairing-Token'='{pairing_token}'}} "
        f"'{bootstrap_url}?os=windows').Content"
    )
    windows_cmd = (
        f'powershell -ExecutionPolicy Bypass -Command "& {{ {ps_inner} }}"'
    )
    return {
        "linux": bash_cmd,
        "macos": bash_cmd,
        "windows": windows_cmd,
    }


def _build_powershell_header(
    machine_id: str, machine_secret: str, platform_url: str,
    baseline_b64: str, tarball_b64: str,
) -> str:
    # PowerShell single-quoted strings are LITERAL — no interpolation,
    # no escapes except '' for a single quote. So we only need to
    # escape single quotes within the values.
    #
    # IMPORTANT: large payloads (tarball, baseline) go into PowerShell
    # *script variables*, NOT $env:* — Windows enforces a 32,767-char
    # per-environment-variable limit, and our tarball is ~480 KB base64.
    # Script vars (the bootstrap is `iex`-evaluated as one big script,
    # so vars set in this header are visible to install.ps1 below) are
    # RAM-bound with no such limit.
    def _esc(v: str) -> str:
        return v.replace("'", "''")
    return (
        "# Auto-generated by /v1/satellite/bootstrap?os=windows.\n"
        "# Do not edit — runs via `iex` in a normal (non-admin) PowerShell.\n"
        "Set-StrictMode -Version Latest\n"
        "$ErrorActionPreference = 'Stop'\n"
        "\n"
        # Small identifiers — $env: is fine and convenient (visible to
        # subprocess inheritance, etc.).
        f"$env:MACHINE_ID = '{_esc(machine_id)}'\n"
        f"$env:MACHINE_SECRET = '{_esc(machine_secret)}'\n"
        f"$env:PLATFORM_URL = '{_esc(platform_url)}'\n"
        "\n"
        # Large payloads — script variables, NOT $env:*. install.ps1
        # reads `$BASELINE_TOOLS_B64` / `$SATELLITE_TARBALL_B64` from
        # the parent scope (same iex'd script).\n"
        f"$BASELINE_TOOLS_B64 = '{baseline_b64}'\n"
        f"$SATELLITE_TARBALL_B64 = '{tarball_b64}'\n"
        "\n"
        "# --- install.ps1 template body ---\n"
    )


# --- Request models ---


class PairMachineRequest(BaseModel):
    name: str
    # Per-machine filesystem-access policy. When `None`, the
    # pair endpoint applies its scope-specific default: admin pairing
    # defaults to TRUE (operational machines), user pairing defaults to
    # FALSE (personal laptops, home-only is the safer baseline). Callers
    # that explicitly want the opposite pass the bool.
    allow_full_fs: bool | None = None


class SetAllowFullFsRequest(BaseModel):
    enabled: bool


class SetMaxSessionsRequest(BaseModel):
    # Proxy-side concurrent-session override for this satellite. `None`
    # (empty input in the UI) clears the override → the satellite's own
    # recommendation is used. The satellite still hard-caps at its physical
    # max regardless of this value.
    max_sessions: int | None = None


class SetDeviceGrantsRequest(BaseModel):
    # The device-control capabilities granted on this machine:
    # any of "computer" / "browser" / "app".
    # Empty list = block all device-local MCPs.
    grants: list[str]


class ExchangeTokenRequest(BaseModel):
    machine_id: str
    pairing_token: str


class AssignAgentRequest(BaseModel):
    agent_slug: str


# --- Helpers ---


def _require_admin(user: UserContext | None) -> UserContext:
    u = require_auth(user)
    if u.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return u


# --- Pairing endpoints ---


@router.post("/v1/admin/remote-machines/pair")
async def pair_machine(
    body: PairMachineRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Generate a one-time pairing token for a new remote machine.

    Returns the machine_id, pairing_token, and an install command
    that can be run on the remote machine.
    """
    _require_admin(user)
    _require_satellite_source()

    # Admin pairing defaults to home-only (least privilege / opt-in full-FS).
    # Even operational machines start scoped to the agent tree + the OS home;
    # the admin opts into whole-host file access via the pairing toggle when a
    # machine genuinely needs it. Existing machines keep their stored value.
    allow_full_fs = body.allow_full_fs if body.allow_full_fs is not None else False
    machine_id = str(uuid.uuid4())
    try:
        result = remote_store.create_remote_machine(
            machine_id=machine_id,
            name=body.name.strip(),
            registered_by=user.sub,
            pairing_scope="admin",
            allow_full_fs=allow_full_fs,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Build the install command using the platform's configured public host.
    # Admin must set `platform_public_url` (or DASHBOARD_PUBLIC_URL falls
    # through) before pairing — otherwise the install command is broken.
    import config as _cfg
    public_host = _cfg.get_platform_public_url()
    if not public_host:
        raise HTTPException(
            status_code=400,
            detail=(
                "Set `platform_public_url` in admin settings (or "
                "DASHBOARD_PUBLIC_URL) before pairing satellites — the "
                "install command needs a public WSS endpoint to embed."
            ),
        )

    return {
        "machine_id": result["id"],
        "name": result["name"],
        "pairing_token": result["pairing_token"],
        "expires_in_hours": remote_store.PAIRING_TOKEN_EXPIRY_HOURS,
        "install_commands": _build_install_commands(
            public_host, result["pairing_token"],
        ),
    }


# `POST /v1/remote-machines/exchange` was removed — the
# bootstrap endpoint above does the exchange atomically as part of
# serving the install script. No separate exchange request needed.


# --- Machine CRUD ---


@router.get("/v1/admin/remote-machines")
async def list_machines(user: UserContext | None = Depends(get_current_user)):
    """List all remote machines with status and assigned agents."""
    _require_admin(user)
    machines = remote_store.get_all_remote_machines()
    # Parse capabilities JSON + merge live WS status for frontend
    import json
    for m in machines:
        try:
            m["capabilities"] = json.loads(m.get("capabilities", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            m["capabilities"] = {}
        # device_grants is a TEXT JSON-array column → parse to a list so the
        # dashboard receives string[].
        m["device_grants"] = sorted(remote_store._parse_device_grants(m.get("device_grants")))
        _merge_live_status(m)
    return {"machines": machines}


@router.get("/v1/admin/remote-machines/{machine_id}")
async def get_machine(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Get a single remote machine with details."""
    _require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    import json
    try:
        machine["capabilities"] = json.loads(machine.get("capabilities", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        machine["capabilities"] = {}
    # device_grants TEXT JSON-array → list for the dashboard.
    machine["device_grants"] = sorted(remote_store._parse_device_grants(machine.get("device_grants")))
    machine["assigned_agents"] = remote_store.get_agents_for_machine(machine_id)
    _merge_live_status(machine)
    return machine


@router.delete("/v1/admin/remote-machines/{machine_id}")
async def delete_machine(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a remote machine. Resets affected agents to local execution.

    Also fires the self-uninstall flow on the satellite — sends
    an ``uninstall`` WS message if connected, then deletes the DB row.
    Offline satellites self-uninstall the next time they try to reconnect
    (auth handler rejects with close code 4006 ``machine_deleted``).
    """
    _require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    await _trigger_self_uninstall(machine_id)
    remote_store.delete_remote_machine(machine_id)
    return {"ok": True}


@router.delete("/v1/satellite/{machine_id}/self-uninstall")
async def self_uninstall_notify(
    machine_id: str,
    x_machine_secret: str = Header(..., alias="X-Machine-Secret"),
):
    """Satellite-initiated dashboard cleanup.

    Called by the satellite's ``uninstall.sh`` / ``uninstall.ps1`` BEFORE
    wiping the local install dir (so the conf file with ``machine_secret``
    is still readable). Authenticated by the same ``machine_secret`` the
    WS auth handler validates — see ``remote_store.verify_machine_secret``.

    Closes the manual-uninstall orphan loop: previously, only
    dashboard-initiated deletes auto-removed both sides. Now manual
    uninstalls on the machine also auto-clean the dashboard entry.

    Idempotent: returns 200 whether the record existed or was already
    deleted. The satellite's local cleanup must succeed regardless of
    this endpoint's outcome (network down, already deleted, etc).

    Skips ``_trigger_self_uninstall`` — the satellite IS uninstalling
    by definition when it calls this; no need to send it a WS message
    telling it to do so.
    """
    machine = remote_store.get_remote_machine(machine_id)
    if machine is None:
        # Already deleted (or never existed). Return success — the
        # satellite's local cleanup already proceeded; we just confirm
        # there's no dashboard record to clean.
        return {"ok": True, "deleted": False}
    if not remote_store.verify_machine_secret(machine_id, x_machine_secret):
        raise HTTPException(status_code=401, detail="Invalid machine secret")
    remote_store.delete_remote_machine(machine_id)
    return {"ok": True, "deleted": True}


@router.post("/v1/admin/remote-machines/{machine_id}/sync-mcps")
async def sync_mcps_now(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Force an MCP sync pass on a satellite — installs anything missing,
    updates anything version-drifted, removes anything no agent targets.

    Desired set = union of assigned MCPs across every agent whose default
    target is this machine. Progress is logged to proxy.log; the admin can
    watch there while the request runs (may take 30-60s on a cold machine).
    """
    _require_admin(user)

    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    from services.mcp import mcp_sync, mcp_registry

    # Satellite sync uses the VISIBILITY set (admin-authorized), not the
    # manager-enabled set. Rationale: pre-install on authorization so that the
    # manager's enable-toggle is instant — otherwise, the first session start
    # after a manager flips an explicit MCP on would fail because the satellite
    # hadn't installed it yet. Cost: the satellite installs MCPs that may
    # never be enabled. Acceptable for v1.
    assigned_union: set[str] = set()
    agent_slugs = remote_store.get_agents_for_machine(machine_id)
    for slug in agent_slugs:
        for manifest in mcp_registry.get_visible_mcps_for_agent(slug):
            assigned_union.add(manifest.name)

    result = await mcp_sync.sync_mcps_for_session(
        machine_id, session_id="", agent_assigned_mcps=list(assigned_union),
        force=False,
    )
    return {
        "ok": result.ok,
        "installed": result.installed,
        "updated": result.updated,
        "removed": result.removed,
        "failed": result.failed,
    }


# --- auto-update toggle + manual trigger ---


class AutoUpdateToggleRequest(BaseModel):
    enabled: bool


@router.put("/v1/admin/remote-machines/{machine_id}/auto-update")
async def set_machine_auto_update(
    machine_id: str,
    body: AutoUpdateToggleRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Toggle the per-machine auto-update policy. When False, the proxy
    rejects version-mismatched satellites instead of pushing the new
    tarball — admin must click "Update now" to trigger the push.
    """
    _require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    remote_store.set_auto_update_enabled(machine_id, body.enabled)
    return {"ok": True, "auto_update_enabled": body.enabled}


# --- per-machine allow_full_fs toggle ---


@router.put("/v1/admin/remote-machines/{machine_id}/allow-full-fs")
async def admin_set_allow_full_fs(
    machine_id: str,
    body: SetAllowFullFsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Toggle the per-machine filesystem-access policy (admin scope).

    When ``True``, the path framework admits any path the
    satellite-user's OS account can reach. When ``False``, only the
    agent tree plus the OS user's home directory are admitted.

    Admin can flip this on ANY machine (admin-paired AND user-paired)
    via the admin Remote Machines page. The user can flip it on their
    OWN machines via the user endpoint.

    Defense-in-depth: when the satellite is online, push a
    ``policy_update`` WS message so its local copy refreshes
    immediately. Without this the satellite would keep the stale
    policy until the next reconnect.
    """
    _require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    remote_store.set_allow_full_fs(machine_id, body.enabled)
    await _push_policy_update(machine_id)
    return {"ok": True, "allow_full_fs": body.enabled}


# --- per-machine concurrent-session override (admin scope) ---


@router.put("/v1/admin/remote-machines/{machine_id}/max-sessions")
async def admin_set_max_sessions(
    machine_id: str,
    body: SetMaxSessionsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Set the proxy-side concurrent-session override for a satellite.

    ``max_sessions = None`` (empty input) clears the override so the
    satellite's own recommendation is used. The proxy soft pre-check
    (``machine_at_capacity``) honors this; the satellite hard-caps at its
    physical max on its own, so no push is needed.
    """
    _require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    value = body.max_sessions
    if value is not None and value < 1:
        raise HTTPException(status_code=422, detail="max_sessions must be >= 1 or empty")
    remote_store.set_remote_machine_max_sessions(machine_id, value)
    return {"ok": True, "max_sessions": value}


@router.put("/v1/users/me/remote-machines/{machine_id}/allow-full-fs")
async def user_set_allow_full_fs(
    machine_id: str,
    body: SetAllowFullFsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """User-scoped toggle. Owner-only — only the user who paired the
    machine can flip its filesystem-access policy. Admin-paired
    machines (the user is not the registering admin) reject with 403.
    """
    u = _require_user_authenticated(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine.get("pairing_scope") != "user":
        raise HTTPException(
            status_code=403,
            detail=(
                "This is an admin-paired (platform) machine. Ask an "
                "admin to flip the toggle from the admin Remote "
                "Machines page."
            ),
        )
    if machine["registered_by"] != u.sub:
        raise HTTPException(status_code=403, detail="Not your machine")
    remote_store.set_allow_full_fs(machine_id, body.enabled)
    await _push_policy_update(machine_id)
    return {"ok": True, "allow_full_fs": body.enabled}


# --- Per-machine device-control consent ---


def _validate_device_grants(grants: list[str]) -> list[str]:
    """Validate + normalize device-control grant keys against the canonical
    capability set (``computer`` / ``browser`` / ``app``). Raises 422 on an
    unknown key; returns the de-duplicated, sorted list."""
    from services.mcp import mcp_registry
    valid = mcp_registry.DEVICE_CAPABILITIES
    cleaned = sorted({str(g) for g in (grants or [])})
    invalid = [g for g in cleaned if g not in valid]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown device capabilities: {invalid}. Valid: {sorted(valid)}",
        )
    return cleaned


@router.put("/v1/admin/remote-machines/{machine_id}/device-grants")
async def admin_set_device_grants(
    machine_id: str,
    body: SetDeviceGrantsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Set the per-machine device-control consent set (admin scope).

    ``grants`` lists the capabilities (``computer`` / ``browser`` / ``app``)
    device-local MCPs may use on this satellite; empty = block all. Device
    control is strictly more powerful than allow_full_fs (it can drive sudo
    prompts, a browser with saved cards), so it defaults closed and is granted
    only here or via the owner endpoint.

    Admin can set this on ANY machine (admin-paired AND user-paired). Pushes a
    ``policy_update`` so a connected satellite + warm sessions refresh live.
    """
    _require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    grants = _validate_device_grants(body.grants)
    remote_store.set_device_grants(machine_id, grants)
    await _push_policy_update(machine_id)
    return {"ok": True, "device_grants": grants}


@router.put("/v1/users/me/remote-machines/{machine_id}/device-grants")
async def user_set_device_grants(
    machine_id: str,
    body: SetDeviceGrantsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Owner-scoped device-control consent. Owner-only — only the user who
    paired the machine can grant device capabilities on it. Admin-paired
    machines reject with 403 (ask an admin). This is the consent gate that
    stops an admin reaching a user's PRIVATE laptop with device control.
    """
    u = _require_user_authenticated(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine.get("pairing_scope") != "user":
        raise HTTPException(
            status_code=403,
            detail=(
                "This is an admin-paired (platform) machine. Ask an admin to "
                "set device-control grants from the admin Remote Machines page."
            ),
        )
    if machine["registered_by"] != u.sub:
        raise HTTPException(status_code=403, detail="Not your machine")
    grants = _validate_device_grants(body.grants)
    remote_store.set_device_grants(machine_id, grants)
    await _push_policy_update(machine_id)
    return {"ok": True, "device_grants": grants}


async def _push_policy_update(machine_id: str) -> None:
    """Notify a connected satellite + refresh warm sessions when this machine's
    policy (``allow_full_fs`` and/or ``device_grants``) changes. Reads the
    CURRENT persisted values, so callers just persist (``set_allow_full_fs`` /
    ``set_device_grants``) then call this with the machine_id.

    The satellite caches policy locally for defense-in-depth re-checks; without
    this push the local copy stays stale until the next WS reconnect (where it
    lands via the ``policy`` field of ``auth_result``). Send failures are
    swallowed — the state IS persisted, so the next reconnect picks it up.

    Also refreshes the PROXY-side cached SecurityContext of every warm session
    targeting this machine — that cache is the primary gate (Read/Write/Edit/
    Bash/MCP, plus the device-control auto-approve), baked at warmup and
    otherwise stale until re-warm.
    """
    machine = remote_store.get_remote_machine(machine_id) or {}
    allow_full_fs = bool(machine.get("allow_full_fs") or False)
    device_grants = sorted(remote_store._parse_device_grants(machine.get("device_grants")))
    try:
        from core.session import session_state
        n_fs = session_state.refresh_target_allow_full_fs(machine_id, allow_full_fs)
        n_dg = session_state.refresh_target_device_grants(machine_id, set(device_grants))
        if n_fs or n_dg:
            logger.info(
                "policy applied live on %s (allow_full_fs→%d session(s), device_grants→%d)",
                machine_id[:8], n_fs, n_dg,
            )
    except Exception:
        logger.exception("proxy-side policy refresh failed (non-fatal)")
    try:
        from core.remote.satellite_connection import get_connection_manager
        cm = get_connection_manager()
        conn = cm.get_connection(machine_id) if hasattr(cm, "get_connection") else None
        if conn is None and machine_id in getattr(cm, "_connections", {}):
            conn = cm._connections[machine_id]
        if conn is None:
            return
        await conn.enqueue_send({
            "type": "policy_update",
            "policy": {
                "allow_full_fs": allow_full_fs,
                "device_grants": device_grants,
            },
        })
    except Exception:
        logger.exception(
            "Failed to push policy_update to %s (non-fatal)",
            machine_id[:8] if machine_id else "?",
        )


async def _do_trigger_update_now(machine_id: str, machine: dict) -> dict:
    """Shared logic for the admin and user "Update now" endpoints.
    Pushes the tarball if connected, else sets pending_update for next
    reconnect. Caller is responsible for authorization."""
    _require_satellite_source()
    from core.remote.satellite_connection import get_connection_manager
    from ws.satellite import (
        SATELLITE_VERSION_LATEST,
        _broadcast_satellite_updating,
    )
    import base64 as _b64

    cm = get_connection_manager()
    if cm.is_connected(machine_id):
        tarball_bytes, expected_sha256 = get_satellite_tarball_with_hash()
        conn = cm.get_connection(machine_id)
        if conn is not None:
            try:
                await conn.enqueue_send({
                    "type": "update_required",
                    "target_version": SATELLITE_VERSION_LATEST,
                    "tarball_b64": _b64.b64encode(tarball_bytes).decode(),
                    "expected_sha256": expected_sha256,
                    "previous_version": machine.get("satellite_version") or "",
                })
                await _broadcast_satellite_updating(
                    machine_id, machine,
                    machine.get("satellite_version") or "",
                    SATELLITE_VERSION_LATEST,
                )
            except Exception as e:
                logger.exception("update-now push failed for %s", machine_id[:8])
                raise HTTPException(status_code=500, detail=str(e))
            return {"ok": True, "queued": False, "pushed_now": True}

    # Offline: set pending_update so the next reconnect triggers the
    # push even if auto_update_enabled is False.
    remote_store.set_pending_update(machine_id, True)
    return {"ok": True, "queued": True, "pushed_now": False}


@router.post("/v1/admin/remote-machines/{machine_id}/update-now")
async def trigger_machine_update_now(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Force a satellite update even when auto-update is disabled. If the
    satellite is currently connected, sends an update_required immediately
    and closes the WS with code 4007. If offline, sets pending_update so
    the next reconnect triggers the push (bypassing the auto_update gate).
    """
    _require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    return await _do_trigger_update_now(machine_id, machine)


# --- Agent-machine targeting ---


@router.post("/v1/admin/remote-machines/{machine_id}/agents")
async def assign_agent(
    machine_id: str,
    body: AssignAgentRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Assign an agent to run on a remote machine."""
    _require_admin(user)

    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    # Per-user satellite isolation: agent default execution targets must
    # point at admin-paired machines. User-paired machines belong to one
    # user's personal sessions only; allowing them as an agent default
    # would let any session on that agent expose data to a user-owned
    # satellite the rest of the agent's users can't see.
    if machine.get("pairing_scope") != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                "Agent default execution targets must be admin-paired "
                f"machines. Machine '{machine.get('name', machine_id)}' "
                "was paired via User Settings (personal scope). The user "
                "may attach it as a per-user override there instead."
            ),
        )

    if not agent_store.agent_exists(body.agent_slug):
        raise HTTPException(status_code=404, detail="Agent not found")

    # Direct LLM agents cannot run remotely
    agent = agent_store.get_agent(body.agent_slug)
    if agent and agent.get("execution_path") == "direct-llm":
        raise HTTPException(
            status_code=400,
            detail="Direct LLM agents always run locally (API calls, no subprocess)",
        )

    remote_store.set_agent_remote_target(
        agent_slug=body.agent_slug,
        machine_id=machine_id,
        added_by=user.sub,
    )
    _kick_presync(machine_id, body.agent_slug)
    return {"ok": True}


@router.delete("/v1/admin/remote-machines/{machine_id}/agents/{agent_slug}")
async def unassign_agent(
    machine_id: str,
    agent_slug: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Remove an agent's remote target, resetting it to local execution."""
    _require_admin(user)
    remote_store.remove_agent_remote_target(agent_slug)
    return {"ok": True}


# --- User-level endpoints (managers+) ---


class SetUserTargetRequest(BaseModel):
    machine_id: str


def _require_creator(user: UserContext | None) -> UserContext:
    """Require a real user (not API key) with creator+ role."""
    u = require_auth(user)
    if getattr(u, "is_api_key", False):
        raise HTTPException(status_code=403, detail="User authentication required (not API key)")
    if u.role not in ("admin", "creator"):
        raise HTTPException(status_code=403, detail="Creator role required")
    return u


def _require_user_authenticated(user: UserContext | None) -> UserContext:
    """Require a real user (any platform role, not API key).

    Viewers are allowed here — their user-level remote-machine override
    only applies to their own user-scoped sessions on their own hardware,
    which does not break the bwrap isolation model (they own the machine).
    The per-agent gate in set_user_remote_target prevents drive-by overrides
    on agents they aren't assigned to.
    """
    u = require_auth(user)
    if getattr(u, "is_api_key", False):
        raise HTTPException(status_code=403, detail="User authentication required (not API key)")
    return u


@router.get("/v1/users/me/remote-machines")
async def list_my_machines(user: UserContext | None = Depends(get_current_user)):
    """List machines visible in the user's personal "Remote Machines" view.

    Always includes the caller's user-paired machines. Admins additionally
    see every admin-paired (platform) machine so they can attach personal
    user-scope agent targets to those too (dual-scope flow: same physical
    host runs both the agent's default sessions and the admin's personal
    user-scope sessions, with disjoint workspace dirs on the satellite).

    The ``pairing_scope`` field on each row lets the UI badge admin-paired
    machines and hide the "Remove" button for them (deletion of admin
    infrastructure stays in the admin dashboard).
    """
    u = _require_user_authenticated(user)
    machines = remote_store.get_visible_machines_for_user(
        u.sub, include_admin_paired=(u.role == "admin"),
    )
    import json
    for m in machines:
        try:
            m["capabilities"] = json.loads(m.get("capabilities", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            m["capabilities"] = {}
        # device_grants is a TEXT JSON-array column → parse to a list so the
        # dashboard receives string[].
        m["device_grants"] = sorted(remote_store._parse_device_grants(m.get("device_grants")))
        _merge_live_status(m)
    targets = remote_store.get_user_remote_targets(u.sub)
    return {
        "machines": machines,
        "targets": targets,
    }


@router.post("/v1/users/me/remote-machines/pair")
async def pair_my_machine(
    body: PairMachineRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Pair a new machine (any authenticated user). Same flow as admin but user-owned."""
    u = _require_user_authenticated(user)
    _require_satellite_source()

    # Admin kill-switch. When off, user pairing is refused outright.
    from storage import database as _db
    if _db.get_platform_setting("allow_user_paired_machines") == "0":
        raise HTTPException(
            status_code=403,
            detail="User-paired machines are disabled by your administrator.",
        )

    # User pairing defaults to home-only (the safer baseline for
    # personal laptops). The user can flip the toggle in the pairing
    # modal to opt into full-FS access.
    allow_full_fs = body.allow_full_fs if body.allow_full_fs is not None else False
    machine_id = str(uuid.uuid4())
    try:
        result = remote_store.create_remote_machine(
            machine_id=machine_id,
            name=body.name.strip(),
            registered_by=u.sub,
            pairing_scope="user",
            allow_full_fs=allow_full_fs,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    import config as _cfg
    public_host = _cfg.get_platform_public_url()
    if not public_host:
        raise HTTPException(
            status_code=400,
            detail=(
                "Platform public URL is not configured — ask your admin "
                "to set platform_public_url (or DASHBOARD_PUBLIC_URL)."
            ),
        )

    return {
        "machine_id": result["id"],
        "name": result["name"],
        "pairing_token": result["pairing_token"],
        "expires_in_hours": remote_store.PAIRING_TOKEN_EXPIRY_HOURS,
        "install_commands": _build_install_commands(
            public_host, result["pairing_token"],
        ),
    }


@router.delete("/v1/users/me/remote-machines/{machine_id}")
async def delete_my_machine(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a user-paired machine (the caller's own).

    Admin-paired (platform) machines can NOT be deleted here even by an
    admin — that flow stays in the admin dashboard to keep the mental
    model clean (deletion of infrastructure is an admin operation, not
    a personal-machine operation).

    Triggers self-uninstall on the satellite before removing the row.
    """
    u = _require_user_authenticated(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine.get("pairing_scope") != "user":
        raise HTTPException(
            status_code=403,
            detail=(
                "This is an admin-paired (platform) machine. Delete it "
                "from the admin Remote Machines page instead."
            ),
        )
    if machine["registered_by"] != u.sub:
        raise HTTPException(status_code=403, detail="Not your machine")
    await _trigger_self_uninstall(machine_id)
    remote_store.delete_remote_machine(machine_id)
    return {"ok": True}


@router.put("/v1/users/me/remote-machines/{machine_id}/auto-update")
async def set_my_machine_auto_update(
    machine_id: str,
    body: AutoUpdateToggleRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Mirror of the admin auto-update toggle for owners of user-paired
    machines — sensitive servers can be pinned by their owner without
    needing admin intervention."""
    u = _require_user_authenticated(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine["registered_by"] != u.sub and u.role != "admin":
        raise HTTPException(status_code=403, detail="Not your machine")
    remote_store.set_auto_update_enabled(machine_id, body.enabled)
    return {"ok": True, "auto_update_enabled": body.enabled}


@router.post("/v1/users/me/remote-machines/{machine_id}/update-now")
async def trigger_my_machine_update_now(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Mirror of the admin update-now trigger for owners of user-paired
    machines."""
    u = _require_user_authenticated(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine["registered_by"] != u.sub and u.role != "admin":
        raise HTTPException(status_code=403, detail="Not your machine")
    return await _do_trigger_update_now(machine_id, machine)


async def _trigger_self_uninstall(machine_id: str) -> None:
    """Send an ``uninstall`` WS message to the satellite if connected, then
    deregister so the WS closes cleanly. No-op when the satellite is offline
    — the WS handler's machine_id-not-found branch fires the same cleanup
    via close code 4006 on the next reconnect attempt.

    Uses direct ``ws.send_text`` (NOT the writer-task queue) so the message
    actually reaches the wire before we close the socket. Enqueueing then
    closing races the close ahead of the send — the writer wakes up with
    an already-closed socket and the uninstall message never transmits.
    """
    import asyncio as _asyncio
    import json as _json
    try:
        from core.remote.satellite_connection import get_connection_manager
        cm = get_connection_manager()
        conn = cm.get_connection(machine_id)
        if conn is None:
            return  # offline; will catch via close code 4006
        # Send the uninstall message directly + synchronously so the bytes
        # are on the wire before we close. Bypass the writer task queue.
        try:
            await conn.ws.send_text(_json.dumps({"type": "uninstall"}))
            # Tiny grace period to let the bytes flush — websockets buffers
            # the frame internally and `send_text` returns before TCP write.
            await _asyncio.sleep(0.2)
        except Exception:
            logger.exception(
                "Failed to send uninstall to satellite %s",
                machine_id[:8],
            )
        # Force-close the WS with code 4006 so the satellite knows the
        # machine was deleted (fallback path if uninstall message somehow
        # didn't arrive — satellite's WS client handles 4006 by running
        # self-uninstall too, belt-and-suspenders).
        with contextlib.suppress(Exception):
            await conn.ws.close(code=4006, reason="machine_deleted")
        await cm.deregister(machine_id)
    except Exception:
        logger.exception(
            "_trigger_self_uninstall failed for %s", machine_id[:8],
        )


@router.get("/v1/users/me/remote-targets")
async def list_my_remote_targets(
    user: UserContext | None = Depends(get_current_user),
):
    """List the user's per-agent remote-target rows.

    The legacy ``agent_slug=''`` global override mode is gone —
    users select per-agent in the dashboard UI.
    """
    u = _require_user_authenticated(user)
    targets = remote_store.get_user_remote_targets(u.sub)
    return {"targets": targets}


@router.put("/v1/users/me/remote-targets/{agent_slug}")
async def set_my_per_agent_target(
    agent_slug: str,
    body: SetUserTargetRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Pin one agent to one of the user's paired machines.

    Validation chain (admins bypass roles/access checks):
      1. The user must have access to ``agent_slug`` (intersect ``user_agents``).
      2. The target machine must exist.
      3. The machine must be owned by THIS user (registered_by == sub) OR
         be an admin-shared machine — but non-admin users
         cannot target admin-shared machines anyway. Enforce ownership.
    """
    u = _require_user_authenticated(user)
    if not agent_slug:
        raise HTTPException(status_code=400, detail="agent_slug required")

    # Access gate: user must be assigned to the agent.
    from storage import database as _db
    if u.role != "admin":
        roles = _db.get_user_agent_roles(u.sub)
        if agent_slug not in roles:
            raise HTTPException(
                status_code=403,
                detail="Not assigned to this agent",
            )

    machine = remote_store.get_remote_machine(body.machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    # Ownership gate: non-admins can only target machines they paired.
    if u.role != "admin" and machine.get("registered_by") != u.sub:
        raise HTTPException(status_code=403, detail="Not your machine")

    # Shared-only (agent-scoped) agents have ONE shared chat history across ALL
    # users — they may not be pinned to a personal (user-paired) machine, which
    # must hold only that user's own scoped chats. They run on admin machines
    # only. (If the agent later leaves shared-only mode, personal overrides are
    # allowed again; switching INTO shared-only purges existing ones.)
    if (machine.get("pairing_scope") or "") == "user":
        from core.session import visibility
        if visibility.is_shared_only(agent_slug):
            raise HTTPException(
                status_code=400,
                detail=("This agent is shared-only (agent-scoped): its chats are "
                        "shared across all users, so it can't run on a personal "
                        "machine. Shared-only agents run on admin machines only."),
            )

    try:
        remote_store.set_user_remote_target(u.sub, body.machine_id, agent_slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _kick_presync(body.machine_id, agent_slug)
    return {"ok": True}


@router.delete("/v1/users/me/remote-targets/{agent_slug}")
async def remove_my_per_agent_target(
    agent_slug: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Remove the user's target row for one agent. Idempotent (200 even if
    no row exists)."""
    u = _require_user_authenticated(user)
    if not agent_slug:
        raise HTTPException(status_code=400, detail="agent_slug required")
    remote_store.remove_user_remote_target(u.sub, agent_slug)
    return {"ok": True}
