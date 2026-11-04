"""The retired preferred engine (``PUT /v1/users/me/preferred-engine``
answers 410, ``GET /v1/users/me/execution-layers`` no longer carries it) and
the retired switch card's read (``GET /v1/users/me/engine-switch`` is gone).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

CLAUDE, CODEX, DIRECT = "claude-code-cli", "codex-cli", "direct-llm"
MEMBER = "user-viewer"


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


GONE = ("The preferred engine setting is gone. The engine picker lists your engines "
        "by how much you use them on each agent.")


class TestThePreferredEngineIsGone:
    def test_the_write_answers_410_and_changes_nothing(self, client):
        from storage import database as task_store
        headers = _cookie(MEMBER, "m@t", "member")
        r = client.put("/v1/users/me/preferred-engine", json={"execution_path": CODEX}, headers=headers)
        assert r.status_code == 410
        assert r.json()["detail"] == GONE
        assert task_store.get_user(MEMBER)["preferred_execution_path"] == ""
        # Any body: a dashboard loaded before the update sends the old one.
        assert client.put("/v1/users/me/preferred-engine", json={}, headers=headers).status_code == 410

    def test_a_session_token_gets_the_same_answer(self, client):
        from tests.conftest import live_session_token
        headers = {"Authorization": f"Bearer {live_session_token('sess-p', 'demo', MEMBER)}"}
        r = client.put("/v1/users/me/preferred-engine", json={"execution_path": CODEX}, headers=headers)
        assert r.status_code == 410

    def test_nobody_gets_401(self, client):
        assert client.put("/v1/users/me/preferred-engine", json={}).status_code == 401

    def test_the_layers_read_no_longer_carries_it(self, client):
        data = client.get("/v1/users/me/execution-layers", headers=_cookie(MEMBER, "m@t", "member")).json()
        assert "preferred_execution_path" not in data
        assert {layer["name"] for layer in data["layers"]} >= {CLAUDE, CODEX, DIRECT}


def test_the_switch_cards_read_is_gone(client):
    r = client.get("/v1/users/me/engine-switch", params={"to": CLAUDE},
                   headers=_cookie(MEMBER, "m@t", "member"))
    assert r.status_code == 404
