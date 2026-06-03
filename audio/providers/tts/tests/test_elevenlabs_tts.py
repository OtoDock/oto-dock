"""ElevenLabsTTS tests: offline contract surface + wire framing against an
in-process fake multi-context WebSocket server (no network, no key)."""

import asyncio
import base64
import json

import httpx
import pytest
import websockets

import audio.providers.tts.elevenlabs as el_mod
from audio.providers.tts.base import UnsupportedProviderOperation
from audio.providers.tts.elevenlabs import (
    ElevenLabsTTS,
    _accepts_language_code,
    _is_ws_model,
    _to_elevenlabs_lang,
)

AUDIO_CHUNKS = [b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"]


# ── Offline contract surface ───────────────────────────────────────


def test_capabilities_and_billing():
    assert ElevenLabsTTS.capabilities.supports_streaming
    assert not ElevenLabsTTS.capabilities.is_local
    assert ElevenLabsTTS.is_stub is False
    assert ElevenLabsTTS.billing_unit() == "char"
    assert ElevenLabsTTS.cost_per_unit() > 0


def test_from_row_loads_voices_and_advanced():
    tts = ElevenLabsTTS.from_row(
        {"provider_type": "tts", "provider_name": "elevenlabs",
         "credential_key": "audio-elevenlabs",
         "voices": {"en": "v-en", "el": "v-el"},
         "advanced": {"model_id": "eleven_multilingual_v2", "stability": 0.4}},
        lambda key: "secret" if key == "audio-elevenlabs" else "",
    )
    assert tts.voices == {"en": "v-en", "el": "v-el"}
    assert tts._model_id == "eleven_multilingual_v2"
    assert tts._voice_settings == {"stability": 0.4}


def test_validate_advanced():
    ok = ElevenLabsTTS.validate_advanced(
        {"model_id": "eleven_flash_v2_5", "stability": 0.5, "speed": 1.0,
         "chunk_length_schedule": [50, 120]},
    )
    assert ok == {}
    bad = ElevenLabsTTS.validate_advanced(
        {"model_id": "sonic-3.5", "stability": 2.0, "speed": "fast",
         "chunk_length_schedule": [10]},
    )
    assert set(bad) == {"model_id", "stability", "speed", "chunk_length_schedule"}


def test_select_voice_falls_back_to_premade_default():
    # Empty voices map -> the shipped PREMADE voice (account-independent)
    # keeps every language speaking; the models are multilingual.
    tts = ElevenLabsTTS(api_key="x")
    assert tts.select_voice("de") == "XrExE9yKIg1WjnnlVkGX"
    assert tts.select_voice("el") == "XrExE9yKIg1WjnnlVkGX"


def test_repr_redacts_api_key():
    tts = ElevenLabsTTS(api_key="super-secret-token", voice_id="v-en")
    assert "super-secret-token" not in repr(tts)


def test_lang_and_model_helpers():
    assert _to_elevenlabs_lang("el-GR") == "el"
    assert _to_elevenlabs_lang("de") == "de"
    assert _to_elevenlabs_lang("multi") is None
    assert _to_elevenlabs_lang(None) is None
    assert _is_ws_model("eleven_flash_v2_5")
    assert _is_ws_model("eleven_multilingual_v2")
    assert not _is_ws_model("eleven_v3")
    assert _accepts_language_code("eleven_flash_v2_5")
    assert _accepts_language_code("eleven_turbo_v2_5")
    assert not _accepts_language_code("eleven_multilingual_v2")
    assert not _accepts_language_code("eleven_v3")


def test_streaming_context_rejects_unknown_rate():
    tts = ElevenLabsTTS(api_key="x", voice_id="v")
    with pytest.raises(ValueError):
        tts.start_streaming_context(output_sample_rate=11025)


async def test_default_list_voices_dedupes_configured_map():
    tts = ElevenLabsTTS(api_key="x", voices={"en": "v1", "de": "v1", "el": "v2"})
    # ElevenLabs overrides list_voices with a live call; the base fallback is
    # exercised via Cartesia-style providers — here we only check the library
    # surface raises cleanly on a provider without one.
    from audio.providers.tts.cartesia import CartesiaTTS

    cart = CartesiaTTS(api_key="x", voices={"en": "v1", "de": "v1", "el": "v2"})
    infos = await cart.list_voices()
    assert {i.id for i in infos} == {"v1", "v2"}
    with pytest.raises(UnsupportedProviderOperation):
        await cart.search_voice_library(search="warm")
    with pytest.raises(UnsupportedProviderOperation):
        await cart.add_library_voice("owner", "vid")


# ── Fake multi-context WS server ───────────────────────────────────


class FakeElevenWS:
    """Scripted stand-in for the multi-stream-input endpoint. Records every
    client frame + connection path; answers a flush with audio + isFinal."""

    def __init__(self):
        self.paths: list[str] = []
        self.frames: list[list[dict]] = []      # per-connection client frames
        self.audio_chunks = list(AUDIO_CHUNKS)
        self.prefix_wrong_context = False        # send a stale-context frame first
        self.drop_is_final = False               # simulate a lost final marker
        self.close_next_connection = False       # kill the socket on first frame
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, ws):
        self.paths.append(ws.request.path)
        conn: list[dict] = []
        self.frames.append(conn)
        async for raw in ws:
            msg = json.loads(raw)
            conn.append(msg)
            if self.close_next_connection:
                self.close_next_connection = False
                await ws.close()
                return
            if msg.get("flush") and msg.get("context_id"):
                cid = msg["context_id"]
                if self.prefix_wrong_context:
                    await ws.send(json.dumps(
                        {"audio": base64.b64encode(b"BAD!").decode(), "contextId": "someone-else"}
                    ))
                for chunk in self.audio_chunks:
                    await ws.send(json.dumps(
                        {"audio": base64.b64encode(chunk).decode(), "contextId": cid}
                    ))
                if not self.drop_is_final:
                    await ws.send(json.dumps({"isFinal": True, "contextId": cid}))


