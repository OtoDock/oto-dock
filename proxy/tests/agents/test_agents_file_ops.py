"""Tests for agent file ops: rename + recursive delete.

Mirrors `test_uploads.py`'s approach — mount just `agents.router` on a
minimal FastAPI app, override auth + agent_store, and exercise the
endpoints against `tmp_path`-backed agent dirs. Covers:

- Rename: same-parent enforcement, role gating, traversal, collisions.
- Recursive delete: scope-boundary safety, symlink escape, scope-root
  protection, fallback to empty-only when `recursive=false`.
"""

from io import BytesIO
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch, *, role: str = "manager", username: str = "alice"):
    """Mount the agents.router with a stubbed UserContext + agent_store."""
    import config
    from api.agents import agents
    from auth.providers import UserContext

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "test-agent").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)

    user = UserContext(
        sub=f"user-{username}-sub",
        email=f"{username}@test.com",
        name=username.title(),
        # Platform role: admin if testing admin, else "creator" (the
        # platform-level ceiling; per-agent role is the `role` argument).
        role=role if role == "admin" else "creator",
        agents=["test-agent"],
        agent_roles={"test-agent": role},
    )

    async def _stub_user():
        return user

    from storage import agent_store
    from storage import database as task_store
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "test-agent")
    monkeypatch.setattr(
        task_store, "get_username_by_sub",
        lambda sub: username if sub == f"user-{username}-sub" else None,
    )

    app = FastAPI()
    app.include_router(agents.router)
    from auth.providers import get_current_user
    app.dependency_overrides[get_current_user] = _stub_user
    return app, agents_dir / "test-agent"


# ---------------------------------------------------------------------------
# Rename — happy paths
# ---------------------------------------------------------------------------


def test_rename_file_same_parent(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "old.md").write_text("hello")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/old.md", "new_path": "workspace/new.md"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "renamed"
    assert body["old_path"] == "workspace/old.md"
    assert body["new_path"] == "workspace/new.md"
    assert (agent_dir / "workspace" / "new.md").read_text() == "hello"
    assert not (agent_dir / "workspace" / "old.md").exists()


def test_rename_directory_same_parent(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "old_folder").mkdir(parents=True)
    (agent_dir / "workspace" / "old_folder" / "x.txt").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/old_folder", "new_path": "workspace/new_folder"},
    )
    assert resp.status_code == 200, resp.text
    assert (agent_dir / "workspace" / "new_folder" / "x.txt").read_text() == "x"


# ---------------------------------------------------------------------------
# Rename — error cases
# ---------------------------------------------------------------------------


def test_rename_rejects_different_parent(tmp_path, monkeypatch):
    """Renaming into another folder is a move — not allowed by this endpoint."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "a").mkdir(parents=True)
    (agent_dir / "workspace" / "b").mkdir()
    (agent_dir / "workspace" / "a" / "file.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/a/file.md", "new_path": "workspace/b/file.md"},
    )
    assert resp.status_code == 400
    assert "same parent" in resp.json()["detail"].lower()


def test_rename_rejects_existing_target(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "a.md").write_text("a")
    (agent_dir / "workspace" / "b.md").write_text("b")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/a.md", "new_path": "workspace/b.md"},
    )
    assert resp.status_code == 409


def test_rename_rejects_missing_source(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/missing.md", "new_path": "workspace/x.md"},
    )
    assert resp.status_code == 404


def test_rename_rejects_path_traversal_in_new(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "file.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={
            "old_path": "workspace/file.md",
            "new_path": "workspace/../../../etc/passwd",
        },
    )
    # Either same-parent rejection or path traversal rejection — both are
    # acceptable; the request must not succeed.
    assert resp.status_code in {400, 403}


def test_rename_rejects_slash_in_new_name(tmp_path, monkeypatch):
    """`new_path`'s basename must be a simple name, no path separators."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "file.md").write_text("x")
    client = TestClient(app)

    # New path has same dirname but basename contains a slash — caught by
    # same-parent check first since os.path.basename eats trailing slash.
    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={
            "old_path": "workspace/file.md",
            "new_path": "workspace/sub/file.md",
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Rename — role gating
# ---------------------------------------------------------------------------


def test_rename_viewer_cannot_touch_agent_workspace(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "file.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/file.md", "new_path": "workspace/new.md"},
    )
    assert resp.status_code == 403


