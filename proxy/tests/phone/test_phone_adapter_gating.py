"""Local-PBX adapter gating for cloud-prep.

``config.LOCAL_PBX_ENABLED`` (default ``not OTODOCK_CLOUD``, explicit
``OTODOCK_LOCAL_PBX_ENABLED`` override) removes the AudioSocket-based Asterisk /
FreePBX adapters from the creatable set — so OtoDock cloud can't add a local PBX
it could never reach, while self-host keeps them. Twilio stays available; 3CX is
deferred (never creatable) though its stub class remains for legacy rows.
See config.py + proxy/api/phone/phone.py::create_phone_server.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import create_session_jwt
from services.phone import phone_adapters
from storage.phone import phone_server_store


def test_all_released_adapters_available_when_local_pbx_enabled(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", True)
    types = set(phone_adapters.available_adapter_types())
    assert {"asterisk_manual", "asterisk_freepbx", "twilio"} <= types
    # Deferred provider: class exists (legacy rows load) but never creatable.
    assert "three_cx" not in types
    assert "three_cx" in phone_adapters.ADAPTER_CLASSES


def test_local_pbx_adapters_dropped_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", False)
    types = set(phone_adapters.available_adapter_types())
    # The AudioSocket/Asterisk family is gated off…
    assert "asterisk_manual" not in types
    assert "asterisk_freepbx" not in types
    # …while the cloud-reachable released adapter remains.
    assert "twilio" in types
    assert "three_cx" not in types


def test_available_is_subset_of_all_known_adapters(monkeypatch):
    # Whatever the flag, we never invent an adapter type that has no class.
    for enabled in (True, False):
        monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", enabled)
        assert set(phone_adapters.available_adapter_types()) <= set(phone_adapters.ADAPTER_CLASSES)


# ── a hosted install is Twilio only, at every door ───────────────────────

def _admin() -> TestClient:
    return TestClient(app, cookies={"session": create_session_jwt(
        "user-admin", "admin@test.com", "Admin User", "admin")})


def _pbx_row() -> dict:
    return phone_server_store.create_server({
        "name": "Office PBX", "adapter_type": "asterisk_freepbx", "host": "192.168.1.30"})


def test_a_pbx_row_is_never_dialled_while_the_pbx_is_off(temp_db, monkeypatch):
    """``load_adapter`` is the one gate: routes, the health worker,
    bootstrap, verify and health all pass it."""
    row = _pbx_row()
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", False)
    with pytest.raises(phone_adapters.PhoneAdapterError) as ei:
        phone_adapters.load_adapter(row)
    assert ei.value.status_code == 400
    r = _admin().post(f"/v1/admin/phone-servers/{row['id']}/health")
    assert r.status_code == 400 and "not available" in r.text
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", True)
    assert phone_adapters.load_adapter(row) is not None, "the broken form: it loads"


def test_the_health_worker_skips_a_pbx_while_it_is_off(temp_db, monkeypatch):
    from services.phone import phone_health_worker
    row = _pbx_row()
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", False)
    asyncio.run(phone_health_worker._reconcile_server(row))
    assert phone_server_store.get_server(row["id"]).get("last_health_check") in (None, "")


def test_a_server_cannot_become_a_pbx_by_its_update(temp_db, monkeypatch):
    twilio = phone_server_store.create_server({"name": "T", "adapter_type": "twilio"})
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", False)
    r = _admin().put(f"/v1/admin/phone-servers/{twilio['id']}",
                     json={"adapter_type": "asterisk_manual"})
    assert r.status_code == 400
    assert phone_server_store.get_server(twilio["id"])["adapter_type"] == "twilio"


def test_the_list_names_what_this_install_offers(temp_db, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", False)
    assert _admin().get("/v1/admin/phone-servers").json()["types"] == ["twilio"]
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", True)
    assert set(_admin().get("/v1/admin/phone-servers").json()["types"]) == {
        "asterisk_manual", "asterisk_freepbx", "twilio"}


def test_the_hosted_flag_wins_over_the_pbx_override():
    """config.py: OTODOCK_CLOUD with OTODOCK_LOCAL_PBX_ENABLED=true is Twilio
    only, with one boot line; read in a fresh interpreter."""
    import os
    import subprocess
    import sys

    from tests._paths import PROXY_DIR
    env = {**os.environ, "OTODOCK_CLOUD": "true", "OTODOCK_LOCAL_PBX_ENABLED": "true"}
    out = subprocess.run(  # the proxy's own config, read by its own interpreter
        [sys.executable, "-c", "import config; print(config.LOCAL_PBX_ENABLED)"],
        cwd=str(PROXY_DIR), env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "False", out.stderr[-500:]


def test_the_ami_socket_never_reaches_the_cloud_metadata_address(monkeypatch):
    import socket

    from services.infra import pinned_http
    from services.phone.phone_adapters.ami import AMIClient
    monkeypatch.setattr(pinned_http, "_getaddrinfo", lambda h, p, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", p))])
    client = AMIClient(host="pbx.example", port=5038, username="u", secret="s")
    with pytest.raises(phone_adapters.PhoneAdapterError) as ei:
        asyncio.run(client.connect())
    assert "may not reach" in ei.value.message
