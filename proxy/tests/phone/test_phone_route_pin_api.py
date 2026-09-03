"""Inbound route PIN — write-only secret sub-resource + config-push contract.

The PIN is an encrypted infra_credentials sidecar (never a route column):
API responses carry only ``pin_configured``; the daemon config push carries
the plaintext for local enforcement (management-WS secret contract); route
deletion and the inbound→outbound direction guard keep credentials from
being stranded.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from storage import phone_route_store
from storage.pg import get_conn


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


def _make_phone_server(adapter_type: str = "asterisk_manual") -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO phone_servers (name, adapter_type, host, "
            "created_at, updated_at) "
            "VALUES (%s, %s, '', NOW()::text, NOW()::text) RETURNING id",
            (f"pbx-{uuid.uuid4().hex[:8]}", adapter_type),
        ).fetchone()
        conn.commit()
    return row["id"]


def _make_route(direction: str = "inbound", **extra) -> dict:
    return phone_route_store.create_route({
        "direction": direction,
        "name": f"route-{uuid.uuid4().hex[:6]}",
        "agent": "personal-assistant",
        "phone_server_id": extra.pop("phone_server_id", None) or _make_phone_server(),
        "audiosocket_uuid": str(uuid.uuid4()) if direction == "inbound" else None,
        **extra,
    })


def _cred_rows(route_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT credential_value_enc FROM infra_credentials WHERE mcp_name = %s",
            (phone_route_store.pin_cred_name(route_id),),
        ).fetchall()


class TestPinSecret:
    def test_set_masks_and_encrypts(self, client):
        route = _make_route("inbound")
        r = client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                       json={"value": "4711"})
        assert r.status_code == 200 and r.json()["pin_configured"] is True

        listed = client.get("/v1/admin/phone/routes").json()["routes"]
        mine = next(x for x in listed if x["id"] == route["id"])
        assert mine["pin_configured"] is True
        # The value never appears anywhere in the API response.
        assert "4711" not in str(listed)
        # Encrypted at rest — ciphertext only.
        rows = _cred_rows(route["id"])
        assert rows and "4711" not in rows[0]["credential_value_enc"]

    @pytest.mark.parametrize("bad", ["123", "1234567", "abcd", "12 34", ""])
    def test_format_validation_never_echoes(self, client, bad):
        route = _make_route("inbound")
        r = client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                       json={"value": bad})
        assert r.status_code == 400
        if bad:
            assert bad not in r.json()["detail"]

    def test_inbound_only(self, client):
        route = _make_route("outbound")
        r = client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                       json={"value": "1234"})
        assert r.status_code == 400
        listed = client.get("/v1/admin/phone/routes").json()["routes"]
        mine = next(x for x in listed if x["id"] == route["id"])
        assert mine["pin_configured"] is False

    def test_delete_clears(self, client):
        route = _make_route("inbound")
        client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                   json={"value": "123456"})
        assert client.delete(
            f"/v1/admin/phone/routes/{route['id']}/pin").status_code == 200
        mine = next(x for x in client.get("/v1/admin/phone/routes").json()["routes"]
                    if x["id"] == route["id"])
        assert mine["pin_configured"] is False
        assert _cred_rows(route["id"]) == []


class TestPinLifecycleGuards:
    def test_route_delete_removes_credential(self, client):
        route = _make_route("inbound")
        client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                   json={"value": "9876"})
        assert _cred_rows(route["id"])
        assert client.delete(
            f"/v1/admin/phone/routes/{route['id']}").status_code == 200
        assert _cred_rows(route["id"]) == []

    def test_direction_flip_blocked_while_pin_configured(self, client):
        route = _make_route("inbound")
        client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                   json={"value": "1234"})
        r = client.put(f"/v1/admin/phone/routes/{route['id']}",
                       json={"direction": "outbound"})
        assert r.status_code == 409
        assert "PIN" in r.json()["detail"]


class TestConfigPushContract:
    def test_push_carries_plaintext_pin_for_inbound_only(self, client, temp_db):
        from services.phone.phone_config import assemble_phone_config

        gated = _make_route("inbound")
        plain = _make_route("inbound")
        out = _make_route("outbound")
        client.put(f"/v1/admin/phone/routes/{gated['id']}/pin",
                   json={"value": "2468"})

        pushed = {r["id"]: r for r in assemble_phone_config()["routes"]}
        assert pushed[gated["id"]]["pin"] == "2468"
        assert "pin" not in pushed[plain["id"]]
        assert "pin" not in pushed[out["id"]]
