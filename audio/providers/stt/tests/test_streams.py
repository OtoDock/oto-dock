"""``LanguageStreams``: the active stream and the candidate behind one
provider, with fakes for the streams."""

import asyncio
import gc
import time
import warnings
from types import SimpleNamespace

import pytest
from audio.capabilities import STTCapabilities
from audio.providers.lid.evidence import SHOWN
from audio.providers.stt import streams as S
from audio.providers.stt.base import Heard, STTProvider
from audio.providers.stt.streams import LanguageStreams


class FakeStream(STTProvider):
    """One fake stream: records what it was sent, answers scripted finals."""

    built: list["FakeStream"] = []
    fail_languages: set[str] = set()

    @classmethod
    def from_row(cls, row, resolver):
        return cls()

    @classmethod
    def billing_unit(cls):
        return "second"

    @classmethod
    def cost_per_unit(cls):
        return 0.5

    def __init__(self):
        FakeStream.built.append(self)
        self.language = None
        self.params = {}
        self.sent: list[bytes] = []
        self.silence: list[int] = []
        self.finals: list[str] = []
        self._interim = ""
        self._last_result = 0.0
        self.closed = False
        self.endpoints = 0
        self.keepalives = 0
        self.cleared = 0
        self.tts_finished: list[bool] = []
        self.fatal: str | None = None
        self._is_open = False
        self.ensure_calls: list[tuple[str, bool]] = []

    @property
    def endpointing_ms(self):
        return 500

    relisten = 0.0
    bound = None

    @property
    def relisten_seconds(self):
        return self.relisten

    def bound_finish(self, seconds):
        self.bound = seconds

    async def start(self, language="multi", sample_rate=None, interim_results=False, endpointing_ms=None):
        if language in FakeStream.fail_languages:
            raise RuntimeError(f"no {language} stream today")
        self.language = language
        self.params = dict(sample_rate=sample_rate, interim_results=interim_results, endpointing_ms=endpointing_ms)
        self._is_open = True

    async def send_audio(self, audio_bytes):
        self.sent.append(bytes(audio_bytes))
        self._count_offered(len(audio_bytes), RATE)

    def answer(self, final: str | None = None, interim: str = ""):
        """The vendor answered: a final into the queue or a partial."""
        if final:
            self.finals.append(final)
        self._interim = interim
        self._last_result = time.monotonic()

    def drain_transcript(self):
        if not self.finals:
            return None
        text, self.finals = " ".join(self.finals), []
        return text

    async def wait_for_transcript(self, timeout=1.0):
        return self.drain_transcript()

    def clear_queue(self):
        self.cleared += 1
        self.finals.clear()
        self._interim = ""

    async def finish(self):
        self.closed = True
        return self.drain_transcript()

    async def close(self):
        self.closed = True
        self._is_open = False

    async def force_endpoint(self):
        self.endpoints += 1

    async def keepalive(self):
        self.keepalives += 1

    async def feed_during_tts(self, audio_bytes):
        self.silence.append(len(audio_bytes))
        self._count_offered(len(audio_bytes), RATE)

    def on_tts_finished(self, was_interrupted):
        self.tts_finished.append(was_interrupted)

    @property
    def latest_interim(self):
        return self._interim

    @property
    def has_pending_finals(self):
        return bool(self.finals)

    @property
    def last_result_monotonic(self):
        return self._last_result

    def pop_fatal_error(self):
        err, self.fatal = self.fatal, None
        return err

    async def ensure_alive(self, language, *, clear_queue=False):
        self.ensure_calls.append((language, clear_queue))
        self._is_open = True
        return True

    def pop_interim(self):
        return self._interim or None


