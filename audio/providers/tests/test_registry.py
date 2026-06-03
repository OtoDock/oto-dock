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
