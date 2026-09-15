"""SileroVad rate parameterization — model mocked, runs on lean installs too.

The sibling modules need the real onnxruntime (localmodels extra) and are
skipped on the proxy venv; these tests pin the wrapper's rate contract
everywhere: the analysis window is derived from the model at the instance's
rate, one model serves the instance for its whole life, and every internal
reset zeroes that model's state instead of building a new one (a
hardcoded-8k rebuild would silently break a 16 kHz duplex session
mid-stream, and a session rebuild per reset was the old binding's cost).
"""

import pytest

from audio.constants import SAMPLE_WIDTH


class _FakeModel:
    instances: list = []

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.window_size_samples = int(sample_rate * 0.032)
        self.processed: list = []
        self.resets = 0
        _FakeModel.instances.append(self)

    def process(self, float32_audio):
        self.processed.append(len(float32_audio))
        return 0.0

    def reset(self):
        self.resets += 1


@pytest.fixture()
def vad_cls(monkeypatch):
    # Patch the module that reads the name, never the facade.
    monkeypatch.setattr("audio.providers.vad.silero.SileroModel", _FakeModel)
    _FakeModel.instances = []
    from audio.providers.vad.silero import SileroVad
    return SileroVad


def _make(vad_cls, **kw):
    kwargs = dict(
        threshold=0.4, silence_duration_ms=550, speech_pad_ms=64,
        min_energy_rms=150, bargein_threshold=0.35, bargein_debounce_ms=300,
        bargein_chunk_ratio=0.5, bargein_silence_duration_ms=500,
    )
    kwargs.update(kw)
    return vad_cls(**kwargs)


def test_default_rate_is_telephony_window(vad_cls):
    vad = _make(vad_cls)
    assert _FakeModel.instances[-1].sample_rate == 8000
    assert vad._chunk_bytes == 256 * SAMPLE_WIDTH


def test_16k_rate_derives_wider_window(vad_cls):
    vad = _make(vad_cls, sample_rate=16000)
    assert _FakeModel.instances[-1].sample_rate == 16000
    assert vad._chunk_bytes == 512 * SAMPLE_WIDTH


def test_process_consumes_window_sized_chunks_at_16k(vad_cls):
    vad = _make(vad_cls, sample_rate=16000)
    model = _FakeModel.instances[-1]
    # Two full windows + a remainder: exactly two inferences, remainder buffered.
    vad.process(b"\x00\x00" * (512 * 2 + 100))
    assert model.processed == [512, 512]
    assert len(vad._buffer) == 100 * SAMPLE_WIDTH


def test_resets_zero_the_one_model_at_instance_rate(vad_cls):
    vad = _make(vad_cls, sample_rate=16000)
    vad.set_bargein_mode(True)
    vad.reset()
    assert len(_FakeModel.instances) == 1
    model = _FakeModel.instances[0]
    assert model.sample_rate == 16000
    assert model.resets == 2  # bargein toggle + reset; construction is not a reset
    assert vad._model is model
