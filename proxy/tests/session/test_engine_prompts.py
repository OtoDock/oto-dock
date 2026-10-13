"""The engine's own permission requests (``core/session/engine_prompts``): a
``can_use_tool`` request a headless Claude CLI raises on its stdio channel is
a prompt waiting on a person. It is answered in the shape the CLI parses,
counted as pending, withdrawn when the CLI withdraws it, surfaced when a
drain reads it between turns, and refused (with the chat's line) when its
surface cannot wait for an answer."""

import asyncio
import json

import pytest

from core.events import chat_writer
from core.events.common_events import PERMISSION_REQUEST
from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeStreamChunk
from core.session import engine_prompts, session_state
from storage import database as task_store
from ws import wire_events as wire


class _Layer:
    """Records the answers written to the engine."""

    def __init__(self):
        self.answers: list[tuple[str, str, bool, dict]] = []

    async def answer_engine_prompt(self, session_id, request_id, approved, tool_input):
        self.answers.append((session_id, request_id, approved, tool_input))


@pytest.fixture
def layer(monkeypatch):
    fake = _Layer()
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "find_layer_for_session", lambda sid: fake)
    from core.remote import remote_file_flow
    monkeypatch.setattr(remote_file_flow, "remote_machine_id", lambda sid: "")
    broadcasts: list[tuple[str, dict]] = []
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda cid, frame: broadcasts.append((cid, frame)))
    fake.broadcasts = broadcasts
    yield fake
    engine_prompts._pending.clear()
    engine_prompts._withdrawn.clear()
    engine_prompts._withdrawn_early.clear()


def _request(rid="req-1", command="rm -rf *"):
    return {"request_id": rid, "tool_name": "Bash", "tool_input": {"command": command},
            "tool_use_id": "toolu_1", "description": ""}


def _kind(session_id: str, client_type: str) -> None:
    session_state._sessions[session_id] = {"client_type": client_type}


async def _settled(rid: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while engine_prompts.owns(rid):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the prompt never settled")
        await asyncio.sleep(0.01)


def test_the_answer_is_the_shape_the_cli_parses():
    allow = engine_prompts.answer_frame("r1", True, {"command": "ls"})
    assert allow == {"type": "control_response", "response": {
        "subtype": "success", "request_id": "r1",
        "response": {"behavior": "allow", "updatedInput": {"command": "ls"}}}}
    # An allow with no known input leaves the call's input as it was.
    assert engine_prompts.answer_frame("r1", True)["response"]["response"] == {"behavior": "allow"}
    deny = engine_prompts.answer_frame("r1", False, {"command": "ls"})["response"]["response"]
    assert deny["behavior"] == "deny" and deny["message"]


@pytest.mark.asyncio
async def test_a_watched_request_counts_as_pending_and_its_answer_reaches_the_engine(layer):
    _kind("s-ans", "dashboard")
    engine_prompts.watch("s-ans", _request("r-ans"), chat_id="")
    assert session_state.has_pending_prompt("s-ans")
    assert session_state.get_permission_request_session("r-ans") == "s-ans"
    assert session_state.resolve_permission("r-ans", True)
    await _settled("r-ans")
    assert layer.answers == [("s-ans", "r-ans", True, {"command": "rm -rf *"})]
    assert not session_state.has_pending_prompt("s-ans")


@pytest.mark.asyncio
async def test_an_unattended_request_nobody_answers_is_refused_with_the_line(layer, monkeypatch, temp_db):
    task_store.create_chat("c-un", "user-admin", "agent-x")
    _kind("s-un", "task")
    monkeypatch.setattr(engine_prompts, "UNATTENDED_WAIT_S", 0.1)
    engine_prompts.watch("s-un", _request("r-un"), chat_id="c-un")
    await _settled("r-un")
    assert layer.answers == [("s-un", "r-un", False, {"command": "rm -rf *"})]
    await chat_writer.drain("c-un", timeout=2)
    rows = [json.loads(m["event_data"]) for m in task_store.get_chat_messages("c-un")
            if m["role"] == "event"]
    assert rows and rows[-1]["subtype"] == wire.SUBTYPE_ENGINE_PROMPT_REFUSED
    assert "`rm -rf *`" in rows[-1]["message"] and "nobody answered" in rows[-1]["message"]
    # The card leaves every viewer (no pump holds it here).
    assert ("c-un", {"type": wire.PROMPT_RETIRED, "chat_id": "c-un",
                     "request_id": "r-un"}) in layer.broadcasts


@pytest.mark.asyncio
async def test_a_phone_call_refuses_at_once(layer):
    _kind("s-ph", "phone")
    engine_prompts.watch("s-ph", _request("r-ph"))
    await _settled("r-ph", timeout=1)
    assert layer.answers == [("s-ph", "r-ph", False, {"command": "rm -rf *"})]


@pytest.mark.asyncio
async def test_a_remote_request_waits_only_on_a_satellite_that_answers(layer, monkeypatch):
    from core.remote import remote_file_flow
    monkeypatch.setattr(remote_file_flow, "remote_machine_id", lambda sid: "m-1")
    _kind("s-rem", "dashboard")
    monkeypatch.setattr(engine_prompts, "ANSWER_SATELLITE_VERSION", "")
    engine_prompts.watch("s-rem", _request("r-rem"))
    await _settled("r-rem", timeout=1)
    assert layer.answers[-1][2] is False
    # A satellite at the answering version waits for the person.
    monkeypatch.setattr(engine_prompts, "ANSWER_SATELLITE_VERSION", "0.5.140")
    from core.remote import satellite_connection

    class _Cm:
        def satellite_version(self, machine_id):
            return "0.5.141"
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _Cm)
    engine_prompts.watch("s-rem", _request("r-rem2"))
    await asyncio.sleep(0.05)
    assert engine_prompts.owns("r-rem2")
    session_state.resolve_permission("r-rem2", True)
    await _settled("r-rem2")
    assert layer.answers[-1][1:3] == ("r-rem2", True)


