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
import sys
import zoneinfo
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

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


_reject_directory_config(_config_env)
_file_cfg = dotenv_values(_config_env) if _config_env.exists() else {}


def _cfg(key: str, default: str = "") -> str:
    """Read config: os.environ (Docker/systemd override) > config.env file > default."""
    return os.environ.get(key) or _file_cfg.get(key) or default


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
            _config_env.write_text("\n".join(lines) + "\n")
        else:
            with open(_config_env, "a") as f:
                f.write(f"{key}={value}\n")
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
# Files larger than this are NOT backed up to the recover-bin (the delete /
# overwrite still proceeds) — a safety net shouldn't copy huge build artifacts.
RECOVER_BIN_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
# Per-agent aggregate ceiling on live recover-bin captures. The recover-bin sits
# OUTSIDE the agent quota tree (sibling of agents/), so without this an overwrite
# loop could grow it unbounded and fill the backing filesystem even while the
# agent itself is over quota. On overflow, capture() evicts that agent's oldest
# entries first (atop the 7-day TTL). 0 = unlimited.
RECOVER_BIN_AGENT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
# Delete tombstones (storage/file_tombstones_store.py) are kept this long so an
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

# Global BACKSTOP cap on request body size (bytes), enforced by an ASGI
# middleware via Content-Length. Set ABOVE the largest legitimate upload (media
# uploads self-limit to 250 MB in api/media/uploads.py) so it only rejects abusive
# oversized bodies (→ 413); finer per-endpoint caps still apply underneath.
MAX_REQUEST_BODY_BYTES = int(_cfg("MAX_REQUEST_BODY_BYTES", str(300 * 1024 * 1024)))

# Hard ceiling for any paginated list endpoint's page size.
MAX_PAGE_SIZE = int(_cfg("MAX_PAGE_SIZE", "500"))

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


