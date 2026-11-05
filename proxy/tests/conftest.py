"""Shared test fixtures for proxy tests.

Uses a DEDICATED PostgreSQL test database (otodock_test) to avoid
destroying production data. Each test starts from emptied tables; the schema
is applied once per database and again only after a test changed it.

IMPORTANT: Tests NEVER run against the production database.
Set TEST_DATABASE_URL to override the test DB location.
"""

import contextlib
import os
import random
import re
import sys
import threading
import zlib
from pathlib import Path
from urllib.parse import urlparse as _urlparse, urlunparse as _urlunparse

import pytest

# Add proxy root to sys.path so imports work like they do in production
_PROXY_DIR = Path(__file__).parent.parent
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

def _ensure_test_database(url: str) -> None:
    """Create the target test database if missing and mark it disposable.

    Connects to the ``postgres`` maintenance DB on the same server. The test DB
    is throwaway, so ``synchronous_commit`` is turned off (durability we don't
    need) to drop the per-commit fsync. Guarded to only ever touch ``*_test*``
    databases, so it can never create/alter the real dev database. If the
    maintenance DB is unreachable (locked-down role), we assume the DB was
    provisioned out of band (dev-setup / CI) and carry on.
    """
    import psycopg

    p = _urlparse(url)
    dbname = p.path.lstrip("/")
    assert "test" in dbname, f"refusing to manage non-test database {dbname!r}"
    admin_url = _urlunparse(p._replace(path="/postgres"))
    try:
        with psycopg.connect(admin_url, autocommit=True) as c:
            exists = c.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            if not exists:
                c.execute(f'CREATE DATABASE "{dbname}"')
            c.execute(f'ALTER DATABASE "{dbname}" SET synchronous_commit TO off')
    except psycopg.Error:
        pass


# Force tests to use a separate database — NEVER the dev/production one.
# Default: same server as the dev DB but database name 'otodock_test'.
_test_url = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://otodock:otodock@localhost:5432/otodock_test",
)

# Under pytest-xdist each worker gets its OWN database (…_gw0, …_gw1, …) so
# parallel workers never share rows or block each other on the per-test wipe.
#
# Concurrent pytest RUNS on one machine must not share state either — the
# per-test wipe (temp_db) deletes a sibling run's rows/files mid-test when
# two runs land on the same _gwN names (observed as the "ws-dashboard
# flake": the longest-lived, most DB-heavy tests died first, different
# modules each run, always green in isolation). So the name also carries a
# per-RUN id, minted ONCE by the first process of the run (serial run or
# xdist controller) and inherited by workers through the environment. The
# id embeds the run's start epoch (hex) so the stale-sweep below can
# age-gate: connection-count alone is NOT enough protection — a sibling
# run that is starting RIGHT NOW has a created-but-not-yet-pooled window
# in which its DBs look droppable (that exact race hung a proof run).
import time

_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER")
_run_uid = os.environ.get("OTO_TEST_RUN_ID")
if not _run_uid:
    _run_uid = f"{int(time.time()):08x}{os.getpid():x}"
    os.environ["OTO_TEST_RUN_ID"] = _run_uid
_pr = _urlparse(_test_url)
_db_suffix = f"_{_run_uid}" + (f"_{_xdist_worker}" if _xdist_worker else "")
_test_url = _urlunparse(
    _pr._replace(path=f"/{_pr.path.lstrip('/')}{_db_suffix}")
)

# A run's DBs are dropped at ITS OWN session end (pytest_sessionfinish
# below); the startup sweep only reaps what a killed run leaked.
_SWEEP_MIN_AGE_S = 2 * 3600


def _run_id_age_s(run_id: str) -> float | None:
    """Age of a run id whose first 8 chars are the start epoch in hex.
    None = unparseable (pre-epoch-format leftovers → treat as stale)."""
    try:
        started = int(run_id[:8], 16)
    except ValueError:
        return None
    if not (1_500_000_000 < started < time.time() + 3600):
        return None
    return time.time() - started


