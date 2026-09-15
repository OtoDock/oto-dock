"""Bootstrap + health endpoint tests — against the real manual adapter
and a provider stub (no patching, no live PBX)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


@pytest.fixture
def client(temp_db):
    from api.phone import phone as phone_router

    app = FastAPI()
    app.include_router(phone_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def test_manual_bootstrap_snippet_verify_health(client):
    s = client.post("/v1/admin/phone-servers", json={
        "name": "pbx", "adapter_type": "asterisk_manual",
        "config": {"audiosocket_endpoint": "ph.local:9092"}}).json()
    sid = s["id"]
    assert s["bootstrap_status"] == "pending"

    boot = client.get(f"/v1/admin/phone-servers/{sid}/bootstrap").json()
    assert "[oto-audiosocket-bridge]" in boot["snippet"]
    assert "ph.local:9092" in boot["snippet"]
    assert boot["requires_bootstrap"] is True and boot["supports_sftp"] is False
    assert boot["status"] == "pending"

    v = client.post(f"/v1/admin/phone-servers/{sid}/bootstrap/verify")
    assert v.status_code == 200 and v.json()["bootstrap_status"] == "verified"
    # persisted
    fetched = client.get("/v1/admin/phone-servers").json()["servers"][0]
    assert fetched["bootstrap_status"] == "verified"
    assert "verify → verified" in fetched["bootstrap_log"]

    h = client.post(f"/v1/admin/phone-servers/{sid}/health")
    assert h.status_code == 200 and h.json()["last_health_status"] == "healthy"


def test_manual_apply_sftp_unsupported(client):
    sid = client.post("/v1/admin/phone-servers", json={"name": "pbx"}).json()["id"]
    r = client.post(f"/v1/admin/phone-servers/{sid}/bootstrap/apply",
                    json={"host": "h", "username": "u", "password": "p"})
    assert r.status_code == 400


def test_stub_provider_bootstrap_is_graceful(client):
    s = client.post("/v1/admin/phone-servers", json={
        "name": "tw", "adapter_type": "twilio"}).json()
    sid = s["id"]
    boot = client.get(f"/v1/admin/phone-servers/{sid}/bootstrap").json()
    assert boot["snippet"] is None
    assert boot["requires_bootstrap"] is False
    v = client.post(f"/v1/admin/phone-servers/{sid}/bootstrap/verify")
    assert v.status_code == 200 and v.json()["bootstrap_status"] == "failed"


def test_bootstrap_unknown_server_404(client):
    assert client.get("/v1/admin/phone-servers/99999/bootstrap").status_code == 404
    assert client.post("/v1/admin/phone-servers/99999/health").status_code == 404


def test_register_secret_minted_shipped_as_list_and_revoked(client):
    """A per-server register secret is minted, embedded in the snippet, shipped in
    the pushed config as a JSON-serializable LIST, and revoked on delete."""
    import json

    from services.phone import phone_config

    s = client.post("/v1/admin/phone-servers", json={
        "name": "pbx-reg", "adapter_type": "asterisk_manual",
        "config": {"audiosocket_endpoint": "ph.local:9092"}}).json()
    sid = s["id"]

    cfg = phone_config.assemble_phone_config()
    shipped = cfg["credentials"]["register_secrets"]
    # a populated LIST, never a set — it MUST round-trip json.dumps for the WS push
    assert isinstance(shipped, list) and len(shipped) >= 1
    json.dumps(cfg)  # would raise on a set

    # the server's snippet embeds one of the shipped secrets as the Bearer token
    snippet = client.get(f"/v1/admin/phone-servers/{sid}/bootstrap").json()["snippet"]
    assert any(sec and f"Bearer {sec}" in snippet for sec in shipped)

    # deleting the server revokes its secret — it drops out of the shipped list
    before = set(phone_config.assemble_phone_config()["credentials"]["register_secrets"])
    client.delete(f"/v1/admin/phone-servers/{sid}")
    after = set(phone_config.assemble_phone_config()["credentials"]["register_secrets"])
    assert before - after  # the deleted server's secret is gone


def test_ami_user_snippet_minted_and_stable(client):
    """AMI adapters get a generated manager-user snippet: username + secret are
    minted once, stored as the server's real AMI credentials (Verify needs no
    typing), stable across re-renders, and IP-locked to the proxy's PBX-facing
    host. Admin-set usernames are respected, never overwritten."""
    s = client.post("/v1/admin/phone-servers", json={
        "name": "pbx", "adapter_type": "asterisk_freepbx",
        "host": "pbx.local",
        "config": {"audiosocket_endpoint": "1.2.3.4:9092",
                   "http_api_endpoint": "1.2.3.4:9093"}}).json()
    sid = s["id"]

    boot = client.get(f"/v1/admin/phone-servers/{sid}/bootstrap").json()
    assert boot["ami_snippet_file"] == "manager_custom.conf"
    assert boot["ami_username"] == "otodock"
    assert "[otodock]" in boot["ami_snippet"]
    assert "permit = 1.2.3.4/255.255.255.255" in boot["ami_snippet"]

    # minted secret landed in the credential store AND is stable on re-render
    from storage.identity import credential_store
    from storage.phone import phone_server_store
    stored = credential_store.get_infra_credentials(
        phone_server_store.ami_cred_name(sid))[phone_server_store.AMI_SECRET_KEY]
    assert stored and f"secret = {stored}" in boot["ami_snippet"]
    again = client.get(f"/v1/admin/phone-servers/{sid}/bootstrap").json()
    assert again["ami_snippet"] == boot["ami_snippet"]

    # the row config gained the username → the adapter's AMI params are wired
    fetched = client.get("/v1/admin/phone-servers").json()["servers"][0]
    assert fetched["config"]["ami_username"] == "otodock"
    assert fetched["ami_secret_configured"] is True


def test_ami_user_snippet_respects_admin_values(client):
    s = client.post("/v1/admin/phone-servers", json={
        "name": "pbx", "adapter_type": "asterisk_manual",
        "config": {"ami_username": "myuser",
                   "audiosocket_endpoint": "ph.local:9092"},
        "ami_secret": "presetsecret"}).json()
    boot = client.get(f"/v1/admin/phone-servers/{s['id']}/bootstrap").json()
    assert boot["ami_snippet_file"] == "manager.conf"  # plain Asterisk target
    assert boot["ami_username"] == "myuser"
    assert "secret = presetsecret" in boot["ami_snippet"]


def test_ami_user_snippet_absent_for_stub_adapters(client):
    s = client.post("/v1/admin/phone-servers", json={
        "name": "tw", "adapter_type": "twilio"}).json()
    boot = client.get(f"/v1/admin/phone-servers/{s['id']}/bootstrap").json()
    assert boot["ami_snippet"] is None and boot["ami_snippet_file"] is None
