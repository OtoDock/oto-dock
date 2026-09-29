"""PostgreSQL connection pools: shared singletons for all storage modules.

Uses psycopg3 sync pools. All storage functions remain synchronous (called
via ``run_db`` / ``asyncio.to_thread`` from async code). ``get_conn()`` picks
the pool from the calling thread:

* the event-loop thread, once armed at the end of boot → the **loop pool**
  (``LoopPool``): three connections, a short acquire timeout, a circuit
  breaker and a liveness deadline;
* a ``run_db`` worker → the **lane pool**, sized to the ``run_db`` lanes plus
  spares, so ``asyncio.to_thread`` traffic can never delay ``run_db``;
* any other thread → the **shared pool**.

Event-loop rule (2026-09-04, from the 09-03 stall incident): a store call
must never run ON the event loop thread from a PERIODIC or RECONNECT-STORM
path — a slow disk turns every ``COMMIT`` (WAL fsync) into a full proxy
freeze (no pings served → every satellite / dashboard / phone socket drops at
once → reconnects add more writes). Off-loop DB work goes through
``run_db``. The loop pool bounds what the remaining on-loop calls cost when
Postgres restarts or freezes: one short wait per outage, then an instant
``DatabaseUnavailable`` until Postgres answers again. ``loop_guard`` is the
test-time fence (see ``tests/conftest.py``).
"""

import asyncio
import contextlib
import functools
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import psycopg_pool
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

logger = logging.getLogger("claude-proxy.db")

_pool: psycopg_pool.ConnectionPool | None = None
_lane_pool: psycopg_pool.ConnectionPool | None = None
_pool_lock = threading.Lock()


class DatabaseUnavailable(psycopg_pool.PoolTimeout):
    """The loop pool refused a connection: its breaker is open, or none came
    free in time. A ``PoolTimeout``, so every existing handler catches it."""


class DatabaseUnresponsive(psycopg.OperationalError):
    """An operation was aborted because Postgres stopped answering (the
    connection is closed; the server cancels the statement once it resumes)."""


# ---------------------------------------------------------------------------
# Connection parameters
# ---------------------------------------------------------------------------

# Only loop-pool connections get a lock timeout: an on-loop statement queued
# behind a row lock would otherwise freeze the loop up to statement_timeout.
_LOOP_LOCK_TIMEOUT_MS = 2000
# PG 14+: the server polls the client socket while a statement runs, so a
# statement whose client went away (an aborted loop query) is cancelled.
_CLIENT_CHECK_INTERVAL_MS = 2000
_CONNECT_TIMEOUT_S = 5
# Keepalives bound a half-open connection (failover, NAT drop) that a
# liveness probe on a fresh connection cannot see.
_THREAD_TCP = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
               "keepalives_count": 3, "tcp_user_timeout": 60000}
_LOOP_TCP = {"keepalives": 1, "keepalives_idle": 2, "keepalives_interval": 1,
             "keepalives_count": 3, "tcp_user_timeout": 5000}


def _server_settings_enabled() -> bool:
    import config
    return config.DB_STATEMENT_TIMEOUT_S > 0 or config.DB_IDLE_IN_TX_TIMEOUT_S > 0


def connection_kwargs(conninfo: str, *, loop: bool = False) -> dict:
    """libpq parameters for a pool connection. The server-side timeouts ride
    the startup packet's ``options`` (no round trip, and ``RESET`` keeps
    them), the platform's ``-c`` flags first and the URL's own ``options`` after, so an
    operator's value wins. Both timeouts at 0 send no ``options`` at all
    (PgBouncer rejects the parameter). Anything the URL already sets wins."""
    import config
    params = conninfo_to_dict(conninfo)
    kwargs: dict = {"row_factory": dict_row, "autocommit": False}
    if _server_settings_enabled():
        flags = []
        if config.DB_STATEMENT_TIMEOUT_S > 0:
            flags.append(f"-c statement_timeout={int(config.DB_STATEMENT_TIMEOUT_S * 1000)}")
        if config.DB_IDLE_IN_TX_TIMEOUT_S > 0:
            flags.append("-c idle_in_transaction_session_timeout="
                         f"{int(config.DB_IDLE_IN_TX_TIMEOUT_S * 1000)}")
        if loop:
            flags.append(f"-c lock_timeout={_LOOP_LOCK_TIMEOUT_MS}")
        if params.get("options"):
            flags.append(params["options"])
        kwargs["options"] = " ".join(flags)
    if "connect_timeout" not in params:
        kwargs["connect_timeout"] = _CONNECT_TIMEOUT_S
    for key, value in (_LOOP_TCP if loop else _THREAD_TCP).items():
        if key not in params:
            kwargs[key] = value
    return kwargs


