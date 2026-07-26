"""_admitted_slot — a run cancelled while PARKED on the admission slot must
be stamped terminal, not left 'pending' forever.

_run_task's inner CancelledError handler lives inside the slot body, so a
cancel during slot acquisition escaped it entirely: the row stayed
'pending' (invisible to recovery until restart, and 409-blocking the run
chat's history delete). The wrapper stamps 'cancelled' for a user stop /
'failed' otherwise — and leaves every in-body path (already stamped by the
inner handler, or the shutdown path that deliberately leaves 'running')
untouched via the status guard.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/tasks/test_admitted_slot_cancel.py -q
"""

import asyncio
import contextlib

import pytest

from services.scheduler import scheduler

pytestmark = pytest.mark.asyncio


@contextlib.asynccontextmanager
async def _parked_slot(session_id, target=""):
    await asyncio.sleep(3600)  # never admits — the cancel target
    yield


async def _cancel_parked(run_id: str, monkeypatch, *, user_stop: bool):
    from core import concurrency
    monkeypatch.setattr(concurrency, "task_slot", _parked_slot)
    if user_stop:
        scheduler._user_cancelled_runs.add(run_id)

    async def _body():
        async with scheduler._admitted_slot("sess-x", "local", run_id):
            pass  # never reached

    t = asyncio.get_running_loop().create_task(_body())
    await asyncio.sleep(0.05)  # parked on the slot now
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    scheduler._user_cancelled_runs.discard(run_id)


async def test_user_cancel_while_parked_stamps_cancelled(temp_db, monkeypatch):
    temp_db.create_run("run-park1", "t1", "agent-x", "schedule", None, "p")
    await _cancel_parked("run-park1", monkeypatch, user_stop=True)
    run = temp_db.get_run("run-park1")
    assert run["status"] == "cancelled"
    assert run["error_message"] == "Interrupted by user"


async def test_platform_cancel_while_parked_stamps_failed(temp_db, monkeypatch):
    temp_db.create_run("run-park2", "t1", "agent-x", "schedule", None, "p")
    await _cancel_parked("run-park2", monkeypatch, user_stop=False)
    run = temp_db.get_run("run-park2")
    assert run["status"] == "failed"
    assert run["error_message"] == "Cancelled while queued"


async def test_in_body_cancel_keeps_inner_stamp(temp_db, monkeypatch):
    # A cancel AFTER admission belongs to the inner handler — the wrapper's
    # status guard must not overwrite a row the body already stamped.
    temp_db.create_run("run-park3", "t1", "agent-x", "schedule", None, "p")

    @contextlib.asynccontextmanager
    async def _instant_slot(session_id, target=""):
        yield
    from core import concurrency
    monkeypatch.setattr(concurrency, "task_slot", _instant_slot)

    async def _body():
        async with scheduler._admitted_slot("sess-y", "local", "run-park3"):
            # Simulates _run_task's inner CancelledError handling.
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                temp_db.update_run("run-park3", status="cancelled",
                                   error_message="Interrupted by user")
                raise

    t = asyncio.get_running_loop().create_task(_body())
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    run = temp_db.get_run("run-park3")
    assert run["status"] == "cancelled"
    assert run["error_message"] == "Interrupted by user"
