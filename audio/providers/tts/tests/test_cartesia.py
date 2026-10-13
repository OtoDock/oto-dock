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
    # A language the admin configured no voice for speaks the shipped native
    # voice for it, ahead of the admin's English voice (an install seeded
    # with en and el then speaks German natively).
    assert tts.select_voice("de") == CartesiaTTS.default_voices["de"]
    # A language nobody ships: the admin's English voice (a multilingual
    # voice still pronounces the text), then the shipped English one over a
    # transient current voice_id (deterministic regardless of prior traffic).
    assert tts.select_voice("pt") == "v-en"
    tts2 = CartesiaTTS(api_key="x", voice_id="v-init", voices={"el": "v-el"})
    assert tts2.select_voice("pt") == CartesiaTTS.default_voices["en"]


def test_select_voice_falls_back_to_shipped_defaults():
    # No voices configured at all -> the shipped public-library defaults keep
    # every language speaking out of the box, each natively.
    tts = CartesiaTTS(api_key="x")
    for lang in ("en", "el", "de", "es", "fr", "it"):
        assert tts.select_voice(lang) == CartesiaTTS.default_voices[lang]
    assert tts.select_voice("pt") == CartesiaTTS.default_voices["en"]
    # The admin's voice for a language always wins over the shipped one.
    tts2 = CartesiaTTS(api_key="x", voices={"el": "v-el"})
    assert tts2.select_voice("el") == "v-el"


def test_a_retired_model_id_builds_its_successor():
    tts = CartesiaTTS(api_key="x", advanced={"model_id": "sonic-turbo"})
    assert tts._model_id == "sonic-3.6"
    assert CartesiaTTS(api_key="x", advanced={"model_id": "sonic-3.5"})._model_id == "sonic-3.5"
    assert CartesiaTTS(api_key="x")._model_id == "sonic-3.6"


def test_has_voice_for_names_a_real_voice_not_the_fallback():
    # The language lock asks this before it moves a call: a configured voice
    # or a shipped default counts; the English fallback select_voice takes
    # does not.
    tts = CartesiaTTS(api_key="x", voices={"en": "v-en", "el": "v-el"})
    assert tts.has_voice_for("el") and tts.has_voice_for("en")
    assert tts.has_voice_for("de") and tts.has_voice_for("it")
    assert not tts.has_voice_for("xx")


def test_a_pinned_voice_wins_for_every_language_and_unpins():
    # A phone route's own voice: the maps are not consulted while it is
    # pinned; has_voice_for still answers for the languages the line speaks.
    tts = CartesiaTTS(api_key="x", voices={"en": "v-en", "el": "v-el"})
    tts.pin_voice(" v-route ")
    assert tts.voice_id == "v-route"
    assert tts.select_voice("el") == "v-route" and tts.select_voice("de") == "v-route"
    assert tts.has_voice_for("el") and not tts.has_voice_for("xx")
    tts.pin_voice("")
    assert tts.select_voice("el") == "v-el"


def test_repr_redacts_api_key():
    tts = CartesiaTTS(api_key="super-secret-token", voice_id="v-en")
    r = repr(tts)
    assert "super-secret-token" not in r
    assert "v-en" in r
