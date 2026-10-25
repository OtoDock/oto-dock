"""WebSocket tests for the chat STT endpoint (``/ws/audio/stt``).

Covers the token-in-init handshake, binary→transcript flow, the cost cap
(1011 when cumulative audio exceeds the token's ``max_seconds``), and replay /
bad-token rejection. Provider building is mocked.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from audio.capabilities import STTCapabilities
from audio.providers.stt.base import Heard
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from services.media import audio_service, ws_audio_token
from ws.audio import ws_audio_stt_handler


class WsFakeSTT:
    is_open = True
    last_result_monotonic = 0.0
    latest_interim = ""

    def __init__(self):
        self.started_with: dict = {}
        self.fatal: str | None = None

    @property
    def has_pending_finals(self):
        return False

    def streams_language(self, language):
        return True

    def enable_dictation_mode(self):
        self.dictation_mode = True

    async def start(self, language, sample_rate=None, interim_results=False, endpointing_ms=None):
        self.started_with = dict(
            language=language, sample_rate=sample_rate,
            interim_results=interim_results, endpointing_ms=endpointing_ms,
        )

    def pop_fatal_error(self):
        err, self.fatal = self.fatal, None
        return err

    keepalives = 0

    async def keepalive(self):
        self.keepalives += 1

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

    def _factory(ptype, *, provider_id=None):
        def build():
            fake = WsFakeSTT()
            fakes.append(fake)
            return fake
        return build, {"id": provider_id or 1, "provider_name": "fake",
                       "advanced": {"chat_endpointing_ms": 1234}}

    monkeypatch.setattr(audio_service, "chat_provider_factory", _factory)
    monkeypatch.setattr(audio_service, "language_identify_enabled", lambda: False)
    app = FastAPI()
    app.add_api_websocket_route("/ws/audio/stt", ws_audio_stt_handler)
    c = TestClient(app)
    c.fakes = fakes  # the providers built during the test, in order
    return c


@pytest.fixture(autouse=True)
def _no_progress(monkeypatch):
    # The progress frame goes out every 2 s; these tests read frames by
    # position, so it is off unless a test turns it on.
    import ws.audio as relay
    monkeypatch.setattr(relay, "_PROGRESS_EVERY_S", 1e6)


def _ready(ws):
    msg = ws.receive_json()
    assert msg["type"] == "ready" and msg["marks_last"] is True and msg["progress_every_s"] > 0


def _token(sub="user-admin", *, max_seconds=60):
    return ws_audio_token.create_ws_audio_token(sub, max_seconds=max_seconds, provider_id=1)["ws_token"]


def test_handshake_and_final_transcript(client):
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
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
        _ready(ws)
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
        _ready(ws)
        ws.send_bytes(b"\x00" * 3200)
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "max_seconds"


def test_provider_fatal_error_is_forwarded(client):
    """A provider that dies AFTER connecting (auth/quota error frame) must
    surface as an error frame — not a mic that silently hears nothing."""
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        client.fakes[0].fatal = ("ElevenLabs speech-to-text auth error: You must be "
                                 "authenticated to use this endpoint.")
        ws.send_bytes(b"\x00" * 3200)
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "provider"
        assert msg["message"] == ("Dictation stopped because the speech service refused its key. "
                                  "An admin can check it in Setup, Audio.")
        # The words owed go out after it, as the one last final.
        assert ws.receive_json() == {"type": "final", "text": "hello world", "last": True}


def test_provider_start_failure_reports_error(client, monkeypatch):
    """start() raising (Deepgram bad key fails at connect) sends an error
    frame before closing instead of dropping the socket silently."""
    class BoomSTT(WsFakeSTT):
        async def start(self, *a, **k):
            raise RuntimeError("Failed to start Deepgram connection")

    monkeypatch.setattr(audio_service, "chat_provider_factory",
                        lambda ptype, *, provider_id=None: (BoomSTT, {"id": 1, "provider_name": "fake", "advanced": {}}))
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "sample_rate": 16000})
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "provider"
        assert "could not start" in msg["message"]


def test_automatic_pick_without_the_identifier_opens_the_detection_stream(client):
    # The identifier off or unavailable: the dashboard's Automatic row stores
    # "auto" and the provider gets its own detection sentinel.
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "auto", "sample_rate": 16000})
        _ready(ws)
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert client.fakes[0].started_with["language"] == "multi"


# ── Automatic language with the local identifier ─────────────────────────
#
# The Automatic pick: the stream opens in the browser's language, the
# identifier reads the voiced audio, a candidate stream opens on a lean with
# the audio replayed, and the shown text switches to it the moment it has
# caught up (a ``replace`` frame carries the new stream's words). Nothing is
# held. A scripted identifier drives the real rule, tracker and streams.

VOICED = np.full(int(16000 * 0.7), 3000, dtype=np.int16).tobytes()   # 0.7 s of voiced audio


class ScriptedIdentifier:
    labels = ["en", "el", "de", "es", "fr", "it"]

    def __init__(self, probs):
        self.probs = probs

    def scores(self, pcm_16k):
        import math
        return np.array([math.log(self.probs.get(lang, 1e-6)) for lang in self.labels])


class StreamFakeSTT(WsFakeSTT):
    """One stream: records what it was sent, answers scripted finals and
    partials, closes on request."""

    def __init__(self):
        super().__init__()
        self.language = None
        self.sent: list[bytes] = []
        self.finals: list[str] = []
        self.interims: list[str] = []
        self.closed = False
        self.endpoints = 0
        self.later: list[str] = []

    async def start(self, language, sample_rate=None, interim_results=False, endpointing_ms=None):
        await super().start(language, sample_rate, interim_results, endpointing_ms)
        self.language = language

    def answered(self, final=None, interim=""):
        if final:
            self.finals.append(final)
        if interim:
            self.interims.append(interim)
            self.latest_interim = interim
        self.last_result_monotonic = time.monotonic()

    async def send_audio(self, b):
        self.sent.append(bytes(b))

    drain_hook = None

    def drain_transcript(self):
        if self.drain_hook is not None:
            hook, self.drain_hook = self.drain_hook, None
            hook()
        if not self.finals:
            text = None
        else:
            text, self.finals = " ".join(self.finals), []
        if self.later:
            self.finals.append(self.later.pop(0))
        return text

    @property
    def has_pending_finals(self):
        return bool(self.finals)

    def pop_interim(self):
        return self.interims.pop(0) if self.interims else None

    async def wait_for_transcript(self, timeout=1.0):
        return self.drain_transcript()

    async def force_endpoint(self):
        self.endpoints += 1

    async def finish(self):
        self.closed = True
        return "the end" if self.language else None

    async def close(self):
        self.closed = True
        self.is_open = False


class WordFakeSTT(StreamFakeSTT):
    """A stream whose provider reports a word confidence: ``hear`` writes its
    ledger on its own audio clock (16 kHz bytes sent)."""

    capabilities = STTCapabilities(reports_confidence=True)

    def __init__(self):
        super().__init__()
        self.heard: list[Heard] = []
        self.audio_offered_s = 0.0

    async def send_audio(self, b):
        await super().send_audio(b)
        self.audio_offered_s += len(b) / 32000

    def hear(self, start, end, words, confidence, *, final=True):
        self.heard.append(Heard(start, end, words, confidence if words else 0.0, final))
        self.last_result_monotonic = time.monotonic()


@pytest.fixture
def switching(temp_db, monkeypatch):
    """A client whose Automatic pick runs the identifier with ``probs``;
    ``fakes`` lists the streams in the order they were built (``words``: a
    provider that reports a word confidence)."""
    fakes: list[StreamFakeSTT] = []
    kind = {"cls": StreamFakeSTT}

    def _factory(ptype, *, provider_id=None):
        def build():
            fake = kind["cls"]()
            fakes.append(fake)
            return fake
        return build, {"id": 1, "provider_name": "fake", "advanced": {}}

    identifiers: list[ScriptedIdentifier] = []

    def _make(probs, *, candidates=("en", "el", "de", "es", "fr", "it"), enabled=True, words=False):
        kind["cls"] = WordFakeSTT if words else StreamFakeSTT

        def _identifier(num_threads=1):
            ident = ScriptedIdentifier(probs)
            identifiers.append(ident)
            return ident
        monkeypatch.setattr(audio_service, "chat_provider_factory", _factory)
        monkeypatch.setattr(audio_service, "language_identify_enabled", lambda: enabled)
        monkeypatch.setattr(audio_service, "language_identify_threads", lambda: 1)
        monkeypatch.setattr(audio_service, "language_candidates", lambda: list(candidates))
        from ws import audio as wsaudio
        monkeypatch.setattr(wsaudio, "EcapaIdentifier", _identifier)
        app = FastAPI()
        app.add_api_websocket_route("/ws/audio/stt", ws_audio_stt_handler)
        c = TestClient(app)
        c.fakes = fakes
        c.identifiers = identifiers
        return c

    return _make


def _open_auto(ws, default_language="en-US"):
    ws.send_json({"type": "init", "token": _token(), "language": "auto", "sample_rate": 16000,
                  "default_language": default_language})
    _ready(ws)


def _voiced(ws, n):
    for _ in range(n):
        ws.send_bytes(VOICED)
        time.sleep(0.05)        # the server's loop runs the reading between chunks


def _until(cond, timeout=3.0):
    """The server runs on its own thread: wait, bounded, for a state it
    reaches between chunks."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("the server did not reach the expected state in time")
        time.sleep(0.01)


