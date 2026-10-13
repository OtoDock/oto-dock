"""DeepgramSTT contract tests (no network — construction + pure surface only)."""

import asyncio
from audio.providers.stt.deepgram import DeepgramSTT


def test_capabilities():
    caps = DeepgramSTT.capabilities
    assert caps.supports_streaming
    assert caps.supports_transcribe_file       # needed by the transcribe endpoint
    assert caps.supports_word_timestamps        # needed for SRT
    assert not caps.is_local


def test_billing():
    assert DeepgramSTT.billing_unit() == "second"
    assert DeepgramSTT.cost_per_unit() > 0
    assert DeepgramSTT.is_free_tier is False


def test_from_row_uses_resolver_and_advanced():
    row = {
        "provider_type": "stt",
        "provider_name": "deepgram",
        "credential_key": "audio-deepgram",
        "advanced": {"call_endpointing_ms": 333, "vad_silence_offset_ms": 75},
    }
    seen = {}

    def resolver(key):
        seen["key"] = key
        return "secret"

    stt = DeepgramSTT.from_row(row, resolver)
    assert seen["key"] == "audio-deepgram"
    assert stt.endpointing_ms == 333
    assert stt.vad_silence_padding_ms == 333 + 75


def test_validate_advanced():
    assert DeepgramSTT.validate_advanced({"call_endpointing_ms": 200}) == {}
    errs = DeepgramSTT.validate_advanced({"call_endpointing_ms": -1, "vad_silence_offset_ms": "x"})
    assert "call_endpointing_ms" in errs and "vad_silence_offset_ms" in errs


def test_repr_redacts_api_key():
    stt = DeepgramSTT(api_key="super-secret-token")
    assert "super-secret-token" not in repr(stt)


def test_to_deepgram_lang_normalizes_bcp47():
    from audio.providers.stt.deepgram import _to_deepgram_lang
    assert _to_deepgram_lang("el-GR") == "el"     # Greek: Deepgram has no regional variant
    assert _to_deepgram_lang("de-DE") == "de"
    assert _to_deepgram_lang("es-ES") == "es"
    assert _to_deepgram_lang("fr-FR") == "fr"
    assert _to_deepgram_lang("it-IT") == "it"
    assert _to_deepgram_lang("en-US") == "en-US"  # English regionals are kept
    assert _to_deepgram_lang("en-GB") == "en-GB"
    assert _to_deepgram_lang("el") == "el"         # already a base code (phone path)
    assert _to_deepgram_lang("") == "multi"        # empty → auto-detect


async def test_on_error_surfaces_via_pop_fatal_error():
    stt = DeepgramSTT(api_key="k")
    assert stt.pop_fatal_error() is None
    await stt._on_error(None, "invalid credentials")
    err = stt.pop_fatal_error()
    assert err is not None and "invalid credentials" in err
    assert stt.pop_fatal_error() is None  # surfaced once, then cleared


async def test_stale_generation_error_is_ignored():
    """A replaced socket's dying gasp (net0001 after a mid-call reconnect)
    must not surface as a pipeline "STT fatal" for the LIVE connection —
    the error callback is generation-guarded like _on_close (live-hit
    2026-08-24: the gen-1 corpse warned STT-fatal while gen-2 was healthy)."""
    stt = DeepgramSTT(api_key="k")
    stt._connection_gen = 2
    await stt._on_error(None, "timeout net0001", _gen=1)
    assert stt.pop_fatal_error() is None       # stale — swallowed
    await stt._on_error(None, "real failure", _gen=2)
    err = stt.pop_fatal_error()
    assert err is not None and "real failure" in err


