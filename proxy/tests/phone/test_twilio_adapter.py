"""TwilioAdapter — REST control plane against httpx.MockTransport.

No network, no account: every vendor call is served by a scripted handler.
Number fixtures deliberately OMIT the real API's ``capabilities`` object —
its literal key would trip the naming guard.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from services.phone.phone_adapters import PhoneAdapterError
from services.phone.phone_adapters import twilio as twilio_mod
from services.phone.phone_adapters.twilio import (
    TwilioAdapter,
    normalize_did,
    normalize_public_base_url,
)

SID = "ACtest000000000000000000000000000"
TOKEN = "auth-token-secret"
BASE = "https://phone.example.com"
WEBHOOK = f"{BASE}/v1/twilio/inbound/7"


@pytest.fixture(autouse=True)
def _dashboard_public_url(monkeypatch):
    """The phone entrance IS the dashboard public URL (no per-server
    override) — pin it so tests never depend on the host env."""
    import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", BASE)


def make_adapter(*, config=None, token=TOKEN) -> TwilioAdapter:
    row = {"id": 7, "name": "tw", "host": "", "adapter_type": "twilio",
           "config": {"account_sid": SID, **(config or {})}}
    return TwilioAdapter(
        row,
        credential_resolver=lambda suffix: (
            {"AUTH_TOKEN": token} if suffix == "twilio-auth-token" else {}),
        media_endpoint="10.0.0.5:9092",
        register_endpoint="10.0.0.5:9093/v1/calls/register",
    )


# Captured at import so a test that mocks twice doesn't chain factories.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(twilio_mod.httpx, "AsyncClient", factory)


def _number(sid="PN1", phone="+15551112222", voice_url="", app_sid=None):
    return {"sid": sid, "phone_number": phone, "voice_url": voice_url,
            "voice_application_sid": app_sid}


# ── helpers ────────────────────────────────────────────────────────


def test_normalize_helpers():
    assert normalize_did("+1 (555) 111-2222") == "15551112222"
    assert normalize_did("") == ""
    assert normalize_public_base_url("https://x.io/") == "https://x.io"
    assert normalize_public_base_url("https://x.io/path") == ""
    assert normalize_public_base_url("http://x.io") == ""


def test_repr_never_leaks_the_token():
    assert TOKEN not in repr(make_adapter())


# ── health + verify (the cascade gate) ─────────────────────────────


def test_health_ok(monkeypatch):
    def handler(request):
        assert request.url.path == f"/2010-04-01/Accounts/{SID}.json"
        assert request.headers.get("Authorization", "").startswith("Basic ")
        return httpx.Response(200, json={
            "friendly_name": "My Project", "status": "active"})

    _mock(monkeypatch, handler)
    status = asyncio.run(make_adapter().health_check())
    assert status.healthy and "My Project" in status.detail


def test_health_fails_on_http_dashboard_url(monkeypatch):
    import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "http://192.168.1.10:8400")
    _mock(monkeypatch, lambda request: pytest.fail("no request expected"))
    status = asyncio.run(make_adapter().health_check())
    assert not status.healthy and "dashboard public URL" in status.detail


def test_health_fails_on_bad_credentials(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(401, json={}))
    status = asyncio.run(make_adapter().health_check())
    assert not status.healthy and "credentials" in status.detail


def test_health_flags_suspended_account(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(
        200, json={"friendly_name": "X", "status": "suspended"}))
    status = asyncio.run(make_adapter().health_check())
    assert not status.healthy and "suspended" in status.detail


def test_verify_bootstrap_is_the_credential_and_url_gate(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(
        200, json={"friendly_name": "X", "status": "active"}))
    assert asyncio.run(make_adapter().verify_bootstrap()).status == "verified"
    # No usable public URL → failed BEFORE any vendor call.
    import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    _mock(monkeypatch, lambda request: pytest.fail("no request expected"))
    assert asyncio.run(make_adapter().verify_bootstrap()).status == "failed"


def test_public_base_is_the_dashboard_url(monkeypatch):
    """The phone entrance is ALWAYS the dashboard public URL (`/v1/twilio/*`
    relay) — https only; an http:// (LAN-only) dashboard URL yields nothing
    rather than webhooks that could never validate."""
    import config
    from services.phone.phone_adapters.twilio import resolve_twilio_public_base

    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://oto.example.com/")
    assert resolve_twilio_public_base() == "https://oto.example.com"
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "http://192.168.1.10:8400")
    assert resolve_twilio_public_base() == ""
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    assert resolve_twilio_public_base() == ""


def test_missing_credentials_are_a_400(monkeypatch):
    _mock(monkeypatch, lambda request: pytest.fail("no request expected"))
    a = make_adapter(token="")
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(a._request("GET", ".json"))
    assert ei.value.status_code == 400


# ── inbound provisioning ───────────────────────────────────────────


def test_provision_inbound_sets_voice_url(monkeypatch):
    posted = {}

    def handler(request):
        if request.method == "GET":
            assert request.url.params["PhoneNumber"] == "1 (555) 111-2222"
            return httpx.Response(200, json={"incoming_phone_numbers": [
                _number(voice_url="https://old.example.com/answer")]})
        posted["path"] = request.url.path
        posted["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json=_number(voice_url=WEBHOOK))

    _mock(monkeypatch, handler)
    handle = asyncio.run(make_adapter().provision_route({
        "id": "r1", "direction": "inbound", "did": "1 (555) 111-2222"}))
    assert posted["path"].endswith("/IncomingPhoneNumbers/PN1.json")
    assert posted["form"] == {"VoiceUrl": WEBHOOK, "VoiceMethod": "POST"}
    assert handle.adapter_data == {
        "number_sid": "PN1",
        "phone_number": "+15551112222",   # Twilio's canonical E.164
        "previous_voice_url": "https://old.example.com/answer",
    }
    assert handle.did == "1 (555) 111-2222"


def test_provision_inbound_number_not_owned_is_400(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(
        200, json={"incoming_phone_numbers": []}))
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(make_adapter().provision_route(
            {"id": "r1", "direction": "inbound", "did": "+19998887777"}))
    assert ei.value.status_code == 400
    assert "owns no phone number" in ei.value.message


def test_provision_inbound_refuses_twiml_app_numbers(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(
        200, json={"incoming_phone_numbers": [_number(app_sid="APxxx")]}))
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(make_adapter().provision_route(
            {"id": "r1", "direction": "inbound", "did": "+15551112222"}))
    assert ei.value.status_code == 400
    assert "TwiML" in ei.value.message


def test_provision_inbound_requires_did_and_public_url(monkeypatch):
    _mock(monkeypatch, lambda request: pytest.fail("no request expected"))
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(make_adapter().provision_route(
            {"id": "r1", "direction": "inbound", "did": ""}))
    assert ei.value.status_code == 400
    import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")  # no entrance
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(make_adapter().provision_route(
            {"id": "r1", "direction": "inbound", "did": "+15551112222"}))
    assert ei.value.status_code == 400


# ── outbound provisioning ──────────────────────────────────────────


def test_provision_outbound_needs_caller_id(monkeypatch):
    _mock(monkeypatch, lambda request: pytest.fail("no request expected"))
    with pytest.raises(PhoneAdapterError):
        asyncio.run(make_adapter().provision_route(
            {"id": "r2", "direction": "outbound", "ami_caller_id": ""}))
    handle = asyncio.run(make_adapter().provision_route(
        {"id": "r2", "direction": "outbound", "ami_caller_id": "+15550009999"}))
    assert "+15550009999" in handle.instructions


# ── deprovision (only-if-ours) ─────────────────────────────────────


def _deprovision_route(voice_url):
    return {"id": "r1", "direction": "inbound", "did": "+15551112222",
            "adapter_data": {"number_sid": "PN1", "phone_number": "+15551112222",
                             "previous_voice_url": voice_url}}


def test_deprovision_clears_only_our_exact_webhook(monkeypatch):
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_number(voice_url=WEBHOOK))
        posted.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json=_number())

    _mock(monkeypatch, handler)
    asyncio.run(make_adapter().deprovision_route(_deprovision_route(WEBHOOK)))
    assert posted == [{"VoiceUrl": "", "VoiceMethod": "POST"}]


def test_deprovision_leaves_repointed_numbers_alone(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_number(
                voice_url="https://phone.example.com/v1/twilio/inbound/9"))
        pytest.fail("must not rewrite a number owned by another server")

    _mock(monkeypatch, handler)
    asyncio.run(make_adapter().deprovision_route(_deprovision_route(WEBHOOK)))


def test_deprovision_tolerates_released_numbers(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(404, json={}))
    asyncio.run(make_adapter().deprovision_route(_deprovision_route(WEBHOOK)))


# ── drift enumeration ──────────────────────────────────────────────


def test_list_provisioned_routes_pages_and_filters(monkeypatch):
    def handler(request):
        page = request.url.params.get("Page", "")
        if page == "1":
            return httpx.Response(200, json={
                "incoming_phone_numbers": [
                    _number(sid="PN3", phone="+15553334444", voice_url=WEBHOOK)],
                "next_page_uri": None,
            })
        return httpx.Response(200, json={
            "incoming_phone_numbers": [
                _number(sid="PN1", phone="+15551112222", voice_url=WEBHOOK),
                _number(sid="PN2", phone="+15552223333",
                        voice_url="https://elsewhere.example.com/answer"),
            ],
            "next_page_uri":
                f"/2010-04-01/Accounts/{SID}/IncomingPhoneNumbers.json?Page=1",
        })

    _mock(monkeypatch, handler)
    handles = asyncio.run(make_adapter().list_provisioned_routes())
    assert [(h.adapter_data["number_sid"], h.did) for h in handles] == [
        ("PN1", "+15551112222"), ("PN3", "+15553334444")]


# ── error envelope ─────────────────────────────────────────────────


def test_vendor_errors_map_onto_the_adapter_envelope(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(
        500, json={"message": "kaboom"}))
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(make_adapter()._request("GET", ".json"))
    assert ei.value.status_code == 502 and ei.value.vendor_status == 500

    def timeout_handler(request):
        raise httpx.ConnectTimeout("slow")

    _mock(monkeypatch, timeout_handler)
    with pytest.raises(PhoneAdapterError) as ei:
        asyncio.run(make_adapter()._request("GET", ".json"))
    assert ei.value.status_code == 504