def _frames_until(ws, kind, limit=12):
    """Frames up to and including the first of ``kind``."""
    out = []
    for _ in range(limit):
        frame = ws.receive_json()
        out.append(frame)
        if frame["type"] == kind:
            return out
    raise AssertionError(f"no {kind} frame in {out}")


def test_the_automatic_pick_starts_in_the_browser_language_and_the_candidate_replays(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws, "en-GB")
        en = client.fakes[0]
        assert en.started_with["language"] == "en" and en.dictation_mode
        ws.send_bytes(bytes(16000 * 2))                 # a second of silence before the first word
        time.sleep(0.05)
        _voiced(ws, 3)                                  # 2.1 s of voice: a lean at 1.4 s, a switch at 2.1 s
        _until(lambda: len(client.fakes) == 2)
        el = client.fakes[1]
        assert el.started_with["language"] == "el" and el.dictation_mode
        # The replay: the pre-roll of silence (0.3 s), then every voiced chunk
        # that had gone out when the candidate opened (the lean's reading was
        # applied with the third chunk).
        replay = b"".join(el.sent)
        assert replay.endswith(VOICED * 3) and len(replay) == 3 * len(VOICED) + int(0.3 * 16000) * 2
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert all(f.closed for f in client.fakes)


def test_finals_are_never_held_and_a_switch_replaces_the_shown_text(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        en = client.fakes[0]
        en.answered(final="yes sir")                    # the default stream's reading goes out at once
        _voiced(ws, 1)
        assert ws.receive_json() == {"type": "final", "text": "yes sir"}
        _voiced(ws, 3)                                  # the lean, the open, the switch (pending)
        el = client.fakes[1]
        el.answered(final="Ναι κύριε")                  # the candidate caught up
        _voiced(ws, 1)
        frames = _frames_until(ws, "replace")
        assert frames[-1] == {"type": "replace", "text": "Ναι κύριε"}
        el.answered(final="συνεχίζω")
        _voiced(ws, 1)
        assert _frames_until(ws, "final")[-1] == {"type": "final", "text": "συνεχίζω"}
        ws.send_json({"type": "stop"})
        _frames_until(ws, "final")


def test_the_identifier_off_falls_back_to_the_providers_automatic_stream(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, enabled=False)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws, "el-GR")
        _voiced(ws, 4)
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert len(client.fakes) == 1 and client.fakes[0].started_with["language"] == "multi"


