"""License API endpoints (set / deactivate / recheck) + the
platform-settings payload. Real test DB; relay seams mocked; admin overridden.
"""

import asyncio
import base64
import json
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

import config
import auth.license as L
from services.billing import relay_client
from app import app
from auth.providers import get_current_user, UserContext
from storage import database as task_store

client = TestClient(app)

_LICENSE_KEYS = (
    "license_key", "license_activation_receipt", "license_check_status",
    "license_last_ok_at", "license_last_check_at", "license_last_seen_clock",
)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def admin():
    async def _admin():
        return UserContext(sub="admin-x", email="a@t.com", name="A", role="admin")

    app.dependency_overrides[get_current_user] = _admin
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def clean_license():
    for k in _LICENSE_KEYS:
        task_store.set_platform_setting(k, "")
    L.set_license_key("")          # also clear the encrypted credential-store copy
    yield
    for k in _LICENSE_KEYS:
        task_store.set_platform_setting(k, "")
    L.set_license_key("")


@pytest.fixture
def sign(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(L, "_LICENSE_PUBLIC_KEY_B64", _b64u(pub))

    def _sign(payload: dict) -> str:
        pb = json.dumps(payload).encode()
        return _b64u(pb) + "." + _b64u(sk.sign(pb))

    return _sign


def _sub_key(sign):
    return sign({"tier": "pro", "license_mode": "subscription", "expiry_date": _future(100)})


def _receipt(sign, key, install_id="test-install"):
    return sign({"license_key": key, "install_id": install_id})


def test_set_license_activates_subscription(admin, clean_license, sign, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    monkeypatch.setattr(relay_client, "get_install_id", lambda: "test-install")
    key = _sub_key(sign)
    rec = _receipt(sign, key)

    async def _activate(k):
        assert k == key
        return rec

    monkeypatch.setattr(relay_client, "activate_license", _activate)
    r = client.post("/v1/admin/license", json={"license_key": key}).json()
    assert r["status"] == "valid" and r["activation_state"] == "activated"
    assert L.get_license_key() == key
    assert task_store.get_platform_setting("license_key") == ""  # stored encrypted, not plain
    assert task_store.get_platform_setting("license_activation_receipt") == rec


def test_set_license_activation_limit_reached(admin, clean_license, sign, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    monkeypatch.setattr(relay_client, "get_install_id", lambda: "test-install")
    key = _sub_key(sign)

    async def _activate(k):
        raise relay_client.RelayError("activation_limit_reached")

    monkeypatch.setattr(relay_client, "activate_license", _activate)
    r = client.post("/v1/admin/license", json={"license_key": key}).json()
    assert "already active" in r["message"].lower()
    assert r["status"] == "unactivated"  # community cap until bound


def test_set_license_invalid_key(admin, clean_license, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    r = client.post("/v1/admin/license", json={"license_key": "garbage.notarealkey"}).json()
    assert "invalid" in r["message"].lower()


def test_set_license_change_deactivates_old(admin, clean_license, sign, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    monkeypatch.setattr(relay_client, "get_install_id", lambda: "test-install")
    old = _sub_key(sign)
    task_store.set_platform_setting("license_key", old)
    task_store.set_platform_setting("license_activation_receipt", _receipt(sign, old))
    deactivated = {"k": None}

    async def _deact(k):
        deactivated["k"] = k

    new = sign({"tier": "team", "license_mode": "subscription", "expiry_date": _future(100)})
    new_rec = _receipt(sign, new)

    async def _activate(k):
        return new_rec

    monkeypatch.setattr(relay_client, "deactivate_license", _deact)
    monkeypatch.setattr(relay_client, "activate_license", _activate)
    client.post("/v1/admin/license", json={"license_key": new})
    assert deactivated["k"] == old  # old binding released first
    assert task_store.get_platform_setting("license_activation_receipt") == new_rec


def test_deactivate_clears_binding(admin, clean_license, sign, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    monkeypatch.setattr(relay_client, "get_install_id", lambda: "test-install")
    key = _sub_key(sign)
    task_store.set_platform_setting("license_key", key)
    task_store.set_platform_setting("license_activation_receipt", _receipt(sign, key))
    task_store.set_platform_setting("license_check_status", "active")

    async def _deact(k):
        return None

    monkeypatch.setattr(relay_client, "deactivate_license", _deact)
    r = client.post("/v1/admin/license/deactivate").json()
    assert task_store.get_platform_setting("license_activation_receipt") == ""
    assert r["activation_state"] == "none"


def test_recheck_forces_a_check(admin, clean_license, sign, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    monkeypatch.setattr(relay_client, "get_install_id", lambda: "test-install")
    key = _sub_key(sign)
    task_store.set_platform_setting("license_key", key)
    task_store.set_platform_setting("license_activation_receipt", _receipt(sign, key))
    task_store.set_platform_setting("license_last_check_at", _past(10))
    called = {"n": 0}

    async def _check(k):
        called["n"] += 1
        return {"status": "active"}

    monkeypatch.setattr(relay_client, "license_check", _check)
    r = client.post("/v1/admin/license/recheck").json()
    assert called["n"] == 1 and r["status"] == "valid"


def test_platform_settings_payload_has_2m_fields(admin, clean_license, sign, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)
    monkeypatch.setattr(relay_client, "is_available", lambda: False)
    key = sign({"tier": "pro", "license_mode": "offline_term", "expiry_date": _future(100)})
    task_store.set_platform_setting("license_key", key)
    data = client.get("/v1/admin/platform-settings").json()
    assert data["license_mode"] == "offline_term"
    assert data["license_status"] == "valid"
    # offline_term is relay-excluded (offline ⇒ no-relay policy) → relay_offered()
    # is False → air_gapped True, even though OTODOCK_AIR_GAPPED is unset.
    assert data["air_gapped"] is True
    assert "license_activation_state" in data and "license_last_check_at" in data


def _off_loop(fn):
    """``fn``, refusing to run on a thread with a running event loop (a
    worker thread has none)."""
    def guarded(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return fn(*a, **kw)
        raise AssertionError(f"{fn.__name__} ran on the event loop")
    return guarded


def test_settings_and_license_read_the_relay_off_the_loop(admin, clean_license, sign, monkeypatch):
    from api.auth import platform as platform_api
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)
    monkeypatch.setattr(platform_api, "get_license_key", _off_loop(platform_api.get_license_key))
    for name in ("relay_offered", "is_connected", "api_relay_enabled",
                 "system_relay_active", "is_available"):
        monkeypatch.setattr(relay_client, name, _off_loop(getattr(relay_client, name)))
    data = client.get("/v1/admin/platform-settings").json()
    assert data["has_license_key"] is False
    assert data["air_gapped"] is False and data["otodock_connection"]["air_gapped"] is False
    assert data["otodock_connection"]["connected"] is False
    assert data["otodock_connection"]["active"] is False

    monkeypatch.setattr(relay_client, "is_available", _off_loop(lambda: True))
    monkeypatch.setattr(relay_client, "get_install_id", lambda: "test-install")
    key = _sub_key(sign)
    rec = _receipt(sign, key)

    async def _activate(k):
        return rec

    monkeypatch.setattr(relay_client, "activate_license", _activate)
    r = client.post("/v1/admin/license", json={"license_key": key}).json()
    assert r["activation_state"] == "activated"


def test_mcp_list_surfaces_hosted_for_type_none(admin, monkeypatch):
    # Regression: GET /v1/admin/mcps read `hosted` INSIDE the per_user credential
    # branch, so a type='none' api_key_relay MCP (image-gen) returned hosted=null
    # → the dashboard never got apiKeyRelay → no availability banner /
    # Credentials-source note.
    from services.mcp import mcp_registry
    m = mcp_registry._parse_manifest(
        config.MCPS_DIR / "custom" / "image-gen-mcp" / "manifest.json"
    )
    if not (m.hosted and m.hosted.api_key_relay):
        pytest.skip("image-gen manifest carries no hosted block in this cut")
    monkeypatch.setitem(mcp_registry._manifests, m.name, m)
    data = client.get("/v1/admin/mcps").json()
    entry = next((x for x in data.get("mcps", []) if x["name"] == m.name), None)
    assert entry is not None
    assert entry["hosted"]["api_key_relay"]["available"] is True
