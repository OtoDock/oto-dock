"""The mid-utterance TTS flush: a complete sentence under the provider's
first-generation threshold plays while the engine pauses or runs a tool,
instead of waiting for the end of input.

Two flush points in the token loop: a tool boundary met with an empty text
buffer and text already with the TTS, and a pause of ``TTS_PAUSE_FLUSH_S``
with no token after a sentence-final chunk (a sentence-final buffer is sent
first). Never mid-sentence, never with nothing sent. The token task that
reads the engine survives the pause: the turn goes on when the engine does.
"""

import asyncio

import pytest
from pipeline_fakes import FakeLLM, make_route

from pipeline import llm as llm_mod
from proxy.client import TOOL_USE_SIGNAL


class HoldingLLM(FakeLLM):
    """A FakeLLM whose stream parks on an ``asyncio.Event`` placed among the
    tokens until the test sets it (the engine thinking or running a tool)."""

    async def send_message(self, text):
        self.prompts.append(text)
        tokens = self._responses.pop(0) if self._responses else []
        for i, token in enumerate(tokens):
            if isinstance(token, asyncio.Event):
                await token.wait()
                continue
            if self.on_token:
                self.on_token(text, i, token)
            yield token


@pytest.fixture
def quick_pause(monkeypatch):
    monkeypatch.setattr(llm_mod, "TTS_PAUSE_FLUSH_S", 0.05)


def _pipeline(make_pipeline, tokens):
    llm = HoldingLLM(responses=[tokens])
    p = make_pipeline(route=make_route(), llm=llm)
    p.state._warmup_done.set()
    return p, llm


async def _turn(p, text="hello", *, release=None, after=0.3):
    if release is not None:
        asyncio.get_running_loop().call_later(after, release.set)
    p.state._utterance_task = asyncio.current_task()
    await asyncio.wait_for(p._process_utterance(text), timeout=5)


def test_a_tool_boundary_with_an_empty_buffer_flushes_the_sentence_with_the_tts(make_pipeline, quick_pause):
    """The sentence went out as a chunk (over the buffer size, with a
    sentence end) before the tool frame arrived with nothing more: the TTS
    is flushed at the boundary and the context stays open for the text
    after the tool."""
    p, llm = _pipeline(make_pipeline, [
        "Let me check the cameras. ", TOOL_USE_SIGNAL, "All clear. ", "Nothing is moving out there. "])

    asyncio.run(_turn(p))

    assert p.tts.text_chunks[0] == ("Let me check the cameras. ", False)
    assert p.tts.flushes == [1]
    assert p.tts.contexts_started == 1
    assert p.tts.text_chunks[-1][1] is True


def test_a_pause_after_a_sentence_final_chunk_flushes_and_the_turn_goes_on(make_pipeline, quick_pause):
    """The engine goes quiet after a complete sentence already with the TTS:
    the flush fires after the pause; the token task survived it, so the
    second sentence is spoken when the engine resumes and the stream ends
    normally."""
    hold = asyncio.Event()
    p, llm = _pipeline(make_pipeline, ["First sentence here, said. ", hold, "Second sentence here, too. "])

    asyncio.run(_turn(p, release=hold, after=0.3))

    assert p.tts.flushes == [1]
    assert p.tts.text_chunks == [
        ("First sentence here, said. ", False), ("Second sentence here, too. ", False), ("", True)]
    assert p.state._full_response == "First sentence here, said. Second sentence here, too. "


def test_a_pause_with_a_sentence_final_buffer_sends_it_then_flushes(make_pipeline, quick_pause):
    """A short goodbye under the buffer size never left the daemon before:
    at the pause it goes to the TTS and the flush makes it play."""
    hold = asyncio.Event()
    p, llm = _pipeline(make_pipeline, ["Bye now.", hold])

    asyncio.run(_turn(p, release=hold, after=0.3))

    assert p.tts.text_chunks[0] == ("Bye now.", False)
    assert p.tts.flushes == [1]
    assert p.tts.text_chunks[-1] == ("", True)


def test_a_pause_never_flushes_mid_sentence(make_pipeline, quick_pause):
    """Neither a buffer without a sentence end nor a chunk cut at the hard
    size limit is flushed: the sentence is not finished."""
    hold = asyncio.Event()
    p, llm = _pipeline(make_pipeline, ["Let me think about this for", hold, " a moment. "])
    asyncio.run(_turn(p, release=hold, after=0.3))
    assert p.tts.flushes == []
    assert p.tts.text_chunks == [("Let me think about this for a moment. ", False), ("", True)]

    hold2 = asyncio.Event()
    long_cut = "a" * 61 + " and then"
    p2, llm2 = _pipeline(make_pipeline, [long_cut, hold2, " it ends. "])
    asyncio.run(_turn(p2, release=hold2, after=0.3))
    assert p2.tts.text_chunks[0] == (long_cut, False)      # the hard cut, mid-sentence
    assert p2.tts.flushes == []