def test_a_browser_language_the_install_does_not_list_starts_in_the_first_listed(switching):
    """The admin's list bounds every outcome, the start included: a German
    browser on a Greek and English install starts in Greek."""
    client = switching({"en": 0.9, "el": 0.05, "de": 0.05}, candidates=("el", "en"))
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws, "de-DE")
        assert client.fakes[0].started_with["language"] == "el"
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert client.identifiers and client.identifiers[-1] is not None


def test_one_listed_language_is_a_fixed_stream_with_nothing_to_detect(switching):
    client = switching({"en": 0.9, "el": 0.05, "de": 0.05}, candidates=("el",))
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws, "el-GR")
        assert client.fakes[0].started_with["language"] == "el"
        _voiced(ws, 3)
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert len(client.fakes) == 1 and client.identifiers == []


def test_a_rate_the_identifier_does_not_read_falls_back_to_the_automatic_stream(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "auto", "sample_rate": 24000,
                      "default_language": "en-US"})
        _ready(ws)
        assert client.fakes[0].started_with["language"] == "multi"
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert client.identifiers == []


def test_a_provider_whose_constructor_raises_answers_an_error_frame(client, monkeypatch):
    def boom():
        raise RuntimeError("no such provider row")

    monkeypatch.setattr(audio_service, "chat_provider_factory",
                        lambda ptype, *, provider_id=None: (boom, {"id": 1, "provider_name": "fake", "advanced": {}}))
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "sample_rate": 16000})
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "provider"
        assert "could not start" in msg["message"]


