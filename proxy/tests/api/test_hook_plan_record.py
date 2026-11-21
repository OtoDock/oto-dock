"""The permission authority in Plan: what the Plan stages allow, and the
record they leave for Claude Code's own ask that follows an allow (2.1.299+
asks again for a call its plan mode does not read as read-only).
"""

from __future__ import annotations

import uuid

import pytest

from api.hooks import permission
from auth.path_policy import SecurityContext
from core.session import session_state
from core.session.session_manager import get_layer_by_path


@pytest.fixture
def plan_session(temp_db):
    """A session in Plan. The Plan stage reads the saved plan rows, so the test database is clean."""
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "plan", SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False,
    ))
    yield sid
    session_state.cleanup_session_permission_state(sid)


def _bind(monkeypatch, path: str | None = "claude-code-cli", *, interactive: bool = False,
          client_type: str = "dashboard"):
    layer = get_layer_by_path(path) if path else None
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: layer)
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: interactive)
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: client_type)


@pytest.mark.asyncio
async def test_a_plan_file_write_is_allowed(plan_session, monkeypatch):
    _bind(monkeypatch)
    out = await permission.decide_tool_permission(
        plan_session, "Write", {"file_path": "/users/alice/.claude/plans/p.md", "content": "# p"})
    assert out["decision"] == "allow"


@pytest.mark.asyncio
async def test_a_write_that_climbs_out_of_the_plans_folder_is_refused(plan_session, monkeypatch):
    _bind(monkeypatch)
    out = await permission.decide_tool_permission(
        plan_session, "Write",
        {"file_path": "/users/alice/.claude/plans/../../workspace/x.md", "content": "x"})
    assert out["decision"] == "deny"


def _home_plan(where: str, session_id: str) -> str:
    """The path Claude Code names its plan by in a session that runs from a home of its own: its
    ``CLAUDE_CONFIG_DIR`` is ``<store>/own/<session id>`` (the sandbox's path on a compute machine,
    the host's on a paired computer, with backslashes on Windows) and it writes
    ``<that>/plans/<name>.md``, which the home's ``plans`` link lands in the store's."""
    return {
        "compute": f"/users/alice/.claude/own/{session_id}/plans/humming-gosling.md",
        "paired": f"/home/bob/.oto-dock/agents/pa/users/alice/.claude/own/{session_id}/plans/humming-gosling.md",
        "windows": (f"C:\\Users\\bob\\.oto-dock\\agents\\pa\\users\\alice\\.claude\\own\\{session_id}"
                    f"\\plans\\humming-gosling.md"),
    }[where]


def _placed_plan_session(where: str) -> str:
    """A session in Plan as its machine sees it: the compute machine's sandbox, a paired computer's
    agents folder, a Windows one's."""
    from core import placement
    from storage import remote_store
    agents_dir = {"paired": "/home/bob/.oto-dock/agents", "windows": "C:/Users/bob/.oto-dock/agents"}.get(where)
    kw = {}
    if agents_dir:
        remote_store.create_remote_machine(machine_id="m-1", name="m1", registered_by="alice", pairing_scope="user")
        kw["placement"] = placement.PlacementCapabilities(
            kind=placement.KIND_USER_REMOTE, label="m", agents_dir=agents_dir, machine_id="m-1",
            home_dir=agents_dir.rsplit("/", 2)[0])
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "plan", SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False, **kw))
    return sid


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["compute", "paired", "windows"])
async def test_a_plan_file_in_the_sessions_own_home_is_allowed(temp_db, monkeypatch, where):
    """In Plan, as its machine's session sees it."""
    sid = _placed_plan_session(where)
    try:
        _bind(monkeypatch)
        out = await permission.decide_tool_permission(
            sid, "Write", {"file_path": _home_plan(where, sid), "content": "# p"})
    finally:
        session_state.cleanup_session_permission_state(sid)
    assert out["decision"] == "allow"


@pytest.mark.asyncio
async def test_a_plan_in_a_codex_folder_is_refused_to_a_claude_session(plan_session, monkeypatch):
    """Codex writes no plan file: a Claude session in Plan may not write a Codex folder's plans."""
    _bind(monkeypatch)
    out = await permission.decide_tool_permission(
        plan_session, "Write", {"file_path": "/workspace/.codex/plans/x.md", "content": "# p"})
    assert out["decision"] == "deny"


