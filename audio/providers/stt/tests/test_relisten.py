"""The second listen (``relisten.py``): the stretches, the speech check, the
cuts, the guards, the order and the bounds; dictation's FIFO and a call's
turn.

The ring's audio encodes its own time (every 0.1 s block holds the sample
``1000 + block``), so the fake speech model and the fake batch endpoint know
which stretch, or which cut, they were handed; silence is zeros.
"""

from __future__ import annotations

import asyncio
import io
import unicodedata
import wave

import numpy as np
import pytest
from audio.providers.stt import relisten as rl
from audio.providers.stt.base import TranscriptResult, Word
from audio.providers.stt.relisten import (
    AudioRing,
    Relisten,
    SpeechCheck,
    TurnRelisten,
    loud_seconds,
    uncovered,
)

RATE = 16000


def _norm(word: str) -> str:
    bare = unicodedata.normalize("NFD", word.strip(".,;:!?").casefold())
    return "".join(c for c in bare if unicodedata.category(c) != "Mn")


def _audio(seconds: float, *, silent: tuple[tuple[float, float], ...] = ()) -> bytes:
    """``seconds`` of time-coded audio; ``silent`` spans are zeros."""
    out = np.zeros(int(seconds * RATE), dtype=np.int16)
    for i in range(0, len(out), RATE // 10):
        t = i / RATE
        if not any(a <= t < b for a, b in silent):
            out[i:i + RATE // 10] = 1000 + i // (RATE // 10)
    return out.tobytes()


def _time_of(sample: float) -> float | None:
    value = int(round(sample))
    return None if value < 1000 else (value - 1000) / 10


class FakeModel:
    """Silero's surface: speech where the test says, by the coded time."""

    window_size_samples = 512

    def __init__(self, speech):
        self.speech = speech

    def reset(self):
        pass

    def process(self, window):
        t = _time_of(window[0] * 32768)
        return 0.9 if t is not None and any(a <= t < b for a, b in self.speech) else 0.05


class FakeBatch:
    """The batch endpoint: answers each cut with the words of a script that
    lie in it (absolute times), relative to the cut's start; ``answer`` can
    shape the answer per cut (by its length), ``delay`` slows it down."""

    def __init__(self, words: list[tuple[str, float, float]], *, answer=None, delay=0.0, fail=None, delay_for=None):
        self.words = words
        self.answer = answer
        self.delay = delay
        self.delay_for = delay_for
        self.fail = fail
        self.cuts: list[tuple[float, float]] = []
        self.active = 0
        self.peak = 0

    async def __call__(self, audio: bytes) -> TranscriptResult:
        with wave.open(io.BytesIO(audio)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        start = _time_of(pcm[0]) if len(pcm) else 0.0
        length = len(pcm) / RATE
        self.cuts.append((round(start, 2), round(length, 2)))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            delay = self.delay_for(start, length) if self.delay_for is not None else self.delay
            if delay:
                await asyncio.sleep(delay)
            if self.fail is not None and self.fail(length):
                raise RuntimeError("the vendor said no")
            words = [(w, s, e) for w, s, e in self.words if start <= s < start + length]
            if self.answer is not None:
                words = self.answer(length, words)
            return TranscriptResult(text=" ".join(w for w, _, _ in words), language="el", audio_seconds=length,
                                    words=[Word(w, s - start, e - start) for w, s, e in words])
        finally:
            self.active -= 1


def _relisten(batch, *, speech, audio: bytes, stream_seconds=None):
    released: list[str] = []
    r = Relisten(transcribe=batch, release=released.append, norm=_norm, rate=RATE,
                 speech=SpeechCheck(RATE, model_factory=lambda _rate: FakeModel(speech)),
                 stream_seconds=stream_seconds)
    r.ring.append(audio)
    return r, released


async def _settled(r: Relisten, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while r.holding:
        assert asyncio.get_running_loop().time() < deadline, "still holding"
        await asyncio.sleep(0.01)


# ── The pieces ──────────────────────────────────────────────────────

def test_uncovered_finds_what_no_word_covers():
    words = [Word("a", 0.5, 1.0), Word("b", 1.0, 1.5), Word("c", 1.7, 2.0)]
    assert uncovered(0.0, 4.0, words) == [(0.0, 0.5), (2.0, 4.0)]
    assert uncovered(0.5, 2.1, words) == []                       # an edge shorter than the floor
    assert uncovered(3.0, 5.0, []) == [(3.0, 5.0)]


def test_the_ring_keeps_the_last_seconds_on_the_connections_clock():
    ring = AudioRing(RATE, keep_s=2.0)
    ring.append(_audio(3.0))
    assert ring.end_s == pytest.approx(3.0)
    samples, at = ring.samples(0.0, 3.0)
    assert at == pytest.approx(1.0) and len(samples) == 2 * RATE   # the first second fell out
    audio, at, seconds = ring.wav(1.5, 2.5)
    assert at == pytest.approx(1.5) and seconds == pytest.approx(1.0) and audio[:4] == b"RIFF"


def test_loudness_counts_the_loud_blocks():
    pcm = np.frombuffer(_audio(2.0, silent=((0.0, 1.0),)), dtype=np.int16)
    assert loud_seconds(pcm, RATE) == pytest.approx(1.0)


def test_the_speech_check_reads_after_its_context():
    check = SpeechCheck(RATE, model_factory=lambda _r: FakeModel([(1.0, 2.0)]))
    pcm = np.frombuffer(_audio(3.0), dtype=np.int16)
    assert check.seconds(pcm[:RATE], pcm[RATE:2 * RATE]) == pytest.approx(1.0, abs=0.07)
    assert check.seconds(pcm[:RATE], pcm[2 * RATE:]) == 0.0
    assert SpeechCheck(48000).usable is False


# ── A window ────────────────────────────────────────────────────────

async def test_an_empty_window_with_speech_comes_back_in_its_place():
    batch = FakeBatch([("ένα", 2.2, 2.6), ("δύο", 2.6, 3.0), ("τρία", 3.1, 3.5)])
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    assert released == [] and r.holding
    await _settled(r)
    assert released == ["ένα δύο τρία"]
    assert r.seconds > 0 and batch.cuts[0] == (1.5, 3.0)          # 0.5 s each side of the gap


async def test_a_quiet_window_is_released_at_once():
    batch = FakeBatch([])
    r, released = _relisten(batch, speech=[], audio=_audio(5.0, silent=((2.0, 4.0),)))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    r.window(start=4.0, end=5.0, text="τέλος", words=[Word("τέλος", 4.1, 4.9)], shown=[], enabled=True)
    assert released == ["τέλος"] and not r.holding and batch.cuts == []


async def test_noise_is_no_gap_and_costs_no_cut():
    batch = FakeBatch([("θόρυβος", 2.2, 2.6)])
    r, released = _relisten(batch, speech=[], audio=_audio(5.0))     # loud, but no speech in it
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == [] and batch.cuts == [] and r.seconds == 0.0


async def test_a_window_the_gate_keeps_out_is_never_held():
    batch = FakeBatch([("ένα", 2.2, 2.6)])
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=False)
    assert not r.holding and batch.cuts == []


async def test_a_head_gap_lands_before_the_finals_own_words():
    # The interim showed the head, the final kept only the tail.
    batch = FakeBatch([("είναι", 0.2, 0.6), ("σωστή", 0.6, 1.0), ("ερώτηση", 1.0, 1.5), ("μετά", 2.0, 2.4)])
    r, released = _relisten(batch, speech=[(0.0, 3.0)], audio=_audio(4.0))
    r.window(start=0.0, end=3.0, text="μετά πήγαμε",
             words=[Word("μετά", 2.0, 2.4), Word("πήγαμε", 2.4, 2.9)], shown=[], enabled=True)
    await _settled(r)
    assert released == ["είναι σωστή ερώτηση μετά πήγαμε"]


async def test_a_later_final_waits_behind_a_held_window():
    batch = FakeBatch([("ένα", 2.2, 2.6), ("δύο", 2.6, 3.0)], delay=0.2)
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(6.0))
    r.window(start=0.0, end=2.0, text="πριν", words=[Word("πριν", 0.1, 1.9)], shown=[], enabled=True)
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    r.window(start=4.0, end=6.0, text="μετά", words=[Word("μετά", 4.1, 5.9)], shown=[], enabled=True)
    assert released == ["πριν"] and r.held_text() == "μετά"
    await _settled(r)
    assert released == ["πριν", "ένα δύο", "μετά"]


async def test_the_shown_words_stay_on_screen_and_are_the_fallback():
    batch = FakeBatch([])                                          # the batch hears nothing either
    shown = [Word("είναι", 2.2, 2.6), Word("σωστή", 2.6, 3.0)]
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=shown, enabled=True)
    assert r.held_text() == "είναι σωστή"
    await _settled(r)
    assert released == ["είναι σωστή"] and len(batch.cuts) == 3    # the two cuts, then the last


async def test_the_second_cut_wins_when_the_first_hears_nothing():
    words = [("ένα", 2.2, 2.6), ("δύο", 2.6, 3.0)]
    batch = FakeBatch(words, answer=lambda length, ws: [] if length < 3.5 else ws)
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(8.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == ["ένα δύο"] and sorted(c[1] for c in batch.cuts) == [3.0, 5.0]


async def test_the_last_cut_runs_only_when_neither_found_a_word():
    words = [("ένα", 2.2, 2.6), ("δύο", 2.6, 3.0)]
    batch = FakeBatch(words, answer=lambda length, ws: ws if length == pytest.approx(4.8) else [])
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(8.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == ["ένα δύο"] and len(batch.cuts) == 3 and batch.cuts[-1] == (1.7, 4.8)


async def test_a_cut_that_fails_leaves_the_other():
    words = [("ένα", 2.2, 2.6)]
    batch = FakeBatch(words, fail=lambda length: length < 3.5)
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(8.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == ["ένα"]


async def test_a_slow_cut_times_out_and_the_window_goes_with_its_shown_words(monkeypatch):
    monkeypatch.setattr(rl, "RELISTEN_TIMEOUT_S", 0.05)
    batch = FakeBatch([("ένα", 2.2, 2.6)], delay=1.0)
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(8.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[Word("ένας", 2.2, 2.6)], enabled=True)
    await _settled(r)
    assert released == ["ένας"]
    assert r.seconds > 0                                           # every cut sent is billed


async def test_the_hold_bound_releases_whatever_the_relisten_does(monkeypatch):
    monkeypatch.setattr(rl, "RELISTEN_HOLD_MAX_S", 0.1)
    batch = FakeBatch([("ένα", 2.2, 2.6)], delay=2.0)
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(8.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[Word("ένας", 2.2, 2.6)], enabled=True)
    r.window(start=4.0, end=6.0, text="μετά", words=[Word("μετά", 4.1, 5.9)], shown=[], enabled=True)
    await asyncio.sleep(0.3)
    assert released == ["ένας", "μετά"]


# ── The guards ──────────────────────────────────────────────────────

async def test_a_neighbours_word_the_batch_retimed_is_not_added_twice():
    # The previous window committed "δύο" at 1.8-2.1; the cut hears it again
    # 0.15 s later, inside the gap.
    batch = FakeBatch([("δύο", 1.95, 2.25), ("τρία", 2.4, 2.8), ("τέσσερα", 2.9, 3.4)])
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.window(start=0.0, end=2.0, text="ένα δύο", words=[Word("ένα", 1.0, 1.5), Word("δύο", 1.8, 2.1)],
             shown=[], enabled=True)
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == ["ένα δύο", "τρία τέσσερα"]


async def test_the_word_the_live_boundary_cut_comes_back():
    # "φύγαμε" began just before the window: the live stream dropped it at
    # the boundary, the batch puts it at the cut's start.
    batch = FakeBatch([("φύγαμε", 1.5, 2.3), ("νωρίς", 2.3, 2.7)])
    r, released = _relisten(batch, speech=[(1.5, 4.0)], audio=_audio(5.0))
    r.window(start=0.0, end=2.0, text="εβδομάδα", words=[Word("εβδομάδα", 1.0, 1.6)], shown=[], enabled=True)
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == ["εβδομάδα", "φύγαμε νωρίς"]


async def test_a_word_starting_after_the_window_belongs_to_the_next():
    batch = FakeBatch([("ένα", 2.2, 2.6), ("δύο", 3.9, 4.3), ("τρία", 4.05, 4.4)])
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(6.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == ["ένα δύο"]


async def test_a_released_window_drops_the_copy_of_a_cut_word():
    batch = FakeBatch([("ένα", 2.2, 2.6), ("δύο", 2.6, 3.0)])
    r, released = _relisten(batch, speech=[(2.0, 3.6)], audio=_audio(6.0, silent=((3.6, 4.0),)))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    r.window(start=4.0, end=6.0, text="δύο τρία", words=[Word("δύο", 2.95, 4.2), Word("τρία", 4.3, 4.8)],
             shown=[], enabled=True)
    await _settled(r)
    assert released == ["ένα δύο", "τρία"]


# ── The bounds ──────────────────────────────────────────────────────

async def test_settle_waits_then_releases_everything():
    batch = FakeBatch([("ένα", 2.2, 2.6)], delay=5.0)
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[Word("ένας", 2.2, 2.6)], enabled=True)
    await r.settle(0.1)
    assert released == ["ένας"] and not r.holding


async def test_cancel_drops_what_is_held():
    batch = FakeBatch([("ένα", 2.2, 2.6)], delay=5.0)
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    r.cancel()
    await asyncio.sleep(0.05)
    assert released == [] and not r.holding


async def test_a_burst_of_windows_queues_beyond_the_active_limit():
    words = [(f"λέξη{i}", 2.0 * i + 0.3, 2.0 * i + 0.7) for i in range(6)]
    batch = FakeBatch(words, delay=0.05)
    r, released = _relisten(batch, speech=[(0.0, 12.0)], audio=_audio(13.0))
    for i in range(6):
        r.window(start=2.0 * i, end=2.0 * i + 2.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    assert released == [f"λέξη{i}" for i in range(6)]
    assert batch.peak <= rl.RELISTEN_MAX_ACTIVE


async def test_a_partial_relisten_keeps_the_shown_words_it_did_not_hear_again():
    batch = FakeBatch([("ένα", 2.2, 2.6)])
    shown = [Word("ένα", 2.2, 2.6), Word("δύο", 2.6, 3.0), Word("τρία", 3.0, 3.4)]
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.window(start=2.0, end=4.0, text="", words=[], shown=shown, enabled=True)
    await _settled(r)
    assert released == ["ένα δύο τρία"]                          # painted words never die


async def test_a_punctuation_token_rides_with_its_word_in_the_merge():
    batch = FakeBatch([("μέση", 1.0, 1.4)])
    r, released = _relisten(batch, speech=[(0.6, 1.8)], audio=_audio(4.0, silent=((2.4, 4.0),)))
    r.window(start=0.0, end=4.0, text="αρχή , τέλος", words=[Word("αρχή", 0.1, 0.5), Word("τέλος", 1.9, 2.3)],
             shown=[], enabled=True)
    await _settled(r)
    assert released == ["αρχή , μέση τέλος"]


# ── The fix round ───────────────────────────────────────────────────

async def test_a_window_out_of_time_keeps_the_words_its_first_gap_brought_back(monkeypatch):
    monkeypatch.setattr(rl, "RELISTEN_HOLD_MAX_S", 0.4)
    # Two gaps in one window; the second gap's cuts are slow.
    batch = FakeBatch([("ένα", 0.3, 0.7), ("τρία", 4.2, 4.6)], delay_for=lambda start, length: 3.0 if start + length > 4.0 else 0.0)
    r, released = _relisten(batch, speech=[(0.0, 6.0)], audio=_audio(7.0))
    r.window(start=0.0, end=6.0, text="δύο", words=[Word("δύο", 2.0, 3.0)], shown=[], enabled=True)
    await asyncio.sleep(0.8)
    assert released == ["ένα δύο"]                               # the first gap's word kept at the bound
    assert {round(c[0]) for c in batch.cuts} >= {0, 2}            # both gaps went out together


async def test_a_final_during_the_close_is_released_and_never_listened_to_again():
    batch = FakeBatch([("ένα", 2.2, 2.6)])
    r, released = _relisten(batch, speech=[(2.0, 4.0)], audio=_audio(5.0))
    r.close()
    r.window(start=2.0, end=4.0, text="τέλος", words=[Word("τέλος", 3.5, 3.9)], shown=[], enabled=True)
    await asyncio.sleep(0.05)
    assert released == ["τέλος"] and not r.holding
    assert batch.cuts == [] and r.seconds == 0.0


async def test_the_batch_seconds_stay_within_the_streams_plus_ten(caplog):
    caplog.set_level("INFO", logger="audio.providers.stt.relisten")
    # The first gap's 3 s cut hears nothing, so its 5 s cut runs: 8 of 10 s
    # spent, and the second window's 3 s cut no longer fits.
    batch = FakeBatch([("ένα", 2.2, 2.6), ("δύο", 6.2, 6.6)], answer=lambda length, ws: [] if length < 3.5 else ws)
    r, released = _relisten(batch, speech=[(2.0, 4.0), (6.0, 8.0)], audio=_audio(9.0), stream_seconds=lambda: 0.0)
    r.window(start=2.0, end=4.0, text="", words=[], shown=[], enabled=True)
    await _settled(r)
    r.window(start=6.0, end=8.0, text="", words=[], shown=[Word("δύο", 6.2, 6.6)], enabled=True)
    await _settled(r)
    assert released == ["ένα", "δύο"]                              # the second from its shown words
    assert r.seconds == pytest.approx(8.0)
    assert [length for _, length in batch.cuts] == [3.0, 5.0]       # one cut at a time, the first gap's only
    assert sum("budget is spent" in rec.message for rec in caplog.records) == 1


def test_a_speech_model_that_fails_to_load_is_remembered_once(monkeypatch, caplog):
    monkeypatch.setattr(rl, "_FAILED", set())
    monkeypatch.setattr(rl, "_MODELS", {})

    class Broken:
        def __init__(self, rate):
            raise OSError("libgomp.so.1: cannot open shared object file")
    import audio.providers.vad.silero_model as silero
    monkeypatch.setattr(silero, "SileroModel", Broken)
    pcm = np.frombuffer(_audio(1.0), dtype=np.int16)
    check = SpeechCheck(RATE)
    assert check.usable
    assert check.seconds(pcm[:100], pcm) == 0.0
    assert check.seconds(pcm[:100], pcm) == 0.0
    assert not check.usable and not SpeechCheck(RATE).usable
    assert sum("did not load" in rec.message for rec in caplog.records) == 1


def test_the_streams_share_one_speech_model_per_rate(monkeypatch):
    monkeypatch.setattr(rl, "_FAILED", set())
    monkeypatch.setattr(rl, "_MODELS", {})
    built = []
    import audio.providers.vad.silero_model as silero

    class Counted(FakeModel):
        def __init__(self, rate):
            built.append(rate)
            super().__init__([])
    monkeypatch.setattr(silero, "SileroModel", Counted)
    pcm = np.frombuffer(_audio(1.0), dtype=np.int16)
    SpeechCheck(RATE).seconds(pcm[:100], pcm)
    SpeechCheck(RATE).seconds(pcm[:100], pcm)
    assert built == [RATE]


def test_the_speech_check_at_8_khz_runs_on_the_audio_upsampled():
    rates = []

    def factory(rate):
        rates.append(rate)
        return FakeModel([(0.2, 0.8)])
    rate8 = 8000
    coded = np.zeros(rate8, dtype=np.int16)
    for i in range(0, rate8, rate8 // 10):
        coded[i:i + rate8 // 10] = 1000 + i // (rate8 // 10)
    check = SpeechCheck(rate8, model_factory=factory)
    assert check.usable
    assert check.seconds(coded[:0], coded) == pytest.approx(0.6, abs=0.07)
    assert rates == [16000]


# ── A call's turn ───────────────────────────────────────────────────

def _turn(batch, *, speech, audio: bytes, stream_seconds=None) -> TurnRelisten:
    t = TurnRelisten(transcribe=batch, norm=_norm, rate=RATE,
                     speech=SpeechCheck(RATE, model_factory=lambda _rate: FakeModel(speech)),
                     stream_seconds=stream_seconds, label="el")
    t.ring.append(audio)
    return t


async def test_a_turn_with_no_empty_window_waits_for_nothing():
    batch = FakeBatch([("δύο", 2.2, 2.6)])
    t = _turn(batch, speech=[(0.0, 4.0)], audio=_audio(5.0))
    t.window(start=0.0, end=2.0, text="ένα", words=[Word("ένα", 0.2, 0.6)], enabled=True)
    t.window(start=2.0, end=4.0, text="δύο", words=[Word("δύο", 2.2, 2.6)], enabled=True)
    assert await t.complete("ένα δύο", 2.0) == "ένα δύο"
    assert batch.cuts == [] and t.seconds == 0.0


async def test_an_empty_window_over_speech_is_heard_again_at_once_and_spliced_by_time():
    batch = FakeBatch([("δύο", 2.2, 2.6), ("τρία", 2.8, 3.2)])
    t = _turn(batch, speech=[(0.0, 6.0)], audio=_audio(7.0))
    t.window(start=0.0, end=2.0, text="ένα", words=[Word("ένα", 0.2, 0.6)], enabled=True)
    t.window(start=2.0, end=4.0, text="", words=[], enabled=True)
    t.window(start=4.0, end=6.0, text="τέσσερα", words=[Word("τέσσερα", 4.2, 4.8)], enabled=True)
    await asyncio.sleep(0.05)
    assert batch.cuts                                          # started before the take
    assert await t.complete("ένα τέσσερα", 2.0) == "ένα δύο τρία τέσσερα"
    assert await t.complete("ένα τέσσερα", 2.0) == "ένα τέσσερα"   # once


async def test_a_turn_text_unlike_its_finals_gets_the_words_after_the_final_before_them():
    batch = FakeBatch([("δύο", 2.2, 2.6)])
    t = _turn(batch, speech=[(0.0, 6.0)], audio=_audio(7.0))
    t.window(start=0.0, end=2.0, text="ένα", words=[Word("ένα", 0.2, 0.6)], enabled=True)
    t.window(start=2.0, end=4.0, text="", words=[], enabled=True)
    # The pipeline dispatched a live partial in the last final's place.
    assert await t.complete("ένα και πέντε", 2.0) == "ένα δύο και πέντε"


async def test_the_turn_waits_at_most_its_bound_and_the_take_cancels_the_rest():
    batch = FakeBatch([("δύο", 2.2, 2.6)], delay=1.0)
    t = _turn(batch, speech=[(0.0, 4.0)], audio=_audio(5.0))
    t.window(start=0.0, end=2.0, text="ένα", words=[Word("ένα", 0.2, 0.6)], enabled=True)
    t.window(start=2.0, end=4.0, text="", words=[], enabled=True)
    began = asyncio.get_running_loop().time()
    assert await t.complete("ένα", 0.2) == "ένα"
    assert asyncio.get_running_loop().time() - began < 0.5
    t.take()
    await asyncio.sleep(0.05)
    assert batch.active == 0                                   # the cut still out was cancelled
    assert await t.complete("τρία", 2.0) == "τρία"           # the next turn starts empty


async def test_a_quiet_window_or_one_the_gate_keeps_out_is_never_heard_again():
    batch = FakeBatch([("δύο", 2.2, 2.6)])
    t = _turn(batch, speech=[(0.0, 4.0)], audio=_audio(5.0, silent=((2.0, 4.0),)))
    t.window(start=2.0, end=4.0, text="", words=[], enabled=True)     # silence: the loudness floor
    t.window(start=0.0, end=2.0, text="", words=[], enabled=False)    # the gate
    await asyncio.sleep(0.05)
    assert batch.cuts == []


async def test_a_wordless_turn_gives_its_words_once_as_its_final():
    batch = FakeBatch([("δύο", 0.4, 0.8)])
    t = _turn(batch, speech=[(0.0, 2.0)], audio=_audio(3.0))
    t.window(start=0.0, end=2.0, text="", words=[], enabled=True)
    assert t.wordless
    assert await t.wait_wordless(2.0) == "δύο"
    assert await t.wait_wordless(2.0) is None
    assert await t.complete("", 2.0) == ""                     # never spliced again


async def test_a_calls_batch_seconds_stay_within_its_budget(caplog):
    caplog.set_level("INFO", logger="audio.providers.stt.relisten")
    batch = FakeBatch([("δύο", 2.2, 2.6)], answer=lambda length, ws: [])
    t = _turn(batch, speech=[(0.0, 12.0)], audio=_audio(13.0), stream_seconds=lambda: 0.0)
    t.window(start=2.0, end=4.0, text="", words=[], enabled=True)
    await asyncio.sleep(0.1)
    t.window(start=8.0, end=10.0, text="", words=[], enabled=True)
    await asyncio.sleep(0.1)
    assert t.seconds <= 10.0
    assert sum("budget is spent" in rec.message for rec in caplog.records) == 1


async def test_two_empty_windows_in_a_row_never_write_their_seam_twice():
    # Each cut hears the word at the seam, with timings far enough apart
    # that the guards against live words cannot tell (the batch drifts).
    script = [("ρωτήσω", 2.2, 2.6), ("σχετικά", 3.6, 3.95), ("σχετικά", 4.05, 4.4), ("με", 4.5, 4.7)]

    def by_cut(length, ws):
        if length == pytest.approx(3.0):
            return [w for w in ws if w[1] < 4.0]                # the first window's cut
        return [w for w in ws if w[1] >= 4.0]                    # the second's
    batch = FakeBatch(script, answer=by_cut)
    t = _turn(batch, speech=[(0.0, 6.0)], audio=_audio(7.0))
    t.window(start=0.0, end=2.0, text="ήθελα να", words=[Word("ήθελα", 0.2, 0.6), Word("να", 0.7, 0.9)], enabled=True)
    t.window(start=2.0, end=4.0, text="", words=[], enabled=True)
    t.window(start=4.0, end=5.0, text="", words=[], enabled=True)
    assert await t.complete("ήθελα να", 2.0) == "ήθελα να ρωτήσω σχετικά με"


async def test_a_wordless_turns_re_listen_that_misses_its_bound_never_opens_the_next_turn():
    batch = FakeBatch([("δύο", 0.4, 0.8)], delay=0.5)
    t = _turn(batch, speech=[(0.0, 2.0)], audio=_audio(3.0))
    t.window(start=0.0, end=2.0, text="", words=[], enabled=True)
    assert await t.wait_wordless(0.1) is None                 # out of time: cancelled
    await asyncio.sleep(0.6)
    assert batch.active == 0
    assert await t.complete("τρία", 2.0) == "τρία"           # nothing late rides in
