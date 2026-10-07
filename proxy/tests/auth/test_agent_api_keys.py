"""Agent API keys are a person's decision.

A key is a durable credential that outlives the session and fires the
agent's webhooks from anywhere, so create, list and revoke answer only a
person at the dashboard: every bearer principal (a session token of a
manager or an admin, the master key) is refused.

Run: cd proxy && venv/bin/pytest tests/auth/test_agent_api_keys.py -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import create_session_jwt
from tests.conftest import live_session_token
from services.infra import api_key_manager
from storage import database as task_store
from storage.agents import agent_store

client = TestClient(app)
AGENT = "keys-proj"
MGR = "user-viewer"


@pytest.fixture(autouse=True)
def _world(temp_db):
    agent_store.create_agent(AGENT, AGENT, created_by="user-admin")
    task_store.set_user_agents(MGR, [AGENT], "user-admin", agent_roles={AGENT: "manager"})


def _bearer(sub: str) -> dict:
    return {"Authorization": f"Bearer {live_session_token(str(uuid.uuid4()), AGENT, sub)}"}


def _cookie(sub: str, role: str) -> dict:
    return {"Cookie": f"session={create_session_jwt(sub, f'{sub}@t.com', sub, role)}"}


@pytest.mark.parametrize("owner", [MGR, "user-admin"])
def test_a_session_token_cannot_create_list_or_revoke(owner):
    row, _raw = api_key_manager.create_agent_key(agent=AGENT, name="k", permissions=["triggers"],
                                                 created_by="user-admin")
    h = _bearer(owner)
    assert client.post(f"/v1/agents/{AGENT}/api-keys", json={"name": "x"}, headers=h).status_code == 403
    assert client.get(f"/v1/agents/{AGENT}/api-keys", headers=h).status_code == 403
    assert client.delete(f"/v1/agents/{AGENT}/api-keys/{row['id']}", headers=h).status_code == 403


def test_the_master_key_cannot_mint():
    import config
    r = client.post(f"/v1/agents/{AGENT}/api-keys", json={"name": "x"},
                    headers={"Authorization": f"Bearer {config.API_KEY}"})
    assert r.status_code == 403


def test_a_cookie_manager_creates_lists_and_revokes():
    h = _cookie(MGR, "member")
    r = client.post(f"/v1/agents/{AGENT}/api-keys", json={"name": "x"}, headers=h)
    assert r.status_code == 200 and r.json()["key"].startswith("otok_")
    keys = client.get(f"/v1/agents/{AGENT}/api-keys", headers=h).json()["keys"]
    assert [k["created_by"] for k in keys] == [MGR]
    assert client.delete(f"/v1/agents/{AGENT}/api-keys/{keys[0]['id']}",
                         headers=h).json()["status"] == "revoked"


def test_a_cookie_member_without_the_manager_role_is_refused():
    task_store.set_user_agents(MGR, [AGENT], "user-admin", agent_roles={AGENT: "editor"})
    r = client.post(f"/v1/agents/{AGENT}/api-keys", json={"name": "x"}, headers=_cookie(MGR, "member"))
    assert r.status_code == 403