def _drop_run_databases(url: str, run_pat: str, min_age_s: float) -> None:
    """Drop per-run test DBs matching ``run_pat`` (a regex on the run-id
    part). Age-gated via the epoch embedded in the run id; a DB with live
    connections additionally refuses DROP and is skipped."""
    import contextlib
    import re

    import psycopg

    p = _urlparse(url)
    base = re.sub(r"_[0-9a-f]+(_gw\d+)?$", "", p.path.lstrip("/"))
    assert "test" in base, f"refusing to sweep non-test databases {base!r}"
    pat = re.compile(rf"^{re.escape(base)}_({run_pat})(_gw\d+)?$")
    admin_url = _urlunparse(p._replace(path="/postgres"))
    try:
        with psycopg.connect(admin_url, autocommit=True) as c:
            rows = c.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s",
                (f"{base}_%",),
            ).fetchall()
            for (name,) in rows:
                m = pat.match(name)
                if not m:
                    continue
                age = _run_id_age_s(m.group(1))
                if age is not None and age < min_age_s:
                    continue  # possibly a run that is starting/running now
                with contextlib.suppress(Exception):
                    # in-use by a live run → DROP refuses → leave it
                    c.execute(f'DROP DATABASE "{name}"')
    except psycopg.Error:
        pass  # locked-down maintenance role — CI provisions out of band


if not _xdist_worker:
    _drop_run_databases(_test_url, r"[0-9a-f]+", _SWEEP_MIN_AGE_S)

_ensure_test_database(_test_url)


def _assert_test_db_reachable(url: str) -> None:
    """Fail FAST when the test database can't be reached.

    Without this, an unreachable/wrong-credential test DB surfaces as
    psycopg_pool silently retrying the connection forever inside the first
    fixture — indistinguishable from a deadlock. Abort collection with the
    actual error and the knob to fix it instead.
    """
    import psycopg

    try:
        psycopg.connect(url, connect_timeout=5).close()
    except Exception as e:
        raise RuntimeError(
            f"test database unreachable: {url!r} → {e}\n"
            "Set TEST_DATABASE_URL to a reachable Postgres (a dedicated "
            "test instance; the DB name must contain 'test')."
        ) from e


_assert_test_db_reachable(_test_url)
os.environ["DATABASE_URL"] = _test_url

# The override above reaches ``config`` only when this file runs before the
# proxy is imported. An in-process ``pytest.main()`` that imported a proxy
# module first already holds ``config.DATABASE_URL`` from config.env (or the
# default): the live database. Re-point the loaded module and drop any pool
# built on it, so the per-test wipe can only reach a disposable database;
# ``_assert_disposable`` is the fence behind this for every other path.
_config_loaded = sys.modules.get("config")
if _config_loaded is not None and getattr(_config_loaded, "DATABASE_URL", "") != _test_url:
    _config_loaded.DATABASE_URL = _test_url
    _pg_loaded = sys.modules.get("storage.pg")
    if _pg_loaded is not None:
        _pg_loaded.close_pool()

# Redirect AGENTS_DIR to a temp scratch path so tests that exercise the
# real filesystem (community-agent installer, agent-create endpoint) don't
# pollute the dev install. Tests that don't touch the FS aren't affected.
# Per-worker under xdist AND per-run (same collision story as the DB names
# above — the per-test wipe deleted a concurrent run's files mid-rename).
# Stale dirs from finished/killed runs are reaped by age; the 24h grace
# can never touch a live run.
import shutil
import tempfile

_agents_dir_name = f"otodock-test-agents-{_run_uid}"
if _xdist_worker:
    _agents_dir_name += f"-{_xdist_worker}"
_test_agents_root = Path(tempfile.gettempdir()) / _agents_dir_name

if not _xdist_worker:
    for _stale in Path(tempfile.gettempdir()).glob("otodock-test-agents-*"):
        try:
            if _stale.is_dir() and time.time() - _stale.stat().st_mtime > 86400:
                shutil.rmtree(_stale, ignore_errors=True)
        except OSError:
            pass

_test_agents_root.mkdir(parents=True, exist_ok=True)

# The folders config reads and writes at import (config.env with the secrets
# it generates, sessions/, the preview and upload folders) are the run's own
# too, beside the agents root: never a worktree's root, never the live
# install's config.env in the main checkout. Assigned, not defaulted: an
# xdist worker inherits the controller's value.
_platform_dir = Path(tempfile.gettempdir()) / f"{_agents_dir_name}-platform"
_platform_dir.mkdir(parents=True, exist_ok=True)
os.environ["PLATFORM_CONFIG_DIR"] = os.environ["PLATFORM_DATA_DIR"] = str(_platform_dir)