def _read_agent_files(model: str) -> list[tuple[str, str]]:
    """Read an agent's prompt.md and every doc under config/context/.

    All files under config/context/ matching `.md`/`.txt`/`.markdown` auto-load
    as context. Files over 1 MB and the tail past a 5 MB total cap are skipped;
    skipped files are listed in a synthesized `_auto_context_skipped.md` so the
    agent knows what didn't make it in.

    Returns list of (relative_path, content) tuples.
    """
    if not is_safe_agent_name(model):
        return []
    agent_dir = AGENTS_DIR / model
    prompt_path = agent_dir / "config" / "prompt.md"
    if not prompt_path.exists():
        return []

    files: list[tuple[str, str]] = [("prompt.md", prompt_path.read_text())]

    context_dir = agent_dir / "config" / "context"
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
                    mount_shared: bool = True) -> str | None:
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

    # Agent-scope (no username): /workspace/ + /knowledge/
    if not username:
        _emit_root("/workspace/", agent_dir / "workspace")
        _emit_root("/knowledge/", agent_dir / "knowledge")
    else:
        # User-scope: own user dir + shared spaces + (manager+) config
        user_dir = agent_dir / "users" / username
        if user_dir.is_dir():
            for sub in ("workspace", "context"):
                _emit_root(f"/users/{username}/{sub}/", user_dir / sub)
        # Shared workspace + knowledge only when the agent's mode has them
        # (Personal-only omits both — they aren't mounted in that mode).
        if mount_shared:
            _emit_root("/workspace/", agent_dir / "workspace")
            _emit_root("/knowledge/", agent_dir / "knowledge")
        if role in ("manager", "admin"):
            _emit_root("/config/", agent_dir / "config")

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
                            username: str | None, role: str) -> str | None:
    """The `# Memory` prompt section: capture directive + per-scope content.

    Scope loading matrix: user-scoped sessions get agent + user memory;
    agent-scoped sessions (no username) get agent memory only. A viewer's
    agent scope is read-only. Returns None when memory is fully disabled.
    """
    from services.memory import memory_file
    from storage import agent_store, memory_store

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
    )
    user_enabled = bool(
        username
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
    if user_enabled:
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
    if role == "viewer" and agent_enabled:
        directive += (
            "\nAgent memory is read-only for your role — save "
            "user-scope memories only.\n"
        )
    parts.append(directive)

    if agent_enabled:
        root = memory_file.scope_root(agent_dir, "agent")
        agent_writable = role != "viewer"
        parts.append(
            "\n## Agent memory (shared)\n\n"
            + _render_memory_scope(
                root, virtual="/memories/agent", budget=budget,
                writable=agent_writable,
            )
            + "\n"
        )
    if user_enabled:
        root = memory_file.scope_root(agent_dir, "user", username)
        parts.append(
            f"\n## User memory ({username})\n\n"
            + _render_memory_scope(root, virtual="/memories/user", budget=budget)
            + "\n"
        )
    return "".join(parts)


def build_agent_prompt(model: str, *,
                       username: str | None = None,
                       role: str = "manager",
                       excluded_mcps: dict[str, str] | None = None,
                       dynamic_contexts: list[tuple[str, str]] | None = None,
                       sandboxed: bool = True,
                       client_type: str = "",
                       is_remote: bool = False,
                       target_has_display: bool | None = None,
                       target_device_grants: set[str] | None = None,
                       mount_shared: bool = True) -> str | None:
    """Build the full system prompt for an agent.

    Section order:

      1. Agent's own ``config/prompt.md``
      2. Platform company context (DB) + universal language rule
      3. ``# Auto-Loaded Documentation`` — ``config/context/*`` knowledge files
      4. ``# Available Tools (MCPs)`` — catalog of enabled MCPs (one-liners)
      5. ``# MCP Tool Skills`` — per-MCP deep-dive docs from manifests
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
        client_type: ``"dashboard"`` / ``"phone"`` / ``"task"`` / ``"trigger"``
            / ``"meeting"`` / ``""``. Drives MCP catalog filtering against
            each manifest's ``exclude_from``. Empty string skips the filter
            (defense-only — upstream usually pre-filters).
        is_remote / target_has_display / target_device_grants: the resolved
            execution target's placement facts, forwarded to the MCP catalog +
            skill loaders so a device-local MCP (computer / browser / app
            control) only appears in the prompt when the session can actually
            run it — i.e. on a satellite that has granted the capability.
            Fail-closed defaults keep ``satellite_only`` / device-capability
            MCPs out of local-session prompts.
    """
    if not is_safe_agent_name(model):
        return None  # unsafe agent name → treat as unknown agent
    own_files = _read_agent_files(model)
    if not own_files:
        return None

    parts = []

    # Agent's own prompt.md (always first)
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
            user_dir = agent_dir / "users" / username
            (user_dir / "workspace").mkdir(parents=True, exist_ok=True)
            (user_dir / "context").mkdir(parents=True, exist_ok=True)

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
            is_remote=is_remote, target_has_display=target_has_display,
            target_device_grants=target_device_grants,
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
            is_remote=is_remote, target_has_display=target_has_display,
            target_device_grants=target_device_grants,
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
    if username:
        user_ctx_dir = agent_dir / "users" / username / "context"
        if user_ctx_dir.is_dir():
            ctx_files = sorted(user_ctx_dir.glob("*.md"))
            if ctx_files:
                parts.append(
                    "\n\n---\n\n"
                    "# User Context (Personal)\n\n"
                    "These personal documents are loaded for the current user only.\n"
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
        )
        if memory_section:
            parts.append(memory_section)

    # Workspace and user directory listing
    ws_listing = _scan_workspace(agent_dir, model, username=username, role=role,
                                 sandboxed=sandboxed, mount_shared=mount_shared)
    if ws_listing:
        parts.append(
            "\n\n---\n\n"
            "# Workspace\n\n"
            "Available directories:\n\n"
            f"```\n{ws_listing}\n```\n"
        )
        # Path guidance uses sandbox-relative paths
        if username:
            parts.append(f"\nUser files go to `/users/{username}/workspace/`.")
            if role != "viewer":
                parts.append(" Agent-scoped output goes to `/workspace/`.")
        else:
            parts.append("\nAgent-scoped output goes to `/workspace/`.")

    # Excluded MCPs section (tools unavailable in this session)
    if excluded_mcps:
        parts.append(
            "\n\n---\n\n"
            "# Unavailable Tools\n\n"
            "The following MCP servers are NOT available in this session:\n"
        )
        for mcp_name, reason in sorted(excluded_mcps.items()):
            parts.append(f"- **{mcp_name}**: {reason}")
        parts.append(
            "\nDo not attempt to use tools from these servers. "
            "If the user asks about them, explain they need to be configured first."
        )

    return "\n".join(parts)


# Persistent sessions: idle timeout before reaping (seconds)
PERSISTENT_SESSION_TIMEOUT = int(_cfg("PERSISTENT_SESSION_TIMEOUT", "900"))


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
#   - "ultra" (Codex, gpt-5.6 Sol/Terra only) is max reasoning PLUS Codex-native
#     proactive multi-agent orchestration (parallel sub-agent workstreams inside
#     one turn). Offered per-model via supports_ultra / get_model_supports_ultra()
#     and mapped in core/layers/codex/helpers.map_effort_to_codex; every other
#     layer/model clamps a stored "ultra" to its own ceiling ("max"/"xhigh")
#     so it can never reach a CLI or API that rejects it.
#   - "ultracode" (Opus 4.8) is deliberately NOT in this list. It is a Claude
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
    "claude-fable-5": {
        "label": "Fable 5",
        "provider": "anthropic",
        "context_window": 1_000_000,   # 1M is Fable 5's native (and only) window
        "pricing": (10.0, 50.0, 12.50, 1.00),  # cache = 1.25x input / 0.1x input
        # CLI-only: Fable's pricing is above Opus-tier, so keeping it off
        # direct-llm bounds accidental spend on the hosted (credit-metered) path.
        # Fable's safety classifiers can refuse a request mid-run; Claude Code
        # ships a built-in automatic fallback to Opus 4.8 for that case.
        "layers": ["claude-code-cli"],
        "server_tools": True,   # supports Anthropic web_search/web_fetch
        "supports_reasoning": True,   # thinking is ALWAYS on for Fable (adaptive)
        "supports_xhigh": True,
    },
    "claude-opus-4-8[1m]": {
        "label": "Opus 4.8",
        "provider": "anthropic",
        "context_window": 1_000_000,   # [1m] suffix selects the 1M window in Claude Code CLI
        "pricing": (5.0, 25.0, 6.25, 0.50),  # cache = 1.25x input / 0.1x input
        "layers": ["claude-code-cli"],
        "server_tools": True,   # supports Anthropic web_search/web_fetch
        "supports_reasoning": True,
        "supports_xhigh": True,   # 4.8 keeps the xhigh level (low/medium/high/xhigh/max). NOTE: "ultracode" is NOT an effort level — it's a CLI session setting that pairs xhigh with workflow orchestration; see get_model_supports_xhigh() and DEFAULT_EFFORT_LEVEL.
    },
    "claude-sonnet-5": {
        "label": "Sonnet 5",
        "provider": "anthropic",
        "context_window": 1_000_000,   # native 1M window (no [1m] suffix needed)
        "pricing": (3.0, 15.0, 3.75, 0.30),  # sticker price; intro $2/$10 runs through 2026-08-31
        "layers": ["claude-code-cli", "direct-llm"],
        "server_tools": True,
        "supports_reasoning": True,
        "supports_xhigh": True,   # first Sonnet with the xhigh effort level
    },
    "claude-haiku-4-5": {
        "label": "Haiku 4.5 (200K)",
        "provider": "anthropic",
        "context_window": 200_000,
        "pricing": (1.0, 5.0, 1.25, 0.10),   # Haiku 4.5 rates (input/output/5m-cache-write/cache-read)
        "layers": ["direct-llm"],
        "server_tools": False,  # does not support programmatic tool calling
    },
    # Admins can add further models (Ollama, LM Studio, more Groq/OpenAI, etc.)
    # dynamically via the admin discover-models UI; the entries below are the
    # predefined builtins that ship with the platform.

    # --- OpenAI models ---
    # All gpt-5 family entries set supports_xhigh: True. Effort mapping:
    # pre-5.6 models top out at wire "xhigh" (platform "max" clamps there);
    # the GPT-5.6 family adds wire "max" — and "ultra" (Sol/Terra only,
    # supports_ultra below): Codex-native proactive multi-agent orchestration
    # on top of max reasoning — see core/layers/codex/helpers.map_effort_to_codex.
    #
    # GPT-5.6 family (2026-07-09): Sol = frontier (replaces the retired
    # gpt-5.5 builtin at the same price), Terra = 5.5-class capability at
    # half the cost, Luna = fast/cheap tier. New prompt-caching billing:
    # cache WRITES cost 1.25x the uncached input rate (previously $0) and
    # cache reads keep the 90% discount — reflected in the pricing tuples.
    # Sol first: registry insertion order makes it the codex-cli "Auto"
    # default (flagship-first, as gpt-5.5 was). Agents still PINNED to
    # gpt-5.5 keep running (the CLI supports it; pricing falls back to the
    # provider default) — sync_builtin_models retires the builtin row so
    # pickers stop offering it.
    "gpt-5.6-sol": {
        "label": "GPT-5.6 Sol",
        "provider": "openai",
        "context_window": 1_000_000,
        "pricing": (5.0, 30.0, 6.25, 0.50),  # per 1M: (input, output, cache_write, cache_read)
        "layers": ["codex-cli"],
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # Codex "ultra": max reasoning + proactive multi-agent
    },
    "gpt-5.6-terra": {
        "label": "GPT-5.6 Terra",
        "provider": "openai",
        "context_window": 1_000_000,
        "pricing": (2.50, 15.0, 3.125, 0.25),
        "layers": ["codex-cli", "direct-llm"],
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # codex-cli only — the dashboard offers Ultra
                                  # solely when the codex engine serves the model
    },
    "gpt-5.6-luna": {
        "label": "GPT-5.6 Luna",
        "provider": "openai",
        "context_window": 1_000_000,
        "pricing": (1.0, 6.0, 1.25, 0.10),
        "layers": ["codex-cli", "direct-llm"],
        "supports_reasoning": True,
        "supports_xhigh": True,
        # No supports_ultra: OpenAI's own model manifest caps Luna (the fast/
        # cheap tier) at "max" — ultra is a Sol/Terra capability.
    },
    # (Older GPT-5.x builtins — gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex
    # — were retired with the 5.6 family: the three 5.6 tiers cover the same
    # frontier/mid/cheap spread. Agents pinned to a retired id keep running;
    # admins can re-add any of them as custom models via discover-models.)

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
    },
    "openai/gpt-oss-120b": {
        "label": "GPT-OSS 120B",
        "provider": "groq",
        "context_window": 131_072,
        "pricing": (0.15, 0.60, 0.0, 0.075),
        "layers": ["direct-llm"],
        "supports_reasoning": True,
    },
}


