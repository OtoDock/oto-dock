"""A caller's words reach the agent whether or not the VAD starts speech on them.

Two layers. The first drives ``CallPipeline.run()`` frame by frame through
the real listen loop: a PIN gate entered with in-band tones, the greeting,
then a sentence. With the real Silero model (where onnxruntime is installed)
the sentence gets its speech start and its turn. With a VAD that never
starts, the turn still goes out: the provider's final has no speech end to
drain it, and the orphan sweep (``pipeline/turn.py``) takes it. The second
layer pins the sweep's rules on a faked pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import wave

import numpy as np
import pytest
from pipeline_fakes import FakeConn, FakeLLM, make_route

from calls import pin_failures
from calls.pin_failures import PinFailureStore
from config_manager import ConfigManager
from pipeline import core as plcore, turn as plturn
from pipeline.vad_serial import SerializedVad
from telephony.dtmf_detect import _COL_FREQS, _DIGITS, _ROW_FREQS
from transport.base import FRAME_AUDIO, FRAME_HANGUP

pytestmark = pytest.mark.asyncio

RATE = 8000
FRAME = 320                      # one 20 ms AudioSocket frame
SENTENCE = "Hey, hello, how are you?"
SPEECH_WAV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "audio", "providers", "lid", "tests", "fixtures", "greek-8k.wav")


# -- the line's audio ---------------------------------------------------------

def _pcm(samples: np.ndarray) -> bytes:
    return np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes()


def _noise(seconds: float, rms: float, seed: int = 1) -> bytes:
    x = np.random.default_rng(seed).standard_normal(int(seconds * RATE))
    return _pcm(x * rms)


def _pin_tones(digits: str) -> bytes:
    """The keys as a phone sends them in-band: 80 ms of the two tones, then
    a pause."""
    def tone(digit: str) -> bytes:
        for ri, row in enumerate(_DIGITS):
            if digit in row:
                f1, f2 = _ROW_FREQS[ri], _COL_FREQS[row.index(digit)]
        t = np.arange(int(RATE * 0.08)) / RATE
        return _pcm((np.sin(2 * np.pi * f1 * t) + np.sin(2 * np.pi * f2 * t)) * 0.15 * 32767)

    pause = b"\x00\x00" * 800
    return pause.join(tone(d) for d in digits) + pause


def _tones(seconds: float, rms: float = 1500.0) -> bytes:
    """A loud sound that is no speech to the real model: two steady tones."""
    t = np.arange(int(seconds * RATE)) / RATE
    return _pcm((np.sin(2 * np.pi * 300 * t) + np.sin(2 * np.pi * 700 * t)) * rms)


def _speech() -> bytes:
    with wave.open(SPEECH_WAV, "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2)
        return w.readframes(w.getnframes())


def _frames(pcm: bytes) -> list[tuple[int, bytes]]:
    pcm = pcm[: len(pcm) // FRAME * FRAME]
    return [(FRAME_AUDIO, pcm[i:i + FRAME]) for i in range(0, len(pcm), FRAME)]


class LineConn(FakeConn):
    """A line read frame by frame. ``at[i]`` is awaited before frame ``i`` is
    handed out (a cancelled read runs it again: the loop reads through a one
    second ``wait_for``). ``clock`` follows the audio, 20 ms a frame. Once the
    frames are out the line stays open until ``release`` is set, then hangs
    up."""

    def __init__(self, frames, clock=None):
        super().__init__(frames=frames, hold_open=True)
        self.at: dict[int, object] = {}
        self.index = 0
        self.clock = clock
        self.release = asyncio.Event()

    async def read_frame(self):
        hook = self.at.get(self.index)
        if hook is not None:
            await hook()
            self.at.pop(self.index, None)
        if not self._frames:
            await self.release.wait()
            return FRAME_HANGUP, b""
        self.index += 1
        if self.clock is not None:
            self.clock.t += 0.02
        return self._frames.pop(0)


class AudioClock:
    """The Silero module's ``time``: its silence timer reads the wall clock,
    and a test feeds a call's audio in a fraction of its length."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


