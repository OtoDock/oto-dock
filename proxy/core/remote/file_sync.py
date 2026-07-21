"""File sync protocol — proxy side of bidirectional file sync with satellites.

Computes file manifests, diffs local vs remote, and prepares files for
push/pull operations over WebSocket.
"""

import base64
import contextlib
import fnmatch
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("claude-proxy.file-sync")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".cache", ".mypy_cache", ".pytest_cache"}

# Manifest hash of zero-byte content — merge logic treats empty files
# specially (see the delete-attribution guard in _resolve_merge).
_EMPTY_CONTENT_HASH = f"sha256:{hashlib.sha256(b'').hexdigest()}"
MAX_CHUNK_SIZE = 512 * 1024  # 512KB per chunk
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB cap on individual files (chunked push/pull handles large media)

# Dotted directories that ARE included in the manifest despite starting
# with ``.``. The default ``not d.startswith(".")`` filter would drop
# every hidden dir; this whitelist is the carve-out for dirs the platform
# legitimately needs to keep in sync with the satellite.
#
# ``.credentials`` (OAuth token files) is deliberately NOT here: token
# files are delivered per-session over the session-file broker channel
# (``remote_execution._collect_session_files`` → satellite
# ``sessions/session_files.py``) and wiped at session close, so a satellite
# disk never holds long-lived refresh tokens between sessions. The platform
# is refresh-authoritative (proxy-side background refresher); the transient
# satellite copies are disposable.
#
# - ``.claude`` — Claude Code CLI per-user dir. Mixed authority:
#   ``settings.json`` + the hook scripts are HOST-LOCAL (regenerated at every
#   session start on whichever host runs it — the proxy's
#   ``ensure_persistent_claude_dir`` for local sessions, the satellite's
#   ``CLISession.start`` / ``_write_cli_hooks`` for remote ones — and the
#   platform copy carries sandbox-internal ``/users/{u}/.claude`` hook paths
#   that are invalid on a satellite; never synced, see
#   ``_CLAUDE_HOST_LOCAL_FILES``). ``projects/<hash>/<sid>.jsonl`` is
#   satellite-authoritative (claude-code writes session state there) and STAYS
#   synced. Including ``.claude`` in the manifest lets the diff see (but not
#   delete — see ``SATELLITE_OWNED_SUBPATHS`` below) the satellite-authored bits.
# - ``.codex`` — mixed authority for the Codex CLI: ``auth.json`` is
#   platform-authoritative; ``config.toml`` / ``AGENTS.md`` / ``hooks.json``
#   are HOST-LOCAL (regenerated at every session start on whichever host runs
#   it — never synced, see ``_CODEX_HOST_LOCAL_FILES``); ``sessions/`` is
#   satellite-authoritative.
INCLUDE_DOTTED_DIRS = {".claude", ".codex"}

# Path segments whose subtrees are **satellite-authoritative** — they may
# legitimately appear in the satellite manifest without a platform
# counterpart, and ``diff_manifests`` must NOT mark them as ``to_delete``
# even when their parent dir is in ``DEFAULT_PUSH_ONLY_SEGMENTS``. These
# are session-state files written by the CLI on the satellite at run time:
# - ``.claude/projects/<hash>/<sid>.jsonl`` — Claude Code session JSONL
#   that ``--resume`` keys on. If we deleted these on every session start
#   the user would lose chat history every time their session was reaped
#   or aborted.
# - ``.claude/tasks/<sid>/<n>.json`` — Claude Code task store
#   (TaskCreate/TaskUpdate), keyed by the session id ``--resume`` keeps.
#   Scrubbing it as a platform-authoritative extra is what lost the task
#   list on every re-warm (the sync runs exactly at session start).
# - ``.codex/sessions/<thread>.jsonl`` — equivalent for Codex.
# Compared against rel_path with ``parts.endswith(SUBPATH)`` semantics —
# any subtree underneath the listed subpath is protected.
SATELLITE_OWNED_SUBPATHS: tuple[tuple[str, ...], ...] = (
    (".claude", "projects"),
    (".claude", "tasks"),
    (".codex", "sessions"),
)

# Directories that are immediate children of a ``.claude``/``.codex`` CLI dir and
# hold pure host-local RUNTIME state with zero sync value — session transcripts
# (the satellite-owned ``projects``/``sessions`` resume JSONLs, tens of MB we'd
# otherwise hash every manifest only to skip in the diff), temp dirs, caches,
# shell snapshots, backups. They churn constantly (``.codex/.tmp`` alone can hold
# thousands of files), so they are PRUNED during the walk — never manifested,
# never synced. Real config + ``skills/`` (curated content) still sync. This set
# MUST stay identical to ``satellite/file_sync.py`` or the two manifests disagree
# and the diff churns.
_CLI_RUNTIME_CHILD_DIRS = frozenset({
    "projects", "sessions",                  # session transcripts (resume state)
    # Claude Code task store (``tasks/<session-id>/<n>.json``) — per-machine
    # runtime state exactly like the transcripts: the CLI keys it by the
    # session id that ``--resume`` keeps, so it must live and die with the
    # host that runs the session, never sync.
    "tasks",
    ".tmp", "tmp", "cache",                  # temp + caches
    "shell-snapshots", "shell_snapshots",    # host-specific shell env
    "backups", "debug", "telemetry", "session-env", "plans",
    # Per-session ssh keys ($OTO_SSH_KEY_DIR) — materialized locally by
    # session_config_dir.materialize_ssh_keys_for_sandbox, delivered to
    # satellites only via the session-file broker (purged at close). Syncing
    # them would persist private keys on every satellite of the agent.
    "ssh",
})


def _is_cli_runtime_child(parent_name: str, child_name: str) -> bool:
    """True for a runtime-cruft dir that is an immediate child of ``.claude``/``.codex``."""
    return parent_name in (".claude", ".codex") and child_name in _CLI_RUNTIME_CHILD_DIRS


