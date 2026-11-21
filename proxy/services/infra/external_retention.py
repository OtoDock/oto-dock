"""Caller-data retention — what an external route leaves behind, and for how long.

An external caller (a phone caller who is not a platform user) leaves three
kinds of data on the platform: their private tree
(``agents/<agent>/externals/<channel>/<slug>/`` — notes, files, the CLI state
of their sessions), the phone conversations (``chats.source_type='phone'``:
rows, messages, on-disk session files) and the call-log rows. They age out
TOGETHER under one admin knob (``external_retention_enabled`` — unset means
ON; ``external_retention_days``, default 90, minimum 1), on the daily
retention sweep (``services/infra/retention.py`` calls :func:`run_pass`) and
on demand ("Forget all caller data now" — :func:`forget_all`).

Rules:

- A caller tree's age is its NEWEST mtime (a read-only call touches the
  root at warmup, so "activity" includes calls that wrote nothing).
- A tree whose session is live right now is never touched (busy = the
  ``external_home`` of a registered security context whose session is in a
  layer registry — the same liveness truth the session sweep uses).
- Ephemeral trees (``_ephemeral/<session id>``) are pruned at hangup; the
  sweep reaps leftovers older than 24 h (a proxy crash mid-call) regardless
  of the toggle.
- ``externals/`` never syncs to a satellite (``core/remote/file_sync.py``),
  so removal is a tree removal plus the purge of the file bookkeeping rows
  (``file_author`` / ``file_tombstones``) under the path — no tombstones are
  written: nothing downstream could resurrect the files.
- Every listing, age, size and removal opens beneath the agent's real folder
  (``core/agent_folder``) with no link followed at any part: only the server
  writes ``externals/`` and its channels, and a link there is never entered.
  A folder that is missing lists nothing, and one that is refused is skipped
  with one WARNING, so one agent never stops the pass for the others.
- Nothing lands in the Recover bin: caller data is deleted, not parked.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from core import agent_folder
from core.session import external_identity
from services.infra import safe_fs, storage_quota
from services.infra.agent_dirs import agent_dirs
from storage import database as task_store
from storage.files import file_author_store, file_tombstones_store
from storage.identity import external_identity_store
from storage.phone import phone_call_log_store

logger = logging.getLogger("otodock")

DEFAULT_DAYS = 90
MIN_DAYS = 1
MAX_DAYS = 3650
#: Leftover ephemeral trees (the hangup prune never ran) are reaped after this.
EPHEMERAL_GRACE_S = 24 * 3600

_UUID_GLOB = "[0-9a-f]*"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings_enabled() -> bool:
    """Unset means ON (the platform-settings convention); only an explicit
    '0' disables the aging (the ephemeral reap always runs)."""
    return task_store.get_platform_setting("external_retention_enabled") != "0"


def settings_days() -> int:
    raw = task_store.get_platform_setting("external_retention_days")
    try:
        days = int(raw) if raw else DEFAULT_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return min(MAX_DAYS, max(MIN_DAYS, days))


def prune_cutoff() -> str | None:
    """ISO cutoff for the call-log insert-time prune (None = keep everything)."""
    if not settings_enabled():
        return None
    return (datetime.now(timezone.utc) - timedelta(days=settings_days())).isoformat()


# ---------------------------------------------------------------------------
# Trees
# ---------------------------------------------------------------------------

def _agent_dirs() -> Iterator[tuple[str, Path]]:
    agents_dir = Path(config.AGENTS_DIR)
    if not agents_dir.is_dir():
        return
    for agent_dir in sorted(agent_dirs(agents_dir)):
        if (agent_dir / external_identity.EXTERNALS_DIRNAME).is_dir():
            yield agent_dir.name, agent_dir


def _folders(agent: str, folder: Path) -> list[str]:
    """The real folders directly in ``folder``, listed beneath the agent's
    real folder: a link is never listed or entered. A missing folder holds
    none, and a refused one holds none with one WARNING."""
    try:
        root, rel = agent_folder.beneath_agent(folder, agent)
        walk = safe_fs.walk_beneath(root, rel)
        try:
            return list(next(walk).dirs)
        finally:
            walk.close()
    except (FileNotFoundError, StopIteration):
        return []
    except OSError:
        logger.warning("caller-data: %s not listed", folder, exc_info=True)
        return []


def iter_caller_homes() -> Iterator[tuple[str, str, str, Path]]:
    """``(agent, channel, slug, home)`` for every durable caller tree.
    Bounded three-level iteration (``externals/<channel>/<slug>``)."""
    for agent, agent_dir in _agent_dirs():
        externals = agent_dir / external_identity.EXTERNALS_DIRNAME
        for channel in _folders(agent, externals):
            for slug in _folders(agent, externals / channel):
                if slug != external_identity.EPHEMERAL_DIRNAME:
                    yield agent, channel, slug, externals / channel / slug


def iter_ephemeral_homes() -> Iterator[tuple[str, Path]]:
    """``(agent, home)`` for every ephemeral tree (``externals/<channel>/_ephemeral/<sid>``)."""
    for agent, agent_dir in _agent_dirs():
        externals = agent_dir / external_identity.EXTERNALS_DIRNAME
        for channel in _folders(agent, externals):
            eph = externals / channel / external_identity.EPHEMERAL_DIRNAME
            for name in _folders(agent, eph):
                yield agent, eph / name


def iter_external_homes() -> Iterator[tuple[str, Path]]:
    """Every external home (durable + ephemeral) — the session sweep's
    orphan / Codex-junk passes bound the CLI state under them too."""
    for agent, _channel, _slug, home in iter_caller_homes():
        yield agent, home
    yield from iter_ephemeral_homes()


def newest_mtime(home: Path) -> float:
    """The tree's last activity: the newest mtime of the root dir (the warmup
    touches it) or of any FILE under it (a write bumps its file). Inner
    directory mtimes are ignored on purpose — they change when the session
    sweep deletes an orphaned session file inside the tree, which is not
    caller activity. Read beneath the agent's real folder with no link
    followed: a home that is a link counts its own time only, and a folder
    below it that cannot be entered is passed over, as a glob passes it. A
    tree that is gone is ``0.0``. A tree whose age cannot be read reads as
    active now, with one WARNING: a caller's data never goes to the recover
    bin, so it waits for the next pass rather than go before its time."""
    try:
        root, rel = agent_folder.beneath_agent(home)
    except OSError:
        return 0.0
    try:
        newest = safe_fs.lstat_beneath(root, rel).st_mtime
    except FileNotFoundError:
        return 0.0
    except OSError as exc:
        return _unread(home, exc)
    try:
        for step in safe_fs.walk_beneath(root, rel, onerror=lambda _exc: None):
            for name in step.files + step.symlinks + step.other:
                try:
                    st = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False)
                except OSError:
                    continue
                newest = max(newest, st.st_mtime)
    except (safe_fs.SymlinkRefused, FileNotFoundError):
        pass
    except OSError as exc:
        return _unread(home, exc)
    return newest


def _unread(home: Path, exc: OSError) -> float:
    """The age of a tree that could not be read: now, so the pass keeps it."""
    logger.warning("caller-data: the age of %s could not be read (%s), kept for the next pass",
                   home, type(exc).__name__)
    return time.time()


def _tree_bytes(path: Path) -> int:
    """The tree's bytes, every name but a folder by its own size
    (``storage_quota.dir_usage``, the one walk that counts a folder)."""
    return storage_quota.dir_usage(path)[0]


def _caller_rel(agent: str, home: Path) -> str:
    """``home``'s rel below the agent's folder, from the path as written,
    when it names a caller tree: ``externals/<channel>/<slug>`` or
    ``externals/<channel>/_ephemeral/<name>``; "" for anything else."""
    try:
        _root, rel = agent_folder.beneath_agent(home, agent)
    except (OSError, ValueError):
        return ""
    parts = rel.split("/")
    if parts[0] != external_identity.EXTERNALS_DIRNAME:
        return ""
    if len(parts) == 3 and parts[2] != external_identity.EPHEMERAL_DIRNAME:
        return rel
    if len(parts) == 4 and parts[2] == external_identity.EPHEMERAL_DIRNAME:
        return rel
    return ""


def _resolved(home: Path) -> str:
    try:
        return str(home.resolve())
    except OSError:
        return str(home)


def remove_home(agent: str, home: Path, stats: dict, dry_run: bool) -> bool:
    """Remove one external home and purge its bookkeeping rows. Refuses
    anything that is not a caller tree of the agent's ``externals/`` (belt
    and braces — the iterators only ever yield such paths). The tree is
    removed beneath the agent's real folder with no link followed, so a
    removal never leaves the tree it was given."""
    rel = _caller_rel(agent, home)
    if not rel:
        stats["errors"] += 1
        logger.error("caller-data: refusing to remove %s (not a caller tree under externals/)", home)
        return False
    size = _tree_bytes(home)
    if dry_run:
        stats["caller_bytes_freed"] += size
        return True
    try:
        safe_fs.rmtree_beneath(agent_folder.agent_root(agent), rel)
    except OSError as e:
        stats["errors"] += 1
        logger.warning("caller-data: failed to remove %s: %s", home, e)
        return False
    prefix = rel + "/"
    with contextlib.suppress(Exception):
        file_tombstones_store.purge_prefix(agent, prefix)
        file_author_store.purge_prefix(agent, prefix)
    # The person's row goes with the tree (an ephemeral tree has none).
    parts = rel.split("/")
    if len(parts) == 3:
        with contextlib.suppress(Exception):
            _forget_record(agent, parts[1], parts[2])
    stats["caller_bytes_freed"] += size
    return True


def _forget_record(agent: str, channel: str, slug: str) -> None:
    """An outside person's record, and the answers their sessions' tools
    gave, which are kept under their channel and caller id
    (``tool_listing.outside_person``)."""
    from services.mcp import tool_listing
    row = external_identity_store.get(agent, channel, slug)
    external_identity_store.forget(agent, channel, slug)
    if row is not None:
        tool_listing.forget(person=tool_listing.outside_person(channel, row.get("external_id") or ""),
                            agent=agent)


# ---------------------------------------------------------------------------
# Phone conversations
# ---------------------------------------------------------------------------

def _session_files(agent: str, sid: str, tid: str) -> list[Path]:
    """The on-disk CLI state of one phone chat: under the agent's shared
    workspace home, a user's home (a user-tied call) or any caller tree —
    every engine names its own files under each home
    (``ExecutionLayer.chat_session_files``)."""
    from core.session.session_manager import get_all_layers
    from services.infra.retention import iter_local_homes
    layers = list(get_all_layers().values())
    files: list[Path] = []
    for _agent, _username, home in iter_local_homes(agent=agent):
        for layer in layers:
            files.extend(layer.chat_session_files(home, sid, tid))
    return files


def _delete_phone_chat(chat: dict, stats: dict, dry_run: bool) -> None:
    from core.session.session_manager import get_all_layers
    from services.infra.retention import _unlink_with_companions
    layers = list(get_all_layers().values())
    for f in _session_files(chat["agent"], chat.get("session_id") or "",
                            chat.get("codex_thread_id") or ""):
        # The file and its engine's folder, beneath the agent's real folder with no
        # link followed (``retention._unlink``); only the bytes are kept.
        counted = {"errors": 0, "files": 0, "bytes": 0}
        _unlink_with_companions(layers, f, counted, "files", "bytes", dry_run)
        stats["caller_bytes_freed"] += counted["bytes"]
        stats["errors"] += counted["errors"]
    if not dry_run:
        # A report outlives an ordinary chat; a caller's conversation is
        # forgotten whole, as its caller was promised, its wrap-up's run
        # (the transcript in its prompt) with it.
        from storage import db_reply_reports
        from storage.chat import conversation_store
        db_reply_reports.delete_for_chat(chat["id"])
        # Every run of its wrap-up: a restart's second run leaves the record
        # naming it alone, and the first holds the transcript as well.
        from core.session import session_kind
        from services.agents import chat_removal
        record = conversation_store.get(chat["id"])
        runs = {r["id"]: r for r in task_store.list_runs(
            limit=100, task_id=conversation_store.wrapup_task_id(chat["id"]))}
        recorded = (record or {}).get("wrapup_run_id") or ""
        if recorded:
            runs.setdefault(recorded, task_store.get_run(recorded) or {})
        for run_id, run in runs.items():
            external_identity.prune_session_scratch(
                config.get_agent_dir(chat["agent"]), (run or {}).get("session_id") or "")
            chat_removal.delete_rows(session_kind.task_chat_id(run_id))
            task_store.delete_run(run_id)
        chat_removal.delete_rows(chat["id"])
    stats["phone_chats_deleted"] += 1


def _chat_is_live(chat: dict, live_session_ids: set, live_pump_chat_ids: set) -> bool:
    sid = chat.get("session_id") or ""
    return (sid and sid in live_session_ids) or chat["id"] in live_pump_chat_ids


# ---------------------------------------------------------------------------
# The pass + Forget all
# ---------------------------------------------------------------------------

STAT_KEYS = (
    "callers_forgotten", "callers_busy_skipped", "ephemeral_reaped",
    "phone_chats_deleted", "call_log_rows_deleted", "caller_bytes_freed",
)


def _init_stats(stats: dict) -> None:
    for k in STAT_KEYS:
        stats.setdefault(k, 0)
    stats.setdefault("errors", 0)


def run_pass(busy_homes: set, live_session_ids: set, live_pump_chat_ids: set,
             stats: dict, dry_run: bool) -> None:
    """The caller-data pass of the daily sweep.

    ``busy_homes`` = resolved ``external_home`` paths of live sessions.
    """
    _init_stats(stats)
    now = time.time()
    # Ephemeral leftovers — always.
    for _agent, home in list(iter_ephemeral_homes()):
        if _resolved(home) in busy_homes:
            continue
        if now - newest_mtime(home) >= EPHEMERAL_GRACE_S:
            if remove_home(_agent, home, stats, dry_run):
                stats["ephemeral_reaped"] += 1
    enabled = settings_enabled()
    stats["caller_data_pass_skipped"] = not enabled
    if not enabled:
        return
    days = settings_days()
    stats["caller_data_days"] = days
    age_s = days * 86400
    for agent, _channel, _slug, home in list(iter_caller_homes()):
        if _resolved(home) in busy_homes:
            stats["callers_busy_skipped"] += 1
            continue
        if now - newest_mtime(home) >= age_s:
            if remove_home(agent, home, stats, dry_run):
                stats["callers_forgotten"] += 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    for chat in task_store.get_phone_chats(cutoff):
        if _chat_is_live(chat, live_session_ids, live_pump_chat_ids):
            continue
        _delete_phone_chat(chat, stats, dry_run)
    if dry_run:
        stats["call_log_rows_deleted"] += phone_call_log_store.count_older_than(cutoff)
    else:
        stats["call_log_rows_deleted"] += phone_call_log_store.prune_older_than(cutoff)


def forget_all(busy_homes: set, live_session_ids: set, live_pump_chat_ids: set) -> dict:
    """Every caller tree (durable + ephemeral), every phone conversation and
    every call-log row — now. Busy trees and live conversations are skipped
    and reported; the admin can run it again after the calls end."""
    stats: dict = {"ran_at": datetime.now(timezone.utc).isoformat()}
    _init_stats(stats)
    stats["phone_chats_busy_skipped"] = 0
    for agent, home in list(iter_external_homes()):
        if _resolved(home) in busy_homes:
            stats["callers_busy_skipped"] += 1
            continue
        if remove_home(agent, home, stats, dry_run=False):
            stats["callers_forgotten"] += 1
    for chat in task_store.get_phone_chats(None):
        if _chat_is_live(chat, live_session_ids, live_pump_chat_ids):
            stats["phone_chats_busy_skipped"] += 1
            continue
        _delete_phone_chat(chat, stats, dry_run=False)
    stats["call_log_rows_deleted"] += phone_call_log_store.delete_all()
    logger.info(
        "caller-data: forget-all removed %s caller trees "
        "(%s bytes), %s phone chats, "
        "%s call-log rows; "
        "skipped %s busy trees, "
        "%s live conversations",
        stats['callers_forgotten'], stats['caller_bytes_freed'], stats['phone_chats_deleted'],
        stats['call_log_rows_deleted'], stats['callers_busy_skipped'],
        stats['phone_chats_busy_skipped']
    )
    return stats


# ---------------------------------------------------------------------------
# Status (admin card)
# ---------------------------------------------------------------------------

def compute_usage() -> dict:
    """Settings + what is on disk / in the DB right now. Sync — call via
    ``asyncio.to_thread`` (walks the externals trees)."""
    per_agent: dict[str, dict] = {}
    callers = 0
    total = 0
    for agent, _channel, _slug, home in iter_caller_homes():
        size = _tree_bytes(home)
        row = per_agent.setdefault(agent, {"callers": 0, "bytes": 0})
        row["callers"] += 1
        row["bytes"] += size
        callers += 1
        total += size
    for agent, home in iter_ephemeral_homes():
        size = _tree_bytes(home)
        per_agent.setdefault(agent, {"callers": 0, "bytes": 0})["bytes"] += size
        total += size
    return {
        "enabled": settings_enabled(),
        "days": settings_days(),
        "callers": callers,
        "bytes": total,
        "agents": per_agent,
        "phone_chats": len(task_store.get_phone_chats(None)),
        "call_log_rows": phone_call_log_store.count_older_than(None),
    }


# ---------------------------------------------------------------------------
# Forget one person
# ---------------------------------------------------------------------------

#: Why a person is not forgotten now: a call of theirs, or its wrap-up, runs.
FORGET_BUSY = "A conversation with this person is still running. Try again when it ends."


def forget_person(agent: str, identity_id: str, busy_homes: set, live_session_ids: set,
                  live_pump_chat_ids: set) -> dict | None:
    """Forget one outside person of the agent, from every store that holds
    them: their conversations (session files, reply reports, messages, the
    chat rows, the records, each wrap-up's run and its chat), their tree
    (memory, files, engine state) with its file bookkeeping, their record,
    the notes made from their conversations, and the call-log rows that name
    them. None when the person is not the agent's; the sentence
    :data:`FORGET_BUSY` (in ``stats["refused"]``) while a call or a wrap-up
    of theirs runs, with nothing touched."""
    from core.session.visibility import PHONE_CHAT_OWNER
    from storage.chat import conversation_store
    row = external_identity_store.get_by_id(identity_id)
    if row is None or row.get("agent") != agent:
        return None
    stats: dict = {"ran_at": datetime.now(timezone.utc).isoformat()}
    _init_stats(stats)
    chats = conversation_store.chats_of_identity(agent, identity_id, owner=PHONE_CHAT_OWNER)
    try:
        home = external_identity.tree_of(config.get_agent_dir(agent), row["channel"], row["slug"])
    except ValueError:
        home = None
    if (any(_chat_is_live(c, live_session_ids, live_pump_chat_ids)
            or c.get("wrapup") in conversation_store.WRAPUP_OPEN for c in chats)
            or (home is not None and _resolved(home) in busy_homes)):
        stats["refused"] = FORGET_BUSY
        return stats
    for chat in chats:
        _delete_phone_chat(chat, stats, dry_run=False)
    if home is not None and home.is_dir():
        if remove_home(agent, home, stats, dry_run=False):
            stats["callers_forgotten"] += 1
    _forget_record(agent, row["channel"], row["slug"])
    stats["notes_deleted"] = conversation_store.forget_notes(agent, identity_id)
    stats["call_log_rows_deleted"] += phone_call_log_store.delete_for_caller(
        agent, row.get("external_id") or "")
    logger.info("caller-data: one person of %s forgotten (%d conversations)", agent, len(chats))
    return stats