# Override config.AGENTS_DIR directly — relying on PLATFORM_DATA_DIR alone
# doesn't help if config was already imported by another module's
# side-effect (e.g. via pre-conftest registration in pytest collection).
import config as _config

_config.AGENTS_DIR = _test_agents_root
# The sessions dir (index.json, prompt files, per-user MCP configs) lives
# BESIDE the agents root, never inside it: the suite must never rewrite the
# live install's sessions/index.json (the T1 dev install runs from this
# checkout), and the per-test wipe of the agents root must not take the
# sessions dir with it. The name keeps the agents-root prefix, so the
# age-gated startup sweep above reaps it too.
_config.SESSIONS_DIR = Path(tempfile.gettempdir()) / f"{_agents_dir_name}-sessions"
_config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
# The recover bin likewise: the live install's bin sits beside its agents
# root, and a capture during a test would land there.
_config.RECOVER_BIN_DIR = Path(tempfile.gettempdir()) / f"{_agents_dir_name}-recover-bin"
if "core.session.session_state" in sys.modules:
    _ss = sys.modules["core.session.session_state"]
    _ss._SESSION_INDEX = _config.SESSIONS_DIR / "index.json"
    _ss._SECURITY_INDEX = _config.SESSIONS_DIR / "security_index.json"

# Core reaches the permission authority through a door the authority's
# module installs when imported (the app imports it before any session), so
# every test process imports it here, as the call-time import used to. After
# the redirection above: the import captures config.AGENTS_DIR in the path
# policy.
import api.hooks.permission  # noqa: F401
import auth.path_policy as _path_policy

assert _path_policy._AGENTS_DIR == _test_agents_root.resolve()

# The stores' post-commit steps (an agent's repo and quota, a person's
# folders and memory scope), installed as the app's start installs them.
from services.agents import agent_provisioning

agent_provisioning.install()


def pytest_addoption(parser):
    parser.addoption(
        "--shard", default="", metavar="I/N",
        help="run part I of N of the suite: the test files whose path hashes to it "
             "(whole files, every file in exactly one part)",
    )


def _parse_shard(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)/(\d+)", value)
    if not match or not 1 <= int(match.group(1)) <= int(match.group(2)):
        raise pytest.UsageError(f"--shard takes I/N with 1 <= I <= N, got {value!r}")
    return int(match.group(1)), int(match.group(2))


def _shard_of(path: str, parts: int) -> int:
    """The part (1..parts) a test file runs in: a hash of its path as pytest
    names it, so every host and every xdist worker agrees, a file's tests
    stay together and a new file needs no list."""
    return zlib.crc32(path.encode("utf-8")) % parts + 1


def pytest_configure(config):
    shard = config.getoption("--shard")
    if shard:
        _parse_shard(shard)
        if os.environ.get("OTODOCK_LOADTEST") == "1":
            raise pytest.UsageError("--shard with OTODOCK_LOADTEST=1: the load harness runs whole and alone")


def pytest_report_header(config):
    lines = []
    if config.getoption("--shard"):
        lines.append(f"shard: {config.getoption('--shard')} of the test files, by path")
    if os.environ.get("OTODOCK_TEST_SHUFFLE"):
        lines.append(f"order: shuffled, OTODOCK_TEST_SHUFFLE={os.environ['OTODOCK_TEST_SHUFFLE']}")
    return lines


def pytest_collection_modifyitems(config, items):
    shard = config.getoption("--shard")
    if shard:
        part, parts = _parse_shard(shard)
        keep, drop = [], []
        for item in items:
            mine = _shard_of(item.nodeid.split("::", 1)[0], parts) == part
            (keep if mine else drop).append(item)
        if drop:
            config.hook.pytest_deselected(items=drop)
            items[:] = keep
    # A test that passes alone and fails in the suite depends on what ran
    # before it: OTODOCK_TEST_SHUFFLE=<seed> runs the collected tests in a
    # seeded random order (the same in every xdist worker) to find it.
    seed = os.environ.get("OTODOCK_TEST_SHUFFLE", "")
    if seed:
        random.Random(seed).shuffle(items)


