"""P2 cracking fixes: ambience stall-suppression, TTS pre-buffer, serialized
VAD ordering (live-hit 2026-08-14: 6–17 pacing stalls per long playback on
BOTH transports, each decorated with an ambience-bed frame → audible tick).
"""

import contextlib
import asyncio
import time

from pipeline_fakes import FakeLLM, make_route


def _cfg(**settings):
    from config_manager import ConfigManager
    cfg = ConfigManager()
    # Breath off: the prebuffer tests count exact frames and the inhale clip
    # would add its own.
    merged = {"breath_enabled": "false"}
    merged.update({k: str(v) for k, v in settings.items()})
    cfg.load({"version": 1, "providers": {}, "settings": merged, "routes": []})
    return cfg


# ── Ambience bed vs voice-sender stalls ─────────────────────────────────


def test_ambience_suppressed_while_voice_segment_active(make_pipeline):
    """A stalled voice sender must NOT trigger bed injection — the bed frame
    lands mid-segment and ticks. Inter-turn silence keeps the bed."""
    p = make_pipeline(route=make_route(), llm=FakeLLM())
    p.state._running = True

    class _Bed:
        def __init__(self):
            self.frames = 0

        def next_frame(self):
            self.frames += 1
            return b"\x00" * 320

        def mix_into(self, frame):
            return frame

    p._ambience = _Bed()

    async def run():
        task = asyncio.create_task(p._ambience_loop())
        # Simulate a voice segment that stalls: sender active, but no voice
        # frame stamped for way past the 30 ms idle threshold.
        p._voice_senders = 1
        p.state._last_voice_sent = time.monotonic() - 1.0
        await asyncio.sleep(0.15)
        stalled_frames = p._ambience.frames
        # Segment ends → bed resumes on the same silence.
        p._voice_senders = 0
        await asyncio.sleep(0.15)
        resumed_frames = p._ambience.frames
        p.state._running = False
        task.cancel()
        return stalled_frames, resumed_frames

    stalled, resumed = asyncio.run(run())
    assert stalled == 0, "bed must not decorate a voice sender's stall"
    assert resumed > 0, "inter-turn bed behavior unchanged"


# ── TTS pre-buffer ──────────────────────────────────────────────────────


def _tts_with_chunks(p, chunks):
    p.tts._audio_chunks = list(chunks)
    return p


def test_prebuffer_banks_before_first_frame(make_pipeline):
    """With a 200 ms floor and 20 ms chunks, nothing plays until the floor is
    banked — then the whole bank flushes through the paced sender."""
    p = make_pipeline(route=make_route(), llm=FakeLLM(),
                      cfg=_cfg(tts_prebuffer_ms=200, tts_hold_max_s=4))
    p.state._running = True
    p.state._stt_active = True
    frame = b"\x00" * 320  # one 20 ms frame @8k
    _tts_with_chunks(p, [frame] * 12)  # 240 ms total

    async def run():
        await asyncio.wait_for(p._stream_tts_audio(), timeout=5.0)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    # 12 × 320 B in = 12 frames out (prebuffer reorders, never drops)
    assert len(p.conn.sent_audio) == 12


def test_prebuffer_flushes_short_utterance(make_pipeline):
    """An utterance shorter than the floor still plays fully (end-of-stream
    flush)."""
    p = make_pipeline(route=make_route(), llm=FakeLLM(),
                      cfg=_cfg(tts_prebuffer_ms=500, tts_hold_max_s=4))
    p.state._running = True
    p.state._stt_active = True
    _tts_with_chunks(p, [b"\x00" * 320] * 3)  # 60 ms < 500 ms floor

    async def run():
        await asyncio.wait_for(p._stream_tts_audio(), timeout=5.0)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert len(p.conn.sent_audio) == 3


def test_prebuffer_disabled_at_zero(make_pipeline):
    p = make_pipeline(route=make_route(), llm=FakeLLM(),
                      cfg=_cfg(tts_prebuffer_ms=0, tts_hold_max_s=4))
    p.state._running = True
    p.state._stt_active = True
    _tts_with_chunks(p, [b"\x00" * 320] * 2)

    async def run():
        await asyncio.wait_for(p._stream_tts_audio(), timeout=5.0)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert len(p.conn.sent_audio) == 2


# ── SerializedVad ordering ──────────────────────────────────────────────