def test_has_pending_finals_tracks_queue_not_interim():
    """Barge-in evidence: mid-speech is_finals clear latest_interim and sit
    in the queue — the pause backstop reads has_pending_finals to see them."""
    stt = DeepgramSTT(api_key="k")
    assert stt.has_pending_finals is False
    stt._latest_interim = "still talking"
    assert stt.has_pending_finals is False     # interims aren't queue entries
    stt._transcript_queue.put_nowait("nice thank you and can you also check")
    assert stt.has_pending_finals is True
    assert stt.drain_transcript() == "nice thank you and can you also check"
    assert stt.has_pending_finals is False


async def test_final_transcript_fires_on_partial_final():
    """R4.6: every is_final lands in the drain queue AND pushes through
    ``on_partial_final`` — streaming UIs render the accumulating utterance
    live from the push (mid-utterance finals are invisible to a polled
    ``latest_interim`` overlay). Interims never fire it, and a raising
    callback must not kill the receive loop."""
    stt = DeepgramSTT(api_key="k")
    got: list[str] = []
    stt.on_partial_final = got.append

    class _Alt:
        transcript = "hello there"

    class _Chan:
        alternatives = [_Alt()]

    class _Res:
        channel = _Chan()
        is_final = True

    await stt._on_transcript(None, _Res())
    assert got == ["hello there"]
    assert stt.drain_transcript() == "hello there"

    _Res.is_final = False
    _Alt.transcript = "hel"
    await stt._on_transcript(None, _Res())
    assert got == ["hello there"]          # interims don't fire the push
    assert stt.latest_interim == "hel"

    def _boom(_text):
        raise RuntimeError("boom")

    stt.on_partial_final = _boom
    _Res.is_final = True
    _Alt.transcript = "more"
    await stt._on_transcript(None, _Res())  # must not raise
    assert stt.drain_transcript() == "more"


# ── Dictation mode (chat-input relay; painted words must neither die nor double) ─
#
# The composer paints interims as committed text. Deepgram finals partition
# the stream: a final covers [start, start+duration) and the next window
# starts exactly at that end, re-recognizing everything after it — so words
# of the shown interim past a final's end come back on their own (committing
# them would print them twice: the English duplicate-words bug, 2026-09-08),
# while a word that began inside the range is gone for good unless promoted
# (Deepgram drops the word the boundary cuts; and on some languages it closes
# a window with an EMPTY final, orphaning the whole painted interim — the
# Greek vanishing-words bug, 2026-09-01). Dictation mode promotes exactly the
# consumed part; default mode keeps call/duplex semantics byte-for-byte (a
# promoted noise interim would be dispatched turn text / false barge-in
# evidence there). Timings below are real Deepgram numbers from a probe.


def _res(text: str, final: bool, *, start: float = 0.0, end: float | None = None,
         words: list[tuple[str, float, float]] | None = None):
    """A live result. ``words`` carries (punctuated word, start, end); when
    given, ``end`` defaults to the last word's end. Without ``words`` the
    result is untimed (the text-only fallback path)."""

    class _Word:
        def __init__(self, w, s, e):
            self.punctuated_word, self.word, self.start, self.end = w, w.strip(".,;"), s, e

    class _Alt:
        transcript = text

    _Alt.words = [_Word(*w) for w in (words or [])]

    class _Chan:
        alternatives = [_Alt()]

    class _Res:
        channel = _Chan()
        is_final = final

    _Res.start = start
    _Res.duration = ((end if end is not None else (words[-1][2] if words else 0.0)) - start)
    return _Res()


def _timed(text: str, start: float, step: float = 0.4) -> list[tuple[str, float, float]]:
    """Evenly timed words for ``text`` from ``start`` (each ``step`` seconds)."""
    out, t = [], start
    for w in text.split():
        out.append((w, round(t, 2), round(t + step, 2)))
        t += step
    return out


async def test_dictation_empty_final_promotes_consumed_interim():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    words = _timed("πήγα στο σπίτι μου σήμερα", 0.5)
    await stt._on_transcript(None, _res("πήγα στο σπίτι μου σήμερα", False, words=words))
    assert stt.pop_interim() == "πήγα στο σπίτι μου σήμερα"
    await stt._on_transcript(None, _res("", True, start=0.0, end=2.8))  # empty final past every word
    assert stt.drain_transcript() == "πήγα στο σπίτι μου σήμερα"
    assert stt.latest_interim == ""


