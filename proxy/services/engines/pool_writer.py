"""The ordered binding writer: one thread that writes the pool's rows in the order they were queued.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

logger = logging.getLogger(__name__)


# --- the binding writer ------------------------------------------------------
# One worker carries every write of the persisted mirror, the seat counters
# and the rests' rows, in order: a bind's upsert lands before its release's
# delete, so a deferred write leaves no orphan row, a
# release's two statements leave the loop, and a reaper's burst is a queue,
# not a stall. A job is a pure store call: it never takes _session_maps_lock
# and never acquires. The spawn flows await a bind's future before they
# proceed, so the row is durable before the session runs; releases and rest
# writes are fire-and-forget. After the shutdown drain every write runs
# inline (the pools close right after).
_binding_writer: ThreadPoolExecutor | None = None


_binding_writer_lock = threading.Lock()


_binding_writes_closed = False


# The shutdown drain's bound: a database stall must not hold the shutdown.
# What is still queued then is dropped; the next boot's counter reset
# (``reset_active_sessions``) and binding prune repair it.
_BINDING_DRAIN_TIMEOUT_S = 30.0


def _writer() -> ThreadPoolExecutor:
    global _binding_writer
    if _binding_writer is None:
        with _binding_writer_lock:
            if _binding_writer is None:
                _binding_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sub-bind")
    return _binding_writer


def _guarded(label: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("Pool: %s failed (the in-memory state stands)", label)
        return None


def _queue_write(label: str, fn, *args) -> Future:
    """Queue one store write on the binding writer; its future resolves when
    the row landed (None when the write failed, logged)."""
    if _binding_writes_closed:
        done: Future = Future()
        done.set_result(_guarded(label, fn, *args))
        return done
    return _writer().submit(_guarded, label, fn, *args)


def flush_binding_writes(timeout: float | None = 30.0) -> None:
    """Wait for every queued write: a seat acquire before it reads the
    counts, a reconcile, the tests, the shutdown. Never on the loop thread
    (a running loop skips it: the counts may then read a write behind)."""
    if _binding_writer is None or _binding_writes_closed:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            _writer().submit(lambda: None).result(timeout)
        except TimeoutError:
            # A wedged writer (a database stall) must not fail the caller's
            # spawn: the counts may then read a write behind.
            logger.warning("Pool: binding writes not flushed within %ss; counts may read a write behind",
                           timeout)
    else:
        logger.debug("Pool: flush_binding_writes on the loop thread skipped")


def close_binding_writes(timeout: float | None = None) -> bool:
    """The shutdown, off the loop (the lifespan runs it in a worker thread):
    wait at most ``timeout`` (``_BINDING_DRAIN_TIMEOUT_S``, 30 s) for the
    queued writes, then every later write runs inline. Returns False when
    the writer did not drain in time (a database stall): it is then shut
    down without waiting, the writes still queued are dropped (the next
    boot's ``reset_active_sessions`` and binding prune repair what they
    would have written) and the one in progress finishes on its own thread."""
    global _binding_writes_closed
    limit = _BINDING_DRAIN_TIMEOUT_S if timeout is None else timeout
    writer = _binding_writer
    drained = True
    if writer is not None and not _binding_writes_closed:
        try:
            writer.submit(lambda: None).result(limit)
        except TimeoutError:
            drained = False
            logger.warning(
                "Pool: binding writes not drained within %ss at shutdown; "
                "the queued ones are dropped", limit,
            )
    _binding_writes_closed = True
    if writer is not None:
        writer.shutdown(wait=drained, cancel_futures=not drained)
    return drained


def _reopen_binding_writes() -> None:
    """Tests: a fresh writer after a close."""
    global _binding_writer, _binding_writes_closed
    _binding_writer = None
    _binding_writes_closed = False
