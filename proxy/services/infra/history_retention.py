"""The history window: chats, runs and verdicts older than a set number of days.

A pass of the daily retention sweep (``services/infra/retention.py`` calls
:func:`run_pass`, on the loop: it reads the live registries and a chat goes
through the delete route's own sequence, ``services/agents/chat_removal``).

Two windows, both off unless set:

- ``agents.history_retention_days``, per agent, an admin's: chats, the agent's
  finished runs (``output_text``, a judge's run and its chat among them) and
  check verdicts with their findings.
- ``chat_rows_retention_days``, platform-wide: chats, with their reply reports.
  Off and absent from the card unless an operator set it.

A chat goes only when it and its group (parent, children, project) are past
the window and nothing still needs any of them. Phone chats are the caller-data
pass's; a meeting's parent chat stays. A run goes only once its chat has, as
who may open a chat is read from its run. A reply report outlives its chat
unless the chat-rows window took it: reports are the admins' own record about
an agent, and an agent's window must not erase them.

A dry run deletes nothing and writes nothing. Deletions are logged by id and
date, never by title, message or file content.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from core.session import session_kind
from core.session.visibility import SCOPE_USER
from storage import database as task_store
from storage.chat import history_store
from storage.checks import db_checks
from storage.pg import run_db

logger = logging.getLogger("otodock")

MIN_DAYS = 7
MAX_DAYS = 3650
# Per agent per sweep: the rest waits for the next day, so a first run on a
# long history stays bounded.
MAX_PER_SWEEP = 2000
ROWS_KEY = "chat_rows_retention_days"

STAT_KEYS = (
    "history_chats_deleted", "history_chats_busy", "history_runs_deleted",
    "history_verdicts_deleted", "history_files_deleted", "history_bytes_freed",
)


def days_from(raw: str | int | None) -> int:
    """The days a stored value means, 0 for none: unset or anything outside
    ``MIN_DAYS``..``MAX_DAYS`` deletes nothing (a forced setting passes no
    route, and a forced ``0`` meant as off must not read as the floor)."""
    if raw in (None, ""):
        return 0
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = -1
    if MIN_DAYS <= days <= MAX_DAYS:
        return days
    if days != 0:
        logger.warning("history window %r is not a number of days from %d to %d: "
                       "nothing is deleted by it", raw, MIN_DAYS, MAX_DAYS)
    return 0


def _rows_days() -> int:
    return days_from(task_store.get_platform_setting(ROWS_KEY))


def _iso_before(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()


def _live_refs() -> tuple[set[str], set[str], set[str]]:
    """The chats, sessions and threads live right now, read on the loop."""
    from services.infra.retention import _build_live_snapshot, _remote_live_ids
    snap = _build_live_snapshot()
    remote, _ = _remote_live_ids()
    return set(snap.pump_chat_ids), set(snap.session_ids) | remote, set(snap.codex_thread_ids) | remote


def _registry_busy(group: list[dict]) -> str:
    """What in this process still holds a chat of the group, or ``""``."""
    from core.events import input_queue
    from core.session import interactive_registry, session_delivery, warmup_registry
    from core.session.session_manager import find_layer_for_session
    from services.remote import chat_move
    pumps, sessions, threads = _live_refs()
    for member in group:
        cid, sid, tid = member["id"], member.get("session_id") or "", member.get("codex_thread_id") or ""
        if cid in pumps:
            return "a running turn"
        if warmup_registry.get(cid) is not None or session_delivery.oneshot_inflight(cid) is not None \
                or input_queue.getting_ready(cid):
            return "a chat still starting"
        if interactive_registry.find_live_for_chat(cid) is not None:
            return "an open terminal"
        if chat_move.moving(cid):
            return "a move"
        if sid and (sid in sessions or find_layer_for_session(sid) is not None):
            return "a live session"
        if tid and tid in threads:
            return "a live session"
    return ""


def _a_task_continues(group: list[dict]) -> bool:
    """A task can still continue the session of some chat of the group."""
    from storage import remote_store
    return any(remote_store.task_can_continue(member) for member in group)


def _files_sync(agent: str, owner: str, chat_id: str, sid: str, tid: str, dry_run: bool,
                assume_gone: Sequence[str] = ()) -> tuple[int, int]:
    """Remove one session's files on this server, ``(files, bytes)``. An id
    another chat or a task that can continue it still names keeps its files
    (``assume_gone``: a dry run's chats already counted, whose rows are still
    there); a chat that ran on a paired machine has none here, and leaves
    that machine's keep list with its row."""
    from core.session.session_manager import get_all_layers
    from services.infra.retention import (
        _UUID_RE,
        _home_for_chat,
        _unlink_with_companions,
    )
    from storage import remote_store

    def ours(ref: str) -> str:
        fits = (ref and _UUID_RE.match(ref)
                and not remote_store.session_refs_shared(chat_id, [ref], assume_gone=assume_gone))
        return ref if fits else ""

    sid_ok, tid_ok = ours(sid), ours(tid)
    if not sid_ok and not tid_ok:
        return 0, 0
    try:
        home = _home_for_chat(agent, owner)
    except ValueError:
        return 0, 0
    layers = list(get_all_layers().values())
    counted = {"errors": 0, "files": 0, "bytes": 0}
    for layer in layers:
        for f in layer.chat_session_files(home, sid_ok, tid_ok):
            _unlink_with_companions(layers, f, counted, "files", "bytes", dry_run)
    return counted["files"], counted["bytes"]


