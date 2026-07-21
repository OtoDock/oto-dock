"""``allow_user_paired_machines`` admin lock-out toggle.

Covers the gate at the three defense-in-depth sites (pair endpoint, WS
reconnect, session start) and the cascade that runs on ON→OFF flip
(close live user-paired satellites + clear their target rows).
"""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_pair_app(monkeypatch, *, allow_user_paired: bool = True):
    """Mount remote_machines.router with stubs for the pair-endpoint test."""
    from api.remote import remote_machines as rm
    from auth.providers import UserContext, get_current_user

    user = UserContext(
        sub="user-sub-self", email="u@test.com", name="U",
        role="creator", agents=[], agent_roles={},
    )

    async def _stub_user():
        return user

    from storage import remote_store as _rs
    from storage import database as _db
    import config as _cfg

    monkeypatch.setattr(
        _db, "get_platform_setting",
        lambda key: ("1" if allow_user_paired else "0")
        if key == "allow_user_paired_machines" else "",
    )
    monkeypatch.setattr(_cfg, "get_platform_public_url", lambda: "platform.test")
    monkeypatch.setattr(
        _rs, "create_remote_machine",
        lambda **kw: {
            "id": "machine-1", "name": kw["name"],
            "pairing_token": "tok",
        },
    )
    monkeypatch.setattr(_rs, "PAIRING_TOKEN_EXPIRY_HOURS", 1, raising=False)

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


# Pairing endpoints 404 outright on a build without the satellite source
# tree (the public cut) — the toggle semantics only exist beneath that gate.
def _requires_satellite_source():
    from ws.satellite import satellite_source_available
    return pytest.mark.skipif(
        not satellite_source_available(),
        reason="pairing needs the satellite source tree (not in this build)",
    )


@_requires_satellite_source()
def test_pair_refused_when_toggle_off(monkeypatch):
    app = _make_pair_app(monkeypatch, allow_user_paired=False)
    client = TestClient(app)
    resp = client.post(
        "/v1/users/me/remote-machines/pair", json={"name": "my-laptop"},
    )
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"].lower()