@pytest.mark.asyncio
async def test_a_withdrawn_request_writes_no_answer(layer):
    _kind("s-wd", "dashboard")
    engine_prompts.watch("s-wd", _request("r-wd"))
    engine_prompts.note_frame("s-wd", {"type": "control_cancel_request", "request_id": "r-wd"},
                              in_turn=True)
    await _settled("r-wd")
    assert layer.answers == []
    assert not session_state.has_pending_prompt("s-wd")


@pytest.mark.asyncio
async def test_a_release_by_a_close_or_an_abort_writes_no_answer(layer):
    _kind("s-rel", "dashboard")
    engine_prompts.watch("s-rel", _request("r-rel"))
    session_state.resolve_session_permissions("s-rel", approved=False)
    await _settled("r-rel")
    assert layer.answers == []


@pytest.mark.asyncio
async def test_a_request_read_between_turns_is_surfaced_on_its_chat(layer, temp_db):
    task_store.create_chat("c-dr", "user-admin", "agent-x")
    task_store.update_chat("c-dr", session_id="s-dr")
    _kind("s-dr", "dashboard")
    frame = {"type": "control_request", "request_id": "r-dr",
             "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                         "input": {"command": "rm -rf build"}, "tool_use_id": "t1"}}
    # A turn's own reader leaves it to the pump.
    engine_prompts.note_frame("s-dr", frame, in_turn=True)
    await asyncio.sleep(0.05)
    assert not engine_prompts.owns("r-dr")
    engine_prompts.note_frame("s-dr", frame)
    deadline = asyncio.get_running_loop().time() + 2
    while not engine_prompts.owns("r-dr"):
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert session_state.has_pending_prompt("s-dr")
    cards = [f for cid, f in layer.broadcasts if f.get("type") == wire.PERMISSION_PROMPT]
    assert cards and cards[0]["request_id"] == "r-dr" and cards[0]["chat_id"] == "c-dr"
    await chat_writer.drain("c-dr", timeout=2)
    rows = [m for m in task_store.get_chat_messages("c-dr")
            if m.get("event_type") == wire.PERMISSION_PROMPT]
    assert rows and json.loads(rows[0]["event_data"])["request_id"] == "r-dr"
    # Shown again to a viewer who opens the chat later.
    assert [p.request_id for p in engine_prompts.pending_for_chat("c-dr")] == ["r-dr"]
    session_state.resolve_permission("r-dr", False)
    await _settled("r-dr")
    assert layer.answers == [("s-dr", "r-dr", False, {"command": "rm -rf build"})]


@pytest.mark.asyncio
async def test_a_request_read_while_the_runs_pump_lives_goes_through_that_pump(layer, temp_db, monkeypatch):
    task_store.create_chat("c-pp", "user-admin", "agent-x")
    task_store.update_chat("c-pp", session_id="s-pp")
    from core.events import stream_pump

    class _Pump:
        is_done = False
        session_id = "s-pp"

        def __init__(self):
            self.event_queue = asyncio.Queue()
    pump = _Pump()
    monkeypatch.setitem(stream_pump._active_pumps, "c-pp", pump)
    engine_prompts.note_frame("s-pp", {"type": "control_request", "request_id": "r-pp",
                                       "request": {"subtype": "can_use_tool",
                                                   "tool_name": "Write", "input": {}}})
    event = await asyncio.wait_for(pump.event_queue.get(), timeout=2)
    assert event.type == PERMISSION_REQUEST and event.data["request_id"] == "r-pp"
    assert event.data["session_id"] == "s-pp"


def test_the_request_names_the_session_that_raised_it():
    chunk = ClaudeStreamChunk(event_type="permission_prompt",
                              event_data={"request_id": "r-m", "tool_name": "Bash",
                                          "tool_input": {}},
                              session_id="participant-sid")
    events = cli_chunk_to_events(chunk)
    assert events[0].type == PERMISSION_REQUEST
    assert events[0].data["session_id"] == "participant-sid"


@pytest.mark.asyncio
async def test_the_cli_session_writes_the_parsed_shape_to_stdin():
    from core.layers.cli.session import PersistentSession

    class _Stdin:
        def __init__(self):
            self.lines: list[bytes] = []

        def write(self, b):
            self.lines.append(b)

        async def drain(self):
            return None

    class _Proc:
        returncode = None

        def __init__(self):
            self.stdin = _Stdin()
    session = PersistentSession.__new__(PersistentSession)
    session.session_id = "s-cli"
    session.proc = _Proc()
    await session.send_control_response("r-cli", True, tool_input={"command": "ls"})
    written = json.loads(session.proc.stdin.lines[0].decode())
    assert written["response"]["response"] == {"behavior": "allow",
                                               "updatedInput": {"command": "ls"}}
    # A process gone: nothing written, nothing raised.
    session.proc.returncode = 1
    await session.send_control_response("r-cli", False)
    assert len(session.proc.stdin.lines) == 1


@pytest.mark.asyncio
async def test_a_request_withdrawn_before_it_registered_is_never_shown(layer, temp_db):
    """The request and its cancel read in one burst: the cancel lands before
    the surfacing registers it, so nothing waits on it and no card shows."""
    task_store.create_chat("c-ew", "user-admin", "agent-x")
    task_store.update_chat("c-ew", session_id="s-ew")
    _kind("s-ew", "dashboard")
    frame = {"type": "control_request", "request_id": "r-ew",
             "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                         "input": {"command": "ls"}, "tool_use_id": "t1"}}
    engine_prompts.note_frame("s-ew", frame)
    engine_prompts.note_frame("s-ew", {"type": "control_cancel_request", "request_id": "r-ew"})
    await asyncio.sleep(0.3)
    assert not engine_prompts.owns("r-ew")
    assert not session_state.has_pending_prompt("s-ew")
    assert [f for _cid, f in layer.broadcasts if f.get("type") == wire.PERMISSION_PROMPT] == []
    assert engine_prompts.withdrawn_before_registered("r-ew")
    # The pump's own card for it is skipped the same way.
    engine_prompts.watch("s-ew", _request("r-ew"))
    assert not engine_prompts.owns("r-ew")
    engine_prompts._withdrawn_early.clear()
