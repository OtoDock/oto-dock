"""The run's end owns the one-time task's row, and a failure after the
verdict is a cleanup failure (core-seams phase 8, interlude part 2).

A one-time task's ``dynamic_tasks`` row used to be deleted in the run
body's ``finally`` under ``attempt >= max_attempts``: a run cancelled or
failed before admission never reached it (the row stayed, ``fired`` and
inert, listed until someone deleted it), and a task with retries
configured kept its row after a completed or cancelled run (no retry
follows either). The frame ``_run_task`` now retires the row whenever the
run ended without a retry successor (the dedup reservation says so).

After the success path's verdict (``_end_run``), a raise in the delegate
delivery or the session close used to re-deliver a synthesized "failed",
page "Task Failed" and, with retries configured, run the completed task
again. The handler now re-delivers what the success path was delivering,
pages nobody and retries nothing; the completion page goes out before the
delivery so a raise there cannot lose it.

The harness is ``test_run_stream_terminal``'s: the real ``_run_task`` on
``temp_db`` with a fake layer and the builders patched. ``max_attempts``
above one is a config only a test can build (``retry`` is not a column).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from services.scheduler import runner, scheduler, shared
from storage import database as task_store
from storage.automation import db_tasks, run_status
from tests.tasks.test_run_stream_terminal import _Layer, _drive, _settle

pytestmark = pytest.mark.asyncio


def _one_time(task_id: str, *, notification_mode: str = "none",
              max_attempts: int = 1) -> shared.TaskDefinition:
    db_tasks.create_dynamic_task(task_id, "agent-x", "count", "count", "cli", "one_time",
                                 None, None, 0, 600, None, scope="agent",
                                 notification_mode=notification_mode)
    shared._dynamic_task_ids.add(task_id)
    return shared.TaskDefinition(
        id=task_id, name="count", agent="agent-x", prompt="count", scope="agent",
        notification_mode=notification_mode, delay_seconds=0,
        retry=shared.RetryPolicy(max_attempts=max_attempts, delay_seconds=0),
    )


def _row_gone(task_id: str) -> bool:
    return (task_store.get_dynamic_task(task_id) is None
            and task_id not in shared._dynamic_task_ids)


@contextlib.asynccontextmanager
async def _parked_slot(session_id, target="", execution_path=None, ring_key=""):
    await asyncio.sleep(3600)
    yield


@pytest.fixture
def notifications(monkeypatch):
    """Every page the runner fires, by title."""
    from services.notifications import notification_manager
    titles: list[str] = []

    async def _record(**kw):
        titles.append(kw.get("title", ""))

    monkeypatch.setattr(notification_manager, "fire_notification", _record)
    return titles


@pytest.fixture
def retries(monkeypatch):
    """The retry hand-offs the handler makes (``_execute_task(attempt + 1)``),
    each reserving the task under a successor's run id as the real one does."""
    calls: list[dict] = []

    async def _record(task, **kw):
        calls.append({"task_id": task.id, **kw})
        shared._active_task_ids[task.id] = f"run-retry-{len(calls)}"
        return f"run-retry-{len(calls)}"

    monkeypatch.setattr(runner, "_execute_task", _record)
    yield calls
    for c in calls:
        shared._active_task_ids.pop(c["task_id"], None)


async def _tick():
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# The one-time task's row
# ---------------------------------------------------------------------------

async def test_a_one_time_task_cancelled_while_parked_loses_its_row(temp_db, monkeypatch):
    from core import concurrency
    run_id, task_id = "run-row1", "t-row1"
    task = _one_time(task_id)
    t, _q = _drive(monkeypatch, _Layer(hold=False), task, run_id, "s-row1")
    monkeypatch.setattr(concurrency, "task_slot", _parked_slot)  # before the body's first step
    await asyncio.sleep(0.1)
    assert await scheduler.cancel_run(run_id)
    with pytest.raises(asyncio.CancelledError):
        await t
    assert temp_db.get_run(run_id)["status"] == run_status.CANCELLED
    assert _row_gone(task_id)
    assert shared._active_task_ids.get(task_id) is None


async def test_a_one_time_task_with_retries_cancelled_mid_flight_loses_its_row(temp_db, monkeypatch):
    run_id, task_id = "run-row2", "t-row2"
    task = _one_time(task_id, max_attempts=3)
    layer = _Layer(hold=True)
    t, q = _drive(monkeypatch, layer, task, run_id, "22222222-2222-4222-8222-22222222row2")
    try:
        await asyncio.wait_for(q.first_text.wait(), 10)
        assert await scheduler.cancel_run(run_id)
        await asyncio.wait_for(t, 10)
        await _settle(f"task-{run_id}")
    finally:
        shared._run_subscribers.pop(run_id, None)
    assert temp_db.get_run(run_id)["status"] == run_status.CANCELLED
    assert _row_gone(task_id)


