"""The memory API binds the calling session to the agent it names.

A session token minted for agent A cannot read or write agent B's memory by
naming B in ``X-Agent-Name``; a user-backed session must have access to the
agent; an unknown agent stays a 404 (the existence check runs first).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


def _seed_agent(slug: str) -> None:
    from storage.agents import agent_store
    if not agent_store.agent_exists(slug):
        agent_store.create_agent(slug, slug.title())


def _headers(agent_for_token: str, agent_named: str, user_sub: str = "") -> dict:
    from auth.session_token import create_session_token
    token = create_session_token(str(uuid.uuid4()), agent_for_token, user_sub)
    return {"Authorization": f"Bearer {token}", "X-Agent-Name": agent_named}


VIEW = {"command": "view", "path": "/memories"}


def test_session_for_another_agent_is_refused(client):
    _seed_agent("mem-a")
    _seed_agent("mem-b")
    r = client.post("/v1/internal/memory/op", json=VIEW, headers=_headers("mem-a", "mem-b"))
    assert r.status_code == 403
    r = client.post("/v1/internal/memory/op", json=VIEW, headers=_headers("mem-a", "mem-a"))
    assert r.status_code == 200


def test_unknown_agent_is_still_a_404(client):
    _seed_agent("mem-a")
    r = client.post("/v1/internal/memory/op", json=VIEW, headers=_headers("mem-a", "ghost"))
    assert r.status_code == 404


def test_user_backed_session_needs_access(client):
    _seed_agent("mem-a")
    # user-viewer (seeded by conftest) has no assignment on mem-a — but a
    # session minted FOR mem-a may act on it (can_access_agent's session rule).
    r = client.post("/v1/internal/memory/op", json=VIEW,
                    headers=_headers("mem-a", "mem-a", user_sub="user-viewer"))
    assert r.status_code == 200
    # A user-backed session minted for another agent, naming mem-a: refused.
    _seed_agent("mem-b")
    r = client.post("/v1/internal/memory/op", json=VIEW,
                    headers=_headers("mem-b", "mem-a", user_sub="user-viewer"))
    assert r.status_code == 403