def _is_cli_runtime_cruft_file(parent_name: str, file_name: str) -> bool:
    """True for CLI backup/corrupted state files directly under ``.claude``/``.codex``
    (e.g. ``.claude.json.backup.<ts>``) — host-local, never synced."""
    if parent_name not in (".claude", ".codex"):
        return False
    return ".backup." in file_name or ".corrupted." in file_name

# Codex app-server keeps per-machine SQLite *runtime state* directly in
# CODEX_HOME — versioned DB families (``state_N``, ``logs_N``, ``goals_N``,
# ``memories_N``, …) plus their ``-wal``/``-shm`` sidecars. Each host — the
# platform AND every satellite — owns its own copies; a foreign one aborts
# daemon init with "migration N … has been modified"
# (``CodexAppServerSession._reset_codex_state`` self-heals it, but only if its
# globs cover the poisoned file). New DB families appear with codex releases,
# so match EVERY SQLite file directly under ``.codex/`` — nothing legitimate to
# sync is ever SQLite there. These NEVER sync, in EITHER direction — excluded
# from every manifest/snapshot below. This is stronger than
# ``SATELLITE_OWNED_SUBPATHS`` (which only blocks satellite→platform *deletes*):
# the SQLite must also never be pushed platform→satellite. The glob matches
# ``_reset_codex_state``'s own glob so what we exclude is exactly what codex
# resets.
_CODEX_RUNTIME_GLOBS = ("*.sqlite*",)

# Per-session REGENERATED Codex config — each host writes its own at session
# start (the proxy's ``_write_config_toml`` for local sessions; the satellite
# from the ``start_session`` payload for remote ones, with rewritten paths,
# SENTINEL bearer tokens and broker fetch tokens instead of real secrets).
# Syncing them copied the platform's secret-bearing ``config.toml`` (real
# OAuth bearer in ``http_headers``) onto every satellite — bypassing the
# bearer-swap; Windows Defender flagged exactly that file — and ping-ponged
# content between hosts on every session. Host-local, NEVER synced, either
# direction. ``auth.json`` joined after the writeback audit: the
# subscription pool's DB tokens are the source of truth (its own refresh
# worker), both paths regenerate auth.json at session start (local
# ``_write_auth_json``; remote via the ``start_session`` ``auth_json``
# payload), and nothing ever reads the file back — syncing it was redundant
# and could clobber a daemon-refreshed token mid-session.
_CODEX_HOST_LOCAL_FILES = frozenset({
    "config.toml", "AGENTS.md", "hooks.json", "auth.json",
})


def _is_codex_runtime_state(rel_path: str) -> bool:
    """True for host-local ``.codex`` files that must never sync in either
    direction: the app-server's per-machine SQLite runtime plus the
    per-session regenerated config files (``_CODEX_HOST_LOCAL_FILES``)."""
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[-2] != ".codex":
        return False
    if parts[-1] in _CODEX_HOST_LOCAL_FILES:
        return True
    return any(fnmatch.fnmatch(parts[-1], g) for g in _CODEX_RUNTIME_GLOBS)


# Per-session REGENERATED Claude Code config — each host writes its own at
# session start (the proxy's ``ensure_persistent_claude_dir`` for local sessions;
# the satellite's ``CLISession.start`` via ``_write_cli_hooks`` /
# ``_write_hook_scripts`` for remote ones). The platform copy carries
# sandbox-internal hook paths (e.g. ``/users/{u}/.claude/permission_gate.py``)
# that are meaningless on a satellite, which immediately regenerates them with
# its own paths before the CLI launches — so syncing them is at best redundant
# and at worst leaves broken paths if startup is interrupted. Host-local, NEVER
# synced, either direction. Mirrors ``_CODEX_HOST_LOCAL_FILES``. (``projects/`` —
# the session ``.jsonl`` — is satellite-authoritative and STAYS synced.)
_CLAUDE_HOST_LOCAL_FILES = frozenset({
    "settings.json", "permission_gate.py", "tool_result_forwarder.py",
    "subagent_tracker.py", "stop_tracker.py", "stdio_path_interceptor.py",
    "mcp-config.json", "system-prompt.md",
    # Claude CLI OAuth credential file. Each host's copy is written by the
    # platform itself — at session start (local: the CLI layer; remote: the
    # satellite from the ``credentials_json`` start payload) and on every
    # rotation by the token fan-out (local write / ``credentials_update``
    # push). Syncing it would ping-pong content between hosts on every
    # rotation and race the dedicated push channel. Mirrors .codex auth.json
    # in _CODEX_HOST_LOCAL_FILES. (``auth.json`` stays excluded too — legacy
    # interactive-TUI login state a stray /login could leave behind.)
    ".credentials.json",
    "auth.json",
})


def _is_claude_runtime_state(rel_path: str) -> bool:
    """True for host-local ``.claude`` config files that must never sync in
    either direction (``_CLAUDE_HOST_LOCAL_FILES``). Matches only DIRECT children
    of ``.claude/`` — so ``.claude/projects/<hash>/<sid>.jsonl`` is unaffected."""
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[-2] != ".claude":
        return False
    return parts[-1] in _CLAUDE_HOST_LOCAL_FILES


def _is_venv_dir(path: Path) -> bool:
    """True if ``path`` is a real Python venv root (contains pyvenv.cfg).
    Lets the manifest skip actual venvs precisely while still syncing a
    workspace dir merely named ``venv``/``bin``/``Scripts`` (no pyvenv.cfg).
    Mirrors satellite/file_sync.py so both manifests agree."""
    try:
        return (path / "pyvenv.cfg").is_file()
    except (OSError, PermissionError):
        return False


def _is_satellite_owned(rel_path: str) -> bool:
    """True when ``rel_path`` lies under a satellite-authoritative subtree.

    The diff layer uses this to filter ``to_delete`` so the satellite's
    session-state files survive a re-sync.
    """
    parts = rel_path.split("/")
    for subpath in SATELLITE_OWNED_SUBPATHS:
        # Search for the (seg1, seg2) pair anywhere in the path. e.g.
        # ``users/alice/.claude/projects/foo/bar.jsonl`` contains
        # (".claude", "projects") at index 2-3.
        for i in range(len(parts) - len(subpath) + 1):
            if tuple(parts[i : i + len(subpath)]) == subpath:
                return True
    return False