@pytest.fixture
async def fake_ws(monkeypatch):
    fake = await FakeElevenWS().start()
    monkeypatch.setattr(el_mod, "_WS_BASE", f"ws://127.0.0.1:{fake.port}")
    yield fake
    await fake.stop()


def _provider(**advanced) -> ElevenLabsTTS:
    return ElevenLabsTTS(api_key="k", voice_id="voice-1", advanced=advanced or None)


async def _drive_utterance(tts, text="Hello there", rate=8000, language=None):
    tts.start_streaming_context(output_sample_rate=rate, language=language)
    await tts.send_text_chunk(text)
    await tts.send_text_chunk("", is_last=True)
    chunks = [c async for c in tts.receive_audio()]
    return chunks


async def test_ws_framing_init_text_flush(fake_ws):
    tts = _provider()
    await tts.connect()
    chunks = await _drive_utterance(tts, "Hello there")
    await tts.close()

    assert chunks == AUDIO_CHUNKS
    frames = fake_ws.frames[0]
    # Frame 1: InitialiseContext — single-space text + config, sent lazily.
    assert frames[0]["text"] == " "
    assert "context_id" in frames[0]
    assert frames[0]["generation_config"]["chunk_length_schedule"]
    # Frame 2: the text chunk, trailing-space enforced.
    assert frames[1]["text"] == "Hello there "
    assert frames[1]["context_id"] == frames[0]["context_id"]
    # Frame 3: the flush.
    assert frames[2] == {"context_id": frames[0]["context_id"], "flush": True}
    # Connection URL carried model + pcm format + max inactivity.
    assert "output_format=pcm_8000" in fake_ws.paths[0]
    assert "inactivity_timeout=180" in fake_ws.paths[0]
    assert "model_id=eleven_flash_v2_5" in fake_ws.paths[0]


