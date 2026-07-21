"""PostgreSQL connection pool — shared singleton for all storage modules.

Uses psycopg3 sync pool. All storage functions remain synchronous
(called via asyncio.to_thread from async code).
"""

import os
import threading

import psycopg_pool
from psycopg.rows import dict_row


_pool: psycopg_pool.ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> psycopg_pool.ConnectionPool:
    """Return the shared connection pool (lazy init, double-checked locking)."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        import config
        # `open=False` matches the future psycopg_pool default — the
        # constructor stays side-effect-free and we open the pool with an
        # explicit `.open()` call. We want the singleton open at
        # construction (so the first DB request doesn't pay setup cost),
        # but doing it in two steps rather than via the current
        # `open=True` default keeps us aligned with where the library is
        # heading and survives the eventual removal of the legacy default.
        _pool = psycopg_pool.ConnectionPool(
            conninfo=config.DATABASE_URL,
            min_size=2,
            max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=False,
        )
        _pool.open()
        return _pool


def get_conn():
    """Return a context-managed connection from the pool.

    Usage:
        with get_conn() as conn:
            conn.execute("SELECT ...", (param,))
            conn.commit()

    On normal exit the connection is returned to the pool.
    On exception the transaction is rolled back automatically.
    """
    return get_pool().connection()


def close_pool(timeout: float = 3.0) -> None:
    """Close the pool at shutdown (idempotent). Its worker/scheduler threads
    are non-daemon — left open they stall interpreter exit until the atexit
    thread join, after uvicorn already logged its shutdown. A later
    ``get_pool()`` call would lazily re-create the pool, so this must be one
    of the LAST shutdown steps."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        try:
            pool.close(timeout=timeout)
        except Exception:
            pass