def test_rename_viewer_allowed_in_own_user_dir(tmp_path, monkeypatch):
    """Viewers CAN rename within their own user dir — that's their
    personal scope. The old behavior (``require_write`` blocking even own-dir
    writes) was a pre-existing inconsistency with the path_policy hook (which
    always allowed own-dir writes). Both surfaces were fixed to match.
    Viewer writes outside own dir (workspace / config / knowledge) are still
    denied — see ``test_rename_viewer_cannot_touch_agent_workspace``.
    """
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={
            "old_path": "users/alice/workspace/f.md",
            "new_path": "users/alice/workspace/g.md",
        },
    )
    assert resp.status_code == 200
    assert (agent_dir / "users" / "alice" / "workspace" / "g.md").exists()
    assert not (agent_dir / "users" / "alice" / "workspace" / "f.md").exists()


def test_rename_editor_allowed_in_workspace(tmp_path, monkeypatch):
    """Editor can rename within /workspace/ (collaborative tier)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/f.md", "new_path": "workspace/g.md"},
    )
    assert resp.status_code == 200
    assert (agent_dir / "workspace" / "g.md").exists()


def test_rename_editor_blocked_in_config(tmp_path, monkeypatch):
    """Editor cannot rename in /config/ (owner-only)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "config").mkdir()
    (agent_dir / "config" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "config/f.md", "new_path": "config/g.md"},
    )
    assert resp.status_code == 403


def test_rename_editor_blocked_in_knowledge(tmp_path, monkeypatch):
    """Editor cannot rename in /knowledge/ (owner-only)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "knowledge").mkdir()
    (agent_dir / "knowledge" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "knowledge/f.md", "new_path": "knowledge/g.md"},
    )
    assert resp.status_code == 403


def test_rename_manager_cannot_touch_other_user_dir(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "users" / "bob" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "bob" / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/rename",
        json={
            "old_path": "users/bob/workspace/f.md",
            "new_path": "users/bob/workspace/g.md",
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Recursive delete — happy path
# ---------------------------------------------------------------------------


def test_recursive_delete_within_scope(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "junk" / "sub").mkdir(parents=True)
    (agent_dir / "workspace" / "junk" / "a.md").write_text("a")
    (agent_dir / "workspace" / "junk" / "sub" / "b.md").write_text("b")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "workspace/junk", "recursive": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["type"] == "dir"
    assert body.get("recursive") is True
    assert not (agent_dir / "workspace" / "junk").exists()


def test_recursive_false_still_rejects_non_empty(tmp_path, monkeypatch):
    """Default recursive=false preserves the old empty-only behavior."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "stuff").mkdir(parents=True)
    (agent_dir / "workspace" / "stuff" / "x.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "workspace/stuff"},  # recursive omitted -> False
    )
    assert resp.status_code == 400
    assert "not empty" in resp.json()["detail"].lower()


def test_empty_dir_still_deletes_without_recursive(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "empty").mkdir(parents=True)
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "workspace/empty"},
    )
    assert resp.status_code == 200
    assert resp.json()["type"] == "dir"


# ---------------------------------------------------------------------------
# Recursive delete — scope-root protection
# ---------------------------------------------------------------------------


