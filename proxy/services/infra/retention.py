"""Session retention + disk cleanup for the local sandboxes and the paired
machines.

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
     reseeds from DB history (core/session/history_seed.py). Chats pinned
     to a paired machine are flagged the same way and their files are
     named to the machine's satellite (``_pass_remote_sessions``);
     direct-LLM chats are never candidates. Under the same
     knob and window, check verdicts (CHECKS.md) older than N days are
     deleted (``_pass_check_verdicts``).
  B. Orphans — fixed 7-day grace, always on: session files no DB row points
     at (deleted chats, CLI subagent sidechains, meeting agent sessions).
     Nothing can ever resume them.
  C. Codex junk — always on: ``logs_*.sqlite*`` + ``.codex/.tmp`` contents in
     idle homes. ``state_*.sqlite`` (thread state) and ``sessions/`` are kept.
  D. MCP tarball cache GC — ``services/mcp_tarball.gc()`` (500 MB quota +
     7-day stale) existed but was never scheduled; the sweep calls it.
  Recover bin (``recover-bin-blobs``) — always on: rows whose bytes are
     gone or incomplete, then bytes no row names after a day of grace
     (``storage/files/recover_bin_store.py``).
  Shares (``share-snapshots``, then ``stale-shares``) — always on: a
     revoked chat share's copy 7 days after the revoke, then revoked and
     declined share rows 30 days after the revoke or the decision
     (``share_store.STALE_SHARE_DAYS``).
  Reply reports (``reply-reports``) — always on: a report a year after it
     was filed, open or closed, its chat there or not
     (``db_reply_reports.KEEP_DAYS``).
  Working files (``working-files``) — its own toggle and window: uploads and
     the browser's screenshots (``services/infra/working_files.py``).
  History (``history``, on the loop before the machines' pass) — off unless
     set: an agent's own window, and the platform's chat rows
     (``services/infra/history_retention.py``).

On a remote machine only the CLI transcripts age out (``_pass_remote_sessions``:
the chats pinned to it are flagged like local ones, and the satellite
removes the files no row and no live session names). Never touched: a
workspace outside those folders, plans, ``state_*.sqlite``,
token/credential dirs.

Safety model: live sessions are excluded via a snapshot of the real runtime
registries (the remote layer's sessions with their threads, Direct LLM's
pool, the ``_session_security`` contexts of those sessions,
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
import posixpath
import re
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from core import layout, placement
from core.layers.codex import session_text
from core.session import external_identity
from core.session.external_identity import external_home_of
from services.infra import external_retention, history_retention, working_files
from services.infra.agent_dirs import agent_dirs
from storage import database as task_store

logger = logging.getLogger("otodock")

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
# A machine's answer to the retention frame: the walk of its agent trees
# and the deletes, off its loop.
_REMOTE_RETENTION_TIMEOUT_S = 60.0

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
# A session's home changed this recently is a start in flight (``_pass_session_homes``).
_HOME_FRESH_S = 3600


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
    # The person's histories were archived beside the store, not in the
    # folder: they go with the archive.
    from services.infra import git_writer
    git_writer.forget_archived(username)


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
    busy_homes: set = field(default_factory=set)          # (agent, username)
    # Resolved ``external_home`` paths of live external sessions (phone
    # callers) — the caller-data pass never touches a tree in use.
    busy_external_homes: set = field(default_factory=set)
    # The sessions the machines run for this platform (the remote layer's):
    # a compute session's home sits in the platform's folder.
    machine_session_ids: set = field(default_factory=set)


def _build_live_snapshot() -> LiveSnapshot:
    """Snapshot every in-use signal. Must run on the event loop (the
    registries are loop-owned); the threaded sweep gets the frozen copy.

    Liveness truth = the registries of who holds a session (the remote
    layer's, a machine's sessions with their threads, and Direct LLM's
    pool) and _active_pumps. Deliberately NOT
    core.session.session_state._sessions (append-only, persisted — would mark
    everything live) and NOT the raw _session_security map: contexts are
    cleared on clean close but survive a proxy restart for up to 24h, so
    blanket-trusting them marked every recently-used home busy and starved
    the junk pass. A context contributes the
    (agent, username) busy-home only when its session is in a live registry.
    """
    snap = LiveSnapshot()
    from core.events.stream_pump import _active_pumps
    from core.layers.direct.session import _direct_sessions
    from core.session.session_state import _session_security

    # Direct-LLM sessions have no files of their own, but an external caller
    # on a Direct agent still has a live caller tree.
    snap.session_ids.update(_direct_sessions.keys())
    # A session a machine runs, with its thread: a compute machine's session
    # keeps its files in the platform's folder, so its home is busy here.
    remote_ids, _by_machine = _remote_live_ids()
    snap.session_ids |= remote_ids
    snap.codex_thread_ids |= remote_ids
    for sid in list(snap.session_ids):
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
    from core.remote.remote_execution import get_remote_layer
    snap.machine_session_ids.update(getattr(get_remote_layer(), "_sessions", {}) or {})
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
    """Remove one session file, or a folder an engine keeps for a session,
    beneath the agent's real folder with no link followed (a planted link at any
    component is left where it is, and is no error); counted under
    ``count_key``, its bytes under ``bytes_key``."""
    from services.infra import safe_fs
    try:
        st = path.lstat()
    except OSError:
        return
    tree = stat.S_ISDIR(st.st_mode)
    size = _tree_bytes(path) if tree else st.st_size
    if not dry_run:
        try:
            _remove_beneath(path, tree=tree)
        except safe_fs.SymlinkRefused:
            logger.debug("retention: %s sits behind a link: left in place", path)
            return
        except OSError as e:
            stats["errors"] += 1
            logger.warning("retention: failed to delete %s: %s", path, e)
            return
    stats[count_key] += 1
    stats[bytes_key] += size


def _remove_beneath(path: Path, *, tree: bool) -> None:
    """``path`` removed beneath the agent's real folder that holds it
    (``core.agent_folder``), or beneath the agents root for a folder of no
    agent there, its ``rel`` from the path as listed, never resolved; one
    outside both (the legacy transcript beside the proxy's own home) is
    removed in place. A path whose parts are refused (a ``..`` part, a
    relative path) raises and removes nothing."""
    import shutil

    from core.agent_folder import beneath_agent
    from services.infra import safe_fs
    try:
        root, rel = beneath_agent(path)
    except safe_fs.EscapeRefused:
        try:
            root, rel = config.AGENTS_DIR, safe_fs.rel_under(str(path), config.AGENTS_DIR)
        except safe_fs.EscapeRefused:
            if tree:
                shutil.rmtree(path)
            else:
                path.unlink()
            return
    if tree:
        safe_fs.rmtree_beneath(root, rel)
    else:
        safe_fs.unlink_beneath(root, rel)


def _unlink_with_companions(layers, path: Path, stats: dict, count_key: str,
                            bytes_key: str, dry_run: bool) -> None:
    """A session file and what its engine keeps beside it (Claude's
    ``<id>/`` folder): the file counts, the folder adds its bytes."""
    _unlink(path, stats, count_key, bytes_key, dry_run)
    for extra in {c for layer in layers for c in layer.session_file_companions(path)}:
        try:
            if not stat.S_ISDIR(extra.lstat().st_mode):
                continue
        except OSError:
            continue
        before = stats[count_key]
        _unlink(extra, stats, count_key, bytes_key, dry_run)
        stats[count_key] = before


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
        try:
            home = _home_for_chat(chat["agent"], chat.get("user_sub") or "")
        except ValueError:
            # A row naming no agent directory (``get_agent_dir``'s barrier):
            # nothing of it is on disk to age out; the sweep goes on.
            logger.warning("retention: chat %s names no agent directory (%r)",
                           str(chat["id"])[:8], chat["agent"])
            continue
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
    layers = list(get_all_layers().values())
    for cid, files in planned:
        if cid not in flagged:
            continue
        for f in files:
            _unlink_with_companions(layers, f, stats, "session_files_deleted", "bytes_freed", dry_run)
    stats["chats_flagged"] += len(flagged)


def _remote_layer_for_sweep():
    from core.session.session_manager import _get_remote_layer
    return _get_remote_layer()


def _remote_live_ids() -> tuple[set[str], dict[str, set[str]]]:
    """The session and thread ids live on machines (the remote layer's
    registry, grace-held sessions included): all of them, and by machine."""
    all_ids: set[str] = set()
    by_machine: dict[str, set[str]] = {}
    for info in list(_remote_layer_for_sweep()._sessions.values()):
        ids = {info.session_id}
        if info.resume_handle:
            ids.add(info.resume_handle)
        all_ids |= ids
        # A compute session beside the platform has its files in the
        # platform's folder: its ids are live here, and no machine is asked.
        if getattr(info, "files_synced", True):
            by_machine.setdefault(info.machine_id, set()).update(ids)
    return all_ids, by_machine


def _int_of(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# A keep list past this many ids is not sent: the frame would run to several
# MB and the machine's socket closes on an oversized one, which would drop the
# machine at every sweep. Its files stay and it counts as unreached.
_KEEP_MAX = 100_000


async def _ask_machine_retention(cm, machine_id: str, keep: set[str], dry_run: bool) -> dict:
    """One machine's row of ``remote_machines``: what it holds after the
    pass and what went, or ``reached: False`` when it did not answer or
    its keep list is past ``_KEEP_MAX``."""
    row: dict = {
        "machine_id": machine_id, "name": cm.satellite_name(machine_id),
        "files": 0, "bytes": 0, "deleted": 0, "deleted_bytes": 0,
        "candidates": [], "reached": False,
    }
    if len(keep) > _KEEP_MAX:
        logger.warning(
            "retention: machine %s (%s) is not asked: its keep list holds %d ids, "
            "over the %d one frame carries; its session files stay",
            machine_id[:8], row["name"], len(keep), _KEEP_MAX,
        )
        return row
    try:
        ack = await cm.send_command(machine_id, {
            "type": "session_files_retention", "keep": sorted(keep),
            "min_age_s": ORPHAN_GRACE_S, "dry_run": dry_run,
        }, timeout=_REMOTE_RETENTION_TIMEOUT_S)
    except RuntimeError as e:
        logger.info("retention: machine %s did not answer the session files "
                    "retention: %s", machine_id[:8], e)
        return row
    if ack.get("status", "ok") != "ok":
        return row
    for key in ("files", "bytes", "deleted", "deleted_bytes"):
        row[key] = _int_of(ack.get(key))
    cands = ack.get("candidates")
    row["candidates"] = [str(c) for c in cands][:50] if isinstance(cands, list) else []
    row["reached"] = True
    return row


async def _pass_remote_sessions(days: int, enabled: bool, live: LiveSnapshot,
                                stats: dict, dry_run: bool,
                                gone_refs: frozenset[str] | set[str] = frozenset()) -> None:
    """The sweep's remote half, on the loop after the threaded passes: the
    chats pinned to a machine age out like local ones (Pass A's guards and
    flag; the live ids are the remote layer's), then every connected
    machine is asked to remove the transcripts no row and no live session
    names (``session_files_retention``, a per-machine keep list, the orphan
    grace by the machine's clock), so an aged chat's old files and the
    orphans go together. An offline machine keeps its files until a sweep
    finds it connected: the flag is set either way
    and the chat's next open reseeds, as a local aged chat's does."""
    from core.remote.satellite_connection import get_connection_manager
    from core.session.session_manager import get_all_layers
    from storage.pg import run_db

    cm = get_connection_manager()
    live_ids, live_by_machine = _remote_live_ids()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # The machines holding an aged chat this sweep could not reach.
    unreached: set[str] = set()
    # The ids of the chats this pass ages: a dry run writes nothing, so they
    # leave the keep lists by hand, as the flagging takes them out of the rows
    # a real run then reads (the machines name the same candidates either way).
    aged_refs: set[str] = set(gone_refs)
    if enabled:
        fileless = [path for path, layer in get_all_layers().items()
                    if layer.capabilities.behaviour.rebuilds_history_from_db]
        candidates = await run_db(task_store.get_remote_retention_candidate_chats,
                                  cutoff, fileless_paths=fileless)
        planned: list[str] = []
        if candidates:
            prot_sids, prot_tids = await run_db(task_store.get_protected_session_refs, cutoff)
            for chat in candidates:
                sid = chat.get("session_id") or ""
                tid = chat.get("codex_thread_id") or ""
                if sid and (sid in prot_sids or sid in live_ids):
                    continue
                if tid and (tid in prot_tids or tid in live_ids):
                    continue
                if chat["id"] in live.pump_chat_ids:
                    continue
                planned.append(chat["id"])
                aged_refs.update(ref for ref in (sid, tid) if ref)
                unreached.add(chat["execution_target"])
        if planned:
            flagged = planned if dry_run else await run_db(
                task_store.flag_chats_for_retention, planned, cutoff)
            stats["chats_flagged"] += len(flagged)
            stats["remote_chats_flagged"] += len(flagged)
    # A compute machine beside the platform keeps no files of its own:
    # nothing to ask it. One on another host keeps its chats' engine records.
    machines = [mid for mid in cm.get_connected_machines() if not cm.shares_folders(mid)]
    if machines:
        # The ids by the machine their row names. The rows that name no place
        # (``""``: the scheduled tasks' continued sessions, rows minted before
        # the column was stamped) ride every machine's keep list; a row the
        # sandbox minted (``local``) has its files here and rides none.
        unowned: set[str] = set()
        by_machine: dict[str, set[str]] = {}
        for target, refs in (await run_db(task_store.get_session_refs_by_target)).items():
            if not target:
                unowned.update(refs)
            elif (mid := placement.machine_of(target)):
                by_machine.setdefault(mid, set()).update(refs)
        if dry_run:
            unowned -= aged_refs
            for refs in by_machine.values():
                refs -= aged_refs
        rows = await asyncio.gather(*(
            _ask_machine_retention(
                cm, mid,
                unowned | by_machine.get(mid, set()) | live_by_machine.get(mid, set()),
                dry_run,
            ) for mid in machines
        ))
        for row in rows:
            stats["remote_machines"].append(row)
            if not row["reached"]:
                unreached.add(row["machine_id"])
                continue
            unreached.discard(row["machine_id"])
            stats["remote_session_files_bytes"] = (
                (stats["remote_session_files_bytes"] or 0) + row["bytes"])
            stats["remote_session_files_deleted"] += row["deleted"]
            stats["remote_bytes_freed"] += row["deleted_bytes"]
            if dry_run and row["candidates"]:
                logger.info(
                    "retention: machine %s (%s) would remove %d session files, e.g. %s",
                    row["machine_id"][:8], row["name"], len(row["candidates"]),
                    ", ".join(row["candidates"][:5]),
                )
    stats["remote_machines_unreached"] = len(unreached)


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
                    _unlink_with_companions(layers, f, stats, "orphans_deleted",
                                            "orphan_bytes", dry_run)


RECORD_MAX_AGE_S = 30 * 86400


def _session_live(session_id: str) -> bool:
    """A terminal runs under this id, or a layer holds it (headless,
    compute, paired): the registries' own single-key lookups, safe from the
    sweep's thread."""
    from core.session import interactive_registry, session_manager
    return (interactive_registry.get(session_id) is not None
            or session_manager.live_session(session_id) is not None)


def _codex_session_leftovers(codex: Path, live: LiveSnapshot, now: float, *,
                             sessions_known: bool = True) -> list[Path]:
    """The own files of a session that is not live, in any home (a session
    closed after a proxy restart has no record of the folder it wrote);
    a thread's record after a month, unless its thread runs (its next
    resume then gets its text once more). Young files stay (``JUNK_MIN_AGE_S``
    at the caller): a session that starts writes them before it registers.
    No session's own file goes while ``sessions_known`` is False: after a
    restart, until every compute machine the session index names has sent
    its first complete report (``compute_bootstrap.waiting``)."""
    out: list[Path] = []
    for f in codex.glob("*"):
        m = session_text.SESSION_FILE_RE.fullmatch(f.name)
        if m is None or not f.is_file():
            continue
        if m.group(2) == session_text.RECORD_SUFFIX:
            if m.group(1) not in live.codex_thread_ids and _mtime_older_than(f, RECORD_MAX_AGE_S, now):
                out.append(f)
        elif sessions_known and not _session_live(m.group(1)):
            out.append(f)
    return out


def _pass_session_homes(live: LiveSnapshot, stats: dict, dry_run: bool) -> None:
    """The homes of sessions that are gone (``core/sandbox/session_home``) in
    the engine folders the platform shares with a compute machine beside it:
    each holds a sign-in file. Never inside the pool's boot grace, when a
    session a restart left running is not back in the registries yet. One
    answer says a session is gone (``subscription_pool.session_gone``: no
    registry holds it, terminals included, and its machine is here and has
    reported since this start), and its stored binding goes with its home.
    A home that changed in the last hour is left: a start makes its home
    long before its binding row, and touches it when it starts again in
    one. Each home is judged by itself: one that cannot be removed keeps no
    other. A machine with its own copy sweeps its own."""
    import psycopg

    from core.sandbox import session_home
    from services.engines import subscription_pool
    if subscription_pool.within_boot_grace():
        return
    now = time.time()
    for _agent, _username, home in iter_local_homes():
        for name in (".claude", ".codex"):
            own = home / name / session_home.OWN
            if own.is_symlink() or not own.is_dir():
                continue
            for sid in sorted(os.listdir(own)):
                try:
                    if (not session_home.valid_id(sid)
                            or not _mtime_older_than(own / sid, _HOME_FRESH_S, now)
                            or not subscription_pool.session_gone(sid)):
                        continue
                    if dry_run:
                        stats["session_homes_removed"] += 1
                    elif session_home.drop(home / name, sid, ()):
                        stats["session_homes_removed"] += 1
                        subscription_pool.drop_ghost_binding(sid)
                except (OSError, psycopg.Error):
                    stats["errors"] = stats.get("errors", 0) + 1
                    logger.warning("retention: the home of session %s in %s is left", sid[:8], own,
                                   exc_info=True)


def _codex_tmp_tree(agent: str, codex: Path) -> tuple[list[Path], list[str]]:
    """The files and links under ``<codex>/.tmp`` (as paths, for the safe
    ``_unlink``) and its folders below it (rels beneath the agent's real
    folder, ``core.agent_folder.agent_root``, for ``safe_fs.rmdir_beneath``),
    from one walk that enters no link: a ``.tmp`` that is a link, or sits
    behind one below the agent's folder, lists nothing."""
    from core.agent_folder import beneath_agent
    from services.infra import safe_fs
    files: list[Path] = []
    dirs: list[str] = []
    try:
        root, base = beneath_agent(codex, agent)
        rel = posixpath.join(base, ".tmp")
        for step in safe_fs.walk_beneath(root, rel, onerror=lambda exc: None):
            here = codex / ".tmp" / step.rel[len(rel):].lstrip("/")
            files.extend(here / name for name in step.files + step.symlinks)
            dirs.extend(step.path(name) for name in step.dirs)
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("retention: %s/.tmp not walked", codex, exc_info=True)
        return [], []
    return files, dirs


def _pass_codex_junk(live: LiveSnapshot, stats: dict, dry_run: bool) -> None:
    from services.remote import compute_bootstrap
    now = time.time()
    sessions_known = not compute_bootstrap.waiting()
    for agent, username, home in iter_local_homes():
        codex = home / ".codex"
        if not codex.is_dir():
            continue
        targets = _codex_session_leftovers(codex, live, now, sessions_known=sessions_known)
        # A temporary sign-in file a killed save left (Codex 0.169 saves by
        # ``.auth-XXXXXX`` and a rename): the credentials. Past the in-flight
        # guard below it goes whatever runs (a save takes a moment).
        targets.extend(f for f in codex.glob(".auth-*") if f.is_file() and not f.is_symlink())
        idle = (agent, username) not in live.busy_homes
        tmp_dirs: list[str] = []
        if idle:
            # Telemetry/log DBs (incl. -wal/-shm sidecars). state_*.sqlite is
            # Codex's thread state — small and load-bearing, never touched.
            targets.extend(codex.glob("logs_*.sqlite*"))
            tmp_files, tmp_dirs = _codex_tmp_tree(agent, codex)
            targets.extend(tmp_files)
        for f in targets:
            if _mtime_older_than(f, JUNK_MIN_AGE_S, now):
                _unlink(f, stats, "codex_junk_files", "codex_junk_bytes", dry_run)
        if not dry_run:
            # Prune emptied staging subdirs, deepest first; keep .tmp itself.
            # A folder that is not empty stays.
            from core.agent_folder import agent_root
            from services.infra import safe_fs
            for rel in sorted(tmp_dirs, key=lambda r: r.count("/"), reverse=True):
                with contextlib.suppress(OSError):
                    safe_fs.rmdir_beneath(agent_root(agent), rel)


def _pass_tarball_gc(stats: dict, dry_run: bool) -> None:
    if dry_run:
        return
    from services.mcp import mcp_tarball
    stats["tarball_bytes"] += mcp_tarball.gc()


def _pass_orphan_histories(stats: dict, dry_run: bool) -> None:
    """The histories of agents that are gone, and old set-aside copies
    (services/infra/git_writer.py). An agent's delete removes its histories
    itself: this is for a removal that failed. An agent counts as gone only
    with no row AND no folder, so an agents folder that is not mounted, or
    cannot be read, removes nothing."""
    from services.infra import git_writer
    from storage.agents import agent_store
    removed = git_writer.remove_orphans(set(agent_store.get_agent_slugs()), dry_run=dry_run)
    if removed:
        logger.info("retention: %s%d histor%s of agents that are gone or set-aside copies past their keep",
                    "would remove " if dry_run else "removed ", removed, "y" if removed == 1 else "ies")


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


def _pass_recover_bin_blobs(stats: dict, dry_run: bool) -> None:
    """The recover bin's rows and bytes agree: a row whose bytes are gone or
    cut short is dropped (a container recreated without the bin's volume
    left the rows behind), then bytes no row names, older than a day."""
    from storage.files import recover_bin_store
    stats["recover_bin_rows_pruned"] += recover_bin_store.prune_missing_blobs(dry_run=dry_run)
    stats["recover_bin_orphans_deleted"] += recover_bin_store.prune_orphan_blobs(dry_run=dry_run)


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


def _pass_notebook_entries(stats: dict, dry_run: bool) -> None:
    """Notebook entries (``storage/notebook/``): every agent keeps its newest 500,
    deleted ones counted, whatever the chat retention toggle says (a size
    bound, like the recover bin's)."""
    from services.notebook.entries import KEEP_PER_AGENT
    from storage.notebook import db_notebook
    stats["notebook_entries_pruned"] += db_notebook.prune_per_agent(
        KEEP_PER_AGENT, dry_run=dry_run,
    )


def _pass_reply_reports(stats: dict, dry_run: bool) -> None:
    """Reports of replies (``storage/db_reply_reports.py``): each one goes
    ``KEEP_DAYS`` after it was filed, whatever the chat retention toggle
    says; a report outlives its chat, so its age is what bounds it."""
    from storage import db_reply_reports
    stats["reply_reports_deleted"] += db_reply_reports.delete_older_than(
        db_reply_reports.KEEP_DAYS, dry_run=dry_run,
    )


def _pass_tool_listings(stats: dict, dry_run: bool) -> None:
    """What a tool's program answered, saved for a person's next session
    (``services/mcp/tool_listing.py``): every row nobody used for 30 days
    goes, whatever the chat retention toggle says (a capture can name an
    account's email)."""
    from services.mcp import tool_listing
    stats["tool_listings_deleted"] += tool_listing.sweep_unused(dry_run=dry_run)


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
        "session_homes_removed": 0,
        "codex_junk_bytes": 0,
        "tarball_bytes": 0,
        "partials_deleted": 0,
        "partial_bytes": 0,
        "quota_projects_reclaimed": 0,
        "mcp_autoupdate_rows_deleted": 0,
        "share_snapshots_deleted": 0,
        "shares_deleted": 0,
        "notebook_entries_pruned": 0,
        "reply_reports_deleted": 0,
        "tool_listings_deleted": 0,
        "recover_bin_rows_pruned": 0,
        "recover_bin_orphans_deleted": 0,
        # The machines (``_pass_remote_sessions``, on the loop after this).
        # ``remote_session_files_bytes`` is what the reached machines still
        # hold, None until one answered.
        "remote_chats_flagged": 0,
        "remote_session_files_deleted": 0,
        "remote_bytes_freed": 0,
        "remote_session_files_bytes": None,
        "remote_machines_unreached": 0,
        "remote_machines": [],
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
        # The working-files window and the history window (their own
        # settings, read in the pass).
        **dict.fromkeys(working_files.STAT_KEYS, 0),
        **dict.fromkeys(history_retention.STAT_KEYS, 0),
        "working_files_grace_until": "",
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
        ("session-homes", lambda: _pass_session_homes(live, stats, dry_run)),
        ("working-files", lambda: working_files.run_pass(stats, dry_run)),
        ("tarball-gc", lambda: _pass_tarball_gc(stats, dry_run)),
        ("orphan-partials", lambda: _pass_orphan_partials(stats, dry_run)),
        ("orphan-histories", lambda: _pass_orphan_histories(stats, dry_run)),
        ("orphan-quota-projects", lambda: _pass_orphan_quota_projects(stats, dry_run)),
        ("mcp-autoupdate-log", lambda: _pass_mcp_autoupdate_log(stats, dry_run)),
        ("recover-bin-blobs", lambda: _pass_recover_bin_blobs(stats, dry_run)),
        ("share-snapshots", lambda: _pass_share_snapshots(stats, dry_run)),
        ("stale-shares", lambda: _pass_stale_shares(stats, dry_run)),
        ("notebook-entries", lambda: _pass_notebook_entries(stats, dry_run)),
        ("reply-reports", lambda: _pass_reply_reports(stats, dry_run)),
        ("tool-listings", lambda: _pass_tool_listings(stats, dry_run)),
        ("offboarded-archives", lambda: _pass_offboarded_archives(stats, dry_run)),
    ])
    for name, fn in passes:
        try:
            fn()
        except Exception:
            stats["errors"] += 1
            logger.exception("retention: pass '%s' failed", name)
    stats["duration_ms"] = int((time.monotonic() - started) * 1000)
    total = (stats["bytes_freed"] + stats["orphan_bytes"]
             + stats["codex_junk_bytes"] + stats["tarball_bytes"]
             + stats["caller_bytes_freed"] + stats["offboarded_bytes"]
             + stats["working_files_bytes"])
    logger.info(
        "retention: sweep done in %sms "
        "(dry_run=%s, enabled=%s): "
        "%s chats flagged, "
        "%s session files, "
        "%s orphans, "
        "%s codex-junk files, "
        "%s callers forgotten, "
        "%s phone chats, "
        "%s share rows, "
        "%s recover-bin rows without bytes, "
        "%s working files, "
        "%s bytes total",
        stats['duration_ms'], dry_run, enabled, stats['chats_flagged'],
        stats['session_files_deleted'], stats['orphans_deleted'], stats['codex_junk_files'],
        stats['callers_forgotten'], stats['phone_chats_deleted'], stats['shares_deleted'],
        stats['recover_bin_rows_pruned'],
        stats['working_uploads_binned'] + stats['working_screenshots_deleted'],
        total
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
        gone_refs: set[str] = set()
        try:
            gone_refs = await history_retention.run_pass(stats, dry_run)
        except Exception:
            stats["errors"] += 1
            logger.exception("retention: pass 'history' failed")
        try:
            await _pass_remote_sessions(days, enabled, snapshot, stats, dry_run, gone_refs)
        except Exception:
            stats["errors"] += 1
            logger.exception("retention: pass 'remote-sessions' failed")
        if stats["remote_machines"]:
            logger.info(
                "retention: %d session files (%d bytes) removed on %d of %d machines, "
                "%d remote chats flagged",
                stats["remote_session_files_deleted"], stats["remote_bytes_freed"],
                sum(1 for r in stats["remote_machines"] if r["reached"]),
                len(stats["remote_machines"]), stats["remote_chats_flagged"],
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
    per 24h. The first run lands once the pool's boot grace is over: the
    pass over the sessions' homes passes over every home inside it, and the
    next run is a day later, which an install that restarts every day never
    reaches."""
    if time.monotonic() - _last_run < _SWEEP_INTERVAL_S and _last_run:
        return
    from services.engines import subscription_pool
    if subscription_pool.within_boot_grace():
        return
    await run_sweep()


# ---------------------------------------------------------------------------
# Storage usage readout (admin card)
# ---------------------------------------------------------------------------

def _tree_bytes(path: Path) -> int:
    """The bytes under ``path`` (``storage_quota.dir_usage``, the one walk
    that counts a folder: beneath the agent's real folder, no link followed)."""
    from services.infra import storage_quota
    return storage_quota.dir_usage(path)[0]


def compute_storage_usage() -> dict:
    """Byte totals for the admin Storage & Retention card. Sync — call via
    asyncio.to_thread (walks the agents tree)."""
    session_files = 0
    codex_junk = 0
    from services.infra import storage_quota
    for _agent, _username, home in iter_local_homes():
        # A home's engine folders are the sessions' to write: counted
        # beneath the agent's real folder with no link followed.
        session_files += storage_quota.dir_usage(home / ".claude" / "projects", match="*.jsonl",
                                                 max_depth=2)[0]
        session_files += storage_quota.dir_usage(home / ".codex" / "sessions", match="*.jsonl")[0]
        codex = home / ".codex"
        codex_junk += storage_quota.dir_usage(codex, match="logs_*.sqlite*", max_depth=1)[0]
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
    from services.media import upload_staging

    # What the machines held at the last sweep (their answers); None until
    # a sweep reached one.
    remote_bytes = (last_sweep or {}).get("remote_session_files_bytes")
    return {
        "agents_bytes": _tree_bytes(Path(config.AGENTS_DIR)),
        "session_files_bytes": session_files,
        "remote_session_files_bytes": remote_bytes if isinstance(remote_bytes, int) else None,
        "codex_junk_bytes": codex_junk,
        "recover_bin_bytes": _tree_bytes(Path(config.RECOVER_BIN_DIR)),
        "sessions_dir_bytes": _tree_bytes(Path(config.SESSIONS_DIR)),
        "upload_staging_bytes": upload_staging.staged_bytes_total(),
        "logs_bytes": logs,
        "retention": {
            "enabled": settings_enabled(),
            "days": settings_days(),
            "last_sweep": last_sweep,
        },
    }
