"""Twilio signaling surface: signatures, webhook TwiML, tokens, status.

Handlers run on a real aiohttp test server wired exactly like production
(OutboundCallAPI app + auth middleware + attach_twilio), with the fake
Twilio side driving signed HTTP/WS requests."""

import json
import re

import pytest
import pytest_asyncio
from aiohttp import test_utils, web

import config
from calls.call_manager import CallManager, CallStatus
from calls.http_api import OutboundCallAPI
from calls.twilio_http import (
    TwilioCallAPI,
    build_stream_twiml,
    compute_signature,
    normalize_public_base_url,
    originate_twilio_call,
    validate_upgrade_signature,
)
from config_manager import ConfigManager

pytestmark = pytest.mark.asyncio

SERVER_ID = 7
AUTH_TOKEN = "twilio-auth-token-secret"
BASE = "https://phone.example.com"
DID = "+15551234567"


def make_cfg(**settings) -> ConfigManager:
    cfg = ConfigManager()
    cfg.load({
        "settings": {k: str(v) for k, v in settings.items()},
        "routes": [
            {
                "id": "r-in", "direction": "inbound", "agent": "unified",
                "llm_mode": "proxy", "enabled": True, "language": "en",
                "audiosocket_uuid": "uuid-in", "did": "1 (555) 123-4567",
                "phone_server_id": SERVER_ID,
            },
            {
                "id": "r-out", "direction": "outbound", "agent": "caller",
                "llm_mode": "proxy", "enabled": True, "language": "en",
                "ami_caller_id": "+15550009999",
                "phone_server_id": SERVER_ID,
            },
        ],
        "servers": {
            str(SERVER_ID): {
                "id": SERVER_ID, "adapter_type": "twilio",
                "account_sid": "ACxxxxxxxx", "auth_token": AUTH_TOKEN,
                "public_base_url": BASE,
            },
            "8": {"id": 8, "adapter_type": "asterisk_freepbx",
                  "ami_host": "pbx.lan", "ami_port": 5038,
                  "ami_username": "oto", "ami_secret": "s3"},
        },
    })
    return cfg


class Harness:
    def __init__(self, cfg):
        self.cfg = cfg
        self.call_manager = CallManager()
        self.run_calls: list[tuple] = []
        self.released: list = []
        self.active = 0
        self.api = OutboundCallAPI(self.call_manager, cfg)

        async def _run_call(conn, uuid, route, is_outbound, caller_info):
            self.run_calls.append((conn, uuid, route, is_outbound, caller_info))
            await conn.close()

        async def _release(call):
            self.released.append(call)

        self.twilio = TwilioCallAPI(
            cfg, self.call_manager,
            run_call=_run_call,
            active_count=lambda: self.active,
            release_warmup=_release,
        )
        self.api.attach_twilio(self.twilio)
        self.client: test_utils.TestClient | None = None

    async def start(self):
        self.client = test_utils.TestClient(test_utils.TestServer(self.api._app))
        await self.client.start_server()
        return self

    async def stop(self):
        await self.client.close()

    def sign(self, path_qs: str, form: dict, *, scheme: str = "https") -> str:
        return compute_signature(AUTH_TOKEN, f"{scheme}://phone.example.com{path_qs}", form)

    async def post_signed(self, path_qs: str, form: dict, signature: str | None = None):
        return await self.client.post(
            path_qs, data=form,
            headers={"X-Twilio-Signature": signature or self.sign(path_qs, form)},
        )


@pytest_asyncio.fixture
async def harness():
    h = await Harness(make_cfg()).start()
    yield h
    await h.stop()


def inbound_form(call_sid="CAtest0001") -> dict:
    return {"CallSid": call_sid, "AccountSid": "ACxxxxxxxx",
            "From": "+306912345678", "To": DID, "CallStatus": "ringing",
            "FromCountry": "GR"}


# ── URL + signature primitives ─────────────────────────────────────


