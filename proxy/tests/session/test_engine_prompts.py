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


def _review_limit(rid: str) -> dict:
    return {"type": "control_request", "request_id": rid, "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf build"},
        "decision_reason_type": "classifier"}}


@pytest.mark.asyncio
async def test_the_reviewers_decline_limit_is_refused_and_auto_re_entered_twice(layer):
    """Claude Code's ask after its reviewer declined several actions in a
    row is never a card: refused with a message the model reads, then auto
    re-entered (Ask and back clears the CLI's pause), twice per session; the
    third time Auto answers with the platform's cards."""
    sid = "sess-review-limit"
    changes: list[str] = []
    messages: dict[str, str] = {}

    async def _reenter(session_id):
        changes.append(session_id)
        return True

    async def _answer(session_id, request_id, approved, tool_input):
        frame = engine_prompts.answer_frame(request_id, approved, tool_input)
        messages[request_id] = frame["response"]["response"]["message"]
        layer.answers.append((session_id, request_id, approved, tool_input))

    layer.reenter_review = _reenter
    layer.answer_engine_prompt = _answer
    session_state.set_session_mode(sid, "autoReview")
    try:
        for n, rid in enumerate(("r0", "r1", "r2"), start=1):
            engine_prompts.note_frame(sid, _review_limit(rid), in_turn=True)
            assert engine_prompts.withdrawn_before_registered(rid)   # the pump shows no card
            for _ in range(200):
                if len(layer.answers) == n and (n == 3 or len(changes) == n):
                    break
                await asyncio.sleep(0.01)
        assert [(r, ok) for _s, r, ok, _i in layer.answers] == [("r0", False), ("r1", False), ("r2", False)]
        assert set(messages.values()) == {engine_prompts._REVIEW_LIMIT_MESSAGE}
        assert changes == [sid, sid]
        assert session_state.get_session_mode(sid) == "autoReview"   # never written by the re-entry
        assert session_state.review_paused(sid)
        # A mode change lifts the pause.
        session_state.set_session_mode(sid, "ask")
        assert not session_state.review_paused(sid)
    finally:
        session_state.cleanup_session_permission_state(sid)
        session_state._session_modes.pop(sid, None)
        session_state._review_reentries.pop(sid, None)


@pytest.mark.asyncio
async def test_a_decline_limit_outside_auto_is_refused_and_nothing_re_enters(layer):
    sid = "sess-review-limit-ask"
    changes: list[str] = []

    async def _reenter(session_id):
        changes.append(session_id)
        return True

    layer.reenter_review = _reenter
    session_state.set_session_mode(sid, "ask")
    try:
        engine_prompts.note_frame(sid, _review_limit("r9"), in_turn=False)
        for _ in range(200):
            if layer.answers:
                break
            await asyncio.sleep(0.01)
        assert [(r, ok) for _s, r, ok, _i in layer.answers] == [("r9", False)]
        await asyncio.sleep(0.05)
        assert changes == [] and not session_state.review_paused(sid)
    finally:
        session_state._session_modes.pop(sid, None)


@pytest.mark.asyncio
async def test_a_mode_picked_during_the_turn_survives_the_re_entry(layer, monkeypatch):
    """The decline limit lands mid-turn; the person picks Ask before the
    turn ends. The CLI layer re-enters only under the session's lock (after
    the turn), reads the mode again there and leaves it: no control frame,
    the session stays in Ask. With the mode still Auto it sends Ask then
    Auto to the CLI, in the CLI's words, and writes no platform mode."""
    from core.layers.cli import layer as cli_layer
    sid = "sess-review-race"
    sent: list = []

    class _Session:
        def __init__(self):
            self.lock = asyncio.Lock()

        async def send_control_request(self, subtype, **kw):
            sent.append((subtype, kw))

        async def send_control_response(self, request_id, approved, tool_input=None):
            pass

    session = _Session()

    async def _get(session_id):
        return session
    monkeypatch.setattr(cli_layer, "get_persistent_session", _get)
    cli = cli_layer.CLIExecutionLayer.__new__(cli_layer.CLIExecutionLayer)
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "find_layer_for_session", lambda s: cli)
    session_state.set_session_mode(sid, "autoReview")
    try:
        await session.lock.acquire()          # the turn holds the session
        engine_prompts.note_frame(sid, _review_limit("rr0"), in_turn=True)
        await asyncio.sleep(0.05)
        session_state.set_session_mode(sid, "ask")   # the person's pick
        session.lock.release()
        await asyncio.sleep(0.05)
        assert sent == []
        assert session_state.get_session_mode(sid) == "ask"
        # Still in Auto when the lock frees: the CLI leaves auto and comes back.
        session_state.set_session_mode(sid, "autoReview")
        assert await cli.reenter_review(sid) is True
        assert sent == [("set_permission_mode", {"mode": "default"}),
                        ("set_permission_mode", {"mode": "auto"})]
        assert session_state.get_session_mode(sid) == "autoReview"
    finally:
        if session.lock.locked():
            session.lock.release()
        session_state.cleanup_session_permission_state(sid)
        session_state._session_modes.pop(sid, None)
        session_state._review_reentries.pop(sid, None)


