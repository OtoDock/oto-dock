"""A headless Codex turn that goes silent past the ceiling with nothing
running is interrupted and ends with the ``silent`` ending, a daemon that
exits mid-turn ends the turn with ``exited``, and a plain turn failure is the
``error`` ending. The daemon stays warm for all three."""

import asyncio

import pytest

import config
from core.events import turn_ending
from core.events.common_events import DONE, ERROR
from core.layers.codex import session as codex_session_mod
from core.layers.codex.session import CodexAppServerSession, TURN_ENDING_EVENT
from core.layers.codex.translator import CodexEventTranslator


class _Client:
    def __init__(self, *, interrupt_fails: bool = False):
        self.requests: list[tuple[str, dict]] = []
        self.is_alive = True
        self.closed = False
        self._interrupt_fails = interrupt_fails

    async def request(self, method, params=None, *, timeout=None):
        self.requests.append((method, params or {}))
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt" and self._interrupt_fails:
            from core.layers.codex.app_server_client import AppServerError
            raise AppServerError("turn/interrupt timed out after 10.0s")
        return {}

    async def close(self):
        self.closed = True
        self.is_alive = False


def _session() -> CodexAppServerSession:
    s = CodexAppServerSession(
        session_id="s1", agent_name="a", model="gpt-5.4",
        sandbox_mode="workspace-write", working_dir="/tmp",
        config_dir="/tmp/.codex", thread_id="thr-1",
    )
    s._started = True
    s._client = _Client()
    return s


async def _run(s: CodexAppServerSession, feed, *, task: bool = False) -> list:
    out = []
    feeder = asyncio.create_task(feed(s))
    async for ev in s.send_message("hello", task=task):
        out.append(ev)
    await feeder
    return out