def model_supports_server_tools(model: str) -> bool:
    """Check if a model supports Anthropic server-side tools (web_search, web_fetch)."""
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
        from storage import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model:
                return bool(m.get("supports_xhigh", 0))
    return False


def get_model_supports_ultra(model: str) -> bool:
    """Return True if the model accepts the platform's "ultra" effort level.

    Ultra is Codex-only (gpt-5.6 Sol/Terra): max reasoning plus Codex-native
    proactive multi-agent orchestration. Registry-only on purpose — custom
    admin-added models have no supports_ultra column (the wire clamp in
    map_effort_to_codex is prefix-based, so a custom "gpt-5.6-sol-*" id still
    maps correctly; the dashboard just won't offer the option). Unknown
    models → False: a stored "ultra" then clamps to the model's ceiling in
    every execution layer rather than risking an API rejection.
    """
    entry = MODEL_REGISTRY.get(model)
    return bool(entry.get("supports_ultra", False)) if entry else False


def get_model_provider(model: str) -> str:
    """Resolve the provider for a model ID.

    Resolution order: MODEL_REGISTRY → DB execution_layer_models → prefix heuristics.
    """
    # 1. Check in-memory registry (Anthropic builtins)
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return entry.get("provider", "anthropic")
    # 2. Check DB (dynamically discovered/added models have provider set)
    with contextlib.suppress(Exception):
        from storage import subscription_store
        db_models = subscription_store.list_models(layer="direct-llm")
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
    (e.g. ``gpt-5.6-sol`` is ``codex-cli`` only), so the no-picker default (primary
    layer + default model) must agree or tasks hard-reject the model.
    """
    if not model:
        return []
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return list(entry.get("layers", []))
    try:
        from storage import subscription_store
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
    "anthropic": (3.0, 15.0, 3.75, 0.30),   # Sonnet-level
    "openai":    (2.0, 8.0, 0, 1.0),          # GPT-4.1 level
    "groq":      (0.20, 0.20, 0, 0),          # very cheap hosted inference
    "ollama":            (0, 0, 0, 0),         # local, free
    "openai_compatible": (0, 0, 0, 0),         # self-hosted endpoint, pricing depends on backend
}
MODEL_DEFAULT_PRICING = PROVIDER_DEFAULT_PRICING["anthropic"]
MODEL_DEFAULT_CONTEXT_WINDOW = 200_000


def get_model_pricing(model: str, provider: str = "") -> tuple[float, float, float, float]:
    """Get (input, output, cache_write, cache_read) pricing per 1M tokens.

    Resolution order: DB (custom pricing) → MODEL_REGISTRY → provider default.
    If provider is given, use it directly for fallback instead of heuristic.
    """
    # 1. Check DB for custom pricing (dynamically added models)
    with contextlib.suppress(Exception):
        from storage import subscription_store
        for m in subscription_store.list_models(layer="direct-llm"):
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
    # Check DB for custom context window
    with contextlib.suppress(Exception):
        from storage import subscription_store
        for m in subscription_store.list_models(layer="direct-llm"):
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
        from storage import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model:
                return bool(m.get("supports_reasoning", 0))
    return False


def get_layer_models(layer: str) -> list[dict]:
    """Get the builtin model list for an execution layer (for LayerCapabilities).

    Includes pricing and context_window so sync_builtin_models can populate DB.
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
                # Ultra is a CODEX capability, not a model property in the
                # abstract: Terra is also a direct-llm model, but only the
                # codex engine can run the multi-agent orchestration — so the
                # flag is emitted per-layer and the dashboard's effort picker
                # sees it only on the codex-cli list.
                "supports_ultra": bool(info.get("supports_ultra", False)) and layer == "codex-cli",
            })
    return models