async def test_dictation_empty_final_leaves_words_past_its_end_live():
    """Words the empty final's range does not reach are re-delivered by the
    next window — they stay painted as the live interim instead of committing."""
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    words = _timed("ένα δύο τρία τέσσερα", 0.5)  # ένα 0.5, δύο 0.9, τρία 1.3, τέσσερα 1.7
    await stt._on_transcript(None, _res("ένα δύο τρία τέσσερα", False, words=words))
    stt.pop_interim()
    await stt._on_transcript(None, _res("", True, start=0.0, end=1.2))  # consumed: ένα, δύο
    assert stt.drain_transcript() == "ένα δύο"
    assert stt.pop_interim() == "τρία τέσσερα"


async def test_dictation_empty_final_consuming_nothing_keeps_interim():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("home into", False, words=_timed("home into", 8.46)))
    assert stt.pop_interim() == "home into"
    await stt._on_transcript(None, _res("", True, start=8.38, end=8.38))  # zero-length final at the window start
    assert stt.drain_transcript() is None
    assert stt.latest_interim == "home into"


async def test_default_mode_empty_final_still_drops_interim():
    stt = DeepgramSTT(api_key="k")
    await stt._on_transcript(None, _res("background noise guess", False, words=_timed("background noise guess", 0.2)))
    await stt._on_transcript(None, _res("", True, start=0.0, end=2.0))
    assert stt.drain_transcript() is None  # call/duplex semantics unchanged


async def test_dictation_short_final_keeps_re_delivered_tail_live_and_commits_the_cut_word():
    """The probe's boundary: the final ends at 8.38, "smart" began at 8.25 (cut
    by the boundary — Deepgram never delivers it again), "home into" begin
    after it (re-delivered by the next window)."""
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    shown = [("And", 4.97, 5.21), ("your", 5.21, 5.37), ("assistant", 5.37, 5.85), ("pulls", 5.85, 6.25),
             ("calendar,", 6.25, 7.05), ("weather,", 7.29, 7.93), ("and", 7.93, 8.09), ("your", 8.09, 8.25),
             ("smart", 8.25, 8.57), ("home", 8.57, 8.81), ("into", 8.81, 9.05)]
    await stt._on_transcript(None, _res(" ".join(w for w, *_ in shown), False, start=4.97, end=9.3, words=shown))
    assert stt.pop_interim() == "And your assistant pulls calendar, weather, and your smart home into"
    final = shown[:8]
    await stt._on_transcript(None, _res("And your assistant pulls calendar, weather, and your", True,
                                        start=4.97, end=8.38, words=final))
    assert stt.drain_transcript() == "And your assistant pulls calendar, weather, and your smart"
    assert stt.pop_interim() == "home into"  # still painted, no blink
    nxt = [("home", 8.46, 8.78), ("into", 8.78, 9.18), ("one", 9.18, 9.42), ("glanceable", 9.42, 9.9)]
    await stt._on_transcript(None, _res("home into one glanceable", False, start=8.38, end=10.0, words=nxt))
    assert stt.pop_interim() == "home into one glanceable"
    fin2 = nxt + [("card.", 10.06, 10.3)]
    await stt._on_transcript(None, _res("home into one glanceable card.", True, start=8.38, end=11.4, words=fin2))
    assert stt.drain_transcript() == "home into one glanceable card."


