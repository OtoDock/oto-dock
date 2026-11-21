"""A person's pinned chats: one person's own mark on one chat of their chat
history, to come back to it.

The rows are ``pinned_chats`` (``storage/chat/schema.init_chats``), keyed by
the person and the chat. A pin goes with its chat and with its person (both
keys cascade) and by its person's unpin. Nothing else removes one: a session
that is cleaned or closed leaves the chat's row, and the pin is the chat's.
Who may pin is the route's (``api/agents/chats.py``), never this module's.

Synchronous (called via ``asyncio.to_thread`` from async code).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import psycopg

from storage.pg import get_conn

#: The listings' condition on a chat row ``c``: among the pins of the person
#: in its one parameter. The pins are one read of their key and the chats are
#: reached by theirs, so the scan reads the pinned rows alone, never a long
#: history row by row (``tests/storage/test_schema_indexes.py`` measures it).
PINNED_BY = "c.id = ANY(ARRAY(SELECT p.chat_id FROM pinned_chats p WHERE p.user_sub = %s))"


def pin(user_sub: str, chat_id: str) -> bool:
    """Pin the chat for the person. True when it is pinned after the call
    (the first time stays on a second pin), False when the chat or the
    person no longer exists."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO pinned_chats (user_sub, chat_id, pinned_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_sub, chat_id) DO NOTHING",
                (user_sub, chat_id, now),
            )
            conn.commit()
    except psycopg.errors.ForeignKeyViolation:
        return False
    return True


def unpin(user_sub: str, chat_id: str) -> None:
    """Remove the person's own pin of the chat, if there is one."""
    with get_conn() as conn:
        conn.execute("DELETE FROM pinned_chats WHERE user_sub = %s AND chat_id = %s",
                     (user_sub, chat_id))
        conn.commit()


def pinned_ids(user_sub: str, chat_ids: Sequence[str]) -> set[str]:
    """The person's pins among ``chat_ids``. A falsy sub has none."""
    if not user_sub or not chat_ids:
        return set()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM pinned_chats WHERE user_sub = %s AND chat_id = ANY(%s)",
            (user_sub, list(chat_ids)),
        ).fetchall()
    return {r["chat_id"] for r in rows}
