"""The CLI's own wake turn read inside a task turn's settle.

A task turn ends its result while a background command still runs, so the
turn's stream settles. The command completes and the CLI opens its own wake
turn in the same stream: an ``init``, tool calls and text, then a
``result``. The settle holds until that result, however long a tool of the
wake turn stays silent, and the run's producer learns the review ran at the
result, never at the ``init``. A wake turn whose result never comes ends as
any turn does (its machine closes the turn or ends the session, silence
past the ceiling), and a wake turn read between turns rides out a silent
gap while its session's stream lives.

The frames are the real capture ``fixtures/cli_wake/probe-bash.jsonl`` with
one tool call written into its wake turn. The session runs on a machine:
the frames reach the stream through the session's event queue.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from core.events import turn_ending, wake_capture
from core.events.bg_command_state import _bg_command_registries, get_bg_command_registry
from core.events.common_events import (
    BG_REVIEW_TURN,
    DONE,
    ERROR,
    METADATA,
    PRODUCER_DONE,
    TEXT,
    TOOL_RESULT,
)
from core.events.task_producer import task_produce
from core.layers.cli.remote import ClaudeRemoteState
from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.session.session_state import get_subagent_registry, reset_subagent_registry
from tests._timing import bound
from tests.remote.test_remote_turn_endings import _frames as _sent, _layer_and_info
from tests.session.test_wake_capture import _frames, _wake_bracket

# One settle slice, and a tool's silence that spans several of them.
_SLICE = 0.02
_SILENCE = _SLICE * 3

_WAKE_TOOL = "toolu_wake_report"
_WAKE_REPORT = "Background task finished: `BG_MARKER_DONE`."


def _script() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """The capture in four parts: the task's own turn up to its result, the
    command's completion, the wake turn up to an open tool call, and the
    rest of the wake turn (the tool's result, the report, the ``result``)."""
    frames = _frames("bash")
    first_result = next(i for i, f in enumerate(frames) if f.get("type") == "result")
    wake = _wake_bracket(frames)
    completion = [f for f in frames[first_result + 1:]
                  if f.get("subtype") in ("task_updated", "task_notification")]
    tool_call = [
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": _WAKE_TOOL, "name": "Bash", "input": {}}}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command": "make report"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    ]
    tool_result = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": _WAKE_TOOL, "content": "ok"}]}}
    return (frames[:first_result + 1], completion,
            [*wake[:2], *tool_call], [tool_result, *wake[2:]])


def _text(events) -> str:
    return "".join(e.data.get("content", "") for e in events if e.type == TEXT)


def _forget(session_id: str) -> None:
    _bg_command_registries.pop(session_id, None)
    wake_capture.forget_session(session_id)


# ── the settle decision ─────────────────────────────────────────────────────

_SID = "sid-wake-settle"


def _controller() -> SettleController:
    reg = get_subagent_registry(_SID)
    for task_id in list(reg.spawned - reg.completed):
        reg.mark_done(task_id)
    reset_subagent_registry(_SID)
    _bg_command_registries.pop(_SID, None)
    return SettleController(_SID, 30.0, ClaudeCLIEventTranslator(_SID),
                            get_hook_activity=lambda sid: None)


def test_an_open_wake_turn_holds_the_settle_until_its_result():
    s = _controller()
    s.note_wake_open()
    assert s.wake_open is False  # an init before the result is the turn's own
    s.enter_settle()
    s.note_wake_open()
    assert s.wake_open is True
    for _ in range(3):
        assert s.should_exit_on_silence(5.0) is False
    assert s.note_wake_result() is True
    assert s.wake_open is False
    assert s.should_exit_on_silence(5.0) is True
    assert s.note_wake_result() is False  # a result with no wake turn open


def test_a_completion_read_in_settle_holds_one_more_slice():
    s = _controller()
    bg = get_bg_command_registry(_SID)
    bg.register_spawn("cmd-1", "tu-1")
    s.enter_settle()
    bg.mark_done("cmd-1", surfaced=False)
    assert s.should_exit_on_silence(5.0) is False  # the wake's init is due
    assert s.should_exit_on_silence(5.0) is True
    _bg_command_registries.pop(_SID, None)


def test_a_wake_that_opened_spends_the_slice_its_completion_held():
    s = _controller()
    bg = get_bg_command_registry(_SID)
    bg.register_spawn("cmd-1", "tu-1")
    s.enter_settle()
    assert s.should_exit_on_silence(5.0) is False
    bg.mark_done("cmd-1", surfaced=False)
    s.note_wake_open()
    assert s.should_exit_on_silence(5.0) is False
    assert s.note_wake_result() is True
    s.enter_settle()
    assert s.should_exit_on_silence(5.0) is True
    _bg_command_registries.pop(_SID, None)


# ── the turn's stream ───────────────────────────────────────────────────────

def _machine_turn(monkeypatch):
    """A machine session's task turn: its stream, its info and its link."""
    monkeypatch.setattr(SettleController, "effective_timeout", lambda self: _SLICE)
    layer, info = _layer_and_info(f"sess-wake-{time.monotonic_ns()}")
    info.engine_state = ClaudeRemoteState(
        translator=info.engine_state.translator,
        settle=SettleController(info.session_id, 30.0, info.engine_state.translator))
    info.task_turn = True
    info.current_send_command_id = "cmd-1"

    async def _send(_machine, frame):
        # The satellite answers a stop with the turn's end.
        if frame.get("type") == "stop_turn":
            info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "cmd-1"})
    layer._cm.send_fire_and_forget = AsyncMock(side_effect=_send)
    return layer, info


