"""The record of each conversation in an agent's Conversations tab
(``conversation_records``) and the notes from conversations
(``outside_notes``).

A record is made at a call's warmup for every phone-owned conversation: the
line the call came on, its direction, the outside person it was with. The
wrap-up after the call writes the rest: its state, the outcome, the
follow-ups and what it wrote. A call on a route tied to a person is that
person's own chat and has no record. A record goes with its chat (the
foreign key), and so do the notes made from it.

Synchronous psycopg calls, run through ``run_db`` / ``asyncio.to_thread``
like the other chat stores.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn

# The line a conversation came on.
LINE_CALLER = "caller"   # an outside person, a withheld number and a Shared-only agent's caller too
LINE_AGENT = "agent"     # the agent's own line: the caller runs as the agent itself
LINE_SHARED = "shared"   # no route matched: the route-less fallback
LINE_PERSON = "person"   # a 1.9.1 call on a route tied to a person, kept for the managers
LINES = (LINE_CALLER, LINE_AGENT, LINE_SHARED, LINE_PERSON)
#: The lines an editor reads; a ``person`` line stays a manager's.
EDITOR_LINES = (LINE_CALLER, LINE_AGENT, LINE_SHARED)
#: The lines whose caller is an external identity: their wrap-up follows the
#: agent's switch (``outside_store.get_wrapup``). On the agent's own line the
#: caller acts as the agent.
EXTERNAL_LINES = (LINE_CALLER, LINE_SHARED)

# The wrap-up's state.
WRAPUP_PENDING = "pending"
WRAPUP_RUNNING = "running"
WRAPUP_DONE = "done"
WRAPUP_SKIPPED = "skipped"
WRAPUP_FAILED = "failed"
#: The agent's managers turned the wrap-up off: nothing ran.
WRAPUP_OFF = "off"
WRAPUP_OPEN = (WRAPUP_PENDING, WRAPUP_RUNNING)

#: The outcome the platform writes when the person said nothing.
NO_ANSWER = "No answer."

#: The notes from conversations an agent keeps, the newest first, and a
#: note's length.
NOTES_KEPT = 40
NOTE_MAX_CHARS = 200

_JSON_FIELDS = ("follow_ups", "wrote")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    out = dict(r)
    for key in _JSON_FIELDS:
        if key in out and isinstance(out[key], str):
            try:
                out[key] = json.loads(out[key] or "[]")
            except ValueError:
                out[key] = []
    return out


def create(chat_id: str, agent: str, *, line: str, direction: str, identity_id: str = "") -> None:
    """The record of a conversation, made at its warmup; nothing when it is
    there (a reconnect of the same call)."""
    if line not in LINES:
        raise ValueError(f"unknown line {line!r}")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO conversation_records
                   (chat_id, agent, line, direction, identity_id, started_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (chat_id) DO NOTHING""",
            (chat_id, agent, line, direction, identity_id, _now()),
        )
        conn.commit()


def get(chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversation_records WHERE chat_id = %s", (chat_id,),
        ).fetchone()
        return _row(row) if row else None


def by_run(run_id: str) -> dict | None:
    """The record a wrap-up run writes into."""
    if not run_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversation_records WHERE wrapup_run_id = %s", (run_id,),
        ).fetchone()
        return _row(row) if row else None


def claim_wrapup(chat_id: str) -> dict | None:
    """Mark the record's wrap-up pending, one more attempt, and return the
    record; None when there is no record or its wrap-up was claimed before
    (a second teardown of the same call, a wrap-up that ended): a wrap-up
    runs once at most."""
    with get_conn() as conn:
        row = conn.execute(
            """UPDATE conversation_records
               SET wrapup = %s, wrapup_attempts = wrapup_attempts + 1,
                   ended_at = CASE WHEN ended_at = '' THEN %s ELSE ended_at END
               WHERE chat_id = %s AND wrapup = ''
               RETURNING *""",
            (WRAPUP_PENDING, _now(), chat_id),
        ).fetchone()
        conn.commit()
        return _row(row) if row else None