class IdleVad:
    """A VAD that never starts speech: the sentence the real one missed."""

    def __init__(self, **kw):
        from audio.providers.vad.base import VadState
        self.state = VadState.IDLE
        self.bargein_mode = False
        self.frames = 0

    def set_bargein_mode(self, on):
        self.bargein_mode = on

    def process(self, audio_bytes):
        from audio.providers.vad.base import VadEvent
        self.frames += 1
        return VadEvent.NONE


# -- the call -----------------------------------------------------------------

def _cfg(**settings) -> ConfigManager:
    cfg = ConfigManager()
    base = {"pin_interdigit_timeout_s": "0.2", "turn_complete_timeout_s": "0.05",
            "breath_enabled": "false"}
    cfg.load({"settings": {**base, **{k: str(v) for k, v in settings.items()}}, "routes": []})
    return cfg


@pytest.fixture
def quick_sweep(monkeypatch):
    """The sweep's waits, shortened; the rules are the module's own."""
    monkeypatch.setattr(plturn, "ORPHAN_SWEEP_S", 0.05, raising=False)
    monkeypatch.setattr(plturn, "ORPHAN_POLL_S", 0.02, raising=False)
    monkeypatch.setattr(plturn, "ORPHAN_STUCK_S", 0.2, raising=False)
    monkeypatch.setattr(plturn, "ORPHAN_WAIT_MAX_S", 0.6, raising=False)
    monkeypatch.setattr(plturn, "OWN_VOICE_TAIL_S", 0.1, raising=False)


@pytest.fixture
def call(make_pipeline, monkeypatch, quick_sweep):
    """A call on a line with a PIN: the gate's tones, then ``line`` after the
    greeting has ended and the line's round trip has passed. Returns the
    pipeline, the connection and the list of dispatched turns."""
    monkeypatch.setattr(pin_failures, "store", PinFailureStore(now=lambda: 1000.0))

    async def no_classifier(cfg):
        return None

    monkeypatch.setattr(plcore, "build_turn_classifier", no_classifier)

    def _make(line: bytes, *, vad_cls, clock=None, final_at: float | None = None, **settings):
        if vad_cls is not None:
            monkeypatch.setattr(plcore, "SileroVad", vad_cls)
        gate = _frames(_pin_tones("1234#"))
        conn = LineConn(gate + _frames(_noise(0.4, 8)) + _frames(line), clock=clock)
        p = make_pipeline(route=make_route(pin="1234"), conn=conn, llm=FakeLLM(),
                          caller_info={"phone": "+16085550100", "did": ""}, cfg=_cfg(**settings))
        p.stt.needs_pre_connect = True           # as Deepgram: open from the gate's end
        dispatched: list[str] = []
        p.turn_out, p.swept, greeted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def record(text, is_opening=False):
            dispatched.append(text)
            p.turn_out.set()

        p._process_utterance = record
        greeting_cleanup = p._greeting_tts_cleanup

        async def cleanup():
            await greeting_cleanup()
            greeted.set()

        p._greeting_tts_cleanup = cleanup
        sweep = getattr(p, "_sweep_orphan_final", None)
        if sweep is not None:
            async def swept(landed):
                try:
                    await sweep(landed)
                finally:
                    p.swept.set()

            p._sweep_orphan_final = swept

        async def greeting_over():
            # The greeting has played and its tail has left the line: what
            # follows is the caller's.
            await greeted.wait()
            await asyncio.sleep(0.15)

        conn.at[len(gate) + 10] = greeting_over
        if final_at is not None:
            async def final_lands():
                p.stt._transcripts.append(SENTENCE)
                p.stt.last_result_monotonic = time.monotonic()
                callback = getattr(p.stt, "on_partial_final", None)
                if callback is not None:
                    callback(SENTENCE)

            conn.at[len(gate) + 20 + int(final_at / 0.02)] = final_lands
        return p, conn, dispatched

    return _make


async def _run_until(p, conn, done: asyncio.Event, timeout: float) -> None:
    """Run the call until ``done`` is set or the timeout, then hang up."""
    task = asyncio.create_task(p.run())
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(done.wait(), timeout)
    conn.release.set()
    await asyncio.wait_for(task, 5.0)


