"""The rows the retention sweep's history pass reads
(``services/infra/history_retention.py``): which chats and runs are past their
window, who holds a chat in a group with others, and what makes one busy.

Nothing here deletes: a chat goes through ``services/agents/chat_removal``,
a run through ``task_store.delete_run``."""

from __future__ import annotations

from services.scheduler import task_kinds
from storage.automation import run_status
from storage.chat import cut_records
from storage.pg import get_conn

# A group larger than this is left alone for this sweep: walking it per
# candidate would make the pass quadratic.
_GROUP_MAX = 200
# A move in one of these still needs the chat's row.
_OPEN_MOVES = ("moving", "awaiting_resume")


def history_windows() -> dict[str, int]:
    """Each agent with a history window, and its days."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT slug, history_retention_days AS days FROM agents "
            "WHERE history_retention_days > 0",
        ).fetchall()
    return {r["slug"]: int(r["days"]) for r in rows}


def agents_with_chats() -> list[str]:
    """Every agent that has a chat row (the chat-rows window applies to all)."""
    with get_conn() as conn:
        return [r["agent"] for r in conn.execute("SELECT DISTINCT agent FROM chats").fetchall()]


def candidate_chats(agent: str, cutoff_iso: str, source_types: list[str], limit: int) -> list[dict]:
    """The agent's chats past the cutoff, oldest first, whose whole group is
    past it too. Meetings hang on their parent chat with no foreign key and
    mint no row of their own, so a meeting's parent stays."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.user_sub, c.agent, c.session_id, c.codex_thread_id,
                      c.execution_target, c.execution_path, c.parent_chat_id,
                      c.project_id, c.updated_at
                 FROM chats c
                WHERE c.agent = %(agent)s
                  AND c.source_type = ANY(%(sources)s)
                  AND c.updated_at < %(cutoff)s
                  AND NOT EXISTS (SELECT 1 FROM meetings m WHERE m.parent_chat_id = c.id)
                  AND NOT EXISTS (SELECT 1 FROM chats p
                                   WHERE p.id = c.parent_chat_id AND p.updated_at >= %(cutoff)s)
                  AND NOT EXISTS (SELECT 1 FROM chats k
                                   WHERE k.parent_chat_id = c.id AND k.updated_at >= %(cutoff)s)
                  AND (c.project_id = ''
                       OR NOT EXISTS (SELECT 1 FROM chats g
                                       WHERE g.project_id = c.project_id
                                         AND g.updated_at >= %(cutoff)s))
                ORDER BY c.updated_at
                LIMIT %(limit)s""",
            {"agent": agent, "sources": source_types, "cutoff": cutoff_iso, "limit": limit},
        ).fetchall()
    return [dict(r) for r in rows]


def group_of(chat: dict) -> list[dict] | None:
    """The chat and its group: its parent, its children and its project's
    members, each with its session ids. None when the group is too large to
    read for one candidate; the chat then waits."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, session_id, codex_thread_id FROM chats
                WHERE id = %(id)s OR id = %(parent)s OR parent_chat_id = %(id)s
                   OR (%(project)s <> '' AND project_id = %(project)s)
                LIMIT %(limit)s""",
            {"id": chat["id"], "parent": chat.get("parent_chat_id") or "",
             "project": chat.get("project_id") or "", "limit": _GROUP_MAX + 1},
        ).fetchall()
    if len(rows) > _GROUP_MAX:
        return None
    return [dict(r) for r in rows]


def blocker(chat_ids: list[str]) -> str:
    """What in the database still needs any of these chats, in plain words
    for the log, or ``""``: a run or a task that holds it, a wake due on it,
    a message waiting, an open turn, a cut turn to continue, a rebuild, an
    undelivered delegate result, a share, a move that needs its row."""
    if not chat_ids:
        return ""
    params = {
        "ids": chat_ids, "live": sorted(run_status.LIVE),
        "continuation": task_kinds.CONTINUATION,
        "unsettled": list(cut_records.UNSETTLED), "moves": list(_OPEN_MOVES),
    }
    checks = (
        ("a live run",
         "SELECT 1 FROM task_runs WHERE chat_id = ANY(%(ids)s) AND status = ANY(%(live)s)"),
        ("a live worker or a task that reports here",
         """SELECT 1 FROM task_runs r JOIN dynamic_tasks d ON d.id = r.task_id
             WHERE r.status = ANY(%(live)s)
               AND (d.target_chat_id = ANY(%(ids)s) OR d.on_complete_chat_id = ANY(%(ids)s))"""),
        ("a pending wake",
         """SELECT 1 FROM dynamic_tasks
             WHERE (task_type = %(continuation)s AND target_chat_id = ANY(%(ids)s))
                OR (on_complete_chat_id = ANY(%(ids)s) AND NOT COALESCE(fired, FALSE))"""),
        ("a queued message", "SELECT 1 FROM chat_input_queue WHERE chat_id = ANY(%(ids)s)"),
        ("an open turn", "SELECT 1 FROM chat_open_turns WHERE chat_id = ANY(%(ids)s)"),
        ("a turn to continue",
         "SELECT 1 FROM chat_cut_turns WHERE chat_id = ANY(%(ids)s) AND state = ANY(%(unsettled)s)"),
        ("a pending rebuild", "SELECT 1 FROM chat_pending_rebuilds WHERE chat_id = ANY(%(ids)s)"),
        ("an undelivered result",
         "SELECT 1 FROM chats WHERE id = ANY(%(ids)s) AND pending_delegate_wake <> ''"),
        ("a share", "SELECT 1 FROM shares WHERE chat_id = ANY(%(ids)s) AND revoked_at IS NULL"),
        ("a move", "SELECT 1 FROM chat_moves WHERE chat_id = ANY(%(ids)s) AND state = ANY(%(moves)s)"),
    )
    with get_conn() as conn:
        for reason, sql in checks:
            if conn.execute(f"{sql} LIMIT 1", params).fetchone() is not None:
                return reason
    return ""


def candidate_runs(agent: str, cutoff_iso: str, limit: int, *,
                   assume_gone: list[str] | None = None) -> list[dict]:
    """The agent's finished runs older than the cutoff whose chat is gone or
    never existed, oldest first. A run stays while its chat does: who may
    open a chat is read from its run. ``assume_gone`` (a dry run) counts
    those chats as gone."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, r.agent, r.session_id, r.chat_id, r.scope, r.created_by,
                      COALESCE(r.completed_at, r.started_at) AS last_at
                 FROM task_runs r
                WHERE r.agent = %(agent)s
                  AND r.status <> ALL(%(live)s)
                  AND COALESCE(r.completed_at, r.started_at) < %(cutoff)s
                  AND NOT EXISTS (
                      SELECT 1 FROM chats c
                       WHERE c.id = COALESCE(NULLIF(r.chat_id, ''), 'task-' || r.id)
                         AND c.id <> ALL(%(gone)s))
                ORDER BY last_at
                LIMIT %(limit)s""",
            {"agent": agent, "live": sorted(run_status.LIVE), "cutoff": cutoff_iso,
             "gone": assume_gone or [], "limit": limit},
        ).fetchall()
    return [dict(r) for r in rows]
