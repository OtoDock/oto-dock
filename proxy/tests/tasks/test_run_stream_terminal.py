"""The run stream's terminal frame (``GET /v1/tasks/runs/{id}/stream``).

Phase 8 of core-seams (the finding's drift): a row that already ended asks
the run vocabulary's ``is_terminal`` — a ``limit_exceeded`` row (a run the
usage check blocked before it ran, which no scheduler task ever publishes
an end for) replays and closes with ``done`` instead of keep-alives forever.

The phase's interlude (the cancel stream): the runner is the one sender of
``done``, after the row is stamped (``runner._end_run``); the producer's
own three ``done`` frames are gone (they were early, unheard after a cancel,
or a word the row did not read); the loop asks the row once after the
subscribe and on every keep-alive tick; a frame broadcast after the run
ended is dropped, not buffered. The ordering pins read the LIVE row at the
moment the frame lands — never a patched ``get_run`` or ``_broadcast``.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.events.common_events import CommonEvent, DONE, TEXT
from services.scheduler import runner, scheduler, shared
from storage import database as task_store
from storage.automation import run_status

pytestmark = pytest.mark.asyncio


async def _stream(monkeypatch, row: dict) -> list[str]:
    from api.tasks import tasks as tasks_api
    from auth.providers import UserContext
    admin = UserContext(sub="api-key", email="api@internal", name="API Key",
                        role="admin", agents=[], is_api_key=True)

    async def _user(_request):
        return admin

    monkeypatch.setattr(tasks_api, "get_current_user", _user)
    monkeypatch.setattr(tasks_api.task_store, "get_run", lambda _rid: dict(row))
    monkeypatch.setattr(tasks_api, "_check_run_access", lambda _run, _user: None)

    async def _never(_run_id):
        raise AssertionError("a terminal row never subscribes to the scheduler")

    monkeypatch.setattr(tasks_api.scheduler, "subscribe_run", _never)
    resp = await tasks_api.stream_run_output(row["id"], SimpleNamespace(), key=None, authorization=None)
    lines: list[str] = []
    async for chunk in resp.body_iterator:
        lines.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return lines


@pytest.mark.parametrize("status", sorted(run_status.TERMINAL))
async def test_every_terminal_row_replays_and_closes(monkeypatch, status):
    row = {"id": "run-t", "status": status, "output_text": "", "agent": "a", "scope": "agent"}
    lines = await _stream(monkeypatch, row)
    assert len(lines) == 1 and '"type": "done"' in lines[0] and f'"status": "{status}"' in lines[0]


async def test_a_limit_blocked_run_no_longer_streams_keepalives_forever(monkeypatch):
    row = {"id": "run-l", "status": run_status.LIMIT_EXCEEDED, "output_text": "",
           "agent": "a", "scope": "agent"}
    lines = await _stream(monkeypatch, row)
    assert lines == ['data: {"type": "done", "status": "limit_exceeded"}\n\n']


# ---------------------------------------------------------------------------
# The runner's end: the stamp, then the frame
# ---------------------------------------------------------------------------

class _SnapQueue(asyncio.Queue):
    """A subscriber that records the LIVE row's status as each frame lands."""

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id
        self.seen: list[tuple[dict, str | None]] = []
        self.first_text = asyncio.Event()

    async def put(self, event):
        row = task_store.get_run(self.run_id)
        self.seen.append((event, row["status"] if row else None))
        if event.get("type") == "text":
            self.first_text.set()
        await super().put(event)

    def dones(self) -> list[tuple[dict, str | None]]:
        return [(e, s) for e, s in self.seen if e.get("type") == "done"]


def _registries_clear(run_id: str, task_id: str) -> bool:
    return (run_id not in shared._run_event_buffer
            and run_id not in shared._run_subscribers
            and run_id not in shared._user_cancelled_runs
            and run_id not in shared._platform_interrupts
            and shared._active_task_ids.get(task_id) != run_id)


