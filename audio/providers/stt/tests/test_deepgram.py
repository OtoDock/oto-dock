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
