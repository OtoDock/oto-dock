"""Health/drift worker tests.

Drives ``run_health_tick`` directly (no sleep) against a controllable
FakeAdapter patched over ``load_adapter``, asserting health persistence and
drift detection. Manual/stub adapters return ``None`` from
``list_provisioned_routes`` → drift is untracked (verified below).
"""

from __future__ import annotations

import asyncio


def _make_fake(phone_adapters, dids, healthy=True):
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
            return phone_adapters.HealthStatus(healthy=healthy, detail="probe")

        async def get_bootstrap_snippet(self):
            return None

        async def verify_bootstrap(self):
            return phone_adapters.BootstrapResult(status="verified")

        async def provision_route(self, route):
            ...

        async def deprovision_route(self, route):
            ...

        async def list_provisioned_routes(self):
            if dids is None:
                return None
            return [phone_adapters.RouteHandle(did=d) for d in dids]

    return FakeAdapter


def _setup_verified_server_with_route(did="+30210"):
    from storage import phone_route_store, phone_server_store

    s = phone_server_store.create_server({"name": "pbx", "adapter_type": "asterisk_manual"})
    phone_server_store.update_server(s["id"], {"bootstrap_status": "verified"})
    phone_route_store.create_route({
        "direction": "inbound", "did": did, "agent": "pa",
        "phone_server_id": s["id"], "audiosocket_uuid": "u1",
    })
    return s["id"]


def test_health_persisted(temp_db, monkeypatch):
    from services.phone import phone_adapters, phone_health_worker
    from storage import phone_server_store

    sid = _setup_verified_server_with_route()
    monkeypatch.setattr(phone_adapters, "load_adapter",
                        _make_fake(phone_adapters, ["+30210"]))
    asyncio.run(phone_health_worker.run_health_tick())
    s = phone_server_store.get_server(sid)
    assert s["last_health_status"] == "healthy"
    assert s["last_health_check"]
    assert s["last_health_detail"] == "probe"


def test_drift_detected(temp_db, monkeypatch):
    from services.phone import phone_adapters, phone_health_worker
    from storage import phone_server_store

    sid = _setup_verified_server_with_route("+30210")
    # PBX reports a different DID set → drift.
    monkeypatch.setattr(phone_adapters, "load_adapter",
                        _make_fake(phone_adapters, ["+999"]))
    asyncio.run(phone_health_worker.run_health_tick())
    s = phone_server_store.get_server(sid)
    assert s["bootstrap_status"] == "drift"
    assert "drift detected" in s["bootstrap_log"]


def test_matching_routes_no_drift(temp_db, monkeypatch):
    from services.phone import phone_adapters, phone_health_worker
    from storage import phone_server_store

    sid = _setup_verified_server_with_route("+30210")
    monkeypatch.setattr(phone_adapters, "load_adapter",
                        _make_fake(phone_adapters, ["+30210"]))
    asyncio.run(phone_health_worker.run_health_tick())
    assert phone_server_store.get_server(sid)["bootstrap_status"] == "verified"


def test_untracked_adapter_no_drift(temp_db, monkeypatch):
    from services.phone import phone_adapters, phone_health_worker
    from storage import phone_server_store

    sid = _setup_verified_server_with_route("+30210")
    # list_provisioned_routes returns None → drift untracked, stays verified.
    monkeypatch.setattr(phone_adapters, "load_adapter",
                        _make_fake(phone_adapters, None))
    asyncio.run(phone_health_worker.run_health_tick())
    assert phone_server_store.get_server(sid)["bootstrap_status"] == "verified"
