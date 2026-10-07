"""Session retention + disk cleanup for LOCAL sandboxed agents.

A platform install's disk is dominated by on-disk CLI session state, not user
content: Claude session JSONLs (``.claude/projects``), Codex rollouts
(``.codex/sessions``), Codex internal telemetry (``logs_*.sqlite`` — observed
130 MB in one agent home) and plugin staging (``.codex/.tmp``), plus orphaned
session files that ``delete_chat`` never removes. This module bounds all of it
with a daily sweep (wired into app.py's registry sweep loop) and powers the
admin "Storage & Retention" card (run-now + usage readout).

Passes (the full list and its order are ``_run_sweep_sync``'s):
  A. Aged chats — governed by the admin knob (``session_retention_enabled``,
     default ON; ``session_retention_days``, default 180): local chats
     untouched for N days lose their session files and are flagged
     ``pending_history_seed='retention'`` — the next turn transparently
     reseeds from DB history (core/session/history_seed.py). Remote/
     satellite chats and direct-LLM are never candidates. Under the same
     knob and window, check verdicts (CHECKS.md) older than N days are
     deleted (``_pass_check_verdicts``).
  B. Orphans — fixed 7-day grace, always on: session files no DB row points
     at (deleted chats, CLI subagent sidechains, meeting agent sessions).
     Nothing can ever resume them.
  C. Codex junk — always on: ``logs_*.sqlite*`` + ``.codex/.tmp`` contents in
     idle homes. ``state_*.sqlite`` (thread state) and ``sessions/`` are kept.
  D. MCP tarball cache GC — ``services/mcp_tarball.gc()`` (500 MB quota +
     7-day stale) existed but was never scheduled; the sweep calls it.
  Shares (``share-snapshots``, then ``stale-shares``) — always on: a
     revoked chat share's copy 7 days after the revoke, then revoked and
     declined share rows 30 days after the revoke or the decision
     (``share_store.STALE_SHARE_DAYS``).

Never touched: anything on a remote satellite, workspaces/user content,
plans, ``state_*.sqlite``, token/credential dirs.

Safety model: live sessions are excluded via a snapshot of the real runtime
registries (cli ``_persistent_sessions``, codex ``_codex_sessions`` incl.
their exact ``config_dir``/``thread_id``, ``_session_security`` contexts,
``_active_pumps``) — NOT ``session_state._sessions``, which is append-only.
Session ids shared across chat rows (continue_session delegation chains) are
protected by a "referenced by any fresh chat" set. Every file age-checked;
flagging chats never bumps ``updated_at`` (chat-list order is preserved).
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import config
from core.session import external_identity
from core.session.external_identity import external_home_of
from services.infra import external_retention
from services.infra.agent_dirs import agent_dirs
from storage import database as task_store
from core import layout

logger = logging.getLogger("claude-proxy")

DEFAULT_DAYS = 180
MIN_DAYS = 7
# Orphan files (no DB reference at all) get a short fixed grace by mtime —
# independent of the admin knob; nothing can resume them.
ORPHAN_GRACE_S = 7 * 86400
# Never touch a file modified within the last hour (in-flight safety).
JUNK_MIN_AGE_S = 3600
# Orphaned ``*.partial`` reaper: a live write renames its .partial within
# seconds, so anything older is abandoned (e.g. a write killed by EDQUOT). The
# manifest skips .partial, so these are never re-synced or otherwise cleaned.
PARTIAL_ORPHAN_AGE_S = 3600
_SWEEP_INTERVAL_S = 86400

_last_run: float = 0.0
_sweep_lock = asyncio.Lock()

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings_enabled() -> bool:
    """Retention Pass A toggle. Unset means ON (the platform-settings
    unset-''-is-default convention); only an explicit '0' disables. On managed
    installs the operator pins this via OTODOCK_FORCED_SETTINGS (the overlay in
    storage.get_platform_setting makes it immutable + the admin UI hides it)."""
    return task_store.get_platform_setting("session_retention_enabled") != "0"


def settings_days() -> int:
    raw = task_store.get_platform_setting("session_retention_days")
    try:
        days = int(raw) if raw else DEFAULT_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return max(MIN_DAYS, days)


# The .offboarded/ archive (services/agents/offboarding_transfer.py): a
# removed person's trees, kept OFFBOARDED_DEFAULT_DAYS after they were
# archived unless the admin says otherwise (0 keeps them for ever).
OFFBOARDED_DEFAULT_DAYS = 180
OFFBOARDED_MAX_DAYS = 3650
# A username as the platform mints it (``db_users._make_username_slug``):
# nothing else under the archive is ever swept or purged.
_ARCHIVE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# A purge waits while the archive of a just-removed person is still due
# to be written (the offboarding timer, plus a margin).
_ARCHIVE_GRACE_S = 600


class ArchiveBusy(RuntimeError):
    """The person's archive is still being written."""


