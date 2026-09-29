"""The permission authority's question branch (core-seams phase 2), keyed on
the ``question`` role and the engine's ``question_tool_holds_turn``:

* an interactive TUI with a human present parks the turn and lets the
  tool run (both engines);
* an engine whose question tool holds the turn open for the platform's
  answers (Codex, headless) is allowed;
* everything else — Claude headless, an autonomous interactive task on
  either engine, a session whose engine is unknown — gets the card and a
  deny-and-inform that names the tool.

Also: the security-context fail-closed deny now precedes the branch.
"""

from __future__ import annotations

import uuid

import pytest

from api.hooks import permission
from auth.path_policy import SecurityContext
from core.session import session_state
from core.session.session_manager import get_layer_by_path


@pytest.fixture
def session():
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "default", SecurityContext(
        role="manager", username="alice", agent="pa", is_admin_agent=False,
    ))
    yield sid
    session_state.cleanup_session_permission_state(sid)


def _bind(monkeypatch, path: str | None, *, interactive: bool = False, client_type: str = "dashboard"):
    layer = get_layer_by_path(path) if path else None
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: layer)
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: interactive)
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: client_type)
    parked: list = []
    monkeypatch.setattr(permission, "_park_interactive_on_dialog", lambda sid, name: parked.append(name))
    return parked


@pytest.mark.asyncio
async def test_claude_headless_gets_the_card_and_a_deny_that_names_the_tool(session, monkeypatch):
    _bind(monkeypatch, "claude-code-cli")
    out = await permission.decide_tool_permission(session, "AskUserQuestion", {"questions": []})
    assert out["decision"] == "deny" and "AskUserQuestion" in out["reason"]
    queued = session_state.get_permission_queue(session).get_nowait()
    assert queued["event_type"] == "question" and queued["tool_name"] == "AskUserQuestion"


@pytest.mark.asyncio
async def test_codex_headless_is_allowed_because_its_tool_holds_the_turn(session, monkeypatch):
    _bind(monkeypatch, "codex-cli")
    out = await permission.decide_tool_permission(session, "request_user_input", {"questions": []})
    assert out == {"decision": "allow"}
    assert session_state.get_permission_queue(session).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,tool", [("claude-code-cli", "AskUserQuestion"), ("codex-cli", "request_user_input")])
async def test_an_interactive_human_parks_and_runs_the_tool(session, monkeypatch, path, tool):
    parked = _bind(monkeypatch, path, interactive=True)
    out = await permission.decide_tool_permission(session, tool, {"questions": []})
    assert out == {"decision": "allow"} and parked == [tool]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,tool", [("claude-code-cli", "AskUserQuestion"), ("codex-cli", "request_user_input")])
async def test_an_interactive_task_gets_the_card_and_a_deny_on_either_engine(session, monkeypatch, path, tool):
    parked = _bind(monkeypatch, path, interactive=True, client_type="task")
    out = await permission.decide_tool_permission(session, tool, {"questions": []})
    assert out["decision"] == "deny" and tool in out["reason"] and parked == []
    assert session_state.get_permission_queue(session).get_nowait()["event_type"] == "question"


@pytest.mark.asyncio
async def test_an_unknown_engine_fails_closed(session, monkeypatch):
    _bind(monkeypatch, None)
    out = await permission.decide_tool_permission(session, "request_user_input", {"questions": []})
    assert out["decision"] == "deny"


@pytest.mark.asyncio
async def test_a_dead_session_is_denied_before_anything_is_enqueued(monkeypatch):
    sid = str(uuid.uuid4())
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda s: None)
    out = await permission.decide_tool_permission(sid, "AskUserQuestion", {"questions": []})
    assert out["decision"] == "deny" and "no longer active" in out["reason"]
    assert session_state.get_permission_queue(sid).empty()
    out = await permission.decide_tool_permission(sid, "ExitPlanMode", {"plan": "p"})
    assert out["decision"] == "deny"
    session_state.cleanup_session_permission_state(sid)