async def _collect(stream) -> list:
    return [e async for e in stream]


def _in_the_open_wake_turn(monkeypatch):
    """A task turn read up to the open tool call of its wake turn: the
    layer, the info and the task that reads the stream."""
    layer, info = _machine_turn(monkeypatch)
    main, completion, wake_head, _tail = _script()
    for frame in [*main, *completion, *wake_head]:
        info.event_queue.put_nowait(frame)
    turn = asyncio.create_task(_collect(layer._adapter(info).stream_turn(info, layer._cm)))
    return layer, info, turn


@pytest.mark.asyncio
async def test_a_wake_turn_silent_past_a_settle_slice_is_read_to_its_result(monkeypatch):
    """The incident: the wake turn's tool is silent for several settle
    slices. Every event of the wake turn reaches the consumer, the review
    is noted at the wake turn's result and never at its init, and the
    stream ends only after that result."""
    layer, info, turn = _in_the_open_wake_turn(monkeypatch)
    _main, _completion, _head, wake_tail = _script()
    try:
        await asyncio.sleep(_SILENCE)
        assert not turn.done(), "the stream ended inside the wake turn's silent tool"
        assert not wake_capture.recently_captured(info.session_id)  # not at the init
        assert _sent(layer._cm, "stop_turn") == []
        for frame in wake_tail:
            info.event_queue.put_nowait(frame)
        events = await asyncio.wait_for(turn, 5)
        kinds = [e.type for e in events]
        assert _text(events) == "STARTED" + _WAKE_REPORT
        assert _WAKE_TOOL in [e.data.get("tool_id") for e in events if e.type == TOOL_RESULT]
        assert kinds.count(METADATA) == 2  # the turn's result and the wake turn's
        assert kinds.count(DONE) == 1 and kinds[-2:] == [METADATA, DONE]
        assert ERROR not in kinds
        assert wake_capture.recently_captured(info.session_id)
        assert len(_sent(layer._cm, "stop_turn")) == 1
    finally:
        turn.cancel()
        _forget(info.session_id)


@pytest.mark.asyncio
async def test_an_open_wake_turn_silent_past_the_ceiling_ends_the_turn(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", _SILENCE)
    layer, info = _machine_turn(monkeypatch)
    main, completion, wake_head, _tail = _script()
    for frame in [*main, *completion, *wake_head]:
        info.event_queue.put_nowait(frame)
    try:
        events = await asyncio.wait_for(
            _collect(layer._adapter(info).stream_turn(info, layer._cm)), 5)
        assert [e.type for e in events][-2:] == [ERROR, DONE]
        assert turn_ending.from_dict(events[-2].data["ending"]).reason == turn_ending.SILENT
        assert len(_sent(layer._cm, "stop_turn")) == 1
    finally:
        _forget(info.session_id)


@pytest.mark.asyncio
async def test_a_wake_turn_its_machine_closes_ends_the_stream_with_no_review_noted(monkeypatch):
    """The engine's process goes inside its wake turn. The machine's reader
    returns at the pipe's end, and at a dead process whose pipe stays open,
    and the machine closes the turn either way. The stream ends there and
    no review ran to its end, so the run's producer still owes one."""
    layer, info, turn = _in_the_open_wake_turn(monkeypatch)
    try:
        await asyncio.sleep(_SILENCE)
        assert not turn.done()
        info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "cmd-1"})
        events = await asyncio.wait_for(turn, 5)
        kinds = [e.type for e in events]
        assert kinds[-1] == DONE and kinds.count(DONE) == 1
        assert not wake_capture.recently_captured(info.session_id)
        # The machine closed the turn itself: this side stops no reader.
        assert _sent(layer._cm, "stop_turn") == []
    finally:
        turn.cancel()
        _forget(info.session_id)


