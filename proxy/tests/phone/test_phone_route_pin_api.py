"""Route PIN — write-only secret sub-resource + the config push's shape.

The PIN is an encrypted infra_credentials sidecar (never a route column):
API responses carry only ``pin_configured``; the daemon config push carries
the plaintext for local enforcement (the management-WS secret rule), only
where the PIN applies (inbound, or outbound in user identity); route
deletion and the scope guard keep credentials from being stranded.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.phone import phone_config
from storage.pg import get_conn
from storage.phone import phone_route_store
from tests.phone.test_phone_route_identity import _make_agent, _make_user


@pytest.fixture
def daemon_outbound_pin():
    """A connected daemon that gates outbound calls on a route's PIN."""
    marker = object()
    phone_config.set_management_capabilities(marker, {phone_config.OUTBOUND_PIN_CAPABILITY: True})
    yield marker
    phone_config.clear_management_capabilities(marker)


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


def _make_phone_server(adapter_type: str = "asterisk_manual", *, verified: bool = False) -> int:
    """A server row; ``verified`` lets a route be re-provisioned on it (the
    manual adapter provisions without I/O)."""
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO phone_servers (name, adapter_type, host, bootstrap_status, "
            "created_at, updated_at) "
            "VALUES (%s, %s, '', %s, NOW()::text, NOW()::text) RETURNING id",
            (f"pbx-{uuid.uuid4().hex[:8]}", adapter_type, "verified" if verified else "pending"),
        ).fetchone()
        conn.commit()
    return row["id"]


def _make_route(direction: str = "inbound", *, verified: bool = False, **extra) -> dict:
    return phone_route_store.create_route({
        "direction": direction,
        "name": f"route-{uuid.uuid4().hex[:6]}",
        "agent": "personal-assistant",
        "phone_server_id": extra.pop("phone_server_id", None) or _make_phone_server(verified=verified),
        "audiosocket_uuid": str(uuid.uuid4()) if direction == "inbound" else None,
        **extra,
    })


def _user_route(direction: str = "outbound", *, verified: bool = False, **extra) -> dict:
    """A route tied to a platform user who is assigned to its agent."""
    agent = _make_agent()
    sub = _make_user(agents=(agent,))
    return _make_route(direction, verified=verified, agent=agent, identity_mode="user",
                       identity_user_sub=sub, **extra)


def _listed(client, route_id: str) -> dict:
    return next(x for x in client.get("/v1/admin/phone/routes").json()["routes"] if x["id"] == route_id)


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
                       json={"value": "471125"})
        assert r.status_code == 200 and r.json()["pin_configured"] is True

        listed = client.get("/v1/admin/phone/routes").json()["routes"]
        mine = next(x for x in listed if x["id"] == route["id"])
        assert mine["pin_configured"] is True
        # The value never appears anywhere in the API response.
        assert "471125" not in str(listed)
        # Encrypted at rest — ciphertext only.
        rows = _cred_rows(route["id"])
        assert rows and "471125" not in rows[0]["credential_value_enc"]

    @pytest.mark.parametrize("bad", ["123", "1234", "12345", "1234567", "abcd", "12 34", ""])
    def test_format_validation_never_echoes(self, client, bad):
        route = _make_route("inbound")
        r = client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                       json={"value": bad})
        assert r.status_code == 400
        if bad:
            assert bad not in r.json()["detail"]

    def test_where_a_pin_belongs(self, client, daemon_outbound_pin):
        """Every inbound route; an outbound route tied to a platform user
        (the person who answers enters it before the agent speaks). The
        outbound advisory stands only while no PIN is set."""
        caller_out = _make_route("outbound")
        r = client.put(f"/v1/admin/phone/routes/{caller_out['id']}/pin", json={"value": "123456"})
        assert r.status_code == 400 and "tied to a platform user" in r.json()["detail"]
        assert _listed(client, caller_out["id"])["pin_configured"] is False
        user_out = _user_route("outbound")
        assert any("Set a PIN" in w for w in _listed(client, user_out["id"])["warnings"])
        r = client.put(f"/v1/admin/phone/routes/{user_out['id']}/pin", json={"value": "123456"})
        assert r.status_code == 200 and r.json()["pin_configured"] is True
        mine = _listed(client, user_out["id"])
        assert mine["pin_configured"] is True and mine["warnings"] == []

    def test_an_outbound_pin_needs_a_daemon_that_gates(self, client):
        """A released daemon gates inbound calls only: without a connected
        daemon that says otherwise the PIN is refused, and one already stored
        is flagged as not asked for."""
        user_out = _user_route("outbound")
        r = client.put(f"/v1/admin/phone/routes/{user_out['id']}/pin", json={"value": "123456"})
        assert r.status_code == 409 and "update it first" in r.json()["detail"]
        phone_route_store.set_route_pin(user_out["id"], "123456")
        mine = _listed(client, user_out["id"])
        assert mine["pin_configured"] is True
        assert any("not asked for" in w for w in mine["warnings"])

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
                   json={"value": "987654"})
        assert _cred_rows(route["id"])
        assert client.delete(
            f"/v1/admin/phone/routes/{route['id']}").status_code == 200
        assert _cred_rows(route["id"]) == []

    def test_direction_flip_blocked_while_pin_configured(self, client):
        route = _make_route("inbound")
        client.put(f"/v1/admin/phone/routes/{route['id']}/pin",
                   json={"value": "123456"})
        r = client.put(f"/v1/admin/phone/routes/{route['id']}",
                       json={"direction": "outbound"})
        assert r.status_code == 409
        assert "PIN" in r.json()["detail"]

    def test_a_user_route_keeps_its_pin_across_the_flip_but_not_into_caller_identity(
            self, client, daemon_outbound_pin):
        route = _user_route("inbound", verified=True)
        client.put(f"/v1/admin/phone/routes/{route['id']}/pin", json={"value": "123456"})
        r = client.put(f"/v1/admin/phone/routes/{route['id']}", json={"direction": "outbound"})
        assert r.status_code == 200, r.text
        assert _listed(client, route["id"])["pin_configured"] is True
        r = client.put(f"/v1/admin/phone/routes/{route['id']}",
                       json={"identity_mode": "caller", "identity_user_sub": ""})
        assert r.status_code == 409 and "Remove the PIN" in r.json()["detail"]
        # A PIN sent with a save that moves the route where none belongs.
        r = client.put(f"/v1/admin/phone/routes/{route['id']}",
                       json={"identity_mode": "caller", "identity_user_sub": "", "pin": "654321"})
        assert r.status_code == 400

    def test_a_stored_pin_going_outbound_needs_a_daemon_that_gates(self, client):
        """No connected daemon gates outbound calls: the flip that would
        carry the stored PIN there is refused like setting one would be."""
        route = _user_route("inbound", verified=True)
        client.put(f"/v1/admin/phone/routes/{route['id']}/pin", json={"value": "123456"})
        r = client.put(f"/v1/admin/phone/routes/{route['id']}", json={"direction": "outbound"})
        assert r.status_code == 409 and "does not gate outbound" in r.json()["detail"]
        assert _listed(client, route["id"])["direction"] == "inbound"