async def test_normalize_public_base_url():
    assert normalize_public_base_url("https://x.example.com/") == "https://x.example.com"
    assert normalize_public_base_url(" https://x.example.com:8443 ") == "https://x.example.com:8443"
    assert normalize_public_base_url("http://x.example.com") == ""   # TLS required
    assert normalize_public_base_url("https://x.example.com/prefix") == ""  # no paths
    assert normalize_public_base_url("") == ""


async def test_compute_signature_known_vector():
    # The documented scheme (and the twilio SDK RequestValidator): URL +
    # NOTE: every value below (incl. the +1234... numbers) is Twilio's own
    # published example vector — not real data; changing any input breaks
    # the pinned signature.
    # alphabetically sorted name+value pairs, HMAC-SHA1, base64. Pinned so a
    # refactor can't silently change the payload assembly; end-to-end truth
    # against real Twilio traffic is a live-verification item.
    sig = compute_signature(
        "12345", "https://mycompany.com/myapp.php?foo=1&bar=2",
        {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030",
         "Digits": "1234", "From": "+12349013030", "To": "+18005551212"},
    )
    assert sig == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


async def test_upgrade_signature_accepts_all_documented_variants():
    cfg_entry = {"auth_token": AUTH_TOKEN, "public_base_url": BASE}

    class _Req:
        path_qs = f"/v1/twilio/media/{SERVER_ID}"
        def __init__(self, sig): self.headers = {"X-Twilio-Signature": sig}

    for url in (f"wss://phone.example.com/v1/twilio/media/{SERVER_ID}",
                f"wss://phone.example.com/v1/twilio/media/{SERVER_ID}/",
                f"https://phone.example.com/v1/twilio/media/{SERVER_ID}"):
        sig = compute_signature(AUTH_TOKEN, url, {})
        assert validate_upgrade_signature(_Req(sig), cfg_entry), url
    assert not validate_upgrade_signature(_Req("bogus"), cfg_entry)


# ── inbound webhook ────────────────────────────────────────────────


async def test_inbound_returns_stream_twiml_with_token(harness):
    resp = await harness.post_signed(f"/v1/twilio/inbound/{SERVER_ID}", inbound_form())
    assert resp.status == 200
    body = await resp.text()
    assert f'<Stream url="wss://phone.example.com/v1/twilio/media/{SERVER_ID}">' in body
    token = re.search(r'name="session" value="([^"]+)"', body).group(1)
    route, caller_info = harness.twilio._pending.pop(token)
    assert route.id == "r-in"  # DID matched through normalization
    assert caller_info["phone"] == "+306912345678"
    assert caller_info["source"] == "phone-twilio"
    assert caller_info["dial_event"]["callsid"] == "CAtest0001"


async def test_inbound_rejects_bad_signature_and_unknown_server(harness):
    resp = await harness.post_signed(
        f"/v1/twilio/inbound/{SERVER_ID}", inbound_form(), signature="wrong")
    assert resp.status == 403
    path = "/v1/twilio/inbound/99"
    resp = await harness.client.post(
        path, data=inbound_form(),
        headers={"X-Twilio-Signature": harness.sign(path, inbound_form())})
    assert resp.status == 403


async def test_inbound_unknown_did_and_capacity_reject_with_twiml(harness):
    form = {**inbound_form(), "To": "+19998887777"}
    resp = await harness.post_signed(f"/v1/twilio/inbound/{SERVER_ID}", form)
    assert resp.status == 200 and "<Reject/>" in await resp.text()

    harness.active = 999
    resp = await harness.post_signed(
        f"/v1/twilio/inbound/{SERVER_ID}", inbound_form("CAcap"))
    assert resp.status == 200 and "<Reject/>" in await resp.text()


