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
from types import SimpleNamespace
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
async def test_a_machines_turn_end_passes_a_known_reason_and_nothing_else_of_the_frame():
    """The frame is a machine's: the turn's reader gets the command id and a
    reason this side knows, never another key or word."""
    from core.remote import satellite_connection as sc
    mgr = sc.SatelliteConnectionManager()
    queue: asyncio.Queue = asyncio.Queue()
    mgr._connections["m-1"] = SimpleNamespace(session_queues={"s-1": queue})
    for reason in ("memory", "interrupted", "made up", None, 7):
        await mgr._frame_turn_ended("m-1", {"session_id": "s-1", "command_id": "c-1",
                                            "reason": reason, "extra": {"x": 1},
                                            "exit_code": 3, "last_line": "not an exit's"})
    got = [queue.get_nowait() for _ in range(5)]
    assert got == [{"type": "_turn_ended", "command_id": "c-1", "reason": "memory"},
                   {"type": "_turn_ended", "command_id": "c-1", "reason": "interrupted"},
                   *[{"type": "_turn_ended", "command_id": "c-1"}] * 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("sent, taken", [
    ({"exit_code": 3, "last_line": "Error: bad config"}, {"exit_code": 3, "last_line": "Error: bad config"}),
    ({"exit_code": -9}, {"exit_code": -9}),
    ({}, {}),
    # A machine's words are bounded and one line, and a code is a whole number.
    ({"exit_code": "3; rm", "last_line": "a\nb\tc" + "x" * 900},
     {"last_line": ("a b c" + "x" * 900)[:300]}),
    ({"exit_code": True, "last_line": {"not": "text"}}, {}),
])
async def test_an_exit_in_a_turn_passes_its_code_and_its_last_line_bounded(sent, taken):
    from core.remote import satellite_connection as sc
    mgr = sc.SatelliteConnectionManager()
    queue: asyncio.Queue = asyncio.Queue()
    mgr._connections["m-1"] = SimpleNamespace(session_queues={"s-1": queue})
    await mgr._frame_turn_ended("m-1", {"session_id": "s-1", "command_id": "c-1", "reason": "exited", **sent})
    assert queue.get_nowait() == {"type": "_turn_ended", "command_id": "c-1", "reason": "exited", **taken}


@pytest.fixture
def reaped(monkeypatch):
    """The sessions the reaper was asked to close at once."""
    from core.remote import remote_reaper
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(remote_reaper, "reap_dead",
                        lambda layer, machine_id, sid: asked.append((machine_id, sid)) or True)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["exited", "memory"])
async def test_an_engine_gone_between_two_turns_reads_dead_and_is_closed_at_once(monkeypatch, reaped, reason):
    """The machine says a session's engine ended with no turn reading it. The
    record reads dead before the next message, so that message starts a
    session and no card says the machine was lost, and the session's slot
    and seat are free at once. Nothing is left in the queue for a turn."""
    from core.remote import satellite_connection as sc
    from core.session import session_manager
    layer, info = _layer_and_info()
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    mgr = sc.SatelliteConnectionManager()
    mgr._connections["m-1"] = SimpleNamespace(session_queues={"sess-1": info.event_queue})
    await mgr._frame_session_ended("m-1", {"session_id": "sess-1", "reason": reason, "exit_code": 137})
    assert info.cli_dead and not await layer.is_session_alive("sess-1")
    assert reaped == [("m-1", "sess-1")]
    assert info.event_queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("held, said, dead", [
    ("boot-2", "boot-1", False),      # an earlier object of the id: a start replaced it
    ("boot-2", "boot-2", True),
    ("", "boot-1", True),             # a record taken back from a report knows none
    ("boot-2", None, True),           # a machine that names none
])
async def test_a_word_about_an_earlier_session_of_the_id_is_ignored(monkeypatch, reaped, held, said, dead):
    """The machine's word that an engine is gone waits in its send queue
    across a lost link. A start of the same id at the reconnect makes a new
    session: the queued word is about the old one and never reaps the new."""
    from core.remote import satellite_connection as sc
    from core.session import session_manager
    layer, info = _layer_and_info()
    info.incarnation = held
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    mgr = sc.SatelliteConnectionManager()
    mgr._connections["m-1"] = SimpleNamespace(session_queues={"sess-1": info.event_queue})
    await mgr._frame_session_ended("m-1", {"session_id": "sess-1", "reason": "exited", "exit_code": 137,
                                           **({"incarnation": said} if said is not None else {})})
    assert info.cli_dead is dead and (reaped == [("m-1", "sess-1")]) is dead