async def test_a_gate_a_greeting_then_a_sentence_reach_the_agent_on_the_real_vad(call, monkeypatch, caplog):
    """The whole path with the real model: tones through the gate, the
    greeting, then speech. The VAD starts speech on it and the turn goes
    out once: the gate and the greeting leave the VAD able to hear. On the
    feed a call gets, each window with the audio before it."""
    pytest.importorskip("onnxruntime")
    from audio.providers.vad import silero

    clock = AudioClock()
    monkeypatch.setattr(silero, "time", clock)
    speech = _speech()
    line = _noise(0.5, 8) + speech + _noise(1.5, 8, seed=2)
    p, conn, dispatched = call(line, vad_cls=silero.SileroVad, clock=clock,
                               final_at=0.5 + len(speech) / 2 / RATE + 0.1)
    assert isinstance(p.vad, SerializedVad) and isinstance(p.vad._vad, silero.SileroVad)
    assert p.vad._vad._model.context_samples == 32
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _run_until(p, conn, p.turn_out, 10.0)
    assert p.state.pin_verified is True
    assert "Speech start" in caplog.text and "Speech end" in caplog.text
    assert dispatched == [SENTENCE]
    # The call's last line: the real detector's windows by loudness, with
    # speech among them, and no word of the caller.
    last = re.search(r"The detector by loudness, windows/those over its bar\. Listening: under 75 (\d+)/\d+, "
                     r".*above 1200 (\d+)/(\d+)\. While the agent spoke: under 75 (\d+)/\d+.*", caplog.text)
    assert last and int(last.group(1)) > 0 and int(last.group(3)) > 0 and SENTENCE not in last.group(0)


async def test_the_sweeps_line_says_what_the_real_detector_scored(call, caplog):
    """The real model stays idle on a loud sound that is no speech, the
    words go out through the sweep, and its line carries the detector's own
    numbers for the stretch it looked back over: why the VAD started no
    speech is in the log, with no word of the caller."""
    pytest.importorskip("onnxruntime")
    from audio.providers.vad import silero

    line = _noise(0.3, 8) + _tones(2.0) + _noise(1.0, 8, seed=2)
    p, conn, dispatched = call(line, vad_cls=silero.SileroVad, final_at=0.3 + 2.0 + 0.5)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _run_until(p, conn, p.turn_out, 5.0)
    assert dispatched == [SENTENCE]
    assert "Speech start" not in caplog.text
    said = re.search(
        r"Words with no speech start: [\d.]+ s of the line at or above 150, loudest \d+, "
        r"[\d.]+ s after they landed\. The detector scored (\d+) windows: (\d+) at or over its bar, "
        r"(\d+) of them loud enough, longest run (\d+) of the 2 a speech start needs, "
        r"highest (0\.\d\d|1\.00), (\d+) in barge-in mode", caplog.text)
    assert said, caplog.text
    windows, over, speech, run = (int(said.group(i)) for i in (1, 2, 3, 4))
    assert windows >= 60                       # the two seconds of sound, window by window
    assert speech <= over <= windows and run < 2
    assert SENTENCE not in said.group(0)


async def test_a_sentence_the_vad_never_starts_on_still_reaches_the_agent(call, caplog):
    """The caller's first words after the greeting, the VAD silent on them
    (on a real call they waited 18 s for his next sentence). The provider's
    final has no speech end to drain it. It goes out within two seconds."""
    line = _noise(0.3, 8) + _noise(2.0, 1500) + _noise(1.0, 8, seed=2)
    p, conn, dispatched = call(line, vad_cls=IdleVad, final_at=0.3 + 2.0 + 0.5)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _run_until(p, conn, p.turn_out, 2.0)
    assert p.vad._vad.frames > 100               # the loop fed the VAD the whole line
    assert "Speech start" not in caplog.text
    assert dispatched == [SENTENCE]
    assert "Words with no speech start: " in caplog.text


async def test_a_final_on_a_quiet_line_stays_where_it_is(call, caplog):
    """Words with nothing loud behind them are not the caller's (a room's
    television, a faint return of the agent's voice): the VAD's energy
    floor is the rule, and the log says what the line carried."""
    line = _noise(0.3, 8) + _noise(2.0, 40) + _noise(1.0, 8, seed=2)
    p, conn, dispatched = call(line, vad_cls=IdleVad, final_at=0.3 + 2.0 + 0.5)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _run_until(p, conn, p.swept, 2.0)
    assert dispatched == []
    assert "Words with no speech start kept: the line was quiet" in caplog.text
    assert p.stt._transcripts == [SENTENCE]      # the next speech end still finds them


