"""HTTP tests for the phone-server admin API.

Covers server CRUD, default selection, AMI-secret storage (encrypted in
infra_credentials, never on the row), and the FK-RESTRICT 409 when a route
still references the server.
"""

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


def test_create_first_is_default(client):
    s = client.post("/v1/admin/phone-servers", json={"name": "pbx1", "host": "h1"}).json()
    assert s["is_default"] is True
    assert s["ami_secret_configured"] is False
    s2 = client.post("/v1/admin/phone-servers", json={"name": "pbx2"}).json()
    assert s2["is_default"] is False


def test_set_default_moves(client):
    s1 = client.post("/v1/admin/phone-servers", json={"name": "pbx1"}).json()
    s2 = client.post("/v1/admin/phone-servers", json={"name": "pbx2"}).json()
    resp = client.put(f"/v1/admin/phone-servers/{s2['id']}/default")
    assert resp.status_code == 200
    servers = {x["id"]: x for x in client.get("/v1/admin/phone-servers").json()["servers"]}
    assert servers[s1["id"]]["is_default"] is False
    assert servers[s2["id"]]["is_default"] is True


def test_ami_secret_set_status_and_delete(client):
    s = client.post("/v1/admin/phone-servers", json={"name": "pbx"}).json()
    resp = client.put(f"/v1/admin/phone-servers/{s['id']}/ami-secret", json={"value": "topsecret"})
    assert resp.status_code == 200
    fetched = client.get("/v1/admin/phone-servers").json()["servers"][0]
    assert fetched["ami_secret_configured"] is True
    # The secret must NOT be echoed back on the row.
    assert "topsecret" not in str(fetched)
    assert client.delete(f"/v1/admin/phone-servers/{s['id']}/ami-secret").status_code == 200
    assert client.get("/v1/admin/phone-servers").json()["servers"][0]["ami_secret_configured"] is False


def test_config_persists(client):
    s = client.post("/v1/admin/phone-servers", json={
        "name": "pbx", "config": {"ami_host": "10.0.0.1", "ami_port": "5038", "ami_username": "admin"},
    }).json()
    assert s["config"]["ami_host"] == "10.0.0.1"
    r = client.put(f"/v1/admin/phone-servers/{s['id']}", json={"config": {"ami_host": "10.0.0.2"}})
    assert r.json()["config"]["ami_host"] == "10.0.0.2"


def test_delete_blocked_when_route_uses_server(client):
    s = client.post("/v1/admin/phone-servers", json={"name": "pbx"}).json()
    # Routes can only be provisioned against a bootstrap-verified server.
    assert client.post(f"/v1/admin/phone-servers/{s['id']}/bootstrap/verify").status_code == 200
    route = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "agent": "ag", "phone_server_id": s["id"],
    })
    assert route.status_code == 200, route.text
    r = client.delete(f"/v1/admin/phone-servers/{s['id']}")
    assert r.status_code == 409
    # Deleting the route frees the server.
    assert client.delete(f"/v1/admin/phone/routes/{route.json()['id']}").status_code == 200
    assert client.delete(f"/v1/admin/phone-servers/{s['id']}").status_code == 200