async def test_dictation_short_final_with_tail_wholly_past_its_end_commits_final_only():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    head = _timed("Ask for a morning brief, and your assistant pulls calendar, weather,", 14.66, 0.3)
    tail = [("and", 18.42, 18.58), ("your", 18.58, 18.74), ("smart", 18.74, 19.06), ("home", 19.06, 19.22), ("in", 19.22, 19.38)]
    await stt._on_transcript(None, _res(" ".join(w for w, *_ in head + tail), False, start=14.42, end=19.4, words=head + tail))
    stt.pop_interim()
    await stt._on_transcript(None, _res(" ".join(w for w, *_ in head), True, start=14.42, end=18.34, words=head))
    assert stt.drain_transcript() == "Ask for a morning brief, and your assistant pulls calendar, weather,"
    assert stt.pop_interim() == "and your smart home in"


async def test_dictation_re_delivered_cut_word_is_stripped_once():
    """A promoted word that straddled the boundary may be re-recognized from
    its tail audio in the next window (start clamped to the window start):
    that copy is dropped from the interim AND the final; a genuine repetition
    (starting after the promoted word ended) is kept."""
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    shown = [("and", 7.93, 8.09), ("your", 8.09, 8.25), ("smart", 8.25, 8.57), ("home", 8.57, 8.81)]
    await stt._on_transcript(None, _res("and your smart home", False, start=4.97, end=9.0, words=shown))
    stt.pop_interim()
    await stt._on_transcript(None, _res("and your", True, start=4.97, end=8.38, words=shown[:2]))
    assert stt.drain_transcript() == "and your smart"
    assert stt.pop_interim() == "home"
    redelivered = [("smart", 8.38, 8.54), ("home", 8.54, 8.78), ("into", 8.78, 9.18)]
    await stt._on_transcript(None, _res("smart home into", False, start=8.38, end=9.3, words=redelivered))
    assert stt.pop_interim() == "home into"
    await stt._on_transcript(None, _res("smart home into one", True, start=8.38, end=9.8,
                                        words=redelivered + [("one", 9.18, 9.42)]))
    assert stt.drain_transcript() == "home into one"
    # The strip is spent with that window: a later "smart" is real speech.
    await stt._on_transcript(None, _res("smart", False, start=9.8, end=10.4, words=[("smart", 9.9, 10.3)]))
    assert stt.pop_interim() == "smart"


async def test_dictation_genuine_repetition_after_cut_word_is_kept():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("very", False, start=0.0, end=1.3, words=[("very", 0.9, 1.3)]))
    stt.pop_interim()
    await stt._on_transcript(None, _res("", True, start=0.0, end=1.1))  # cuts "very" (0.9-1.3)
    assert stt.drain_transcript() == "very"
    # The next window starts at 1.1; a second "very" spoken AFTER the first
    # ended (1.3) is a repetition, not the tail of the promoted word.
    await stt._on_transcript(None, _res("very good", True, start=1.1, end=2.2,
                                        words=[("very", 1.4, 1.8), ("good", 1.8, 2.1)]))
    assert stt.drain_transcript() == "very good"


async def test_dictation_revised_final_wins_when_not_an_extension():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    words = _timed("να πάμε τώρα εκεί", 0.5)
    await stt._on_transcript(None, _res("να πάμε τώρα εκεί", False, words=words))
    stt.pop_interim()
    await stt._on_transcript(None, _res("Θα πάμε τώρα.", True, start=0.0, end=2.5,
                                        words=_timed("Θα πάμε τώρα.", 0.5)))  # reword
    assert stt.drain_transcript() == "Θα πάμε τώρα."
    assert stt.latest_interim == ""  # "εκεί" (1.7) was inside the range: Deepgram's revision wins


async def test_dictation_shrunk_interim_not_resent():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("ένα δύο τρία", False))
    assert stt.pop_interim() == "ένα δύο τρία"
    await stt._on_transcript(None, _res("ένα δύο", False))  # pure backtrack
    assert stt.pop_interim() is None
    await stt._on_transcript(None, _res("ένα δύο τέσσερα", False))  # reword passes
    assert stt.pop_interim() == "ένα δύο τέσσερα"