# Precomputed once at module load: model_id → index in MODEL_REGISTRY order.
# Used to sort the DB fallback so builtins surface in the order they appear
# in MODEL_REGISTRY.keys() (e.g. Fable 5 before Opus 4.8 before Sonnet 5). Python
# 3.7+ guarantees dict insertion order, so this is stable.
_MODEL_REGISTRY_ORDER: dict[str, int] = {
    model_id: i for i, model_id in enumerate(MODEL_REGISTRY.keys())
}


def resolve_agent_model(agent_name: str) -> str:
    """Resolve the effective model for an agent session.

    Precedence:
      1. agents.default_model (admin-set per-agent) — used if non-empty.
      2. First enabled model for agent's execution_path from the
         execution_layer_models DB table — builtins first in MODEL_REGISTRY
         insertion order, then custom models by created_at ASC.
      3. Raise RuntimeError — no silent Anthropic fallback, no env-var
         default. The caller decides how to surface this to the user.

    This is the single entry point for model resolution across CLI, Direct
    LLM, Codex, tasks, meetings, phone — replaces the old get_cli_model /
    get_agent_model split which both baked in Anthropic-specific defaults.

    Raises:
        RuntimeError: if the agent has no default_model AND no enabled model
            exists for its execution_path. Happens on fresh installs before
            the admin enables any models, or after an admin disables every
            builtin for a layer without adding a custom one. Message includes
            the agent name and execution path so admins know where to look.
    """
    from storage import agent_store, subscription_store

    agent = agent_store.get_agent(agent_name)
    if agent and agent.get("default_model"):
        return agent["default_model"]

    path = (agent.get("execution_path") if agent else None) or "claude-code-cli"
    db_models = subscription_store.list_models(layer=path)
    enabled = [m for m in db_models if m.get("enabled")]
    if not enabled:
        raise RuntimeError(
            f"No enabled model available for agent '{agent_name}' "
            f"(execution_path='{path}'). Configure one at Admin > "
            f"Execution Layers."
        )

    def _sort_key(m: dict) -> tuple:
        # Builtins first (is_builtin=True sorts before False via `not`),
        # then within builtins by MODEL_REGISTRY order,
        # then by created_at for tie-breaks + custom-model ordering.
        return (
            not m.get("is_builtin"),
            _MODEL_REGISTRY_ORDER.get(m.get("model_id", ""), 999),
            m.get("created_at", ""),
        )

    enabled.sort(key=_sort_key)
    return enabled[0]["model_id"]