RATE = 16000
CHUNK = b"\x01\x00" * (RATE // 10)       # 0.1 s


@pytest.fixture(autouse=True)
def _reset():
    FakeStream.built = []
    FakeStream.fail_languages = set()
    yield


def build() -> LanguageStreams:
    return LanguageStreams(FakeStream, "en")


def by_language(lang: str) -> FakeStream:
    return next(s for s in FakeStream.built if s.language == lang)


async def opened(ls: LanguageStreams) -> LanguageStreams:
    await ls.start(language="en", sample_rate=RATE, interim_results=True, endpointing_ms=1500)
    return ls


def test_the_default_stream_opens_with_the_parameters_and_billing_follows_the_class():
    async def run():
        ls = await opened(build())
        assert by_language("en").params == dict(sample_rate=RATE, interim_results=True, endpointing_ms=1500)
        assert ls.is_open and ls.active == "en" and ls.languages == ["en"]
        assert ls.billing_unit() == "second" and ls.cost_per_unit() == 0.5
        assert ls.endpointing_ms == 500 and ls.vad_silence_padding_ms == 550
    asyncio.run(run())


def test_a_candidate_gets_the_replay_then_the_frames_that_arrived_while_it_connected():
    async def run():
        ls = await opened(build())
        for _ in range(5):
            await ls.send_audio(CHUNK)
        gate = asyncio.Event()
        real_start = FakeStream.start

        async def slow_start(self, **kw):
            await gate.wait()
            await real_start(self, **kw)
        FakeStream.start = slow_start
        try:
            task = asyncio.create_task(ls.open("el"))
            await asyncio.sleep(0)
            await ls.send_audio(CHUNK * 2)                 # arrives while connecting
            gate.set()
            assert await task is True
        finally:
            FakeStream.start = real_start
        el = by_language("el")
        assert b"".join(el.sent) == CHUNK * 7              # the replay, then the buffered frames
        assert all(len(p) <= int(S.REPLAY_PIECE_S * RATE) * 2 for p in el.sent[:-1])
        assert el.params["sample_rate"] == RATE and el.params["endpointing_ms"] == 1500
        assert round(ls.extra_seconds, 1) == 0.7                       # the replay and the buffered frames
        await ls.send_audio(CHUNK)
        assert el.sent[-1] == CHUNK and by_language("en").sent[-1] == CHUNK   # live: both fed
    asyncio.run(run())


def test_a_switch_is_pending_until_the_candidate_answers_then_the_reads_move():
    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK)
        await ls.open("el")
        en, el = by_language("en"), by_language("el")
        en.answer(final="hello there")
        assert ls.drain_transcript() == "hello there"      # the active's text
        assert ls.switch("el") is True
        assert ls.pending == "el" and ls.active == "en" and ls.switched() is None
        el.answer(final="γεια σας")                        # the candidate caught up
        assert ls.switched() == ("el", "γεια σας")
        assert ls.active == "el" and ls.pending is None
        assert ls.drain_transcript() is None               # that text was handed over, not re-drained
        el.answer(final="τι κάνετε")
        assert ls.drain_transcript() == "τι κάνετε"
        assert ls.text_since_take("el") == "γεια σας τι κάνετε"
        assert ls.text_since_take("en") == "hello there"
    asyncio.run(run())


def test_a_switch_is_abandoned_when_the_candidate_never_answers(monkeypatch):
    monkeypatch.setattr(S, "SWITCH_READY_S", 0.05)

    async def run():
        ls = await opened(build())
        await ls.open("el")
        ls.switch("el")
        await asyncio.sleep(0.08)
        assert ls.pending is None and ls.active == "en" and ls.switched() is None
    asyncio.run(run())


def test_switching_to_the_default_needs_no_answer_and_an_unknown_stream_is_refused():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("el").answer(interim="γ")
        ls.switch("el")
        assert ls.switched()[0] == "el"
        ls.switch("en")
        assert ls.active == "en" and ls.switched() == ("en", "")
        assert ls.switch("de") is False
    asyncio.run(run())


def test_take_moves_the_cursors_resets_the_replay_and_closes_the_candidate():
    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK)
        await ls.open("el")
        by_language("en").answer(final="one")
        by_language("el").answer(final="ένα")
        ls.drain_transcript()
        assert ls.take() == ["el"]
        await asyncio.sleep(0)
        assert by_language("el").closed and ls.languages == ["en"]
        assert ls.text_since_take("en") == "" and ls._replay == bytearray()
        by_language("en").answer(final="two")
        ls.drain_transcript()
        assert ls.text_since_take("en") == "two"
        # the next lean may open again, with the next turn's audio
        await ls.send_audio(CHUNK * 3)
        assert await ls.open("el") is True
        assert b"".join(FakeStream.built[-1].sent) == CHUNK * 3
    asyncio.run(run())


def test_a_take_abandons_a_pending_switch_and_closes_the_candidate():
    """A candidate kept through a take would answer the words just taken
    as the next turn's: the take closes it and ends the pending switch (a
    caller that wants the switch waits for it before the take)."""
    async def run():
        ls = await opened(build())
        await ls.open("el")
        ls.switch("el")
        assert ls.pending == "el"
        assert ls.take() == ["el"]
        assert ls.pending is None and ls.languages == ["en"] and ls.active == "en"
        t0 = time.monotonic()
        await ls.wait_switched(1.0)                         # nothing to wait for
        assert time.monotonic() - t0 < 0.2
        by_language("el").answer(final="ναι")               # the closed stream's late answer lands nowhere
        assert ls.switched() is None and ls.drain_transcript() is None
        # A resolved switch is kept: the default is never closed by a take.
        await ls.open("el")
        FakeStream.built[-1].answer(final="ναι")             # the new el stream, not the closed one
        ls.switch("el")
        ls.switched()
        assert ls.active == "el"
        assert ls.take() == []
        assert sorted(ls.languages) == ["el", "en"]
    asyncio.run(run())


def test_a_registered_candidate_takes_a_switch_before_it_connects():
    """The rule's open and switch for one language arrive in one batch (a
    short first answer): the switch must find the stream and go pending, not
    fail because the connect has not run yet."""
    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK * 4)
        assert ls.register("el") is True
        assert ls.switch("el") is True and ls.pending == "el"
        task = asyncio.create_task(ls.open("el"))
        await ls.send_audio(CHUNK)                          # arrives while it connects
        assert await task is True
        el = by_language("el")
        assert b"".join(el.sent) == CHUNK * 5               # the replay snapshot, then the buffered frame
        assert ls.pending == "el"                           # live, not yet answered
        el.answer(interim="γ")
        assert ls.switched() == ("el", "") and ls.active == "el"
        assert ls.register("el") is True and await ls.open("el") is True   # idempotent once live
    asyncio.run(run())


