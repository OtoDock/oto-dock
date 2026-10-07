"""Log records leave the event loop through a bounded queue; one thread
writes stderr and the rotating file.

Why: at the 2026-09-05 internal boot the loop thread stalled 8.9 s inside
``RotatingFileHandler.emit → stream.flush()`` while a deploy-time ``docker
prune`` hit the same disk — the same class as the 2026-09-03 outage
(synchronous Postgres commits on the loop), just a different writer. So
every handler that touches a file descriptor lives behind the queue: the
file, stderr (journald's stream socket and docker's log pipe both back onto
the disk), and uvicorn's own loggers (``app.py`` passes ``log_config=None``
so they propagate here instead of installing their own synchronous stream
handlers).

Drop, never block: a full queue means the writer is waiting on the disk, so
a blocking ``put`` would be the loop waiting on the disk one hop later, and
an unbounded queue turns a wedged disk into a memory leak (~12 MB/min at
the boot-storm rate of ~200 lines/s). Overflow drops the record and counts
it; the writer's first line after recovering reports how many went missing.
``QUEUE_SIZE`` covers ~50 s at that peak rate in ~10 MB.

``configure`` is a no-op when the target logger already has handlers —
``logging.basicConfig``'s contract, and what keeps the pytest capture
handlers (on the root by the time a test module imports ``app``) from being
joined by a writer thread and a ``proxy.log`` full of test noise.

Shutdown — three exits, each drained with a bound (a wedged writer must not
hold the exit hostage): uvicorn's ``Server.capture_signals`` re-raises the
SIGTERM/SIGINT it served after the graceful shutdown, so the process dies by
the signal and atexit never runs — ``exit_signal_handler`` (installed by
``app.py`` as the handler uvicorn restores) drains first, then lets the
default disposition end the process with the same status; the atexit hook
covers a plain interpreter exit; the exit failsafe in ``startup.py`` drains
with a shorter bound and skips ``logging.shutdown()`` when the drain fails —
the writer then holds the handler lock and a flush would block forever.
``SIGKILL`` / ``os._exit`` lose at most the queue contents.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import logging.handlers
import os
import queue
import re
import signal
import threading
import time
from pathlib import Path

QUEUE_SIZE = 10_000
ATEXIT_DRAIN_S = 30.0
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# One line per HTTP request — stderr only, never the file: at ~10 MB/day
# it would cut the file's 120 MB window from months to under two weeks.
_ACCESS_LOGGER = "uvicorn.access"
# A token a URL carries: the WOPI ``access_token`` (in Collabora's socket path
# it is URL-encoded), a share or download ``token`` / ``t``, a ``key``, and
# the usual names of keys, signatures, codes and passwords. The value goes,
# the name stays, so a line still says what was asked for.
_URL_SECRET = re.compile(
    r"((?:[?&]|%3F|%26)(?:access_token|token|key|t|api_?key|sig|signature|"
    r"x-amz-signature|x-goog-signature|code|password|secret)(?:=|%3D))(?:(?!%26)[^&\s\"'#])+",
    re.IGNORECASE,
)
# A share link's token is a path segment (``/s/<token>``, its socket too).
_SHARE_PATH = re.compile(r"(/s/)[A-Za-z0-9_-]{8,}")
# The HTTP clients log every request's full URL at INFO.
_QUIET_LOGGERS = ("httpx", "httpcore")

_active: LogQueue | None = None


_DROPPED_ATTR = "_log_queue_dropped"


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """``put_nowait`` or drop. Never ``handleError``: the stdlib route would
    write a traceback to stderr from the producer thread for every dropped
    record — the exact write this module removes from the loop.

    The drop count rides in-band: the first record that gets a slot after a
    gap carries the number of records lost before it, so the writer's
    overflow line lands exactly where the gap is in the output."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__(q)
        self._lock = threading.Lock()
        self._dropped = 0        # not yet reported
        self.dropped_total = 0   # lifetime, for /health

    def enqueue(self, record: logging.LogRecord) -> None:
        # ``record`` is prepare()'s copy — safe to annotate.
        with self._lock:
            n, self._dropped = self._dropped, 0
        if n:
            setattr(record, _DROPPED_ATTR, n)
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self._dropped += n + 1
                self.dropped_total += 1

    def take_dropped(self) -> int:
        with self._lock:
            n, self._dropped = self._dropped, 0
        return n