def offboarded_enabled() -> bool:
    """Unset means ON; only an explicit '0' disables the archive sweep."""
    return task_store.get_platform_setting("offboarded_retention_enabled") != "0"


def offboarded_days_from(raw: str | None) -> int:
    """The archive retention a stored value means: unset is the default, 0
    keeps every archive for ever. A value outside 0..OFFBOARDED_MAX_DAYS or
    not a number (a forced setting, an older write: the settings route
    refuses one) keeps every archive too, never the default: a "-1" meant as
    "for ever" must not delete anything."""
    if not raw:
        return OFFBOARDED_DEFAULT_DAYS
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = -1
    if 0 <= days <= OFFBOARDED_MAX_DAYS:
        return days
    logger.warning("offboarded_retention_days %r is not a number of days from 0 to %d: "
                   "every archive is kept", raw, OFFBOARDED_MAX_DAYS)
    return 0


def offboarded_days() -> int:
    """Days an archive is kept after it was written (``offboarded_days_from``)."""
    return offboarded_days_from(task_store.get_platform_setting("offboarded_retention_days"))


def _archive_dirname() -> str:
    from services.agents.offboarding_transfer import ARCHIVE_DIRNAME
    return ARCHIVE_DIRNAME


def _archive_names() -> list[str]:
    """The archived people: the directories directly under the archive whose
    name is a username (links and anything else are left alone). Raises when
    the archive root cannot be entered safely (a link standing in for it)."""
    from services.infra import safe_fs
    walk = safe_fs.walk_beneath(config.AGENTS_DIR, _archive_dirname())
    try:
        top = next(walk)
        names = [n for n in top.dirs if _ARCHIVE_NAME_RE.match(n)]
        top.dirs[:] = []
    except FileNotFoundError:
        return []
    finally:
        walk.close()
    return names


def _archive_contents(username: str) -> tuple[list[str], int]:
    """An archive's agent folders and its size in bytes, walked beneath the
    agents root without following a link. A folder that cannot be entered
    (a tree the agent left unreadable) is skipped, so the size is a floor."""
    from services.infra import safe_fs
    rel = f"{_archive_dirname()}/{username}"
    agents: list[str] = []
    total = 0
    for step in safe_fs.walk_beneath(config.AGENTS_DIR, rel, onerror=lambda _e: None):
        if step.rel == rel:
            agents = list(step.dirs)
        for name in step.files:
            with contextlib.suppress(OSError):
                total += os.stat(name, dir_fd=step.dirfd, follow_symlinks=False).st_size
    return agents, total


