"""WebSocket tests for the chat STT endpoint (``/ws/audio/stt``).

Covers the token-in-init handshake, binary→transcript flow, the cost cap
(1011 when cumulative audio exceeds the token's ``max_seconds``), and replay /
bad-token rejection. Provider building is mocked.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from audio.providers.stt.base import LanguageGuess
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


def test_automatic_pick_opens_the_detection_stream(client):
    # The dashboard's Automatic row stores "auto"; the provider gets its own
    # detection sentinel.
    with client.websocket_connect("/ws/audio/stt") as ws:
        ws.send_json({"type": "init", "token": _token(), "language": "auto", "sample_rate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert client.fakes[0].started_with["language"] == "multi"


# ── The dictation lock on the Automatic pick ─────────────────────────────
#
# A provider whose automatic stream covers a fixed set of languages (Deepgram:
# ten, Greek not among them): the relay identifies the first words through
# the batch endpoint, holds the automatic stream's results meanwhile, and
# reopens the stream in the identified language with the buffered audio
# replayed (the phone route's mechanism, on the chat mic).

VOICED = np.full(int(16000 * 0.7), 3000, dtype=np.int16).tobytes()   # 0.7 s of voiced audio


class LockFakeSTT(WsFakeSTT):
    """An automatic stream that covers English only."""

    multi_languages = frozenset({"en"})

    def __init__(self, guess=None, *, identify_delay=0.0):
        super().__init__()
        self.guess = guess
        self.identify_delay = identify_delay
        self.identify_delays: list[float] = []   # per request, ahead of identify_delay
        self.restart_fails: set[str] = set()
        self.identified: list[bytes] = []
        self.restarts: list[str] = []
        self.sent: list[bytes] = []
        self.finals: list[str] = []      # what drain_transcript answers, in order
        self.interims: list[str] = []    # what pop_interim answers, in order
        self.endpoints = 0

    def pop_interim(self):
        return self.interims.pop(0) if self.interims else None

    async def identify_language(self, wav):
        self.identified.append(wav)
        delay = self.identify_delays.pop(0) if self.identify_delays else self.identify_delay
        if delay:
            await asyncio.sleep(delay)
        return self.guess

    def streams_language(self, lang):
        return True

    async def restart(self, lang):
        self.restarts.append(lang)
        self.sent.append(f"restart:{lang}".encode())
        return lang not in self.restart_fails

    async def send_audio(self, b):
        self.sent.append(bytes(b))

    def drain_transcript(self):
        return self.finals.pop(0) if self.finals else None

    async def force_endpoint(self):
        self.endpoints += 1

    async def wait_for_transcript(self, timeout=1.0):
        return self.drain_transcript()

    async def finish(self):
        return "the end"


def _lock_client(monkeypatch, fake):
    def _build(ptype, *, provider_id=None):
        return fake, {"id": 1, "provider_name": "fake", "advanced": {}}

    monkeypatch.setattr(audio_service, "build_chat_provider", _build)
    app = FastAPI()
    app.add_api_websocket_route("/ws/audio/stt", ws_audio_stt_handler)
    return TestClient(app)


def _open_auto(ws):
    ws.send_json({"type": "init", "token": _token(), "language": "auto", "sample_rate": 16000})
    assert ws.receive_json() == {"type": "ready"}


def _voiced(ws, n):
    for _ in range(n):
        ws.send_bytes(VOICED)
        time.sleep(0.03)        # the server's loop runs the identification between chunks


def test_a_greek_speaker_reopens_the_stream_in_greek_with_the_audio_replayed(temp_db, monkeypatch):
    fake = LockFakeSTT(LanguageGuess("el", 0.95, "καλησπέρα σας λέω"))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["kalispera sas leo"]          # the automatic stream's reading: held, then dropped
        fake.interims = ["kalispera"]                # its partial shows meanwhile
        _voiced(ws, 2)                               # 1.4 s voiced: the identification runs
        assert ws.receive_json() == {"type": "interim", "text": "kalispera"}
        _voiced(ws, 1)                               # the answer is in: the restart and the replay
        fake.finals = ["Καλησπέρα σας λέω"]
        _voiced(ws, 1)                               # locked: forwarded as it comes
        assert ws.receive_json() == {"type": "final", "text": "Καλησπέρα σας λέω"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "the end"}
    assert fake.restarts == ["el"] and len(fake.identified) == 1
    assert fake.identified[0][:4] == b"RIFF"
    i = fake.sent.index(b"restart:el")
    assert fake.sent[:i] == [VOICED, VOICED]        # the automatic stream heard the first chunks
    replay = fake.sent[i + 1:-1]
    assert b"".join(replay) == VOICED * 3           # every buffered chunk, the trigger included
    assert all(len(p) == 6400 for p in replay[:-1]) # in 200 ms pieces
    assert fake.sent[-1] == VOICED                  # then the live feed resumes
    assert fake.endpoints == 1                      # the stop right after a replay waits for its final


def test_a_covered_language_releases_the_held_finals_without_a_restart(temp_db, monkeypatch):
    fake = LockFakeSTT(LanguageGuess("en", 0.92, "hello there friend"))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["hello there"]
        _voiced(ws, 3)
        assert ws.receive_json() == {"type": "final", "text": "hello there"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "the end"}
    assert fake.restarts == [] and len(fake.identified) == 1
    assert fake.sent == [VOICED, VOICED, VOICED]


def test_three_unsure_answers_release_the_held_finals(temp_db, monkeypatch):
    fake = LockFakeSTT(LanguageGuess("el", 0.3, "κάτι"))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["one"]
        _voiced(ws, 7)                               # attempts at 1.2, 2.2 and 3.2 s of voiced audio
        assert ws.receive_json() == {"type": "final", "text": "one"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "the end"}
    assert len(fake.identified) == 3 and fake.restarts == []


def test_a_covered_language_stays_at_the_lower_confidence(temp_db, monkeypatch):
    """Accented English read as English at 0.65: the automatic stream already
    covers it, so the held words go out at once instead of after three tries."""
    fake = LockFakeSTT(LanguageGuess("en", 0.65, "hello"))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["hello"]
        _voiced(ws, 3)
        assert ws.receive_json() == {"type": "final", "text": "hello"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "the end"}
    assert len(fake.identified) == 1 and fake.restarts == []


def test_a_menu_language_the_stream_lacks_moves_at_the_lower_confidence(temp_db, monkeypatch):
    """The first clip of continuous Greek answers Greek at about 0.75: that
    moves the stream, since a wrong move can only reach Greek here."""
    fake = LockFakeSTT(LanguageGuess("el", 0.72, "καλησπέρα σας"))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 3)
        fake.finals = ["Καλησπέρα σας"]
        _voiced(ws, 1)
        assert ws.receive_json() == {"type": "final", "text": "Καλησπέρα σας"}
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert fake.restarts == ["el"] and len(fake.identified) == 1


def test_a_second_request_in_flight_answers_first_and_locks(temp_db, monkeypatch):
    """A slow first identification does not hold the lock: a second request
    goes out a second of voice later, its answer lands first and moves the
    stream, and the first is dropped."""
    fake = LockFakeSTT(LanguageGuess("el", 0.95, "καλησπέρα σας λέω"))
    fake.identify_delays = [5.0, 0.0]
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        _voiced(ws, 2)                               # the first request, slow
        _voiced(ws, 2)                               # the second at 2.8 s voiced, answered at once
        _voiced(ws, 1)                               # its answer is in: the restart and the replay
        fake.finals = ["Καλησπέρα σας λέω"]
        _voiced(ws, 1)                               # locked: forwarded as it comes
        assert ws.receive_json() == {"type": "final", "text": "Καλησπέρα σας λέω"}
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert fake.restarts == ["el"] and len(fake.identified) == 2


def test_a_language_the_menu_does_not_offer_never_moves_the_stream(temp_db, monkeypatch):
    """A confident Polish guess (accented speech, say) keeps the automatic
    stream: the lock moves only to a language the dictation menu offers."""
    fake = LockFakeSTT(LanguageGuess("pl", 0.9, "dzień dobry państwu"))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["jen dobry"]
        _voiced(ws, 3)
        assert ws.receive_json() == {"type": "final", "text": "jen dobry"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "the end"}
    assert len(fake.identified) == 1 and fake.restarts == []


def test_wordless_answers_spend_requests_not_attempts(temp_db, monkeypatch):
    """Room noise over the floor: an answer with no words keeps the lock open
    for the first real words, each request a second of voice apart, until the
    request budget is gone; then the stream stays."""
    fake = LockFakeSTT(LanguageGuess("", 0.0, ""))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["one"]
        _voiced(ws, 12)                              # requests a second of voice apart, five in all
        assert ws.receive_json() == {"type": "final", "text": "one"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "the end"}
    assert len(fake.identified) == 5 and fake.restarts == []


def test_a_provider_that_identifies_in_stream_is_never_asked(temp_db, monkeypatch):
    class SelfDetecting(LockFakeSTT):
        multi_languages = None

    fake = SelfDetecting(LanguageGuess("el", 0.95, "καλησπέρα σας λέω"))
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["Καλησπέρα"]
        _voiced(ws, 3)
        assert ws.receive_json() == {"type": "final", "text": "Καλησπέρα"}
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert fake.identified == [] and fake.restarts == []


def test_a_failed_restart_reopens_the_automatic_stream_and_replays(temp_db, monkeypatch):
    """The Greek stream refused: the automatic stream is reopened and the
    buffered audio replayed into it (the old stream's unfinished window died
    with it), the held reading dropped since the replay re-delivers it."""
    fake = LockFakeSTT(LanguageGuess("el", 0.95, "καλησπέρα σας λέω"))
    fake.restart_fails = {"el"}
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["kalispera"]
        _voiced(ws, 3)
        fake.finals = ["kalispera sas"]
        _voiced(ws, 1)
        assert ws.receive_json() == {"type": "final", "text": "kalispera sas"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "the end"}
    assert fake.restarts == ["el", "multi"]
    i = fake.sent.index(b"restart:multi")
    assert b"".join(fake.sent[i + 1:-1]) == VOICED * 3


def test_partials_show_while_the_lock_holds_the_finals(temp_db, monkeypatch):
    """The identification still running: the automatic stream's partials
    reach the composer at once (a partial is replaced by the next, never
    committed), the finals wait for the answer."""
    fake = LockFakeSTT(LanguageGuess("en", 0.9, "hello there"), identify_delay=5.0)
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.interims = ["hel", "hello"]
        fake.finals = ["hello there"]
        _voiced(ws, 2)
        assert ws.receive_json() == {"type": "interim", "text": "hel"}
        assert ws.receive_json() == {"type": "interim", "text": "hello"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "hello there the end"}


def test_the_silence_before_the_first_word_is_neither_identified_nor_replayed(temp_db, monkeypatch):
    """A pause before speaking: only a 0.3 s pre-roll of it reaches the
    identification request and the replay, so neither waits on dead air."""
    fake = LockFakeSTT(LanguageGuess("el", 0.95, "καλησπέρα σας λέω"))
    client = _lock_client(monkeypatch, fake)
    silence = bytes(3200)                            # 0.1 s at 16 kHz
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        for _ in range(10):
            ws.send_bytes(silence)                   # a second of nothing
        _voiced(ws, 3)                               # the request, the answer, the restart
        _voiced(ws, 1)                               # locked: the live feed resumes
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert len(fake.identified) == 1
    assert len(fake.identified[0]) == 44 + 3 * 3200 + 2 * len(VOICED)   # the pre-roll, two voiced chunks
    i = fake.sent.index(b"restart:el")
    assert b"".join(fake.sent[i + 1:-1]) == bytes(3 * 3200) + VOICED * 3


def test_both_restarts_failing_sends_the_held_words_before_the_error(temp_db, monkeypatch):
    fake = LockFakeSTT(LanguageGuess("el", 0.95, "καλησπέρα σας λέω"))
    fake.restart_fails = {"el", "multi"}
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["kalispera"]
        _voiced(ws, 3)
        assert ws.receive_json() == {"type": "final", "text": "kalispera"}
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == "provider"


def test_a_stop_during_the_hold_sends_one_final_with_the_held_words_first(temp_db, monkeypatch):
    """The client resolves on the first final after a stop and closes, so
    the held words and the stream's own tail travel in ONE frame."""
    fake = LockFakeSTT(LanguageGuess("el", 0.95, "καλησπέρα σας λέω"), identify_delay=5.0)
    client = _lock_client(monkeypatch, fake)
    with client.websocket_connect("/ws/audio/stt") as ws:
        _open_auto(ws)
        fake.finals = ["held words"]
        _voiced(ws, 2)                               # the identification is still running
        fake.finals = ["more held"]
        _voiced(ws, 1)
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "held words more held the end"}
    assert len(fake.identified) == 1 and fake.restarts == []