def _configure(conn: psycopg.Connection) -> None:
    """Per new connection: the one setting older servers would refuse in the
    startup packet. Leaves the connection idle (the pool requires it)."""
    if _server_settings_enabled() and conn.info.server_version >= 140000:
        conn.execute(f"SET client_connection_check_interval = {_CLIENT_CHECK_INTERVAL_MS}")
        conn.commit()


# ---------------------------------------------------------------------------
# The shared pool and the run_db lane pool
# ---------------------------------------------------------------------------

def pool_max_size() -> int:
    import config
    return config.DB_POOL_MAX_SIZE


def _new_thread_pool(name: str, max_size: int) -> psycopg_pool.ConnectionPool:
    import config
    # No checkout ``check``: psycopg_pool sleeps with backoff (1, 2, 4, 8 s)
    # in the caller's thread for every dead connection it finds after a
    # restart. A broken connection drains the pool instead (_tracked).
    pool = psycopg_pool.ConnectionPool(
        conninfo=config.DATABASE_URL,
        min_size=2,
        max_size=max_size,
        timeout=config.DB_POOL_TIMEOUT_S,
        kwargs=connection_kwargs(config.DATABASE_URL),
        configure=_configure,
        name=name,
        open=False,
    )
    pool.open()
    return pool


def get_pool() -> psycopg_pool.ConnectionPool:
    """The shared pool (lazy init, double-checked locking): every thread that
    is neither the armed loop thread nor a ``run_db`` worker."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = _new_thread_pool("shared", pool_max_size())
        return _pool


def lane_pool() -> psycopg_pool.ConnectionPool:
    """The ``run_db`` workers' own pool: one connection per worker plus two
    spares, so a depth-2 nested acquisition can never deadlock the lanes."""
    global _lane_pool
    if _lane_pool is not None:
        return _lane_pool
    with _pool_lock:
        if _lane_pool is None:
            size = db_executor_workers(LANE_FAST) + db_executor_workers(LANE_BULK) + 2
            _lane_pool = _new_thread_pool("lanes", size)
        return _lane_pool


_drain_at: dict[int, float] = {}
_DRAIN_EVERY_S = 10.0


def _schedule_drain(pool: psycopg_pool.ConnectionPool) -> None:
    """A connection of ``pool`` was found broken (a Postgres restart kills
    them all): replace the idle ones in the background, at most every 10 s,
    so each stale connection does not fail a caller of its own."""
    now = time.monotonic()
    with _pool_lock:
        last = _drain_at.get(id(pool))
        if last is not None and now - last < _DRAIN_EVERY_S:
            return
        _drain_at[id(pool)] = now

    def _drain():
        with contextlib.suppress(Exception):
            pool.drain()

    threading.Thread(target=_drain, name="db-drain", daemon=True).start()


@contextlib.contextmanager
def _tracked(pool: psycopg_pool.ConnectionPool, timeout: float | None):
    conn = pool.getconn(timeout=timeout)
    try:
        with conn:
            yield conn
    finally:
        lost = conn.closed or conn.broken
        pool.putconn(conn)
        if lost:
            _schedule_drain(pool)


# ---------------------------------------------------------------------------
# The loop pool
# ---------------------------------------------------------------------------

# Generators whose outcome must never be left unknown: a COMMIT cut off by a
# freeze may have committed, and the 503 the caller gets invites a retry.
_NEVER_ABORT = frozenset({"_commit_gen", "_exit_gen"})


def _must_not_abort(gen) -> bool:
    return getattr(getattr(gen, "gi_code", None), "co_name", "") in _NEVER_ABORT


def _server_replied(exc: BaseException) -> bool:
    """True when the error proves Postgres is up: it answered, even with a
    refusal (too many connections, an auth failure, starting up). A connect
    timeout, a refused or reset connection, or our own deadline = no answer."""
    if isinstance(exc, (psycopg.errors.ConnectionTimeout, DatabaseUnresponsive)):
        return False
    if getattr(exc, "sqlstate", None):
        return True
    text = str(exc)
    return "FATAL:" in text or "ERROR:" in text


def _deadline_gen(gen, deadline_s: float, what: str):
    """Drive a psycopg wait generator, aborting once it has waited
    ``deadline_s``: psycopg's wait loop resumes it with ``READY_NONE`` every
    interval, so this runs without any extra thread."""
    start = time.monotonic()
    try:
        state = next(gen)
        while True:
            ready = yield state
            if not ready and time.monotonic() - start >= deadline_s:
                raise DatabaseUnresponsive(f"{what}: no answer from Postgres in {deadline_s:.1f}s")
            state = gen.send(ready)
    except StopIteration as ex:
        return ex.value


class _SentinelConnection(psycopg.Connection):
    """The liveness probe's own connection: every operation has a hard
    deadline, and a timed-out connection is closed."""

    deadline_s = 2.0

    def wait(self, gen, interval=0.1):
        try:
            return super().wait(_deadline_gen(gen, self.deadline_s, "liveness probe"), interval)
        except DatabaseUnresponsive:
            with contextlib.suppress(Exception):
                self.close()
            raise


class _Liveness:
    """Answers "does Postgres still answer?" through one persistent sentinel
    connection (no connect, auth or connection slot per probe; a server at
    ``max_connections`` still answers it). Probes run on daemon threads."""

    def __init__(self, conninfo: str, kwargs: dict, deadline_s: float):
        self._conninfo = conninfo
        self._kwargs = {**kwargs, "autocommit": True}
        self._kwargs.pop("row_factory", None)
        self._kwargs["connect_timeout"] = max(2, int(deadline_s + 0.999))
        self._conn_class = type("_Sentinel", (_SentinelConnection,), {"deadline_s": deadline_s})
        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()
        self._conn_lock = threading.Lock()
        self._running = False
        self._result: tuple[float, float, bool] | None = None  # (started, finished, alive)

    def verdict(self, since: float, max_age: float) -> bool | None:
        """The answer of a probe that started at or after ``since`` and
        finished within the last ``max_age`` seconds, or None while one runs
        (starting it if none does). Freshness counts from the finish: a probe
        of a frozen server takes its whole deadline."""
        with self._lock:
            result = self._result
            if (result is not None and result[0] >= since
                    and time.monotonic() - result[1] <= max_age):
                return result[2]
            if self._running:
                return None
            self._running = True
        threading.Thread(target=self._probe_async, name="db-liveness", daemon=True).start()
        return None

    def _probe_async(self) -> None:
        started = time.monotonic()
        alive = self.probe()
        with self._lock:
            self._result = (started, time.monotonic(), alive)
            self._running = False

    def probe(self) -> bool:
        with self._conn_lock:
            conn = self._conn
            if conn is not None and not conn.closed:
                try:
                    conn.execute("SELECT 1")
                    return True
                except DatabaseUnresponsive:
                    return False
                except Exception:
                    if not (conn.closed or conn.broken):
                        return True  # the server answered, with an error
                    # The sentinel itself died (a restart): a fresh one decides.
            self._conn = None
            try:
                self._conn = self._conn_class.connect(self._conninfo, **self._kwargs)
                self._conn.execute("SELECT 1")
                return True
            except Exception as e:
                with contextlib.suppress(Exception):
                    if self._conn is not None:
                        self._conn.close()
                self._conn = None
                return _server_replied(e)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._conn is not None:
                self._conn.close()


class _LoopConnection(psycopg.Connection):
    """A loop-pool connection. On the armed loop thread its waits are
    watched: past the owner's probe delay the sentinel is asked, and when
    Postgres does not answer the operation is aborted (never a commit)."""

    _oto_owner: "LoopPool | None" = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Idle since: creation, then each return to the pool.
        self._oto_returned_at = time.monotonic()
        self._oto_hard_deadline: float | None = None

    def wait(self, gen, interval=0.1):
        owner = self._oto_owner
        hard = self._oto_hard_deadline
        if hard is not None:
            wrapped = _deadline_gen(gen, hard, "connection check")
        elif owner is not None and owner.watching() and not _must_not_abort(gen):
            wrapped = owner.watched(gen)
        else:
            return super().wait(gen, interval)
        try:
            return super().wait(wrapped, interval)
        except DatabaseUnresponsive:
            with contextlib.suppress(Exception):
                self.close()
            raise


class LoopPool:
    """The event-loop thread's private pool with a circuit breaker.

    Trips: *slow* (open at least ``breaker_s``) when no connection comes
    free within ``acquire_timeout`` or an operation is aborted because
    Postgres stopped answering; *fast* (no floor) when a connection is found
    closed or broken, which on a healthy server recovers in milliseconds.
    While open, every checkout raises ``DatabaseUnavailable`` at once. One
    recovery thread waits out the floor, asks the sentinel, then publishes a
    freshly opened pool (psycopg_pool's own reconnect backoff reaches tens
    of seconds after a long outage) and closes the breaker."""

    _BACKOFF_S = (0.2, 0.5, 1.0, 2.0)
    _SLOW_ACQUIRE_S = 0.05
    _REPROBE_S = 2.0

    def __init__(self, conninfo: str, *, thread_ident: int, size: int = 3,
                 acquire_timeout: float = 0.5, breaker_s: float = 5.0,
                 probe_after_s: float = 1.0, probe_deadline_s: float = 2.0,
                 idle_check_s: float = 30.0, name: str = "loop"):
        self.name = name
        self.thread_ident = thread_ident
        self.acquire_timeout = acquire_timeout
        self.breaker_s = breaker_s
        self.probe_after_s = probe_after_s
        self.idle_check_s = idle_check_s
        self._size = size
        self._conninfo = conninfo
        self._kwargs = connection_kwargs(conninfo, loop=True)
        self._conn_class = type("_LoopConn", (_LoopConnection,), {"_oto_owner": self})
        self._liveness = _Liveness(conninfo, self._kwargs, probe_deadline_s)
        self._lock = threading.Lock()
        self._pool = self._new_pool()
        self._tripped = False
        self._closing = False
        self._recovering = False
        self._floor_until = 0.0
        self._trip_seq = 0
        self._opened_at = 0.0
        self.trips = 0
        self.last_reason = ""
        self._slow_logged_at: float | None = None

    def _new_pool(self) -> psycopg_pool.ConnectionPool:
        return psycopg_pool.ConnectionPool(
            conninfo=self._conninfo, min_size=self._size, max_size=self._size,
            timeout=self.acquire_timeout, kwargs=self._kwargs,
            connection_class=self._conn_class, configure=_configure,
            name=self.name, open=False,
        )

    def open(self, wait_s: float = 0.0) -> None:
        """Open the pool; with ``wait_s`` also wait for its connections and
        connect the sentinel, so the first probe is one query, not a connect."""
        self._pool.open(wait=wait_s > 0, timeout=wait_s or 30.0)
        if wait_s > 0:
            self._liveness.probe()

    # -- the watched wait -------------------------------------------------

    def watching(self) -> bool:
        return self.probe_after_s > 0 and threading.get_ident() == self.thread_ident

    def watched(self, gen):
        start = time.monotonic()
        try:
            state = next(gen)
            while True:
                ready = yield state
                if not ready and time.monotonic() - start >= self.probe_after_s:
                    # A verdict counts if its probe started after this wait
                    # passed the delay and is recent, so a long statement
                    # keeps being re-checked while it runs.
                    verdict = self._liveness.verdict(
                        since=start + self.probe_after_s, max_age=self._REPROBE_S)
                    if verdict is False:
                        raise DatabaseUnresponsive(
                            f"Postgres stopped answering (waited {time.monotonic() - start:.1f}s)")
                state = gen.send(ready)
        except StopIteration as ex:
            return ex.value

    # -- checkout -----------------------------------------------------------

    @contextlib.contextmanager
    def connection(self):
        if self._tripped:
            raise DatabaseUnavailable(
                f"database unavailable (loop pool breaker open: {self.last_reason})")
        pool = self._pool
        t0 = time.monotonic()
        conn = self._checkout(pool, t0 + self.acquire_timeout)
        waited = time.monotonic() - t0
        if waited > self._SLOW_ACQUIRE_S:
            self._log_slow_acquire(waited)
        reason = ""
        try:
            with conn:
                yield conn
        except DatabaseUnresponsive:
            reason = "Postgres stopped answering"
            raise
        finally:
            lost = conn.closed or conn.broken
            conn._oto_returned_at = time.monotonic()
            with contextlib.suppress(Exception):
                pool.putconn(conn)
            if lost:
                if reason:
                    self._trip(reason, slow=True)
                else:
                    self._trip("connection lost", slow=False)

    def _checkout(self, pool: psycopg_pool.ConnectionPool, deadline: float):
        """A connection within the acquire timeout. One idle for a while may
        have died with a Postgres restart: it gets one bounded empty round
        trip first, and a dead one is discarded (the pool reconnects at once)
        and the next is tried, before the caller's work has run."""
        while True:
            try:
                conn = pool.getconn(timeout=max(0.0, deadline - time.monotonic()))
            except (psycopg_pool.PoolTimeout, psycopg_pool.PoolClosed) as e:
                self._trip(f"no connection within {self.acquire_timeout:.1f}s", slow=True)
                raise DatabaseUnavailable(f"database unavailable ({e})") from e
            if time.monotonic() - conn._oto_returned_at <= self.idle_check_s:
                return conn
            conn._oto_hard_deadline = 0.3
            try:
                psycopg_pool.ConnectionPool.check_connection(conn)
                return conn
            except DatabaseUnresponsive as e:
                with contextlib.suppress(Exception):
                    pool.putconn(conn)
                self._trip("Postgres stopped answering", slow=True)
                raise DatabaseUnavailable("database unavailable (no answer)") from e
            except Exception:
                with contextlib.suppress(Exception):
                    pool.putconn(conn)  # closed or broken: discarded
            finally:
                conn._oto_hard_deadline = None

    def _log_slow_acquire(self, waited: float) -> None:
        now = time.monotonic()
        if self._slow_logged_at is not None and now - self._slow_logged_at < 10.0:
            return
        self._slow_logged_at = now
        f, line, fn = _caller_outside_storage()
        logger.warning("on-loop DB connection waited %.0f ms (%s:%d %s)", waited * 1000, f, line, fn)

    # -- breaker ------------------------------------------------------------

    def _trip(self, reason: str, *, slow: bool) -> None:
        now = time.monotonic()
        with self._lock:
            if self._closing:
                return
            first = not self._tripped
            self._tripped = True
            self._trip_seq += 1
            self.trips += 1
            self.last_reason = reason
            if slow:
                self._floor_until = max(self._floor_until, now + self.breaker_s)
            if first:
                self._opened_at = now
            start = not self._recovering
            self._recovering = True
        if first:
            logger.warning("loop DB pool: breaker open (%s); on-loop DB calls fail at "
                           "once until Postgres answers", reason)
        if start:
            threading.Thread(target=self._recover, name="db-loop-recover", daemon=True).start()

    def _recover(self) -> None:
        attempt = 0
        while True:
            with self._lock:
                if self._closing:
                    self._recovering = False
                    return
                wait = self._floor_until - time.monotonic()
                seq = self._trip_seq
            if wait > 0:
                time.sleep(wait)
                continue
            try:
                if self._liveness.probe():
                    fresh = self._new_pool()
                    try:
                        fresh.open(wait=True, timeout=2.0)
                    except Exception:
                        fresh.close(timeout=1.0)
                        raise
                    with self._lock:
                        if self._closing or seq != self._trip_seq:
                            installed = False
                        else:
                            installed = True
                            old, self._pool = self._pool, fresh
                            self._tripped = False
                            self._recovering = False
                            open_s = time.monotonic() - self._opened_at
                    if installed:
                        logger.info("loop DB pool: breaker closed after %.1fs", open_s)
                        old.close(timeout=1.0)
                        return
                    fresh.close(timeout=1.0)
                    continue  # a new trip arrived meanwhile: judge again
            except Exception:
                logger.debug("loop DB pool recovery attempt failed", exc_info=True)
            time.sleep(self._BACKOFF_S[min(attempt, len(self._BACKOFF_S) - 1)])
            attempt += 1

    # -- lifecycle and stats ------------------------------------------------

    def stats(self) -> dict:
        if self._closing:
            state = "closed-for-good"
        else:
            state = "open" if self._tripped else "closed"
        out = {"state": state, "trips": self.trips, "last_reason": self.last_reason}
        if self._tripped:
            out["open_for_s"] = round(time.monotonic() - self._opened_at, 1)
        with contextlib.suppress(Exception):
            out["pool"] = self._pool.get_stats()
        return out

    def close(self, timeout: float = 3.0) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            pool = self._pool
        with contextlib.suppress(Exception):
            pool.close(timeout=timeout)
        self._liveness.close()