@dataclass
class FileEntry:
    """A single file in a manifest."""
    path: str       # relative to agent dir
    hash: str       # sha256:<hex>
    size: int
    mtime: float


# Two cross-host mtimes are "un-orderable" if they fall within this margin
# (after clock-offset adjustment) — network/heartbeat jitter could flip them, so
# the merge declines to order them and lets the platform stay live.
CLOCK_JITTER_MARGIN_SECONDS = 2.0


@dataclass
class FileAction:
    """One file's resolution from the 3-way merge. The caller executes ``op``,
    then records/clears the base + tombstone + (optional) recover-bin capture."""
    rel_path: str
    op: str                            # "push" | "pull" | "delete_satellite" | "delete_platform" | "noop"
    base_hash: str | None = None       # record this as the new base (caller stats
                                       # the platform mtime); None = no base write
    clear_base: bool = False           # drop the base row (converged-to-absent)
    drop_tombstone: bool = False       # path is live again → remove its tombstone
    capture_side: str | None = None    # "platform" | "satellite": whose CURRENT
                                       # bytes to copy to the recover-bin first
    capture_reason: str | None = None  # "conflict" | "deleted"
    notify_user: str | None = None     # cross-user conflict: the loser's username
                                       # slug to notify (resolved to a sub at send)


@dataclass
class MergePlan:
    """Result of the 3-way merge: per-file actions + the isolation/security
    scrub list (satellite files that violate per-user/role isolation — deleted
    on the satellite, never captured, never pulled)."""
    actions: list[FileAction]
    to_scrub: list[str]


def _adjusted_satellite_mtime(satellite_mtime: float, clock_offset: float | None) -> float | None:
    """Convert a satellite epoch mtime into the proxy's clock. ``clock_offset`` =
    ``proxy_utc − satellite_utc`` (from the heartbeat). ``None`` if no offset yet."""
    if clock_offset is None:
        return None
    return satellite_mtime + clock_offset


def _platform_wins_divergence(
    platform_mtime: float, satellite_mtime: float, clock_offset: float | None,
) -> bool:
    """True if the platform copy should stay live on a both-changed divergence.

    Newest-wins by offset-adjusted epoch mtime; un-orderable (within the jitter
    margin, or no offset yet) → platform-wins (deterministic, never a coin-flip).
    Epoch mtime is timezone-immune; the offset only corrects genuine wall-clock skew.
    """
    adj = _adjusted_satellite_mtime(satellite_mtime, clock_offset)
    if adj is None:
        return True  # no measured offset → un-orderable → platform-wins
    if adj > platform_mtime + CLOCK_JITTER_MARGIN_SECONDS:
        return False  # satellite genuinely newer
    return True       # platform newer, or within margin → platform-wins


def compute_manifest(
    agent_dir: Path,
    *,
    scope: str = "full",
    execution_path: str = "claude-code-cli",
    target_username: str | None = None,
    target_role: str = "",
    exclude_user_dirs: bool = False,
) -> list[FileEntry]:
    """Compute file manifest for an agent directory.

    Args:
        agent_dir: Root agent directory on the platform.
        scope: "full" | "config_only" (for push-only config dir).
        execution_path: "claude-code-cli" or "codex-cli" (affects which config dirs to include).
        exclude_user_dirs: True for SHARED-ONLY agents — their mode has no
            per-user scope at all, so the ``users/`` subtree never syncs to
            any satellite (stray dirs from older installs stay platform-side).
        target_username: When set, the manifest is computed for a user-paired
            satellite owned by this username — apply the per-user
            isolation blacklist so the manifest excludes OTHER users'
            data and agent-scoped credentials. None = admin-shared
            target, no filtering (default).
        target_role: the per-agent role of the session driving
            this sync (``"manager"`` / ``"editor"`` / ``"viewer"`` / ``"admin"``
            / ``""`` for agent-scope sessions). When the role is not in the
            owner tier (manager/admin), ``config/`` paths are excluded so
            the satellite never receives the agent's prompt / context files
            for editor/viewer sessions. ``""`` = no role filter (default;
            agent-scope sessions inherit this behavior — config is not
            relevant to them either).
    """
    entries = []
    if not agent_dir.exists():
        return entries

    for root, dirs, files in os.walk(agent_dir):
        # Skip heavy / unwanted dirs. Hidden dirs are skipped UNLESS in
        # the dotted-dir whitelist (``.credentials/``, ``.claude/``,
        # ``.codex/``) so OAuth tokens + CLI config sync to the satellite.
        # Without the whitelist carve-out, every OAuth-based MCP on a
        # remote satellite would fail silently at runtime because its
        # tokens never reach the satellite.
        _root_name = os.path.basename(root)
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS
            and not _is_venv_dir(Path(root) / d)
            and (not d.startswith(".") or d in INCLUDE_DOTTED_DIRS)
            and not _is_cli_runtime_child(_root_name, d)
        ]
        if exclude_user_dirs and root == str(agent_dir) and "users" in dirs:
            dirs.remove("users")

        rel_root = Path(root).relative_to(agent_dir)

        # Scope filtering
        if scope == "config_only":
            # Only include config/ directory
            if str(rel_root) != "." and not str(rel_root).startswith("config"):
                continue

        for f in files:
            file_path = Path(root) / f
            rel_path = str(rel_root / f) if str(rel_root) != "." else f

            # Codex per-machine SQLite runtime never syncs (see helper above) —
            # exclude before any other filter so it's never pushed to a satellite.
            if _is_codex_runtime_state(rel_path):
                continue
            # Claude Code per-session config (settings.json + hooks) is host-local —
            # each host regenerates it at session start; never sync (see helper).
            if _is_claude_runtime_state(rel_path):
                continue
            # CLI backup/corrupted state files (.claude.json.backup.* etc.) — cruft.
            if _is_cli_runtime_cruft_file(_root_name, f):
                continue
            # Transient staging files from an in-flight / aborted chunked push or
            # pull (``pull_file_to_path`` writes ``<dest>.partial`` under the agent
            # dir). Never part of the synced tree — excluded symmetrically in
            # satellite/file_sync.py so the 3-way merge never adopts/propagates one.
            if f.endswith(".partial"):
                continue

            # Per-user + role isolation. Single source of truth shared with the
            # active-session fan-out (``services/remote/workspace_fanout.py``) via
            # ``should_sync_to_target`` — a file ships under identical push-direction
            # policy whether it lands at session start (this manifest) or mid-turn
            # (fan-out). Drops other users' data + agent-scope credentials on
            # user-paired targets, and ``config/`` for non-owner sessions.
            if not should_sync_to_target(rel_path, target_username, target_role):
                continue

            try:
                # Skip symlinks entirely — they can't be transferred meaningfully
                # via base64 file content and would silently become regular files
                # on the other side.
                if file_path.is_symlink():
                    continue
                stat = file_path.stat()
                # Skip very large files (> MAX_FILE_SIZE).
                if stat.st_size > MAX_FILE_SIZE:
                    logger.warning(
                        "Skipping %s in manifest: %.1f MB exceeds %d MB limit",
                        rel_path, stat.st_size / 1024 / 1024, MAX_FILE_SIZE // 1024 // 1024,
                    )
                    continue
                file_hash = _hash_file(file_path)
                entries.append(FileEntry(
                    path=rel_path,
                    hash=file_hash,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                ))
            except (OSError, PermissionError):
                continue

    return entries