def pytest_sessionfinish(session, exitstatus):
    """Controller/serial only: drop this run's own databases + scratch dirs.

    Best-effort — a worker whose pool hasn't closed yet makes its DROP fail
    (skipped); the next run's age-gated startup sweep reaps any leftovers.
    """
    # Loop-guard inventory (OTODOCK_DB_LOOP_GUARD=count): every store call
    # issued from an event-loop thread during the run, as file:line — the
    # explicit backlog of loop-side DB calls still to convert (the testing
    # page, "Event-loop DB guard"). Written per worker so xdist runs merge.
    try:
        from storage import pg as _pg
        _hits = _pg.loop_guard_hits()
        if _hits:
            _out = os.environ.get("OTODOCK_DB_LOOP_GUARD_OUT", "")
            _lines = [f"{n:6d}  {f}:{ln}  {fn}" for (f, ln, fn), n in
                      sorted(_hits.items(), key=lambda kv: -kv[1])]
            if _out:
                with open(_out, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(_lines) + "\n")
            else:
                print("\n[db-loop-guard] loop-side store calls:\n" + "\n".join(_lines))
    except (ImportError, OSError):
        pass
    _stats_out = os.environ.get("OTODOCK_TEST_SCHEMA_STATS", "")
    if _stats_out and _schema_stats["tests"]:
        import json
        with contextlib.suppress(OSError), open(_stats_out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"worker": _xdist_worker or "serial", **_schema_stats}) + "\n")
    # Stop the run_db lanes, then close the pools: their worker threads are
    # non-daemon, so left open they stall interpreter exit (about 20 s per
    # test process) and keep this run's databases busy, so the DROP below
    # would skip them.
    try:
        from storage import pg as _pg
        _pg.shutdown_db_executor()
        _pg.close_pool(timeout=2.0)
    except Exception:  # noqa: BLE001  # the session's teardown must not mask the run's own result
        pass
    if _xdist_worker:
        return
    import re

    _drop_run_databases(_test_url, re.escape(_run_uid), min_age_s=0.0)
    for _d in Path(tempfile.gettempdir()).glob(f"otodock-test-agents-{_run_uid}*"):
        shutil.rmtree(_d, ignore_errors=True)


def _assert_disposable(conn) -> None:
    """The connection must be on a database the suite owns, one whose name
    carries ``test``: everything the per-test wipe does goes through here."""
    dbname = conn.info.dbname or ""
    assert "test" in dbname, f"refusing to touch non-test database {dbname!r}"


def _public_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return [r["tablename"] for r in rows]


def _truncate_all_tables(conn, tables=None):
    """Wipe every row from the public-schema tables for a clean per-test slate
    (``tables``: the list read when the schema was applied; read here without).

    Uses DELETE, NOT ``TRUNCATE``: TRUNCATE forces a synchronous per-file fsync
    (``DataFileImmediateSync``) for every table it touches, and this fixture
    runs once per test (autouse), so on a durable server that per-table fsync
    dominates the whole suite's wall-clock. DELETE on these near-empty test
    tables skips it entirely. Behaviour matches the old TRUNCATE — all rows
    gone, sequences NOT reset (plain TRUNCATE also keeps sequences).

    FK ordering is sidestepped by disabling replication-role triggers for the
    DELETEs; ``SET LOCAL`` auto-resets on commit/rollback, so no state leaks
    back to the pooled connection.

    One text, one round trip (a statement per table cost five times the
    wipe). A text with no parameters goes out unprepared, so it can hold
    many statements; it must stay outside any pipeline.
    """
    _assert_disposable(conn)
    if tables is None:
        tables = _public_tables(conn)
    if not tables:
        return
    conn.execute(
        "SET LOCAL session_replication_role = 'replica';"
        + "".join(f'DELETE FROM "{t}";' for t in tables)
        + "SET LOCAL session_replication_role = 'origin';"
    )