def test_a_candidate_that_fails_to_connect_ends_the_pending_switch():
    FakeStream.fail_languages = {"el"}

    async def run():
        ls = await opened(build())
        ls.register("el")
        ls.switch("el")
        assert ls.pending == "el"
        assert await ls.open("el") is False
        t0 = time.monotonic()
        await ls.wait_switched(1.0)
        assert time.monotonic() - t0 < 0.2
        assert ls.pending is None and ls.dropped() == ["el"] and ls.languages == ["en"]
        assert ls.switched() is None
    asyncio.run(run())


def test_wait_switched_sees_the_candidate_answer_with_no_frame_flowing():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        ls.switch("el")

        async def later():
            await asyncio.sleep(0.05)
            by_language("el").answer(final="αργά")
        task = asyncio.create_task(later())
        t0 = time.monotonic()
        await ls.wait_switched(1.0)
        assert time.monotonic() - t0 < 0.5
        assert ls.switched() == ("el", "αργά") and ls.active == "el"
        await task
    asyncio.run(run())


def test_a_switch_pending_at_the_finish_is_abandoned_and_the_active_finishes():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        ls.switch("el")
        by_language("en").finals.append("the end")
        assert await ls.finish() == "the end"
        assert ls.active == "en" and ls.pending is None and ls.switched() is None
        await asyncio.sleep(0)
        assert by_language("el").closed
    asyncio.run(run())


def test_the_next_final_after_a_take_can_be_dropped():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("el").answer(interim="ναι κύ")
        ls.switch("el")
        ls.switched()
        ls.discard_next_final()
        ls.take()
        by_language("el").answer(final="ναι κύριε")           # the words in flight at the take
        assert ls.drain_transcript() is None and ls.text_since_take() == ""
        by_language("el").answer(final="και μετά")            # the next turn's words go through
        assert ls.drain_transcript() == "και μετά"
    asyncio.run(run())


def test_settle_closes_everything_but_the_active_and_refuses_new_opens():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("el").answer(interim="x")
        ls.switch("el")
        ls.switched()
        ls.settle()
        await asyncio.sleep(0)
        assert by_language("en").closed and not by_language("el").closed
        assert await ls.open("de") is False
        assert ls.languages == ["el"]
    asyncio.run(run())


def test_writes_and_hooks_fan_out_to_every_live_stream():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        en, el = by_language("en"), by_language("el")
        await ls.feed_during_tts(CHUNK)
        await ls.force_endpoint()
        await ls.keepalive()
        ls.on_tts_finished(False)
        ls.clear_queue()
        for s in (en, el):
            assert s.silence == [len(CHUNK)] and s.endpoints == 1 and s.keepalives == 1
            assert s.tts_finished == [False] and s.cleared == 1
    asyncio.run(run())


def test_a_force_endpoint_during_connect_is_sent_after_the_replay():
    async def run():
        ls = await opened(build())
        gate = asyncio.Event()
        real_start = FakeStream.start

        async def slow_start(self, **kw):
            await gate.wait()
            await real_start(self, **kw)
        FakeStream.start = slow_start
        try:
            task = asyncio.create_task(ls.open("el"))
            await asyncio.sleep(0)
            await ls.force_endpoint()
            assert by_language("en").endpoints == 1
            gate.set()
            await task
        finally:
            FakeStream.start = real_start
        assert by_language("el").endpoints == 1
    asyncio.run(run())


def test_evidence_looks_at_every_stream_while_text_comes_from_the_active():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("el").answer(interim="γεια")
        assert ls.speech_evidence is True
        assert ls.latest_interim == "" and ls.pop_interim() is None
        by_language("el").answer(final="γεια σας")
        assert ls.has_pending_finals is True
        assert ls.drain_transcript() is None                # the active has nothing
        assert ls.text_since_take("el") == "γεια σας"
    asyncio.run(run())


def test_a_dead_candidate_is_dropped_and_reported():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("el").fatal = "quota"
        await ls.send_audio(CHUNK)
        assert ls.dropped() == ["el"] and ls.languages == ["en"]
        assert ls.switch("el") is False
        FakeStream.fail_languages = {"de"}
        assert await ls.open("de") is False
        assert ls.dropped() == ["de"]
    asyncio.run(run())


def test_the_guard_reconnects_the_active_stream_in_its_own_language():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("el").answer(interim="x")
        ls.switch("el")
        ls.switched()
        assert await ls.ensure_alive("en", clear_queue=True) is True
        assert by_language("el").ensure_calls == [("el", True)]
        assert by_language("en").cleared == 1 and by_language("en").ensure_calls == []
    asyncio.run(run())


