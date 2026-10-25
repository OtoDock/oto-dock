"""Event-loop stall watchdog — production monitoring for the one thread that
serves every WebSocket ping, login and dashboard frame.

Why: on 2026-09-03 the internal proxy froze for up to three minutes at a time
while its neighbours on the same VM kept running. The logs held NO line from
the frozen window, so the blocking call had to be reconstructed from health-
probe gaps and image-unpack timestamps a day later. This module makes every
stall self-describing, at two levels:

* **Slow loop** (``report_s``, ``LOOP_WATCHDOG_REPORT_S``, 0.25 s): the loop
  thread's stack is captured WHILE the stall lasts and logged once at INFO
  with the duration when it ends; at most ``_REPORT_BURST`` such lines a
  minute, the rest counted into the next one. At 100 sessions the symptom is
  many 100-600 ms stalls, never one long one.
* **Stall** (``threshold_s``, ``LOOP_WATCHDOG_THRESHOLD_S``, 2 s): a WARNING
  with the stack, repeated every ``_REPEAT_S`` while it lasts, and a
  recovery line with the total duration.

The loop's own tick also records how late it ran, per minute for an hour
(``lateness_ms``: p50 / p99 / max over the last minute and hour). Every
``_FD_SAMPLE_S`` the watcher counts the open descriptors and warns once above
80 % of the soft limit (at the limit uvloop silently drops every new
connection); those numbers are ``fd_stats()``, apart from ``stats()``, and
only ``GET /v1/admin/health`` shows either (never the public ``GET /health``).

Cost: one 50 ms timer on the loop, a daemon thread that reads one float every
25 ms, and a directory count every 10 s. Disabled entirely with a threshold
``<= 0`` (``LOOP_WATCHDOG_THRESHOLD_S``). ``start()`` takes the two cadences:
the satellite (this module is vendored there byte for byte, hash-pinned by
``scripts/sync-satellite-code.sh``) runs a 250 ms tick and a 100 ms watch, so
a laptop on battery wakes 14 times a second instead of 60.

The watcher also measures its own overshoot: a wait that returns
``_FROZEN_S`` or more late while the process burned no CPU meanwhile means
the whole PROCESS did not run (the host was paused, the daemon starved, a
SIGSTOP), so the line says that and shows no stack, instead of "event loop
stalled" over a loop that was never blocked. The CPU tells the two apart: a
call on the loop thread that holds the GIL (a huge ``json.dumps``, a large
regex) stops the watcher too, but the process is busy all through it, so it
is judged as the stall it is. After a freeze the loop is judged from the
moment the process ran again, never from the tick the freeze interrupted: a
loop still blocked ``threshold_s`` after the thaw is a stall with its stack.

Ordering (load-bearing, see startup.py): ``start()`` runs as the LAST boot
step, after the synchronous schema init / preflight / manifest scan, so a
long boot never logs a fake stall; ``stop()`` flags the thread BEFORE the
tick task is cancelled, so shutdown never logs one either.

Known limit: a C-level call that holds the GIL (a huge ``json.loads``) hides
the Python stack until it returns (the stall is counted and the stack shows
the caller once the call ends); blocking waits such as psycopg's release the
GIL, so the stall class this was built for is observable.
"""

from __future__ import annotations

import asyncio
import bisect
import collections
import hashlib
import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("otodock.loop-watchdog")

_TICK_S = 0.05
_WATCH_S = 0.025
# A watcher wake this late past its own wait is a frozen process, not a
# blocked loop (the proxy's threads share the freeze), unless the process
# burned at least this share of the overshoot as CPU meanwhile: then a
# call that holds the GIL kept the watcher out, and the loop was blocked.
_FROZEN_S = 0.5
_FROZEN_CPU_FRACTION = 0.5
_REPEAT_S = 10.0
_STACK_FRAMES = 25
_REPORT_BURST = 5
_REPORT_WINDOW_S = 60.0
_FD_SAMPLE_S = 10.0
_FD_WARN_FRACTION = 0.8
_FD_REARM_FRACTION = 0.7

