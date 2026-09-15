"""Per-user context files (``/v1/agents/{name}/user-context``).

Minimal-router harness (auth + stores stubbed). The filename is a single
route segment, but it can still spell ``..`` or name a symlink that points
out of ``users/<u>/context/`` — both answer 403; a plain file round-trips.
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def ctx_app(tmp_path, monkeypatch):
    import config
    from api.agents import user_context  # noqa: F401 — registers the routes
    from api.agents._router import router
    from auth.providers import UserContext, get_current_user
    from core.session import visibility
    from storage.agents import agent_store
    from storage import database as task_store

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "test-agent")
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice")
    monkeypatch.setattr(visibility, "is_shared_only", lambda name: False)

    user = UserContext(
        sub="user-alice", email="alice@t.com", name="Alice", role="creator",
        agents=["test-agent"], agent_roles={"test-agent": "editor"},
    )

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _stub
    ctx_dir = agents_dir / "test-agent" / "users" / "alice" / "context"
    return TestClient(app), ctx_dir


def test_roundtrip(ctx_app):
    client, ctx_dir = ctx_app
    r = client.put("/v1/agents/test-agent/user-context/notes", json={"content": "hi"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "notes.md"
    assert (ctx_dir / "notes.md").read_text() == "hi"
    r = client.get("/v1/agents/test-agent/user-context/notes.md")
    assert r.status_code == 200 and r.json()["content"] == "hi"
    assert [f["name"] for f in client.get("/v1/agents/test-agent/user-context").json()] == ["notes.md"]
    assert client.delete("/v1/agents/test-agent/user-context/notes.md").status_code == 200
    assert not (ctx_dir / "notes.md").exists()


def test_dotdot_filename_is_403(ctx_app):
    client, _ = ctx_app
    assert client.get("/v1/agents/test-agent/user-context/%2e%2e").status_code == 403
    assert client.delete("/v1/agents/test-agent/user-context/%2e%2e").status_code == 403


def test_symlink_pointing_out_is_403(ctx_app, tmp_path):
    client, ctx_dir = ctx_app
    secret = tmp_path / "secret.md"
    secret.write_text("private")
    ctx_dir.mkdir(parents=True)
    os.symlink(secret, ctx_dir / "link.md")
    assert client.get("/v1/agents/test-agent/user-context/link.md").status_code == 403
    r = client.put("/v1/agents/test-agent/user-context/link.md", json={"content": "x"})
    assert r.status_code == 403
    assert secret.read_text() == "private"
    assert client.delete("/v1/agents/test-agent/user-context/link.md").status_code == 403
    assert secret.exists()
