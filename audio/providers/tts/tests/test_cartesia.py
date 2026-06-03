"""CartesiaTTS contract tests (no network — construction + pure surface only)."""

from audio.providers.tts.cartesia import CartesiaTTS


def test_capabilities_and_billing():
    assert CartesiaTTS.capabilities.supports_streaming
    assert not CartesiaTTS.capabilities.is_local
    assert CartesiaTTS.billing_unit() == "char"
    assert CartesiaTTS.cost_per_unit() > 0


def test_from_row_loads_voices():
    row = {
        "provider_type": "tts",
        "provider_name": "cartesia",
        "credential_key": "audio-cartesia",
        "voices": {"en": "v-en", "el": "v-el"},
        "advanced": {},
    }
    tts = CartesiaTTS.from_row(row, lambda key: "secret")
    assert tts.voices == {"en": "v-en", "el": "v-el"}


def test_select_voice():
    tts = CartesiaTTS(api_key="x", voices={"en": "v-en", "el": "v-el"})
    assert tts.select_voice("el") == "v-el"
    assert tts.voice_id == "v-el"
    # language without a configured voice falls back to the English voice
    # (a multilingual voice still pronounces the text correctly)
    assert tts.select_voice("de") == "v-en"
    assert tts.voice_id == "v-en"
    # no English voice either -> the shipped default wins over a transient
    # current voice_id (deterministic regardless of prior traffic)
    tts2 = CartesiaTTS(api_key="x", voice_id="v-init", voices={"el": "v-el"})
    assert tts2.select_voice("de") == CartesiaTTS.default_voices["en"]


def test_select_voice_falls_back_to_shipped_defaults():
    # No voices configured at all -> the shipped public-library defaults keep
    # every language speaking out of the box.
    tts = CartesiaTTS(api_key="x")
    assert tts.select_voice("el") == CartesiaTTS.default_voices["el"]
    assert tts.select_voice("de") == CartesiaTTS.default_voices["en"]
    # Admin-configured voices always win over the defaults.
    tts2 = CartesiaTTS(api_key="x", voices={"en": "v-en"})
    assert tts2.select_voice("el") == "v-en"


def test_repr_redacts_api_key():
    tts = CartesiaTTS(api_key="super-secret-token", voice_id="v-en")
    r = repr(tts)
    assert "super-secret-token" not in r
    assert "v-en" in r