def redact(text: str) -> str:
    """``text`` with the value of every URL token parameter replaced by
    ``[redacted]``."""
    if "/s/" in text:
        text = _SHARE_PATH.sub(r"\1[redacted]", text)
    if "=" not in text and "%3D" not in text and "%3d" not in text:
        return text
    return _URL_SECRET.sub(r"\1[redacted]", text)


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """The rotating file, created 0600 (every rotation too): it holds
    request paths, addresses and account names."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with contextlib.suppress(OSError):  # a file owned by another uid keeps its mode
            os.fchmod(fd, 0o600)
        return open(fd, self.mode, encoding=self.encoding, errors=self.errors)


class _Listener(logging.handlers.QueueListener):
    """The single consumer: the overflow line precedes the record that
    carries the count."""

    def __init__(self, q: queue.Queue, *handlers: logging.Handler,
                 source: _DroppingQueueHandler) -> None:
        super().__init__(q, *handlers, respect_handler_level=True)
        self._source = source

    def handle(self, record: logging.LogRecord) -> None:
        n = getattr(record, _DROPPED_ATTR, 0)
        if n:
            super().handle(self._overflow_record(n))
        super().handle(self._redacted(record))

    @staticmethod
    def _redacted(record: logging.LogRecord) -> logging.LogRecord:
        """Off the loop: ``prepare`` already merged the args (and any
        traceback) into ``msg``; a record built here still has its args."""
        text = record.getMessage()
        clean = redact(text)
        if clean is not text and clean != text:
            record.msg, record.args = clean, None
        return record

    @staticmethod
    def _overflow_record(n: int) -> logging.LogRecord:
        return logging.LogRecord(
            "claude-proxy.log-queue", logging.WARNING, __file__, 0,
            "log queue overflow: dropped %d records while the log writer "
            "was blocked", (n,), None,
        )

    def enqueue_sentinel(self) -> None:
        # Clean path: wait for a slot. The stdlib's put_nowait raises
        # queue.Full whenever the writer is merely busy.
        self.queue.put(self._sentinel)

    def stop(self) -> None:
        super().stop()
        self._report_unreported_drops()

    def drain(self, timeout_s: float) -> bool:
        """Finish the queue and stop the thread, waiting at most
        ``timeout_s``. False when the writer is wedged (the sentinel could
        not be placed or the thread did not finish in time)."""
        t = self._thread
        if t is None:
            return True
        deadline = time.monotonic() + timeout_s
        try:
            self.queue.put(self._sentinel, timeout=timeout_s)
        except queue.Full:
            return False
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            return False
        self._thread = None
        self._report_unreported_drops()
        return True

    def _report_unreported_drops(self) -> None:
        """Drops with no later record to carry them (drops, then silence,
        then shutdown) — written by the stopping thread once the writer
        thread is gone."""
        n = self._source.take_dropped()
        if n:
            super().handle(self._overflow_record(n))


class LogQueue:
    """One configured writer: the queue handler on the logger + the thread
    behind it. ``stop()`` is the clean path (unbounded, tests + normal
    exit); ``drain()`` the bounded one (atexit, failsafe)."""

    def __init__(self, handler: _DroppingQueueHandler,
                 listener: _Listener) -> None:
        self._handler = handler
        self._listener = listener

    def stop(self) -> None:
        self._listener.stop()

    def drain(self, timeout_s: float) -> bool:
        return self._listener.drain(timeout_s)

    def stats(self) -> dict:
        q = self._handler.queue
        return {
            "dropped": self._handler.dropped_total,
            "queued": q.qsize(),
            "capacity": q.maxsize,
        }


def configure(
    *,
    log_path: Path,
    max_bytes: int,
    backup_count: int,
    queue_size: int = QUEUE_SIZE,
    level: int = logging.INFO,
    logger: logging.Logger | None = None,
) -> LogQueue | None:
    """Install the queue handler on ``logger`` (default: the root) and start
    the writer thread. Returns None — and installs nothing — when the logger
    already has handlers (see the module docstring)."""
    global _active
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    target = logger if logger is not None else logging.getLogger()
    if target.handlers:
        return None
    fmt = logging.Formatter(_FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    with contextlib.suppress(OSError):  # an existing file from before the floor
        os.chmod(log_path, 0o600)
    file_handler = _PrivateRotatingFileHandler(
        str(log_path), maxBytes=max_bytes, backupCount=backup_count,
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(lambda record: record.name != _ACCESS_LOGGER)
    q: queue.Queue = queue.Queue(queue_size)
    handler = _DroppingQueueHandler(q)
    listener = _Listener(q, stream, file_handler, source=handler)
    target.addHandler(handler)
    target.setLevel(level)
    listener.start()
    _active = LogQueue(handler, listener)
    return _active


def drain(timeout_s: float) -> bool:
    """Bounded drain of the active writer; True when there is nothing to
    drain (no writer configured) or it finished in time."""
    return True if _active is None else _active.drain(timeout_s)


def stats() -> dict:
    return {"dropped": 0, "queued": 0, "capacity": 0} if _active is None else _active.stats()


def exit_signal_handler(signum: int, frame) -> None:
    """SIGTERM/SIGINT handler for the serving process: drain, then die by
    the signal. uvicorn swaps this out for its own graceful handler while
    it serves and restores + re-raises it afterwards (``capture_signals``),
    which is when the shutdown's last records are still in the queue."""
    drain(ATEXIT_DRAIN_S)
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


def _atexit_drain() -> None:
    drain(ATEXIT_DRAIN_S)


# Registered after logging's own atexit hook, so it runs BEFORE
# logging.shutdown() (LIFO): the queue is empty when the handlers flush.
atexit.register(_atexit_drain)
