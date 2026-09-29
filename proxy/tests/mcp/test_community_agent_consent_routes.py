"""The install dialog's consent over the routes (COMMUNITY-AGENTS-REGISTRY.md
"Consent").

Load-bearing: the preview carries each template app in the approval card's
row shape with its signature and each check with its words; the install
route takes a person at the dashboard only (a bearer is refused, whatever
its role) and passes the caller's own consent through; a template without
apps or checks previews as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user

client = TestClient(app)

HOME = {"title": "Home", "egress": ["api.open-meteo.com"], "handlers": {"on_schedule": {"tick": {"cron": "0 7 * * *"}}},
        "actions": [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"},
                    {"id": "go", "label": "Go", "type": "fire_task", "task": "report"}]}
ENTRY = {
    "slug": "pa", "display_name": "PA", "version": "3.0.0", "required_mcps": [],
    "platform_min_version": "1.4.0",
    "apps": [{"slug": "home", "title": "Home", "visibility": "user", "app_json": HOME,
              "blueprint_json": {"format": 1, "tasks": [{"slug": "report", "description": "The report", "prompt": "do it"}]},
              "tree_sha": "t", "sig": "sig-home", "owner_approval": True}],
    "checks": [{"name": "lint", "description": "Lint it", "sections": ["script", "judge"], "applies": ["chats"],
                "mandatory": True, "script": "lint.sh", "sig": "sig-lint"}],
}


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean():
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _admin(**kw) -> UserContext:
    return UserContext(sub="admin-sub", email="admin@test.com", name="Admin", role="admin",
                       agents=[], agent_roles={}, **kw)


def test_the_preview_carries_the_apps_and_checks_in_the_cards_shape(temp_db):
    _as(_admin())
    with patch("services.community.community_agents_catalog.fetch_registry",
               new=AsyncMock(return_value={"agents": [ENTRY]})), \
            patch("services.community.community_catalog.fetch_registry",
                  new=AsyncMock(return_value={"mcps": []})):
        body = client.get("/v1/community/agents/pa/preview").json()
    assert body["consent_scope"] == "everyone"
    (home,) = body["apps"]
    assert home["slug"] == "home" and home["visibility"] == "user" and home["sig"] == "sig-home"
    assert home["owner_approval"] is True
    row = home["row"]
    assert row["scope"] == "personal" and row["kind"] == "folder" and row["actions_approved"] is False
    assert row["egress"] == ["api.open-meteo.com"] and row["handlers"]["on_schedule"]["tick"]["cron"] == "0 7 * * *"
    assert row["manifest_empty"] is False and row["external"]["session_days"] == 30
    go = next(a for a in row["actions"] if a["id"] == "go")
    assert go["task_name"] == "The report"
    assert home["blueprint_tasks"] == [{"slug": "report", "description": "The report", "prompt": "do it"}]
    (lint,) = body["checks"]
    assert lint["mandatory"] is True and lint["sig"] == "sig-lint"
    assert lint["words"] == "runs a script in your members' sessions, on their machines; a judge run per turn (on chats)"
    # A creator's consent covers the shared apps and their own copy only.
    _as(UserContext(sub="c", email="c@test.com", name="C", role="creator", agents=[], agent_roles={}))
    with patch("services.community.community_agents_catalog.fetch_registry",
               new=AsyncMock(return_value={"agents": [ENTRY]})), \
            patch("services.community.community_catalog.fetch_registry",
                  new=AsyncMock(return_value={"mcps": []})):
        assert client.get("/v1/community/agents/pa/preview").json()["consent_scope"] == "own"
    # An entry without apps previews as before.
    _as(_admin())
    with patch("services.community.community_agents_catalog.fetch_registry",
               new=AsyncMock(return_value={"agents": [{**ENTRY, "apps": None, "checks": None}]})), \
            patch("services.community.community_catalog.fetch_registry",
                  new=AsyncMock(return_value={"mcps": []})):
        body = client.get("/v1/community/agents/pa/preview").json()
    assert body["apps"] == [] and body["checks"] == []


def test_the_install_route_takes_a_person_and_passes_their_consent(temp_db):
    calls: list[dict] = []

    async def fake_install(**kw):
        calls.append(kw)
        return {"agent_slug": kw["target_slug"], "agent": {}}

    # A bearer is refused whatever its role.
    _as(_admin(is_api_key=True))
    r = client.post("/v1/agents/install-from-community", json={"template_slug": "pa"})
    assert r.status_code == 403
    _as(_admin())
    with patch("services.community.community_agent_installer.install_from_catalog", new=fake_install):
        r = client.post("/v1/agents/install-from-community", json={
            "template_slug": "pa", "target_slug": "pa-2", "manager_user": "someone-else",
            "display_name": " My PA ",
            "approve_apps": {"home": "sig-home"}, "approve_checks": {"lint": "sig-lint"}})
    assert r.status_code == 200, r.text
    (kw,) = calls
    assert kw["installer_user_sub"] == "someone-else" and kw["installer_role"] == "admin"
    assert kw["display_name"] == "My PA"
    # The consent is the caller's own, whoever the install is for.
    assert kw["consent_by"] == "admin-sub"
    assert kw["app_consent"] == {"home": "sig-home"} and kw["check_consent"] == {"lint": "sig-lint"}
    # Without the fields nothing is consented.
    calls.clear()
    with patch("services.community.community_agent_installer.install_from_catalog", new=fake_install):
        client.post("/v1/agents/install-from-community", json={"template_slug": "pa"})
    assert calls[0]["consent_by"] == "" and calls[0]["app_consent"] is None


def test_any_manager_of_the_agent_may_see_and_run_its_update(temp_db):
    """The update routes ask for a manager of the agent, whatever their
    platform role (an admin manages every agent); a viewer is refused."""
    manager = UserContext(sub="m-sub", email="m@test.com", name="M", role="member",
                          agents=["pa"], agent_roles={"pa": "manager"})
    viewer = UserContext(sub="v-sub", email="v@test.com", name="V", role="member",
                         agents=["pa"], agent_roles={"pa": "viewer"})
    _as(manager)
    assert client.get("/v1/agents/pa/update-template/status").status_code == 200
    _as(viewer)
    r = client.get("/v1/agents/pa/update-template/status")
    assert r.status_code == 403 and "manager" in r.json()["detail"]
    # The plan route runs the same authority before anything else.
    assert client.post("/v1/agents/pa/update-template/plan").status_code == 403

