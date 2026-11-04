"""The 1.9.1 audience columns migrate forward on a released install's
database: a task, a trigger and a notification written before the columns
existed come through the boot telling every member (``viewer``), a row
written after the boot takes the new default (``editor`` for a task and a
trigger, ``viewer`` for a notification), the role CHECKs refuse a word the
per-agent roles do not know, and a second boot changes nothing.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from storage import pg, schema as pg_schema
from storage.pg import get_conn

AUDIENCE = (("dynamic_tasks", "notify_audience"), ("triggers", "notify_audience"),
            ("notifications", "audience"))


def _old_shape() -> None:
    pg.close_pool()
    with get_conn() as conn:
        for table, column in AUDIENCE:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
        conn.commit()
    pg.close_pool()


def _migrate(times: int = 1) -> None:
    pg.close_pool()
    for _ in range(times):
        with get_conn() as conn:
            pg_schema.init_schema(conn)
            pg_schema.run_migrations(conn)
            conn.commit()
    pg.close_pool()


def _insert_rows(tag: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO dynamic_tasks (id, agent, name, prompt, task_type, created_at, scope) "
            "VALUES (%s, 'mig-agent', 'n', 'p', 'scheduled', NOW()::text, 'agent')", (f"t-{tag}",))
        conn.execute(
            "INSERT INTO triggers (id, slug, name, scope, agent, created_by, created_at, updated_at) "
            "VALUES (%s, %s, 'n', 'agent', 'mig-agent', 'user-manager', NOW()::text, NOW()::text)",
            (f"g-{tag}", f"slug-{tag}"))
        conn.execute(
            "INSERT INTO notifications (id, title, body, scope, target, source, notification_type, "
            "created_at) VALUES (%s, 't', 'b', 'agent', 'mig-agent', 'mcp', 'immediate', NOW()::text)",
            (f"n-{tag}",))
        conn.commit()


def _audiences(tag: str) -> tuple[str, str, str]:
    with get_conn() as conn:
        task = conn.execute("SELECT notify_audience FROM dynamic_tasks WHERE id=%s",
                            (f"t-{tag}",)).fetchone()
        trigger = conn.execute("SELECT notify_audience FROM triggers WHERE id=%s",
                               (f"g-{tag}",)).fetchone()
        notification = conn.execute("SELECT audience FROM notifications WHERE id=%s",
                                    (f"n-{tag}",)).fetchone()
    return task["notify_audience"], trigger["notify_audience"], notification["audience"]


def _cleanup(tags: list[str]) -> None:
    with get_conn() as conn:
        for tag in tags:
            conn.execute("DELETE FROM dynamic_tasks WHERE id=%s", (f"t-{tag}",))
            conn.execute("DELETE FROM triggers WHERE id=%s", (f"g-{tag}",))
            conn.execute("DELETE FROM notifications WHERE id=%s", (f"n-{tag}",))
        conn.commit()


def test_rows_written_before_the_columns_keep_telling_every_member():
    old, new = uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]
    _old_shape()
    try:
        _insert_rows(old)
        _migrate(times=2)
        assert _audiences(old) == ("viewer", "viewer", "viewer")
        _insert_rows(new)
        assert _audiences(new) == ("editor", "editor", "viewer")
        with get_conn() as conn:
            assert pg_schema.check_schema_drift(conn) == []
    finally:
        pg.close_pool()
        _cleanup([old, new])
        # The DROP and the ADD reordered the rows' columns: every pooled
        # session's cached plan of a SELECT * is stale, so the pools close.
        pg.close_pool(timeout=2.0)


def test_a_fresh_database_has_the_new_defaults_and_no_drift():
    tag = uuid.uuid4().hex[:8]
    _migrate()
    try:
        _insert_rows(tag)
        assert _audiences(tag) == ("editor", "editor", "viewer")
        with get_conn() as conn:
            assert pg_schema.check_schema_drift(conn) == []
    finally:
        _cleanup([tag])


@pytest.mark.parametrize("word", ["admin", "everyone", ""])
def test_the_checks_refuse_a_word_the_roles_do_not_know(word):
    tag = uuid.uuid4().hex[:8]
    _migrate()
    try:
        _insert_rows(tag)
        for table, column, key in (("dynamic_tasks", "notify_audience", f"t-{tag}"),
                                   ("triggers", "notify_audience", f"g-{tag}"),
                                   ("notifications", "audience", f"n-{tag}")):
            with get_conn() as conn, pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(f"UPDATE {table} SET {column}=%s WHERE id=%s", (word, key))
    finally:
        _cleanup([tag])
