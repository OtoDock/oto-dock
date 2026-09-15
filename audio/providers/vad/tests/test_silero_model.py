"""SileroModel — the ONNX session behind SileroVad, on the bundled model.

Pins the feed contract the phone's endpointing was tuned against: one exact
32 ms window per call, the state carried between calls and zeroed by
``reset()``, both supported rates. Skipped on lean installs without the
``localmodels`` extra (e.g. the proxy venv).
"""

import array
import math
import os

import pytest

pytest.importorskip("onnxruntime")

from audio.providers.vad.silero_model import SileroModel, default_model_path


def _tone(n: int, rate: int, hz: float = 220.0, amp: float = 0.3) -> array.array:
    return array.array("f", (amp * math.sin(2 * math.pi * hz * i / rate) for i in range(n)))


def test_bundled_model_is_present():
    assert os.path.isfile(default_model_path())


@pytest.mark.parametrize("rate, window", [(8000, 256), (16000, 512)])
def test_window_is_32ms_at_each_rate(rate, window):
    model = SileroModel(rate)
    assert model.sample_rate == rate
    assert model.window_size_samples == window


def test_rejects_unsupported_rate():
    with pytest.raises(ValueError):
        SileroModel(44100)


def test_rejects_wrong_window_length():
    model = SileroModel(16000)
    with pytest.raises(ValueError):
        model.process(array.array("f", [0.0] * 511))
    with pytest.raises(ValueError):
        model.process(array.array("f", [0.0] * 513))


@pytest.mark.parametrize("rate", [8000, 16000])
def test_silence_is_not_speech(rate):
    model = SileroModel(rate)
    silence = array.array("f", [0.0] * model.window_size_samples)
    for _ in range(10):
        assert model.process(silence) < 0.1


def test_accepts_every_float32_buffer_shape():
    import numpy as np

    n = 512
    tone = _tone(n, 16000)
    a = SileroModel(16000).process(tone)
    b = SileroModel(16000).process(tone.tobytes())
    c = SileroModel(16000).process(np.frombuffer(tone.tobytes(), dtype=np.float32))
    d = SileroModel(16000).process(list(tone))
    assert a == b == c == d


def test_deterministic_across_instances():
    tone = _tone(256, 8000)
    assert SileroModel(8000).process(tone) == SileroModel(8000).process(tone)


def test_state_carries_between_windows_and_reset_clears_it():
    model = SileroModel(16000)
    tone = _tone(512, 16000)
    first = model.process(tone)
    second = model.process(tone)
    assert first != second  # the recurrent state moved
    model.reset()
    assert model.process(tone) == first  # a zeroed state is a fresh sequence


def test_reset_on_a_fresh_model_changes_nothing():
    model = SileroModel(8000)
    tone = _tone(256, 8000)
    fresh = SileroModel(8000).process(tone)
    model.reset()
    assert model.process(tone) == fresh
