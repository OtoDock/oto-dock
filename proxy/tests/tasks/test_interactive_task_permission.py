"""decide_tool_permission for an autonomous interactive TASK.

An interactive CHAT (a human is present) lets ``AskUserQuestion`` RUN so the TUI
renders native question cards. An interactive TASK (``client_type == "task"``, no
viewer) must NOT — otherwise the cards block on an answer nobody gives and the
unattended run hangs. The task falls through to the same deny-and-inform path as a
headless ``-p`` task. DB-free (the question branch returns before Pass-1; the
security context and the session's engine are stubbed).
"""
import pytest

from api.hooks import permission, routing


@pytest.fixture
def _stub(monkeypatch):
    """Stub the session lookups so decide_tool_permission runs DB-free, with the
    session treated as interactive. Returns the list the deny path pushes to."""
    monkeypatch.setattr(permission, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(routing, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    monkeypatch.setattr(permission, "get_session_security", lambda sid: object())
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: None)
    pushed = []

    class _Q:
        async def put(self, item):
            pushed.append(item)

    monkeypatch.setattr(permission, "get_permission_queue", lambda sid: _Q())
    return pushed


@pytest.mark.asyncio
async def test_askuserquestion_interactive_task_denies(monkeypatch, _stub):
    # client_type "task" → no viewer → deny + surface the question (don't let the
    # TUI block on cards nobody answers).
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "auto")
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "task")
    res = await permission.decide_tool_permission("s", "AskUserQuestion", {"questions": []})
    assert res["decision"] == "deny"
    assert "Do NOT re-ask" in res["reason"]
    assert _stub and _stub[0]["event_type"] == "question"


@pytest.mark.asyncio
async def test_askuserquestion_interactive_chat_allows(monkeypatch, _stub):
    # A human re-opening the run is client_type "dashboard" → let the tool RUN so
    # the TUI renders the native question cards inline.
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "dashboard")
    res = await permission.decide_tool_permission("s", "AskUserQuestion", {"questions": []})
    assert res["decision"] == "allow"
    assert _stub == []  # nothing surfaced to a dashboard queue — the TUI handles it