_loop_db: LoopPool | None = None


def _set_loop_pool(lp: LoopPool | None) -> None:
    global _loop_db
    _loop_db = lp


def loop_pool() -> LoopPool | None:
    return _loop_db


def arm_loop_pool(thread_ident: int | None = None, *, open_wait_s: float = 2.0) -> LoopPool:
    """Give the calling (event-loop) thread its private pool. The LAST boot
    step: everything before it (schema init, seeders, recovery) uses the
    shared pool with its normal wait."""
    import config
    lp = LoopPool(
        config.DATABASE_URL,
        thread_ident=thread_ident if thread_ident is not None else threading.get_ident(),
        acquire_timeout=config.DB_LOOP_POOL_TIMEOUT_S,
        breaker_s=config.DB_LOOP_BREAKER_S,
        probe_after_s=config.DB_LOOP_LIVENESS_PROBE_S,
    )
    try:
        lp.open(wait_s=open_wait_s)
    except Exception:
        logger.warning("loop DB pool: not full after %.0fs; it keeps connecting", open_wait_s)
    old = _loop_db
    _set_loop_pool(lp)
    if old is not None:
        old.close()
    return lp


def disarm_loop_pool() -> None:
    lp = _loop_db
    _set_loop_pool(None)
    if lp is not None:
        lp.close()


