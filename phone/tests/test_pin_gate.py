"""Inbound PIN gate: prompts, digit collection, attempts, cooldowns.

Unit-drives ``PinGateMixin._pin_gate`` on a faked pipeline (conftest
``make_pipeline``) plus run()-level checks that a failed gate never touches
STT/LLM and that PIN-less routes skip the gate entirely. The failure store
is monkeypatched per test — it is a module singleton with real-time windows.
"""

import asyncio

import pytest

from calls import pin_failures
from calls.pin_failures import PinFailureStore
from config_manager import ConfigManager
from transport.base import FRAME_AUDIO, FRAME_DTMF, FRAME_HANGUP, TransportError

from pipeline_fakes import FakeConn, FakeLLM, make_route

pytestmark = pytest.mark.asyncio


def _digits(*chars):
    return [(FRAME_DTMF, c.encode()) for c in chars]


def _cfg(**settings):
    cfg = ConfigManager()
    cfg.load({"settings": {k: str(v) for k, v in settings.items()},
              "routes": []})
    return cfg


@pytest.fixture
def fresh_store(monkeypatch):
    """Isolated failure store with a controllable clock."""
    clock = {"t": 1000.0}
    store = PinFailureStore(now=lambda: clock["t"])
    monkeypatch.setattr(pin_failures, "store", store)
    return store, clock


def _gate_pipeline(make_pipeline, *, pin="1234", frames=None, caller="+16085550100",
                   cfg=None, hold_open=True):
    conn = FakeConn(frames=frames or [], hold_open=hold_open)
    return make_pipeline(
        route=make_route(pin=pin),
        conn=conn,
        caller_info={"phone": caller, "did": "+16085550147"},
        cfg=cfg or _cfg(pin_interdigit_timeout_s="0.2"),
    ), conn


async def test_correct_pin_passes(make_pipeline, fresh_store):
    p, conn = _gate_pipeline(make_pipeline, frames=_digits("1", "2", "3", "4", "#"))
    assert await p._pin_gate() is True
    assert conn.hangups == 0
    assert p.state.call_outcome == "completed"
    assert p.state.pin_attempts == 1


async def test_wrong_pin_three_attempts_then_hangup(make_pipeline, fresh_store):
    store, _ = fresh_store
    frames = _digits("9", "9", "9", "9", "#") * 3
    p, conn = _gate_pipeline(make_pipeline, frames=frames)
    assert await p._pin_gate() is False
    assert conn.hangups == 1
    assert p.state.call_outcome == "pin_failed"
    assert p.state.pin_attempts == 3
    # 3 wrong entries → 3 failures on both windows.
    assert not store.number_locked("+16085550100")
    assert len(store._numbers["16085550100"]) == 3


async def test_six_digit_entry_evaluates_without_pound(make_pipeline, fresh_store):
    p, conn = _gate_pipeline(
        make_pipeline, pin="123456",
        frames=_digits("1", "2", "3", "4", "5", "6"))
    assert await p._pin_gate() is True
    assert p.state.pin_attempts == 1


async def test_star_clears_the_entry(make_pipeline, fresh_store):
    frames = _digits("9", "8", "*", "1", "2", "3", "4", "#")
    p, conn = _gate_pipeline(make_pipeline, frames=frames)
    assert await p._pin_gate() is True
    assert p.state.pin_attempts == 1


async def test_typeahead_digits_collected_while_prompt_plays(make_pipeline, fresh_store):
    """Digits arriving MID-PROMPT must count — the prompt runs concurrently
    with the read loop and never blocks collection (audit F8: a sequential
    prompt would let Twilio's drop-oldest queue eat type-ahead digits)."""
    p, conn = _gate_pipeline(make_pipeline, frames=[])  # silent line at start

    prompt_playing = asyncio.Event()

    async def slow_synthesize(text, *, language=None, output_sample_rate=None):
        prompt_playing.set()
        return b"\x00\x00" * 160 * 250  # ~5 s of frames at 20 ms pacing

    p.tts.synthesize = slow_synthesize
    gate_task = asyncio.create_task(p._pin_gate())
    await asyncio.wait_for(prompt_playing.wait(), 2.0)
    conn._frames.extend(_digits("1", "2", "3", "4", "#"))  # typed mid-prompt
    # Gate resolves on the digits long before the 5 s prompt would finish.
    assert await asyncio.wait_for(gate_task, timeout=3.0) is True
    assert p.state.pin_attempts == 1


