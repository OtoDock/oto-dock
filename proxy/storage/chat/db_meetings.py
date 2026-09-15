"""Meeting and meeting-turn queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

import contextlib
from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

def create_meeting(meeting_id: str, topic: str, participants: str,
                   moderator: str, strategy: str, max_turns: int,
                   parent_chat_id: str, parent_session_id: str | None,
                   parent_run_id: str | None, scope: str,
                   created_by: str | None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO meetings
               (id, topic, participants, active_participants, moderator,
                strategy, max_turns, parent_chat_id, parent_session_id,
                parent_run_id, scope, created_by, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (meeting_id, topic, participants, participants, moderator,
             strategy, max_turns, parent_chat_id, parent_session_id,
             parent_run_id, scope, created_by, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM meetings WHERE id=%s", (meeting_id,)).fetchone()
        return dict(row) if row else {}


def get_meeting(meeting_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id=%s", (meeting_id,)).fetchone()
        return dict(row) if row else None


def count_active_meeting_participants(created_by: str) -> int:
    """Total participants across one creator's ACTIVE meetings — the input to
    the per-creator meeting cap (MAX_PARALLEL_SPAWNS on meetings-mcp)."""
    import json as _json
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT participants FROM meetings "
            "WHERE created_by=%s AND status IN ('active','concluding','pending')",
            (created_by,),
        ).fetchall()
    total = 0
    for r in rows:
        with contextlib.suppress(ValueError, TypeError):
            total += len(_json.loads(r["participants"]) or [])
    return total


def update_meeting(meeting_id: str, **fields) -> bool:
    allowed = {"status", "current_round", "active_participants", "summary",
               "cost_usd", "concluded_at", "parent_session_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    sql = f"UPDATE meetings SET {', '.join(f'{k}=%s' for k in updates)} WHERE id=%s"
    values = list(updates.values()) + [meeting_id]
    with get_conn() as conn:
        conn.execute(sql, values)
        conn.commit()
        return True


def mark_orphaned_meetings_failed() -> int:
    """Mark meetings stuck in active/pending/concluding as failed (proxy restart recovery).

    Returns the number of rows updated.  Idempotent — safe to call on every
    startup even if no meetings are orphaned.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE meetings SET status='failed', concluded_at=%s "
            "WHERE status IN ('active', 'pending', 'concluding')",
            (now,),
        )
        conn.commit()
        return cur.rowcount


def get_active_meeting_for_chat(parent_chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE parent_chat_id=%s AND status IN ('active','concluding','pending') "
            "ORDER BY created_at DESC LIMIT 1",
            (parent_chat_id,),
        ).fetchone()
        return dict(row) if row else None


def list_meetings(limit: int = 50, offset: int = 0,
                  agent: str | None = None,
                  status: str | None = None,
                  scope_user_sub: str | None = None,
                  created_by: str | None = None) -> list[dict]:
    """List meetings with optional filtering by agent, status, scope, and creator."""
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        # Match agent in JSON participants array or as moderator
        conditions.append('(participants LIKE %s OR moderator=%s)')
        params.extend([f'%"{agent}"%', agent])
    if status:
        conditions.append("status=%s")
        params.append(status)
    if scope_user_sub is not None:
        conditions.append("(scope='agent' OR (scope='user' AND created_by=%s))")
        params.append(scope_user_sub)
    if created_by:
        conditions.append("created_by=%s")
        params.append(created_by)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM meetings {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_meeting_count(agent: str | None = None,
                      status: str | None = None,
                      scope_user_sub: str | None = None,
                      created_by: str | None = None) -> int:
    """Count meetings matching filters."""
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        conditions.append('(participants LIKE %s OR moderator=%s)')
        params.extend([f'%"{agent}"%', agent])
    if status:
        conditions.append("status=%s")
        params.append(status)
    if scope_user_sub is not None:
        conditions.append("(scope='agent' OR (scope='user' AND created_by=%s))")
        params.append(scope_user_sub)
    if created_by:
        conditions.append("created_by=%s")
        params.append(created_by)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS cnt FROM meetings {where}", params).fetchone()
        return row["cnt"] if row else 0


def add_meeting_turn(meeting_id: str, round_number: int, turn_order: int,
                     agent: str, role: str, content: str, thinking: str,
                     tool_summary: str, session_id: str | None,
                     cost_usd: float) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO meeting_turns
               (meeting_id, round_number, turn_order, agent, role,
                content, thinking, tool_summary, session_id, cost_usd, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (meeting_id, round_number, turn_order, agent, role,
             content, thinking, tool_summary, session_id, cost_usd, now),
        )
        row_id = cur.fetchone()["id"]
        conn.commit()
        return row_id


def get_meeting_turns(meeting_id: str, since_round: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meeting_turns WHERE meeting_id=%s AND round_number>=%s "
            "ORDER BY round_number, turn_order",
            (meeting_id, since_round),
        ).fetchall()
        return [dict(r) for r in rows]