# Paths blacklisted for user-paired satellite syncs. The role-v2 model
# moved all OAuth tokens to `users/{u}/.credentials/{provider}-tokens/`
# (user-scope) and `knowledge/.credentials/` (agent-scope). The legacy
# `workspace/credentials/` and `.cache/oauth/` locations from before role-v2
# are no longer used — dropped in the role-v2 cleanup.
_SENSITIVE_PATH_PREFIXES: tuple[str, ...] = (
    "service-accounts/",
)


def _is_other_user_or_sensitive(rel_path: str, target_username: str) -> bool:
    """Return True if this path must NOT sync to a user-paired satellite.

    Blacklist rules:
      * `users/{u}/...` where `{u}` != ``target_username`` — other users' data
        (their workspace, their `.credentials/{provider}-tokens/`, etc.)
      * Any path beginning with `service-accounts/` — platform-level
        service-account credentials (only used by agent-scope sessions,
        which are refused on user-paired machines)
      * `knowledge/.credentials/` — agent-scope service-account tokens

    The role-based `config/` filter is handled separately in
    ``compute_manifest`` via the ``target_role`` parameter — non-owner
    sessions never see config files even on their own paired laptop.
    """
    parts = rel_path.split("/")
    if parts and parts[0] == "users" and len(parts) >= 2:
        if parts[1] != target_username:
            return True
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if rel_path.startswith(prefix):
            return True
    if rel_path.startswith("knowledge/.credentials/"):
        return True
    return False


# Default platform-authoritative dir PREFIX (matched via str.startswith).
# `config/` is owner-authored agent behaviour (prompt.md, context/ docs)
# — STATIC: written at install + owner edits, NOT regenerated per
# session. (Memory lives under knowledge/ + users/, not here.) By default it's pushed DOWN to satellites (and satellite-only
# extras scrubbed), but an owner-tier session (manager/admin WITH a username —
# the machine owner's on user-paired targets, the session human's on
# admin-shared targets) curates it on its remote machine, so
# `_initial_workspace_sync` drops it from this set for those sessions to let
# edits sync back (mirroring can_write_back).
# The platform-minted/regenerated `.claude`/`.codex` live in
# DEFAULT_PUSH_ONLY_SEGMENTS and stay push-only for EVERY session.
DEFAULT_PUSH_ONLY_PREFIXES: tuple[str, ...] = (
    "config/",  # owner-authored agent config (prompts, context, cron)
)

DEFAULT_PUSH_ONLY_SEGMENTS: tuple[str, ...] = (
    ".claude",  # CLI per-user config: settings.json, hooks, system prompt
    ".codex",   # Codex per-user config: config.toml, AGENTS.md, auth.json
    # OAuth token files no longer sync at all (per-session broker delivery,
    # see INCLUDE_DOTTED_DIRS) — kept here so ``can_write_back`` keeps
    # refusing satellite-side token writes, defense-in-depth.
    ".credentials",
)


def _is_push_only(path: str, prefixes: set[str], segments: set[str]) -> bool:
    """Decide if a relpath is platform-authoritative."""
    if any(path.startswith(p) for p in prefixes):
        return True
    parts = path.split("/")
    return any(seg in parts for seg in segments)


def is_canonical_rel_path(rel_path: str) -> bool:
    """Syntactic gate for an agent-tree-relative path from the wire/hooks.

    A legit path is always ``relative_to(agent_dir).as_posix()`` —
    forward-slash, no leading '/', no '..' / '.' / NUL / backslash, and its
    first segment is one of the four agent-tree scopes. Reject anything else
    so the scope a caller judges (``parts[0]``) is exactly the scope the
    write later resolves to; otherwise an editor could launder
    ``workspace/../config/x`` past the workspace rule (within-tree scope
    escalation), and a mistranslated satellite-host absolute like
    ``C:/Users/.../workspace/x`` would mkdir a junk ``C:/`` chain inside the
    platform's agent dir (``relative_to`` alone can't catch it — on Linux
    ``C:`` is just a directory name). Shared by ``can_write_back`` and the
    ``pull_through`` / ``push_back`` hook flows.
    """
    if not rel_path or "\x00" in rel_path or "\\" in rel_path:
        return False
    parts = rel_path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return parts[0] in ("config", "knowledge", "workspace", "users")


