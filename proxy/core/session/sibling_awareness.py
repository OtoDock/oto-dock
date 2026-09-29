"""Sibling-session awareness — a compact "parallel activity" prelude.

Sessions of the same (agent, chat-history owner) learn what else is running
RIGHT NOW: live sibling chats (title + generating/awaiting user) and running
background task runs (name + dyn-<task_id>, so the agent can look one up via
list_tasks / get_task_result ONLY when its work depends on it). Injected:

- per turn at the three layer prepend sites (after the [Current time] line),
- at session start via the delegation-mcp dynamic-context block,
- piggybacked on server-delivered prompts (the PTY rung writes raw bytes and
  never passes the layer sites).

On-change-only: the rendered line is hash-deduped per chat, so an unchanged
sibling set injects once, not every turn; an EMPTY set clears the hash so a
reappearance re-injects. Privacy: visibility is keyed by the exact chat-row
owner — user-scoped titles never cross owners; shared pools (``agent::``)
are visible to every member of that agent by construction. Everything is
best-effort: any failure returns the prompt unchanged.
"""

import asyncio
import logging
import time

from storage import database as task_store
from core.session import session_kind

logger = logging.getLogger("claude-proxy.siblings")

_SNAP_TTL = 2.0    # live-lane + running-task snapshot
_ROW_TTL = 30.0    # chat-row / session→chat lookups
_MAX_LISTED = 5    # per kind; the rest collapses to "+N more"

_snapshot: dict = {"ts": 0.0, "lanes": {}, "tasks": []}
_chat_rows: dict[str, tuple[float, dict | None]] = {}
_session_chats: dict[str, tuple[float, str]] = {}
_last_hash: dict[str, int] = {}

_STATUS_LABEL = {"generating": "generating", "awaiting_user": "awaiting user"}


def _build_snapshot_sync() -> dict:
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session
    from core.session.session_state import _chat_streaming_state
    from services.delegation import lane_status

    candidates: set[str] = set(_chat_streaming_state.keys())
    candidates.update(_active_pumps.keys())
    for s in list(interactive_session._sessions.values()):
        if s.alive and s.chat_id:
            candidates.add(s.chat_id)
    lanes: dict[str, str] = {}
    for cid in candidates:
        status = lane_status.chat_status(cid)
        if status != lane_status.STATUS_IDLE:
            lanes[cid] = status
    return {
        "ts": time.monotonic(),
        "lanes": lanes,
        "tasks": task_store.list_running_task_runs(),
    }


def _get_snapshot_sync() -> dict:
    global _snapshot
    if time.monotonic() - _snapshot["ts"] > _SNAP_TTL:
        _snapshot = _build_snapshot_sync()
    return _snapshot


def _chat_row_sync(chat_id: str) -> dict | None:
    cached = _chat_rows.get(chat_id)
    if cached and time.monotonic() - cached[0] < _ROW_TTL:
        return cached[1]
    row = task_store.get_chat(chat_id)
    _chat_rows[chat_id] = (time.monotonic(), row)
    return row


def _chat_id_for_session_sync(session_id: str) -> str:
    cached = _session_chats.get(session_id)
    if cached and time.monotonic() - cached[0] < _ROW_TTL:
        return cached[1]
    row = task_store.get_chat_by_session(session_id)
    chat_id = row["id"] if row else ""
    if row:
        _chat_rows[chat_id] = (time.monotonic(), row)
    _session_chats[session_id] = (time.monotonic(), chat_id)
    return chat_id


def _gather_sync(agent: str, owner: str, own_chat_id: str) -> tuple[list, list]:
    """(sibling chats, running tasks) visible to (agent, owner), own lane
    excluded, worker lanes deduped out of the task list."""
    snap = _get_snapshot_sync()
    siblings: list[tuple[str, str]] = []
    sibling_ids: set[str] = set()
    for cid, status in snap["lanes"].items():
        if cid == own_chat_id or session_kind.is_task_chat_id(cid):
            continue
        row = _chat_row_sync(cid)
        if not row or row.get("agent") != agent or row.get("user_sub") != owner:
            continue
        siblings.append((row.get("title") or cid[:8], status))
        sibling_ids.add(cid)
    tasks: list[tuple[str, str]] = []
    for r in snap["tasks"]:
        if r.get("agent") != agent:
            continue
        rcid = r.get("chat_id") or ""
        if rcid and (rcid == own_chat_id or rcid in sibling_ids):
            continue
        if r.get("scope") == "user" and r.get("created_by") != owner:
            continue
        tasks.append((r.get("name") or r["task_id"], r["task_id"]))
    return siblings, tasks


