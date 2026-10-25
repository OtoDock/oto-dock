"""The turn each chat has in flight (``chat_open_turns``).

A chat's pump writes its row when a turn starts and deletes it, by the turn's
token, when the turn ends; a process that ends without its shutdown leaves the
row, so the boot knows which turns it cut (``services/scheduler/cut_turns.py``).
``engine_open`` follows the engine's own turns inside the pump's turn: off
when the engine's result arrived, so a row left with it off is a turn that
had finished. The rows an engine turn saved before its end are named by
``early_ranges``, appended in the same transaction as those rows: a replay of
the turn after a restart deletes them before it writes the whole turn again,
and a turn nothing replays keeps them.

Synchronous: the pump calls these on the chat's writer lane, the boot and the
recovery on the DB executor or the lane.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, NamedTuple

import psycopg

from storage.chat.db_chats import _ROLE_USER, rebuild_chat_search
from storage.pg import get_conn

logger = logging.getLogger("otodock")


class Taken(NamedTuple):
    """A cut turn's record as its replay took it."""
    early: list[tuple[int, int]]
    started_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ranges_of(raw: str | None) -> list[tuple[int, int]]:
    try:
        return [(int(a), int(b)) for a, b in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        logger.warning("chat_open_turns: an unreadable early_ranges value: %r", raw)
        return []


def open_turn(chat_id: str, token: str, session_id: str) -> None:
    """The chat's turn ``token`` started: its row, replacing any older one,
    its engine turn open, no early ranges. A chat with no row writes
    nothing."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO chat_open_turns (chat_id, turn_token, session_id, started_at,
                                            engine_open, early_ranges)
               SELECT %s, %s, %s, %s, TRUE, '[]' WHERE EXISTS (SELECT 1 FROM chats WHERE id=%s)
               ON CONFLICT (chat_id) DO UPDATE SET turn_token=EXCLUDED.turn_token,
                   session_id=EXCLUDED.session_id, started_at=EXCLUDED.started_at,
                   engine_open=TRUE, early_ranges='[]'""",
            (chat_id, token, session_id, _now(), chat_id),
        )
        conn.commit()


def note_range_in(conn: psycopg.Connection, chat_id: str, token: str, after: int, upto: int) -> None:
    """Inside the caller's transaction (the rows' own): the engine turn saved
    the rows above ``after`` up to ``upto`` early."""
    conn.execute(
        "UPDATE chat_open_turns SET early_ranges = (early_ranges::jsonb || %s::jsonb)::text "
        "WHERE chat_id=%s AND turn_token=%s",
        (json.dumps([[after, upto]]), chat_id, token),
    )


def _engine_turn(chat_id: str, token: str, *, running: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE chat_open_turns SET engine_open=%s, early_ranges='[]' "
            "WHERE chat_id=%s AND turn_token=%s",
            (running, chat_id, token),
        )
        conn.commit()


def engine_turn_done(chat_id: str, token: str) -> None:
    """The engine's result arrived: the rows saved so far are the chat's for
    good (a replay covers a later engine turn only)."""
    _engine_turn(chat_id, token, running=False)


def engine_turn_began(chat_id: str, token: str) -> None:
    """A new engine turn runs inside the pump's turn (a queued message, an
    interaction, a prompt the engine took on its own): its ranges start over."""
    _engine_turn(chat_id, token, running=True)


def end_turn(chat_id: str, token: str) -> bool:
    """The turn ``token`` ended, or the boot found it finished: its row goes.
    A newer turn's row (another token) stays. True when a row was deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM chat_open_turns WHERE chat_id=%s AND turn_token=%s", (chat_id, token),
        )
        conn.commit()
    return bool(cur.rowcount)


def list_open() -> list[dict]:
    """Every row: at boot, the turns the last process left."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, turn_token, session_id, started_at, engine_open "
            "FROM chat_open_turns ORDER BY started_at",
        ).fetchall()
    return [dict(r) for r in rows]


def _closing_row_in(conn: psycopg.Connection[Any], chat_id: str, event_type: str, event_data: str,
                    abort_graceful: bool | None, since: str, turn_started: str) -> bool:
    """The closing row and the abort flags, inside the caller's transaction,
    unless a person wrote to the chat after ``since`` (the row would land
    after their message, and the flags would shape the next send) or the
    chat's newest row is a card of the same kind the cut turn wrote itself
    (written at or after ``turn_started``: a cut between a typed ending's row
    and the turn's end). An older card is an earlier turn's: a turn started
    since (a prompt the engine took with no row of its own) gets its row."""
    newer = conn.execute(
        "SELECT 1 FROM chat_messages WHERE chat_id=%s AND role=%s AND created_at > %s LIMIT 1",
        (chat_id, _ROLE_USER, since),
    ).fetchone()
    if newer:
        return False
    last = conn.execute(
        "SELECT event_type, event_data, created_at FROM chat_messages WHERE chat_id=%s "
        "ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if last and last["event_type"] == event_type and (last["created_at"] or "") >= turn_started:
        try:
            if json.loads(last["event_data"] or "{}").get("subtype") == json.loads(event_data).get("subtype"):
                return False
        except (TypeError, ValueError, AttributeError):
            pass
    now = _now()
    conn.execute(
        """INSERT INTO chat_messages (chat_id, role, content, event_type, event_data, author_sub, created_at)
           VALUES (%s, 'event', '', %s, %s, '', %s)""",
        (chat_id, event_type, event_data, now),
    )
    if abort_graceful is None:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (now, chat_id))
    else:
        conn.execute(
            "UPDATE chats SET last_turn_aborted=TRUE, last_abort_graceful=%s, updated_at=%s "
            "WHERE id=%s",
            (abort_graceful, now, chat_id),
        )
    return True


def close_cut(chat_id: str, token: str, event_type: str, event_data: str, *,
              abort_graceful: bool | None, since: str) -> bool:
    """The turn ``token`` was cut and nothing will replay it: in one
    transaction its row goes and, only when it was still there, the closing
    row lands (``_closing_row_in``; ``abort_graceful`` sets
    ``last_turn_aborted`` and ``last_abort_graceful``, None leaves both).
    Once per turn: a second call, or one after a new turn took the chat,
    writes nothing. The chat's search row is rebuilt (the turn's checkpoints
    left it). True when the closing row was written."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM chat_open_turns WHERE chat_id=%s AND turn_token=%s RETURNING started_at",
            (chat_id, token),
        ).fetchone()
        written = row is not None and _closing_row_in(
            conn, chat_id, event_type, event_data, abort_graceful, since, row["started_at"])
        conn.commit()
    if row is not None:
        rebuild_chat_search(chat_id)
    return written


def write_closing(chat_id: str, event_type: str, event_data: str, *,
                  abort_graceful: bool | None, since: str, turn_started: str) -> bool:
    """The closing row of a cut turn whose record the caller already took
    (an adoption whose replay never began; ``turn_started``: the record's
    ``started_at``). True when it was written."""
    with get_conn() as conn:
        written = _closing_row_in(conn, chat_id, event_type, event_data, abort_graceful, since,
                                  turn_started)
        conn.commit()
    rebuild_chat_search(chat_id)
    return written


def take_record(chat_id: str, token: str) -> Taken | None:
    """The cut turn ``token``'s record, taken before its replay pump writes
    its own: its early ranges and its start, None when another turn holds
    the chat."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM chat_open_turns WHERE chat_id=%s AND turn_token=%s "
            "RETURNING early_ranges, started_at",
            (chat_id, token),
        ).fetchone()
        conn.commit()
    return Taken(_ranges_of(row["early_ranges"]), row["started_at"]) if row else None