def is_engine_machinery_path(rel_path: str) -> bool:
    """True when ``rel_path`` sits under CLI/Codex machinery or credentials —
    the structurally push-only segments ``can_write_back`` denies for EVERY
    role. Engines rewrite their own runtime state mid-turn (e.g. codex
    refreshes ``.codex/models_cache.json`` every turn), so the satellite
    reports these as ordinary ``file_changed`` writes and the denial is
    routine, not a policy anomaly — the applier logs them quietly instead of
    WARNING (observed 2026-07-09: recurring write-back-denied noise per
    codex turn)."""
    parts = rel_path.split("/")
    return ".claude" in parts or ".codex" in parts or ".credentials" in parts


def can_write_back(rel_path: str, role: str, username: str,
                   mount_username: str | None = None) -> bool:
    """Can a satellite session with this role + username write this
    agent-tree-relative path BACK to the platform?

    ``mount_username`` is the session's MOUNT identity (``SecurityContext.
    mount_username`` — "" for agent-scope mounts including Shared-only human
    chats). It gates ONLY the per-user-dir rule below; ``username`` stays the
    REAL human for the config/knowledge curation rule. ``None`` (legacy
    callers, machine-pairing identities) falls back to ``username``.

    This is the SINGLE source of truth for the satellite→platform write
    direction, used at BOTH enforcement layers — the per-turn ``file_changed``
    applier (``core/remote/satellite_connection.py``) and the initial-sync
    ``diff_manifests`` to_pull decision. It mirrors the native-tool
    per-role write matrix enforced by the permission gate.

    SECURITY: ``role``/``username`` MUST come from the proxy-side authenticated
    ``SecurityContext`` (``get_session_security``), never from the satellite's
    wire payload. This is the **only** filesystem-write gate for Codex remote
    sessions — Codex has no per-tool permission hooks, so a Codex
    editor/viewer agent CAN write outside its scope on the satellite disk;
    this predicate is what stops those writes from reaching the platform.

    ``rel_path`` is forward-slash, relative to the agent root (e.g.
    ``"knowledge/x.md"``, ``"users/alice/workspace/y.md"``). Fail-closed:
    empty / unclassifiable paths return ``False``.
    """
    if not is_canonical_rel_path(rel_path):
        return False
    parts = rel_path.split("/")
    # 1. CLI/Codex machinery (settings.json, hooks, AGENTS.md, auth.json,
    #    config.toml, AND session *.jsonl under projects/ or sessions/) +
    #    ``.credentials/`` OAuth tokens are NEVER accepted back — they're
    #    platform-authoritative / satellite-owned and (credentials) secret. The
    #    platform is the sole source of truth for all three, for every role.
    if is_engine_machinery_path(rel_path):
        return False
    owner_tier = role in ("manager", "admin")
    top = parts[0]
    # 2. Agent config + knowledge — owner curation only. Requires owner-tier
    #    (manager/admin) AND a real username. ``username`` here is the REAL
    #    human from the SecurityContext (a Shared-only human chat keeps it set;
    #    only ``mount_username`` blanks on scope) — so an owner-tier human
    #    curates knowledge/config from any scope, mirroring the local mounts
    #    (owner-tier humans mount both RW via ``config_visible``). SERVICE
    #    sessions (tasks / phone / triggers — no human) carry username == ""
    #    and mount knowledge RO, so the username gate blocks exactly them.
    if top in ("config", "knowledge"):
        return owner_tier and bool(username)
    # 3. Shared agent workspace — editor tier and up (viewer is read-only here).
    if top == "workspace":
        return role in ("manager", "admin", "editor")
    # 4. Per-user dirs — own dir only, any role, keyed on the MOUNT identity
    #    (a Shared-only human chat has no per-user dirs; its mount_username
    #    "" denies every users/ write-back). (Stricter than local for
    #    admin-on-admin-agent cross-user writes — intentionally not synced back
    #    from a remote satellite; revisit if a real workflow needs it.)
    if top == "users":
        mu = username if mount_username is None else mount_username
        if len(parts) < 2 or not mu:
            return False
        return parts[1] == mu
    # 5. Anything else (root-level / unknown) — fail closed.
    return False


def should_sync_to_target(rel_path: str, username: str | None, role: str) -> bool:
    """Should this agent-tree-relative path be PUSHED to a satellite whose active
    session has this ``(username, role)``?

    Push-direction mirror of the two inline isolation filters in
    ``compute_manifest`` — the SINGLE source of truth for "may this machine
    receive this file", shared by ``compute_manifest`` (the session-start manifest)
    and ``services/remote/workspace_fanout.py`` (the per-write active-session fan-out), so
    a file ships under identical policy whether it lands at session start or mid-turn.

    Rules (behavior-preserving extraction of ``compute_manifest``):
      * ``username is None`` ⇒ admin-shared target — no per-user filter (mirrors
        ``compute_manifest``'s ``target_username is not None`` guard). The fan-out
        always passes a concrete username (a real slug, or ``""`` for agent-scope),
        so a user-paired / agent-scope target only receives ``users/{own}`` + shared
        paths, never another user's data.
      * Other users' data + agent-scope credentials (``_is_other_user_or_sensitive``)
        never sync to a user-scoped target.
      * ``config/`` is owner-tier only (manager/admin); non-owner (editor / viewer /
        agent-scope) targets never receive the agent's prompt/context files.

    NOTE: this governs the PUSH direction (platform → satellite). The satellite →
    platform WRITE direction is the separate ``can_write_back`` predicate above.
    """
    if username is not None and _is_other_user_or_sensitive(rel_path, username):
        return False
    owner_tier = role in ("manager", "admin")
    if not owner_tier and rel_path.startswith("config/"):
        return False
    return True


def _divergence_capture(
    path: str, author_of, satellite_user: str, platform_wins: bool,
) -> tuple[str | None, str | None, str | None]:
    """``(capture_side, capture_reason, notify_user)`` for a both-changed divergence.

    STRICT policy: capture only a genuine **cross-user** collision on a **shared**
    file (the loser's parallel work must survive → capture + notify). Personal /
    same-user → newest-wins, **no capture**. Unknown author (or unknown satellite
    user, e.g. an agent-scope session) → a **silent** recoverable copy, no notify,
    so an attribution gap never drops a real cross-user edit. The loser is
    whichever side did NOT win. ``author_of(path)`` and ``satellite_user`` are
    username slugs.
    """
    if path.split("/", 1)[0] == "users":
        return (None, None, None)  # personal → same-user domain → no capture
    loser_side = "satellite" if platform_wins else "platform"
    author = author_of(path)  # platform last-writer slug, or None
    if not author or not satellite_user:
        return (loser_side, "conflict", None)  # can't confirm same-user → silent copy
    if author == satellite_user:
        return (None, None, None)  # same user edited both sides → no capture
    loser = satellite_user if platform_wins else author
    return (loser_side, "conflict", loser)


