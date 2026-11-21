"""The feed of the speech detector on a call and in a voice conversation.

Every detector gets each window with the audio just before it, as its model
expects. The pipeline and the voice session say nothing about the feed: the
model has one. No setting chooses, and the rows two earlier builds stored
are not read.
"""

from __future__ import annotations

import logging
from pathlib import Path

import line_audio
import numpy as np
import pytest
from pipeline_fakes import FakeConn, FakeLLM, FakeSTT, FakeTTS, FakeVAD

from config_manager import ConfigManager

_PHONE = Path(__file__).resolve().parents[1]
SPEECH_WAV = _PHONE.parent / "audio" / "providers" / "lid" / "tests" / "fixtures" / "greek-8k.wav"
AMBIENCE = _PHONE / "assets" / "ambience"


def _settings(cfg: ConfigManager, **settings: str) -> ConfigManager:
    cfg.load({"settings": dict(settings), "routes": []})
    return cfg


@pytest.mark.asyncio
async def test_a_call_says_nothing_about_the_feed_whatever_is_stored(make_pipeline, caplog):
    """A call builds its detector with no word about the feed. The rows two
    earlier builds stored are not read, and the call's first line names no
    feed."""
    plain = make_pipeline(cfg=_settings(ConfigManager()), conn=FakeConn(), llm=FakeLLM())
    stored = make_pipeline(cfg=_settings(ConfigManager(), vad_context="off"), conn=FakeConn(), llm=FakeLLM())
    voice_on = make_pipeline(cfg=_settings(ConfigManager(), voice_vad_context="on"), conn=FakeConn(), llm=FakeLLM())
    voice_off = make_pipeline(cfg=_settings(ConfigManager(), voice_vad_context="off"), conn=FakeConn(), llm=FakeLLM())
    for p in (plain, stored, voice_on, voice_off):
        assert "context" not in p.vad._vad.kwargs
    assert not hasattr(ConfigManager(), "vad_context") and not hasattr(ConfigManager(), "voice_vad_context")
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await stored.run()
    started = [r.getMessage() for r in caplog.records if "Pipeline started" in r.getMessage()]
    assert started == ["[test:0] Pipeline started (agent=unified, outbound=False)"]


@pytest.mark.parametrize("stored", [{"vad_context": "off"}, {"voice_vad_context": "off"}, {"voice_vad_context": "on"}])
def test_a_calls_detector_reads_each_window_with_the_audio_before_it(monkeypatch, stored):
    """The real detector, as a call builds it: 32 samples of context at a
    line's rate, whatever an earlier build stored."""
    pytest.importorskip("onnxruntime")
    from audio.providers.vad.silero import SileroVad
    from pipeline_fakes import make_route

    from pipeline import CallPipeline, providers as plproviders

    monkeypatch.setattr(plproviders, "get_provider_class",
                        lambda kind, name: FakeSTT if kind == "stt" else FakeTTS)
    p = CallPipeline(conn=FakeConn(), route=make_route(), cfg=_settings(ConfigManager(), **stored),
                     call_manager=None, outbound_call_id=None, llm_backend=FakeLLM(),
                     audiosocket_uuid="uuid-test", caller_info=None)
    assert isinstance(p.vad._vad, SileroVad)
    assert p.vad._vad._model.context_samples == 32


class _VoiceConn:
    """A voice conversation's connection, as the session reads it."""

    call_uuid = "dx-feed"
    peer_addr = "duplex:feed"
    sample_rate_in = 16000
    sample_rate_out = 24000
    frame_bytes_out = 960
    is_closed = False
    on_barge_in = None

    def send_event(self, payload):
        pass


_VOICE_SESSION = {"stt": {"provider_name": "deepgram"}, "tts": {"provider_name": "cartesia"},
                  "language": "en", "chat_endpointing_ms": 1500}


@pytest.mark.parametrize("stored", [{}, {"vad_context": "on"}, {"voice_vad_context": "off"}, {"voice_vad_context": "on"}])
def test_a_voice_conversation_says_nothing_about_the_feed_whatever_is_stored(monkeypatch, stored):
    """A voice conversation builds its detector as a call does, with no word
    about the feed. The rows two earlier builds stored are not read."""
    from duplex import session as dsession
    from pipeline import providers as plproviders

    monkeypatch.setattr(dsession, "SileroVad", FakeVAD)
    monkeypatch.setattr(plproviders, "get_provider_class",
                        lambda kind, name: FakeSTT if kind == "stt" else FakeTTS)
    session = dsession.DuplexSession(_VoiceConn(), _settings(ConfigManager(), **stored), dict(_VOICE_SESSION))
    assert "context" not in session.vad._vad.kwargs
    assert not hasattr(session.cfg, "voice_vad_context")


