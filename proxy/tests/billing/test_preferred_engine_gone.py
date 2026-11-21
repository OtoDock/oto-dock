"""The retired preferred engine: its write route and its column are gone,
``GET /v1/users/me/execution-layers`` no longer carries it, and the retired
switch card's read (``GET /v1/users/me/engine-switch``) is gone.
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


class TestThePreferredEngineIsGone:
    def test_the_write_route_is_gone(self, client):
        headers = _cookie(MEMBER, "m@t", "member")
        r = client.put("/v1/users/me/preferred-engine", json={"execution_path": CODEX}, headers=headers)
        assert r.status_code in (404, 405)

    def test_a_restored_1_9_1_users_table_loses_the_column(self, temp_db):
        from storage import database as task_store
        from storage.database import get_conn
        from storage.identity.schema import migrate_identity
        with get_conn() as conn:
            conn.execute("ALTER TABLE users "
                         "ADD COLUMN preferred_execution_path TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE users SET preferred_execution_path = %s WHERE sub = %s", (CODEX, MEMBER))
            for _ in range(2):
                migrate_identity(conn)
            conn.commit()
            left = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'users' AND column_name = 'preferred_execution_path'").fetchone()
        assert left is None
        assert task_store.get_user(MEMBER)["sub"] == MEMBER

    def test_the_layers_read_no_longer_carries_it(self, client):
        data = client.get("/v1/users/me/execution-layers", headers=_cookie(MEMBER, "m@t", "member")).json()
        assert "preferred_execution_path" not in data
        assert {layer["name"] for layer in data["layers"]} >= {CLAUDE, CODEX, DIRECT}


def test_the_switch_cards_read_is_gone(client):
    r = client.get("/v1/users/me/engine-switch", params={"to": CLAUDE},
                   headers=_cookie(MEMBER, "m@t", "member"))
    assert r.status_code == 404
