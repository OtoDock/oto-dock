"""Unit tests for the phone-server adapter framework.

Covers the dataclasses + error, the manual Asterisk reference adapter, the
graceful provider stubs, and the loader factory (incl. media-endpoint
resolution). No live PBX — these exercise the engine surface directly.
"""

from __future__ import annotations

import asyncio

import pytest

from services.phone.phone_adapters import (
    BootstrapResult,
    HealthStatus,
    PhoneAdapterError,
    RouteHandle,
    load_adapter,
)


def _server(**over):
    row = {"id": 7, "name": "pbx", "host": "pbx.local",
           "adapter_type": "asterisk_manual", "config": {}}
    row.update(over)
    return row


def test_dataclasses_and_error():
    assert RouteHandle().adapter_data == {}
    assert HealthStatus(healthy=True).bootstrap_intact is None
    assert BootstrapResult(status="verified").snippet is None
    e = PhoneAdapterError("boom", status_code=504, vendor_status=500, vendor_body="x")
    assert e.status_code == 504 and e.vendor_status == 500 and e.message == "boom"
    # default status is the 502 "upstream vendor" envelope
    assert PhoneAdapterError("y").status_code == 502


def test_repr_never_leaks_secrets():
    a = load_adapter(_server())
    r = repr(a)
    assert "server_id=7" in r and "ManualAsteriskAdapter" in r


def test_manual_adapter_lifecycle():
    a = load_adapter(_server(config={"audiosocket_endpoint": "ph.local:9092",
                                     "http_api_endpoint": "ph.local:9093"}))
    snippet = asyncio.run(a.get_bootstrap_snippet())
    assert "[oto-audiosocket-bridge]" in snippet
    assert "exten => _.,1" in snippet  # pattern form (shared template; not `s`)
    assert "AudioSocket(${AS_UUID},ph.local:9092)" in snippet
    # outbound context ships in the same snippet too
    assert "[oto-audiosocket-outbound]" in snippet
    assert "AudioSocket(${OUTBOUND_UUID},ph.local:9092)" in snippet
    # inbound bridge also registers caller metadata (Bearer the per-server
    # secret) at the daemon's HTTP API endpoint before AudioSocket connects
    assert "http://ph.local:9093/v1/calls/register" in snippet
    assert "Authorization: Bearer" in snippet
    assert "__REGISTER_ENDPOINT__" not in snippet and "__REGISTER_SECRET__" not in snippet
    assert asyncio.run(a.verify_bootstrap()).status == "verified"
    assert asyncio.run(a.health_check()).healthy is True
    assert asyncio.run(a.list_provisioned_routes()) is None
    # inbound provisioning allocates nothing on the PBX but returns the AstDB cmd
    h = asyncio.run(a.provision_route(
        {"direction": "inbound", "did": "+30210", "audiosocket_uuid": "u-1", "id": "r1"}))
    assert h.did == "+30210" and h.audiosocket_uuid == "u-1"
    assert h.adapter_data["astdb_key"] == "otodock/route_uuid/+30210"
    assert "database put otodock/route_uuid/+30210 u-1" in h.instructions
    # outbound provisioning is a no-op handle
    ho = asyncio.run(a.provision_route({"direction": "outbound", "id": "r2"}))
    assert ho.audiosocket_uuid is None
    # deprovision is a no-op (returns None, never raises)
    assert asyncio.run(a.deprovision_route({"direction": "inbound", "did": "+30210", "id": "r1"})) is None


def test_manual_adapter_rejects_sftp():
    a = load_adapter(_server())
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(a.apply_bootstrap({"host": "x"}))
    assert ei.value.status_code == 400


def test_stub_adapters_degrade_gracefully():
    cx = load_adapter(_server(adapter_type="three_cx"))
    assert asyncio.run(cx.get_bootstrap_snippet()) is None
    assert asyncio.run(cx.verify_bootstrap()).status == "failed"
    assert asyncio.run(cx.health_check()).healthy is False
    with pytest.raises(PhoneAdapterError):
        asyncio.run(cx.provision_route({"id": "r", "direction": "inbound"}))
    # deleting a never-provisioned route must not explode
    assert asyncio.run(cx.deprovision_route({"id": "r"})) is None

    tw = load_adapter(_server(adapter_type="twilio"))
    assert tw.requires_bootstrap is False


def test_loader_unknown_type():
    with pytest.raises(PhoneAdapterError) as ei:
        load_adapter(_server(adapter_type="bogus"))
    assert ei.value.status_code == 400


def test_media_endpoint_resolution(monkeypatch):
    import config
    monkeypatch.setattr(config, "AUDIOSOCKET_PUBLIC_HOST", "10.0.0.5")
    # explicit per-server override wins
    a = load_adapter(_server(config={"audiosocket_endpoint": "explicit:1234"}))
    assert a.media_endpoint == "explicit:1234"
    # otherwise: auto-resolved host + default port (no admin IP entry)
    b = load_adapter(_server(config={}))
    assert b.media_endpoint == "10.0.0.5:9092"


def test_media_endpoint_warns_on_container_autodetect(monkeypatch, caplog):
    """An AUTODETECTED host inside a container is the container's own bridge
    IP — the PBX can't dial it, so the render must warn (the dialplan snippet
    would be silently broken otherwise)."""
    import logging
    import os.path

    import config
    monkeypatch.setattr(config, "AUDIOSOCKET_PUBLIC_HOST", "10.200.0.4")
    monkeypatch.setattr(config, "AUDIOSOCKET_PUBLIC_HOST_AUTODETECTED", True)
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path, "exists",
        lambda p: True if p == "/.dockerenv" else real_exists(p),
    )
    with caplog.at_level(logging.WARNING):
        load_adapter(_server(config={}))
    assert any("OTO_AUDIOSOCKET_PUBLIC_HOST" in r.message for r in caplog.records)

    # operator-set host (not autodetected) → no warning, even in a container
    caplog.clear()
    monkeypatch.setattr(config, "AUDIOSOCKET_PUBLIC_HOST_AUTODETECTED", False)
    with caplog.at_level(logging.WARNING):
        load_adapter(_server(config={}))
    assert not any("OTO_AUDIOSOCKET_PUBLIC_HOST" in r.message for r in caplog.records)


def test_media_endpoint_uses_admin_port(temp_db, monkeypatch):
    import config
    from storage import database as t
    monkeypatch.setattr(config, "AUDIOSOCKET_PUBLIC_HOST", "10.0.0.5")
    t.set_platform_setting("phone_audiosocket_port", "9095")
    assert load_adapter(_server(config={})).media_endpoint == "10.0.0.5:9095"
