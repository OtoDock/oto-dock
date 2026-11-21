"""The permission-mode words from before 1.9 migrate forward on a released
install's database: a chat a 1.8 release stored in ``default`` or
``acceptEdits`` comes through the boot in ``ask`` or ``autoReview``, every
other stored mode (a NULL included) is left as it was, the column's default
becomes Ask, and a second boot changes nothing. The suite's database is the
1.9.1 shape, whose ``reply_reports`` step returns early: the rewrite runs
before it.
"""

from __future__ import annotations

import pytest

from storage import database as task_store, pg, schema as pg_schema
from storage.pg import get_conn

ROWS = {"c-default": "default", "c-accept": "acceptEdits", "c-dont": "dontAsk",
        "c-auto": "auto", "c-judge": "judge", "c-plan": "plan", "c-null": None}


def _migrate(times: int) -> None:
    pg.close_pool()
    for _ in range(times):
        with get_conn() as conn:
            pg_schema.init_schema(conn)
            pg_schema.run_migrations(conn)
            conn.commit()
    pg.close_pool()


def _column_default() -> str:
    with get_conn() as conn:
        return conn.execute(
            "SELECT column_default FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'chats' AND column_name = 'permission_mode'").fetchone()["column_default"]


def _modes() -> dict[str, str | None]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, permission_mode FROM chats WHERE id = ANY(%s)",
                            (list(ROWS),)).fetchall()
    return {r["id"]: r["permission_mode"] for r in rows}


@pytest.fixture
def old_default(temp_db):
    """The chats table as 1.9.1 made it: its mode column defaults to
    'default'. The suite's table gets its own default back whatever the
    test did, so no later test on this worker meets the old one."""
    with get_conn() as conn:
        conn.execute("ALTER TABLE chats ALTER COLUMN permission_mode SET DEFAULT 'default'")
        conn.commit()
    try:
        yield
    finally:
        pg.close_pool()
        with get_conn() as conn:
            conn.execute("ALTER TABLE chats ALTER COLUMN permission_mode SET DEFAULT 'ask'")
            conn.commit()


def test_the_old_words_are_rewritten_once_and_nothing_else_moves(old_default):
    for cid, mode in ROWS.items():
        task_store.create_chat(cid, "user-admin", "demo")
        with get_conn() as conn:
            conn.execute("UPDATE chats SET permission_mode = %s WHERE id = %s", (mode, cid))
            conn.commit()
    _migrate(1)
    assert _modes() == {**ROWS, "c-default": "ask", "c-accept": "autoReview"}
    assert _column_default().startswith("'ask'")
    with get_conn() as conn:
        conn.execute("INSERT INTO chats (id, user_sub, agent, created_at, updated_at) "
                     "VALUES ('c-new', 'user-admin', 'demo', NOW()::text, NOW()::text)")
        conn.commit()
    assert task_store.get_chat("c-new")["permission_mode"] == "ask"
    _migrate(1)
    assert _modes() == {**ROWS, "c-default": "ask", "c-accept": "autoReview"}


def test_a_fresh_table_is_made_with_ask(temp_db):
    assert _column_default().startswith("'ask'")
