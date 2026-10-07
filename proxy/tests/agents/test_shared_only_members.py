"""A Shared-only agent holds no viewer or contributor assignment.

Its sessions run as the agent, which takes the editor tier, so the routes
that write assignment rows refuse a new or changed one below that tier
there, and switching an agent to Shared only asks a person to confirm the
people whose rows go. A row an older install already holds is kept until
an admin changes or removes it.
"""

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from services.agents import shared_only_members
from storage import database as db
from storage.agents import agent_store

client = TestClient(app)

_ADMIN = "local:admin"
_PM = "local:pm"
_EDITOR = "local:ed"


def _login(role="admin", sub=_ADMIN, *, api_key=False, agent_roles=None, agent=""):
    async def _user():
        return UserContext(sub=sub, email=f"{sub}@t.com", name=sub, role=role,
                           agent_roles=agent_roles or {}, is_api_key=api_key, agent=agent)
    app.dependency_overrides[get_current_user] = _user


def _shared_only(slug):
    agent_store.update_agent(slug, collaborative=False, default_scope="agent")


@pytest.fixture(autouse=True)
def _fixtures():
    db.upsert_user(_ADMIN, "admin@t.com", "Admin", "admin")
    db.upsert_user(_PM, "pm@t.com", "Site PM", "member")
    db.upsert_user(_EDITOR, "ed@t.com", "Ed", "member")
    agent_store.create_agent("office", "Office")
    agent_store.create_agent("site", "Site")
    _shared_only("office")
    _login()
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ── the full-set PUT ───────────────────────────────────────────────────

def _put(agents, roles):
    return client.put(f"/v1/admin/users/{_PM}/agents", json={"agents": agents, "agent_roles": roles})


def test_the_put_refuses_a_new_row_below_editor_on_a_shared_only_agent():
    for role in ("viewer", "contributor"):
        resp = _put(["office", "site"], {"office": role, "site": "viewer"})
        assert resp.status_code == 400, resp.text
        assert "office" in resp.json()["detail"] and "site" not in resp.json()["detail"]
    assert db.get_user_agent_roles(_PM) == {}
    assert _put(["office"], {"office": "editor"}).status_code == 200
    assert db.get_user_agent_roles(_PM) == {"office": "editor"}


def test_the_put_judges_the_role_it_writes_and_only_the_agents_it_writes():
    # No role sent: the store writes viewer, so it is refused.
    assert _put(["office"], {}).status_code == 400
    # A stale role for an agent the save drops is never judged.
    resp = _put(["site"], {"site": "viewer", "office": "viewer"})
    assert resp.status_code == 200, resp.text


def test_the_put_keeps_an_unchanged_older_row_and_refuses_changing_it_downwards():
    db.set_user_agents(_PM, ["office"], _ADMIN, agent_roles={"office": "viewer"})
    # Editing something else keeps the older row as it is.
    resp = _put(["office", "site"], {"office": "viewer", "site": "editor"})
    assert resp.status_code == 200, resp.text
    assert db.get_user_agent_roles(_PM) == {"office": "viewer", "site": "editor"}
    # Changing it to another low role is refused; raising it is fine.
    assert _put(["office", "site"], {"office": "contributor", "site": "editor"}).status_code == 400
    assert _put(["office", "site"], {"office": "editor", "site": "editor"}).status_code == 200


# ── the additive POST and the new-user default ─────────────────────────

def test_the_post_refuses_below_editor_on_a_shared_only_agent():
    resp = client.post(f"/v1/admin/users/{_PM}/agents/office", json={"role": "viewer"})
    assert resp.status_code == 400 and "office" in resp.json()["detail"]
    assert db.get_user_agent_roles(_PM) == {}
    assert client.post(f"/v1/admin/users/{_PM}/agents/office",
                       json={"role": "editor"}).status_code == 200
    assert client.post(f"/v1/admin/users/{_PM}/agents/site",
                       json={"role": "viewer"}).status_code == 200


def test_the_default_for_new_users_takes_editor_or_above_on_a_shared_only_agent():
    url = "/v1/admin/agents/office/default-for-new-users"
    assert client.put(url, json={"enabled": True, "role": "viewer"}).status_code == 400
    assert client.put(url, json={"enabled": True, "role": "editor"}).status_code == 200
    assert client.put("/v1/admin/agents/site/default-for-new-users",
                      json={"enabled": True, "role": "viewer"}).status_code == 200


def test_the_assigner_skips_a_below_editor_default_on_a_shared_only_agent():
    from services.community import default_agent_assigner
    agent_store.set_default_for_new_users_role("office", "viewer")  # stored before the switch
    agent_store.set_default_for_new_users_role("site", "viewer")
    default_agent_assigner.assign_default_agents(_PM)
    assert db.get_user_agent_roles(_PM) == {"site": "viewer"}


# ── switching an agent to Shared only ──────────────────────────────────

def _switch(**extra):
    return client.patch("/v1/agents/site", json={"collaborative": False, "default_scope": "agent", **extra})


def _members():
    db.set_user_agents(_PM, ["site"], _ADMIN, agent_roles={"site": "contributor"})
    db.set_user_agents(_EDITOR, ["site", "office"], _ADMIN,
                       agent_roles={"site": "editor", "office": "editor"})


