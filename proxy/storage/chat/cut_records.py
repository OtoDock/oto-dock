"""The turns the platform itself cut (``chat_cut_turns``).

A turn that a stop, a crash or a lost machine ended has a record here: who
it ran as, the kind of session, its mode, the machine, the task run, the
plan it implemented and the card it waited on, copied from the turn's
open-turn row (``storage/chat/open_turns.py``), which is what a killed
process leaves. The continue pass (``services/scheduler/continue_pass.py``)
works from the records and settles each one once. A record holds no role:
whoever continues a chat reads the person's standing at that moment.

A record is taken with ``claim``, a compare-and-set in the store, so two
passes never take one. Synchronous: call these on the DB executor or the
chat's writer lane.
"""

import logging
from datetime import datetime, timedelta, timezone

from storage.chat.db_chats import _ROLE_USER
from storage.chat.open_turns import FACTS
from storage.pg import get_conn

logger = logging.getLogger("otodock")

#: The pass will take it.
PENDING = "pending"
#: Its machine has not said whether it still holds the session.
WAITING = "waiting"
#: The pass holds it.
RUNNING = "running"
#: Settled: the chat was continued, a person's own turn took its place, it
#: was dropped (``reason`` says why), or its machine still holds the session.
CONTINUED = "continued"
GAVE_WAY = "gave_way"
DROPPED = "dropped"
LEFT = "left"
UNSETTLED = (PENDING, WAITING, RUNNING)

#: What cut the turn: the platform's own stop, a process that ended with no
#: stop on record, a machine that lost the session.
CAUSE_STOP = "stop"
CAUSE_CRASH = "crash"
CAUSE_MACHINE = "machine"
#: A usage limit cut a turn nobody watched and the chat continued on another
#: account (``services/engines/pool_limits.py``): written settled, for the caps.
CAUSE_LIMIT = "limit"
#: A boot that takes nothing up found it (a first start, a restore, a long
#: downtime). Written here because the stamp a boot writes reads as caught up
#: to every later boot: what this boot decided outlives its process.
CAUSE_NOT_TAKEN_UP = "not_taken_up"

#: The reason a person's Stop settles a waiting run's record with
#: (``cut_turns.stopped_by_person``): a session it closed never comes back.
STOPPED_BY_PERSON = "a person stopped its run"

#: A settled record is kept this long, for the cap and for whoever reads why
#: a chat was not continued.
KEEP_DAYS = 30