def test_seconds_sent_count_the_replay_the_live_frames_and_the_silence():
    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK * 5)                      # 0.5 s to the default
        await ls.open("el")                                 # 0.5 s replayed
        await ls.send_audio(CHUNK)                          # 0.1 s to both
        await ls.feed_during_tts(CHUNK)                     # 0.1 s of silence to both
        assert round(ls.billed_seconds, 2) == 1.4
        assert round(ls.extra_seconds, 2) == 0.7
        ls.take()                                           # the candidate closes, its seconds stay billed
        assert round(ls.billed_seconds, 2) == 1.4
        assert round(ls.extra_seconds, 2) == 0.7
        # The switch resolves and the decision settles: the default closes and
        # its seconds stay the default's, never the extra.
        await ls.open("el")
        by_language("el").answer(interim="x")
        ls.switch("el")
        ls.switched()
        await ls.send_audio(CHUNK)                          # 0.1 s to both
        ls.settle()
        assert round(ls.billed_seconds, 2) == 1.6
        assert round(ls.extra_seconds, 2) == 0.8                 # the closed candidate and the live one, never the default
    asyncio.run(run())


def test_wait_for_transcript_answers_the_new_active_after_a_switch():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        el = by_language("el")

        async def later():
            await asyncio.sleep(0.05)
            el.answer(final="αργά")
            ls.switch("el")
        task = asyncio.create_task(later())
        assert await ls.wait_for_transcript(timeout=1.0) == "αργά" or ls.switched() == ("el", "αργά")
        await task
    asyncio.run(run())


def test_finish_takes_the_active_stream_and_closes_the_rest():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("en").finals.append("the end")
        assert await ls.finish() == "the end"
        await asyncio.sleep(0)
        assert by_language("el").closed and by_language("en").closed
    asyncio.run(run())


def test_close_flags_the_wrapper_and_a_late_open_closes_its_own_stream():
    async def run():
        ls = await opened(build())
        gate = asyncio.Event()
        real_start = FakeStream.start

        async def slow_start(self, **kw):
            await gate.wait()
            await real_start(self, **kw)
        FakeStream.start = slow_start
        try:
            task = asyncio.create_task(ls.open("el"))
            await asyncio.sleep(0)
            await ls.close()
            gate.set()
            assert await task is False
        finally:
            FakeStream.start = real_start
        await asyncio.sleep(0)
        assert all(s.closed for s in FakeStream.built)
        assert ls.closed and not ls.is_open
    asyncio.run(run())


def test_dictation_mode_reaches_every_stream():
    async def run():
        ls = build()
        ls.enable_dictation_mode()
        await opened(ls)
        await ls.open("el")
        assert all(s.dictation_mode for s in FakeStream.built)
    asyncio.run(run())


def test_partial_finals_are_forwarded_from_the_active_stream_only():
    async def run():
        ls = await opened(build())
        got = []
        ls.on_partial_final = got.append
        await ls.open("el")
        by_language("en")._emit_partial_final("from en")
        by_language("el")._emit_partial_final("from el")
        assert got == ["from en"]
    asyncio.run(run())


def test_switched_carries_the_text_as_of_the_call_not_the_resolve():
    """A final drained between the resolve and the consumer's poll is in the
    text the consumer shows again, so the replace never lacks a final that
    already went out."""
    async def run():
        ls = await opened(build())
        await ls.open("el")
        el = by_language("el")
        el.answer(final="ένα")
        ls.switch("el")
        assert ls.pending is None                           # resolved inside the read
        el.answer(final="δύο")
        assert ls.drain_transcript() == "δύο"              # drained before the consumer polled
        assert ls.switched() == ("el", "ένα δύο")
    asyncio.run(run())


# ── The words decide (providers that report a word confidence) ──────────


class WordStream(FakeStream):
    """A stream whose provider reports a word confidence: ``hear`` writes
    its ledger on its own audio clock."""

    capabilities = STTCapabilities(reports_confidence=True)

    def hear(self, start, end, words, confidence, *, final=True, text=""):
        entry = Heard(start, end, words, confidence if words else 0.0, final)
        if final:
            self._heard_final(entry)
            if text:
                self.finals.append(text)
            self._interim = ""
        else:
            self._heard_interim(entry)
            self._interim = text
        self._last_result = time.monotonic()


def words() -> LanguageStreams:
    return LanguageStreams(WordStream, "en")


async def _two_seconds_then_el(ls: LanguageStreams) -> tuple[WordStream, WordStream]:
    """Two seconds to the default, then the Greek candidate with all of it
    replayed (its clock starts where the default's does)."""
    for _ in range(20):
        await ls.send_audio(CHUNK)
    assert await ls.open("el") is True
    return by_language("en"), by_language("el")


def test_the_candidates_words_beating_the_shown_streams_resolve_the_switch():
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)                              # heard the span, found nothing
        el.hear(0.0, 2.0, 6, 0.92, text="Καλησπέρα με ακούς")
        assert ls.switch("el") is True
        assert ls.switched() == ("el", "Καλησπέρα με ακούς") and ls.refused() is None
    asyncio.run(run())


def test_the_shown_streams_words_refuse_the_switch_once():
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 8, 0.97)
        el.hear(0.0, 2.0, 7, 0.70)
        ls.switch("el")
        assert ls.pending is None and ls.active == "en" and ls.switched() is None
        language, verdict = ls.refused()
        assert language == "el" and verdict.outcome == SHOWN and not verdict.decisive
        assert ls.refused() is None
        assert "el" in ls.languages                            # kept: the next proposal compares again
    asyncio.run(run())