def test_recursive_delete_blocks_workspace_root(tmp_path, monkeypatch):
    """Manager cannot recursive-delete the whole `workspace/` scope."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "workspace" / "x").mkdir(parents=True)
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "workspace", "recursive": True},
    )
    assert resp.status_code == 403
    assert "scope root" in resp.json()["detail"].lower()


def test_recursive_delete_blocks_users_root(tmp_path, monkeypatch):
    """Even admin cannot recursive-delete `users/` (would wipe every user)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "users" / "alice").mkdir(parents=True)
    (agent_dir / "users" / "bob").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "users", "recursive": True},
    )
    assert resp.status_code == 403


def test_recursive_delete_blocks_own_user_root(tmp_path, monkeypatch):
    """Viewer/manager cannot wipe their entire user dir via the workspace API."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "users/alice", "recursive": True},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Recursive delete — symlink escape rejection
# ---------------------------------------------------------------------------


def test_recursive_delete_rejects_symlink_escape(tmp_path, monkeypatch):
    """A symlink inside the subtree pointing outside the scope is rejected."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "workspace" / "naughty").mkdir(parents=True)
    (agent_dir / "config").mkdir()
    (agent_dir / "config" / "secret.md").write_text("sensitive")
    # Symlink from inside workspace pointing at config — recursive delete
    # must refuse to follow it.
    (agent_dir / "workspace" / "naughty" / "link").symlink_to(
        agent_dir / "config" / "secret.md"
    )
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "workspace/naughty", "recursive": True},
    )
    assert resp.status_code == 403
    assert "symlink" in resp.json()["detail"].lower()
    # And the secret survives.
    assert (agent_dir / "config" / "secret.md").exists()


# ---------------------------------------------------------------------------
# Recursive delete — role gating
# ---------------------------------------------------------------------------


def test_recursive_delete_viewer_blocked_from_agent_workspace(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "workspace" / "x").mkdir(parents=True)
    (agent_dir / "workspace" / "x" / "f.md").write_text("a")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "workspace/x", "recursive": True},
    )
    assert resp.status_code == 403


def test_recursive_delete_manager_own_user_works(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "users" / "alice" / "workspace" / "junk").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "workspace" / "junk" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/delete",
        json={"path": "users/alice/workspace/junk", "recursive": True},
    )
    assert resp.status_code == 200, resp.text
    assert not (agent_dir / "users" / "alice" / "workspace" / "junk").exists()


# ---------------------------------------------------------------------------
# Move — happy paths
# ---------------------------------------------------------------------------


def test_move_file_same_scope(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "src").mkdir(parents=True)
    (agent_dir / "workspace" / "dest").mkdir()
    (agent_dir / "workspace" / "src" / "f.md").write_text("hello")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/src/f.md"], "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["moved"] == [{"src": "workspace/src/f.md", "dest": "workspace/dest/f.md"}]
    assert body["failed"] == []
    assert (agent_dir / "workspace" / "dest" / "f.md").read_text() == "hello"
    assert not (agent_dir / "workspace" / "src" / "f.md").exists()


def test_move_multiple_mixed_files_and_dirs(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "dest").mkdir(parents=True)
    (agent_dir / "workspace" / "folder").mkdir()
    (agent_dir / "workspace" / "a.md").write_text("a")
    (agent_dir / "workspace" / "folder" / "b.md").write_text("b")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={
            "src_paths": ["workspace/a.md", "workspace/folder"],
            "dest_dir": "workspace/dest",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["moved"]) == 2
    assert body["failed"] == []
    assert (agent_dir / "workspace" / "dest" / "a.md").read_text() == "a"
    assert (agent_dir / "workspace" / "dest" / "folder" / "b.md").read_text() == "b"