def test_serialized_vad_orders_mode_swaps_against_inference():
    """set_bargein_mode submitted between aprocess calls lands between them
    on the single worker — the mode swap can never interleave mid-inference
    (audit F11)."""
    from pipeline.vad_serial import SerializedVad

    calls = []

    class SlowVad:
        state = "idle"

        def process(self, audio):
            time.sleep(0.03)  # slow inference in the worker thread
            calls.append(("process", audio))
            return "event"

        def set_bargein_mode(self, on):
            calls.append(("mode", on))

    v = SerializedVad(SlowVad())

    async def run():
        t1 = asyncio.create_task(v.aprocess(b"a"))
        await asyncio.sleep(0)  # t1 queued on the worker first
        v.set_bargein_mode(True)  # queued second
        t2 = asyncio.create_task(v.aprocess(b"b"))  # queued third
        assert await t1 == "event"
        assert await t2 == "event"

    asyncio.run(run())
    v.shutdown()
    assert calls == [("process", b"a"), ("mode", True), ("process", b"b")]


def test_serialized_vad_passthrough_and_state():
    from pipeline.vad_serial import SerializedVad

    class Inner:
        state = "speaking"
        threshold = 0.4

    v = SerializedVad(Inner())
    assert v.state == "speaking"
    assert v.threshold == 0.4
    v.shutdown()


# ── First-audio latency instrumentation (P4) ────────────────────────────


def test_first_audio_latency_line_renders(make_pipeline, caplog):
    """The per-segment breakdown logs at first frame when dispatch stamps are
    set (and stays silent for unstamped segments like the greeting)."""
    import logging

    p = make_pipeline(route=make_route(), llm=FakeLLM(),
                      cfg=_cfg(tts_prebuffer_ms=0, tts_hold_max_s=4))
    p.state._running = True
    p.state._stt_active = True
    _tts_with_chunks(p, [b"\x00" * 320])
    p.state._t_dispatch = time.monotonic() - 1.0
    p.state._t_text_first = time.monotonic() - 0.8
    p.state._t_audio_first = 0.0  # stamped by the playback path itself

    async def run():
        with caplog.at_level(logging.INFO, logger="pipeline"):
            await asyncio.wait_for(p._stream_tts_audio(), timeout=5.0)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    lines = [r.message for r in caplog.records if "First-audio latency" in r.message]
    assert len(lines) == 1
    assert "dispatch→text" in lines[0] and "audio→frame" in lines[0]


# ── Barge-in pause/confirm/commit ───────────────────────────────────────
#
# VAD evidence alone never kills a turn (live-hit 2026-08-20/21: a stuck
# "speech" state with zero transcripts aborted mid-tool generations). Speech
# pauses the sender; only a non-empty final transcript commits; an episode
# with no words resumes playback losslessly.


class StreamTTS:
    """FakeTTS whose audio stream stays OPEN until the test ends it — the
    segment task keeps consuming, so pause/commit land mid-playback."""

    def __init__(self):
        import asyncio as _a
        self.voice_id = ""
        self.voices = {}
        self.cancelled = False
        self.contexts_started = 0
        self.text_chunks = []
        self.q: _a.Queue = _a.Queue()

    async def connect(self, *, output_sample_rate=None):
        pass

    async def close(self):
        pass

    def start_streaming_context(self, **kw):
        self.contexts_started += 1

    async def send_text_chunk(self, text, is_last=False):
        self.text_chunks.append((text, is_last))

    async def receive_audio(self):
        while True:
            item = await self.q.get()
            if item is None:
                return
            yield item

    def cancel(self):
        self.cancelled = True

    def select_voice(self, language):
        return self.voice_id


def _pause_cfg(**extra):
    settings = {"bargein_confirm_timeout_s": "0.3",
                "bargein_resume_grace_s": "0.1",
                "tts_prebuffer_ms": "0", "tts_hold_max_s": "4"}
    settings.update({k: str(v) for k, v in extra.items()})
    return _cfg(**settings)


def _playing_pipeline(make_pipeline, llm=None, cfg=None):
    """Pipeline wired for an open-ended TTS segment (start it in the loop
    with ``_start_playing`` — task creation needs the running loop)."""
    p = make_pipeline(route=make_route(), llm=llm or FakeLLM(),
                      cfg=cfg or _pause_cfg())
    p.state._running = True
    p.state._stt_active = True
    p.tts = StreamTTS()
    return p


