"""/auth/config relay flags + /v1/user/credits stub."""

from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import get_current_user, UserContext

client = TestClient(app)


def test_auth_config_exposes_connectivity_flags(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)  # connected
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "")  # relay unbuilt
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    data = client.get("/auth/config").json()
    assert data["air_gapped"] is False
    assert data["relay_available"] is False  # connected but no base
    assert data["cloud"] is False
    # `relay_base` itself must NOT be exposed; nor the retired `relay_enabled`
    assert "relay_base" not in data
    assert "relay_enabled" not in data


def test_auth_config_relay_available_when_base_set(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)  # connected
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://api.otodock.io")
    data = client.get("/auth/config").json()
    assert data["relay_available"] is True


def test_auth_config_air_gapped(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", True)
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://api.otodock.io")
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    data = client.get("/auth/config").json()
    assert data["air_gapped"] is True
    assert data["relay_available"] is False  # air-gapped → relay not offered


def test_auth_config_cloud_forces_not_air_gapped(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", True)  # ignored on cloud
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://api.otodock.io")
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    data = client.get("/auth/config").json()
    assert data["air_gapped"] is False  # cloud overrides the flag
    assert data["relay_available"] is True


def test_user_credits_canonical_shape():
    async def _stub_user():
        return UserContext(sub="user-x", email="x@t.com", name="X", role="member")

    app.dependency_overrides[get_current_user] = _stub_user
    try:
        data = client.get("/v1/user/credits").json()
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # Canonical shape — NOT a throwaway has_balance/billing-status.
    assert set(data.keys()) == {
        "balance_usd", "balance_eur_approx", "low_threshold", "recent_transactions",
    }
    assert data["balance_usd"] == 0.0
    assert data["recent_transactions"] == []