async def test_end_run_stamps_then_tells_and_the_word_is_the_rows(temp_db):
    run_id = "run-end1"
    temp_db.create_run(run_id, "t1", "agent-x", "manual", None, "p")
    shared._run_event_buffer[run_id] = []
    q = _SnapQueue(run_id)
    shared._run_subscribers.setdefault(run_id, []).append(q)
    try:
        await runner._end_run(run_id, run_status.CANCELLED,
                              error_message="Interrupted by user", completed_at=shared.now_iso())
        assert q.dones() == [({"type": "done", "status": run_status.CANCELLED}, run_status.CANCELLED)]
        assert temp_db.get_run(run_id)["error_message"] == "Interrupted by user"
        # ``done`` is the last frame: the buffer and the subscribers retired with it.
        assert run_id not in shared._run_event_buffer and run_id not in shared._run_subscribers
        await runner._broadcast(run_id, {"type": "text", "text": "tail"})
        assert q.qsize() == 1
    finally:
        shared._run_event_buffer.pop(run_id, None)
        shared._run_subscribers.pop(run_id, None)


async def test_a_frame_after_the_run_ended_is_dropped_not_buffered(temp_db):
    run_id = "run-late"
    q: asyncio.Queue = asyncio.Queue()
    shared._run_subscribers.setdefault(run_id, []).append(q)
    try:
        assert run_id not in shared._run_event_buffer
        await runner._broadcast(run_id, {"type": "text", "text": "tail"})
        assert run_id not in shared._run_event_buffer
        assert q.empty()
    finally:
        shared._run_subscribers.pop(run_id, None)


# ---------------------------------------------------------------------------
# The loop asks the row
# ---------------------------------------------------------------------------

async def _live_stream(monkeypatch, run_id: str, *, keepalive: float = 0.5,
                       after_subscribe=None) -> list[str]:
    """The endpoint over the real store and the real ``subscribe_run``;
    ``after_subscribe(q)`` runs once the queue is registered (a stamp with
    no broadcast, a queued frame) — the assertions read the row and the
    frames, never a patched lookup."""
    from api.tasks import tasks as tasks_api
    from auth.providers import UserContext
    admin = UserContext(sub="api-key", email="api@internal", name="API Key",
                        role="admin", agents=[], is_api_key=True)

    async def _user(_request):
        return admin

    real_subscribe = scheduler.subscribe_run

    async def _subscribe(rid):
        q = await real_subscribe(rid)
        if after_subscribe is not None:
            after_subscribe(q)
        return q

    monkeypatch.setattr(tasks_api, "get_current_user", _user)
    monkeypatch.setattr(tasks_api, "_check_run_access", lambda _run, _user: None)
    monkeypatch.setattr(tasks_api.run_stream, "KEEPALIVE_S", keepalive)
    monkeypatch.setattr(tasks_api.scheduler, "subscribe_run", _subscribe)
    resp = await tasks_api.stream_run_output(run_id, SimpleNamespace(), key=None, authorization=None)
    lines: list[str] = []
    async for chunk in resp.body_iterator:
        lines.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return lines


def _dones(lines: list[str]) -> list[str]:
    return [ln for ln in lines if '"type": "done"' in ln]


async def test_a_stamp_with_no_broadcast_closes_the_stream_on_the_next_tick(temp_db, monkeypatch):
    run_id = "run-tick"
    temp_db.create_run(run_id, "t1", "agent-x", "manual", None, "p")
    task_store.update_run(run_id, status=run_status.RUNNING)

    async def _stamp_later():
        await asyncio.sleep(0.15)
        task_store.update_run(run_id, status=run_status.CANCELLED)

    stamp = asyncio.ensure_future(_stamp_later())
    try:
        lines = await asyncio.wait_for(_live_stream(monkeypatch, run_id, keepalive=0.05), 5)
    finally:
        await stamp
    assert lines[0] == 'data: {"type": "status", "status": "running"}\n\n'
    assert ": keep-alive\n\n" in lines
    assert _dones(lines) == ['data: {"type": "done", "status": "cancelled"}\n\n']
    assert lines[-1] == _dones(lines)[0]
    assert run_id not in shared._run_subscribers


