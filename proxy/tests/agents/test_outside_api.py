"""The external identities routes: the manager gate, what is on offer, the
validation, a change of one field leaving the rest, the knowledge folders
and their subfolders, the wrap-up switch."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from services.mcp import mcp_manifest_store, mcp_permissions
from services.mcp.mcp_manifest_types import CredentialConfig, McpManifest, ServerConfig
from storage import database as task_store
from storage.agents import agent_store, outside_store

ADMIN, MANAGER, VIEWER = "user-admin", "user-manager", "user-viewer"
AGENT = "support"
_RO = {"readOnlyHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": True}


def _mk(name, *, server_name="", category="custom", exclude_from=()):
    return McpManifest(
        name=name, label=name.replace("-", " ").title(), description=f"{name} does things.",
        version="1.0.0", category=category,
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=list(exclude_from), skills=[], server_name=server_name,
        mcp_dir=Path(config.MCPS_DIR) / "custom" / name,
    )


@pytest.fixture
def client(temp_db, monkeypatch, tmp_path):
    from app import app
    # The platform users per conftest._seed_users: the admin, a creator who
    # manages the agent, a member who views it.
    agent_store.create_agent(AGENT, "Support", created_by=ADMIN)
    task_store.set_user_agents(MANAGER, [AGENT], ADMIN, agent_roles={AGENT: "manager"})
    task_store.set_user_agents(VIEWER, [AGENT], ADMIN, agent_roles={AGENT: "viewer"})
    # The agent's enabled tools, as the configuration view lists them.
    enabled = [_mk("crm"), _mk("file-tools", server_name="file-tools-mcp"), _mk("delegation-mcp"),
               _mk("skills-pack", category="skill"), _mk("screen-only", exclude_from=["phone"]),
               _mk("members-only", exclude_from=["external"]), _mk("own-chats-only", exclude_from=["shared_chat"])]
    monkeypatch.setattr("api.agents.outside.mcp_registry.get_agent_mcps_all_placements",
                        lambda name: list(enabled) if name == AGENT else [])
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {m.name: m for m in enabled})
    monkeypatch.setattr(mcp_permissions, "_hints", {
        "crm": {"find_contact": _RO, "update_contact": _WRITE, "unlabelled": {}},
    })
    monkeypatch.setattr(mcp_permissions, "_hints_loaded", True)
    # The agent's knowledge folders on disk, and one attached library.
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    for sub in ("knowledge/faq", "knowledge/prices/2026", "knowledge/memory", "knowledge/.credentials",
                "knowledge/shared/lib-a/guides", "knowledge/shared/lib-b/docs/api", "knowledge/shared/lib-b/other"):
        (agents_dir / AGENT / sub).mkdir(parents=True)
    monkeypatch.setattr("api.agents.outside.db_knowledge_libraries.attachments_for_consumer",
                        lambda name: [{"source_agent": "lib-a", "subdir": "", "writable": False, "name": "Lib A"},
                                      {"source_agent": "lib-b", "subdir": "docs", "writable": False, "name": ""}])
    yield TestClient(app)
    mcp_permissions.reset_cache()


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


MANAGER_H = lambda: _cookie(MANAGER, "manager@test.com", "creator")
VIEWER_H = lambda: _cookie(VIEWER, "viewer@test.com", "member")


def test_the_page_offers_the_enabled_tools_minus_the_denied_set_and_the_skills(client):
    r = client.get(f"/v1/agents/{AGENT}/outside", headers=MANAGER_H())
    assert r.status_code == 200, r.text
    page = r.json()
    assert [t["mcp"] for t in page["tools"]] == ["crm", "file-tools"]
    crm = page["tools"][0]
    assert crm["access"] == "" and crm["functions"] == [] and not crm["changed"]
    assert crm["known_functions"] == [
        {"name": "find_contact", "reads_only": True},
        {"name": "unlabelled", "reads_only": False},
        {"name": "update_contact", "reads_only": False},
    ]
    # A server whose list was never read: the page says so with null.
    assert page["tools"][1]["known_functions"] is None
    assert page["files"] == "read_only" and page["web"] is False and page["knowledge"] == []
    assert page["knowledge_choices"] == ["faq", "prices", "shared/lib-a", "shared/lib-b/docs"]


def test_only_a_tool_a_call_can_get_is_offered(client):
    """A tool its maker keeps off calls, off outside sessions or off chats that
    are not a person's own is never offered, nor accepted: picked, it would
    give a caller nothing."""
    page = client.get(f"/v1/agents/{AGENT}/outside", headers=MANAGER_H()).json()
    assert [t["mcp"] for t in page["tools"]] == ["crm", "file-tools"]
    for mcp in ("screen-only", "members-only", "own-chats-only"):
        assert _put(client, {"tools": [{"mcp": mcp, "access": "read_only"}]}).status_code == 400, mcp
    assert outside_store.get_tool_picks(AGENT) == {}


def test_a_viewer_and_an_unknown_agent_are_refused(client):
    assert client.get(f"/v1/agents/{AGENT}/outside", headers=VIEWER_H()).status_code == 403
    assert client.get("/v1/agents/nobody/outside", headers=MANAGER_H()).status_code in (403, 404)
    assert client.get(f"/v1/agents/{AGENT}/outside").status_code == 401


def _put(client, body, headers=None):
    return client.put(f"/v1/agents/{AGENT}/outside", json=body, headers=headers or MANAGER_H())


def test_the_picks_are_validated(client):
    put = lambda body: _put(client, body)
    assert put({"tools": [{"mcp": "delegation-mcp", "access": "read_only"}]}).status_code == 400
    assert put({"tools": [{"mcp": "nope", "access": "read_only"}]}).status_code == 400
    assert put({"tools": [{"mcp": "crm", "access": "all"}]}).status_code == 400
    assert put({"tools": [{"mcp": "crm", "access": "custom"}]}).status_code == 400
    assert put({"tools": [{"mcp": "crm", "access": "read_only"}, {"mcp": "crm", "access": "read_write"}]}).status_code == 400
    assert put({"files": "write"}).status_code == 400
    # The 2.0 page's whole-list field is gone: a caller still sending it is told.
    assert put({"knowledge": ["faq"]}).status_code == 422
    # Nothing was stored by a refused request.
    assert outside_store.get_tool_picks(AGENT) == {}
    assert outside_store.get_settings(AGENT)["knowledge"] == []


@pytest.mark.parametrize("folder", [
    "memory", "memory/topics", "prices/memory", ".credentials", ".credentials/x", "shared", "/faq", "faq/",
    "../etc", "faq/../prices", "missing", "prices/2027", "shared/lib-a/../../memory",
    # A library's folder outside what is attached, a source never attached.
    "shared/lib-b", "shared/lib-b/other", "shared/lib-c/x",
    # Inside an attachment but not on disk: only its own root is made by the session.
    "shared/lib-a/missing", "shared/lib-b/docs/missing",
])
def test_a_folder_outside_the_knowledge_on_offer_is_refused(client, folder):
    r = _put(client, {"add_knowledge": [folder]})
    assert r.status_code == 400, (folder, r.text)
    assert outside_store.get_settings(AGENT)["knowledge"] == []


def test_a_linked_folder_is_refused(client, tmp_path):
    knowledge = config.AGENTS_DIR / AGENT / "knowledge"
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (knowledge / "faq" / "link").symlink_to(outside)
    (knowledge / "linked").symlink_to(knowledge / "memory")
    for folder in ("faq/link", "faq/link/inner", "linked"):
        assert _put(client, {"add_knowledge": [folder]}).status_code == 400, folder
    assert outside_store.get_settings(AGENT)["knowledge"] == []


def test_subfolders_are_picked_and_a_folder_covers_its_own(client):
    r = _put(client, {"add_knowledge": ["prices/2026", "shared/lib-a/guides", "shared/lib-b/docs/api"]})
    assert r.status_code == 200, r.text
    assert r.json()["knowledge"] == ["prices/2026", "shared/lib-a/guides", "shared/lib-b/docs/api"]
    # A library's own root needs no folder on disk yet: the session makes it.
    (config.AGENTS_DIR / AGENT / "knowledge/shared/lib-b/docs/api").rename(
        config.AGENTS_DIR / AGENT / "knowledge/shared/lib-b/docs/moved")
    assert _put(client, {"add_knowledge": ["shared/lib-b/docs"]}).json()["knowledge"] == [
        "prices/2026", "shared/lib-a/guides", "shared/lib-b/docs"]
    # A folder added over its subfolder replaces it; one under a picked
    # folder adds nothing.
    page = _put(client, {"add_knowledge": ["prices", "shared/lib-a"]}).json()
    assert page["knowledge"] == ["prices", "shared/lib-a", "shared/lib-b/docs"]
    assert _put(client, {"add_knowledge": ["prices/2026"]}).json()["knowledge"] == page["knowledge"]
    assert _put(client, {"remove_knowledge": ["prices", "nothing-there"]}).json()["knowledge"] == [
        "shared/lib-a", "shared/lib-b/docs"]


def test_a_change_leaves_everything_it_does_not_name(client, monkeypatch):
    r = _put(client, {"tools": [{"mcp": "crm", "access": "read_only"},
                                {"mcp": "file-tools", "access": "custom",
                                 "functions": ["read_document", " read_document", "list "]}],
                      "files": "read_write", "web": True, "add_knowledge": ["faq"]})
    assert r.status_code == 200, r.text
    page = r.json()
    by = {t["mcp"]: t for t in page["tools"]}
    assert by["crm"]["access"] == "read_only" and by["file-tools"]["functions"] == ["list", "read_document"]
    assert page["files"] == "read_write" and page["web"] is True and page["knowledge"] == ["faq"]
    assert outside_store.get_tool_picks(AGENT)["crm"]["granted"] == ["find_contact"]
    assert client.get(f"/v1/agents/{AGENT}/outside", headers=MANAGER_H()).json() == page
    # A label change stamped the Read only pick: a change of anything else,
    # and the same choice written again, keep the stamp and what it gave.
    outside_store.mark_pick_changed(AGENT, "crm", outside_store.OUTSIDE, ["find_contact", "unlabelled"])
    for change in ({"web": False}, {"files": "read_only"}, {"tools": [{"mcp": "crm", "access": "read_only"}]},
                   {"tools": [{"mcp": "file-tools", "access": "read_write"}]}, {"wrapup_enabled": False}):
        assert _put(client, change).status_code == 200, change
    row = outside_store.get_tool_picks(AGENT)["crm"]
    assert row["changed_at"] and row["granted"] == ["find_contact", "unlabelled"]
    page = client.get(f"/v1/agents/{AGENT}/outside", headers=MANAGER_H()).json()
    assert page["web"] is False and page["files"] == "read_only" and page["knowledge"] == ["faq"]
    assert {t["mcp"]: t["access"] for t in page["tools"]} == {"crm": "read_only", "file-tools": "read_write"}
    # The tool's own choice written anew clears the stamp.
    _put(client, {"tools": [{"mcp": "crm", "access": "read_write"}]})
    assert outside_store.get_tool_picks(AGENT)["crm"]["changed_at"] == ""
    # A tool taken away goes alone.
    page = _put(client, {"remove_tools": ["crm"]}).json()
    assert {t["mcp"]: t["access"] for t in page["tools"]} == {"crm": "", "file-tools": "read_write"}
    assert _put(client, {"tools": [{"mcp": "crm", "access": "read_write"}]}, VIEWER_H()).status_code == 403


def test_a_folder_that_went_away_refuses_nothing_else(client):
    _put(client, {"add_knowledge": ["prices/2026"]})
    (config.AGENTS_DIR / AGENT / "knowledge/prices/2026").rmdir()
    r = _put(client, {"web": True})
    assert r.status_code == 200, r.text
    assert r.json()["knowledge"] == ["prices/2026"] and r.json()["web"] is True
    assert _put(client, {"remove_knowledge": ["prices/2026"]}).json()["knowledge"] == []


def test_the_wrapup_switch_is_on_until_turned_off(client):
    page = client.get(f"/v1/agents/{AGENT}/outside", headers=MANAGER_H()).json()
    assert page["wrapup_enabled"] is True and page["shared_notes"] is False
    page = _put(client, {"wrapup_enabled": False}).json()
    assert page["wrapup_enabled"] is False and outside_store.get_wrapup(AGENT)["wrapup_enabled"] is False
    page = _put(client, {"shared_notes": True}).json()
    assert page["wrapup_enabled"] is False and page["shared_notes"] is True
    assert _put(client, {"wrapup_enabled": True}).json()["wrapup_enabled"] is True


def test_only_a_tool_the_request_names_starts_its_listing(client, monkeypatch):
    """crm's functions are known; file-tools's are not: the pick on the
    latter schedules the listing, the former needs none, and a change of
    anything else starts none."""
    from services.mcp import tool_listing
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(tool_listing, "schedule", lambda agent, mcp: scheduled.append((agent, mcp)))
    r = _put(client, {"tools": [{"mcp": "crm", "access": "read_only", "functions": []},
                                {"mcp": "file-tools", "access": "custom", "functions": ["read_document"]}]})
    assert r.status_code == 200, r.text
    assert scheduled == [(AGENT, "file-tools")]
    for change in ({"web": True}, {"tools": [{"mcp": "crm", "access": "read_write"}]}):
        _put(client, change)
    assert scheduled == [(AGENT, "file-tools")]


def test_a_pick_no_session_reads_never_swallows_a_new_one_under_it(client):
    """``shared/lib-b`` picked while lib-b was attached whole, then re-attached
    with ``docs`` alone: the old pick reads nothing, and adding the folder the
    session can read replaces it instead of being taken as covered."""
    outside_store.update(AGENT, add_knowledge=["shared/lib-b", "faq"])
    page = _put(client, {"add_knowledge": ["shared/lib-b/docs"]}).json()
    assert page["knowledge"] == ["faq", "shared/lib-b/docs"]
    # A pick the session reads still covers its subfolders.
    assert _put(client, {"add_knowledge": ["shared/lib-b/docs/api"]}).json()["knowledge"] == [
        "faq", "shared/lib-b/docs"]


def _folder_app(slug, *, username="", kind="folder", methods=None, files=None, agent=AGENT):
    import json
    blocks = {}
    if methods is not None:
        blocks["exports"] = json.dumps({"methods": {
            m: ({"description": m, "min_role": f} if f else {"description": m}) for m, f in methods.items()}})
    if files is not None:
        blocks["files"] = json.dumps(files)
    row = task_store.upsert_app(agent, username, None, slug, title=slug.title(),
                                rel_path=f"workspace/apps/{slug}", kind=kind, blocks=blocks or None)
    assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), ADMIN)
    return task_store.get_app(row["id"])


def test_apps_are_added_one_change_at_a_time_and_only_from_the_agents_own(client):
    """The Apps section: the agent's shared apps with a server are offered;
    a manager adds and removes them by id; another agent's app, a personal
    one, one with no server and one reading a folder outside the picks are
    refused with the reason; a viewer is refused the route."""
    agent_store.create_agent("other", "Other", created_by=ADMIN)
    booking = _folder_app("booking", methods={"book": "external"})
    later = _folder_app("later", methods={"list": None})
    _folder_app("mine", username="manager", methods={"book": "external"})
    page_app = _folder_app("page", kind="file")
    foreign = _folder_app("foreign", agent="other", methods={"book": "external"})
    reads = _folder_app("reads", methods={"book": "external"}, files={"read": ["workspace/reports"]})
    page = client.get(f"/v1/agents/{AGENT}/outside", headers=MANAGER_H()).json()
    assert page["apps"] == []
    assert [a["slug"] for a in page["app_choices"]] == ["booking", "later", "reads"]
    assert page["app_choices"][0]["methods"] == ["book"]
    r = client.put(f"/v1/agents/{AGENT}/outside", json={"add_apps": [booking["id"], later["id"]]},
                   headers=MANAGER_H())
    assert r.status_code == 200, r.text
    apps = {a["slug"]: a for a in r.json()["apps"]}
    assert apps["booking"]["problem"] == "" and apps["booking"]["changed"] is False
    assert apps["later"]["problem"] == "This app opens nothing to external identities yet."
    for app, words in ((foreign, "not an app of this agent"), (page_app, "no server"),
                       (reads, "may not see: workspace/reports")):
        r = client.put(f"/v1/agents/{AGENT}/outside", json={"add_apps": [app["id"]]}, headers=MANAGER_H())
        assert r.status_code == 400 and words in r.json()["detail"], r.text
    # A change of something else keeps the apps.
    assert client.put(f"/v1/agents/{AGENT}/outside", json={"web": True},
                      headers=MANAGER_H()).json()["apps"][0]["slug"] == "booking"
    r = client.put(f"/v1/agents/{AGENT}/outside", json={"remove_apps": [later["id"]]}, headers=MANAGER_H())
    assert [a["slug"] for a in r.json()["apps"]] == ["booking"]
    assert client.put(f"/v1/agents/{AGENT}/outside", json={"add_apps": [booking["id"]]},
                      headers=VIEWER_H()).status_code == 403
    assert list(outside_store.get_app_picks(AGENT)) == [booking["id"]]
    # One request naming a tool and an app writes both.
    r = client.put(f"/v1/agents/{AGENT}/outside", json={
        "tools": [{"mcp": "crm", "access": "read_only"}], "add_apps": [later["id"]]}, headers=MANAGER_H())
    assert r.status_code == 200, r.text
    assert outside_store.get_tool_picks(AGENT)["crm"]["access"] == "read_only"
    assert set(outside_store.get_app_picks(AGENT)) == {booking["id"], later["id"]}
