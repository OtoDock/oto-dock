"""Smart Turn v3 must run fully OFFLINE.

Its Whisper mel front-end loads the **vendored** ``preprocessor_config.json``
from disk (``audio/models/whisper_feature_extractor/``), never the HuggingFace
Hub. These guard the 1b-0b vendoring: if the asset stops shipping (package-data
drift) or the loader regresses to the Hub id, they fail here instead of silently
re-introducing a first-phone-call network dependency.

Skipped where the on-box model deps aren't installed (e.g. the lean proxy venv).
"""

import os

import numpy as np
import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("transformers")

from audio.providers.turn import smart_turn


def test_vendored_feature_extractor_ships():
    cfg = os.path.join(smart_turn.default_feature_extractor_dir(), "preprocessor_config.json")
    assert os.path.isfile(cfg), f"vendored Whisper config missing at {cfg}"


async def test_smart_turn_offline_uses_vendored_config(monkeypatch, tmp_path):
    # Point HF at an EMPTY cache and force offline. A regression to the Hub id
    # ("openai/whisper-small") would fail to load here; the vendored local dir
    # ignores HF entirely and succeeds — proving the classifier is offline.
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")

    clf = smart_turn.SmartTurnClassifier(
        model_path=smart_turn.default_model_path(),
        threshold=0.5, num_threads=1, audio_window_s=8.0,
    )
    # End-to-end: mel extraction (vendored config) → ONNX inference.
    res = await clf.classify_audio(np.zeros(16000, dtype=np.float32))
    assert isinstance(res, bool)


def test_one_loaded_model_per_key_shared_by_every_classifier():
    """The session and the extractor load once per (path, threads) and every
    classifier on that key wraps the same object: the daemon warms it at
    start, and a call pays no load (the first call after a daemon start
    lost two seconds of audio to it)."""
    model = smart_turn.load_model(None, 1)
    assert smart_turn.load_model(smart_turn.default_model_path(), 1) is model
    smart_turn.prewarm_smart_turn(None, 1)
    assert smart_turn.load_model(None, 1) is model
    a = smart_turn.SmartTurnClassifier(
        model_path=smart_turn.default_model_path(), threshold=0.5, num_threads=1, audio_window_s=8.0)
    b = smart_turn.SmartTurnClassifier(
        model_path=smart_turn.default_model_path(), threshold=0.9, num_threads=1, audio_window_s=4.0)
    assert a._model is model and b._model is model
    assert a._threshold == 0.5 and b._threshold == 0.9