def test_the_switch_names_the_people_who_lose_their_row_and_changes_nothing():
    _members()
    resp = _switch()
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "shared_only_removals"
    assert [(p["sub"], p["role"], p["name"], p["platform_role"]) for p in detail["people"]] == [
        (_PM, "contributor", "Site PM", "member")]
    assert agent_store.get_agent("site")["collaborative"] is True
    assert db.get_user_agent_roles(_PM) == {"site": "contributor"}
    # A confirmation drawn from another list is refused with the live one.
    resp = _switch(confirm_removals=[_EDITOR])
    assert resp.status_code == 409 and [p["sub"] for p in resp.json()["detail"]["people"]] == [_PM]


def test_a_confirmed_switch_removes_exactly_those_rows(monkeypatch):
    from services.agents import offboarding
    _members()
    agent_store.set_default_for_new_users_role("site", "viewer")
    dispatched = []

    async def _record(sub, agent_losses, actor_sub, **kw):
        dispatched.append((sub, [(a.agent, a.old_row, a.new_row) for a in agent_losses], actor_sub))
    monkeypatch.setattr(offboarding, "dispatch_losses", _record)
    resp = _switch(confirm_removals=[_PM])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["collaborative"] is False and body["default_scope"] == "agent"
    assert body["shared_only_switch"] == {"removed": [_PM], "not_removed": [],
                                          "default_cleared": True}
    assert db.get_user_agent_roles(_PM) == {}
    assert db.get_user_agent_roles(_EDITOR) == {"site": "editor", "office": "editor"}
    assert agent_store.get_agent("site")["default_for_new_users_role"] == ""
    assert dispatched == [(_PM, [("site", "contributor", "")], _ADMIN)]


def test_a_switch_with_nobody_below_editor_needs_no_confirmation():
    db.set_user_agents(_EDITOR, ["site"], _ADMIN, agent_roles={"site": "manager"})
    resp = _switch()
    assert resp.status_code == 200, resp.text
    assert resp.json()["shared_only_switch"] == {"removed": [], "not_removed": [],
                                                 "default_cleared": False}


def test_one_axis_is_enough_to_turn_an_agent_shared_only():
    _members()
    agent_store.update_agent("site", collaborative=False)  # Personal only
    resp = client.patch("/v1/agents/site", json={"default_scope": "agent"})
    assert resp.status_code == 409, resp.text


def test_an_agent_session_never_sees_the_list_and_cannot_confirm():
    _members()
    # A session of the agent, its person a manager there (the agent-config MCP).
    _login(role="member", sub=_EDITOR, api_key=True, agent_roles={"site": "manager"}, agent="site")
    for extra in ({}, {"confirm_removals": [_PM]}):
        resp = _switch(**extra)
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, str) and "1 person" in detail and _PM not in detail
    assert db.get_user_agent_roles(_PM) == {"site": "contributor"}
    assert agent_store.get_agent("site")["collaborative"] is True


def test_a_row_the_confirmation_did_not_name_stays_and_is_reported(monkeypatch):
    _members()
    real = shared_only_members.below_editor_members
    calls = []

    def _late_joiner(agent):
        rows = real(agent)
        calls.append(agent)
        if len(calls) == 2:  # read after the mode write: someone joined meanwhile
            db.set_user_agents(_EDITOR, ["site", "office"], _ADMIN,
                               agent_roles={"site": "viewer", "office": "editor"})
            rows = real(agent)
        return rows
    monkeypatch.setattr(shared_only_members, "below_editor_members", _late_joiner)
    resp = _switch(confirm_removals=[_PM])
    assert resp.status_code == 200, resp.text
    assert resp.json()["shared_only_switch"]["not_removed"] == [_EDITOR]
    assert db.get_user_agent_roles(_EDITOR)["site"] == "viewer"


def test_the_older_rows_are_counted_by_agent():
    db.set_user_agents(_PM, ["office"], _ADMIN, agent_roles={"office": "contributor"})
    assert shared_only_members.legacy_rows_by_agent() == {"office": 1}


def test_an_admins_row_is_neither_listed_nor_removed():
    """An admin acts as admin whatever the row says: their viewer row is
    inert, so the switch neither names nor removes it."""
    _members()
    db.set_user_agents(_ADMIN, ["site"], _ADMIN, agent_roles={"site": "viewer"})
    resp = _switch()
    assert [p["sub"] for p in resp.json()["detail"]["people"]] == [_PM]
    resp = _switch(confirm_removals=[_PM])
    assert resp.status_code == 200, resp.text
    assert db.get_user_agent_roles(_ADMIN) == {"site": "viewer"}


def test_the_post_answers_already_assigned_for_an_unchanged_older_row():
    db.set_user_agents(_PM, ["office"], _ADMIN, agent_roles={"office": "contributor"})
    resp = client.post(f"/v1/admin/users/{_PM}/agents/office", json={"role": "contributor"})
    assert resp.status_code == 200 and resp.json()["status"] == "already_assigned"
    assert client.post(f"/v1/admin/users/{_PM}/agents/office", json={"role": "viewer"}).status_code == 400


def test_a_failure_after_the_mode_write_reports_the_rows_kept(monkeypatch):
    _members()

    async def _boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(shared_only_members, "remove_members", _boom)
    resp = _switch(confirm_removals=[_PM])
    assert resp.status_code == 200, resp.text
    assert resp.json()["collaborative"] is False
    assert resp.json()["shared_only_switch"]["not_removed"] == [_PM]
