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
import re
import tarfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_admin, require_auth
from services.remote.remote_status import get_live_machine_status
from core import placement
from core import host_os
from storage import remote_store
from storage.agents import agent_store
from storage.pg import run_db
from auth import roles


def _merge_live_status(machine: dict) -> dict:
    """Overlay in-memory WS status over the DB row so clients see reality.

    The DB `status` column is updated on lifecycle events only and lags the
    live connection state. Callers should never return the raw DB `status`
    to API clients; use this wrapper instead.

    Runs on the loop: the row the caller already holds is handed to the
    status helper so it never re-reads the store for an offline machine.
    """
    live = get_live_machine_status(machine["id"], machine=machine)
    machine["status"] = live["state"]
    machine["last_heartbeat_age_s"] = live["last_heartbeat_age_s"]
    machine["reachable"] = live["reachable"]
    if live["last_seen_iso"]:
        machine["last_seen"] = live["last_seen_iso"]
    return machine

logger = logging.getLogger("claude-proxy.remote-machines")
router = APIRouter()


def _cli_pins_by_binary() -> dict[str, str]:
    """``{binary: pinned version}`` for every engine with a CLI — the machine
    cards judge ``capabilities.cli_status`` (keyed by binary name) against
    these. Read at call time, never frozen at import."""
    from core.session.session_manager import get_all_layers
    return {
        layer.capabilities.runtime.binary: layer.pinned_cli_version()
        for layer in get_all_layers().values()
        if layer.capabilities.runtime.binary
    }


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

    # The ``?os=`` word names the install flavour (``core/host_os.py``
    # folds every accepted spelling to a family): ``linux`` and ``macos``
    # both get the bash bootstrap (install.sh detects the host with uname),
    # ``windows`` the PowerShell one.
    os_hint = request.query_params.get("os", "linux").lower()
    family = host_os.bootstrap_family(os_hint)
    if not family:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported os: {os_hint!r}. Expected linux, macos, or windows.",
        )

    # Atomic exchange: validates token, returns machine_secret, clears
    # the pairing token from the DB (one-time use). Failure modes:
    # invalid token, expired token, already-exchanged token.
    def _exchange() -> tuple[str, str] | None:
        import hashlib as _h
        token_hash = _h.sha256(token.encode()).hexdigest()
        with __import__("storage.pg", fromlist=["get_conn"]).get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM remote_machines WHERE pairing_token_hash = %s",
                (token_hash,),
            ).fetchone()
        if not row:
            return None
        return row["id"], remote_store.exchange_pairing_token(
            machine_id=row["id"], pairing_token=token,
        )

    try:
        exchanged = await run_db(_exchange)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    if not exchanged:
        raise HTTPException(
            status_code=401,
            detail="Invalid or already-exchanged pairing token",
        )
    machine_id, machine_secret = exchanged

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
        machine_id, machine_secret, platform_url, family,
    )
    media_type = (
        "text/plain"
        if host_os.ROWS[family].powershell
        else "text/x-shellscript"
    )
    return Response(
        content=body,
        media_type=media_type,
        headers={"Cache-Control": "no-cache"},
    )


def _build_bootstrap_script(
    machine_id: str, machine_secret: str, platform_url: str,
    family: str = host_os.LINUX,
) -> bytes:
    """Generate the self-extracting bootstrap script body for a host family.

    Layout (both flavors):
      1. Shebang + env-var exports (MACHINE_ID, MACHINE_SECRET, PLATFORM_URL)
      2. Two base64 payloads: baseline-tools script + satellite tarball
      3. The static install template body (install.sh or install.ps1)

    ``family`` is a word of ``host_os.FAMILIES``; the row's ``powershell``
    fact picks the PowerShell template and header (Windows) over the bash
    ones (Linux and macOS alike — install.sh detects the host with uname).
    """
    import base64 as _b64

    tarball = _build_satellite_tarball()
    tarball_b64 = _b64.b64encode(tarball).decode("ascii")

    scripts_dir = _SATELLITE_DIR.parent / "scripts"
    row = host_os.ROWS[family]

    if row.powershell:
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

    if row.powershell:
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


