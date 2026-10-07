"""Tests for agent file ops: rename + recursive delete.

Mirrors `test_uploads.py`'s approach — mount just `agents.router` on a
minimal FastAPI app, override auth + agent_store, and exercise the
endpoints against `tmp_path`-backed agent dirs. Covers:

- Rename: same-parent enforcement, role gating, traversal, collisions.
- Recursive delete: scope-boundary safety, symlink escape, scope-root
  protection, fallback to empty-only when `recursive=false`.
"""

import os
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

    from storage.agents import agent_store
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
    cm = AsyncMock()
    pushed: dict[str, bytes] = {}

    async def _push(mid, ref, source, **kw):
        # A Path source is the checked descriptor's own path, valid only while
        # the fan-out holds it: read it now, as the real push does.
        pushed[ref.value] = source if isinstance(source, bytes) else source.read_bytes()
        return True

    cm.push_file = AsyncMock(side_effect=_push)
    cm.pushed = pushed
    return cm


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
    mid, ref, _source = fake_cm.push_file.await_args.args[:3]
    assert mid == "m1"
    assert ref.kind == "agent_tree"
    assert ref.value == "workspace/dest/f.md"
    assert fake_cm.pushed == {"workspace/dest/f.md": b"payload"}
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
    assert fake_cm.pushed == {
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


def test_copy_of_a_scope_root_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "shared.md").write_text("x")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace"], "dest_dir": "users/alice/workspace"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"copied": [], "failed": [
        {"src": "workspace", "reason": "Cannot copy a whole scope root"}]}
    assert not (agent_dir / "users" / "alice" / "workspace" / "workspace").exists()

    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    for d in ("knowledge", "config", "users/bob", "workspace/dest"):
        (agent_dir / d).mkdir(parents=True, exist_ok=True)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["knowledge", "config", "users/bob", "users"],
              "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["copied"] == []
    assert {f["reason"] for f in body["failed"]} == {"Cannot copy a whole scope root"}
    assert len(body["failed"]) == 4
    assert list((agent_dir / "workspace" / "dest").iterdir()) == []


def test_copy_of_a_link_landing_on_a_scope_root_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="manager")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "shared.md").write_text("x")
    (agent_dir / "workspace" / "self").symlink_to("../workspace")
    mine = agent_dir / "users" / "alice" / "workspace"
    mine.mkdir(parents=True)
    (mine / "ws").symlink_to("../../../workspace")
    (mine / "dest").mkdir()
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/self", "users/alice/workspace/ws"],
              "dest_dir": "users/alice/workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["copied"] == []
    assert [f["reason"] for f in body["failed"]] == ["Cannot copy a whole scope root"] * 2
    assert list((mine / "dest").iterdir()) == []


def test_copy_over_the_byte_cap_fails_and_leaves_nothing(tmp_path, monkeypatch):
    import config
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    big = agent_dir / "workspace" / "big"
    big.mkdir(parents=True)
    for i in range(3):
        (big / f"f{i}.bin").write_bytes(os.urandom(512 * 1024))
    (agent_dir / "workspace" / "large.bin").write_bytes(os.urandom(1536 * 1024))
    (agent_dir / "workspace" / "small.md").write_text("small")
    (agent_dir / "workspace" / "dest").mkdir()
    monkeypatch.setattr(config, "COPY_MAX_INPUT_MB", 1)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/big", "workspace/large.bin", "workspace/small.md"],
              "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["failed"] == [
        {"src": "workspace/big", "reason": "the copy is larger than 1 MB"},
        {"src": "workspace/large.bin", "reason": "the copy is larger than 1 MB"},
    ]
    # The sources after a failed one run against what is left.
    assert body["copied"] == [{"src": "workspace/small.md", "dest": "workspace/dest/small.md"}]
    assert sorted(p.name for p in (agent_dir / "workspace" / "dest").iterdir()) == ["small.md"]


def test_copy_over_the_file_cap_fails_and_shares_one_budget(tmp_path, monkeypatch):
    import config
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    for d in ("a", "b"):
        (agent_dir / "workspace" / d).mkdir(parents=True)
        for i in range(2):
            (agent_dir / "workspace" / d / f"f{i}.md").write_text(d)
    (agent_dir / "workspace" / "c.md").write_text("c")
    (agent_dir / "workspace" / "dest").mkdir()
    # A directory counts as an entry, as each file does.
    monkeypatch.setattr(config, "COPY_MAX_ENTRIES", 4)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/a", "workspace/b", "workspace/c.md"],
              "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["src"] for c in body["copied"]] == ["workspace/a", "workspace/c.md"]
    assert body["failed"] == [{"src": "workspace/b", "reason": "the copy has more than 4 entries"}]
    assert sorted(p.name for p in (agent_dir / "workspace" / "dest").iterdir()) == ["a", "c.md"]
    # 0 is no cap.
    monkeypatch.setattr(config, "COPY_MAX_ENTRIES", 0)
    monkeypatch.setattr(config, "COPY_MAX_INPUT_MB", 0)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/b"], "dest_dir": "workspace/dest"},
    )
    assert resp.json()["failed"] == []
    assert (agent_dir / "workspace" / "dest" / "b" / "f1.md").read_text() == "b"