async def test_silent_caller_times_out_per_attempt_then_fails(make_pipeline, fresh_store):
    p, conn = _gate_pipeline(make_pipeline, frames=[])  # silence, line open
    assert await asyncio.wait_for(p._pin_gate(), timeout=10.0) is False
    assert p.state.call_outcome == "pin_failed"
    assert p.state.pin_attempts == 3
    assert conn.hangups == 1


async def test_overall_timeout_counts_one_failure(make_pipeline, fresh_store):
    store, _ = fresh_store
    cfg = _cfg(pin_entry_timeout_s="0.3", pin_interdigit_timeout_s="30")
    p, conn = _gate_pipeline(make_pipeline, frames=[], cfg=cfg)
    assert await p._pin_gate() is False
    assert p.state.call_outcome == "pin_timeout"
    assert conn.hangups == 1
    assert len(store._numbers["16085550100"]) == 1


async def test_hangup_mid_entry_records_no_failure(make_pipeline, fresh_store):
    store, _ = fresh_store
    p, conn = _gate_pipeline(
        make_pipeline, frames=_digits("1") + [(FRAME_HANGUP, b"")])
    with pytest.raises(TransportError):
        await p._pin_gate()
    assert p.state.call_outcome == "hangup"
    assert "16085550100" not in store._numbers  # abandoned ≠ attack


async def test_audio_frames_feed_the_detector_on_audiosocket(make_pipeline, fresh_store):
    """Without native DTMF events the gate decodes in-band tones."""
    import numpy as np

    from telephony.dtmf_detect import _COL_FREQS, _DIGITS, _ROW_FREQS

    def tone(digit, dur_s=0.08, rate=8000, amp=0.3):
        for ri, row in enumerate(_DIGITS):
            if digit in row:
                f1, f2 = _ROW_FREQS[ri], _COL_FREQS[row.index(digit)]
        t = np.arange(int(rate * dur_s)) / rate
        x = amp / 2 * np.sin(2 * np.pi * f1 * t) + amp / 2 * np.sin(2 * np.pi * f2 * t)
        return (x * 32767).astype("<i2").tobytes()

    silence = b"\x00\x00" * 800
    pcm = silence.join(tone(d) for d in "1234#") + silence
    frames = [(FRAME_AUDIO, pcm[i:i + 320]) for i in range(0, len(pcm), 320)]
    p, conn = _gate_pipeline(make_pipeline, frames=frames)
    assert not conn.has_dtmf_events
    assert await asyncio.wait_for(p._pin_gate(), timeout=5.0) is True


async def test_number_cooldown_refuses_before_any_prompt(make_pipeline, fresh_store):
    store, _ = fresh_store
    for _ in range(5):
        store.record_failure("+1 (608) 555-0100", "other-route")
    # Same human number, different formatting — normalization must match.
    p, conn = _gate_pipeline(make_pipeline, frames=_digits("1", "2", "3", "4", "#"))
    assert await p._pin_gate() is False
    assert p.state.call_outcome == "pin_cooldown"
    assert conn.hangups == 1
    assert p.state.pin_attempts == 0  # never got an attempt


async def test_route_breaker_locks_without_caller_number(make_pipeline, fresh_store):
    store, _ = fresh_store
    for i in range(20):
        store.record_failure(f"+1608555{i:04d}", "r1")
    p, conn = _gate_pipeline(make_pipeline, caller="",
                             frames=_digits("1", "2", "3", "4", "#"))
    assert await p._pin_gate() is False
    assert p.state.call_outcome == "pin_cooldown"


async def test_cooldown_expires_and_success_clears(make_pipeline, fresh_store):
    store, clock = fresh_store
    for _ in range(5):
        store.record_failure("+16085550100", "r1")
    assert store.number_locked("+16085550100")
    clock["t"] += 15 * 60 + 1
    assert not store.number_locked("+16085550100")

    p, conn = _gate_pipeline(make_pipeline, frames=_digits("1", "2", "3", "4", "#"))
    assert await p._pin_gate() is True
    assert "16085550100" not in store._numbers  # success wiped the slate


async def test_eviction_never_drops_locked_entries(fresh_store, monkeypatch):
    store, clock = fresh_store
    monkeypatch.setattr(pin_failures, "_CAP", 6)
    for _ in range(5):
        store.record_failure("+16085550001", "r1")   # locked
    for i in range(2, 9):
        store.record_failure(f"+160855500{i:02d}", "r1")
    assert store.number_locked("+16085550001")
    assert len(store._numbers) <= 6


