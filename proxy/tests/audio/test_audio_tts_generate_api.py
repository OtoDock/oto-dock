"""HTTP tests for the voice-over generation endpoints (/v1/audio/tts/generate
+ the voices catalog/search/add trio). Provider building is mocked — these
cover the endpoint contracts, guards, override plumbing, and the WAV wrap.
"""

from __future__ import annotations

import io
import wave

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio.providers.tts.base import UnsupportedProviderOperation, VoiceInfo
from auth.providers import UserContext, get_current_user
from services.media import audio_service
from storage import database as task_store


class _Caps:
    def __init__(self, is_local=False):
        self.is_local = is_local


class FakeGenTTS:
    """Fake streaming TTS for the generate path — one PCM blob per sentence."""

    capabilities = _Caps()

    def __init__(self, *, voices=None, is_local=False, voice_infos=None):
        self.voice_id = ""
        self.voices = voices or {}
        self.capabilities = _Caps(is_local)
        self._voice_infos = voice_infos or []
        self.added = None
        self._pending = 0

    def select_voice(self, language):
        chosen = self.voices.get(language) or self.voices.get("en")
        if chosen:
            self.voice_id = chosen
        return self.voice_id

    async def connect(self):
        pass

    def start_streaming_context(self, *, output_sample_rate=None, language=None):
        self.rate = output_sample_rate
        self.language = language

    async def send_text_chunk(self, text, is_last=False):
        if text:
            self._pending += 1

    async def receive_audio(self):
        for _ in range(max(1, self._pending)):
            yield b"\x00\x01" * 160

    async def close(self):
        pass

    async def list_voices(self):
        return self._voice_infos

    async def search_voice_library(self, **kwargs):
        self.search_kwargs = kwargs
        return self._voice_infos

    async def add_library_voice(self, public_owner_id, voice_id, name=None):
        self.added = (public_owner_id, voice_id, name)
        return voice_id

    def billing_unit(self):
        return "char"

    def cost_per_unit(self):
        return 0.00005


class Harness:
    def __init__(self):
        self.provider = FakeGenTTS(voices={"en": "v-en", "el": "v-el"})
        self.row = {"id": 7, "provider_name": "fake-tts", "voices": {"en": "v-en"}}
        self.build_calls = []
        self.usage = []

    def build(self, *, provider_id=None, advanced_overrides=None):
        self.build_calls.append({"provider_id": provider_id, "overrides": advanced_overrides})
        return self.provider, self.row

    def record(self, user_sub, source_type, provider_name, provider, *, chars=0, seconds=0.0):
        self.usage.append({"source": source_type, "provider": provider_name,
                           "chars": chars, "seconds": seconds})