def _resolve_merge(
    path: str, P: str | None, S: str | None, Pm: float, Sm: float,
    B: str | None, T: float | None, *,
    clock_offset: float | None, author_of, satellite_user: str,
    target_role: str, target_username: str,
    satellite_tree_present: bool = True,
) -> FileAction | None:
    """The 3-way merge for one path (after push-only / isolation filtering).

    ``P/S/B`` = platform/satellite/base hash (``None`` = absent); ``T`` =
    tombstone ``deleted_at_mtime`` or ``None``. Returns the ``FileAction`` to take,
    or ``None`` when nothing needs doing. Every **pull** is gated by
    ``can_write_back`` — an unauthorized satellite→platform write becomes a
    no-op (leave the satellite's copy), never a clobber.
    """
    def cwb() -> bool:
        return can_write_back(path, target_role, target_username)

    # --- both present ---
    if P is not None and S is not None:
        if P == S:
            # In sync. Heal a stale/absent base; retire an obsolete tombstone.
            if B != P or T is not None:
                return FileAction(path, "noop", base_hash=P, drop_tombstone=(T is not None))
            return None
        # Both present, hashes differ.
        if B is not None and P == B and S != B:
            # Only the satellite changed since base → adopt it.
            if cwb():
                return FileAction(path, "pull", base_hash=S, drop_tombstone=(T is not None))
            return None  # no write authority → leave the satellite's divergent copy
        if B is not None and S == B and P != B:
            # Only the platform changed since base → push.
            return FileAction(path, "push", base_hash=P, drop_tombstone=(T is not None))
        # Both changed since base, or first-contact (no base) → divergence.
        platform_wins = _platform_wins_divergence(Pm, Sm, clock_offset)
        cap_side, cap_reason, notify_user = _divergence_capture(
            path, author_of, satellite_user, platform_wins,
        )
        if platform_wins:
            return FileAction(
                path, "push", base_hash=P, drop_tombstone=(T is not None),
                capture_side=cap_side, capture_reason=cap_reason, notify_user=notify_user,
            )
        if cwb():
            return FileAction(
                path, "pull", base_hash=S, drop_tombstone=(T is not None),
                capture_side=cap_side, capture_reason=cap_reason, notify_user=notify_user,
            )
        # Satellite "won" on time but can't write back → nothing changes → no capture.
        return None

    # --- platform only ---
    if P is not None and S is None:
        # The satellite lacks a platform file. If it CONVERGED on this exact version
        # (B == P), its tree is still ALIVE (non-empty manifest — not a wipe), AND the
        # session has write authority for the path, then the satellite DELETED it
        # out-of-turn → propagate the delete to the platform (delete-attribution:
        # B == P is the "the satellite had THIS file and removed it" signal — no delete
        # timestamp needed). Capture the platform bytes to the recover-bin first (7-day
        # undo). Otherwise RE-PUSH, never delete from absence: a brand-new file
        # (no base), an edit-vs-delete where the platform changed it since (B != P →
        # edit wins), a viewer with no write-back authority, or a wiped/empty satellite.
        # ZERO-BYTE files are never delete-attributed: historically the
        # satellite acked-ok while silently skipping empty writes, so a
        # poisoned base (B == hash-of-empty, satellite never had the file)
        # is indistinguishable from a real satellite delete — deleting here
        # would destroy the platform's file. Re-push instead; the worst case
        # is a deliberately-deleted empty file resurrecting (safe direction).
        if (B is not None and P == B and satellite_tree_present and cwb()
                and P != _EMPTY_CONTENT_HASH):
            return FileAction(
                path, "delete_platform", clear_base=True,
                capture_side="platform", capture_reason="deleted",
            )
        return FileAction(path, "push", base_hash=P, drop_tombstone=(T is not None))

    # --- satellite only ---
    if S is not None and P is None:
        if T is not None:
            adj = _adjusted_satellite_mtime(Sm, clock_offset)
            if adj is not None and adj > T + CLOCK_JITTER_MARGIN_SECONDS:
                # Re-created on the satellite AFTER the platform's delete → re-create
                # wins → adopt it and retire the tombstone.
                if cwb():
                    return FileAction(path, "pull", base_hash=S, drop_tombstone=True)
                return None
            # Delete not yet applied here (or un-orderable) → the platform's delete
            # wins: capture the satellite's bytes silently, then delete on the
            # satellite. Keep the tombstone (other idle machines still need it).
            return FileAction(
                path, "delete_satellite", clear_base=True,
                capture_side="satellite", capture_reason="deleted",
            )
        # No tombstone → never a delete → adopt the satellite-authored file.
        if cwb():
            return FileAction(path, "pull", base_hash=S)
        return None  # satellite-authored but not writable-back → leave it

    # --- both absent on this machine (reached via a stale base/tombstone key) ---
    if B is not None:
        return FileAction(path, "noop", clear_base=True)  # keep the shared tombstone
    return None