def test_even_words_hold_the_switch_until_it_is_abandoned(monkeypatch):
    monkeypatch.setattr(S, "SWITCH_READY_S", 0.05)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 6, 0.85)
        el.hear(0.0, 2.0, 6, 0.88)
        ls.switch("el")
        assert ls.pending == "el"
        await asyncio.sleep(0.08)
        assert ls.pending is None and ls.active == "en" and ls.refused() is None
    asyncio.run(run())


def test_a_switch_whose_candidate_has_no_word_waits_for_its_next_window(monkeypatch):
    """nova-3 answered the Greek candidate's first window empty, and the
    English stream answers Greek speech empty too: no verdict can form, so
    the switch waits (it used to be given up 1.5 s after the candidate
    caught up, the dictation settling in English with an empty box)."""
    monkeypatch.setattr(S, "SWITCH_READY_S", 0.05)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 2.0, 0, 0.0)
        ls.switch("el")
        await asyncio.sleep(0.15)
        assert ls.pending == "el"                              # held past the ready bound
        en.hear(2.0, 3.5, 0, 0.0)                              # English hears Greek as nothing
        el.hear(2.0, 3.5, 5, 0.91, text="Καλησπέρα με ακούς")  # the candidate's next window
        assert ls.switched() is not None and ls.active == "el"
    asyncio.run(run())


def test_a_candidate_with_no_word_yet_is_waited_for_even_against_the_shown_streams_words(monkeypatch):
    """The English stream turns Greek speech into a few weak English words
    while the Greek candidate's first window came back empty."""
    monkeypatch.setattr(S, "SWITCH_READY_S", 0.05)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 4, 0.42)
        el.hear(0.0, 2.0, 0, 0.0)
        ls.switch("el")
        await asyncio.sleep(0.15)
        assert ls.pending == "el"
        en.hear(2.0, 3.5, 3, 0.40)
        el.hear(2.0, 3.5, 6, 0.93, text="Καλησπέρα με ακούς")
        assert ls.switched() is not None and ls.active == "el"
    asyncio.run(run())


def _streams_clock(monkeypatch):
    """The switch's clock, moved by hand (no sleep can race a bound)."""
    offset = [0.0]
    monkeypatch.setattr(S, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))

    def advance(seconds: float) -> None:
        offset[0] += seconds
    return advance


def test_the_candidates_first_words_get_their_own_time_after_an_empty_window(monkeypatch):
    """Its first worded answer is a partial (a win must hold on two answers):
    the time to the second runs from those words, not from the empty window
    the candidate caught up with seconds earlier."""
    advance = _streams_clock(monkeypatch)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 2.0, 0, 0.0)                               # caught up, no word
        ls.switch("el")
        advance(S.SWITCH_READY_S + 0.5)                         # past the ready bound
        en.hear(2.0, 3.5, 0, 0.0, final=False)
        el.hear(2.0, 3.5, 6, 0.95, final=False, text="Καλησπέρα με")
        assert ls.pending == "el"                               # one winning answer: held, not given up
        el.hear(2.0, 4.0, 8, 0.95, final=False, text="Καλησπέρα με ακούς")
        assert ls.switched() is not None and ls.active == "el"
    asyncio.run(run())


def test_a_wordless_candidate_against_strong_shown_words_goes_at_the_settle_point(monkeypatch):
    """Accented English: the Greek candidate answers without a word while
    the shown stream hears it clearly. During speech it is held (its next
    window may bring words); once the take or the stop settled every stream,
    it goes ``SWITCH_READY_S`` after that, never the long bound."""
    advance = _streams_clock(monkeypatch)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 8, 0.90)
        el.hear(0.0, 2.0, 0, 0.0)
        ls.switch("el")
        advance(S.SWITCH_READY_S + 0.5)
        assert ls.pending == "el"                               # speech goes on: held
        ls.settle_point()
        advance(S.SWITCH_READY_S - 0.2)
        assert ls.pending == "el"
        advance(0.3)
        assert ls.pending is None and ls.active == "en"         # 1.5 s after the settle point, not 8 s
    asyncio.run(run())


def test_a_settle_point_late_in_the_hold_never_stretches_it(monkeypatch):
    """A long first turn: the take settles 7 s into the hold; the candidate
    goes at the long bound, not 1.5 s after the settle point."""
    advance = _streams_clock(monkeypatch)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 8, 0.90)
        el.hear(0.0, 2.0, 0, 0.0)
        ls.switch("el")
        advance(S.SWITCH_WORDLESS_MAX_S - 1.0)
        assert ls.pending == "el"
        ls.settle_point()
        advance(1.1)
        assert ls.pending is None and ls.active == "en"
    asyncio.run(run())


def test_a_wordless_switch_is_given_up_at_its_own_bound(monkeypatch):
    monkeypatch.setattr(S, "SWITCH_READY_S", 0.05)
    monkeypatch.setattr(S, "SWITCH_WORDLESS_MAX_S", 0.1)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 2.0, 0, 0.0)
        ls.switch("el")
        await asyncio.sleep(0.15)
        en.hear(2.0, 2.5, 0, 0.0, final=False)                 # a new answer: the verdict is read again
        assert ls.pending is None and ls.active == "en"
    asyncio.run(run())


