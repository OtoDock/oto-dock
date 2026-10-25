"""An agent's ``default_effort`` is a level on the platform's ladder.

The API took any string, so a typo reached the CLI's ``--effort`` and every
spawn failed. A level one engine lacks is fine (each engine folds onto its
own ladder: Claude reads ``ultra`` as ``max``), and the check is the
platform's ladder rather than the agent's engines because the settings page
saves the engines and the effort in separate requests. An agent saved
before the check keeps loading: its value reads as the default.
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


def _agent(effort: str = "") -> str:
    slug = f"effort-{uuid.uuid4().hex[:8]}"
    agent_store.create_agent(slug, "Effort test", execution_path="claude-code-cli")
    if effort:
        with get_conn() as conn:
            conn.execute("UPDATE agents SET default_effort=%s WHERE slug=%s", (effort, slug))
            conn.commit()
        agent_store._invalidate_cache()
    return slug


def test_a_level_on_the_ladder_is_saved_and_a_typo_is_refused(client):
    slug = _agent()
    assert client.patch(f"/v1/agents/{slug}", json={"default_effort": "xhigh"}).status_code == 200
    assert (agent_store.get_agent(slug) or {}).get("default_effort") == "xhigh"
    # A level only Codex offers is still a level: Claude folds it to max.
    assert client.patch(f"/v1/agents/{slug}", json={"default_effort": "ultra"}).status_code == 200
    r = client.patch(f"/v1/agents/{slug}", json={"default_effort": "turbo"})
    assert r.status_code == 400
    assert "low, medium, high, xhigh, max, ultra" in r.json()["detail"]
    assert (agent_store.get_agent(slug) or {}).get("default_effort") == "ultra"
    # Empty clears it back to the default.
    assert client.patch(f"/v1/agents/{slug}", json={"default_effort": ""}).status_code == 200


def test_a_saved_value_off_the_ladder_never_blocks_the_agent(client):
    slug = _agent(effort="turbo")
    # Another field saves; the stored value is not re-judged.
    assert client.patch(f"/v1/agents/{slug}", json={"description": "still here"}).status_code == 200
    # The engines read the default, not the typo.
    assert config.get_cli_effort(slug) == config.DEFAULT_EFFORT_LEVEL
    with get_conn() as conn:
        conn.execute("UPDATE agents SET default_effort='max' WHERE slug=%s", (slug,))
        conn.commit()
    agent_store._invalidate_cache()
    assert config.get_cli_effort(slug) == "max"


def test_create_refuses_a_level_off_the_ladder(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    r = client.post("/v1/agents", json={"display_name": "Effort Create",
                                        "execution_path": "claude-code-cli",
                                        "default_effort": "maximum"})
    assert r.status_code == 400
    assert not agent_store.agent_exists("effort-create")


@pytest.mark.parametrize("body", [
    {"display_name": "Admin"},                       # the slug comes from the name
    {"display_name": "Platform helper", "slug": "system"},
    {"display_name": "Who", "slug": " ME "},
])
def test_create_refuses_a_reserved_slug(client, monkeypatch, tmp_path, body):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    r = client.post("/v1/agents", json={**body, "execution_path": "claude-code-cli"})
    assert r.status_code == 400
    assert "is reserved by OtoDock. Pick another ID." in r.json()["detail"]
    assert not list(tmp_path.iterdir())


def test_an_agent_that_already_holds_a_reserved_slug_keeps_working(client):
    agent_store.create_agent("setup", "Setup from an older release")
    r = client.patch("/v1/agents/setup", json={"description": "still here"})
    assert r.status_code == 200, r.text


def test_a_create_that_fails_says_so_without_the_cause(client, monkeypatch, tmp_path, caplog):
    from pathlib import Path
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

    def no_room(self, *a, **kw):
        raise PermissionError(13, "Permission denied", str(self))
    with monkeypatch.context() as m, caplog.at_level("ERROR", logger="otodock"):
        m.setattr(Path, "write_text", no_room)
        r = client.post("/v1/agents", json={"display_name": "Folder Fails",
                                            "execution_path": "claude-code-cli"})
    assert (r.status_code, r.json()["detail"]) == (
        500, "Could not create the agent's files. The server log has the details.")
    assert str(tmp_path) not in r.text and "Permission denied" in caplog.text

    def broken(*a, **kw):
        raise RuntimeError("duplicate key value violates unique constraint")
    monkeypatch.setattr(agent_store, "create_agent", broken)
    r = client.post("/v1/agents", json={"display_name": "Record Fails",
                                        "execution_path": "claude-code-cli"})
    assert (r.status_code, r.json()["detail"]) == (
        500, "Could not save the new agent. The server log has the details.")