class TestConfigPushContract:
    def test_the_push_withholds_an_outbound_pin_from_a_daemon_without_the_gate(
            self, client, temp_db, daemon_outbound_pin):
        from services.phone.phone_config import (
            OUTBOUND_PIN_CAPABILITY,
            assemble_phone_config,
            config_for_daemon,
        )

        gated = _make_route("inbound")
        user_out = _user_route("outbound")
        client.put(f"/v1/admin/phone/routes/{gated['id']}/pin", json={"value": "246813"})
        client.put(f"/v1/admin/phone/routes/{user_out['id']}/pin", json={"value": "135791"})
        config = assemble_phone_config()
        for caps in (None, {}, {"call_control": True}):
            routes = {r["id"]: r for r in config_for_daemon(config, caps)["routes"]}
            assert routes[gated["id"]]["pin"] == "246813"
            assert "pin" not in routes[user_out["id"]]
        assert config_for_daemon(config, {OUTBOUND_PIN_CAPABILITY: True}) is config
        # The source is left as assembled.
        assert {r["id"]: r for r in config["routes"]}[user_out["id"]]["pin"] == "135791"

    def test_push_carries_plaintext_pin_where_a_pin_belongs(self, client, temp_db, daemon_outbound_pin):
        from services.phone.phone_config import assemble_phone_config

        gated = _make_route("inbound")
        plain = _make_route("inbound")
        out = _make_route("outbound")
        user_out = _user_route("outbound")
        client.put(f"/v1/admin/phone/routes/{gated['id']}/pin",
                   json={"value": "246813"})
        client.put(f"/v1/admin/phone/routes/{user_out['id']}/pin",
                   json={"value": "135791"})
        # A PIN stranded on an outbound caller route (stored before the
        # rules) gates nobody: never pushed.
        phone_route_store.set_route_pin(out["id"], "999999")

        pushed = {r["id"]: r for r in assemble_phone_config()["routes"]}
        assert pushed[gated["id"]]["pin"] == "246813"
        assert pushed[user_out["id"]]["pin"] == "135791"
        assert "pin" not in pushed[plain["id"]]
        assert "pin" not in pushed[out["id"]]

    def test_a_shorter_pin_stored_earlier_keeps_working(self, client, temp_db):
        """The 6-digit floor applies to a PIN being set: a 4-digit one
        stored before it still reaches the daemon and survives a save."""
        from services.phone.phone_config import assemble_phone_config

        route = _make_route("inbound")
        phone_route_store.set_route_pin(route["id"], "4711")
        r = client.put(f"/v1/admin/phone/routes/{route['id']}", json={"name": "renamed"})
        assert r.status_code == 200, r.text
        pushed = {x["id"]: x for x in assemble_phone_config()["routes"]}
        assert pushed[route["id"]]["pin"] == "4711"
        r = client.put(f"/v1/admin/phone/routes/{route['id']}/pin", json={"value": "4712"})
        assert r.status_code == 400 and "6 digits" in r.json()["detail"]
        assert phone_route_store.get_route_pin(route["id"]) == "4711"