# The schema is applied once per database, then again only when its structure
# may have changed: a sequence in a schema of the suite's own counts every DDL
# statement the database sees (two event triggers call nextval), and a test
# starts with an apply when the count is not the one this process stored.
#   - both events: a failed CREATE INDEX CONCURRENTLY leaves an invalid index
#     and never reaches ddl_command_end;
#   - ENABLE ALWAYS: DDL under session_replication_role = replica counts too;
#   - the function lives in the suite's schema with the definer's rights, so a
#     recreated public schema keeps it and a role with no grant can run DDL;
#   - a sequence is not transactional: rolled-back DDL counts.
_HARNESS = "otodock_test_harness"
_HARNESS_SQL = (
    f"CREATE SCHEMA IF NOT EXISTS {_HARNESS}",
    f"CREATE SEQUENCE IF NOT EXISTS {_HARNESS}.ddl_seq",
    f"CREATE OR REPLACE FUNCTION {_HARNESS}.note_ddl() RETURNS event_trigger "
    "LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog "
    f"AS $$ BEGIN PERFORM nextval('{_HARNESS}.ddl_seq'); END $$",
    f"DROP EVENT TRIGGER IF EXISTS {_HARNESS}_start",
    f"CREATE EVENT TRIGGER {_HARNESS}_start ON ddl_command_start "
    f"EXECUTE FUNCTION {_HARNESS}.note_ddl()",
    f"ALTER EVENT TRIGGER {_HARNESS}_start ENABLE ALWAYS",
    f"DROP EVENT TRIGGER IF EXISTS {_HARNESS}_end",
    f"CREATE EVENT TRIGGER {_HARNESS}_end ON ddl_command_end "
    f"EXECUTE FUNCTION {_HARNESS}.note_ddl()",
    f"ALTER EVENT TRIGGER {_HARNESS}_end ENABLE ALWAYS",
    # A sequence never read has no last value: the count starts at one.
    f"SELECT nextval('{_HARNESS}.ddl_seq')",
)
# The database's name and the sequence's oid ride the mark: two databases, or
# one dropped and created again under its name, never share one. ``writers``
# is not part of it: the other sessions of this database that hold a write
# transaction open (a previous test's thread still at work).
_MARK_SQL = (
    "SELECT current_database() AS db, "
    f"to_regclass('{_HARNESS}.ddl_seq')::oid AS seq, "
    f"pg_sequence_last_value(to_regclass('{_HARNESS}.ddl_seq')) AS count, "
    "(SELECT count(*) FROM pg_event_trigger "
    f"  WHERE evtname IN ('{_HARNESS}_start', '{_HARNESS}_end') AND evtenabled = 'A') AS triggers, "
    "(SELECT count(*) FROM pg_stat_activity "
    "  WHERE datname = current_database() AND pid <> pg_backend_pid() "
    "    AND backend_type = 'client backend' AND backend_xid IS NOT NULL) AS writers"
)
_WRITERS_WAIT_S = 2.0

# What this process applied last: the mark and the public tables it left.
_applied_mark: tuple | None = None
_applied_tables: list[str] = []
# How often the schema was applied and the wipe waited, and after which test
# (OTODOCK_TEST_SCHEMA_STATS names a file the session's end appends them to).
_schema_stats: dict = {"tests": 0, "applies": 0, "waits": 0, "lane_waits": 0, "after": []}
_previous_test = ""


def _ddl_mark(conn) -> tuple[tuple | None, int]:
    """The database's DDL mark (None when the count cannot be trusted: no
    sequence, never read, a trigger gone or disabled) and how many other
    sessions hold a write transaction open."""
    row = conn.execute(_MARK_SQL).fetchone()
    if row["count"] is None or row["triggers"] != 2:
        return None, row["writers"]
    return (row["db"], row["seq"], row["count"]), row["writers"]


def _settled_mark(conn) -> tuple | None:
    """The mark once no other session of this database holds a write
    transaction open, or after ``_WRITERS_WAIT_S``: rows a previous test's
    thread commits after the wipe would stay for this test. The activity
    view is frozen for a transaction, so each look ends its own."""
    mark, writers = _ddl_mark(conn)
    if writers:
        _schema_stats["waits"] += 1
        deadline = time.monotonic() + _WRITERS_WAIT_S
        while writers and time.monotonic() < deadline:
            conn.rollback()
            time.sleep(0.01)
            mark, writers = _ddl_mark(conn)
    return mark


# A store job runs on a lane's thread, not on the test's loop: when the loop
# ends and cancels the task that awaited it, the job goes on, and what it
# reads or writes then belongs to the test before. The schema's apply used to
# give such a job 190 ms by accident. Each test now starts once every lane
# has finished what it was given: as many gate jobs as the lane has workers,
# which can all run at once only when no worker holds an earlier job.
_LANE_WAIT_S = 2.0
# Lanes given up on: a worker held for good (a test that froze its database
# on purpose) must not cost every later test the wait. A new lane (the pools
# closed, the lanes stopped) is drained again.
_held_lanes: set[int] = set()