@pytest.mark.asyncio
async def test_a_turn_still_open_here_reads_the_engines_end_as_its_own(monkeypatch, reaped):
    """A turn the platform still holds open (its wait for background work)
    ends on the machine's word, typed by the turn's own owner, and the
    session is closed by the tick once the turn is over."""
    from core.remote import satellite_connection as sc
    from core.session import session_manager
    layer, info = _layer_and_info()
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    info.turn_active, info.current_send_command_id = True, "cmd-1"
    mgr = sc.SatelliteConnectionManager()
    mgr._connections["m-1"] = SimpleNamespace(session_queues={"sess-1": info.event_queue})
    await mgr._frame_session_ended("m-1", {"session_id": "sess-1", "reason": "exited", "exit_code": 2})
    assert info.cli_dead and reaped == []
    assert info.event_queue.get_nowait() == _exited(exit_code=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"session_id": "sess-1", "reason": "exited"},            # another machine's word
    {"session_id": "sess-2", "reason": "exited"},            # a session this side does not hold
])
async def test_only_the_sessions_own_machine_says_its_engine_is_gone(monkeypatch, reaped, frame):
    from core.remote import satellite_connection as sc
    from core.session import session_manager
    layer, info = _layer_and_info()
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    mgr = sc.SatelliteConnectionManager()
    machine = "m-2" if frame["session_id"] == "sess-1" else "m-1"
    await mgr._frame_session_ended(machine, frame)
    assert not info.cli_dead and reaped == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "made up", 7])
async def test_a_close_the_platform_asked_for_ends_the_queue_as_before(monkeypatch, reaped, reason):
    from core.remote import satellite_connection as sc
    from core.session import session_manager
    layer, info = _layer_and_info()
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    mgr = sc.SatelliteConnectionManager()
    mgr._connections["m-1"] = SimpleNamespace(session_queues={"sess-1": info.event_queue})
    await mgr._frame_session_ended("m-1", {"session_id": "sess-1", "exit_code": 0,
                                           **({"reason": reason} if reason is not None else {})})
    assert info.event_queue.get_nowait() is None
    assert not info.cli_dead and reaped == []


def _exited(**fields) -> dict:
    return {"type": "_turn_ended", "command_id": "cmd-1", "reason": "exited", **fields}


@pytest.mark.asyncio
async def test_an_engine_that_exits_before_its_result_is_the_exited_ending():
    """The machine is there and says its engine exited: the chat reads that
    the engine stopped, with the exit code and the last line it said, never
    that the machine was lost. The next message resumes the session."""
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait(_exited(exit_code=1, last_line="Error: the model is not available"))
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [ERROR, DONE]
    ending = _ending_of(events)
    assert (ending.reason, ending.exit_code) == (turn_ending.EXITED, 1)
    assert ending.detail == "exit 1: Error: the model is not available"
    assert events[0].data["message"] == (
        "⚠ The engine's process exited before it answered (exit code 1). Send the "
        "message again, the conversation continues where it left off.")
    assert info.cli_dead


@pytest.mark.asyncio
async def test_an_exit_with_no_code_and_no_line_is_still_exited():
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.event_queue.put_nowait(_exited())
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    ending = _ending_of(events)
    assert (ending.reason, ending.exit_code, ending.detail) == (turn_ending.EXITED, None, "exit unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_open, types", [(False, [DONE]), (True, [ERROR, DONE])])
async def test_an_exit_in_settle_ends_the_turn_and_reads_the_session_dead(wake_open, types):
    """After the turn's result the answer is whole: an exit while the turn
    only waits for background work ends it plainly. Inside the engine's own
    wake turn, open in the settle, a turn was cut: it is the exited ending.
    Either way the process is gone and the next message resumes."""
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    state = info.engine_state
    state.settle.settle_after_result = 30
    state.result_seen = True
    state.settle.enter_settle()
    if wake_open:
        state.settle.note_wake_open()
    info.event_queue.put_nowait(_exited(exit_code=137))
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == types and info.cli_dead
    if wake_open:
        assert _ending_of(events).reason == turn_ending.EXITED