@_requires_satellite_source()
def test_pair_allowed_when_toggle_on(monkeypatch):
    app = _make_pair_app(monkeypatch, allow_user_paired=True)
    client = TestClient(app)
    resp = client.post(
        "/v1/users/me/remote-machines/pair", json={"name": "my-laptop"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["machine_id"] == "machine-1"
    assert body["name"] == "my-laptop"


# ---------------------------------------------------------------------------
# Force-disconnect cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_cascade_closes_user_paired_satellites(monkeypatch):
    """ON→OFF flip closes WS for user-paired satellites and clears their
    targets. Admin-paired satellites untouched."""
    from api.auth.auth import _enforce_user_paired_disabled
    from storage import remote_store as _rs

    # Two connected machines: one user-paired, one admin-paired
    fake_ws_user = MagicMock()
    fake_ws_user.close = AsyncMock()
    fake_ws_admin = MagicMock()
    fake_ws_admin.close = AsyncMock()

    fake_conn_user = MagicMock()
    fake_conn_user.ws = fake_ws_user
    fake_conn_admin = MagicMock()
    fake_conn_admin.ws = fake_ws_admin

    fake_cm = MagicMock()
    fake_cm.get_connected_machines = MagicMock(
        return_value=["machine-user", "machine-admin"],
    )
    fake_cm.get_connection = MagicMock(
        side_effect={
            "machine-user": fake_conn_user,
            "machine-admin": fake_conn_admin,
        }.get,
    )
    fake_cm.deregister = AsyncMock()

    def fake_get_machine(mid: str):
        if mid == "machine-user":
            return {"id": mid, "registered_by": "user-1", "pairing_scope": "user"}
        if mid == "machine-admin":
            return {"id": mid, "registered_by": "admin-1", "pairing_scope": "admin"}
        return None

    cleared: list[str] = []
    monkeypatch.setattr(_rs, "get_remote_machine", fake_get_machine)
    monkeypatch.setattr(
        _rs, "clear_user_remote_targets_for_machine",
        lambda mid: (cleared.append(mid), 1)[1],
    )
    monkeypatch.setattr(_rs, "get_all_user_paired_machines", lambda: [])

    with patch("core.remote.satellite_connection.get_connection_manager",
               return_value=fake_cm):
        await _enforce_user_paired_disabled()

    # User-paired: WS closed with 4005, deregister called, targets cleared
    fake_ws_user.close.assert_awaited_once()
    args, kwargs = fake_ws_user.close.await_args
    assert kwargs.get("code") == 4005
    assert kwargs.get("reason") == "feature_disabled_by_admin"
    fake_cm.deregister.assert_awaited_with("machine-user")
    assert "machine-user" in cleared

    # Admin-paired: untouched
    fake_ws_admin.close.assert_not_awaited()
    assert "machine-admin" not in cleared


@pytest.mark.asyncio
async def test_enforce_cascade_clears_offline_user_paired_machines(monkeypatch):
    """Offline user-paired machines: cascade still clears their targets."""
    from api.auth.auth import _enforce_user_paired_disabled
    from storage import remote_store as _rs

    fake_cm = MagicMock()
    fake_cm.get_connected_machines = MagicMock(return_value=[])
    fake_cm.get_connection = MagicMock(return_value=None)
    fake_cm.deregister = AsyncMock()

    monkeypatch.setattr(_rs, "get_remote_machine", lambda mid: None)
    monkeypatch.setattr(
        _rs, "get_all_user_paired_machines",
        lambda: [{"id": "offline-user-machine"}],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        _rs, "clear_user_remote_targets_for_machine",
        cleared.append,
    )

    with patch("core.remote.satellite_connection.get_connection_manager",
               return_value=fake_cm):
        await _enforce_user_paired_disabled()

    assert cleared == ["offline-user-machine"]


# ---------------------------------------------------------------------------
# Session-start gate
# ---------------------------------------------------------------------------


def test_session_start_refuses_user_paired_when_disabled(monkeypatch):
    """Defense-in-depth: even if a stale user_remote_targets row survives,
    get_execution_layer raises if the toggle is off."""
    from core.session.session_manager import get_execution_layer

    fake_agent = {
        "execution_path": "claude-code-cli",
        "execution_target": "machine-user",
    }
    fake_machine = {
        "id": "machine-user", "registered_by": "user-1",
        "pairing_scope": "user",
    }

    with patch("core.session.session_manager.agent_store") as mock_store, \
         patch("storage.remote_store.resolve_execution_target",
               return_value=("machine-user", None)), \
         patch("storage.remote_store.get_remote_machine",
               return_value=fake_machine), \
         patch("storage.database.get_platform_setting",
               side_effect=lambda k: "0" if k == "allow_user_paired_machines" else ""):
        mock_store.get_agent.return_value = fake_agent
        with pytest.raises(RuntimeError, match="disabled by admin"):
            get_execution_layer("test-agent", user_sub="user-1")


def test_session_start_allows_user_paired_when_enabled(monkeypatch):
    """Toggle ON → no refusal at session start."""
    from core.session.session_manager import get_execution_layer

    fake_agent = {
        "execution_path": "claude-code-cli",
        "execution_target": "machine-user",
    }
    fake_machine = {
        "id": "machine-user", "registered_by": "user-1",
        "pairing_scope": "user",
    }

    with patch("core.session.session_manager.agent_store") as mock_store, \
         patch("storage.remote_store.resolve_execution_target",
               return_value=("machine-user", None)), \
         patch("storage.remote_store.get_remote_machine",
               return_value=fake_machine), \
         patch("storage.database.get_platform_setting", return_value=""):
        mock_store.get_agent.return_value = fake_agent
        # Should not raise; returns the remote layer
        layer = get_execution_layer("test-agent", user_sub="user-1")
        assert layer is not None