async def test_injected_ami_digits_pass_the_gate(make_pipeline, fresh_store):
    """Signalled digits from the AMI event listener arrive via the
    poll_dtmf side-channel — no FRAME_DTMF, no in-band audio — and must
    drive the gate exactly like native digits, including '#'."""
    p, conn = _gate_pipeline(make_pipeline, frames=[])  # silent line
    for d in "1234#":
        conn.inject_dtmf(d, 80)
    assert await asyncio.wait_for(p._pin_gate(), timeout=5.0) is True
    assert p.state.pin_attempts == 1
    assert p._pin_src_counts.get("ami") == 5


async def test_injected_star_clears_like_frame_star(make_pipeline, fresh_store):
    p, conn = _gate_pipeline(make_pipeline, frames=[])
    for d in "98*1234#":
        conn.inject_dtmf(d, 80)
    assert await asyncio.wait_for(p._pin_gate(), timeout=5.0) is True


async def test_cross_source_dedupe_rules(make_pipeline, fresh_store):
    """One key press can fire both in-band (detector, ~40 ms into the tone)
    and via AMI (DTMFEnd at tone END — a held key trails by its DurationMs).
    The dedupe is duration-aware one way, plain-window the other, and never
    same-source."""
    import time as _time

    p, _ = _gate_pipeline(make_pipeline)
    p._pin_last_accept = None
    p._pin_src_counts = {}

    # AMI duplicate of a just-accepted in-band digit: inside DurationMs+300ms.
    assert p._pin_accept("5", "inband", 0) is True
    assert p._pin_accept("5", "ami", 600) is False
    # Outside the window (in-band accept 1 s ago, window = 0.6 + 0.3 s).
    p._pin_last_accept = ("inband", "5", _time.monotonic() - 1.0)
    assert p._pin_accept("5", "ami", 600) is True

    # Detector trailing an AMI-accepted digit: plain 300 ms window.
    p._pin_last_accept = None
    assert p._pin_accept("7", "ami", 100) is True
    assert p._pin_accept("7", "inband", 0) is False
    p._pin_last_accept = ("ami", "7", _time.monotonic() - 0.5)
    assert p._pin_accept("7", "inband", 0) is True

    # Same-source repeats are real double-presses — never deduped.
    assert p._pin_accept("7", "inband", 0) is True

    # Distinct digits cross-source are never deduped.
    assert p._pin_accept("8", "ami", 50) is True


async def test_late_ami_pound_duplicate_cannot_burn_next_attempt(
        make_pipeline, fresh_store):
    """The held-# case: the detector accepted '#' and ended attempt 1; the
    AMI DTMFEnd for the SAME press arrives late (tone end) and sits queued
    when attempt 2's collect starts — it must be deduped by the gate-scoped
    state, not terminate attempt 2 with an empty buffer."""
    import time as _time

    p, conn = _gate_pipeline(make_pipeline,
                             frames=_digits("1", "2", "3", "4", "#"))
    # Gate-scoped dedupe state as _pin_attempt_loop leaves it right after
    # the detector-accepted '#' closed the previous attempt.
    p._pin_last_accept = ("inband", "#", _time.monotonic())
    p._pin_src_counts = {}
    conn.inject_dtmf("#", 900)  # the press's late AMI copy
    entered = await asyncio.wait_for(
        p._pin_collect("pin_retry", None), timeout=5.0)
    assert entered == "1234"


async def test_run_skips_gate_without_pin(make_pipeline, monkeypatch):
    called = []

    async def spy_gate(self):
        called.append(True)
        return False

    from pipeline.pin_gate import PinGateMixin
    monkeypatch.setattr(PinGateMixin, "_pin_gate", spy_gate)
    p = make_pipeline(route=make_route(pin=""), llm=FakeLLM(),
                      conn=FakeConn())  # empty frames → hangup after greeting
    await p.run()
    assert called == []


async def test_failed_gate_never_touches_stt_or_llm(make_pipeline, fresh_store):
    """The whole point of the gate's placement: a refused call must not
    open STT, build an LLM backend, or start warmup."""
    frames = _digits("9", "9", "9", "9", "#") * 3
    conn = FakeConn(frames=frames, hold_open=True)
    p = make_pipeline(
        route=make_route(pin="1234"), conn=conn,
        caller_info={"phone": "+16085550100", "did": ""},
        cfg=_cfg(pin_interdigit_timeout_s="0.2"),
    )
    p.stt.needs_pre_connect = True  # would pre-connect if it got that far
    await p.run()
    assert conn.hangups == 1
    assert p.state.call_outcome == "pin_failed"
    assert p.llm is None
    assert p.stt.started == []
    assert p.state._warmup_task is None