async def test_a_one_time_task_with_retries_left_to_complete_loses_its_row(temp_db, monkeypatch):
    run_id, task_id = "run-row3", "t-row3"
    task = _one_time(task_id, max_attempts=3)
    t, _q = _drive(monkeypatch, _Layer(hold=False), task, run_id, "33333333-3333-4333-8333-33333333row3")
    try:
        await asyncio.wait_for(t, 20)
        await _settle(f"task-{run_id}")
    finally:
        shared._run_subscribers.pop(run_id, None)
    assert temp_db.get_run(run_id)["status"] == run_status.COMPLETED
    assert _row_gone(task_id)


# ---------------------------------------------------------------------------
# A failure after the verdict
# ---------------------------------------------------------------------------

async def test_a_failure_after_the_verdict_neither_pages_nor_retries(temp_db, monkeypatch, notifications, retries):
    from services.scheduler import interactive
    run_id, task_id = "run-row4", "t-row4"
    task = _one_time(task_id, notification_mode="auto", max_attempts=3)
    closes: list[str] = []

    async def _close_once(session_id):
        closes.append(session_id)
        if len(closes) == 1:
            raise RuntimeError("the PTY close blew up after the verdict")

    monkeypatch.setattr(interactive, "_close_interactive_task_session", _close_once)
    t, q = _drive(monkeypatch, _Layer(hold=False), task, run_id, "44444444-4444-4444-8444-44444444row4")
    try:
        await asyncio.wait_for(t, 20)
        await _settle(f"task-{run_id}")
        await _tick()
    finally:
        shared._run_subscribers.pop(run_id, None)
    assert len(closes) == 2  # the success path's close raised; the handler's went through
    row = temp_db.get_run(run_id)
    assert row["status"] == run_status.COMPLETED and row["error_message"] is None
    assert notifications == ["Task Complete: count"]
    assert retries == []
    assert q.dones() == [({"type": "done", "status": run_status.COMPLETED}, run_status.COMPLETED)]
    assert _row_gone(task_id)


async def test_a_failure_before_the_verdict_still_pages_retries_and_keeps_the_row(temp_db, monkeypatch, notifications, retries):
    run_id, task_id = "run-row5", "t-row5"
    task = _one_time(task_id, notification_mode="auto", max_attempts=2)

    class _DeadOnSpawn(_Layer):
        async def start_session(self, session_id, agent_cfg):
            raise RuntimeError("spawn failed")

    t, q = _drive(monkeypatch, _DeadOnSpawn(hold=False), task, run_id, "55555555-5555-4555-8555-55555555row5")
    try:
        await asyncio.wait_for(t, 20)
        await _tick()
    finally:
        shared._run_subscribers.pop(run_id, None)
    row = temp_db.get_run(run_id)
    assert row["status"] == run_status.FAILED and row["error_message"] == "spawn failed"
    assert notifications == ["Task Failed: count"]
    assert [c["attempt"] for c in retries] == [2]
    assert q.dones() == [({"type": "done", "status": run_status.FAILED}, run_status.FAILED)]
    # The retry owns the row from here: kept, and the reservation is its.
    assert task_store.get_dynamic_task(task_id) is not None
    assert shared._active_task_ids.get(task_id) == "run-retry-1"
    db_tasks.delete_dynamic_task(task_id)
    shared._dynamic_task_ids.discard(task_id)


async def test_a_delivery_that_raises_after_the_verdict_is_retried_with_the_same_word(temp_db, monkeypatch, notifications, retries):
    from services.scheduler import delivery
    run_id, task_id = "run-row6", "t-row6"
    task = _one_time(task_id, notification_mode="auto", max_attempts=3)
    deliveries: list[tuple[str, str]] = []

    async def _raise_once(task_def, final_status, final_output, **kw):
        deliveries.append((final_status, final_output))
        if len(deliveries) == 1:
            raise RuntimeError("the delivery's read failed")

    monkeypatch.setattr(delivery, "_deliver_task_result", _raise_once)
    t, _q = _drive(monkeypatch, _Layer(hold=False), task, run_id, "66666666-6666-4666-8666-66666666row6")
    try:
        await asyncio.wait_for(t, 20)
        await _settle(f"task-{run_id}")
        await _tick()
    finally:
        shared._run_subscribers.pop(run_id, None)
    assert temp_db.get_run(run_id)["status"] == run_status.COMPLETED
    assert deliveries == [(run_status.COMPLETED, "1\n2\n"), (run_status.COMPLETED, "1\n2\n")]
    assert notifications == ["Task Complete: count"]  # sent before the delivery, once
    assert retries == []
    assert _row_gone(task_id)
