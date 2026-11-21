"""Disk work the event loop must never wait on: the one owner.

- ``run(fn, ...)``: a commit the caller awaits (an upload's writes, its fsync
  and its rename; a satellite pull's commit) on the file-commit pool.
  ``run_in(lane, fn, ...)``: the same on a lane of its own, for work that can
  hold a thread for long (``docker cp`` after a tool call).
- ``behind(fn, ..., key=session_id)``: disk work nobody waits on (a closed
  session's folders, its gateway descriptor). ``settled(key)`` waits for what
  is left under a key: every start of a session id does, through
  ``session_closing.wait_closed``.
- ``StateFile``: a file the platform rewrites whole from memory (the session
  index, the security index, the Docker tools' held sessions), encoded on the
  loop and written on a thread of its own.

The file-commit pool is its own on purpose. ``run_db``'s lanes carry the
store calls, and one multi-second fsync parked there would stall every query
behind it. The default executor carries every ``asyncio.to_thread`` site in
the proxy, and a disk that stalls for seconds would drain it for everyone.
Here a slow commit queues behind the other commits and nothing else. A state
file has a thread of its own for the same reason one step further: a removal
from the security index never queues behind an upload's batches.
"""

import asyncio
import contextlib
import functools
import logging
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import config
from core import loop_watchdog

logger = logging.getLogger("otodock")

_executor: ThreadPoolExecutor | None = None
# The lanes beside the pool: name -> (workers, pool once used).
# "session-probes": the searches of a session's own engine folder before a
# start or a resume (a transcript, a rollout), bounded but long in a big tree.
_LANES: dict[str, int] = {"tool-outputs": 2, "session-homes": 2, "session-probes": 2}
_lanes: dict[str, ThreadPoolExecutor] = {}
_lanes_lock = threading.Lock()
# key -> the ``behind`` jobs not done yet (written by the worker threads too).
_behind: dict[str, set[Future]] = {}
_behind_lock = threading.Lock()
#: How long the shutdown's ``flush_all`` waits for the state files, together.
FLUSH_WAIT_S = 10.0
# Every state file of the process, for the shutdown's ``flush_all``.
_state_files: "weakref.WeakSet[StateFile]" = weakref.WeakSet()


def executor() -> ThreadPoolExecutor:
    """The shared pool, created on first use and reported to the watchdog."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=config.FILE_COMMIT_WORKERS, thread_name_prefix="file-commit",
        )
        loop_watchdog.watch_executor("file-commit", _executor)
    return _executor


def _lane(name: str, workers: int) -> ThreadPoolExecutor:
    """A pool of its own, created on first use and reported to the watchdog."""
    with _lanes_lock:
        pool = _lanes.get(name)
        if pool is None:
            pool = _lanes[name] = ThreadPoolExecutor(max_workers=workers,
                                                     thread_name_prefix=name.replace(":", "-"))
            loop_watchdog.watch_executor(name, pool)
        return pool


async def run(fn, /, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` on the file-commit pool and await it."""
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(executor(), lambda: fn(*args, **kwargs))
    return await loop.run_in_executor(executor(), fn, *args)


async def run_in(lane: str, fn, /, *args, **kwargs):
    """``run`` on one of ``_LANES``: work that can hold a thread for long
    never queues the commits."""
    pool = _lane(lane, _LANES[lane])
    return await asyncio.get_running_loop().run_in_executor(
        pool, functools.partial(fn, *args, **kwargs))


def _logged(fn, args: tuple, name: str) -> None:
    try:
        fn(*args)
    except Exception:
        logger.exception("disk work %s failed", name)