@pytest.mark.asyncio
async def test_a_remote_re_entry_reads_the_mode_and_writes_none(monkeypatch):
    """The remote placement sends the two control frames through the
    engine's adapter only while the session is in Auto, and leaves the
    session record and the platform's mode alone."""
    from unittest.mock import AsyncMock, MagicMock

    from core.layers.cli.remote import ClaudeRemoteAdapter
    from core.remote.remote_execution import RemoteExecutionLayer
    sid = "sess-review-remote"
    lay = RemoteExecutionLayer(MagicMock())
    lay._cm.send_fire_and_forget = AsyncMock()
    info = MagicMock(session_id=sid, machine_id="m1", mode="autoReview")
    lay._sessions[sid] = info
    monkeypatch.setattr(lay, "_adapter", lambda i: ClaudeRemoteAdapter(MagicMock()))
    try:
        session_state.set_session_mode(sid, "ask")
        assert await lay.reenter_review(sid) is False
        lay._cm.send_fire_and_forget.assert_not_called()
        session_state.set_session_mode(sid, "autoReview")
        assert await lay.reenter_review(sid) is True
        frames = [c.args[1] for c in lay._cm.send_fire_and_forget.call_args_list]
        assert [(f["subtype"], f["kwargs"]) for f in frames] == [
            ("set_permission_mode", {"mode": "default"}), ("set_permission_mode", {"mode": "auto"})]
        assert info.mode == "autoReview" and session_state.get_session_mode(sid) == "autoReview"
    finally:
        lay._sessions.pop(sid, None)
        session_state._session_modes.pop(sid, None)


def _plan_ask(rid: str, tool: str = "mcp__display__display_ui", tool_input: dict | None = None,
              tool_use_id: str = "toolu_plan", **extra) -> dict:
    request = {"subtype": "can_use_tool", "tool_name": tool,
               "input": {"html": "<p>x</p>"} if tool_input is None else tool_input,
               "tool_use_id": tool_use_id, "decision_reason_type": "mode", **extra}
    return {"type": "control_request", "request_id": rid, "request": request}


async def _answers(layer, n: int) -> None:
    for _ in range(200):
        if len(layer.answers) >= n:
            return
        await asyncio.sleep(0.01)


@pytest.fixture
def plan_record():
    yield
    engine_prompts._gate_allowed.clear()
    engine_prompts._set_modes.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("in_turn", [True, False])
async def test_the_cli_ask_for_a_call_the_gate_allowed_in_plan_is_answered_at_once(
        layer, plan_record, in_turn):
    """Claude Code 2.1.299+ asks again for a call the hook allowed in plan
    mode: the gate's record answers it with the request's own input, no card
    (in the turn's stream or read between turns), and the record is used up."""
    inp = {"html": "<p>x</p>"}
    engine_prompts.note_gate_allow("s-plan", "toolu_plan", "mcp__display__display_ui",
                                   engine_prompts.input_digest(inp))
    engine_prompts.note_frame("s-plan", _plan_ask("r-plan", tool_input=inp), in_turn=in_turn)
    assert engine_prompts.withdrawn_before_registered("r-plan")
    await _answers(layer, 1)
    assert layer.answers == [("s-plan", "r-plan", True, inp)]
    assert not engine_prompts.owns("r-plan")
    assert engine_prompts._gate_allowed == {}


