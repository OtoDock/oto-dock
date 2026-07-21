"""Route-cascade tests — create/delete/update provision via the adapter.

Uses a controllable in-memory FakeAdapter (patched over ``load_adapter``) so the
engine's orchestration — gate-on-verified, UUID allocation, adapter_data
persistence, 502 + rollback, best-effort deprovision, re-provision-on-edit — is
exercised without any live PBX.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


class _State:
    def __init__(self):
        self.fail_provision = False
        self.fail_deprovision = False
        self.calls: list[tuple[str, str]] = []


@pytest.fixture
def cascade(temp_db, monkeypatch):
    from api.phone import phone as phone_router
    from services.phone import phone_adapters

    state = _State()

    class FakeAdapter(phone_adapters.PhoneServerAdapter):
        adapter_type = "fake"

        def __init__(self, server):
            super().__init__(
                server,
                credential_resolver=lambda suffix: {},
                media_endpoint="127.0.0.1:40000",
                register_endpoint="127.0.0.1:8600",
            )

        async def health_check(self):
            return phone_adapters.HealthStatus(healthy=True, detail="ok")

        async def get_bootstrap_snippet(self):
            return "snippet"

        async def verify_bootstrap(self):
            return phone_adapters.BootstrapResult(status="verified")

        async def provision_route(self, route):
            state.calls.append(("provision", route["id"]))
            if state.fail_provision:
                raise phone_adapters.PhoneAdapterError(
                    "provision boom", status_code=502, vendor_status=500)
            return phone_adapters.RouteHandle(
                adapter_data={"ok": True, "did": route.get("did")},
                audiosocket_uuid=route.get("audiosocket_uuid"),
                did=route.get("did"),
                instructions="run the AstDB command",
            )

        async def deprovision_route(self, route):
            state.calls.append(("deprovision", route["id"]))
            if state.fail_deprovision:
                raise phone_adapters.PhoneAdapterError("deprovision boom")

    monkeypatch.setattr(phone_adapters, "load_adapter", FakeAdapter)

    app = FastAPI()
    app.include_router(phone_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app), state


def _verified_server(client, name="pbx"):
    s = client.post("/v1/admin/phone-servers", json={"name": name}).json()
    assert client.post(f"/v1/admin/phone-servers/{s['id']}/bootstrap/verify").status_code == 200
    return s


def test_create_inbound_provisions_and_persists(cascade):
    client, state = cascade
    s = _verified_server(client)
    r = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "name": "main", "agent": "pa",
        "did": "+30210", "phone_server_id": s["id"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # UUID auto-allocated, adapter_data persisted, instructions surfaced
    assert body["audiosocket_uuid"]
    assert body["adapter_data"]["ok"] is True
    assert body["provisioning_instructions"] == "run the AstDB command"
    assert ("provision", body["id"]) in state.calls


def test_create_on_unverified_server_409(cascade):
    client, state = cascade
    s = client.post("/v1/admin/phone-servers", json={"name": "pbx"}).json()  # pending
    r = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+1", "phone_server_id": s["id"]})
    assert r.status_code == 409
    assert "not verified" in r.json()["detail"]
    assert state.calls == []  # never reached the adapter


def test_provision_failure_rolls_back_and_502(cascade):
    client, state = cascade
    s = _verified_server(client)
    state.fail_provision = True
    r = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+30211", "phone_server_id": s["id"]})
    assert r.status_code == 502
    assert "provider returned 500" in r.json()["detail"]
    # the row must NOT survive a failed provision
    assert client.get("/v1/admin/phone/routes").json()["routes"] == []


def test_delete_deprovisions(cascade):
    client, state = cascade
    s = _verified_server(client)
    rid = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+30212", "phone_server_id": s["id"]}).json()["id"]
    state.calls.clear()
    resp = client.delete(f"/v1/admin/phone/routes/{rid}")
    assert resp.status_code == 200 and "warning" not in resp.json()
    assert ("deprovision", rid) in state.calls
    assert client.get("/v1/admin/phone/routes").json()["routes"] == []


def test_delete_survives_deprovision_failure_with_warning(cascade):
    client, state = cascade
    s = _verified_server(client)
    rid = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+30213", "phone_server_id": s["id"]}).json()["id"]
    state.fail_deprovision = True
    resp = client.delete(f"/v1/admin/phone/routes/{rid}")
    assert resp.status_code == 200
    assert "de-provisioning on the phone server failed" in resp.json()["warning"]
    # row is still gone (local delete always wins)
    assert client.get("/v1/admin/phone/routes").json()["routes"] == []


def test_update_did_reprovisions(cascade):
    client, state = cascade
    s = _verified_server(client)
    rid = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+30214", "phone_server_id": s["id"]}).json()["id"]
    state.calls.clear()
    resp = client.put(f"/v1/admin/phone/routes/{rid}", json={"did": "+30215"})
    assert resp.status_code == 200
    # provision on the new identity happens before tearing down the old one
    ops = [op for op, _ in state.calls]
    assert ops == ["provision", "deprovision"]
    assert client.get("/v1/admin/phone/routes").json()["routes"][0]["did"] == "+30215"


def test_update_non_identity_field_skips_reprovision(cascade):
    client, state = cascade
    s = _verified_server(client)
    rid = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+30216", "phone_server_id": s["id"]}).json()["id"]
    state.calls.clear()
    resp = client.put(f"/v1/admin/phone/routes/{rid}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert state.calls == []  # no adapter calls for a plain rename


def test_duplicate_inbound_did_409(cascade):
    client, state = cascade
    s = _verified_server(client)
    ok = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+30220", "phone_server_id": s["id"]})
    assert ok.status_code == 200
    dup = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "did": "+30220", "phone_server_id": s["id"]})
    assert dup.status_code == 409
    assert "already routed" in dup.json()["detail"]


def test_outbound_route_persists_dial_prefix(cascade):
    client, state = cascade
    s = _verified_server(client)
    r = client.post("/v1/admin/phone/routes", json={
        "direction": "outbound", "name": "sales", "agent": "caller",
        "ami_caller_id": '"Acme Test" <+15551234567>', "dial_prefix": "81",
        "phone_server_id": s["id"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # dial_prefix + caller_id (with name decoration) persist and survive the cascade
    assert body["dial_prefix"] == "81"
    assert body["ami_caller_id"] == '"Acme Test" <+15551234567>'
    # it's in the list the config push sends to the daemon
    routes = client.get("/v1/admin/phone/routes").json()["routes"]
    assert any(rt["id"] == body["id"] and rt["dial_prefix"] == "81" for rt in routes)
    # editable
    upd = client.put(f"/v1/admin/phone/routes/{body['id']}", json={"dial_prefix": "82"})
    assert upd.status_code == 200 and upd.json()["dial_prefix"] == "82"


# -- FreePBX adapter end-to-end (real adapter, mocked AMI) ------------------

@pytest.fixture
def freepbx_cascade(temp_db, monkeypatch):
    """Cascade through the REAL FreePBX adapter with a mocked ``AMIClient`` — so
    ``load_adapter('asterisk_freepbx')`` → ``DBPut`` is exercised end-to-end
    through the API without a live PBX."""
    import config
    from api.phone import phone as phone_router
    from services.phone.phone_adapters import asterisk_freepbx

    monkeypatch.setattr(config, "AUDIOSOCKET_PUBLIC_HOST", "10.0.0.5")
    ami = {"store": {}, "puts": [], "dels": [], "params": None}

    class _FakeAMI:
        def __init__(self, *, host, port, username, secret):
            ami["params"] = (host, port, username, secret)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def db_put(self, f, k, v):
            ami["puts"].append((f, k, v)); ami["store"][(f, k)] = v

        async def db_get(self, f, k):
            return ami["store"].get((f, k))

        async def db_del(self, f, k):
            ami["dels"].append((f, k)); ami["store"].pop((f, k), None)

    monkeypatch.setattr(asterisk_freepbx, "AMIClient", _FakeAMI)

    app = FastAPI()
    app.include_router(phone_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app), ami


def test_freepbx_verify_then_provision_writes_astdb(freepbx_cascade):
    client, ami = freepbx_cascade
    s = client.post("/v1/admin/phone-servers", json={
        "name": "freepbx", "adapter_type": "asterisk_freepbx", "host": "pbx",
        "config": {"ami_host": "10.0.0.9", "ami_username": "otodock"},
        "ami_secret": "sek",
    }).json()
    # verify drives a real AMI DB round-trip (mocked) → verified
    v = client.post(f"/v1/admin/phone-servers/{s['id']}/bootstrap/verify")
    assert v.status_code == 200 and v.json()["bootstrap_status"] == "verified"
    assert ami["params"] == ("10.0.0.9", 5038, "otodock", "sek")

    r = client.post("/v1/admin/phone/routes", json={
        "direction": "inbound", "name": "main", "agent": "pa",
        "did": "200", "phone_server_id": s["id"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    uuid_val = body["audiosocket_uuid"]
    assert uuid_val
    assert body["adapter_data"] == {"mode": "ami", "astdb_key": "otodock/route_uuid/200"}
    assert "oto-audiosocket-bridge,200,1" in body["provisioning_instructions"]
    # the real adapter wrote the DID→UUID map over (mocked) AMI
    assert ("otodock", "route_uuid/200", uuid_val) in ami["puts"]

    # deleting the route deprovisions (DBDel) on the way out
    client.delete(f"/v1/admin/phone/routes/{body['id']}")
    assert ("otodock", "route_uuid/200") in ami["dels"]
