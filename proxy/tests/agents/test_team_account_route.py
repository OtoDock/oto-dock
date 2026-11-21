"""The team account on the agents route (``api/agents/agents.py``): a
manager sets it from the settings page, never a session token or an API
key; it needs an agent with both spaces, and a mode without them turns it
off in the same write; the floor is an agent role; and a change in whose
accounts people's chats run on, or in the floor, tells the chats once
(``chat_reload.team_account_changed``), a mode change that keeps the
answer tells nothing."""

import asyncio

import pytest
from fastapi import HTTPException

from api.agents import agents as api
from auth.providers import UserContext
from services.mcp import account_context
from services.remote import chat_reload
from storage.agents import agent_store

AGENT = "team-route-agent"


@pytest.fixture
def world(temp_db, monkeypatch):
    agent_store.create_agent(AGENT, "Team")
    told: list = []

    async def changed(agent, *, lends):
        told.append((agent, lends))

    monkeypatch.setattr(chat_reload, "team_account_changed", changed)
    return told


def _ctx(role="admin", *, session=False, api_key=False):
    return UserContext(
        sub="user-admin" if role == "admin" else "user-manager", email="a@b.c", name="N",
        role=role, agent_roles={AGENT: "manager"},
        agent=AGENT if session else "", session_id="sid-1" if session else "",
        is_api_key=session or api_key)


def _update(user, **fields):
    async def go():
        result = await api.update_agent(AGENT, api.UpdateAgentRequest(**fields), user=user)
        await asyncio.sleep(0)   # the flip's closes run off the request
        return result
    return asyncio.run(go())


def test_a_manager_turns_it_on_and_the_chats_are_told_once(world):
    result = _update(_ctx("member"), team_account=True)
    assert result["team_account"] is True and result["team_account_floor"] == "editor"
    assert account_context.people_use_agents_accounts(AGENT)
    assert world == [(AGENT, True)]
    # The floor alone: told with no lending change.
    _update(_ctx("member"), team_account_floor="viewer")
    assert world == [(AGENT, True), (AGENT, None)]
    # Nothing changed for the chats: nothing said.
    _update(_ctx("member"), team_account=True, team_account_floor="viewer")
    assert len(world) == 2
    _update(_ctx("member"), team_account=False)
    assert world[-1] == (AGENT, False)


def test_a_session_token_and_an_api_key_may_not_change_it(world):
    for user in (_ctx(session=True), _ctx(api_key=True)):
        with pytest.raises(HTTPException) as e:
            _update(user, team_account=True)
        assert e.value.status_code == 403 and e.value.detail == api.TEAM_ACCOUNT_WHO
        with pytest.raises(HTTPException) as e:
            _update(user, team_account_floor="viewer")
        assert e.value.status_code == 403
    assert world == []


def test_it_needs_both_spaces_and_a_mode_without_them_turns_it_off(world):
    agent_store.update_agent(AGENT, collaborative=False, default_scope="user")
    with pytest.raises(HTTPException) as e:
        _update(_ctx(), team_account=True)
    assert e.value.status_code == 409 and e.value.detail == api.TEAM_ACCOUNT_MODE
    # Both axes in one write: the request's mode decides.
    with pytest.raises(HTTPException):
        _update(_ctx(), team_account=True, collaborative=False, default_scope="agent")
    result = _update(_ctx(), team_account=True, collaborative=True, default_scope="user")
    assert result["team_account"] is True and world == [(AGENT, True)]
    # Shared only keeps people's chats on the agent's accounts: the switch
    # goes off in the same write and the chats hear nothing of it.
    result = _update(_ctx(), collaborative=False, default_scope="agent", confirm_removals=[])
    assert result["team_account"] is False
    assert account_context.people_use_agents_accounts(AGENT)
    assert world == [(AGENT, True)]
    # Personal only: off, and people's chats are back on their own.
    agent_store.update_agent(AGENT, team_account=True, collaborative=True, default_scope="user")
    world.clear()
    result = _update(_ctx(), collaborative=False, default_scope="user")
    assert result["team_account"] is False and world == [(AGENT, False)]


def test_the_floor_is_an_agent_role(world):
    with pytest.raises(HTTPException) as e:
        _update(_ctx(), team_account_floor="admin")
    assert e.value.status_code == 400
    assert _update(_ctx(), team_account_floor="manager")["team_account_floor"] == "manager"
