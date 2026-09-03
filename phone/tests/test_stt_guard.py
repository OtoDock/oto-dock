"""STT liveness guard + phantom-hold cap (live-hit 2026-08-14: both test
calls went deaf — Deepgram idle-died after an inbound media gap, ElevenLabs
died during a long playback mute; no reconnect existed, and a 39 s phantom
VAD "speech" window held a ready reply until the caller gave up).

Covers the pipeline-side mechanics with fakes: guard tick (keepalive on
starvation, reconnect on death, fatal-error poll), the reconnect latch, the
TTS hold cap (+ barge-in timer arming at a cap-triggered start), and the
turn-timer phantom short-circuit. Provider-side liveness details live in
audio/providers/stt/tests/.
"""

import asyncio
import time

import pytest

from pipeline_fakes import FakeSTT, FakeLLM, make_route


class GuardProbeSTT(FakeSTT):
    """FakeSTT with switchable health for guard-path tests."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.open = True
        self.fatal = None
        self.ensure_calls = []
        self.ensure_result = True
        self.ensure_gate: asyncio.Event | None = None

    @property
    def is_open(self):
        return self.open

    def pop_fatal_error(self):
        err, self.fatal = self.fatal, None
        return err

    async def ensure_alive(self, language, *, clear_queue=False):
        self.ensure_calls.append((language, clear_queue))
        if self.ensure_gate is not None:
            await self.ensure_gate.wait()
        if self.ensure_result:
            self.open = True
        return self.ensure_result


def _guard_pipeline(make_pipeline):
    p = make_pipeline(route=make_route(language="en"), llm=FakeLLM())
    p.stt = GuardProbeSTT()
    p.state._running = True
    p.state._stt_active = True
    p.state._stt_last_fed = time.monotonic()
    return p


def test_guard_tick_keepalive_on_starvation(make_pipeline):
    """No bytes fed for >3 s → keepalive; a freshly-fed provider is left alone."""
    p = _guard_pipeline(make_pipeline)

    asyncio.run(p._stt_guard_tick())
    assert p.stt.keepalives == 0  # just fed — no keepalive

    p.state._stt_last_fed = time.monotonic() - 4.0
    asyncio.run(p._stt_guard_tick())
    assert p.stt.keepalives == 1
    # tick re-stamps last_fed, so the next tick stays quiet
    asyncio.run(p._stt_guard_tick())
    assert p.stt.keepalives == 1


def test_guard_tick_reconnects_dead_connection(make_pipeline):
    """is_open False → ensure_alive(clear_queue=False) — never the opening
    clear (audit F6: the queue may hold a barge-in utterance's finals)."""
    p = _guard_pipeline(make_pipeline)
    p.stt.open = False

    asyncio.run(p._stt_guard_tick())
    assert p.stt.ensure_calls == [("en", False)]
    assert p.stt.open is True  # reconnected


def test_guard_tick_reconnects_on_fatal_error(make_pipeline):
    """A provider fatal (Deepgram Error event) triggers reconnect even while
    the socket still reports open — free signal the old code never polled."""
    p = _guard_pipeline(make_pipeline)
    p.stt.fatal = "Deepgram speech-to-text error: NET-0001"

    asyncio.run(p._stt_guard_tick())
    assert p.stt.ensure_calls == [("en", False)]


def test_guard_tick_idle_when_stt_inactive(make_pipeline):
    p = _guard_pipeline(make_pipeline)
    p.state._stt_active = False
    p.stt.open = False
    p.state._stt_last_fed = time.monotonic() - 10.0

    asyncio.run(p._stt_guard_tick())
    assert p.stt.ensure_calls == []
    assert p.stt.keepalives == 0


def test_reconnect_latch_single_chain(make_pipeline):
    """Concurrent reconnect attempts collapse into one chain (in-flight latch)."""
    p = _guard_pipeline(make_pipeline)
    p.stt.open = False
    p.stt.ensure_gate = asyncio.Event()

    async def run():
        t1 = asyncio.create_task(p._stt_reconnect())
        await asyncio.sleep(0.01)  # t1 is now parked inside ensure_alive
        t2 = asyncio.create_task(p._stt_reconnect())
        await asyncio.sleep(0.01)  # t2 must return via the latch, not stack
        p.stt.ensure_gate.set()
        await asyncio.gather(t1, t2)

    asyncio.run(run())
    assert p.stt.ensure_calls == [("en", False)]
    assert p.state._stt_reconnecting is False


def test_reconnect_gives_up_after_capped_retries(make_pipeline):
    """A hopeless provider gets a bounded retry chain, then the guard's next
    tick owns the follow-up — no infinite loop, latch released."""
    p = _guard_pipeline(make_pipeline)
    p.stt.open = False
    p.stt.ensure_result = False

    asyncio.run(p._stt_reconnect())
    assert len(p.stt.ensure_calls) == 4  # 0 / 0.5 / 1 / 2 s backoff steps
    assert p.state._stt_reconnecting is False


# ── TTS hold cap (phantom speech must not starve a ready reply) ─────────


def _hold_pipeline(make_pipeline, *, hold_max_s="0.3"):
    from config_manager import ConfigManager
    cfg = ConfigManager()
    cfg.load({"version": 1, "providers": {}, "settings":
              {"tts_hold_max_s": hold_max_s}, "routes": []})
    p = make_pipeline(route=make_route(language="en"), llm=FakeLLM(), cfg=cfg)
    p.state._running = True
    p.state._stt_active = True
    p.state._user_silent.clear()  # "user speaking" per VAD
    return p


def test_hold_cap_starts_playback_without_evidence(make_pipeline):
    """VAD-speech with zero transcript evidence releases the reply at the cap
    (was: held indefinitely — 30 s on the live call)."""
    p = _hold_pipeline(make_pipeline)

    async def run():
        t0 = time.monotonic()
        await asyncio.wait_for(p._stream_tts_audio(), timeout=5.0)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.05)  # let the post-playback probe task settle
        return elapsed

    elapsed = asyncio.run(run())
    assert p.conn.sent_audio, "capped hold must still play the audio"
    assert 0.2 <= elapsed < 2.0