def _parse_iso(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def list_offboarded_archives() -> dict:
    """The admin list: every archive with its agent folders, size, when the
    person was removed and archived, and when the sweep deletes it ("" when
    it never will: the setting keeps it, or its date is unknown)."""
    enabled, days = offboarded_enabled(), offboarded_days()
    retired = {r["username"]: r for r in task_store.list_retired_usernames()}
    archives = []
    for name in _archive_names():
        try:
            agents, size = _archive_contents(name)
        except OSError:
            logger.warning("retention: the archive of %s could not be measured", name, exc_info=True)
            agents, size = [], None
        row = retired.get(name) or {}
        archived = _parse_iso(row.get("archived_at") or "")
        purge_after = ""
        if enabled and days > 0 and archived:
            purge_after = (archived + timedelta(days=days)).isoformat()
        archives.append({"username": name, "agents": agents, "bytes": size,
                         "retired_at": row.get("retired_at") or "",
                         "archived_at": row.get("archived_at") or "",
                         "purge_after": purge_after})
    return {"archives": archives, "retention": {"enabled": enabled, "days": days}}


def purge_offboarded_archive(username: str) -> dict:
    """Delete one person's archive now (the admin action). ValueError for a
    name that is not a username, FileNotFoundError when there is no such
    archive, ArchiveBusy while it is still being written."""
    from services.agents.offboarding_transfer import ARCHIVE_AFTER_S
    if not _ARCHIVE_NAME_RE.match(username or ""):
        raise ValueError("not a username")
    if username not in _archive_names():
        raise FileNotFoundError(username)
    row = next((r for r in task_store.list_retired_usernames() if r["username"] == username), None)
    if row and not row.get("archived_at"):
        retired = _parse_iso(row.get("retired_at") or "")
        if retired and (datetime.now(timezone.utc) - retired).total_seconds() \
                < ARCHIVE_AFTER_S + _ARCHIVE_GRACE_S:
            raise ArchiveBusy(username)
    try:
        _agents, size = _archive_contents(username)
    except FileNotFoundError:
        return {"username": username, "bytes": 0}
    _delete_archive(username)
    return {"username": username, "bytes": size}


def _open_writable(parent_fd: int, name: str) -> None:
    """Give the proxy's own directory ``name`` (under ``parent_fd``) its
    owner bits back, without following a link: opened as a path handle with
    O_NOFOLLOW and changed through that handle."""
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd)
    try:
        os.chmod(f"/proc/self/fd/{fd}", 0o700)
    finally:
        os.close(fd)


def _make_tree_writable(rel: str) -> None:
    """Every directory of the tree at ``rel`` owner-writable and enterable
    (a module cache is read-only, a tree can be left unreadable), each one
    before the walk enters it; links are never followed."""
    from services.infra import safe_fs
    parent_rel, _, leaf = rel.rpartition("/")
    for step in safe_fs.walk_beneath(config.AGENTS_DIR, parent_rel):
        if step.rel == parent_rel:
            if leaf in step.dirs:
                _open_writable(step.dirfd, leaf)
            step.dirs[:] = []
    for step in safe_fs.walk_beneath(config.AGENTS_DIR, rel, onerror=lambda _e: None):
        for name in step.dirs:
            with contextlib.suppress(OSError):
                _open_writable(step.dirfd, name)


def _delete_archive(username: str) -> None:
    """Remove one archive beneath the agents root; a tree a permission stops
    is made owner-writable first and removed again."""
    from services.infra import safe_fs
    rel = f"{_archive_dirname()}/{username}"
    try:
        safe_fs.rmtree_beneath(config.AGENTS_DIR, rel, missing_ok=True)
    except PermissionError:
        _make_tree_writable(rel)
        safe_fs.rmtree_beneath(config.AGENTS_DIR, rel, missing_ok=True)


def _pass_offboarded_archives(stats: dict, dry_run: bool) -> None:
    """Delete the archives written more than ``offboarded_days`` ago. An
    archive whose date is unknown (no retired row, or no ``archived_at``) is
    never deleted here; an admin purges it by hand."""
    if not offboarded_enabled():
        return
    days = offboarded_days()
    if days <= 0:
        return
    try:
        names = _archive_names()
    except OSError:
        stats["errors"] += 1
        logger.warning("retention: the archive of removed people was not swept "
                       "(its folder could not be entered safely)", exc_info=True)
        return
    if not names:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    retired = {r["username"]: r for r in task_store.list_retired_usernames()}
    for name in names:
        archived = _parse_iso((retired.get(name) or {}).get("archived_at") or "")
        if archived is None or archived > cutoff:
            continue
        try:
            _agents, size = _archive_contents(name)
            if not dry_run:
                _delete_archive(name)
                logger.info("retention: deleted the archive of %s (%d bytes, archived %s)",
                            name, size, archived.date().isoformat())
        except OSError:
            stats["errors"] += 1
            logger.warning("retention: the archive of %s was not deleted", name, exc_info=True)
            continue
        stats["offboarded_deleted"] += 1
        stats["offboarded_bytes"] += size


# ---------------------------------------------------------------------------
# Live snapshot — what is in use RIGHT NOW
# ---------------------------------------------------------------------------