async def test_inbound_replayed_callsid_rejected(harness):
    form = inbound_form("CAreplay")
    first = await harness.post_signed(f"/v1/twilio/inbound/{SERVER_ID}", form)
    assert "<Connect>" in await first.text()
    second = await harness.post_signed(f"/v1/twilio/inbound/{SERVER_ID}", form)
    assert "<Reject/>" in await second.text()


async def test_bearer_middleware_bypass_is_twilio_scoped(harness):
    # /v1/twilio/* skips the bearer check (signature auth instead)…
    resp = await harness.client.post(f"/v1/twilio/inbound/{SERVER_ID}",
                                     data=inbound_form())
    assert resp.status == 403  # signature missing, NOT bearer 401
    # …while the guarded surfaces still demand their bearer.
    resp = await harness.client.post("/v1/calls/register", json={})
    assert resp.status == 401


# ── media websocket ────────────────────────────────────────────────


def _start_msg(token: str) -> dict:
    return {"event": "start", "streamSid": "MZstream", "start": {
        "streamSid": "MZstream", "callSid": "CAmedia",
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000,
                        "channels": 1},
        "customParameters": {"session": token},
    }}


async def _open_media_ws(harness, token: str):
    path = f"/v1/twilio/media/{SERVER_ID}"
    sig = compute_signature(AUTH_TOKEN, f"wss://phone.example.com{path}", {})
    ws = await harness.client.ws_connect(path, headers={"X-Twilio-Signature": sig})
    await ws.send_str(json.dumps({"event": "connected"}))
    await ws.send_str(json.dumps(_start_msg(token)))
    return ws


async def test_media_ws_runs_inbound_call(harness):
    resp = await harness.post_signed(f"/v1/twilio/inbound/{SERVER_ID}", inbound_form())
    token = re.search(r'name="session" value="([^"]+)"', await resp.text()).group(1)
    ws = await _open_media_ws(harness, token)
    await ws.receive()  # server closes once the stub run_call returns
    assert len(harness.run_calls) == 1
    conn, uuid, route, is_outbound, caller_info = harness.run_calls[0]
    assert uuid == token and route.id == "r-in" and not is_outbound
    assert conn.call_uuid == "CAmedia"
    assert caller_info["did"] == DID
    # Single-use: the same token cannot resolve twice.
    ws2 = await _open_media_ws(harness, token)
    await ws2.receive()
    assert len(harness.run_calls) == 1


async def test_media_ws_refuses_bad_signature_and_bad_token(harness):
    resp = await harness.client.get(
        f"/v1/twilio/media/{SERVER_ID}",
        headers={"X-Twilio-Signature": "nope",
                 "Upgrade": "websocket", "Connection": "Upgrade"})
    assert resp.status == 403
    ws = await _open_media_ws(harness, "no-such-token")
    msg = await ws.receive()
    assert msg.type.name in ("CLOSE", "CLOSED", "CLOSING")
    assert harness.run_calls == []


async def test_media_ws_outbound_claims_uuid_once(harness):
    call = harness.call_manager.register_call(
        phone_number="+306900000000", task_description="t")
    call.route_id = "r-out"
    harness.call_manager.update_status(call.call_id, CallStatus.DIALING)

    ws = await _open_media_ws(harness, call.audio_uuid)
    await ws.receive()
    assert len(harness.run_calls) == 1
    _, uuid, route, is_outbound, _ = harness.run_calls[0]
    assert uuid == call.audio_uuid and route.id == "r-out" and is_outbound

    # Second WS for the same uuid: claimed → refused (status also moved on).
    ws2 = await _open_media_ws(harness, call.audio_uuid)
    await ws2.receive()
    assert len(harness.run_calls) == 1


async def test_media_ws_refuses_non_dialing_outbound(harness):
    call = harness.call_manager.register_call(
        phone_number="+306900000000", task_description="t")
    call.route_id = "r-out"
    harness.call_manager.update_status(
        call.call_id, CallStatus.FAILED, error="no answer (dial timeout)")
    ws = await _open_media_ws(harness, call.audio_uuid)
    await ws.receive()
    assert harness.run_calls == []


