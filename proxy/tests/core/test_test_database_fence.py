"""The suite's own fence: its per-test wipe only ever reaches a database the
suite owns, one whose name carries ``test``, whatever was imported first.
And its per-test reset: the schema is applied again exactly when a statement
may have changed it, and the wipe leaves nothing of the test before."""

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parents[2]


def _suite():
    """The loaded ``tests/conftest.py`` module, under whichever name pytest
    registered it."""
    target = _PROXY / "tests" / "conftest.py"
    for mod in list(sys.modules.values()):
        file = getattr(mod, "__file__", None)
        if file and Path(file).resolve() == target:
            return mod
    raise AssertionError("the suite's conftest is not loaded")


class _Info:
    def __init__(self, dbname: str) -> None:
        self.dbname = dbname


class _Conn:
    """A connection that records what it was asked and holds no rows."""

    def __init__(self, dbname: str) -> None:
        self.info = _Info(dbname)
        self.executed: list[str] = []

    def execute(self, sql, *_args):
        self.executed.append(sql)
        return self

    def fetchall(self):
        return []


def test_the_wipe_refuses_a_database_not_named_test():
    conn = _Conn("otodock")
    with pytest.raises(AssertionError, match="non-test database"):
        _suite()._truncate_all_tables(conn)
    assert conn.executed == []


def test_the_wipe_lists_the_tables_of_a_test_database():
    conn = _Conn("otodock_test_0123abcd_gw1")
    _suite()._truncate_all_tables(conn)
    assert len(conn.executed) == 1


def test_the_suite_points_config_at_its_own_database():
    import config

    assert config.DATABASE_URL == os.environ["DATABASE_URL"]
    assert "test" in config.DATABASE_URL.rsplit("/", 1)[-1]