@dataclass
class LiveSnapshot:
    session_ids: set = field(default_factory=set)
    codex_thread_ids: set = field(default_factory=set)
    pump_chat_ids: set = field(default_factory=set)
    codex_config_dirs: set = field(default_factory=set)   # resolved .codex paths
    busy_homes: set = field(default_factory=set)          # (agent, username)
    # Resolved ``external_home`` paths of live external sessions (phone
    # callers) — the caller-data pass never touches a tree in use.
    busy_external_homes: set = field(default_factory=set)


def _build_live_snapshot() -> LiveSnapshot:
    """Snapshot every in-use signal. Must run on the event loop (the
    registries are loop-owned); the threaded sweep gets the frozen copy.

    Liveness truth = the warm-daemon registries (cli _persistent_sessions +
    codex _codex_sessions) and _active_pumps. Deliberately NOT
    core.session.session_state._sessions (append-only, persisted — would mark
    everything live) and NOT the raw _session_security map: contexts are
    cleared on clean close but survive a proxy restart for up to 24h, so
    blanket-trusting them marked every recently-used home busy and starved
    the junk pass. A context contributes the
    (agent, username) busy-home only when its session is in a live registry.
    """
    snap = LiveSnapshot()
    from core.layers.cli.session import _persistent_sessions
    from core.layers.codex.session import _codex_sessions
    from core.layers.direct.session import _direct_sessions
    from core.session.session_state import _session_security
    from core.events.stream_pump import _active_pumps

    snap.session_ids.update(_persistent_sessions.keys())
    # Direct-LLM sessions have no files of their own, but an external caller
    # on a Direct agent still has a live caller tree.
    snap.session_ids.update(_direct_sessions.keys())
    for sid, sess in _codex_sessions.items():
        snap.session_ids.add(sid)
        tid = getattr(sess, "thread_id", None)
        if tid:
            snap.codex_thread_ids.add(tid)
        cfg_dir = getattr(sess, "config_dir", "") or ""
        if cfg_dir:
            try:
                snap.codex_config_dirs.add(str(Path(cfg_dir).resolve()))
            except OSError:
                snap.codex_config_dirs.add(str(cfg_dir))
    for sid in snap.session_ids:
        ctx = _session_security.get(sid)
        agent = getattr(ctx, "agent", "") or "" if ctx else ""
        if agent:
            snap.busy_homes.add((agent, getattr(ctx, "username", "") or ""))
        home = external_home_of(ctx) if ctx else ""
        if home:
            try:
                snap.busy_external_homes.add(str(Path(home).resolve()))
            except OSError:
                snap.busy_external_homes.add(home)
    snap.pump_chat_ids.update(_active_pumps.keys())
    return snap


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def iter_local_homes(agent: str = "") -> Iterator[tuple[str, str, Path]]:
    """Yield (agent, username, home) for every local agent home — one
    agent's when ``agent`` is given.

    Bounded iteration over the known shapes
    ``AGENTS_DIR/<agent>/users/<username>``, ``AGENTS_DIR/<agent>/workspace``
    (agent-scope sessions: tasks/phone without a user) and the external
    callers' trees ``AGENTS_DIR/<agent>/externals/<channel>/<slug>`` (+ the
    ``_ephemeral/<sid>`` ones; reported with username "" — the CLI state
    under them is bounded like any other home) — never a tree walk.
    """
    agents_dir = Path(config.AGENTS_DIR)
    if not agents_dir.is_dir():
        return
    for agent_dir in sorted(agent_dirs(agents_dir)):
        if agent and agent_dir.name != agent:
            continue
        users_dir = agent_dir / layout.USERS
        if users_dir.is_dir():
            for user_home in sorted(users_dir.iterdir()):
                if user_home.is_dir():
                    yield agent_dir.name, user_home.name, user_home
        ws = agent_dir / layout.WORKSPACE
        if ws.is_dir():
            yield agent_dir.name, "", ws
        ext = agent_dir / external_identity.EXTERNALS_DIRNAME
        if ext.is_dir():
            for channel_dir in sorted(ext.iterdir()):
                if not channel_dir.is_dir():
                    continue
                for home in sorted(channel_dir.iterdir()):
                    if not home.is_dir():
                        continue
                    if home.name == external_identity.EPHEMERAL_DIRNAME:
                        for eph in sorted(home.iterdir()):
                            if eph.is_dir():
                                yield agent_dir.name, "", eph
                    else:
                        yield agent_dir.name, "", home


