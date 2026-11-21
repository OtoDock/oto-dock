"""A reload, a move, or a continuation after a usage limit, that waits for
its chat's turn to end (``chat_pending_rebuilds``).

The waiter itself is a task of this process (``services/remote/chat_reload.py``,
``chat_move.py``). The row is what a restart finds: who asked, why, the
account choices of a reload, the place of a move or the session and account
of a continuation (``target``), and when the wait runs out. One per chat (a
later request replaces an earlier one, as the waiters do, but a continuation
never replaces another kind's row), deleted when the waiter ends, unless the
platform's own stop cut the wait. Synchronous.
"""

import json
from datetime import datetime, timedelta, timezone

from storage.pg import get_conn

RELOAD = "reload"
MOVE = "move"
LIMIT = "limit"


def save(chat_id: str, what: str, *, person: str, wait_s: float, reason: str = "",
         target: str = "", choices: dict[str, str] | None = None, replace: bool = True) -> None:
    """``replace`` False keeps a row the chat already has."""
    now = datetime.now(timezone.utc)
    on_conflict = ("""DO UPDATE SET what=EXCLUDED.what, person=EXCLUDED.person,
                   reason=EXCLUDED.reason, target=EXCLUDED.target, choices=EXCLUDED.choices,
                   deadline=EXCLUDED.deadline, created_at=EXCLUDED.created_at""" if replace
                   else "DO NOTHING")
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO chat_pending_rebuilds (chat_id, what, person, reason, target, choices,
                                                  deadline, created_at)
               SELECT %s, %s, %s, %s, %s, %s, %s, %s WHERE EXISTS (SELECT 1 FROM chats WHERE id=%s)
               ON CONFLICT (chat_id) {on_conflict}""",
            (chat_id, what, person, reason, target, json.dumps(choices or {}),
             (now + timedelta(seconds=wait_s)).isoformat(), now.isoformat(), chat_id),
        )
        conn.commit()


def clear(chat_id: str, what: str) -> None:
    """The waiter ended: its row goes (a later request of the other kind
    keeps its own)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_pending_rebuilds WHERE chat_id=%s AND what=%s",
                     (chat_id, what))
        conn.commit()


def take(chat_id: str) -> dict | None:
    """The chat's row, removed: whoever settles it does so once."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM chat_pending_rebuilds WHERE chat_id=%s RETURNING *", (chat_id,),
        ).fetchone()
        conn.commit()
    return _shaped(row)


def listed() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM chat_pending_rebuilds ORDER BY created_at").fetchall()
    return [r for r in (_shaped(row) for row in rows) if r]


def _shaped(row) -> dict | None:
    if not row:
        return None
    out = dict(row)
    try:
        out["choices"] = {str(k): str(v) for k, v in json.loads(out.get("choices") or "{}").items()}
    except (TypeError, ValueError, AttributeError):
        out["choices"] = {}
    return out


def seconds_left(row: dict) -> float:
    """What is left of the row's wait, 0 when it ran out."""
    try:
        deadline = datetime.fromisoformat(row["deadline"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
