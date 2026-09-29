"""A run cancelled while PARKED on the admission slot must end: stamped
terminal, its stream told, its registries released.

``_run_task_body``'s own CancelledError handler lives inside the slot, so a
cancel during slot acquisition escapes it; ``_run_task``'s frame stamps the
still-``pending`` row (``_end_unstamped``: 'cancelled' for a user stop,
'failed' with the platform's reason otherwise), sends the stream's ``done``
and releases the task's dedup reservation — which a parked cancel used to
leak until a restart, so a recurring task cancelled while queued never
fired again. A row the body already stamped is left alone.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/tasks/test_admitted_slot_cancel.py -q
"""

import asyncio
import contextlib

import pytest

from services.scheduler import runner, scheduler, shared
from storage import database as task_store
from storage.automation import run_status

pytestmark = pytest.mark.asyncio


@contextlib.asynccontextmanager
async def _parked_slot(session_id, target="", execution_path=None):
    await asyncio.sleep(3600)  # never admits — the cancel target
    yield


class _Subscriber(asyncio.Queue):
    def __init__(self, run_id):
        super().__init__()
        self.run_id = run_id
        self.seen = []

    async def put(self, event):
        self.seen.append((event, task_store.get_run(self.run_id)["status"]))
        await super().put(event)


def _task(task_id: str) -> shared.TaskDefinition:
    return shared.TaskDefinition(id=task_id, name="q", agent="agent-x", prompt="p",
                                 scope="agent", notification_mode="none")


async def _cancel_parked(run_id: str, task_id: str, monkeypatch, *, user_stop: bool,
                         reason: str | None = None) -> _Subscriber:
    from core import concurrency
    from core.config import task_config_builder
    from storage import remote_store
    monkeypatch.setattr(concurrency, "task_slot", _parked_slot)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
    monkeypatch.setattr(remote_store, "resolve_execution_target", lambda *a, **k: ("local", None))
    task = _task(task_id)
    task_store.create_run(run_id, task_id, task.agent, "schedule", None, task.prompt,
                          "scheduled", task.scope, None)
    shared._active_task_ids[task_id] = run_id
    q = _Subscriber(run_id)
    shared._run_subscribers.setdefault(run_id, []).append(q)
    t = asyncio.get_running_loop().create_task(
        runner._run_task(run_id, f"sess-{run_id}", task, task.prompt, "scheduled", None, 1))
    shared._running_tasks[run_id] = t
    t.add_done_callback(lambda _: shared._running_tasks.pop(run_id, None))
    await asyncio.sleep(0.05)  # parked on the slot now
    if user_stop:
        assert await scheduler.cancel_run(run_id)
    elif reason:
        assert scheduler.platform_cancel_run(run_id, reason)
    else:
        t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    return q


def _released(run_id: str, task_id: str) -> bool:
    return (shared._active_task_ids.get(task_id) != run_id
            and run_id not in shared._user_cancelled_runs
            and run_id not in shared._platform_interrupts
            and run_id not in shared._run_subscribers
            and run_id not in shared._run_event_buffer)


async def test_user_cancel_while_parked_stamps_cancelled_and_tells_the_stream(temp_db, monkeypatch):
    q = await _cancel_parked("run-park1", "t-park1", monkeypatch, user_stop=True)
    run = temp_db.get_run("run-park1")
    assert run["status"] == run_status.CANCELLED
    assert run["error_message"] == "Interrupted by user"
    assert q.seen == [({"type": "done", "status": run_status.CANCELLED}, run_status.CANCELLED)]
    assert _released("run-park1", "t-park1")


async def test_platform_cancel_while_parked_stamps_failed_with_its_reason(temp_db, monkeypatch):
    q = await _cancel_parked("run-park2", "t-park2", monkeypatch, user_stop=False,
                             reason="the check's judge timed out")
    run = temp_db.get_run("run-park2")
    assert run["status"] == run_status.FAILED
    assert run["error_message"] == "the check's judge timed out"
    assert q.seen == [({"type": "done", "status": run_status.FAILED}, run_status.FAILED)]
    assert _released("run-park2", "t-park2")