def test_an_unsupported_browser_language_means_english(switching):
    client = switching({"en": 0.9, "el": 0.05, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws, "xx-YY")
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert client.fakes[0].started_with["language"] == "en"


def test_one_candidate_at_a_time(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        assert len(client.fakes) == 2 and client.fakes[1].language == "el"
        client.fakes[1].answered(interim="γ")
        _voiced(ws, 1)                                  # the switch to el resolves
        # The leader moves to German: a German candidate opens beside the
        # default and the active Greek stream (the one inactive candidate).
        client.identifiers[-1].probs = {"en": 0.1, "el": 0.05, "de": 0.85}
        _voiced(ws, 2)
        assert [f.language for f in client.fakes] == ["en", "el", "de"]
        assert not client.fakes[1].closed and not client.fakes[2].closed
        # The leader moves on again: the German candidate goes, a French one opens.
        client.identifiers[-1].probs = {"en": 0.1, "el": 0.05, "fr": 0.85}
        _voiced(ws, 2)
        assert [f.language for f in client.fakes] == ["en", "el", "de", "fr"]
        assert client.fakes[2].closed and not client.fakes[3].closed
        ws.send_json({"type": "stop"})
        _frames_until(ws, "final")


def test_usage_and_the_cap_count_every_stream(switching, monkeypatch):
    recorded = {}

    def _record(sub, source, name, provider, *, seconds=0.0, chars=0):
        recorded.update(sub=sub, seconds=seconds)
    monkeypatch.setattr(audio_service, "record_audio_usage", _record)
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 4)                                  # 2.8 s to the default, a replay of 2.1 s plus 0.7 live to el
        ws.send_json({"type": "stop"})
        _frames_until(ws, "final")
        # The usage is written as the socket ends: close it and wait here,
        # before the client's exit cancels the server's teardown.
        ws.close()
        _until(lambda: "seconds" in recorded, timeout=10.0)
    assert round(recorded["seconds"], 1) == round(2.8 + 2.1 + 0.7, 1)


def test_the_cap_closes_once_every_streams_seconds_pass_it(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(max_seconds=4), "language": "auto",
                      "sample_rate": 16000, "default_language": "en"})
        _ready(ws)
        _voiced(ws, 4)                                  # 2.8 s of audio, but 5.6 s billed across two streams
        frames = _frames_until(ws, "error")
        assert frames[-1]["code"] == "max_seconds"