def test_a_reading_that_moves_back_ends_a_wordless_switch():
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 2.0, 0, 0.0)
        ls.switch("el")
        assert ls.pending == "el"
        assert ls.switch("en") is True                          # the rule proposes the shown language
        assert ls.pending is None and ls.active == "en"
    asyncio.run(run())


def test_the_calls_second_listen_reaches_every_stream_and_the_active_completes():
    taken: list[str] = []
    enabled: list[str] = []

    class Turned(WordStream):
        def enable_turn_relisten(self):
            enabled.append(self.language or "?")

        async def complete_turn(self, text, bound):
            return f"{text} +{self.language}"

        def turn_taken(self):
            taken.append(self.language)

    async def run():
        ls = await opened(LanguageStreams(Turned, "en"))
        ls.enable_turn_relisten()
        await ls.send_audio(CHUNK)
        await ls.open("el")
        assert len(enabled) == 2                                # the default, then the candidate built
        assert await ls.complete_turn("ένα", 2.0) == "ένα +en"   # the active stream's
        ls.turn_taken()
        assert sorted(taken) == ["el", "en"]
    asyncio.run(run())


def test_a_provider_without_confidence_keeps_the_acoustic_rule(caplog):
    import logging

    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK)
        await ls.open("el")
        by_language("el").answer(interim="γ")
        with caplog.at_level(logging.INFO, logger="audio.providers.stt.streams"):
            ls.switch("el")
            assert ls.switched() == ("el", "")
    asyncio.run(run())
    assert any("words not compared" in r.getMessage() for r in caplog.records)


def test_one_word_each_side_decides_once_the_speech_settled():
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 1, 0.55)
        el.hear(0.0, 2.0, 1, 0.95, text="Ναι")
        ls.switch("el")
        assert ls.pending == "el"                              # one word is no evidence while speech goes on
        ls.settle_point()
        assert ls.switched() == ("el", "Ναι")
        ls.take()
        assert ls._speech_settled is False                      # the take ends the settled point
    asyncio.run(run())


def test_the_defaults_decisive_words_call_a_resolved_switch_back():
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 2.0, 5, 0.9)
        ls.switch("el")
        assert ls.switched()[0] == "el"
        assert ls.called_back() is None
        for _ in range(30):                                    # three more seconds
            await ls.send_audio(CHUNK)
        en.hear(2.0, 5.0, 12, 0.97, text="and then I said")
        el.hear(2.0, 5.0, 10, 0.62)
        assert ls.called_back() == "en" and ls.called_back() is None
        assert ls.switch("en") is True                         # resolves on the same comparison
        assert ls.switched() == ("en", "and then I said") and ls.active == "en"
    asyncio.run(run())


def test_an_endpoint_waits_for_a_candidate_still_working_through_its_replay():
    """The vendor transcribes a replay at about real-time pace: an endpoint
    sent before the candidate caught up would close its window mid-replay."""
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 1.5, 0, 0.0, final=False)
        await ls.force_endpoint()
        assert en.endpoints == 1 and el.endpoints == 0          # the default keeps up; the candidate is behind
        el.hear(0.0, 1.0, 2, 0.9, final=False)
        assert ls.pending is None and el.endpoints == 0         # still behind
        el.hear(0.0, 1.8, 4, 0.9, final=False)
        ls.drain_transcript()                                   # any poll sends it once caught up
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert el.endpoints == 1
    asyncio.run(run())


def test_an_opening_candidate_holds_its_endpoint_instead_of_sending_it_after_the_replay():
    async def run():
        ls = await opened(words())
        for _ in range(10):
            await ls.send_audio(CHUNK)
        gate = asyncio.Event()
        real_start = FakeStream.start

        async def slow_start(self, **kw):
            await gate.wait()
            await real_start(self, **kw)
        WordStream.start = slow_start
        try:
            task = asyncio.create_task(ls.open("el"))
            await asyncio.sleep(0)
            await ls.force_endpoint()
            gate.set()
            await task
        finally:
            WordStream.start = real_start
        el = by_language("el")
        await asyncio.sleep(0)
        assert el.endpoints == 0 and ls._streams["el"].endpoint_at == pytest.approx(1.0)
        el.hear(0.0, 1.0, 3, 0.9, final=False)
        ls.drain_transcript()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert el.endpoints == 1
    asyncio.run(run())


def test_a_held_endpoint_goes_out_at_its_bound_whatever_the_stream_did(monkeypatch):
    monkeypatch.setattr(S, "ENDPOINT_HOLD_MAX_S", 0.05)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        await ls.force_endpoint()
        assert el.endpoints == 0
        await asyncio.sleep(0.08)
        ls.drain_transcript()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert el.endpoints == 1
    asyncio.run(run())


def test_wait_answered_waits_for_a_lagging_active_stream():
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 1.0, 3, 0.95, text="Καλησπέρα με")          # a final: the win needs no second answer
        ls.switch("el")
        assert ls.switched()[0] == "el" and ls.active_behind

        async def catches_up():
            await asyncio.sleep(0.1)
            el.hear(0.0, 2.0, 6, 0.94, text="Καλησπέρα με ακούς κανονικά")
        task = asyncio.create_task(catches_up())
        t0 = time.monotonic()
        await ls.wait_answered(1.0)
        assert time.monotonic() - t0 < 0.5 and not ls.active_behind
        assert ls.drain_transcript() == "Καλησπέρα με ακούς κανονικά"
        await task
    asyncio.run(run())


