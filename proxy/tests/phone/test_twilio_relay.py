"""The public /v1/twilio/* relay — proxy → phone daemon, byte-fidelity.

A real aiohttp fake daemon serves the far side on a loopback port; the relay
app is exercised through TestClient (HTTP + WebSocket)."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from aiohttp import web
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from api.phone import twilio_relay


class FakeDaemon:
    """Loopback stand-in for the phone daemon's /v1/twilio surface, running
    in its own event-loop thread so the sync TestClient can't deadlock it."""

    def __init__(self):
        self.requests: list[dict] = []
        self.ws_headers: list[dict] = []
        self.ws_received: list[str] = []
        self.refuse_ws = False
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()

    async def _inbound(self, request: web.Request) -> web.Response:
        self.requests.append({
            "path_qs": request.path_qs,
            "body": await request.read(),
            "signature": request.headers.get("X-Twilio-Signature", ""),
            "content_type": request.headers.get("Content-Type", ""),
        })
        return web.Response(text="<Response><Reject/></Response>",
                            content_type="text/xml", status=200)

    async def _media(self, request: web.Request) -> web.StreamResponse:
        if self.refuse_ws:
            return web.json_response({"error": "bad signature"}, status=403)
        self.ws_headers.append(
            {"signature": request.headers.get("X-Twilio-Signature", "")})
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"event": "hello-from-daemon"}))
        async for msg in ws:
            self.ws_received.append(msg.data)
            if msg.data == "close-now":
                break
            await ws.send_str(f"echo:{msg.data}")
        await ws.close()
        return ws

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _start():
            app = web.Application()
            app.router.add_post("/v1/twilio/inbound/{sid}", self._inbound)
            app.router.add_post("/v1/twilio/status/{sid}", self._inbound)
            app.router.add_get("/v1/twilio/media/{sid}", self._media)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self._started.set()

        self._loop.run_until_complete(_start())
        self._loop.run_forever()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        assert self._started.wait(5)
        return self

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture
def daemon(monkeypatch):
    d = FakeDaemon().start()
    monkeypatch.setattr(config, "PHONE_SERVER_URL", f"http://127.0.0.1:{d.port}")
    yield d
    d.stop()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(twilio_relay.router)
    app.add_api_websocket_route(
        "/v1/twilio/media/{server_id}", twilio_relay.ws_twilio_media_relay)
    return TestClient(app)


def test_webhook_relay_preserves_bytes_headers_and_query(client, daemon):
    body = b"CallSid=CAx&From=%2B301234&To=%2B15551112222"
    resp = client.post(
        "/v1/twilio/status/7?u=abc-123",
        content=body,
        headers={"X-Twilio-Signature": "sig==",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/xml")
    assert resp.text == "<Response><Reject/></Response>"
    seen = daemon.requests[0]
    assert seen["path_qs"] == "/v1/twilio/status/7?u=abc-123"
    assert seen["body"] == body                      # raw bytes, no re-encode
    assert seen["signature"] == "sig=="
    assert seen["content_type"] == "application/x-www-form-urlencoded"


def test_webhook_relay_daemon_down_is_502(client, monkeypatch):
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://127.0.0.1:1")
    resp = client.post("/v1/twilio/inbound/7", content=b"x")
    assert resp.status_code == 502


def test_webhook_relay_rejects_non_numeric_server_id(client, daemon):
    resp = client.post("/v1/twilio/inbound/evil-path", content=b"x")
    assert resp.status_code == 404
    assert daemon.requests == []


def test_media_ws_bridges_frames_and_forwards_signature(client, daemon):
    with client.websocket_connect(
        "/v1/twilio/media/7", headers={"X-Twilio-Signature": "wsig=="},
    ) as ws:
        assert json.loads(ws.receive_text()) == {"event": "hello-from-daemon"}
        ws.send_text('{"event":"media"}')
        assert ws.receive_text() == 'echo:{"event":"media"}'
    assert daemon.ws_headers == [{"signature": "wsig=="}]
    assert daemon.ws_received[0] == '{"event":"media"}'


def test_media_ws_daemon_close_propagates(client, daemon):
    with client.websocket_connect("/v1/twilio/media/7") as ws:
        ws.receive_text()  # hello
        ws.send_text("close-now")
        # Daemon side closed → the relay must close Twilio's leg too.
        with pytest.raises(Exception):
            ws.receive_text()


def test_media_ws_daemon_refusal_closes_client(client, daemon):
    daemon.refuse_ws = True
    with client.websocket_connect("/v1/twilio/media/7") as ws:
        with pytest.raises(Exception):
            ws.receive_text()
    assert daemon.ws_headers == []