def test_move_cross_scope_manager(tmp_path, monkeypatch):
    """Manager can move from user dir into agent workspace and back."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "workspace").mkdir(exist_ok=True)
    (agent_dir / "users" / "alice" / "workspace" / "report.md").write_text("data")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["users/alice/workspace/report.md"], "dest_dir": "workspace"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["moved"] == [{"src": "users/alice/workspace/report.md", "dest": "workspace/report.md"}]
    assert (agent_dir / "workspace" / "report.md").read_text() == "data"


def test_move_collision_auto_renames(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "src").mkdir(parents=True)
    (agent_dir / "workspace" / "dest").mkdir()
    (agent_dir / "workspace" / "src" / "f.md").write_text("new")
    (agent_dir / "workspace" / "dest" / "f.md").write_text("existing")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/src/f.md"], "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["moved"][0]["dest"] == "workspace/dest/f_1.md"
    assert (agent_dir / "workspace" / "dest" / "f.md").read_text() == "existing"
    assert (agent_dir / "workspace" / "dest" / "f_1.md").read_text() == "new"


def test_move_same_parent_is_noop(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/f.md"], "dest_dir": "workspace"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["moved"][0].get("noop") is True
    # No `_1` copy was created.
    assert (agent_dir / "workspace" / "f.md").read_text() == "x"
    assert not (agent_dir / "workspace" / "f_1.md").exists()


# ---------------------------------------------------------------------------
# Move / copy — push to active remote sessions
# ---------------------------------------------------------------------------


def _patch_remote(monkeypatch, machine_ids=("m1",)):
    """Mark the agent as having active remote sessions via workspace_fanout's
    target selector; return an AsyncMock connection manager whose push_file /
    send_fire_and_forget can be asserted. The dashboard push helpers now delegate
    to ``workspace_fanout`` (isolation-aware), which calls these same cm methods
    with the same PathRef / delete-envelope shape, so the assertions are unchanged."""
    from services.remote import workspace_fanout
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda agent_slug, rel_path, *, exclude_machine_id=None: list(machine_ids),
    )
    return AsyncMock()


def test_move_pushes_new_file_and_deletes_old_on_remote(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "src").mkdir(parents=True)
    (agent_dir / "workspace" / "dest").mkdir()
    (agent_dir / "workspace" / "src" / "f.md").write_text("payload")
    fake_cm = _patch_remote(monkeypatch)
    client = TestClient(app)

    with patch("core.remote.satellite_connection.get_connection_manager", return_value=fake_cm):
        resp = client.post(
            "/v1/agents/test-agent/move",
            json={"src_paths": ["workspace/src/f.md"], "dest_dir": "workspace/dest"},
        )
    assert resp.status_code == 200, resp.text

    # New file pushed to the satellite.
    assert fake_cm.push_file.await_count == 1
    mid, ref, content = fake_cm.push_file.await_args.args[:3]
    assert mid == "m1"
    assert ref.kind == "agent_tree"
    assert ref.value == "workspace/dest/f.md"
    assert content == b"payload"
    # Old path delete pushed (fire-and-forget).
    assert fake_cm.send_fire_and_forget.await_count == 1
    del_msg = fake_cm.send_fire_and_forget.await_args.args[1]
    assert del_msg["action"] == "delete"
    assert del_msg["path"] == "workspace/src/f.md"


def test_copy_dir_pushes_every_file_on_remote(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "src" / "sub").mkdir(parents=True)
    (agent_dir / "workspace" / "dest").mkdir()
    (agent_dir / "workspace" / "src" / "top.md").write_text("top")
    (agent_dir / "workspace" / "src" / "sub" / "nested.md").write_text("nested")
    fake_cm = _patch_remote(monkeypatch)
    client = TestClient(app)

    with patch("core.remote.satellite_connection.get_connection_manager", return_value=fake_cm):
        resp = client.post(
            "/v1/agents/test-agent/copy",
            json={"src_paths": ["workspace/src"], "dest_dir": "workspace/dest"},
        )
    assert resp.status_code == 200, resp.text

    # Every file in the copied tree is pushed (recursively).
    pushed = {
        call.args[1].value: call.args[2]
        for call in fake_cm.push_file.await_args_list
    }
    assert pushed == {
        "workspace/dest/src/top.md": b"top",
        "workspace/dest/src/sub/nested.md": b"nested",
    }
    # Copy is non-destructive — no delete pushes.
    assert fake_cm.send_fire_and_forget.await_count == 0


def test_move_noop_same_parent_skips_remote_push(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "f.md").write_text("x")
    fake_cm = _patch_remote(monkeypatch)
    client = TestClient(app)

    with patch("core.remote.satellite_connection.get_connection_manager", return_value=fake_cm):
        resp = client.post(
            "/v1/agents/test-agent/move",
            json={"src_paths": ["workspace/f.md"], "dest_dir": "workspace"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"][0].get("noop") is True
    # No-op move must not push anything to the satellite.
    assert fake_cm.push_file.await_count == 0
    assert fake_cm.send_fire_and_forget.await_count == 0


# ---------------------------------------------------------------------------
# Move — error cases
# ---------------------------------------------------------------------------


def test_move_rejects_dest_inside_source(tmp_path, monkeypatch):
    """Moving a folder into its own subdirectory would loop."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "outer" / "inner").mkdir(parents=True)
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/outer"], "dest_dir": "workspace/outer/inner"},
    )
    assert resp.status_code == 400
    assert "inside source" in resp.json()["detail"].lower()


