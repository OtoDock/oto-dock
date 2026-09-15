"""Meeting platform kill-switch + per-creator participant cap.

Both live on the CREATE endpoint — meetings-mcp reaches sessions via the
extra_mcps force-inject (bypasses mcp_state at config build), so create time
is the only real gate. A missing mcp_state row means enabled; only an
explicit admin disable blocks.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.meetings.meetings import create_meeting, CreateMeetingRequest
from auth.providers import UserContext
from storage.agents import agent_store
from storage.mcp import mcp_store
from storage import database as task_store


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


@pytest.fixture
def two_agents(temp_db):
    agent_store.create_agent("a1", "A1", collaborative=True)
    agent_store.create_agent("a2", "A2", collaborative=True)
    return _user({"a1": "editor", "a2": "editor"})


def test_missing_state_row_allows(two_agents):
    assert _create(two_agents, ["a1", "a2"], x_agent_name="a1")["status"] == "pending"


def test_kill_switch_blocks_creation(two_agents):
    mcp_store.set_mcp_enabled("meetings-mcp", False)
    with pytest.raises(HTTPException) as ei:
        _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    assert ei.value.status_code == 403
    assert "disabled" in ei.value.detail


def test_participant_cap_counts_active_meetings(two_agents):
    # Default cap 4: one 2-agent meeting fits, a second fills it, a third
    # exceeds (4 active + 2 new > 4).
    assert _create(two_agents, ["a1", "a2"], x_agent_name="a1")["status"] == "pending"
    assert _create(two_agents, ["a1", "a2"], x_agent_name="a1")["status"] == "pending"
    with pytest.raises(HTTPException) as ei:
        _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    assert ei.value.status_code == 403
    assert "Meeting limit reached" in ei.value.detail


def test_concluded_meetings_free_the_cap(two_agents):
    m1 = _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    task_store.update_meeting(m1["meeting_id"], status="concluded")
    assert _create(two_agents, ["a1", "a2"], x_agent_name="a1")["status"] == "pending"


def test_cap_is_per_creator(two_agents):
    _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    other = _user({"a1": "editor", "a2": "editor"}, sub="u-2")
    assert _create(other, ["a1", "a2"], x_agent_name="a1")["status"] == "pending"


def test_admin_config_raises_cap(two_agents):
    mcp_store.set_mcp_config_value("meetings-mcp", "MAX_PARALLEL_SPAWNS", "6")
    _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    _create(two_agents, ["a1", "a2"], x_agent_name="a1")
    assert _create(two_agents, ["a1", "a2"], x_agent_name="a1")["status"] == "pending"