async def test_dictation_short_final_after_backtrack_keeps_painted_text():
    """The never-drop guarantee compares against what was SENT (painted),
    not just the newest raw interim — a backtracked interim followed by a
    head-only final must still commit the longer painted text."""
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    words = _timed("ένα δύο τρία τέσσερα", 0.5)
    await stt._on_transcript(None, _res("ένα δύο τρία τέσσερα", False, words=words))
    stt.pop_interim()  # painted
    await stt._on_transcript(None, _res("ένα δύο", False, words=words[:2]))  # backtrack (suppressed)
    assert stt.pop_interim() is None
    await stt._on_transcript(None, _res("Ένα δύο.", True, start=0.0, end=2.5, words=_timed("Ένα δύο.", 0.5)))
    assert stt.drain_transcript() == "Ένα δύο. τρία τέσσερα"


async def test_dictation_untimed_results_count_everything_shown_as_consumed():
    """A result without word timings cannot place the boundary: the whole
    painted interim is treated as consumed (the pre-timing behaviour)."""
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("πήγα στο σπίτι μου σήμερα", False))
    stt.pop_interim()
    await stt._on_transcript(None, _res("Πήγα στο σπίτι.", True))
    assert stt.drain_transcript() == "Πήγα στο σπίτι. μου σήμερα"


async def test_dictation_finish_flushes_leftover_interim():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("τελευταία λέξη", False, words=_timed("τελευταία λέξη", 0.3)))
    stt.pop_interim()
    assert await stt.finish() == "τελευταία λέξη"
    assert stt.latest_interim == ""


async def test_default_mode_finish_drops_leftover_interim():
    stt = DeepgramSTT(api_key="k")
    await stt._on_transcript(None, _res("stray tail", False))
    assert await stt.finish() is None


async def test_dictation_promotion_never_fires_partial_final_push():
    """Promotions are queue-only: on_partial_final (duplex captions/barge-in
    wiring) stays real-final-only even in dictation mode."""
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    got: list[str] = []
    stt.on_partial_final = got.append
    await stt._on_transcript(None, _res("κάτι", False, words=_timed("κάτι", 0.2)))
    await stt._on_transcript(None, _res("", True, start=0.0, end=1.0))
    assert got == []
    assert stt.drain_transcript() == "κάτι"


async def test_start_and_clear_queue_reset_dictation_timeline():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("very", False, start=0.0, end=1.3, words=[("very", 0.9, 1.3)]))
    stt.pop_interim()
    await stt._on_transcript(None, _res("", True, start=0.0, end=1.1))
    assert stt._promoted_edge is not None
    stt.clear_queue()
    assert stt._promoted_edge is None and stt._latest_interim_words == []


# ── Liveness (guard-task surface; spy-based, no network) ────────────────


def test_ensure_alive_reconnects_with_active_params():
    """A mid-call reconnect must restart with the ACTIVE negotiated params —
    the old recover path dropped the sample rate back to the 8 kHz
    constructor default, decoding a 16 kHz duplex mic as garbage (audit F5)."""
    import asyncio

    stt = DeepgramSTT(api_key="k")
    stt._active_rate = 16000
    stt._interim_results = True
    stt._endpointing_override = 321
    stt._is_open = False
    seen = {}

    async def spy_start(language="multi", sample_rate=None,
                        interim_results=False, endpointing_ms=None):
        seen.update(language=language, sample_rate=sample_rate,
                    interim_results=interim_results, endpointing_ms=endpointing_ms)
        stt._is_open = True

    stt.start = spy_start
    ok = asyncio.run(stt.ensure_alive("el", clear_queue=False))
    assert ok is True
    assert seen == {"language": "el", "sample_rate": 16000,
                    "interim_results": True, "endpointing_ms": 321}