async def _retire_chat(chat: dict, cutoff: str, rows_cutoff: str, stats: dict, dry_run: bool,
                       gone_refs: set[str], assume_gone: Sequence[str]) -> bool:
    """One candidate chat. True when it went, or would."""
    from services.agents import chat_removal
    group = await run_db(history_store.group_of, chat)
    if group is None:
        stats["history_chats_busy"] += 1
        return False
    reason = _registry_busy(group)
    if not reason:
        reason = await run_db(history_store.blocker, [m["id"] for m in group])
    if not reason and await run_db(_a_task_continues, group):
        reason = "a task that continues its session"
    if reason:
        stats["history_chats_busy"] += 1
        logger.info("retention: chat %s of %s is kept for now: %s", chat["id"][:8], chat["agent"], reason)
        return False
    with_reports = bool(rows_cutoff) and chat["updated_at"] < rows_cutoff
    if not dry_run and not await chat_removal.remove_chat(
            chat, untouched_before=cutoff, with_reply_reports=with_reports):
        return False
    sid, tid = chat.get("session_id") or "", chat.get("codex_thread_id") or ""
    files, size = await asyncio.to_thread(
        _files_sync, chat["agent"], chat.get("user_sub") or "", chat["id"], sid, tid, dry_run,
        assume_gone)
    stats["history_chats_deleted"] += 1
    stats["history_files_deleted"] += files
    stats["history_bytes_freed"] += size
    gone_refs.update(r for r in (sid, tid) if r)
    if not dry_run:
        logger.info("retention: deleted chat %s of %s (last touched %s, %d files, %d bytes)",
                    chat["id"][:8], chat["agent"], (chat["updated_at"] or "")[:10], files, size)
    return True


async def _retire_run(run: dict, stats: dict, dry_run: bool) -> None:
    if not dry_run and not await run_db(task_store.delete_run, run["id"]):
        return
    owner = run.get("created_by") or "" if run.get("scope") == SCOPE_USER else ""
    files, size = await asyncio.to_thread(
        _files_sync, run["agent"], owner, "", run.get("session_id") or "", "", dry_run)
    stats["history_runs_deleted"] += 1
    stats["history_files_deleted"] += files
    stats["history_bytes_freed"] += size
    if not dry_run:
        logger.info("retention: deleted run %s of %s (finished %s, %d files, %d bytes)",
                    run["id"][:8], run["agent"], (run.get("last_at") or "")[:10], files, size)


async def _sweep_agent(agent: str, agent_days: int, rows_days: int, now: datetime,
                       stats: dict, dry_run: bool, gone_refs: set[str]) -> None:
    windows = [d for d in (agent_days, rows_days) if d]
    if not windows:
        return
    cutoff = _iso_before(now, min(windows))
    rows_cutoff = _iso_before(now, rows_days) if rows_days else ""
    sources = session_kind.source_types(session_kind.DASHBOARD, session_kind.TASK)
    chats = await run_db(history_store.candidate_chats, agent, cutoff, sources, MAX_PER_SWEEP)
    gone_chats: list[str] = []
    for chat in chats:
        if await _retire_chat(chat, cutoff, rows_cutoff, stats, dry_run, gone_refs,
                              gone_chats if dry_run else ()):
            gone_chats.append(chat["id"])
    if not agent_days:
        return
    agent_cutoff = _iso_before(now, agent_days)
    runs = await run_db(history_store.candidate_runs, agent, agent_cutoff, MAX_PER_SWEEP,
                        assume_gone=gone_chats if dry_run else None)
    for run in runs:
        await _retire_run(run, stats, dry_run)
    stats["history_verdicts_deleted"] += await run_db(
        db_checks.delete_verdicts_before, agent_cutoff, agent=agent, dry_run=dry_run)


async def run_pass(stats: dict, dry_run: bool) -> set[str]:
    """The pass. Returns the session and thread ids of the chats it took, or
    on a dry run would take: the machines' pass reads them off the keep lists,
    as a real run's rows no longer list them."""
    for key in STAT_KEYS:
        stats.setdefault(key, 0)
    gone_refs: set[str] = set()
    rows_days = await run_db(_rows_days)
    windows = await run_db(history_store.history_windows)
    agents = set(windows)
    if rows_days:
        agents |= set(await run_db(history_store.agents_with_chats))
    now = datetime.now(timezone.utc)
    for agent in sorted(agents):
        try:
            await _sweep_agent(agent, days_from(windows.get(agent)), rows_days, now,
                               stats, dry_run, gone_refs)
        except Exception:
            stats["errors"] += 1
            logger.exception("retention: the history pass failed on %s", agent)
    return gone_refs
