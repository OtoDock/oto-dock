"""The connection pools behind ``get_conn()`` (storage/pg.py).

Three pools, picked by the calling thread: the event-loop thread's private
loop pool (a short acquire timeout, a circuit breaker and a liveness
deadline), the ``run_db`` lane pool, and the shared pool for everything else.
The outage tests put a local TCP proxy between the pool and the test
database: ``cut`` resets every connection and refuses new ones (a Postgres
restart), ``freeze`` accepts bytes and never answers (``docker pause``, a VM
freeze).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

import config
from storage import pg


# ---------------------------------------------------------------------------
# A TCP proxy in front of the test database
# ---------------------------------------------------------------------------

class _Proxy:
    def __init__(self, host: str, port: int):
        self._up = (host, port)
        self._ls = socket.socket()
        self._ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ls.bind(("127.0.0.1", 0))
        self._ls.listen(64)
        self.port = self._ls.getsockname()[1]
        self.mode = "pass"
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._closed:
            try:
                c, _ = self._ls.accept()
            except OSError:
                return
            if self.mode == "cut":
                _reset(c)
                continue
            try:
                u = socket.create_connection(self._up)
            except OSError:
                _reset(c)
                continue
            with self._lock:
                self._socks += [c, u]
            threading.Thread(target=self._pump, args=(c, u), daemon=True).start()
            threading.Thread(target=self._pump, args=(u, c), daemon=True).start()

    def _pump(self, src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                while self.mode == "freeze":
                    time.sleep(0.01)
                if self.mode == "cut":
                    break
                dst.sendall(data)
        except OSError:
            pass
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)

    def cut(self):
        self.mode = "cut"
        with self._lock:
            socks, self._socks = self._socks, []
        for s in socks:
            _reset(s)

    def close(self):
        self._closed = True
        self.cut()
        with contextlib.suppress(OSError):
            self._ls.close()


def _reset(s: socket.socket) -> None:
    with contextlib.suppress(OSError):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    with contextlib.suppress(OSError):
        s.close()


@pytest.fixture
def proxy_url():
    u = urlparse(config.DATABASE_URL)
    px = _Proxy(u.hostname or "127.0.0.1", u.port or 5432)
    url = urlunparse(u._replace(netloc=f"{u.username}:{u.password}@127.0.0.1:{px.port}"))
    try:
        yield px, url
    finally:
        px.mode = "pass"
        px.close()


@pytest.fixture
def loop_pool():
    """A small LoopPool armed for the current (test loop) thread; always
    disarmed again, because temp_db runs its schema on this same thread."""
    made: list[pg.LoopPool] = []

    def make(url: str, **kw) -> pg.LoopPool:
        opts = dict(size=2, acquire_timeout=0.3, breaker_s=0.5,
                    probe_after_s=0.3, probe_deadline_s=0.5, idle_check_s=30.0)
        opts.update(kw)
        lp = pg.LoopPool(url, thread_ident=threading.get_ident(), **opts)
        lp.open(wait_s=5)
        made.append(lp)
        pg._set_loop_pool(lp)
        return lp

    try:
        yield make
    finally:
        pg._set_loop_pool(None)
        for lp in made:
            lp.close()


@pytest.fixture
def fresh_pools():
    """Tests that kill backends leave dead connections in every pool:
    start the next test from new pools (they re-create lazily)."""
    yield
    pg.close_pool()


class _Ticker:
    """Records the loop's own lateness (how long it could not run)."""

    def __init__(self, period: float = 0.01):
        self.period = period
        self.late: list[float] = []
        self._stop = False

    async def _run(self):
        while not self._stop:
            t = time.perf_counter()
            await asyncio.sleep(self.period)
            self.late.append(time.perf_counter() - t - self.period)

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> float:
        self._stop = True
        await self._task
        return max(self.late, default=0.0)


def _read(conn_fn=pg.get_conn):
    with conn_fn() as conn:
        return conn.execute("SELECT 1 AS one").fetchone()["one"]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_conn_picks_the_pool_from_the_calling_thread(loop_pool):
    lp = loop_pool(config.DATABASE_URL)

    def pool_name():
        with pg.get_conn() as conn:
            return conn._pool.name

    assert pool_name() == lp.name
    assert await asyncio.to_thread(pool_name) == pg.get_pool().name
    assert await pg.run_db(pool_name) == pg.lane_pool().name
    assert await pg.run_db_fast(pool_name) == pg.lane_pool().name
    assert len({lp.name, pg.get_pool().name, pg.lane_pool().name}) == 3


@pytest.mark.asyncio
async def test_nested_acquisition_on_the_loop(loop_pool):
    loop_pool(config.DATABASE_URL)
    with pg.get_conn() as outer:
        with pg.get_conn() as inner:
            assert inner.execute("SELECT 2 AS v").fetchone()["v"] == 2
        assert outer.execute("SELECT 1 AS v").fetchone()["v"] == 1


