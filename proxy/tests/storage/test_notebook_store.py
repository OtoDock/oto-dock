"""The Notebook's table: it arrives on a
released install's database at boot, its text rule, the hourly limit under
a lock, the soft delete that blanks the text, paging, and the retention cap
of 500 entries per agent.

Run: cd proxy && venv/bin/pytest tests/storage/test_notebook_store.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from storage import pg, schema as pg_schema
from storage.notebook import db_notebook
from storage.pg import get_conn


@pytest.fixture
def agents(temp_db):
    from storage.agents import agent_store
    for slug in ("nb-a", "nb-b"):
        agent_store.create_agent(slug, slug, created_by="user-admin")
    return ("nb-a", "nb-b")


def _seed(agent: str, n: int, *, start: datetime | None = None) -> None:
    """``n`` entries one minute apart, the newest at ``start``."""
    start = start or datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notebook_entries (id, agent, surface, text, created_at) "
            "SELECT %s || '-' || g, %s, 'task', 'entry ' || g, "
            "%s - make_interval(mins => g) FROM generate_series(0, %s - 1) g",
            (agent, agent, start, n),
        )
        conn.commit()


def _count(agent: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM notebook_entries WHERE agent = %s", (agent,),
        ).fetchone()
    return int(row["n"]) if row else 0


def test_the_table_comes_with_the_boot_on_a_database_without_it(temp_db):
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS notebook_entries")
        conn.commit()
    pg.close_pool()
    for _ in range(2):
        with get_conn() as conn:
            pg_schema.init_schema(conn)
            pg_schema.run_migrations(conn)
            conn.commit()
        pg.close_pool()
    with get_conn() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'notebook_entries'").fetchall()}
    assert cols == {"id", "agent", "author_sub", "surface", "text", "created_at",
                    "deleted_at", "deleted_by"}


def test_the_text_rule_holds_in_the_table(agents):
    with pytest.raises(psycopg.errors.CheckViolation), get_conn() as conn:
        conn.execute(
            "INSERT INTO notebook_entries (id, agent, text) VALUES ('x', 'nb-a', '')")
    with pytest.raises(psycopg.errors.CheckViolation), get_conn() as conn:
        conn.execute(
            "INSERT INTO notebook_entries (id, agent, text) VALUES ('y', 'nb-a', %s)",
            ("x" * 1201,))


def test_the_hourly_limit_refuses_the_next_post(agents):
    for i in range(3):
        assert db_notebook.insert_entry(f"e{i}", "nb-a", None, "task", "t", max_per_hour=3)
    assert db_notebook.insert_entry("e3", "nb-a", None, "task", "t", max_per_hour=3) is None
    assert db_notebook.insert_entry("f0", "nb-b", None, "task", "t", max_per_hour=3), \
        "another agent's limit is its own"


def test_an_hour_old_post_no_longer_counts(agents):
    _seed("nb-a", 3, start=datetime.now(timezone.utc) - timedelta(minutes=61))
    assert db_notebook.insert_entry("new", "nb-a", None, "task", "t", max_per_hour=3)


def test_a_deletion_keeps_the_row_and_blanks_the_text(agents):
    db_notebook.insert_entry("gone", "nb-a", "user-admin", "chat", "a secret", max_per_hour=20)
    assert db_notebook.soft_delete("gone", "user-manager")
    row = db_notebook.get_entry("gone")
    assert row is not None
    assert row["text"] == "" and row["deleted_by"] == "user-manager"
    assert row["deleted_at"] is not None
    assert not db_notebook.soft_delete("gone", "user-manager"), "a second delete finds nothing"
    assert db_notebook.latest(["nb-a"], limit=10) == []


def test_latest_pages_by_time_and_id(agents):
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        for i in ("a", "b", "c"):
            conn.execute(
                "INSERT INTO notebook_entries (id, agent, text, created_at) "
                "VALUES (%s, 'nb-a', %s, %s)", (i, i, now))
        conn.commit()
    first = db_notebook.latest(["nb-a"], limit=2)
    assert [r["id"] for r in first] == ["c", "b"]
    rest = db_notebook.latest(
        ["nb-a"], limit=2, before=(first[-1]["created_at"], first[-1]["id"]))
    assert [r["id"] for r in rest] == ["a"], "rows sharing a time page by id"


def test_latest_since_and_agents(agents):
    now = datetime.now(timezone.utc)
    _seed("nb-a", 5, start=now)
    _seed("nb-b", 2, start=now)
    rows = db_notebook.latest(["nb-a", "nb-b"], limit=50, since=now - timedelta(minutes=1, seconds=30))
    assert sorted(r["id"] for r in rows) == ["nb-a-0", "nb-a-1", "nb-b-0", "nb-b-1"]
    assert db_notebook.latest([], limit=5) == []


def test_the_retention_cap_keeps_the_newest_500_per_agent(agents):
    _seed("nb-a", 520)
    _seed("nb-b", 10)
    with get_conn() as conn:
        conn.execute("UPDATE notebook_entries SET deleted_at = now(), text = '' "
                     "WHERE id = 'nb-a-3'")
        conn.commit()
    assert db_notebook.prune_per_agent(500, dry_run=True) == 20
    assert _count("nb-a") == 520, "a dry run deletes nothing"
    assert db_notebook.prune_per_agent(500) == 20
    assert _count("nb-a") == 500 and _count("nb-b") == 10
    with get_conn() as conn:
        oldest = conn.execute(
            "SELECT id FROM notebook_entries WHERE agent = 'nb-a' "
            "ORDER BY created_at LIMIT 1").fetchone()
    assert oldest is not None and oldest["id"] == "nb-a-499"


def test_the_sweep_runs_the_cap_whatever_the_chat_toggle(agents, monkeypatch):
    from services.infra import retention
    from services.infra.retention import LiveSnapshot
    monkeypatch.setattr(retention, "_pass_tarball_gc", lambda stats, dry_run: None)
    _seed("nb-a", 503)
    stats = retention._run_sweep_sync(30, False, LiveSnapshot(), False)
    assert stats["notebook_entries_pruned"] == 3
    assert _count("nb-a") == 500


def test_an_agent_deleted_takes_its_entries(agents):
    from storage.agents import agent_store
    _seed("nb-a", 2)
    agent_store.delete_agent("nb-a")
    assert _count("nb-a") == 0