def test_a_pause_with_nothing_sent_flushes_nothing(make_pipeline, quick_pause):
    hold = asyncio.Event()
    p, llm = _pipeline(make_pipeline, [hold, "Here you go. "])

    asyncio.run(_turn(p, release=hold, after=0.3))

    assert p.tts.flushes == []
    assert p.tts.text_chunks == [("Here you go.", True)]


def test_a_pause_flush_fires_once_until_more_text_is_sent(make_pipeline, quick_pause):
    """Several pause periods after one sentence: one flush, not one per
    period; the next sentence arms the next flush."""
    hold = asyncio.Event()
    p, llm = _pipeline(make_pipeline, [
        "First sentence here, said. ", hold, "Second sentence here, too. ", asyncio.Event()])
    tail = llm._responses[0][-1]

    async def run():
        asyncio.get_running_loop().call_later(0.5, hold.set)
        asyncio.get_running_loop().call_later(0.9, tail.set)
        await _turn(p)
    asyncio.run(run())

    assert p.tts.flushes == [1, 2]


def test_the_sentence_final_rule():
    final = llm_mod._SENTENCE_FINAL_RE.search
    assert final("Goodbye.") and final("Really?") and final("Ευχαριστώ;") and final("Done! ")
    assert final('He said "bye."') and final("(sure.) ") and final("Fine…")
    assert not final("Goodbye") and not final("3.5 million") and not final("Dr. ") is None
    assert not final("a sentence cut at sixty chars and") and not final("")


def test_playback_goes_idle_in_a_gap_of_the_open_context_and_resumes(make_pipeline, monkeypatch):
    """A flushed short sentence played and the context stays open: once the
    audio drained, playback is idle (barge-in off, the line is the
    caller's), and the next chunk marks it playing again."""
    from pipeline import playback as playback_mod
    monkeypatch.setattr(playback_mod, "_PLAYBACK_IDLE_S", 0.05)
    p = make_pipeline(route=make_route(), llm=FakeLLM())
    gap = asyncio.Event()
    frame = b"\x00\x00" * 160

    async def receive():
        yield frame
        await gap.wait()
        yield frame
    p.tts.receive_audio = receive
    states = []
    p._on_tts_playback_start = lambda: states.append("speaking")
    p._on_tts_playback_end = lambda: states.append("listening")
    modes = []
    p.vad.set_bargein_mode = modes.append

    async def run():
        p.state._running = True
        p.state._tts_flushed_open = True
        task = asyncio.create_task(p._stream_tts_audio())
        await asyncio.sleep(1.0)                       # the breath, the frame, the gap
        idle = (p.state._tts_playing, modes[-1], states[-1], len(p.conn.sent_audio),
                p.state._tts_idle)
        gap.set()
        await asyncio.wait_for(task, timeout=3)
        return idle
    idle = asyncio.run(run())

    breath_frames = idle[3] - 1
    assert idle == (False, False, "listening", breath_frames + 1, True)   # the frame played, then idle
    assert p.state._tts_idle is False
    assert states == ["speaking", "listening", "speaking", "listening"]
    assert modes == [True, False, True]                   # the segment's caller resets the mode at its end
    assert len(p.conn.sent_audio) == breath_frames + 2        # the second frame played after the gap


def test_a_cancel_during_the_idle_gap_ends_the_segment(make_pipeline, monkeypatch):
    from pipeline import playback as playback_mod
    monkeypatch.setattr(playback_mod, "_PLAYBACK_IDLE_S", 0.05)
    p = make_pipeline(route=make_route(), llm=FakeLLM())
    gap = asyncio.Event()

    async def receive():
        yield b"\x00\x00" * 160
        await gap.wait()
        yield b"\x00\x00" * 160
    p.tts.receive_audio = receive

    p._breath_pcm = None

    async def run():
        p.state._running = True
        p.state._tts_flushed_open = True
        p.state._tts_task = asyncio.create_task(p._stream_tts_audio())
        await asyncio.sleep(0.4)
        assert p.state._tts_playing is False and p.state._tts_idle
        await p._cancel_tts()
        return p.state._tts_task
    task = asyncio.run(run())

    assert task is None and p.tts.cancelled and p.state._utterance_cancelled
    assert not gap.is_set()