def _home_for_chat(agent: str, user_sub: str) -> Path:
    """Mirror of the can_resume_session home recipe (cli/layer.py): user-scope
    chats live under users/<username>; sentinel subs (task::<agent>, phone)
    have no users row -> agent-scope workspace home."""
    username = task_store.get_username_by_sub(user_sub) or "" if user_sub else ""
    base = config.get_agent_dir(agent)
    return layout.user_dir(base, username) if username else (base / layout.WORKSPACE)


def _unlink(path: Path, stats: dict, count_key: str, bytes_key: str,
            dry_run: bool) -> None:
    try:
        size = path.lstat().st_size
    except OSError:
        return
    if not dry_run:
        try:
            path.unlink()
        except OSError as e:
            stats["errors"] += 1
            logger.warning(f"retention: failed to delete {path}: {e}")
            return
    stats[count_key] += 1
    stats[bytes_key] += size


def _mtime_older_than(path: Path, age_s: float, now: float) -> bool:
    try:
        return (now - path.lstat().st_mtime) >= age_s
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------

def _pass_aged_chats(days: int, live: LiveSnapshot, stats: dict,
                     dry_run: bool) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # An engine that rebuilds every turn from chat_messages keeps no session
    # files, so its chats are never candidates — the registry says which
    # (reached function-locally: services reach the registry that way).
    from core.session.session_manager import get_all_layers
    fileless = [
        path for path, layer in get_all_layers().items()
        if layer.capabilities.behaviour.rebuilds_history_from_db
    ]
    candidates = task_store.get_retention_candidate_chats(cutoff, fileless_paths=fileless)
    if not candidates:
        return
    # Session files are shared across chat rows (continue_session delegation
    # reuses one session id on a fresh chat per round) — protect any id a
    # FRESH chat still references.
    prot_sids, prot_tids = task_store.get_protected_session_refs(cutoff)
    planned: list[tuple[str, list[Path]]] = []
    for chat in candidates:
        sid = chat.get("session_id") or ""
        tid = chat.get("codex_thread_id") or ""
        if sid and (sid in prot_sids or sid in live.session_ids):
            continue
        if tid and (tid in prot_tids or tid in live.codex_thread_ids):
            continue
        if chat["id"] in live.pump_chat_ids:
            continue
        home = _home_for_chat(chat["agent"], chat.get("user_sub") or "")
        # Each engine names the files of one chat under a home
        # (``ExecutionLayer.chat_session_files``; the id-shape guard is the
        # sweep's).
        sid_ok = sid if sid and _UUID_RE.match(sid) else ""
        tid_ok = tid if tid and _UUID_RE.match(tid) else ""
        files = [f for layer in get_all_layers().values()
                 for f in layer.chat_session_files(home, sid_ok, tid_ok)]
        # Planned even when files are already missing: the chat can't resume
        # either way, and the digest is the right outcome on next open.
        planned.append((chat["id"], files))
    if not planned:
        return
    # The flag goes FIRST, with the candidate cutoff: a chat resumed since the
    # candidate query is not flagged and keeps its files; only a flagged
    # chat's files are removed, so no chat ends up with a session id and no
    # session behind it.
    if dry_run:
        flagged = {cid for cid, _ in planned}
    else:
        flagged = set(task_store.flag_chats_for_retention(
            [cid for cid, _ in planned], cutoff))
    for cid, files in planned:
        if cid not in flagged:
            continue
        for f in files:
            _unlink(f, stats, "session_files_deleted", "bytes_freed", dry_run)
    stats["chats_flagged"] += len(flagged)


def _pass_check_verdicts(days: int, stats: dict, dry_run: bool) -> None:
    """Check verdicts (CHECKS.md) older than the same window: the evidence
    of turns whose session files just went; small rows, but a list that
    never ends otherwise. Same knob as Pass A, so an install that keeps
    every chat keeps every verdict."""
    from storage.checks import db_checks
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    stats["check_verdicts_deleted"] += db_checks.delete_verdicts_before(cutoff, dry_run=dry_run)


