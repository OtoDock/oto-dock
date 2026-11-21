"""The platform's planned stop: wait for open work, hold what is new.

A drain is asked for before the server is stopped (an update, a deploy, the
unit's own stop). From then on every door that would start a new turn reads
``holding()`` and holds it instead (a message waits in its chat's queue, a
task's fire stays a pending run, a wake is stored), while the work already
open gets a bounded time to finish. When nothing is working any more, or the
bound has run out, the platform is ``held``: whatever is still open is cut by
the stop, recorded, and continued after it
(``services/scheduler/continue_pass.py``).

Three states. ``running``: nothing is held. ``draining``: new work is held,
open work is waited for. ``held``: new work is held until the process stops,
until ``resume``, or until ``HELD_TTL_S`` passes with no stop and no new
ask for the drain (a stop that never came must not hold the platform for
ever, and a backup whose copy is long keeps its hold by asking again).

``stopping`` is the process itself going down (its stop signal, or the
lifespan's own stop): it holds new work too, and nothing takes it back.

``starting`` is a boot that builds the bundled tools' environments after the
server listens (``startup._boot_mcps``): new work is held until the boot's
continue ends it (``end_start_hold``), apart from the states above, so a drain
asked meanwhile runs as ever and the start's end never resumes it.

This module imports nothing above ``core``. Whoever owns a kind of work
registers how to count it (``register_counter``), and whoever must act at a
change registers a hook (``on_held``, ``on_resume``, ``on_change``).

``work()`` is the one busy-or-idle answer: the drain's own wait, the admin
command's ``status``, the admin health, ``/metrics`` and a controller that
stops an idle install all read it, never a count of their own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from core import background

logger = logging.getLogger("otodock")

RUNNING = "running"
DRAINING = "draining"
HELD = "held"

#: How long open work is waited for when the caller names no bound.
DEFAULT_BOUND_S = 600.0
#: How long the platform expects to be away, told to the machines.
DEFAULT_AWAY_S = 900
#: A ``held`` platform nobody stopped takes new work again this long after
#: the last ask for the drain: a caller still at work asks again (a backup,
#: every five minutes of its copy), and one that was killed asks no more.
HELD_TTL_S = 900.0
#: Nothing is working when two reads this far apart both find nothing: a
#: turn that ends can start a delegation, a meeting or a review.
QUIET_S = 5.0
#: Background work whose count has not moved for this long is no longer
#: waited for (a server an agent left running never ends).
BACKGROUND_STILL_S = 60.0
_POLL_S = 1.0

#: The reason a terminal is closed for when the platform itself stops: its
#: turn's open-turn row stays for the boot.
PLATFORM_STOPPING = "platform-stopping"

#: The kinds of open work, in the order a person reads them.
KINDS = ("turns", "runs", "calls", "duplex", "meetings", "terminals", "moves", "background")
BACKGROUND = "background"

Hook = Callable[[], "Awaitable[None] | None"]

_state = RUNNING
_stopping = False
_starting = False
_drained = False
_deadline: float | None = None
#: When a held platform takes new work again by itself.
_held_until = 0.0
_away_s = DEFAULT_AWAY_S
_task: asyncio.Task | None = None
_counters: dict[str, Callable[[], int]] = {}
_waiting: Callable[[], int] | None = None
_queued: Callable[[], int] | None = None
_sessions: Callable[[], int] | None = None
_compute: Callable[[], dict] | None = None
_hooks: dict[str, list[Hook]] = {
    "settle": [], "held": [], "resume": [], "change": [], "start_ended": []}
_clock: Callable[[], float] = time.monotonic
# The last moment anything worked by the drain's own rule or waited in a
# queue (``idle_s``), and the background count's own clock (``_Waited``).
_last_busy: float | None = None
_idle_waited: _Waited | None = None


def note_stopping() -> None:
    """The process is going down: nothing new starts from here."""
    global _stopping
    _stopping = True


def note_running() -> None:
    """The process starts: one process is one life, except under the tests,
    where an app starts again after its own shutdown."""
    global _stopping
    _stopping = False


def stopping() -> bool:
    return _stopping


def hold_for_start() -> None:
    """The boot waits for a compute machine to sign in: new work is held
    until ``end_start_hold``."""
    global _starting
    _starting = True


def starting() -> bool:
    return _starting


#: What a door that refuses new work says while only the start holds it: the
#: platform is not updating, it waits for its compute machine to sign in.
STARTING_LINE = "OtoDock is starting. Try again in a minute."


def refusal(updating: str) -> str:
    """The line a door says for work it refuses under a hold: its own
    ``updating`` words for a drain or the stop, ``STARTING_LINE`` while the
    start alone holds new work."""
    return STARTING_LINE if _starting and not _stopping and _state == RUNNING else updating


async def end_start_hold() -> None:
    """The start's wait for a compute machine ended (or its bound ran out):
    new work goes out again, unless a drain or the stop holds it."""
    global _starting
    if not _starting:
        return
    _starting = False
    logger.info("drain: the start's hold ended")
    await _run_hooks("start_ended")


def drop_start_hold() -> None:
    """The start's hold ends with the process going down: no hook runs."""
    global _starting
    _starting = False