async def test_a_row_that_ended_between_the_precheck_and_the_subscribe_closes_at_once(temp_db, monkeypatch):
    run_id = "run-gap"
    temp_db.create_run(run_id, "t1", "agent-x", "manual", None, "p")
    task_store.update_run(run_id, status=run_status.RUNNING)
    # The subscribe replays the buffer (the runner appends every frame there
    # while the run lives); the stamp lands in the gap.
    shared._run_event_buffer[run_id] = [{"type": "text", "text": "partial"}]

    def _ended(_q):
        task_store.update_run(run_id, status=run_status.COMPLETED, output_text="partial")

    try:
        lines = await asyncio.wait_for(_live_stream(monkeypatch, run_id, keepalive=5.0, after_subscribe=_ended), 5)
    finally:
        shared._run_event_buffer.pop(run_id, None)
    assert lines == [
        'data: {"type": "status", "status": "running"}\n\n',
        'data: {"type": "text", "text": "partial"}\n\n',
        'data: {"type": "done", "status": "completed"}\n\n',
    ]


async def test_a_queued_done_and_a_terminal_row_yield_exactly_one_done(temp_db, monkeypatch):
    run_id = "run-both"
    temp_db.create_run(run_id, "t1", "agent-x", "manual", None, "p")
    task_store.update_run(run_id, status=run_status.RUNNING)

    def _both(q):
        task_store.update_run(run_id, status=run_status.CANCELLED)
        q.put_nowait({"type": "done", "status": run_status.CANCELLED})

    lines = await asyncio.wait_for(_live_stream(monkeypatch, run_id, keepalive=5.0, after_subscribe=_both), 5)
    assert _dones(lines) == ['data: {"type": "done", "status": "cancelled"}\n\n']


async def test_a_terminal_row_read_with_no_text_yet_replays_the_output(temp_db, monkeypatch):
    run_id = "run-out"
    temp_db.create_run(run_id, "t1", "agent-x", "manual", None, "p")
    task_store.update_run(run_id, status=run_status.RUNNING)

    def _ended(_q):
        task_store.update_run(run_id, status=run_status.COMPLETED, output_text="final answer")

    lines = await asyncio.wait_for(_live_stream(monkeypatch, run_id, keepalive=5.0, after_subscribe=_ended), 5)
    assert lines[1:] == [
        'data: {"type": "text", "text": "final answer"}\n\n',
        'data: {"type": "done", "status": "completed"}\n\n',
    ]


async def test_the_runners_end_reaches_a_live_subscriber_through_the_queue(temp_db, monkeypatch):
    run_id = "run-live"
    temp_db.create_run(run_id, "t1", "agent-x", "manual", None, "p")
    task_store.update_run(run_id, status=run_status.RUNNING)
    shared._run_event_buffer[run_id] = []

    async def _end_later():
        await asyncio.sleep(0.1)
        await runner._end_run(run_id, run_status.COMPLETED, output_text="x", completed_at=shared.now_iso())

    ending = asyncio.ensure_future(_end_later())
    try:
        lines = await asyncio.wait_for(_live_stream(monkeypatch, run_id, keepalive=5.0), 5)
    finally:
        await ending
        shared._run_event_buffer.pop(run_id, None)
    assert lines == [
        'data: {"type": "status", "status": "running"}\n\n',
        'data: {"type": "done", "status": "completed"}\n\n',
    ]


# ---------------------------------------------------------------------------
# The run itself: a cancel mid-flight, a run left to complete
# ---------------------------------------------------------------------------