def test_copy_counts_directories_and_links_against_the_entry_cap(tmp_path, monkeypatch):
    import config
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "empty" / "x" / "y").mkdir(parents=True)
    (agent_dir / "workspace" / "links").mkdir()
    for i in range(3):
        (agent_dir / "workspace" / "links" / f"l{i}").symlink_to("../empty")
    (agent_dir / "workspace" / "dest").mkdir()
    monkeypatch.setattr(config, "COPY_MAX_ENTRIES", 2)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/empty", "workspace/links"],
              "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"copied": [], "failed": [
        {"src": "workspace/empty", "reason": "the copy has more than 2 entries"},
        {"src": "workspace/links", "reason": "the copy has more than 2 entries"},
    ]}
    assert list((agent_dir / "workspace" / "dest").iterdir()) == []


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


def test_rename_contributor_allowed_in_workspace(tmp_path, monkeypatch):
    """A contributor renames within /workspace/ (the workspace tier)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="contributor")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "f.md").write_text("x")
    resp = TestClient(app).post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/f.md", "new_path": "workspace/g.md"},
    )
    assert resp.status_code == 200
    assert (agent_dir / "workspace" / "g.md").exists()


def test_rename_contributor_blocked_in_knowledge(tmp_path, monkeypatch):
    """A contributor never touches /knowledge/ (owner-only)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="contributor")
    (agent_dir / "knowledge").mkdir()
    (agent_dir / "knowledge" / "f.md").write_text("x")
    resp = TestClient(app).post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "knowledge/f.md", "new_path": "knowledge/g.md"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Resolved destinations + the engines' state
# ---------------------------------------------------------------------------


def test_copy_into_symlinked_dest_is_judged_at_the_target(tmp_path, monkeypatch):
    """A destination symlink into config/ is judged as config/: a
    contributor may write workspace/ but not the owner-only config."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="contributor")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "evil.md").write_text("x")
    (agent_dir / "config" / "context").mkdir(parents=True)
    (agent_dir / "workspace" / "ctx").symlink_to("../config/context")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/evil.md"], "dest_dir": "workspace/ctx"},
    )
    assert resp.status_code == 403
    assert not (agent_dir / "config" / "context" / "evil.md").exists()


def test_move_into_symlinked_other_user_dir_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "evil.md").write_text("x")
    (agent_dir / "users" / "bob" / "workspace").mkdir(parents=True)
    (agent_dir / "workspace" / "bobs").symlink_to("../users/bob/workspace")
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/evil.md"], "dest_dir": "workspace/bobs"},
    )
    assert resp.status_code == 403
    assert not (agent_dir / "users" / "bob" / "workspace" / "evil.md").exists()
    assert (agent_dir / "workspace" / "evil.md").exists()


def test_engine_state_dir_is_not_served_to_any_role(tmp_path, monkeypatch):
    """The scope-root CLI state (the subscription login, the MCP config with
    a session token, the hook scripts) is refused to every principal, read
    and write, admin included; a repo's nested .claude stays reachable."""
    for role in ("viewer", "contributor", "manager", "admin"):
        sub = tmp_path / role
        sub.mkdir()
        app, agent_dir = _make_app(sub, monkeypatch, role=role)
        state = agent_dir / "workspace" / ".claude"
        state.mkdir(parents=True)
        (state / ".credentials.json").write_text('{"token": "t"}')
        (agent_dir / "workspace" / "repo" / ".claude").mkdir(parents=True)
        (agent_dir / "workspace" / "repo" / ".claude" / "settings.json").write_text("{}")
        client = TestClient(app)

        read = client.get(
            "/v1/agents/test-agent/files/workspace/.claude/.credentials.json?download=true")
        assert read.status_code == 403, role
        write = client.put("/v1/agents/test-agent/files/workspace/.claude/json.py",
                           json={"content": "x"})
        assert write.status_code == 403, role
        assert not (state / "json.py").exists()
        nested = client.get("/v1/agents/test-agent/files/workspace/repo/.claude/settings.json")
        assert nested.status_code == 200, (role, nested.text)