def behind(fn, /, *args, name: str, key: str = "") -> None:
    """Run ``fn(*args)`` and let the caller go on: queued on the file-commit
    pool when a loop runs on this thread, at once otherwise (a worker thread,
    a script, a sync test). A failure is logged. The job is a pool job, not a
    task, so a shutdown's cancel of the loop's tasks never drops it: the
    interpreter's exit runs the pool's queue. ``key`` (a session id) lets
    ``settled`` wait for it."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _logged(fn, args, name)
        return
    job = executor().submit(_logged, fn, args, name)
    if key:
        with _behind_lock:
            _behind.setdefault(key, set()).add(job)
        job.add_done_callback(functools.partial(_behind_done, key))


def _behind_done(key: str, job: Future) -> None:
    with _behind_lock:
        jobs = _behind.get(key)
        if jobs is not None:
            jobs.discard(job)
            if not jobs:
                _behind.pop(key, None)


async def settled(key: str, timeout: float) -> bool:
    """Wait, without holding the loop, for the work ``behind`` queued under
    ``key``. False (logged) when some still runs after ``timeout``: the jobs
    that have not started by then are cancelled, so a removal of a session's
    last life never runs after its new life began."""
    with _behind_lock:
        jobs = [j for j in _behind.get(key, ()) if not j.done()]
    if not jobs:
        return True
    _done, pending = await asyncio.wait([asyncio.wrap_future(j) for j in jobs],
                                        timeout=max(0.0, timeout))
    if pending:
        dropped = sum(1 for j in jobs if not j.done() and j.cancel())
        logger.warning("%s: disk work of its last life still runs after %.0f s; going on "
                       "(%d not started, dropped)", key[:8], timeout, dropped)
        return False
    return True


def atomic_replace(path: Path, blob: str, *, fsync: bool) -> bool:
    """Replace ``path`` with ``blob`` the way ``safe_fs.atomic_writer`` does
    (a temp file beside it, renamed onto the name: a crash leaves the old
    file, never a truncated one). False on failure, logged. Blocking:
    ``StateFile`` calls it on its own thread."""
    from services.infra import safe_fs
    try:
        safe_fs.atomic_write_beneath(path.parent, path.name, blob.encode("utf-8"), fsync=fsync)
        return True
    except OSError as e:
        logger.error("Failed to save %s: %s", path.name, e)
        return False


class StateFile:
    """A file rewritten whole from memory, without holding the loop.

    ``mark_dirty`` counts a change. With no ``delay`` the encode runs inside
    that call (the snapshot of that moment, as the synchronous save took it)
    and the write is handed to the file's thread before the call returns; a
    change while a write is in flight keeps its own snapshot, the newest one,
    which is written when that write ends. With a delay a timer encodes and
    writes, so a burst of changes is one write. Each write carries the count
    of changes it holds and only a newer one is installed.

    With no running loop (import, a script, a sync test) the write is
    synchronous on the caller's thread; a change from another thread is
    handed to the owner loop. ``written`` lets a caller wait for its change
    to be on disk without holding the loop."""

    def __init__(self, name: str, path: Callable[[], Path], encode: Callable[[], str], *,
                 fsync: bool, delay: Callable[[], float] = lambda: 0.0) -> None:
        self.name = name
        self._path = path
        self._encode = encode
        self._fsync = fsync
        self._delay = delay
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._inflight: asyncio.Future | None = None
        self._dirty = False                      # a delayed change not encoded yet
        self._pending: tuple[Path, str, int] | None = None   # encoded, waits for the write in flight
        self._seq = 0          # changes counted
        self._installed = 0    # the newest change on disk
        self._settled = 0      # the newest change whose write ended, landed or not
        self._waiters: list[tuple[int, asyncio.Future]] = []
        self._lock = threading.Lock()
        _state_files.add(self)

    def _thread(self) -> ThreadPoolExecutor:
        return _lane(f"state-file:{self.name}", 1)

    def mark_dirty(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        owner = self._loop
        if owner is not None and (owner.is_closed() or (
                owner is not running and not owner.is_running())):
            self._drop_owner()
            owner = None
        if owner is not None and owner is not running:
            try:
                owner.call_soon_threadsafe(self.mark_dirty)
                return
            except RuntimeError:  # the owner closed in between
                self._drop_owner()
        self._seq += 1
        if running is None:
            self._dirty = True
            self.flush_sync()
            return
        self._loop = running
        delay = self._delay()
        if delay > 0:
            self._dirty = True
            if self._handle is None and self._inflight is None:
                self._handle = running.call_later(delay, self._start_flush)
            return
        snapshot = self._snapshot()
        if snapshot is None:
            return
        if self._inflight is None:
            self._submit(*snapshot)
        else:
            self._pending = snapshot

    def _drop_owner(self) -> None:
        """The owner loop is gone: its timer and its waiters went with it. A
        write it left in flight still ends on the file's thread."""
        self._loop = None
        self._handle = self._inflight = None
        self._waiters.clear()

    def _snapshot(self) -> tuple[Path, str, int] | None:
        """The state now, encoded, with its path and its count; None (logged,
        its waiters told) when the encode fails."""
        self._dirty = False
        gen = self._seq
        try:
            return self._path(), self._encode(), gen
        except Exception:
            logger.exception("%s: the state could not be encoded; nothing was written", self.name)
            self._settle(gen)
            return None

    def _install(self, path: Path, blob: str, gen: int) -> bool:
        """Write ``blob`` when it is newer than what is on disk. True when it
        landed or something newer already had."""
        with self._lock:
            if gen <= self._installed:
                return True
            if atomic_replace(path, blob, fsync=self._fsync):
                self._installed = gen
                return True
            return False

    def _submit(self, path: Path, blob: str, gen: int) -> None:
        """Hand the write to the file's thread (inside this call). A submit
        that fails (no thread can start) is logged and its waiters told: it
        never raises into the caller's close."""
        loop = self._loop
        if loop is None:
            return
        try:
            self._inflight = loop.run_in_executor(self._thread(), self._install, path, blob, gen)
        except Exception:
            logger.exception("%s: the write could not start", self.name)
            self._inflight = None
            self._settle(gen)
            return
        self._inflight.add_done_callback(functools.partial(self._written_one, gen))

    def _start_flush(self) -> None:
        self._handle = None
        if not self._dirty or self._loop is None:
            return
        snapshot = self._snapshot()
        if snapshot is not None:
            self._submit(*snapshot)

    def _written_one(self, gen: int, fut: asyncio.Future) -> None:
        self._inflight = None
        if not fut.cancelled() and fut.exception() is not None:
            logger.error("%s: write failed", self.name, exc_info=fut.exception())
        self._settle(gen)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if self._pending is not None:
            snapshot, self._pending = self._pending, None
            self._submit(*snapshot)
        elif self._dirty and self._handle is None:
            self._handle = loop.call_later(max(0.0, self._delay()), self._start_flush)

    def _settle(self, gen: int) -> None:
        """The write of every change up to ``gen`` ended: wake their waiters,
        each with whether its change is on disk. On the owner loop."""
        self._settled = max(self._settled, gen)
        keep = []
        for target, fut in self._waiters:
            if target <= gen:
                if not fut.done():
                    fut.set_result(self._installed >= target)
            else:
                keep.append((target, fut))
        self._waiters = keep

    def reset_for_tests(self) -> None:
        """Forget the owner loop and what waits, after the write in flight."""
        if self._lock.acquire(timeout=30):
            self._lock.release()
        self._drop_owner()
        self._pending = None
        self._dirty = False

    def counted(self) -> int:
        """The count of changes so far: a caller's ``written(upto=...)``."""
        return self._seq

    async def written(self, timeout: float, *, upto: int | None = None) -> bool:
        """Wait, without holding the loop, until every change counted before
        this call (or up to ``upto``) is on disk. False when its write failed,
        ``timeout`` ran out or another running loop owns the file (logged);
        True at once when nothing is pending."""
        loop = asyncio.get_running_loop()
        owner = self._loop
        if owner is not None and owner is not loop and owner.is_running():
            # A change made on this loop was handed to the owner and may not be
            # counted yet, so nothing here can say it landed.
            logger.warning("%s: another loop owns the file's writes; this wait cannot see "
                           "them land", self.name)
            return False
        target = self._seq if upto is None else upto
        if self._installed >= target:
            return True
        if owner is not loop:
            # Written synchronously, or the owner loop is gone: the disk answers.
            return False
        if self._settled >= target:
            return False
        fut = loop.create_future()
        self._waiters.append((target, fut))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError:
            logger.warning("%s: not on disk after %.0f s; the close goes on and the write lands "
                           "when the disk answers", self.name, timeout)
            return False

    def flush_sync(self, wait_s: float | None = None) -> None:
        """Write the newest state now. With ``wait_s`` (the shutdown) the write
        goes to the file's thread, after a write in flight, and the caller
        waits at most that long, also for a write in flight alone: a disk that
        never answers keeps the job queued for the interpreter's exit, never
        the caller. Without it (no running loop) the write runs on the calling
        thread."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        snapshot = self._snapshot() if self._dirty else self._pending
        self._pending = None
        if snapshot is None:
            if wait_s is not None and self._inflight is not None:
                # The lane has one thread: a job that does nothing ends after the
                # write in flight, so the shutdown waits for that write too.
                self._wait_for_lane(self._thread().submit(lambda: None), wait_s)
            return
        path, blob, gen = snapshot
        if wait_s is None:
            self._install(path, blob, gen)
        else:
            self._wait_for_lane(self._thread().submit(self._install, path, blob, gen), wait_s)
        loop = self._loop
        if loop is None or loop.is_closed():
            self._settled = max(self._settled, gen)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._settle(gen)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._settle, gen)

    def _wait_for_lane(self, job: Future, wait_s: float) -> None:
        try:
            job.result(timeout=max(0.0, wait_s))
        except TimeoutError:
            logger.warning("%s: the shutdown's write is still queued after %.0f s", self.name, wait_s)


def flush_all() -> None:
    """Write every state file's pending change now (the graceful shutdown),
    waiting at most ``FLUSH_WAIT_S`` for all of them together."""
    deadline = time.monotonic() + FLUSH_WAIT_S
    for state_file in list(_state_files):
        try:
            state_file.flush_sync(wait_s=deadline - time.monotonic())
        except Exception:
            logger.exception("%s: the shutdown's write failed", state_file.name)