def test_a_stop_right_after_a_switch_waits_for_the_new_streams_final(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        el = client.fakes[1]
        el.answered(interim="γεια")
        _voiced(ws, 1)                                  # the switch resolves on the partial
        frames = _frames_until(ws, "replace")
        assert frames[-1] == {"type": "replace", "text": ""}
        el.finals.append("γεια σας")                    # the final the stop must wait for
        ws.send_json({"type": "stop"})
        assert _frames_until(ws, "final")[-1] == {"type": "final", "text": "γεια σας the end", "last": True}
    assert client.fakes[1].endpoints == 1


def test_a_stop_while_the_switch_is_pending_and_answered_sends_the_replace_then_the_final(switching, monkeypatch):
    """A short Greek sentence under an English browser, stop pressed while
    the candidate catches up: the stop resolves the switch, the composer
    gets the Greek words as a replace before the one final."""
    from ws import audio as wsaudio
    monkeypatch.setattr(wsaudio, "_STOP_FINAL_WAIT_S", 0.1)
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05})
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        en = client.fakes[0]
        en.answered(final="Anna Viotria")
        _voiced(ws, 1)
        assert ws.receive_json() == {"type": "final", "text": "Anna Viotria"}
        _voiced(ws, 3)                                  # the lean, the open, the switch (pending)
        _until(lambda: len(client.fakes) == 2)
        el = client.fakes[1]
        el.answered(final="Ένα, δύο, τρία")             # answered, but no frame flows after it
        ws.send_json({"type": "stop"})
        frames = _frames_until(ws, "final")
        assert frames[-2:] == [{"type": "replace", "text": "Ένα, δύο, τρία"}, {"type": "final", "text": "the end", "last": True}]
    assert client.fakes[0].closed and client.fakes[1].closed


# ── The words decide (a provider that reports a word confidence) ─────────
#
# The ear (the scripted identifier) leans to Greek on every reading, as it
# does for a Greek speaker's English. The streams' words confirm, refuse or
# call the switch back.


def _el_exists(client):
    _until(lambda: len(client.fakes) == 2 and client.fakes[1].language == "el")
    time.sleep(0.05)
    return client.fakes[0], client.fakes[1]


def _all_frames_until(ws, kind, limit=40):
    return _frames_until(ws, kind, limit)


def test_a_switch_the_shown_words_refuse_never_sends_a_replace(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        en, el = _el_exists(client)
        en.hear(0.0, 5.0, 14, 0.97)                     # accented English: the English words are clear
        el.hear(0.0, 5.0, 12, 0.70)                     # the Greek stream's are garbage
        _voiced(ws, 3)
        ws.send_json({"type": "stop"})
        frames = _all_frames_until(ws, "final")
        assert all(f["type"] != "replace" for f in frames)
        assert frames[-1] == {"type": "final", "text": "the end", "last": True}


def test_a_decisive_refusal_settles_on_the_shown_language(switching, caplog):
    import logging
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, words=True)
    with caplog.at_level(logging.INFO, logger="otodock"):
        with client.websocket_connect("/ws/audio/stt") as ws:
            _open_auto(ws)
            _voiced(ws, 3)
            en, el = _el_exists(client)
            en.hear(0.0, 6.0, 18, 0.97)
            el.hear(0.0, 6.0, 15, 0.68)
            _voiced(ws, 6)                              # 3 s of voice since the open: the refusal finalises
            _until(lambda: el.closed)
            ws.send_json({"type": "stop"})
            _all_frames_until(ws, "final")
    assert any("dictation language final: en" in r.getMessage() for r in caplog.records)


def test_the_defaults_decisive_words_call_the_switch_back_with_a_replace(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        en, el = _el_exists(client)
        en.hear(0.0, 2.0, 0, 0.0)                       # the English stream heard nothing at first
        el.hear(0.0, 2.0, 5, 0.92)
        _voiced(ws, 1)
        assert _frames_until(ws, "replace")[-1]["type"] == "replace"
        en.finals.append("and then I said")
        en.hear(2.0, 6.0, 12, 0.97)
        el.hear(2.0, 6.0, 10, 0.60)
        _voiced(ws, 2)
        frames = _frames_until(ws, "replace")
        assert frames[-1] == {"type": "replace", "text": "and then I said"}
        ws.send_json({"type": "stop"})
        _all_frames_until(ws, "final")


def test_a_final_drained_after_the_switch_travels_inside_the_replace(switching):
    """The switch resolved before the chunk's drain: the new stream's final
    that the drain then hands back is folded into the replace, never sent as
    a final the replace would overwrite."""
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        en, el = _el_exists(client)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 2.0, 6, 0.93)
        el.finals.append("Καλησπέρα")                    # drained at the resolve: in the replace's snapshot
        el.later = ["με ακούς"]                          # drained by the chunk's own drain, after it
        _voiced(ws, 1)
        frames = _frames_until(ws, "replace")
        assert frames[-1] == {"type": "replace", "text": "Καλησπέρα με ακούς"}
        assert all(f["type"] != "final" for f in frames)
        ws.send_json({"type": "stop"})
        _all_frames_until(ws, "final")