@pytest.mark.asyncio
async def test_a_plan_file_in_another_sessions_home_is_refused(plan_session, monkeypatch):
    _bind(monkeypatch)
    out = await permission.decide_tool_permission(
        plan_session, "Write", {"file_path": _home_plan("compute", str(uuid.uuid4())), "content": "# p"})
    assert out["decision"] == "deny"


@pytest.fixture
def records():
    from core.session import engine_prompts
    engine_prompts._gate_allowed.clear()
    yield engine_prompts._gate_allowed
    engine_prompts._gate_allowed.clear()


@pytest.mark.asyncio
async def test_a_read_command_allowed_in_plan_is_recorded_for_the_clis_ask(
        plan_session, monkeypatch, records):
    from core.session import engine_prompts
    _bind(monkeypatch)
    inp = {"command": "ls -la", "description": "list"}
    out = await permission.decide_tool_permission(plan_session, "Bash", inp, tool_use_id="toolu_ls")
    assert out["decision"] == "allow"
    assert records[plan_session]["toolu_ls"][:2] == ("Bash", engine_prompts.input_digest(inp))


def _sensitive_file_ask(inp: dict, blocked_path: str) -> dict:
    """Claude Code 2.1.322's ask for a plan its session writes through its home's ``plans`` link (its
    fields as read on T1's compute machine): the link's target reads as a sensitive file."""
    return {"subtype": "can_use_tool", "tool_use_id": "toolu_plan", "tool_name": "Write", "input": inp,
            "decision_reason_type": "safetyCheck", "classifier_approvable": True, "blocked_path": blocked_path,
            "decision_reason": f"Claude requested permissions to edit {inp['file_path']} which is a sensitive file."}


@pytest.mark.asyncio
@pytest.mark.parametrize("target,answered", [
    ("/users/alice/.claude/plans/humming-gosling.md", True),
    ("/users/alice/.claude/plans/another-plan.md", False),
    ("/users/alice/.claude/settings.json", False),
    ("/workspace/repo/.claude/plans/humming-gosling.md", False),
], ids=["the-store-plan", "another-plan", "a-settings-file", "a-plan-of-that-name-elsewhere"])
async def test_a_plan_file_in_the_sessions_own_home_answers_the_clis_sensitive_file_ask(
        plan_session, monkeypatch, records, target, answered):
    """The hook's allow of the plan is recorded; the engine's ask that follows is answered only when
    the link's target is this session's plan of the same name, else it stays the person's card."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    inp = {"file_path": _home_plan("compute", plan_session), "content": "# p"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    assert engine_prompts._take_gate_allow(plan_session, _sensitive_file_ask(inp, target)) is answered


@pytest.mark.asyncio
@pytest.mark.parametrize("where,store_plan", [
    ("compute", "/users/alice/.claude/plans/humming-gosling.md"),
    ("paired", "/home/bob/.oto-dock/agents/pa/users/alice/.claude/plans/humming-gosling.md"),
    ("windows", "C:\\Users\\bob\\.oto-dock\\agents\\pa\\users\\alice\\.claude\\plans\\humming-gosling.md"),
])
async def test_the_sensitive_file_ask_is_answered_for_the_store_plan_its_home_links_to(
        temp_db, monkeypatch, records, where, store_plan):
    """The target is the written path with the home's part taken out, on every placement."""
    from core.session import engine_prompts
    sid = _placed_plan_session(where)
    try:
        _bind(monkeypatch)
        inp = {"file_path": _home_plan(where, sid), "content": "# p"}
        out = await permission.decide_tool_permission(sid, "Write", inp, tool_use_id="toolu_plan")
        answered = engine_prompts._take_gate_allow(sid, _sensitive_file_ask(inp, store_plan))
    finally:
        session_state.cleanup_session_permission_state(sid)
    assert out["decision"] == "allow"
    assert answered


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["home", "/workspace/proj/.claude/plans/x.md"])
@pytest.mark.parametrize("reason", ["mode", "other", "workingDir"])
async def test_a_plan_files_record_answers_no_plan_mode_ask(plan_session, monkeypatch, records, path, reason):
    """A plan file's record answers the engine's sensitive-file question and nothing else: its plan mode's
    own question for the same call stays the person's card, as on master, even naming the very target a
    sensitive-file ask would name."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    home = path == "home"
    inp = {"file_path": _home_plan("compute", plan_session) if home else path, "content": "# p"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    ask = {"subtype": "can_use_tool", "tool_use_id": "toolu_plan", "tool_name": "Write", "input": inp,
           "decision_reason_type": reason,
           "blocked_path": "/users/alice/.claude/plans/humming-gosling.md" if home else path}
    assert not engine_prompts._take_gate_allow(plan_session, ask)


@pytest.mark.asyncio
async def test_a_changed_input_keeps_the_sensitive_file_card(plan_session, monkeypatch, records):
    """The record answers only the input the hook judged: the same call and target with other content
    stays a card."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    inp = {"file_path": _home_plan("compute", plan_session), "content": "# p"}
    await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    ask = _sensitive_file_ask({**inp, "content": "# another plan"}, "/users/alice/.claude/plans/humming-gosling.md")
    assert not engine_prompts._take_gate_allow(plan_session, ask)