async def test_ws_language_code_only_for_flash_family(fake_ws):
    tts = _provider()  # flash default
    await tts.connect()
    await _drive_utterance(tts, "Geia sou", language="el-GR")
    await tts.close()
    assert "language_code=el" in fake_ws.paths[-1]

    tts2 = _provider(model_id="eleven_multilingual_v2")
    await tts2.connect()
    await _drive_utterance(tts2, "Geia sou", language="el-GR")
    await tts2.close()
    assert "language_code" not in fake_ws.paths[-1]


async def test_stale_context_frames_filtered(fake_ws):
    fake_ws.prefix_wrong_context = True
    tts = _provider()
    await tts.connect()
    chunks = await _drive_utterance(tts)
    await tts.close()
    assert b"BAD!" not in b"".join(chunks)
    assert chunks == AUDIO_CHUNKS


async def test_rate_rebind_reconnects(fake_ws):
    tts = _provider()
    await tts.connect()                      # binds pcm_8000
    await _drive_utterance(tts, rate=24000)  # needs pcm_24000 → reconnect
    await tts.close()
    assert len(fake_ws.paths) == 2
    assert "output_format=pcm_8000" in fake_ws.paths[0]
    assert "output_format=pcm_24000" in fake_ws.paths[1]


async def test_voice_rebind_reconnects(fake_ws):
    tts = _provider()
    await tts.connect()
    await _drive_utterance(tts)
    tts.voice_id = "voice-2"                 # chat WS mutates voice_id directly
    await _drive_utterance(tts)
    await tts.close()
    assert "/voice-1/" in fake_ws.paths[0]
    assert "/voice-2/" in fake_ws.paths[-1]


async def test_reconnect_after_socket_death(fake_ws):
    fake_ws.close_next_connection = True     # server kills conn 1 on its first frame
    tts = _provider()
    await tts.connect()
    tts.start_streaming_context()
    await tts.send_text_chunk("Hello there")
    await asyncio.sleep(0.2)                 # let the client process the close
    await tts.send_text_chunk("", is_last=True)
    chunks = [c async for c in tts.receive_audio()]
    await tts.close()
    # The send path transparently reconnected AND re-initialised the context on
    # the new socket (init_gen tracking) — audio still flowed.
    assert chunks == AUDIO_CHUNKS
    assert len(fake_ws.paths) == 2
    conn2 = fake_ws.frames[1]
    assert conn2[0]["text"] == " "           # re-sent InitialiseContext
    assert conn2[-1].get("flush") is True


async def test_lost_is_final_ends_via_stall_guard(fake_ws, monkeypatch):
    fake_ws.drop_is_final = True
    monkeypatch.setattr(el_mod, "_POST_FLUSH_STALL_S", 2.0)
    tts = _provider()
    await tts.connect()
    chunks = await asyncio.wait_for(_drive_utterance(tts), timeout=10)
    await tts.close()
    assert chunks == AUDIO_CHUNKS  # audio still delivered; stall guard ended it


async def test_cancel_then_immediate_close(fake_ws):
    # The chat WS calls close() in a finally right after cancel() — the
    # fire-and-forget close-context frame may lose that race (the dying socket
    # kills the contexts server-side anyway); the contract is: no exception.
    tts = _provider()
    await tts.connect()
    tts.start_streaming_context()
    await tts.send_text_chunk("A sentence that will be interrupted")
    tts.cancel()
    await tts.close()
    await asyncio.sleep(0.05)  # give the fire-and-forget task a beat — no raise


async def test_cancel_sends_close_context_when_socket_lives(fake_ws):
    # Phone barge-in: cancel() mid-turn, the socket stays for the next turn —
    # the close_context frame must go out so server-side generation stops.
    tts = _provider()
    await tts.connect()
    tts.start_streaming_context()
    await tts.send_text_chunk("A sentence that will be interrupted")
    tts.cancel()
    await asyncio.sleep(0.2)
    flat = [f for conn in fake_ws.frames for f in conn]
    assert any(f.get("close_context") for f in flat)
    # Next turn still works on the same provider.
    chunks = await _drive_utterance(tts, "Next turn")
    await tts.close()
    assert chunks == AUDIO_CHUNKS