def test_the_new_streams_partial_is_painted_again_after_the_replace(switching):
    """The composer repaints from the placed words on a replace, which wipes
    the partial it showed a pass earlier: the active stream's latest partial
    goes out once more right after the replace, even when nothing new was
    popped."""
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        en, el = _el_exists(client)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 2.0, 6, 0.93)
        el.latest_interim = "Καλησπέρα με ακούς"            # shown before the switch, popped already
        _voiced(ws, 1)
        frames = _frames_until(ws, "interim", limit=16)
        kinds = [f["type"] for f in frames]
        assert "replace" in kinds and kinds.index("replace") < kinds.index("interim")
        assert frames[-1] == {"type": "interim", "text": "Καλησπέρα με ακούς"}
        ws.send_json({"type": "stop"})
        _all_frames_until(ws, "final")


def test_a_switch_that_resolves_inside_the_drain_sends_the_old_final_then_the_replace(switching):
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        en, el = _el_exists(client)
        en.hear(0.0, 2.0, 0, 0.0)
        en.finals.append("Can you spell out")           # the shown stream's final, drained first

        def words_arrive():
            el.hear(0.0, 2.0, 6, 0.93)
            el.finals.append("Καλησπέρα με ακούς")
        el.drain_hook = words_arrive                    # the candidate's words land inside the drain
        _voiced(ws, 2)
        frames = _frames_until(ws, "replace")
        kinds = [(f["type"], f.get("text")) for f in frames]
        assert kinds.index(("final", "Can you spell out")) < kinds.index(("replace", "Καλησπέρα με ακούς"))
        ws.send_json({"type": "stop"})
        _all_frames_until(ws, "final")


def test_the_language_list_is_read_off_the_loop(switching, monkeypatch):
    import asyncio
    on_loop = []

    def candidates():
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return ["en", "el"]

    client = switching({"en": 0.9, "el": 0.05, "de": 0.05})
    monkeypatch.setattr(audio_service, "language_candidates", candidates)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert on_loop == [False]


def test_an_unreadable_language_list_starts_with_every_language(switching, monkeypatch):
    def broken():
        raise RuntimeError("the settings store is down")

    client = switching({"en": 0.9, "el": 0.05, "de": 0.05})
    monkeypatch.setattr(audio_service, "language_candidates", broken)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws, "el-GR")
        assert client.fakes[0].started_with["language"] == "el"
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert client.identifiers                           # the streams and the identifier ran