async def _wait_playing(p, timeout=2.0):
    t0 = time.monotonic()
    while not p.state._tts_playing:
        assert time.monotonic() - t0 < timeout, "playback never started"
        await asyncio.sleep(0.01)


async def _start_playing(p, timeout=2.0):
    p.tts.q.put_nowait(b"\x00" * 640)  # two 8k frames — playback starts
    p.state._tts_task = asyncio.create_task(p._stream_tts_audio())
    await _wait_playing(p, timeout)


def test_pause_parks_sender_and_resume_is_lossless(make_pipeline):
    """Speech-start pauses the sender in place (pause frame to the peer, no
    frames while parked); resume continues and delivers the rest."""
    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p._pause_playback()
        assert p.state._playback_paused
        assert getattr(p.conn, "pauses", 0) == 1
        await asyncio.sleep(0.06)
        sent_at_pause = len(p.conn.sent_audio)
        p.tts.q.put_nowait(b"\x00" * 640)  # synthesis continues while paused
        await asyncio.sleep(0.10)
        assert len(p.conn.sent_audio) == sent_at_pause, "sender must park"
        p._resume_playback("test")
        assert getattr(p.conn, "resumes", 0) == 1
        p.tts.q.put_nowait(None)  # end of synthesis
        await asyncio.wait_for(p.state._tts_task, timeout=3.0)
        assert len(p.conn.sent_audio) > sent_at_pause, "resume must deliver"
        assert not p.state._playback_paused

    asyncio.run(run())


def test_pause_commit_cancels_and_aborts_in_flight_turn(make_pipeline):
    """A confirmed transcript on a paused playback commits: TTS cancelled
    (no fade — nothing audible), upstream abort fired (turn in flight)."""
    llm = FakeLLM(llm_mode="proxy")
    p = _playing_pipeline(make_pipeline, llm=llm)

    async def run():
        await _start_playing(p)
        p._pause_playback()
        p.state._queued_speech.append("real words")
        p._interrupt_for_queued_speech()
        for _ in range(100):
            if not p.state._playback_paused and p.tts.cancelled:
                break
            await asyncio.sleep(0.01)
        assert p.tts.cancelled
        assert not p.state._playback_paused
        assert llm.aborts == 1
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_commit_after_engine_done_skips_upstream_abort(make_pipeline):
    """Liveness gate: a commit landing after the engine turn finished must
    not fire a session-scoped abort (it could kill an unrelated queued
    turn)."""
    llm = FakeLLM(llm_mode="proxy")
    llm.turn_in_flight = False
    p = _playing_pipeline(make_pipeline, llm=llm)

    async def run():
        await _start_playing(p)
        p._pause_playback()
        await p._cancel_tts()
        assert llm.aborts == 0
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_interim_evidence_never_commits_or_aborts(make_pipeline):
    """Audit F2 boundary: the interim fallback keeps the DISPATCH (queue
    append) but must never kill a paused playback or a generation."""
    llm = FakeLLM(llm_mode="proxy")
    p = _playing_pipeline(make_pipeline, llm=llm)

    async def run():
        await _start_playing(p)
        p._pause_playback()
        p.state._queued_speech.append("interim guess")
        p._interrupt_for_queued_speech(final_evidence=False)
        await asyncio.sleep(0.05)
        assert p.state._playback_paused, "interim must not commit"
        assert not p.tts.cancelled
        assert llm.aborts == 0
        p.tts.q.put_nowait(None)
        await p._cancel_tts()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_confirm_timeout_resumes_and_heals_phantom_vad(make_pipeline):
    """Stuck VAD (speech-start, no end, no transcript): the confirm backstop
    resumes playback and heals the silence flag — the 16 s wedge that killed
    turns AND taxed every later segment's hold gate."""
    from audio.providers.vad.base import VadState

    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p.vad.state = VadState.SPEAKING
        p.state._user_silent.clear()
        p._pause_playback()
        await asyncio.sleep(0.6)  # > 0.3 s confirm timeout, no evidence
        assert not p.state._playback_paused, "backstop must resume"
        assert getattr(p.conn, "resumes", 0) == 1
        assert p.state._user_silent.is_set(), "phantom wedge must heal"
        p.tts.q.put_nowait(None)
        await asyncio.wait_for(p.state._tts_task, timeout=3.0)

    asyncio.run(run())


