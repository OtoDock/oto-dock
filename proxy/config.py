"""Platform configuration — config.env parsing, secrets, and derived settings.

The facade every importer reads as ``config.<name>``. The ``configuration``
package holds the model registry and the model a session runs
(``configuration.models``), the system prompt's rendering
(``configuration.prompts``) and the value parsers with the key registry
(``configuration.readers``); this module re-exports their names. A test
patches a moved name in the module whose code reads it. Everything that runs
at import stays here, in its order: the config.env guards and load, every key
read, the generated secrets, the unknown-key warning last.
"""

import contextlib
import hmac
import http.client
import json
import os
import re as _re
import secrets
import shutil
import stat
import sys
import zoneinfo
from datetime import datetime
from pathlib import Path

import psycopg
from dotenv import dotenv_values

from configuration.models import (
    DEFAULT_EFFORT_LEVEL as DEFAULT_EFFORT_LEVEL,
    MODEL_DEFAULT_CONTEXT_WINDOW as MODEL_DEFAULT_CONTEXT_WINDOW,
    MODEL_DEFAULT_PRICING as MODEL_DEFAULT_PRICING,
    MODEL_GOOD_AT_MAX_CHARS as MODEL_GOOD_AT_MAX_CHARS,
    MODEL_REGISTRY as MODEL_REGISTRY,
    MODEL_SUCCESSORS as MODEL_SUCCESSORS,
    MODEL_TIER_LABELS as MODEL_TIER_LABELS,
    PROVIDER_DEFAULT_PRICING as PROVIDER_DEFAULT_PRICING,
    WEB_SEARCH_USD_PER_REQUEST as WEB_SEARCH_USD_PER_REQUEST,
    get_agent_model as get_agent_model,
    get_cli_effort as get_cli_effort,
    get_cli_model as get_cli_model,
    get_layer_models as get_layer_models,
    get_model_context_window as get_model_context_window,
    get_model_layers as get_model_layers,
    get_model_long_context as get_model_long_context,
    get_model_pricing as get_model_pricing,
    get_model_provider as get_model_provider,
    get_model_supports_ultra as get_model_supports_ultra,
    get_model_supports_xhigh as get_model_supports_xhigh,
    get_model_tier as get_model_tier,
    live_model_id as live_model_id,
    model_catalog_sort_key as model_catalog_sort_key,
    model_supports_reasoning as model_supports_reasoning,
    model_supports_server_tools as model_supports_server_tools,
    price_usage as price_usage,
    resolve_agent_model as resolve_agent_model,
    resolve_layer_default_model as resolve_layer_default_model,
    resolve_run_model as resolve_run_model,
    successor_model as successor_model,
)
from configuration.prompts import (
    build_agent_prompt as build_agent_prompt,
    fold_unavailable_mcps as fold_unavailable_mcps,
    persona_is_unconfigured as persona_is_unconfigured,
)
from configuration.readers import (
    DEFAULT_NO_PROXY as DEFAULT_NO_PROXY,
    EGRESS_PROXY_KEYS as EGRESS_PROXY_KEYS,
    FOREIGN_CONFIG_KEYS as FOREIGN_CONFIG_KEYS,
    FOREIGN_CONFIG_PREFIXES as FOREIGN_CONFIG_PREFIXES,
    INTERNAL_CONFIG_KEYS as INTERNAL_CONFIG_KEYS,
    LAZY_CONFIG_KEYS as LAZY_CONFIG_KEYS,
    _parse_bool as _parse_bool,
    _parse_float as _parse_float,
    _parse_int as _parse_int,
    export_egress_proxy as export_egress_proxy,
    unknown_config_keys as unknown_config_keys,
)

# Paths (needed early for config.env resolution)
BASE_DIR = Path(__file__).parent
PLATFORM_ROOT = BASE_DIR.parent  # oto-dock/


def _boot_note(msg: str) -> None:
    """Import-time boot notice — logging isn't configured yet, so write to
    stderr, which reaches the container/systemd log."""
    sys.stderr.write(msg + "\n")


# ---------------------------------------------------------------------------
# Pinned CLI versions — VERSIONS.md is the single source of truth.
# ---------------------------------------------------------------------------
# The platform pins the EXACT Claude Code / Codex CLI versions it runs against.
# Satellites reconcile their installed CLIs to these on every WS auth, and the
# CLIs' own auto-update is disabled (see core/sandbox/sandbox.py + env_builder.py), so a
# fleet machine can't drift off the pin. We read the values straight out of
# VERSIONS.md so a one-line edit there propagates everywhere (install scripts
# read the same file). Missing/garbled VERSIONS.md → empty string → the
# reconcile becomes a no-op (fail-open), never a crash.
_VERSIONS_MD = PLATFORM_ROOT / "VERSIONS.md"


def _read_pinned_version(key: str) -> str:
    """Parse ``KEY=x.y.z`` out of VERSIONS.md. Returns "" if absent/unreadable."""
    try:
        text = _VERSIONS_MD.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = _re.search(rf"^\s*{_re.escape(key)}\s*=\s*([^\s#]+)\s*$", text, _re.MULTILINE)
    return m.group(1).strip() if m else ""


PINNED_CLAUDE_CODE_VERSION = _read_pinned_version("CLAUDE_CODE_VERSION")
PINNED_CODEX_VERSION = _read_pinned_version("CODEX_VERSION")
PINNED_OTODOCK_VERSION = _read_pinned_version("OTODOCK_VERSION")  # platform version (VERSIONS.md)

# Production deploys split persistent data (agents, db, sessions) and config (.env)
# from the application code. Set PLATFORM_DATA_DIR and PLATFORM_CONFIG_DIR to
# override (e.g. /var/lib/otodock and /etc/otodock). In dev (env unset) both
# default to PLATFORM_ROOT so behavior is unchanged from the current layout.
PLATFORM_DATA_DIR = Path(os.environ.get("PLATFORM_DATA_DIR") or str(PLATFORM_ROOT))
PLATFORM_CONFIG_DIR = Path(os.environ.get("PLATFORM_CONFIG_DIR") or str(PLATFORM_ROOT))

# Read shared platform config by parsing config.env directly (this module
# never writes os.environ). os.environ is checked first so Docker/systemd env
# overrides win — note the shipped proxy.service ALSO loads config.env via
# EnvironmentFile= (needed for pre-config keys like PLATFORM_DATA_DIR above),
# so under systemd the keys do sit in the proxy's process env. Agent
# subprocesses never inherit them either way: the sandbox builds a curated
# env from scratch (core/sandbox/env_builder.py).
# Lives in PLATFORM_CONFIG_DIR (e.g. /etc/otodock/config.env) — defaults to
# PLATFORM_ROOT so dev + the bundled compose (which bind-mount config.env at the
# platform root) are unchanged.
_config_env = PLATFORM_CONFIG_DIR / "config.env"


def _reject_directory_config(path: Path) -> None:
    """Fail fast (clear message, no traceback) when the config path is a
    directory instead of a file.

    The bundled compose bind-mounts ``${OTODOCK_ENV_FILE:-./.env}`` onto this
    path — and when that host file doesn't exist at ``docker compose up``,
    Docker silently creates an empty *directory* in its place. dotenv then
    reads a directory as "no config" (every key falls back to its default) and
    the proxy only dies later, at the first secret persist, with a bare
    IsADirectoryError crash-loop that says nothing about the cause. Upgrades
    from installs that kept their settings in ``config.env`` (the pre-1.2.0
    name) hit exactly this.
    """
    if not path.is_dir():
        return
    raise SystemExit(
        f"FATAL: the OtoDock config file at {path} is a directory, not a file.\n"
        "Docker creates an empty directory at a bind-mount source that does not\n"
        "exist — the compose file mounts ${OTODOCK_ENV_FILE:-./.env} here, so this\n"
        "means there was no .env file next to docker-compose.yml at compose up.\n"
        "Fix it on the Docker host, in the directory with docker-compose.yml:\n"
        "    docker compose down     # release the mount so the dir can be removed\n"
        "    rmdir .env              # remove the empty directory Docker created\n"
        "    mv config.env .env      # upgrading and your settings live in config.env\n"
        "    # fresh install instead: cp config.env.example .env  (then edit it)\n"
        "    docker compose up -d\n"
        "(Source checkouts use scripts/compose.sh, which points the mount at\n"
        "config.env via OTODOCK_ENV_FILE and is not affected.)"
    )


def _reject_unreadable_config(path: Path) -> None:
    """Fail fast with the fix when ``config.env`` exists but this account
    cannot read it (a tightened file owned by another account)."""
    if path.exists() and not os.access(path, os.R_OK):
        raise SystemExit(
            f"FATAL: the OtoDock config file at {path} is not readable by uid {os.getuid()}.\n"
            "It holds every secret: make it owned by the account the proxy runs as, mode 600."
        )


_reject_directory_config(_config_env)
_reject_unreadable_config(_config_env)
_file_cfg = dotenv_values(_config_env) if _config_env.exists() else {}
# Before any client exists: the outbound clients read the egress proxy from
# the environment, which a Docker install's .env never reaches by itself.
export_egress_proxy(_file_cfg)


# Every key read through _cfg at import: the unknown-key warning at the end
# of this module compares config.env's keys against it.
_CFG_KEYS_READ: set[str] = set()


def _cfg(key: str, default: str = "") -> str:
    """Read config: os.environ (Docker/systemd override) > config.env file > default."""
    _CFG_KEYS_READ.add(key)
    return os.environ.get(key) or _file_cfg.get(key) or default


def _cfg_bool(key: str, default: bool | None) -> bool | None:
    return _parse_bool(key, _cfg(key, ""), default)


def _cfg_int(key: str, default: int, *, minimum: int | None = None) -> int:
    return _parse_int(key, _cfg(key, ""), default, minimum)


def _cfg_float(key: str, default: float, *, minimum: float | None = None) -> float:
    return _parse_float(key, _cfg(key, ""), default, minimum)


# Is the PROXY ITSELF running inside a container? Distinct from OTODOCK_CLOUD
# (which is the multi-tenant SaaS axis): RUNNING_IN_DOCKER is the bare-metal
# (T1) vs Docker-Compose (T2) split. Drives how the proxy reaches the Docker
# daemon and Docker-MCP service URLs (the T2 socket-proxy backend).
# Explicit env wins; ``/.dockerenv`` is only a fallback (absent in some k8s
# runtimes). No-op on bare-metal (env unset + no /.dockerenv → False), so the
# live native install is unaffected.
def _default_running_in_docker() -> bool:
    import os
    return os.path.exists("/.dockerenv")

_running_in_docker_env = _cfg_bool("RUNNING_IN_DOCKER", None)
if _running_in_docker_env is None:
    RUNNING_IN_DOCKER = _default_running_in_docker()
else:
    RUNNING_IN_DOCKER = _running_in_docker_env


