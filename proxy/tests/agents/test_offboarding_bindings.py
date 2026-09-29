"""The service-binding subscriber (``services/agents/offboarding_bindings.py``):
a binding and the service-scope webhook subscriptions that lend a
person's account to an agent do not outlive the manager standing that
created them.

Run: cd proxy && venv/bin/pytest tests/agents/test_offboarding_bindings.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.agents import offboarding
from services.agents import offboarding_bindings as bindings
from services.agents.offboarding import AgentLoss, OffboardEvent
from services.webhooks import subscription_manager
from storage import database as db
from storage.agents import agent_store
from storage.automation import webhook_subscription_store
from storage.identity import credential_store

A, B = "sb-a", "sb-b"
MCP = "github-mcp"


@pytest.fixture
def world(temp_db, monkeypatch):
    """Two agents, an admin, a person managing both and lending one account
    to each, one service-scope subscription per agent on that account,
    and the vendor delete recorded instead of called."""
    agent_store.create_agent(A, "A")
    agent_store.create_agent(B, "B")
    admin = db.create_local_user("admin@t.com", "Ada Admin", "Ada", "admin", "x")
    pat = db.create_local_user("pat@t.com", "Pat Person", "Pat", "member", "x")
    db.set_user_agents(pat, [A, B], admin, agent_roles={A: "manager", B: "manager"})
    credential_store.set_user_credentials(pat, MCP, {"k": "v"}, account_label="acct")
    for agent in (A, B):
        assert credential_store.set_service_agent_binding(
            MCP, agent, account_label="acct", owner_sub=pat, set_by=pat)
    subs = {agent: _sub(agent, pat)["id"] for agent in (A, B)}
    deleted: list[tuple] = []

    async def _vendor(row, *, token_owner_sub=""):
        deleted.append((row["agent"], token_owner_sub))
        return True

    monkeypatch.setattr(subscription_manager, "_vendor_delete", _vendor)
    return SimpleNamespace(admin=admin, pat=pat, subs=subs, deleted=deleted)


def _sub(agent, created_by, label="acct"):
    return webhook_subscription_store.create_subscription(
        scope="service", owner="", agent=agent, mcp_name=MCP, provider_id="github",
        account_label=label, vendor_target="repo", selected_events=["push"],
        selected_subevents={}, signing_secret="", created_by=created_by,
    )


def _event(w, reason, *losses):
    return OffboardEvent(sub=w.pat, reason=reason, agents=tuple(losses), actor_sub=w.admin,
                         username="pat", email="pat@t.com", display_name="Pat")


def _rows(agent):
    return webhook_subscription_store.list_subscriptions(scope="service", agent=agent)


@pytest.mark.asyncio
async def test_losing_the_manager_tier_clears_the_binding_and_its_subscriptions(world):
    w = world
    db.set_user_agents(w.pat, [A, B], w.admin, agent_roles={A: "editor", B: "manager"})
    await bindings.on_offboard(_event(w, offboarding.DEMOTED, AgentLoss(A, "manager", "editor")))
    assert credential_store.list_service_agent_bindings_for_owner(w.pat) == [
        {"mcp_name": MCP, "agent_name": B, "account_label": "acct"}]
    assert _rows(A) == [] and [r["id"] for r in _rows(B)] == [w.subs[B]]
    # The vendor delete resolved the token through the lender, not the
    # standing-gated binding lookup.
    assert w.deleted == [(A, w.pat)]


@pytest.mark.asyncio
async def test_a_person_who_keeps_the_manager_tier_loses_nothing(world):
    w = world
    await bindings.on_offboard(_event(w, offboarding.DEMOTED, AgentLoss(A, "editor", "viewer")))
    assert len(credential_store.list_service_agent_bindings_for_owner(w.pat)) == 2
    assert len(_rows(A)) == 1 and len(_rows(B)) == 1 and w.deleted == []


@pytest.mark.asyncio
async def test_a_deletion_sweeps_the_subscriptions_whose_bindings_were_dropped(world):
    w = world
    # Another manager's standing binding on B keeps its own subscription.
    other = db.create_local_user("mo@t.com", "Mo Manager", "Mo", "member", "x")
    db.set_user_agents(other, [B], w.admin, agent_roles={B: "manager"})
    credential_store.set_user_credentials(other, MCP, {"k": "v"}, account_label="theirs")
    assert credential_store.set_service_agent_binding(
        MCP, B, account_label="theirs", owner_sub=other, set_by=other)
    keep = _sub(B, other, label="theirs")["id"]
    # The admin delete route drops the bindings and the person before the event.
    credential_store.cleanup_service_agent_bindings_for_owner(w.pat)
    assert db.delete_user(w.pat)
    await bindings.on_offboard(_event(w, offboarding.DELETED))
    assert _rows(A) == []
    assert [r["id"] for r in _rows(B)] == [keep]
    assert sorted(w.deleted) == [(A, ""), (B, "")]


@pytest.mark.asyncio
async def test_the_admin_delete_route_unregisters_through_the_lender_before_the_token_goes(world):
    """The route clears each binding while the person's token file still
    exists, so the vendor delete runs through the lender; the subscriber's
    sweep then finds nothing left."""
    import asyncio
    from fastapi.testclient import TestClient
    from app import app
    from auth.providers import UserContext, get_current_user
    w = world

    async def _admin():
        return UserContext(sub=w.admin, email="admin@t.com", name="Ada", role="admin",
                           agent_roles={})
    app.dependency_overrides[get_current_user] = _admin
    try:
        resp = await asyncio.to_thread(
            lambda: TestClient(app).delete(f"/v1/admin/users/{w.pat}"))
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == 200, resp.text
    assert sorted(w.deleted) == [(A, w.pat), (B, w.pat)]
    assert _rows(A) == [] and _rows(B) == []
    assert credential_store.list_service_agent_bindings_for_owner(w.pat) == []
    assert db.get_user(w.pat) is None