def test_ensure_alive_queue_semantics():
    """Healthy probe: clear_queue=False keeps queued finals (audit F6 — after
    a barge-in they are the interrupting utterance); True discards them."""
    import asyncio

    async def run():
        stt = DeepgramSTT(api_key="k")
        stt._is_open = True
        stt._transcript_queue.put_nowait("survivor")
        assert await stt.ensure_alive("en", clear_queue=False) is True
        assert stt.drain_transcript() == "survivor"

        stt._transcript_queue.put_nowait("echo artifact")
        assert await stt.ensure_alive("en", clear_queue=True) is True
        assert stt.drain_transcript() is None

    asyncio.run(run())


def test_recover_after_opening_wraps_ensure_alive():
    """The base wrapper keeps the historical opening semantics
    (clear stale echo transcripts on a healthy connection)."""
    import asyncio

    async def run():
        stt = DeepgramSTT(api_key="k")
        stt._is_open = True
        stt._transcript_queue.put_nowait("echo")
        assert await stt.recover_after_opening("en") is True
        assert stt.drain_transcript() is None

    asyncio.run(run())


def test_send_skip_warning_rate_limited(caplog):
    """A dead socket logs ONE send-skip warning per incident, not one per
    20 ms frame (a dead 35 s call tail used to log 1,700+)."""
    import asyncio
    import logging

    async def run():
        stt = DeepgramSTT(api_key="k")
        stt._connection = object()  # "had a connection"
        stt._is_open = False
        with caplog.at_level(logging.WARNING):
            for _ in range(50):
                await stt.send_audio(b"\x00" * 320)
        return stt

    stt = asyncio.run(run())
    skips = [r for r in caplog.records if "send skipped" in r.message]
    assert len(skips) == 1
    assert stt._send_skips == 50


# ── The SDK's own logger (no network) ────────────────────────────────────


def _sdk_connection():
    from deepgram import DeepgramClient
    return DeepgramClient(api_key="k" * 40).listen.asyncwebsocket.v("1")


def test_the_sdk_logs_once_through_the_process_and_drops_its_finish_noise(caplog):
    """The pinned SDK builds each connection's logger with its own bare
    stderr handler (every record printed twice) and logs the task it just
    cancelled at ERROR on every finish."""
    import logging
    from audio.providers.stt.deepgram import _route_sdk_logger

    conn = _sdk_connection()
    assert conn._logger.handlers and conn._logger is not logging.getLogger(conn._logger.name)
    _route_sdk_logger(conn)
    _route_sdk_logger(conn)
    assert conn._logger.handlers == [] and len(conn._logger.filters) == 1
    with caplog.at_level(logging.DEBUG):
        conn._logger.error("tasks cancelled error: %s", "")
        conn._logger.error("send() failed - ConnectionClosed: %s", "1006")
    seen = [r.getMessage() for r in caplog.records if r.name == conn._logger.name]
    assert seen == ["send() failed - ConnectionClosed: 1006"]


def test_start_routes_the_connections_logger():
    import asyncio
    from types import SimpleNamespace

    conn = _sdk_connection()

    async def _start(options):
        return True

    conn.start = _start
    stt = DeepgramSTT(api_key="k")
    stt._client = SimpleNamespace(listen=SimpleNamespace(
        asyncwebsocket=SimpleNamespace(v=lambda _version: conn)))
    asyncio.run(stt.start("en"))
    assert conn._logger.handlers == []


def test_to_deepgram_lang_auto_is_the_code_switching_stream():
    from audio.providers.stt.deepgram import _to_deepgram_lang
    assert _to_deepgram_lang("auto") == "multi"


def test_streams_language_follows_the_identification_set():
    stt = DeepgramSTT(api_key="x")
    assert stt.streams_language("el") and stt.streams_language("en-US") and stt.streams_language("de")
    assert not stt.streams_language("xx") and not stt.streams_language("")