@pytest.mark.asyncio
async def test_a_session_that_ends_inside_its_open_wake_turn_ends_the_turn_lost(monkeypatch):
    """The session's end reaches the stream inside the open wake turn (the
    machine ended the session, the reconnect grace ran out): the turn ends
    typed as before its result, and the next message resumes the session."""
    layer, info, turn = _in_the_open_wake_turn(monkeypatch)
    try:
        await asyncio.sleep(_SLICE * 2)
        assert not turn.done()
        info.event_queue.put_nowait(None)
        events = await asyncio.wait_for(turn, 5)
        assert [e.type for e in events][-2:] == [ERROR, DONE]
        assert turn_ending.from_dict(events[-2].data["ending"]).reason == turn_ending.LOST
        assert info.cli_dead and not await layer.is_session_alive(info.session_id)
        assert not wake_capture.recently_captured(info.session_id)
    finally:
        turn.cancel()
        _forget(info.session_id)


# ── the run's producer ──────────────────────────────────────────────────────

def _machine_run(monkeypatch):
    """A machine session a task run drives through the remote layer. The
    machine streams ``first_turn`` once the run's message reached it and
    answers a stop with the turn's end; ``messages`` holds every message
    the layer sent it."""
    monkeypatch.setattr(SettleController, "effective_timeout", lambda self: _SLICE)
    layer, info = _layer_and_info(f"sess-wake-run-{time.monotonic_ns()}")
    messages: list[str] = []
    first_turn: list[dict] = []

    async def _command(_machine, frame, **_kw):
        if frame.get("type") == "send_message":
            messages.append(frame["message"])
            # A frame queued before the send would be read as an earlier
            # turn's leftover.
            for line in first_turn if len(messages) == 1 else ():
                info.event_queue.put_nowait(line)
        return {}

    async def _send(_machine, frame):
        if frame.get("type") == "stop_turn":
            info.event_queue.put_nowait(
                {"type": "_turn_ended", "command_id": info.current_send_command_id})
    layer._cm.send_command = AsyncMock(side_effect=_command)
    layer._cm.send_fire_and_forget = AsyncMock(side_effect=_send)
    return layer, info, messages, first_turn


