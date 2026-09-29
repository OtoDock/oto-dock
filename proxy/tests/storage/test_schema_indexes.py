"""The indexes the hot task-run and chat lookups rely on.

Without them each of these was a sequential scan, several of them on the
event loop: the task-history list alone froze the proxy 2 s at 3k task chats
and 28 s at 30k runs. Each query is captured from its REAL store function,
then EXPLAINed twice with the same parameters: as sent (a custom plan) and as
a prepared statement under ``plan_cache_mode = force_generic_plan`` (psycopg
prepares a query server-side after five runs, and a generic plan cannot see
the parameter values). The expression index only matches when the query's
``COALESCE(started_at, '')`` is textually the same, so a rewrite of these
queries fails here first.
"""

from __future__ import annotations

import contextlib
import json
import re

import psycopg
import pytest
from psycopg import sql

import config
from storage import pg, schema
from storage.automation import db_tasks
from storage.chat import db_chats


def _seed(conn) -> None:
    now = "2026-09-26T10:00:00"
    chats, runs = [], []
    for i in range(400):
        cid = f"task-r{i}" if i % 2 else f"c{i}"
        chats.append((cid, f"u{i % 7}", f"agent{i % 5}", f"s{i}", now, now,
                      "task" if i % 2 else "chat",
                      f"proj{i % 9}" if i % 3 == 0 else "",
                      f"c{i - 1}" if i % 4 == 0 and i else "",
                      now if i % 5 == 0 else None,
                      "wake" if i == 7 else ""))
        runs.append((f"r{i}", f"t{i % 11}", f"agent{i % 5}", "cron", "completed",
                     f"2026-09-{1 + i % 25:02d}T10:00:00", f"sess{i % 50}", cid))
    conn.cursor().executemany(
        "INSERT INTO chats (id, user_sub, agent, session_id, created_at, updated_at, "
        "source_type, project_id, parent_chat_id, last_response_at, pending_delegate_wake) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", chats)
    conn.cursor().executemany(
        "INSERT INTO task_runs (id, task_id, agent, trigger_type, status, started_at, "
        "session_id, chat_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", runs)
    conn.execute("ANALYZE chats")
    conn.execute("ANALYZE task_runs")


class _Recorder:
    """Wraps a store module's ``get_conn`` and records every statement."""

    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    def get_conn(self):
        rec = self

        @contextlib.contextmanager
        def _cm():
            with pg.get_conn() as conn:
                class _Conn:
                    def execute(self, query, params=None, **kw):
                        rec.queries.append((query, tuple(params or ())))
                        return conn.execute(query, params, **kw)

                    def __getattr__(self, name):
                        return getattr(conn, name)

                yield _Conn()

        return _cm()


def _plans(query: str, params: tuple) -> tuple[str, str]:
    """(custom, generic) EXPLAIN JSON text for one captured statement."""
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        c.execute("SET enable_seqscan = off")
        custom = c.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone()[0]
        n = 0

        def _dollar(m):
            nonlocal n
            if m.group(0) == "%%":
                return "%"
            n += 1
            return f"${n}"

        prepared = re.sub(r"%%|%s", _dollar, query)
        c.execute("SET plan_cache_mode = force_generic_plan")
        c.execute(sql.SQL("PREPARE zz_idx AS {}").format(sql.SQL(prepared)))
        args = sql.SQL(", ").join(sql.Literal(p) for p in params)
        generic = c.execute(
            sql.SQL("EXPLAIN (FORMAT JSON) EXECUTE zz_idx({})").format(args)
            if params else sql.SQL("EXPLAIN (FORMAT JSON) EXECUTE zz_idx")).fetchone()[0]
        c.execute("DEALLOCATE zz_idx")
    return json.dumps(custom), json.dumps(generic)


@pytest.fixture
def seeded():
    with pg.get_conn() as conn:
        _seed(conn)
        conn.commit()


CASES = [
    ("idx_runs_chat_started", db_chats, "list_task_chats", ("agent1", "u1")),
    ("idx_runs_chat_started", db_chats, "list_task_chats", ("agent1", None)),
    ("idx_runs_chat_started", db_tasks, "get_run_for_chat", ("task-r3",)),
    ("idx_runs_chat_started", db_tasks, "get_runs_for_chats", (["task-r3", "task-r5"],)),
    # chat_id and a live status: with few live runs the status index is the
    # better plan, and either one keeps it off a sequential scan.
    (("idx_runs_chat_started", "idx_runs_status"), db_tasks, "has_live_run", ("task-r3",)),
    ("idx_runs_chat_started", db_tasks, "update_latest_run_status_for_chat",
     ("task-r3", "completed")),
    ("idx_runs_session", db_tasks, "get_session_cost", ("sess3",)),
    ("idx_runs_session", db_tasks, "get_run_by_session", ("sess3",)),
    ("idx_chats_session", db_chats, "get_chat_by_session", ("s3",)),
    ("idx_chats_project", db_chats, "list_chats_by_project", ("proj3",)),
    ("idx_chats_parent", db_chats, "list_chats_by_parent", ("c3",)),
    ("idx_chats_last_response", db_chats, "list_unread_finished_chats", ("2026-01-01",)),
    ("idx_chats_pending_wake", db_chats, "list_chats_with_pending_wakes", ()),
]