# ---------------------------------------------------------------------------
# The breaker: a restart (cut) and a freeze
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_costs_the_loop_one_short_wait(loop_pool, proxy_url):
    px, url = proxy_url
    lp = loop_pool(url)
    assert _read() == 1
    tk = _Ticker()
    tk.start()
    px.cut()
    refused = 0
    t_down = time.monotonic()
    while time.monotonic() - t_down < 1.5:
        try:
            _read()
        except (psycopg.OperationalError, pg.DatabaseUnavailable):
            refused += 1
        await asyncio.sleep(0.05)
    px.mode = "pass"
    t_up = time.monotonic()
    ok_after = None
    while time.monotonic() - t_up < 10:
        try:
            _read()
            ok_after = time.monotonic() - t_up
            break
        except (psycopg.OperationalError, pg.DatabaseUnavailable):
            await asyncio.sleep(0.05)
    stall = await tk.stop()
    assert refused > 0
    assert stall < 0.6, f"loop stalled {stall:.2f}s during a restart"
    assert ok_after is not None and ok_after < 5.0
    assert lp.stats()["state"] == "closed"


@pytest.mark.asyncio
async def test_freeze_aborts_after_the_liveness_probe(loop_pool, proxy_url):
    px, url = proxy_url
    lp = loop_pool(url)
    assert _read() == 1
    px.mode = "freeze"
    t = time.monotonic()
    with pytest.raises(pg.DatabaseUnresponsive):
        _read()
    took = time.monotonic() - t
    # probe_after 0.3 s + the sentinel's 0.5 s deadline, plus slack.
    assert took < 2.0, f"freeze abort took {took:.2f}s"
    assert lp.stats()["state"] == "open"
    t = time.monotonic()
    with pytest.raises(pg.DatabaseUnavailable):
        _read()
    assert time.monotonic() - t < 0.05  # open breaker: no wait at all
    px.mode = "pass"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            assert _read() == 1
            break
        except (psycopg.OperationalError, pg.DatabaseUnavailable):
            await asyncio.sleep(0.1)
    else:
        pytest.fail("the loop pool never recovered after the thaw")
    assert lp.stats()["state"] == "closed"


@pytest.mark.asyncio
async def test_freeze_with_the_default_timings(loop_pool, proxy_url):
    """With the shipped delays the probe itself takes its whole 2 s deadline
    on a frozen server: its verdict must still count (it once went stale
    before it arrived, and the loop waited out the whole freeze)."""
    px, url = proxy_url
    loop_pool(url, acquire_timeout=0.5, breaker_s=5.0, probe_after_s=1.0,
              probe_deadline_s=2.0)
    assert _read() == 1
    px.mode = "freeze"
    t = time.monotonic()
    with pytest.raises(pg.DatabaseUnresponsive):
        _read()
    assert time.monotonic() - t < 3.8


@pytest.mark.asyncio
async def test_a_slow_but_alive_statement_is_not_aborted(loop_pool):
    lp = loop_pool(config.DATABASE_URL)
    with pg.get_conn() as conn:
        conn.execute("SELECT pg_sleep(1.0)")
    assert lp.stats()["trips"] == 0


def test_a_commit_is_never_aborted():
    """The watched wait leaves the commit generators alone: a COMMIT cut off
    by a freeze has an unknown outcome, and the 503 would invite a retry."""

    def _commit_gen():
        yield 1

    def _exit_gen():
        yield 1

    def _execute_gen():
        yield 1

    assert pg._must_not_abort(_commit_gen())
    assert pg._must_not_abort(_exit_gen())
    assert not pg._must_not_abort(_execute_gen())


def test_the_sentinel_counts_any_server_reply_as_alive():
    u = urlparse(config.DATABASE_URL)
    bad_pw = urlunparse(u._replace(netloc=f"{u.username}:wrong-password@{u.hostname}:{u.port}"))
    with pytest.raises(psycopg.OperationalError) as ei:
        psycopg.connect(bad_pw, connect_timeout=3).close()
    assert pg._server_replied(ei.value) is True

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    refused = urlunparse(u._replace(netloc=f"{u.username}:{u.password}@127.0.0.1:{port}"))
    with pytest.raises(psycopg.OperationalError) as ei:
        psycopg.connect(refused, connect_timeout=3).close()
    assert pg._server_replied(ei.value) is False
    assert pg._server_replied(psycopg.errors.ConnectionTimeout("x")) is False