@pytest.mark.asyncio
async def test_the_producer_ends_after_the_wake_turns_result_and_sends_no_review(monkeypatch):
    layer, info, messages, first_turn = _machine_run(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    main, completion, wake_head, wake_tail = _script()
    first_turn.extend(main)
    run = asyncio.create_task(task_produce(
        layer, info.session_id, "do the task", queue, "run-wake-1", bg_poll=0.02))
    try:
        for _ in range(250):
            if messages:
                break
            await asyncio.sleep(0.02)
        assert len(messages) == 1
        await asyncio.sleep(_SLICE * 2)  # the command still runs: the settle holds
        for frame in [*completion, *wake_head]:
            info.event_queue.put_nowait(frame)
        await asyncio.sleep(_SILENCE)
        assert not run.done(), "the run finished while the wake turn still ran"
        for frame in wake_tail:
            info.event_queue.put_nowait(frame)
        await asyncio.wait_for(run, 5)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert _text(events) == "STARTED" + _WAKE_REPORT
        # The wake turn was the review: the machine got the run's message alone.
        assert len(messages) == 1 and messages[0].endswith("do the task")
        assert not [e for e in events if e.type == BG_REVIEW_TURN]
        assert ERROR not in [e.type for e in events]
        assert events[-1].type == PRODUCER_DONE
    finally:
        run.cancel()
        _forget(info.session_id)


# ── a wake turn read between turns ──────────────────────────────────────────

def _chat_for(session_id: str) -> str:
    from storage import database as task_store
    chat_id = f"chat-{session_id}"
    task_store.create_chat(chat_id, "user-admin", "test-agent", "default",
                           model="claude-sonnet-5", execution_path="claude-code-cli")
    task_store.update_chat(chat_id, session_id=session_id)
    return chat_id


def _queue_reader(queue: asyncio.Queue):
    async def _read(timeout: float) -> dict | None:
        try:
            return await asyncio.wait_for(queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
    return _read


async def _capture_with_a_gap(session_id: str, **kw) -> bool:
    """One wake bracket whose tool is silent for ``_SILENCE`` mid-way."""
    _main, _completion, wake_head, wake_tail = _script()
    queue: asyncio.Queue = asyncio.Queue()
    for frame in wake_head[1:]:
        queue.put_nowait(frame)

    async def _late():
        await asyncio.sleep(_SILENCE)
        for frame in wake_tail:
            queue.put_nowait(frame)
    late = asyncio.create_task(_late())
    try:
        return await wake_capture.capture_wake_turn(
            session_id, wake_head[0], _queue_reader(queue), source="test", **kw)
    finally:
        await late


@pytest.mark.asyncio
async def test_a_capture_rides_out_a_gap_while_its_session_is_alive(monkeypatch):
    from storage import database as task_store
    monkeypatch.setattr(wake_capture, "WAKE_FRAME_GAP_S", _SLICE)
    sid = "s-wake-gap-alive"
    chat_id = _chat_for(sid)
    assert await _capture_with_a_gap(sid, alive=lambda: True) is True
    assert wake_capture.recently_captured(sid)
    assert "BG_MARKER_DONE" in json.dumps(task_store.get_chat_messages(chat_id))
    wake_capture.forget_session(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", ({"alive": lambda: False}, {}), ids=("gone", "no-liveness"))
async def test_a_gap_ends_the_capture_of_a_session_that_is_gone(monkeypatch, kw):
    from storage import database as task_store
    monkeypatch.setattr(wake_capture, "WAKE_FRAME_GAP_S", _SLICE)
    sid = f"s-wake-gap-{len(kw)}"
    chat_id = _chat_for(sid)
    assert await _capture_with_a_gap(sid, **kw) is False
    assert not wake_capture.recently_captured(sid)
    assert "BG_MARKER_DONE" not in json.dumps(task_store.get_chat_messages(chat_id))


@pytest.mark.asyncio
async def test_a_live_capture_ends_at_the_turn_ceiling(monkeypatch):
    monkeypatch.setattr(wake_capture, "WAKE_FRAME_GAP_S", _SLICE)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", _SLICE * 3)
    sid = "s-wake-gap-ceiling"
    _main, _completion, wake_head, _tail = _script()
    started = time.monotonic()
    assert await wake_capture.capture_wake_turn(
        sid, wake_head[0], _queue_reader(asyncio.Queue()), source="test",
        alive=lambda: True) is False
    assert time.monotonic() - started < bound(2)
    assert not wake_capture.recently_captured(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("attached", (True, False), ids=("stream-attached", "stream-gone"))
async def test_the_drain_between_turns_passes_its_streams_liveness(monkeypatch, attached):
    """A wake turn met between turns: the drain gives the capture the state
    of the session's stream, so a silent tool does not end the capture of a
    session that can still write, and a gap ends one whose stream is gone."""
    monkeypatch.setattr(wake_capture, "WAKE_FRAME_GAP_S", _SLICE)
    layer, info = _layer_and_info(f"sess-wake-drain-{time.monotonic_ns()}")
    layer._cm.is_session_stream_attached = MagicMock(return_value=attached)
    _main, _completion, wake_head, wake_tail = _script()
    for frame in wake_head:
        info.event_queue.put_nowait(frame)

    async def _late():
        await asyncio.sleep(_SILENCE)
        for frame in wake_tail:
            info.event_queue.put_nowait(frame)
    late = asyncio.create_task(_late())
    try:
        assert await layer.drain_bg_commands(info.session_id, budget=0.01) is True
        await late
        assert wake_capture.recently_captured(info.session_id) is attached
        layer._cm.is_session_stream_attached.assert_called_with("m-1", info.session_id)
    finally:
        _forget(info.session_id)
