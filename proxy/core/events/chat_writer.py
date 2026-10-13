"""Ordered per-chat off-loop writer for chat rows.

The pump's turn-end persistence and the dashboard handlers' per-click writes
used to run their store calls ON the event loop, relying on loop atomicity
between ``await``s for their check-then-write sequences (there is no
per-chat lock anywhere). Moving them to the DB executor naively would break
two things: ORDER (a user row landing after the assistant blocks it
preceded; the next pump's cutoff read observing a stale table) and the
check-then-write atomicity. This module keeps both: every chat has ONE
FIFO lane of synchronous jobs, run one at a time on the dedicated DB
executor (``storage.pg.run_db``), never coalesced — chat rows are
append-only and every one must land. A job bundles the reads and writes
that must be atomic against other writers of the same chat (the
read-modify-write of ``total_cost``, the "set the title only when unset"
pattern, a compare-and-set run-status flip) and returns plain values the
loop-side code consumes.

Rules for callers:
  * A job captures PLAIN VALUES snapshotted on the loop (block lists, ids,
    deltas), never live pump/connection state, and touches nothing
    loop-bound (no notification broadcasts, no registries) — those run on
    the loop from the job's result.
  * Anything that must observe this chat's earlier writes (a history read
    after the pump's rows, the next pump's cutoff) is itself a lane job.
  * ``drain(chat_id)`` before publishing state that the DB must back
  (the pump drains before it leaves ``_active_pumps`` and broadcasts
  "ready"); ``drain_all`` at shutdown, before the executor stops.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import Any, Callable

from storage.pg import run_db

logger = logging.getLogger("otodock.chat-writer")


class _Lane:
    __slots__ = ("queue", "task")

    def __init__(self) -> None:
        self.queue: collections.deque[tuple[Callable[[], Any], asyncio.Future, str]] = (
            collections.deque()
        )
        self.task: asyncio.Task | None = None


_lanes: dict[str, _Lane] = {}


def submit(chat_id: str, job: Callable[[], Any], *, label: str = "") -> asyncio.Future:
    """Queue ``job`` (a sync callable) on the chat's lane. Returns a future
    resolved on the loop with the job's return value, or its exception.
    Requires a running loop. A failure is logged here as well, so a caller
    that never awaits the future still leaves a trace."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    lane = _lanes.get(chat_id)
    if lane is None:
        lane = _lanes[chat_id] = _Lane()
    lane.queue.append((job, fut, label or getattr(job, "__name__", "job")))
    if lane.task is None or lane.task.done():
        lane.task = loop.create_task(
            _flush(chat_id, lane), name=f"chat-writer:{chat_id[:8]}",
        )
    return fut


async def _flush(chat_id: str, lane: _Lane) -> None:
    try:
        while lane.queue:
            job, fut, label = lane.queue.popleft()
            try:
                result = await run_db(job)
            except asyncio.CancelledError:
                # Shutdown (executor cancel_futures) or an explicit cancel:
                # nothing queued behind will run — fail it all now so no
                # caller hangs on a future the lane can never resolve.
                if not fut.done():
                    fut.cancel()
                while lane.queue:
                    _, pending_fut, _ = lane.queue.popleft()
                    if not pending_fut.done():
                        pending_fut.cancel()
                raise
            except Exception as e:
                logger.warning(
                    "chat-writer %s: %s failed: %s", chat_id[:8], label, e, exc_info=True,
                )
                if not fut.done():
                    fut.set_exception(e)
                    fut.exception()  # logged above — never "exception was never retrieved"
                continue
            if not fut.done():
                fut.set_result(result)
    finally:
        # No await between the last resolution and here, so no submit can
        # slip a job into a lane we are about to drop (a submit during a
        # running job is picked up by the while loop above).
        if _lanes.get(chat_id) is lane and not lane.queue:
            _lanes.pop(chat_id, None)


def pending(chat_id: str) -> int:
    """Jobs queued or running for the chat."""
    lane = _lanes.get(chat_id)
    if lane is None:
        return 0
    running = 1 if lane.task is not None and not lane.task.done() else 0
    return len(lane.queue) + running


async def drain(chat_id: str, timeout: float | None = None) -> bool:
    """Await every job queued for the chat so far. True when the lane is
    idle, False on timeout (the jobs keep running)."""
    lane = _lanes.get(chat_id)
    if lane is None or lane.task is None or lane.task.done():
        return True
    task = lane.task
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        return False
    except asyncio.CancelledError:
        # The FLUSHER was cancelled (its queued jobs failed): that is not
        # the caller's cancellation — report it as "not landed".
        if task.cancelled():
            return False
        raise
    except Exception:
        pass  # the flusher's own failure is logged there
    return True


async def drain_all(timeout: float | None = None) -> bool:
    """Await every lane (shutdown, tests). True when all landed in time."""
    tasks = [
        lane.task for lane in list(_lanes.values())
        if lane.task is not None and not lane.task.done()
    ]
    if not tasks:
        return True
    done, pending_tasks = await asyncio.wait(
        [asyncio.shield(t) for t in tasks], timeout=timeout,
    )
    return not pending_tasks


def reset_for_tests() -> None:
    """Forget every lane (the tests' temp DB is truncated between tests;
    a leftover lane would otherwise carry a stale task object)."""
    _lanes.clear()