@pytest.mark.asyncio
async def test_exit_plan_modes_own_question_after_the_approval_is_answered(layer, plan_record):
    """ExitPlanMode's ask carries no reason and an empty input (the plan is in
    its file) and asks for the person: the plan review already answered it."""
    engine_prompts.note_gate_allow("s-exit", "toolu_exit", "ExitPlanMode",
                                   engine_prompts.input_digest({"plan": "# p"}))
    frame = {"type": "control_request", "request_id": "r-exit", "request": {
        "subtype": "can_use_tool", "tool_name": "ExitPlanMode", "input": {},
        "tool_use_id": "toolu_exit", "requires_user_interaction": True}}
    engine_prompts.note_frame("s-exit", frame, in_turn=True)
    await _answers(layer, 1)
    assert layer.answers == [("s-exit", "r-exit", True, {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"tool_use_id": "toolu_other"},
    {"tool": "mcp__memory-mcp__memory"},
    {"tool_input": {"html": "<p>changed</p>"}},
    {"decision_reason_type": "safetyCheck"},
    {"decision_reason_type": "sandboxOverride"},
    {"decision_reason_type": "asyncAgent"},
    {"decision_reason_type": "permissionPromptTool"},
    {"decision_reason_type": "someFutureReason"},
    {"decision_reason_type": "rule"},
    {"decision_reason_type": "hook"},
    {"decision_reason_type": "subcommandResults"},
    {"classifier_approvable": False},
    {"decision_reason_code": "org_ask_ceiling"},
])
async def test_an_ask_the_record_does_not_cover_stays_a_card(layer, plan_record, change):
    """Another call, another tool, a changed input, a safety check, a rule,
    another hook, a compound command or a managed ceiling: the request is a
    card as before, and an unused record stays for its own call."""
    engine_prompts.note_gate_allow("s-card", "toolu_plan", "mcp__display__display_ui",
                                   engine_prompts.input_digest({"html": "<p>x</p>"}))
    kwargs = dict(change)
    tool = kwargs.pop("tool", "mcp__display__display_ui")
    tool_input = kwargs.pop("tool_input", None)
    tool_use_id = kwargs.pop("tool_use_id", "toolu_plan")
    frame = _plan_ask("r-card", tool=tool, tool_input=tool_input, tool_use_id=tool_use_id)
    frame["request"].update(kwargs)
    engine_prompts.note_frame("s-card", frame, in_turn=True)
    await asyncio.sleep(0.05)
    assert layer.answers == []
    assert not engine_prompts.withdrawn_before_registered("r-card")


@pytest.mark.asyncio
async def test_a_record_is_used_once_and_expires(layer, plan_record):
    digest = engine_prompts.input_digest({"html": "<p>x</p>"})
    engine_prompts.note_gate_allow("s-once", "toolu_plan", "mcp__display__display_ui", digest)
    engine_prompts.note_frame("s-once", _plan_ask("r-1"), in_turn=True)
    await _answers(layer, 1)
    engine_prompts.note_frame("s-once", _plan_ask("r-2"), in_turn=True)
    await asyncio.sleep(0.05)
    assert [rid for _s, rid, _ok, _i in layer.answers] == ["r-1"]
    # A record past its life answers nothing, and the next write prunes it.
    engine_prompts.note_gate_allow("s-old", "toolu_old", "mcp__display__display_ui", digest)
    name, kept, _expiry, kind = engine_prompts._gate_allowed["s-old"]["toolu_old"]
    engine_prompts._gate_allowed["s-old"]["toolu_old"] = (name, kept, 0.0, kind)
    engine_prompts.note_frame("s-old", _plan_ask("r-3", tool_use_id="toolu_old"), in_turn=True)
    await asyncio.sleep(0.05)
    assert [rid for _s, rid, _ok, _i in layer.answers] == ["r-1"]
    engine_prompts.note_gate_allow("s-new", "toolu_new", "mcp__display__display_ui", digest)
    assert "s-old" not in engine_prompts._gate_allowed


@pytest.mark.asyncio
async def test_a_remote_record_waits_on_a_satellite_that_answers(layer, plan_record, monkeypatch):
    """A satellite older than the answer's shape keeps today's path: the ask
    is a card (refused at once there), never answered from the record."""
    from core.remote import remote_file_flow, satellite_connection
    monkeypatch.setattr(remote_file_flow, "remote_machine_id", lambda sid: "m-1")

    class _CM:
        def satellite_version(self, machine):
            return "0.5.139"
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _CM)
    engine_prompts.note_gate_allow("s-rem", "toolu_plan", "mcp__display__display_ui",
                                   engine_prompts.input_digest({"html": "<p>x</p>"}))
    engine_prompts.note_frame("s-rem", _plan_ask("r-rem"), in_turn=True)
    await asyncio.sleep(0.05)
    assert layer.answers == []
    assert not engine_prompts.withdrawn_before_registered("r-rem")