def _write_config_env(text: str, *, append: bool) -> None:
    """Write ``config.env``: it holds every master secret, so a new file is
    created 0600, and on bare metal an existing one is set to 0600 on its
    open descriptor. In a container the file is the host's bind-mounted
    ``.env``, which the host's ``docker compose`` must still read: its mode
    is the operator's (``_tighten_config_mode`` says when it is too open)."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    with os.fdopen(os.open(_config_env, flags, 0o600), "w") as f:
        f.write(text)
        if not RUNNING_IN_DOCKER:
            with contextlib.suppress(OSError):  # a file owned by another uid keeps its mode
                os.fchmod(f.fileno(), 0o600)


def _tighten_config_mode(path: Path, *, in_container: bool) -> None:
    """Close a ``config.env`` other accounts can read (the umask left 0664
    on bare-metal installs). On bare metal a file this process owns is set
    to 0600; one owned by another account (``root:otodock 0640`` is a valid
    layout) is only reported when everyone can read it. In a container the
    host fixes it. Never fatal: a read-only mount is a valid layout."""
    try:
        st = path.stat()
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    mode = stat.S_IMODE(st.st_mode)
    if in_container:
        if mode & 0o077:
            _boot_note(f"WARNING: the .env mounted at {path} is readable by other accounts "
                       f"(mode {mode:o}); it holds every secret. On the Docker host: chmod 600 .env "
                       "(keep it owned by the account that runs docker compose)")
        return
    if st.st_uid == os.geteuid():
        if mode & 0o077:
            try:
                os.chmod(path, 0o600)
                _boot_note(f"config.env at {path} was readable by other accounts; set to 0600")
                return
            except OSError:
                pass
        else:
            return
    elif not mode & 0o007:
        return
    _boot_note(f"WARNING: config.env at {path} is readable by other accounts (mode {mode:o}); "
               "it holds every secret: make it owned by the account the proxy runs as with "
               "mode 600 (or 640 with a group only the proxy's services share)")


_tighten_config_mode(_config_env, in_container=RUNNING_IN_DOCKER)


def _persist_secret(key: str, value: str) -> None:
    """Auto-generate + persist a secret to config.env.

    Runs at import. In the containerised (uid 1000) deployment the bind-mounted
    config.env must be writable by the runtime user — the compose init sidecar
    chowns it to 1000 before the proxy starts. If that didn't happen, a bare
    ``open(..,"a")`` would raise EACCES and crash the proxy before any app code;
    we convert that into an actionable error instead.
    """
    _file_cfg[key] = value  # available to later _cfg() reads in this module
    existing = _config_env.read_text() if _config_env.exists() else ""
    lines = existing.splitlines()
    blank_line_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            if stripped[len(key) + 1:].strip().strip("'\""):
                return  # already persisted — no write needed
            # A bare ``KEY=`` line (the copy-config.env.example-and-edit
            # path) reads as empty, so the caller regenerates the secret —
            # but the key "existing" here used to count as persisted,
            # rotating JWT_SECRET/PROXY_API_KEY/VAPID_* on every restart.
            # Rewrite the blank line in place instead.
            blank_line_idx = i
            break
    try:
        if blank_line_idx is not None:
            lines[blank_line_idx] = f"{key}={value}"
            _write_config_env("\n".join(lines) + "\n", append=False)
        else:
            _write_config_env(f"{key}={value}\n", append=True)
    except OSError as e:
        _uid = os.getuid() if hasattr(os, "getuid") else "?"
        raise RuntimeError(
            f"Cannot persist generated {key}: config.env at {_config_env} is not "
            f"writable by uid {_uid} ({e}). In Docker, ensure the init sidecar "
            f"chowns config.env to the proxy's uid; or pre-set {key} in the "
            f"environment so no write is needed."
        ) from e


def collabora_ssl_termination_for(url: str) -> str:
    """Collabora's ``ssl.termination`` for a public URL: ``true`` for https."""
    return "true" if (url or "").strip().lower().startswith("https://") else "false"


def _set_config_line(key: str, value: str) -> None:
    """Every active ``KEY=`` line of config.env (``export`` too) set to
    ``value``, else one appended line; the other lines are kept as they are."""
    _file_cfg[key] = value
    lines = (_config_env.read_text() if _config_env.exists() else "").splitlines()
    found = False
    for i, line in enumerate(lines):
        bare = line.strip()
        if bare.startswith("export "):
            bare = bare[len("export "):].lstrip()
        if "=" in bare and bare.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            found = True
    if found:
        _write_config_env("\n".join(lines) + "\n", append=False)
    else:
        _write_config_env(f"{key}={value}\n", append=True)


def capture_dashboard_public_url(origin: str) -> bool:
    """First-run capture of DASHBOARD_PUBLIC_URL (setup wizard).

    Captures ONLY when the operator never set the value in config.env (the
    FILE is the explicit user setting — compose always injects a localhost
    default via the container environment, so an env check can never read
    "unset") AND the effective value is empty or that localhost default.
    A localhost origin is never captured (it IS the default). Explicit
    config always wins; the new value applies at the next platform restart
    (the proxy sends it to Collabora as the Host; Collabora reads the scheme,
    ``COLLABORA_SSL_TERMINATION``, written beside it, at its start)."""
    origin = (origin or "").strip().rstrip("/")
    _localhost = _re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")
    if not _re.match(r"^https?://[^\s/]+$", origin) or _localhost.match(origin):
        return False
    if (_file_cfg.get("DASHBOARD_PUBLIC_URL") or "").strip():
        return False
    effective = _cfg("DASHBOARD_PUBLIC_URL").strip().rstrip("/")
    if effective and not _localhost.match(effective):
        return False
    _persist_secret("DASHBOARD_PUBLIC_URL", origin)
    _set_config_line("COLLABORA_SSL_TERMINATION", collabora_ssl_termination_for(origin))
    return True

# Normalized once (an operator may spell PLATFORM_DATA_DIR with ``..``), so
# ``get_agent_dir``'s normalized answer stays ``AGENTS_DIR / name``.
AGENTS_DIR = Path(os.path.normpath(PLATFORM_DATA_DIR / "agents"))  # persistent — overridable via PLATFORM_DATA_DIR
# Workspace recover-bin (passive trash-can): the pre-change bytes of a deleted
# file or the losing side of a genuine cross-user concurrent conflict are backed
# up here so they stay recoverable. Reaped after 7 days. Sibling of agents/ so it
# sits OUTSIDE any agent tree (never manifested/synced).
RECOVER_BIN_DIR = PLATFORM_DATA_DIR / "recover-bin"
# Version-pinned document-preview snapshots (services/media/preview_snapshots.py):
# proxy-private copies of a file as it was when a preview was pushed, so the
# dashboard's "previous version" block shows trustworthy history. Sibling of
# agents/ so no agent or satellite sync can reach or tamper with it.
PREVIEW_SNAPSHOT_DIR = PLATFORM_DATA_DIR / "preview-snapshots"
# In-flight chunked-upload staging (api/media/uploads.py): each large browser
# upload assembles here as `<upload_id>.partial` + `<upload_id>.json` meta,
# then atomic-renames into the agent tree at complete. Sibling of agents/ —
# SAME filesystem (so the final os.replace stays atomic) but OUTSIDE every
# agent tree, keeping staging invisible to sync manifests, agent shells, and
# the `*.partial` retention reaper that patrols AGENTS_DIR.
UPLOAD_STAGING_DIR = PLATFORM_DATA_DIR / "upload-staging"
# Files larger than this are NOT backed up to the recover-bin (the delete /
# overwrite still proceeds) — a safety net shouldn't copy huge build artifacts.
# Deliberately much smaller than OTODOCK_MAX_FILE_MB (Windows-Recycle-Bin-style:
# big files bypass the bin) so a couple of video-sized deletions can't evict the
# small-file undo history everyone actually needs. Raise per-install if desired.
RECOVER_BIN_MAX_BYTES = _cfg_int("RECOVER_BIN_MAX_MB", 100) * 1024 * 1024
# Per-agent aggregate ceiling on live recover-bin captures. The recover-bin sits
# OUTSIDE the agent quota tree (sibling of agents/), so without this an overwrite
# loop could grow it unbounded and fill the backing filesystem even while the
# agent itself is over quota. On overflow, capture() evicts that agent's oldest
# entries first (atop the 7-day TTL). 0 = unlimited.
RECOVER_BIN_AGENT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
# Delete tombstones (storage/files/file_tombstones_store.py) are kept this long so an
# offline satellite that missed a delete still applies it on reconnect. After
# this, a re-created file at the same path is simply re-adopted (the delete is
# assumed long-since propagated). Generous because satellites can be offline for
# weeks; the row is tiny (metadata only, no bytes).
FILE_TOMBSTONE_TTL_DAYS = 30
MCPS_DIR = PLATFORM_ROOT / "mcps"  # code — ships with platform install
# Persistent per-session runtime state (session index, security index, pending
# permissions, sse-mcp-configs, prompt-files). Under PLATFORM_DATA_DIR so it
# survives a container/image replace alongside agents (was BASE_DIR/sessions —
# inside the image, lost on recreate). Defaults to PLATFORM_ROOT/sessions in dev.
SESSIONS_DIR = PLATFORM_DATA_DIR / "sessions"

# Ensure sessions directory exists
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Server
PORT = _cfg_int("PROXY_PORT", 8400)
HOST = _cfg("PROXY_HOST", "0.0.0.0")
# The internal listener's port on 127.0.0.1, assigned by app.py at boot once
# its socket is bound (0 = none: tests, scripts, a server not started from
# app.py). Different on every boot: read it when a connection is made, never
# at import. Sandboxes and the satellite tunnel reach the proxy there, where
# forwarding headers are never read (auth/lan_check.py).
INTERNAL_LISTENER_PORT = 0

# Authentication
API_KEY = _cfg("PROXY_API_KEY")
if not API_KEY:
    API_KEY = secrets.token_urlsafe(32)
    _persist_secret("PROXY_API_KEY", API_KEY)
    _boot_note(f"Generated PROXY_API_KEY — saved to {_config_env}")


def is_master_key(token: str) -> bool:
    """Constant-time check that ``token`` is the master ``PROXY_API_KEY``.

    The single source of truth for the master-key comparison — guards against an
    empty key matching an empty token, and uses ``hmac.compare_digest`` so the
    check is timing-safe. Use this everywhere instead of ``token == API_KEY``."""
    return bool(API_KEY) and hmac.compare_digest(token, API_KEY)

# Telephony-scoped secret: authenticates the phone server's HTTP API callers
# (phone-mcp on /api/calls, the Asterisk dialplan on /v1/calls/register). Kept
# separate from the master key so the telephony surface never holds it.
PHONE_API_SECRET = _cfg("PHONE_API_SECRET")
if not PHONE_API_SECRET:
    PHONE_API_SECRET = secrets.token_urlsafe(32)
    _persist_secret("PHONE_API_SECRET", PHONE_API_SECRET)
    _boot_note(f"Generated PHONE_API_SECRET — saved to {_config_env}")

# ---------------------------------------------------------------------------
# Rate limiting + request-size + pagination limits (centralized, env-overridable)
# ---------------------------------------------------------------------------
# Trusted reverse-proxy hops. EMPTY (default) → the X-Forwarded-For header is NOT
# trusted: the real socket peer is used for rate-limiting + the LAN gate, so a
# client cannot spoof its source IP. Behind a reverse proxy / load balancer, set
# this to the proxy's address(es) (comma-separated IPs or CIDRs) so XFF is
# honoured ONLY from those hops.
TRUSTED_PROXIES = [s.strip() for s in _cfg("TRUSTED_PROXY", "").split(",") if s.strip()]

# Event-loop stall watchdog (core/loop_watchdog.py): a daemon thread logs the
# loop thread's stack when the loop stops ticking for longer than this many
# seconds, and counts stalls for the ``loop`` block of GET /v1/admin/health. 0 disables it. Added
# after the 2026-09-03 stall, whose blocking frame had to be reconstructed
# from health-probe gaps because nothing in the frozen window was logged.
LOOP_WATCHDOG_THRESHOLD_S = _cfg_float("LOOP_WATCHDOG_THRESHOLD_S", 2.0)
# Shorter stalls, from this long up, are logged once with the stalled stack
# and counted in the ``loop`` histogram. 0 disables them.
LOOP_WATCHDOG_REPORT_S = _cfg_float("LOOP_WATCHDOG_REPORT_S", 0.25)

# Per-sandbox cap on the RAM-backed ``/tmp`` tmpfs (bwrap ``--size``), in MB.
# ``/tmp`` is the agent's HOME (package caches, model downloads) and the CLI's
# runtime root; every local sandbox has its own, so an uncapped fleet can
# commit N × RAM/2. Bounded by half the host's RAM at use (never looser than
# the kernel default); 0 disables; ignored where bubblewrap lacks ``--size``
# (0.6.1). See core/sandbox/sandbox.py::tmpfs_cap_mb.
SANDBOX_TMP_SIZE_MB = _cfg_int("SANDBOX_TMP_SIZE_MB", 4096)

# Universal per-file size cap: ONE knob for the satellite sync engine (manifest
# + push/pull legs) AND every dashboard/chat upload (both hit POST /v1/upload).
# Satellites learn the sync cap via the auth_result policy handshake — the proxy
# config is the single source of truth; pre-0.5.103 satellites are pinned to the
# legacy 100MB by the per-machine gate in satellite_connection.effective_sync_cap.
MAX_FILE_MB = _cfg_int("OTODOCK_MAX_FILE_MB", 1024)
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
SYNC_MAX_FILE_BYTES = MAX_FILE_BYTES  # alias read by core/remote/file_sync.py
MAX_UPLOAD_SIZE_BYTES = MAX_FILE_BYTES  # alias read live by api/media/uploads.py