@pytest.mark.parametrize("stored", [{}, {"voice_vad_context": "off"}])
def test_a_voice_conversations_detector_reads_each_window_with_the_audio_before_it(monkeypatch, stored):
    """The real detector, as a voice conversation builds it: 64 samples of
    context at a microphone's rate, whatever an earlier build stored."""
    pytest.importorskip("onnxruntime")
    from audio.providers.vad.silero import SileroVad

    from duplex import session as dsession
    from pipeline import providers as plproviders

    monkeypatch.setattr(plproviders, "get_provider_class",
                        lambda kind, name: FakeSTT if kind == "stt" else FakeTTS)
    session = dsession.DuplexSession(_VoiceConn(), _settings(ConfigManager(), **stored), dict(_VOICE_SESSION))
    assert isinstance(session.vad._vad, SileroVad)
    assert (session.vad._vad._model.sample_rate, session.vad._vad._model.context_samples) == (16000, 64)


# -- the call's last line: what its detector heard, by loudness ----------------

@pytest.mark.asyncio
async def test_a_call_ends_with_what_its_detector_heard_by_loudness(make_pipeline, caplog):
    """One line after the call closed, numbers only: per band of loudness the
    detector's windows and those over its bar, while it listened and while
    the agent spoke. A call the detector never heard says nothing."""
    from audio.providers.vad.silero import Loudness

    heard = Loudness(listening=((812, 0), (40, 3), (22, 9), (61, 40), (120, 97), (14, 12)),
                     bargein=((300, 0), (0, 0), (5, 1), (2, 2), (0, 0), (0, 0)))
    said = ("[test:0] The detector by loudness, windows/those over its bar. "
            "Listening: under 75 812/0, 75 to 150 40/3, 150 to 300 22/9, 300 to 600 61/40, "
            "600 to 1200 120/97, above 1200 14/12. "
            "While the agent spoke: under 75 300/0, 75 to 150 0/0, 150 to 300 5/1, 300 to 600 2/2, "
            "600 to 1200 0/0, above 1200 0/0")
    for by_loudness, expected in ((lambda: heard, [said]),
                                  (lambda: Loudness(((0, 0),) * 6, ((0, 0),) * 6), []),
                                  (None, [])):
        caplog.clear()
        p = make_pipeline(conn=FakeConn(), llm=FakeLLM())
        if by_loudness is not None:
            p.vad._vad.by_loudness = by_loudness
        with caplog.at_level(logging.INFO, logger="pipeline"):
            await p.run()
        lines = [r.getMessage() for r in caplog.records]
        assert [line for line in lines if "by loudness" in line] == expected
        if expected:
            assert lines.index(said) > next(i for i, line in enumerate(lines) if "call closed" in line)


# -- the guard: a caller's sentence on a line, a room behind it ----------------

#: The sentence starts this long after the detector's last reset, at each of
#: these places in the room's recording: 24 placements a room.
_PAUSES_S = (0.3, 1.0, 3.0, 10.0)
_PLACES_S = (0, 5, 11, 17, 23, 29)
#: Speech must start within this much of the sentence's first sample.
_HEARD_WITHIN_S = 0.5
#: The caller's level and the room's, as RMS on the line: a caller who does
#: not speak up, in a room that is not silent.
_CALLER_RMS, _ROOM_RMS = 600.0, 200.0


class _AudioClock:
    """The detector's clock, moved by the audio fed: its silence timer reads
    the wall clock and a test feeds minutes of line in seconds."""

    t = 1000.0

    def monotonic(self) -> float:
        return self.t


def _call_detector():
    """The detector as a call on a fresh install builds it: the phone
    service's own defaults (the parity test holds them equal to the seed's)
    and the silence the default speech-to-text gives a call."""
    from audio.providers.vad.silero import SileroVad

    cfg = ConfigManager()
    return SileroVad(
        threshold=cfg.vad_threshold, silence_duration_ms=500 + cfg.vad_silence_offset_ms,
        speech_pad_ms=cfg.vad_speech_pad_ms, min_energy_rms=cfg.vad_min_energy_rms,
        bargein_threshold=cfg.bargein_threshold, bargein_debounce_ms=cfg.bargein_debounce_ms,
        bargein_chunk_ratio=cfg.bargein_chunk_ratio,
        bargein_silence_duration_ms=cfg.bargein_silence_duration_ms,
        sample_rate=line_audio.RATE,
    )