async def test_identify_language_reads_the_batch_answer(monkeypatch):
    from audio.providers.stt.base import TranscriptResult
    stt = DeepgramSTT(api_key="x")
    seen = {}

    async def fake_transcribe(audio, *, language=None):
        seen["language"] = language
        return TranscriptResult(text="Καλημέρα σας", language="el", audio_seconds=1.4,
                                provider_used="deepgram", language_confidence=0.93)

    monkeypatch.setattr(stt, "transcribe_file", fake_transcribe)
    guess = await stt.identify_language(b"RIFF")
    assert seen["language"] is None                 # no language given: the engine names it
    assert (guess.language, guess.confidence, guess.text) == ("el", 0.93, "Καλημέρα σας")


async def test_identify_language_without_an_answer_is_none(monkeypatch):
    from audio.providers.stt.base import TranscriptResult

    async def fake_transcribe(audio, *, language=None):
        return TranscriptResult(text="", language="", audio_seconds=0.0)

    stt = DeepgramSTT(api_key="x")
    monkeypatch.setattr(stt, "transcribe_file", fake_transcribe)
    assert await stt.identify_language(b"RIFF") is None


class _OldConnection:
    """The connection a restart replaces: its finish is awaited off the
    caller's path, after the new stream is open."""

    def __init__(self, calls):
        self.calls = calls

    async def finish(self):
        self.calls.append("finish-old")


async def test_restart_opens_the_new_stream_before_the_old_one_closes(monkeypatch):
    stt = DeepgramSTT(api_key="x")
    stt._active_rate = 8000
    stt._interim_results = True
    stt._endpointing_override = 350
    stt._is_open = True
    calls = []
    stt._connection = _OldConnection(calls)

    async def fake_start(*, language, sample_rate=None, interim_results=False, endpointing_ms=None):
        calls.append(("start", language, sample_rate, interim_results, endpointing_ms))

    monkeypatch.setattr(stt, "start", fake_start)
    assert await stt.restart("el") is True
    assert calls == [("start", "el", 8000, True, 350)]      # the old one is still open here
    await asyncio.sleep(0)
    assert calls[-1] == "finish-old"                          # closed in the background
    assert stt._is_open is True


async def test_restart_reports_a_failed_start_and_closes_the_old_stream(monkeypatch):
    stt = DeepgramSTT(api_key="x")
    stt._is_open = True
    calls = []
    stt._connection = _OldConnection(calls)

    async def fake_start(**kw):
        raise RuntimeError("socket refused")

    monkeypatch.setattr(stt, "start", fake_start)
    assert await stt.restart("el") is False
    assert stt._is_open is False
    await asyncio.sleep(0)
    assert calls == ["finish-old"]


async def test_a_call_mode_final_clears_the_partial_it_covers():
    """Outside dictation mode a final ends its partial's life: the call
    pipeline's drains fall back to ``latest_interim`` when the queue is
    empty, and a partial left here was appended once per drain (every call
    dispatched its words two or three times)."""
    stt = DeepgramSTT(api_key="k")
    await stt._on_transcript(None, _res("καλησπέρα", False, words=_timed("καλησπέρα", 0.2)))
    assert stt.latest_interim == "καλησπέρα"
    await stt._on_transcript(None, _res("Καλησπέρα μας και", True, start=0.0, end=1.4))
    assert stt.latest_interim == ""
    assert stt.drain_transcript() == "Καλησπέρα μας και"
    assert stt.latest_interim == ""          # nothing for a second drain to repeat


async def test_a_transcript_from_a_replaced_connection_is_ignored():
    """The Transcript handler is generation-bound like Error and Close: a
    late result of the stream a restart replaced never lands in the new
    stream's queue (the dictation restart replays audio the old stream was
    still transcribing)."""
    stt = DeepgramSTT(api_key="k")
    stt._connection_gen = 2
    await stt._on_transcript(None, _res("old stream", True, start=0.0, end=1.0), _gen=1)
    assert stt.drain_transcript() is None
    await stt._on_transcript(None, _res("new stream", True, start=0.0, end=1.0), _gen=2)
    assert stt.drain_transcript() == "new stream"