# Sync-ignore rule table: marker-confirmed generated/build-dir exclusion for
# satellite workspace sync (1.5). Shipped to 0.5.110+ satellites in the
# auth_result policy handshake and applied to the proxy's own per-machine
# manifest walk for those machines (core/remote/file_sync.py holds the engine
# + full semantics; from the 1.5 sync-ignore design round).
#
# EVOLUTION CONTRACT — read before editing:
#   * ADDITIVE data edits (new named rule, new marker, new veto entry) are
#     safe proxy-only changes: 0.5.110+ satellites apply whatever v1 table
#     the handshake carries, no satellite release needed.
#   * Any FORMAT/semantic change requires version=2 + a NEW satellite
#     capability gate (satellites reject unknown versions → legacy walk, so
#     the proxy would have to compute legacy manifests for them or the diff
#     misattributes whole trees as deletes).
#   * A named rule WITHOUT sibling evidence is refused by validation — bare
#     dir-name exclusion is exactly the false-positive class this design
#     forbids (a project root merely NAMED `target` must keep syncing).
#   * Veto entries must be files that THEMSELVES SYNC (never `.git` — the
#     platform copy lacks it and the two walks would disagree → churn).
SYNC_IGNORE_RULES: dict = {
    "version": 1,
    "in_dir_markers": [
        # Cache Directory Tagging spec (bford.info/cachedir) — cargo>=1.46
        # target/, uv caches+venvs, pytest/ruff/mypy caches write it. Only
        # honored with the spec signature (first 43 bytes).
        {"file": "CACHEDIR.TAG", "signed": True},
        {"file": "CMakeCache.txt"},       # any CMake build tree, any name
        {"file": "project.assets.json"},  # NuGet obj dir, any name
        {"dir": "meson-info"},            # Meson builddir (never in-tree)
    ],
    "named": [
        {"dir": "target", "siblings": ["Cargo.toml", "pom.xml", "build.sbt"]},
        {"dir": "build", "siblings": ["build.gradle", "build.gradle.kts",
                                      "settings.gradle", "settings.gradle.kts",
                                      "pubspec.yaml"]},
        {"dir": "obj", "sibling_globs": ["*.csproj", "*.fsproj", "*.vbproj"]},
        {"dir": "Pods", "siblings": ["Podfile"]},
        {"dir": "_build", "siblings": ["mix.exs"]},
        {"dir": "dist-newstyle", "siblings": ["cabal.project"],
         "sibling_globs": ["*.cabal"]},
        {"dir": "zig-out", "siblings": ["build.zig"]},
    ],
    "veto_files": ["CMakeLists.txt", "Cargo.toml", "package.json",
                   "pyproject.toml", "setup.py", "go.mod", "pom.xml",
                   "build.gradle", "build.gradle.kts", "mix.exs",
                   "composer.json", "Gemfile", "pubspec.yaml", "build.sbt",
                   "build.zig", "meson.build"],
    "veto_globs": ["*.csproj", "*.cabal"],
}

# Sync-delta telemetry (alert-only — sync NEVER pauses): an admin
# notification fires when one reconcile plans more than this many new
# pull-side files / bytes, or the per-turn inbound write counter crosses it
# within its rolling window. Signal for extending SYNC_IGNORE_RULES, never a
# decision point.
SYNC_DELTA_ALERT_FILES = _cfg_int("OTODOCK_SYNC_DELTA_ALERT_FILES", 2000)
SYNC_DELTA_ALERT_BYTES = _cfg_int("OTODOCK_SYNC_DELTA_ALERT_MB", 500) * 1024 * 1024

# Chunk size handed to browsers by POST /v1/upload/chunked/init. Files bigger
# than one chunk upload as N sequential ≤chunk-size requests, so CDN/gateway
# request-body caps (Cloudflare ~100MB Free/Pro, nginx client_max_body_size)
# never see the whole file. The default keeps a 1GB file at 32 requests while
# clearing common edge caps with a 3× margin. The server DICTATES this value —
# clients slice strictly by what init returns (a client-chosen size would
# invite silent offset corruption).
UPLOAD_CHUNK_MB = _cfg_int("OTODOCK_UPLOAD_CHUNK_MB", 32)
UPLOAD_CHUNK_BYTES = UPLOAD_CHUNK_MB * 1024 * 1024
# Chunked-upload staging bounds: open uploads per user (429) and in total
# (503), staged MB per user (429; never below one file's cap), and how long
# an upload may wait for its FIRST chunk before the sweep drops it (one
# with chunks keeps the 24 h resume window). 0 = no cap for the counts.
UPLOAD_MAX_OPEN = _cfg_int("OTODOCK_UPLOAD_MAX_OPEN", 4)
UPLOAD_MAX_OPEN_TOTAL = _cfg_int("OTODOCK_UPLOAD_MAX_OPEN_TOTAL", 256)
UPLOAD_STAGING_USER_MB = _cfg_int("OTODOCK_UPLOAD_STAGING_USER_MB", 2 * MAX_FILE_MB, minimum=MAX_FILE_MB)
UPLOAD_FIRST_CHUNK_S = _cfg_int("OTODOCK_UPLOAD_FIRST_CHUNK_S", 3600)
# Free space a new upload must leave on the data disk: the larger of the two
# (507 below it).
MIN_FREE_DISK_MB = _cfg_int("OTODOCK_MIN_FREE_DISK_MB", 5120)
MIN_FREE_DISK_PCT = _cfg_float("OTODOCK_MIN_FREE_DISK_PCT", 5)

