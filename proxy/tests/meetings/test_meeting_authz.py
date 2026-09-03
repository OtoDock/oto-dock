"""Meeting creation authorization — a real-human VIEWER must not be able to
convene a meeting that makes a Shared-only agent (or any agent-scope meeting)
run with agent-scope MANAGER capability (shared ``/workspace/`` RW).

This mirrors ``_enforce_task_scope`` for tasks (api/tasks/tasks.py): agent-scope
participation requires editor+. A participant runs agent scope when the meeting
is agent-scoped OR the agent is Shared-only. User-scope participants run as the
caller's own per-agent role (self-limiting), so read access suffices.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/meetings/test_meeting_authz.py -q
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.meetings.meetings import create_meeting, CreateMeetingRequest
from auth.providers import UserContext
from storage import agent_store


def _user(role_map: dict[str, str], sub: str = "u-1") -> UserContext:
    return UserContext(
        sub=sub, email=f"{sub}@x.test", name="U", role="member",
        agents=list(role_map.keys()), agent_roles=dict(role_map),
    )


def _create(user, agents, scope="user", x_agent_name=None):
    req = CreateMeetingRequest(
        topic="t", agents=agents, scope=scope,
        parent_session_id="", parent_run_id="",
    )
    return asyncio.run(create_meeting(req, user=user, x_agent_name=x_agent_name))


def test_viewer_cannot_include_shared_only_agent(temp_db):
    agent_store.create_agent("ops", "Ops", collaborative=True)
    agent_store.create_agent("caller", "Caller", default_scope="agent", collaborative=False)
    # Manager on the moderator (ops), only VIEWER on the Shared-only caller.
    u = _user({"ops": "manager", "caller": "viewer"})
    with pytest.raises(HTTPException) as ei:
        _create(u, ["ops", "caller"], scope="user", x_agent_name="ops")
    assert ei.value.status_code == 403
    assert "caller" in ei.value.detail


def test_editor_can_include_shared_only_agent(temp_db):
    agent_store.create_agent("ops", "Ops", collaborative=True)
    agent_store.create_agent("caller", "Caller", default_scope="agent", collaborative=False)
    u = _user({"ops": "manager", "caller": "editor"})
    out = _create(u, ["ops", "caller"], scope="user", x_agent_name="ops")
    assert out["status"] == "pending"


def test_viewer_user_scope_collaborative_meeting_ok(temp_db):
    # A viewer CAN convene a user-scope meeting of collaborative agents — they
    # run as the viewer's own read-only per-agent role, so it is self-limiting.
    agent_store.create_agent("a1", "A1", collaborative=True)
    agent_store.create_agent("a2", "A2", collaborative=True)
    u = _user({"a1": "viewer", "a2": "viewer"})
    out = _create(u, ["a1", "a2"], scope="user", x_agent_name="a1")
    assert out["status"] == "pending"


def test_viewer_cannot_create_agent_scope_meeting(temp_db):
    # Agent-scope meeting → every participant runs agent-scope → editor+ required.
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("a2", "A2", default_scope="agent", collaborative=True)
    u = _user({"a1": "viewer", "a2": "viewer"})
    with pytest.raises(HTTPException) as ei:
        _create(u, ["a1", "a2"], scope="agent", x_agent_name="a1")
    assert ei.value.status_code == 403


def _nouser(agent: str) -> UserContext:
    """No-user AGENT_SESSION JWT (agent-scope task / trigger / phone)."""
    return UserContext(
        sub="session:s-1", email="", name="", role="agent",
        is_api_key=True, session_id="s-1", agent=agent,
    )


def test_nouser_session_clamped_to_roster(temp_db):
    # C4 (2026-08-14): a no-user session used to skip ALL per-participant
    # checks — it could convene any agent that exists. Its reach is now its
    # agent's delegation roster ∪ itself (autonomy needs explicit wiring).
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("a2", "A2", default_scope="agent", collaborative=True)
    agent_store.create_agent("a3", "A3", default_scope="agent", collaborative=True)
    agent_store.set_delegation_targets("a1", ["a2"])
    # Wired participant: allowed.
    out = _create(_nouser("a1"), ["a1", "a2"], scope="agent", x_agent_name="a1")
    assert out["status"] == "pending"
    # Unwired participant: 403, naming the agent.
    with pytest.raises(HTTPException) as ei:
        _create(_nouser("a1"), ["a1", "a3"], scope="agent", x_agent_name="a1")
    assert ei.value.status_code == 403
    assert "a3" in ei.value.detail


def test_nouser_clamp_ignores_client_header(temp_db):
    # The roster is read from the TOKEN's agent — a spoofed X-Agent-Name
    # naming a well-wired agent must not widen the reach.
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("hub", "Hub", default_scope="agent", collaborative=True)
    agent_store.create_agent("a3", "A3", default_scope="agent", collaborative=True)
    agent_store.set_delegation_targets("hub", ["a3", "a1"])
    with pytest.raises(HTTPException) as ei:
        _create(_nouser("a1"), ["a1", "a3"], scope="agent", x_agent_name="hub")
    assert ei.value.status_code == 403


def test_master_key_unclamped(temp_db):
    agent_store.create_agent("a1", "A1", default_scope="agent", collaborative=True)
    agent_store.create_agent("a2", "A2", default_scope="agent", collaborative=True)
    svc = UserContext(sub="api-key", email="", name="", role="admin",
                      is_api_key=True)
    out = _create(svc, ["a1", "a2"], scope="agent", x_agent_name="a1")
    assert out["status"] == "pending"