# Tick lateness buckets (upper bounds, ms) and stall-duration buckets
# (lower bounds, ms).
_LATE_BOUNDS_MS = (10, 25, 50, 100, 250, 500, 1000, 2000)
_STALL_BOUNDS_MS = (250, 500, 1000, 2000, 5000)
_LATE_MINUTES = 60

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_tick_task: asyncio.Task | None = None
_last_tick: float = 0.0
_loop_thread_ident: int | None = None
_threshold_s: float = 0.0
_report_s: float = 0.0
_tick_s: float = _TICK_S
_watch_s: float = _WATCH_S
_executors: dict[str, ThreadPoolExecutor] = {}

# Counters (read by stats(); written by the watchdog thread only, except the
# lateness minutes, written by the tick).
_stalls = 0
_stall_seconds_total = 0.0
_last_stall_s = 0.0
_max_stall_s = 0.0
_slow = 0
_frozen = 0
_stall_hist = dict.fromkeys(_STALL_BOUNDS_MS, 0)
# (minute, counts per lateness bucket incl. the overflow one, max ms)
_late_minutes: collections.deque = collections.deque(maxlen=_LATE_MINUTES)
_fds: dict = {"open": None, "limit": None}


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def watch_executor(name: str, executor: ThreadPoolExecutor) -> None:
    """Report a thread pool's queue depth in ``stats()["executors"]``."""
    _executors[name] = executor


