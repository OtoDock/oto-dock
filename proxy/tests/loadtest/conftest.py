"""The load harness's gate, calibration, database durability, box record and
report.

Every test under this package is marked ``loadtest`` and skipped unless
``OTODOCK_LOADTEST=1``; the skip is added at collection, so the normal gate
never builds a fixture here. With the variable set, a run under an xdist
worker, or one that collected anything outside this package, stops with an
error instead: two measuring processes on one box measure each other, and a
serial full-suite run leaves module state behind (the manifests, the rate
limiter, the pump registry) that the checks would then measure. A run asked
for that measures nothing must not read as green.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

_HERE = Path(__file__).resolve().parent


def _enabled() -> bool:
    return os.environ.get("OTODOCK_LOADTEST") == "1"


def pytest_configure(config):
    # Loaded here only when the package is named on the command line; a
    # worker of a whole-suite xdist run stops at collection instead.
    if _enabled() and getattr(config.option, "numprocesses", None):
        pytest.exit("OTODOCK_LOADTEST=1: run the load harness serially (-n 0), alone on the box", returncode=4)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # trylast: after -m / -k deselection, so "-m loadtest" counts as alone.
    parents: dict[Path, bool] = {}

    def is_mine(item) -> bool:
        parent = item.path.parent
        if parent not in parents:
            resolved = parent.resolve()
            parents[parent] = resolved == _HERE or _HERE in resolved.parents
        return parents[parent]

    mine = [item for item in items if is_mine(item)]
    if not mine:
        return
    for item in mine:
        item.add_marker(pytest.mark.loadtest)
    if not _enabled():
        skip = pytest.mark.skip(reason="the load harness runs on demand: OTODOCK_LOADTEST=1, -n 0, tests/loadtest alone")
        for item in mine:
            item.add_marker(skip)
    elif os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.exit("OTODOCK_LOADTEST=1: run the load harness serially (-n 0), alone on the box", returncode=4)
    elif len(mine) != len(items):
        pytest.exit("OTODOCK_LOADTEST=1: collect tests/loadtest alone (other tests leave state it would measure)",
                    returncode=4)


@pytest.fixture(scope="session", autouse=True)
def _durable_commits():
    """Commits as production pays them: the suite turns ``synchronous_commit``
    off on its throwaway databases; this run's database gets it back, and the
    pools reconnect under it."""
    import psycopg

    import config
    from storage import pg

    name = urlparse(config.DATABASE_URL).path.lstrip("/")
    assert "test" in name, name
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'ALTER DATABASE "{name}" SET synchronous_commit TO on')
    pg.shutdown_db_executor()
    pg.close_pool()
    yield


@pytest.fixture(scope="session", autouse=True)
def _run_record(_durable_commits):
    from tests.loadtest import _harness as h

    h.record("run", **h.run_info(), box=h.box_snapshot())
    yield
    h.record("run-end", box=h.box_snapshot())


@pytest.fixture(scope="session", autouse=True)
def _calibrated(_run_record):
    """Every check depends on the instruments seeing a stall they are shown."""
    from tests.loadtest import _harness as h

    result = h.calibrate()
    h.record("calibration", **result)
    if result["problems"]:
        pytest.fail("the instruments failed calibration: " + "; ".join(result["problems"]),
                    pytrace=False)


@pytest.fixture(autouse=True)
def _restore_process_state():
    """The sync backstop behind every scenario's own ``finally``: helpers
    killed, the loop pool disarmed, the process knobs a check changed put
    back. A loop pool left armed would pin the main thread that runs the next
    test's ``temp_db``."""
    import resource

    import config
    from storage import pg
    from tests.loadtest import _harness as h

    saved = {k: getattr(config, k) for k in ("HOST", "PORT", "INTERNAL_LISTENER_PORT", "DATABASE_URL")}
    nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
    yield
    h.kill_helpers()
    pg.disarm_loop_pool()
    moved = config.DATABASE_URL != saved["DATABASE_URL"]
    for k, v in saved.items():
        setattr(config, k, v)
    if moved:
        pg.shutdown_db_executor()
        pg.close_pool(timeout=1.0)
    if resource.getrlimit(resource.RLIMIT_NOFILE) != nofile:
        resource.setrlimit(resource.RLIMIT_NOFILE, nofile)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _enabled():
        return
    from tests.loadtest import _harness as h

    if not h.RECORDS:
        return
    terminalreporter.section("load harness")
    for rec in h.RECORDS:
        terminalreporter.write_line(h.summary_line(rec))