def _pass_orphans(live: LiveSnapshot, stats: dict, dry_run: bool) -> None:
    """Session files no chat row and no live session references — each
    engine enumerates its own under a home with the id it belongs to
    (``ExecutionLayer.iter_session_files``); the id-shape, reference and
    grace guards are the sweep's."""
    from core.session.session_manager import get_all_layers
    refs = task_store.get_all_session_refs()
    refs |= live.session_ids | live.codex_thread_ids
    now = time.time()
    layers = list(get_all_layers().values())
    for _agent, _username, home in iter_local_homes():
        for layer in layers:
            for f, ref in layer.iter_session_files(home):
                if not _UUID_RE.match(ref) or ref in refs:
                    continue
                if _mtime_older_than(f, ORPHAN_GRACE_S, now):
                    _unlink(f, stats, "orphans_deleted", "orphan_bytes", dry_run)


def _pass_codex_junk(live: LiveSnapshot, stats: dict, dry_run: bool) -> None:
    now = time.time()
    for agent, username, home in iter_local_homes():
        codex = home / ".codex"
        if not codex.is_dir():
            continue
        if (agent, username) in live.busy_homes:
            continue
        try:
            resolved = str(codex.resolve())
        except OSError:
            resolved = str(codex)
        if resolved in live.codex_config_dirs:
            continue
        # Telemetry/log DBs (incl. -wal/-shm sidecars). state_*.sqlite is
        # Codex's thread state — small and load-bearing, never touched.
        targets: list[Path] = list(codex.glob("logs_*.sqlite*"))
        tmp = codex / ".tmp"
        if tmp.is_dir():
            targets.extend(p for p in tmp.rglob("*")
                           if p.is_file() or p.is_symlink())
        for f in targets:
            if _mtime_older_than(f, JUNK_MIN_AGE_S, now):
                _unlink(f, stats, "codex_junk_files", "codex_junk_bytes", dry_run)
        if tmp.is_dir() and not dry_run:
            # Prune emptied staging subdirs, deepest first; keep .tmp itself.
            subdirs = sorted((p for p in tmp.rglob("*") if p.is_dir()),
                             key=lambda p: len(p.parts), reverse=True)
            for d in subdirs:
                with contextlib.suppress(OSError):
                    d.rmdir()


def _pass_tarball_gc(stats: dict, dry_run: bool) -> None:
    if dry_run:
        return
    from services.mcp import mcp_tarball
    stats["tarball_bytes"] += mcp_tarball.gc()


def _pass_orphan_partials(stats: dict, dry_run: bool) -> None:
    """Reap ``*.partial`` files left under the agent tree by a write that died
    mid-flight (e.g. EDQUOT under a full quota). See PARTIAL_ORPHAN_AGE_S."""
    root = config.AGENTS_DIR
    if not root.exists():
        return
    now = time.time()
    for p in root.rglob("*.partial"):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if _mtime_older_than(p, PARTIAL_ORPHAN_AGE_S, now):
            _unlink(p, stats, "partials_deleted", "partial_bytes", dry_run)


def _pass_mcp_autoupdate_log(stats: dict, dry_run: bool) -> None:
    """Trim the automatic MCP-update run log (keep ~90 days / newest 500 rows)."""
    if dry_run:
        return
    from storage.mcp import mcp_autoupdate_store
    stats["mcp_autoupdate_rows_deleted"] += mcp_autoupdate_store.prune()


def _pass_orphan_quota_projects(stats: dict, dry_run: bool) -> None:
    """Drift insurance for hard storage quotas: zero the limit on any project
    whose agent no longer exists (delete_agent already reclaims, so this only
    catches a failed reclaim or pre-feature rows). Project rows are kept as
    tombstones so their XFS IDs are never reused. No-op unless hard enforcement
    is active."""
    from services.infra import storage_quota
    if not storage_quota.hard_enabled():
        return
    try:
        from storage.agents import agent_store
        live = set(agent_store.get_agent_slugs())
        for row in storage_quota.list_projects():
            if row.get("agent_slug") not in live:
                if not dry_run:
                    storage_quota.reclaim_project(row["scope_key"])
                stats["quota_projects_reclaimed"] += 1
    except Exception:
        stats["errors"] += 1
        logger.exception("retention: orphan quota-project reap failed")


# ---------------------------------------------------------------------------
# Sweep entry points
# ---------------------------------------------------------------------------

