"""Background tasks the phone daemon starts and does not await.

The event loop keeps only a WEAK reference to a task: one whose result is
never stored can be garbage-collected mid-flight, and an exception it dies
with surfaces only as "Task exception was never retrieved" at collection
time, if ever. ``spawn()`` is the one way to start such a task: it keeps the
task in a registry until it is done (then drops it) and hands the exception
a task died with to the loop's exception handler at once, under the task's
``owner:name``.

The proxy's ``core/background.py`` is the same helper, with two differences:
the death goes to the loop's handler (the daemon's own, where it set one,
as a task's death reached it before) rather than a logger, and a
``SystemExit`` or ``KeyboardInterrupt`` ends a task as an exit, not a death.
The daemon needs no shutdown cancel of its own: ``asyncio.run`` ends what is
still running when it stops.

A task the caller keeps (assigned, awaited, cancelled later) does not need
this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str, owner: str) -> asyncio.Task[Any]:
    """Start ``coro`` on the running loop as a tracked task named
    ``"<owner>:<name>"``. Called off the loop's thread it raises, as
    ``asyncio.create_task`` does."""
    task = asyncio.get_running_loop().create_task(coro, name=f"{owner}:{name}")
    _tasks.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None or isinstance(exc, (SystemExit, KeyboardInterrupt)):
        return
    task.get_loop().call_exception_handler({
        "message": f"background task {task.get_name()} died",
        "exception": exc,
        "task": task,
    })