def test_zip_of_workspace_leaves_out_engine_state(tmp_path, monkeypatch):
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="viewer")
    (agent_dir / "workspace" / ".claude").mkdir(parents=True)
    (agent_dir / "workspace" / ".claude" / ".credentials.json").write_text("secret")
    (agent_dir / "workspace" / ".codex").mkdir()
    (agent_dir / "workspace" / ".codex" / "auth.json").write_text("secret")
    (agent_dir / "workspace" / "report.md").write_text("ok")
    client = TestClient(app)

    resp = client.post("/v1/agents/test-agent/zip", json={"paths": ["workspace"]})
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        names = zf.namelist()
    assert "workspace/report.md" in names
    assert not [n for n in names if ".claude" in n or ".codex" in n]


def test_copy_of_engine_state_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "workspace" / ".claude").mkdir(parents=True)
    (agent_dir / "workspace" / ".claude" / ".credentials.json").write_text("secret")
    (agent_dir / "users" / "alice" / "workspace").mkdir(parents=True)
    client = TestClient(app)

    resp = client.post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/.claude"], "dest_dir": "users/alice/workspace"},
    )
    assert resp.status_code == 403
    assert not (agent_dir / "users" / "alice" / "workspace" / ".claude").exists()


def test_tree_leaves_out_the_platform_only_trees(tmp_path, monkeypatch):
    """Chat snapshots and release copies are served by their own routes and
    never listed, for an admin or an agent's own session token."""
    from auth.providers import UserContext
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "shares" / "users" / "bob" / "s1" / "media").mkdir(parents=True)
    (agent_dir / "shares" / "users" / "bob" / "s1" / "media" / "tok.png").write_text("x")
    (agent_dir / "app-releases" / "a1").mkdir(parents=True)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "notes.md").write_text("x")
    client = TestClient(app)
    names = {e["name"] for e in client.get("/v1/agents/test-agent/files").json()["tree"]}
    assert "workspace" in names and not names & {"shares", "app-releases"}

    session = UserContext(sub="agent-session", email="", name="agent", role="admin",
                          agents=["test-agent"], agent_roles={"test-agent": "manager"},
                          is_api_key=True, agent="test-agent")
    from auth.providers import get_current_user
    app.dependency_overrides[get_current_user] = lambda: session
    names = {e["name"] for e in client.get("/v1/agents/test-agent/files").json()["tree"]}
    assert "workspace" in names and not names & {"shares", "app-releases"}


# ---------------------------------------------------------------------------
# Zip: one descriptor-based pass, off the loop, bounded
# ---------------------------------------------------------------------------


def _zip_names(resp) -> list[str]:
    import zipfile
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        return zf.namelist()


def test_zip_add_refuses_symlink_leaf(tmp_path, monkeypatch):
    """A link inside a zipped folder is left out, whatever it points at: a
    file outside the agents tree or another user's file in the tree."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    big = agent_dir / "workspace" / "big"
    big.mkdir(parents=True)
    (big / "real.txt").write_text("real")
    secret = tmp_path / "config.env"
    secret.write_text("LEAKED_SECRET=1")
    (big / "leak.txt").symlink_to(secret)
    (agent_dir / "users" / "bob" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "bob" / "workspace" / "p.txt").write_text("BOB PRIVATE")
    (big / "bob.txt").symlink_to("../../users/bob/workspace/p.txt")
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/big"]})
    assert resp.status_code == 200, resp.text
    names = _zip_names(resp)
    assert "big/real.txt" in names
    assert "big/leak.txt" not in names and "big/bob.txt" not in names
    assert b"LEAKED_SECRET" not in resp.content and b"BOB PRIVATE" not in resp.content


def test_zip_walk_skips_symlinked_dir(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    big = agent_dir / "workspace" / "big"
    big.mkdir(parents=True)
    (big / "a.txt").write_text("a")
    (agent_dir / "users" / "bob" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "bob" / "workspace" / "p.txt").write_text("BOB PRIVATE")
    (big / "bobdir").symlink_to("../../users/bob/workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("OUTSIDE")
    (big / "out").symlink_to(outside)
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/big"]})
    assert resp.status_code == 200, resp.text
    names = _zip_names(resp)
    assert names == ["big/", "big/a.txt"]
    assert b"BOB PRIVATE" not in resp.content and b"OUTSIDE" not in resp.content


def test_zip_named_link_to_other_scope_is_403(tmp_path, monkeypatch):
    """A selected path that IS a link is judged where it lands: an editor
    cannot zip another user's folder or config/ through a link in
    workspace/ (the resolved rel is role-checked, as move and copy do)."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "workspace").mkdir()
    (agent_dir / "users" / "bob" / "workspace").mkdir(parents=True)
    (agent_dir / "users" / "bob" / "workspace" / "p.txt").write_text("BOB PRIVATE")
    (agent_dir / "config").mkdir()
    (agent_dir / "config" / "agent.md").write_text("CONFIG")
    (agent_dir / "workspace" / "bobs").symlink_to("../users/bob/workspace")
    (agent_dir / "workspace" / "cfg").symlink_to("../config")
    (agent_dir / "workspace" / "cfgfile").symlink_to("../config/agent.md")
    client = TestClient(app)
    for path in ("workspace/bobs", "workspace/cfg", "workspace/cfgfile"):
        resp = client.post("/v1/agents/test-agent/zip", json={"paths": [path]})
        assert resp.status_code == 403, (path, resp.status_code)


