"""Meeting and meeting-turn queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).
"""

import contextlib
import json
from datetime import datetime, timezone
from typing import Any

from storage.chat import meeting_status
from storage.pg import get_conn


# ---------------------------------------------------------------------------
# Meetings — the status vocabulary is storage/chat/meeting_status.py
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
            "WHERE created_by=%s AND status = ANY(%s)",
            (created_by, sorted(meeting_status.LIVE)),
        ).fetchall()
    total = 0
    for r in rows:
        with contextlib.suppress(ValueError, TypeError):
            total += len(_json.loads(r["participants"]) or [])
    return total


_UPDATABLE = frozenset({"status", "current_round", "active_participants", "summary",
                        "cost_usd", "concluded_at", "parent_session_id"})


def _update_sql(fields: dict) -> tuple[str, list[Any]]:
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    status = updates.get("status")
    if status is not None and status not in meeting_status.STATUSES:
        raise ValueError(f"invalid meeting status: {status!r}")
    if not updates:
        return "", []
    return ", ".join(f"{k}=%s" for k in updates), list(updates.values())


def update_meeting(meeting_id: str, **fields) -> bool:
    """Write the named columns; a ``status`` outside
    ``meeting_status.STATUSES`` is refused (the writers keep their own
    transition guards; this is membership)."""
    set_clause, values = _update_sql(fields)
    if not set_clause:
        return False
    with get_conn() as conn:
        conn.execute(f"UPDATE meetings SET {set_clause} WHERE id=%s", values + [meeting_id])
        conn.commit()
        return True


def update_meeting_if(meeting_id: str, statuses, **fields) -> bool:
    """``update_meeting`` for a row whose status is one of ``statuses`` at
    the moment of the write, in one statement: the orchestrator's
    read-then-write steps (a paused meeting resumed by the moderator, an
    end, the conclusion) run on the DB executor now, so two of them may
    interleave; the condition keeps a later end from being overwritten by
    an earlier resume. False when the row is elsewhere."""
    set_clause, values = _update_sql(fields)
    if not set_clause:
        return False
    with get_conn() as conn:
        row = conn.execute(
            f"UPDATE meetings SET {set_clause} WHERE id=%s AND status = ANY(%s) RETURNING id",
            values + [meeting_id, sorted(statuses)],
        ).fetchone()
        conn.commit()
        return row is not None


def remove_active_participant(meeting_id: str, agent_slug: str, statuses) -> list[str] | None:
    """Take ``agent_slug`` out of ``active_participants`` under the row's
    lock (two agents leaving at once must both leave) while the status is
    one of ``statuses``. The remaining list, or None when the row is
    elsewhere or the agent was not active."""
    import json as _json
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, active_participants FROM meetings WHERE id=%s FOR UPDATE",
            (meeting_id,),
        ).fetchone()
        if not row or row["status"] not in statuses:
            conn.rollback()
            return None
        try:
            active = list(_json.loads(row["active_participants"] or "[]"))
        except (ValueError, TypeError):
            active = []
        if agent_slug not in active:
            conn.rollback()
            return None
        active.remove(agent_slug)
        conn.execute("UPDATE meetings SET active_participants=%s WHERE id=%s",
                     (_json.dumps(active), meeting_id))
        conn.commit()
        return active


def mark_orphaned_meetings_failed() -> int:
    """Mark meetings stuck in active/pending/concluding as failed (proxy restart recovery).

    Returns the number of rows updated.  Idempotent — safe to call on every
    startup even if no meetings are orphaned.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE meetings SET status=%s, concluded_at=%s "
            "WHERE status = ANY(%s)",
            (meeting_status.FAILED, now, sorted(meeting_status.LIVE)),
        )
        conn.commit()
        return cur.rowcount


def get_active_meeting_for_chat(parent_chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE parent_chat_id=%s AND status = ANY(%s) "
            "ORDER BY created_at DESC LIMIT 1",
            (parent_chat_id, sorted(meeting_status.LIVE)),
        ).fetchone()
        return dict(row) if row else None


def chat_has_meeting_event(chat_id: str, subtype: str, meeting_id: str) -> bool:
    """Whether the chat holds a persisted ``system`` row of ``subtype`` for
    ``meeting_id``. ``subtype`` comes from the caller (``ws/wire_events``;
    a SQL literal reads no vocabulary). ``event_data`` is TEXT and ``''`` on
    most rows, so a LIKE prefilters on the default ``json.dumps``
    separators and the meeting id is compared after parsing."""
    if not (chat_id and subtype and meeting_id):
        return False
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_data FROM chat_messages "
            "WHERE chat_id=%s AND event_type='system' AND event_data LIKE %s",
            (chat_id, f'%"subtype": {json.dumps(subtype)}%'),
        ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["event_data"] or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("meeting_id") == meeting_id:
            return True
    return False


def _meeting_conditions(agent, status, scope_user_sub, created_by,
                        readable_agents) -> tuple[str, list[Any]]:
    """The WHERE the list and its count share, so a page and its total
    agree. ``scope_user_sub`` keeps a caller to agent-scope rows and their
    own user-scope rows; ``readable_agents`` (a person's agents, a no-user
    session's own agent and its edge targets; None for an admin or the
    master key) narrows the agent-scope rows to meetings whose EVERY
    participant the caller can read: access to one participant never opens
    a meeting with others. ``participants`` is always a JSON array written
    by ``create_meeting``; an empty one is nobody's."""
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        # Match agent in JSON participants array or as moderator
        conditions.append('(participants LIKE %s OR moderator=%s)')
        params.extend([f'%"{agent}"%', agent])
    if status:
        conditions.append("status=%s")
        params.append(status)
    if scope_user_sub is not None and readable_agents is not None:
        conditions.append(
            "((scope='user' AND created_by=%s) OR (scope='agent' AND participants <> '[]' "
            "AND participants::jsonb <@ to_jsonb(%s::text[])))")
        params.extend([scope_user_sub, list(readable_agents)])
    elif scope_user_sub is not None:
        conditions.append("(scope='agent' OR (scope='user' AND created_by=%s))")
        params.append(scope_user_sub)
    if created_by:
        conditions.append("created_by=%s")
        params.append(created_by)
    return (f"WHERE {' AND '.join(conditions)}" if conditions else ""), params


def list_meetings(limit: int = 50, offset: int = 0,
                  agent: str | None = None,
                  status: str | None = None,
                  scope_user_sub: str | None = None,
                  created_by: str | None = None,
                  readable_agents: list[str] | None = None) -> list[dict]:
    """List meetings with optional filtering by agent, status, scope, creator
    and the caller's readable agents (``_meeting_conditions``)."""
    where, params = _meeting_conditions(agent, status, scope_user_sub, created_by,
                                        readable_agents)
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
                      created_by: str | None = None,
                      readable_agents: list[str] | None = None) -> int:
    """Count meetings matching the list's filters."""
    where, params = _meeting_conditions(agent, status, scope_user_sub, created_by,
                                        readable_agents)
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS cnt FROM meetings {where}", params).fetchone()
        return row["cnt"] if row else 0


def list_live_meetings_created_by(sub: str) -> list[dict]:
    """The person's meetings that are not over (the offboarding end)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meetings WHERE created_by=%s AND NOT (status = ANY(%s))",
            (sub, sorted(meeting_status.TERMINAL)),
        ).fetchall()
        return [dict(r) for r in rows]


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