def set_wrapup(chat_id: str, state: str, *, run_id: str | None = None,
               outcome: str | None = None) -> None:
    """The wrap-up's state, its run, and the outcome when the platform writes
    it (no answer, a failure's reason)."""
    sets, params = ["wrapup = %s"], [state]
    if run_id is not None:
        sets.append("wrapup_run_id = %s")
        params.append(run_id)
    if outcome is not None:
        sets.append("outcome = %s")
        params.append(outcome)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE conversation_records SET {', '.join(sets)} WHERE chat_id = %s",
            (*params, chat_id),
        )
        conn.commit()


def record_outcome(chat_id: str, outcome: str, follow_ups: list[str]) -> None:
    """What the wrap-up recorded with its tool."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversation_records SET outcome = %s, follow_ups = %s WHERE chat_id = %s",
            (outcome, json.dumps(follow_ups), chat_id),
        )
        conn.commit()


def add_wrote(chat_id: str, item: dict) -> None:
    """One thing the wrap-up wrote (a memory topic, a file), for the card
    under the conversation."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wrote FROM conversation_records WHERE chat_id = %s FOR UPDATE", (chat_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return
        try:
            wrote = json.loads(row["wrote"] or "[]")
        except ValueError:
            wrote = []
        if item not in wrote:
            wrote.append(item)
        conn.execute(
            "UPDATE conversation_records SET wrote = %s WHERE chat_id = %s",
            (json.dumps(wrote[-50:]), chat_id),
        )
        conn.commit()


def wrapup_task_id(chat_id: str) -> str:
    """The task id every run of a conversation's wrap-up carries."""
    return f"wrapup-{chat_id}"


def unfinished() -> list[dict]:
    """The records whose wrap-up is still open: the boot's pass reads them."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM conversation_records WHERE wrapup IN (%s, %s)", WRAPUP_OPEN,
        ).fetchall()
        return [_row(r) for r in rows]


def calls_with_no_person(owner: str) -> list[dict]:
    """The phone conversations that name no outside person, each with its
    session, its Codex thread and the call log's label for its session.
    ``owner`` is the phone owner."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.agent, c.session_id, c.codex_thread_id, c.created_at,
                      COALESCE(l.identity, '') AS label
               FROM chats c
               LEFT JOIN LATERAL (
                   SELECT p.identity FROM phone_call_log p
                   WHERE c.session_id IS NOT NULL AND c.session_id <> ''
                     AND p.session_id = c.session_id
                   ORDER BY p.started_at DESC LIMIT 1) l ON TRUE
               WHERE c.user_sub = %s AND c.source_type = 'phone'
                 AND c.external_identity_id = ''""",
            (owner,),
        ).fetchall()
        return [dict(r) for r in rows]


def name_person(chat_id: str, identity_id: str) -> None:
    """Name the outside person of a conversation from before the record
    named them: its chat and its record, on the caller line."""
    with get_conn() as conn:
        conn.execute("UPDATE chats SET external_identity_id = %s WHERE id = %s", (identity_id, chat_id))
        conn.execute("UPDATE conversation_records SET identity_id = %s, line = %s WHERE chat_id = %s",
                     (identity_id, LINE_CALLER, chat_id))
        conn.commit()


def for_identity(agent: str, identity_id: str) -> list[dict]:
    """Every record of an outside person's conversations on the agent."""
    if not identity_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM conversation_records WHERE agent = %s AND identity_id = %s "
            "ORDER BY started_at DESC",
            (agent, identity_id),
        ).fetchall()
        return [_row(r) for r in rows]