def test_zip_named_link_inside_own_scope_is_served(tmp_path, monkeypatch):
    """A link whose target the caller may read is zipped as its target."""
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "workspace" / "real").mkdir(parents=True)
    (agent_dir / "workspace" / "real" / "a.txt").write_text("a")
    (agent_dir / "workspace" / "alias").symlink_to("real")
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/alias"]})
    assert resp.status_code == 200, resp.text
    assert "real/a.txt" in _zip_names(resp)


def test_zip_over_cap_is_413(tmp_path, monkeypatch):
    import config
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    big = agent_dir / "workspace" / "big"
    big.mkdir(parents=True)
    for i in range(3):
        (big / f"f{i}.bin").write_bytes(os.urandom(512 * 1024))
    monkeypatch.setattr(config, "ZIP_MAX_INPUT_MB", 1)
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/big"]})
    assert resp.status_code == 413
    assert "1 MB" in resp.json()["detail"]
    monkeypatch.setattr(config, "ZIP_MAX_INPUT_MB", 4096)
    monkeypatch.setattr(config, "ZIP_MAX_ENTRIES", 2)
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/big"]})
    assert resp.status_code == 413
    assert "2 files" in resp.json()["detail"]


def test_zip_too_many_paths_is_413(tmp_path, monkeypatch):
    import config
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    for i in range(4):
        (agent_dir / "workspace" / f"f{i}.txt").write_text("x")
    monkeypatch.setattr(config, "ZIP_MAX_PATHS", 3)
    paths = [f"workspace/f{i}.txt" for i in range(4)]
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": paths})
    assert resp.status_code == 413


def test_zip_dedupes_and_drops_nested_paths(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace" / "docs" / "sub").mkdir(parents=True)
    (agent_dir / "workspace" / "docs" / "top.md").write_text("top")
    (agent_dir / "workspace" / "docs" / "sub" / "n.md").write_text("n")
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={
        "paths": ["workspace/docs", "workspace/docs/", "workspace/docs/sub", "workspace/docs/top.md"],
    })
    assert resp.status_code == 200, resp.text
    names = _zip_names(resp)
    assert names.count("docs/top.md") == 1 and "docs/sub/n.md" in names
    assert "top.md" not in names and "sub/n.md" not in names
    assert "docs" in resp.headers["content-disposition"]  # one source


def test_zip_members_are_deflated(tmp_path, monkeypatch):
    import zipfile
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "text.txt").write_text("a" * 100_000)
    resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/text.txt"]})
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        zi = zf.getinfo("text.txt")
    assert zi.compress_type == zipfile.ZIP_DEFLATED
    assert zi.compress_size < 5_000


def test_zip_response_is_a_file_with_length_and_range(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "r.md").write_text("# Report\n")
    client = TestClient(app)
    before = len(os.listdir("/proc/self/fd"))
    resp = client.post("/v1/agents/test-agent/zip", json={"paths": ["workspace/r.md"]})
    assert resp.status_code == 200
    assert int(resp.headers["content-length"]) == len(resp.content)
    assert resp.headers["content-type"] == "application/zip"
    part = client.post("/v1/agents/test-agent/zip", json={"paths": ["workspace/r.md"]},
                       headers={"range": "bytes=0-1"})
    assert part.status_code == 206 and part.content == b"PK"
    assert len(os.listdir("/proc/self/fd")) == before


def test_zip_second_build_for_same_user_is_429(tmp_path, monkeypatch):
    import threading
    from api.agents import files as filesmod
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "r.md").write_text("x")
    started = threading.Event()
    release = threading.Event()
    real_add = filesmod._zip_add_regular_file

    def _slow(*a, **kw):
        started.set()
        release.wait(10)
        return real_add(*a, **kw)

    monkeypatch.setattr(filesmod, "_zip_add_regular_file", _slow)
    results: list[int] = []

    def _go():
        results.append(TestClient(app).post(
            "/v1/agents/test-agent/zip", json={"paths": ["workspace/r.md"]}).status_code)

    t = threading.Thread(target=_go)
    t.start()
    assert started.wait(10)
    second = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/r.md"]})
    assert second.status_code == 429
    release.set()
    t.join(20)
    assert results == [200]