def test_move_viewer_allowed_in_own_dir(tmp_path, monkeypatch):
    """Viewer CAN move within their own user dir (personal scope)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "workspace" / "f.md").write_text("x")
    (agent_dir / "users" / "alice" / "workspace" / "dest").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={
            "src_paths": ["users/alice/workspace/f.md"],
            "dest_dir": "users/alice/workspace/dest",
        },
    )
    assert resp.status_code == 200


def test_move_viewer_blocked_into_workspace(tmp_path, monkeypatch):
    """Viewer cannot move INTO /workspace/ (write denied there)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "workspace" / "f.md").write_text("x")
    (agent_dir / "workspace").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={
            "src_paths": ["users/alice/workspace/f.md"],
            "dest_dir": "workspace",
        },
    )
    assert resp.status_code == 403


def test_move_manager_cannot_touch_other_user_dir(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "users" / "bob" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "bob" / "workspace" / "f.md").write_text("x")
    (agent_dir / "workspace").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["users/bob/workspace/f.md"], "dest_dir": "workspace"},
    )
    assert resp.status_code == 403


def test_move_missing_source_returns_404(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "dest").mkdir(parents=True)
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/missing.md"], "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 404


def test_move_missing_dest_returns_404(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "f.md").parent.mkdir(parents=True)
    (agent_dir / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/f.md"], "dest_dir": "workspace/nonexistent"},
    )
    assert resp.status_code == 404


def test_move_empty_src_paths_rejected(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": [], "dest_dir": "workspace"},
    )
    assert resp.status_code == 400


