"""Platform configuration — config.env parsing, secrets, and derived settings.

Deliberately ONE module: a `config/` package split was reviewed and retired
for good (2026-07-10) — the import-order-sensitive blocks make any split
high-blast-radius for zero user value. Don't re-propose it.
"""

import contextlib
import hmac
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

from dotenv import dotenv_values
from auth import roles
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities
from core import layout

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


def _cfg(key: str, default: str = "") -> str:
    """Read config: os.environ (Docker/systemd override) > config.env file > default."""
    return os.environ.get(key) or _file_cfg.get(key) or default


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

_running_in_docker_env = _cfg("RUNNING_IN_DOCKER", "").lower()
if _running_in_docker_env in ("1", "true", "yes"):
    RUNNING_IN_DOCKER = True
elif _running_in_docker_env in ("0", "false", "no"):
    RUNNING_IN_DOCKER = False
else:
    RUNNING_IN_DOCKER = _default_running_in_docker()


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


def capture_dashboard_public_url(origin: str) -> bool:
    """First-run capture of DASHBOARD_PUBLIC_URL (setup wizard).

    Captures ONLY when the operator never set the value in config.env (the
    FILE is the explicit user setting — compose always injects a localhost
    default via the container environment, so an env check can never read
    "unset") AND the effective value is empty or that localhost default.
    A localhost origin is never captured (it IS the default). Explicit
    config always wins; the new value applies at the next platform restart
    (Collabora reads it at container start)."""
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
    return True

AGENTS_DIR = PLATFORM_DATA_DIR / "agents"  # persistent — overridable via PLATFORM_DATA_DIR
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
RECOVER_BIN_MAX_BYTES = int(_cfg("RECOVER_BIN_MAX_MB", "100")) * 1024 * 1024
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
PORT = int(_cfg("PROXY_PORT", "8400"))
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
LOOP_WATCHDOG_THRESHOLD_S = float(_cfg("LOOP_WATCHDOG_THRESHOLD_S", "2.0") or 0)
# Shorter stalls, from this long up, are logged once with the stalled stack
# and counted in the ``loop`` histogram. 0 disables them.
LOOP_WATCHDOG_REPORT_S = float(_cfg("LOOP_WATCHDOG_REPORT_S", "0.25") or 0)

# Per-sandbox cap on the RAM-backed ``/tmp`` tmpfs (bwrap ``--size``), in MB.
# ``/tmp`` is the agent's HOME (package caches, model downloads) and the CLI's
# runtime root; every local sandbox has its own, so an uncapped fleet can
# commit N × RAM/2. Bounded by half the host's RAM at use (never looser than
# the kernel default); 0 disables; ignored where bubblewrap lacks ``--size``
# (0.6.1). See core/sandbox/sandbox.py::tmpfs_cap_mb.
SANDBOX_TMP_SIZE_MB = int(_cfg("SANDBOX_TMP_SIZE_MB", "4096") or 0)

# Universal per-file size cap: ONE knob for the satellite sync engine (manifest
# + push/pull legs) AND every dashboard/chat upload (both hit POST /v1/upload).
# Satellites learn the sync cap via the auth_result policy handshake — the proxy
# config is the single source of truth; pre-0.5.103 satellites are pinned to the
# legacy 100MB by the per-machine gate in satellite_connection.effective_sync_cap.
MAX_FILE_MB = int(_cfg("OTODOCK_MAX_FILE_MB", "1024"))
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
SYNC_DELTA_ALERT_FILES = int(_cfg("OTODOCK_SYNC_DELTA_ALERT_FILES", "2000"))
SYNC_DELTA_ALERT_BYTES = int(_cfg("OTODOCK_SYNC_DELTA_ALERT_MB", "500")) * 1024 * 1024

# Chunk size handed to browsers by POST /v1/upload/chunked/init. Files bigger
# than one chunk upload as N sequential ≤chunk-size requests, so CDN/gateway
# request-body caps (Cloudflare ~100MB Free/Pro, nginx client_max_body_size)
# never see the whole file. The default keeps a 1GB file at 32 requests while
# clearing common edge caps with a 3× margin. The server DICTATES this value —
# clients slice strictly by what init returns (a client-chosen size would
# invite silent offset corruption).
UPLOAD_CHUNK_MB = int(_cfg("OTODOCK_UPLOAD_CHUNK_MB", "32"))
UPLOAD_CHUNK_BYTES = UPLOAD_CHUNK_MB * 1024 * 1024
# Chunked-upload staging bounds: open uploads per user (429) and in total
# (503), staged MB per user (429; never below one file's cap), and how long
# an upload may wait for its FIRST chunk before the sweep drops it (one
# with chunks keeps the 24 h resume window). 0 = no cap for the counts.
UPLOAD_MAX_OPEN = int(_cfg("OTODOCK_UPLOAD_MAX_OPEN", "4"))
UPLOAD_MAX_OPEN_TOTAL = int(_cfg("OTODOCK_UPLOAD_MAX_OPEN_TOTAL", "256"))
UPLOAD_STAGING_USER_MB = max(
    MAX_FILE_MB, int(_cfg("OTODOCK_UPLOAD_STAGING_USER_MB", str(2 * MAX_FILE_MB))))
UPLOAD_FIRST_CHUNK_S = int(_cfg("OTODOCK_UPLOAD_FIRST_CHUNK_S", "3600"))
# Free space a new upload must leave on the data disk: the larger of the two
# (507 below it).
MIN_FREE_DISK_MB = int(_cfg("OTODOCK_MIN_FREE_DISK_MB", "5120"))
MIN_FREE_DISK_PCT = float(_cfg("OTODOCK_MIN_FREE_DISK_PCT", "5"))

# Global cap on CONCURRENT LARGE outbound (proxy→satellite) workspace pushes,
# across ALL machines and both push paths (live fan-out + initial-sync push
# branch). Per-machine pacing already exists (windowed acks + the two-lane
# writer); this bounds the AGGREGATE — default 3 ≈ max 24MB in-flight
# regardless of machine count. 0 = unlimited (gate disabled).
SYNC_FANOUT_CONCURRENCY = int(_cfg("OTODOCK_SYNC_FANOUT_CONCURRENCY", "3"))
# Pushes SMALLER than this bypass the gate entirely (no slot, no queued row):
# a small file is a handful of 512KB frames already bounded by the bulk queue,
# and gating it would head-of-line-block live edits behind bulk transfers.
SYNC_FANOUT_MIN_MB = int(_cfg("OTODOCK_SYNC_FANOUT_MIN_MB", "4"))
# A satellite's file_changed frames: appliers in flight per machine (frames
# past it are dropped with a rate-limited warning; 0 = no shedding),
# appliers running at once per machine and in total, and the size of the
# last-change stamp map.
SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE = int(
    _cfg("SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", "64"))
SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE = max(1, int(
    _cfg("SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE", "4")))
SAT_FILE_CHANGED_CONCURRENCY_GLOBAL = max(1, int(_cfg("SAT_FILE_CHANGED_CONCURRENCY_GLOBAL", "8")))
SAT_FILE_CHANGED_STAMP_MAX = max(1, int(_cfg("SAT_FILE_CHANGED_STAMP_MAX", "4096")))
# Threads that write, fsync and rename uploaded and pulled files.
FILE_COMMIT_WORKERS = max(1, int(_cfg("FILE_COMMIT_WORKERS", "4")))

# Global BACKSTOP cap on request body size (bytes), enforced by an ASGI
# middleware via Content-Length. Defaults to the universal file cap plus margin
# (multipart framing overhead) so it only rejects abusive oversized bodies
# (→ 413); finer per-endpoint caps still apply underneath.
MAX_REQUEST_BODY_BYTES = int(
    _cfg("MAX_REQUEST_BODY_BYTES", str(MAX_FILE_BYTES + 64 * 1024 * 1024))
)

# Tiered body caps under that backstop, counted on the streamed bytes: the
# login routes, the webhook receivers, a request with no valid credential (a
# dashboard session cookie, the service key or a session token as the bearer,
# or a WOPI access token on the WOPI and Collabora routes; one that is
# presented but does not validate gets 401), and every other route off the
# upload allowlist (which keeps the backstop). 0 turns a tier off (the
# backstop still applies).
MAX_AUTH_BODY_BYTES = int(_cfg("MAX_AUTH_BODY_BYTES", str(64 * 1024)))
MAX_WEBHOOK_BODY_BYTES = int(_cfg("MAX_WEBHOOK_BODY_BYTES", str(2 * 1024 * 1024)))
MAX_UNAUTH_BODY_BYTES = int(_cfg("MAX_UNAUTH_BODY_BYTES", str(64 * 1024)))
MAX_JSON_BODY_BYTES = int(_cfg("MAX_JSON_BODY_BYTES", str(8 * 1024 * 1024)))

# Hard ceiling for any paginated list endpoint's page size.
MAX_PAGE_SIZE = int(_cfg("MAX_PAGE_SIZE", "500"))

# A text file larger than this is not read inline by the files API (413):
# the client downloads it instead.
INLINE_TEXT_MAX_BYTES = int(_cfg("INLINE_TEXT_MAX_BYTES", str(5 * 1024 * 1024)))
# Workspace zip downloads: builds at once, paths in one request, files in
# one archive and the total input size (413 past the last three).
ZIP_MAX_CONCURRENT = max(1, int(_cfg("ZIP_MAX_CONCURRENT", "2")))
ZIP_MAX_PATHS = int(_cfg("ZIP_MAX_PATHS", "500"))
ZIP_MAX_ENTRIES = int(_cfg("ZIP_MAX_ENTRIES", "100000"))
ZIP_MAX_INPUT_MB = int(_cfg("ZIP_MAX_INPUT_MB", "4096"))
# Temporary public image URLs (/v1/images/temp): the mint copies the image,
# so each copy is capped, and so are the copies one session and all
# sessions hold at once.
TEMP_IMAGE_MAX_BYTES = int(_cfg("TEMP_IMAGE_MAX_MB", "20")) * 1024 * 1024
TEMP_IMAGE_MAX_PER_SESSION = int(_cfg("TEMP_IMAGE_MAX_PER_SESSION", "20"))
TEMP_IMAGE_MAX_TOTAL_BYTES = int(_cfg("TEMP_IMAGE_MAX_TOTAL_MB", "256")) * 1024 * 1024

# Password hashes (bcrypt, about a core for 250 ms each) computed at once;
# the rest wait their turn off the event loop.
AUTH_HASH_CONCURRENCY = max(1, int(_cfg("AUTH_HASH_CONCURRENCY", "4")))
# Password hashes waiting their turn; past this a login or a password change
# is answered 503 with Retry-After instead of queueing.
AUTH_HASH_MAX_WAITERS = int(_cfg("AUTH_HASH_MAX_WAITERS", "64"))
# Webhook-fire key checks (bcrypt) at once, and how many may wait before
# further fires are refused (503).
WEBHOOK_KEY_VERIFY_CONCURRENCY = max(1, int(_cfg("WEBHOOK_KEY_VERIFY_CONCURRENCY", "2")))
WEBHOOK_KEY_VERIFY_MAX_WAITERS = int(_cfg("WEBHOOK_KEY_VERIFY_MAX_WAITERS", "16"))

# Max total DECOMPRESSED size of an uploaded community-MCP zip (zip-bomb guard).
MCP_ZIP_DECOMPRESSED_MAX = int(_cfg("MCP_ZIP_DECOMPRESSED_MAX", str(500 * 1024 * 1024)))


def _rate_limit_rule(prefix: str, mx: int, window: int, base_block: int, max_block: int) -> dict:
    """One keyed-rate-limit bucket config, each field env-overridable as
    ``RATE_LIMIT_<PREFIX>_{MAX,WINDOW,BLOCK,MAX_BLOCK}``."""
    return {
        "max": int(_cfg(f"RATE_LIMIT_{prefix}_MAX", str(mx))),
        "window": int(_cfg(f"RATE_LIMIT_{prefix}_WINDOW", str(window))),
        "base_block": int(_cfg(f"RATE_LIMIT_{prefix}_BLOCK", str(base_block))),
        "max_block": int(_cfg(f"RATE_LIMIT_{prefix}_MAX_BLOCK", str(max_block))),
    }