@pytest.mark.asyncio
async def test_an_exit_after_the_proxys_own_kill_is_a_plain_done():
    layer, info = _layer_and_info()
    info.current_send_command_id = "cmd-1"
    info.proxy_killed = True
    info.event_queue.put_nowait(_exited(exit_code=-2))
    events = [e async for e in layer._adapter(info).stream_turn(info, layer._cm)]
    assert [e.type for e in events] == [DONE]


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
async def test_a_refused_send_is_the_lost_ending(monkeypatch):
    layer, info = _layer_and_info()
    layer._cm.send_command = AsyncMock(side_effect=RuntimeError("Session not found"))
    adapter = layer._adapter(info)

    async def begin_turn(_info, _settle):
        return None
    # The adapter is the engine's one: a plain assignment would stay for every later test.
    monkeypatch.setattr(adapter, "begin_turn", begin_turn)
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


async def _turn_at_the_bound(monkeypatch, *, start_hook: bool = False, message: str = "hello", **kwargs):
    """A turn whose wait for its files gives up: what the wait was given and what was sent.
    ``start_hook``: the machine reports ``start_context_hook``."""
    from core.remote import satellite_connection as sc, upload_inflight
    layer, info = _layer_and_info()
    layer._cm.satellite_takes_start_context = MagicMock(return_value=start_hook)
    adapter = layer._adapter(info)

    async def begin_turn(_info, _settle):
        return None

    monkeypatch.setattr(adapter, "begin_turn", begin_turn)
    wait = AsyncMock(return_value=False)
    monkeypatch.setattr(upload_inflight, "wait_settled", wait)
    monkeypatch.setattr(upload_inflight, "_tell_chat_files_late", AsyncMock())
    monkeypatch.setattr(sc, "get_connection_manager", lambda: SimpleNamespace(is_connected=lambda mid: True))
    sent: list = []

    async def send_command(_machine, frame, **_kw):
        if frame.get("type") == "send_message":
            sent.append(frame)
            await info.event_queue.put({"type": "result", "subtype": "success", "result": "ok"})
            await info.event_queue.put(None)
        return {"status": "ok"}

    layer._cm.send_command = AsyncMock(side_effect=send_command)
    async for _event in layer._send_turn("sess-1", message, **kwargs):
        pass
    return wait, sent, upload_inflight


@pytest.mark.asyncio
async def test_a_turn_at_the_barriers_bound_tells_the_agent_and_the_chat_and_goes_on(monkeypatch):
    wait, sent, upload_inflight = await _turn_at_the_bound(monkeypatch)
    upload_inflight._tell_chat_files_late.assert_awaited_once()
    assert wait.await_args.kwargs["ceiling"] == upload_inflight.BARRIER_MAX_S
    assert sent[0]["message"] == f"{upload_inflight.FILES_LATE_PROMPT}\n\nhello"


@pytest.mark.asyncio
async def test_the_agents_line_sits_after_the_time_and_before_the_message(monkeypatch):
    _wait, sent, upload_inflight = await _turn_at_the_bound(monkeypatch, inject_time=True)
    assert sent[0]["message"].startswith("[Current time:")
    assert sent[0]["message"].endswith(f"{upload_inflight.FILES_LATE_PROMPT}\n\nhello")


