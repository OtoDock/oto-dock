"""The caller's language, read from the audio while they speak
(``pipeline/language.py``): a route that detects runs the local identifier
on the voiced audio, opens a candidate stream on a lean, switches the call
at SURE (or on a lean at the first turn's take), and settles after a few
stable seconds. A scripted identifier drives the real rule, tracker and
streams over the test fakes."""

import asyncio
import logging
import math
import time

import numpy as np
import pytest
from audio.providers.lid import rule as R
from audio.providers.stt import streams as S
from pipeline_fakes import FakeLLM, make_route

from pipeline import language as pl

RATE = 8000
HALF_SECOND = b"\x10\x00" * (RATE // 2)      # 0.5 s of 8 kHz PCM16


class ScriptedIdentifier:
    """Answers one fixed distribution (the languages the pipeline knows);
    records every window."""

    labels = ["en", "el", "de", "es", "fr", "it"]

    def __init__(self, probs: dict[str, float]):
        self.probs = probs
        self.windows: list[np.ndarray] = []

    def scores(self, pcm_16k):
        self.windows.append(pcm_16k)
        return np.array([math.log(self.probs.get(lang, 1e-6)) for lang in self.labels])


def _detecting_route(language="en"):
    route = make_route(language=language)
    route.language_detect = True
    return route


async def _no_wait(segments):
    return 0.0


@pytest.fixture
def detecting(make_pipeline, monkeypatch):
    """A detecting pipeline whose identifier answers ``probs``."""

    def _make(probs, *, route=None, voices=None, llm=None):
        ident = ScriptedIdentifier(probs)
        monkeypatch.setattr(pl, "identifier_available", lambda: True)
        monkeypatch.setattr(pl, "loaded_model", lambda num_threads=1: object())
        monkeypatch.setattr(pl, "EcapaIdentifier", lambda **kw: ident)
        p = make_pipeline(route=route or _detecting_route(), llm=llm or FakeLLM())
        p.tts.voices = voices if voices is not None else {"en": "v-en", "el": "v-el", "de": "v-de"}
        p.state._warmup_done.set()
        p._classify_turn = _no_wait
        return p, ident

    return _make


async def _start(p):
    await p.stt.start(language="en", sample_rate=RATE, interim_results=True)
    p.state._stt_active = True


async def _speak(p, seconds: float):
    """Voiced audio in half-second pieces, each reading awaited and applied."""
    for _ in range(int(seconds * 2)):
        await p._stt_feed(HALF_SECOND, voiced=True)
        task = p._language_tracker._task
        if task is not None and not task.done():
            await task
        p._language_poll()
        await asyncio.sleep(0)


def _stream(p, lang):
    return p.stt._streams[lang].provider


async def _candidate_answers(p, lang, final: str | None = None, interim: str = ""):
    """The candidate's open task finished and the vendor answered."""
    task = p.state._language_open_task
    if task is not None:
        await task
    fake = _stream(p, lang)
    if final:
        fake._transcripts.append(final)
    fake.latest_interim = interim
    fake.last_result_monotonic = time.monotonic()
    p._language_poll()


def test_a_route_without_detection_keeps_a_plain_provider(make_pipeline):
    p = make_pipeline(route=make_route(language="en"))
    assert p._language_tracker is None and not isinstance(p.stt, S.LanguageStreams)
    assert p.state._language_locked is True


def test_detection_is_off_when_the_identifier_is_unavailable(make_pipeline, monkeypatch):
    monkeypatch.setattr(pl, "identifier_available", lambda: False)
    p = make_pipeline(route=_detecting_route())
    assert p._language_tracker is None and not isinstance(p.stt, S.LanguageStreams)
    assert p.state._language_locked is True


def test_detection_is_off_for_a_call_that_lands_before_the_model_is_loaded(make_pipeline, monkeypatch):
    """The constructor runs on the loop and never loads the model: a call
    before the pre-warm finished runs in the route's language."""
    monkeypatch.setattr(pl, "identifier_available", lambda: True)
    monkeypatch.setattr(pl, "loaded_model", lambda num_threads=1: None)
    p = make_pipeline(route=_detecting_route())
    assert p._language_tracker is None and not isinstance(p.stt, S.LanguageStreams)
    assert p.state._language_locked is True


def test_a_route_language_the_model_does_not_know_keeps_the_plain_provider(detecting):
    """The route API takes any string: a language the identifier has no
    label for (or an empty one) must not break the call's setup."""
    for language in ("he", ""):
        p, _ = detecting({"en": 0.9, "el": 0.05, "de": 0.05}, route=_detecting_route(language))
        assert p._language_tracker is None and not isinstance(p.stt, S.LanguageStreams)
        assert p.state._language_locked is True


def test_the_identifier_threads_setting_is_clamped_and_never_raises():
    import os

    from config_manager import ConfigManager
    cfg = ConfigManager()
    for raw, want in (("0", 1), ("-3", 1), ("abc", 1), ("", 1), ("2", min(2, os.cpu_count() or 1)), ("9999", os.cpu_count() or 1)):
        cfg._data = {"settings": {"language_identify_threads": raw}}
        assert cfg.language_identify_threads == want


def test_a_detecting_route_builds_the_streams_and_waits_for_the_first_words(detecting):
    p, ident = detecting({"en": 0.9, "el": 0.05, "de": 0.05})
    assert isinstance(p.stt, S.LanguageStreams) and p.stt.active == "en"
    assert p.state._language_locked is False
    assert p._language_tracker.rule.switch_at == R.SURE and p._language_tracker.rule.lean_at_take
    assert p._language_tracker.rule.candidates == ["el", "de", "es", "fr", "it"]


def test_a_lean_opens_the_candidate_with_the_turns_audio_replayed(detecting):
    p, ident = detecting({"en": 0.12, "el": 0.83, "de": 0.05})

    async def run():
        await _start(p)
        await _speak(p, 1.0)                                   # the first reading at 1.0 s: a lean
        assert p.state._language_open_task is not None
        await p.state._language_open_task
        el = _stream(p, "el")
        assert b"".join(el.sent) == HALF_SECOND * 2             # the replay: every frame so far
        assert el.language == "el" and p.stt.active == "en"     # open, not yet the read surface
        assert len(ident.windows) == 1 and len(ident.windows[0]) == 16000   # upsampled to 16 kHz
    asyncio.run(run())


def test_the_call_switches_at_sure_and_the_voice_follows_once_the_candidate_answers(detecting):
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        await _start(p)
        await _speak(p, 1.5)                                   # open at 1.0 s, a switch at 1.5 s (SURE)
        assert p.stt.pending == "el" and p._locked_language == "en"
        _stream(p, "en")._transcripts.append("yes sir I'm here")
        p.stt.drain_transcript()
        p._append_turn_segment("yes sir I'm here")
        await _candidate_answers(p, "el", final="Γεια σας είμαι εδώ")
        assert p.stt.active == "el" and p._locked_language == "el"
        assert p.tts.voice_id == "v-el" and "el" in p._filler_key
        assert p.state._turn_segments == ["Γεια σας είμαι εδώ"]  # the turn's words are the new stream's
        assert p.state._language_locked is False               # not final yet
    asyncio.run(run())


def test_a_typed_seed_survives_the_rewrite(detecting):
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        await _start(p)
        p._append_turn_segment("typed draft")
        p.state._turn_typed.append("typed draft")
        await _speak(p, 1.5)
        await _candidate_answers(p, "el", final="ναι")
        assert p.state._turn_segments == ["typed draft", "ναι"]
    asyncio.run(run())


def test_final_settles_the_streams_and_nothing_moves_after(detecting):
    p, ident = detecting({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        await _candidate_answers(p, "el", interim="γ")
        assert p.stt.active == "el"
        await _speak(p, 3.0)                                   # three stable seconds after the switch
        assert p.state._language_locked is True and p._language_tracker.rule.final == "el"
        await asyncio.sleep(0)
        assert p.stt.languages == ["el"]                       # the English stream closed
        ident.probs = {"en": 0.95, "el": 0.03, "de": 0.02}
        await _speak(p, 2.0)
        assert p._locked_language == "el" and p.stt.active == "el"
    asyncio.run(run())


def test_the_first_take_lets_a_lean_leader_win_and_dispatches_its_words(detecting):
    llm = FakeLLM(responses=[["Γεια σας!"]])
    p, _ = detecting({"en": 0.12, "el": 0.83, "de": 0.05}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)                                   # a lean: the candidate opens, no switch (SURE)
        await p.state._language_open_task
        assert p._locked_language == "en"
        _stream(p, "en")._transcripts.append("call him a there")
        _stream(p, "el")._transcripts.append("Καλημέρα θέλω ένα ραντεβού")
        _stream(p, "el").last_result_monotonic = time.monotonic()
        p.state._turn_segments.append(p.stt.drain_transcript())
        p.state._user_silent.set()
        await p._turn_timeout_handler("call him a there")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts and llm.prompts[0] == "Καλημέρα θέλω ένα ραντεβού"
    assert p._locked_language == "el" and p.tts.voice_id == "v-el"


def test_a_candidate_that_never_answers_lets_the_default_words_go_out(detecting, monkeypatch):
    monkeypatch.setattr(S, "SWITCH_READY_S", 0.05)
    llm = FakeLLM(responses=[["Hello!"]])
    p, _ = detecting({"en": 0.12, "el": 0.83, "de": 0.05}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        await p.state._language_open_task
        _stream(p, "en")._transcripts.append("hello there")
        p.state._turn_segments.append(p.stt.drain_transcript())
        p.state._user_silent.set()
        await p._turn_timeout_handler("hello there")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["hello there"]
    assert p._locked_language == "en" and p._language_tracker.rule.current == "en"


def test_the_llm_busy_queue_path_takes_without_waiting(detecting):
    p, _ = detecting({"en": 0.12, "el": 0.83, "de": 0.05})

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        await p.state._language_open_task
        p.state._utterance_task = asyncio.get_running_loop().create_future()   # a turn in flight
        _stream(p, "en")._transcripts.append("more words")
        t0 = time.monotonic()
        await p._process_speech_end()
        assert time.monotonic() - t0 < 0.5
        assert p.state._queued_speech == ["more words"]
        assert p.stt.languages == ["en"]                       # the take closed the inactive candidate
        p.state._utterance_task.cancel()
    asyncio.run(run())


def test_a_candidate_that_fails_to_open_comes_back_once_then_stays_out(detecting):
    p, _ = detecting({"en": 0.12, "el": 0.83, "de": 0.05})

    async def run():
        await _start(p)
        original = p.stt._build
        calls = {"n": 0}

        def failing():
            calls["n"] += 1
            fake = original()
            if calls["n"] <= 2:
                async def boom(**kw):
                    raise RuntimeError("no el stream")
                fake.start = boom
            return fake
        p.stt._build = failing
        await _speak(p, 1.0)                                   # a lean: the open fails
        await p.state._language_open_task
        p._language_poll()
        assert p.stt.languages == ["en"] and p._language_tracker.rule.opened is None
        p._note_turn_taken()                                   # the next turn
        await _speak(p, 1.0)                                   # leans again: a second try, fails again
        await p.state._language_open_task
        p._language_poll()
        assert "el" in p._language_tracker.rule._unavailable
    asyncio.run(run())


def test_a_language_the_line_cannot_serve_is_kept_out(detecting):
    p, _ = detecting({"en": 0.12, "el": 0.83, "de": 0.05}, voices={"en": "v-en"})

    async def run():
        await _start(p)
        await _speak(p, 2.0)
        assert p.state._language_open_task is None and p.stt.languages == ["en"]
        assert "el" in p._language_tracker.rule._unavailable and p._locked_language == "en"
    asyncio.run(run())


def test_cleanup_stops_the_tracker_and_closes_every_stream(detecting):
    p, _ = detecting({"en": 0.12, "el": 0.83, "de": 0.05})

    async def run():
        await _start(p)
        await _speak(p, 1.0)
        await p.state._language_open_task
        await p._cleanup()
        assert p._language_tracker.closed and p.stt.closed
        assert p.state._language_open_task is None
    asyncio.run(run())


def test_a_final_while_the_switch_is_pending_settles_on_the_candidate_once_it_answers(detecting):
    """The rule finalises while the candidate is still catching up: the
    settle waits for the pending switch, so the call ends up on the language
    the rule chose, not on the stream that happened to be active."""
    p, ident = detecting({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        await _start(p)
        await _speak(p, 1.5)                                   # open, then a switch pending
        await p.state._language_open_task
        assert p.stt.pending == "el"
        await _speak(p, 3.0)                                   # three stable seconds: the rule finalises
        assert p._language_tracker.rule.final == "el"
        assert p.state._language_final_pending is True and p.stt.languages == ["en", "el"]
        await _candidate_answers(p, "el", interim="γ")         # now it answered: the switch and the settle
        assert p.stt.active == "el" and p.state._language_locked is True
        await asyncio.sleep(0)
        assert p.stt.languages == ["el"]
    asyncio.run(run())


def test_a_one_word_first_answer_switches_the_call_and_dispatches_its_words(detecting):
    """A 0.5 s answer gives the rule no reading before the take: the take's
    own reading opens the candidate and switches in one batch, the stream
    must exist for the switch to go pending, and the dispatch waits for its
    words. The call script's "Ναι." in Greek."""
    llm = FakeLLM(responses=[["Ναι!"]])
    p, _ = detecting({"en": 0.02, "el": 0.97, "de": 0.01}, llm=llm)

    async def answerer():
        for _ in range(200):
            stream = p.stt._streams.get("el")
            if stream is not None and stream.live:
                stream.provider._transcripts.append("Ναι")
                stream.provider.last_result_monotonic = time.monotonic()
                return
            await asyncio.sleep(0.005)

    async def run():
        await _start(p)
        await p._stt_feed(HALF_SECOND, voiced=True)            # 0.5 s of voice, no reading yet
        assert p._language_tracker._task is None
        _stream(p, "en")._transcripts.append("Nah")
        p.state._turn_segments.append(p.stt.drain_transcript())
        p.state._user_silent.set()
        bg = asyncio.create_task(answerer())
        await p._turn_timeout_handler("Nah")
        await bg
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts and llm.prompts[0] == "Ναι"
    assert p._locked_language == "el" and p.tts.voice_id == "v-el"
    assert p.stt.active == "el" and sorted(p.stt.languages) == ["el", "en"]


def test_open_and_switch_in_one_reading_leave_the_switch_pending(detecting):
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        await _start(p)
        await p._stt_feed(HALF_SECOND * 3, voiced=True)        # 1.5 s at once: one reading, open and switch together
        await p._language_tracker._task
        p._language_poll()
        assert p.stt.pending == "el" and p._language_tracker.rule.current == "el"
        assert p.state._language_open_task is not None
        await _candidate_answers(p, "el", final="ναι")
        assert p.stt.active == "el" and p._locked_language == "el"
    asyncio.run(run())


def test_a_queue_take_with_a_pending_switch_abandons_it(detecting):
    """The agent is busy, the turn is queued without waiting: the candidate
    is closed with the take, so its answer to the queued words never seeds
    the next turn, and the rule falls back to the language still heard."""
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        await p.state._language_open_task
        assert p.stt.pending == "el"
        p.state._utterance_task = asyncio.get_running_loop().create_future()
        el = _stream(p, "el")
        _stream(p, "en")._transcripts.append("more words")
        await p._process_speech_end()
        assert p.state._queued_speech == ["more words"]
        assert p.stt.pending is None and p.stt.languages == ["en"]
        el._transcripts.append("περισσότερες λέξεις")           # the closed stream's late answer
        el.last_result_monotonic = time.monotonic()
        p._language_poll()
        assert p.state._turn_segments == [] and p.stt.active == "en"
        assert p._language_tracker.rule.current == "en" and p._language_tracker.rule.opened is None
        p.state._utterance_task.cancel()
    asyncio.run(run())


def test_the_dispatch_waits_for_the_new_streams_final_after_a_switch_on_a_partial(detecting):
    llm = FakeLLM(responses=[["Γεια σας!"]])
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        _stream(p, "en")._transcripts.append("yes sir")
        p.state._turn_segments.append(p.stt.drain_transcript())
        await _candidate_answers(p, "el", interim="ναι κύ")    # the switch resolves on the partial
        assert p.stt.active == "el" and p.state._turn_segments == []
        p.state._user_silent.set()

        async def final_lands():
            await asyncio.sleep(0.1)
            el = _stream(p, "el")
            el.latest_interim = ""
            el._transcripts.append("ναι κύριε")
            el.last_result_monotonic = time.monotonic()
        bg = asyncio.create_task(final_lands())
        await p._turn_timeout_handler("yes sir")
        await bg
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["ναι κύριε"]
    assert p.state._turn_segments == []


def test_a_final_still_in_flight_at_the_bound_is_dropped_when_it_lands(detecting, monkeypatch):
    """The new stream's final misses the bound: the turn goes out as the
    pipeline's own interim fallback (the partial of the right language), and
    the final is dropped when it lands instead of becoming the next turn."""
    monkeypatch.setattr(pl, "TAKE_FINAL_WAIT_S", 0.05)
    llm = FakeLLM(responses=[["Hello!"]])
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        _stream(p, "en")._transcripts.append("yes sir")
        p.state._turn_segments.append(p.stt.drain_transcript())
        await _candidate_answers(p, "el", interim="ναι κύ")
        p.state._user_silent.set()
        await p._turn_timeout_handler("yes sir")              # the final never comes in time
        for _ in range(3):
            await asyncio.sleep(0)
        el = _stream(p, "el")
        el._transcripts.append("ναι κύριε")                   # it lands after the take
        el.last_result_monotonic = time.monotonic()
        assert p.stt.drain_transcript() is None               # dropped, not the next turn's words
        el._transcripts.append("και μετά")
        assert p.stt.drain_transcript() == "και μετά"
    asyncio.run(run())
    assert llm.prompts == ["ναι κύ"]


# ── The words decide (a provider that reports a word confidence) ─────────

from audio.capabilities import STTCapabilities
from audio.providers.stt.base import Heard
from pipeline_fakes import FakeSTT, FakeTTS


class WordSTT(FakeSTT):
    """The call STT fake whose provider reports a word confidence: ``hear``
    writes its ledger on its own audio clock (8 kHz bytes offered)."""

    capabilities = STTCapabilities(reports_confidence=True)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.heard: list[Heard] = []
        self.audio_offered_s = 0.0
        self.endpoints = 0

    async def send_audio(self, audio):
        await super().send_audio(audio)
        self.audio_offered_s += len(audio) / (RATE * 2)

    async def feed_during_tts(self, audio):
        self.audio_offered_s += len(audio) / (RATE * 2)

    async def force_endpoint(self):
        self.endpoints += 1

    def hear(self, start, end, words, confidence, *, text=None):
        self.heard.append(Heard(start, end, words, confidence if words else 0.0, True))
        if text:
            self._transcripts.append(text)
        self.last_result_monotonic = time.monotonic()


@pytest.fixture
def worded(detecting, monkeypatch):
    """``detecting`` with streams whose provider reports a word confidence."""
    from pipeline import providers as plproviders
    monkeypatch.setattr(plproviders, "get_provider_class",
                        lambda kind, name: WordSTT if kind == "stt" else FakeTTS)
    return detecting


def test_a_take_waits_for_a_switch_pending_under_a_held_final_and_settles_on_its_words(worded):
    """The rule finalised on Greek while the switch still waited for the
    candidate's words: the take waits for them instead of abandoning the
    switch, and the call settles on the language the words chose."""
    llm = FakeLLM(responses=[["Γεια σας!"]])
    p, _ = worded({"en": 0.03, "el": 0.95, "de": 0.02}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)                                   # open, then a switch pending: no words yet
        await p.state._language_open_task
        await _speak(p, 3.0)                                   # three stable seconds: the rule finalises
        assert p._language_tracker.rule.done and p.state._language_final_pending
        assert p.stt.pending == "el"
        _stream(p, "en").hear(0.0, 4.5, 0, 0.0)
        _stream(p, "en")._transcripts.append("can you spell out")
        p.state._turn_segments.append(p.stt.drain_transcript())
        p.state._user_silent.set()

        async def words_arrive():
            await asyncio.sleep(0.1)
            _stream(p, "el").hear(0.0, 4.5, 9, 0.93, text="Καλησπέρα με ακούς ήθελα να σε ρωτήσω κάτι")
        bg = asyncio.create_task(words_arrive())
        await p._turn_timeout_handler("can you spell out")
        await bg
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["Καλησπέρα με ακούς ήθελα να σε ρωτήσω κάτι"]
    assert p._locked_language == "el" and p.state._language_locked is True


def test_a_take_the_shown_words_refuse_dispatches_the_defaults_words(worded):
    """Accented English: the ear leans Greek and the take would let the lean
    win, the words refuse it, the agent gets the English words."""
    llm = FakeLLM(responses=[["Sure."]])
    p, _ = worded({"en": 0.12, "el": 0.83, "de": 0.05}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)                                   # a lean: the candidate opens, no switch (SURE)
        await p.state._language_open_task
        _stream(p, "en").hear(0.0, 1.5, 5, 0.97, text="I would like an appointment")
        _stream(p, "el").hear(0.0, 1.5, 5, 0.68, text="Ai wood laik an apointment")
        p.state._turn_segments.append(p.stt.drain_transcript())
        p.state._user_silent.set()
        await p._turn_timeout_handler("I would like an appointment")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["I would like an appointment"]
    assert p._locked_language == "en" and p.tts.voice_id == "v-en"
    assert p._language_tracker.rule.current == "en"


def test_a_one_word_greek_first_answer_switches_on_its_word_at_the_take(worded):
    llm = FakeLLM(responses=[["Ναι!"]])
    p, _ = worded({"en": 0.02, "el": 0.97, "de": 0.01}, llm=llm)

    async def answerer():
        for _ in range(200):
            stream = p.stt._streams.get("el")
            if stream is not None and stream.live:
                stream.provider.hear(0.0, 0.5, 1, 0.95, text="Ναι")
                return
            await asyncio.sleep(0.005)

    async def run():
        await _start(p)
        await p._stt_feed(HALF_SECOND, voiced=True)            # 0.5 s of voice, no reading yet
        _stream(p, "en").hear(0.0, 0.5, 0, 0.0)               # the English stream heard nothing
        p.state._turn_segments.append("Nah")
        p.state._user_silent.set()
        bg = asyncio.create_task(answerer())
        await p._turn_timeout_handler("Nah")
        await bg
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["Ναι"]
    assert p._locked_language == "el"


def test_the_take_adds_the_final_that_landed_after_the_dispatchs_drain(detecting):
    p, _ = detecting({"en": 0.9, "el": 0.05, "de": 0.05})

    async def run():
        await _start(p)
        await _speak(p, 1.0)
        p.state._turn_segments.append("first part")
        _stream(p, "en")._transcripts.append("and the rest")  # lands after the dispatch's own drain
        joined = await p._language_take("first part")
        assert joined == "first part and the rest"
        assert p.stt.drain_transcript() is None                # handed out once
    asyncio.run(run())


def test_a_late_final_the_interim_fallback_already_sent_is_not_added_twice(detecting):
    p, _ = detecting({"en": 0.9, "el": 0.05, "de": 0.05})

    async def run():
        await _start(p)
        await _speak(p, 1.0)
        p.state._turn_segments.append("yes sir")
        p.state._interim_fallback_text, p.state._interim_fallback_at = "yes sir", time.monotonic()
        _stream(p, "en")._transcripts.append("Yes, sir.")
        assert await p._language_take("yes sir") == "yes sir"
    asyncio.run(run())


def test_every_other_take_drops_the_drained_text_with_its_turn(detecting):
    p, _ = detecting({"en": 0.9, "el": 0.05, "de": 0.05})

    async def run():
        await _start(p)
        await _speak(p, 1.0)
        _stream(p, "en")._transcripts.append("queued words")
        assert p._note_turn_taken() == "queued words"          # a queue site ignores it
        assert p.stt.drain_transcript() is None and p.state._turn_segments == []
    asyncio.run(run())


def test_the_drop_mark_of_a_final_that_never_came_dies_at_its_deadline(detecting, monkeypatch):
    """The take armed the drop for the new stream's final in flight; it
    never lands: the next utterance's first final goes through."""
    monkeypatch.setattr(pl, "TAKE_FINAL_WAIT_S", 0.05)
    llm = FakeLLM(responses=[["Hello!"]])
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        _stream(p, "en")._transcripts.append("yes sir")
        p.state._turn_segments.append(p.stt.drain_transcript())
        await _candidate_answers(p, "el", interim="ναι κύ")
        p.state._user_silent.set()
        await p._turn_timeout_handler("yes sir")               # armed: the final was in flight
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.sleep(0.1)                                # past the mark's deadline
        await p._stt_feed(HALF_SECOND, voiced=False)            # frames keep flowing on a call
        el = _stream(p, "el")
        el.latest_interim = ""
        el._transcripts.append("και κάτι ακόμα")               # the next utterance
        assert p.stt.drain_transcript() == "και κάτι ακόμα"
    asyncio.run(run())


def test_a_candidate_replays_from_a_pre_roll_before_the_turns_first_voice(detecting):
    """The vendor works through a replay at about real-time pace: the
    listening before the caller spoke is trimmed to a pre-roll."""
    p, _ = detecting({"en": 0.12, "el": 0.83, "de": 0.05})

    async def run():
        await _start(p)
        for _ in range(4):                                     # 2 s of silence before the first word
            await p._stt_feed(HALF_SECOND, voiced=False)
        await _speak(p, 1.0)
        await p.state._language_open_task
        el = _stream(p, "el")
        assert len(b"".join(el.sent)) == len(HALF_SECOND) * 3  # the 0.5 s pre-roll and the second of voice
        p._note_turn_taken()                                   # the next turn trims again at its first voice
        assert p._language_voiced_since_take is False
    asyncio.run(run())


def test_detection_switched_off_says_so(make_pipeline, monkeypatch, caplog):
    import logging

    from config_manager import ConfigManager
    monkeypatch.setattr(pl, "identifier_available", lambda: True)
    cfg = ConfigManager()
    cfg._data = {"settings": {"language_identify_enabled": "false"}}
    with caplog.at_level(logging.INFO, logger="pipeline"):
        p = make_pipeline(route=_detecting_route(), cfg=cfg)
    assert p._language_tracker is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("detection is off (the admin switched it off)" in m for m in messages)
    assert not any("unavailable" in m for m in messages)


def test_the_identifier_is_pre_warmed_only_where_something_detects():
    from types import SimpleNamespace

    from config_manager import ConfigManager
    from pipeline.providers import language_identifier_wanted
    cfg = ConfigManager()
    cfg._data = {"settings": {"language_identify_enabled": "true"}}
    routes = [SimpleNamespace(language_detect=False)]
    cfg.enabled_routes = lambda: routes
    assert language_identifier_wanted(cfg) is False
    assert language_identifier_wanted(cfg, live_voice=True) is True
    routes.append(SimpleNamespace(language_detect=True))
    assert language_identifier_wanted(cfg) is True
    cfg._data = {"settings": {"language_identify_enabled": "false"}}
    assert language_identifier_wanted(cfg, live_voice=True) is False


def test_the_drop_mark_takes_the_stale_final_when_it_arrives_late_in_its_life(detecting, monkeypatch):
    """The final the mark exists for lands near the end of its life and the
    next drain comes after it: dropped on arrival all the same."""
    monkeypatch.setattr(pl, "TAKE_FINAL_WAIT_S", 0.2)
    llm = FakeLLM(responses=[["Hello!"]])
    p, _ = detecting({"en": 0.03, "el": 0.95, "de": 0.02}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        _stream(p, "en")._transcripts.append("yes sir")
        p.state._turn_segments.append(p.stt.drain_transcript())
        await _candidate_answers(p, "el", interim="ναι κύ")
        p.state._user_silent.set()
        await p._turn_timeout_handler("yes sir")               # armed: the final was in flight
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        el = _stream(p, "el")
        el.latest_interim = ""
        el._transcripts.append("ναι κύριε")                    # the stale final, late in the mark's life
        await p._stt_feed(HALF_SECOND, voiced=False)            # the frame after its arrival
        await asyncio.sleep(0.3)                                # the next drain, past the deadline
        assert p.stt.drain_transcript() is None
    asyncio.run(run())


def test_a_later_turns_trailing_stream_has_its_words_collected_at_the_take(worded):
    """The switch happened in an earlier turn and the new stream still trails
    its replay at this turn's dispatch: its words of this turn are waited for
    (within the take's budget) so they never seed the next turn."""
    llm = FakeLLM(responses=[["Ναι!"]])
    p, _ = worded({"en": 0.03, "el": 0.95, "de": 0.02}, llm=llm)

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        await p.state._language_open_task
        el = _stream(p, "el")
        _stream(p, "en").hear(0.0, 1.5, 0, 0.0)
        el.hear(0.0, 0.6, 3, 0.93, text="Καλησπέρα με ακούς")     # a final: the switch resolves
        p._language_poll()
        assert p.stt.active == "el" and p.stt.active_behind      # still behind its replay
        p._language_tracker.rule.final = "el"                    # the rule has settled meanwhile
        p.state._language_turn_switched = False                  # an earlier turn's switch

        async def catches_up():
            await asyncio.sleep(0.1)
            el.hear(0.6, 1.5, 2, 0.9, text="ήθελα κάτι")
        bg = asyncio.create_task(catches_up())
        p.state._turn_segments[:] = ["Καλησπέρα με ακούς"]      # the dispatch's segments for this turn
        joined = await p._language_take("Καλησπέρα με ακούς")
        await bg
        assert joined == "Καλησπέρα με ακούς ήθελα κάτι"
        assert p.stt.drain_transcript() is None                  # nothing left to seed the next turn
    asyncio.run(run())


async def _switched_in_an_earlier_turn(p):
    """A call that moved to Greek in an earlier turn whose stream still trails
    its replay at this turn's dispatch: the first words of the turn are in,
    the rest still on the way."""
    await _start(p)
    await _speak(p, 1.5)
    await p.state._language_open_task
    el = _stream(p, "el")
    _stream(p, "en").hear(0.0, 1.5, 0, 0.0)
    el.hear(0.0, 0.6, 3, 0.93, text="Καλησπέρα με ακούς")         # a final: the switch resolves
    p._language_poll()
    assert p.stt.active == "el" and p.stt.active_behind
    assert p._language_tracker.rule.final is None
    p.state._language_turn_switched = False                      # an earlier turn's switch
    p.state._turn_segments[:] = ["Καλησπέρα με ακούς"]
    return el


def test_the_calls_second_take_finalises_and_still_catches_the_trailing_stream_up(worded, caplog):
    """Before the rule is final, the dispatched take finalises the call
    (the second take) and waits all the same for the new stream's words of
    this turn, which join the dispatch instead of seeding the next turn."""
    llm = FakeLLM(responses=[["Ναι!"]])
    p, _ = worded({"en": 0.03, "el": 0.95, "de": 0.02}, llm=llm)

    async def run():
        el = await _switched_in_an_earlier_turn(p)
        p._language_tracker.rule._takes = 1                      # the first turn's take happened

        async def catches_up():
            await asyncio.sleep(0.1)
            el.hear(0.6, 1.5, 2, 0.9, text="ήθελα κάτι")
        bg = asyncio.create_task(catches_up())
        with caplog.at_level(logging.INFO, logger="pipeline"):
            joined = await p._language_take("Καλησπέρα με ακούς")
        await bg
        assert joined == "Καλησπέρα με ακούς ήθελα κάτι"
        assert p._language_tracker.rule.final == "el" and "en" not in p.stt.languages
        assert p.stt.drain_transcript() is None
    asyncio.run(run())
    assert any("trailed its audio by" in r.getMessage() for r in caplog.records)


def test_live_voice_catches_the_trailing_stream_up_at_every_take_until_the_final(worded):
    """Live voice asks the take for no verdict, so the rule stays open for
    several turns: a take whose active stream still trails waits for its
    words all the same, and what is heard follows at the take."""
    p, _ = worded({"en": 0.03, "el": 0.95, "de": 0.02})
    p._heard_follows_switch = False
    p._language_tracker.rule.lean_at_take = False

    async def run():
        el = await _switched_in_an_earlier_turn(p)
        assert p._heard_language == "en"                         # not yet: live voice moves it at the take

        async def catches_up():
            await asyncio.sleep(0.1)
            el.hear(0.6, 1.5, 2, 0.9, text="ήθελα κάτι")
        bg = asyncio.create_task(catches_up())
        joined = await p._language_take("Καλησπέρα με ακούς")
        await bg
        assert joined == "Καλησπέρα με ακούς ήθελα κάτι"
        assert p._language_tracker.rule.final is None and "en" in p.stt.languages
        assert p._heard_language == "el"
    asyncio.run(run())


def test_a_trailing_streams_interim_fallback_survives_a_catch_up_that_hears_nothing(worded, monkeypatch):
    """The dispatch fell back to the live partial of a stream still trailing
    its audio and the final never lands within the take's catch-up: the
    partial's words go out as dispatched, and the final is dropped when it
    lands later instead of seeding the next turn."""
    monkeypatch.setattr(pl, "TAKE_CATCH_UP_S", 0.2)
    p, _ = worded({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        el = await _switched_in_an_earlier_turn(p)
        p._language_tracker.rule.final = "el"                    # the rule has settled meanwhile
        el.latest_interim = "ήθελα κά"
        p.state._turn_segments[:] = ["Καλησπέρα με ακούς", "ήθελα κά"]
        p.state._interim_fallback_text, p.state._interim_fallback_at = "ήθελα κά", time.monotonic()
        joined = await p._language_take("Καλησπέρα με ακούς ήθελα κά")
        assert joined == "Καλησπέρα με ακούς ήθελα κά"
        el.latest_interim = ""
        el._transcripts.append("ήθελα κάτι")                     # the final, late: its words went out already
        assert p.stt.drain_transcript() is None
    asyncio.run(run())


def test_a_switch_on_a_final_waits_for_no_final_of_the_stream_it_left(worded):
    """The switch resolved this turn on the candidate's final while the
    stream it left still holds an undrained final of its own: the take
    dispatches the new stream's words at once, with no endpoint and no wait
    for a final the active stream will never send."""
    p, _ = worded({"en": 0.03, "el": 0.95, "de": 0.02})

    async def run():
        await _start(p)
        await _speak(p, 1.5)
        await p.state._language_open_task
        en, el = _stream(p, "en"), _stream(p, "el")
        en.hear(0.0, 1.5, 0, 0.0)
        el.hear(0.0, 1.5, 4, 0.93, text="Καλησπέρα με ακούς κάτι")  # caught up, a final: the switch resolves
        p._language_poll()
        assert p.stt.active == "el" and not p.stt.active_behind and p.state._language_turn_switched
        en._transcripts.append("kalispera me akous")             # the stream it left finalises late
        assert p.stt.has_pending_finals and not p.stt.has_drainable_finals
        t0 = time.monotonic()
        joined = await p._language_take("Nah")
        took = time.monotonic() - t0
        assert joined == "Καλησπέρα με ακούς κάτι"
        assert en.endpoints == 0 and el.endpoints == 0
        assert took < 0.5
    asyncio.run(run())


def test_every_wait_of_a_take_shares_one_budget(worded, monkeypatch, caplog):
    monkeypatch.setattr(pl, "TAKE_BUDGET_S", 0.3)
    p, _ = worded({"en": 0.12, "el": 0.83, "de": 0.05})

    async def run():
        await _start(p)
        await _speak(p, 1.5)                                   # a lean: the candidate opens
        await p.state._language_open_task
        p.state._turn_segments.append("hello there")
        t0 = time.monotonic()
        with caplog.at_level(logging.INFO, logger="pipeline"):
            await p._language_take("hello there")              # the take's switch never decides
        return time.monotonic() - t0
    took = asyncio.run(run())
    assert took < 0.8
    assert any("language take:" in r.getMessage() and "s waited" in r.getMessage() for r in caplog.records)


# ── The call's second listen at the take (``relisten.TurnRelisten``) ──

def test_every_call_stt_gets_the_second_listen(make_pipeline):
    p = make_pipeline(route=make_route(language="el"))
    assert getattr(p.stt, "turn_relisten", False) is True


def test_the_take_completes_the_turn_with_what_the_second_listen_heard(make_pipeline):
    p = make_pipeline(route=make_route(language="el"))

    async def run():
        p.state._turn_segments.append("ένα τρία")
        p.stt.completed = "ένα δύο τρία"
        joined = await p._language_take("ένα τρία")
        assert joined == "ένα δύο τρία"
        text, bound = p.stt.completions[0]
        assert text == "ένα τρία" and 0 < bound <= 2.0
        assert p.stt.takes == 1                                 # the turn's notes go with the take
    asyncio.run(run())


def test_a_turn_the_second_listen_did_not_change_goes_as_heard(make_pipeline):
    p = make_pipeline(route=make_route(language="el"))

    async def run():
        p.state._turn_segments.append("ένα")
        assert await p._language_take("ένα") == "ένα"
        assert p.state._turn_segments == ["ένα"]
    asyncio.run(run())


def test_a_queued_turn_is_taken_without_its_second_listen(make_pipeline):
    p = make_pipeline(route=make_route(language="el"))
    p._note_turn_taken()
    assert p.stt.takes == 1 and not getattr(p.stt, "completions", [])


def test_a_wordless_turn_reaches_the_agent_with_its_second_listens_words(make_pipeline):
    p = make_pipeline(route=make_route(language="el"), llm=FakeLLM())

    async def run():
        p.state._stt_active = True
        p.state._speech_end_time = time.monotonic()
        p.stt.transcript_wait_timeout_s = 0.01
        p.stt.wordless = "Καλησπέρα"
        await p._delayed_drain_retry()
        assert p.state._turn_segments == ["Καλησπέρα"]
        p._cancel_turn_timer()
    asyncio.run(run())


def test_the_cleanup_reports_the_second_listen_once_with_the_routes_provider(make_pipeline, monkeypatch):
    from pipeline import core as plcore
    sent = []

    async def report(**kw):
        sent.append(kw)
    monkeypatch.setattr(plcore, "report_relisten_usage", report)
    p = make_pipeline(route=make_route(language="el"), llm=FakeLLM(session_id="sess-9"))
    p.stt.relisten_seconds = 7.5
    p._route_settings.stt_provider = {"provider_name": "deepgram"}
    asyncio.run(p._cleanup())
    assert sent == [{"agent": p.route.agent, "provider": "deepgram", "seconds": 7.5, "session_id": "sess-9"}]


def test_a_call_with_no_second_listen_reports_none(make_pipeline, monkeypatch):
    from pipeline import core as plcore
    sent = []

    async def report(**kw):
        sent.append(kw)
    monkeypatch.setattr(plcore, "report_relisten_usage", report)
    p = make_pipeline(route=make_route(language="el"))
    asyncio.run(p._cleanup())
    assert sent == []