def test_a_one_word_dictation_decides_at_the_stop(switching, monkeypatch):
    """A stop with the switch still pending: the speech has ended, every
    stream is finalised, and one Greek word against an English stream that
    heard nothing switches."""
    from ws import audio as wsaudio
    monkeypatch.setattr(wsaudio, "_STOP_FINAL_WAIT_S", 0.1)
    client = switching({"en": 0.1, "el": 0.85, "de": 0.05}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        en, el = _el_exists(client)
        _voiced(ws, 1)                                  # the switch goes pending: no words yet
        en.hear(0.0, 2.8, 0, 0.0)
        el.hear(0.0, 2.8, 1, 0.95)
        el.finals.append("Ναι")
        ws.send_json({"type": "stop"})
        frames = _all_frames_until(ws, "final")
        assert {"type": "replace", "text": "Ναι"} in frames
    assert en.endpoints >= 1                            # the stop finalised the shown stream


def test_the_stop_is_the_dictations_take_a_one_word_dictation_is_read_there(switching, monkeypatch):
    """0.7 s of voice is never read while the person dictates (the first
    reading needs a second): the stop takes one reading, the take rule lets a
    sure leader open and switch, and the candidate's word decides."""
    from ws import audio as wsaudio
    monkeypatch.setattr(wsaudio, "_STOP_FINAL_WAIT_S", 0.1)
    client = switching({"en": 0.02, "el": 0.97, "de": 0.01}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        en = client.fakes[0]
        ws.send_bytes(VOICED)                           # one word: no reading yet
        time.sleep(0.05)
        assert len(client.fakes) == 1
        en.hear(0.0, 0.7, 0, 0.0)                       # the English stream heard nothing
        ws.send_json({"type": "stop"})
        _until(lambda: len(client.fakes) == 2 and client.fakes[1].language == "el")
        el = client.fakes[1]
        el.finals.append("Ναι")
        el.hear(0.0, 0.7, 1, 0.95)
        frames = _all_frames_until(ws, "final")
        assert {"type": "replace", "text": "Ναι"} in frames


def test_a_one_word_english_dictation_the_ear_is_not_sure_of_stays(switching, monkeypatch):
    from ws import audio as wsaudio
    monkeypatch.setattr(wsaudio, "_STOP_FINAL_WAIT_S", 0.1)
    client = switching({"en": 0.3, "el": 0.68, "de": 0.02}, words=True)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        ws.send_bytes(VOICED)
        time.sleep(0.05)
        ws.send_json({"type": "stop"})
        frames = _all_frames_until(ws, "final")
        assert all(f["type"] != "replace" for f in frames) and len(client.fakes) == 1


def test_a_provider_without_word_confidence_never_switches_a_short_dictation_at_the_stop(switching, monkeypatch):
    """Without the words to check it, a lone reading at the stop would switch
    an accented "Yes, send it": the stop is no take there, as before."""
    from ws import audio as wsaudio
    monkeypatch.setattr(wsaudio, "_STOP_FINAL_WAIT_S", 0.1)
    client = switching({"en": 0.02, "el": 0.97, "de": 0.01}, words=False)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        ws.send_bytes(VOICED)                           # 0.7 s of voice: never read while dictating
        time.sleep(0.05)
        ws.send_json({"type": "stop"})
        frames = _all_frames_until(ws, "final")
        assert all(f["type"] != "replace" for f in frames)
    assert len(client.fakes) == 1                       # no candidate stream was opened


# ── The link: the keepalive, the progress, the endings ─────────────────

def _quick_link(monkeypatch, **values):
    import ws.audio as relay
    for name, value in {"_TICK_S": 0.02, **values}.items():
        monkeypatch.setattr(relay, name, value)


def test_a_quiet_link_keeps_the_stream_alive_and_says_how_much_arrived(client, monkeypatch):
    _quick_link(monkeypatch, _IDLE_KEEPALIVE_S=0.05, _PROGRESS_EVERY_S=0.1)
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        ws.send_bytes(b"\x00" * 3200)                   # 0.1 s, then the link goes quiet
        frames = [ws.receive_json() for _ in range(3)]
        assert all(f["type"] == "progress" and f["audio_s"] == 0.1 for f in frames)
        assert all(0 < f["left_s"] <= 180 for f in frames)     # the seconds before the limit
        assert client.fakes[0].keepalives >= 1
        ws.send_json({"type": "stop"})
        assert _frames_until(ws, "final")[-1] == {"type": "final", "text": "hello world", "last": True}


def test_the_stop_answers_one_marked_final_even_when_empty(client, monkeypatch):
    async def nothing(self):
        return None
    monkeypatch.setattr(WsFakeSTT, "finish", nothing)
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "", "last": True}


def test_the_cap_says_so_plainly_and_sends_the_words_owed(client):
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(max_seconds=0), "language": "en", "sample_rate": 16000})
        _ready(ws)
        ws.send_bytes(b"\x00" * 3200)
        assert ws.receive_json() == {
            "type": "error", "code": "max_seconds",
            "message": "Dictation reached its time limit and stopped. What was heard is in the box. "
                       "Tap the mic to continue.",
        }
        assert ws.receive_json() == {"type": "final", "text": "hello world", "last": True}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_a_stream_that_stopped_answering_says_so_plainly(client):
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        client.fakes[0].fatal = ('Deepgram speech-to-text error: {"description": "ConnectionClosed in '
                                 'AbstractAsyncWebSocketClient._listening", "message": "received 1011"}')
        ws.send_bytes(b"\x00" * 3200)
        msg = ws.receive_json()
        assert msg["code"] == "provider" and msg["message"] == (
            "Dictation stopped because the speech service stopped answering. What was heard is in the box. "
            "Tap the mic to continue.")
        assert ws.receive_json() == {"type": "final", "text": "hello world", "last": True}


