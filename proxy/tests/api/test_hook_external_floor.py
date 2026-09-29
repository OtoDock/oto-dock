"""The permission-hook floor for external sessions: no shell — before the
path gate, before any mode branch. The web tools are not floored; WebFetch
still goes through the SSRF gate."""

from __future__ import annotations

import uuid

import pytest

from api.hooks.hooks import decide_tool_permission
from auth.path_policy import SecurityContext
from core.events import tool_roles
from core.session import session_state


@pytest.fixture
def external_session():
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "auto", SecurityContext(
        role="manager", username="", agent="support", is_admin_agent=False,
        session_scope="agent", principal="external", external_claim="phone:+1",
    ))
    yield sid
    session_state.cleanup_session_permission_state(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", tool_roles.names_of(tool_roles.SHELL))
async def test_denied_even_for_a_manager_in_auto_mode(external_session, tool):
    decision = await decide_tool_permission(external_session, tool, {"command": "ls"})
    assert decision["decision"] == "deny"
    assert "external routes" in decision["reason"]


@pytest.mark.asyncio
async def test_other_tools_are_not_floored(external_session):
    decision = await decide_tool_permission(external_session, "Glob", {"pattern": "*.md"})
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_web_tools_are_not_floored(external_session):
    """A caller can already hear anything the session reads, so the web
    tools add no channel (2026-09-08); the SSRF gate still guards WebFetch."""
    decision = await decide_tool_permission(
        external_session, "WebSearch", {"query": "otodock phone routes"},
    )
    assert decision["decision"] == "allow"
    decision = await decide_tool_permission(
        external_session, "WebFetch", {"url": "https://docs.otodock.io/features/phone"},
    )
    assert decision["decision"] == "allow"
    decision = await decide_tool_permission(
        external_session, "WebFetch", {"url": "http://192.168.1.10/admin"},
    )
    assert decision["decision"] == "deny" and "private" in decision["reason"]
