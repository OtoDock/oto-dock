"""A person's own chats per agent and engine: what orders the engines they
see (``services/engines/engine_resolution.py``).

Counted: the chats the person opened themselves in the dashboard (the chat
page, the Android and iOS apps), on the engine each is on now. Never a task
run's chat, a delegated lane (``origin = 'delegated'``), the satellite's own
CLI session (``origin = 'otodock'``), a phone call, a Shared-only agent's
shared chats (owned by ``agent::<slug>``) or a chat that never named an
engine. The predicate is repeated literally in the query so the partial
index ``idx_chats_own_engine_use`` (``storage/chat/schema.init_chats``)
serves it under a generic plan too.

Synchronous (called via ``asyncio.to_thread`` from async code).
"""

from __future__ import annotations

from storage.pg import get_conn


def own_chat_counts(user_sub: str, since: str) -> dict[str, dict[str, int]]:
    """``{agent: {engine: chats}}`` for the person's own dashboard chats
    created at or after ``since`` (ISO UTC, the format ``create_chat``
    writes). A falsy sub counts nothing."""
    if not user_sub:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT agent, execution_path, COUNT(*) AS n
                 FROM chats
                WHERE user_sub = %s AND created_at >= %s
                  AND source_type = 'chat' AND origin = 'dashboard'
                  AND execution_path <> '' AND id NOT LIKE 'task-%%'
                GROUP BY agent, execution_path""",
            (user_sub, since),
        ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["agent"], {})[row["execution_path"]] = int(row["n"])
    return counts