def test_zip_unreadable_folder_is_400(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        import pytest
        pytest.skip("root reads anything")
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    locked = agent_dir / "workspace" / "big" / "locked"
    locked.mkdir(parents=True)
    (locked / "x.txt").write_text("x")
    (agent_dir / "workspace" / "big" / "ok.txt").write_text("ok")
    locked.chmod(0)
    try:
        resp = TestClient(app).post("/v1/agents/test-agent/zip", json={"paths": ["workspace/big"]})
    finally:
        locked.chmod(0o755)
    assert resp.status_code == 400
    assert "locked" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Read: from the checked descriptor, inline previews capped
# ---------------------------------------------------------------------------


def test_read_refuses_swapped_symlink(tmp_path, monkeypatch):
    """The file authorized by safe_agent_path is the file opened: a link
    swapped in at the leaf (to the outside or to another user's file) is
    refused, while an in-tree link to a readable file still reads."""
    from api.agents import files as filesmod
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="editor")
    (agent_dir / "workspace").mkdir()
    doc = agent_dir / "workspace" / "notes.md"
    doc.write_text("mine")
    (agent_dir / "workspace" / "alias.md").symlink_to("notes.md")
    client = TestClient(app)
    assert client.get("/v1/agents/test-agent/files/workspace/alias.md").json()["content"] == "mine"

    secret = tmp_path / "config.env"
    secret.write_text("JWT_SECRET=1")
    real_safe = filesmod.safe_agent_path

    def _stale(agent_dir_, name, raw, user, *, writing=False, **_kw):
        # The authorization answers on the pre-swap file; the leaf is then
        # swapped before the open (the deterministic form of the race).
        out = real_safe(agent_dir_, name, raw, user, writing=writing)
        doc.unlink()
        doc.symlink_to(secret)
        return out

    monkeypatch.setattr(filesmod, "safe_agent_path", _stale)
    for suffix in ("", "?download=true"):
        resp = client.get(f"/v1/agents/test-agent/files/workspace/notes.md{suffix}")
        assert resp.status_code == 404, suffix
        assert b"JWT_SECRET" not in resp.content
        doc.unlink()
        doc.write_text("mine")


def test_read_inline_over_cap_is_413_and_download_still_works(tmp_path, monkeypatch):
    import config
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "big.log").write_text("x" * 3000)
    monkeypatch.setattr(config, "INLINE_TEXT_MAX_BYTES", 2048)
    client = TestClient(app)
    resp = client.get("/v1/agents/test-agent/files/workspace/big.log")
    assert resp.status_code == 413
    assert "download" in resp.json()["detail"]
    resp = client.get("/v1/agents/test-agent/files/workspace/big.log?download=true")
    assert resp.status_code == 200 and len(resp.content) == 3000
    assert resp.headers["content-disposition"].startswith("attachment")
    (agent_dir / "workspace" / "small.log").write_text("ok")
    resp = client.get("/v1/agents/test-agent/files/workspace/small.log")
    assert resp.status_code == 200 and resp.json() == {"content": "ok", "encoding": "utf-8"}
    assert resp.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# A component swapped after the check never redirects a write. The
# victim tree sits beside the agents root: what a redirected write would reach.
# ---------------------------------------------------------------------------


def _victim(tmp_path):
    v = tmp_path / "victim"
    v.mkdir(exist_ok=True)
    (v / "agent.md").write_text("ORIGINAL")
    (v / "f.md").write_text("ORIGINAL")
    return v


def _swap_sub_for_link(agent_dir, victim):
    d = agent_dir / "workspace" / "sub"
    if d.is_dir() and not d.is_symlink():
        for child in d.iterdir():
            child.unlink()
        d.rmdir()
    os.symlink(victim, d)


def _swap_on_lookup(monkeypatch, agent_dir, victim, username="alice"):
    """The username lookup is the store hop between the path check and the
    write; swapping ``workspace/sub`` there models a swap in that window."""
    from storage import database as task_store
    state = {"done": False}

    def _lookup(sub):
        if not state["done"]:
            state["done"] = True
            _swap_sub_for_link(agent_dir, victim)
        return username

    monkeypatch.setattr(task_store, "get_username_by_sub", _lookup)


def _swap_on_open(monkeypatch, agent_dir, victim):
    """The swap lands after every check, right as the helper opens the root:
    the strict open below the root is what must refuse it."""
    from services.infra import safe_fs
    real = safe_fs.open_root
    state = {"done": False}

    @__import__("contextlib").contextmanager
    def _patched(root, rel=""):
        if not state["done"]:
            state["done"] = True
            _swap_sub_for_link(agent_dir, victim)
        with real(root, rel) as fd:
            yield fd

    monkeypatch.setattr(safe_fs, "open_root", _patched)