async def test_media_ws_rejects_wrong_media_format(harness):
    resp = await harness.post_signed(f"/v1/twilio/inbound/{SERVER_ID}", inbound_form())
    token = re.search(r'name="session" value="([^"]+)"', await resp.text()).group(1)
    path = f"/v1/twilio/media/{SERVER_ID}"
    sig = compute_signature(AUTH_TOKEN, f"wss://phone.example.com{path}", {})
    ws = await harness.client.ws_connect(path, headers={"X-Twilio-Signature": sig})
    msg = _start_msg(token)
    msg["start"]["mediaFormat"] = {"encoding": "audio/l16", "sampleRate": 16000}
    await ws.send_str(json.dumps({"event": "connected"}))
    await ws.send_str(json.dumps(msg))
    end = await ws.receive()
    assert end.type.name in ("CLOSE", "CLOSED", "CLOSING")
    assert harness.run_calls == []


# ── status callbacks ───────────────────────────────────────────────


async def _post_status(harness, uuid: str, call_status: str):
    path_qs = f"/v1/twilio/status/{SERVER_ID}?u={uuid}"
    form = {"CallSid": "CAout", "CallStatus": call_status}
    return await harness.post_signed(path_qs, form)


async def test_status_terminal_fails_dialing_call_and_releases_warmup(harness):
    call = harness.call_manager.register_call(
        phone_number="+306900000000", task_description="t")
    harness.call_manager.update_status(call.call_id, CallStatus.DIALING)
    resp = await _post_status(harness, call.audio_uuid, "no-answer")
    assert resp.status == 200
    assert call.status == CallStatus.FAILED and call.error == "no-answer"
    assert harness.released == [call]


async def test_status_completed_while_dialing_is_terminal(harness):
    call = harness.call_manager.register_call(
        phone_number="+306900000000", task_description="t")
    harness.call_manager.update_status(call.call_id, CallStatus.DIALING)
    await _post_status(harness, call.audio_uuid, "completed")
    assert call.status == CallStatus.FAILED
    assert "before media" in call.error


async def test_status_never_touches_connected_calls(harness):
    call = harness.call_manager.register_call(
        phone_number="+306900000000", task_description="t")
    harness.call_manager.update_status(call.call_id, CallStatus.CONNECTED)
    await _post_status(harness, call.audio_uuid, "no-answer")
    assert call.status == CallStatus.CONNECTED
    assert harness.released == []


async def test_status_requires_signature_over_query_string(harness):
    call = harness.call_manager.register_call(
        phone_number="+306900000000", task_description="t")
    harness.call_manager.update_status(call.call_id, CallStatus.DIALING)
    # Signature computed WITHOUT the query string must fail — Twilio signs
    # the full URL including ?u=….
    path_qs = f"/v1/twilio/status/{SERVER_ID}?u={call.audio_uuid}"
    form = {"CallSid": "CAout", "CallStatus": "no-answer"}
    bad = compute_signature(
        AUTH_TOKEN, f"https://phone.example.com/v1/twilio/status/{SERVER_ID}", form)
    resp = await harness.post_signed(path_qs, form, signature=bad)
    assert resp.status == 403
    assert call.status == CallStatus.DIALING


# ── outbound origination (REST) ────────────────────────────────────


