"""The per-agent default execution mode (interactive terminal vs headless)
can only be set when the default model runs on a CLI engine THAT THE AGENT
USES — a local model served by both Direct LLM and Codex resolves to
codex-cli too, which let a Direct-LLM-only agent store an interactive
default it can never run (live-hit 2026-09-07).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth.providers import UserContext, get_current_user
from storage.agents import agent_store
from storage.pg import get_conn


@pytest.fixture
def client(temp_db):
    from api.agents import agents as agents_router
    app = FastAPI()
    app.include_router(agents_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})
    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def _agent(execution_path: str, default_model: str) -> str:
    slug = f"mode-{uuid.uuid4().hex[:8]}"
    agent_store.create_agent(slug, "Mode test")
    with get_conn() as conn:
        conn.execute(
            "UPDATE agents SET execution_path=%s, default_model=%s WHERE slug=%s",
            (execution_path, default_model, slug),
        )
        conn.commit()
    return slug


def test_interactive_default_needs_a_cli_engine_the_agent_uses(client, monkeypatch):
    # A local model that both Direct LLM and Codex can serve.
    monkeypatch.setattr(
        config, "get_model_layers",
        lambda m: ["direct-llm", "codex-cli"] if m == "qwen-local" else [],
    )
    direct_only = _agent("direct-llm", "qwen-local")
    r = client.patch(f"/v1/agents/{direct_only}", json={"default_execution_mode": "interactive"})
    assert r.status_code == 400
    assert "that this agent uses" in r.json()["detail"]
    # Headless is refused there too: the row must not claim a mode at all.
    assert client.patch(f"/v1/agents/{direct_only}",
                        json={"default_execution_mode": "-p"}).status_code == 400
    # Clearing it is always fine.
    assert client.patch(f"/v1/agents/{direct_only}",
                        json={"default_execution_mode": ""}).status_code == 200

    codex_agent = _agent("codex-cli", "qwen-local")
    r = client.patch(f"/v1/agents/{codex_agent}", json={"default_execution_mode": "interactive"})
    assert r.status_code == 200
    assert (agent_store.get_agent(codex_agent) or {}).get("default_execution_mode") == "interactive"
