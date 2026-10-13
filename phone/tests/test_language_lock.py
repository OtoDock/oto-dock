"""The caller's language, identified once per call and locked
(``pipeline/language.py``): a route that detects starts in its own language,
sends the first long-enough utterance to the STT's batch identification and
moves the voice, the fillers and the stream to a confident, speakable
answer; anything else keeps the route's language."""

import asyncio

from audio.providers.stt.base import LanguageGuess
from pipeline import language as pl
from pipeline_fakes import FakeLLM, make_route

SECOND = 8000 * 2          # bytes of 8 kHz PCM16 per second


async def _no_wait(segments):
    return 0.0


def _detecting_route(language="en"):
    route = make_route(language=language)
    route.language_detect = True
    return route


def _pipeline(make_pipeline, *, route=None, voices=None, guess=None, llm=None):
    p = make_pipeline(route=route or _detecting_route(), llm=llm or FakeLLM())
    p.tts.voices = voices if voices is not None else {"en": "v-en", "el": "v-el"}
    p.stt.guess = guess
    p.state._stt_active = True
    return p


async def _identify(p, seconds=1.5):
    """A speech end with ``seconds`` of voiced audio: the new identification
    task, awaited, or None when none started."""
    before = p.state._language_task
    p.state._speech_audio_buf = bytearray(b"\x01\x00" * int(seconds * 8000))
    p._consider_language_identification()
    task = p.state._language_task
    if task is None or task is before:
        return None
    await task
    return task


def test_a_route_without_detection_is_locked_at_construction(make_pipeline):
    p = make_pipeline(route=make_route(language="en"))
    assert p.state._language_locked is True
    p.stt.guess = LanguageGuess("el", 0.99, "Καλημέρα σας λέω")
    assert asyncio.run(_identify(p)) is None
    assert p.stt.identified == []


def test_a_short_utterance_is_not_sent(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.99, "ναι"))
    assert asyncio.run(_identify(p, seconds=0.8)) is None
    assert p.stt.identified == [] and p.state._language_locked is False


def test_a_confident_other_language_moves_voice_fillers_and_stream(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.95, "Καλημέρα θέλω ένα ραντεβού"))
    asyncio.run(_identify(p))

    assert p.state._language_locked is True and p._locked_language == "el"
    assert p.tts.voice_id == "v-el"
    assert p.stt.restarted == ["el"]
    assert "el" in p._filler_key
    assert p.stt.identified and p.stt.identified[0][:4] == b"RIFF"
    assert p._take_language_first_text() == "Καλημέρα θέλω ένα ραντεβού"
    assert p._take_language_first_text() == ""        # once


def test_the_route_language_confirmed_locks_without_a_restart(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("en", 0.7, "hello there"))
    asyncio.run(_identify(p))
    assert p.state._language_locked is True and p._locked_language == "en"
    assert p.stt.restarted == []
    assert p._take_language_first_text() == ""


def test_an_unsure_answer_keeps_the_route_until_the_attempts_run_out(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.5, "κάτι είπα εδώ"))

    async def run():
        await _identify(p)
        assert p.state._language_locked is False and p.state._language_attempts == 1
        await _identify(p)
        assert p.state._language_locked is False and p.state._language_attempts == 2
        await _identify(p)
        assert p.state._language_locked is True and p._locked_language == "en"
        # Exhausted: no further identification.
        assert await _identify(p) is None
    asyncio.run(run())
    assert len(p.stt.identified) == 3 and p.stt.restarted == []


def test_one_word_is_not_enough_to_move(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.97, "Καλημέρα"))
    asyncio.run(_identify(p))
    assert p._locked_language == "en" and p.stt.restarted == []
    assert p.state._language_locked is False and p.state._language_attempts == 1


def test_no_voice_for_the_language_keeps_the_route(make_pipeline):
    p = _pipeline(make_pipeline, voices={"en": "v-en"}, guess=LanguageGuess("el", 0.97, "Καλημέρα σας λέω"))
    asyncio.run(_identify(p))
    assert p._locked_language == "en" and p.stt.restarted == []


def test_a_language_the_stream_cannot_transcribe_keeps_the_route(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.97, "Καλημέρα σας λέω"))
    p.stt.streamable = {"en"}
    asyncio.run(_identify(p))
    assert p._locked_language == "en" and p.stt.restarted == []


def test_no_answer_counts_as_an_attempt(make_pipeline):
    p = _pipeline(make_pipeline, guess=None)
    asyncio.run(_identify(p))
    assert p.state._language_attempts == 1 and p.state._language_locked is False


def test_a_failed_identification_is_an_attempt_too(make_pipeline):
    p = _pipeline(make_pipeline)

    async def boom(audio_wav):
        raise RuntimeError("401")
    p.stt.identify_language = boom
    asyncio.run(_identify(p))
    assert p.state._language_attempts == 1 and p.state._language_locked is False


def test_one_identification_at_a_time(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.95, "Καλημέρα σας λέω"))
    p.stt.identify_delay_s = 0.05

    async def run():
        p.state._speech_audio_buf = bytearray(b"\x01\x00" * 12000)
        p._consider_language_identification()
        first = p.state._language_task
        p._consider_language_identification()          # a second speech end while it runs
        assert p.state._language_task is first
        await first
    asyncio.run(run())
    assert len(p.stt.identified) == 1