def diff_manifests(
    local: list[FileEntry],
    remote: list[dict],
    *,
    base: dict[str, tuple[str, float]] | None = None,
    tombstones: dict[str, float] | None = None,
    clock_offset: float | None = None,
    author_of=None,
    satellite_user: str = "",
    push_only_dirs: set[str] | None = None,
    push_only_segments: set[str] | None = None,
    target_username: str | None = None,
    target_role: str = "",
    session_username: str = "",
    exclude_user_dirs: bool = False,
) -> MergePlan:
    """Resolve a 3-way merge (platform vs satellite vs last-converged base) into a
    per-file action plan — **newest-version-wins**, not proxy-always-wins.

    Args:
        local: Platform manifest (``FileEntry`` with hash + mtime).
        remote: Satellite manifest (dicts with ``path``/``hash``/``mtime``).
        base: ``{rel_path: (base_hash, base_mtime)}`` from ``sync_state`` — the
            hash last converged with this machine. Empty = first sync.
        tombstones: ``{rel_path: deleted_at_mtime}`` of LIVE platform tombstones —
            the only thing that authorizes deleting a satellite copy.
        clock_offset: signed ``proxy_utc − satellite_utc`` (from the heartbeat) to
            adjust satellite mtimes into the proxy clock; ``None`` = unknown →
            divergences are un-orderable → platform-wins.
        author_of: ``callable(rel_path) -> user_sub | None`` (``file_author``) for
            cross-user conflict detection.
        satellite_user: the username slug of the satellite session driving this
            sync (the satellite-side writer), for cross-user comparison.
        push_only_dirs / push_only_segments: platform-authoritative paths
            (``config/``, ``.claude``, ``.codex``, ``.credentials``) — always
            pushed, never pulled/conflicted.
        target_username / target_role: per-user/role isolation. A user-paired
            target scrubs other users' satellite files; ``can_write_back`` gates
            every pull by role.
        session_username: the SESSION's authenticated human (SecurityContext
            slug; ``""`` for service sessions) — the ``can_write_back`` identity
            on ADMIN-SHARED machines, where ``target_username`` is ``None`` by
            design (it is the machine-pairing filter, not a person). A
            user-paired machine keeps using its owner (``target_username``); an
            orphaned-owner machine (``target_username == ""``) stays
            fail-closed with no substitution.
        exclude_user_dirs: True for SHARED-ONLY agents — ``users/`` paths are
            dropped from BOTH manifests before merging (never pushed, never
            pulled, never scrubbed: a stray satellite-side copy is simply
            ignored rather than deleted from the user's disk).

    Returns a ``MergePlan`` (per-file ``FileAction`` list + the ``to_scrub``
    isolation deletes). Pure / side-effect-free — the caller executes it.
    """
    # NB: explicit `is not None` (NOT `or`) so a caller can pass an EMPTY set to
    # drop config/ from push-only (owner-tier config write-back) — `set() or X`
    # would wrongly fall back to the default.
    push_only_p = (
        push_only_dirs if push_only_dirs is not None
        else set(DEFAULT_PUSH_ONLY_PREFIXES)
    )
    push_only_s = (
        push_only_segments if push_only_segments is not None
        else set(DEFAULT_PUSH_ONLY_SEGMENTS)
    )
    base = base or {}
    tombstones = tombstones or {}
    if author_of is None:
        author_of = lambda _p: None  # noqa: E731

    if exclude_user_dirs:
        # Drop users/ from EVERY input (manifests + carried state) so the
        # union loop below can never manufacture an action for one.
        local = [e for e in local if not e.path.startswith("users/")]
        remote = [r for r in remote
                  if not str(r.get("path", "")).startswith("users/")]
        base = {k: v for k, v in base.items() if not k.startswith("users/")}
        tombstones = {k: v for k, v in tombstones.items()
                      if not k.startswith("users/")}
    local_map = {e.path: e for e in local}
    remote_map = {r["path"]: r for r in remote}
    # The satellite tree is "alive" iff it reported ANY file. An EMPTY manifest with
    # a non-empty base = a wiped / re-imaged satellite (or a delete-EVERYTHING) → the
    # wipe-guard: never attribute deletes from absence, re-push instead. A partial
    # delete (even a big subtree) still leaves the rest of the tree reporting, so it
    # is correctly attributed — no volume heuristic.
    sat_tree_present = bool(remote_map)

    actions: list[FileAction] = []
    to_scrub: list[str] = []

    # Iterate the union of all known keys so a both-absent file with only a stale
    # base/tombstone row still gets cleaned up.
    all_paths = set(local_map) | set(remote_map) | set(base) | set(tombstones)
    for path in all_paths:
        lentry = local_map.get(path)
        rentry = remote_map.get(path)
        P = lentry.hash if lentry else None
        S = rentry.get("hash") if rentry else None
        Pm = lentry.mtime if lentry else 0.0
        Sm = float(rentry.get("mtime") or 0.0) if rentry else 0.0
        brow = base.get(path)
        B = brow[0] if brow else None
        T = tombstones.get(path)  # deleted_at_mtime or None

        # 1. Satellite-authoritative session state (.claude/projects, .codex/
        #    sessions) — the platform owns none of it: never push/pull/delete.
        if _is_satellite_owned(path):
            continue

        # 2. Platform-authoritative push-only paths — push on mismatch, scrub
        #    satellite-only extras; never pull / conflict / tombstone. The set is
        #    caller-controlled: `.claude`/`.codex`/`.credentials` (regenerated /
        #    minted by the platform) are always here; `config/` is here EXCEPT for
        #    owner-tier sessions, which curate it on the remote and sync it back.
        if _is_push_only(path, push_only_p, push_only_s):
            if P is not None:
                if S is None or P != S:
                    actions.append(FileAction(path, "push", base_hash=P))
            elif S is not None:
                # Satellite-only extra under a push-only path. Scrubbing is an
                # ISOLATION measure, so it applies where holding the file is an
                # anomaly: USER-paired machines (config/ never syncs to
                # non-owners) and the platform-regenerated SEGMENTS
                # (.claude/.codex/.credentials) everywhere. On an ADMIN-SHARED
                # machine (target_username None) a prefix extra (config/) is
                # legitimate pending curation — e.g. just written by an
                # owner-tier session whose identity THIS merge doesn't carry
                # (idle fingerprint sweep, reconnect catch-up, a concurrent
                # non-owner warmup) — so leave it for a merge with a real
                # owner-tier identity to pull. Scrubbing here deleted
                # owner-written config/context files.
                if (target_username is not None
                        or _is_push_only(path, set(), push_only_s)):
                    to_scrub.append(path)
            continue

        # 3. Isolation scrub: another user's data / agent-scope creds that leaked
        #    onto a user-paired satellite — delete it there, NEVER pull (it would
        #    be a cross-user write). The local manifest already excludes these, so
        #    they only ever appear satellite-side.
        if (S is not None and target_username is not None
                and _is_other_user_or_sensitive(path, target_username)):
            to_scrub.append(path)
            continue

        # 4. The 3-way merge. The pull gate's ``can_write_back`` identity: the
        #    machine owner on user-paired targets; the session's authenticated
        #    human on admin-shared targets (``target_username is None``). An
        #    orphaned owner ("") substitutes nothing — fail-closed.
        action = _resolve_merge(
            path, P, S, Pm, Sm, B, T,
            clock_offset=clock_offset, author_of=author_of,
            satellite_user=satellite_user,
            target_role=target_role,
            target_username=(
                target_username if target_username is not None else session_username
            ) or "",
            satellite_tree_present=sat_tree_present,
        )
        if action is not None:
            actions.append(action)

    return MergePlan(actions=actions, to_scrub=to_scrub)