@pytest.mark.asyncio
async def test_a_plan_record_never_answers_a_sensitive_file_ask_for_another_call(plan_session, monkeypatch, records):
    """A read the Plan stage allowed keeps its own record's rules: a safety ask is no plan floor ask."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    inp = {"command": "ls -la", "description": "list"}
    await permission.decide_tool_permission(plan_session, "Bash", inp, tool_use_id="toolu_plan")
    ask = {**_sensitive_file_ask({"file_path": "x", **inp}, "/users/alice/.claude/plans/x.md"),
           "tool_name": "Bash", "input": inp}
    assert not engine_prompts._take_gate_allow(plan_session, ask)


@pytest.mark.asyncio
async def test_exit_plan_mode_allowed_for_a_lane_is_recorded(plan_session, monkeypatch, records):
    """ExitPlanMode outside a dashboard chat is allowed at once; the CLI then
    asks for it over stdio, which the record answers."""
    _bind(monkeypatch, client_type="task")
    out = await permission.decide_tool_permission(
        plan_session, "ExitPlanMode", {"plan": "# p"}, tool_use_id="toolu_exit")
    assert out == {"decision": "allow"}
    assert "toolu_exit" in records[plan_session]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["terminal", "codex", "no_id", "denied"])
async def test_nothing_is_recorded_where_no_ask_follows(plan_session, monkeypatch, records, case):
    """A terminal asks in its own dialog, Codex raises no stdio ask, an
    older gate script sends no id, a refused call is never asked for."""
    _bind(monkeypatch, "codex-cli" if case == "codex" else "claude-code-cli",
          interactive=case == "terminal")
    command = "touch made.txt" if case == "denied" else "ls -la"
    out = await permission.decide_tool_permission(
        plan_session, "Bash", {"command": command},
        tool_use_id="" if case == "no_id" else "toolu_x")
    assert out["decision"] == ("deny" if case == "denied" else "allow")
    assert records == {}


@pytest.mark.asyncio
async def test_a_mode_word_in_the_request_never_records_outside_plan(monkeypatch, records):
    """The agent's shell holds its session's token and could post the word
    "plan" itself: the record follows the gate's own stages and mode, never
    the request's words (Don't Ask records the hook's kind, which answers no
    plan floor's ask)."""
    from core.session import engine_prompts
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "dontAsk", SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False,
    ))
    try:
        _bind(monkeypatch)
        out = await permission.decide_tool_permission(
            sid, "Bash", {"command": "ls -la"}, live_permission_mode="plan", tool_use_id="toolu_f")
        assert out["decision"] == "allow"
        assert [v[3] for v in records[sid].values()] == [engine_prompts.HOOK_RECORD]
    finally:
        session_state.cleanup_session_permission_state(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,allowed", [
    ("git --version", True),
    ("git version", True),
    ("git status", False),
    ("git --version\ntouch x", False),
    ("git push", False),
])
async def test_plan_runs_gits_version_check_alone(plan_session, monkeypatch, records, command, allowed):
    _bind(monkeypatch)
    out = await permission.decide_tool_permission(
        plan_session, "Bash", {"command": command}, tool_use_id="toolu_git")
    assert out["decision"] == ("allow" if allowed else "deny")
    assert ("toolu_git" in records.get(plan_session, {})) is allowed


