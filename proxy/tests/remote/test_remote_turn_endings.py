"""A remote Claude turn the engine stopped, and a turn its consumer leaves
early: the satellite's CLI and its per-turn reader never outlive the turn.

A typed ending (a decline, a usage limit) hard-aborts the session before the
ERROR is forwarded, since a declined Claude turn goes on by itself and the
consumer may stop at the ERROR; the session is flagged dead so its next start
resumes. A turn generator closed before ``turn_ended`` sends ``stop_turn``, so
the next send does not meet a second reader on the CLI's stdout.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.events import turn_ending
from core.events.common_events import DONE, ERROR
from core.layers.cli.remote import ClaudeRemoteState
from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo

_REFUSAL = {"type": "stream_event", "event": {"type": "message_delta", "delta": {
    "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber"}}}}


def _layer_and_info(session_id: str = "sess-1"):
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    cm = MagicMock()
    cm.send_fire_and_forget = AsyncMock()
    cm.send_command = AsyncMock(return_value={})
    cm.wait_abort_acked = AsyncMock(return_value=True)
    cm.is_session_in_grace = MagicMock(return_value=False)
    layer._cm = cm
    info = RemoteSessionInfo(
        session_id=session_id, machine_id="m-1", agent_name="agent-1",
        execution_path="claude-code-cli", event_queue=asyncio.Queue(),
    )
    translator = ClaudeCLIEventTranslator(session_id)
    info.engine_state = ClaudeRemoteState(
        translator=translator, settle=SettleController(session_id, 0, translator))
    layer._sessions = {session_id: info}
    return layer, info


def _frames(cm, frame_type: str) -> list[dict]:
    return [c.args[1] for c in cm.send_fire_and_forget.await_args_list
            if c.args[1].get("type") == frame_type]


@pytest.mark.asyncio
async def test_a_typed_ending_aborts_the_session_before_it_is_forwarded(monkeypatch):
    layer, info = _layer_and_info()
    adapter = layer._adapter(info)

    async def begin_turn(_info, _settle):
        return None

    monkeypatch.setattr(adapter, "begin_turn", begin_turn)

    async def send_command(_machine, frame, **_kw):
        # The satellite streams the turn once the message is sent (a frame
        # queued earlier would be drained as a previous turn's leftover).
        if frame.get("type") == "send_message":
            info.event_queue.put_nowait(_REFUSAL)
        return {}

    layer._cm.send_command = AsyncMock(side_effect=send_command)
    seen: list = []
    async for event in layer._send_turn("sess-1", "hello"):
        if event.type == ERROR:
            # The abort went out before the consumer saw the ending.
            assert _frames(layer._cm, "abort") and info.cli_dead
        seen.append(event)
    assert [e.type for e in seen] == [ERROR, DONE]
    assert turn_ending.from_dict(seen[0].data["ending"]).reason == turn_ending.DECLINED
    assert info.turn_active is False


@pytest.mark.asyncio
async def test_a_turn_left_early_sends_stop_turn():
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait({"type": "result", "subtype": "success", "is_error": True,
                                 "result": "Overloaded", "_command_id": "cmd-1"})
    stream = layer._adapter(info).stream_turn(info, layer._cm)
    while (await stream.__anext__()).type != ERROR:
        pass
    await stream.aclose()  # the consumer stops at the ERROR
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert _frames(layer._cm, "stop_turn")


@pytest.mark.asyncio
async def test_a_turn_closed_by_its_own_turn_ended_sends_no_extra_stop_turn():
    # The satellite closed the turn itself before any result (its process
    # went): the ``lost`` ending, and no stop_turn of this side's.
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "cmd-1"})
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [ERROR, DONE]
    assert turn_ending.from_dict(events[0].data["ending"]).reason == turn_ending.LOST
    await asyncio.sleep(0)
    assert _frames(layer._cm, "stop_turn") == []



@pytest.mark.asyncio
async def test_a_typed_ending_in_a_replayed_turn_aborts_the_session():
    """A turn re-adopted after a restart that the model's safety classifier
    declined: the CLI is ended as for a live turn, before the ERROR."""
    queue: asyncio.Queue = asyncio.Queue()
    cm = MagicMock()
    cm.create_session_queue = MagicMock(return_value=queue)
    cm.wait_abort_acked = AsyncMock(return_value=True)

    async def send(_machine, msg):
        if msg.get("type") == "resume_session_stream":
            queue.put_nowait({"type": "_resume_replay_begin", "truncated": False, "count": 1})
            queue.put_nowait(_REFUSAL)

    cm.send_fire_and_forget = AsyncMock(side_effect=send)
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = cm
    layer._sessions = {}
    events = []
    async for e in layer.adopt_session(
        machine_id="m-1", session_id="s-1", agent_name="pa",
        execution_path="claude-code-cli", command_id="cmd-1",
    ):
        if e.type == ERROR:
            assert _frames(cm, "abort")
        events.append(e)
    assert [e.type for e in events][-2:] == [ERROR, DONE]
    assert layer._sessions["s-1"].cli_dead


@pytest.mark.asyncio
async def test_an_error_ending_in_a_replayed_turn_leaves_the_session():
    """Only a decline, a limit or a silence ends a replayed turn's CLI, as
    for a live turn: an error the satellite reports ends the turn alone."""
    queue: asyncio.Queue = asyncio.Queue()
    cm = MagicMock()
    cm.create_session_queue = MagicMock(return_value=queue)
    cm.wait_abort_acked = AsyncMock(return_value=True)
    cm.is_session_in_grace = MagicMock(return_value=False)

    async def send(_machine, msg):
        if msg.get("type") == "resume_session_stream":
            queue.put_nowait({"type": "_resume_replay_begin", "truncated": False, "count": 1})
            queue.put_nowait({"type": "error", "message": "the engine reported an error"})

    cm.send_fire_and_forget = AsyncMock(side_effect=send)
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = cm
    layer._sessions = {}
    events = [e async for e in layer.adopt_session(
        machine_id="m-1", session_id="s-1", agent_name="pa",
        execution_path="claude-code-cli", command_id="cmd-1",
    )]
    assert [e.type for e in events][-2:] == [ERROR, DONE]
    assert _ending_of([e for e in events if e.type == ERROR]).reason == turn_ending.ERROR
    assert not _frames(cm, "abort")


# ---------------------------------------------------------------------------
# The abnormal endings of a remote turn: the stream or the process lost, the
# satellite's error, silence past the ceiling.
# ---------------------------------------------------------------------------

def _ending_of(events):
    return turn_ending.from_dict(events[0].data["ending"])


@pytest.mark.asyncio
async def test_the_satellites_session_end_is_a_lost_ending():
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait(None)
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [ERROR, DONE]
    assert _ending_of(events).reason == turn_ending.LOST
    # The next send resumes the session instead of meeting the dead CLI.
    assert info.cli_dead and not await layer.is_session_alive("sess-1")


@pytest.mark.asyncio
async def test_a_turn_ended_before_any_result_is_a_lost_ending():
    """The satellite closes a turn whenever its reader returns, an end of
    output included: what ran may not be on disk, so it is not graceful."""
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "cmd-1"})
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [ERROR, DONE]
    ending = _ending_of(events)
    assert ending.reason == turn_ending.LOST and ending.graceful is False
    assert ending.abort_stamps() == {"last_turn_aborted": True, "last_abort_graceful": False}
    assert info.cli_dead


@pytest.mark.asyncio
async def test_the_session_end_after_the_proxys_own_kill_is_a_plain_done():
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.proxy_killed = True
    info.event_queue.put_nowait(None)
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [DONE]
    assert not info.cli_dead


@pytest.mark.asyncio
async def test_the_hard_abort_marks_the_kill_as_the_proxys_own():
    layer, info = _layer_and_info()
    layer._cm.drop_grace_session = MagicMock()
    layer._cm.arm_abort_acked = MagicMock()
    await layer._hard_abort("sess-1")
    assert info.proxy_killed is True


@pytest.mark.asyncio
async def test_a_turn_ended_after_the_result_is_a_plain_done():
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.engine_state.result_seen = True
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "cmd-1"})
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [DONE]


@pytest.mark.asyncio
async def test_the_grace_expiry_is_lost_and_a_refused_send_is_error():
    layer, info = _layer_and_info()
    info.event_queue.put_nowait({"type": "error", "message": "stream interrupted",
                                 "durable_marker": True})
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [ERROR, DONE]
    assert _ending_of(events).reason == turn_ending.LOST

    layer, info = _layer_and_info("sess-2")
    info.event_queue.put_nowait({"type": "error", "message": "CLI process not running"})
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [ERROR, DONE]
    ending = _ending_of(events)
    assert ending.reason == turn_ending.ERROR and ending.detail == "CLI process not running"


@pytest.mark.asyncio
async def test_silence_past_the_ceiling_ends_the_remote_turn_silent(monkeypatch):
    import config
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(settle_mod.SettleController, "effective_timeout", lambda self: 0.1)
    layer, info = _layer_and_info()
    aborted: list[str] = []

    async def hard_abort(sid):
        aborted.append(sid)
        return False
    layer._hard_abort = hard_abort
    adapter = layer._adapter(info)

    async def begin_turn(_info, _settle):
        return None
    monkeypatch.setattr(adapter, "begin_turn", begin_turn)
    # The remote turn runs the adapter's stream and, on the ``silent``
    # ending, hard-aborts the session before the ending is forwarded.
    events = [e async for e in layer._send_turn("sess-1", "hello")]
    assert [e.type for e in events] == [ERROR, DONE]
    assert _ending_of(events).reason == turn_ending.SILENT
    assert aborted == ["sess-1"] and _frames(layer._cm, "stop_turn")
    assert info.turn_active is False


@pytest.mark.asyncio
async def test_an_open_tool_keeps_the_silent_remote_turn(monkeypatch):
    import asyncio as _aio
    import config
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    monkeypatch.setattr(settle_mod.SettleController, "effective_timeout", lambda self: 0.1)
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait({"type": "stream_event", "_command_id": "cmd-1", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash"}}})

    async def _later():
        await _aio.sleep(0.6)
        info.event_queue.put_nowait({"type": "result", "subtype": "success", "is_error": False,
                                     "result": "done", "_command_id": "cmd-1"})
    feeder = _aio.create_task(_later())
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    await feeder
    assert ERROR not in [e.type for e in events] and events[-1].type == DONE


@pytest.mark.asyncio
async def test_a_refused_send_is_the_lost_ending():
    layer, info = _layer_and_info()
    layer._cm.send_command = AsyncMock(side_effect=RuntimeError("Session not found"))
    adapter = layer._adapter(info)

    async def begin_turn(_info, _settle):
        return None
    adapter.begin_turn = begin_turn
    events = [e async for e in layer._send_turn("sess-1", "hello")]
    assert [e.type for e in events] == [ERROR, DONE]
    assert _ending_of(events).reason == turn_ending.LOST and info.cli_dead


@pytest.mark.asyncio
async def test_the_reconnect_grace_holds_the_silence_ceiling(monkeypatch):
    import asyncio as _aio
    import config
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(settle_mod.SettleController, "effective_timeout", lambda self: 0.1)
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    layer._cm.is_session_in_grace = MagicMock(return_value=True)

    async def _expire():
        await _aio.sleep(0.6)
        info.event_queue.put_nowait({"type": "error", "message": "stream interrupted",
                                     "durable_marker": True})
    expiring = _aio.create_task(_expire())
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    await expiring
    assert _ending_of(events).reason == turn_ending.LOST


@pytest.mark.asyncio
async def test_a_remote_task_turn_keeps_its_silence_to_the_turn_ceiling(monkeypatch):
    import asyncio as _aio
    import config
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    monkeypatch.setattr(settle_mod.SettleController, "effective_timeout", lambda self: 0.1)
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.task_turn = True

    async def _later():
        await _aio.sleep(0.6)
        info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "cmd-1"})
    feeder = _aio.create_task(_later())
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    await feeder
    assert _ending_of(events).reason == turn_ending.LOST   # not silent


@pytest.mark.asyncio
async def test_a_remote_steer_neither_restarts_the_silence_nor_lands_past_it(monkeypatch):
    import time as _time
    import config
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    layer, info = _layer_and_info()
    layer._cm.satellite_supports_steer_turn = MagicMock(return_value=True)
    layer._cm.send_command = AsyncMock(return_value={"steered": True})
    from core.remote import upload_inflight
    monkeypatch.setattr(upload_inflight, "wait_settled", AsyncMock())
    adapter = layer._adapter(info)
    info.last_event_at = _time.monotonic() - 10.0
    assert await adapter.steer(info, layer._cm, "go") is True
    assert _time.monotonic() - info.last_event_at >= 10.0
    info.last_event_at = _time.monotonic() - 61.0
    assert await adapter.steer(info, layer._cm, "again") is False
    assert layer._cm.send_command.await_count == 1