def test_resume_grace_after_speech_end_without_final(make_pipeline):
    """SPEECH_END on a paused playback with no final inside the grace →
    playback resumes where it froze (the noise/cough case)."""
    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p._pause_playback()
        # SPEECH_END arms the grace task (core loop does this); user silent.
        p.state._pause_grace_task = asyncio.create_task(
            p._pause_resume_after_grace())
        await asyncio.sleep(0.3)  # > 0.1 s grace
        assert not p.state._playback_paused
        assert getattr(p.conn, "resumes", 0) == 1
        p.tts.q.put_nowait(None)
        await asyncio.wait_for(p.state._tts_task, timeout=3.0)

    asyncio.run(run())


def test_cleanup_while_paused_does_not_leak_or_hang(make_pipeline):
    """Teardown with a paused segment: _cleanup must cancel the pause
    machinery and the parked sender without deadlocking (edge 7)."""
    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p._pause_playback()
        await asyncio.wait_for(p._cleanup(), timeout=5.0)
        assert not p.state._playback_paused
        assert p.state._pause_confirm_timer is None

    asyncio.run(run())


def test_tool_boundary_commit_absorbs_cancel_and_unwinds(make_pipeline):
    """The Path-2 hole: a commit while the pre-tool segment awaits its drain
    must not abandon the token loop into a zombie context — the absorb is
    scoped and the turn unwinds to the completion tail."""
    from proxy.client import TOOL_USE_SIGNAL

    llm = FakeLLM(llm_mode="proxy", responses=[
        ["Let me check. ", TOOL_USE_SIGNAL, "The answer."],
    ])
    p = make_pipeline(route=make_route(), llm=llm, cfg=_pause_cfg())
    p.state._running = True
    p.state._stt_active = True
    p.state._warmup_done.set()
    p.tts = StreamTTS()
    p.tts.q.put_nowait(b"\x00" * 640)

    async def run():
        ut = asyncio.create_task(p._process_utterance("hi"))
        await _wait_playing(p, timeout=3.0)
        await asyncio.sleep(0.1)  # let the loop park at the segment await
        contexts_before = p.tts.contexts_started
        await p._cancel_tts()  # commit while the loop awaits the segment
        await asyncio.wait_for(ut, timeout=3.0)
        # No fresh post-tool streaming context after the commit (the zombie
        # sender that would speak the dead turn's post-tool text).
        assert p.tts.contexts_started == contexts_before
        assert llm.aborts >= 1  # server-side stop fired

    asyncio.run(run())


def test_tool_boundary_teardown_cancel_propagates(make_pipeline):
    """The scoped-absorb blocker: cancelling the utterance task itself while
    it awaits the pre-tool drain must PROPAGATE (an absorbed teardown cancel
    deadlocks _cleanup)."""
    from proxy.client import TOOL_USE_SIGNAL

    llm = FakeLLM(llm_mode="proxy", responses=[
        ["Let me check. ", TOOL_USE_SIGNAL, "The answer."],
    ])
    p = make_pipeline(route=make_route(), llm=llm, cfg=_pause_cfg())
    p.state._running = True
    p.state._stt_active = True
    p.state._warmup_done.set()
    p.tts = StreamTTS()
    p.tts.q.put_nowait(b"\x00" * 640)

    async def run():
        ut = asyncio.create_task(p._process_utterance("hi"))
        await _wait_playing(p, timeout=3.0)
        await asyncio.sleep(0.1)  # let the loop reach the segment await
        ut.cancel()
        await asyncio.wait([ut], timeout=3.0)
        # The task must END promptly (the token loop's own CancelledError
        # handler returns) — an absorbed teardown cancel would park it
        # forever and deadlock _cleanup. cancelled() vs clean-return is not
        # the contract; promptness is.
        assert ut.done(), "teardown cancel must not deadlock"
        if p.state._tts_task and not p.state._tts_task.done():
            p.state._tts_task.cancel()
            await asyncio.wait([p.state._tts_task], timeout=1.0)

    asyncio.run(run())