def _make_client(harness, role="admin"):
    from api.audio import audio as audio_router

    app = FastAPI()
    app.include_router(audio_router.router)

    async def _user():
        return UserContext(sub="user-1", email="u@test.com", name="U",
                           role=role, agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


@pytest.fixture
def harness(temp_db, monkeypatch):
    h = Harness()
    monkeypatch.setattr(audio_service, "build_generate_provider", h.build)
    monkeypatch.setattr(audio_service, "record_audio_usage", h.record)
    audio_service._tts_windows.clear()
    return h


@pytest.fixture
def client(harness):
    return _make_client(harness)


# --- /v1/audio/tts/generate -------------------------------------------------

def test_generate_returns_wav_with_metadata(client, harness):
    resp = client.post("/v1/audio/tts/generate", json={"text": "Hello world. Second one."})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    with wave.open(io.BytesIO(resp.content), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24000
        assert w.getnframes() > 0
    assert resp.headers["X-Provider-Used"] == "fake-tts"
    assert resp.headers["X-Voice-Used"] == "v-en"
    assert float(resp.headers["X-Audio-Seconds"]) > 0
    # Usage recorded with chars + seconds.
    assert harness.usage[0]["source"] == "audio-tts-generate"
    assert harness.usage[0]["chars"] == len("Hello world. Second one.")
    assert harness.usage[0]["seconds"] > 0


def test_generate_passes_overrides_and_voice(client, harness):
    resp = client.post("/v1/audio/tts/generate", json={
        "text": "Kalimera",
        "provider_id": 7,
        "voice_id": "custom-voice",
        "language": "el-GR",
        "model_id": "eleven_v3",
        "voice_settings": {"stability": 0.3, "speed": 1.05},
        "sample_rate": 44100,
    })
    assert resp.status_code == 200
    call = harness.build_calls[0]
    assert call["provider_id"] == 7
    assert call["overrides"] == {"model_id": "eleven_v3", "stability": 0.3, "speed": 1.05}
    assert resp.headers["X-Voice-Used"] == "custom-voice"
    assert harness.provider.rate == 44100
    assert harness.provider.language == "el"  # base_lang normalized


def test_generate_local_provider_ignores_voice_override(client, harness):
    harness.provider = FakeGenTTS(voices={"en": "v-local"}, is_local=True)
    resp = client.post("/v1/audio/tts/generate", json={"text": "Hi", "voice_id": "nope"})
    assert resp.status_code == 200
    assert resp.headers["X-Voice-Used"] == "v-local"


def test_generate_guards(client, harness):
    assert client.post("/v1/audio/tts/generate", json={"text": "  "}).status_code == 400
    assert client.post(
        "/v1/audio/tts/generate", json={"text": "hi", "sample_rate": 11025},
    ).status_code == 400
    task_store.set_platform_setting("audio_tts_max_chars_per_request", "5")
    assert client.post("/v1/audio/tts/generate", json={"text": "too long"}).status_code == 413


def test_generate_rate_limited(client, harness):
    task_store.set_platform_setting("audio_tts_rate_limit_chars_per_min", "10")
    assert client.post("/v1/audio/tts/generate", json={"text": "0123456789"}).status_code == 200
    resp = client.post("/v1/audio/tts/generate", json={"text": "x"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_generate_no_voice_resolvable_is_400(client, harness):
    harness.provider = FakeGenTTS(voices={})  # fresh cloud row: empty voice map
    resp = client.post("/v1/audio/tts/generate", json={"text": "hi"})
    assert resp.status_code == 400
    assert "voice_id" in resp.json()["detail"]


def test_generate_provider_resolution_errors(harness):
    client = _make_client(harness)

    def boom(**kwargs):
        raise audio_service._no_provider("tts", 99)

    harness_build = harness.build
    try:
        audio_service.build_generate_provider = boom
        assert client.post(
            "/v1/audio/tts/generate", json={"text": "hi", "provider_id": 99},
        ).status_code == 404

        def no_default(**kwargs):
            raise audio_service._no_provider("tts")

        audio_service.build_generate_provider = no_default
        assert client.post("/v1/audio/tts/generate", json={"text": "hi"}).status_code == 503
    finally:
        audio_service.build_generate_provider = harness_build


@pytest.mark.parametrize("builder, kind", [
    ("build_generate_provider", "tts"), ("build_chat_provider", "stt"), ("build_chat_provider", "tts"),
])
def test_a_missing_provider_says_where_to_pick_one(monkeypatch, builder, kind):
    from storage.prefs import audio_provider_store

    monkeypatch.setattr(audio_provider_store, "get_provider", lambda provider_id: None)
    monkeypatch.setattr(audio_provider_store, "get_default_provider", lambda *a, **k: None)
    args = (kind,) if builder == "build_chat_provider" else ()
    with pytest.raises(audio_service.AudioUnavailableError) as stale:
        getattr(audio_service, builder)(*args, provider_id=1)
    assert str(stale.value) == (
        f"No {kind.upper()} provider with id 1 is configured: select one in Admin, Audio.")
    with pytest.raises(audio_service.AudioUnavailableError) as none:
        getattr(audio_service, builder)(*args)
    assert str(none.value) == f"No {kind.upper()} provider configured: select one in Admin, Audio."


def test_generate_empty_audio_is_502(client, harness):
    class SilentTTS(FakeGenTTS):
        async def receive_audio(self):
            return
            yield  # pragma: no cover

    harness.provider = SilentTTS(voices={"en": "v-en"})
    resp = client.post("/v1/audio/tts/generate", json={"text": "hi"})
    assert resp.status_code == 502
    assert "no audio" in resp.json()["detail"]


# --- voices catalog / search / add -------------------------------------------

VOICES = [
    VoiceInfo(id="v1", name="George", languages=["en", "el"], category="premade",
              preview_url="https://x/p.mp3", description="male narrator"),
    VoiceInfo(id="v2", name="Nikos", languages=["el"], category="high_quality",
              owner_id="own1"),
]


def test_voices_catalog(client, harness):
    harness.provider._voice_infos = VOICES
    resp = client.get("/v1/audio/tts/voices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_name"] == "fake-tts"
    assert body["configured"] == {"en": "v-en"}
    assert [v["id"] for v in body["voices"]] == ["v1", "v2"]
    assert body["voices"][0]["languages"] == ["en", "el"]


def test_voices_search_passes_filters(client, harness):
    harness.provider._voice_infos = VOICES
    resp = client.get("/v1/audio/tts/voices/search", params={
        "search": "warm", "language": "el", "gender": "male", "page_size": 5,
    })
    assert resp.status_code == 200
    kwargs = harness.provider.search_kwargs
    assert kwargs["search"] == "warm"
    assert kwargs["language"] == "el"
    assert kwargs["gender"] == "male"
    assert kwargs["page_size"] == 5
    assert resp.json()["voices"][1]["owner_id"] == "own1"


def test_voices_search_unsupported_is_400(client, harness):
    class NoLibrary(FakeGenTTS):
        async def search_voice_library(self, **kwargs):
            raise UnsupportedProviderOperation("no library here")

    harness.provider = NoLibrary()
    resp = client.get("/v1/audio/tts/voices/search", params={"search": "x"})
    assert resp.status_code == 400
    assert "no library" in resp.json()["detail"]


def test_voices_add_requires_admin(harness):
    member = _make_client(harness, role="member")
    resp = member.post("/v1/audio/tts/voices/add", json={
        "public_owner_id": "own1", "voice_id": "v2",
    })
    assert resp.status_code == 403
    assert harness.provider.added is None


def test_voices_add_as_admin(client, harness):
    resp = client.post("/v1/audio/tts/voices/add", json={
        "public_owner_id": "own1", "voice_id": "v2", "name": "Nikos",
    })
    assert resp.status_code == 200
    assert resp.json()["voice_id"] == "v2"
    assert harness.provider.added == ("own1", "v2", "Nikos")


# --- service-layer: build_generate_provider ----------------------------------

def test_build_generate_provider_merges_overrides_for_cloud(temp_db, monkeypatch):
    row = {"id": 1, "provider_type": "tts", "provider_name": "cloudy",
           "credential_key": "k", "advanced": {"model_id": "base", "stability": 0.5}}
    built = {}

    class CloudCls:
        capabilities = _Caps(is_local=False)

    monkeypatch.setattr(audio_service.audio_provider_store, "get_provider", lambda pid: row)
    monkeypatch.setattr(audio_service.registry, "get_provider_class", lambda t, n: CloudCls)

    def fake_build(r, resolver):
        built["row"] = r
        return "instance"

    monkeypatch.setattr(audio_service.registry, "get_or_build_provider", fake_build)
    inst, out_row = audio_service.build_generate_provider(
        provider_id=1, advanced_overrides={"model_id": "override", "speed": 1.1},
    )
    assert inst == "instance"
    assert built["row"]["advanced"] == {"model_id": "override", "stability": 0.5, "speed": 1.1}
    # The original row object was NOT mutated (copy-on-override).
    assert row["advanced"] == {"model_id": "base", "stability": 0.5}


def test_build_generate_provider_ignores_overrides_for_local(temp_db, monkeypatch):
    row = {"id": 1, "provider_type": "tts", "provider_name": "localy",
           "credential_key": None, "advanced": {"model_id": "base"}}
    built = {}

    class LocalCls:
        capabilities = _Caps(is_local=True)

    monkeypatch.setattr(audio_service.audio_provider_store, "get_provider", lambda pid: row)
    monkeypatch.setattr(audio_service.registry, "get_provider_class", lambda t, n: LocalCls)
    monkeypatch.setattr(
        audio_service.registry, "get_or_build_provider",
        lambda r, resolver: built.setdefault("row", r) and "inst" or "inst",
    )
    audio_service.build_generate_provider(provider_id=1, advanced_overrides={"model_id": "x"})
    assert built["row"]["advanced"] == {"model_id": "base"}  # untouched


def test_build_generate_provider_default_falls_back_to_calls(temp_db, monkeypatch):
    table = {("tts", "calls"): {"id": 2, "provider_type": "tts", "provider_name": "cloudy",
                                "credential_key": None, "advanced": {}}}

    class CloudCls:
        capabilities = _Caps(is_local=False)

    monkeypatch.setattr(
        audio_service.audio_provider_store, "get_default_provider",
        lambda ptype, ctx: table.get((ptype, ctx)),
    )
    monkeypatch.setattr(audio_service.registry, "get_provider_class", lambda t, n: CloudCls)
    monkeypatch.setattr(audio_service.registry, "get_or_build_provider", lambda r, res: "inst")
    inst, row = audio_service.build_generate_provider()
    assert row["id"] == 2

    table.clear()
    with pytest.raises(audio_service.AudioUnavailableError):
        audio_service.build_generate_provider()