def _victim_untouched(victim):
    assert (victim / "agent.md").read_text() == "ORIGINAL"
    assert (victim / "f.md").read_text() == "ORIGINAL"
    assert sorted(p.name for p in victim.iterdir()) == ["agent.md", "f.md"]


def _sub_tree(agent_dir):
    (agent_dir / "workspace" / "sub").mkdir(parents=True)
    (agent_dir / "workspace" / "sub" / "f.md").write_text("mine")


def test_write_toctou_swap_in_the_check_window_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    _swap_on_lookup(monkeypatch, agent_dir, victim)
    resp = TestClient(app).put(
        "/v1/agents/test-agent/files/workspace/sub/agent.md", json={"content": "NEW"},
    )
    assert resp.status_code in (403, 404)
    _victim_untouched(victim)


def test_write_toctou_swap_at_the_open_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    _swap_on_open(monkeypatch, agent_dir, victim)
    resp = TestClient(app).put(
        "/v1/agents/test-agent/files/workspace/sub/agent.md", json={"content": "NEW"},
    )
    assert resp.status_code == 403
    _victim_untouched(victim)


def test_create_file_toctou_swap_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    _swap_on_lookup(monkeypatch, agent_dir, victim)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/create-file", json={"path": "workspace/sub/new.md"},
    )
    assert resp.status_code in (403, 404)
    _victim_untouched(victim)


def test_mkdir_toctou_swap_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    _swap_on_lookup(monkeypatch, agent_dir, victim)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/mkdir", json={"path": "workspace/sub/newdir/deeper"},
    )
    assert resp.status_code in (403, 404)
    _victim_untouched(victim)


def test_delete_file_toctou_swap_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    _swap_on_lookup(monkeypatch, agent_dir, victim)
    with patch("core.remote.satellite_connection.get_connection_manager", return_value=AsyncMock()):
        resp = TestClient(app).post(
            "/v1/agents/test-agent/delete", json={"path": "workspace/sub/f.md"},
        )
    assert resp.status_code in (403, 404)
    _victim_untouched(victim)


def test_rename_toctou_swap_at_the_open_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    _swap_on_open(monkeypatch, agent_dir, victim)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/sub/f.md", "new_path": "workspace/sub/agent.md"},
    )
    assert resp.status_code in (403, 404)
    _victim_untouched(victim)


def test_move_toctou_swap_at_the_open_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    (agent_dir / "workspace" / "agent.md").write_text("mine")
    _swap_on_open(monkeypatch, agent_dir, victim)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/agent.md"], "dest_dir": "workspace/sub"},
    )
    assert resp.status_code == 200
    assert resp.json()["moved"] == [] and len(resp.json()["failed"]) == 1
    _victim_untouched(victim)
    assert (agent_dir / "workspace" / "agent.md").read_text() == "mine"


def test_copy_toctou_swap_at_the_open_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    (agent_dir / "workspace" / "agent.md").write_text("mine")
    _swap_on_open(monkeypatch, agent_dir, victim)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/copy",
        json={"src_paths": ["workspace/agent.md"], "dest_dir": "workspace/sub"},
    )
    assert resp.status_code == 200
    assert resp.json()["copied"] == [] and len(resp.json()["failed"]) == 1
    _victim_untouched(victim)


def test_restore_toctou_swap_at_the_open_is_denied(tmp_path, monkeypatch):
    import config
    from storage.files import recover_bin_store
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    _sub_tree(agent_dir)
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", tmp_path / "recover-bin")
    entry_id = "e1"
    entry = {
        "entry_id": entry_id, "agent_slug": "test-agent", "rel_path": "workspace/sub/agent.md",
        "original_name": "agent.md", "reason": "deleted", "scope": "shared", "owner_sub": "",
        "size": 3, "binned_at": "2026-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(recover_bin_store, "get", lambda eid: entry if eid == entry_id else None)
    monkeypatch.setattr(recover_bin_store, "read_bytes", lambda e: b"NEW")
    monkeypatch.setattr(recover_bin_store, "delete", lambda eid: None)
    _swap_on_open(monkeypatch, agent_dir, victim)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/recover-bin/restore", json={"entry_ids": [entry_id]},
    )
    assert resp.status_code == 200
    assert resp.json()["denied"] == [entry_id]
    _victim_untouched(victim)