def chats_of_identity(agent: str, identity_id: str, *, owner: str) -> list[dict]:
    """The phone conversations of the agent with one outside person, with
    what deleting them needs (the session, the Codex thread, the wrap-up's
    run). ``owner`` is the phone owner."""
    if not identity_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.agent, c.session_id, c.codex_thread_id, c.created_at,
                      COALESCE(r.wrapup, '') AS wrapup, COALESCE(r.wrapup_run_id, '') AS wrapup_run_id
               FROM chats c LEFT JOIN conversation_records r ON r.chat_id = c.id
               WHERE c.agent = %s AND c.user_sub = %s AND c.source_type = 'phone'
                 AND (c.external_identity_id = %s OR r.identity_id = %s)
               ORDER BY c.created_at""",
            (agent, owner, identity_id, identity_id),
        ).fetchall()
        return [dict(r) for r in rows]


def _list_filter(agent: str, *, owner: str, lines, person: str,
                 since: str) -> tuple[str, list[Any]]:
    sql = (" FROM chats c JOIN conversation_records r ON r.chat_id = c.id"
           " WHERE c.agent = %s AND c.user_sub = %s AND r.line = ANY(%s)")
    params: list[Any] = [agent, owner, list(lines)]
    if person:
        sql += " AND r.identity_id = %s"
        params.append(person)
    if since:
        sql += " AND c.created_at >= %s"
        params.append(since)
    return sql, params


_LIST_COLUMNS = ("c.*, r.line, r.direction, r.identity_id AS person_id, r.outcome, "
                 "r.follow_ups, r.wrapup, r.wrote, r.ended_at")


def list_for_agent(agent: str, *, owner: str, lines=LINES, person: str = "", since: str = "",
                   offset: int = 0, limit: int = 50) -> list[dict]:
    """The conversations of the agent's Conversations tab, the most recent
    first, each chat row with its record. ``owner`` is the phone owner
    (``visibility.PHONE_CHAT_OWNER``, which this layer does not import)."""
    where, params = _list_filter(agent, owner=owner, lines=lines, person=person, since=since)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS}{where} ORDER BY c.updated_at DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
        return [_row(r) for r in rows]


def count_for_agent(agent: str, *, owner: str, lines=LINES, person: str = "",
                    since: str = "") -> int:
    where, params = _list_filter(agent, owner=owner, lines=lines, person=person, since=since)
    with get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n{where}", params).fetchone()["n"]


# --- notes from conversations --------------------------------------------------

def add_note(agent: str, chat_id: str, identity_id: str, text: str) -> None:
    """The conversation's one note for the team, dated today: a second note
    of the same conversation replaces its first, so one wrap-up cannot push
    out the others. The agent keeps its newest :data:`NOTES_KEPT`."""
    day = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM outside_notes WHERE chat_id = %s", (chat_id,))
        conn.execute(
            "INSERT INTO outside_notes (agent, identity_id, chat_id, day, text) "
            "VALUES (%s, %s, %s, %s, %s)",
            (agent, identity_id, chat_id, day, text[:NOTE_MAX_CHARS]),
        )
        conn.execute(
            """DELETE FROM outside_notes WHERE agent = %s AND id NOT IN (
                   SELECT id FROM outside_notes WHERE agent = %s ORDER BY id DESC LIMIT %s)""",
            (agent, agent, NOTES_KEPT),
        )
        conn.commit()


def notes_for(agent: str) -> list[dict]:
    """The agent's notes from conversations, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT day, text FROM outside_notes WHERE agent = %s ORDER BY id",
            (agent,),
        ).fetchall()
        return [dict(r) for r in rows]


def notes_of_identity(agent: str, identity_id: str) -> list[dict]:
    if not identity_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT day, text FROM outside_notes WHERE agent = %s AND identity_id = %s ORDER BY id",
            (agent, identity_id),
        ).fetchall()
        return [dict(r) for r in rows]


def forget_notes(agent: str, identity_id: str) -> int:
    """A forgotten person's notes, gone."""
    if not identity_id:
        return 0
    with get_conn() as conn:
        n = conn.execute(
            "DELETE FROM outside_notes WHERE agent = %s AND identity_id = %s",
            (agent, identity_id),
        ).rowcount
        conn.commit()
        return n