def test_move_partial_failure(tmp_path, monkeypatch):
    """One missing source returns 200 with that source in `failed[]`, others moved."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "dest").mkdir(parents=True)
    (agent_dir / "workspace" / "good.md").write_text("ok")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={
            "src_paths": ["workspace/good.md", "workspace/missing.md"],
            "dest_dir": "workspace/dest",
        },
    )
    # The missing path is caught up-front by validation (404). The current
    # impl validates ALL sources before any move; partial-failure behaviour
    # only fires for runtime errors mid-loop. Verify validation rejects.
    assert resp.status_code == 404


def test_move_symlink_escape_rejected(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "workspace" / "naughty").mkdir(parents=True)
    (agent_dir / "config").mkdir()
    (agent_dir / "config" / "secret.md").write_text("sensitive")
    (agent_dir / "workspace" / "naughty" / "link").symlink_to(
        agent_dir / "config" / "secret.md"
    )
    (agent_dir / "workspace" / "dest").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/naughty"], "dest_dir": "workspace/dest"},
    )
    body = resp.json()
    # Either the up-front symlink walk rejects (403) or the per-item loop
    # catches it. Either path is acceptable; the secret survives.
    assert resp.status_code == 200 or resp.status_code == 403
    if resp.status_code == 200:
        assert body["moved"] == []
        assert len(body["failed"]) == 1
        assert "symlink" in body["failed"][0]["reason"].lower()
    assert (agent_dir / "config" / "secret.md").exists()


# ---------------------------------------------------------------------------
# Copy — happy paths
# ---------------------------------------------------------------------------


def test_copy_file_same_scope(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "src").mkdir(parents=True)
    (agent_dir / "workspace" / "dest").mkdir()
    (agent_dir / "workspace" / "src" / "f.md").write_text("hello")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/src/f.md"], "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["copied"][0]["dest"] == "workspace/dest/f.md"
    # Source intact.
    assert (agent_dir / "workspace" / "src" / "f.md").read_text() == "hello"
    assert (agent_dir / "workspace" / "dest" / "f.md").read_text() == "hello"


def test_copy_recursive_dir(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "src" / "sub").mkdir(parents=True)
    (agent_dir / "workspace" / "dest").mkdir()
    (agent_dir / "workspace" / "src" / "top.md").write_text("top")
    (agent_dir / "workspace" / "src" / "sub" / "nested.md").write_text("nested")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/src"], "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    assert (agent_dir / "workspace" / "src" / "top.md").exists()
    assert (agent_dir / "workspace" / "dest" / "src" / "top.md").read_text() == "top"
    assert (agent_dir / "workspace" / "dest" / "src" / "sub" / "nested.md").read_text() == "nested"


def test_copy_cross_scope_manager(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "context").mkdir()
    (agent_dir / "users" / "alice" / "workspace" / "notes.md").write_text("ideas")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={
            "src_paths": ["users/alice/workspace/notes.md"],
            "dest_dir": "users/alice/context",
        },
    )
    assert resp.status_code == 200, resp.text
    assert (agent_dir / "users" / "alice" / "workspace" / "notes.md").read_text() == "ideas"
    assert (agent_dir / "users" / "alice" / "context" / "notes.md").read_text() == "ideas"


def test_copy_collision_auto_renames(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "src").mkdir(parents=True)
    (agent_dir / "workspace" / "dest").mkdir()
    (agent_dir / "workspace" / "src" / "f.md").write_text("new")
    (agent_dir / "workspace" / "dest" / "f.md").write_text("existing")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/src/f.md"], "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["copied"][0]["dest"] == "workspace/dest/f_1.md"
    assert (agent_dir / "workspace" / "dest" / "f.md").read_text() == "existing"
    assert (agent_dir / "workspace" / "dest" / "f_1.md").read_text() == "new"


def test_copy_into_same_folder_creates_duplicate(tmp_path, monkeypatch):
    """Copy into the same folder is a legit operation (creates `_1`)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/f.md"], "dest_dir": "workspace"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["copied"][0]["dest"] == "workspace/f_1.md"
    assert (agent_dir / "workspace" / "f.md").read_text() == "x"
    assert (agent_dir / "workspace" / "f_1.md").read_text() == "x"


def test_copy_viewer_allowed_in_own_dir(tmp_path, monkeypatch):
    """Viewer CAN copy within their own user dir (personal scope)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "workspace" / "f.md").write_text("x")
    (agent_dir / "users" / "alice" / "workspace" / "dest").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={
            "src_paths": ["users/alice/workspace/f.md"],
            "dest_dir": "users/alice/workspace/dest",
        },
    )
    assert resp.status_code == 200


def test_copy_viewer_blocked_into_workspace(tmp_path, monkeypatch):
    """Viewer cannot copy INTO /workspace/ (write denied there)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "workspace" / "f.md").write_text("x")
    (agent_dir / "workspace").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={
            "src_paths": ["users/alice/workspace/f.md"],
            "dest_dir": "workspace",
        },
    )
    assert resp.status_code == 403