def _pass_share_snapshots(stats: dict, dry_run: bool) -> None:
    """Chat snapshots (SHARING.md): a revoked share's copy goes seven days
    after the revoke; a directory with no share row at all goes at once."""
    from datetime import timedelta
    from services.sharing import chat_snapshot
    from storage.sharing import share_store
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    for share in share_store.list_revoked_before(cutoff):
        if not dry_run:
            chat_snapshot.remove(share)
            share_store.clear_snapshot_ref(share["id"])
        stats["share_snapshots_deleted"] = stats.get("share_snapshots_deleted", 0) + 1
    if not dry_run:
        n = chat_snapshot.sweep_orphans(share_store.list_all_share_ids())
        stats["share_snapshots_deleted"] = stats.get("share_snapshots_deleted", 0) + n


def _pass_stale_shares(stats: dict, dry_run: bool) -> None:
    """Share rows (SHARING.md "The ``shares`` table"): a revoked or a
    declined one is deleted 30 days after its revoke or its decision.
    Runs after ``share-snapshots``, which finds a revoked chat share's copy
    through its row."""
    from storage.sharing import share_store
    stats["shares_deleted"] += share_store.delete_stale_shares(dry_run=dry_run)


def _run_sweep_sync(days: int, enabled: bool, live: LiveSnapshot,
                    dry_run: bool) -> dict:
    started = time.monotonic()
    stats: dict = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "retention_days": days,
        "retention_pass_skipped": not enabled,
        "chats_flagged": 0,
        "check_verdicts_deleted": 0,
        "session_files_deleted": 0,
        "bytes_freed": 0,
        "orphans_deleted": 0,
        "orphan_bytes": 0,
        "codex_junk_files": 0,
        "codex_junk_bytes": 0,
        "tarball_bytes": 0,
        "partials_deleted": 0,
        "partial_bytes": 0,
        "quota_projects_reclaimed": 0,
        "mcp_autoupdate_rows_deleted": 0,
        "share_snapshots_deleted": 0,
        "shares_deleted": 0,
        # Caller data (external routes) — services/infra/external_retention.py
        "callers_forgotten": 0,
        "callers_busy_skipped": 0,
        "ephemeral_reaped": 0,
        "phone_chats_deleted": 0,
        "call_log_rows_deleted": 0,
        "caller_bytes_freed": 0,
        # The archive of removed people (its own setting, read in the pass).
        "offboarded_deleted": 0,
        "offboarded_bytes": 0,
        "errors": 0,
    }
    passes = [
        # Caller data FIRST (its own toggle + window: external_retention_enabled
        # / _days; the ephemeral reap inside always runs): an aged phone chat
        # is deleted whole here — Pass A would otherwise NULL its session id
        # (losing the file link) before this pass sees it.
        ("caller-data", lambda: external_retention.run_pass(
            live.busy_external_homes, live.session_ids, live.pump_chat_ids,
            stats, dry_run)),
    ]
    if enabled:
        passes.append(("aged-chats", lambda: _pass_aged_chats(days, live, stats, dry_run)))
        passes.append(("check-verdicts", lambda: _pass_check_verdicts(days, stats, dry_run)))
    passes.extend([
        ("orphans", lambda: _pass_orphans(live, stats, dry_run)),
        ("codex-junk", lambda: _pass_codex_junk(live, stats, dry_run)),
        ("tarball-gc", lambda: _pass_tarball_gc(stats, dry_run)),
        ("orphan-partials", lambda: _pass_orphan_partials(stats, dry_run)),
        ("orphan-quota-projects", lambda: _pass_orphan_quota_projects(stats, dry_run)),
        ("mcp-autoupdate-log", lambda: _pass_mcp_autoupdate_log(stats, dry_run)),
        ("share-snapshots", lambda: _pass_share_snapshots(stats, dry_run)),
        ("stale-shares", lambda: _pass_stale_shares(stats, dry_run)),
        ("offboarded-archives", lambda: _pass_offboarded_archives(stats, dry_run)),
    ])
    for name, fn in passes:
        try:
            fn()
        except Exception:
            stats["errors"] += 1
            logger.exception(f"retention: pass '{name}' failed")
    stats["duration_ms"] = int((time.monotonic() - started) * 1000)
    total = (stats["bytes_freed"] + stats["orphan_bytes"]
             + stats["codex_junk_bytes"] + stats["tarball_bytes"]
             + stats["caller_bytes_freed"] + stats["offboarded_bytes"])
    logger.info(
        f"retention: sweep done in {stats['duration_ms']}ms "
        f"(dry_run={dry_run}, enabled={enabled}): "
        f"{stats['chats_flagged']} chats flagged, "
        f"{stats['session_files_deleted']} session files, "
        f"{stats['orphans_deleted']} orphans, "
        f"{stats['codex_junk_files']} codex-junk files, "
        f"{stats['callers_forgotten']} callers forgotten, "
        f"{stats['phone_chats_deleted']} phone chats, "
        f"{stats['shares_deleted']} share rows, "
        f"{total} bytes total"
    )
    return stats