def _gated_segment(make_pipeline, monkeypatch, *, frames=2):
    """A pipeline whose provider yields one frame, then holds on ``gap``
    before the next: the shape of a flushed sentence that drained while the
    engine works."""
    from pipeline import playback as playback_mod
    monkeypatch.setattr(playback_mod, "_PLAYBACK_IDLE_S", 0.05)
    p = make_pipeline(route=make_route(), llm=FakeLLM(llm_mode="proxy"))
    gap = asyncio.Event()
    frame = b"\x00\x00" * 160

    async def receive():
        yield frame
        await gap.wait()
        for _ in range(frames - 1):
            yield frame
    p.tts.receive_audio = receive
    p._breath_pcm = None
    p.cfg.load({"settings": {"tts_hold_max_s": "4.0", "tts_prebuffer_ms": "0"}, "routes": []})
    return p, gap


def test_an_engine_stall_without_a_flush_never_goes_idle(make_pipeline, monkeypatch):
    """Nothing was flushed (the provider holds text under its threshold, or
    the engine stalled mid-sentence): a gap in the audio is a gap, not an
    idle playback. Barge-in stays on, the UI stays 'speaking'."""
    p, gap = _gated_segment(make_pipeline, monkeypatch)
    states = []
    p._on_tts_playback_start = lambda: states.append("speaking")
    p._on_tts_playback_end = lambda: states.append("listening")

    async def run():
        p.state._running = True
        task = asyncio.create_task(p._stream_tts_audio())
        await asyncio.sleep(0.4)
        mid = (p.state._tts_playing, p.state._tts_idle, list(states))
        gap.set()
        await asyncio.wait_for(task, timeout=3)
        return mid
    mid = asyncio.run(run())

    assert mid == (True, False, ["speaking"])
    assert states == ["speaking", "listening"] and len(p.conn.sent_audio) == 2


def test_the_chunk_after_an_idle_gap_waits_for_the_callers_silence(make_pipeline, monkeypatch):
    """The caller started speaking over the idle gap (nothing was playing,
    so no pause): the next chunk waits for their silence, playback stays
    off meanwhile (the STT keeps the live feed), and plays once they stop."""
    from audio.providers.vad.base import VadState
    p, gap = _gated_segment(make_pipeline, monkeypatch)

    async def run():
        p.state._running = True
        p.state._tts_flushed_open = True
        task = asyncio.create_task(p._stream_tts_audio())
        await asyncio.sleep(0.3)
        assert p.state._tts_idle and len(p.conn.sent_audio) == 1
        p.state._user_silent.clear()                   # the caller is speaking
        p.vad.state = VadState.SPEAKING
        gap.set()                                      # the post-tool chunk arrives
        await asyncio.sleep(0.4)
        waiting = (p.state._tts_playing, p.state._tts_idle, len(p.conn.sent_audio),
                   task.done())
        p.vad.state = VadState.IDLE
        p.state._user_silent.set()                     # they stopped
        await asyncio.wait_for(task, timeout=3)
        return waiting
    waiting = asyncio.run(run())

    assert waiting == (False, True, 1, False)          # held: not playing, nothing sent
    assert len(p.conn.sent_audio) == 2 and p.state._stt_early_unmuted is False


def test_a_talker_still_going_at_the_resume_keeps_the_stt_unmuted(make_pipeline, monkeypatch):
    """The hold released on the cap with the VAD still reading speech (no
    transcript evidence): the resume keeps the STT on the live feed, as a
    segment's first chunk does."""
    from audio.providers.vad.base import VadState
    p, gap = _gated_segment(make_pipeline, monkeypatch)
    p.cfg.load({"settings": {"tts_hold_max_s": "0.2", "tts_prebuffer_ms": "0"}, "routes": []})

    async def run():
        p.state._running = True
        p.state._tts_flushed_open = True
        task = asyncio.create_task(p._stream_tts_audio())
        await asyncio.sleep(0.3)
        p.state._user_silent.clear()
        p.vad.state = VadState.SPEAKING
        gap.set()
        await asyncio.wait_for(task, timeout=3)        # the cap released the hold
    asyncio.run(run())

    assert p.state._stt_early_unmuted is True and len(p.conn.sent_audio) == 2


def test_a_receiver_that_ended_under_an_open_context_gets_a_fresh_one(make_pipeline, quick_pause):
    """The provider's receiver ended before the end of input (its socket
    died across a long tool run): the text still to come opens a fresh
    context and receiver instead of going into a context nobody reads."""
    hold = asyncio.Event()
    p, llm = _pipeline(make_pipeline, ["First sentence here, said. ", hold, "Second sentence here, too. "])
    p._breath_pcm = None
    original = p.tts.receive_audio
    calls = []

    async def receive():
        calls.append(1)
        gen = original()
        if len(calls) == 1:
            # The first context: its first audio, then the socket is gone.
            async for c in gen:
                yield c
                return
        async for c in gen:
            yield c
    p.tts.receive_audio = receive

    asyncio.run(_turn(p, release=hold, after=0.6))

    assert p.tts.contexts_started == 2 and p.tts.flushes == [1]
    assert [t for t, _ in p.tts.text_chunks] == [
        "First sentence here, said. ", "Second sentence here, too. ", ""]
    assert p.tts.text_chunks[-1][1] is True            # the end of input on the fresh context
    assert len(calls) == 2 and len(p.conn.sent_audio) == 2


