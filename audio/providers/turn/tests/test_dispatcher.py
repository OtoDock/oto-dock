"""Turn dispatcher + factory tests (no model load when both backends are off)."""

import os

from audio.providers.turn.dispatcher import TurnClassifierDispatcher, build_dispatcher
from audio.providers.turn.smart_turn import default_model_path


def test_bundled_model_present():
    assert os.path.isfile(default_model_path())


def _build(**overrides):
    kwargs = dict(
        smart_turn_enabled=False,
        smart_turn_threshold=0.5,
        smart_turn_onnx_threads=1,
        smart_turn_audio_window_s=8.0,
        groq_api_key="",
        lang_map={},
        default_backend="smart_turn",
    )
    kwargs.update(overrides)
    return build_dispatcher(**kwargs)


def test_build_dispatcher_returns_none_when_no_backend():
    # Neither backend enabled → None, and crucially no ONNX/Whisper load happens.
    assert _build() is None


def test_build_dispatcher_groq_uses_default_base_url():
    # A BYO key (no base_url) → the classifier talks to Groq directly.
    disp = _build(groq_api_key="byo-key")
    assert disp is not None
    from audio.providers.turn.groq import GroqTurnClassifier
    assert disp._groq is not None
    assert disp._groq._base_url == GroqTurnClassifier.DEFAULT_BASE_URL


def test_build_dispatcher_groq_honors_hosted_base_url():
    # Hosted relay: a minted token + the relay base_url are threaded through to
    # the GroqTurnClassifier (so its POST /chat/completions hits the relay).
    relay = "https://relay.example.io/v1/relay/groq/v1"
    disp = _build(groq_api_key="minted-token", groq_base_url=relay)
    assert disp is not None
    assert disp._groq is not None
    assert disp._groq._base_url == relay


def test_backend_routing():
    disp = TurnClassifierDispatcher(
        smart_turn=None, groq=None,
        lang_map={"el": "groq", "en": "smart_turn"},
        default_backend="smart_turn",
    )
    assert disp.backend_for_language("el") == "groq"
    assert disp.backend_for_language("en") == "smart_turn"
    assert disp.backend_for_language("de") == "smart_turn"  # default
    assert disp.needs_audio("en") is False  # no smart_turn instance loaded
