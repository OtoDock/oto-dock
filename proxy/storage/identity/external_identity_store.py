"""The outside people an agent has spoken with (``external_identities``):
one row per channel and id, stored once, touched at every conversation's
start. A caller on the agent's phone line is the first channel; a link
visitor or a channel's member joins later with the same row shape. The row
is bookkeeping about a person who is not a platform user: when their tree
goes (the caller-data sweep, Forget all), the row goes with it.

Synchronous psycopg calls, run through ``asyncio.to_thread`` like the other
identity stores.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from storage.pg import get_conn

_CALLER_LABELS = ("caller:", "caller-pin:")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    return dict(r)


def touch(agent: str, channel: str, external_id: str, slug: str, *, verified: bool = False) -> str:
    """The row of this person, made or updated: ``last_seen`` now, one more
    conversation, ``verified_at`` now when a PIN verified them. ``slug`` is
    the tree's path component the caller resolved for the id. Returns the
    row's id (the chat row carries it)."""
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO external_identities
               (id, agent, channel, external_id, slug, first_seen, last_seen,
                verified_at, conversations)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
               ON CONFLICT (agent, channel, slug) DO UPDATE SET
                   external_id = EXCLUDED.external_id,
                   last_seen = EXCLUDED.last_seen,
                   conversations = external_identities.conversations + 1,
                   verified_at = CASE WHEN EXCLUDED.verified_at <> ''
                                      THEN EXCLUDED.verified_at
                                      ELSE external_identities.verified_at END
               RETURNING id""",
            (str(uuid.uuid4()), agent, channel, external_id, slug, now, now,
             now if verified else "", ),
        ).fetchone()
        conn.commit()
        return row["id"]


def get(agent: str, channel: str, slug: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM external_identities WHERE agent = %s AND channel = %s AND slug = %s",
            (agent, channel, slug),
        ).fetchone()
        return _row(row) if row else None


def get_by_id(identity_id: str) -> dict | None:
    if not identity_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM external_identities WHERE id = %s", (identity_id,),
        ).fetchone()
        return _row(row) if row else None


def list_for_agent(agent: str) -> list[dict]:
    """Every outside person of the agent, the most recent first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM external_identities WHERE agent = %s ORDER BY last_seen DESC, slug",
            (agent,),
        ).fetchall()
        return [_row(r) for r in rows]


def forget(agent: str, channel: str, slug: str) -> bool:
    """The row goes with the person's tree."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM external_identities WHERE agent = %s AND channel = %s AND slug = %s",
            (agent, channel, slug),
        )
        conn.commit()
        return cur.rowcount > 0


def caller_of_label(label: str) -> str:
    """The id a call-log label names, "" when it names no caller."""
    return next((label[len(p):] for p in _CALLER_LABELS if label.startswith(p)), "")


def callers_in_call_log() -> list[tuple[str, str, str, str, int]]:
    """``(agent, external_id, first, last, count)`` per caller the call log
    names (``caller:<id>`` and ``caller-pin:<id>`` rows), the two label forms
    of one id as two rows."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT agent, identity, MIN(started_at) AS first_seen,
                      MAX(started_at) AS last_seen, COUNT(*) AS n
               FROM phone_call_log
               WHERE identity LIKE 'caller:%%' OR identity LIKE 'caller-pin:%%'
               GROUP BY agent, identity""",
        ).fetchall()
    out = []
    for r in rows:
        label = r["identity"]
        ident = caller_of_label(label)
        if ident:
            out.append((r["agent"], ident, r["first_seen"], r["last_seen"], int(r["n"])))
    return out


def add_if_absent(agent: str, channel: str, external_id: str, slug: str, *,
                  first_seen: str, last_seen: str, conversations: int) -> bool:
    """A row for a person known from before the record existed (the first
    boot's backfill); nothing when the row is there."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO external_identities
               (id, agent, channel, external_id, slug, first_seen, last_seen,
                verified_at, conversations)
               VALUES (%s, %s, %s, %s, %s, %s, %s, '', %s)
               ON CONFLICT (agent, channel, slug) DO NOTHING""",
            (str(uuid.uuid4()), agent, channel, external_id, slug, first_seen, last_seen, conversations),
        )
        conn.commit()
        return cur.rowcount > 0
