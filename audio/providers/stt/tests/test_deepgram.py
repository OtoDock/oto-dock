"""DeepgramSTT contract tests (no network — construction + pure surface only)."""

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


# ── Dictation mode (chat-input relay; the Greek vanishing-words bug) ─────
#
# The composer paints interims as committed text. Deepgram on non-English
# languages sometimes closes a window with an EMPTY final (orphaning the
# painted interim — the base never advances and the next window's interim
# replaces everything shown) or a final covering only the head of the shown
# interim. Dictation mode promotes what the user saw; default mode keeps
# call/duplex semantics byte-for-byte (a promoted noise interim would be
# dispatched turn text / false barge-in evidence there).


def _res(text: str, final: bool):
    class _Alt:
        transcript = text

    class _Chan:
        alternatives = [_Alt()]

    class _Res:
        channel = _Chan()
        is_final = final

    return _Res()


async def test_dictation_empty_final_promotes_shown_interim():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("πήγα στο σπίτι μου σήμερα", False))
    assert stt.pop_interim() == "πήγα στο σπίτι μου σήμερα"
    await stt._on_transcript(None, _res("", True))  # empty final
    assert stt.drain_transcript() == "πήγα στο σπίτι μου σήμερα"
    assert stt.latest_interim == ""


async def test_default_mode_empty_final_still_drops_interim():
    stt = DeepgramSTT(api_key="k")
    await stt._on_transcript(None, _res("background noise guess", False))
    await stt._on_transcript(None, _res("", True))
    assert stt.drain_transcript() is None  # call/duplex semantics unchanged


async def test_dictation_short_final_commits_shown_interim():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("πήγα στο σπίτι μου σήμερα", False))
    stt.pop_interim()
    # Final covers only the head (smart_format casing/punctuation differs).
    await stt._on_transcript(None, _res("Πήγα στο σπίτι.", True))
    assert stt.drain_transcript() == "πήγα στο σπίτι μου σήμερα"


async def test_dictation_revised_final_wins_when_not_an_extension():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("να πάμε τώρα εκεί", False))
    stt.pop_interim()
    await stt._on_transcript(None, _res("Θα πάμε τώρα.", True))  # reword
    assert stt.drain_transcript() == "Θα πάμε τώρα."


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
    await stt._on_transcript(None, _res("ένα δύο τρία τέσσερα", False))
    stt.pop_interim()  # painted
    await stt._on_transcript(None, _res("ένα δύο", False))  # backtrack (suppressed)
    assert stt.pop_interim() is None
    await stt._on_transcript(None, _res("Ένα δύο.", True))  # final = head only
    assert stt.drain_transcript() == "ένα δύο τρία τέσσερα"


async def test_dictation_finish_flushes_leftover_interim():
    stt = DeepgramSTT(api_key="k")
    stt.enable_dictation_mode()
    await stt._on_transcript(None, _res("τελευταία λέξη", False))
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
    await stt._on_transcript(None, _res("κάτι", False))
    await stt._on_transcript(None, _res("", True))
    assert got == []
    assert stt.drain_transcript() == "κάτι"


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
