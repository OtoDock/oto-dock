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
def plan_session():
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