def _lateness(minutes: int) -> dict:
    now_min = int(time.monotonic() // 60)
    counts = [0] * (len(_LATE_BOUNDS_MS) + 1)
    top = 0.0
    for minute, cs, mx in list(_late_minutes):
        if now_min - minute < minutes:
            for i, c in enumerate(cs):
                counts[i] += c
            top = max(top, mx)
    total = sum(counts)

    def pct(p: float) -> float:
        if not total:
            return 0.0
        want, run = p * total, 0
        for i, c in enumerate(counts):
            run += c
            if run >= want:
                return float(_LATE_BOUNDS_MS[i]) if i < len(_LATE_BOUNDS_MS) else round(top, 1)
        return round(top, 1)

    return {"p50": pct(0.5), "p99": pct(0.99), "max": round(top, 1), "ticks": total}


def stats() -> dict:
    """Cheap snapshot for ``/health`` and tests."""
    executors = {}
    for name, ex in list(_executors.items()):
        try:
            executors[name] = {"workers": ex._max_workers, "threads": len(ex._threads),
                               "queued": ex._work_queue.qsize()}
        except AttributeError:
            continue
    return {
        "enabled": is_running(),
        "threshold_s": _threshold_s,
        "stalls": _stalls,
        "stall_seconds_total": round(_stall_seconds_total, 3),
        "last_stall_s": round(_last_stall_s, 3),
        "max_stall_s": round(_max_stall_s, 3),
        "report_s": _report_s,
        "slow": _slow,
        "frozen": _frozen,
        "histogram_ms": {str(b): n for b, n in _stall_hist.items()},
        "lateness_ms": {"1m": _lateness(1), "1h": _lateness(_LATE_MINUTES)},
        "executors": executors,
    }


def fd_stats() -> dict:
    """The last descriptor sample: ``{"open", "limit"}`` (None where the
    platform has no ``/proc`` or rlimits). Admin-only material."""
    return {"open": _fds["open"], "limit": _fds["limit"]}


def reset_stats() -> None:
    global _stalls, _stall_seconds_total, _last_stall_s, _max_stall_s, _slow, _frozen
    _stalls = 0
    _stall_seconds_total = 0.0
    _last_stall_s = 0.0
    _max_stall_s = 0.0
    _slow = 0
    _frozen = 0
    for b in _stall_hist:
        _stall_hist[b] = 0
    _late_minutes.clear()


def _record_lateness(late_s: float) -> None:
    ms = max(0.0, late_s * 1000.0)
    minute = int(time.monotonic() // 60)
    if not _late_minutes or _late_minutes[-1][0] != minute:
        _late_minutes.append((minute, [0] * (len(_LATE_BOUNDS_MS) + 1), 0.0))
    m, counts, mx = _late_minutes[-1]
    counts[bisect.bisect_left(_LATE_BOUNDS_MS, ms)] += 1
    if ms > mx:
        _late_minutes[-1] = (m, counts, ms)


async def _tick_loop() -> None:
    """Loop-side heartbeat: stamps ``_last_tick`` every ``_TICK_S``, records
    its own lateness, and records the loop thread ident (for
    ``sys._current_frames``) on its first run."""
    global _last_tick, _loop_thread_ident
    _loop_thread_ident = threading.get_ident()
    _last_tick = time.monotonic()
    try:
        while not _stop.is_set():
            await asyncio.sleep(_tick_s)
            now = time.monotonic()
            _record_lateness(now - _last_tick - _tick_s)
            _last_tick = now
    except asyncio.CancelledError:
        return


def _loop_stack() -> str:
    """The loop thread's stack as file/line/function only: no source lines,
    so formatting never reads a file (a slow disk is a stall cause)."""
    ident = _loop_thread_ident
    if ident is None:
        return "<loop thread ident unknown>"
    frame = sys._current_frames().get(ident)
    if frame is None:
        return "<loop thread frame unavailable>"
    lines: list[str] = []
    while frame is not None and len(lines) < _STACK_FRAMES:
        code = frame.f_code
        lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        frame = frame.f_back
    return "\n".join(reversed(lines))


def _sample_fds() -> None:
    try:
        n = len(os.listdir("/proc/self/fd"))
    except OSError:
        return
    try:
        import resource
        limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, OSError, ValueError):
        limit = None
    warned = _fds.get("warned", False)
    _fds.update(open=n, limit=limit)
    if not limit or limit <= 0:
        return
    if not warned and n >= limit * _FD_WARN_FRACTION:
        _fds["warned"] = True
        logger.warning("open file descriptors at %d of %d: new connections and session "
                       "spawns fail at the limit", n, limit)
    elif warned and n < limit * _FD_REARM_FRACTION:
        _fds["warned"] = False


class _Burst:
    """At most ``_REPORT_BURST`` lines a window; the rest are counted into
    the next line's "(N more)"."""

    def __init__(self) -> None:
        self.at: float | None = None
        self.n = 0
        self.held = 0

    def take(self, now: float) -> str | None:
        """The suffix for a line that may go out now, None for one held."""
        if self.at is None or now - self.at >= _REPORT_WINDOW_S:
            self.at, self.n = now, 0
        if self.n >= _REPORT_BURST:
            self.held += 1
            return None
        self.n += 1
        more = f" ({self.held} more since the last line)" if self.held else ""
        self.held = 0
        return more


def _watch() -> None:
    global _stalls, _stall_seconds_total, _last_stall_s, _max_stall_s, _slow, _frozen
    stalled_since: float | None = None
    stall_stack = ""
    escalated = False
    last_report = 0.0
    slow_lines = _Burst()
    frozen_lines = _Burst()
    next_fd_sample = 0.0
    last_wake = time.monotonic()
    last_cpu = time.process_time()
    # When the process last ran again after a freeze: until the loop has
    # ticked since, its lag is measured from here, not from the tick the
    # freeze interrupted (that tick is old by the freeze, which was no stall;
    # a loop still blocked after the thaw is one).
    thaw = 0.0

    def _end_stall(duration: float, now: float) -> None:
        global _stalls, _stall_seconds_total, _last_stall_s, _max_stall_s, _slow
        nonlocal stalled_since
        stalled_since = None
        ms = duration * 1000.0
        i = bisect.bisect_right(_STALL_BOUNDS_MS, ms) - 1
        if i >= 0:
            _stall_hist[_STALL_BOUNDS_MS[i]] += 1
        if _report_s > 0 and duration >= _report_s:
            _slow += 1
        if escalated:
            _stalls += 1
            _stall_seconds_total += duration
            _last_stall_s = duration
            _max_stall_s = max(_max_stall_s, duration)
            logger.info("event loop recovered after %.1fs stall", duration)
        elif _report_s > 0 and duration >= _report_s:
            more = slow_lines.take(now)
            if more is not None:
                logger.info("slow event loop: stalled %.2fs%s; loop thread stack:\n%s",
                            duration, more, stall_stack)

    while not _stop.wait(_watch_s):
        now = time.monotonic()
        cpu = time.process_time()
        overshoot = now - last_wake - _watch_s
        busy = cpu - last_cpu
        last_wake, last_cpu = now, cpu
        if overshoot >= _FROZEN_S and busy < overshoot * _FROZEN_CPU_FRACTION:
            # The watcher woke late and the process burned no CPU meanwhile:
            # nothing ran. A stall under way ended when the freeze began.
            _frozen += 1
            if stalled_since is not None:
                _end_stall(max(0.0, now - overshoot - stalled_since), now)
            thaw = now
            more = frozen_lines.take(now)
            if more is not None:
                logger.info(
                    "the process did not run for %.1fs%s (the host was paused or the "
                    "daemon starved); the loop was not blocked", overshoot, more,
                )
            continue
        if now >= next_fd_sample:
            next_fd_sample = now + _FD_SAMPLE_S
            _sample_fds()
        if _last_tick <= 0.0:
            continue  # tick task not started yet
        baseline = max(_last_tick, thaw)
        lag = now - baseline - _tick_s
        entry = _report_s if _report_s > 0 else _threshold_s
        if lag >= entry:
            if stalled_since is None:
                stalled_since = baseline + _tick_s
                stall_stack = _loop_stack()
                escalated = False
            if lag >= _threshold_s:
                if not escalated:
                    escalated = True
                    last_report = now
                    logger.warning(
                        "event loop stalled %.1fs (threshold %.1fs); loop thread stack:\n%s",
                        lag, _threshold_s, _loop_stack(),
                    )
                elif now - last_report >= _REPEAT_S:
                    last_report = now
                    logger.warning(
                        "event loop still stalled (%.1fs); loop thread stack:\n%s",
                        now - stalled_since, _loop_stack(),
                    )
        elif stalled_since is not None:
            _end_stall(_last_tick - stalled_since, now)


def start(threshold_s: float, report_s: float = 0.0, *,
          tick_s: float = _TICK_S, watch_s: float = _WATCH_S) -> bool:
    """Start the watchdog on the running loop. Returns False when disabled
    (``threshold_s <= 0``) or already running. ``report_s`` > 0 also reports
    shorter stalls (at INFO, rate-limited). ``tick_s`` and ``watch_s`` are
    the loop tick and the watcher's wait (the module defaults for the
    proxy; a coarser pair for a laptop). Must be called from the loop
    thread (it creates the tick task)."""
    global _thread, _tick_task, _threshold_s, _report_s, _last_tick, _tick_s, _watch_s
    if threshold_s is None or threshold_s <= 0:
        return False
    with _lock:
        if is_running():
            return False
        _stop.clear()
        _threshold_s = float(threshold_s)
        _report_s = float(report_s) if report_s and 0 < report_s < threshold_s else 0.0
        _tick_s = float(tick_s) if tick_s and tick_s > 0 else _TICK_S
        _watch_s = float(watch_s) if watch_s and watch_s > 0 else _WATCH_S
        _last_tick = 0.0
        _tick_task = asyncio.get_running_loop().create_task(
            _tick_loop(), name="loop-watchdog-tick",
        )
        _thread = threading.Thread(target=_watch, name="loop-watchdog", daemon=True)
        _thread.start()
    return True


def stop(join_timeout: float = 2.0) -> None:
    """Flag the thread to stop FIRST (so cancelling the tick task can never
    read as a stall), then cancel the tick task and join. Idempotent."""
    global _thread, _tick_task
    with _lock:
        _stop.set()
        thread, _thread = _thread, None
        task, _tick_task = _tick_task, None
    if task is not None and not task.done():
        task.cancel()
    if thread is not None and thread.is_alive():
        thread.join(timeout=join_timeout)


def self_hash() -> str:
    """SHA256 of this module's source — used by the satellite drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""