def test_a_minute_without_audio_ends_the_dictation(client, monkeypatch):
    _quick_link(monkeypatch, _IDLE_END_S=0.2)
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "idle"
        assert msg["message"].startswith("Dictation stopped because no sound reached it for a minute.")
        assert ws.receive_json() == {"type": "final", "text": "hello world", "last": True}


def test_words_a_provider_releases_go_out_while_the_link_is_quiet(client, monkeypatch):
    _quick_link(monkeypatch)
    later = {"text": None}
    monkeypatch.setattr(WsFakeSTT, "drain_transcript", lambda self: later.pop("text", None))
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        later["text"] = "ένα δύο"                         # released while no frame arrives at all
        assert ws.receive_json() == {"type": "final", "text": "ένα δύο"}
        ws.send_json({"type": "stop"})
        _frames_until(ws, "final")


def test_the_seconds_listened_again_are_a_usage_row_of_their_own(client, monkeypatch, caplog):
    caplog.set_level("INFO", logger="otodock")
    recorded = {}

    def _record(sub, source, name, provider, *, seconds=0.0, chars=0):
        recorded[source] = seconds
    monkeypatch.setattr(audio_service, "record_audio_usage", _record)
    monkeypatch.setattr(WsFakeSTT, "relisten_seconds", 2.5, raising=False)
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        ws.send_bytes(b"\x00" * 32000)                  # 1 s
        ws.send_json({"type": "stop"})
        _frames_until(ws, "final")
        ws.close()
        _until(lambda: len(recorded) == 2, timeout=10.0)
    assert recorded == {"audio-stt-chat": pytest.approx(1.0), "audio-stt-chat-relisten": pytest.approx(2.5)}
    assert any("1.0 s streamed, 2.5 s listened again" in r.message for r in caplog.records)


def test_a_stall_then_a_burst_keeps_every_final_in_order(client, monkeypatch):
    _quick_link(monkeypatch, _IDLE_KEEPALIVE_S=0.05)
    sent = {"n": 0}
    pending: list[str] = []

    async def count(self, b):
        sent["n"] += 1
        pending.append(f"w{sent['n']}")                # one final per chunk, drained once
    monkeypatch.setattr(WsFakeSTT, "send_audio", count)
    monkeypatch.setattr(WsFakeSTT, "drain_transcript", lambda self: pending.pop(0) if pending else None)
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "en", "sample_rate": 16000})
        _ready(ws)
        finals = []
        for _ in range(3):
            ws.send_bytes(b"\x00" * 3200)
            finals.append(ws.receive_json()["text"])
        time.sleep(0.3)                                  # the link stalls: the vendor gets its keepalive
        for _ in range(3):
            ws.send_bytes(b"\x00" * 3200)               # the burst
        while len(finals) < 6:
            frame = ws.receive_json()
            if frame["type"] == "final":
                finals.append(frame["text"])
        assert client.fakes[0].keepalives >= 1
        ws.send_json({"type": "stop"})
        _frames_until(ws, "final")
    assert finals[:6] == [f"w{i}" for i in range(1, 7)]


def test_a_refused_key_is_told_apart_from_other_failures():
    assert audio_service.is_key_refusal("ElevenLabs speech-to-text auth error: You must be authenticated")
    assert audio_service.is_key_refusal("Deepgram rejected the API key")
    assert not audio_service.is_key_refusal("received 1011 (internal error) Deepgram did not receive audio data")