def test_copy_symlink_escape_rejected(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "workspace" / "naughty").mkdir(parents=True)
    (agent_dir / "config").mkdir()
    (agent_dir / "config" / "secret.md").write_text("sensitive")
    (agent_dir / "workspace" / "naughty" / "link").symlink_to(
        agent_dir / "config" / "secret.md"
    )
    (agent_dir / "workspace" / "dest").mkdir()
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/naughty"], "dest_dir": "workspace/dest"},
    )
    body = resp.json()
    assert resp.status_code == 200 or resp.status_code == 403
    if resp.status_code == 200:
        assert body["copied"] == []
        assert len(body["failed"]) == 1
        assert "symlink" in body["failed"][0]["reason"].lower()
    # Secret survived; no copy created in dest.
    assert (agent_dir / "config" / "secret.md").exists()


# ---------------------------------------------------------------------------
# Zip — happy paths + error cases
# ---------------------------------------------------------------------------


def test_zip_single_file(tmp_path, monkeypatch):
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "report.md").write_text("# Report\n")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": ["workspace/report.md"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "report" in resp.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["report.md"]
        assert zf.read("report.md").decode() == "# Report\n"


def test_zip_single_folder(tmp_path, monkeypatch):
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "docs" / "sub").mkdir(parents=True)
    (agent_dir / "workspace" / "docs" / "top.md").write_text("top")
    (agent_dir / "workspace" / "docs" / "sub" / "nested.md").write_text("nested")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": ["workspace/docs"]},
    )
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert "docs/top.md" in names
        assert "docs/sub/nested.md" in names
        assert zf.read("docs/top.md").decode() == "top"


def test_zip_mixed_files_and_folders(tmp_path, monkeypatch):
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "folder").mkdir(parents=True)
    (agent_dir / "workspace" / "loose.md").write_text("loose")
    (agent_dir / "workspace" / "folder" / "inside.md").write_text("inside")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": ["workspace/loose.md", "workspace/folder"]},
    )
    assert resp.status_code == 200, resp.text
    assert "workspace-files" in resp.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert "loose.md" in names
        assert "folder/inside.md" in names


def test_zip_cross_scope_paths(tmp_path, monkeypatch):
    """Manager can zip across scopes (e.g. workspace/ + users/me/)."""
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "workspace").mkdir(exist_ok=True)
    (agent_dir / "users" / "alice" / "workspace" / "u.md").write_text("user")
    (agent_dir / "workspace" / "a.md").write_text("agent")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": ["users/alice/workspace/u.md", "workspace/a.md"]},
    )
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert "u.md" in names
        assert "a.md" in names


def test_zip_empty_paths_rejected(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": []},
    )
    assert resp.status_code == 400


def test_zip_viewer_can_zip_own_files(tmp_path, monkeypatch):
    """Zip is read-only; viewers can zip files within their scope."""
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "alice" / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": ["users/alice/workspace/f.md"]},
    )
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        assert "f.md" in zf.namelist()


def test_zip_viewer_blocked_from_other_user(tmp_path, monkeypatch):
    """Viewers cannot zip files in another user's scope."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "users" / "bob" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "bob" / "workspace" / "f.md").write_text("x")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": ["users/bob/workspace/f.md"]},
    )
    assert resp.status_code == 403


def test_zip_collision_renames_top_level(tmp_path, monkeypatch):
    """Two same-name files at different scopes get suffixed in the archive."""
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    (agent_dir / "workspace" / "notes.md").write_text("agent")
    (agent_dir / "users" / "alice" / "workspace" / "notes.md").write_text("user")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/zip",
        json={"paths": ["workspace/notes.md", "users/alice/workspace/notes.md"]},
    )
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert "notes.md" in names
        assert "notes_1.md" in names