@pytest.mark.asyncio
async def test_a_plan_refusal_says_why(plan_session, monkeypatch):
    """The model reads the reason; a bare deny reached it as "Denied by user"."""
    _bind(monkeypatch)
    out = await permission.decide_tool_permission(plan_session, "Bash", {"command": "npm run build"})
    assert out == {"decision": "deny", "reason": permission._PLAN_REFUSAL}
    assert "Plan mode only reads" in out["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["ask", "defer"])
async def test_a_hook_ask_or_defer_records_nothing(plan_session, monkeypatch, records, decision):
    """Only an allow leaves a record: an ``ask`` (the terminal's prompt) or a
    ``defer`` (the engine's own check decides) never answers the CLI's ask,
    even when a Plan stage had marked the call."""
    _bind(monkeypatch)

    async def _decided(session_id, tool_name, tool_input, pass1_out, live, *, wait_s):
        pass1_out[permission._GATE_RECORD] = "plan"
        return {"decision": decision}

    monkeypatch.setattr(permission, "_decide_tool_permission", _decided)
    out = await permission.decide_tool_permission(
        plan_session, "Bash", {"command": "ls -la"}, tool_use_id="toolu_ask")
    assert out == {"decision": decision}
    assert records == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,mode,expected", [
    # Implement: the mode moves first, the call is approved and recorded.
    ("implement", "autoReview", "allow"),
    # Edit: the mode stays in Plan, the model waits for the feedback.
    ("edit", "plan", "modify the plan"),
    # Cancel: the mode moved back first, the call is denied and the model stops.
    ("cancel", "ask", permission.PLAN_CANCELLED_REASON),
])
async def test_the_plan_reviews_answer_reaches_exit_plan_mode(
        plan_session, monkeypatch, records, answer, mode, expected):
    import asyncio
    _bind(monkeypatch)
    task = asyncio.create_task(permission.decide_tool_permission(
        plan_session, "ExitPlanMode", {"plan": "# p"}, tool_use_id="toolu_exit"))
    queue = session_state.get_permission_queue(plan_session)
    card = await asyncio.wait_for(queue.get(), 5)
    assert card["event_type"] == "plan_review"
    # The dashboard sets the mode before it resolves the wait.
    session_state.set_session_mode(plan_session, mode)
    assert session_state.resolve_permission(card["request_id"], answer == "implement")
    out = await asyncio.wait_for(task, 5)
    if expected == "allow":
        assert out == {"decision": "allow"} and "toolu_exit" in records[plan_session]
    else:
        assert out["decision"] == "deny" and expected in out["reason"]
        assert plan_session not in records


@pytest.fixture
def mode_session():
    """A session in a given mode (``mode_session(mode)``)."""
    made: list[str] = []

    def _make(mode: str) -> str:
        sid = str(uuid.uuid4())
        session_state.register_session_state(sid, mode, SecurityContext(
            role="manager", username="alice", agent="pa", is_admin_agent=False,
        ))
        made.append(sid)
        return sid
    yield _make
    for sid in made:
        session_state.cleanup_session_permission_state(sid)


_RM = {"command": "rm -f /users/alice/workspace/out/old.txt", "description": "clear"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,client", [("dontAsk", "dashboard"), ("auto", "task"),
                                         ("auto", "dashboard")])
async def test_where_the_hook_alone_decides_its_allow_is_recorded(
        mode_session, monkeypatch, records, mode, client):
    """Don't Ask and the unattended word: the hook is the only authority, so
    the CLI's own ask for the call (its safety check under bypass) is
    answered from the record."""
    from core.session import engine_prompts
    sid = mode_session(mode)
    _bind(monkeypatch, client_type=client)
    out = await permission.decide_tool_permission(sid, "Bash", _RM, tool_use_id="toolu_rm")
    assert out["decision"] == "allow"
    assert records[sid]["toolu_rm"][0] == "Bash"
    assert records[sid]["toolu_rm"][3] == engine_prompts.HOOK_RECORD


@pytest.mark.asyncio
async def test_in_ask_only_the_persons_allow_is_recorded(mode_session, monkeypatch, records):
    """Ask: the person's Allow on the hook's card is recorded (the CLI's own
    ask for the same call is answered); a refusal, and a read the gate
    allows with no card, record nothing."""
    import asyncio

    from core.session import engine_prompts
    sid = mode_session("ask")
    _bind(monkeypatch)
    queue = session_state.get_permission_queue(sid)
    for approve, tool_use_id in ((True, "toolu_yes"), (False, "toolu_no")):
        task = asyncio.create_task(permission.decide_tool_permission(
            sid, "Bash", _RM, tool_use_id=tool_use_id))
        card = await asyncio.wait_for(queue.get(), 5)
        assert card["event_type"] == "permission_prompt"
        assert session_state.resolve_permission(card["request_id"], approve)
        out = await asyncio.wait_for(task, 5)
        assert out["decision"] == ("allow" if approve else "deny")
    assert list(records[sid]) == ["toolu_yes"]
    assert records[sid]["toolu_yes"][3] == engine_prompts.HOOK_RECORD
    out = await permission.decide_tool_permission(
        sid, "Bash", {"command": "ls -la"}, tool_use_id="toolu_ls")
    assert out == {"decision": "allow"} and "toolu_ls" not in records[sid]