# Thin aliases kept for semantic clarity at call sites — CLI vs Direct path
# used to differ, but after dropping the Anthropic-specific env defaults
# they do exactly the same thing. Delegating to one implementation means
# the ~11 existing call sites don't need updates.
def get_cli_model(agent_name: str) -> str:
    """Get the CLI model for an agent (dashboard sessions)."""
    return resolve_agent_model(agent_name)


def get_agent_model(agent_name: str) -> str:
    """Get the model for an agent (direct API / phone path)."""
    return resolve_agent_model(agent_name)


def get_cli_effort(agent_name: str) -> str:
    """Get the effort level for an agent (CLI sessions)."""
    from storage import agent_store
    agent = agent_store.get_agent(agent_name)
    return (agent["default_effort"] if agent and agent["default_effort"] else DEFAULT_EFFORT_LEVEL)

# Execution-path-level builtin tools (server-side Anthropic API tools).
# Each execution path defines its own available builtin tools.
EXECUTION_PATH_BUILTIN_TOOLS: dict[str, list[dict]] = {
    "direct-llm": [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209", "name": "web_fetch"},
    ],
    # "claude-code-cli": []  — CLI has its own builtin tools
    # "ollama": []           — future: custom search tools
}

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
SESSION_EST_LIGHT_MB = int(_cfg("SESSION_EST_LIGHT_MB", "250"))    # Direct-LLM reserve (no CLI process)
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