def _first_speech_start(vad, clock: _AudioClock, audio: np.ndarray) -> float | None:
    """Seconds into ``audio`` of the detector's first speech start after a
    reset, fed as a line feeds it: 20 ms frames."""
    vad.reset()
    pcm = line_audio.pcm16(audio)
    for i in range(0, len(pcm) - 319, 320):
        clock.t += 0.02
        if vad.process(pcm[i:i + 320]).value == "speech_start":
            return i / 2 / line_audio.RATE
    return None


def sentences_missed(vad, clock: _AudioClock) -> list[tuple[str, float, int, float | None]]:
    """The placements in which the detector did not start speech within
    ``_HEARD_WITHIN_S`` of the sentence's start: no start at all, a late one,
    or one on the room before the caller spoke. Each as (room, pause, place,
    the start it gave)."""
    rate = line_audio.RATE
    sentence = line_audio.line(line_audio.at_voiced_level(line_audio.read_wav(SPEECH_WAV)[: int(2.5 * rate)], _CALLER_RMS))
    missed = []
    for room in ("office", "nature"):
        recording = line_audio.read_wav(AMBIENCE / f"{room}.wav")
        for pause in _PAUSES_S:
            for place in _PLACES_S:
                lead = int(pause * rate)
                stretch = np.take(recording, np.arange(place * rate, place * rate + lead + len(sentence) + rate),
                                  mode="wrap")
                heard = line_audio.line(line_audio.at_level(stretch, _ROOM_RMS))
                heard[lead:lead + len(sentence)] += sentence
                start = _first_speech_start(vad, clock, heard)
                if start is None or not pause <= start <= pause + _HEARD_WITHIN_S:
                    missed.append((room, pause, place, start))
    return missed


# Both tests have room, measured with the helpers above. With the context:
# none of 48 missed, the latest start 0.16 s into the sentence against a bound
# of 0.5, and the room before the sentence never scores two windows in a row
# above 0.065 against a bar of 0.4. On the window alone: 19 missed, 15 of them
# with no start at all. Another CPU moves a score in its fifth decimal.
def test_the_detector_hears_a_callers_first_sentence_on_a_line(monkeypatch):
    """A sentence at a modest level, as a line carries it, with a room behind
    it, at 48 placements: the detector starts speech within half a second of
    its first sample. The real model, the values a fresh install runs on,
    and the feed a call gets: none named. If the model's default ever goes
    back to the window alone, this fails with about four in ten missed."""
    pytest.importorskip("onnxruntime")
    from audio.providers.vad import silero

    clock = _AudioClock()
    monkeypatch.setattr(silero, "time", clock)
    assert sentences_missed(_call_detector(), clock) == []


def test_the_window_alone_loses_callers_on_a_line(monkeypatch):
    """The same 48 placements with nothing in front of each window, the feed
    of an older binding, built in this test only: the detector misses many
    of them. This is why no detector runs on it, and the proof that the test
    above can tell the two feeds apart."""
    pytest.importorskip("onnxruntime")
    from audio.providers.vad import silero
    from audio.providers.vad.silero_model import SileroModel

    class WindowAlone(SileroModel):
        def _input(self, x):
            return x.reshape(1, -1)

    clock = _AudioClock()
    monkeypatch.setattr(silero, "time", clock)
    monkeypatch.setattr(silero, "SileroModel", WindowAlone)
    assert len(sentences_missed(_call_detector(), clock)) >= 10


# -- the second listen's check on a call's audio -------------------------------

def test_the_second_listen_hears_a_callers_sentence_on_a_line_and_not_a_room(monkeypatch):
    """The second listen's own check on the real model, at a call's rate: it
    reads most of a caller's sentence on a line as speech, and none of a
    room with no caller in it. On the window alone the same sentence read
    as 0.6 s of speech, with the audio before each window as 1.9 s of 2."""
    pytest.importorskip("onnxruntime")
    from audio.providers.stt import relisten

    monkeypatch.setattr(relisten, "_FAILED", set())
    monkeypatch.setattr(relisten, "_MODELS", {})
    rate = line_audio.RATE
    lead = int(relisten.SPEECH_CONTEXT_S * rate)

    def heard(x: np.ndarray) -> float:
        pcm = np.frombuffer(line_audio.pcm16(x), dtype="<i2")
        return relisten.SpeechCheck(rate).seconds(pcm[:lead], pcm[lead:lead + 2 * rate])

    sentence = line_audio.line(line_audio.at_voiced_level(line_audio.read_wav(SPEECH_WAV)[: int(2.5 * rate)], _CALLER_RMS))
    room = line_audio.line(line_audio.at_level(line_audio.read_wav(AMBIENCE / "city.wav")[: 3 * rate], 400.0))
    assert heard(np.concatenate([np.zeros(lead), sentence])) >= 1.5
    assert heard(room) < relisten.RELISTEN_MIN_SPEECH_S