def state() -> str:
    return _state


def holding() -> bool:
    """New work is held: a drain is on, the boot still builds the tools, or
    the process is stopping."""
    return _stopping or _starting or _state != RUNNING


def away_s() -> int:
    """How long the platform expects to be away, as the drain was told."""
    return _away_s


def register_counter(kind: str, count: Callable[[], int]) -> None:
    """How many of ``kind`` (one of ``KINDS``) are working right now. Work
    that waits on a person is not working: it goes to ``register_waiting``."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind of open work: {kind!r}")
    _counters[kind] = count


def register_waiting(count: Callable[[], int]) -> None:
    """How many turns wait on a person (a card, a plan review)."""
    global _waiting
    _waiting = count


def register_queued(count: Callable[[], int]) -> None:
    """How many messages wait in the chats' queues."""
    global _queued
    _queued = count


def register_sessions(count: Callable[[], int]) -> None:
    """How many sessions are live on this server (a live session with no turn
    open is not work)."""
    global _sessions
    _sessions = count


def register_compute(read: Callable[[], dict]) -> None:
    """What the compute machines hold now (``work()["compute"]``): there is
    one drain, and its wait counts their open turns among the others."""
    global _compute
    _compute = read


def _read_compute() -> dict:
    if _compute is None:
        return {}
    try:
        return dict(_compute())
    except Exception:  # noqa: BLE001  # a broken reader must not fail a status read
        logger.warning("drain: the compute read failed", exc_info=True)
        return {}


def on_settle(hook: Hook) -> None:
    """Run as the wait ends, while the state still answers ``draining``:
    whoever follows the drain stops the server at ``held``, so a wait the
    stop must honour goes here and not in ``on_held``. It must bound itself."""
    _hooks["settle"].append(hook)


def on_held(hook: Hook) -> None:
    """Run when a drain reaches ``held``."""
    _hooks["held"].append(hook)


def on_resume(hook: Hook) -> None:
    """Run when a drain is called off, or its hold runs out."""
    _hooks["resume"].append(hook)


def on_change(hook: Hook) -> None:
    """Run at every change of state."""
    _hooks["change"].append(hook)


def on_start_ended(hook: Hook) -> None:
    """Run when the start's hold ends."""
    _hooks["start_ended"].append(hook)


def _read(fn: Callable[[], int] | None) -> int:
    if fn is None:
        return 0
    try:
        return max(0, int(fn()))
    except Exception:  # noqa: BLE001  # one owner's broken counter must not fail the drain or a status read
        logger.warning("drain: a counter failed", exc_info=True)
        return 0


def open_work() -> dict[str, int]:
    """What is working right now, per kind."""
    return {kind: _read(_counters.get(kind)) for kind in KINDS}


def note_activity() -> int:
    """Read the counters once and stamp the last busy moment by the drain's
    own rule (background work still for ``BACKGROUND_STILL_S`` is not
    waited for, so it keeps nothing busy), a message waiting in a queue or a
    turn waiting for a person (an approval card, a question). Returns
    ``idle_s``: 0 while something works or waits, or the boot starts. The
    drain itself does not wait for a person; an idle stop must not stop an
    install someone is about to answer, and reads ``waiting_on_people`` to
    stop past a forgotten card. A sampler calls it every few seconds, so a
    short turn is never missed."""
    global _last_busy, _idle_waited
    now = _clock()
    if _idle_waited is None:
        _idle_waited = _Waited()
    working = _idle_waited.working(open_work())
    if _last_busy is None or working or _read(_queued) or _read(_waiting) or _starting:
        _last_busy = now
    return max(0, round(now - _last_busy))