# ---------------------------------------------------------------------------
# get_conn
# ---------------------------------------------------------------------------

_lane_local = threading.local()


def get_conn(*, timeout: float | None = None):
    """Return a context-managed connection from the pool for this thread.

    Usage:
        with get_conn() as conn:
            conn.execute("SELECT ...", (param,))
            conn.commit()

    On normal exit the connection is returned to the pool.
    On exception the transaction is rolled back automatically.
    ``timeout`` applies to the thread pools (the loop pool has its own).
    """
    if _guard_thread_ident is not None or _GUARD_MODE:
        _check_loop_guard()
    lp = _loop_db
    if lp is not None and threading.get_ident() == lp.thread_ident:
        return lp.connection()
    if getattr(_lane_local, "in_lane", False):
        return _tracked(lane_pool(), timeout)
    return _tracked(get_pool(), timeout)


def pool_stats() -> dict:
    """Every pool's psycopg_pool stats, the lane queues and the breaker."""
    out: dict = {}
    for key, pool in (("shared", _pool), ("lanes", _lane_pool)):
        if pool is not None:
            with contextlib.suppress(Exception):
                out[key] = pool.get_stats()
    out["lane_queues"] = {lane: ex._work_queue.qsize() for lane, ex in list(_executors.items())}
    if _loop_db is not None:
        out["loop"] = _loop_db.stats()
    return out


