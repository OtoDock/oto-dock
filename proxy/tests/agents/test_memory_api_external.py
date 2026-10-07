"""The memory API for external sessions: only the caller's own scope."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


def _seed_agent(slug: str, *, collaborative: bool = True, default_scope: str = "user") -> None:
    from storage.agents import agent_store
    if not agent_store.agent_exists(slug):
        agent_store.create_agent(slug, slug.title(), default_scope=default_scope)
    if not collaborative:
        from storage.pg import get_conn
        with get_conn() as c:
            c.execute("UPDATE agents SET collaborative = FALSE WHERE slug = %s", (slug,))
            c.commit()


def _headers(agent: str, claim: str) -> dict:
    from tests.conftest import live_session_token
    token = live_session_token(str(uuid.uuid4()), agent, external=claim)
    return {"Authorization": f"Bearer {token}", "X-Agent-Name": agent}


def _op(client, agent, claim, **body):
    return client.post("/v1/internal/memory/op", json=body, headers=_headers(agent, claim))


def test_caller_scope_only(client):
    import config
    _seed_agent("ext-mem")
    claim = "phone:+302101234567"
    r = _op(client, "ext-mem", claim, command="view", path="/memories")
    assert r.status_code == 200 and not r.json()["is_error"]
    listing = r.json()["output"]
    assert "user/" in listing and "agent/" not in listing

    r = _op(client, "ext-mem", claim, command="create", path="/memories/user/name.md",
            file_text="# Name\nThe caller is Maria.\n")
    assert r.status_code == 200 and not r.json()["is_error"], r.json()
    saved = (config.AGENTS_DIR / "ext-mem" / "externals" / "phone" / "302101234567"
             / "context" / "memory" / "name.md")
    assert saved.is_file()

    r = _op(client, "ext-mem", claim, command="create", path="/memories/agent/x.md",
            file_text="# x\n")
    assert r.json()["is_error"] and "external routes" in r.json()["output"]
    r = _op(client, "ext-mem", claim, command="view", path="/memories/agent/")
    assert r.json()["is_error"]


def test_shared_mode_has_no_memory(client):
    _seed_agent("ext-mem")
    r = _op(client, "ext-mem", "phone:", command="view", path="/memories")
    assert r.json()["is_error"] and "not available on this line" in r.json()["output"]


def test_shared_only_agent_has_no_caller_memory(client):
    _seed_agent("ext-shared-only", collaborative=False, default_scope="agent")
    r = _op(client, "ext-shared-only", "phone:+3021", command="view", path="/memories")
    assert r.json()["is_error"] and "not available on this line" in r.json()["output"]


def test_ephemeral_caller_writes_under_the_ephemeral_tree(client):
    import config
    _seed_agent("ext-mem")
    sid = str(uuid.uuid4())
    r = _op(client, "ext-mem", f"phone:ephemeral:{sid}", command="create",
            path="/memories/user/t.md", file_text="# t\n")
    assert not r.json()["is_error"], r.json()
    assert (config.AGENTS_DIR / "ext-mem" / "externals" / "phone" / "_ephemeral" / sid
            / "context" / "memory" / "t.md").is_file()