# -- the sweep's rules --------------------------------------------------------

def _listening(make_pipeline, *, loud: bool = True):
    """A pipeline in the listening state with one final in the provider's
    queue and, when ``loud``, the caller's voice on the line behind it."""
    p = make_pipeline(conn=FakeConn(hold_open=True), llm=FakeLLM())
    p.state._running = True
    p.state._stt_active = True
    p.stt._transcripts.append(SENTENCE)
    timers: list[str] = []

    async def timer(joined):
        timers.append(joined)

    p._turn_timeout_handler = timer
    if loud:
        now = time.monotonic()
        for i in range(25):
            p.state._line_frames.append((now - 0.5 + i * 0.02, 1500.0, 0.02))
    return p, timers


async def _sweep(p, landed: float) -> None:
    """One sweep, then one step of the loop: the turn timer it starts is a
    task of its own."""
    await p._sweep_orphan_final(landed)
    await asyncio.sleep(0)


async def test_a_call_listens_for_a_final(make_pipeline):
    p = make_pipeline()
    assert p.stt.on_partial_final == p._on_stt_partial_final
    assert p._orphan_needs_loud_line is True


async def test_a_landed_final_starts_the_sweep_except_under_the_agents_voice(make_pipeline, quick_sweep):
    p, timers = _listening(make_pipeline)
    p.state._tts_playing = True                  # the echo window: the provider hears silence
    p._on_stt_partial_final(SENTENCE)
    await asyncio.sleep(0.15)
    assert timers == []
    p.state._tts_playing = False
    p._on_stt_partial_final(SENTENCE)
    await asyncio.sleep(0.15)
    assert timers == [SENTENCE]


async def test_the_agents_own_voice_on_the_line_is_never_the_callers(make_pipeline, quick_sweep):
    """A thinking filler returns on the line while the engine works. Its
    final must not be queued as the caller's words: that would stop the
    reply being generated."""
    p, timers = _listening(make_pipeline, loud=False)

    async def parked():
        await asyncio.sleep(30)

    p.state._utterance_task = asyncio.create_task(parked())
    try:
        loud = _noise(0.02, 1500)
        p.state._last_voice_sent = time.monotonic()       # the filler's frames going out
        for _ in range(20):
            p._note_line_frame(loud)                      # and coming back
        assert len(p.state._line_frames) == 0
        await _sweep(p, time.monotonic())
        assert p.state._queued_speech == [] and p.llm.aborts == 0
        assert p.stt._transcripts == [SENTENCE]

        # The caller, once the agent's voice has left the line: queued, and
        # the queued words interrupt as spoken ones do.
        p.state._last_voice_sent = time.monotonic() - 1.0
        for _ in range(20):
            p._note_line_frame(loud)
        assert len(p.state._line_frames) == 20
        await _sweep(p, time.monotonic())
        await asyncio.sleep(0.01)                         # the abort is its own task
        assert p.state._queued_speech == [SENTENCE] and p.llm.aborts == 1
        # The queued words are a turn taken: the streams move on and the
        # loudness behind them is spent.
        assert p.stt.takes == 1 and len(p.state._line_frames) == 0
    finally:
        p.state._utterance_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await p.state._utterance_task


async def test_loudness_from_before_a_take_or_a_reply_proves_nothing(make_pipeline, quick_sweep):
    p, timers = _listening(make_pipeline)
    p._note_turn_taken()                                  # the caller's last turn went out
    await _sweep(p, time.monotonic())
    assert timers == []

    p, timers = _listening(make_pipeline)
    p.state._last_voice_sent = time.monotonic()           # a reply's last word, still arriving
    p._note_line_frame(_noise(0.02, 1500))
    await _sweep(p, time.monotonic())
    assert timers == []

    p, timers = _listening(make_pipeline)                 # loud, but long before the words
    await _sweep(p, time.monotonic() + plturn.ORPHAN_LOUD_LOOKBACK_S + 1.0)
    assert timers == []

    p, timers = _listening(make_pipeline, loud=False)     # loud only after the words landed
    landed = time.monotonic() - 1.0
    for i in range(25):
        p.state._line_frames.append((landed + 0.1 + i * 0.02, 1500.0, 0.02))
    await _sweep(p, landed)
    assert timers == [] and p.stt._transcripts == [SENTENCE]