def _read_settings() -> tuple[bool, int]:
    return settings_enabled(), settings_days()


async def run_sweep(*, dry_run: bool = False) -> dict:
    """Run one full sweep (the run-now endpoint + the daily tick). The live
    snapshot is built on the event loop; the settings read, the file/DB work
    and the stats write run off it. The lock serializes run-now against the
    daily tick."""
    from storage.pg import run_db
    global _last_run
    async with _sweep_lock:
        enabled, days = await run_db(_read_settings)
        snapshot = _build_live_snapshot()
        stats = await asyncio.to_thread(
            _run_sweep_sync, days, enabled, snapshot, dry_run,
        )
        if not dry_run:
            _last_run = time.monotonic()
            try:
                await run_db(
                    task_store.set_platform_setting,
                    "session_retention_last_sweep", json.dumps(stats),
                )
            except Exception:
                logger.exception("retention: failed to persist sweep stats")
        return stats


async def maybe_run_daily() -> None:
    """Called every 60s from app.py's registry sweep loop; runs at most once
    per 24h. First run lands ~60s after boot (cheap when there's nothing
    to do)."""
    if time.monotonic() - _last_run < _SWEEP_INTERVAL_S and _last_run:
        return
    await run_sweep()


# ---------------------------------------------------------------------------
# Storage usage readout (admin card)
# ---------------------------------------------------------------------------

def _tree_bytes(path: Path) -> int:
    total = 0
    try:
        if not path.exists():
            return 0
        for p in path.rglob("*"):
            try:
                st = p.lstat()
            except OSError:
                continue
            if not (st.st_mode & 0o170000) == 0o040000:  # not a directory
                total += st.st_size
    except OSError:
        pass
    return total


def compute_storage_usage() -> dict:
    """Byte totals for the admin Storage & Retention card. Sync — call via
    asyncio.to_thread (walks the agents tree)."""
    session_files = 0
    codex_junk = 0
    for _agent, _username, home in iter_local_homes():
        for f in (home / ".claude" / "projects").glob("*/*.jsonl"):
            with contextlib.suppress(OSError):
                session_files += f.lstat().st_size
        for f in (home / ".codex" / "sessions").rglob("*.jsonl"):
            with contextlib.suppress(OSError):
                session_files += f.lstat().st_size
        codex = home / ".codex"
        for f in codex.glob("logs_*.sqlite*"):
            with contextlib.suppress(OSError):
                codex_junk += f.lstat().st_size
        codex_junk += _tree_bytes(codex / ".tmp")

    base = Path(config.BASE_DIR)
    logs = 0
    for f in base.glob("proxy.log*"):
        with contextlib.suppress(OSError):
            logs += f.lstat().st_size

    last_sweep = None
    raw = task_store.get_platform_setting("session_retention_last_sweep")
    if raw:
        try:
            last_sweep = json.loads(raw)
        except (ValueError, TypeError):
            last_sweep = None

    # Chunked uploads in flight sit outside every agent tree (and every
    # quota) until they complete: shown here so an admin sees the disk they hold.
    from api.media import uploads

    return {
        "agents_bytes": _tree_bytes(Path(config.AGENTS_DIR)),
        "session_files_bytes": session_files,
        "codex_junk_bytes": codex_junk,
        "recover_bin_bytes": _tree_bytes(Path(config.RECOVER_BIN_DIR)),
        "sessions_dir_bytes": _tree_bytes(Path(config.SESSIONS_DIR)),
        "upload_staging_bytes": uploads.staged_bytes_total(),
        "logs_bytes": logs,
        "retention": {
            "enabled": settings_enabled(),
            "days": settings_days(),
            "last_sweep": last_sweep,
        },
    }