# bucket → {max, window, base_block, max_block} (seconds, except ``max`` = count
# within the window). Consumed by ``auth/rate_limiter.py``. ``login`` preserves
# the historical thresholds; the rest gate previously-unlimited surfaces.
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
    # Per-IP webhook AUTH-FAILURE throttle — strict, to rate-limit key brute force.
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
    # Wrong passwords on an external link, per client IP and per link
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
CLAUDE_TIMEOUT = int(_cfg("CLAUDE_TIMEOUT", "7200"))  # 2 hours — headroom for long tasks; per-deployment override via CLAUDE_TIMEOUT, per-install override via the session_timeout platform setting

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
PHONE_CALLS_PER_AGENT_PER_HOUR = int(_cfg("PHONE_CALLS_PER_AGENT_PER_HOUR", "10"))
PHONE_CALLS_PER_USER_PER_HOUR = int(_cfg("PHONE_CALLS_PER_USER_PER_HOUR", "5"))
PHONE_CALLS_PER_AGENT_PER_DAY = int(_cfg("PHONE_CALLS_PER_AGENT_PER_DAY", "50"))
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
    """Return the root directory for an agent: AGENTS_DIR / agent_name."""
    return AGENTS_DIR / agent_name


_DOC_EXTENSIONS = {".md", ".txt", ".markdown"}
_MAX_DOC_BYTES = 1 * 1024 * 1024       # 1 MB per file
_MAX_TOTAL_DOC_BYTES = 5 * 1024 * 1024  # 5 MB total per agent