def _bypass_ask(rid: str, command: str = "cd out && rm -f *", tool_use_id: str = "toolu_rm",
                **extra) -> dict:
    """Claude Code's ask under bypass for an ``rm`` its safety check cannot
    clear (the shape probed on 2.1.296 and 2.1.312)."""
    request = {"subtype": "can_use_tool", "tool_name": "Bash",
               "input": {"command": command, "description": "clear"},
               "tool_use_id": tool_use_id, "decision_reason_type": "safetyCheck",
               "classifier_approvable": False, "suppress_always_allow_rule": True, **extra}
    return {"type": "control_request", "request_id": rid, "request": request}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["safetyCheck", "subcommandResults"])
async def test_the_clis_safety_ask_for_a_call_the_hook_alone_allowed_is_answered(
        layer, plan_record, reason):
    """Don't Ask, the unattended word, or the person's Allow in Ask: the hook
    is the authority, so the CLI's own ask for that call is answered from the
    record at once, never a card."""
    inp = {"command": "cd out && rm -f *", "description": "clear"}
    engine_prompts.note_gate_allow("s-hook", "toolu_rm", "Bash", engine_prompts.input_digest(inp),
                                   kind=engine_prompts.HOOK_RECORD)
    engine_prompts.note_frame("s-hook", _bypass_ask("r-rm", decision_reason_type=reason),
                              in_turn=True)
    assert engine_prompts.withdrawn_before_registered("r-rm")
    await _answers(layer, 1)
    assert layer.answers == [("s-hook", "r-rm", True, inp)]
    assert engine_prompts._gate_allowed == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"command": "rm -rf /"},
    {"tool_use_id": "toolu_other"},
    {"classifier_approvable": True},
    {"classifier_approvable": None},
    {"decision_reason_type": "mode"},
    {"decision_reason_type": "rule"},
    {"decision_reason_type": "hook"},
    {"decision_reason_code": "org_ask_ceiling"},
])
async def test_a_hook_record_does_not_cover_another_ask(layer, plan_record, change):
    """A changed command, another call, an ask the CLI's reviewer could take
    (Auto keeps its cards), a plan floor's reason, a rule, another hook or a
    managed ceiling: the request is a card as before."""
    inp = {"command": "cd out && rm -f *", "description": "clear"}
    engine_prompts.note_gate_allow("s-hk", "toolu_rm", "Bash", engine_prompts.input_digest(inp),
                                   kind=engine_prompts.HOOK_RECORD)
    kwargs = dict(change)
    frame = _bypass_ask("r-hk", command=kwargs.pop("command", inp["command"]),
                        tool_use_id=kwargs.pop("tool_use_id", "toolu_rm"))
    frame["request"].update(kwargs)
    if frame["request"].get("classifier_approvable") is None:
        frame["request"].pop("classifier_approvable")
    engine_prompts.note_frame("s-hk", frame, in_turn=True)
    await asyncio.sleep(0.05)
    assert layer.answers == []
    assert not engine_prompts.withdrawn_before_registered("r-hk")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,word", [("autoReview", "auto"), ("ask", "default"),
                                       ("plan", ""), ("dontAsk", "")])
async def test_exit_plan_modes_answer_moves_the_cli_to_the_chosen_mode(
        layer, plan_record, mode, word):
    """After Implement in Auto or in Ask the answer to ExitPlanMode's own
    question carries setMode, so a chat that started in Plan implements under
    Claude's own reviewer in Auto; any other mode keeps the plain allow."""
    sid = f"s-set-{mode}"
    session_state.set_session_mode(sid, mode)
    try:
        engine_prompts.note_gate_allow(sid, "toolu_exit", "ExitPlanMode",
                                       engine_prompts.input_digest({"plan": "# p"}))
        frame = {"type": "control_request", "request_id": f"r-set-{mode}", "request": {
            "subtype": "can_use_tool", "tool_name": "ExitPlanMode", "input": {},
            "tool_use_id": "toolu_exit", "requires_user_interaction": True}}
        engine_prompts.note_frame(sid, frame, in_turn=True)
        await _answers(layer, 1)
        decision = engine_prompts.answer_frame(f"r-set-{mode}", True, {})["response"]["response"]
        if word:
            assert decision["updatedPermissions"] == [
                {"type": "setMode", "mode": word, "destination": "session"}]
        else:
            assert "updatedPermissions" not in decision
        # Used once: a second answer for the same id carries nothing.
        assert "updatedPermissions" not in engine_prompts.answer_frame(
            f"r-set-{mode}", True, {})["response"]["response"]
    finally:
        session_state._session_modes.pop(sid, None)


# --- the hook's own cards between turns (offer_hook_card, end_hook_card) -----