async def test_the_sweeps_line_carries_the_detectors_numbers_for_its_stretch(make_pipeline, quick_sweep, caplog):
    """The sweep asks the VAD for what it scored over the stretch the
    loudness rule reads, and says it on both of its lines. A VAD that keeps
    no scores leaves the line as it was."""
    from audio.providers.vad.silero import Scored

    asked: list[tuple[float, float]] = []

    def scored(since, until):
        asked.append((since, until))
        return Scored(windows=94, over=12, speech=9, longest_run=1, needed_run=2, peak=0.71, bargein=3)

    tail = (". The detector scored 94 windows: 12 at or over its bar, 9 of them loud enough, "
            "longest run 1 of the 2 a speech start needs, highest 0.71, 3 in barge-in mode")
    p, timers = _listening(make_pipeline)
    p.vad._vad.scored = scored
    landed = time.monotonic()
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _sweep(p, landed)
    assert timers == [SENTENCE]
    assert asked == [(landed - plturn.ORPHAN_LOUD_LOOKBACK_S, landed)]
    assert re.search(r"Words with no speech start: 0\.50 s of the line at or above 150, loudest 1500, "
                     r"0\.\d s after they landed" + re.escape(tail), caplog.text)

    caplog.clear()
    p, timers = _listening(make_pipeline, loud=False)     # the kept line says it too
    p.vad._vad.scored = scored
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _sweep(p, time.monotonic())
    assert timers == []
    assert re.search(r"kept: the line was quiet \(0\.00 s at or above 150, loudest 0, 0\.\d s after they landed\)"
                     + re.escape(tail), caplog.text)

    caplog.clear()
    p, timers = _listening(make_pipeline)                 # a VAD with no scores
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _sweep(p, time.monotonic())
    assert timers == [SENTENCE] and "The detector" not in caplog.text
    assert re.search(r"Words with no speech start: .* after they landed\n", caplog.text)


async def test_a_hold_during_the_wait_leaves_the_words_to_the_composer(make_pipeline, quick_sweep):
    """The line is muted while the sweep waits (live voice's hold: the
    composer owns the draft now). The sweep sends nothing and the words
    stay where the hold finds them."""
    p, timers = _listening(make_pipeline)
    p.state._user_silent.clear()
    p.stt.last_result_monotonic = time.monotonic()
    task = asyncio.create_task(p._sweep_orphan_final(time.monotonic()))
    await asyncio.sleep(0.08)
    assert not task.done()
    p.conn.is_muted = True
    await asyncio.wait_for(task, 2.0)
    await asyncio.sleep(0)
    assert timers == [] and p.stt._transcripts == [SENTENCE]


async def test_a_swept_turn_starts_with_no_audio_of_the_last_one(make_pipeline, quick_sweep):
    """The audio classifier's buffer holds the last turn's speech: the VAD
    started none for these words. A fresh swept turn empties it. A turn
    already gathering keeps its audio."""
    p, timers = _listening(make_pipeline)
    p.state._speech_audio_buf.extend(b"\x01\x00" * 800)
    await _sweep(p, time.monotonic())
    assert timers == [SENTENCE] and len(p.state._speech_audio_buf) == 0

    p, timers = _listening(make_pipeline)
    p.state._turn_segments.append("And then")
    p.state._speech_audio_buf.extend(b"\x01\x00" * 800)
    await _sweep(p, time.monotonic())
    assert timers == ["And then " + SENTENCE] and len(p.state._speech_audio_buf) == 1600