def close_pool(timeout: float = 3.0) -> None:
    """Close every pool at shutdown (idempotent): the loop pool first
    (disarmed, so a later on-loop call falls back to the shared pool), then
    the lane and shared pools. A later ``get_conn()`` lazily re-creates the
    thread pools, so this must be one of the LAST shutdown steps."""
    global _pool, _lane_pool
    disarm_loop_pool()
    with _pool_lock:
        pools = [p for p in (_lane_pool, _pool) if p is not None]
        _pool = _lane_pool = None
    for pool in pools:
        with contextlib.suppress(Exception):
            pool.close(timeout=timeout)


# ---------------------------------------------------------------------------
# run_db lanes
# ---------------------------------------------------------------------------

LANE_BULK = "bulk"
LANE_FAST = "fast"

_executors: dict[str, ThreadPoolExecutor] = {}
_db_executor_lock = threading.Lock()


def _mark_lane_thread() -> None:
    _lane_local.in_lane = True


def db_executor_workers(lane: str = LANE_BULK) -> int:
    """Worker count of a ``run_db`` lane (the bulk lane is the default)."""
    import config
    n = config.DB_FAST_LANE_WORKERS if lane == LANE_FAST else config.DB_BULK_LANE_WORKERS
    return max(1, int(n))


def db_executor(lane: str = LANE_BULK) -> ThreadPoolExecutor:
    """The thread pool of a ``run_db`` lane (lazy singleton per lane)."""
    ex = _executors.get(lane)
    if ex is not None:
        return ex
    with _db_executor_lock:
        ex = _executors.get(lane)
        if ex is None:
            ex = ThreadPoolExecutor(
                max_workers=db_executor_workers(lane),
                thread_name_prefix="db" if lane == LANE_BULK else f"db-{lane}",
                initializer=_mark_lane_thread,
            )
            _executors[lane] = ex
        return ex


