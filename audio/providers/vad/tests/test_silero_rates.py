"""SileroVad rate parameterization — model mocked, runs on lean installs too.

The sibling module needs the real ``silero_vad_lite`` (localmodels extra) and
is skipped on the proxy venv; these tests pin the wrapper's rate contract
everywhere: the analysis window is derived from the model at the instance's
rate, and every internal reset re-creates the model at that SAME rate — a
hardcoded-8k reset would silently break a 16 kHz duplex session mid-stream.
"""

import sys
import types

import pytest

from audio.constants import SAMPLE_WIDTH


class _FakeModel:
    instances: list = []

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.window_size_samples = int(sample_rate * 0.032)
        self.processed: list = []
        _FakeModel.instances.append(self)

    def process(self, float32_audio):
        self.processed.append(len(float32_audio))
        return 0.0


@pytest.fixture()
def vad_cls(monkeypatch):
    fake_mod = types.ModuleType("silero_vad_lite")
    fake_mod.SileroVAD = _FakeModel
    monkeypatch.setitem(sys.modules, "silero_vad_lite", fake_mod)
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


def test_resets_recreate_model_at_instance_rate(vad_cls):
    vad = _make(vad_cls, sample_rate=16000)
    vad.set_bargein_mode(True)
    vad.reset()
    assert {m.sample_rate for m in _FakeModel.instances} == {16000}
    assert len(_FakeModel.instances) == 3  # init + bargein toggle + reset