def test_hold_with_interim_evidence_waits(make_pipeline):
    """A live interim = a real transcribing speaker — the cap must NOT fire;
    playback starts only when the user goes silent."""
    p = _hold_pipeline(make_pipeline)
    p.stt = FakeSTT()
    p.stt.latest_interim = "για να δούμε"

    async def run():
        task = asyncio.create_task(p._stream_tts_audio())
        await asyncio.sleep(0.6)  # well past the 0.3 s cap
        assert not p.conn.sent_audio, "evidence must keep the hold"
        p.state._user_silent.set()  # user finishes
        await asyncio.wait_for(task, timeout=5.0)
        await asyncio.sleep(0.05)  # let the post-playback probe task settle

    asyncio.run(run())
    assert p.conn.sent_audio


def test_hold_cap_start_never_arms_an_abort(make_pipeline):
    """Cap-triggered start with VAD still in SPEAKING: the speech produced
    zero transcript evidence, so it is phantom — playback proceeds with NO
    pause and NO abort armed (the old F10 timer here was the exact VAD-only
    turn-kill path the pause/confirm/commit design removed)."""
    from audio.providers.vad.base import VadState

    p = _hold_pipeline(make_pipeline)
    p.vad.state = VadState.SPEAKING

    async def run():
        await asyncio.wait_for(p._stream_tts_audio(), timeout=5.0)
        await asyncio.sleep(0.05)  # settle probe

    asyncio.run(run())
    assert p.conn.sent_audio
    assert p.state._playback_paused is False
    assert p.state._pause_confirm_timer is None
    assert p.llm.aborts == 0


# ── Turn-timer phantom short-circuit ────────────────────────────────────


def test_turn_timer_short_circuits_phantom_speech(make_pipeline):
    """User-speaking wait dispatches after ~2 s when the "speech" produced no
    transcript evidence (was: a flat 5 s on every noisy line)."""
    p = _guard_pipeline(make_pipeline)
    p.state._user_silent.clear()
    p.state._turn_segments.append("hello there")
    dispatched = []

    async def fake_process(text, **kw):
        dispatched.append(text)

    p._process_utterance = fake_process

    async def fake_classify(segments):
        return 0.0  # "complete" — no extra wait

    p._classify_turn = fake_classify

    async def run():
        t0 = time.monotonic()
        await asyncio.wait_for(p._turn_timeout_handler("hello there"), timeout=10.0)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.05)  # let the dispatch task run the fake
        return elapsed

    elapsed = asyncio.run(run())
    assert dispatched == ["hello there"]
    assert 1.8 <= elapsed < 4.0  # 2 s short-circuit, not the 5 s full wait


# ── Failure mode #3: alive-but-mute session (live-hit 2026-08-14 #2) ─────

@pytest.mark.asyncio
async def test_guard_reconnects_a_mute_session(make_pipeline):
    """Socket open, sends flowing, VAD saw 7+ s of speech, ZERO results →
    the guard must treat the session as mute and reconnect (neither the
    death poll nor the starvation keepalive can see this)."""
    p = _guard_pipeline(make_pipeline)
    p.state._stt_unheard_speech_s = 7.5
    await p._stt_guard_tick()
    assert p.stt.ensure_calls == [("en", False)]
    assert p.state._stt_unheard_speech_s == 0.0


@pytest.mark.asyncio
async def test_results_arriving_reset_the_mute_counter(make_pipeline):
    """Any provider result moves last_result_monotonic — the tick resets the
    accumulated speech instead of reconnecting."""
    p = _guard_pipeline(make_pipeline)
    p.state._stt_unheard_speech_s = 12.0
    p.stt.last_result_monotonic = time.monotonic()
    await p._stt_guard_tick()
    assert p.stt.ensure_calls == []
    assert p.state._stt_unheard_speech_s == 0.0
    # Counter builds again with no new results → next tick reconnects.
    p.state._stt_unheard_speech_s = 7.5
    await p._stt_guard_tick()
    assert p.stt.ensure_calls == [("en", False)]


@pytest.mark.asyncio
async def test_mute_counter_below_threshold_is_left_alone(make_pipeline):
    p = _guard_pipeline(make_pipeline)
    p.state._stt_unheard_speech_s = 3.0
    await p._stt_guard_tick()
    assert p.stt.ensure_calls == []
    assert p.state._stt_unheard_speech_s == 3.0