def test_the_take_keeps_the_active_text_it_drained():
    async def run():
        ls = await opened(build())
        by_language("en").answer(final="late words")          # landed after the consumer's drain
        ls.take()
        assert ls.drained_at_take() == "late words" and ls.drained_at_take() == ""
        assert ls.text_since_take() == ""
    asyncio.run(run())


def test_the_discard_mark_survives_the_take_that_follows_it_and_dies_at_the_next():
    async def run():
        ls = await opened(build())
        ls.discard_next_final(within_s=5.0)
        ls.take()                                              # the arming turn's own take
        by_language("en").answer(final="the turn's words")     # lands after it: dropped
        assert ls.drain_transcript() is None
        ls.discard_next_final(within_s=5.0)
        ls.take()
        ls.take()                                              # the final never came: gone at the next take
        by_language("en").answer(final="the next turn")
        assert ls.drain_transcript() == "the next turn"
    asyncio.run(run())


def test_the_discard_mark_dies_at_its_deadline():
    async def run():
        ls = await opened(build())
        ls.discard_next_final(within_s=0.05)
        await asyncio.sleep(0.08)
        await ls.send_audio(CHUNK)                             # a frame past the deadline ends the mark
        by_language("en").answer(final="the next utterance")
        assert ls.drain_transcript() == "the next utterance"
    asyncio.run(run())


def test_born_at_follows_a_trimmed_replay_and_the_take():
    async def run():
        ls = await opened(words())
        for _ in range(30):                                    # 3.0 s
            await ls.send_audio(CHUNK)
        ls.trim_replay(0.5)
        assert await ls.open("el") is True
        el = ls._streams["el"]
        assert el.offset == pytest.approx(2.5) and el.born_at == pytest.approx(2.5)
        by_language("el").hear(0.0, 0.5, 2, 0.9, final=False)
        assert el.side().heard[0].start == pytest.approx(2.5)  # on the shared clock
        await ls.send_audio(CHUNK)
        ls.take()
        await asyncio.sleep(0)
        en = ls._streams["en"]
        assert en.born_at == pytest.approx(3.1)
    asyncio.run(run())


def test_a_drain_answers_for_the_active_stream_while_evidence_looks_at_every_stream():
    async def run():
        ls = await opened(build())
        await ls.open("el")
        by_language("el").answer(final="γεια σας")
        assert ls.has_pending_finals is True                # the barge-in evidence
        assert ls.has_drainable_finals is False             # nothing a drain would return
        by_language("en").answer(final="hello")
        assert ls.has_drainable_finals is True
    asyncio.run(run())


def test_the_words_time_starts_once_a_candidate_caught_up_with_its_replay(monkeypatch):
    """A candidate opened at a take or a stop replays the whole utterance and
    answers it at about real-time pace: an undecided switch is not abandoned
    while it is still catching up (up to the cap), then the words get their
    time."""
    monkeypatch.setattr(S, "SWITCH_READY_S", 0.05)
    monkeypatch.setattr(S, "SWITCH_WAIT_MAX_S", 0.3)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 0.8, 1, 0.9, final=False)                  # still in its replay
        ls.switch("el")
        await asyncio.sleep(0.1)
        assert ls.pending == "el"                              # behind: not abandoned
        el.hear(0.0, 2.0, 1, 0.9, final=False)                  # caught up, still undecided
        assert ls.pending == "el"
        await asyncio.sleep(0.08)
        assert ls.pending is None and ls.active == "en"        # its time ran out
    asyncio.run(run())


def test_a_candidate_that_never_catches_up_is_abandoned_at_the_cap(monkeypatch):
    monkeypatch.setattr(S, "SWITCH_WAIT_MAX_S", 0.1)

    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        ls.switch("el")
        await asyncio.sleep(0.15)
        assert ls.pending is None and ls.active == "en"
    asyncio.run(run())


def test_clear_words_decide_while_the_candidate_is_still_behind():
    async def run():
        ls = await opened(words())
        for _ in range(30):                                    # 3 s, all replayed
            await ls.send_audio(CHUNK)
        await ls.open("el")
        en, el = by_language("en"), by_language("el")
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 1.0, 3, 0.93, final=False)                 # behind, already clear
        ls.switch("el")
        assert ls.pending == "el"                               # one answer: held
        el.hear(0.0, 1.5, 4, 0.93, final=False)                 # the next answer agrees
        assert ls.switched() is not None and ls.active == "el"
    asyncio.run(run())


def test_a_held_endpoint_goes_out_once_the_stream_had_the_time_to_catch_up(monkeypatch):
    """The vendor works at real-time pace at least: live for as long as the
    audio it had to answer, the stream has caught up even when it holds its
    next answer until its final."""
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        el_stream = ls._streams["el"]
        el_stream.live_since = time.monotonic() - 1.0          # live for a second of its two
        await ls.force_endpoint()
        assert el.endpoints == 0 and el_stream.endpoint_at == pytest.approx(2.0)
        el_stream.live_since = time.monotonic() - 2.4           # now as long as its audio
        ls.drain_transcript()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert el.endpoints == 1
    asyncio.run(run())