@pytest.mark.asyncio
async def test_auto_records_nothing_for_the_reviewers_asks(mode_session, monkeypatch, records):
    """Auto keeps its cards: the CLI's reviewer asks, the record answers none."""
    sid = mode_session("autoReview")
    _bind(monkeypatch)
    out = await permission.decide_tool_permission(
        sid, "Write", {"file_path": "/users/alice/workspace/a.md", "content": "a"},
        tool_use_id="toolu_w")
    assert out["decision"] == "allow"
    assert sid not in records


@pytest.mark.parametrize("where", ["compute", "paired", "windows"])
def test_the_pump_names_the_plan_its_session_wrote_in_its_own_home(where):
    """The plan review card reads the plan by the file name the pump took from the session's write: a plan
    written in the session's own home is named, one in another session's home is not."""
    import asyncio
    from types import SimpleNamespace

    from core.events.pump_event_handlers import PumpEventHandlers

    async def _forward(*_a, **_k):
        return None

    def _pump(sid):
        return SimpleNamespace(session_id=sid, _active_tools={}, _plan_filename="", _forward_event=_forward)
    sid = str(uuid.uuid4())
    own, other = _pump(sid), _pump(str(uuid.uuid4()))
    for pump in (own, other):
        asyncio.run(PumpEventHandlers._on_tool_input(pump, {"name": "Write", "file_path": _home_plan(where, sid)}, None))
    assert own._plan_filename == "humming-gosling.md" and other._plan_filename == ""


def test_one_owner_knows_a_plan_file_of_this_session():
    from core.sandbox.session_config_dir import plan_file_name
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    yes = ["/users/alice/.claude/plans/a.md",
           f"/users/alice/.claude/own/{sid}/plans/a.md",
           f"C:\\Users\\b\\agents\\pa\\workspace\\.claude\\own\\{sid}\\plans\\a.md"]
    no = [f"/users/alice/.claude/own/{other}/plans/a.md",                # another session's home
          "/workspace/.codex/plans/a.md", f"/users/alice/.codex/own/{sid}/plans/a.md",  # Codex writes no plan file
          f"/users/alice/.claude/own/{sid}/plans/../../../workspace/a.md",  # climbs out
          "/users/alice/.claude/plans/a.txt", "/users/alice/.claude/plans/sub/a.md",
          f"/users/alice/notes/own/{sid}/plans/a.md", "/users/alice/plans/a.md", ""]
    assert [plan_file_name(p, sid) for p in yes] == ["a.md"] * 3
    assert [plan_file_name(p, sid) for p in no] == [""] * len(no)
    assert plan_file_name(f"/users/alice/.claude/own/{sid}/plans/a.md", "") == ""