def test_frames_during_the_restart_are_kept_and_replayed(make_pipeline):
    p = _pipeline(make_pipeline)
    gate = asyncio.Event()

    async def slow_restart(language):
        await gate.wait()
        p.stt.restarted.append(language)
        return True
    p.stt.restart = slow_restart

    async def run():
        task = asyncio.create_task(p._restart_stt("el"))
        await asyncio.sleep(0)
        assert p.state._stt_restarting is True
        await p._stt_feed(b"\x01\x00" * 160)
        await p._stt_feed(b"\x02\x00" * 160)
        assert p.stt.sent == []                           # nothing reaches a closed stream
        gate.set()
        await task
    asyncio.run(run())
    assert p.state._stt_restarting is False
    assert p.stt.sent == [b"\x01\x00" * 160 + b"\x02\x00" * 160]
    assert p.stt.restarted == ["el"]


def test_a_failed_restart_drops_the_buffer_and_keeps_listening_state(make_pipeline):
    p = _pipeline(make_pipeline)
    p.stt.restart_ok = False

    async def run():
        await p._restart_stt("el")
        await p._stt_feed(b"\x03\x00" * 160)
    asyncio.run(run())
    assert p.state._stt_restarting is False
    assert p.stt.sent == [b"\x03\x00" * 160]              # the live feed resumed


def test_the_first_dispatch_waits_for_the_identification_and_takes_its_text(make_pipeline):
    llm = FakeLLM(responses=[["Γεια σας!"]])
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.95, "Καλημέρα θέλω ένα ραντεβού"), llm=llm)
    p.stt.identify_delay_s = 0.05
    p.state._warmup_done.set()
    p._classify_turn = _no_wait

    async def run():
        p.state._speech_audio_buf = bytearray(b"\x01\x00" * 12000)
        p._consider_language_identification()            # still running at dispatch
        p.state._turn_segments.append("call him there I want to run the view")
        p.state._user_silent.set()
        await p._turn_timeout_handler("call him there I want to run the view")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts and llm.prompts[0] == "Καλημέρα θέλω ένα ραντεβού"
    assert p._locked_language == "el"


def test_a_slow_identification_does_not_hold_the_dispatch(make_pipeline, monkeypatch):
    monkeypatch.setattr(pl, "DISPATCH_WAIT_S", 0.02)
    llm = FakeLLM(responses=[["Hello!"]])
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.95, "Καλημέρα θέλω ένα ραντεβού"), llm=llm)
    p.stt.identify_delay_s = 0.2
    p.state._warmup_done.set()
    p._classify_turn = _no_wait

    async def run():
        p.state._speech_audio_buf = bytearray(b"\x01\x00" * 12000)
        p._consider_language_identification()
        p.state._turn_segments.append("hello there")
        p.state._user_silent.set()
        await p._turn_timeout_handler("hello there")
        for _ in range(3):
            await asyncio.sleep(0)
        p.state._language_task.cancel()
    asyncio.run(run())
    assert llm.prompts and llm.prompts[0] == "hello there"


def test_cleanup_cancels_a_running_identification(make_pipeline):
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.95, "Καλημέρα σας λέω"))
    p.stt.identify_delay_s = 5.0

    async def run():
        p.state._speech_audio_buf = bytearray(b"\x01\x00" * 12000)
        p._consider_language_identification()
        task = p.state._language_task
        await asyncio.sleep(0)
        await p._cleanup()
        assert task.cancelled() and p.state._language_task is None
    asyncio.run(run())


def test_a_late_identification_never_replaces_the_next_utterance(make_pipeline, monkeypatch):
    """The identification lands after its turn was dispatched (a slow batch
    call): the lock still happens, but its text belongs to the turn it heard,
    never to the caller's next words."""
    monkeypatch.setattr(pl, "DISPATCH_WAIT_S", 0.02)
    llm = FakeLLM(responses=[["Hello!"], ["Sure."]])
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.95, "Καλημέρα θέλω ένα ραντεβού"), llm=llm)
    p.stt.identify_delay_s = 0.1
    p.state._warmup_done.set()
    p._classify_turn = _no_wait

    async def run():
        p.state._speech_audio_buf = bytearray(b"\x01\x00" * 12000)
        p._consider_language_identification()
        p.state._turn_segments.append("first utterance")
        p.state._user_silent.set()
        await p._turn_timeout_handler("first utterance")          # goes ahead with the stream's text
        await p.state._language_task                              # the lock lands afterwards
        assert p._locked_language == "el"
        if p.state._utterance_task is not None:
            await asyncio.wait_for(p.state._utterance_task, 5.0)   # the first turn's reply played
        p.state._turn_segments.append("second utterance")
        await p._turn_timeout_handler("second utterance")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["first utterance", "second utterance"]


def test_a_truncated_buffer_locks_the_language_but_keeps_the_streams_text(make_pipeline):
    """A long first utterance overran the audio window: the identification
    heard its tail only, so the stream's text stays the turn's."""
    llm = FakeLLM(responses=[["Γεια σας!"]])
    p = _pipeline(make_pipeline, guess=LanguageGuess("el", 0.95, "ραντεβού για αύριο"), llm=llm)
    p.state._warmup_done.set()
    p._classify_turn = _no_wait

    async def run():
        p.state._speech_audio_buf = bytearray(b"\x01\x00" * 12000)
        p.state._speech_audio_truncated = True
        p._consider_language_identification()
        await p.state._language_task
        assert p._locked_language == "el" and p.stt.restarted == ["el"]
        p.state._turn_segments.append("a long opening sentence that the window cut")
        p.state._user_silent.set()
        await p._turn_timeout_handler("a long opening sentence that the window cut")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["a long opening sentence that the window cut"]