def test_queued_speech_during_an_idle_segment_never_aborts_the_turn(make_pipeline):
    """Idle after a flushed sentence, the engine about to call the end tool:
    the caller's "thanks, bye" is queued and handled at the stream's end,
    never an interrupt-style abort of the generation (which would re-dispatch
    the words and produce a second goodbye)."""
    llm = FakeLLM(llm_mode="proxy")
    p = make_pipeline(route=make_route(llm_mode="proxy"), llm=llm)

    async def run():
        p.state._tts_idle = True
        p.state._tts_playing = False
        p.state._queued_speech.append("thanks bye")
        p._interrupt_for_queued_speech()
        await asyncio.sleep(0.05)
        idle_aborts = llm.aborts
        p.state._tts_idle = False                      # the silent tool phase, as before
        p._interrupt_for_queued_speech()
        await asyncio.sleep(0.05)
        return idle_aborts, llm.aborts
    assert asyncio.run(run()) == (0, 1)


def test_the_cleanup_cancels_an_idle_segment(make_pipeline, monkeypatch):
    p, gap = _gated_segment(make_pipeline, monkeypatch)

    async def run():
        p.state._running = True
        p.state._tts_flushed_open = True
        p.state._tts_task = asyncio.create_task(p._stream_tts_audio())
        await asyncio.sleep(0.3)
        assert p.state._tts_idle
        await p._cleanup()
        return p.state._tts_task
    task = asyncio.run(run())

    assert task is None and p.tts.cancelled and not gap.is_set()


def _duplex_session(monkeypatch):
    from pipeline_fakes import FakeSTT, FakeTTS, FakeVAD

    from config_manager import ConfigManager
    from duplex import session as dsession
    from pipeline import providers as plproviders

    monkeypatch.setattr(dsession, "SileroVad", FakeVAD)
    monkeypatch.setattr(
        plproviders, "get_provider_class",
        lambda kind, name: FakeSTT if kind == "stt" else FakeTTS,
    )

    class FakeDuplexConn:
        call_uuid = "dx-idle-test"
        peer_addr = "duplex:idle"
        sample_rate_in = 16000
        sample_rate_out = 24000
        frame_bytes_out = 960
        is_closed = False
        on_barge_in = None

        def __init__(self):
            self.events = []
            self.sent_audio = []

        def send_event(self, payload):
            self.events.append(payload)

        def send_audio(self, pcm):
            self.sent_audio.append(pcm)

        def flush_playback(self):
            pass

        def pause_playback(self):
            pass

        def resume_playback(self):
            pass

        async def drain(self):
            pass

    return dsession.DuplexSession(
        FakeDuplexConn(), ConfigManager(),
        {"stt": {"provider_name": "deepgram"}, "tts": {"provider_name": "cartesia"},
         "language": "en", "chat_endpointing_ms": 1500},
    )


def test_a_tap_during_an_idle_segment_cuts_it_and_ends_the_context(monkeypatch):
    """The duplex Stop tap while a flushed sentence's segment is idle with
    its context open: the TTS is cancelled and the context ended, as over
    audible playback; a tap while thinking keeps its own path."""
    s = _duplex_session(monkeypatch)
    hold = asyncio.Event()

    async def run():
        s.state._tts_idle = True
        s.state._tts_task = asyncio.create_task(hold.wait())
        await s._on_manual_barge_in()
        return s.state._tts_task
    task = asyncio.run(run())

    assert task is None and s.tts.cancelled and s.state._utterance_cancelled


def test_a_typed_message_while_the_turn_is_busy_is_stamped(monkeypatch):
    """A typed or released message queued behind a busy turn carries its
    time: the farewell check reads it beside the voice stamp."""
    import time
    s = _duplex_session(monkeypatch)
    hold = asyncio.Event()

    async def run():
        s.state._utterance_task = asyncio.create_task(hold.wait())
        before = time.monotonic()
        await s._on_typed_utterance("one more thing")
        typed_at = s.state._typed_input_at
        await s._on_release("and another")
        hold.set()
        return before, typed_at, s.state._typed_input_at
    before, typed_at, released_at = asyncio.run(run())

    assert typed_at >= before and released_at >= typed_at
    assert s.state._queued_speech == ["one more thing", "and another"]