_COPIED = ("turn_token", "session_id", *FACTS, "card", "engine_open", "started_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert(conn, chat_id: str, row: dict, cause: str, state: str) -> dict | None:
    # (the interpolated names are this module's own constants)
    values = [row.get(name) if name == "engine_open" else str(row.get(name) or "")
              for name in _COPIED]
    made = conn.execute(
        f"""INSERT INTO chat_cut_turns (chat_id, {", ".join(_COPIED)}, cause, cut_at, state)
            SELECT %s, {", ".join(["%s"] * len(_COPIED))}, %s, %s, %s
            WHERE EXISTS (SELECT 1 FROM chats WHERE id=%s)
            RETURNING *""",
        (chat_id, *values, cause, _now(), state, chat_id),
    ).fetchone()
    return dict(made) if made else None


def move_open(chat_id: str, token: str, *, cause: str, state: str) -> dict | None:
    """The open-turn row ``token`` becomes a record, in one transaction: the
    row goes, the record is written. None when another turn holds the chat
    or the chat is gone."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM chat_open_turns WHERE chat_id=%s AND turn_token=%s RETURNING *",
            (chat_id, token),
        ).fetchone()
        made = _insert(conn, chat_id, dict(row), cause, state) if row else None
        conn.commit()
    return made


def copy_open(row: dict, *, cause: str, state: str) -> dict | None:
    """A record of the open-turn row ``row``, which stays: a turn its machine
    may still replay takes that row itself (``open_turns.take_record``)."""
    with get_conn() as conn:
        made = _insert(conn, row["chat_id"], row, cause, state)
        conn.commit()
    return made


def add(chat_id: str, *, cause: str, state: str, **facts) -> dict | None:
    """A record with no open-turn row behind it: a run cut between its
    turns, a session its machine lost."""
    row = {"engine_open": False, **facts}
    with get_conn() as conn:
        made = _insert(conn, chat_id, row, cause, state)
        conn.commit()
    return made


def add_settled(chat_id: str, *, cause: str, state: str, reason: str = "", **facts) -> dict | None:
    """A record the pass never takes, written settled: a continuation that
    happened by another road (after a usage limit) counts toward the caps
    with the pass's own (``continued_in_a_row``, ``continued_since``), and is
    pruned with them (``tidy_at_boot``)."""
    row = {"engine_open": False, **facts}
    with get_conn() as conn:
        made = _insert(conn, chat_id, row, cause, state)
        if made is not None:
            conn.execute("UPDATE chat_cut_turns SET reason=%s, settled_at=%s WHERE id=%s",
                         (reason, _now(), made["id"]))
        conn.commit()
    return made


def stopped_sessions(session_ids: list[str]) -> dict[str, dict]:
    """The newest record a person's Stop settled for each of these sessions
    (``STOPPED_BY_PERSON``), by session id: one statement for a machine's
    whole report."""
    if not session_ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (session_id) * FROM chat_cut_turns "
            "WHERE session_id = ANY(%s) AND state = %s AND reason = %s "
            "ORDER BY session_id, id DESC",
            (list(session_ids), DROPPED, STOPPED_BY_PERSON),
        ).fetchall()
    return {r["session_id"]: dict(r) for r in rows}


def get(record_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_cut_turns WHERE id=%s", (record_id,)).fetchone()
    return dict(row) if row else None


def in_state(*states: str, machine_id: str | None = None) -> list[dict]:
    """The records in one of ``states``, oldest first, of one machine when
    ``machine_id`` is given."""
    query = "SELECT * FROM chat_cut_turns WHERE state = ANY(%s)"
    args: list = [list(states)]
    if machine_id is not None:
        query += " AND machine_id=%s"
        args.append(machine_id)
    with get_conn() as conn:
        rows = conn.execute(query + " ORDER BY id", args).fetchall()
    return [dict(r) for r in rows]


def unsettled_chats() -> set[str]:
    """The chats with a record nobody settled: a sweep that would start a
    session on one leaves it to the pass."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM chat_cut_turns WHERE state = ANY(%s)", (list(UNSETTLED),),
        ).fetchall()
    return {r["chat_id"] for r in rows}


def newest_of(chat_id: str) -> dict | None:
    """The chat's newest record, settled or not: what cut its last turn."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_cut_turns WHERE chat_id=%s ORDER BY id DESC LIMIT 1", (chat_id,),
        ).fetchone()
    return dict(row) if row else None


def unsettled_of(chat_id: str) -> dict | None:
    """The chat's newest unsettled record."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_cut_turns WHERE chat_id=%s AND state = ANY(%s) "
            "ORDER BY id DESC LIMIT 1", (chat_id, list(UNSETTLED)),
        ).fetchone()
    return dict(row) if row else None


def claim(record_id: int) -> dict | None:
    """Take a ``pending`` record for the pass: its row, now ``running`` with
    one more attempt counted, or None when someone else took or settled it."""
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE chat_cut_turns SET state=%s, attempts=attempts+1 "
            "WHERE id=%s AND state=%s RETURNING *", (RUNNING, record_id, PENDING),
        ).fetchone()
        conn.commit()
    return dict(row) if row else None


def name_run(record_id: int, run_id: str) -> None:
    """The record is the cut turn of the run ``run_id``."""
    with get_conn() as conn:
        conn.execute("UPDATE chat_cut_turns SET run_id=%s WHERE id=%s", (run_id, record_id))
        conn.commit()


def release(record_id: int) -> None:
    """A claimed record whose start did not happen goes back to ``pending``."""
    _move(record_id, PENDING, (RUNNING,))


def make_pending(record_id: int, *, taken: bool = False, cause: str = "") -> bool:
    """A ``waiting`` record whose machine said the session is gone.
    ``taken``: a record a holder took up and whose turn its machine cut, from
    the holder's state (``waiting``: an adoption's replay, which counts as an
    attempt; ``running``: the pass, whose claim counted one). ``cause``: what
    cut it this time, kept on a record the boot did not take up."""
    if not taken:
        return _move(record_id, PENDING, (WAITING,))
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_cut_turns SET state=%s, "
            "attempts = attempts + CASE WHEN state=%s THEN 1 ELSE 0 END, "
            "cause = CASE WHEN cause=%s OR %s='' THEN cause ELSE %s END "
            "WHERE id=%s AND state = ANY(%s)",
            (PENDING, WAITING, CAUSE_NOT_TAKEN_UP, cause, cause, record_id, [WAITING, RUNNING]),
        )
        conn.commit()
    return bool(cur.rowcount)


