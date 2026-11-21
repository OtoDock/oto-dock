"""The memory API for an outside session: refused at the door and behind it."""

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
        agent_store.create_agent(slug, slug.title(), default_scope="user")


def _headers(agent: str, claim: str) -> dict:
    from tests.conftest import live_session_token
    token = live_session_token(str(uuid.uuid4()), agent, external=claim)
    return {"Authorization": f"Bearer {token}", "X-Agent-Name": agent}


def _op(client, agent, claim, **body):
    return client.post("/v1/internal/memory/op", json=body, headers=_headers(agent, claim))


@pytest.mark.parametrize("claim", ["phone:+302101234567", "phone:", "phone:ephemeral:4f1d2c3b-0000-4000-8000-0000000000e1"])
def test_an_outside_session_is_refused_the_memory_route(client, claim):
    """Whatever the person's home: the middleware keeps the memory route off an
    outside token, and nothing lands on disk."""
    import config
    _seed_agent("ext-mem")
    r = _op(client, "ext-mem", claim, command="create", path="/memories/user/name.md",
            file_text="# Name\nThe caller is Maria.\n")
    assert r.status_code == 403
    externals = config.AGENTS_DIR / "ext-mem" / "externals"
    assert not list(externals.rglob("name.md")) if externals.exists() else True


@pytest.mark.asyncio
async def test_the_handler_refuses_an_outside_session_behind_the_middleware(temp_db):
    """Defence in depth: the handler itself answers an outside session with
    the tool's own refusal, never a scope."""
    from unittest.mock import AsyncMock

    from api.agent_data import memory as memory_api
    from auth import roles
    from auth.providers import UserContext

    _seed_agent("ext-mem")
    sid = str(uuid.uuid4())
    user = UserContext(sub=f"session:{sid}", email="", name="", role=roles.SERVICE,
                       is_api_key=True, session_id=sid, agent="ext-mem",
                       external_claim="phone:+3021", external_channel="phone")
    assert user.is_external
    request = AsyncMock()
    out = await memory_api.memory_op(request, user=user, x_agent_name="ext-mem")
    assert out["is_error"] and "not written during an outside conversation" in out["output"]
    request.json.assert_not_called()
