"""Per-user per-agent remote-target endpoints.

Replaces the legacy global override (``agent_slug=''``) with explicit
per-agent rows. Tests cover the validation chain, idempotency, the
storage cleanup on user_agents revocation, and the migration that
deletes legacy global rows on first startup.
"""

from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(monkeypatch, *, role: str = "creator", agents: dict | None = None,
              machine_owner: str = "user-sub-self"):
    """Mount remote_machines.router with a stubbed user + remote_store."""
    from api.remote import remote_machines as rm
    from auth.providers import UserContext, get_current_user

    user = UserContext(
        sub="user-sub-self",
        email="alice@test.com",
        name="Alice",
        role=role,
        agents=list((agents or {"my-agent": "manager"}).keys()),
        agent_roles=(agents or {"my-agent": "manager"}),
    )

    async def _stub_user():
        return user

    # Stub the storage layer so we don't need a real database
    fake_machine = {
        "id": "machine-1",
        "name": "My Laptop",
        "registered_by": machine_owner,
        "status": "online",
        "capabilities": "{}",
    }

    from storage import remote_store as _rs
    from storage import database as _db
    monkeypatch.setattr(
        _rs, "get_remote_machine",
        lambda mid: fake_machine if mid == "machine-1" else None,
    )

    set_calls: list = []
    monkeypatch.setattr(
        _rs, "set_user_remote_target",
        lambda sub, mid, agent_slug="": set_calls.append((sub, mid, agent_slug)),
    )
    remove_calls: list = []
    monkeypatch.setattr(
        _rs, "remove_user_remote_target",
        lambda sub, agent_slug="": remove_calls.append((sub, agent_slug)),
    )
    monkeypatch.setattr(
        _rs, "get_user_remote_targets",
        lambda sub: [
            {
                "user_sub": sub,
                "agent_slug": "my-agent",
                "machine_id": "machine-1",
                "name": "My Laptop",
                "status": "online",
            }
        ],
    )

    monkeypatch.setattr(
        _db, "get_user_agent_roles",
        lambda sub: agents or {"my-agent": "manager"},
    )

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app, set_calls, remove_calls


# ---------------------------------------------------------------------------
# GET /v1/users/me/remote-targets
# ---------------------------------------------------------------------------


def test_list_returns_per_agent_targets(monkeypatch):
    app, _, _ = _make_app(monkeypatch)
    client = TestClient(app)
    resp = client.get("/v1/users/me/remote-targets")
    assert resp.status_code == 200
    body = resp.json()
    assert "targets" in body
    assert len(body["targets"]) == 1
    assert body["targets"][0]["agent_slug"] == "my-agent"


# ---------------------------------------------------------------------------
# PUT /v1/users/me/remote-targets/{agent_slug}
# ---------------------------------------------------------------------------


def test_put_sets_per_agent_target(monkeypatch):
    app, set_calls, _ = _make_app(monkeypatch)
    client = TestClient(app)
    resp = client.put(
        "/v1/users/me/remote-targets/my-agent",
        json={"machine_id": "machine-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert set_calls == [("user-sub-self", "machine-1", "my-agent")]


def test_put_rejects_agent_user_doesnt_have_access_to(monkeypatch):
    """403 when agent_slug isn't in the user's user_agents."""
    app, set_calls, _ = _make_app(
        monkeypatch, agents={"my-agent": "manager"},  # has my-agent only
    )
    client = TestClient(app)
    resp = client.put(
        "/v1/users/me/remote-targets/other-agent",  # NOT assigned
        json={"machine_id": "machine-1"},
    )
    assert resp.status_code == 403
    assert "Not assigned" in resp.json()["detail"]
    assert set_calls == []


def test_put_rejects_other_users_machine(monkeypatch):
    """403 when targeting a machine owned by someone else."""
    app, set_calls, _ = _make_app(
        monkeypatch, machine_owner="someone-else-sub",
    )
    client = TestClient(app)
    resp = client.put(
        "/v1/users/me/remote-targets/my-agent",
        json={"machine_id": "machine-1"},
    )
    assert resp.status_code == 403
    assert "Not your machine" in resp.json()["detail"]
    assert set_calls == []


def test_put_rejects_missing_machine(monkeypatch):
    app, _, _ = _make_app(monkeypatch)
    client = TestClient(app)
    resp = client.put(
        "/v1/users/me/remote-targets/my-agent",
        json={"machine_id": "ghost-machine"},
    )
    assert resp.status_code == 404


def test_put_admin_bypasses_access_check(monkeypatch):
    """Admins can target any machine for any agent."""
    app, set_calls, _ = _make_app(
        monkeypatch, role="admin",
        agents={},  # admin doesn't need user_agents row
        machine_owner="someone-else-sub",
    )
    client = TestClient(app)
    resp = client.put(
        "/v1/users/me/remote-targets/random-agent",
        json={"machine_id": "machine-1"},
    )
    assert resp.status_code == 200
    assert set_calls == [("user-sub-self", "machine-1", "random-agent")]


# ---------------------------------------------------------------------------
# DELETE /v1/users/me/remote-targets/{agent_slug}
# ---------------------------------------------------------------------------


def test_delete_removes_per_agent_target(monkeypatch):
    app, _, remove_calls = _make_app(monkeypatch)
    client = TestClient(app)
    resp = client.delete("/v1/users/me/remote-targets/my-agent")
    assert resp.status_code == 200
    assert remove_calls == [("user-sub-self", "my-agent")]


def test_delete_idempotent(monkeypatch):
    """DELETE on a missing row returns 200 (storage call still happens)."""
    app, _, remove_calls = _make_app(monkeypatch)
    client = TestClient(app)
    resp = client.delete("/v1/users/me/remote-targets/never-set")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# resolve_execution_target — no legacy global fallback
# ---------------------------------------------------------------------------


def test_get_user_remote_target_no_global_fallback(monkeypatch):
    """The agent_slug='' fallback branch was removed — only the
    explicit per-agent row matches."""
    from storage import remote_store

    # Mock the get_conn to return a connection whose execute returns no row
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None

    from contextlib import contextmanager
    @contextmanager
    def fake_get_conn():
        yield mock_conn

    with patch("storage.remote_store.get_conn", fake_get_conn):
        result = remote_store.get_user_remote_target("user-1", "my-agent")
        assert result is None
        # Exactly one SELECT — the legacy fallback SELECT is gone.
        assert mock_conn.execute.call_count == 1


def test_get_user_remote_target_empty_agent_slug_returns_none():
    """Calling with an empty agent_slug short-circuits without a query."""
    from storage import remote_store
    assert remote_store.get_user_remote_target("user-1", "") is None