def _drain_store_lanes() -> None:
    from concurrent.futures import wait as wait_all

    from storage import pg

    for lane in list(pg._executors.values()):
        if id(lane) in _held_lanes:
            continue
        workers = lane._max_workers
        gate = threading.Barrier(workers)

        def _hold(gate=gate) -> None:
            with contextlib.suppress(threading.BrokenBarrierError):
                gate.wait(_LANE_WAIT_S)

        started = time.monotonic()
        try:
            jobs = [lane.submit(_hold) for _ in range(workers)]
        except RuntimeError:
            continue    # stopped by the test before
        wait_all(jobs, timeout=_LANE_WAIT_S + 1.0)
        if gate.broken:
            _held_lanes.add(id(lane))
        if time.monotonic() - started > 0.05:
            _schema_stats["lane_waits"] += 1


def _install_harness(conn) -> None:
    """The DDL count's sequence, function and triggers, inside a savepoint: a
    role that may not create an event trigger leaves nothing behind, the mark
    stays None and every test applies the schema."""
    import psycopg

    try:
        with conn.transaction():
            for sql in _HARNESS_SQL:
                conn.execute(sql)
    except psycopg.Error:
        pass


def _apply_schema(conn) -> None:
    """Apply the schema to an emptied database and store its mark. Emptied
    first: a step that reads the rows (a unique index created only without
    duplicates, a CHECK rewritten only when every row passes) must not keep
    what a previous test's rows made of it."""
    global _applied_mark, _applied_tables
    from storage import schema as pg_schema

    _applied_mark = None
    _truncate_all_tables(conn)
    conn.commit()
    pg_schema.init_schema(conn)
    pg_schema.run_migrations(conn)
    mark, _writers = _ddl_mark(conn)
    if mark is None:
        _install_harness(conn)
    conn.commit()
    _applied_tables = _public_tables(conn)
    # Read after the commit: the apply's own statements moved the count.
    _applied_mark, _writers = _ddl_mark(conn)
    conn.rollback()
    _schema_stats["applies"] += 1
    if len(_schema_stats["after"]) < 200:
        _schema_stats["after"].append(_previous_test)


@pytest.fixture(autouse=True)
def _no_remembered_resume_targets():
    """The remote resume keeps the targets its probes resolved for two
    minutes; tests reuse session ids with other rows behind them."""
    from core.remote import remote_resume
    remote_resume._resume_targets.clear()
    yield
    remote_resume._resume_targets.clear()


@pytest.fixture(autouse=True, scope="session")
def _no_exit_failsafe():
    """A test whose app shutdown ran (a lifespan client) arms the proxy's
    hard-exit failsafe: a daemon timer that os._exit(3)s the process three
    seconds later, here an xdist worker in the middle of a later test. The
    failsafe's own tests arm it by hand."""
    import startup
    startup._ARM_EXIT_FAILSAFE = False
    yield


@pytest.fixture(autouse=True)
def _compose_runs_allowed():
    """A test whose app shutdown ran (a lifespan client) stops every later
    compose run of the process: each test starts with them allowed."""
    from services.mcp import docker_manager
    docker_manager._stopping.clear()
    yield
    docker_manager._stopping.clear()


@pytest.fixture(autouse=True)
def _platform_running():
    """A test whose app shutdown ran marks the platform ending for the
    process (pumps then keep a cut turn's open-turn record): each test
    starts with it running."""
    import sys
    pump_module = sys.modules.get("core.events.stream_pump")
    if pump_module is not None:
        pump_module._platform_ending = False
    yield
    pump_module = sys.modules.get("core.events.stream_pump")
    if pump_module is not None:
        pump_module._platform_ending = False


@pytest.fixture(autouse=True)
def _catalog_loop_unbound():
    """A test whose app startup ran (a lifespan client) binds the app
    catalog to that client's loop, closed when the client exits: each
    test starts with none, so an emit runs inline."""
    import sys
    catalog = sys.modules.get("services.apps.catalog")
    if catalog is not None:
        catalog._loop = None
    yield
    catalog = sys.modules.get("services.apps.catalog")
    if catalog is not None:
        catalog._loop = None


@pytest.fixture(autouse=True)
def _no_renderer_of_the_box(monkeypatch):
    """A folder app's deploy has file-tools render its page when one answers,
    found through the manifests (the checkout's own, once a test scanned
    them) and remembered for a while. On a box that runs an install, that is
    the install's sidecar: a deploy test would call it, and fail on what it
    said. Each test starts with no renderer known and none to find; a test
    of the rendered check brings its own."""
    from services.apps import app_render
    monkeypatch.setattr(app_render, "renderer_base_url", lambda: "")
    app_render._probe.update({"at": 0.0, "ok": False, "reason": "", "base": ""})
    yield


