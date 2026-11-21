"""The permission authority's reading of a tool limit (``api/hooks/limits``):
a function the session's limits leave out is refused before every mode
branch, for every client kind and whatever the engine was told; a granted
function and a server without a limit go on; an outside session keeps its
own floor; the limit survives the security index."""

from __future__ import annotations

import uuid

import pytest

from api.hooks.hooks import decide_tool_permission
from auth.path_policy import SecurityContext
from core.session import session_state

LIMITS = (("mail", ("read_mail", "list_labels")), ("crm-srv", ("find",)))


def _register(mode: str = "dontAsk", client_type: str = "dashboard", **facts) -> str:
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, mode, SecurityContext(
        role="manager", username="alice", agent="support", is_admin_agent=False,
        session_scope="user", tool_limits=LIMITS, **facts,
    ))
    session_state._sessions.setdefault(sid, {"created": True, "message_count": 0})["client_type"] = client_type
    return sid


@pytest.fixture
def sessions():
    made: list[str] = []

    def make(**kw) -> str:
        sid = _register(**kw)
        made.append(sid)
        return sid
    yield make
    for sid in made:
        session_state.cleanup_session_permission_state(sid)
        session_state._sessions.pop(sid, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,client", [
    ("dontAsk", "dashboard"), ("autoReview", "dashboard"), ("plan", "dashboard"), ("ask", "dashboard"),
    ("auto", "task"), ("auto", "meeting"), ("auto", "phone"),
])
async def test_a_function_left_off_is_refused_in_every_mode_and_client_kind(sessions, mode, client):
    sid = sessions(mode=mode, client_type=client)
    decision = await decide_tool_permission(sid, "mcp__mail__send_mail", {"to": "x"})
    assert decision["decision"] == "deny", (mode, client, decision)
    assert decision["reason"] == ("mail is limited for this agent: it may not use send_mail. A manager can "
                                  "change this in Agent Settings, Configuration, What the agent may use.")


@pytest.mark.asyncio
async def test_a_granted_function_and_an_unlimited_server_go_on(sessions):
    sid = sessions(mode="dontAsk")
    for tool in ("mcp__mail__read_mail", "mcp__mail__list_labels", "mcp__crm-srv__find", "mcp__other__anything"):
        decision = await decide_tool_permission(sid, tool, {"q": "x"})
        assert decision["decision"] == "allow", (tool, decision)


@pytest.mark.asyncio
async def test_an_allow_remembered_for_the_tool_does_not_lift_it(sessions):
    sid = sessions(mode="ask")
    session_state.remember_session_tool_allow(sid, "mcp__mail__send_mail")
    decision = await decide_tool_permission(sid, "mcp__mail__send_mail", {"to": "x"})
    assert decision["decision"] == "deny" and "limited for this agent" in decision["reason"]


@pytest.mark.asyncio
async def test_a_codex_name_the_mapping_did_not_meet_is_still_refused(sessions):
    """Codex's hook sanitises a server key (``crm-srv`` → ``crm_srv``); a name
    the layer could not map back keeps that spelling, and the reading
    matches the pair by it: closed, never open."""
    sid = sessions(mode="dontAsk")
    decision = await decide_tool_permission(sid, "mcp__crm_srv__update", {"q": "x"})
    assert decision["decision"] == "deny" and "may not use update" in decision["reason"]
    decision = await decide_tool_permission(sid, "mcp__crm_srv__find", {"q": "x"})
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_an_outside_session_keeps_its_own_floor(sessions):
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "auto", SecurityContext(
        role="external", username="", agent="support", is_admin_agent=False, session_scope="agent",
        principal="external", external_claim="phone:+1", outside_grants=(("crm", ("find",)),)))
    try:
        decision = await decide_tool_permission(sid, "mcp__crm__update", {"q": "x"})
        assert decision["decision"] == "deny" and "outside people" in decision["reason"]
        decision = await decide_tool_permission(sid, "mcp__crm__find", {"q": "x"})
        assert decision["decision"] == "allow"
    finally:
        session_state.cleanup_session_permission_state(sid)


def test_the_limit_survives_the_security_index():
    ctx = SecurityContext(role="manager", username="alice", agent="support", is_admin_agent=False,
                          tool_limits=LIMITS)
    back = session_state._deserialize_security_ctx(session_state._serialize_security_ctx(ctx))
    assert back.tool_limits == LIMITS and back == ctx
