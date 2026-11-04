"""The department set routes (``api/departments/sets.py``) and the single
install's offer (``api/mcp/community.py``): the list, the preview, the
install as a job, the job's state, and the gates (creator or admin, a
person at the dashboard for the install, the starter or an admin for the
state).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from auth import roles
from auth.providers import create_session_jwt
from services.community import department_set_installer as installer
from storage.agents import agent_store, db_departments, db_set_installs
from tests.conftest import live_session_token
from tests.mcp.test_community_agent_installer import _make_user

ADMIN = "user-admin"
CREATOR = "user-manager"
MEMBER = "user-viewer"

REGISTRY = {"registry_version": "1", "updated_at": "2026-10-28T00:00:00Z", "agents": [
    {"slug": "head", "display_name": "Head", "version": "1.0.0", "platform_min_version": "1.0.0", "required_mcps": [],
     "apps": [{"slug": "board", "title": "Board", "visibility": "agent", "sig": "sig-b", "app_json": {"title": "Board"}}]},
    {"slug": "clerk", "display_name": "Clerk", "version": "1.0.0", "platform_min_version": "1.0.0", "required_mcps": []},
], "departments": [{
    "slug": "office", "name": "Office", "version": "0.1.0", "levels": ["Head", "Team"], "platform_min_version": "1.0.0",
    "members": [{"template": "head", "level": "Head"}, {"template": "clerk", "level": "Team"}],
    "library": {"source": "head", "subdir": "facts"}, "schedules": [], "bindings": []},
    {"slug": "broken", "name": "Broken", "version": "0.1.0", "levels": ["Head"],
     "members": [{"template": "ghost", "level": "Head"}]}]}


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("services.community.community_agents_catalog.fetch_registry",
                        AsyncMock(return_value=REGISTRY))
    monkeypatch.setattr("services.community.community_catalog.fetch_registry", AsyncMock(return_value={"mcps": []}))


def _cookie(sub: str, email: str, role: str) -> TestClient:
    return TestClient(app, headers={"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"})


ADMIN_C = lambda: _cookie(ADMIN, "admin@test.com", roles.ADMIN)
CREATOR_C = lambda: _cookie(CREATOR, "manager@test.com", roles.CREATOR)
MEMBER_C = lambda: _cookie(MEMBER, "viewer@test.com", roles.MEMBER)


def test_the_list_names_every_set_with_its_members_and_its_problems(temp_db):
    r = ADMIN_C().get("/v1/community/departments")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registry_version"] == "1"
    office, broken = body["departments"]
    assert office["slug"] == "office" and office["problems"] == [] and office["missing_members"] == []
    assert [m["display_name"] for m in office["members"]] == ["Head", "Clerk"]
    assert office["members"][0]["in_catalog"] is True and office["installed"] == []
    assert broken["slug"] == "broken" and broken["missing_members"] == ["ghost"]
    assert MEMBER_C().get("/v1/community/departments").status_code == 403
    assert CREATOR_C().get("/v1/community/departments").status_code == 200


def test_the_preview_plans_each_member_with_its_consent_items(temp_db):
    agent_store.create_agent("clerk", "Clerk", created_by=ADMIN, community_template="clerk",
                             community_template_version="1.0.0")
    r = ADMIN_C().get("/v1/community/departments/office/preview", params={"department_name": "Lady G"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["department_name"] == "Lady G" and body["name_problem"] is None and body["running_job"] is None
    head, clerk = body["members"]
    assert head["action"] == installer.ACTION_NEW and head["slug"] == "head" and head["apps"][0]["sig"] == "sig-b"
    assert clerk["action"] == installer.ACTION_ADOPT and clerk["slug"] == "clerk"
    assert body["library"] == {"source": "head", "subdir": "facts", "writable": False}
    assert body["consent_scope"] == "everyone" and body["levels"] == ["Head", "Team"]
    # The name the person typed: a department this set made under it
    # holds members the preview shows as used, never as placed elsewhere.
    dept = db_departments.create_department("Lady G", ADMIN, level_names=["Head", "Team"],
                                            community_set="office", community_set_version="0.1.0")
    db_departments.place_members(dept["id"], [("clerk", dept["levels"][1]["id"])])
    r = ADMIN_C().get("/v1/community/departments/office/preview", params={"department_name": "Lady G"})
    clerk = r.json()["members"][1]
    assert clerk["action"] == installer.ACTION_ADOPT and clerk["slug"] == "clerk" and clerk["placed"] == []
    r = ADMIN_C().get("/v1/community/departments/office/preview", params={"department_name": "Other"})
    clerk = r.json()["members"][1]
    assert clerk["action"] == installer.ACTION_NEW and clerk["placed"] == ["clerk"]
    r = ADMIN_C().get("/v1/community/departments/office/preview", params={"department_name": "a:b", "new": "clerk"})
    assert r.json()["name_problem"] and r.json()["members"][1]["action"] == installer.ACTION_NEW
    assert ADMIN_C().get("/v1/community/departments/nope/preview").status_code == 404
    r = ADMIN_C().get("/v1/community/departments/broken/preview")
    assert r.status_code == 409 and "not in the catalog" in r.json()["detail"]


def test_the_install_starts_a_job_that_the_starter_and_an_admin_may_read(temp_db):
    with patch.object(installer, "start", new=AsyncMock(side_effect=lambda dset, registry, name, u, consent, choices=None:
                                                         db_set_installs.create(dset.slug, dset.version, name, u.acting_sub, []))) as start:
        r = CREATOR_C().post("/v1/community/departments/office/install",
                             json={"department_name": "Lady G", "consent": True, "new": ["clerk"]})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert start.await_args.kwargs["consent"] is True and start.await_args.kwargs["choices"] == {"clerk": installer.ACTION_NEW}
    assert CREATOR_C().get(f"/v1/community/departments/installs/{job_id}").json()["department_name"] == "Lady G"
    assert ADMIN_C().get(f"/v1/community/departments/installs/{job_id}").status_code == 200
    _make_user("user-other", "o@test.com", "creator")
    other = _cookie("user-other", "o@test.com", roles.CREATOR)
    assert other.get(f"/v1/community/departments/installs/{job_id}").status_code == 403
    assert ADMIN_C().get("/v1/community/departments/installs/setjob-nope").status_code == 404
    assert MEMBER_C().post("/v1/community/departments/office/install", json={}).status_code == 403
    # A bearer never installs: its consent would approve apps.
    token = live_session_token("sid-sets", "pa", user_sub=ADMIN)
    r = TestClient(app, headers={"Authorization": f"Bearer {token}"}).post(
        "/v1/community/departments/office/install", json={})
    assert r.status_code == 403


def test_the_install_refusals_carry_their_sentences(temp_db):
    r = ADMIN_C().post("/v1/community/departments/office/install", json={"department_name": "a/b"})
    assert r.status_code == 400 and "may not contain" in r.json()["detail"]
    db_set_installs.create("office", "0.1.0", "Lady G", ADMIN, [])
    r = ADMIN_C().post("/v1/community/departments/office/install", json={"department_name": "Lady G"})
    assert r.status_code == 409 and "being installed already" in r.json()["detail"]
    assert ADMIN_C().post("/v1/community/departments/broken/install", json={}).status_code == 409


def test_the_single_install_preview_offers_the_department_and_the_install_takes_it(temp_db, monkeypatch):
    agent_store.create_agent("clerk", "Clerk", created_by=ADMIN, community_template="clerk",
                             community_template_version="1.0.0")
    r = ADMIN_C().get("/v1/community/agents/head/preview")
    assert r.status_code == 200, r.text
    offer = r.json()["set"]
    assert offer["slug"] == "office" and offer["name"] == "Office" and offer["level"] == "Head"
    assert offer["members"] == [{"template": "clerk", "agent_slug": "clerk", "display_name": "Clerk", "level": "Team"}]
    assert offer["existing"] == []
    # No standalone member of the set: no offer.
    dept = db_departments.create_department("Other", ADMIN, level_names=["Team"])
    db_departments.place_members(dept["id"], [("clerk", dept["levels"][0]["id"])])
    assert ADMIN_C().get("/v1/community/agents/head/preview").json()["set"] is None
    db_departments.delete_department(dept["id"])

    async def fake_install(**kw):
        agent_store.create_agent(kw["target_slug"], "Head", created_by=kw["installer_user_sub"],
                                 community_template=kw["template_slug"], community_template_version="1.0.0")
        return {"agent_slug": kw["target_slug"], "batch_id": None, "created_requests": [], "ready_mcps": []}

    monkeypatch.setattr("services.community.community_agent_installer.install_from_catalog", fake_install)
    monkeypatch.setattr("services.knowledge.library_projector.attach_setup", AsyncMock())
    r = ADMIN_C().post("/v1/agents/install-from-community",
                       json={"template_slug": "head", "department_set": {"slug": "office", "name": "Lady G"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_slug"] == "head" and body["department"]["name"] == "Lady G"
    assert "The department Lady G holds Head and Clerk." in "\n".join(body["department_lines"])
    assert agent_store.get_agent("clerk")["department_id"] == body["department"]["id"]
    assert agent_store.get_agent("head")["department_id"] == body["department"]["id"]
    # The offer with nobody to place: the agent stands, the line says so.
    r = ADMIN_C().post("/v1/agents/install-from-community",
                       json={"template_slug": "head", "target_slug": "head-2",
                             "department_set": {"slug": "office", "name": "Lady H"}})
    assert r.status_code == 200 and r.json()["department"] is None
    assert "No second agent of this set stands alone" in r.json()["department_lines"][0]