@pytest.mark.parametrize("index,module,fn,args", CASES,
                         ids=[f"{c[2]}-{i}" for i, c in enumerate(CASES)])
def test_the_hot_lookups_use_their_index(seeded, monkeypatch, index, module, fn, args):
    rec = _Recorder()
    monkeypatch.setattr(module, "get_conn", rec.get_conn)
    getattr(module, fn)(*args)
    assert rec.queries, f"{fn} ran no query"
    query, params = rec.queries[0]
    custom, generic = _plans(query, params)
    wanted = index if isinstance(index, tuple) else (index,)
    assert any(i in custom for i in wanted), f"{fn}: {wanted} not in the custom plan"
    assert any(i in generic for i in wanted), f"{fn}: {wanted} not in the generic plan"


def test_list_runs_by_session_uses_the_session_index(seeded, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(db_tasks, "get_conn", rec.get_conn)
    db_tasks.list_runs(limit=50, session_id="sess3")
    custom, generic = _plans(*rec.queries[0])
    assert "idx_runs_session" in custom and "idx_runs_session" in generic


_SORT = '"Node Type": "Sort"'


@pytest.mark.parametrize("kwargs", [
    {"agents_filter": ["agent1"]},       # a person whose reach is one agent
    {"agent": "agent1"},                 # the Tasks page's agent filter
    {"agent": "agent1", "agents_filter": ["agent1", "agent2"]},
])
def test_one_agents_run_history_is_read_in_index_order(seeded, monkeypatch, kwargs):
    """The Tasks page polls one agent's runs newest first every 15 s: the
    (agent, started_at DESC NULLS LAST) index must serve the ORDER BY
    itself. A Sort node means the index key and the ORDER BY disagree (a
    DESC key is NULLS FIRST) and every run of the agent is sorted."""
    rec = _Recorder()
    monkeypatch.setattr(db_tasks, "get_conn", rec.get_conn)
    db_tasks.list_runs(limit=50, **kwargs)
    custom, generic = _plans(*rec.queries[0])
    assert "idx_runs_agent_started" in custom and "idx_runs_agent_started" in generic
    assert _SORT not in custom and _SORT not in generic
    assert not any("idx_runs_agent\"" in p for p in (custom, generic))


def test_the_run_stats_read_the_day_through_the_started_at_index(seeded, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(db_tasks, "get_conn", rec.get_conn)
    db_tasks.get_stats()
    for query, params in rec.queries:
        if "started_at" in query:
            custom, generic = _plans(query, params)
            # The failed-today count may take the status index instead
            # (few failed rows); either keeps it off a sequential scan.
            wanted = ("idx_runs_started_at", "idx_runs_status")
            assert any(i in custom for i in wanted) and any(i in generic for i in wanted)


def test_the_single_column_agent_index_is_gone():
    with pg.get_conn() as conn:
        schema.init_schema(conn)
        conn.commit()
        names = {r["indexname"] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'task_runs'").fetchall()}
    assert "idx_runs_agent_started" in names and "idx_runs_agent" not in names


def test_an_invalid_index_is_rebuilt_at_boot():
    """An interrupted CREATE INDEX CONCURRENTLY leaves an INVALID index that
    CREATE INDEX IF NOT EXISTS would skip forever."""
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        c.execute("DROP INDEX IF EXISTS idx_runs_session")
        c.execute("INSERT INTO task_runs (id, task_id, agent, trigger_type, status, session_id) "
                  "VALUES ('d1','t','a','cron','completed','dup'), ('d2','t','a','cron','completed','dup')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("CREATE UNIQUE INDEX CONCURRENTLY idx_runs_session ON task_runs (session_id)")
        valid = c.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = 'idx_runs_session'::regclass").fetchone()[0]
        assert valid is False
    with pg.get_conn() as conn:
        schema.init_schema(conn)
        conn.commit()
        row = conn.execute(
            "SELECT indisvalid, indisunique FROM pg_index "
            "WHERE indexrelid = 'idx_runs_session'::regclass").fetchone()
    assert row == {"indisvalid": True, "indisunique": False}


def test_init_schema_twice_is_a_no_op():
    with pg.get_conn() as conn:
        schema.init_schema(conn)
        conn.commit()
        before = conn.execute("SELECT count(*) AS n FROM pg_indexes WHERE schemaname = current_schema()").fetchone()["n"]
        schema.init_schema(conn)
        schema.run_migrations(conn)
        conn.commit()
        after = conn.execute("SELECT count(*) AS n FROM pg_indexes WHERE schemaname = current_schema()").fetchone()["n"]
    assert before == after
