"""Background tasks the proxy starts and does not await.

The event loop keeps only a WEAK reference to a task: one whose result is
never stored can be garbage-collected mid-flight, and an exception it dies
with surfaces only as "Task exception was never retrieved" at collection
time, if ever. ``spawn()`` is the one way to start such a task: it keeps the
task in a registry until it is done (then drops it), logs the exception a
task died with under its owner and name, and ``cancel_all()`` ends what is
still running at shutdown (``startup._shutdown_cleanup``, after the sessions
closed themselves and before the database pool closes): a grace period for
the tasks to finish their work, then a cancel.

A spawned task that awaits a task it does not own (a chat's pump) waits on it
through ``asyncio.wait``: cancelling the waiter must not cancel what it waits
for.

A task the caller keeps (assigned, awaited, cancelled later) does not need
this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("otodock.background")

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str, owner: str) -> asyncio.Task[Any]:
    """Start ``coro`` on the running loop as a tracked task named
    ``"<owner>:<name>"``. Called off the loop's thread it raises, as
    ``asyncio.create_task`` does: the registry is the loop's own."""
    task = asyncio.get_running_loop().create_task(coro, name=f"{owner}:{name}")
    _tasks.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task %s died: %r", task.get_name(), exc, exc_info=exc)


def running(owner: str | None = None) -> list[asyncio.Task[Any]]:
    """The tracked tasks not yet done (of one owner, or all)."""
    prefix = f"{owner}:" if owner is not None else ""
    return [t for t in _tasks if not t.done() and t.get_name().startswith(prefix)]


async def cancel_all(grace: float = 2.0, timeout: float = 1.0) -> int:
    """Give the tracked tasks of the current loop ``grace`` seconds to finish,
    cancel the rest and wait up to ``timeout`` for their teardown. Returns how
    many were cancelled."""
    loop = asyncio.get_running_loop()
    pending = [t for t in _tasks if not t.done() and t.get_loop() is loop]
    if pending and grace > 0:
        with contextlib.suppress(Exception):
            await asyncio.wait(pending, timeout=grace)
    left = [t for t in pending if not t.done()]
    for task in left:
        task.cancel()
    if left:
        with contextlib.suppress(Exception):
            await asyncio.wait(left, timeout=timeout)
    return len(left)