async def run_db(fn, /, *args, **kwargs):
    """Run a synchronous store function on the bulk ``run_db`` lane and await
    its result. The ONLY sanctioned way to call a store from a periodic loop,
    a WebSocket handler or any reconnect-storm path."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor(LANE_BULK), functools.partial(fn, *args, **kwargs),
    )


async def run_db_fast(fn, /, *args, **kwargs):
    """``run_db`` on the fast lane: principal loads and primary-key reads,
    never queued behind turn persistence, listings or search."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor(LANE_FAST), functools.partial(fn, *args, **kwargs),
    )


def shutdown_db_executor(*, wait: bool = False) -> None:
    """Stop every ``run_db`` lane at shutdown (idempotent). Pending futures
    are cancelled; a worker parked on a stalled commit is left to finish so
    the interpreter's exit failsafe (startup.py) decides, not us."""
    with _db_executor_lock:
        executors = list(_executors.values())
        _executors.clear()
    for ex in executors:
        with contextlib.suppress(Exception):
            ex.shutdown(wait=wait, cancel_futures=True)


# ---------------------------------------------------------------------------
# Loop-thread guard (tests + one-off inventory; never armed in production)
# ---------------------------------------------------------------------------
#
# Two mechanisms:
#   * ``arm_loop_guard()`` / ``loop_guard()`` — pin ONE thread ident (the test
#     loop thread); ``get_conn()`` on that thread raises. Exact, no probing,
#     no false positives from the direct-LLM helper loop or APScheduler jobs.
#     Used by the ``loop_db_guard`` pytest fixture around the exercised call.
#   * ``OTODOCK_DB_LOOP_GUARD=count`` — inventory mode: every ``get_conn()``
#     issued while an event loop is running on the calling thread records the
#     first caller frame outside ``storage/``. ``OTODOCK_DB_LOOP_GUARD=raise``
#     raises instead. Both probe ``asyncio.get_running_loop()`` and are for
#     one-off audit runs only.

