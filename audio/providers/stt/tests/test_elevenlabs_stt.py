"""ElevenLabsSTT tests: offline contract surface, batch mapping via
MockTransport, and streaming lifecycle against an in-process fake Scribe
realtime WebSocket server (no network, no key)."""

import asyncio
import base64
import json

import httpx
import pytest
import websockets

import audio.providers.stt.elevenlabs as el_mod
from audio.providers.stt.elevenlabs import ElevenLabsSTT, _to_elevenlabs_lang


# ── Offline contract surface ───────────────────────────────────────


def test_capabilities_and_billing():
    caps = ElevenLabsSTT.capabilities
    assert caps.supports_streaming
    assert caps.supports_transcribe_file
    assert caps.supports_word_timestamps
    assert not caps.is_local
    assert ElevenLabsSTT.is_stub is False
    assert ElevenLabsSTT.billing_unit() == "second"
    assert ElevenLabsSTT.cost_per_unit() > 0


def test_from_row_reads_advanced():
    stt = ElevenLabsSTT.from_row(
        {"provider_type": "stt", "provider_name": "elevenlabs",
         "credential_key": "audio-elevenlabs",
         "advanced": {"call_endpointing_ms": 300, "model_id": "scribe_v2_realtime"}},
        lambda key: "secret",
    )
    assert stt.endpointing_ms == 300
    assert stt.vad_silence_padding_ms == 350


def test_validate_advanced():
    assert ElevenLabsSTT.validate_advanced({"call_endpointing_ms": 200}) == {}
    bad = ElevenLabsSTT.validate_advanced(
        {"call_endpointing_ms": -1, "chat_endpointing_ms": "x", "silence_gate_rms": -5})
    assert set(bad) == {"call_endpointing_ms", "chat_endpointing_ms", "silence_gate_rms"}


def test_default_advanced_settings_surface_models_and_gate():
    d = ElevenLabsSTT.default_advanced_settings()
    assert d["model_id"] == "scribe_v2_realtime"
    assert d["batch_model_id"] == "scribe_v2"
    assert d["chat_endpointing_ms"] == 1500
    assert d["call_endpointing_ms"] == 500
    assert d["silence_gate_rms"] == 300


# ── Silence gate (anti-hallucination) ──────────────────────────────
# Scribe is Whisper-family and invents speech from open-mic room noise;
# sub-threshold frames are zeroed (timeline preserved for server VAD).


def test_silence_gate_zeroes_noise_and_honors_hangover():
    stt = ElevenLabsSTT(api_key="k")  # default gate 300, rate 8000
    voiced = b"\x00\x10" * 2000     # 0.25 s @ 8 kHz, RMS 4096
    noise = b"\x00\x01" * 2000      # 0.25 s, RMS 256
    # Cold start: no hangover -> noise is zeroed.
    assert stt._gate_silence(noise) == b"\x00" * len(noise)
    # Voiced passes and opens the 0.6 s hangover window.
    assert stt._gate_silence(voiced) == voiced
    # Within the hangover, noise still passes (soft trailing syllables).
    for _ in range(3):
        assert stt._gate_silence(noise) == noise
    # Window exhausted -> zeroed again.
    assert stt._gate_silence(noise) == b"\x00" * len(noise)


def test_silence_gate_disabled_and_odd_length_pass_through():
    noise = b"\x00\x01" * 100
    assert ElevenLabsSTT(api_key="k", silence_gate_rms=0)._gate_silence(noise) == noise
    odd = b"\x00\x01\x02"  # not clean pcm16 -> untouched
    assert ElevenLabsSTT(api_key="k")._gate_silence(odd) == odd


def test_from_row_reads_silence_gate():
    stt = ElevenLabsSTT.from_row(
        {"credential_key": "k", "advanced": {"silence_gate_rms": 42}}, lambda key: "s")
    assert stt._silence_gate_rms == 42


def test_repr_redacts_api_key():
    stt = ElevenLabsSTT(api_key="super-secret")
    assert "super-secret" not in repr(stt)


def test_lang_normalizer():
    assert _to_elevenlabs_lang("el-GR") == "el"
    assert _to_elevenlabs_lang("en") == "en"
    assert _to_elevenlabs_lang("multi") is None
    assert _to_elevenlabs_lang("") is None


async def test_start_rejects_unknown_rate():
    stt = ElevenLabsSTT(api_key="k")
    with pytest.raises(ValueError):
        await stt.start(sample_rate=11025)


# ── Batch transcription via MockTransport ──────────────────────────


