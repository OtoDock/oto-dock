"""The install dialog's consent over the routes (COMMUNITY-AGENTS-REGISTRY.md
"Consent").

Load-bearing: the preview carries each template app in the approval card's
row shape with its signature and each check with its words; the install
route takes a person at the dashboard only (a bearer is refused, whatever
its role) and passes the caller's own consent through; a template without
apps or checks previews as before; each tool row names the tool and says
whether every agent has it.
"""

from __future__ import annotations

from types import SimpleNamespace
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


def test_the_preview_says_which_app_brings_a_server(temp_db):
    """The registry's ``has_server`` reaches the row (the card's media line
    reads it) and the app's own ``adds_server`` (the dialog's line); an entry
    the registry wrote before it named a server says nothing."""
    _as(_admin())
    with_server = {**ENTRY, "apps": [{**ENTRY["apps"][0], "has_server": True}]}
    for entry, expected in ((with_server, True), (ENTRY, False)):
        with patch("services.community.community_agents_catalog.fetch_registry",
                   new=AsyncMock(return_value={"agents": [entry]})), \
                patch("services.community.community_catalog.fetch_registry",
                      new=AsyncMock(return_value={"mcps": []})):
            (home,) = client.get("/v1/community/agents/pa/preview").json()["apps"]
        assert home["adds_server"] is expected and home["row"]["has_server"] is expected, home


def test_the_preview_names_each_tool_and_says_which_every_agent_has(temp_db):
    """Each tool row carries its display name (the installed one's, else the
    community list's, else its name) and whether every agent has it. A tool
    from the community list waits for a request only when the installer is
    not an admin: an admin's install adds it at once."""
    manifests = {"schedules-mcp": SimpleNamespace(label="Schedules", assignment_mode="auto"),
                 "phone-mcp": SimpleNamespace(label="Phone Calls", assignment_mode="explicit")}
    entry = {**ENTRY, "required_mcps": [{"name": n} for n in ("schedules-mcp", "notion-mcp", "navis-mcp", "phone-mcp")]}

    def rows(user: UserContext) -> dict[str, dict]:
        _as(user)
        with patch("services.community.community_agents_catalog.fetch_registry",
                   new=AsyncMock(return_value={"agents": [entry]})), \
                patch("services.community.community_catalog.fetch_registry",
                      new=AsyncMock(return_value={"mcps": [{"name": "notion-mcp", "label": "Notion"}]})), \
                patch("services.mcp.mcp_registry.get_manifest", side_effect=manifests.get), \
                patch("services.mcp.mcp_registry.core_mcp_names", return_value=["schedules-mcp"]), \
                patch("storage.mcp.mcp_store.get_mcp_instances", return_value=[]):
            body = client.get("/v1/community/agents/pa/preview").json()
        return {m["name"]: m for m in body["required_mcps"]}

    admin = rows(_admin())
    assert {n: (m["label"], m["core"]) for n, m in admin.items()} == {
        "schedules-mcp": ("Schedules", True), "notion-mcp": ("Notion", False),
        "navis-mcp": ("navis-mcp", False), "phone-mcp": ("Phone Calls", False)}
    assert admin["notion-mcp"]["request_type"] == "install" and admin["notion-mcp"]["needs_request"] is False
    assert admin["navis-mcp"]["blocked"] is True and admin["phone-mcp"]["blocked"] is True
    assert admin["schedules-mcp"]["needs_request"] is False and admin["schedules-mcp"]["blocked"] is False
    creator = rows(UserContext(sub="c", email="c@test.com", name="C", role="creator", agents=[], agent_roles={}))
    assert creator["notion-mcp"]["needs_request"] is True and creator["notion-mcp"]["label"] == "Notion"


def test_the_store_lists_each_tool_by_the_name_a_person_reads(temp_db):
    """The browse list names each tool as the install preview does: the
    installed one's label, else the community catalog's, else its id."""
    _as(_admin())
    entry = {**ENTRY, "required_mcps": [{"name": "schedules-mcp"}, {"name": "notion-mcp"}, {"name": "navis-mcp"}]}
    with patch("services.community.community_agents_catalog.fetch_registry",
               new=AsyncMock(return_value={"agents": [entry]})), \
            patch("services.community.community_catalog.fetch_registry",
                  new=AsyncMock(return_value={"mcps": [{"name": "notion-mcp", "label": "Notion"}]})), \
            patch("services.mcp.mcp_registry.get_manifest",
                  side_effect={"schedules-mcp": SimpleNamespace(label="Schedules")}.get):
        (card,) = client.get("/v1/community/agents").json()["agents"]
    assert [(m["name"], m["label"]) for m in card["required_mcps"]] == [
        ("schedules-mcp", "Schedules"), ("notion-mcp", "Notion"), ("navis-mcp", "navis-mcp")]


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
    assert kw["server_consent"] is None
    # Without the fields nothing is consented.
    calls.clear()
    with patch("services.community.community_agent_installer.install_from_catalog", new=fake_install):
        client.post("/v1/agents/install-from-community", json={"template_slug": "pa"})
    assert calls[0]["consent_by"] == "" and calls[0]["app_consent"] is None


def test_the_dialogs_pass_the_apps_whose_server_they_showed(temp_db):
    calls: list[dict] = []

    async def fake_install(**kw):
        calls.append(kw)
        return {"agent_slug": kw["target_slug"], "agent": {}}

    async def fake_apply(agent_slug, by_sub, by_role, **kw):
        calls.append(kw)
        return {"job_id": "j", "to_version": "3.0.0"}

    _as(_admin())
    with patch("services.community.community_agent_installer.install_from_catalog", new=fake_install):
        r = client.post("/v1/agents/install-from-community", json={
            "template_slug": "pa", "approve_apps": {"home": "sig-home"}, "approve_servers": ["home"]})
    assert r.status_code == 200 and calls[-1]["server_consent"] == ["home"], r.text
    with patch("services.community.community_agent_updater.apply", new=fake_apply):
        r = client.post("/v1/agents/pa/update-template", json={
            "from_version": "2.0.0", "approve_apps": {"home": "sig-home"}, "approve_servers": ["home"]})
    assert r.status_code == 202 and calls[-1]["server_consent"] == ["home"], r.text


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

