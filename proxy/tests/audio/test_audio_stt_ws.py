"""WebSocket tests for the chat STT endpoint (``/ws/audio/stt``).

Covers the token-in-init handshake, binary→transcript flow, the cost cap
(1011 when cumulative audio exceeds the token's ``max_seconds``), and replay /
bad-token rejection. Provider building is mocked.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from services.media import audio_service
from services.media import ws_audio_token
from ws.audio import ws_audio_stt_handler


class WsFakeSTT:
    def __init__(self):
        self.started_with: dict = {}
        self.fatal: str | None = None

    async def start(self, language, sample_rate=None, interim_results=False, endpointing_ms=None):
        self.started_with = dict(
            language=language, sample_rate=sample_rate,
            interim_results=interim_results, endpointing_ms=endpointing_ms,
        )

    def pop_fatal_error(self):
        err, self.fatal = self.fatal, None
        return err

    async def send_audio(self, b):
        pass

    def drain_transcript(self):
        return None

    async def finish(self):
        return "hello world"

    async def close(self):
        pass

    def billing_unit(self):
        return "second"

    def cost_per_unit(self):
        return 0.0


@pytest.fixture
def client(temp_db, monkeypatch):
    fakes: list[WsFakeSTT] = []

    def _build(ptype, *, provider_id=None):
        fake = WsFakeSTT()
        fakes.append(fake)
        return fake, {"id": provider_id or 1, "provider_name": "fake",
                      "advanced": {"chat_endpointing_ms": 1234}}

    monkeypatch.setattr(audio_service, "build_chat_provider", _build)
    app = FastAPI()
    app.add_api_websocket_route("/ws/audio/stt", ws_audio_stt_handler)
    c = TestClient(app)
    c.fakes = fakes  # the providers built during the test, in order
    return c


def _token(sub="user-admin", *, max_seconds=60):
    return ws_audio_token.create_ws_audio_token(sub, max_seconds=max_seconds, provider_id=1)["ws_token"]


def test_handshake_and_final_transcript(client):
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x00" * 3200)
        ws.send_json({"type": "stop"})
        msg = ws.receive_json()
        assert msg["type"] == "final" and msg["text"] == "hello world"
    # Chat opens the provider with its own endpointing (advanced.chat_endpointing_ms),
    # 16 kHz, and interims — never the call endpointing.
    assert client.fakes[0].started_with == {
        "language": "en", "sample_rate": 16000,
        "interim_results": True, "endpointing_ms": 1234,
    }


def test_bad_token_rejected(client):
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": "garbage"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_replayed_token_rejected(client):
    tok = _token()
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": tok, "sample_rate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_json({"type": "stop"})
        ws.receive_json()
    # Same token again → jti already consumed → rejected.
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": tok, "sample_rate": 16000})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_cost_cap_closes(client):
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(max_seconds=0), "sample_rate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x00" * 3200)
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "max_seconds"


def test_provider_fatal_error_is_forwarded(client):
    """A provider that dies AFTER connecting (auth/quota error frame) must
    surface as an error frame — not a mic that silently hears nothing."""
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        client.fakes[0].fatal = "ElevenLabs speech-to-text auth error: bad key"
        ws.send_bytes(b"\x00" * 3200)
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "provider"
        assert "auth error" in msg["message"]


def test_provider_start_failure_reports_error(client, monkeypatch):
    """start() raising (Deepgram bad key fails at connect) sends an error
    frame before closing instead of dropping the socket silently."""
    class BoomSTT(WsFakeSTT):
        async def start(self, *a, **k):
            raise RuntimeError("Failed to start Deepgram connection")

    def _build(ptype, *, provider_id=None):
        return BoomSTT(), {"id": 1, "provider_name": "fake", "advanced": {}}

    monkeypatch.setattr(audio_service, "build_chat_provider", _build)
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "sample_rate": 16000})
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "provider"
        assert "could not start" in msg["message"]