def _render_line(siblings: list, tasks: list) -> str:
    parts = []
    if siblings:
        listed = ", ".join(
            f"'{title}' ({_STATUS_LABEL.get(status, status)})"
            for title, status in siblings[:_MAX_LISTED])
        extra = f" +{len(siblings) - _MAX_LISTED} more" if len(siblings) > _MAX_LISTED else ""
        parts.append(f"sibling sessions: {listed}{extra}")
    if tasks:
        listed = ", ".join(f"'{name}' ({task_id})" for name, task_id in tasks[:_MAX_LISTED])
        extra = f" +{len(tasks) - _MAX_LISTED} more" if len(tasks) > _MAX_LISTED else ""
        parts.append(f"background tasks running: {listed}{extra}")
    return (
        "[Parallel activity — for awareness only, no action needed: "
        + "; ".join(parts)
        + ". Check one only if your work depends on it "
        "(peek_session / get_task_result).]"
    )


def _line_for_chat_sync(chat_id: str) -> str:
    own = _chat_row_sync(chat_id)
    if not own:
        return ""
    siblings, tasks = _gather_sync(
        own.get("agent") or "", own.get("user_sub") or "", chat_id)
    if not siblings and not tasks:
        _last_hash.pop(chat_id, None)
        return ""
    line = _render_line(siblings, tasks)
    h = hash(line)
    if _last_hash.get(chat_id) == h:
        return ""
    _last_hash[chat_id] = h
    return line


async def prelude_line(session_id: str) -> str:
    """The changed-since-last-injection awareness line for this session's
    chat, or "". Never raises — awareness must never break a turn."""
    if not session_id:
        return ""
    try:
        chat_id = await asyncio.to_thread(_chat_id_for_session_sync, session_id)
        if not chat_id:
            return ""
        return await asyncio.to_thread(_line_for_chat_sync, chat_id)
    except Exception:
        logger.exception(f"sibling prelude failed for session {session_id[:8]}")
        return ""


async def prepend_if_changed(chat_id: str, text: str) -> str:
    """Server-delivery piggyback: prepend the changed line to ``text``."""
    if not chat_id:
        return text
    try:
        line = await asyncio.to_thread(_line_for_chat_sync, chat_id)
    except Exception:
        logger.exception(f"sibling piggyback failed for chat {chat_id[:8]}")
        return text
    return f"{line}\n\n{text}" if line else text


def context_block(agent_name: str, user_sub: str) -> str | None:
    """Session-start markdown block for the dynamic-context provider — covers
    layers with no per-turn injection (PTY, remote) on their first turn."""
    try:
        from core.session.visibility import chat_history_owner, shared_chat_owner
        owner = (chat_history_owner(agent_name, user_sub)
                 if user_sub else shared_chat_owner(agent_name))
        siblings, tasks = _gather_sync(agent_name, owner, own_chat_id="")
    except Exception:
        logger.exception(f"sibling context block failed for agent {agent_name}")
        return None
    if not siblings and not tasks:
        return None
    lines = ["## Active parallel sessions",
             "For awareness only — no action needed:"]
    for title, status in siblings[:_MAX_LISTED]:
        lines.append(f"- '{title}' — {_STATUS_LABEL.get(status, status)}")
    for name, task_id in tasks[:_MAX_LISTED]:
        lines.append(f"- background task '{name}' ({task_id}) — running")
    lines.append(
        "Check one only if your work depends on it: peek_session / "
        "list_tasks / get_task_result.")
    return "\n".join(lines)