@pytest.mark.asyncio
async def test_a_plan_another_running_session_wrote_keeps_the_card(plan_session, monkeypatch, records):
    """The scope's plans folder is every session's of the scope, and the CLI admits only its own plan by
    name: a write of the plan another running session wrote, reached through this session's own home, is
    allowed as before and never recorded, so the CLI's ask for it stays the person's card."""
    from types import SimpleNamespace

    from core.events import stream_pump
    from core.session import engine_prompts
    _bind(monkeypatch)
    monkeypatch.setitem(stream_pump._active_pumps, "chat-other", SimpleNamespace(
        session_id=str(uuid.uuid4()), _plan_filename="another-chats-plan.md"))
    inp = {"file_path": f"/users/alice/.claude/own/{plan_session}/plans/another-chats-plan.md",
           "content": "# replaced"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    assert plan_session not in records
    assert not engine_prompts._take_gate_allow(
        plan_session, _sensitive_file_ask(inp, "/users/alice/.claude/plans/another-chats-plan.md"))


@pytest.mark.asyncio
async def test_a_sessions_own_running_turn_names_no_other_sessions_plan(plan_session, monkeypatch, records):
    """The pump of the session's own turn has the plan's name before the hook judges the write."""
    from types import SimpleNamespace

    from core.events import stream_pump
    from core.session import engine_prompts
    _bind(monkeypatch)
    monkeypatch.setitem(stream_pump._active_pumps, "chat-own", SimpleNamespace(
        session_id=plan_session, _plan_filename="humming-gosling.md"))
    inp = {"file_path": _home_plan("compute", plan_session), "content": "# p"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    assert engine_prompts._take_gate_allow(
        plan_session, _sensitive_file_ask(inp, "/users/alice/.claude/plans/humming-gosling.md"))


def _saved_plan(temp_db, chat_id: str, name: str, session_id: str = "") -> None:
    """A chat that offered a plan for review: its saved plan row, and the session it runs on."""
    temp_db.create_chat(chat_id, "user-admin", "pa")
    if session_id:
        temp_db.update_chat(chat_id, session_id=session_id)
    if name:
        temp_db.add_chat_plan(chat_id, name, "# the reviewed plan")


@pytest.mark.asyncio
@pytest.mark.parametrize("own_chat", [True, False], ids=["this-chat-known", "no-chat-in-hand"])
async def test_a_plan_another_chat_saved_keeps_the_card_with_no_turn_running(
        temp_db, plan_session, monkeypatch, records, own_chat):
    """A finished chat's plan, or one after a restart: no pump holds its name, its saved row does. A write of
    it is allowed as before and not recorded, so the engine's question stays the person's card. With no chat
    in hand, any saved row of the name is another's."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    if own_chat:
        _saved_plan(temp_db, "chat-mine", "", plan_session)
    _saved_plan(temp_db, "chat-finished", "another-chats-plan.md")
    inp = {"file_path": f"/users/alice/.claude/own/{plan_session}/plans/another-chats-plan.md",
           "content": "# replaced"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    assert plan_session not in records
    assert not engine_prompts._take_gate_allow(
        plan_session, _sensitive_file_ask(inp, "/users/alice/.claude/plans/another-chats-plan.md"))


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", ["this-chats-plan", "nobody-saved-it"])
async def test_this_chats_own_saved_plan_and_a_new_name_stay_silent(
        temp_db, plan_session, monkeypatch, records, saved):
    """A chat that resumes and plans again writes the name its own row holds: no card. A name no chat saved
    is new: no card either."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    _saved_plan(temp_db, "chat-mine", "humming-gosling.md" if saved == "this-chats-plan" else "", plan_session)
    inp = {"file_path": _home_plan("compute", plan_session), "content": "# p"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    assert engine_prompts._take_gate_allow(
        plan_session, _sensitive_file_ask(inp, "/users/alice/.claude/plans/humming-gosling.md"))


@pytest.mark.asyncio
async def test_a_failed_read_of_the_saved_plans_keeps_the_card(plan_session, monkeypatch, records):
    """When the store cannot say whose plan a name is, the write is not recorded: the safe side is a card."""
    import psycopg

    from storage import database as task_store
    _bind(monkeypatch)

    def _down(name, chat_id):
        raise psycopg.OperationalError("the store is away")
    monkeypatch.setattr(task_store, "chat_plan_held_elsewhere", _down)
    inp = {"file_path": _home_plan("compute", plan_session), "content": "# p"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    assert plan_session not in records


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["home", "/users/alice/.claude/plans/humming-gosling.md",
                                  "/workspace/repo/.claude/plans/x.md"])
async def test_a_blocked_path_that_is_the_written_path_keeps_the_card(plan_session, monkeypatch, records, path):
    """Claude Code names a blocked path only when the write lands elsewhere than the path it names: an
    ask whose blocked path is the written path itself is not the home's link, and stays a card."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    inp = {"file_path": _home_plan("compute", plan_session) if path == "home" else path, "content": "# p"}
    out = await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    assert out["decision"] == "allow"
    assert not engine_prompts._take_gate_allow(plan_session, _sensitive_file_ask(inp, inp["file_path"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("approvable", [False, None])
async def test_a_person_only_sensitive_file_ask_keeps_the_card(plan_session, monkeypatch, records, approvable):
    """The record answers the ask as read on T1 (``classifier_approvable`` true): one the CLI marks for
    a person alone (a landing outside the working folders, a path it calls suspicious) stays a card."""
    from core.session import engine_prompts
    _bind(monkeypatch)
    inp = {"file_path": _home_plan("compute", plan_session), "content": "# p"}
    await permission.decide_tool_permission(plan_session, "Write", inp, tool_use_id="toolu_plan")
    ask = _sensitive_file_ask(inp, "/users/alice/.claude/plans/humming-gosling.md")
    if approvable is None:
        del ask["classifier_approvable"]
    else:
        ask["classifier_approvable"] = approvable
    assert not engine_prompts._take_gate_allow(plan_session, ask)
