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
  so removal is a plain rmtree plus the purge of the file bookkeeping rows
  (``file_author`` / ``file_tombstones``) under the path — no tombstones are
  written: nothing downstream could resurrect the files.
- Nothing lands in the Recover bin: caller data is deleted, not parked.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import config
from core.session import external_identity
from services.infra.agent_dirs import agent_dirs
from storage import database as task_store
from storage.files import file_author_store
from storage.files import file_tombstones_store
from storage.phone import phone_call_log_store

logger = logging.getLogger("claude-proxy")

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


def iter_caller_homes() -> Iterator[tuple[str, str, str, Path]]:
    """``(agent, channel, slug, home)`` for every durable caller tree.
    Bounded three-level iteration (``externals/<channel>/<slug>``)."""
    for agent, agent_dir in _agent_dirs():
        for channel_dir in sorted((agent_dir / external_identity.EXTERNALS_DIRNAME).iterdir()):
            if not channel_dir.is_dir():
                continue
            for home in sorted(channel_dir.iterdir()):
                if home.is_dir() and not home.is_symlink() \
                        and home.name != external_identity.EPHEMERAL_DIRNAME:
                    yield agent, channel_dir.name, home.name, home


def iter_ephemeral_homes() -> Iterator[tuple[str, Path]]:
    """``(agent, home)`` for every ephemeral tree (``externals/<channel>/_ephemeral/<sid>``)."""
    for agent, agent_dir in _agent_dirs():
        for channel_dir in sorted((agent_dir / external_identity.EXTERNALS_DIRNAME).iterdir()):
            eph = channel_dir / external_identity.EPHEMERAL_DIRNAME
            if not eph.is_dir():
                continue
            for home in sorted(eph.iterdir()):
                if home.is_dir() and not home.is_symlink():
                    yield agent, home


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
    caller activity."""
    newest = 0.0
    with contextlib.suppress(OSError):
        newest = home.lstat().st_mtime
    try:
        for p in home.rglob("*"):
            try:
                st = p.lstat()
            except OSError:
                continue
            if (st.st_mode & 0o170000) != 0o040000:      # not a directory
                newest = max(newest, st.st_mtime)
    except OSError:
        pass
    return newest


def _tree_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                st = p.lstat()
            except OSError:
                continue
            if not (st.st_mode & 0o170000) == 0o040000:
                total += st.st_size
    except OSError:
        pass
    return total


def _rel_prefix(agent: str, home: Path) -> str:
    try:
        return home.resolve().relative_to(config.get_agent_dir(agent).resolve()).as_posix() + "/"
    except (OSError, ValueError):
        return ""


def _resolved(home: Path) -> str:
    try:
        return str(home.resolve())
    except OSError:
        return str(home)


def remove_home(agent: str, home: Path, stats: dict, dry_run: bool) -> bool:
    """rmtree one external home + purge its bookkeeping rows. Refuses anything
    outside the agent's ``externals/`` (belt and braces — the iterators only
    ever yield such paths)."""
    rel = _rel_prefix(agent, home)
    if not rel.startswith(external_identity.EXTERNALS_DIRNAME + "/"):
        stats["errors"] += 1
        logger.error(f"caller-data: refusing to remove {home} (not under externals/)")
        return False
    size = _tree_bytes(home)
    if dry_run:
        stats["caller_bytes_freed"] += size
        return True
    try:
        shutil.rmtree(home)
    except OSError as e:
        stats["errors"] += 1
        logger.warning(f"caller-data: failed to remove {home}: {e}")
        return False
    with contextlib.suppress(Exception):
        file_tombstones_store.purge_prefix(agent, rel)
        file_author_store.purge_prefix(agent, rel)
    stats["caller_bytes_freed"] += size
    return True


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
    for f in _session_files(chat["agent"], chat.get("session_id") or "",
                            chat.get("codex_thread_id") or ""):
        try:
            size = f.lstat().st_size
            if not dry_run:
                f.unlink()
            stats["caller_bytes_freed"] += size
        except OSError:
            continue
    if not dry_run:
        task_store.delete_chat(chat["id"])
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
        f"caller-data: forget-all removed {stats['callers_forgotten']} caller trees "
        f"({stats['caller_bytes_freed']} bytes), {stats['phone_chats_deleted']} phone chats, "
        f"{stats['call_log_rows_deleted']} call-log rows; "
        f"skipped {stats['callers_busy_skipped']} busy trees, "
        f"{stats['phone_chats_busy_skipped']} live conversations"
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