def test_a_close_right_after_a_held_endpoint_went_out_leaves_no_call_unawaited():
    def run():
        async def go():
            ls = await opened(words())
            _en, el = await _two_seconds_then_el(ls)
            el_stream = ls._streams["el"]
            el_stream.live_since = time.monotonic() - 1.0
            await ls.force_endpoint()                             # held
            el_stream.live_since = time.monotonic() - 2.4
            ls.drain_transcript()                                 # goes out as a side call
            await ls.close()                                      # before that call ran
        asyncio.run(go())
        gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run()
    assert not [w for w in caught if "never awaited" in str(w.message)]


def test_a_win_on_one_early_window_that_does_not_hold_never_resolves():
    async def run():
        ls = await opened(words())
        en, el = await _two_seconds_then_el(ls)
        en.hear(0.0, 2.0, 0, 0.0)
        el.hear(0.0, 1.0, 3, 0.9, final=False)                 # one flattering window
        ls.switch("el")
        assert ls.pending == "el"
        el.hear(0.0, 2.0, 3, 0.5, final=False)                 # the next answer does not hold it
        en.hear(0.0, 2.0, 6, 0.95)
        assert ls.active == "en"
    asyncio.run(run())


def test_the_marked_final_is_dropped_when_it_arrives_whenever_it_is_drained():
    """The mark lives 0.2 s; the stale final lands at 0.1 s; the next drain
    comes at 0.4 s, past the mark's deadline: the final is gone all the
    same (decided at its arrival, a frame after it)."""
    async def run():
        ls = await opened(build())
        forwarded = []
        ls.on_partial_final = forwarded.append
        ls.discard_next_final(within_s=0.2)
        await asyncio.sleep(0.1)
        by_language("en").answer(final="the turn just taken")
        by_language("en")._emit_partial_final("the turn just taken")
        await ls.send_audio(CHUNK)                             # the frame after its arrival
        await asyncio.sleep(0.3)
        assert ls.drain_transcript() is None
        assert forwarded == []                                 # never pushed to a live caption either
        by_language("en").answer(final="the next turn")
        await ls.send_audio(CHUNK)
        assert ls.drain_transcript() == "the next turn"
    asyncio.run(run())


def test_a_silent_default_stream_is_never_behind():
    """A pause is no lag: the default's endpoint goes at once and a stop with
    no switch never waits for a catch-up."""
    async def run():
        ls = await opened(words())
        for _ in range(30):                                     # 3 s, the default answered only the first
            await ls.send_audio(CHUNK)
        by_language("en").hear(0.0, 1.0, 2, 0.95)
        assert not ls._streams["en"].behind and not ls.active_behind
        await ls.force_endpoint()
        assert by_language("en").endpoints == 1
    asyncio.run(run())


def test_the_silence_fed_during_playback_stays_in_the_replay():
    """The default's clock counts the playback's silence; the replay keeps it
    too, so a candidate's words land where the default heard the speech."""
    async def run():
        ls = await opened(words())
        await ls.send_audio(CHUNK * 5)                          # 0.5 s of speech
        await ls.feed_during_tts(CHUNK * 3)                     # 0.3 s of playback
        await ls.send_audio(CHUNK * 2)                          # 0.2 s more
        assert ls.register("el") is True
        el = ls._streams["el"]
        assert len(el.replay) == len(CHUNK) * 10
        assert el.offset == pytest.approx(0.0)
    asyncio.run(run())


def test_only_the_shown_stream_or_while_pending_the_candidate_listens_again():
    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK)
        await ls.open("el")
        en, el = by_language("en"), by_language("el")
        assert en.relisten_gate() is True and el.relisten_gate() is False
        ls.switch("el")
        assert ls.pending == "el" and el.relisten_gate() is True and en.relisten_gate() is False
        el.answer(final="γεια σας")
        assert ls.switched() is not None
        assert el.relisten_gate() is True and en.relisten_gate() is False   # a default in the wrong language
    asyncio.run(run())


def test_the_second_listens_bill_on_every_stream_open_or_closed():
    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK)
        await ls.open("el")
        by_language("en").relisten = 1.5
        by_language("el").relisten = 2.0
        assert ls.relisten_seconds == 3.5
        ls.settle()                                        # the candidate closes
        await asyncio.sleep(0)
        assert ls.relisten_seconds == 3.5
        await ls.close()
        assert ls.relisten_seconds == 3.5
    asyncio.run(run())


def test_every_stream_spends_one_budget_for_the_dictation():
    async def run():
        ls = await opened(build())
        await ls.send_audio(CHUNK)
        await ls.open("el")
        by_language("en").relisten = 1.5
        by_language("el").relisten = 2.0
        for stream in (by_language("en"), by_language("el")):
            assert stream.dictation_seconds() == ls.billed_seconds
            assert stream.dictation_relistened() == 3.5
    asyncio.run(run())


def test_the_finish_bound_and_the_display_go_to_the_active_stream():
    async def run():
        ls = await opened(build())
        ls.bound_finish(1.25)
        assert by_language("en").bound == 1.25
        by_language("en").answer(interim="ένα")
        assert ls.display_interim == "ένα"
    asyncio.run(run())
