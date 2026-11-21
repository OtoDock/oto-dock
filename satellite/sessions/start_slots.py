"""How many sessions start at once on this machine.

A start is a burst: the engine's interpreter, its tool servers, a sandbox's
mounts. A wave of them (every chat warm again after the platform restarted)
would take the cores this machine's own satellite needs to keep its link up.
So every start of a session's process takes a slot (``slot``): a headless
engine, a Codex daemon, a terminal's child. A slot is held through the start
and a moment after it, the child's own startup burst.

The bound is the platform's number when its admin set one
(``SESSION_SPAWN_CONCURRENCY``, sent at the sign-in as
``auth_result.session_starts`` to the install's compute machines only), else
half this machine's CPUs, at least 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

logger = logging.getLogger("satellite")

#: How long a slot is held after its start returned.
_SETTLE_S = 1.0

_wanted = 0
_slots: dict[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = {}


def _cpus() -> int:
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 4


def bound() -> int:
    """The starts allowed at once now."""
    return _wanted or max(2, _cpus() // 2)


def set_bound(wanted: object) -> None:
    """Take the platform's number (a whole number above 0), or go back to
    this machine's own for anything else. Starts already waiting keep the
    bound they queued under."""
    global _wanted
    _wanted = wanted if isinstance(wanted, int) and not isinstance(wanted, bool) and wanted > 0 else 0


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    held = _slots.get(loop)
    if held is None or held[0] != bound():
        for stale in [lp for lp in _slots if lp.is_closed()]:
            _slots.pop(stale, None)
        held = _slots[loop] = (bound(), asyncio.Semaphore(bound()))
    return held[1]


@contextlib.asynccontextmanager
async def slot():
    """One start of a session's process. A start that raised frees its slot
    at once, and a waiter that is cancelled takes none."""
    sem = _semaphore()
    if sem.locked():
        loop = asyncio.get_running_loop()
        queued_at = loop.time()
        await sem.acquire()
        logger.info("a session start waited %.1fs for a slot (%d at once)", loop.time() - queued_at, bound())
    else:
        await sem.acquire()
    try:
        yield
    except BaseException:
        sem.release()
        raise
    asyncio.get_running_loop().call_later(_SETTLE_S, sem.release)
