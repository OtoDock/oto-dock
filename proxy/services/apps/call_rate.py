"""The pace of an app's calls: one token bucket, and the rule for a platform
method on top of it.

The bucket (``take``) is the one every app route counts with: the app's own
API, its egress, its notifications, and a platform method. A platform
method (``otodock.platform`` from a page, the same call from an app's
server or an agent) is paced by ``platform_call``:

- a RESERVE per app and caller, there for a page that opens (its first
  screen reads a few dozen small files at once), refilled at the steady
  rate, so a page that never stops calling gets the steady rate and no more;
- a CEILING per app across its callers, so one app with many people on it
  cannot take the platform's worker threads;
- a tighter bucket for the WRITES.

All three are taken together or not at all, and a refusal says how long to
wait. Calls refused together are each told a LATER moment, one refill
apart: told the same wait they would all come back at once, one would
pass and the rest be refused a second time. So a page that asks for more
than its reserve gets its answers at the steady rate instead of refusals.
``slot`` bounds the calls that RUN at once, per app and caller, per app
and for the whole platform, so sixty reads of 2 MB admitted at once are
never sixty files in memory and one app's slow calls never hold every
place. The rest wait their turn, each for ``PLACE_WAIT_S`` at most, then
it is told to come back. A waiting call holds its connection, and the
server answers a plain 503 to everyone past its own bound of open
connections (``config.HTTP_LIMIT_CONCURRENCY``), so the line has an end
well under that bound (``line_bound``): with that many admitted calls
waiting for a place, the next one is told to come back before any bucket
is taken.

Every take happens on the event loop: the table has no lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import deque
from collections.abc import AsyncIterator

import config

_buckets: dict[str, tuple[float, float]] = {}
_BUCKETS_SOFT = 4096
_BUCKETS_HARD = 8192
# A table past its soft bound is scanned once a second, or sooner when this
# many keys came in since the last scan: never at every take.
_SWEEP_STEP = 512
_swept_at = 0.0
_swept_size = 0
# Per bucket: the moment the last refused call was told to come back.
_promised: dict[str, float] = {}

# A platform method, per app and caller: the reserve and its refill. Sized
# on a measured opening: a first screen reads
# about fifteen files, a list of twenty projects with their pictures takes
# twenty-six calls, and a person who opens the app, a project and the list
# again within a few seconds makes about seventy.
RESERVE = 80.0
REFILL_PER_S = 4.0
# Across every caller of one app.
APP_CEILING = 600.0
APP_REFILL_PER_S = 100.0
# The methods that change something, per app and caller.
WRITE_RESERVE = 8.0
WRITE_REFILL_PER_S = 2.0
WRITE_METHODS = frozenset({
    "files.write", "viewer.data.write", "notifications.create", "setup.complete",
})
# How many platform methods run at once: of one caller on one app, of one
# app across its callers, and on the whole platform. The platform's number
# is three apps' share, so two apps with slow calls never keep a third one
# out. With the media route's eight gates it is twenty of the default
# pool's thirty-two threads.
IN_FLIGHT_PER_CALLER = 4
IN_FLIGHT_PER_APP = 4
IN_FLIGHT_PLATFORM = 12
# How long an admitted call waits for its places before it is told to come
# back.
PLACE_WAIT_S = 2.0


def line_bound() -> int:
    """How many requests may wait in one line, across the platform. Each
    holds its connection, and the server refuses everyone past
    ``config.HTTP_LIMIT_CONCURRENCY`` open connections (the dashboard's
    sockets and idle connections count there too): a line takes a quarter
    of that bound at most, so this one and the media route's together leave
    half of it free. With the server's bound switched off, its default
    stands in."""
    return max(1, (config.HTTP_LIMIT_CONCURRENCY or 4000) // 4)


# How many admitted calls may wait for a place, across every caller. What a
# call past them is told: come back in a second, one served call (measured:
# about 5 ms) after the call refused before it.
WAITING_PLATFORM = line_bound()
LINE_WAIT_S = 1.0
LINE_STEP_S = 0.005
_LINE = "platform|line"

# The words a page has matched since 1.7: kept as they are.
TOO_FAST = "Too fast — try again in a moment"


def _level(key: str, rate: float, burst: float, now: float) -> float:
    tokens, at = _buckets.get(key, (burst, now))
    return min(burst, tokens + (now - at) * rate)


def _purge(now: float) -> None:
    global _swept_at, _swept_size
    size = len(_buckets)
    if size <= _BUCKETS_SOFT or (now - _swept_at < 1.0 and size - _swept_size < _SWEEP_STEP):
        return
    _sweep(now)
    _swept_at, _swept_size = now, len(_buckets)


def _sweep(now: float) -> None:
    for k in [k for k, (_t, at) in _buckets.items() if now - at > 120]:
        _buckets.pop(k, None)
    for k in [k for k, at in _promised.items() if at < now]:
        _promised.pop(k, None)
    if len(_buckets) > _BUCKETS_HARD:
        # A flood of keys nobody uses twice: a bucket idle for five seconds
        # holds nothing worth keeping.
        for k in [k for k, (_t, at) in _buckets.items() if now - at > 5]:
            _buckets.pop(k, None)


def _later(key: str, now: float, wait: float, step: float) -> float:
    """A refused call's own moment to come back, as seconds from now:
    ``wait`` away, or one ``step`` after the call refused before it on
    ``key``."""
    back = max(now + wait, _promised.get(key, 0.0) + step)
    _promised[key] = back
    return back - now


def take(key: str, rate: float, burst: float) -> bool:
    """One call against ``key``: False when its bucket is empty."""
    now = time.monotonic()
    tokens = _level(key, rate, burst, now)
    if tokens < 1.0:
        _buckets[key] = (tokens, now)
        return False
    _buckets[key] = (tokens - 1.0, now)
    _purge(now)
    return True


class TooFast(Exception):
    """A platform call past its pace; ``retry_after`` is the seconds until
    the call would pass."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(TOO_FAST)
        self.retry_after = retry_after

    @property
    def retry_after_ms(self) -> int:
        return max(1, math.ceil(self.retry_after * 1000))

    @property
    def retry_after_s(self) -> int:
        """The ``Retry-After`` header: whole seconds, never zero."""
        return max(1, math.ceil(self.retry_after))