async def test_originate_twilio_call_posts_documented_form():
    seen = {}

    async def calls_endpoint(request):
        seen["auth"] = request.headers.get("Authorization", "")
        seen["form"] = list((await request.post()).items())
        return web.json_response({"sid": "CAnew", "status": "queued"})

    app = web.Application()
    app.router.add_post(
        "/2010-04-01/Accounts/ACxxxxxxxx/Calls.json", calls_endpoint)
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        from telephony import twilio_rest
        cfg = make_cfg()
        route = cfg.get_outbound_route("r-out")
        call = CallManager().register_call(
            phone_number="+306911111111", task_description="book a table")
        server_cfg = dict(cfg.twilio_server(SERVER_ID))

        base_url = f"http://127.0.0.1:{client.port}"
        orig = twilio_rest.TwilioRestClient.BASE_URL
        twilio_rest.TwilioRestClient.BASE_URL = base_url
        try:
            await originate_twilio_call(cfg, route, call, server_cfg)
        finally:
            twilio_rest.TwilioRestClient.BASE_URL = orig

        form = dict(seen["form"])
        assert form["To"] == "+306911111111"
        assert form["From"] == "+15550009999"
        assert form["Timeout"] == "45"
        assert form["StatusCallbackMethod"] == "POST"
        assert (form["StatusCallback"]
                == f"{BASE}/v1/twilio/status/{SERVER_ID}?u={call.audio_uuid}")
        events = [v for k, v in seen["form"] if k == "StatusCallbackEvent"]
        assert events == ["initiated", "answered", "completed"]
        assert form["Twiml"] == build_stream_twiml(BASE, SERVER_ID, call.audio_uuid)
        assert seen["auth"].startswith("Basic ")
    finally:
        await client.close()


async def test_originate_twilio_call_maps_vendor_rejection():
    async def calls_endpoint(request):
        return web.json_response({"code": 21211, "message": "invalid To"},
                                 status=400)

    app = web.Application()
    app.router.add_post(
        "/2010-04-01/Accounts/ACxxxxxxxx/Calls.json", calls_endpoint)
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        from telephony import twilio_rest
        cfg = make_cfg()
        route = cfg.get_outbound_route("r-out")
        call = CallManager().register_call(
            phone_number="not-a-number", task_description="t")
        orig = twilio_rest.TwilioRestClient.BASE_URL
        twilio_rest.TwilioRestClient.BASE_URL = f"http://127.0.0.1:{client.port}"
        try:
            with pytest.raises(twilio_rest.TwilioRestError) as exc:
                await originate_twilio_call(
                    cfg, route, call, dict(cfg.twilio_server(SERVER_ID)))
        finally:
            twilio_rest.TwilioRestClient.BASE_URL = orig
        assert exc.value.status == 400
    finally:
        await client.close()


# ── dispatch: _create_call picks REST vs AMI by server type ────────


async def test_create_call_dispatches_twilio_routes_to_rest(harness, monkeypatch):
    calls = []

    async def fake_originate(cfg, route, call, server_cfg):
        calls.append((route.id, call.phone_number, server_cfg["account_sid"]))

    monkeypatch.setattr(
        "calls.twilio_http.originate_twilio_call", fake_originate)
    monkeypatch.setattr(config, "PHONE_API_SECRET", "s3cret")

    # The pre-warm path would dial the (possibly live) proxy — stub it out.
    async def _no_warmup(call_id, route):
        harness.call_manager.get_call(call_id)._opening_ready.set()

    monkeypatch.setattr(harness.api, "_warmup_caller_session", _no_warmup)

    resp = await harness.client.post(
        "/api/calls",
        json={"phone_number": "+306912223344", "task_description": "test"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert resp.status == 202
    assert calls == [("r-out", "+306912223344", "ACxxxxxxxx")]
    body = await resp.json()
    assert body["status"] == "dialing"


async def test_per_server_ami_resolution():
    cfg = make_cfg(ami_host="default.lan", ami_port="5038", ami_username="flat")
    # Server 8 (asterisk row in the servers map) resolves per-server coords…
    assert cfg.server_ami(8) == {"host": "pbx.lan", "port": 5038,
                                 "username": "oto", "secret": "s3"}
    # …a Twilio row or unknown id falls back to the flat settings (None).
    assert cfg.server_ami(SERVER_ID) is None
    assert cfg.server_ami(None) is None
