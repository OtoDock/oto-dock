"""Waiting on a task run the platform started for itself (a check's judge,
a call's wrap-up): its terminal status through ``db_tasks.on_run_finished``,
a future the writer's thread completes, with a wall clock and a cancel the
caller decides.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from storage.automation import run_status

if TYPE_CHECKING:
    from collections.abc import Callable

RUN_POLL_S = 2.0

# The wait's own two outcomes beside the run's terminal statuses: the
# wall clock lapsed, or the caller cancelled. Neither is a run status (the
# platform cancel that follows stamps the row ``failed``).
WAIT_TIMED_OUT = "timeout"
WAIT_CANCELLED = "cancelled"

_run_futures: dict[str, asyncio.Future] = {}
_hooked = False


def install_hook() -> None:
    """Hand a finished run's status to its waiter (once per process)."""
    global _hooked
    if _hooked:
        return
    from storage.automation import db_tasks
    db_tasks.on_run_finished(_on_run_finished)
    _hooked = True


def _on_run_finished(run_id: str, status: str) -> None:
    fut = _run_futures.get(run_id)
    if fut is None:
        return
    loop = fut.get_loop()

    def _set() -> None:
        if not fut.done():
            fut.set_result(status)
    loop.call_soon_threadsafe(_set)


async def wait_run(run_id: str, *, timeout: float, cancelled: Callable[[], bool],
                   what: str) -> str:
    """The run's terminal status, with the row re-read in case it ended
    before the future existed; ``cancelled()`` polled every two seconds —
    the run is cancelled and ``WAIT_CANCELLED`` returned when it says so.
    ``what`` names the run in the cancel's reason ("the check's judge")."""
    from services.scheduler import scheduler
    from storage import database as task_store
    install_hook()
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _run_futures[run_id] = fut
    try:
        row = await asyncio.to_thread(task_store.get_run, run_id)
        if row and run_status.is_terminal(row.get("status")):
            return row["status"]
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                scheduler.platform_cancel_run(run_id, f"{what} timed out")
                return WAIT_TIMED_OUT
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=min(RUN_POLL_S, left))
            except asyncio.TimeoutError:
                if cancelled():
                    scheduler.platform_cancel_run(run_id, "the person sent a new message")
                    return WAIT_CANCELLED
    except asyncio.CancelledError:
        # The waiter was cut (a judge's budget, a turn): the run stops with
        # it instead of spending on for an answer nobody reads.
        scheduler.platform_cancel_run(run_id, f"{what} was stopped")
        raise
    finally:
        _run_futures.pop(run_id, None)