def test_short_episode_with_words_is_ignored_and_resumes(make_pipeline):
    """The duration gate (operator decision 2026-08-24, revived from the
    pre-pause design): an ack/cough episode shorter than bargein_timer_s is
    ignored ENTIRELY — queue cleared, nothing dispatched, no commit, no
    abort — and playback resumes where it froze. This is what keeps "ok" /
    "ναι" spoken over the reply from killing it."""
    llm = FakeLLM(llm_mode="proxy")
    p = _playing_pipeline(make_pipeline, llm=llm,
                          cfg=_pause_cfg(bargein_timer_s="0.6"))

    async def run():
        await _start_playing(p)
        p._pause_playback()
        p.stt._transcripts.append("ok")   # short ack, transcribed
        await asyncio.sleep(0.1)          # episode ends well under 0.6s
        handled = p._handle_pause_speech_end()
        assert handled is True
        assert not p.state._playback_paused, "short episode must resume"
        assert getattr(p.conn, "resumes", 0) == 1
        assert p.stt._transcripts == [], "queue must be cleared (discarded)"
        assert p.state._queued_speech == []
        assert not p.tts.cancelled
        assert llm.aborts == 0
        p.tts.q.put_nowait(None)
        await asyncio.wait_for(p.state._tts_task, timeout=3.0)

    asyncio.run(run())


def test_long_episode_arms_grace_and_falls_through(make_pipeline):
    """An episode >= bargein_timer_s is a real barge-in candidate: the
    handler arms the resume-grace and returns False so the listen loop
    falls through to normal transcript processing (commit on words)."""
    p = _playing_pipeline(make_pipeline,
                          cfg=_pause_cfg(bargein_timer_s="0.05"))

    async def run():
        await _start_playing(p)
        p._pause_playback()
        await asyncio.sleep(0.1)          # episode outlives the threshold
        handled = p._handle_pause_speech_end()
        assert handled is False
        assert p.state._playback_paused, "still paused — awaiting the final"
        assert p.state._pause_grace_task is not None
        await p._cancel_tts()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_confirm_monitor_holds_while_final_pending(make_pipeline):
    """The Patras live-hit (2026-08-24, duplex 7a092c59): a long utterance
    whose first phrase Deepgram finalized mid-speech CLEARED the interim and
    parked the words in the provider queue — the old wall-clock backstop saw
    "no evidence" and resumed the reply over the still-talking caller. The
    monitor must treat pending finals as live evidence and keep the pause."""
    from audio.providers.vad.base import VadState

    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p.vad.state = VadState.SPEAKING
        p.state._user_silent.clear()
        p._pause_playback()
        p.stt._transcripts.append("nice thank you and can you also check")
        await asyncio.sleep(0.45)  # well past the 0.3 s confirm timeout
        assert p.state._playback_paused, \
            "pending final = real speaker — no wall-clock resume"
        assert getattr(p.conn, "resumes", 0) == 0
        await p._cancel_tts()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_confirm_monitor_evolving_interim_never_resumes(make_pipeline):
    """A real monologue keeps the interim text evolving — the monitor must
    wait indefinitely (SPEECH_END resolution owns the ending), even far past
    every timeout multiple."""
    from audio.providers.vad.base import VadState

    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p.vad.state = VadState.SPEAKING
        p.state._user_silent.clear()
        p._pause_playback()
        for i in range(6):  # 1.2 s of growing partials (0.3 s timeout)
            p.stt.latest_interim = "and the final thing " * (i + 1)
            await asyncio.sleep(0.2)
            assert p.state._playback_paused, "evolving interim must hold"
        assert getattr(p.conn, "resumes", 0) == 0
        await p._cancel_tts()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_confirm_monitor_synthetic_speech_end_on_stuck_final(make_pipeline):
    """Wedge bound: VAD never fires SPEECH_END but a real final is waiting —
    the monitor runs the SPEECH_END processing synthetically (commit beats
    resume) instead of ever resuming over the caller's words. The synthetic
    path must skip the duration gate (audit A3) so the pending final is
    processed, not discarded."""
    from audio.providers.vad.base import VadState

    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p.vad.state = VadState.SPEAKING
        p.state._user_silent.clear()
        p._pause_playback()
        # Episode already old (past 2 x 0.3 s) with a stuck final.
        p.state._pause_speech_start = time.monotonic() - 1.0
        p.stt._transcripts.append("check the weather in patras")
        for _ in range(300):  # static evidence ages past 2 x timeout
            if p.state._turn_timer is not None or p.state._queued_speech:
                break
            await asyncio.sleep(0.01)
        assert (p.state._turn_timer is not None
                or p.state._queued_speech), \
            "synthetic speech end must route the final to dispatch"
        assert p.stt._transcripts == [], "the final must be drained, not lost"
        self_cancel = p.state._turn_timer
        if self_cancel is not None:
            self_cancel.cancel()
        await p._cancel_tts()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_confirm_monitor_stale_interim_resumes_bounded(make_pipeline):
    """A static, never-finalizing interim with nothing consuming it (VAD
    idle, retries spent) must not hold the pause forever — the bounded
    escape resumes with the silence flag healed."""
    p = _playing_pipeline(make_pipeline)

    async def run():
        await _start_playing(p)
        p.state._user_silent.clear()
        p._pause_playback()
        p.stt.latest_interim = "ghost partial"   # never changes, never finals
        for _ in range(300):
            if not p.state._playback_paused:
                break
            await asyncio.sleep(0.01)
        assert not p.state._playback_paused, "stale evidence must resume"
        assert p.state._user_silent.is_set()
        p.tts.q.put_nowait(None)
        await asyncio.wait_for(p.state._tts_task, timeout=3.0)

    asyncio.run(run())