@pytest.fixture
def hook_cards(layer, monkeypatch):
    from core.events import chat_writer
    rows: list[tuple[str, dict]] = []

    def _submit(chat_id, fn, *, label=""):
        rows.append((label, dict(fn.keywords)))
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut
    monkeypatch.setattr(chat_writer, "submit", _submit)
    monkeypatch.setattr(engine_prompts, "_ping_hook_card", lambda *a, **k: None)
    layer.rows = rows
    yield layer
    engine_prompts._hook_cards.clear()


def _ask_item(rid: str) -> dict:
    return {"event_type": "permission_prompt", "request_id": rid, "tool_name": "Bash",
            "tool_input": {"command": "rm -rf out"}}


@pytest.mark.asyncio
async def test_a_hook_card_with_no_pump_joins_the_resumes_read(hook_cards):
    queue = asyncio.Queue()
    assert engine_prompts.offer_hook_card(queue, _ask_item("r-h1"), session_id="s-h",
                                          chat_id="c-h") is True
    assert queue.empty()
    [p] = engine_prompts.pending_for_chat("c-h")
    assert engine_prompts.card_frame(p) == {
        "type": "permission_prompt", "chat_id": "c-h", "request_id": "r-h1",
        "tool_name": "Bash", "tool_input": {"command": "rm -rf out"}}
    assert hook_cards.broadcasts == [("c-h", engine_prompts.card_frame(p))]
    # Never answered through the layer: the hook answers its own call.
    assert not engine_prompts.owns("r-h1")
    engine_prompts.end_hook_card(queue, "r-h1", answered=True, approved=True,
                                 caller_gone=False, chat_id="c-h")
    assert engine_prompts.pending_for_chat("c-h") == []
    assert hook_cards.answers == []
    [(label, kw)] = hook_cards.rows
    assert label == "hook_card" and kw["event_type"] == "permission_prompt"
    assert json.loads(kw["event_data"])["resolved"] is True


@pytest.mark.asyncio
async def test_a_hook_plan_review_frame_and_its_row(hook_cards):
    queue = asyncio.Queue()
    engine_prompts.offer_hook_card(queue, {
        "event_type": "plan_review", "request_id": "r-hp", "plan": "# p",
        "tool_input": {"planFilePath": "/x/plan-z.md"}}, session_id="s-h", chat_id="c-h")
    [p] = engine_prompts.pending_for_chat("c-h")
    assert engine_prompts.card_frame(p)["type"] == "plan_review"
    assert engine_prompts.card_frame(p)["filename"] == "plan-z.md"
    engine_prompts.note_card_action("r-hp", "edit")
    engine_prompts.end_hook_card(queue, "r-hp", answered=True, approved=False,
                                 caller_gone=False, chat_id="c-h")
    row = json.loads([kw for lab, kw in hook_cards.rows if lab == "hook_card"][0]["event_data"])
    assert row["type"] == "plan_review" and row["action"] == "edit"


@pytest.mark.asyncio
async def test_a_hook_card_with_no_chat_goes_onto_its_queue(hook_cards):
    queue = asyncio.Queue()
    assert engine_prompts.offer_hook_card(queue, _ask_item("r-nc"), session_id="s-h",
                                          chat_id="") is False
    assert queue.get_nowait()["request_id"] == "r-nc"
    assert hook_cards.broadcasts == []


@pytest.mark.asyncio
async def test_an_unanswered_hook_card_is_retired_where_it_shows(hook_cards):
    queue = asyncio.Queue()
    engine_prompts.offer_hook_card(queue, _ask_item("r-rt"), session_id="s-h", chat_id="c-h")
    engine_prompts.end_hook_card(queue, "r-rt", answered=False, approved=False,
                                 caller_gone=False, chat_id="c-h")
    assert hook_cards.broadcasts[-1] == ("c-h", {"type": "prompt_retired", "chat_id": "c-h",
                                                 "request_id": "r-rt"})
    assert queue.empty()
    # A card a live pump showed: its retire goes through that pump's queue.
    from core.events import stream_pump

    class _Pump:
        perm_queue = queue
        is_done = False
    stream_pump._active_pumps["c-live"] = _Pump()
    try:
        engine_prompts.end_hook_card(queue, "r-pumped", answered=False, approved=False,
                                     caller_gone=True, chat_id="c-live")
        assert queue.get_nowait() == {"event_type": "prompt_retired", "request_id": "r-pumped",
                                      "caller_gone": True}
    finally:
        stream_pump._active_pumps.pop("c-live", None)