@pytest.mark.asyncio
async def test_a_dead_idle_loop_connection_is_replaced_quickly(loop_pool, fresh_pools):
    """After a restart the loop pool's idle connections are dead; the idle
    check discards them before the caller's work runs, so the call itself
    succeeds and nothing trips."""
    lp = loop_pool(config.DATABASE_URL, idle_check_s=0.0)
    assert _read() == 1
    assert _terminate_backends(config.DATABASE_URL)
    t = time.monotonic()
    assert _read() == 1
    assert time.monotonic() - t < 1.0
    assert lp.stats()["trips"] == 0


def _terminate_backends(url: str) -> list[dict]:
    """Kill every other backend of the test database (the pools' idle
    connections), from a connection of our own."""
    with psycopg.connect(url, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        return c.execute(
            "SELECT pid, pg_terminate_backend(pid) AS ok FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchall()


# ---------------------------------------------------------------------------
# Thread pools: drain after a broken connection, lanes, settings
# ---------------------------------------------------------------------------

def test_a_broken_shared_connection_drains_the_idle_ones(fresh_pools):
    for _ in range(3):
        assert _read() == 1
    _terminate_backends(config.DATABASE_URL)
    with contextlib.suppress(psycopg.OperationalError):
        _read()  # at most one caller sees the dead connection
    t = time.monotonic()
    while True:
        try:
            assert _read() == 1
            break
        except psycopg.OperationalError:
            assert time.monotonic() - t < 2.0, "the stale connections were not drained"
            time.sleep(0.05)


@pytest.mark.asyncio
async def test_the_fast_lane_is_not_queued_behind_the_bulk_lane(monkeypatch):
    monkeypatch.setattr(config, "DB_BULK_LANE_WORKERS", 2)
    monkeypatch.setattr(config, "DB_FAST_LANE_WORKERS", 1)
    pg.shutdown_db_executor()
    release = threading.Event()

    def hold():
        with pg.get_conn() as conn:
            conn.execute("SELECT 1")
            release.wait(10)

    try:
        held = [asyncio.ensure_future(pg.run_db(hold)) for _ in range(4)]
        await asyncio.sleep(0.2)
        t = time.monotonic()
        assert await pg.run_db_fast(_read) == 1
        assert time.monotonic() - t < 1.0
    finally:
        release.set()
        await asyncio.gather(*held)
        pg.shutdown_db_executor()


def test_server_side_settings_on_every_pool(monkeypatch):
    monkeypatch.setattr(config, "DB_STATEMENT_TIMEOUT_S", 300.0)
    monkeypatch.setattr(config, "DB_IDLE_IN_TX_TIMEOUT_S", 60.0)
    url = config.DATABASE_URL + "?options=-c%20statement_timeout%3D7000"
    thread = pg.connection_kwargs(url)
    loop = pg.connection_kwargs(url, loop=True)
    # The platform's flags first, the operator's after: the last -c wins.
    assert thread["options"].index("statement_timeout=300000") < thread["options"].index("statement_timeout=7000")
    assert "lock_timeout" not in thread["options"]
    assert "-c lock_timeout=2000" in loop["options"]
    assert thread["connect_timeout"] == 5
    assert thread["keepalives_idle"] == 30 and loop["keepalives_idle"] == 2

    kw = pg.connection_kwargs(config.DATABASE_URL, loop=True)
    kw.pop("row_factory", None)
    with psycopg.connect(config.DATABASE_URL, **kw) as c:
        show = {k: c.execute(f"SHOW {k}").fetchone()[0] for k in (
            "statement_timeout", "idle_in_transaction_session_timeout", "lock_timeout")}
    assert show == {"statement_timeout": "5min",
                    "idle_in_transaction_session_timeout": "1min",
                    "lock_timeout": "2s"}

    monkeypatch.setattr(config, "DB_STATEMENT_TIMEOUT_S", 0.0)
    monkeypatch.setattr(config, "DB_IDLE_IN_TX_TIMEOUT_S", 0.0)
    assert "options" not in pg.connection_kwargs(config.DATABASE_URL, loop=True)


def test_close_pool_disarms_and_closes_everything(loop_pool):
    lp = loop_pool(config.DATABASE_URL)
    assert pg.loop_pool() is lp
    pg.close_pool()
    assert pg.loop_pool() is None
    assert lp.stats()["state"] == "closed-for-good"
    assert _read() == 1  # the shared pool re-creates lazily, as before


@pytest.mark.asyncio
async def test_arm_loop_pool_routes_the_calling_thread():
    lp = pg.arm_loop_pool(open_wait_s=5)
    try:
        assert pg.loop_pool() is lp and lp.thread_ident == threading.get_ident()
        with pg.get_conn() as conn:
            assert conn._pool.name == "loop"
    finally:
        pg.disarm_loop_pool()
    assert pg.loop_pool() is None
    with pg.get_conn() as conn:
        assert conn._pool.name == "shared"