class _Layer:
    """One text frame and the turn's end; then — when asked to hold — the
    turn stays open until the session is closed (the cancel handler's
    ``close_session``), yields one trailing frame (the closing CLI's tail)
    and returns, the way the CLI reader returns on EOF."""

    def __init__(self, *, hold: bool):
        self.hold = hold
        self._closed = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}
        self.closes = 0

    @asynccontextmanager
    async def session_lock(self, session_id):
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            yield

    async def start_session(self, session_id, agent_cfg):
        pass

    async def close_session(self, session_id):
        self.closes += 1
        self._closed.set()

    async def send_message(self, session_id, prompt, **kw):
        yield CommonEvent(type=TEXT, data={"content": "1\n2\n"})
        yield CommonEvent(type=DONE, data={})
        if self.hold:
            await self._closed.wait()
            yield CommonEvent(type=TEXT, data={"content": "3\n"})

    async def wait_for_bg_subagents(self, session_id, timeout=0.0):
        return 0

    async def drain_bg_commands(self, session_id, *, budget=0.0):
        return False

    async def is_session_alive(self, session_id):
        return not self._closed.is_set()

    def remote_stream_severed(self, session_id):
        return False

    def session_idle_seconds(self, session_id):
        return 0.0

    async def probe_session_process_dead(self, session_id):
        return False

    async def prepare_resume(self, session_id):
        pass

    async def can_resume_session(self, *a, **k):
        return False


@asynccontextmanager
async def _instant_slot(session_id, target="", execution_path=None):
    yield


def _drive(monkeypatch, layer: _Layer, task: shared.TaskDefinition, run_id: str,
           session_id: str) -> tuple[asyncio.Task, _SnapQueue]:
    """``_run_task`` the way ``_execute_task`` launches it: the run row, the
    dedup reservation and the running-tasks registry set; the builders and
    the slot patched; a snapshotting subscriber registered up front."""
    import config
    from core import concurrency
    from core.config import task_config_builder
    from core.session import session_manager, session_state
    from storage import remote_store

    async def _cfg(agent, task_def, sid, **kw):
        return SimpleNamespace(execution_target="local", execution_path="claude-code-cli",
                               interactive=False, resume=False, extra_env={}, chat_id="")

    monkeypatch.setattr(task_config_builder, "build_task_agent_config", _cfg)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
    monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **k: layer)
    monkeypatch.setattr(remote_store, "resolve_execution_target", lambda *a, **k: ("local", None))
    monkeypatch.setattr(concurrency, "task_slot", _instant_slot)
    monkeypatch.setattr(config, "get_cli_model", lambda agent, layer=None: "test-model")
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

    task_store.create_run(run_id, task.id, task.agent, "manual", None, task.prompt,
                          "one-time", task.scope, None)
    shared._active_task_ids[task.id] = run_id
    q = _SnapQueue(run_id)
    shared._run_subscribers.setdefault(run_id, []).append(q)
    t = asyncio.get_running_loop().create_task(
        runner._run_task(run_id, session_id, task, task.prompt, "manual", None, 1))
    shared._running_tasks[run_id] = t
    t.add_done_callback(lambda _: shared._running_tasks.pop(run_id, None))
    return t, q


async def _settle(chat_id: str) -> None:
    """The pump outlives ``_run_task`` on a cancel (the shield keeps it): wait
    for it, then clear what a pump leaves in memory."""
    from core.events import chat_writer, stream_pump
    pump = stream_pump._active_pumps.get(chat_id)
    if pump is not None and pump._task is not None:
        await asyncio.wait_for(asyncio.shield(pump._task), 10)
    await chat_writer.drain(chat_id, timeout=5.0)
    stream_pump._active_pumps.pop(chat_id, None)
    stream_pump._chat_streaming_state.pop(chat_id, None)


def _task(task_id: str) -> shared.TaskDefinition:
    return shared.TaskDefinition(id=task_id, name="count", agent="agent-x", prompt="count",
                                 scope="agent", notification_mode="none")


async def test_a_cancel_mid_flight_closes_the_live_stream_with_the_rows_verdict(temp_db, monkeypatch):
    run_id, session_id = "run-cx1", "11111111-1111-4111-8111-111111111111"
    layer = _Layer(hold=True)
    t, q = _drive(monkeypatch, layer, _task("t-cx1"), run_id, session_id)
    try:
        await asyncio.wait_for(q.first_text.wait(), 10)
        assert temp_db.get_run(run_id)["status"] == run_status.RUNNING
        assert await scheduler.cancel_run(run_id)
        await asyncio.wait_for(t, 10)
        await _settle(f"task-{run_id}")
    finally:
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
    row = temp_db.get_run(run_id)
    assert row["status"] == run_status.CANCELLED
    assert row["error_message"] == "Interrupted by user"
    assert row["duration_ms"] is not None  # the body's own handler, not the pre-body end
    assert layer.closes == 1
    # Exactly one done, the row's word, and the row already read it when it landed.
    assert q.dones() == [({"type": "done", "status": run_status.CANCELLED}, run_status.CANCELLED)]
    # The closing CLI's trailing frame never reached the stream or a buffer.
    assert not any(e.get("text") == "3\n" for e, _ in q.seen)
    assert _registries_clear(run_id, "t-cx1")