async def test_a_stuck_vad_does_not_keep_finished_words(make_pipeline, quick_sweep, caplog):
    """The VAD holds "speech" on line noise after the words stopped: once
    the STT has been quiet for the bound the sweep goes on, and the turn
    timer's own rule for speech with no transcript sends the turn."""
    p, timers = _listening(make_pipeline)
    p.state._user_silent.clear()
    landed = time.monotonic()
    p.stt.last_result_monotonic = landed
    with caplog.at_level(logging.INFO, logger="pipeline"):
        await _sweep(p, landed)
    assert "Words with no speech end: " in caplog.text    # a speech the VAD never ended
    assert timers == [SENTENCE]
    assert time.monotonic() - landed >= plturn.ORPHAN_STUCK_S
    assert not p.state._user_silent.is_set()              # the VAD's word stays the VAD's


async def test_the_sweep_waits_for_a_caller_who_is_still_speaking(make_pipeline, quick_sweep):
    """The VAD says speech and the STT keeps answering: a speech end will
    take the words. The sweep starts no timer under a speaker, and gives up
    at its cap."""
    p, timers = _listening(make_pipeline)
    p.state._user_silent.clear()
    landed = time.monotonic()
    task = asyncio.create_task(p._sweep_orphan_final(landed))
    while not task.done():
        p.stt.last_result_monotonic = time.monotonic()
        await asyncio.sleep(0.02)
    assert timers == [] and p.stt._transcripts == [SENTENCE]
    assert time.monotonic() - landed >= plturn.ORPHAN_WAIT_MAX_S

    # The speech end drains them meanwhile: nothing is left to send.
    p, timers = _listening(make_pipeline)
    p.state._user_silent.clear()
    p.stt.last_result_monotonic = time.monotonic()
    task = asyncio.create_task(p._sweep_orphan_final(time.monotonic()))
    await asyncio.sleep(0.08)
    p.stt._transcripts.clear()
    p.state._user_silent.set()
    await asyncio.wait_for(task, 2.0)
    assert timers == []


async def test_a_live_partial_holds_the_sweep_until_its_words_are_finished(make_pipeline, quick_sweep):
    p, timers = _listening(make_pipeline)
    p.stt.latest_interim = "and one more"
    task = asyncio.create_task(p._sweep_orphan_final(time.monotonic()))
    for _ in range(5):
        p.stt.last_result_monotonic = time.monotonic()
        await asyncio.sleep(0.02)
    assert not task.done() and timers == []
    p.stt._transcripts.append("and one more thing.")      # the partial's final
    p.stt.latest_interim = ""
    await asyncio.wait_for(task, 2.0)
    # The stand-in drains one final a call; both are in the queue's order.
    assert timers == [SENTENCE]

    # A partial whose final never comes: quiet for the bound, the sweep goes on.
    p, timers = _listening(make_pipeline)
    p.stt.latest_interim = "and"
    landed = time.monotonic()
    p.stt.last_result_monotonic = landed
    await _sweep(p, landed)
    assert timers == [SENTENCE]


async def test_the_sweep_leaves_words_the_pipeline_already_owns(make_pipeline, quick_sweep):
    p, timers = _listening(make_pipeline)

    async def pending():
        await asyncio.sleep(30)

    p.state._turn_timer = asyncio.create_task(pending())  # its last drain takes them
    await _sweep(p, time.monotonic())
    p.state._turn_timer.cancel()
    assert timers == [] and p.stt._transcripts == [SENTENCE]

    p, timers = _listening(make_pipeline)
    p.state._tts_playing = True                           # audible playback owns the floor
    await _sweep(p, time.monotonic())
    assert timers == []

    p, timers = _listening(make_pipeline)
    p.state._running = False                              # the call is over
    await _sweep(p, time.monotonic())
    assert timers == []


async def test_a_final_whose_partial_was_dispatched_is_dropped_though_the_sweep_waited(make_pipeline, quick_sweep):
    """The turn went out on the live partial and its real final landed a
    second later. The sweep reads it later still: the two seconds of the
    duplicate check count from the landing."""
    p, timers = _listening(make_pipeline)
    landed = time.monotonic() - 1.5
    p.state._interim_fallback_text = SENTENCE
    p.state._interim_fallback_at = landed - 1.0           # 2.5 s before the sweep reads it
    for i in range(25):
        p.state._line_frames.append((landed - 0.5 + i * 0.02, 1500.0, 0.02))
    await _sweep(p, landed)
    assert timers == [] and p.stt._transcripts == []