def test_short_restart_after_long_episode_keeps_pending_final(make_pipeline):
    """Audit B2 (2026-08-24): a trailing-energy RESTART right after a real
    episode re-stamps the episode clock; its quick SPEECH_END must NOT
    discard the long episode's just-landed final — those words are the
    commit evidence. The pause's long-episode marker routes the short blip
    into normal processing instead of the ack-discard path."""
    p = _playing_pipeline(make_pipeline,
                          cfg=_pause_cfg(bargein_timer_s="0.6"))

    async def run():
        await _start_playing(p)
        p._pause_playback()
        p.state._pause_long_episode = True   # a real episode already ran
        p.stt._transcripts.append("please also check the weather in patras")
        await asyncio.sleep(0.05)            # the blip is way under 0.6 s
        handled = p._handle_pause_speech_end()
        assert handled is False, "must fall through to normal processing"
        assert p.stt._transcripts, "the pending final must survive"
        assert p.state._playback_paused, "no resume — the commit path owns it"
        await p._cancel_tts()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_interim_fallback_late_final_is_deduped(make_pipeline):
    """Paros double-send (live-hit 2026-08-25 00:45): the turn timer
    dispatched using the INTERIM fallback ("Paros"), then the real final
    ("Paros.") landed ~1s later, unconsumed, and re-dispatched the same
    word as its own turn. A final matching the recorded interim-dispatched
    text (normalized, tight window) must be dropped — once — while any
    OTHER final still dispatches."""
    p = _playing_pipeline(make_pipeline)

    async def run():
        # The flush site recorded the interim-dispatched text.
        p.state._interim_fallback_text = "Paros"
        p.state._interim_fallback_at = time.monotonic()

        # Busy turn (utterance in flight) — the late final rides the
        # delayed queue retry, exactly the live sequence.
        async def _parked():
            await asyncio.sleep(30)
        p.state._utterance_task = asyncio.create_task(_parked())
        try:
            p.stt._transcripts.append("Paros.")
            await p._delayed_queue_retry()
            assert list(p.state._queued_speech) == [], \
                "the late duplicate final must be dropped"
            assert p.state._interim_fallback_text == "", "one-shot consume"

            # A DIFFERENT final right after still dispatches normally.
            p.stt._transcripts.append("and Naxos too")
            await p._delayed_queue_retry()
            assert list(p.state._queued_speech) == ["and Naxos too"]
        finally:
            p.state._utterance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await p.state._utterance_task
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_interim_fallback_window_expires(make_pipeline):
    """Outside the 2s window the same words are genuinely NEW speech
    (the user repeating themselves) and must dispatch."""
    p = _playing_pipeline(make_pipeline)
    p.state._interim_fallback_text = "Paros"
    p.state._interim_fallback_at = time.monotonic() - 3.0
    assert p._is_interim_fallback_dup("Paros.") is False
    p.state._interim_fallback_at = time.monotonic()
    assert p._is_interim_fallback_dup("check Paros please") is False
    assert p._is_interim_fallback_dup("PAROS!") is True