def platform_call(app_id: str, actor: str, method: str, cost: float = 1.0) -> None:
    """Admit one platform method of ``actor`` on ``app_id`` or raise
    ``TooFast``. ``cost`` is what the call takes from each bucket (a batch
    of media links costs more than one)."""
    now = time.monotonic()
    if _waiting >= WAITING_PLATFORM:
        raise TooFast(_later(_LINE, now, LINE_WAIT_S, LINE_STEP_S))
    wanted = [
        (f"{app_id}|platform|*", APP_REFILL_PER_S, APP_CEILING),
        (f"{app_id}|platform|{actor}", REFILL_PER_S, RESERVE),
    ]
    if method in WRITE_METHODS:
        wanted.append((f"{app_id}|platform-write|{actor}", WRITE_REFILL_PER_S, WRITE_RESERVE))
    levels = [_level(key, rate, burst, now) for key, rate, burst in wanted]
    wait = 0.0
    short: tuple[str, float] | None = None
    for (key, rate, burst), tokens in zip(wanted, levels, strict=True):
        need = min(cost, burst)
        if tokens < need and (need - tokens) / rate > wait:
            wait, short = (need - tokens) / rate, (key, rate)
    if short is not None:
        # Nothing is taken from any bucket: a refused call costs nothing.
        for (key, _rate, _burst), tokens in zip(wanted, levels, strict=True):
            _buckets[key] = (tokens, now)
        # Its own moment to come back, one refill after the call refused
        # before it on the bucket that is short.
        key, rate = short
        raise TooFast(_later(key, now, wait, cost / rate))
    for (key, _rate, burst), tokens in zip(wanted, levels, strict=True):
        _buckets[key] = (tokens - min(cost, burst), now)
    _purge(now)


class Gate:
    """At most ``limit`` holders; the rest wait in order. Its futures belong
    to the loop that waits, so a gate outlives no loop it is used on."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.busy = 0
        self.waiters: deque[asyncio.Future[None]] = deque()

    async def enter(self) -> None:
        if self.busy < self.limit and not self.waiters:
            self.busy += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.append(fut)
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self.leave()   # the place was handed over as the wait ended
            else:
                with contextlib.suppress(ValueError):
                    self.waiters.remove(fut)
            raise

    def leave(self) -> None:
        while self.waiters:
            fut = self.waiters.popleft()
            if not fut.done():
                fut.set_result(None)   # the place goes to the next in line
                return
        self.busy -= 1

    @property
    def idle(self) -> bool:
        return self.busy == 0 and not self.waiters

    @property
    def full(self) -> bool:
        """Whether the next one to enter would wait."""
        return self.busy >= self.limit or bool(self.waiters)


_platform_gate = Gate(IN_FLIGHT_PLATFORM)
_caller_gates: dict[str, Gate] = {}
_app_gates: dict[str, Gate] = {}
_waiting = 0   # admitted calls that wait for a place: their caller's, their app's or the platform's


_Held = tuple[dict[str, Gate], str, Gate]


def _release(held: list[_Held], entering: _Held | None = None) -> None:
    """Give back the places a call holds. ``entering`` is the gate it stood
    in line for when it gave up: a wait that ends as a place is handed over
    gives the place on, which can leave that gate with nobody, so it is
    dropped here too."""
    for gates, key, gate in reversed(held):
        gate.leave()
        if gate.idle and gates.get(key) is gate:
            del gates[key]
    if entering is not None:
        gates, key, gate = entering
        if gate.idle and gates.get(key) is gate:
            del gates[key]


@contextlib.asynccontextmanager
async def slot(app_id: str, actor: str) -> AsyncIterator[None]:
    """Hold one of the caller's places, one of the app's and one of the
    platform's while a platform method runs. A call that gets them not
    within ``PLACE_WAIT_S`` holds none and raises ``TooFast``."""
    global _waiting
    held: list[_Held] = []
    entering: _Held | None = None
    _waiting += 1
    try:
        async with asyncio.timeout(PLACE_WAIT_S):
            for gates, key, limit in ((_caller_gates, f"{app_id}|{actor}", IN_FLIGHT_PER_CALLER),
                                      (_app_gates, app_id, IN_FLIGHT_PER_APP)):
                gate = gates.get(key)
                if gate is None:
                    gate = gates[key] = Gate(limit)
                entering = (gates, key, gate)
                await gate.enter()
                held.append(entering)
                entering = None
            await _platform_gate.enter()
    except TimeoutError:
        _release(held, entering)
        raise TooFast(_later(_LINE, time.monotonic(), LINE_WAIT_S, LINE_STEP_S)) from None
    except BaseException:
        _release(held, entering)
        raise
    finally:
        _waiting -= 1
    try:
        yield
    finally:
        _platform_gate.leave()
        _release(held)


def reset() -> None:
    """Forget every bucket and every place (the tests' clean start)."""
    global _waiting, _swept_at, _swept_size
    _buckets.clear()
    _promised.clear()
    _caller_gates.clear()
    _app_gates.clear()
    _platform_gate.busy = 0
    _platform_gate.waiters.clear()
    _waiting = 0
    _swept_at, _swept_size = 0.0, 0