def _cli_pin_env_exports() -> list[tuple[str, str]]:
    """``[(env name, pinned version)]`` for every engine with a CLI, in
    registry order, skipping an empty pin. The env name is the engine's
    ``pin_key`` upper-cased plus ``_VERSION`` — ``CLAUDE_CODE_VERSION`` /
    ``CODEX_VERSION``: the row ``VERSIONS.md`` carries and the name
    ``install-baseline-tools.{sh,ps1}`` read (a contract test pins the
    spelling to the pin key). Read at call time, never frozen at import."""
    from core.session.session_manager import get_all_layers
    out: list[tuple[str, str]] = []
    for layer in get_all_layers().values():
        pin_key = layer.capabilities.runtime.pin_key
        version = layer.pinned_cli_version()
        if pin_key and version:
            out.append((f"{pin_key.upper()}_VERSION", version))
    return out


def _build_bash_header(
    machine_id: str, machine_secret: str, platform_url: str,
    baseline_b64: str, tarball_b64: str, helper_b64: str = "",
) -> str:
    def _esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")

    # CLI pins ride along (exported — the baseline script runs as a CHILD
    # bash, unlike the payload vars below) so pairing-time installs use THIS
    # proxy's pinned versions rather than the script's baked defaults. Tiny
    # values, so MAX_ARG_STRLEN is no concern; empty pins are skipped — the
    # baseline script's ${VAR:-default} treats empty as unset.
    pin_exports = "".join(
        f'export {name}="{_esc(version)}"\n' for name, version in _cli_pin_env_exports()
    )
    return (
        "#!/bin/bash\n"
        "# Auto-generated by /v1/satellite/bootstrap. Do not edit.\n"
        "set -euo pipefail\n"
        "\n"
        f'export MACHINE_ID="{_esc(machine_id)}"\n'
        f'export MACHINE_SECRET="{_esc(machine_secret)}"\n'
        f'export PLATFORM_URL="{_esc(platform_url)}"\n'
        + pin_exports +
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
        host_os.BOOTSTRAP_LINUX: bash_cmd,
        host_os.BOOTSTRAP_MACOS: bash_cmd,
        host_os.BOOTSTRAP_WINDOWS: windows_cmd,
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

    # CLI pins ride along ($env: — visible to install-baseline-tools.ps1) so
    # pairing-time installs use THIS proxy's pinned versions rather than the
    # script's baked defaults. Only set when non-empty: the .ps1 default falls
    # back on falsy $env: values.
    pin_lines = "".join(
        f"$env:{name} = '{_esc(version)}'\n" for name, version in _cli_pin_env_exports()
    )
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
        + pin_lines +
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


class SetBrowserModeRequest(BaseModel):
    # "dedicated" (the per-agent browser profile) or "own" (the machine
    # user's signed-in Chrome/Edge/Brave through the Playwright Extension).
    # "own" needs the "browser" device grant.
    mode: str


class SetBrowserTokenRequest(BaseModel):
    # The token shown on the Playwright Extension's page; the whole
    # `PLAYWRIGHT_MCP_EXTENSION_TOKEN=…` line its copy button yields is fine.
    token: str


class SetBrowserUnattendedRequest(BaseModel):
    enabled: bool


class ExchangeTokenRequest(BaseModel):
    machine_id: str
    pairing_token: str


class AssignAgentRequest(BaseModel):
    agent_slug: str


# --- Helpers ---


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
    require_admin(user)
    _require_satellite_source()

    # Admin pairing defaults to home-only (least privilege / opt-in full-FS).
    # Even operational machines start scoped to the agent tree + the OS home;
    # the admin opts into whole-host file access via the pairing toggle when a
    # machine genuinely needs it. Existing machines keep their stored value.
    allow_full_fs = body.allow_full_fs if body.allow_full_fs is not None else False
    machine_id = str(uuid.uuid4())
    try:
        result = await run_db(
            remote_store.create_remote_machine,
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
    require_admin(user)
    machines = await run_db(remote_store.get_all_remote_machines)
    # Parse capabilities JSON + merge live WS status for frontend
    import json

    # Per-machine (not top-level): the dashboard's useRemoteMachines hook
    # returns data.machines and would drop an envelope key. Lets the cards
    # judge capabilities.cli_status against the platform's pinned versions.
    cli_pins = _cli_pins_by_binary()
    for m in machines:
        try:
            m["capabilities"] = json.loads(m.get("capabilities", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            m["capabilities"] = {}
        # device_grants is a TEXT JSON-array column → parse to a list so the
        # dashboard receives string[].
        m["device_grants"] = sorted(placement.parse_device_grants(m.get("device_grants")))
        _shape_browser_fields(m)
        m["cli_pins"] = cli_pins
        _merge_live_status(m)
    return {"machines": machines}


@router.get("/v1/admin/remote-machines/{machine_id}")
async def get_machine(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Get a single remote machine with details."""
    require_admin(user)

    def _job() -> tuple[dict | None, list[str]]:
        machine = remote_store.get_remote_machine(machine_id)
        if not machine:
            return None, []
        return machine, remote_store.get_agents_for_machine(machine_id)

    machine, assigned_agents = await run_db(_job)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    import json
    try:
        machine["capabilities"] = json.loads(machine.get("capabilities", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        machine["capabilities"] = {}
    # device_grants TEXT JSON-array → list for the dashboard.
    machine["device_grants"] = sorted(placement.parse_device_grants(machine.get("device_grants")))
    _shape_browser_fields(machine)
    machine["assigned_agents"] = assigned_agents
    machine["cli_pins"] = _cli_pins_by_binary()
    _merge_live_status(machine)
    return machine


async def _notify_owner_machine_removed(machine: dict, owner_sub: str) -> None:
    """Tell the owner of a user-paired machine that an admin removed it — the
    notification path the admin offline alerts use. A delivery failure never
    fails the delete (the row is already gone)."""
    from services.notifications import notification_manager

    name = machine.get("name") or str(machine.get("id") or "")[:8]
    try:
        await notification_manager.fire_notification(
            title=f"Remote machine removed: {name}",
            body=(
                f"An admin removed your remote machine '{name}'. The satellite "
                "will uninstall itself from it; your agents run where they run "
                "by default until you pair a machine again."
            ),
            severity="warning", scope="user", target=owner_sub,
            source="satellite", source_id=str(machine.get("id") or ""),
        )
    except Exception:
        logger.exception(
            "Failed to notify %s that their machine was removed", owner_sub[:16],
        )


@router.delete("/v1/admin/remote-machines/{machine_id}")
async def delete_machine(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a remote machine — admin- or user-paired. Resets affected
    agents to local execution.

    Also fires the self-uninstall flow on the satellite — sends
    an ``uninstall`` WS message if connected, then deletes the DB row.
    Offline satellites self-uninstall the next time they try to reconnect
    (auth handler rejects with close code 4006 ``machine_deleted``).
    A user-paired machine's owner (when it is not the caller) is notified.
    """
    u = require_admin(user)
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    await _trigger_self_uninstall(machine_id)
    await run_db(remote_store.delete_remote_machine, machine_id)
    owner = machine.get("registered_by") or ""
    if machine.get("pairing_scope") == placement.PAIRING_USER and owner and owner != u.sub:
        await _notify_owner_machine_removed(machine, owner)
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
    def _job() -> str:
        if remote_store.get_remote_machine(machine_id) is None:
            return "missing"
        if not remote_store.verify_machine_secret(machine_id, x_machine_secret):
            return "bad_secret"
        remote_store.delete_remote_machine(machine_id)
        return "deleted"

    outcome = await run_db(_job)
    if outcome == "missing":
        # Already deleted (or never existed). Return success — the
        # satellite's local cleanup already proceeded; we just confirm
        # there's no dashboard record to clean.
        return {"ok": True, "deleted": False}
    if outcome == "bad_secret":
        raise HTTPException(status_code=401, detail="Invalid machine secret")
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
    require_admin(user)

    from services.mcp import mcp_sync, mcp_registry

    # Satellite sync uses the VISIBILITY set (admin-authorized), not the
    # manager-enabled set. Rationale: pre-install on authorization so that the
    # manager's enable-toggle is instant — otherwise, the first session start
    # after a manager flips an explicit MCP on would fail because the satellite
    # hadn't installed it yet. Cost: the satellite installs MCPs that may
    # never be enabled. Acceptable for v1.
    def _job() -> set[str] | None:
        if not remote_store.get_remote_machine(machine_id):
            return None
        union: set[str] = set()
        for slug in remote_store.get_agents_for_machine(machine_id):
            for manifest in mcp_registry.get_visible_mcps_for_agent(slug):
                union.add(manifest.name)
        return union

    assigned_union = await run_db(_job)
    if assigned_union is None:
        raise HTTPException(status_code=404, detail="Machine not found")

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
    require_admin(user)

    def _job() -> dict | None:
        machine = remote_store.get_remote_machine(machine_id)
        if machine:
            remote_store.set_auto_update_enabled(machine_id, body.enabled)
        return machine

    if not await run_db(_job):
        raise HTTPException(status_code=404, detail="Machine not found")
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
    require_admin(user)

    def _job() -> dict | None:
        machine = remote_store.get_remote_machine(machine_id)
        if machine:
            remote_store.set_allow_full_fs(machine_id, body.enabled)
        return machine

    if not await run_db(_job):
        raise HTTPException(status_code=404, detail="Machine not found")
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
    require_admin(user)
    from storage.pg import run_db
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    value = body.max_sessions
    if value is not None and value < 1:
        raise HTTPException(status_code=422, detail="max_sessions must be >= 1 or empty")
    await run_db(remote_store.set_remote_machine_max_sessions, machine_id, value)
    # Refresh the per-connection cache the capacity pre-check reads (it never
    # touches the DB on the loop); a no-op for an offline machine, whose next
    # register() re-reads the row.
    from core.remote.satellite_connection import get_connection_manager
    get_connection_manager().note_max_sessions(machine_id, value)
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

    # The ownership gates sit between the read and the write, so they run
    # in the job; an HTTPException raised there reaches the client unchanged.
    def _job() -> None:
        machine = remote_store.get_remote_machine(machine_id)
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")
        if machine.get("pairing_scope") != placement.PAIRING_USER:
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

    await run_db(_job)
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
    require_admin(user)

    def _job() -> list[str] | None:
        if not remote_store.get_remote_machine(machine_id):
            return None
        grants = _validate_device_grants(body.grants)
        remote_store.set_device_grants(machine_id, grants)
        return grants

    grants = await run_db(_job)
    if grants is None:
        raise HTTPException(status_code=404, detail="Machine not found")
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

    def _job() -> list[str]:
        machine = remote_store.get_remote_machine(machine_id)
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")
        if machine.get("pairing_scope") != placement.PAIRING_USER:
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
        return grants

    grants = await run_db(_job)
    await _push_policy_update(machine_id)
    return {"ok": True, "device_grants": grants}


# --- Own-browser mode (browser-control) ---
#
# Per-machine opt-in that points the browser-control MCP at the machine
# user's signed-in browser (Playwright Extension) instead of the dedicated
# per-agent profile. Same consent register as the device grants: only the
# role that owns the machine's consent may flip it (owner for user-paired,
# admin for admin-paired — an admin never reaches a user's personal browser).
# Changes apply at the next session warmup; running sessions keep the
# browser they were built with.

_BROWSER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_BROWSER_TOKEN_PREFIX = "PLAYWRIGHT_MCP_EXTENSION_TOKEN="


def _validate_browser_mode(mode: str) -> str:
    if mode not in remote_store.BROWSER_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown browser mode {mode!r}. Valid: {list(remote_store.BROWSER_MODES)}",
        )
    return mode


def _normalize_browser_token(raw: str) -> str:
    """The extension's copy button copies the whole ``NAME=value`` line and
    people paste it quoted — accept all of that and keep only the token
    (32 random bytes, base64url without padding → 43 chars)."""
    tok = (raw or "").strip().strip("'\"").strip()
    if tok.upper().startswith(_BROWSER_TOKEN_PREFIX):
        tok = tok[len(_BROWSER_TOKEN_PREFIX):].strip().strip("'\"")
    if not _BROWSER_TOKEN_RE.match(tok):
        raise HTTPException(
            status_code=422,
            detail=(
                "That does not look like a Playwright Extension token — copy it "
                "from the extension's page (its toolbar icon) and paste it here."
            ),
        )
    return tok


# The four helpers below read or write the store: routes call them inside a
# ``run_db`` job, never on the loop.
def _browser_machine_for_admin(machine_id: str, user: UserContext | None) -> dict:
    require_admin(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine.get("pairing_scope") == placement.PAIRING_USER:
        raise HTTPException(
            status_code=403,
            detail=(
                "Own-browser mode on a user-paired machine is its owner's "
                "choice — only they can change it (Settings → Remote machines)."
            ),
        )
    return machine


def _browser_machine_for_owner(machine_id: str, user: UserContext | None) -> dict:
    u = _require_user_authenticated(user)
    machine = remote_store.get_remote_machine(machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine.get("pairing_scope") != placement.PAIRING_USER:
        raise HTTPException(
            status_code=403,
            detail=(
                "This is an admin-paired (platform) machine. Ask an admin to "
                "change its browser mode from the admin Remote Machines page."
            ),
        )
    if machine["registered_by"] != u.sub:
        raise HTTPException(status_code=403, detail="Not your machine")
    return machine


def _apply_browser_mode(machine: dict, mode: str) -> dict:
    mode = _validate_browser_mode(mode)
    granted = placement.parse_device_grants(machine.get("device_grants"))
    if mode == "own" and "browser" not in granted:
        raise HTTPException(
            status_code=422, detail="Grant browser control on this machine first.",
        )
    remote_store.set_browser_mode(machine["id"], mode)
    return {
        "ok": True,
        "browser_mode": mode,
        # The token survives a mode switch (store rule) — the flag reports
        # what is stored, whichever mode is now selected.
        "browser_extension_token_set": bool(machine.get("browser_extension_token_set")),
    }


def _apply_browser_token(machine: dict, token: str | None) -> dict:
    if token is None:
        remote_store.set_browser_extension_token(machine["id"], None)
        return {"ok": True, "browser_extension_token_set": False}
    if remote_store._parse_browser_mode(machine.get("browser_mode")) != "own":
        raise HTTPException(
            status_code=422, detail="Turn on \"Use my own browser\" first.",
        )
    remote_store.set_browser_extension_token(
        machine["id"], _normalize_browser_token(token),
    )
    return {"ok": True, "browser_extension_token_set": True}


def _apply_browser_unattended(machine: dict, enabled: bool) -> dict:
    """Whether unattended sessions (tasks, calls, meetings) get the saved
    own-browser token; off, a session a person drives still does."""
    if enabled and remote_store._parse_browser_mode(machine.get("browser_mode")) != "own":
        raise HTTPException(
            status_code=422, detail="Turn on \"Use my own browser\" first.",
        )
    remote_store.set_browser_unattended(machine["id"], enabled)
    return {"ok": True, "browser_unattended": bool(enabled)}


@router.put("/v1/admin/remote-machines/{machine_id}/browser-mode")
async def admin_set_browser_mode(
    machine_id: str,
    body: SetBrowserModeRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Set the browser-control mode of an ADMIN-paired machine. Body:
    ``{mode: "dedicated" | "own"}``; ``own`` needs the ``browser`` grant
    (422). The stored extension token survives a switch back to ``dedicated``;
    it is only delivered while the mode is ``own``. User-paired machines reject
    with 403 (the owner decides)."""
    def _job() -> dict:
        return _apply_browser_mode(_browser_machine_for_admin(machine_id, user), body.mode)

    return await run_db(_job)


@router.put("/v1/admin/remote-machines/{machine_id}/browser-token")
async def admin_set_browser_token(
    machine_id: str,
    body: SetBrowserTokenRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Store the Playwright Extension token of an ADMIN-paired machine in
    ``own`` mode (422 otherwise). The value is encrypted at rest and never
    returned; ``browser_extension_token_set`` on the machine row says whether
    one is stored."""
    def _job() -> dict:
        return _apply_browser_token(_browser_machine_for_admin(machine_id, user), body.token)

    return await run_db(_job)


@router.delete("/v1/admin/remote-machines/{machine_id}/browser-token")
async def admin_clear_browser_token(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Forget the stored extension token — sessions go back to asking for a
    click in the browser."""
    def _job() -> dict:
        return _apply_browser_token(_browser_machine_for_admin(machine_id, user), None)

    return await run_db(_job)


@router.put("/v1/admin/remote-machines/{machine_id}/browser-unattended")
async def admin_set_browser_unattended(
    machine_id: str,
    body: SetBrowserUnattendedRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Let unattended sessions on an ADMIN-paired machine use its saved
    own-browser token (``{enabled}``; turning it on needs ``own`` mode)."""
    def _job() -> dict:
        return _apply_browser_unattended(_browser_machine_for_admin(machine_id, user), body.enabled)

    return await run_db(_job)


@router.put("/v1/users/me/remote-machines/{machine_id}/browser-unattended")
async def user_set_browser_unattended(
    machine_id: str,
    body: SetBrowserUnattendedRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Owner-scoped twin of the admin route."""
    def _job() -> dict:
        return _apply_browser_unattended(_browser_machine_for_owner(machine_id, user), body.enabled)

    return await run_db(_job)


@router.put("/v1/users/me/remote-machines/{machine_id}/browser-mode")
async def user_set_browser_mode(
    machine_id: str,
    body: SetBrowserModeRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Owner-scoped browser-control mode (user-paired machines only; 403 for
    admin-paired and for anyone but the pairing user). Same body + rules as
    the admin route."""
    def _job() -> dict:
        return _apply_browser_mode(_browser_machine_for_owner(machine_id, user), body.mode)

    return await run_db(_job)


@router.put("/v1/users/me/remote-machines/{machine_id}/browser-token")
async def user_set_browser_token(
    machine_id: str,
    body: SetBrowserTokenRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Owner-scoped: store the extension token of the caller's own machine."""
    def _job() -> dict:
        return _apply_browser_token(_browser_machine_for_owner(machine_id, user), body.token)

    return await run_db(_job)


@router.delete("/v1/users/me/remote-machines/{machine_id}/browser-token")
async def user_clear_browser_token(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Owner-scoped: forget the stored extension token."""
    def _job() -> dict:
        return _apply_browser_token(_browser_machine_for_owner(machine_id, user), None)

    return await run_db(_job)


def _shape_browser_fields(machine: dict) -> None:
    """Public shape of the two own-browser columns on a machine row: the mode
    normalised, the token reduced to a presence flag (the ciphertext never
    leaves the store)."""
    machine["browser_mode"] = remote_store._parse_browser_mode(machine.get("browser_mode"))
    machine["browser_extension_token_set"] = bool(machine.get("browser_extension_token_set"))
    machine["browser_unattended"] = bool(machine.get("browser_unattended"))


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

    Only the row read leaves the loop; the session-state refresh and the
    send stay on it.
    """
    machine = await run_db(remote_store.get_remote_machine, machine_id) or {}
    allow_full_fs = bool(machine.get("allow_full_fs") or False)
    device_grants = sorted(placement.parse_device_grants(machine.get("device_grants")))
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


#: The refusal of an update to a machine on a plaintext link, in the words of
#: the auth path's (``ws/satellite.py``): the satellite takes no code over it.
_PLAINTEXT_UPDATE_REFUSAL = (
    "This machine's link is plaintext, so no update is pushed to it: "
    "re-run the installer there."
)


def _plaintext_link(conn, machine: dict) -> bool:
    """Whether the machine runs on a plaintext link it opted into
    (``insecure_transport``): the live connection's capabilities, else the
    ones it last reported (the row's JSON column)."""
    if conn is not None:
        caps = getattr(conn, "capabilities", None) or {}
    else:
        caps = machine.get("capabilities") or {}
        if isinstance(caps, str):
            import json
            try:
                caps = json.loads(caps)
            except ValueError:
                caps = {}
    return isinstance(caps, dict) and bool(caps.get("insecure_transport"))


async def _do_trigger_update_now(machine_id: str, machine: dict) -> dict:
    """Shared logic for the admin and user "Update now" endpoints.
    Pushes the tarball if connected, else sets pending_update for next
    reconnect. A machine on a plaintext link it opted into takes no code
    over it (the satellite refuses ``update_required``, and the auth path
    never pushes to it): 409, nothing sent, queued or announced. Caller is
    responsible for authorization."""
    _require_satellite_source()
    from core.remote.satellite_connection import get_connection_manager
    from ws.satellite import (
        SATELLITE_VERSION_LATEST,
        _broadcast_satellite_updating,
        note_update_pushed,
    )
    import base64 as _b64

    cm = get_connection_manager()
    connected = cm.is_connected(machine_id)
    live = cm.get_connection(machine_id) if connected else None
    if _plaintext_link(live, machine):
        raise HTTPException(status_code=409, detail=_PLAINTEXT_UPDATE_REFUSAL)
    if connected:
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
                # Same rollback detection + budget as the automatic push:
                # if the satellite comes back below the target, the next
                # reconnect records/announces it (ws/satellite.py).
                note_update_pushed(machine_id, SATELLITE_VERSION_LATEST)
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
    await run_db(remote_store.set_pending_update, machine_id, True)
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
    require_admin(user)
    machine = await run_db(remote_store.get_remote_machine, machine_id)
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
    require_admin(user)

    def _job() -> None:
        machine = remote_store.get_remote_machine(machine_id)
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

        # Per-user satellite isolation: agent default execution targets must
        # point at admin-paired machines. User-paired machines belong to one
        # user's personal sessions only; allowing them as an agent default
        # would let any session on that agent expose data to a user-owned
        # satellite the rest of the agent's users can't see.
        if not placement.machine_is_admin_paired(machine):
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

        # An engine that cannot run on a satellite cannot be assigned one
        agent = agent_store.get_agent(body.agent_slug)
        from core.session.session_manager import get_layer_capabilities
        _ac = get_layer_capabilities((agent or {}).get("execution_path") or "") if agent else None
        if _ac is not None and not _ac.runtime.supports_remote_execution:
            raise HTTPException(
                status_code=400,
                detail=f"{_ac.display_name} agents always run locally (API calls, no subprocess)",
            )

        remote_store.set_agent_remote_target(
            agent_slug=body.agent_slug,
            machine_id=machine_id,
            added_by=user.sub,
        )

    await run_db(_job)
    _kick_presync(machine_id, body.agent_slug)
    return {"ok": True}


@router.delete("/v1/admin/remote-machines/{machine_id}/agents/{agent_slug}")
async def unassign_agent(
    machine_id: str,
    agent_slug: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Remove an agent's remote target, resetting it to local execution."""
    require_admin(user)
    await run_db(remote_store.remove_agent_remote_target, agent_slug)
    return {"ok": True}


# --- User-level endpoints (managers+) ---


class SetUserTargetRequest(BaseModel):
    machine_id: str


def _require_creator(user: UserContext | None) -> UserContext:
    """Require a real user (not API key) with creator+ role."""
    u = require_auth(user)
    if getattr(u, "is_api_key", False):
        raise HTTPException(status_code=403, detail="User authentication required (not API key)")
    if not roles.is_creator_or_above(u.role):
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

    def _job() -> tuple[list[dict], list[dict]]:
        return (
            remote_store.get_visible_machines_for_user(
                u.sub, include_admin_paired=u.is_admin,
            ),
            remote_store.get_user_remote_targets(u.sub),
        )

    machines, targets = await run_db(_job)
    import json
    for m in machines:
        try:
            m["capabilities"] = json.loads(m.get("capabilities", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            m["capabilities"] = {}
        # device_grants is a TEXT JSON-array column → parse to a list so the
        # dashboard receives string[].
        m["device_grants"] = sorted(placement.parse_device_grants(m.get("device_grants")))
        _shape_browser_fields(m)
        _merge_live_status(m)
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

    from storage import database as _db

    # User pairing defaults to home-only (the safer baseline for
    # personal laptops). The user can flip the toggle in the pairing
    # modal to opt into full-FS access.
    allow_full_fs = body.allow_full_fs if body.allow_full_fs is not None else False
    machine_id = str(uuid.uuid4())

    def _job() -> dict | None:
        # Admin kill-switch. When off, user pairing is refused outright.
        if _db.get_platform_setting("allow_user_paired_machines") == "0":
            return None
        return remote_store.create_remote_machine(
            machine_id=machine_id,
            name=body.name.strip(),
            registered_by=u.sub,
            pairing_scope="user",
            allow_full_fs=allow_full_fs,
        )

    try:
        result = await run_db(_job)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if result is None:
        raise HTTPException(
            status_code=403,
            detail="User-paired machines are disabled by your administrator.",
        )

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
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine.get("pairing_scope") != placement.PAIRING_USER:
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
    await run_db(remote_store.delete_remote_machine, machine_id)
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

    def _job() -> None:
        machine = remote_store.get_remote_machine(machine_id)
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")
        if machine["registered_by"] != u.sub and not u.is_admin:
            raise HTTPException(status_code=403, detail="Not your machine")
        remote_store.set_auto_update_enabled(machine_id, body.enabled)

    await run_db(_job)
    return {"ok": True, "auto_update_enabled": body.enabled}


@router.post("/v1/users/me/remote-machines/{machine_id}/update-now")
async def trigger_my_machine_update_now(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Mirror of the admin update-now trigger for owners of user-paired
    machines."""
    u = _require_user_authenticated(user)
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine["registered_by"] != u.sub and not u.is_admin:
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
    targets = await run_db(remote_store.get_user_remote_targets, u.sub)
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

    from storage import database as _db

    def _job() -> None:
        # Access gate: user must be assigned to the agent.
        if not u.is_admin:
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
        if not u.is_admin and machine.get("registered_by") != u.sub:
            raise HTTPException(status_code=403, detail="Not your machine")

        # Shared-only (agent-scoped) agents have ONE shared chat history across ALL
        # users — they may not be pinned to a personal (user-paired) machine, which
        # must hold only that user's own scoped chats. They run on admin machines
        # only. (If the agent later leaves shared-only mode, personal overrides are
        # allowed again; switching INTO shared-only purges existing ones.)
        if (machine.get("pairing_scope") or "") == placement.PAIRING_USER:
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

    await run_db(_job)
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
    await run_db(remote_store.remove_user_remote_target, u.sub, agent_slug)
    return {"ok": True}