# Global cap on CONCURRENT LARGE outbound (proxy→satellite) workspace pushes,
# across ALL machines and both push paths (live fan-out + initial-sync push
# branch). Per-machine pacing already exists (windowed acks + the two-lane
# writer); this bounds the AGGREGATE — default 3 ≈ max 24MB in-flight
# regardless of machine count. 0 = unlimited (gate disabled).
SYNC_FANOUT_CONCURRENCY = _cfg_int("OTODOCK_SYNC_FANOUT_CONCURRENCY", 3)
# Pushes SMALLER than this bypass the gate entirely (no slot, no queued row):
# a small file is a handful of 512KB frames already bounded by the bulk queue,
# and gating it would head-of-line-block live edits behind bulk transfers.
SYNC_FANOUT_MIN_MB = _cfg_int("OTODOCK_SYNC_FANOUT_MIN_MB", 4)
# A satellite's file_changed frames: appliers in flight per machine (frames
# past it are dropped with a rate-limited warning; 0 = no shedding),
# appliers running at once per machine and in total, and the size of the
# last-change stamp map.
SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE = _cfg_int("SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 64)
SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE = _cfg_int("SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE", 4, minimum=1)
SAT_FILE_CHANGED_CONCURRENCY_GLOBAL = _cfg_int("SAT_FILE_CHANGED_CONCURRENCY_GLOBAL", 8, minimum=1)
SAT_FILE_CHANGED_STAMP_MAX = _cfg_int("SAT_FILE_CHANGED_STAMP_MAX", 4096, minimum=1)
# Threads that write, fsync and rename uploaded and pulled files.
FILE_COMMIT_WORKERS = _cfg_int("FILE_COMMIT_WORKERS", 4, minimum=1)

# Global BACKSTOP cap on request body size (bytes), enforced by an ASGI
# middleware via Content-Length. Defaults to the universal file cap plus margin
# (multipart framing overhead) so it only rejects abusive oversized bodies
# (→ 413); finer per-endpoint caps still apply underneath.
MAX_REQUEST_BODY_BYTES = _cfg_int("MAX_REQUEST_BODY_BYTES", MAX_FILE_BYTES + 64 * 1024 * 1024)

# Tiered body caps under that backstop, counted on the streamed bytes: the
# login routes, the webhook receivers, a request with no valid credential (a
# dashboard session cookie, the service key or a session token as the bearer,
# or a WOPI access token on the WOPI and Collabora routes; one that is
# presented but does not validate gets 401), and every other route off the
# upload allowlist (which keeps the backstop). 0 turns a tier off (the
# backstop still applies).
MAX_AUTH_BODY_BYTES = _cfg_int("MAX_AUTH_BODY_BYTES", 64 * 1024)
MAX_WEBHOOK_BODY_BYTES = _cfg_int("MAX_WEBHOOK_BODY_BYTES", 2 * 1024 * 1024)
# The webhook caps a route lifts to once its sender is checked
# (api/events/webhook_body.py): a signed delivery of a large-event provider
# (GitHub, GitLab, Jira) and a trigger fire whose API key verified. Never
# below MAX_WEBHOOK_BODY_BYTES; 0 is the backstop.
MAX_WEBHOOK_SIGNED_BODY_BYTES = _cfg_int("MAX_WEBHOOK_SIGNED_BODY_BYTES", 25 * 1024 * 1024)
MAX_WEBHOOK_KEYED_BODY_BYTES = _cfg_int("MAX_WEBHOOK_KEYED_BODY_BYTES", 25 * 1024 * 1024)
# Webhook bodies being read at once, in bytes: in all (half of it for
# unchecked senders) and per client address. 0 turns a bound off.
MAX_WEBHOOK_INFLIGHT_BYTES = _cfg_int("MAX_WEBHOOK_INFLIGHT_BYTES", 128 * 1024 * 1024)
MAX_WEBHOOK_INFLIGHT_BYTES_PER_CLIENT = _cfg_int("MAX_WEBHOOK_INFLIGHT_BYTES_PER_CLIENT", 32 * 1024 * 1024)
MAX_UNAUTH_BODY_BYTES = _cfg_int("MAX_UNAUTH_BODY_BYTES", 64 * 1024)
MAX_JSON_BODY_BYTES = _cfg_int("MAX_JSON_BODY_BYTES", 8 * 1024 * 1024)

# Hard ceiling for any paginated list endpoint's page size.
MAX_PAGE_SIZE = _cfg_int("MAX_PAGE_SIZE", 500)

# A text file larger than this is not read inline by the files API (413):
# the client downloads it instead.
INLINE_TEXT_MAX_BYTES = _cfg_int("INLINE_TEXT_MAX_BYTES", 5 * 1024 * 1024)
# Workspace zip downloads: builds at once, paths in one request, files in
# one archive and the total input size (413 past the last three).
ZIP_MAX_CONCURRENT = _cfg_int("ZIP_MAX_CONCURRENT", 2, minimum=1)
ZIP_MAX_PATHS = _cfg_int("ZIP_MAX_PATHS", 500)
ZIP_MAX_ENTRIES = _cfg_int("ZIP_MAX_ENTRIES", 100000)
ZIP_MAX_INPUT_MB = _cfg_int("ZIP_MAX_INPUT_MB", 4096)
# Workspace copies through the files API: the bytes and the files one
# request may copy across all its sources (a source past either fails;
# 0 = no cap).
COPY_MAX_INPUT_MB = _cfg_int("COPY_MAX_INPUT_MB", 4096)
COPY_MAX_ENTRIES = _cfg_int("COPY_MAX_ENTRIES", 100000)
# Temporary public image URLs (/v1/images/temp): the mint copies the image,
# so each copy is capped, and so are the copies one session and all
# sessions hold at once.
TEMP_IMAGE_MAX_BYTES = _cfg_int("TEMP_IMAGE_MAX_MB", 20) * 1024 * 1024
TEMP_IMAGE_MAX_PER_SESSION = _cfg_int("TEMP_IMAGE_MAX_PER_SESSION", 20)
TEMP_IMAGE_MAX_TOTAL_BYTES = _cfg_int("TEMP_IMAGE_MAX_TOTAL_MB", 256) * 1024 * 1024

# Password hashes (bcrypt, about a core for 250 ms each) computed at once;
# the rest wait their turn off the event loop.
AUTH_HASH_CONCURRENCY = _cfg_int("AUTH_HASH_CONCURRENCY", 4, minimum=1)
# Password hashes waiting their turn; past this a login or a password change
# is answered 503 with Retry-After instead of queueing.
AUTH_HASH_MAX_WAITERS = _cfg_int("AUTH_HASH_MAX_WAITERS", 64)
# Webhook-fire key checks (bcrypt) at once, and how many may wait before
# further fires are refused (503).
WEBHOOK_KEY_VERIFY_CONCURRENCY = _cfg_int("WEBHOOK_KEY_VERIFY_CONCURRENCY", 2, minimum=1)
WEBHOOK_KEY_VERIFY_MAX_WAITERS = _cfg_int("WEBHOOK_KEY_VERIFY_MAX_WAITERS", 16)

# Max total DECOMPRESSED size of an uploaded community-MCP zip (zip-bomb guard).
MCP_ZIP_DECOMPRESSED_MAX = _cfg_int("MCP_ZIP_DECOMPRESSED_MAX", 500 * 1024 * 1024)


def _rate_limit_rule(prefix: str, mx: int, window: int, base_block: int, max_block: int) -> dict:
    """One keyed-rate-limit bucket config, each field env-overridable as
    ``RATE_LIMIT_<PREFIX>_{MAX,WINDOW,BLOCK,MAX_BLOCK}``."""
    return {
        "max": _cfg_int(f"RATE_LIMIT_{prefix}_MAX", mx),
        "window": _cfg_int(f"RATE_LIMIT_{prefix}_WINDOW", window),
        "base_block": _cfg_int(f"RATE_LIMIT_{prefix}_BLOCK", base_block),
        "max_block": _cfg_int(f"RATE_LIMIT_{prefix}_MAX_BLOCK", max_block),
    }


# bucket → {max, window, base_block, max_block} (seconds, except ``max`` = count
# within the window). Consumed by ``auth/rate_limiter.py``. ``login`` preserves
# the historical thresholds; the rest gate previously-unlimited surfaces.
# Keys one rate-limit bucket keeps at most (auth/rate_limiter.py).
RATE_LIMIT_MAX_KEYS = _cfg_int("RATE_LIMIT_MAX_KEYS", 50000, minimum=100)

RATE_LIMIT_RULES = {
    "login":   _rate_limit_rule("LOGIN", 10, 900, 900, 14400),
    # Wrong passwords from a trusted-device browser (the device cookie skips
    # the account tarpit), per device: past it that browser is an unknown one.
    "login_device": _rate_limit_rule("LOGIN_DEVICE", 5, 900, 900, 3600),
    "2fa":     _rate_limit_rule("2FA", 10, 900, 900, 3600),
    "forgot":  _rate_limit_rule("FORGOT", 5, 3600, 3600, 14400),
    "reset":   _rate_limit_rule("RESET", 10, 3600, 1800, 14400),
    "invite":  _rate_limit_rule("INVITE", 10, 3600, 1800, 14400),
    # Passkey login ceremony: each attempt = options + verify (2 hits).
    "passkey": _rate_limit_rule("PASSKEY", 30, 900, 900, 3600),
    # Per-trigger SUCCESS throttle — generous (10/s sustained) so a legitimately
    # busy integration isn't dropped; only a runaway/leaked-key flood trips it.
    "webhook": _rate_limit_rule("WEBHOOK", 600, 60, 30, 600),
    # Per-address webhook AUTH-FAILURE throttle — strict, to rate-limit key brute force.
    "webhook_auth": _rate_limit_rule("WEBHOOK_AUTH", 20, 300, 300, 3600),
    # Per-USER password-confirm throttle (change-password/change-email/
    # TOTP-disable/passkey management): these re-verify the account password
    # on an already-authed session, so without a cap a stolen session cookie
    # is an unbounded online password oracle.
    "confirm": _rate_limit_rule("CONFIRM", 10, 900, 900, 3600),
    # An agent opening one of its apps on a user's screen: a few per minute,
    # never a stream of interruptions.
    "app_open": _rate_limit_rule("APP_OPEN", 3, 60, 60, 300),
    # Shares created per user: a share is an exfiltration channel, so a
    # burst of them is never legitimate.
    "share_create": _rate_limit_rule("SHARE_CREATE", 10, 3600, 1800, 7200),
    # Reports of an agent's reply per user, repeats included (the route also
    # caps new reports at 20 an hour in the database).
    "reply_report": _rate_limit_rule("REPLY_REPORT", 60, 3600, 600, 3600),
    # SSO sign-ins started per address: each one holds a state the callback
    # needs, so a flood of anonymous starts must not push others out.
    "sso_start": _rate_limit_rule("SSO_START", 60, 300, 300, 3600),
    # Wrong passwords on an external link, per client address and per link
    # (failures only, like the login bucket).
    "share_unlock_ip": _rate_limit_rule("SHARE_UNLOCK_IP", 10, 300, 300, 3600),
    "share_unlock_share": _rate_limit_rule("SHARE_UNLOCK_SHARE", 10, 300, 300, 3600),
    # An app's inbound hooks (APPS.md "Inbound hooks"): the one
    # unauthenticated write path into the platform — counted per client IP
    # once the app row is found (a malformed or unknown id is a 404 that
    # counts nothing), skipped when the resolver says the address is shared
    # by every client, and per app (a vendor's honest burst passes; a flood
    # trips it).
    "app_inbound_ip": _rate_limit_rule("APP_INBOUND_IP", 120, 60, 60, 600),
    "app_inbound_app": _rate_limit_rule("APP_INBOUND_APP", 600, 60, 30, 600),
    # Failed inbound-hook signatures, per hook (and per client address for a
    # distinct client), checked right before the next verification.
    "app_inbound_fail": _rate_limit_rule("APP_INBOUND_FAIL", 20, 300, 300, 3600),
    # Webhook-fire key failures per key prefix, checked before bcrypt runs.
    "webhook_prefix": _rate_limit_rule("WEBHOOK_PREFIX", 10, 300, 300, 3600),
    # Vendor and relay webhook receivers, per client IP (unauthenticated).
    "webhook_receive_ip": _rate_limit_rule("WEBHOOK_RECEIVE_IP", 120, 60, 60, 600),
    # Chunked uploads started per user.
    "upload_init": _rate_limit_rule("UPLOAD_INIT", 120, 3600, 600, 3600),
    # Engine connects started per person: each ChatGPT start spawns a host
    # process; the Claude form restarts its flow after a refused paste.
    "oauth_start_openai": _rate_limit_rule("OAUTH_START_OPENAI", 10, 900, 900, 3600),
    "oauth_start_claude": _rate_limit_rule("OAUTH_START_CLAUDE", 20, 900, 900, 3600),
}

# Stable per-install identifier (short, NOT a secret). Namespaces the proxy's
# Docker-MCP compose projects + container/volume names so they can't collide
# with the operator's OWN compose projects (a bare `camoufox` project), or with
# a second OtoDock install sharing the same Docker daemon. Generated once and
# persisted so it's stable across restarts — like the secrets above.
INSTALL_ID = _cfg("OTODOCK_INSTALL_ID")
if not INSTALL_ID:
    INSTALL_ID = secrets.token_hex(4)  # 8 hex chars, e.g. "a1b2c3d4"
    _persist_secret("OTODOCK_INSTALL_ID", INSTALL_ID)
    _boot_note(f"Generated OTODOCK_INSTALL_ID={INSTALL_ID} — saved to {_config_env}")

# Claude Code settings. Same resolution ladder as CODEX_BIN below: explicit
# override → PATH → the user-prefix npm bin. The absolute fallback matters
# under systemd (PATH is system-dirs only) AND inside the bwrap sandbox
# (whose PATH never includes user homes — the layers mount the resolved
# binary's directory instead; see cli_install_ro_binds).
CLAUDE_BIN = _cfg(
    "CLAUDE_BIN",
    shutil.which("claude")
    or str(Path.home() / ".npm-global" / "bin" / "claude")
)

# Codex CLI settings
CODEX_BIN = _cfg(
    "CODEX_BIN",
    shutil.which("codex")
    or str(Path.home() / ".npm-global" / "bin" / "codex")
)
# Bun runs app servers (apps with a server/ folder) inside their own
# sandbox; the binary is bind-mounted in from this path. Same ladder:
# explicit override → PATH → the official user install (a dev box without
# sudo for /usr/local/bin). Empty when none exists: app servers then refuse
# to start with a log line, everything else runs.
BUN_BIN = _cfg(
    "BUN_BIN",
    shutil.which("bun")
    or (str(Path.home() / ".bun" / "bin" / "bun")
        if (Path.home() / ".bun" / "bin" / "bun").is_file() else "")
)
CLAUDE_TIMEOUT = _cfg_int("CLAUDE_TIMEOUT", 7200)  # 2 hours — headroom for long tasks; per-deployment override via CLAUDE_TIMEOUT, per-install override via the session_timeout platform setting
# A chat turn with no stream line and nothing keeping it alive (no open tool,
# no prompt waiting on a person, no background work, no recent hook) for this
# long is ended with the ``silent`` ending (core/events/turn_life.py). Env or
# config.env only, like CLAUDE_TIMEOUT, floored so a 0 cannot end every turn.
TURN_SILENCE_S = _cfg_int("TURN_SILENCE_S", 600, minimum=60)

# Audio/video playback transcoding (services/media/media_pipeline.py). ffmpeg/ffprobe
# are PROXY-SIDE ONLY — never mounted into the agent bwrap sandbox, never on
# satellites, never in a bash tier. In production point these at a location
# OUTSIDE the sandbox-mounted system dirs (/usr,/bin,/lib,/sbin), e.g.
# /opt/otodock/bin/ffmpeg, so a sandboxed process can't even see the binary.
# Empty → fall back to a PATH lookup (dev convenience).
FFMPEG_PATH = _cfg("OTO_FFMPEG_PATH", "")
FFPROBE_PATH = _cfg("OTO_FFPROBE_PATH", "")
# Transcode/remux cache. Empty → PLATFORM_DATA_DIR/media-cache (persistent so
# chat-history media still plays after a restart).
MEDIA_CACHE_DIR = _cfg("OTO_MEDIA_CACHE_DIR", "")

# Phone server (phone MCP uses this to initiate calls)
PHONE_SERVER_URL = _cfg("PHONE_SERVER_URL", "http://127.0.0.1:9093")
# Calls agents may place through the relay: per agent and per tied user
# each hour, per agent each day (0 = no cap), and optional E.164 prefixes a
# destination must start with (comma-separated; empty = any number).
PHONE_CALLS_PER_AGENT_PER_HOUR = _cfg_int("PHONE_CALLS_PER_AGENT_PER_HOUR", 10)
PHONE_CALLS_PER_USER_PER_HOUR = _cfg_int("PHONE_CALLS_PER_USER_PER_HOUR", 5)
PHONE_CALLS_PER_AGENT_PER_DAY = _cfg_int("PHONE_CALLS_PER_AGENT_PER_DAY", 50)
PHONE_CALL_ALLOWED_PREFIXES = [
    p.strip() for p in _cfg("PHONE_CALL_ALLOWED_PREFIXES", "").split(",") if p.strip()
]

# Tools the agent is allowed to use automatically
ALLOWED_TOOLS = _cfg("ALLOWED_TOOLS", "").strip()
# Empty = all tools allowed (via --dangerously-skip-permissions)

# Agent names are slugs minted by ``agent_store.sanitize_slug`` (``[a-z0-9-]``);
# built-in agents follow the same shape. This allowlist confines every
# ``AGENTS_DIR / <name>`` join so a request-supplied agent name can never
# traverse out of the agent tree (``/``, ``\``, ``..``, NUL are all excluded).
_SAFE_AGENT_NAME = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_safe_agent_name(name: str) -> bool:
    """True if ``name`` is a filesystem-safe agent slug (no path escape)."""
    return bool(name) and ".." not in name and _SAFE_AGENT_NAME.fullmatch(name) is not None


def get_agent_dir(agent_name: str) -> Path:
    """The root directory of an agent, ``AGENTS_DIR / agent_name``, behind a
    lexical barrier: a name that is empty, absolute, carries a path
    separator or would leave the agents tree raises ``PathOutsideRoot`` (a
    ``ValueError``). No other shape is asked of the name, so every slug a
    released install already has resolves as it always did."""
    from services.infra.path_confinement import PathOutsideRoot, join_under
    if not agent_name or "/" in agent_name or "\\" in agent_name:
        raise PathOutsideRoot(f"{agent_name!r} is not an agent name")
    return join_under(AGENTS_DIR, agent_name)


# Persistent sessions: idle timeout before reaping (seconds)
PERSISTENT_SESSION_TIMEOUT = _cfg_int("PERSISTENT_SESSION_TIMEOUT", 900)

# A session with tracked background work (a Bash command left running, a
# background subagent) is not idle: the reapers and the evictor spare it up
# to this ceiling, then close it anyway (a job that never ends must not pin a
# slot forever). Default = the per-turn ceiling.
BACKGROUND_WORK_CEILING_S = _cfg_int("BACKGROUND_WORK_CEILING_S", CLAUDE_TIMEOUT)


def get_session_timeout() -> int:
    """Return the CLI session timeout in seconds (from DB, fallback to env)."""
    with contextlib.suppress(Exception):
        from storage import database as _db
        val = _db.get_platform_setting("session_timeout")
        if val:
            return int(val)
    return CLAUDE_TIMEOUT


def get_idle_timeout() -> int:
    """Return the session idle-reap timeout in seconds (DB, fallback to env).

    ONE admin-editable knob (platform_setting ``session_idle_timeout``) honored by
    EVERY layer's idle reaper — headless CLI / Direct / Codex, remote, the
    orphaned-MCP-manager sweep, and the interactive PTY reaper — so "reap a
    session after N minutes of inactivity" means the same thing everywhere. The
    interactive reaper additionally spares a terminal that's on-screen or in
    satellite reconnect-grace (don't kill visible state) — that's not a longer
    timeout. Distinct from ``session_timeout`` (``get_session_timeout``), which
    is the per-TURN ceiling (CLAUDE_TIMEOUT, 2h)."""
    with contextlib.suppress(Exception):
        from storage import database as _db
        val = _db.get_platform_setting("session_idle_timeout")
        if val:
            return int(val)
    return PERSISTENT_SESSION_TIMEOUT


def get_jwt_expiry_hours() -> int:
    """Return JWT session expiry in hours (from DB, fallback to env)."""
    with contextlib.suppress(Exception):
        from storage import database as _db
        val = _db.get_platform_setting("jwt_expiry_hours")
        if val:
            return int(val)
    return JWT_EXPIRY_HOURS

# Max output tokens for Direct LLM API responses (all providers).
DIRECT_LLM_MAX_TOKENS = _cfg_int("DIRECT_LLM_MAX_TOKENS", 8192)

# The API the OpenAI provider speaks on the Direct LLM path
# (core/layers/providers/openai_adapter.py): "responses" (default) is the
# Responses API — reasoning effort together with function tools, streamed
# reasoning summaries, stateless (store: false); "chat" pins the provider to
# Chat Completions, where OpenAI rejects effort next to tools (the pre-1.5.1
# behaviour — an escape hatch, a restart applies it). A custom base URL on
# the openai provider and the OpenAI-compatible providers (Groq, Ollama,
# LM Studio, …) always use chat completions regardless.
DIRECT_LLM_OPENAI_API = _cfg("DIRECT_LLM_OPENAI_API", "responses").strip().lower()
if DIRECT_LLM_OPENAI_API not in ("responses", "chat"):
    DIRECT_LLM_OPENAI_API = "responses"
# Output budget of a Responses request that carries reasoning: there
# ``max_output_tokens`` INCLUDES the reasoning tokens, so DIRECT_LLM_MAX_TOKENS
# alone would let a high-effort answer exhaust its budget while thinking. The
# larger of the two applies; the hosted relay clamps at its own ceiling.
DIRECT_LLM_OPENAI_REASONING_MAX_TOKENS = _cfg_int("DIRECT_LLM_OPENAI_REASONING_MAX_TOKENS", 32768)

# Deferred tool loading on Direct LLM sessions
# (core/layers/direct/tool_catalog.py). "auto" (default) keeps the MCP tool
# schemas out of the request — behind a client-side `tool_search` tool and a
# compact catalog in the prompt — when the deferrable schemas exceed the
# threshold (≈ tokens, JSON bytes / 4); "on" always defers; "off" sends every
# schema on every request (the pre-1.5.1 behaviour). Manifests opt an MCP out
# of deferral with `always_load: true` (memory-mcp in the bundled set).
DIRECT_LLM_TOOL_SEARCH = _cfg("DIRECT_LLM_TOOL_SEARCH", "auto").strip().lower()
if DIRECT_LLM_TOOL_SEARCH not in ("auto", "on", "off"):
    DIRECT_LLM_TOOL_SEARCH = "auto"
DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS = _cfg_int("DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS", 6000)

# When true, the shared MCP installer will attempt to install missing system
# packages (libmagic, libreoffice, etc.) via the local package manager using
# sudo. Default: false — the installer only warns and produces a clear error
# message so the admin can install manually. Applies both on the proxy host
# and on every satellite that runs `mcp_installer.install_system_requirements`.
MCP_AUTO_INSTALL_SYSTEM_DEPS = _cfg_bool("MCP_AUTO_INSTALL_SYSTEM_DEPS", False)


# Max thinking tokens cap. Used alongside --effort to ensure thinking content
# is streamed (visible in dashboard). The adaptive effort still controls WHEN
# and HOW MUCH to think; this just sets the upper bound and enables streaming.
MAX_THINKING_TOKENS = _cfg_int("MAX_THINKING_TOKENS", 100000)


# Push notifications (VAPID for Web Push, FCM for Android)
VAPID_PUBLIC_KEY = _cfg("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = _cfg("VAPID_PRIVATE_KEY", "")
if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
    # Any failure here → push notifications will be disabled.
    with contextlib.suppress(Exception):
        import base64

        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        _vk = ec.generate_private_key(ec.SECP256R1())
        _priv_raw = _vk.private_numbers().private_value.to_bytes(32, "big")
        _pub_raw = _vk.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        VAPID_PRIVATE_KEY = base64.urlsafe_b64encode(_priv_raw).rstrip(b"=").decode()
        VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(_pub_raw).rstrip(b"=").decode()
        _persist_secret("VAPID_PUBLIC_KEY", VAPID_PUBLIC_KEY)
        _persist_secret("VAPID_PRIVATE_KEY", VAPID_PRIVATE_KEY)
        _boot_note(f"Generated VAPID key pair — saved to {_config_env}")
VAPID_EMAIL = _cfg("VAPID_EMAIL", "admin@localhost")
FCM_SERVICE_ACCOUNT_PATH = _cfg("FCM_SERVICE_ACCOUNT_PATH", "")


# Direct session idle timeout (seconds) before reaping
DIRECT_SESSION_TIMEOUT = _cfg_int("DIRECT_SESSION_TIMEOUT", 900)

# Hooks directory (PreToolUse permission gate, etc.)
HOOKS_DIR = BASE_DIR / "hooks"
HOOKS_DIR.mkdir(exist_ok=True)

# Database
DATABASE_URL = _cfg(
    "DATABASE_URL",
    "postgresql://otodock:otodock@localhost:5432/otodock"
)

# Connection pools and the threads around the event loop (storage/pg.py,
# startup.py, app.py). Per process at these defaults: the shared pool (30),
# the run_db lane pool (fast + bulk + 2) and the loop pool (3): 52, plus 3
# for a moment during a loop-pool recovery, against Postgres' 100.
DB_POOL_MAX_SIZE = _cfg_int("DB_POOL_MAX_SIZE", 30)
DB_POOL_TIMEOUT_S = _cfg_float("DB_POOL_TIMEOUT_S", 10)
DB_FAST_LANE_WORKERS = _cfg_int("DB_FAST_LANE_WORKERS", 6, minimum=1)
DB_BULK_LANE_WORKERS = _cfg_int("DB_BULK_LANE_WORKERS", 10, minimum=1)
# The event loop's own pool: how long it waits for a connection before the
# breaker opens, how long the breaker stays open, and after how long one
# operation triggers a liveness probe of Postgres (0 disables the probe).
DB_LOOP_POOL_TIMEOUT_S = _cfg_float("DB_LOOP_POOL_TIMEOUT_S", 0.5)
DB_LOOP_BREAKER_S = _cfg_float("DB_LOOP_BREAKER_S", 5)
DB_LOOP_LIVENESS_PROBE_S = _cfg_float("DB_LOOP_LIVENESS_PROBE_S", 1.0)
# Server-side statement_timeout and idle_in_transaction_session_timeout on
# every pool connection; 0 disables each (PgBouncer in transaction mode).
DB_STATEMENT_TIMEOUT_S = _cfg_float("DB_STATEMENT_TIMEOUT_S", 300)
DB_IDLE_IN_TX_TIMEOUT_S = _cfg_float("DB_IDLE_IN_TX_TIMEOUT_S", 60)
# The asyncio default executor (asyncio.to_thread). More threads worsen GIL
# contention with the loop (measured); 32 covers the to_thread sites.
DEFAULT_EXECUTOR_WORKERS = _cfg_int("DEFAULT_EXECUTOR_WORKERS", 32, minimum=1)
# uvicorn limit_concurrency: past this many open connections (WebSockets
# included) a new HTTP request gets 503. 0 disables.
HTTP_LIMIT_CONCURRENCY = _cfg_int("HTTP_LIMIT_CONCURRENCY", 4000)
# A WebSocket's write buffer (app.py, _BoundedWebSocketProtocol): past this
# many buffered bytes a sender waits for the reader instead of growing the
# buffer, and a reader that drains none of it for WS_WRITE_STALL_S is
# dropped. The stall sits past the keepalive's ping plus pong timeout so a
# satellite that merely stalls its loop for a moment is never cut by it.
WS_WRITE_BUFFER_MAX_BYTES = _cfg_int("WS_WRITE_BUFFER_MAX_BYTES", 4 * 1024 * 1024, minimum=64 * 1024)
WS_WRITE_STALL_S = _cfg_float("WS_WRITE_STALL_S", 60, minimum=1.0)
# Satellite transfers (REMOTE-AGENTS.md "Chunked transfer"): the file bytes
# a connection may have in flight to a satellite (its pong queues behind
# every byte already handed to the transport), and how long a push or a
# pull may go without any transfer frame on its connection before it fails.
BULK_CREDIT_BYTES = _cfg_int("BULK_CREDIT_BYTES", 1024 * 1024, minimum=64 * 1024)
PUSH_STALL_S = _cfg_float("PUSH_STALL_S", 90, minimum=10.0)
PULL_STALL_S = _cfg_float("PULL_STALL_S", 90, minimum=10.0)

# ---------------------------------------------------------------------------
# Concurrency admission (core/concurrency.py + core/sandbox/host_resources.py)
# ---------------------------------------------------------------------------
# Local-session admission is a TWO-GATE test, not a static count ceiling:
#   GATE 1 (reservation budget): Σ per-session estimate ≤ total_RAM × BUDGET_FRACTION
#   GATE 2 (live-RAM veto):      live_available_RAM − estimate ≥ FLOOR
# A new LOCAL session is admitted iff BOTH pass. Gate 1 is the instant, deterministic
# burst bound; Gate 2 (a cheap cgroup/proc read) is the precise OOM backstop that sees
# REAL usage, so small boxes pack to real capacity and the box can't OOM under
# non-session pressure. Remote sessions never count (their satellite enforces).
#
# The per-session estimate is coarse by session TYPE (calibrated via
# scripts/loadtest_sessions.py): a CLI/Codex session's process tree (the CLI + its
# STDIO MCP children — Docker MCPs are sibling containers, remote MCPs are HTTP, so
# both add ~0 here) measured ~0.9–1.1 GB grown (~330 MB fresh + ~115 MB/stdio MCP);
# a Direct-LLM session (no CLI process, just the in-proxy adapter + usually zero
# or one sandboxed stdio MCP) is far lighter. The estimates sit at the LOW end of
# the grown range on purpose — they are admission bookkeeping, not allocations:
# Gate 2's live MemAvailable read (grow-in-debited) is the real OOM safety and
# reclaims/penalizes the difference as sessions actually grow, so a smaller
# estimate buys small hosts real concurrency without losing the burst bound.
SESSION_EST_HEAVY_MB = _cfg_int("SESSION_EST_HEAVY_MB", 850)    # CLI/Codex/interactive/phone-CLI/task reserve
SESSION_EST_LIGHT_MB = _cfg_int("SESSION_EST_LIGHT_MB", 250)    # floor of the Direct-LLM estimate (no CLI process)
# Gate-1 budget = total RAM × this. Generous on purpose — Gate 1 is a coarse cap +
# burst bound, Gate 2 is the real safety, so this never needs precise tuning.
BUDGET_FRACTION = _cfg_float("BUDGET_FRACTION", 0.90)
# Gate-2 headroom kept free for the OS/kernel/page-cache. Effective floor is
# max(this, total_mb × 0.03) so large hosts keep proportional headroom.
SESSION_RESERVE_FLOOR_MB = _cfg_int("SESSION_RESERVE_FLOOR_MB", 256)
# Gate-2 SWAP CREDIT: MemAvailable counts zero swap, so a small box with swap
# configured denies sessions it could comfortably run by paging out cold heap
# (idle CLI sessions spend most of their life blocked on the API — their
# coldest pages swap gracefully). The live reading gains min(SwapFree / 2,
# this cap): half of free swap so the credit shrinks as swap fills, capped so
# admission never leans DEEP into swap. 0 disables. Boxes without swap are
# unaffected (SwapFree = 0).
SESSION_SWAP_CREDIT_MB = _cfg_int("SESSION_SWAP_CREDIT_MB", 512)
# Under Gate-1 pressure, an idle LOCAL session this old (seconds) may be gracefully
# evicted to admit an active one (clamped below the idle-reap timeout). Streaming
# sessions have ~0 idle age and are never eligible.
SESSION_EVICT_FLOOR_S = _cfg_int("SESSION_EVICT_FLOOR_S", 300)
# Optional HARD cap on concurrent LOCAL sessions — an env-only escape hatch (no admin
# UI). 0 = no cap (pure budget, the default). For operators on noisy shared boxes who
# want a deterministic ceiling regardless of the live-RAM reading.
OTODOCK_MAX_LOCAL_SESSIONS = _cfg_int("OTODOCK_MAX_LOCAL_SESSIONS", 0)
# Sessions one person may hold at once (0 = no cap), and the session spawns
# allowed at once (0 = half the CPUs this process may use, at least 2).
MAX_SESSIONS_PER_USER = _cfg_int("OTODOCK_MAX_SESSIONS_PER_USER", 8)
SESSION_SPAWN_CONCURRENCY = _cfg_int("SESSION_SPAWN_CONCURRENCY", 0) or max(
    2, len(os.sched_getaffinity(0)) // 2 if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 4) // 2)
# Admission: how long a session start waits in the queue for a slot, and how
# long a parked delegate result or continuation wake waits for one before it
# is stored for the chat's next turn.
ADMISSION_QUEUE_WAIT_S = _cfg_float("ADMISSION_QUEUE_WAIT_S", 30)
ADMISSION_WAKE_WAIT_S = _cfg_float("ADMISSION_WAKE_WAIT_S", 600)
# The session index is written behind: dirty entries flush at most this often.
SESSION_INDEX_FLUSH_S = _cfg_float("SESSION_INDEX_FLUSH_S", 2.0)

SCHEDULER_TIMEZONE = _cfg("SCHEDULER_TIMEZONE", "UTC")
SCHEDULER_MODE = _cfg("SCHEDULER_MODE", "embedded")  # embedded | standalone
PROXY_INTERNAL_URL = _cfg("PROXY_INTERNAL_URL", f"http://localhost:{PORT}")
SCHEDULER_SYNC_INTERVAL = _cfg_int("SCHEDULER_SYNC_INTERVAL", 30)
# Minimum seconds between scheduled-task SESSION SPAWNS. Tasks that share a
# fire time (several 06:30 digests) otherwise start their CLI + sandbox +
# MCP handshakes in the same second — a burst that spikes RAM/CPU on small
# hosts. The fire times themselves stay exact; only the session starts are
# spaced. 0 disables. Manual Run-Now and event triggers are never spaced.
TASK_SPAWN_SPACING_SECONDS = _cfg_int("TASK_SPAWN_SPACING_SECONDS", 10)
# Active clocked tasks (recurring, and one-time not yet fired) one person may
# create and one agent may hold (429 past them; admins are exempt), and the
# pending self-continuations per chat and per agent. 0 = no cap.
TASK_MAX_ACTIVE_PER_USER = _cfg_int("TASK_MAX_ACTIVE_PER_USER", 25)
TASK_MAX_ACTIVE_PER_AGENT = _cfg_int("TASK_MAX_ACTIVE_PER_AGENT", 100)
CONTINUATION_MAX_ACTIVE_PER_CHAT = _cfg_int("CONTINUATION_MAX_ACTIVE_PER_CHAT", 10)
CONTINUATION_MAX_ACTIVE_PER_AGENT = _cfg_int("CONTINUATION_MAX_ACTIVE_PER_AGENT", 100)


def get_platform_timezone() -> str:
    """Return the platform timezone string (e.g. 'Europe/Athens').

    Reads from platform_settings DB first, falls back to SCHEDULER_TIMEZONE env var.
    Result is cached for 60 seconds to avoid DB hits on every message.
    """
    import time
    now = time.monotonic()
    if (_tz_cache["tz"] is not None and now - _tz_cache["at"] < 60):
        return _tz_cache["tz"]
    try:
        from storage import database as _db
        val = _db.get_platform_setting("platform_timezone")
        tz = val or SCHEDULER_TIMEZONE
    except psycopg.Error:
        tz = SCHEDULER_TIMEZONE
    _tz_cache["tz"] = tz
    _tz_cache["at"] = now
    return tz


_tz_cache: dict = {"tz": None, "at": 0.0}


def get_platform_tz() -> zoneinfo.ZoneInfo:
    """Return a ZoneInfo object for the platform timezone."""
    return zoneinfo.ZoneInfo(get_platform_timezone())


def get_platform_public_url() -> str:
    """Return the platform's public WSS endpoint host for satellite install.

    The host part of the public dashboard URL — satellites dial it outbound,
    and the install command embeds it as the WSS endpoint they connect to.
    Sourced solely from ``DASHBOARD_PUBLIC_URL`` (config.env); the old
    DB-backed admin field was removed (deployments already set the public URL
    in their compose env). Returns empty string when unset — the admin must
    set ``DASHBOARD_PUBLIC_URL`` before users can pair satellites.
    """
    val = DASHBOARD_PUBLIC_URL or ""
    # Strip scheme — keep just the host[:port] so callers can prefix wss://
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if val.startswith(prefix):
            val = val[len(prefix):]
            break
    return val.rstrip("/")


def now_local() -> datetime:
    """Return current datetime in the platform's configured timezone."""
    return datetime.now(get_platform_tz())


def format_current_time(user_tz: str | None = None) -> str:
    """Return formatted current time string for agent datetime injection.

    Renders wall-clock now in ``user_tz`` when supplied, else the platform TZ.
    Includes the IANA timezone name AND the UTC offset so the agent has
    unambiguous information about what timezone the wall-clock time is in.
    Without this, agents would see e.g. "07:09" and incorrectly assume it's
    UTC, then send back ISO datetimes with "+00:00" — causing scheduled
    tasks/notifications to fire at the wrong absolute moment (off by the
    UTC offset of the active timezone).

    The 24-hour time is followed by a 12-hour AM/PM gloss in parentheses so
    LLMs cannot drift into reading e.g. "04:02" as 4 PM in dinner-shaped
    contexts. Bare 24-hour numbers in the morning window (00–11) are one
    digit away from a "PM" interpretation; the gloss removes that ambiguity.

    Invalid IANA names fall back silently to the platform TZ — defence in
    depth against bad client_info payloads.
    """
    tz_name = user_tz or get_platform_timezone()
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
        tz_name = get_platform_timezone()
        tz = get_platform_tz()
    now = datetime.now(tz)
    # %z gives "+0300"; reformat to "+03:00" for readability.
    raw_off = now.strftime("%z")
    if raw_off:
        offset = f"{raw_off[:3]}:{raw_off[3:]}"
    else:
        offset = "+00:00"
    h24 = now.strftime("%H:%M")
    h12 = (now.hour % 12) or 12
    ampm = "PM" if now.hour >= 12 else "AM"
    h12_str = f"{h12}:{now.strftime('%M')} {ampm}"
    return f"{now.strftime('%A, %B %d, %Y')} {h24} ({h12_str}) {tz_name} (UTC{offset})"


# DEPRECATED: get_task_mcp_config and get_user_mcp_config removed.
# Use mcp_registry.build_session_mcp_config() instead.


# Dashboard (React SPA served as static files)
DASHBOARD_DIST = BASE_DIR.parent / "dashboard" / "dist"
DASHBOARD_ENABLED = _cfg_bool("DASHBOARD_ENABLED", True)

# Wake-word spotter assets (wasm engine + model .data), committed in-repo.
# Deliberately OUTSIDE dashboard/dist: `cap sync` copies all of dist into the
# APK, and these ~18 MB are runtime-fetched, never bundled.
KWS_ASSETS_DIR = BASE_DIR / "assets" / "kws"

# JWT / Session
_jwt = _cfg("JWT_SECRET")
if not _jwt:
    _jwt = secrets.token_urlsafe(64)
    _persist_secret("JWT_SECRET", _jwt)
    _boot_note(f"Generated JWT_SECRET — saved to {_config_env}")
JWT_SECRET = _jwt
JWT_EXPIRY_HOURS = _cfg_int("JWT_EXPIRY_HOURS", 168)
# The shortest JWT_SECRET or credential key a boot accepts as newly set
# (storage.identity.credential_store.judge_secret_floor).
SECRET_MIN_LENGTH = 32


def credential_key_setting() -> str:
    """The ``CREDENTIAL_ENCRYPTION_KEY`` line: the process environment, then
    config.env (a Docker install bind-mounts the file, so its lines never
    reach the environment). Comma-separated: the first key encrypts, the
    others only decrypt."""
    return _cfg("CREDENTIAL_ENCRYPTION_KEY")
DASHBOARD_PUBLIC_URL = _cfg("DASHBOARD_PUBLIC_URL", "")


def _default_cookie_secure() -> bool:
    """Whether the session cookie should carry the ``Secure`` flag.

    A ``Secure`` cookie is ONLY sent by the browser over HTTPS — so on a plain
    HTTP deploy (localhost / LAN, or before TLS is set up) hardcoding it breaks
    login: setup/login returns 200 + Set-Cookie, but the browser silently drops
    the Secure cookie over http://, so every subsequent authed call 401s and the
    dashboard fails to load. Derive it from the PUBLIC URL scheme: this is correct
    for a direct HTTP/HTTPS deploy AND for a TLS-terminating reverse proxy (where
    the proxy itself sees HTTP but the browser talks HTTPS to the public https://
    URL, so the Secure cookie is delivered + returned). Override with COOKIE_SECURE.
    """
    return (DASHBOARD_PUBLIC_URL or "").strip().lower().startswith("https://")


_cookie_secure_env = _cfg_bool("COOKIE_SECURE", None)
COOKIE_SECURE = _default_cookie_secure() if _cookie_secure_env is None else _cookie_secure_env

# --- OtoDock relay + deployment ---
# Deployment axis: false = self-hosted (the fair-source product), true =
# OtoDock-operated cloud SaaS. Drives the deployment-aware licensing
# enforcement model and hosted-OAuth security.
OTODOCK_CLOUD = _cfg_bool("OTODOCK_CLOUD", False)

# Local-PBX (Asterisk / FreePBX) telephony adapters are AudioSocket-based — they
# only work where the PBX can reach the phone daemon on the LAN. Enabled by default
# on self-host (the 99% case); OFF on OtoDock cloud (a tenant's LAN PBX can't reach
# a multi-tenant pool — cloud telephony goes through Twilio/3CX instead). An explicit
# OTODOCK_LOCAL_PBX_ENABLED wins either way (a Twilio-only self-host sets it false;
# an advanced cloud op sets it true).
_local_pbx_raw = _cfg_bool("OTODOCK_LOCAL_PBX_ENABLED", None)
LOCAL_PBX_ENABLED = (not OTODOCK_CLOUD) if _local_pbx_raw is None else _local_pbx_raw

def untrusted_edge(in_docker: bool, public_url: str, trusted: list[str]) -> bool:
    """In a container the edge never connects from 127.0.0.1, so an https
    public URL with no TRUSTED_PROXY means every visitor shares the edge's
    address (the admin's forwarding warnings carry it too)."""
    return in_docker and public_url.strip().lower().startswith("https://") and not trusted


def _warn_untrusted_edge(in_docker: bool, public_url: str, trusted: list[str]) -> bool:
    """``untrusted_edge``, said at boot and never refused: fronted installs
    must still start."""
    if not untrusted_edge(in_docker, public_url, trusted):
        return False
    _boot_note(
        "ERROR: DASHBOARD_PUBLIC_URL is https but TRUSTED_PROXY is empty: behind a "
        "reverse proxy every visitor shares its address, so one person's failed "
        "logins lock everyone out. In .env set TRUSTED_PROXY to the address the "
        "reverse proxy connects from: for one on this host, the otodock network's "
        "gateway (docker network inspect otodock -f "
        "'{{(index .IPAM.Config 0).Gateway}}', 10.200.0.1 by default) with the "
        "proxy port published on 127.0.0.1 only; for one on another machine, its IP."
    )
    return True


_warn_untrusted_edge(RUNNING_IN_DOCKER, DASHBOARD_PUBLIC_URL, TRUSTED_PROXIES)

# --- T2 (Docker-Compose) service-DNS + daemon wiring ---
# All of these are consumed ONLY when RUNNING_IN_DOCKER is true (see
# ``core/config/deployment.py``); on bare-metal (T1) they are inert, so the defaults
# never affect the live native install. They describe the shared Docker-Compose
# topology: a containerised proxy that drives the Docker daemon through a
# read-restricted socket-proxy and reaches its Docker MCPs by service-DNS on a
# shared network.
#
#   DOCKER_SOCKET_PROXY_HOST/PORT — the Tecnativa docker-socket-proxy the proxy
#     points ``DOCKER_HOST`` at instead of a local ``/var/run/docker.sock``.
#   PROXY_SERVICE_NAME            — the proxy's own service-DNS name; Docker MCPs
#     (and Collabora) reach the proxy here on their callbacks instead of
#     ``host.docker.internal``.
#   OTODOCK_NETWORK               — the shared user-defined network every MCP
#     container joins (used by the T2 compose rewrite in a later sub-step).
#   OTODOCK_AGENTS_VOLUME         — the platform's shared agents named volume
#     (`otodock-agents` in the shipped compose; `name:` is pinned there). The
#     T2 compose rewrite maps an MCP's `${HOST_AGENTS_DIR}` agents-tree bind
#     onto this volume so sidecars see the real agent files — the host path a
#     containerised proxy would hand them is an empty auto-created dir.
DOCKER_SOCKET_PROXY_HOST = _cfg("DOCKER_SOCKET_PROXY_HOST", "docker-socket-proxy")
DOCKER_SOCKET_PROXY_PORT = _cfg_int("DOCKER_SOCKET_PROXY_PORT", 2375)
PROXY_SERVICE_NAME = _cfg("PROXY_SERVICE_NAME", "otodock-proxy")
OTODOCK_NETWORK = _cfg("OTODOCK_NETWORK", "otodock")
OTODOCK_AGENTS_VOLUME = _cfg("OTODOCK_AGENTS_VOLUME", "otodock-agents")

# --- Docker-MCP address pool (T1 self-sufficiency) ---
# OtoDock pins the subnet of every per-MCP bridge it creates on bare-metal (T1)
# to a unique /24 carved from this pool, via a generated docker-compose.override.yml
# (see services/mcp/compose_rewrite.ensure_t1_override). This is what stops a busy host
# — where Docker's 172.16/12 default pools are exhausted — from auto-allocating a
# 192.168.x bridge that overlaps the operator's LAN and black-holes their route.
# No private range is universally safe, so it's an operator knob: change it if
# 10.201.0.0/16 overlaps your LAN/VPN. (T2 already routes MCPs onto OTODOCK_NETWORK;
# T3/cloud has no local bridges.)
OTODOCK_MCP_ADDRESS_POOL = _cfg("OTODOCK_MCP_ADDRESS_POOL", "10.201.0.0/16")
# Force a local `docker build` of Docker MCPs on T1 instead of pulling the catalog's
# pre-built `server.image` (for Dockerfile iteration in dev). Default = prefer pull.
OTODOCK_MCP_BUILD_LOCAL = _cfg_bool("OTODOCK_MCP_BUILD_LOCAL", False)
# Default memory bound injected into every docker-MCP container whose compose
# declares no limit of its own (an MCP's explicit mem_limit / deploy limit
# always wins). Guards the platform against a leaky community sidecar eating
# the host's RAM and vetoing all session admission (Gate 2 reads live
# MemAvailable). Injected with memswap_limit = mem_limit — no swap growth, a
# runaway container OOM-restarts instead of thrashing the host. '0'/'none'
# disables injection.
OTODOCK_MCP_DEFAULT_MEM_LIMIT = _cfg("OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g")

# --- Operator-forced platform settings (managed / OtoDock-cloud installs) ---
# A JSON object in config.env that pins selected ``platform_settings`` keys the
# customer-admin must NOT change (e.g. {"session_retention_enabled":"1",
# "session_retention_days":"180","smtp_host":"...","password_min_score":"3"}).
# Read ONCE at startup and overlaid on every platform-setting read in
# ``storage.database`` (so EVERY consumer honors it uniformly), surfaced as
# ``forced_keys`` in the admin settings API (UI locks/hides them), and ignored
# on write. Fail-safe: unset / blank / invalid JSON / non-object → {} (no-op).
# NOTE: never put ``license_key`` here — that would freeze the relay re-issue
# adoption; use OTODOCK_LICENSE_KEY (a one-time seed) instead.
def _parse_forced_settings(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        # platform_settings values are TEXT; mirror the bool-as-"1"/"" convention.
        out[str(k)] = ("1" if v else "") if isinstance(v, bool) else str(v)
    return out


_FORCED_SETTINGS = _parse_forced_settings(_cfg("OTODOCK_FORCED_SETTINGS", ""))


def forced_settings() -> dict[str, str]:
    """Operator-forced platform settings, immutable by the admin. Empty unless
    ``OTODOCK_FORCED_SETTINGS`` is configured (managed / cloud installs)."""
    return _FORCED_SETTINGS


# Bootstrap license key for managed / cloud installs. Seeds
# ``platform_settings.license_key`` ONCE at startup when the DB has none; the
# ``license_check_worker`` then owns updates via the relay re-issue (adopt-on-
# check). NOT an override — a stale value never clobbers a worker-adopted key.
OTODOCK_LICENSE_KEY = _cfg("OTODOCK_LICENSE_KEY", "").strip()

# --- Cloudflare Turnstile (login bot-protection) ---
# When BOTH are set (OtoDock-managed / cloud installs) they take precedence over the
# DB platform-settings, and the admin sees only a read-only "managed" badge — these
# keys are OtoDock infra and are never exposed in any API response. Self-hosters leave
# these blank and set their own keys in Setup → Security (the secret is stored
# Fernet-encrypted in the DB). Resolution precedence: env (both keys) > DB > disabled.
# NOTE: do NOT put turnstile_* keys in OTODOCK_FORCED_SETTINGS — that mechanism *shows*
# the locked value to the admin; these dedicated vars exist precisely to hide the secret.
TURNSTILE_SITE_KEY = _cfg("OTODOCK_TURNSTILE_SITE_KEY", "").strip()
TURNSTILE_SECRET_KEY = _cfg("OTODOCK_TURNSTILE_SECRET_KEY", "").strip()

# Air-gapped mode: this install makes NO outbound calls to OtoDock at all — not
# to the hosted relay, not to the license server. Default false = "connected"
# (hosted relay offered + subscription license phone-home). Set true for isolated
# enterprise installs (they bring their own OAuth apps and use an `offline_term`
# license that validates purely by signature, never phoning home). This is a
# NO-OUTBOUND switch, NOT a license-mode switch: the KEY's signed `license_mode`
# decides licensing; this flag only governs outbound. `OTODOCK_CLOUD=true` forces
# it false (the control plane manages connectivity). The relay holds every OtoDock
# secret — no install does. See `services/billing/relay_client.relay_offered()`.
OTODOCK_AIR_GAPPED = _cfg_bool("OTODOCK_AIR_GAPPED", False)
# Relay base URL. Defaults to the live OtoDock relay so a connected install
# works out of the box (native push, hosted OAuth, license activation/liveness)
# with zero config. Outbound is still fully gated: OTODOCK_AIR_GAPPED=true makes
# this install reach OtoDock NOT AT ALL, and a signed `offline_term` license
# disables the relay regardless of any flag (see relay_offered()). Override for
# staging / a private relay. Kept server-side — never exposed to clients (only
# the derived booleans are).
OTODOCK_RELAY_BASE = _cfg("OTODOCK_RELAY_BASE", "https://api.otodock.io")

# The daily release check (services/infra/release_check.py): admins are told
# when a newer OtoDock release exists. Off under OTODOCK_AIR_GAPPED and on the
# cloud whatever this says; the URL is GitHub's releases/latest answer, and an
# operator-set one may point at a private mirror.
RELEASE_CHECK_ENABLED = _cfg_bool("OTODOCK_RELEASE_CHECK", True)
RELEASE_CHECK_DEFAULT_URL = "https://api.github.com/repos/OtoDock/oto-dock/releases/latest"
RELEASE_CHECK_URL = _cfg("OTODOCK_RELEASE_CHECK_URL", RELEASE_CHECK_DEFAULT_URL).strip()
# A database last migrated by a NEWER release refuses the boot
# (storage/schema.py::platform_version_preflight); this lets it through.
ALLOW_NEWER_DATABASE = _cfg_bool("OTODOCK_ALLOW_NEWER_DATABASE", False)

# --- Sandbox network isolation (local agents) ---
# ALWAYS ON (no toggle): every local sandboxed process (CLI / Codex sessions,
# Direct-LLM stdio MCPs) runs inside a pasta-managed network namespace — NAT'd
# outbound internet, but RFC1918 + the host's own subnet + cloud-metadata
# blackholed, with egress carved ONLY to the proxy hook port + the session's
# configured MCP / docker-MCP / local-LLM targets (registry-derived at spawn;
# see services/mcp/mcp_registry.resolve_sandbox_egress). There is no un-isolated
# mode — genuinely un-isolated execution is a remote machine. Requires `pasta`
# (passt) + `ip` (iproute2) + unprivileged user namespaces on the host —
# enforced by a hard-fail startup preflight (core/sandbox/sandbox.py::netns_preflight).
# Satellites are unaffected (no bwrap there — remote execution trusts the
# paired machine's own boundary).

# --- Storage quotas ---
# Per-agent disk limits over the LOCAL agent tree (AGENTS_DIR). Two buckets per
# agent (XFS project IDs): shared (workspace+knowledge+config) + per-user
# (users/{u}/). Remote satellites are NOT enforced — they inherit the boundary
# because a sync-back into a full local folder fails. Two tiers:
#   - Soft tier (always on once a limit is set): measures usage + fires the
#     90/95/100% WARNING notifications + powers the admin UI. Any filesystem, no
#     privilege. This is the baseline everywhere.
#   - Hard tier (XFS project quotas): over-limit writes fail with EDQUOT.
#     AUTO-enabled when the agents dir is on an XFS mount with project quota
#     active (mount option `prjquota`); otherwise the soft tier runs and a single
#     line is logged (never bricks boot). Put the data dir on a project-quota XFS
#     volume to get it — we never create an image or reconfigure a host fs. See
#     services/infra/storage_quota.py::quotas_preflight.
# Set OTODOCK_STORAGE_QUOTAS=off to force the soft tier even on a capable fs.
STORAGE_QUOTAS_FORCE_SOFT = _cfg("OTODOCK_STORAGE_QUOTAS", "auto").lower() in (
    "off", "false", "0", "no", "disabled",
)
# Absolute path to the privileged helper that drives `xfs_quota` (project assign
# / limit / report — it neither creates nor mounts anything). Called directly
# when the proxy runs as root (privileged Docker), else via `sudo -n` (bare-metal;
# allowlisted in /etc/sudoers.d/otodock-quota).
OTODOCK_QUOTA_HELPER = _cfg("OTODOCK_QUOTA_HELPER", str(BASE_DIR / "scripts" / "oto-quota"))
# Default per-bucket limits when the admin hasn't set the platform_settings keys
# (quota_shared_folder_mb / quota_user_folder_mb / *_inodes). MB; 0 = unlimited
# (accounting continues, no enforcement). Inode caps ship but default OFF.
QUOTA_SHARED_FOLDER_MB_DEFAULT = 15360   # 15 GB — workspace + knowledge + config
QUOTA_USER_FOLDER_MB_DEFAULT = 2048      # 2 GB  — each users/{username}/
QUOTA_EXTERNAL_FOLDER_MB_DEFAULT = 1024  # 1 GB  — externals/ (every caller's tree of one agent)
QUOTA_SHARED_FOLDER_INODES_DEFAULT = 0   # 0 = unlimited (file-count cap default off)
QUOTA_USER_FOLDER_INODES_DEFAULT = 0

# --- Auth mode ---
AUTH_PROVIDER_BYPASS = _cfg_bool("AUTH_PROVIDER_BYPASS", False)

# --- OIDC provider ---
OIDC_ENABLED = _cfg_bool("OIDC_ENABLED", False)
OIDC_PROVIDER_NAME = _cfg("OIDC_PROVIDER_NAME", "SSO")
OIDC_DISCOVERY_URL = _cfg("OIDC_DISCOVERY_URL", "")
OIDC_CLIENT_ID = _cfg("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = _cfg("OIDC_CLIENT_SECRET", "")
OIDC_AUTHORIZE_URL = _cfg("OIDC_AUTHORIZE_URL", "")
OIDC_TOKEN_URL = _cfg("OIDC_TOKEN_URL", "")
OIDC_USERINFO_URL = _cfg("OIDC_USERINFO_URL", "")
OIDC_LOGOUT_URL = _cfg("OIDC_LOGOUT_URL", "")
# The ID token's signature keys and issuer (auth/providers/oidc_provider.py
# verify_id_token): the discovery document's unless set; with neither, the
# token's own issuer when it shares the token endpoint's origin.
OIDC_JWKS_URL = _cfg("OIDC_JWKS_URL", "")
OIDC_ISSUER = _cfg("OIDC_ISSUER", "")
# The ID token signing algorithms the provider announces (discovery).
OIDC_ID_TOKEN_ALGS: list[str] = []
OIDC_SCOPES = _cfg("OIDC_SCOPES", "openid profile email groups")
if "openid" not in OIDC_SCOPES.split():
    # The sign-in needs an ID token, which only the openid scope returns.
    OIDC_SCOPES = f"openid {OIDC_SCOPES}".strip()
OIDC_REDIRECT_URI = _cfg("OIDC_REDIRECT_URI", "") or (
    f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/auth/callback" if DASHBOARD_PUBLIC_URL else ""
)
OIDC_ROLE_ADMIN_GROUP = _cfg("OIDC_ROLE_ADMIN_GROUP", "")
OIDC_ROLE_CREATOR_GROUP = _cfg("OIDC_ROLE_CREATOR_GROUP", "")
OIDC_ROLE_MEMBER_GROUP = _cfg("OIDC_ROLE_MEMBER_GROUP", "")
# The identity-provider confirm (SHARING.md "The confirm") is a plain login
# round trip by default: the provider says whether the signed-in browser is
# the same account, and a signed-in one answers at once. True asks the
# provider for a new login instead (prompt=login, max_age=0) and refuses an
# answer whose ``auth_time`` is older than the click — only for a provider
# whose flows honour that (Authentik's defaults do not: they land the
# browser on the callback with no answer, or let the old session through).
OIDC_CONFIRM_FRESH_LOGIN = _cfg_bool("OIDC_CONFIRM_FRESH_LOGIN", False)
# With the fresh login on, an ID token without ``auth_time`` passes unless
# this is true (some providers omit the claim).
OIDC_CONFIRM_REQUIRE_AUTH_TIME = _cfg_bool("OIDC_CONFIRM_REQUIRE_AUTH_TIME", False)

# Explicit UA — Authentik (and many WAFs) reject the default Python client
# User-Agents with 403.
OIDC_DISCOVERY_USER_AGENT = "OtoDock/1.0 OIDC-Discovery"


def apply_oidc_discovery(meta: dict) -> None:
    """Fill any OIDC endpoint URL not explicitly set via env from a parsed
    .well-known/openid-configuration document. Explicit env vars always win —
    a URL that already has a value is never overwritten. Shared by the
    import-time fetch below and the lazy re-discovery in
    auth.providers.oidc_provider (for an IdP that boots slower than the proxy).
    """
    global OIDC_AUTHORIZE_URL, OIDC_TOKEN_URL, OIDC_USERINFO_URL, OIDC_LOGOUT_URL
    global OIDC_JWKS_URL, OIDC_ISSUER, OIDC_ID_TOKEN_ALGS
    OIDC_AUTHORIZE_URL = OIDC_AUTHORIZE_URL or meta.get("authorization_endpoint", "")
    OIDC_TOKEN_URL = OIDC_TOKEN_URL or meta.get("token_endpoint", "")
    OIDC_USERINFO_URL = OIDC_USERINFO_URL or meta.get("userinfo_endpoint", "")
    OIDC_LOGOUT_URL = OIDC_LOGOUT_URL or meta.get("end_session_endpoint", "")
    OIDC_JWKS_URL = OIDC_JWKS_URL or meta.get("jwks_uri", "")
    OIDC_ISSUER = OIDC_ISSUER or meta.get("issuer", "")
    algs = meta.get("id_token_signing_alg_values_supported")
    if not OIDC_ID_TOKEN_ALGS and isinstance(algs, list):
        OIDC_ID_TOKEN_ALGS = [a for a in algs if isinstance(a, str) and a.lower() != "none"]


# OIDC discovery: if OIDC_DISCOVERY_URL is set, fetch .well-known and
# populate any of OIDC_{AUTHORIZE,TOKEN,USERINFO,LOGOUT}_URL that
# weren't individually set. Explicit env vars always win. Failures
# log a warning but don't block startup — the endpoints are then
# re-discovered lazily on the next SSO login attempt
# (auth.providers.oidc_provider.ensure_oidc_discovery), so an IdP that
# comes up after the proxy doesn't require a proxy restart.
if OIDC_ENABLED and OIDC_DISCOVERY_URL:
    import json as _json
    import urllib.request as _urllib_request
    try:
        _req = _urllib_request.Request(
            OIDC_DISCOVERY_URL,
            headers={"User-Agent": OIDC_DISCOVERY_USER_AGENT},
        )
        with _urllib_request.urlopen(_req, timeout=5) as _resp:
            _meta = _json.loads(_resp.read())
        apply_oidc_discovery(_meta)
    except (OSError, ValueError, AttributeError, http.client.HTTPException) as _e:
        _boot_note(f"WARN: OIDC discovery failed for {OIDC_DISCOVERY_URL}: {_e}")

# Group → role mapping, built only from explicitly-set env vars. An OIDC
# deployment that wants role mapping MUST set the three OIDC_ROLE_*_GROUP
# env vars; there is intentionally no implicit default group naming.
OIDC_ROLE_GROUPS: dict[str, str] = {}
if OIDC_ROLE_ADMIN_GROUP:
    OIDC_ROLE_GROUPS[OIDC_ROLE_ADMIN_GROUP] = "admin"
if OIDC_ROLE_CREATOR_GROUP:
    OIDC_ROLE_GROUPS[OIDC_ROLE_CREATOR_GROUP] = "creator"
if OIDC_ROLE_MEMBER_GROUP:
    OIDC_ROLE_GROUPS[OIDC_ROLE_MEMBER_GROUP] = "member"


# WOPI / Collabora
WOPI_SECRET = _cfg("WOPI_SECRET", JWT_SECRET)
# Accept a WOPI call that carries no signature of Collabora (api/media/wopi_proof):
# for an older Collabora, or an operator's own one, that has no proof key yet. It
# takes effect only while Collabora advertises no key; off unless set.
WOPI_ACCEPT_UNSIGNED = _cfg_bool("WOPI_ACCEPT_UNSIGNED", False)
# COLLABORA_URL auto-derives to sub-path mode (OSS default): the platform proxy
# reverse-proxies /collabora/* internally, so users get a working preview just
# by setting DASHBOARD_PUBLIC_URL. Override explicitly for:
#   - subdomain mode: COLLABORA_URL=https://collabora.example.com
#   - central cloud: COLLABORA_URL=https://collabora.otodock.io
# To disable preview entirely, leave both DASHBOARD_PUBLIC_URL and COLLABORA_URL
# empty (an unconfigured platform) or stop the Collabora container (proxy then
# returns 502 on iframe load).
def _default_collabora_url() -> str:
    if not DASHBOARD_PUBLIC_URL:
        return ""
    return f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/collabora"

COLLABORA_URL = _cfg("COLLABORA_URL", _default_collabora_url())
# Internal URL that Collabora (inside Docker) uses to reach proxy WOPI endpoints.
# Must bypass Cloudflare/Authentik — direct to proxy on the host network.
#
# Auto-derived from the deployment shape. A proxy in Docker Compose
# (RUNNING_IN_DOCKER) is reached by service DNS, the PROXY_SERVICE_NAME host
# on the proxy's port (the shipped compose names the proxy service
# `otodock-proxy`). A proxy on the host (a native install with the Docker
# Collabora of file-tools-mcp) is reached as host.docker.internal on the
# proxy's port, which works on Linux because the file-tools-mcp compose
# already declares `extra_hosts: host.docker.internal:host-gateway`.
#
# Same deployment split (and the same proxy service-DNS name) as the Docker-MCP
# callback host in ``core/config/deployment.proxy_callback_host`` — keyed on
# RUNNING_IN_DOCKER + PROXY_SERVICE_NAME so WOPI and
# ``${platform.proxy_url_for_docker}`` always resolve to the SAME proxy host.
# K8s and unusual setups override `WOPI_BASE_URL` via `config.env` to point at
# the cluster service DNS (e.g. http://otodock-proxy.otodock.svc.cluster.local:8400).
def _default_wopi_base_url() -> str:
    if RUNNING_IN_DOCKER:
        return f"http://{PROXY_SERVICE_NAME}:{PORT}"
    return f"http://host.docker.internal:{PORT}"

WOPI_BASE_URL = _cfg("WOPI_BASE_URL", _default_wopi_base_url())

# Host Asterisk dials to reach the phone server's AudioSocket listener — the
# dial target baked into the generated dialplan snippet. This is NOT the bind
# address (``phone_audiosocket_host`` = 0.0.0.0 stays the listener bind) and NOT
# the PBX address; it's *this* machine's address as reached by the PBX.
#
# Auto-derived: the machine's primary outbound IP (reachable by a same-host PBX
# at its own LAN IP and by a bridged PBX VM); falls back to 127.0.0.1.
#
# Docker NOTE: inside a container this resolves to the *container* IP, which an
# external/host Asterisk can't reach — the compose/setup MUST set
# OTO_AUDIOSOCKET_PUBLIC_HOST to the host's reachable address.
def _default_audiosocket_public_host() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no traffic; selects the default-route iface
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip or "127.0.0.1"
    except OSError:
        return "127.0.0.1"

AUDIOSOCKET_PUBLIC_HOST = _cfg("OTO_AUDIOSOCKET_PUBLIC_HOST", _default_audiosocket_public_host())
# True when the operator never set the key and the fallback autodetect ran —
# the phone adapters use this to warn when that autodetect happened inside a
# container (the detected IP is the container's own, unreachable by the PBX).
AUDIOSOCKET_PUBLIC_HOST_AUTODETECTED = not _cfg("OTO_AUDIOSOCKET_PUBLIC_HOST")

# Whether TLS terminates in front of Collabora (drives the scheme of its
# origin check and generated self-URLs). Plain-http installs (LAN T1 on
# :8400) need false or the WS origin check expects https and rejects the
# editor socket. Docker installs carry it in config.env, which the T2 compose
# file reads (upgrade.sh, install.sh and the wizard write it, scripts/compose.sh
# exports it); a native install gets it in the file-tools .env from here.
def sync_collabora_scheme() -> str | None:
    """Keep config.env's ``COLLABORA_SSL_TERMINATION`` on the scheme of the
    ``DASHBOARD_PUBLIC_URL`` the same file sets, as Collabora's entrypoint
    derived it at every start before 26.04 (whose image has no shell): the
    line is written when it is missing or disagrees, and the boot log says
    the editor follows at the next ``docker compose up -d``. Left alone when
    the file sets no ``DASHBOARD_PUBLIC_URL``, or sets ``COLLABORA_URL`` (a
    Collabora on its own address keeps the operator's value). Returns the
    value written, or None."""
    url = (_file_cfg.get("DASHBOARD_PUBLIC_URL") or "").strip()
    if not url or (_file_cfg.get("COLLABORA_URL") or "").strip():
        return None
    want = collabora_ssl_termination_for(url)
    have = (_file_cfg.get("COLLABORA_SSL_TERMINATION") or "").strip().lower()
    if have and (have in ("1", "true", "yes", "on")) == (want == "true"):
        return None
    try:
        _set_config_line("COLLABORA_SSL_TERMINATION", want)
    except OSError as e:
        _boot_note(f"WARNING: COLLABORA_SSL_TERMINATION should be {want} for "
                   f"DASHBOARD_PUBLIC_URL={url}, and config.env could not be written "
                   f"({type(e).__name__}): set it by hand, then run docker compose up -d")
        return None
    _boot_note(f"COLLABORA_SSL_TERMINATION={want} written to config.env (the scheme of "
               "DASHBOARD_PUBLIC_URL): a Docker install's document editor follows at the "
               "next docker compose up -d")
    return want


sync_collabora_scheme()
COLLABORA_SSL_TERMINATION = _cfg(
    "COLLABORA_SSL_TERMINATION", collabora_ssl_termination_for(DASHBOARD_PUBLIC_URL),
)

# Path prefix Collabora is mounted under on the dashboard's domain. Empty for
# subdomain deployments (e.g. https://collabora.example.com); set to e.g.
# "/collabora" for same-domain sub-path deployments where the reverse proxy
# routes ${DASHBOARD_PUBLIC_URL}/collabora/* to the Collabora container WITHOUT
# stripping the prefix (Collabora's own service_root awareness handles the
# routing internally). Auto-derived from the URL path of COLLABORA_URL so a
# user only sets COLLABORA_URL and everything downstream lines up.
def _default_service_root() -> str:
    if not COLLABORA_URL:
        return ""
    try:
        from urllib.parse import urlparse
        return urlparse(COLLABORA_URL).path.rstrip("/") or ""
    except ValueError:
        return ""

COLLABORA_SERVICE_ROOT = _cfg("COLLABORA_SERVICE_ROOT", _default_service_root())

# Where the platform proxy reaches the Collabora container internally for
# sub-path reverse-proxying (api/media/collabora_proxy.py). Default suits native
# install / dev (host port 9981); Docker-compose deployments override to
# `http://collabora:9980` (service DNS) when the proxy itself runs in compose.
# Only consulted in sub-path mode (COLLABORA_URL on the dashboard's host);
# ignored in subdomain / central-cloud modes where the proxy doesn't intercept
# /collabora/* at all.
COLLABORA_BACKEND_URL = _cfg("COLLABORA_BACKEND_URL", "http://localhost:9981")

# Google Workspace OAuth
GOOGLE_OAUTH_CLIENT_ID = _cfg("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = _cfg("GOOGLE_OAUTH_CLIENT_SECRET", "")
_google_redirect_default = (
    f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/v1/oauth/google/callback"
    if DASHBOARD_PUBLIC_URL else ""
)
GOOGLE_OAUTH_REDIRECT_URI = _cfg(
    "GOOGLE_OAUTH_REDIRECT_URI", _google_redirect_default
)


_unknown_keys = unknown_config_keys(
    _file_cfg.keys(), _CFG_KEYS_READ | LAZY_CONFIG_KEYS | FOREIGN_CONFIG_KEYS,
)
if _unknown_keys:
    _boot_note(
        f"WARNING: {_config_env} sets keys no OtoDock component reads: "
        f"{', '.join(_unknown_keys)} (a misspelt key is silently ignored; "
        f"config.env.example lists every key)."
    )
