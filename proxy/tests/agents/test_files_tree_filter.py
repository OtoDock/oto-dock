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
        _dir("config", "config", [_file("agent.md", "config/agent.md")]),
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
              is_api_key: bool = False, principal_sub: str = ""):
    """Mount agents.router with a stubbed UserContext (mirrors
    test_agents_file_ops._make_app) and a two-user users/ dir on disk."""
    import config
    from api.agents import agents
    from auth.providers import UserContext, get_current_user
    from storage.agents import agent_store
    from storage import database as task_store

    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "test-agent"
    for sub in ("config", "knowledge", "workspace",
                "users/alex", f"users/{username}/workspace"):
        (agent_dir / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)

    user = UserContext(
        sub=principal_sub or f"user-{username}-sub",
        email=f"{username}@test.com",
        name=username.title(),
        role=role if role == "admin" else "creator",
        agents=["test-agent"],
        agent_roles={"test-agent": role},
        is_api_key=is_api_key,
        # A session token carries the agent it was started for (the files
        # routes bind it); the cookie principal carries none.
        agent="test-agent" if is_api_key else "",
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


def test_files_endpoint_master_key_stays_unfiltered(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, role="admin", is_api_key=True, principal_sub="api-key")
    resp = TestClient(app).get("/v1/agents/test-agent/files")
    assert resp.status_code == 200
    assert _users_children(resp.json()["tree"]) == ["alex", "jim"]


def test_files_endpoint_a_persons_session_token_is_filtered_like_their_cookie(tmp_path, monkeypatch):
    """A session token that names a person lists at that person's tier: an
    admin's own session sees the other users' private folders no more than
    the admin's cookie does."""
    app = _make_app(tmp_path, monkeypatch, role="admin", is_api_key=True)
    resp = TestClient(app).get("/v1/agents/test-agent/files")
    assert resp.status_code == 200
    assert _users_children(resp.json()["tree"]) == ["jim"]


# ---------------------------------------------------------------------------
# The scoped walk: decides at depth 1, lists only what the role sees
# ---------------------------------------------------------------------------


def _seed_disk_tree(agent_dir):
    for sub in ("config/context", "knowledge/docs", "workspace/proj/sub",
                "users/alex/workspace", "users/jim/workspace/notes",
                "shares/users/jim/s1", "app-releases/a1", "workspace/node_modules/x",
                "workspace/.hidden"):
        (agent_dir / sub).mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "agent.md").write_text("cfg")
    (agent_dir / "config" / "context" / "c.md").write_text("c")
    (agent_dir / "knowledge" / "docs" / "k.md").write_text("k")
    (agent_dir / "workspace" / "proj" / "sub" / "deep.md").write_text("d")
    (agent_dir / "workspace" / "top.md").write_text("t")
    (agent_dir / "users" / "alex" / "workspace" / "a.md").write_text("a")
    (agent_dir / "users" / "jim" / "workspace" / "notes" / "n.md").write_text("n")
    (agent_dir / "shares" / "users" / "jim" / "s1" / "chat.json").write_text("{}")
    (agent_dir / "workspace" / "node_modules" / "x" / "i.js").write_text("")
    (agent_dir / "workspace" / ".hidden" / "h").write_text("")


def test_scoped_walk_matches_full_walk_then_filter(tmp_path, monkeypatch):
    import config
    from api.agents.files import _build_tree, _build_tree_scoped
    from core.remote.file_sync import is_platform_only_tree
    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "test-agent"
    _seed_disk_tree(agent_dir)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    for role, username in (("viewer", "jim"), ("editor", "jim"), ("manager", "jim"),
                           ("admin", "jim"), ("editor", ""), ("contributor", "alex")):
        old = _build_tree(agent_dir, agent_dir, depth=1, max_depth=20)
        old = [e for e in old if not is_platform_only_tree(e["path"])]
        expected = _filter_tree(old, role, username=username)
        got = _build_tree_scoped("test-agent", role, username)
        assert got == expected, (role, username)
    full = _build_tree_scoped("test-agent", "admin", "jim", full=True)
    assert _users_children(full) == ["alex", "jim"]
    assert not {n["name"] for n in full} & {"shares", "app-releases"}


def test_tree_lists_in_tree_readable_links_only(tmp_path, monkeypatch):
    import config
    from api.agents.files import _build_tree_scoped
    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "test-agent"
    _seed_disk_tree(agent_dir)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    ws = agent_dir / "workspace"
    (ws / "alias.md").symlink_to("top.md")                           # readable target
    (ws / "alex.md").symlink_to("../users/alex/workspace/a.md")      # another user's file
    (ws / "cfg.md").symlink_to("../config/agent.md")                 # owner-only
    (ws / "dirlink").symlink_to("proj")                              # a directory
    outside = tmp_path / "outside.md"
    outside.write_text("o")
    (ws / "out.md").symlink_to(outside)                              # leaves the tree
    (ws / "dangling.md").symlink_to("nope.md")

    def _ws_names(role, username):
        tree = _build_tree_scoped("test-agent", role, username)
        ws_node = next(n for n in tree if n["name"] == "workspace")
        return {c["name"] for c in ws_node["children"]}

    editor = _ws_names("editor", "jim")
    assert "alias.md" in editor and "top.md" in editor
    assert not editor & {"alex.md", "cfg.md", "dirlink", "out.md", "dangling.md"}
    manager = _ws_names("manager", "jim")
    assert "cfg.md" in manager and "alex.md" not in manager
    tree = _build_tree_scoped("test-agent", "editor", "jim")
    alias = next(c for c in next(n for n in tree if n["name"] == "workspace")["children"]
                 if c["name"] == "alias.md")
    assert alias["type"] == "file" and alias["size"] == 1 and alias["path"] == "workspace/alias.md"


def test_tree_never_enters_other_users(tmp_path, monkeypatch):
    """Other users' folders are not walked at all: a FIFO planted there
    never blocks the listing, and their names never reach the response."""
    import os
    from api.agents.files import _build_tree_scoped
    import config
    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "test-agent"
    _seed_disk_tree(agent_dir)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    os.mkfifo(agent_dir / "users" / "alex" / "workspace" / "pipe")
    tree = _build_tree_scoped("test-agent", "editor", "jim")
    assert _users_children(tree) == ["jim"]
    flat = str(tree)
    assert "alex" not in flat and "pipe" not in flat


def test_files_endpoint_returns_json_bytes_off_the_loop(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, role="editor")
    resp = TestClient(app).get("/v1/agents/test-agent/files")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    tree = resp.json()["tree"]
    assert _names(tree) == ["knowledge", "users", "workspace"]
    assert _users_children(tree) == ["jim"]