@pytest.mark.asyncio
async def test_silence_past_the_ceiling_interrupts_and_ends_silent(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(codex_session_mod, "_SILENCE_SLICE_S", 0.1)
    s = _session()

    async def feed(_s):
        return None
    events = await asyncio.wait_for(_run(s, feed), 5)
    assert [e.type for e in events] == [TURN_ENDING_EVENT]
    assert turn_ending.from_dict(events[0].data).reason == turn_ending.SILENT
    assert ("turn/interrupt", {"threadId": "thr-1", "turnId": "turn-1"}) in s._client.requests
    assert s.is_alive


@pytest.mark.asyncio
async def test_an_open_item_keeps_the_silent_turn(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    monkeypatch.setattr(codex_session_mod, "_SILENCE_SLICE_S", 0.1)
    s = _session()

    async def feed(_s):
        await asyncio.sleep(0.05)
        _s._default_consumer.put_nowait(("item/started", {"threadId": "thr-1",
                                         "item": {"id": "it-1", "type": "mcpToolCall"}}))
        await asyncio.sleep(0.7)
        _s._default_consumer.put_nowait(("item/completed", {"threadId": "thr-1",
                                         "item": {"id": "it-1", "type": "mcpToolCall"}}))
        _s._default_consumer.put_nowait(("turn/completed", {"threadId": "thr-1",
                                         "turn": {"status": "completed"}}))
    events = await asyncio.wait_for(_run(s, feed), 5)
    assert [e.type for e in events] == ["item/started", "item/completed", "turn/completed"]
    assert not any(m == "turn/interrupt" for m, _ in s._client.requests)


@pytest.mark.asyncio
async def test_a_daemon_exit_mid_turn_is_the_exited_ending():
    s = _session()

    async def feed(_s):
        await asyncio.sleep(0.05)
        _s._default_consumer.put_nowait(("__daemon_exit__", {}))
    events = await asyncio.wait_for(_run(s, feed), 5)
    assert [e.type for e in events] == [TURN_ENDING_EVENT]
    assert turn_ending.from_dict(events[0].data).reason == turn_ending.EXITED


def test_the_translator_maps_the_session_endings_and_plain_failures():
    tr = CodexEventTranslator(model="gpt-5.4", session_id="s1")
    silent = turn_ending.TurnEnding(turn_ending.SILENT, detail="no output for 600 s")
    from core.layers.codex.session import CodexEvent
    events = tr.translate(CodexEvent(type=TURN_ENDING_EVENT, data=silent.as_dict()))
    assert [e.type for e in events] == [ERROR, DONE]
    assert turn_ending.from_dict(events[0].data["ending"]) == silent
    assert events[0].data["message"] == silent.line()

    failed = tr.translate(CodexEvent(type="turn/completed", data={
        "threadId": "thr-1", "turn": {"status": "failed", "error": {"message": "model 400"}}}))
    err = [e for e in failed if e.type == ERROR][0]
    ending = turn_ending.from_dict(err.data["ending"])
    assert ending.reason == turn_ending.ERROR and ending.detail == "model 400"
    assert ending.reason not in turn_ending.KILLS_PROCESS


def test_the_codex_layer_reads_the_daemons_last_activity():
    import time
    from core.layers.codex.layer import CodexCLIExecutionLayer
    from core.layers.codex.session import _codex_sessions
    layer = CodexCLIExecutionLayer.__new__(CodexCLIExecutionLayer)
    s = _session()
    s.last_activity = time.monotonic() - 42.0
    _codex_sessions[s.session_id] = s
    try:
        assert 41.0 < layer.session_idle_seconds(s.session_id) < 50.0
    finally:
        _codex_sessions.pop(s.session_id, None)
    assert layer.session_idle_seconds("no-such") is None


@pytest.mark.asyncio
async def test_an_open_message_item_does_not_keep_the_silent_turn(monkeypatch):
    """A stream that stalls in the middle of an answer is silence, not work:
    only a tool-like item takes the turn ceiling."""
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    monkeypatch.setattr(codex_session_mod, "_SILENCE_SLICE_S", 0.1)
    s = _session()

    async def feed(_s):
        await asyncio.sleep(0.02)
        _s._default_consumer.put_nowait(("item/started", {"threadId": "thr-1",
                                         "item": {"id": "msg-1", "type": "agentMessage"}}))
    events = await asyncio.wait_for(_run(s, feed), 5)
    assert events[-1].type == TURN_ENDING_EVENT
    assert turn_ending.from_dict(events[-1].data).reason == turn_ending.SILENT


@pytest.mark.asyncio
async def test_a_silent_turn_ends_graceful_and_an_unanswered_interrupt_closes_the_daemon(
        monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(codex_session_mod, "_SILENCE_SLICE_S", 0.1)
    s = _session()
    s._client = _Client(interrupt_fails=True)

    async def feed(_s):
        return None
    events = await asyncio.wait_for(_run(s, feed), 5)
    ending = turn_ending.from_dict(events[-1].data)
    assert ending.reason == turn_ending.SILENT and ending.graceful is True
    assert s._client.closed and not s.is_alive


@pytest.mark.asyncio
async def test_a_task_turn_keeps_its_silence_to_the_turn_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    monkeypatch.setattr(codex_session_mod, "_SILENCE_SLICE_S", 0.1)
    s = _session()

    async def feed(_s):
        await asyncio.sleep(0.6)
        _s._default_consumer.put_nowait(("turn/completed", {"threadId": "thr-1",
                                         "turn": {"status": "completed"}}))
    events = await asyncio.wait_for(_run(s, feed, task=True), 5)
    assert [e.type for e in events] == ["turn/completed"]


@pytest.mark.asyncio
async def test_a_steer_neither_restarts_the_silence_nor_lands_past_it(monkeypatch):
    import time
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    s = _session()
    s._current_turn_id = "turn-1"
    s._last_event_at = time.monotonic() - 10.0
    assert await s.steer("go") is True
    assert time.monotonic() - s._last_event_at >= 10.0
    s._last_event_at = time.monotonic() - 61.0
    assert await s.steer("again") is False
    assert [m for m, _ in s._client.requests].count("turn/steer") == 1


def test_the_reaper_leaves_a_live_turn_to_its_ceiling():
    from core.layers.codex.session import _codex_reap_candidates, _codex_sessions
    s = _session()
    s.last_activity = 0.0
    s._current_turn_id = "turn-1"
    _codex_sessions[s.session_id] = s
    try:
        assert s.session_id not in _codex_reap_candidates(1e6, 100)
        s._current_turn_id = None
        assert s.session_id in _codex_reap_candidates(1e6, 100)
    finally:
        _codex_sessions.pop(s.session_id, None)
