"""Registry contract: discovery maps, lazy resolution, stub behavior, from_row routing."""

import pytest

from audio.providers import registry


def test_known_maps_populated():
    assert {"deepgram", "elevenlabs", "canary"} <= set(registry.KNOWN_STT_PROVIDERS)
    assert {"cartesia", "elevenlabs", "chatterbox"} <= set(registry.KNOWN_TTS_PROVIDERS)


def test_lazy_resolution_returns_classes():
    assert registry.get_provider_class("tts", "cartesia").__name__ == "CartesiaTTS"
    assert registry.get_provider_class("stt", "deepgram").__name__ == "DeepgramSTT"


def test_unknown_provider_raises_keyerror():
    with pytest.raises(KeyError):
        registry.get_provider_class("stt", "does-not-exist")


def test_build_provider_routes_through_from_row():
    row = {
        "provider_type": "tts",
        "provider_name": "cartesia",
        "credential_key": "audio-cartesia",
        "voices": {"en": "voice-en", "el": "voice-el"},
        "advanced": {},
    }
    tts = registry.build_provider(row, lambda key: "fake-key" if key == "audio-cartesia" else "")
    assert tts.__class__.__name__ == "CartesiaTTS"
    assert tts.voices == {"en": "voice-en", "el": "voice-el"}


def test_cloud_providers_not_cached():
    # is_local=False → always a fresh instance (per-connection state).
    row = {"provider_type": "stt", "provider_name": "deepgram", "credential_key": "k", "advanced": {}}
    resolver = lambda key: "key"
    a = registry.get_or_build_provider(row, resolver)
    b = registry.get_or_build_provider(row, resolver)
    assert a is not b


def test_stub_classes_importable_but_raise_on_init():
    canary_cls = registry.get_provider_class("stt", "canary")
    with pytest.raises(NotImplementedError):
        canary_cls()
    with pytest.raises(NotImplementedError):
        registry.get_provider_class("tts", "chatterbox")()


def test_stub_flag_marks_unimplemented_providers():
    # is_stub drives the admin add-menu: stubs hidden, implemented offered.
    assert registry.get_provider_class("stt", "canary").is_stub is True
    assert registry.get_provider_class("tts", "chatterbox").is_stub is True
    assert registry.get_provider_class("stt", "deepgram").is_stub is False
    assert registry.get_provider_class("tts", "cartesia").is_stub is False
    assert registry.get_provider_class("tts", "elevenlabs").is_stub is False
    assert registry.get_provider_class("stt", "elevenlabs").is_stub is False


def test_a_retired_model_id_resolves_to_its_chain_end_at_build_time():
    from audio.providers.model_succession import DEFAULT_MODELS, FORMER_DEFAULTS, MODEL_SUCCESSORS, successor_of
    assert successor_of({"a": "b", "b": "c"}, "a") == "c"
    assert successor_of({"a": "b", "b": "a"}, "a") in ("a", "b")       # cycle-safe
    assert successor_of({}, "x") == "x" and successor_of({}, "") == ""
    cartesia = registry.get_provider_class("tts", "cartesia")
    assert cartesia.live_model_id("sonic-turbo") == DEFAULT_MODELS[("tts", "cartesia")]["model_id"]
    assert cartesia.live_model_id("sonic-3.5") == "sonic-3.5"            # a former default, not retired
    assert cartesia.live_model_id(None) == ""
    tts = registry.build_provider(
        {"provider_type": "tts", "provider_name": "cartesia", "credential_key": "k",
         "voices": {}, "advanced": {"model_id": "sonic-2"}},
        lambda key: "fake-key",
    )
    assert tts._model_id == "sonic-3.6"
    # Every provider names its defaults as placeholders and stores none of them.
    for kind, defaults in DEFAULT_MODELS.items():
        cls = registry.get_provider_class(*kind)
        assert cls.default_model_ids() == defaults
        assert not set(cls.default_advanced_settings()) & {"model_id", "batch_model_id"}
    assert set(FORMER_DEFAULTS) <= set(DEFAULT_MODELS) and set(MODEL_SUCCESSORS) <= set(DEFAULT_MODELS)


def test_elevenlabs_builds_from_row():
    tts = registry.build_provider(
        {"provider_type": "tts", "provider_name": "elevenlabs",
         "credential_key": "audio-elevenlabs", "voices": {"en": "v-en"}, "advanced": {}},
        lambda key: "fake-key",
    )
    assert tts.__class__.__name__ == "ElevenLabsTTS"
    assert tts.voices == {"en": "v-en"}
    stt = registry.build_provider(
        {"provider_type": "stt", "provider_name": "elevenlabs",
         "credential_key": "audio-elevenlabs", "advanced": {}},
        lambda key: "fake-key",
    )
    assert stt.__class__.__name__ == "ElevenLabsSTT"
