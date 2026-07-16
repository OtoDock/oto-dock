"""HTTP tests for the chat-audio runtime endpoints (capability / TTS / STT
session mint / transcribe). Provider building is mocked — the Deepgram/Cartesia
SDKs aren't a test dependency; these cover the endpoint contracts + guards.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user
from services.media import audio_service
from storage import database as task_store


# --- fakes ----------------------------------------------------------------

class _Caps:
    supports_transcribe_file = True


class FakeTTS:
    """Fake streaming TTS: emits a little PCM per pushed sentence, paced through a
    queue so the producer (send_text_chunk) and consumer (receive_audio) interleave
    like the real provider."""

    def __init__(self):
        self.voice_id = ""
        self.voices = {}
        self._q: asyncio.Queue | None = None

    def select_voice(self, language):
        return self.voice_id

    async def connect(self):
        self._q = asyncio.Queue()

    def start_streaming_context(self, *, output_sample_rate=None, language=None):
        pass

    async def send_text_chunk(self, text, is_last=False):
        if text:
            await self._q.put(b"\x00\x01" * 80)  # 80 samples of PCM per sentence
        if is_last:
            await self._q.put(None)  # sentinel → receive ends

    async def receive_audio(self):
        while True:
            item = await self._q.get()
            if item is None:
                return
            yield item

    async def close(self):
        pass

    def billing_unit(self):
        return "char"

    def cost_per_unit(self):
        return 0.0


class FakeSTT:
    capabilities = _Caps()

    async def transcribe_file(self, data, *, language=None):
        from audio.providers.stt.base import TranscriptResult, Word
        return TranscriptResult(
            text="hello there", language=language or "en", audio_seconds=2.0,
            words=[Word("hello", 0.0, 0.4), Word("there", 0.4, 0.8)], provider_used="fake",
        )

    def billing_unit(self):
        return "second"

    def cost_per_unit(self):
        return 0.0


def _fake_build(provider_type, *, provider_id=None):
    inst = FakeTTS() if provider_type == "tts" else FakeSTT()
    return inst, {"id": provider_id or 1, "provider_name": "fake"}


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setattr(audio_service, "build_chat_provider", _fake_build)
    audio_service._tts_windows.clear()
    from api.audio import audio as audio_router

    app = FastAPI()
    app.include_router(audio_router.router)

    async def _user():
        return UserContext(sub="user-admin", email="u@test.com", name="U",
                           role="admin", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


def _set(key, value):
    task_store.set_platform_setting(key, value)


# --- capability -----------------------------------------------------------

def test_capability_endpoint(client):
    _set("audio_chat_user_policy", "native_preferred")
    r = client.get("/v1/audio/capability?has_native_tts=true&has_native_stt=false")
    assert r.status_code == 200
    body = r.json()
    assert body["icons_enabled"] is True
    assert body["tts"] == "native_only"


# --- TTS ------------------------------------------------------------------

def test_tts_synthesize_streams_pcm(client):
    r = client.post("/v1/audio/tts/synthesize", json={"text": "hello world. how are you?"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/L16")
    assert r.headers["x-audio-sample-rate"] == str(audio_service.CHAT_AUDIO_TARGET_RATE)
    assert len(r.content) > 0  # streamed raw PCM (2 sentences → 2 chunks)


def test_tts_char_cap(client):
    _set("audio_tts_max_chars_per_request", "10")
    r = client.post("/v1/audio/tts/synthesize", json={"text": "x" * 20})
    assert r.status_code == 413


def test_tts_rate_limit(client):
    _set("audio_tts_rate_limit_chars_per_min", "12")
    assert client.post("/v1/audio/tts/synthesize", json={"text": "x" * 8}).status_code == 200
    r = client.post("/v1/audio/tts/synthesize", json={"text": "x" * 8})
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}


def test_tts_synthesize_blocked_when_disabled(client):
    _set("audio_chat_enabled", "false")
    assert client.post("/v1/audio/tts/synthesize", json={"text": "hi"}).status_code == 403


def test_tts_synthesize_blocked_native_only(client):
    _set("audio_chat_user_policy", "native_only")
    assert client.post("/v1/audio/tts/synthesize", json={"text": "hi"}).status_code == 403


# --- STT session mint -----------------------------------------------------

def test_stt_session_mint(client):
    _set("audio_chat_user_policy", "user_choice")
    r = client.post("/v1/audio/stt/session", json={"provider_id": 5})
    assert r.status_code == 200
    body = r.json()
    assert "ws_token" in body and body["max_seconds"] == 180


def test_stt_session_blocked_when_disabled(client):
    _set("audio_chat_enabled", "false")
    assert client.post("/v1/audio/stt/session", json={"provider_id": 5}).status_code == 403


def test_stt_session_blocked_native_only(client):
    _set("audio_chat_user_policy", "native_only")
    r = client.post("/v1/audio/stt/session", json={"provider_id": 5})
    assert r.status_code == 403


# --- transcribe -----------------------------------------------------------

def test_transcribe(client):
    r = client.post(
        "/v1/audio/transcribe",
        files={"file": ("a.wav", b"RIFFfakeaudio-bytes", "audio/wav")},
        data={"language": "en"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "hello there"
    assert body["audio_seconds"] == 2  # int(round(2.0))
    assert len(body["words"]) == 2 and body["words"][0]["word"] == "hello"


def test_transcribe_rejects_empty_file(client):
    r = client.post("/v1/audio/transcribe", files={"file": ("a.wav", b"", "audio/wav")})
    assert r.status_code == 400