async def test_a_run_left_to_complete_sends_exactly_one_done_after_the_stamp(temp_db, monkeypatch):
    run_id, session_id = "run-cx2", "22222222-2222-4222-8222-222222222222"
    layer = _Layer(hold=False)
    t, q = _drive(monkeypatch, layer, _task("t-cx2"), run_id, session_id)
    try:
        await asyncio.wait_for(t, 20)
        await _settle(f"task-{run_id}")
    finally:
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
    row = temp_db.get_run(run_id)
    assert row["status"] == run_status.COMPLETED
    assert row["output_text"] == "1\n2\n"
    assert q.dones() == [({"type": "done", "status": run_status.COMPLETED}, run_status.COMPLETED)]
    assert [e["type"] for e, _ in q.seen] == ["text", "done"]
    assert _registries_clear(run_id, "t-cx2")


async def test_the_streams_word_waits_for_the_demotion(temp_db, monkeypatch):
    """A run the runner demotes after a clean turn (an engine error recorded
    by the pump) streams ``done failed`` — the producer's early ``completed``
    was the wrong word and is gone."""
    from core.events import stream_pump
    run_id, session_id = "run-cx3", "33333333-3333-4333-8333-333333333333"
    layer = _Layer(hold=False)
    real_start = stream_pump.ChatStreamPump.start

    def _start(self):
        self.last_error = "Not logged in"
        return real_start(self)

    monkeypatch.setattr(stream_pump.ChatStreamPump, "start", _start)
    t, q = _drive(monkeypatch, layer, _task("t-cx3"), run_id, session_id)
    try:
        await asyncio.wait_for(t, 20)
        await _settle(f"task-{run_id}")
    finally:
        shared._run_subscribers.pop(run_id, None)
        shared._run_event_buffer.pop(run_id, None)
    assert temp_db.get_run(run_id)["status"] == run_status.FAILED
    assert q.dones() == [({"type": "done", "status": run_status.FAILED}, run_status.FAILED)]


async def test_a_cancel_in_the_pre_slot_window_ends_the_row(temp_db, monkeypatch):
    """The first ``await`` of ``_run_task`` (the target resolve) is reachable
    the instant the run id is returned; a cancel there used to leave the
    row ``pending`` forever with the registries reserved."""
    from core.config import task_config_builder
    from storage import remote_store
    run_id, task_id = "run-pre1", "t-pre1"
    task = _task(task_id)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
    monkeypatch.setattr(remote_store, "resolve_execution_target",
                        lambda *a, **k: time.sleep(0.4) or ("local", None))
    task_store.create_run(run_id, task_id, task.agent, "manual", None, task.prompt,
                          "one-time", task.scope, None)
    shared._active_task_ids[task_id] = run_id
    q = _SnapQueue(run_id)
    shared._run_subscribers.setdefault(run_id, []).append(q)
    t = asyncio.get_running_loop().create_task(
        runner._run_task(run_id, "s-pre1", task, task.prompt, "manual", None, 1))
    shared._running_tasks[run_id] = t
    t.add_done_callback(lambda _: shared._running_tasks.pop(run_id, None))
    await asyncio.sleep(0.05)  # inside the thread hop
    assert await scheduler.cancel_run(run_id)
    with pytest.raises(asyncio.CancelledError):
        await t
    row = temp_db.get_run(run_id)
    assert row["status"] == run_status.CANCELLED
    assert row["error_message"] == "Interrupted by user"
    assert q.dones() == [({"type": "done", "status": run_status.CANCELLED}, run_status.CANCELLED)]
    assert _registries_clear(run_id, task_id)
