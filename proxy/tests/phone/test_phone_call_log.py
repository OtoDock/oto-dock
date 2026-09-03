"""Phone call log: daemon report ingest + admin read API + retention.

One row per call (incl. never-warmed calls: PIN-refused, capacity-rejected).
The ingest endpoint enriches from the route row (name, agent, outbound
caller-ID) and coerces junk; the FK is dropped for unknown routes so the row
still lands. Route deletion NULLs the FK but keeps the name snapshot.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth.providers import UserContext, get_current_user
from storage import phone_call_log_store, phone_route_store
from storage.pg import get_conn


@pytest.fixture
def client(temp_db):
    from api.phone import phone as phone_router
    from api.phone import phone_usage as phone_usage_router

    app = FastAPI()
    app.include_router(phone_router.router)
    app.include_router(phone_usage_router.router)

    async def _admin():
        return UserContext(sub="admin-sub", email="admin@test.com", name="Admin",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {config.API_KEY}"}


def _make_server() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO phone_servers (name, adapter_type, host, "
            "created_at, updated_at) "
            "VALUES (%s, 'asterisk_manual', '', NOW()::text, NOW()::text) "
            "RETURNING id",
            (f"pbx-{uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.commit()
    return row["id"]


def _make_route(direction="inbound", **extra) -> dict:
    return phone_route_store.create_route({
        "direction": direction,
        "name": extra.pop("name", f"route-{uuid.uuid4().hex[:6]}"),
        "agent": "personal-assistant",
        "phone_server_id": _make_server(),
        "audiosocket_uuid": str(uuid.uuid4()) if direction == "inbound" else None,
        **extra,
    })


def _report(client, **over):
    payload = {
        "route_id": "", "direction": "inbound",
        "from_number": "+15550001111", "to_number": "+16085550147",
        "transport": "twilio", "call_uuid": f"CA{uuid.uuid4().hex[:12]}",
        "outcome": "completed", "pin_attempts": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": 42,
        **over,
    }
    return client.post("/v1/phone/calls/report", headers=_auth(), json=payload)


class TestIngest:
    def test_requires_master_key(self, client):
        r = client.post("/v1/phone/calls/report", json={"outcome": "completed"})
        assert r.status_code in (401, 403)

    def test_report_is_on_the_service_key_allowlist(self):
        """Live-hit 2026-08-14: the daemon's teardown reports all 403'd —
        the ``service_key_confinement`` middleware rejected the master key
        before ``verify_api_key`` ever ran, because the new endpoint wasn't
        on the S2S allowlist (tests mount the router without middleware, so
        nothing caught it). Pin the entry, method-scoped."""
        from auth.service_endpoints import is_service_endpoint_allowed
        assert is_service_endpoint_allowed("POST", "/v1/phone/calls/report")
        assert not is_service_endpoint_allowed("GET", "/v1/phone/calls/report")
        assert not is_service_endpoint_allowed("POST", "/v1/admin/phone/call-log")

    def test_report_enriches_from_route(self, client):
        route = _make_route("inbound", name="Twilio Test")
        r = _report(client, route_id=route["id"], outcome="pin_failed",
                    pin_attempts=3)
        assert r.status_code == 200 and r.json()["recorded"] is True

        body = client.get("/v1/admin/phone/call-log",
                          params={"route_id": route["id"]}).json()
        assert body["total"] == 1
        row = body["calls"][0]
        assert row["route_name"] == "Twilio Test"
        assert row["agent"] == "personal-assistant"
        assert row["outcome"] == "pin_failed"
        assert row["pin_attempts"] == 3
        assert row["from_number"] == "+15550001111"

    def test_junk_outcome_and_direction_coerced(self, client):
        _report(client, outcome="lol", direction="sideways")
        row = client.get("/v1/admin/phone/call-log").json()["calls"][0]
        assert row["outcome"] == "failed"
        assert row["direction"] == "inbound"

    def test_unknown_route_id_lands_without_fk(self, client):
        r = _report(client, route_id=str(uuid.uuid4()))
        assert r.json()["recorded"] is True
        row = client.get("/v1/admin/phone/call-log").json()["calls"][0]
        assert row["route_id"] is None

    def test_outbound_from_number_falls_back_to_caller_id(self, client):
        route = _make_route("outbound", ami_caller_id="+16085550147")
        _report(client, route_id=route["id"], direction="outbound",
                from_number="", to_number="+15550002222", outcome="no_answer")
        row = client.get("/v1/admin/phone/call-log",
                         params={"route_id": route["id"]}).json()["calls"][0]
        assert row["from_number"] == "+16085550147"
        assert row["outcome"] == "no_answer"


class TestReadApi:
    def test_filter_and_pagination(self, client):
        r1, r2 = _make_route("inbound"), _make_route("inbound")
        for i in range(3):
            _report(client, route_id=r1["id"],
                    started_at=f"2026-08-1{i + 1}T00:00:00+00:00")
        _report(client, route_id=r2["id"])

        all_rows = client.get("/v1/admin/phone/call-log").json()
        assert all_rows["total"] == 4
        one = client.get("/v1/admin/phone/call-log",
                         params={"route_id": r1["id"]}).json()
        assert one["total"] == 3
        # Newest-first + offset/limit.
        page = client.get(
            "/v1/admin/phone/call-log",
            params={"route_id": r1["id"], "offset": 1, "limit": 1}).json()
        assert len(page["calls"]) == 1
        assert page["calls"][0]["started_at"].startswith("2026-08-12")

    def test_pin_value_never_present(self, client):
        # Belt-and-braces: nothing PIN-shaped is stored beyond the count.
        route = _make_route("inbound")
        _report(client, route_id=route["id"], outcome="pin_failed",
                pin_attempts=2)
        row = client.get("/v1/admin/phone/call-log").json()["calls"][0]
        assert set(row) >= {"outcome", "pin_attempts"}
        assert "pin" not in {k for k in row if k != "pin_attempts"}


class TestRetentionAndCascade:
    def test_old_rows_pruned_on_insert(self, client, temp_db):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        phone_call_log_store.insert_call({
            "direction": "inbound", "outcome": "completed", "started_at": old,
        })
        _report(client)  # fresh insert triggers the prune
        body = client.get("/v1/admin/phone/call-log").json()
        assert body["total"] == 1
        assert body["calls"][0]["started_at"] > old

    def test_route_delete_nulls_fk_keeps_name(self, client, temp_db):
        route = _make_route("inbound", name="Ephemeral")
        _report(client, route_id=route["id"])
        phone_route_store.delete_route(route["id"])
        row = client.get("/v1/admin/phone/call-log").json()["calls"][0]
        assert row["route_id"] is None
        assert row["route_name"] == "Ephemeral"