@pytest.fixture(autouse=True)
def temp_db():
    """Provide a clean database for each test.

    - The schema: applied when this process has not applied it to this
      database yet, or a statement changed its structure since
    - Before each test: every table emptied
    - Seeds minimal user data that most tests need
    """
    global _previous_test
    from storage import pg as pg_pool

    _drain_store_lanes()
    with pg_pool.get_conn() as conn:
        _assert_disposable(conn)
        mark = _settled_mark(conn)
        if mark is None or mark != _applied_mark:
            _apply_schema(conn)
        _truncate_all_tables(conn, _applied_tables)
        _seed_users(conn)
        conn.commit()
    _schema_stats["tests"] += 1
    _previous_test = os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]

    # Invalidate in-memory caches so post-TRUNCATE state is observed. The
    # agent_store cache is the load-bearing one — its API normally
    # invalidates itself on writes, but TRUNCATE goes around the API.
    # Under the cache's own lock: a load that began before the wipe
    # stores what it read when it ends, and must end before the reset.
    try:
        from storage.agents import agent_store as _agent_store
        with _agent_store._cache_lock:
            _agent_store._invalidate_cache()
    except Exception:  # noqa: BLE001  # the fixture's resets: one module's must not skip the others
        pass

    # The pool-cap readings are cached per pool for 30 s; a cap row or a
    # sample written by the previous test must not be served to the next.
    try:
        from services.billing import pool_caps as _pool_caps
        _pool_caps.clear_cache()
    except Exception:  # noqa: BLE001  # the fixture's resets: one module's must not skip the others
        pass

    # A person's engine use is kept for a minute: the chats a previous test
    # opened must not order the next test's engines.
    try:
        from services.engines import engine_resolution as _engine_resolution
        _engine_resolution.clear_use_cache()
    except Exception:  # noqa: BLE001  # the fixture's resets: one module's must not skip the others
        pass

    # The transfer registry is module-global and nothing in tests runs its
    # sweep, so a finished transfer from an earlier test in this process
    # would replay as a ``transfer_state`` frame into the next dashboard
    # connect (ws_dashboard golden-master tests see an unexpected frame).
    try:
        from core.remote import transfer_registry as _transfer_registry
        _transfer_registry._items.clear()
    except Exception:  # noqa: BLE001  # the fixture's resets: one module's must not skip the others
        pass

    # The per-chat writer lanes are module-global and hold flusher tasks
    # bound to whichever loop ran the previous test (the WS suites use a
    # fresh asyncio.run per scenario) — a leftover lane would never flush.
    try:
        from core.events import chat_writer as _chat_writer
        _chat_writer.reset_for_tests()
    except Exception:  # noqa: BLE001  # the fixture's resets: one module's must not skip the others
        pass

    # The audience cache and the webhook dispatcher's counters are module
    # global: an entry one test filled must not answer the next test's read.
    try:
        from services.notifications import notification_manager as _nm
        from services.webhooks import webhook_dispatcher as _wd
        _nm.reset_audience_cache()
        # A file_updated window left open by a test (its timer on a loop that
        # ended) must not join the next test's burst; the install id read
        # once per process must not outlive the test that stubbed it.
        _nm._file_batches.clear()
        _nm._install_id_value = ""
        from services.notifications import push_sender as _ps
        _ps.reset_relay_verdict()
        from api.agents import chats as _chats_api
        _chats_api._active_memo.reset()
        # The binding writer runs INLINE under the suite: the pool tests read
        # the persisted mirror right after a bind or a release. The queue
        # itself is exercised by tests/billing/test_binding_writer.py, which
        # reopens it for its own tests.
        from services.engines import subscription_pool as _pool
        _pool._binding_writer = None
        _pool._binding_writes_closed = True
        _wd.reset_caches()
    except Exception:  # noqa: BLE001  # the fixture's resets: one module's must not skip the others
        pass

    # Also wipe any agent directories left behind on the filesystem by
    # tests that exercise the real install path. Test AGENTS_DIR was
    # redirected to a tempdir at module import; we just clear its contents.
    import shutil
    try:
        import config as _cfg
        if _cfg.AGENTS_DIR.exists():
            for child in _cfg.AGENTS_DIR.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
    except OSError:
        pass

    from storage import database as db

    # The session liveness tables are per process: a test's marks, floors
    # and headless entries must not make the next test's made-up session id
    # live. The ids ``live_session_token`` minted for stay live (a token
    # built at module import is used by every test of its file).
    try:
        from core.session import session_state as _liveness
        _liveness.reset_liveness_for_tests()
        # A prompt's wait a test's loop ended with is no live wait of the next.
        _liveness._prompt_watch.clear()
        for _sid in _LIVE_TEST_SIDS:
            _liveness.mark_starting(_sid, _LIVE_TEST_TTL_S)
    except Exception:  # noqa: BLE001  # the fixture's resets: one module's must not skip the others
        pass

    yield db

    # No teardown needed — next test truncates