def _mock_client_factory(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(el_mod.httpx, "AsyncClient", factory)


SCRIBE_RESPONSE = {
    "language_code": "el",
    "language_probability": 0.97,
    "text": "Καλημέρα κόσμε",
    "words": [
        {"text": "Καλημέρα", "type": "word", "start": 0.1, "end": 0.6},
        {"text": " ", "type": "spacing", "start": 0.6, "end": 0.7},
        {"text": "(laughter)", "type": "audio_event", "start": 0.7, "end": 1.0},
        {"text": "κόσμε", "type": "word", "start": 1.0, "end": 1.5},
    ],
}


async def test_transcribe_file_maps_words_and_duration(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key")
        seen["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json=SCRIBE_RESPONSE)

    _mock_client_factory(monkeypatch, handler)
    stt = ElevenLabsSTT(api_key="k")
    result = await stt.transcribe_file(b"fake-bytes", language="el-GR")

    assert "/v1/speech-to-text" in seen["url"]
    assert seen["key"] == "k"
    assert "multipart/form-data" in seen["content_type"]
    assert result.text == "Καλημέρα κόσμε"
    assert result.language == "el"
    assert [w.word for w in result.words] == ["Καλημέρα", "κόσμε"]  # word-type only
    assert result.audio_seconds == 1.5  # last word end
    assert result.provider_used == "elevenlabs"


async def test_transcribe_file_auth_error(monkeypatch):
    _mock_client_factory(monkeypatch, lambda request: httpx.Response(401, json={"detail": "no"}))
    stt = ElevenLabsSTT(api_key="bad")
    with pytest.raises(RuntimeError, match="rejected the API key"):
        await stt.transcribe_file(b"x")


# ── Streaming lifecycle vs fake Scribe server ──────────────────────


class FakeScribeWS:
    """Scripted Scribe realtime endpoint: records client frames, emits a
    partial per audio chunk and a committed transcript on commit. Can also
    push an unsolicited committed frame (a server-side VAD commit)."""

    def __init__(self):
        self.paths: list[str] = []
        self.frames: list[dict] = []
        self.partial_text = "kali"
        self.committed_text = "Καλημέρα κόσμε"
        self.server = None
        self.port = 0
        self.ws = None  # newest live connection

    async def start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def push_committed(self, text: str):
        await self.ws.send(json.dumps({"message_type": "committed_transcript", "text": text}))

    async def push_error(self, mtype: str, detail: str):
        await self.ws.send(json.dumps({"message_type": mtype, "error": detail}))

    def commit_frames(self) -> list[dict]:
        return [f for f in self.frames if f.get("commit")]

    async def _handler(self, ws):
        self.paths.append(ws.request.path)
        self.ws = ws
        await ws.send(json.dumps({"message_type": "session_started", "session_id": "s1"}))
        async for raw in ws:
            msg = json.loads(raw)
            self.frames.append(msg)
            if msg.get("message_type") != "input_audio_chunk":
                continue
            if msg.get("commit"):
                await ws.send(json.dumps(
                    {"message_type": "committed_transcript", "text": self.committed_text}
                ))
            else:
                await ws.send(json.dumps(
                    {"message_type": "partial_transcript", "text": self.partial_text}
                ))


@pytest.fixture
async def fake_scribe(monkeypatch):
    fake = await FakeScribeWS().start()
    monkeypatch.setattr(el_mod, "_WS_BASE", f"ws://127.0.0.1:{fake.port}")
    yield fake
    await fake.stop()


async def test_streaming_lifecycle(fake_scribe):
    stt = ElevenLabsSTT(api_key="k", endpointing_ms=400)
    await stt.start(language="el-GR", sample_rate=16000, interim_results=True)

    # Connection URL carries the mapped format + vad commit strategy.
    path = fake_scribe.paths[0]
    assert "audio_format=pcm_16000" in path
    assert "commit_strategy=vad" in path
    assert "language_code=el" in path
    assert "vad_silence_threshold_secs=0.4" in path

    await stt.send_audio(b"\x00\x10" * 160)
    await asyncio.sleep(0.2)  # let the partial arrive

    # Interim dedup: first pop returns it, second returns None.
    assert stt.pop_interim() == "kali"
    assert stt.pop_interim() is None
    assert stt.latest_interim == "kali"

    # Audio frame shape.
    frame = fake_scribe.frames[0]
    assert frame["message_type"] == "input_audio_chunk"
    assert frame["sample_rate"] == 16000
    assert frame["commit"] is False
    assert base64.b64decode(frame["audio_base_64"]) == b"\x00\x10" * 160

    # force_endpoint commits → committed transcript lands in the queue.
    await stt.force_endpoint()
    text = await stt.wait_for_transcript(timeout=2.0)
    assert text == "Καλημέρα κόσμε"
    assert stt.latest_interim == ""  # cleared on commit

    commit_frame = fake_scribe.frames[-1]
    assert commit_frame["commit"] is True
    assert len(base64.b64decode(commit_frame["audio_base_64"])) > 0  # silence payload

    await stt.close()
    assert stt._is_open is False


async def test_finish_commits_and_drains(fake_scribe):
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(sample_rate=16000)
    await stt.send_audio(b"\x00\x10" * 160)  # voiced (RMS 4096, above the gate)
    text = await stt.finish()
    assert text == "Καλημέρα κόσμε"


async def test_language_autodetect_when_multi(fake_scribe):
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(language="multi", sample_rate=16000)
    await stt.close()
    path = fake_scribe.paths[0]
    assert "language_code" not in path
    assert "include_language_detection=true" in path


async def test_recover_after_opening_reconnects(fake_scribe):
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(language="en", sample_rate=16000)
    # Simulate the socket dying during a long opening TTS.
    await stt._ws.close()
    await asyncio.sleep(0.2)
    assert stt._is_open is False
    ok = await stt.recover_after_opening("en")
    assert ok is True
    assert stt._is_open is True
    assert len(fake_scribe.paths) == 2  # a genuine reconnect happened
    await stt.close()


async def test_drain_and_clear_queue(fake_scribe):
    voiced = b"\x00\x10" * 160
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(sample_rate=16000)
    await stt.send_audio(voiced)
    await stt.force_endpoint()
    await asyncio.sleep(0.2)
    assert stt.drain_transcript() == "Καλημέρα κόσμε"
    assert stt.drain_transcript() is None
    await stt.send_audio(voiced)
    await stt.force_endpoint()
    await asyncio.sleep(0.2)
    stt.clear_queue()
    assert stt.drain_transcript() is None
    await stt.close()


# ── Hallucination guard: no commit / no text without voiced audio ──
# Whisper-family Scribe invents text when transcribing silence, so a commit
# covering only gated (zeroed) audio must never happen — and text the server
# produces for such a span must never surface (live repro 2026-07-15:
# trailing repeated Greek on mic-stop after a pause).


async def test_finish_skips_commit_when_nothing_voiced(fake_scribe):
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(sample_rate=16000)
    await stt.send_audio(b"\x00\x01" * 160)  # below-gate noise → zeroed
    text = await stt.finish()
    assert text is None
    assert fake_scribe.commit_frames() == []  # the commit was never sent


async def test_commit_resumes_after_new_voiced_audio(fake_scribe):
    voiced = b"\x00\x10" * 160
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(sample_rate=16000)
    await stt.send_audio(voiced)
    await stt.force_endpoint()
    assert await stt.wait_for_transcript(timeout=2.0) == "Καλημέρα κόσμε"
    # Committed transcript consumed the buffer → silence-only follow-up commit
    # is skipped…
    await stt.send_audio(b"\x00\x01" * 160)
    await stt.force_endpoint()
    await asyncio.sleep(0.2)
    assert len(fake_scribe.commit_frames()) == 1
    # …but fresh voiced audio re-arms it.
    await stt.send_audio(voiced)
    await stt.force_endpoint()
    assert await stt.wait_for_transcript(timeout=2.0) == "Καλημέρα κόσμε"
    assert len(fake_scribe.commit_frames()) == 2
    await stt.close()


async def test_silence_only_committed_transcript_is_dropped(fake_scribe):
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(sample_rate=16000)
    await stt.send_audio(b"\x00\x01" * 160)  # zeroed — nothing voiced
    await asyncio.sleep(0.1)
    # Server VAD commits the silence span anyway and hallucinates text.
    await fake_scribe.push_committed("Πάει καλά; Πάει καλά.")
    await asyncio.sleep(0.2)
    assert stt.drain_transcript() is None
    # Real speech afterwards still comes through.
    await stt.send_audio(b"\x00\x10" * 160)
    await fake_scribe.push_committed("Καλημέρα")
    await asyncio.sleep(0.2)
    assert stt.drain_transcript() == "Καλημέρα"
    await stt.close()


async def test_error_frames_surface_via_pop_fatal_error(fake_scribe):
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(sample_rate=16000)
    assert stt.pop_fatal_error() is None
    await fake_scribe.push_error("auth_error", "You must be authenticated to use this endpoint.")
    await asyncio.sleep(0.2)
    err = stt.pop_fatal_error()
    assert err is not None and "auth error" in err and "authenticated" in err
    assert stt.pop_fatal_error() is None  # surfaced once, then cleared
    await stt.close()


async def test_partials_suppressed_when_nothing_voiced(fake_scribe):
    stt = ElevenLabsSTT(api_key="k")
    await stt.start(sample_rate=16000, interim_results=True)
    # The fake emits a partial for every chunk — with only gated audio sent,
    # it must not surface (the composer previews partials live).
    await stt.send_audio(b"\x00\x01" * 160)
    await asyncio.sleep(0.2)
    assert stt.pop_interim() is None
    assert stt.latest_interim == ""
    # Voiced audio → partials flow again.
    await stt.send_audio(b"\x00\x10" * 160)
    await asyncio.sleep(0.2)
    assert stt.pop_interim() == "kali"
    await stt.close()