async def test_cancel_unblocks_receive(fake_ws):
    fake_ws.audio_chunks = []  # server never answers the flush with isFinal
    fake_ws.drop_is_final = True
    tts = _provider()
    await tts.connect()
    tts.start_streaming_context()
    await tts.send_text_chunk("hello")

    async def consume():
        return [c async for c in tts.receive_audio()]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    tts.cancel()
    chunks = await asyncio.wait_for(task, timeout=5)
    await tts.close()
    assert chunks == []


# ── REST paths via MockTransport ───────────────────────────────────


def _mock_client_factory(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(el_mod.httpx, "AsyncClient", factory)


async def test_synthesize_rest(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"\x00\x01" * 100)

    _mock_client_factory(monkeypatch, handler)
    tts = _provider(stability=0.6)
    audio = await tts.synthesize("Kalimera", language="el-GR")
    assert len(audio) == 200
    assert "/v1/text-to-speech/voice-1" in seen["url"]
    assert "output_format=pcm_8000" in seen["url"]
    assert "language_code=el" in seen["url"]  # flash default accepts it
    assert seen["key"] == "k"
    assert seen["body"]["voice_settings"] == {"stability": 0.6}


async def test_synthesize_auth_error(monkeypatch):
    _mock_client_factory(monkeypatch, lambda request: httpx.Response(401, json={"detail": "nope"}))
    tts = _provider()
    with pytest.raises(RuntimeError, match="rejected the API key"):
        await tts.synthesize("hello")


async def test_v3_http_streaming_fallback(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"".join(AUDIO_CHUNKS))

    _mock_client_factory(monkeypatch, handler)
    tts = _provider(model_id="eleven_v3")
    await tts.connect()  # no WS for v3 — must not try to open one
    tts.start_streaming_context(output_sample_rate=24000)
    await tts.send_text_chunk("First part. ")
    await tts.send_text_chunk("Second part.", is_last=True)
    chunks = [c async for c in tts.receive_audio()]
    await tts.close()
    assert b"".join(chunks) == b"".join(AUDIO_CHUNKS)
    assert "/v1/text-to-speech/voice-1/stream" in seen["url"]
    assert "output_format=pcm_24000" in seen["url"]
    assert seen["body"]["text"] == "First part. Second part."
    assert seen["body"]["model_id"] == "eleven_v3"


async def test_list_voices(monkeypatch):
    payload = {"voices": [
        {"voice_id": "v1", "name": "George", "category": "premade",
         "preview_url": "https://x/p.mp3",
         "labels": {"gender": "male", "age": "middle-aged", "language": "en"},
         "verified_languages": [{"language": "en"}, {"language": "el"}]},
    ]}
    _mock_client_factory(monkeypatch, lambda request: httpx.Response(200, json=payload))
    tts = _provider()
    voices = await tts.list_voices()
    assert voices[0].id == "v1"
    assert voices[0].name == "George"
    assert voices[0].languages == ["en", "el"]
    assert "male" in voices[0].description


async def test_search_voice_library(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"voices": [
            {"voice_id": "v9", "name": "Nikos", "public_owner_id": "own1",
             "category": "high_quality", "language": "el", "description": "warm narrator"},
        ]})

    _mock_client_factory(monkeypatch, handler)
    tts = _provider()
    out = await tts.search_voice_library(search="warm", language="el-GR", gender="male")
    assert "/v1/shared-voices" in seen["url"]
    assert "language=el" in seen["url"]
    assert out[0].owner_id == "own1"
    assert out[0].languages == ["el"]


async def test_add_library_voice(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"voice_id": "v9"})

    _mock_client_factory(monkeypatch, handler)
    tts = _provider()
    vid = await tts.add_library_voice("own1", "v9", name="Nikos")
    assert vid == "v9"
    assert "/v1/voices/add/own1/v9" in seen["url"]
