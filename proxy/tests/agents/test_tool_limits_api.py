"""The tool-limits routes: the manager gate, the three columns, the
validation, a change of one column leaving the rest, the outside column
read here and written elsewhere, a session token that may only tighten,
and the listing a Read only limit schedules."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from services.mcp import mcp_manifest_store, mcp_permissions
from services.mcp.mcp_manifest_types import CredentialConfig, McpManifest, ServerConfig
from storage import database as task_store
from storage.agents import agent_store, outside_store
from tests.conftest import live_session_token

ADMIN, MANAGER, VIEWER = "user-admin", "user-manager", "user-viewer"
AGENT = "limited"
_RO = {"readOnlyHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": True}


def _mk(name, *, server_name="", category="custom"):
    return McpManifest(
        name=name, label=name.replace("-", " ").title(), description=f"{name} does things.",
        version="1.0.0", category=category,
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], server_name=server_name,
        mcp_dir=Path(config.MCPS_DIR) / "custom" / name,
    )


@pytest.fixture
def client(temp_db, monkeypatch):
    from app import app
    agent_store.create_agent(AGENT, "Limited", created_by=ADMIN)
    task_store.set_user_agents(MANAGER, [AGENT], ADMIN, agent_roles={AGENT: "manager"})
    task_store.set_user_agents(VIEWER, [AGENT], ADMIN, agent_roles={AGENT: "viewer"})
    enabled = [_mk("crm"), _mk("mail", server_name="mail-srv"), _mk("skills-pack", category="skill")]
    monkeypatch.setattr("api.agents.tool_limits.mcp_registry.get_agent_mcps_all_placements",
                        lambda name: list(enabled) if name == AGENT else [])
    monkeypatch.setattr("api.agents.outside.mcp_registry.get_agent_mcps_all_placements",
                        lambda name: list(enabled) if name == AGENT else [])
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {m.name: m for m in enabled})
    monkeypatch.setattr(mcp_permissions, "_hints", {
        "crm": {"find": _RO, "update": _WRITE, "bare": {}},
    })
    monkeypatch.setattr(mcp_permissions, "_hints_loaded", True)
    yield TestClient(app)
    mcp_permissions.reset_cache()


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


MANAGER_H = lambda: _cookie(MANAGER, "manager@test.com", "creator")
VIEWER_H = lambda: _cookie(VIEWER, "viewer@test.com", "member")


def _put(client, body, headers=None):
    return client.put(f"/v1/agents/{AGENT}/tool-limits", json=body, headers=headers or MANAGER_H())


def test_the_page_lists_every_enabled_tool_with_its_three_columns(client):
    r = client.get(f"/v1/agents/{AGENT}/tool-limits", headers=MANAGER_H())
    assert r.status_code == 200, r.text
    tools = r.json()["tools"]
    assert [t["mcp"] for t in tools] == ["crm", "mail"]
    crm = tools[0]
    assert crm["label"] == "Crm" and crm["outside"] is None and crm["runs"] is None and crm["chats"] is None
    assert crm["known_functions"] == [
        {"name": "bare", "reads_only": False}, {"name": "find", "reads_only": True},
        {"name": "update", "reads_only": False}]
    assert tools[1]["known_functions"] is None
    assert client.get(f"/v1/agents/{AGENT}/tool-limits", headers=VIEWER_H()).status_code == 403
    assert client.get(f"/v1/agents/{AGENT}/tool-limits").status_code == 401
    assert client.get("/v1/agents/nobody/tool-limits", headers=MANAGER_H()).status_code in (403, 404)


def test_a_limit_is_written_per_column_and_leaves_the_rest(client, monkeypatch):
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr("services.mcp.tool_listing.schedule", lambda agent, mcp: scheduled.append((agent, mcp)))
    r = _put(client, {"tools": [{"mcp": "crm", "runs": {"access": "read_only"},
                                 "chats": {"access": "custom", "functions": ["find", "update", "find"]}}]})
    assert r.status_code == 200, r.text
    crm = r.json()["tools"][0]
    assert crm["runs"] == {"access": "read_only", "functions": [], "changed": False}
    assert crm["chats"] == {"access": "custom", "functions": ["find", "update"], "changed": False}
    assert crm["outside"] is None
    assert outside_store.get_tool_picks(AGENT, governs="runs")["crm"]["granted"] == ["find"]
    assert outside_store.get_tool_picks(AGENT) == {}
    assert scheduled == []   # crm is listed
    # One column changed alone: the other stays.
    r = _put(client, {"tools": [{"mcp": "crm", "runs": {"access": "custom", "functions": ["find"]}}]})
    crm = r.json()["tools"][0]
    assert crm["runs"]["access"] == "custom" and crm["chats"]["functions"] == ["find", "update"]
    # A Read only limit on a server never listed schedules its listing.
    r = _put(client, {"tools": [{"mcp": "mail", "chats": {"access": "read_only"}}]})
    assert r.status_code == 200 and scheduled == [(AGENT, "mail")]
    assert outside_store.get_tool_picks(AGENT, governs="chats")["mail"]["granted"] == []
    # A removal takes one column's row.
    r = _put(client, {"remove": [{"mcp": "crm", "governs": "chats"}]})
    crm = r.json()["tools"][0]
    assert crm["chats"] is None and crm["runs"]["access"] == "custom"


def test_the_limits_are_validated(client):
    assert _put(client, {"tools": [{"mcp": "crm", "runs": {"access": "read_write"}}]}).status_code == 400
    assert "read only or custom" in _put(client, {"tools": [{"mcp": "crm", "runs": {"access": "read_write"}}]}).json()["detail"]
    assert _put(client, {"tools": [{"mcp": "nope", "runs": {"access": "read_only"}}]}).status_code == 400
    assert _put(client, {"tools": [{"mcp": "skills-pack", "runs": {"access": "read_only"}}]}).status_code == 400
    assert _put(client, {"tools": [{"mcp": "crm", "runs": {"access": "custom", "functions": []}}]}).status_code == 400
    assert _put(client, {"tools": [{"mcp": "crm", "outside": {"access": "read_only"}}]}).status_code == 422
    assert _put(client, {"remove": [{"mcp": "crm", "governs": "outside"}]}).status_code == 400
    assert _put(client, {"remove": [{"mcp": "nope", "governs": "runs"}]}).status_code == 400
    assert _put(client, {"tools": [{"mcp": "crm", "runs": {"access": "read_only"}}]}, VIEWER_H()).status_code == 403
    assert outside_store.get_tool_picks(AGENT, governs="runs") == {}


def test_a_session_token_only_tightens(client):
    token = live_session_token("sess-limits-1", AGENT, MANAGER)
    session = {"Authorization": f"Bearer {token}"}
    # Adding a limit where none exists: allowed.
    r = _put(client, {"tools": [{"mcp": "crm", "chats": {"access": "custom", "functions": ["find", "update"]}}]}, session)
    assert r.status_code == 200, r.text
    # Narrowing the list: allowed.
    r = _put(client, {"tools": [{"mcp": "crm", "chats": {"access": "custom", "functions": ["find"]}}]}, session)
    assert r.status_code == 200
    # Widening, changing the kind, or removing: refused with the sentence.
    for body in ({"tools": [{"mcp": "crm", "chats": {"access": "custom", "functions": ["find", "bare"]}}]},
                 {"tools": [{"mcp": "crm", "chats": {"access": "read_only"}}]},
                 {"remove": [{"mcp": "crm", "governs": "chats"}]}):
        r = _put(client, body, session)
        assert r.status_code == 403, body
        assert r.json()["detail"] == "Only a manager, from Agent Settings, can widen what the agent may use."
    assert outside_store.get_tool_picks(AGENT, governs="chats")["crm"]["functions"] == ["find"]
    # A manager at the dashboard may.
    assert _put(client, {"remove": [{"mcp": "crm", "governs": "chats"}]}).status_code == 200
    assert outside_store.get_tool_picks(AGENT, governs="chats") == {}


def test_the_outside_route_still_writes_its_own_column(client):
    r = client.put(f"/v1/agents/{AGENT}/outside", json={"tools": [{"mcp": "crm", "access": "read_only"}]},
                   headers=MANAGER_H())
    assert r.status_code == 200, r.text
    page = client.get(f"/v1/agents/{AGENT}/tool-limits", headers=MANAGER_H()).json()
    assert page["tools"][0]["outside"] == {"access": "read_only", "functions": [], "changed": False}
    assert page["tools"][0]["runs"] is None