def settle(record_id: int, state: str, reason: str = "") -> bool:
    """Settle an unsettled record. False when it was settled already."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_cut_turns SET state=%s, reason=%s, settled_at=%s "
            "WHERE id=%s AND state = ANY(%s)", (state, reason, _now(), record_id, list(UNSETTLED)),
        )
        conn.commit()
    return bool(cur.rowcount)


def _move(record_id: int, to: str, frm: tuple[str, ...]) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE chat_cut_turns SET state=%s WHERE id=%s AND state = ANY(%s)",
                           (to, record_id, list(frm)))
        conn.commit()
    return bool(cur.rowcount)


def tidy_at_boot() -> tuple[int, int, int]:
    """Boot, before anything reads the records: a record the last process
    held goes back to ``pending``, of several unsettled records of one chat
    only the newest stays (``supersede_older``: one the pass had claimed counts
    as continued, the others are dropped), and settled records older
    than ``KEEP_DAYS`` go. Returns ``(taken back, superseded, pruned)``."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    with get_conn() as conn:
        back = conn.execute("UPDATE chat_cut_turns SET state=%s WHERE state=%s",
                            (PENDING, RUNNING)).rowcount
        conn.commit()
    older = supersede_older()
    with get_conn() as conn:
        pruned = conn.execute(
            "DELETE FROM chat_cut_turns WHERE settled_at <> '' AND settled_at < %s", (cutoff,),
        ).rowcount
        conn.commit()
    return back, older, pruned


def mark_not_taken_up() -> int:
    """A boot that takes nothing up says so on every unsettled record, in one
    statement. Returns how many."""
    with get_conn() as conn:
        marked = conn.execute("UPDATE chat_cut_turns SET cause=%s WHERE state = ANY(%s)",
                              (CAUSE_NOT_TAKEN_UP, list(UNSETTLED))).rowcount
        conn.commit()
    return marked


def supersede_older() -> int:
    """Of several unsettled records of one chat only the newest stays. One
    the pass had claimed was a start, and the later cut is how it ended, so
    it counts toward the cap as continued: a turn that brings the platform
    down is not continued for ever. The rest are dropped. Returns how many."""
    with get_conn() as conn:
        older = conn.execute(
            "UPDATE chat_cut_turns c SET state=CASE WHEN c.attempts > 0 THEN %s ELSE %s END, "
            "reason=%s, settled_at=%s "
            "WHERE c.state = ANY(%s) AND EXISTS (SELECT 1 FROM chat_cut_turns n "
            "WHERE n.chat_id=c.chat_id AND n.id > c.id AND n.state = ANY(%s))",
            (CONTINUED, DROPPED, "a later turn of the chat was cut", _now(),
             list(UNSETTLED), list(UNSETTLED)),
        ).rowcount
        conn.commit()
    return older


def continued_in_a_row(chat_id: str) -> int:
    """The chat's records the pass continued since a person last wrote to
    it: the cap's count."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM chat_cut_turns WHERE chat_id=%s AND state=%s AND cut_at > "
            "COALESCE((SELECT max(created_at) FROM chat_messages WHERE chat_id=%s AND role=%s), '')",
            (chat_id, CONTINUED, chat_id, _ROLE_USER),
        ).fetchone()
    return int(row["n"]) if row else 0


def continued_since(chat_id: str, hours: float) -> int:
    """The chat's records the pass continued in the last ``hours``."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM chat_cut_turns WHERE chat_id=%s AND state=%s AND cut_at > %s",
            (chat_id, CONTINUED, since),
        ).fetchone()
    return int(row["n"]) if row else 0


def person_wrote_since(chat_id: str, since: str) -> bool:
    """A person's message landed on the chat after ``since``."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM chat_messages WHERE chat_id=%s AND role=%s AND created_at > %s LIMIT 1",
            (chat_id, _ROLE_USER, since),
        ).fetchone()
    return row is not None