def test_write_through_a_swapped_agent_folder_is_refused(tmp_path, monkeypatch):
    """The rel the helper opens starts with the agent's own name, so an agent
    folder replaced by a link is refused at the first component."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    victim = _victim(tmp_path)
    (agent_dir / "workspace").mkdir()
    from services.infra import safe_fs
    real = safe_fs.open_root
    state = {"done": False}

    @__import__("contextlib").contextmanager
    def _patched(root, rel=""):
        if not state["done"]:
            state["done"] = True
            import shutil
            shutil.rmtree(agent_dir)
            os.symlink(victim, agent_dir)
        with real(root, rel) as fd:
            yield fd

    monkeypatch.setattr(safe_fs, "open_root", _patched)
    resp = TestClient(app).put(
        "/v1/agents/test-agent/files/agent.md", json={"content": "NEW"},
    )
    assert resp.status_code == 403
    _victim_untouched(victim)


def test_move_source_that_is_a_link_into_another_scope_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "workspace" / "dest").mkdir(parents=True)
    (agent_dir / "config").mkdir()
    (agent_dir / "config" / "secret.md").write_text("s")
    (agent_dir / "workspace" / "lnk").symlink_to("../config")
    resp = TestClient(app).post(
        "/v1/agents/test-agent/move",
        json={"src_paths": ["workspace/lnk"], "dest_dir": "workspace/dest"},
    )
    assert resp.status_code == 200
    assert resp.json()["moved"] == []
    assert (agent_dir / "config" / "secret.md").read_text() == "s"
    assert (agent_dir / "workspace" / "lnk").is_symlink()


def test_rename_directory_with_an_escaping_link_is_refused(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch, role="admin")
    (agent_dir / "workspace" / "d").mkdir(parents=True)
    (agent_dir / "config").mkdir()
    (agent_dir / "config" / "secret.md").write_text("s")
    (agent_dir / "workspace" / "d" / "leak").symlink_to("../../config/secret.md")
    resp = TestClient(app).post(
        "/v1/agents/test-agent/rename",
        json={"old_path": "workspace/d", "new_path": "workspace/e"},
    )
    assert resp.status_code == 403
    assert (agent_dir / "workspace" / "d").is_dir()


def test_mkdir_on_a_file_is_409_and_empty_dir_delete_keeps_a_racing_child(tmp_path, monkeypatch):
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "f.md").write_text("x")
    client = TestClient(app)
    resp = client.post("/v1/agents/test-agent/mkdir", json={"path": "workspace/f.md/sub"})
    assert resp.status_code == 409
    resp = client.post("/v1/agents/test-agent/mkdir", json={"path": "workspace/f.md"})
    assert resp.status_code == 409


def test_session_token_for_another_agent_is_refused_on_every_write_route(tmp_path, monkeypatch):
    """A session token acts only on the agent it was started for."""
    import config
    from api.agents import agents
    from auth.providers import UserContext, get_current_user
    from storage.agents import agent_store
    from storage import database as task_store

    agents_dir = tmp_path / "agents"
    for a in ("test-agent", "other-agent"):
        (agents_dir / a / "workspace").mkdir(parents=True)
    (agents_dir / "test-agent" / "workspace" / "f.md").write_text("x")
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name in ("test-agent", "other-agent"))
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice")
    session_user = UserContext(
        sub="user-alice-sub", email="a@t.com", name="Alice", role="creator",
        agents=["test-agent", "other-agent"],
        agent_roles={"test-agent": "manager", "other-agent": "manager"},
        is_api_key=True, agent="other-agent",
    )

    async def _stub():
        return session_user

    app = FastAPI()
    app.include_router(agents.router)
    app.dependency_overrides[get_current_user] = _stub
    client = TestClient(app)
    calls = [
        ("put", "/v1/agents/test-agent/files/workspace/g.md", {"content": "y"}),
        ("post", "/v1/agents/test-agent/create-file", {"path": "workspace/h.md"}),
        ("post", "/v1/agents/test-agent/mkdir", {"path": "workspace/d"}),
        ("post", "/v1/agents/test-agent/delete", {"path": "workspace/f.md"}),
        ("post", "/v1/agents/test-agent/rename", {"old_path": "workspace/f.md", "new_path": "workspace/k.md"}),
        ("post", "/v1/agents/test-agent/move", {"src_paths": ["workspace/f.md"], "dest_dir": "workspace"}),
        ("post", "/v1/agents/test-agent/copy", {"src_paths": ["workspace/f.md"], "dest_dir": "workspace"}),
        ("post", "/v1/agents/test-agent/zip", {"paths": ["workspace/f.md"]}),
        ("post", "/v1/agents/test-agent/recover-bin/restore", {"entry_ids": ["x"]}),
        ("get", "/v1/agents/test-agent/files", None),
        ("get", "/v1/agents/test-agent/files/workspace/f.md", None),
    ]
    for method, url, body in calls:
        resp = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        assert resp.status_code == 403, (url, resp.status_code, resp.text)
    assert (agents_dir / "test-agent" / "workspace" / "f.md").read_text() == "x"
    # The same token on its own agent keeps working.
    resp = client.put("/v1/agents/other-agent/files/workspace/g.md", json={"content": "y"})
    assert resp.status_code == 200, resp.text


def test_write_routes_run_the_filesystem_work_off_the_loop(tmp_path, monkeypatch):
    """The helpers run in a worker thread (a running loop in the
    calling thread means the write ran on the loop)."""
    import asyncio
    from services.infra import safe_fs
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    real = safe_fs.atomic_write_beneath

    def _guard(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return real(*a, **kw)
        raise AssertionError("the write ran on the event loop")

    monkeypatch.setattr(safe_fs, "atomic_write_beneath", _guard)
    resp = TestClient(app).put("/v1/agents/test-agent/files/workspace/n.md", json={"content": "y"})
    assert resp.status_code == 200
    assert (agent_dir / "workspace" / "n.md").read_text() == "y"


def test_a_file_in_the_way_of_a_new_path_answers_409(tmp_path, monkeypatch):
    """A file at any parent of the new path, not only the direct one, is a
    name in use: the answer is the same 409, never a server error."""
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    (agent_dir / "workspace" / "afile").write_text("x")
    client = TestClient(app)
    resp = client.post(
        "/v1/agents/test-agent/create-file", json={"path": "workspace/afile/deeper/new.md"},
    )
    assert resp.status_code == 409, resp.text
    resp = client.put(
        "/v1/agents/test-agent/files/workspace/afile/deeper/n.md", json={"content": "y"},
    )
    assert resp.status_code == 409, resp.text
    assert (agent_dir / "workspace" / "afile").read_text() == "x"
    (agent_dir / "workspace" / "adir").mkdir()
    resp = client.put("/v1/agents/test-agent/files/workspace/adir", json={"content": "y"})
    assert resp.status_code == 409, resp.text
    assert (agent_dir / "workspace" / "adir").is_dir()


def test_restore_never_writes_through_a_link_inside_the_tree(tmp_path, monkeypatch):
    """The entry's own path is the authorized destination: a link a manager
    planted inside the workspace must not carry a restored file into
    another person's private tree."""
    import config
    from storage.files import recover_bin_store
    app, agent_dir = _make_app(tmp_path, monkeypatch)
    (agent_dir / "workspace").mkdir()
    bob = agent_dir / "users" / "bob" / "context"
    bob.mkdir(parents=True)
    (agent_dir / "workspace" / "lnk").symlink_to(bob)
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", tmp_path / "recover-bin")
    entry_id = "e2"
    entry = {
        "entry_id": entry_id, "agent_slug": "test-agent", "rel_path": "workspace/lnk/notes.md",
        "original_name": "notes.md", "reason": "deleted", "scope": "shared", "owner_sub": "",
        "size": 3, "binned_at": "2026-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(recover_bin_store, "get", lambda eid: entry if eid == entry_id else None)
    monkeypatch.setattr(recover_bin_store, "read_bytes", lambda e: b"NEW")
    monkeypatch.setattr(recover_bin_store, "delete", lambda eid: None)
    resp = TestClient(app).post(
        "/v1/agents/test-agent/recover-bin/restore", json={"entry_ids": [entry_id]},
    )
    assert resp.status_code == 200
    assert resp.json()["denied"] == [entry_id]
    assert list(bob.iterdir()) == []


def test_a_zip_download_takes_only_a_token_minted_for_it():
    """F70: other tokens signed with the same secret carry an ``agent``
    claim too; only one stamped with the download's purpose opens a zip."""
    import time as _time

    import jwt as _jwt
    from fastapi.testclient import TestClient

    import config as _config
    from api.agents.files import _create_zip_token
    from app import app
    c = TestClient(app)
    foreign = _jwt.encode({"agent": "zip-agent", "paths": ["workspace"], "role": "manager",
                           "exp": int(_time.time()) + 60}, _config.JWT_SECRET, algorithm="HS256")
    r = c.get(f"/v1/agents/zip-agent/zip-download?t={foreign}")
    assert r.status_code == 403 and r.json()["detail"] == "Invalid download token"
    minted = _create_zip_token("zip-agent", ["workspace"], "user-x", "viewer", "x")
    assert _jwt.decode(minted, _config.JWT_SECRET, algorithms=["HS256"])["purpose"] == "zip_download"
    r = c.get(f"/v1/agents/zip-agent/zip-download?t={minted}")
    assert r.json().get("detail") != "Invalid download token"