SCHEDULER_TIMEZONE = _cfg("SCHEDULER_TIMEZONE", "UTC")
SCHEDULER_MODE = _cfg("SCHEDULER_MODE", "embedded")  # embedded | standalone
PROXY_INTERNAL_URL = _cfg("PROXY_INTERNAL_URL", f"http://localhost:{PORT}")
SCHEDULER_SYNC_INTERVAL = int(_cfg("SCHEDULER_SYNC_INTERVAL", "30"))


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

ROLE_PRIORITY = {"admin": 0, "creator": 1, "member": 2}

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
# Auto-derived from deployment shape:
#   - Proxy in Docker-Compose (RUNNING_IN_DOCKER)
#       → "http://${PROXY_SERVICE_NAME}:${PORT}" (service DNS; the shipped compose
#         names the proxy service `otodock-proxy`).
#   - Proxy on host (native install + Docker Collabora — file-tools-mcp default)
#       → "http://host.docker.internal:${PORT}" (works on Linux with
#         `extra_hosts: host.docker.internal:host-gateway` already declared in
#         the file-tools-mcp compose).
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

# Default role for phone/API-key sessions on non-admin agents.
# Admin agents always get "admin". Override specific agents here.
# Agents not listed get the default ("viewer").
PHONE_AGENT_ROLES: dict[str, str] = {
    # "personal-assistant": "manager",  # uncomment if phone PA needs workspace writes
}
PHONE_DEFAULT_ROLE: str = "viewer"