def test_a_proxy_import_before_the_suite_is_re_pointed():
    """An in-process run that imported a proxy module first: ``config`` read
    config.env (or its default) before the suite's override existed, so the
    suite must re-point it. The run's own database is dropped at the end."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "PYTEST_XDIST_WORKER", "OTO_TEST_RUN_ID")}
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "import config\n"
        "before = config.DATABASE_URL\n"
        "import tests.conftest as suite\n"
        "import os, re\n"
        "after = config.DATABASE_URL\n"
        "suite._drop_run_databases(after, re.escape(os.environ['OTO_TEST_RUN_ID']), 0)\n"
        "print(before); print(after); print(os.environ['DATABASE_URL'])\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=_PROXY, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    before, after, env_url = out.stdout.strip().splitlines()[-3:]
    assert before != after
    assert after == env_url
    assert re.search(r"test", after.rsplit("/", 1)[-1])


# ── the schema is applied again exactly when a statement may have changed it ──

def _count(conn) -> int:
    mark, _writers = _suite()._ddl_mark(conn)
    conn.rollback()
    assert mark is not None, "the DDL count is not installed (a role that may not create an event trigger?)"
    return mark[2]


def test_the_mark_is_this_databases_and_rows_never_move_it(temp_db):
    from storage.pg import get_conn

    suite = _suite()
    with get_conn() as conn:
        mark, _writers = suite._ddl_mark(conn)
        assert mark == suite._applied_mark
        assert mark[0] == conn.info.dbname and "test" in mark[0]
        before = mark[2]
        conn.execute("INSERT INTO users (sub, email, name, role, created_at, last_login) "
                     "VALUES ('u-mark', 'm@test.com', 'M', 'member', 'now', 'now')")
        conn.execute("UPDATE users SET name = 'N' WHERE sub = 'u-mark'")
        conn.execute("DELETE FROM users WHERE sub = 'u-mark'")
        conn.commit()
        assert _count(conn) == before


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE fence_probe (id int)",
    "CREATE TABLE IF NOT EXISTS users (id int)",                  # a no-op still counts
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS name TEXT",       # as the migrations' own
    "CREATE INDEX fence_probe_idx ON users (name)",
    "CREATE TEMP TABLE fence_probe_temp (id int)",
], ids=["create", "create-noop", "alter-noop", "index", "temp"])
def test_a_statement_that_may_change_the_structure_moves_the_mark(temp_db, ddl):
    from storage.pg import get_conn

    with get_conn() as conn:
        before = _count(conn)
        conn.execute(ddl)
        conn.rollback()     # the count is a sequence: a rolled-back statement stays counted
        assert _count(conn) > before


def test_a_statement_that_fails_moves_the_mark(temp_db):
    """A failed CREATE INDEX CONCURRENTLY leaves an invalid index behind and
    never reaches the end event: the start event counts it."""
    import psycopg

    from storage.pg import get_conn

    with get_conn() as conn:
        before = _count(conn)
        with pytest.raises(psycopg.Error):
            conn.execute("ALTER TABLE users ADD COLUMN sub TEXT")
        conn.rollback()
        assert _count(conn) > before


def test_the_replica_role_and_a_role_with_no_grant_are_counted(temp_db):
    from storage.pg import get_conn

    with get_conn() as conn:
        before = _count(conn)
        conn.execute("SET LOCAL session_replication_role = 'replica'")
        conn.execute("CREATE TABLE fence_probe (id int)")
        conn.rollback()
        replica = _count(conn)
        assert replica > before
        # The role lives in this transaction only: the rollback takes it back.
        conn.execute("CREATE ROLE fence_probe_role NOLOGIN")
        conn.execute("GRANT CREATE ON SCHEMA public TO fence_probe_role")
        conn.execute("SET LOCAL ROLE fence_probe_role")
        conn.execute("CREATE TABLE fence_probe (id int)")
        conn.rollback()
        assert _count(conn) > replica


def test_a_changed_table_is_applied_again_and_the_mark_stored(temp_db):
    from storage import pg
    from storage.pg import get_conn

    suite = _suite()

    def _has_column(conn) -> bool:
        return conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'pinned_apps' AND column_name = 'hidden'").fetchone() is not None

    with get_conn() as conn:
        conn.execute("ALTER TABLE pinned_apps DROP COLUMN hidden")
        conn.commit()
        assert not _has_column(conn)
        mark = suite._settled_mark(conn)
        assert mark is not None and mark != suite._applied_mark
        applies = suite._schema_stats["applies"]
        suite._apply_schema(conn)
        assert _has_column(conn)
        conn.rollback()
        assert suite._schema_stats["applies"] == applies + 1
        assert suite._settled_mark(conn) == suite._applied_mark
        conn.rollback()
    pg.close_pool()     # a changed table's shape: no cached plan of the old one stays


def test_a_dropped_count_reads_as_no_mark_and_is_installed_again(temp_db):
    from storage.pg import get_conn

    suite = _suite()
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA {suite._HARNESS} CASCADE")
        conn.commit()
        assert suite._settled_mark(conn) is None
        suite._apply_schema(conn)
        assert suite._applied_mark is not None
        assert suite._settled_mark(conn) == suite._applied_mark
        conn.rollback()


def test_a_disabled_trigger_reads_as_no_mark(temp_db):
    from storage.pg import get_conn

    suite = _suite()
    with get_conn() as conn:
        conn.execute(f"ALTER EVENT TRIGGER {suite._HARNESS}_end DISABLE")
        conn.commit()
        assert suite._settled_mark(conn) is None
        suite._apply_schema(conn)
        assert suite._settled_mark(conn) == suite._applied_mark
        conn.rollback()


# ── the wipe leaves nothing of the test before ──────────────────────────────

def test_the_wipe_empties_every_table_through_its_foreign_keys(temp_db):
    from storage import database as db
    from storage.pg import get_conn

    suite = _suite()
    db.create_chat("chat-fence", "user-admin", "fence-agent", "default")
    with get_conn() as conn:
        conn.execute("INSERT INTO chat_messages (chat_id, role, content, created_at) "
                     "VALUES ('chat-fence', 'user', 'hello', 'now')")
        conn.commit()
        tables = suite._public_tables(conn)
        assert sorted(tables) == sorted(suite._applied_tables) and len(tables) > 50
        suite._truncate_all_tables(conn, suite._applied_tables)
        suite._seed_users(conn)
        # The wipe's own replica role ended with it: the seed ran under the origin's rules.
        assert conn.execute("SHOW session_replication_role").fetchone()["session_replication_role"] == "origin"
        conn.commit()
        left = {t: conn.execute(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"] for t in tables}
        conn.rollback()
    assert left.pop("users") == 4
    assert {t: n for t, n in left.items() if n} == {}


def _writer(url: str, hold_s: float | None, done: threading.Event) -> threading.Thread:
    """Another session that inserts a row and commits ``hold_s`` later (or
    holds the transaction until ``done`` is set)."""
    import psycopg

    started = threading.Event()

    def run() -> None:
        with psycopg.connect(url) as other:
            other.execute("INSERT INTO users (sub, email, name, role, created_at, last_login) "
                          "VALUES ('u-late', 'late@test.com', 'Late', 'member', 'now', 'now')")
            started.set()
            if hold_s is None:
                done.wait(10)
                other.rollback()
            else:
                time.sleep(hold_s)
                other.commit()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(10)
    return thread


def test_the_wipe_waits_for_a_writer_the_test_before_left_open(temp_db):
    import config
    from storage.pg import get_conn

    suite = _suite()
    waits = suite._schema_stats["waits"]
    thread = _writer(config.DATABASE_URL, 0.3, threading.Event())
    with get_conn() as conn:
        suite._settled_mark(conn)
        # The writer committed before the mark came back: the wipe takes its row.
        suite._truncate_all_tables(conn, suite._applied_tables)
        conn.commit()
        assert conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0
        conn.rollback()
    thread.join(10)
    assert suite._schema_stats["waits"] == waits + 1


def test_a_writer_that_never_ends_holds_the_wipe_for_its_bound_only(temp_db, monkeypatch):
    import config
    from storage.pg import get_conn

    suite = _suite()
    monkeypatch.setattr(suite, "_WRITERS_WAIT_S", 0.2)
    done = threading.Event()
    thread = _writer(config.DATABASE_URL, None, done)
    try:
        with get_conn() as conn:
            started = time.monotonic()
            suite._settled_mark(conn)
            waited = time.monotonic() - started
            conn.rollback()
        assert 0.2 <= waited < 5
    finally:
        done.set()
        thread.join(10)


# ── a store job the test before left on a lane ──────────────────────────────

def test_a_job_left_on_a_lane_ends_before_the_next_test_starts(temp_db):
    """What a cancelled task leaves behind: its job goes on on the lane's
    thread. The drain returns only after it."""
    from storage import pg

    suite = _suite()
    done = threading.Event()

    def _late() -> None:
        time.sleep(0.3)
        done.set()

    pg.db_executor(pg.LANE_BULK).submit(_late)
    waits = suite._schema_stats["lane_waits"]
    suite._drain_store_lanes()
    assert done.is_set()
    assert suite._schema_stats["lane_waits"] == waits + 1
    # Nothing left: the next drain passes at once.
    started = time.monotonic()
    suite._drain_store_lanes()
    assert time.monotonic() - started < 0.2
    assert suite._schema_stats["lane_waits"] == waits + 1


def test_a_lane_held_for_good_costs_its_wait_once(temp_db, monkeypatch):
    from storage import pg

    suite = _suite()
    monkeypatch.setattr(suite, "_LANE_WAIT_S", 0.2)
    release = threading.Event()
    lane = pg.db_executor(pg.LANE_BULK)
    lane.submit(release.wait, 30)
    try:
        started = time.monotonic()
        suite._drain_store_lanes()
        assert 0.2 <= time.monotonic() - started < 5
        assert id(lane) in suite._held_lanes
        started = time.monotonic()
        suite._drain_store_lanes()
        assert time.monotonic() - started < 0.15
    finally:
        release.set()
        suite._held_lanes.discard(id(lane))
        # The lanes are made again on their next use, and drained again.
        pg.shutdown_db_executor(wait=True)
    assert pg.db_executor(pg.LANE_BULK) is not lane
    suite._drain_store_lanes()
    assert not suite._held_lanes


# ── parts of the suite and a shuffled order ─────────────────────────────────

def _test_files() -> list[str]:
    return sorted(p.relative_to(_PROXY).as_posix() for p in (_PROXY / "tests").rglob("test_*.py"))


def test_every_test_file_runs_in_exactly_one_part():
    suite = _suite()
    files = _test_files()
    assert len(files) > 500
    assert {suite._shard_of(f, 1) for f in files} == {1}
    for parts in (2, 3, 4):
        placed = [suite._shard_of(f, parts) for f in files]
        assert set(placed) == set(range(1, parts + 1))
        # Near even by count, so no part is the whole suite in disguise.
        assert max(placed.count(n) for n in set(placed)) < len(files) * 0.75


@pytest.mark.parametrize("value", ["", "0/2", "3/2", "1", "1/0", "a/b", "1/2/3", "-1/2"])
def test_a_malformed_part_is_a_usage_error(value):
    if value == "":
        return  # no option: the whole suite
    with pytest.raises(pytest.UsageError):
        _suite()._parse_shard(value)


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


class _Config:
    """What the collection hook reads of a config: the option, the hook that
    reports what it left out."""

    def __init__(self, shard: str = "") -> None:
        self.shard = shard
        self.deselected: list = []
        self.hook = self

    def getoption(self, _name: str) -> str:
        return self.shard

    def pytest_deselected(self, items) -> None:
        self.deselected.extend(items)


def _collected(shard: str = "") -> tuple[list[str], list[str]]:
    """The hook run over two tests of every real test file: (kept, left out)."""
    items = [_Item(f"{f}::test_{n}") for f in _test_files() for n in ("a", "b")]
    config = _Config(shard)
    _suite().pytest_collection_modifyitems(config, items)
    return [i.nodeid for i in items], [i.nodeid for i in config.deselected]


def test_the_parts_split_the_collection_by_file_and_lose_nothing(monkeypatch):
    monkeypatch.delenv("OTODOCK_TEST_SHUFFLE", raising=False)
    whole, none = _collected()
    assert none == [] and len(whole) > 1000
    one, not_one = _collected("1/2")
    two, not_two = _collected("2/2")
    assert one == not_two and two == not_one
    assert sorted(one + two) == sorted(whole)
    assert {i.split("::")[0] for i in one}.isdisjoint({i.split("::")[0] for i in two})
    # The order inside a part is the collection's own.
    assert one == [i for i in whole if i in set(one)]


def test_a_seed_orders_the_collection_the_same_way_every_time(monkeypatch):
    monkeypatch.delenv("OTODOCK_TEST_SHUFFLE", raising=False)
    whole, _ = _collected()
    monkeypatch.setenv("OTODOCK_TEST_SHUFFLE", "7")
    first, _ = _collected()
    again, _ = _collected()
    part, _ = _collected("1/2")
    monkeypatch.setenv("OTODOCK_TEST_SHUFFLE", "8")
    other, _ = _collected()
    assert first == again and first != whole and sorted(first) == sorted(whole)
    assert other != first and sorted(other) == sorted(whole)
    assert sorted(part) == sorted(_collected_without_seed("1/2", monkeypatch))


def _collected_without_seed(shard: str, monkeypatch) -> list[str]:
    monkeypatch.delenv("OTODOCK_TEST_SHUFFLE")
    return _collected(shard)[0]