def persona_is_unconfigured(agent_name: str) -> bool:
    """True when the agent's persona file still holds nothing but its title.

    A dashboard-created agent is seeded with ``# {display_name}\\n\\n`` and
    nothing else, and that empty state is otherwise INVISIBLE — the prompt
    builder only bails on a MISSING persona, so a title-only file renders as
    a perfectly valid (and empty) section 1. The manager-facing
    ``# Building Agents`` section uses this to say so, once, until someone
    writes the persona.

    Fail-safe: an unreadable/missing file returns False. Never nag when we
    cannot tell (a template-installed agent always has real content).
    """
    if not is_safe_agent_name(agent_name):
        return False
    config_dir = AGENTS_DIR / agent_name / layout.CONFIG
    for candidate in (config_dir / "agent.md", config_dir / "prompt.md"):
        try:
            text = candidate.read_text()
        except OSError:
            continue
        # Drop a single leading H1 (the seeded title) and see what remains.
        body = text.strip()
        if body.startswith("#"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        return not body.strip()
    return False


def _read_agent_files(model: str) -> list[tuple[str, str]]:
    """Read an agent's persona (config/agent.md) and every doc under config/context/.

    ``agent.md`` is the persona file; ``prompt.md`` is its pre-1.4 name and
    stays readable so a restored old backup keeps working until the startup
    sweep (``agent_store.migrate_persona_filenames``) converges it. When both
    exist (e.g. a stale satellite pushed the old file back), ``agent.md`` wins.

    All files under config/context/ matching `.md`/`.txt`/`.markdown` auto-load
    as context. Files over 1 MB and the tail past a 5 MB total cap are skipped;
    skipped files are listed in a synthesized `_auto_context_skipped.md` so the
    agent knows what didn't make it in.

    Returns list of (relative_path, content) tuples.
    """
    if not is_safe_agent_name(model):
        return []
    agent_dir = AGENTS_DIR / model
    config_dir = agent_dir / layout.CONFIG
    persona_text: str | None = None
    # Two passes: a concurrent rename/restore can remove the probed file
    # between exists() and read_text() — re-probe both names before giving up.
    for _attempt in range(2):
        for candidate in (config_dir / "agent.md", config_dir / "prompt.md"):
            try:
                persona_text = candidate.read_text()
                break
            except OSError:
                continue
        if persona_text is not None:
            break
    if persona_text is None:
        return []

    files: list[tuple[str, str]] = [("agent.md", persona_text)]

    context_dir = agent_dir / layout.CONFIG / layout.CONTEXT
    if not context_dir.is_dir():
        return files

    candidates = sorted(
        p for p in context_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _DOC_EXTENSIONS
    )

    total_bytes = 0
    skipped: list[str] = []
    for p in candidates:
        size = p.stat().st_size
        rel = str(p.relative_to(agent_dir))
        if size > _MAX_DOC_BYTES:
            skipped.append(f"{rel} ({size} bytes — over 1 MB per-file limit)")
            continue
        if total_bytes + size > _MAX_TOTAL_DOC_BYTES:
            skipped.append(f"{rel} (5 MB total cap reached)")
            break
        files.append((rel, p.read_text(encoding="utf-8", errors="replace")))
        total_bytes += size

    if skipped:
        files.append((
            "_auto_context_skipped.md",
            "## Auto-context files skipped\n\n"
            + "\n".join(f"- {s}" for s in skipped),
        ))

    return files


# Directories to skip when scanning the workspace tree. The session-state
# / runtime-cache dirs (.claude, .codex) would only add noise; .git is a
# huge tree of plumbing the agent never needs to read directly; venv /
# node_modules / __pycache__ are dependency artefacts. ``.`` prefix dirs
# are also skipped (existing behavior).
_TREE_SKIP_DIRS: set[str] = {
    "venv", "node_modules", "__pycache__",
}

_TREE_MAX_DEPTH = 5
_TREE_MAX_BYTES = 50_000          # ~12K tokens at typical text density
_TREE_MAX_NAME_LEN = 80           # truncate excessively long filenames


def _scan_workspace(agent_dir: Path, agent_name: str, *,
                    username: str | None = None,
                    role: str = "manager",
                    sandboxed: bool = True,
                    mount_shared: bool = True,
                    external_home: str = "") -> str | None:
    """Scan an agent's directories and return a formatted tree.

    Returns ``None`` if no folder is visible to this session. Otherwise
    returns a multi-line string with directories AND files, sorted
    dirs-first within each level, max depth ``_TREE_MAX_DEPTH``,
    truncated when the total rendered text exceeds ``_TREE_MAX_BYTES``.

    Role-based visibility mirrors ``# Folders`` in the permission context:

    - viewer (user-scope): users/{own}/ + workspace/ (RO) + knowledge/ (RO)
    - editor (user-scope): users/{own}/ + workspace/ (RW) + knowledge/ (RO)
    - manager/admin (user-scope): users/{own}/ + workspace/ + knowledge/
      + config/
    - agent-scoped (no username): workspace/ + knowledge/

    When ``sandboxed=True``, root paths use sandbox-relative prefixes
    (``/workspace/`` etc.) — what the CLI sees inside bwrap.
    """
    lines: list[str] = []
    rendered_bytes = 0
    truncated_count = 0  # entries past the cap we couldn't fit

    def _trim_name(name: str) -> str:
        if len(name) <= _TREE_MAX_NAME_LEN:
            return name
        return name[:_TREE_MAX_NAME_LEN - 1] + "…"

    def _walk(current: Path, depth: int, indent: int) -> None:
        nonlocal rendered_bytes, truncated_count
        if depth > _TREE_MAX_DEPTH:
            return
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            return
        # Filter: skip hidden + denylisted dirs; keep everything else.
        visible = [
            e for e in entries
            if not e.name.startswith(".") and e.name not in _TREE_SKIP_DIRS
        ]
        # Sort: directories first, then files; both alphabetical.
        dirs = sorted([e for e in visible if e.is_dir()], key=lambda p: p.name)
        files = sorted([e for e in visible if e.is_file()], key=lambda p: p.name)
        for d in dirs:
            line = f"{'  ' * indent}{_trim_name(d.name)}/"
            if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
                truncated_count += 1
                continue
            lines.append(line)
            rendered_bytes += len(line) + 1
            _walk(d, depth + 1, indent + 1)
        for f in files:
            line = f"{'  ' * indent}{_trim_name(f.name)}"
            if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
                truncated_count += 1
                continue
            lines.append(line)
            rendered_bytes += len(line) + 1

    def _emit_root(rel_label: str, root: Path) -> None:
        nonlocal rendered_bytes
        if not root.is_dir():
            return
        line = rel_label
        if rendered_bytes + len(line) + 1 > _TREE_MAX_BYTES:
            return
        lines.append(line)
        rendered_bytes += len(line) + 1
        _walk(root, 1, 1)

    # An external caller's own tree (mounted at /caller) comes first.
    if external_home:
        for sub in layout.USER_SUBDIRS:
            _emit_root(f"/caller/{sub}/", Path(external_home) / sub)
    # Agent-scope (no username): /workspace/ + /knowledge/
    if not username:
        _emit_root(f"{layout.V_WORKSPACE}/", agent_dir / layout.WORKSPACE)
        _emit_root(f"{layout.V_KNOWLEDGE}/", agent_dir / layout.KNOWLEDGE)
    else:
        # User-scope: own user dir + shared spaces + (manager+) config
        user_dir = layout.user_dir(agent_dir, username)
        if user_dir.is_dir():
            for sub in layout.USER_SUBDIRS:
                _emit_root(f"{layout.virtual_user_root(username)}/{sub}/", user_dir / sub)
        # Shared workspace + knowledge only when the agent's mode has them
        # (Personal-only omits both — they aren't mounted in that mode).
        if mount_shared:
            _emit_root(f"{layout.V_WORKSPACE}/", agent_dir / layout.WORKSPACE)
            _emit_root(f"{layout.V_KNOWLEDGE}/", agent_dir / layout.KNOWLEDGE)
        if roles.can_manage(role):
            _emit_root(f"{layout.V_CONFIG}/", agent_dir / layout.CONFIG)

    if truncated_count:
        lines.append(
            f"… ({truncated_count} more entries not shown — tree truncated "
            f"at ~{_TREE_MAX_BYTES // 1000}KB)"
        )

    return "\n".join(lines) if lines else None


# Memory injection: while a scope's topic files total ≤ the admin
# `inline_budget_bytes` (memory_settings, default 8 KB) they inject in FULL;
# past it the section degrades to the generated MEMORY.md index + the
# `memory` tool's `view`. A hand-edited single file above the per-file cap
# is skipped with a note (tool writes can't produce one).
_MEMORY_INLINE_FILE_HARD_CAP = 64 * 1024


def _render_memory_scope(root, *, virtual: str, budget: int,
                         writable: bool = True) -> str:
    """One scope's body: inline topics, index-only, or empty-state priming."""
    from services.memory import memory_file

    topics = memory_file.iter_topic_files(root)
    if not topics:
        if writable:
            return (
                f"_No memories saved yet. Create the first topic with the "
                f"`memory` tool: `create {virtual}/<topic>.md`._"
            )
        return "_No memories saved yet._"
    total = sum(p.stat().st_size for p in topics)
    out: list[str] = []
    if total <= budget:
        for p in topics:
            rel = p.relative_to(root).as_posix()
            if p.stat().st_size > _MEMORY_INLINE_FILE_HARD_CAP:
                out.append(f"### `{rel}`\n\n_(skipped — file over 64 KB; "
                           f"read it with the `memory` tool)_")
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace").rstrip()
            except OSError:
                continue
            out.append(f"### `{rel}`\n\n{content}")
    else:
        # Over budget → index only. Heal first so hand-edits are reflected.
        memory_file.heal_index_if_stale(root)
        index_path = root / memory_file.INDEX_FILENAME
        try:
            index = index_path.read_text(encoding="utf-8", errors="replace").rstrip()
        except OSError:
            index = memory_file.build_index_content(root).rstrip()
        out.append(index)
        out.append(
            f"_(memory over the inline budget — index only; read a topic "
            f'with `memory(command="view", path="{virtual}/<topic>")`)_'
        )
    return "\n\n".join(out)


def _render_memory_sections(model: str, agent_dir: Path, *,
                            username: str | None, role: str,
                            external: bool = False,
                            external_home: str = "") -> str | None:
    """The `# Memory` prompt section: capture directive + per-scope content.

    Scope loading matrix: user-scoped sessions get agent + user memory;
    agent-scoped sessions (no username) get agent memory only. A viewer's
    agent scope is read-only. An EXTERNAL session (a phone caller who is not
    a platform user) never gets the agent memory; with a caller tree
    (``external_home``) the caller's own memory is its user scope — same
    toggles and mode clamp as a user's. Returns None when memory is fully
    disabled.
    """
    from services.memory import memory_file
    from storage.agents import agent_store
    from storage.agents import memory_store

    settings = memory_store.get_settings()
    toggles = memory_store.get_agent_toggles(model)
    agent_row = agent_store.get_agent(model) or {}
    # Visibility-modes: Personal-only has no agent memory; Shared-only has no
    # user memory. Clamp availability to the agent's mode, on top of the
    # master + per-agent toggles. (Shared-only also has username="" here, which
    # already zeroes user memory; the mode check is belt-and-braces.)
    _collaborative = bool(agent_row.get("collaborative", True))
    _row_scope = agent_row.get("default_scope") or "user"
    _mode_agent_ok = _collaborative or _row_scope == "agent"   # agent scope exists
    _mode_user_ok = _collaborative or _row_scope == "user"     # user scope exists
    agent_enabled = bool(
        settings.get("agent_memory_enabled")
        and toggles.get("agent_memory_enabled")
        and _mode_agent_ok
        and not external
    )
    user_enabled = bool(
        (external_home if external else username)
        and settings.get("user_memory_enabled")
        and toggles.get("user_memory_enabled")
        and _mode_user_ok
    )
    if not (agent_enabled or user_enabled):
        return None

    budget = int(settings.get("inline_budget_bytes") or 8192)

    parts: list[str] = ["\n\n---\n\n# Memory\n"]

    default_scope = agent_row.get("default_scope") or (
        "user" if user_enabled else "agent"
    )
    if default_scope == "user" and not user_enabled:
        default_scope = "agent"
    if default_scope == "agent" and not agent_enabled:
        default_scope = "user"

    scope_lines = []
    if agent_enabled:
        scope_lines.append(
            "- `/memories/agent/` — shared with every user of this "
            "agent: operational facts, conventions, workflows, shared "
            "project state."
        )
    if user_enabled and external:
        scope_lines.append(
            "- `/memories/user/` — private to this caller: their "
            "preferences, context, facts about them. Nothing else on this "
            "line is remembered."
        )
    elif user_enabled:
        scope_lines.append(
            f"- `/memories/user/` — private to {username}: their "
            "preferences, context, facts about them."
        )
    directive = (
        "\nYou have persistent memory that survives across sessions — "
        "markdown topic files you manage with the `memory` tool:\n"
        + "\n".join(scope_lines)
        + f"\n\nDefault scope for new memories: `/memories/{default_scope}/`."
        " **Save the moment you learn something durable** — don't wait "
        "for the end of the session: decisions, infrastructure facts, "
        "conventions, preferences, corrections you receive, anything a "
        "future session will need. When the user says \"remember "
        "this\", save it.\n\n"
        "**Maintain, don't accumulate**: the content below is your "
        "current memory — update existing topics (`str_replace`) "
        "instead of adding duplicates, merge related facts into one "
        "topic, delete entries that turn out wrong, date entries "
        "(YYYY-MM-DD), and supersede outdated facts (\"was X until "
        "DATE; now Y\"). Prefer targeted edits — never rewrite a whole "
        "topic from scratch when a small edit will do. Start each "
        "topic file with a one-line `# heading` (it becomes its index "
        "entry; `MEMORY.md` is auto-generated — never edit it). Never "
        "store secrets, credentials, or tokens.\n"
    )
    # The shared memory is the editor tier's to write (the memory API's
    # rule); a viewer or a contributor reads it and saves user-scope only.
    agent_writable = roles.can_edit(role)
    if not agent_writable and agent_enabled:
        directive += (
            "\nAgent memory is read-only for your role — save "
            "user-scope memories only.\n"
        )
    parts.append(directive)

    if agent_enabled:
        root = memory_file.scope_root(agent_dir, "agent")
        parts.append(
            "\n## Agent memory (shared)\n\n"
            + _render_memory_scope(
                root, virtual="/memories/agent", budget=budget,
                writable=agent_writable,
            )
            + "\n"
        )
    if user_enabled and external:
        root = Path(external_home) / layout.CONTEXT / "memory"
        parts.append(
            "\n## Caller memory (private to this caller)\n\n"
            + _render_memory_scope(root, virtual="/memories/user", budget=budget)
            + "\n"
        )
    elif user_enabled:
        root = memory_file.scope_root(agent_dir, "user", username)
        parts.append(
            f"\n## User memory ({username})\n\n"
            + _render_memory_scope(root, virtual="/memories/user", budget=budget)
            + "\n"
        )
    return "".join(parts)


# Per-bulletin injection cap. Bulletins are prompt-level context — a short
# team briefing, not a document dump.
_BULLETIN_MAX_BYTES = 4 * 1024
# Read-side soft warning. Bulletin edits are plain file writes (no tool
# result to warn through, unlike memory's write-time soft limit), so the
# injected section itself tells every attached agent when pruning is due —
# any RW-attached agent can act on it.
_BULLETIN_SOFT_WARN_BYTES = 3 * 1024


def _read_bulletin(source_agent: str, sub_rel: str) -> str:
    """The SOURCE-resident bulletin's text, capped at 4 KB and truncated on
    a UTF-8 character boundary (decode drops a split trailing sequence).
    '' when the file is missing/unreadable — injection then skips it."""
    path = get_agent_dir(source_agent) / layout.KNOWLEDGE / sub_rel
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        data = path.read_bytes()
    except OSError:
        return ""
    truncated = len(data) > _BULLETIN_MAX_BYTES
    text = data[:_BULLETIN_MAX_BYTES].decode("utf-8", errors="ignore").strip()
    if truncated and text:
        text += ("\n\n_(bulletin truncated at 4 KB — anything below the cap "
                 "never reaches agents; writers should prune older entries)_")
    elif len(data) > _BULLETIN_SOFT_WARN_BYTES and text:
        text += (f"\n\n_(this bulletin is {len(data) / 1024:.1f} KB of the "
                 "4 KB injected cap — writers should prune older entries)_")
    return text


def _render_library_bulletins(agent_name: str) -> str | None:
    """The ``# Library Bulletins`` prompt section — one subsection per
    shared library this agent is ATTACHED to or is the SOURCE of, reading
    the source-resident ``bulletin/<library name>.md`` only (writable
    attachments may author the bulletin since 1.5 — those edits ARRIVE
    here via the projector's normal mirror→source adoption, so the source
    copy stays the single read point). Default-on convention: no flag,
    missing file = no subsection."""
    from storage.knowledge import db_knowledge_libraries
    # Each entry names the library's source, its subdir, its name, whether
    # it is the agent's own and whether it is writable.
    entries: list[tuple[str, str, str, bool, bool]] = []
    for a in db_knowledge_libraries.attachments_for_consumer(agent_name):
        entries.append((a["source_agent"], a["subdir"] or "",
                        a["name"] or "", False, bool(a.get("writable"))))
    for lib in db_knowledge_libraries.libraries_of(agent_name):
        entries.append((agent_name, lib["subdir"] or "",
                        lib["name"] or "", True, False))
    sections: list[str] = []
    for source, subdir, name, own, writable in entries:
        rel = db_knowledge_libraries.bulletin_rel(subdir, name)
        if not rel:
            continue
        text = _read_bulletin(source, rel)
        if not text:
            continue
        if own:
            origin = "this agent's own library"
            hint = f"\n_File: `{layout.V_KNOWLEDGE}/{rel}`._"
        elif writable:
            mount = (f"{layout.V_KNOWLEDGE}/shared/{source}/{subdir}/" if subdir
                     else f"{layout.V_KNOWLEDGE}/shared/{source}/")
            origin = f"from the **{source}** agent"
            hint = (f"\n_Writable attachment — publish updates at "
                    f"`{mount}bulletin/{name}.md`._")
        else:
            origin = f"from the **{source}** agent"
            hint = ""
        sections.append(f"## {name} ({origin}){hint}\n\n{text}")
    if not sections:
        return None
    return (
        "\n\n---\n\n# Library Bulletins\n\n"
        "Each shared knowledge library carries a bulletin — a small runtime "
        "broadcast file auto-loaded into the context of every agent attached "
        "to that library, at every session start. Teams use it to pass "
        "daily/weekly progress and \"what changed\" notes between agents "
        "automatically; it is NOT a memory — keep durable facts in memory or "
        "knowledge files, and keep the bulletin short (only its first 4 KB "
        "is injected): prune superseded entries whenever you update it.\n\n"
        + "\n\n".join(sections)
    )


def build_agent_prompt(model: str, *,
                       username: str | None = None,
                       role: str = "manager",
                       excluded_mcps: dict[str, str] | None = None,
                       dynamic_contexts: list[tuple[str, str]] | None = None,
                       sandboxed: bool = True,
                       client_type: str = "",
                       placement: PlacementCapabilities = LOCAL_PLACEMENT,
                       mount_shared: bool = True,
                       execution_path: str = "",
                       skip_http_mcps: bool = False,
                       external: bool = False,
                       external_home: str = "") -> str | None:
    """Build the full system prompt for an agent.

    ``external`` = an EXTERNAL session (a phone caller who is not a platform
    user): the MCP catalog, the inline skills and the skill catalog drop the
    MCPs such a session never attaches (``mcp_registry.session_exclusion_reason``),
    the "Unavailable Tools" list omits the external rules themselves, and the
    shared agent memory is never injected. ``external_home`` (the caller's
    own tree, mounted at /caller) makes the caller's memory the user scope
    and auto-loads the caller's context docs — the per-user shape, re-rooted.

    Section order:

      1. Agent's own ``config/agent.md`` (the persona)
      2. Platform company context (DB) + universal language rule
      3. ``# Auto-Loaded Documentation`` — ``config/context/*`` knowledge files
      4. ``# Available Tools (MCPs)`` — catalog of enabled MCPs (one-liners)
      5. ``# MCP Tool Skills`` — per-MCP deep-dive docs from manifests
      5b. ``# Skills`` — on-demand skill catalog (Direct-LLM sessions only)
      6. ``# MCP Dynamic Context`` — runtime-generated context per MCP
      7. ``# User Context (Personal)`` — ``users/{u}/context/*`` personal docs
      8. ``# Workspace`` — live directory tree (role-filtered)
      9. ``# Excluded MCPs`` — only when tools are unavailable

    Identity / scope / folder / permission sections are appended later by
    ``auth.path_policy.build_permission_context`` from the per-session
    config builders (config_builder / task_config_builder / phone_config_builder).

    Args:
        model: Agent name.
        username: Filesystem-safe username slug. When set, creates per-user dirs
                  and loads user-context files.
        role: User role (viewer/editor/manager/admin). Controls which
              directories appear in the workspace tree listing.
        excluded_mcps: {mcp_name: reason} dict of unavailable MCPs.
        dynamic_contexts: list of (mcp_name, markdown) tuples from
            ``services.mcp.dynamic_context.get_dynamic_contexts``.
        sandboxed: when True, paths in the prompt use sandbox-relative
            prefixes (``/workspace/``, ``/users/{u}/``).
        client_type: a session kind's name (``core/session/session_kind.py``:
            the manifests' ``exclude_from`` word) or ``""``. Drives MCP catalog filtering against
            each manifest's ``exclude_from``. Empty string skips the filter
            (defense-only — upstream usually pre-filters).
        placement: the session's resolved placement (``core.placement``),
            forwarded to the MCP catalog + skill loaders so a device-local
            MCP (computer / browser / app control) only appears in the prompt
            when the session can actually run it — i.e. on a satellite that
            has granted the capability. The local default keeps
            ``satellite_only`` / device-capability
            MCPs out of local-session prompts.
        execution_path: ``"claude-code-cli"`` / ``"codex-cli"`` /
            ``"direct-llm"`` / ``""``. Only ``"direct-llm"`` changes the
            output: it appends the ``# Skills`` catalog (the CLI engines index
            their materialized skills dir themselves).
        skip_http_mcps: leave sidecar (HTTP-transport) MCPs out of the MCP
            catalog, the inline skills and the skill catalog — a phone
            Direct-LLM session never connects them, so the prompt must not
            describe tools it cannot reach.
    """
    if not is_safe_agent_name(model):
        return None  # unsafe agent name → treat as unknown agent
    own_files = _read_agent_files(model)
    if not own_files:
        return None

    parts = []

    # Agent's own persona (agent.md — always first)
    parts.append(own_files[0][1])

    # Platform company context (from DB — admin-configured) + universal
    # language rule. The language rule is functional (the agent needs to
    # mirror the user's language) and emitted regardless of branding.
    # When ``company_name`` is empty, we deliberately skip the branding
    # block — unbranded deployments don't get an unsolicited marketing
    # line. The DB read can fail during startup (schema not yet migrated)
    # or in tests with no DB; in either case the language rule still ships.
    _LANGUAGE_RULE = (
        "Respond in the same language the user uses unless they ask you to switch."
    )
    company_name = ""
    instructions = ""
    with contextlib.suppress(Exception):
        from storage import database as _db
        company_name = _db.get_platform_setting("company_name") or ""
        instructions = _db.get_platform_setting("platform_instructions") or ""
    if company_name:
        parts.append(f"\n\n---\n\n# {company_name}\n")
        if instructions:
            parts.append(instructions)
        parts.append(f"\n\n{_LANGUAGE_RULE}")
    else:
        parts.append(f"\n\n---\n\n{_LANGUAGE_RULE}")

    # Agent's own extra docs
    if len(own_files) > 1:
        parts.append(
            "\n\n---\n\n"
            "# Auto-Loaded Documentation\n\n"
            "These files are pre-loaded. Do NOT read them from disk.\n"
        )
        for rel_path, content in own_files[1:]:
            parts.append(f"\n## `{rel_path}`\n\n{content}")

    agent_dir = get_agent_dir(model)

    # Auto-create per-user directories
    if username:
        with contextlib.suppress(Exception):
            user_dir = layout.user_dir(agent_dir, username)
            for sub in layout.USER_SUBDIRS:
                (user_dir / sub).mkdir(parents=True, exist_ok=True)

    # Available Tools (MCPs) catalog — one-line summary per enabled MCP.
    # Sits BEFORE MCP Tool Skills so the agent gets a top-down map of its
    # capabilities before diving into per-MCP usage docs. Filtered by
    # client_type against each manifest's exclude_from (defense in depth —
    # build_session_mcp_config already excludes the right ones, but this
    # keeps the prompt's catalog in sync with the actual loaded set).
    # mcp_registry not yet initialized (startup race) → no catalog.
    with contextlib.suppress(Exception):
        from services.mcp import mcp_registry
        catalog = mcp_registry.build_available_mcps_section(
            model, context=client_type or "",
            placement=placement,
            skip_http_mcps=skip_http_mcps,
            external=external,
        )
        if catalog:
            parts.append("\n\n---\n\n" + catalog)

    # Load MCP skills for this agent (from manifest-driven skill system).
    # The real client_type threads through so skill-level exclude_from
    # (e.g. voiceover's ["phone"]) is honored — it was dead at this call
    # site until 2026-07 (hardcoded "system_prompt" matched nothing).
    # mcp_registry not yet initialized (startup race) → no skills section.
    with contextlib.suppress(Exception):
        from services.mcp import mcp_registry
        skills = mcp_registry.get_skills_for_agent(
            model, context=client_type or "",
            placement=placement,
            skip_http_mcps=skip_http_mcps,
            external=external,
        )
        # Only "always" skills inline; "on_demand" skills reach CLI sessions
        # as materialized skill folders (core/sandbox/skills_materializer)
        # where the CLI's own progressive disclosure indexes them, and are
        # deliberately absent on direct-llm (no Read tool to follow a body).
        inline = [(sid, body) for sid, body, loading in skills
                  if loading == "always"]
        if inline:
            parts.append(
                "\n\n---\n\n"
                "# MCP Tool Skills\n\n"
                "The following tool instructions are auto-loaded from active MCP servers.\n"
            )
            for skill_id, content in inline:
                parts.append(f"\n{content}")

    # On-demand skill catalog — only for an engine whose skills are delivered
    # as a prompt catalog (Direct LLM's client-side `Skill` builtin loads what
    # the prompt lists, one line each). The CLI engines index the
    # materialized skills dir themselves with their own Skill tool.
    if _skills_delivery(execution_path) == "prompt_catalog":
        with contextlib.suppress(Exception):
            from services.mcp import mcp_registry
            catalog = mcp_registry.get_skill_catalog_for_agent(
                model, context=client_type or "",
                placement=placement,
                skip_http_mcps=skip_http_mcps,
                external=external,
            )
            if catalog:
                parts.append(
                    "\n\n---\n\n"
                    "# Skills\n\n"
                    "On-demand skills for this session. Load one with the "
                    "`Skill` tool (its full instructions come back as the "
                    "result) before doing that kind of work:\n\n"
                )
                parts.append("\n".join(
                    f"- `{sid}` — {desc}" if desc else f"- `{sid}`"
                    for sid, desc in catalog
                ))

    # Dynamic MCP context (runtime-generated, only for assigned MCPs)
    if dynamic_contexts:
        parts.append("\n\n---\n\n# MCP Dynamic Context\n")
        for _mcp_name, content in dynamic_contexts:
            parts.append(f"\n{content}")

    # Load user-context files (per-user, per-agent personal docs).
    # Size budget enforced here so a runaway pasted markdown can't blow
    # the prompt context. Mirrors the
    # _MAX_DOC_BYTES / _MAX_TOTAL_DOC_BYTES pattern in _read_agent_files.
    _USER_CTX_MAX_FILE_BYTES = 256 * 1024     # 256 KB per file
    _USER_CTX_MAX_TOTAL_BYTES = 1024 * 1024   # 1 MB across all user-ctx files
    # An external caller's context docs (their tree's context/) load the same
    # way — the per-user shape, re-rooted.
    user_ctx_dir = None
    if external_home:
        user_ctx_dir = Path(external_home) / layout.CONTEXT
    elif username:
        user_ctx_dir = layout.context_dir(agent_dir, username)
    if user_ctx_dir is not None:
        if user_ctx_dir.is_dir():
            ctx_files = sorted(user_ctx_dir.glob("*.md"))
            if ctx_files:
                parts.append(
                    "\n\n---\n\n"
                    + ("# Caller Context\n\n"
                       "These notes about the current caller are loaded for "
                       "this caller only.\n"
                       if external_home else
                       "# User Context (Personal)\n\n"
                       "These personal documents are loaded for the current user only.\n")
                )
                running_bytes = 0
                skipped: list[tuple[str, int, str]] = []  # (name, size, reason)
                for md_file in ctx_files:
                    try:
                        size = md_file.stat().st_size
                    except Exception:
                        continue
                    if size > _USER_CTX_MAX_FILE_BYTES:
                        skipped.append((md_file.name, size, "exceeds per-file cap"))
                        continue
                    if running_bytes + size > _USER_CTX_MAX_TOTAL_BYTES:
                        skipped.append((md_file.name, size, "exceeds total cap"))
                        continue
                    with contextlib.suppress(Exception):
                        parts.append(
                            f"\n## `{md_file.name}`\n\n{md_file.read_text()}"
                        )
                        running_bytes += size
                if skipped:
                    skipped_md = "\n".join(
                        f"- `{n}` ({sz:,} bytes): {reason}"
                        for n, sz, reason in skipped
                    )
                    parts.append(
                        "\n## `_user_context_skipped.md`\n\n"
                        "The following user-context files were skipped due to "
                        "size limits.\n\n"
                        + skipped_md
                    )

    # Memory — agent-maintained topic files (knowledge/memory/ shared,
    # users/{u}/context/memory/ per-user) with the capture directive. The
    # generic context loaders above never see these dirs (user-context glob
    # is flat; knowledge/ isn't auto-loaded) — this is their ONLY injection
    # point. Best-effort: a memory failure never breaks prompt build.
    with contextlib.suppress(Exception):
        memory_section = _render_memory_sections(
            model, agent_dir, username=username, role=role,
            external=external, external_home=external_home,
        )
        if memory_section:
            parts.append(memory_section)

    # Shared-library bulletins — the SOURCE-resident
    # ``<subtree>/bulletin/<library name>.md`` of every library this agent
    # is attached to or shares, read server-side so personal-only consumers
    # and agent-scope sessions get it too. Best-effort like memory.
    with contextlib.suppress(Exception):
        bulletin_section = _render_library_bulletins(model)
        if bulletin_section:
            parts.append(bulletin_section)

    # Workspace and user directory listing
    ws_listing = _scan_workspace(agent_dir, model, username=username, role=role,
                                 sandboxed=sandboxed, mount_shared=mount_shared,
                                 external_home=external_home)
    if ws_listing:
        parts.append(
            "\n\n---\n\n"
            "# Workspace\n\n"
            "Available directories:\n\n"
            f"```\n{ws_listing}\n```\n"
        )
        # Path guidance uses sandbox-relative paths
        if external_home:
            parts.append("\nFiles for this caller go to `/caller/workspace/`.")
            if roles.can_write_workspace(role):
                parts.append(" Shared output goes to `/workspace/`.")
        elif external and not roles.can_write_workspace(role):
            parts.append("\nThis session has no writable folder.")
        elif username:
            parts.append(f"\nUser files go to `{layout.virtual_workspace(username)}/`.")
            if roles.can_write_workspace(role):
                parts.append(" Agent-scoped output goes to `/workspace/`.")
        else:
            parts.append("\nAgent-scoped output goes to `/workspace/`.")

    # Excluded MCPs section (tools unavailable in this session). On an
    # external session the entries that ARE the external rules are left
    # out: a caller-facing prompt must not enumerate the platform's
    # management surface (there is nothing the model could "configure").
    listed_exclusions = dict(excluded_mcps or {})
    if external and listed_exclusions:
        with contextlib.suppress(Exception):
            from services.mcp.mcp_registry import (
                EXTERNAL_CONTEXT_REASON, EXTERNAL_DENIED_REASON,
            )
            listed_exclusions = {
                k: v for k, v in listed_exclusions.items()
                if v not in (EXTERNAL_DENIED_REASON, EXTERNAL_CONTEXT_REASON)
            }
    if listed_exclusions:
        parts.append(
            "\n\n---\n\n"
            "# Unavailable Tools\n\n"
            "The following MCP servers are NOT available in this session:\n"
        )
        for mcp_name, reason in sorted(listed_exclusions.items()):
            parts.append(f"- **{mcp_name}**: {reason}")
        parts.append(
            "\nDo not attempt to use tools from these servers. "
            "If the user asks about them, explain they need to be configured first."
        )

    return "\n".join(parts)


# Persistent sessions: idle timeout before reaping (seconds)
PERSISTENT_SESSION_TIMEOUT = int(_cfg("PERSISTENT_SESSION_TIMEOUT", "900"))

# A session with tracked background work (a Bash command left running, a
# background subagent) is not idle: the reapers and the evictor spare it up
# to this ceiling, then close it anyway (a job that never ends must not pin a
# slot forever). Default = the per-turn ceiling.
BACKGROUND_WORK_CEILING_S = int(_cfg("BACKGROUND_WORK_CEILING_S", str(CLAUDE_TIMEOUT)))


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
DIRECT_LLM_MAX_TOKENS = int(_cfg("DIRECT_LLM_MAX_TOKENS", "8192"))

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
DIRECT_LLM_OPENAI_REASONING_MAX_TOKENS = int(
    _cfg("DIRECT_LLM_OPENAI_REASONING_MAX_TOKENS", "32768")
)

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
DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS = int(
    _cfg("DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS", "6000")
)

# When true, the shared MCP installer will attempt to install missing system
# packages (libmagic, libreoffice, etc.) via the local package manager using
# sudo. Default: false — the installer only warns and produces a clear error
# message so the admin can install manually. Applies both on the proxy host
# and on every satellite that runs `mcp_installer.install_system_requirements`.
MCP_AUTO_INSTALL_SYSTEM_DEPS = _cfg("MCP_AUTO_INSTALL_SYSTEM_DEPS", "").lower() in ("1", "true", "yes")

# NOTE: ANTHROPIC_API_KEY / ANTHROPIC_MODEL / CLAUDE_MODEL env vars have been
# removed. API keys flow through the subscription pool (admin-managed via
# Execution Layers page); model selection flows through agent.default_model
# with a DB-driven fallback to the first enabled model for the agent's
# execution_path (see resolve_agent_model() below). No more Anthropic-specific
# platform-level defaults — the platform is provider-agnostic.


# Default effort level for adaptive reasoning (CLI --effort flag).
# Applied at session startup.
# Valid platform levels: "low", "medium", "high", "xhigh", "max" — plus
# "ultra" on the models that support it (per-model, see below).
#   - "max" is the model's reasoning ceiling on every layer but Codex 5.6+:
#     the `claude` CLI's --effort flag accepts only low/medium/high/xhigh/max.
#   - "xhigh" is a distinct intermediate level between "high" and "max", only
#     supported on Opus 4.7+ (Anthropic, incl. 4.8) and the OpenAI gpt-5 family.
#     Models without support silently fall back to "max" at wire time via the
#     failsafe in each execution layer — see get_model_supports_xhigh().
#   - "ultra" (Codex: gpt-5.6 Sol/Terra and GPT-6 Astra) is max reasoning PLUS Codex-native
#     proactive multi-agent orchestration (parallel sub-agent workstreams inside
#     one turn). Offered per-model via supports_ultra / get_model_supports_ultra()
#     and mapped in core/layers/codex/helpers.map_effort_to_codex; every other
#     layer/model clamps a stored "ultra" to its own ceiling ("max"/"xhigh")
#     so it can never reach a CLI or API that rejects it.
#   - "ultracode" (Opus 5) is deliberately NOT in this list. It is a Claude
#     Code *session setting* (`"ultracode": true` via --settings), not a wire
#     effort value: it sends "xhigh" to the model AND turns on dynamic
#     multi-agent workflow orchestration in the CLI harness. Modelling it would
#     be a separate per-session CLI toggle, not a seventh --effort level.
# The platform values are passed through as-is to Anthropic (no translation).
# OpenAI/Codex adapters map the platform scale onto their own native values.
DEFAULT_EFFORT_LEVEL = "high"

# Max thinking tokens cap. Used alongside --effort to ensure thinking content
# is streamed (visible in dashboard). The adaptive effort still controls WHEN
# and HOW MUCH to think; this just sets the upper bound and enables streaming.
MAX_THINKING_TOKENS = int(_cfg("MAX_THINKING_TOKENS", "100000"))


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


# ---------------------------------------------------------------------------
# Central model registry — single source of truth for model metadata.
# Pricing is per 1M tokens: (input, output, cache_write, cache_read).
# Update this when Anthropic changes pricing or adds new models.
# ---------------------------------------------------------------------------
MODEL_REGISTRY: dict[str, dict] = {
    # Fable 5.1 (2026-09-01) replaces the Fable 5 builtin at the SAME base
    # price ($10/$50) with cheaper cache reads (2.5% of input vs 10% —
    # platform.claude.com models overview), so the swap is lossless: a
    # one-time boot remap moves fable-5-pinned agents/tasks/chats to
    # claude-fable-5-1 (MODEL_SUCCESSORS, walked at startup); sync_builtin_models
    # retires the builtin row so pickers stop offering Fable 5 (still served
    # by Anthropic as a legacy model — admins can re-add claude-fable-5 as a
    # custom model).
    "claude-fable-5-1": {
        "label": "Fable 5.1",
        "provider": "anthropic",
        "context_window": 1_000_000,   # 1M native window (as Fable 5)
        "pricing": (10.0, 50.0, 12.50, 0.25),  # cache write 1.25x input; reads 2.5% of input (5.1 cut)
        # CLI-only: Fable's pricing is above Opus-tier, so keeping it off
        # direct-llm bounds accidental spend on the hosted (credit-metered) path.
        # Fable's safety classifiers can refuse a request mid-run; Claude Code
        # ships a built-in automatic fallback to its default Opus for that
        # case (Opus 5.5 since Claude Code 2.1.280; interactive TUI only).
        "layers": ["claude-code-cli"],
        "server_tools": True,   # supports Anthropic web_search/web_fetch
        "supports_reasoning": True,   # thinking is ALWAYS on for Fable (adaptive)
        "supports_xhigh": True,
        "tier": 1,
        "good_at": "complex coding, architecture, subtle bugs, judgement calls and long autonomous work",
    },
    # Opus 5.5 (API id claude-opus-5-5, no alias) replaces the retired Opus 5
    # builtin (which had itself replaced Opus 4.8) at a LOWER price on every
    # element of the tuple with the same capabilities
    # (1M, xhigh, server tools, adaptive thinking always on), so the swap is
    # lossless: a boot remap moves Opus 5-pinned agents/tasks/chats to
    # claude-opus-5-5 (MODEL_SUCCESSORS, walked at startup; Opus 4.8 pins
    # follow the chain); sync_builtin_models retires the builtin row so
    # pickers stop offering Opus 5 (Anthropic lists it as legacy; retirement
    # not announced). Admins can re-add claude-opus-5 as a custom model — the
    # walk then leaves pins on it alone. Prices per 1M from the
    # platform.claude.com models overview at the swap: $4 in, $20 out, $5 cache write
    # (1.25x input), $0.20 cache read (5% of input). Needs Claude Code
    # >= 2.1.280 (VERSIONS.md). Fable 5.1 stays the tier-1 offer above it.
    "claude-opus-5-5": {
        "label": "Opus 5.5",
        "provider": "anthropic",
        "context_window": 1_000_000,   # native 1M window, the API default (no beta header)
        "pricing": (4.0, 20.0, 5.0, 0.20),  # (input, output, cache_write, cache_read) per 1M
        "layers": ["claude-code-cli"],
        "server_tools": True,   # supports Anthropic web_search/web_fetch
        "supports_reasoning": True,
        "supports_xhigh": True,   # keeps the xhigh level (low/medium/high/xhigh/max). NOTE: "ultracode" is NOT an effort level — it's a CLI session setting that pairs xhigh with workflow orchestration; see get_model_supports_xhigh() and DEFAULT_EFFORT_LEVEL.
        "tier": 2,
        "good_at": "substantial, well-scoped coding and analysis; complex or open-ended work belongs on the frontier",
    },
    "claude-sonnet-5": {
        "label": "Sonnet 5",
        "provider": "anthropic",
        "context_window": 1_000_000,   # native 1M window (no [1m] suffix needed)
        "pricing": (2.0, 10.0, 2.50, 0.20),  # $2/$10 made the STANDARD price 2026-08 (the launch-promo increase to $3/$15 on Sept 1 was cancelled — platform.claude.com pricing page + CC 2.1.243 changelog)
        "layers": ["claude-code-cli", "direct-llm"],
        "server_tools": True,
        "supports_reasoning": True,
        "supports_xhigh": True,   # first Sonnet with the xhigh effort level
        "tier": 3,
        "good_at": "drafting, summaries and bounded analysis",
    },
    "claude-haiku-4-5": {
        "label": "Haiku 4.5 (200K)",
        "provider": "anthropic",
        "context_window": 200_000,
        "pricing": (1.0, 5.0, 1.25, 0.10),   # Haiku 4.5 rates (input/output/5m-cache-write/cache-read)
        "layers": ["direct-llm"],
        "server_tools": False,  # does not support programmatic tool calling
        "tier": 4,
        "good_at": "high-volume routine work; no programmatic tool calling on Direct LLM",
    },
    # Admins can add further models (Ollama, LM Studio, more Groq/OpenAI, etc.)
    # dynamically via the admin discover-models UI; the entries below are the
    # predefined builtins that ship with the platform.

    # --- OpenAI models ---
    # All gpt-5 family entries set supports_xhigh: True. Effort mapping:
    # pre-5.6 models top out at wire "xhigh" (platform "max" clamps there);
    # the GPT-5.6 family and GPT-6 add wire "max" — and "ultra" (Sol/Terra
    # and Astra, supports_ultra below): Codex-native proactive multi-agent
    # orchestration on top of max reasoning (Astra delegates at xhigh) — see
    # core/layers/codex/helpers.map_effort_to_codex.
    #
    # GPT-6 Sol and GPT-6 Luna (API ids gpt-6-sol / gpt-6-luna, no alias)
    # replace the retired GPT-5.6 Sol and GPT-5.6 Luna builtins at
    # a LOWER price on every element of the tuple with the same effort
    # ceilings (Sol: max + ultra; Luna: max, no ultra), so both swaps are
    # lossless MODEL_SUCCESSORS entries; GPT-5.6 Terra stays (no cheaper
    # same-capability successor — GPT-6 Sol is dearer). Prices per 1M from
    # developers.openai.com/api/docs/pricing at the swap, stated by OpenAI
    # as permanent, not promotional. Prompt-caching billing as the
    # 5.6 family: cache WRITES cost 1.25x the uncached input rate, cache
    # reads keep the 90% discount — reflected in the tuples. Codex 0.156.1
    # bundles both (VERSIONS.md); its catalog marks 5.6 Sol/Terra → GPT-6
    # Sol and 5.6 Luna → GPT-6 Luna.
    # Context: 272k is the PRICING-TIER line, not the raw model window —
    # the GPT-6 and 5.6 models are really 1.05M (922k input + 128k output),
    # but input beyond 272k bills the WHOLE request at the long-context tier
    # (2x input / 1.5x output). We keep the registry at 272k deliberately:
    # it is codex-cli's own bundled default cap (raised to an opt-in 872k
    # override ceiling in 0.149.0, default unchanged) and it keeps the
    # direct-llm layer's context management from silently crossing into
    # double-rate territory. codex-cli reads the LIVE modelContextWindow at
    # runtime (headless gauges self-correct); this registry number drives
    # the direct-llm layer's context management and the capability display.
    # Agents still PINNED to a retired id with no successor (gpt-5.5) keep
    # running (the CLI supports it; pricing falls back to the provider
    # default) — sync_builtin_models retires the builtin row so pickers
    # stop offering it.
    "gpt-6-sol": {
        "label": "GPT-6 Sol",
        "provider": "openai",
        "context_window": 272_000,   # the pricing-tier line — see the window note above
        "pricing": (2.0, 10.0, 2.50, 0.20),  # per 1M: (input, output, cache_write, cache_read)
        "layers": ["codex-cli"],
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # Codex "ultra": max reasoning + proactive multi-agent
        "tier": 2,
        "good_at": "substantial, well-scoped coding and analysis; complex or open-ended work belongs on the frontier",
    },
    # GPT-6 Astra (2026-09-03; API id gpt-6-astra, no alias, no snapshots): a
    # NEW model, not a rename of any row — no MODEL_SUCCESSORS entry points
    # at it. OpenAI's flagship and Codex's own bundled default since 0.153.4.
    # The platform's codex-cli default is DECLARED (core/layers/codex
    # ModelPolicy.default_model = GPT-6 Sol, tier 2): Astra costs 5× GPT-6
    # Sol on an API key, so a frontier model stays a deliberate choice —
    # registry order no longer decides the "Auto" default.
    # Context: the same 272k pricing-tier line as the 5.6 family (1.05M real:
    # 922k in + 128k out; input beyond 272k bills the whole request at
    # 2× input / 1.5× output — $20 / $75 there). Prices per 1M verified
    # 2026-09-08 (developers.openai.com/api/docs/models/gpt-6-astra):
    # $10 in, $50 out, $12.50 cache write, $1 cached read. codex-cli only for
    # now: the hosted relay prices per model and has no Astra row yet, so a
    # direct-llm listing would fail hosted installs — add the layer with the
    # relay row. Reasoning: low/medium/high/xhigh/max on the API; Codex adds
    # "ultra" (delegation at xhigh — multi_agent_reasoning_effort — not max).
    "gpt-6-astra": {
        "label": "GPT-6 Astra",
        "provider": "openai",
        "context_window": 272_000,
        "pricing": (10.0, 50.0, 12.50, 1.00),  # per 1M: (input, output, cache_write, cache_read)
        "layers": ["codex-cli"],
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # Codex "ultra": proactive multi-agent on top of xhigh reasoning
        "tier": 1,
        "good_at": "complex coding, architecture, subtle bugs, judgement calls and long autonomous work",
    },
    "gpt-5.6-terra": {
        "label": "GPT-5.6 Terra",
        "provider": "openai",
        "context_window": 272_000,   # corrected from 1M — see the family window note above
        "pricing": (2.0, 12.0, 2.50, 0.20),  # 2026-07-30 cut ($2.50/$15 → $2/$12), permanent
        "layers": ["codex-cli", "direct-llm"],
        "server_tools": True,   # OpenAI's built-in web_search (Responses API, direct-llm)
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # codex-cli only — the dashboard offers Ultra
                                  # solely when the codex engine serves the model
        "tier": 3,
        "good_at": "drafting, summaries and bounded analysis",
    },
    # GPT-6 Luna is on direct-llm too: the hosted relay prices per model and
    # rejects unknown ids, so its relay row (otodock-commercial
    # relay_vendors.py) must be served BEFORE an OSS release lists a new
    # direct-llm builtin — a direct-llm row here without one fails every
    # hosted install's titles and Luna chats.
    "gpt-6-luna": {
        "label": "GPT-6 Luna",
        "provider": "openai",
        "context_window": 272_000,   # the pricing-tier line — see the window note above
        "pricing": (0.10, 0.50, 0.125, 0.01),  # per 1M: (input, output, cache_write, cache_read)
        "layers": ["codex-cli", "direct-llm"],
        "server_tools": True,   # OpenAI's built-in web_search (Responses API, direct-llm)
        "supports_reasoning": True,
        "supports_xhigh": True,
        # No supports_ultra: OpenAI's own model manifest caps Luna (the fast/
        # cheap tier) at "max" — ultra is a Sol/Terra/Astra capability.
        "tier": 4,
        "good_at": "high-volume routine work",
    },
    # (Older GPT-5.x builtins — gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex
    # — were retired with the 5.6 family, and 5.6 Sol / 5.6 Luna with the
    # GPT-6 pair. Agents pinned to a retired id with a successor
    # move at boot; the others keep running; admins can re-add any of them
    # as custom models via discover-models.)

    # --- Groq models (direct-llm; OpenAI-compatible API) ---
    # gpt-oss-120b is GroqCloud production; qwen3.6-27b is Groq "preview" (kept
    # anyway: it's Groq's recommended fast general-purpose tier). supports_reasoning
    # per model (gpt-oss: yes; qwen3.6 is a hybrid thinker the GroqAdapter pins to
    # non-thinking via reasoning_effort="none" — see openai_compat_adapter.py).
    # No supports_xhigh (Groq has no xhigh tier). Admins can add more Groq /
    # Ollama / etc. models dynamically via the admin discover-models UI.
    # Cache pricing: Groq's automatic prompt caching bills cached input at a
    # 50% discount (writes free) and reports it OpenAI-style via
    # prompt_tokens_details.cached_tokens — cache_read = input/2 keeps the
    # cost rows exact whenever Groq reports a hit (models it doesn't cache
    # simply never report one).
    "qwen/qwen3.6-27b": {
        "label": "Qwen3.6 27B",
        "provider": "groq",
        "context_window": 131_072,
        "pricing": (0.60, 3.00, 0.0, 0.30),
        "layers": ["direct-llm"],
        "tier": 4,
        "good_at": "general-purpose answers served on Groq",
    },
    "openai/gpt-oss-120b": {
        "label": "GPT-OSS 120B",
        "provider": "groq",
        "context_window": 131_072,
        "pricing": (0.15, 0.60, 0.0, 0.075),
        "layers": ["direct-llm"],
        "supports_reasoning": True,
        "tier": 4,
        "good_at": "open-weight answers served on Groq",
    },
}

# Capability tiers, most capable first. A tier is a judgement about which
# offered model an agent should pick for a job (a curated table shipped with
# the registry, never fetched: installs on private networks must give the
# same answer as everyone else). Builtins carry theirs on the entry above;
# admin-added models are untiered until an admin tags them.
MODEL_TIER_LABELS: dict[int, str] = {
    1: "frontier",
    2: "strong",
    3: "balanced",
    4: "fast",
}
MODEL_GOOD_AT_MAX_CHARS = 120


def get_model_tier(model: str) -> tuple[int | None, str, str]:
    """``(tier, tier_label, good_at)`` for a model id: registry first, then
    the first tiered ``execution_layer_models`` row for the id (a custom
    model tagged by an admin), else ``(None, "", "")`` = untiered."""
    entry = MODEL_REGISTRY.get(model)
    if entry:
        tier = entry.get("tier")
        return tier, MODEL_TIER_LABELS.get(tier, ""), entry.get("good_at", "")
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model and m.get("tier"):
                tier = int(m["tier"])
                return tier, MODEL_TIER_LABELS.get(tier, ""), m.get("good_at") or ""
    return None, "", ""


def model_catalog_sort_key(m: dict) -> tuple:
    """Sort key for model rows wherever a list is shown to an agent or a
    human: tier first (untiered last), builtins before customs, then
    registry order, then creation time."""
    tier = m.get("tier")
    return (
        int(tier) if tier else 99,
        not m.get("is_builtin", True),
        _MODEL_REGISTRY_ORDER.get(m.get("model_id") or m.get("value") or "", 999),
        str(m.get("created_at") or ""),
    )


def model_supports_server_tools(model: str) -> bool:
    """Whether the Direct LLM engine offers the provider's own server-run tools
    on this model: Anthropic ``web_search`` / ``web_fetch``
    (``AnthropicAdapter.SERVER_TOOLS``), OpenAI's built-in ``web_search`` on the
    Responses API. Registry builtins only — an admin-added model stays False
    (a custom OpenAI id may be a gateway that rejects the tool)."""
    entry = MODEL_REGISTRY.get(model)
    return entry.get("server_tools", False) if entry else False


def get_model_supports_xhigh(model: str) -> bool:
    """Return True if the model accepts the platform's "xhigh" effort level.

    xhigh was introduced by Anthropic with Opus 4.7 as a new level between
    "high" and "max" (not an alias for "max"). OpenAI's gpt-5 family has had
    xhigh as the top of its reasoning-effort scale for a while. Pre-4.7
    Anthropic models reject xhigh at the API, so each execution layer's
    startup path consults this helper and silently falls back to "max" on
    models that don't support it — see the failsafe in core/layers/cli/session.py,
    core/layers/providers/anthropic_adapter.py, etc.

    Resolution order: MODEL_REGISTRY (builtins) → DB execution_layer_models
    (admin-edited custom models) → False (conservative — unknown models
    fall back to "max" rather than risking an API rejection).
    """
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return bool(entry.get("supports_xhigh", False))
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model:
                return bool(m.get("supports_xhigh", 0))
    return False


def get_model_supports_ultra(model: str) -> bool:
    """Return True if the model accepts the platform's "ultra" effort level.

    Ultra is Codex-only (GPT-6 Sol, gpt-5.6 Terra, GPT-6 Astra): max reasoning plus
    Codex-native proactive multi-agent orchestration. Registry-only on purpose — custom
    admin-added models have no supports_ultra column (the wire clamp in
    map_effort_to_codex is prefix-based, so a custom "gpt-6-sol-*" id still
    maps correctly; the dashboard just won't offer the option). Unknown
    models → False: a stored "ultra" then clamps to the model's ceiling in
    every execution layer rather than risking an API rejection.
    """
    entry = MODEL_REGISTRY.get(model)
    return bool(entry.get("supports_ultra", False)) if entry else False


def get_model_provider(model: str, layer: str = "") -> str:
    """Resolve the provider for a model ID.

    Resolution order: MODEL_REGISTRY → DB execution_layer_models → prefix
    heuristics. ``layer`` scopes the DB lookup to that layer's rows (a model
    id discovered on codex-cli lives in codex-cli rows); empty = any layer,
    first match (usage attribution and titles only know the model id).
    """
    # 1. Check in-memory registry (Anthropic builtins)
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return entry.get("provider", "anthropic")
    # 2. Check DB (dynamically discovered/added models have provider set)
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        db_models = subscription_store.list_models(layer=layer or None)
        for m in db_models:
            if m.get("model_id") == model and m.get("provider"):
                return m["provider"]
    # 3. Prefix heuristics for unknown models
    if model.startswith(("gpt-", "o1-", "o3", "o4-", "chatgpt-", "openai/")):
        return "openai"
    if model.startswith(("llama", "gemma", "mixtral", "deepseek", "qwen")):
        return "groq"
    if model.startswith("claude"):
        return "anthropic"
    return "anthropic"


def get_model_layers(model: str) -> list[str]:
    """Resolve which execution layers a model belongs to.

    Resolution: MODEL_REGISTRY (builtins) → DB execution_layer_models (custom).
    Empty list = unknown model. Used to keep an agent's PRIMARY execution layer
    consistent with its default model — a model only runs on its own layer
    (e.g. ``gpt-6-sol`` is ``codex-cli`` only), so the no-picker default (primary
    layer + default model) must agree or tasks hard-reject the model.
    """
    if not model:
        return []
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return list(entry.get("layers", []))
    try:
        from storage.billing import subscription_store
        return [
            m["layer"] for m in subscription_store.list_models()
            if m.get("model_id") == model and m.get("layer")
        ]
    except Exception:
        return []


# Per-provider default pricing (input, output, cache_write, cache_read) per 1M tokens.
# Used when a model isn't in MODEL_REGISTRY (e.g., dynamically discovered models).
# A future per-model pricing table in the DB can override these defaults.
PROVIDER_DEFAULT_PRICING: dict[str, tuple[float, float, float, float]] = {
    "anthropic": (2.0, 10.0, 2.50, 0.20),   # Sonnet-level (tracks Sonnet 5's standard $2/$10, 2026-08)
    "openai":    (2.0, 8.0, 0, 1.0),          # GPT-4.1 level
    "groq":      (0.20, 0.20, 0, 0),          # very cheap hosted inference
    "ollama":            (0, 0, 0, 0),         # local, free
    "openai_compatible": (0, 0, 0, 0),         # self-hosted endpoint, pricing depends on backend
}
MODEL_DEFAULT_PRICING = PROVIDER_DEFAULT_PRICING["anthropic"]
MODEL_DEFAULT_CONTEXT_WINDOW = 200_000

# Retired builtin → successor. Walked at boot (startup.py →
# subscription_store.remap_retired_model per entry, each retired id remapped
# to the END of its chain via successor_model, so the dict's order never
# strands a pin on an intermediate retired id) so every persisted PIN of
# a retired id — agents.default_model, chats.model, dynamic_tasks.override_model
# — moves to the successor. Why pins need this at all: sync_builtin_models
# retires the old row, the retired id then fails the model-allowed check, and a
# pinned agent's NEW chats would silently fall back to the layer's first
# enabled model (not necessarily the successor). Agents on "Auto" never need
# it — they follow MODEL_REGISTRY order. Only list lossless swaps (same or
# lower price, same capabilities); a retirement WITHOUT a successor just
# leaves the pins to the fallback. One line per retirement, no startup code.
# The walk skips a retired id an admin has re-added as a custom row (the
# admin chose to keep it; its pins stay). Pins the walk never sees — a
# check's judge.model, the title-model setting — resolve through
# successor_model() at read time.
MODEL_SUCCESSORS: dict[str, str] = {
    "claude-opus-4-8[1m]": "claude-opus-5",   # 2026-07-24 — identical price tuple
    "claude-fable-5": "claude-fable-5-1",     # 2026-09-01 — same base price, cheaper cache reads
    "claude-opus-5": "claude-opus-5-5",       # 2026-09-24 — cheaper on every element of the tuple
    "gpt-5.6-sol": "gpt-6-sol",               # 2026-09-24 — cheaper on every element, keeps ultra
    "gpt-5.6-luna": "gpt-6-luna",             # 2026-09-24 — cheaper on every element, max-only as before
}


def successor_model(model: str) -> str:
    """The id a retired builtin pin resolves to today: ``MODEL_SUCCESSORS``
    followed to the end of its chain (``claude-opus-4-8[1m]`` →
    ``claude-opus-5`` → ``claude-opus-5-5``), the id itself when no entry
    applies (cycle-safe). The boot walk remaps every retired id to this end;
    the pins the walk never rewrites (a check's ``judge.model``, the
    title-model setting) read through it instead."""
    cur = model or ""
    seen: set[str] = set()
    while cur in MODEL_SUCCESSORS and cur not in seen:
        seen.add(cur)
        cur = MODEL_SUCCESSORS[cur]
    return cur


def get_model_pricing(model: str, provider: str = "") -> tuple[float, float, float, float]:
    """Get (input, output, cache_write, cache_read) pricing per 1M tokens.

    Resolution order: DB (custom pricing) → MODEL_REGISTRY → provider default.
    If provider is given, use it directly for fallback instead of heuristic.
    """
    # 1. Check DB for custom pricing (dynamically added models).
    # EVERY layer's rows, not just direct-llm: an admin who priced a custom
    # model on the Codex or Claude card had that price silently ignored and
    # got the registry's (or the provider default's) numbers in every usage
    # record. The row is keyed by (layer, model_id), so the first row for
    # this model id is the admin's answer whichever card they set it on.
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model and m.get("pricing_input", 0) > 0:
                return (
                    m["pricing_input"], m["pricing_output"],
                    m.get("pricing_cache_write", 0), m.get("pricing_cache_read", 0),
                )
    # 2. Check in-memory registry (Anthropic builtins)
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return entry["pricing"]
    # 3. Fall back to provider-level default
    resolved = provider or get_model_provider(model)
    return PROVIDER_DEFAULT_PRICING.get(resolved, MODEL_DEFAULT_PRICING)


def get_model_context_window(model: str) -> int:
    """Get the context window size for a model.

    Resolution order: DB → MODEL_REGISTRY → default.
    """
    # Check DB for a custom context window — every layer's rows, for the same
    # reason as get_model_pricing above (an admin's window set on the Codex or
    # Claude card was ignored, so the gauge and the direct-llm truncation
    # threshold used the registry's number instead).
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model and m.get("context_window", 0) > 0:
                return m["context_window"]
    entry = MODEL_REGISTRY.get(model)
    return entry["context_window"] if entry else MODEL_DEFAULT_CONTEXT_WINDOW


def model_supports_reasoning(model: str) -> bool:
    """Check if a model supports reasoning effort parameters.

    Resolution: MODEL_REGISTRY (builtin) → DB flag (dynamic/admin) → False.
    Admin can toggle this per model in the execution layers page.
    """
    # Builtin models: MODEL_REGISTRY is authoritative
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return entry.get("supports_reasoning", False)
    # Dynamic models: check DB (admin-configured via execution layers page)
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model:
                return bool(m.get("supports_reasoning", 0))
    return False


def get_layer_models(layer: str, *, offers_ultra: bool = False) -> list[dict]:
    """Get the builtin model list for an execution layer (for LayerCapabilities).

    Includes pricing and context_window so sync_builtin_models can populate DB.
    ``offers_ultra``: the ENGINE can run the "ultra" effort at all (Codex's
    proactive multi-agent orchestration); the per-model registry flag is then
    ANDed with it, so Terra — also a direct-llm model — shows ultra on the
    codex list only, and Luna never does.
    """
    models = [{"value": "", "label": "System Default"}]
    for model_id, info in MODEL_REGISTRY.items():
        if layer in info.get("layers", []):
            pricing = info.get("pricing", MODEL_DEFAULT_PRICING)
            models.append({
                "value": model_id,
                "label": info["label"],
                "provider": info.get("provider", "anthropic"),
                "context_window": info.get("context_window", 0),
                "pricing_input": pricing[0],
                "pricing_output": pricing[1],
                "pricing_cache_write": pricing[2],
                "pricing_cache_read": pricing[3],
                "supports_reasoning": info.get("supports_reasoning", False),
                "supports_xhigh": info.get("supports_xhigh", False),
                "supports_ultra": bool(info.get("supports_ultra", False)) and offers_ultra,
                "tier": info.get("tier"),
                "tier_label": MODEL_TIER_LABELS.get(info.get("tier"), ""),
                "good_at": info.get("good_at", ""),
            })
    return models


# Precomputed once at module load: model_id → index in MODEL_REGISTRY order.
# Used to sort the DB fallback so builtins surface in the order they appear
# in MODEL_REGISTRY.keys() (e.g. Fable 5 before Opus 5 before Sonnet 5). Python
# 3.7+ guarantees dict insertion order, so this is stable.
_MODEL_REGISTRY_ORDER: dict[str, int] = {
    model_id: i for i, model_id in enumerate(MODEL_REGISTRY.keys())
}


def _pool_providers(layer: str) -> set[str]:
    """Providers with an active platform-pool subscription on ``layer`` (the
    relay rows count; personal-only accounts do not — they serve one user's
    chats, and the System Default is an agent-level choice)."""
    try:
        from storage.billing import subscription_store
        return {
            (s.get("provider") or "")
            for s in subscription_store.list_platform_pool(layer)
        } - {""}
    except Exception:
        return set()


def resolve_agent_model(agent_name: str, layer: str | None = None) -> str:
    """Resolve the effective model for an agent session.

    Precedence (engine-contract lane, phase 2 — the declared default is the
    new middle term; the operator's rule is "the tier is the rule": each
    engine DECLARES its default as the best tier it serves with tier 1
    offered but never defaulted):
      1. agents.default_model (the per-agent PIN). Two-mode, byte for byte as
         before: with ``layer=None`` it is returned with no check at all; with
         ``layer`` given it is returned when a row for it exists on that
         layer — enabled or not. A pin is an explicit human choice; the
         admin's ``enabled`` toggle moves the DEFAULT below, never a pin.
      2. The layer's declared ``ModelPolicy.default_model``, when its row on
         the layer exists, is ENABLED, and the platform pool can serve its
         provider (no pool at all = no filter).
      3. Else the enabled (+ pool-served) rows, sorted so the fallback falls
         DOWN the tiers from the declared default and never up: tiers at or
         below the default's tier first (tier order, builtins first, then
         registry order, then created_at); tiers ABOVE it only when nothing
         else is enabled — so disabling Opus 5 lands on Sonnet 5, not Fable.
      4. Raise RuntimeError — no silent Anthropic fallback, no env-var
         default. The caller decides how to surface this to the user.

    ``layer`` — the execution path the session will ACTUALLY run on. Task /
    delegate runs can override the agent's engine (override_execution_path),
    and the agent default model is engine-specific: stamping a Claude model
    onto a codex turn makes the provider 400 every turn ("The
    'claude-sonnet-5' model is not supported when using Codex with a ChatGPT
    account" — the 2026-08 "empty task turn" bug). When ``layer`` is given
    and agents.default_model is not served by it, the default is SKIPPED and
    the layer's own first enabled model is used instead. ``layer=None``
    keeps the historical behavior (the agent's own execution_path).

    This is the single entry point for model resolution across CLI, Direct
    LLM, Codex, tasks, meetings, phone — replaces the old get_cli_model /
    get_agent_model split which both baked in Anthropic-specific defaults.

    Raises:
        RuntimeError: if no usable default AND no enabled model exists for
            the effective layer. Happens on fresh installs before the admin
            enables any models, or after an admin disables every builtin for
            a layer without adding a custom one. Message includes the agent
            name and execution path so admins know where to look.
    """
    from storage.agents import agent_store
    from storage.billing import subscription_store
    # Function-local: config is imported by nearly everything, so it must not
    # take a module-level dependency on the execution-layer package.
    from core.execution_layer import DEFAULT_EXECUTION_PATH

    agent = agent_store.get_agent(agent_name)
    path = (layer
            or (agent.get("execution_path") if agent else None)
            or DEFAULT_EXECUTION_PATH)
    default_model = (agent or {}).get("default_model") or ""
    if default_model:
        if not layer:
            return default_model
        # Layer override: honor the default only when that layer serves it
        # (mirror of ws/dashboard._model_allowed_for_path — fail OPEN on a
        # lookup error: this guards cross-layer poison, it is not the model
        # registry's gatekeeper).
        try:
            served = any(
                (m.get("model_id") or "") == default_model
                for m in subscription_store.list_models(layer=path)
            )
        except Exception:
            served = True
        if served:
            return default_model
    return resolve_layer_default_model(path, agent_name=agent_name)


def resolve_layer_default_model(layer: str, *, agent_name: str = "") -> str:
    """The model an UNPINNED agent on ``layer`` runs right now — steps 2-4 of
    :func:`resolve_agent_model`, which calls this after the pin check. The
    catalog (``GET /v1/execution-layers``) serves it per engine as
    ``auto_model`` so the dashboard's "Auto" option names the model that
    will actually run instead of re-deriving it (and getting it wrong).

    ``agent_name`` only decorates the error: the rule itself is per engine
    and per install (enablement + the platform pool), never per agent.

    Raises:
        RuntimeError: when no model is enabled on ``layer``.
    """
    from storage.billing import subscription_store

    db_models = subscription_store.list_models(layer=layer)
    enabled = [m for m in db_models if m.get("enabled")]
    if not enabled:
        who = f"for agent '{agent_name}' " if agent_name else ""
        raise RuntimeError(
            f"No enabled model available {who}"
            f"(execution_path='{layer}'). Configure one at Admin > "
            f"Execution Layers."
        )

    # Prefer a provider the platform pool can actually serve: a local-only
    # install would otherwise land on a vendor builtin and fail with "no
    # credentials". A pool with no providers (personal accounts only) applies
    # no filter — and neither does a lookup error (_pool_providers returns an
    # empty set for both).
    configured = _pool_providers(layer)
    if configured:
        served = [m for m in enabled if (m.get("provider") or "") in configured]
        if served:
            enabled = served

    # 2. The engine's DECLARED default, if it survived both filters above.
    declared = _declared_default_model(layer)
    if declared and any(m.get("model_id") == declared for m in enabled):
        return declared

    # 3. Fall DOWN the tiers from the declared default, never up: a row whose
    # tier is better (numerically lower) than the default's sorts LAST, so it
    # is reached only when nothing at or below the default's tier is enabled
    # and servable. Within a band: tier order, builtins first, registry
    # order, created_at. Untiered custom rows count as the worst tier.
    default_tier = get_model_tier(declared)[0] if declared else None

    def _sort_key(m: dict) -> tuple:
        tier = m.get("tier")
        tier_n = int(tier) if tier else 99
        above_default = default_tier is not None and tier_n < default_tier
        return (
            above_default,
            tier_n,
            not m.get("is_builtin"),
            _MODEL_REGISTRY_ORDER.get(m.get("model_id", ""), 999),
            m.get("created_at", ""),
        )

    enabled.sort(key=_sort_key)
    return enabled[0]["model_id"]


def _skills_delivery(layer: str) -> str:
    """The engine's ``behaviour.skills_delivery`` ("materialized_dir" for an
    unregistered or empty id — nothing is appended to the prompt)."""
    try:
        from core.session.session_manager import get_layer_capabilities
        caps = get_layer_capabilities(layer or "")
    except Exception:
        return "materialized_dir"
    return caps.behaviour.skills_delivery if caps else "materialized_dir"


def _declared_default_model(layer: str) -> str:
    """The ``ModelPolicy.default_model`` the layer declares, or "" when the
    layer is unknown or the registry cannot be reached (a test that stubs
    the store must not need the layer packages)."""
    try:
        from core.session.session_manager import get_layer_capabilities
        caps = get_layer_capabilities(layer)
    except Exception:
        return ""
    return caps.model_policy.default_model if caps else ""


# Thin aliases kept for semantic clarity at call sites — CLI vs Direct path
# used to differ, but after dropping the Anthropic-specific env defaults
# they do exactly the same thing. Delegating to one implementation means
# the ~11 existing call sites don't need updates.
def get_cli_model(agent_name: str, layer: str | None = None) -> str:
    """Get the CLI model for an agent (dashboard sessions). ``layer`` = the
    execution path the session will actually run on, when it can differ
    from the agent's own (task/delegate overrides)."""
    return resolve_agent_model(agent_name, layer=layer)


def get_agent_model(agent_name: str) -> str:
    """Get the model for an agent (direct API / phone path)."""
    return resolve_agent_model(agent_name)


def get_cli_effort(agent_name: str) -> str:
    """Get the effort level for an agent (CLI sessions)."""
    from storage.agents import agent_store
    agent = agent_store.get_agent(agent_name)
    return (agent["default_effort"] if agent and agent["default_effort"] else DEFAULT_EFFORT_LEVEL)

# What one provider-run web search costs on top of the tokens (USD per search):
# Anthropic's web_search tool and OpenAI's built-in web_search both list $10 per
# 1k calls. Added to the per-turn cost by ProviderAdapter.calculate_cost from
# ProviderUsage.web_search_requests; the hosted relay bills the same fee ×1.25.
WEB_SEARCH_USD_PER_REQUEST = 0.01

# Direct session idle timeout (seconds) before reaping
DIRECT_SESSION_TIMEOUT = int(_cfg("DIRECT_SESSION_TIMEOUT", "900"))

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
DB_POOL_MAX_SIZE = int(_cfg("DB_POOL_MAX_SIZE", "30"))
DB_POOL_TIMEOUT_S = float(_cfg("DB_POOL_TIMEOUT_S", "10"))
DB_FAST_LANE_WORKERS = max(1, int(_cfg("DB_FAST_LANE_WORKERS", "6")))
DB_BULK_LANE_WORKERS = max(1, int(_cfg("DB_BULK_LANE_WORKERS", "10")))
# The event loop's own pool: how long it waits for a connection before the
# breaker opens, how long the breaker stays open, and after how long one
# operation triggers a liveness probe of Postgres (0 disables the probe).
DB_LOOP_POOL_TIMEOUT_S = float(_cfg("DB_LOOP_POOL_TIMEOUT_S", "0.5"))
DB_LOOP_BREAKER_S = float(_cfg("DB_LOOP_BREAKER_S", "5"))
DB_LOOP_LIVENESS_PROBE_S = float(_cfg("DB_LOOP_LIVENESS_PROBE_S", "1.0"))
# Server-side statement_timeout and idle_in_transaction_session_timeout on
# every pool connection; 0 disables each (PgBouncer in transaction mode).
DB_STATEMENT_TIMEOUT_S = float(_cfg("DB_STATEMENT_TIMEOUT_S", "300"))
DB_IDLE_IN_TX_TIMEOUT_S = float(_cfg("DB_IDLE_IN_TX_TIMEOUT_S", "60"))
# The asyncio default executor (asyncio.to_thread). More threads worsen GIL
# contention with the loop (measured); 32 covers the to_thread sites.
DEFAULT_EXECUTOR_WORKERS = max(1, int(_cfg("DEFAULT_EXECUTOR_WORKERS", "32")))
# uvicorn limit_concurrency: past this many open connections (WebSockets
# included) a new HTTP request gets 503. 0 disables.
HTTP_LIMIT_CONCURRENCY = int(_cfg("HTTP_LIMIT_CONCURRENCY", "4000"))

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
SESSION_EST_HEAVY_MB = int(_cfg("SESSION_EST_HEAVY_MB", "850"))    # CLI/Codex/interactive/phone-CLI/task reserve
SESSION_EST_LIGHT_MB = int(_cfg("SESSION_EST_LIGHT_MB", "250"))    # floor of the Direct-LLM estimate (no CLI process)
# Gate-1 budget = total RAM × this. Generous on purpose — Gate 1 is a coarse cap +
# burst bound, Gate 2 is the real safety, so this never needs precise tuning.
BUDGET_FRACTION = float(_cfg("BUDGET_FRACTION", "0.90"))
# Gate-2 headroom kept free for the OS/kernel/page-cache. Effective floor is
# max(this, total_mb × 0.03) so large hosts keep proportional headroom.
SESSION_RESERVE_FLOOR_MB = int(_cfg("SESSION_RESERVE_FLOOR_MB", "256"))
# Gate-2 SWAP CREDIT: MemAvailable counts zero swap, so a small box with swap
# configured denies sessions it could comfortably run by paging out cold heap
# (idle CLI sessions spend most of their life blocked on the API — their
# coldest pages swap gracefully). The live reading gains min(SwapFree / 2,
# this cap): half of free swap so the credit shrinks as swap fills, capped so
# admission never leans DEEP into swap. 0 disables. Boxes without swap are
# unaffected (SwapFree = 0).
SESSION_SWAP_CREDIT_MB = int(_cfg("SESSION_SWAP_CREDIT_MB", "512"))
# Under Gate-1 pressure, an idle LOCAL session this old (seconds) may be gracefully
# evicted to admit an active one (clamped below the idle-reap timeout). Streaming
# sessions have ~0 idle age and are never eligible.
SESSION_EVICT_FLOOR_S = int(_cfg("SESSION_EVICT_FLOOR_S", "300"))
# Optional HARD cap on concurrent LOCAL sessions — an env-only escape hatch (no admin
# UI). 0 = no cap (pure budget, the default). For operators on noisy shared boxes who
# want a deterministic ceiling regardless of the live-RAM reading.
OTODOCK_MAX_LOCAL_SESSIONS = int(_cfg("OTODOCK_MAX_LOCAL_SESSIONS", "0") or "0")
# Sessions one person may hold at once (0 = no cap), and the session spawns
# allowed at once (0 = half the CPUs this process may use, at least 2).
MAX_SESSIONS_PER_USER = int(_cfg("OTODOCK_MAX_SESSIONS_PER_USER", "8") or "0")
SESSION_SPAWN_CONCURRENCY = int(_cfg("SESSION_SPAWN_CONCURRENCY", "0") or "0") or max(
    2, len(os.sched_getaffinity(0)) // 2 if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 4) // 2)
# Admission: how long a session start waits in the queue for a slot, and how
# long a parked delegate result or continuation wake waits for one before it
# is stored for the chat's next turn.
ADMISSION_QUEUE_WAIT_S = float(_cfg("ADMISSION_QUEUE_WAIT_S", "30"))
ADMISSION_WAKE_WAIT_S = float(_cfg("ADMISSION_WAKE_WAIT_S", "600"))
# The session index is written behind: dirty entries flush at most this often.
SESSION_INDEX_FLUSH_S = float(_cfg("SESSION_INDEX_FLUSH_S", "2.0"))

SCHEDULER_TIMEZONE = _cfg("SCHEDULER_TIMEZONE", "UTC")
SCHEDULER_MODE = _cfg("SCHEDULER_MODE", "embedded")  # embedded | standalone
PROXY_INTERNAL_URL = _cfg("PROXY_INTERNAL_URL", f"http://localhost:{PORT}")
SCHEDULER_SYNC_INTERVAL = int(_cfg("SCHEDULER_SYNC_INTERVAL", "30"))
# Minimum seconds between scheduled-task SESSION SPAWNS. Tasks that share a
# fire time (several 06:30 digests) otherwise start their CLI + sandbox +
# MCP handshakes in the same second — a burst that spikes RAM/CPU on small
# hosts. The fire times themselves stay exact; only the session starts are
# spaced. 0 disables. Manual Run-Now and event triggers are never spaced.
TASK_SPAWN_SPACING_SECONDS = int(_cfg("TASK_SPAWN_SPACING_SECONDS", "10"))
# Active clocked tasks (recurring, and one-time not yet fired) one person may
# create and one agent may hold (429 past them; admins are exempt), and the
# pending self-continuations per chat and per agent. 0 = no cap.
TASK_MAX_ACTIVE_PER_USER = int(_cfg("TASK_MAX_ACTIVE_PER_USER", "25"))
TASK_MAX_ACTIVE_PER_AGENT = int(_cfg("TASK_MAX_ACTIVE_PER_AGENT", "100"))
CONTINUATION_MAX_ACTIVE_PER_CHAT = int(_cfg("CONTINUATION_MAX_ACTIVE_PER_CHAT", "10"))
CONTINUATION_MAX_ACTIVE_PER_AGENT = int(_cfg("CONTINUATION_MAX_ACTIVE_PER_AGENT", "100"))


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
    except Exception:
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
    except Exception:
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
DASHBOARD_ENABLED = _cfg("DASHBOARD_ENABLED", "true").lower() == "true"

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
JWT_EXPIRY_HOURS = int(_cfg("JWT_EXPIRY_HOURS", "168"))
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


_cookie_secure_env = _cfg("COOKIE_SECURE", "").strip().lower()
if _cookie_secure_env in ("1", "true", "yes", "on"):
    COOKIE_SECURE = True
elif _cookie_secure_env in ("0", "false", "no", "off"):
    COOKIE_SECURE = False
else:
    COOKIE_SECURE = _default_cookie_secure()

# --- OtoDock relay + deployment ---
# Deployment axis: false = self-hosted (the fair-source product), true =
# OtoDock-operated cloud SaaS. Drives the deployment-aware licensing
# enforcement model and hosted-OAuth security.
OTODOCK_CLOUD = _cfg("OTODOCK_CLOUD", "").lower() == "true"

# Local-PBX (Asterisk / FreePBX) telephony adapters are AudioSocket-based — they
# only work where the PBX can reach the phone daemon on the LAN. Enabled by default
# on self-host (the 99% case); OFF on OtoDock cloud (a tenant's LAN PBX can't reach
# a multi-tenant pool — cloud telephony goes through Twilio/3CX instead). An explicit
# OTODOCK_LOCAL_PBX_ENABLED wins either way (a Twilio-only self-host sets it false;
# an advanced cloud op sets it true).
_local_pbx_raw = _cfg("OTODOCK_LOCAL_PBX_ENABLED", "").strip().lower()
if _local_pbx_raw in ("1", "true", "yes", "on"):
    LOCAL_PBX_ENABLED = True
elif _local_pbx_raw in ("0", "false", "no", "off"):
    LOCAL_PBX_ENABLED = False
else:
    LOCAL_PBX_ENABLED = not OTODOCK_CLOUD

def _warn_untrusted_edge(in_docker: bool, public_url: str, trusted: list[str]) -> bool:
    """In a container the edge never connects from 127.0.0.1, so an https
    public URL with no TRUSTED_PROXY means every visitor shares the edge's
    address. Said at boot, never refused: fronted installs must still start."""
    if not (in_docker and public_url.strip().lower().startswith("https://") and not trusted):
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
DOCKER_SOCKET_PROXY_PORT = int(_cfg("DOCKER_SOCKET_PROXY_PORT", "2375"))
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
OTODOCK_MCP_BUILD_LOCAL = _cfg("OTODOCK_MCP_BUILD_LOCAL", "").strip().lower() in (
    "1", "true", "yes", "on",
)
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
OTODOCK_AIR_GAPPED = _cfg("OTODOCK_AIR_GAPPED", "").lower() == "true"
# Relay base URL. Defaults to the live OtoDock relay so a connected install
# works out of the box (native push, hosted OAuth, license activation/liveness)
# with zero config. Outbound is still fully gated: OTODOCK_AIR_GAPPED=true makes
# this install reach OtoDock NOT AT ALL, and a signed `offline_term` license
# disables the relay regardless of any flag (see relay_offered()). Override for
# staging / a private relay. Kept server-side — never exposed to clients (only
# the derived booleans are).
OTODOCK_RELAY_BASE = _cfg("OTODOCK_RELAY_BASE", "https://api.otodock.io")

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
AUTH_PROVIDER_BYPASS = _cfg("AUTH_PROVIDER_BYPASS", "").lower() == "true"

# --- OIDC provider ---
OIDC_ENABLED = _cfg("OIDC_ENABLED", "").lower() == "true"
OIDC_PROVIDER_NAME = _cfg("OIDC_PROVIDER_NAME", "SSO")
OIDC_DISCOVERY_URL = _cfg("OIDC_DISCOVERY_URL", "")
OIDC_CLIENT_ID = _cfg("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = _cfg("OIDC_CLIENT_SECRET", "")
OIDC_AUTHORIZE_URL = _cfg("OIDC_AUTHORIZE_URL", "")
OIDC_TOKEN_URL = _cfg("OIDC_TOKEN_URL", "")
OIDC_USERINFO_URL = _cfg("OIDC_USERINFO_URL", "")
OIDC_LOGOUT_URL = _cfg("OIDC_LOGOUT_URL", "")
OIDC_SCOPES = _cfg("OIDC_SCOPES", "openid profile email groups")
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
OIDC_CONFIRM_FRESH_LOGIN = _cfg("OIDC_CONFIRM_FRESH_LOGIN", "").lower() == "true"
# With the fresh login on, an ID token without ``auth_time`` passes unless
# this is true (some providers omit the claim).
OIDC_CONFIRM_REQUIRE_AUTH_TIME = _cfg("OIDC_CONFIRM_REQUIRE_AUTH_TIME", "").lower() == "true"

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
    OIDC_AUTHORIZE_URL = OIDC_AUTHORIZE_URL or meta.get("authorization_endpoint", "")
    OIDC_TOKEN_URL = OIDC_TOKEN_URL or meta.get("token_endpoint", "")
    OIDC_USERINFO_URL = OIDC_USERINFO_URL or meta.get("userinfo_endpoint", "")
    OIDC_LOGOUT_URL = OIDC_LOGOUT_URL or meta.get("end_session_endpoint", "")


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
    except Exception as _e:
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
    except Exception:
        return "127.0.0.1"

AUDIOSOCKET_PUBLIC_HOST = _cfg("OTO_AUDIOSOCKET_PUBLIC_HOST", _default_audiosocket_public_host())
# True when the operator never set the key and the fallback autodetect ran —
# the phone adapters use this to warn when that autodetect happened inside a
# container (the detected IP is the container's own, unreachable by the PBX).
AUDIOSOCKET_PUBLIC_HOST_AUTODETECTED = not _cfg("OTO_AUDIOSOCKET_PUBLIC_HOST")

# Hosts allowed to embed the Collabora iframe (CSP frame-ancestors). Without
# this, Collabora defaults to allowing only the WOPI host, which blocks the
# dashboard origin. Collabora sends the value as an explicit CSP source list,
# which REPLACES the browser's same-origin default — and a CSP host-source
# without a port matches only the scheme's default port, so the PORT must be
# preserved (the standard install serves on :8400; hostname-only blocked every
# sub-path preview there with "refused to connect"). Also feeds Collabora's
# `server_name` (self-URL building + WS origin check), which equally needs the
# non-default port. Default: host[:port] of DASHBOARD_PUBLIC_URL.
def _default_frame_ancestors() -> str:
    if not DASHBOARD_PUBLIC_URL:
        return ""
    try:
        from urllib.parse import urlparse
        u = urlparse(DASHBOARD_PUBLIC_URL)
        host = u.hostname or ""
        return f"{host}:{u.port}" if host and u.port else host
    except Exception:
        return ""

COLLABORA_FRAME_ANCESTORS = _cfg("COLLABORA_FRAME_ANCESTORS", _default_frame_ancestors())

# Whether TLS terminates in front of Collabora (drives the scheme of its
# origin check and generated self-URLs). Plain-http installs (LAN T1 on
# :8400) need false or the WS origin check expects https and rejects the
# editor socket. Derived from the DASHBOARD_PUBLIC_URL scheme, same rule the
# T2 compose entrypoint applies.
COLLABORA_SSL_TERMINATION = _cfg(
    "COLLABORA_SSL_TERMINATION",
    "true" if (DASHBOARD_PUBLIC_URL or "").strip().lower().startswith("https://") else "false",
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
    except Exception:
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