def work() -> dict:
    """The state and the counts, as the two routes, the admin health and
    ``/metrics`` answer them."""
    counts = open_work()
    left = 0
    if _state == DRAINING and _deadline is not None:
        left = max(0, round(_deadline - _clock()))
    return {
        "state": _state,
        "drained": _state == HELD and _drained,
        "busy": any(counts.values()),
        "open": counts,
        "waiting_on_people": _read(_waiting),
        "queued": _read(_queued),
        "bound_left_s": left,
        "holding": holding(),
        "starting": _starting,
        "sessions": _read(_sessions),
        "idle_s": note_activity(),
        "compute": _read_compute(),
    }


async def _run_hooks(name: str) -> None:
    for hook in list(_hooks[name]):
        try:
            result = hook()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except Exception:  # one owner's hook must not stop the others or the drain
            logger.exception("drain: a %s hook failed", name)


async def _set_state(new: str) -> None:
    global _state
    if new == _state:
        return
    logger.info("drain: %s -> %s", _state, new)
    _state = new
    await _run_hooks("change")


class _Waited:
    """What the drain still waits for: every kind while any of it works,
    background work only while its count moves."""

    def __init__(self) -> None:
        self._background = -1
        self._moved_at = _clock()

    def working(self, counts: dict[str, int]) -> int:
        background = counts.get(BACKGROUND, 0)
        now = _clock()
        if background != self._background:
            self._background, self._moved_at = background, now
        still = now - self._moved_at >= BACKGROUND_STILL_S
        return sum(n for kind, n in counts.items() if kind != BACKGROUND) + (
            0 if still else background)


async def _drain(bound_s: float) -> None:
    """Wait for open work up to the bound, hold, then wait out the hold."""
    global _drained, _deadline, _held_until
    try:
        waited = _Waited()
        quiet_since: float | None = None
        _drained = False
        while True:
            now = _clock()
            if waited.working(open_work()) == 0:
                quiet_since = now if quiet_since is None else quiet_since
                if bound_s <= 0 or now - quiet_since >= QUIET_S:
                    _drained = True
                    break
            else:
                quiet_since = None
            if _deadline is None or now >= _deadline:
                break
            await asyncio.sleep(_POLL_S)
        _deadline = None
        if not _drained:
            logger.warning("drain: the wait ran out with work still open: %s", open_work())
        await _run_hooks("settle")
        await _set_state(HELD)
        await _run_hooks("held")
        _held_until = _clock() + HELD_TTL_S
        while True:
            # Read again each round: a new ask for the drain moves it on.
            left = _held_until - _clock()
            if left <= 0:
                break
            await asyncio.sleep(min(left, _POLL_S))
        logger.warning("drain: held for %.0f s with no stop and no new ask, taking new work again", HELD_TTL_S)
        await _resume()
    except asyncio.CancelledError:
        raise
    except Exception:  # a broken drain must never leave the platform holding for ever
        logger.exception("drain: failed, taking new work again")
        await _resume()


async def _resume() -> None:
    global _deadline
    _deadline = None
    if _state == RUNNING:
        return
    await _set_state(RUNNING)
    await _run_hooks("resume")


async def drain(bound_s: float | None = None, away: int | None = None) -> dict:
    """Start a drain, or join the one that is on. Returns ``work()``."""
    global _task, _deadline, _away_s, _held_until
    if _state == RUNNING and not _stopping:
        bound = DEFAULT_BOUND_S if bound_s is None else max(0.0, float(bound_s))
        _away_s = DEFAULT_AWAY_S if away is None else max(0, min(int(away), 3600))
        _deadline = _clock() + bound
        await _set_state(DRAINING)
        _task = background.spawn(_drain(bound), name="platform-drain", owner="drain")
    elif _state == HELD:
        # Asked again while held: the caller is still at work (a backup's
        # copy). The hold counts from now.
        _held_until = _clock() + HELD_TTL_S
    return work()


async def resume() -> dict:
    """Call a drain off: new work starts again and what was held goes out."""
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task})
    await _resume()
    return work()


def reset_for_tests() -> None:
    """Back to a process that never drained (tests only)."""
    global _state, _stopping, _starting, _drained, _deadline, _task, _away_s, _waiting, _queued
    global _clock, _sessions, _last_busy, _idle_waited, _compute, _held_until
    if _task is not None and not _task.done():
        _task.cancel()
    _state, _stopping, _starting, _drained, _deadline, _task = RUNNING, False, False, False, None, None
    _held_until = 0.0
    _away_s, _waiting, _queued, _clock = DEFAULT_AWAY_S, None, None, time.monotonic
    _sessions, _last_busy, _idle_waited, _compute = None, None, None, None
    _counters.clear()
    for hooks in _hooks.values():
        hooks.clear()
