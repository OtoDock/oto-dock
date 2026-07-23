"""File-tree role filtering (3-tier model + admin).

Regression coverage for the "empty workspace" display bug: admins used to
receive the UNFILTERED tree (every users/<name>/ dir), and the dashboard
assumed users/children[0] was the caller's own folder — so any other
user's dir sorting first showed an empty "My Workspace". `_filter_tree`
now treats admin as owner-tier: config/ visible, users/ filtered to the
admin's OWN username, exactly like manager. API-key principals stay
unfiltered (automation needs the full tree).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.agents.files import _filter_tree


def _dir(name: str, path: str, children: list | None = None) -> dict:
    return {
        "name": name, "type": "dir", "path": path, "size": 0,
        "modified": "2026-07-23T00:00:00+00:00", "children": children or [],
    }


def _file(name: str, path: str) -> dict:
    return {
        "name": name, "type": "file", "path": path, "size": 1,
        "modified": "2026-07-23T00:00:00+00:00", "children": [],
    }


def _tree() -> list[dict]:
    return [
        _dir("config", "config", [_file("prompt.md", "config/prompt.md")]),
        _dir("knowledge", "knowledge"),
        _dir("users", "users", [
            _dir("alex", "users/alex"),
            _dir("jim", "users/jim", [_dir("workspace", "users/jim/workspace")]),
        ]),
        _dir("workspace", "workspace"),
    ]


def _names(nodes: list[dict]) -> list[str]:
    return [n["name"] for n in nodes]


def _users_children(nodes: list[dict]) -> list[str]:
    users = next(n for n in nodes if n["name"] == "users")
    return [c["name"] for c in users["children"]]


def test_admin_sees_owner_tier_with_own_users_dir_only():
    filtered = _filter_tree(_tree(), "admin", username="jim")
    assert _names(filtered) == ["config", "knowledge", "users", "workspace"]
    # Other users' personal dirs are NOT in the browsing tree — even for
    # admins. (users/alex sorts before users/jim; pre-fix this made the
    # dashboard render alex's empty dir as the admin's "My Workspace".)
    assert _users_children(filtered) == ["jim"]


def test_admin_without_username_hides_users_subtree():
    filtered = _filter_tree(_tree(), "admin", username="")
    assert _names(filtered) == ["config", "knowledge", "workspace"]


def test_manager_unchanged_config_plus_own_users_dir():
    filtered = _filter_tree(_tree(), "manager", username="jim")
    assert _names(filtered) == ["config", "knowledge", "users", "workspace"]
    assert _users_children(filtered) == ["jim"]


def test_editor_and_viewer_hide_config():
    for role in ("editor", "viewer"):
        filtered = _filter_tree(_tree(), role, username="jim")
        assert _names(filtered) == ["knowledge", "users", "workspace"]
        assert _users_children(filtered) == ["jim"]


def test_source_tree_not_mutated():
    tree = _tree()
    _filter_tree(tree, "admin", username="jim")
    users = next(n for n in tree if n["name"] == "users")
    assert [c["name"] for c in users["children"]] == ["alex", "jim"]


# ---------------------------------------------------------------------------
# Endpoint level — GET /v1/agents/{name}/files
# ---------------------------------------------------------------------------


def _make_app(tmp_path, monkeypatch, *, role: str, username: str = "jim",
              is_api_key: bool = False):
    """Mount agents.router with a stubbed UserContext (mirrors
    test_agents_file_ops._make_app) and a two-user users/ dir on disk."""
    import config
    from api.agents import agents
    from auth.providers import UserContext, get_current_user
    from storage import agent_store
    from storage import database as task_store

    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "test-agent"
    for sub in ("config", "knowledge", "workspace",
                "users/alex", f"users/{username}/workspace"):
        (agent_dir / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)

    user = UserContext(
        sub=f"user-{username}-sub",
        email=f"{username}@test.com",
        name=username.title(),
        role=role if role == "admin" else "creator",
        agents=["test-agent"],
        agent_roles={"test-agent": role},
        is_api_key=is_api_key,
    )

    async def _stub_user():
        return user

    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "test-agent")
    monkeypatch.setattr(
        task_store, "get_username_by_sub",
        lambda sub: username if sub == f"user-{username}-sub" else None,
    )

    app = FastAPI()
    app.include_router(agents.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


def test_files_endpoint_admin_gets_filtered_users(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, role="admin")
    resp = TestClient(app).get("/v1/agents/test-agent/files")
    assert resp.status_code == 200
    tree = resp.json()["tree"]
    assert _users_children(tree) == ["jim"]
    assert "config" in _names(tree)


def test_files_endpoint_api_key_stays_unfiltered(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, role="admin", is_api_key=True)
    resp = TestClient(app).get("/v1/agents/test-agent/files")
    assert resp.status_code == 200
    assert _users_children(resp.json()["tree"]) == ["alex", "jim"]