# The session ids ``live_session_token`` marked live, re-marked by ``temp_db``
# before every test.
_LIVE_TEST_SIDS: set[str] = set()
_LIVE_TEST_TTL_S = 3600.0


def live_session_token(session_id: str, agent_name: str, user_sub: str = "", **kw) -> str:
    """A session token whose session counts as LIVE for the rest of the
    process (marked starting; ``temp_db`` re-marks it before every test), so
    a test that exercises a route through the session refusal gets past it
    with a made-up session id. A test whose point is the refusal mints with
    ``create_session_token`` directly."""
    from auth.session_token import create_session_token
    from core.session import session_state
    _LIVE_TEST_SIDS.add(session_id)
    session_state.mark_starting(session_id, _LIVE_TEST_TTL_S)
    return create_session_token(session_id, agent_name, user_sub, **kw)


class _LoopDbGuard:
    """Handle returned by the ``loop_db_guard`` fixture. ``active()`` arms the
    guard for the CURRENT thread (the pytest-asyncio loop thread) for the
    duration of the block: any ``storage.pg.get_conn()`` issued on that thread
    raises ``LoopGuardViolation``. Wrap ONLY the exercised call — store reads
    the test body needs for its own assertions go through ``run_db`` /
    ``asyncio.to_thread``."""

    def active(self):
        from storage import pg
        return pg.loop_guard()

    @contextlib.contextmanager
    def recording(self):
        """The guard for the CURRENT thread in record mode: every
        ``get_conn()`` issued on it is noted, never refused, as the
        ``(proxy-relative file, function)`` of its first caller outside
        ``storage/`` (the count mode's frame). Yields the set; the inventory
        tests (``tests/core/test_loop_store_inventory.py``) compare it with
        their allowlist."""
        from storage import pg
        sites: set[tuple[str, str]] = set()
        ident = threading.get_ident()
        prior_check, prior_ident = pg._check_loop_guard, pg._guard_thread_ident

        def _note() -> None:
            if threading.get_ident() == ident:
                where, _line, func = pg._caller_outside_storage()
                sites.add((os.path.relpath(where, _PROXY_DIR), func))
            else:
                prior_check()

        pg._check_loop_guard = _note
        pg._guard_thread_ident = ident
        try:
            yield sites
        finally:
            pg._check_loop_guard = prior_check
            pg._guard_thread_ident = prior_ident


@pytest.fixture
def loop_db_guard():
    """Opt-in, function-scoped, never autouse: the regression fence for the
    paths converted to off-loop DB access (the testing page, "Event-loop DB
    guard"). Disarmed on teardown no matter how the test exits."""
    from storage import pg
    yield _LoopDbGuard()
    pg.disarm_loop_guard()


def _seed_users(conn=None):
    """Insert test users into the DB (on ``conn``, the caller commits; on a
    connection of its own without)."""
    from datetime import datetime, timezone

    from storage.pg import get_conn

    now = datetime.now(timezone.utc).isoformat()
    users = [
        ("user-admin", "admin@test.com", "Admin User", "admin", now, now),
        ("user-manager", "manager@test.com", "Manager User", "creator", now, now),
        ("user-viewer", "viewer@test.com", "Viewer User", "member", now, now),
        ("user-viewer2", "viewer2@test.com", "Viewer Two", "member", now, now),
    ]
    insert = ("INSERT INTO users (sub, email, name, role, created_at, last_login) "
              "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING")
    if conn is not None:
        conn.cursor().executemany(insert, users)
        return
    with get_conn() as own:
        own.cursor().executemany(insert, users)
        own.commit()