async def test_a_bare_cancel_while_parked_says_cancelled_while_queued(temp_db, monkeypatch):
    q = await _cancel_parked("run-park3", "t-park3", monkeypatch, user_stop=False)
    run = temp_db.get_run("run-park3")
    assert run["status"] == run_status.FAILED
    assert run["error_message"] == "Cancelled while queued"
    assert [e["status"] for e, _ in q.seen] == [run_status.FAILED]
    assert _released("run-park3", "t-park3")


async def test_a_row_the_body_stamped_is_left_alone(temp_db, monkeypatch):
    # The frame's end runs after the body's own handler on every in-body
    # cancel; a row past ``pending`` is that handler's verdict (or the
    # shutdown path's deliberately still-running run) and stays.
    temp_db.create_run("run-park4", "t-park4", "agent-x", "schedule", None, "p")
    task_store.update_run("run-park4", status=run_status.CANCELLED, error_message="Interrupted by user")
    shared._run_event_buffer["run-park4"] = []
    q = _Subscriber("run-park4")
    shared._run_subscribers.setdefault("run-park4", []).append(q)
    try:
        await runner._end_unstamped("run-park4")
        task_store.update_run("run-park4", status=run_status.RUNNING)
        await runner._end_unstamped("run-park4")
    finally:
        shared._run_event_buffer.pop("run-park4", None)
        shared._run_subscribers.pop("run-park4", None)
    assert temp_db.get_run("run-park4")["status"] == run_status.RUNNING
    assert q.seen == []


async def test_a_cancel_between_the_config_build_and_the_session_start_gives_the_seat_back(
        temp_db, monkeypatch):
    # The build takes a pool seat that only start_session's bind lets a close
    # release; a user stop landing before the bind must return it, as the
    # failure path does.
    from types import SimpleNamespace

    import config
    from core import concurrency
    from core.config import config_builder, task_config_builder
    from core.session import session_manager, session_state
    from storage import remote_store

    cfg = SimpleNamespace(execution_target="local", execution_path="claude-code-cli",
                          interactive=False, resume=False, extra_env={}, chat_id="",
                          subscription_id="sub-1")
    starting = asyncio.Event()
    released: list = []

    class _Layer:
        closes = 0

        async def start_session(self, session_id, agent_cfg):
            starting.set()
            await asyncio.sleep(3600)

        async def close_session(self, session_id):
            _Layer.closes += 1

    @contextlib.asynccontextmanager
    async def _instant_slot(session_id, target="", execution_path=None):
        yield

    async def _cfg(agent, task_def, sid, **kw):
        return cfg

    monkeypatch.setattr(concurrency, "task_slot", _instant_slot)
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", _cfg)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
    monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **k: _Layer())
    monkeypatch.setattr(remote_store, "resolve_execution_target", lambda *a, **k: ("local", None))
    monkeypatch.setattr(config, "get_cli_model", lambda agent, layer=None: "test-model")
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
    monkeypatch.setattr(config_builder, "release_config_seat",
                        lambda sid, c, **kw: released.append((sid, c, kw)))

    run_id, task = "run-seat1", _task("t-seat1")
    session_id = "33333333-3333-4333-8333-333333333333"
    task_store.create_run(run_id, task.id, task.agent, "manual", None, task.prompt,
                          "one-time", task.scope, None)
    shared._active_task_ids[task.id] = run_id
    t = asyncio.get_running_loop().create_task(
        runner._run_task(run_id, session_id, task, task.prompt, "manual", None, 1))
    shared._running_tasks[run_id] = t
    t.add_done_callback(lambda _: shared._running_tasks.pop(run_id, None))
    await asyncio.wait_for(starting.wait(), 10)
    assert await scheduler.cancel_run(run_id)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(t, 10)

    assert temp_db.get_run(run_id)["status"] == run_status.CANCELLED
    assert _Layer.closes == 1
    assert released == [(session_id, cfg, {})]
    assert _released(run_id, task.id)