def prepare_outgoing_files(
    agent_dir: Path, paths: list[str],
) -> list[dict]:
    """Prepare files for pushing to satellite.

    Returns list of file_push message dicts. Files > MAX_CHUNK_SIZE are
    split into write_chunk actions.
    """
    messages = []
    for rel_path in paths:
        file_path = agent_dir / rel_path
        if not file_path.exists():
            continue

        # Symlinks don't round-trip — skip.
        if file_path.is_symlink():
            continue

        # Validate path stays within agent_dir
        try:
            file_path.resolve().relative_to(agent_dir.resolve())
        except ValueError:
            logger.warning("Path traversal attempt: %s", rel_path)
            continue

        try:
            content = file_path.read_bytes()
            file_hash = f"sha256:{hashlib.sha256(content).hexdigest()}"

            if len(content) <= MAX_CHUNK_SIZE:
                messages.append({
                    "action": "write",
                    "path": rel_path,
                    "content_b64": base64.b64encode(content).decode(),
                    "hash": file_hash,
                })
            else:
                # Chunked transfer
                offset = 0
                chunk_idx = 0
                while offset < len(content):
                    chunk = content[offset:offset + MAX_CHUNK_SIZE]
                    messages.append({
                        "action": "write_chunk",
                        "path": rel_path,
                        "chunk_index": chunk_idx,
                        "total_chunks": (len(content) + MAX_CHUNK_SIZE - 1) // MAX_CHUNK_SIZE,
                        "content_b64": base64.b64encode(chunk).decode(),
                        "hash": file_hash if offset + MAX_CHUNK_SIZE >= len(content) else "",
                    })
                    offset += MAX_CHUNK_SIZE
                    chunk_idx += 1
        except (OSError, PermissionError) as e:
            logger.warning("Cannot read %s for sync: %s", rel_path, e)
            continue

    return messages


def _unlink_quiet(p: Path) -> None:
    """Best-effort unlink — used to reap a ``.partial`` left by a failed write."""
    with contextlib.suppress(OSError):
        p.unlink(missing_ok=True)


def prune_empty_parents(file_path: Path, root: Path) -> None:
    """After a file delete, remove now-empty parent directories bottom-up.

    Sync has no rmdir action (manifests/snapshots are file-based), so a tree
    delete propagates per-file deletes and leaves the empty dir skeleton
    behind — this mirrors the deleter's intent instead. Never touches: the
    sync root, its depth-1 children (the platform skeleton — workspace/
    users/ config/ knowledge/ …), or per-user roots (users/<name>). The
    first non-empty directory stops the walk (rmdir refuses it)."""
    try:
        root_r = Path(root).resolve()
        cur = Path(file_path).parent.resolve()
    except OSError:
        return
    while True:
        try:
            rel = cur.relative_to(root_r)
        except ValueError:
            return
        depth = len(rel.parts)
        if depth < 2 or (depth == 2 and rel.parts[0] == "users"):
            return
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def apply_incoming_file(
    agent_dir: Path,
    path: str,
    action: str,
    content_b64: str | None = None,
    *,
    final_chunk: bool = False,
) -> None:
    """Apply a file change received from the satellite.

    Validates that the path stays within agent_dir. Writes are atomic:
    `write` uses write-to-.partial + fsync + rename. `write_chunk` appends
    to .partial until `final_chunk=True`, then renames atomically.
    """
    target = (agent_dir / path).resolve()

    # Path traversal check
    try:
        target.relative_to(agent_dir.resolve())
    except ValueError:
        logger.warning("Path traversal in incoming file: %s", path)
        return

    partial = target.with_suffix(target.suffix + ".partial")

    if action == "delete":
        if target.exists():
            target.unlink()
        prune_empty_parents(target, agent_dir)
    elif action == "mkdir":
        target.mkdir(parents=True, exist_ok=True)
    elif action == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        # "" is a real ZERO-BYTE write (b64 of empty content); only None means
        # "no inline content" (large-file flow — the caller pulls instead).
        if content_b64 is not None:
            content = base64.b64decode(content_b64)
            try:
                with open(partial, "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(partial, target)
            except OSError:
                # EDQUOT / ENOSPC / I/O: drop the orphan .partial so it can't
                # leak quota — it's manifest-invisible and never swept, so a
                # left-behind partial would consume the agent's quota and wedge
                # every retry. Re-raise so the caller leaves sync_state
                # un-advanced and the satellite re-sends once space frees up.
                _unlink_quiet(partial)
                raise
    elif action == "write_chunk":
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if content_b64:
                content = base64.b64decode(content_b64)
                with open(partial, "ab") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
            if final_chunk and partial.exists():
                os.replace(partial, target)
        except OSError:
            # A failed chunk wedges the whole chunked transfer; drop the
            # accumulated .partial rather than leak it (same reasoning as above).
            _unlink_quiet(partial)
            raise


def _hash_file(path: Path) -> str:
    """Compute sha256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"