_GUARD_MODE = os.environ.get("OTODOCK_DB_LOOP_GUARD", "").strip().lower()
_guard_thread_ident: int | None = None
_guard_hits: dict[tuple[str, int, str], int] = {}
_guard_lock = threading.Lock()


class LoopGuardViolation(RuntimeError):
    """A store was called on the event loop thread while the guard was armed."""


def arm_loop_guard(thread_ident: int | None = None) -> None:
    global _guard_thread_ident
    _guard_thread_ident = thread_ident if thread_ident is not None else threading.get_ident()


def disarm_loop_guard() -> None:
    global _guard_thread_ident
    _guard_thread_ident = None


@contextlib.contextmanager
def loop_guard():
    """Arm the guard for the CURRENT thread for the duration of the block."""
    global _guard_thread_ident
    prev = _guard_thread_ident
    arm_loop_guard()
    try:
        yield
    finally:
        _guard_thread_ident = prev


def loop_guard_hits() -> dict[tuple[str, int, str], int]:
    """Inventory recorded in ``count`` mode: {(file, line, function): n}."""
    with _guard_lock:
        return dict(_guard_hits)


def _caller_outside_storage() -> tuple[str, int, str]:
    frame = sys._getframe(2)
    while frame is not None:
        fname = frame.f_code.co_filename
        if (os.sep + "storage" + os.sep not in fname and "storage/pg.py" not in fname
                and not fname.endswith("contextlib.py")):
            return (fname, frame.f_lineno, frame.f_code.co_name)
        frame = frame.f_back
    return ("?", 0, "?")


def _check_loop_guard() -> None:
    if _guard_thread_ident is not None and threading.get_ident() == _guard_thread_ident:
        f, line, fn = _caller_outside_storage()
        raise LoopGuardViolation(
            f"blocking DB call on the event loop thread: {f}:{line} ({fn})"
        )
    if _GUARD_MODE in ("count", "raise", "1"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # not a loop thread
        hit = _caller_outside_storage()
        if _GUARD_MODE == "count":
            with _guard_lock:
                _guard_hits[hit] = _guard_hits.get(hit, 0) + 1
            return
        raise LoopGuardViolation(
            f"blocking DB call on an event loop thread: {hit[0]}:{hit[1]} ({hit[2]})"
        )