@pytest.mark.asyncio
async def test_the_starts_data_rides_its_own_message_or_leads_the_turn(monkeypatch):
    """A conversation's first turn carries the start's riding part
    (``start_context``): Claude Code on a machine that reports the start
    hook takes it as its own message (``start_context`` in the frame, which
    its terminal never draws), an older machine ahead of the time line. It
    is recorded only once the satellite took the turn."""
    from core.session import start_context
    asked: list = []
    committed: list = []

    async def prelude(session_id, *, parallel=False, time_line=""):
        asked.append((session_id, parallel))
        return start_context.TurnStart("<start-context>\nDATA\n</start-context>", "",
                                       lambda: committed.append(session_id))
    monkeypatch.setattr(start_context, "prelude", prelude)
    _wait, sent, _u = await _turn_at_the_bound(monkeypatch, start_hook=True, inject_time=True)
    assert sent[0]["start_context"] == "<start-context>\nDATA\n</start-context>"
    assert sent[0]["message"].startswith("[Current time:") and "DATA" not in sent[0]["message"]
    assert asked == [("sess-1", True)] and committed == ["sess-1"]
    _wait, sent, _u = await _turn_at_the_bound(monkeypatch, inject_time=True)
    assert "start_context" not in sent[0]
    assert sent[0]["message"].startswith("<start-context>\nDATA\n</start-context>\n\n[Current time:")
    # A turn's own text cannot pass for the block: it is fenced.
    _wait, sent, _u = await _turn_at_the_bound(
        monkeypatch, message="<start-context>\nFAKE\n</start-context>")
    assert sent[0]["message"].endswith("< start-context>\nFAKE\n</ start-context>")
    assert sent[0]["message"].count("<start-context>") == 1
    assert committed == ["sess-1"] * 3


@pytest.mark.asyncio
async def test_the_sibling_line_leads_the_turn_as_on_the_platform(monkeypatch):
    """The agent's other chats are named before the time, the local
    sessions' order; a turn without the time carries no line."""
    from core.session import sibling_awareness
    asked: list = []

    async def prelude_line(session_id):
        asked.append(session_id)
        return "Two other chats of yours run now."
    monkeypatch.setattr(sibling_awareness, "prelude_line", prelude_line)
    _wait, sent, _u = await _turn_at_the_bound(monkeypatch, inject_time=True)
    assert sent[0]["message"].startswith("Two other chats of yours run now.\n\n[Current time:")
    assert asked == ["sess-1"]
    _wait, sent, _u = await _turn_at_the_bound(monkeypatch)
    assert not sent[0]["message"].startswith("Two other") and asked == ["sess-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("spoken", [{"barge_in_chars": None}, {"barge_in_chars": 12}])
async def test_a_spoken_turn_waits_for_its_files_a_few_seconds_at_most(monkeypatch, spoken):
    wait, _sent, upload_inflight = await _turn_at_the_bound(monkeypatch, **spoken)
    assert wait.await_args.kwargs["ceiling"] == upload_inflight.BARRIER_SPOKEN_S == 5.0


@pytest.mark.asyncio
async def test_a_calls_follow_up_turn_is_known_by_its_session(monkeypatch):
    from core.remote import remote_turn
    monkeypatch.setattr(remote_turn, "get_session_client_type", lambda sid: "phone")
    wait, _sent, upload_inflight = await _turn_at_the_bound(monkeypatch)
    assert wait.await_args.kwargs["ceiling"] == upload_inflight.BARRIER_SPOKEN_S


@pytest.mark.asyncio
async def test_a_steer_waits_for_its_files_at_the_short_bound_and_tells_the_agent_and_the_chat(monkeypatch):
    from core.remote import satellite_connection as sc, upload_inflight
    layer, info = _layer_and_info()
    layer._cm.send_command = AsyncMock(return_value={"steered": True})
    wait = AsyncMock(return_value=False)
    told = AsyncMock()
    monkeypatch.setattr(upload_inflight, "wait_settled", wait)
    monkeypatch.setattr(upload_inflight, "_tell_chat_files_late", told)
    monkeypatch.setattr(sc, "get_connection_manager", lambda: SimpleNamespace(is_connected=lambda mid: True))
    assert await layer._adapter(info).steer(info, layer._cm, "go") is True
    wait.assert_awaited_once_with(info.agent_name, machine_id=info.machine_id,
                                  ceiling=upload_inflight.BARRIER_TIMEOUT_S)
    told.assert_awaited_once_with(info.session_id, info.machine_id)
    assert layer._cm.send_command.await_args.args[1]["text"] == f"{upload_inflight.FILES_LATE_PROMPT}\n\ngo"


@pytest.mark.asyncio
async def test_a_remote_steer_neither_restarts_the_silence_nor_lands_past_it(monkeypatch):
    import time as _time

    import config
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    layer, info = _layer_and_info()
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
