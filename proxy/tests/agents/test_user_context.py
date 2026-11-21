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
    from storage import database as task_store
    from storage.agents import agent_store

    agents_dir = tmp_path / "agents"
    (agents_dir / "test-agent").mkdir(parents=True)
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


def test_a_context_file_past_the_cap_is_not_read_whole(ctx_app):
    client, ctx_dir = ctx_app
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "big.md").write_bytes(b"x" * (1024 * 1024 + 1))
    assert client.get("/v1/agents/test-agent/user-context/big.md").status_code == 413


def test_a_link_inside_the_context_folder_still_works(ctx_app):
    """A person's own link to another file of their context folder reads
    and writes the file it names, as before."""
    client, ctx_dir = ctx_app
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "real.md").write_text("real")
    os.symlink("real.md", ctx_dir / "alias.md")
    assert client.get("/v1/agents/test-agent/user-context/alias.md").json()["content"] == "real"
    assert client.put("/v1/agents/test-agent/user-context/alias.md", json={"content": "new"}).status_code == 200
    assert (ctx_dir / "real.md").read_text() == "new"


def test_a_context_folder_replaced_by_a_link_is_never_written_through(ctx_app, tmp_path):
    from api.agents import user_context
    client, ctx_dir = ctx_app
    ctx_dir.mkdir(parents=True)
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    root, rel = user_context._context_place("test-agent", "alice")
    judged = user_context._file_rel

    def judge_then_swap(*a):
        # The folder replaced by a link between the judgement and the write.
        answer = judged(*a)
        ctx_dir.rename(tmp_path / "aside")
        ctx_dir.symlink_to(stand_in)
        return answer
    import unittest.mock
    with unittest.mock.patch.object(user_context, "_file_rel", judge_then_swap), pytest.raises(OSError):
        user_context._write_context_file(root, rel, "notes.md", "x")
    assert list(stand_in.iterdir()) == []


def test_a_link_from_the_context_folder_to_another_folder_of_the_agent_is_403(ctx_app):
    """A link inside the agent's folder but out of the person's context
    folder (another person's workspace) is refused like one out of the
    agent."""
    client, ctx_dir = ctx_app
    ctx_dir.mkdir(parents=True)
    bob = ctx_dir.parent.parent / "bob" / "workspace"
    bob.mkdir(parents=True)
    (bob / "private.md").write_text("BOB")
    os.symlink(bob / "private.md", ctx_dir / "peek.md")
    r = client.get("/v1/agents/test-agent/user-context/peek.md")
    assert r.status_code == 403 and "BOB" not in r.text


def test_an_agent_behind_the_operators_link_lists_reads_and_writes_context(ctx_app, tmp_path):
    """An agent folder an admin moved behind a link (the operator's own
    layout) keeps its context files: written, listed and read in the real
    folder, as on any other agent."""
    client, _ = ctx_app
    agent = tmp_path / "agents" / "test-agent"
    real = tmp_path / "disk2" / "test-agent"
    real.parent.mkdir()
    agent.rename(real)
    agent.symlink_to(real)
    assert client.put("/v1/agents/test-agent/user-context/notes", json={"content": "hi"}).status_code == 200
    assert (real / "users" / "alice" / "context" / "notes.md").read_text() == "hi"
    assert [f["name"] for f in client.get("/v1/agents/test-agent/user-context").json()] == ["notes.md"]
    assert client.get("/v1/agents/test-agent/user-context/notes.md").json()["content"] == "hi"
    assert client.delete("/v1/agents/test-agent/user-context/notes.md").status_code == 200
    assert not (real / "users" / "alice" / "context" / "notes.md").exists()
