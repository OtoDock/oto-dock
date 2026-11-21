"""SileroModel — the ONNX session behind SileroVad, on the bundled model.

Pins the model's input. One exact 32 ms window per call, the state carried
between calls and zeroed by ``reset()``, both supported rates. Every window
reaches the session with the 4 ms before it in front, the model's contract.
No caller can ask for the window alone. Skipped on lean installs without the
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


# -- the model's input: one builder, always with the context --------------------

class _Recorder:
    """In place of the session: keeps every input it is handed, as handed
    (``raw``) and as it read at that moment (``inputs``)."""

    def __init__(self):
        self.raw: list = []
        self.inputs: list = []

    def run(self, _outputs, feeds):
        import numpy as np

        self.raw.append(feeds["input"])
        self.inputs.append(feeds["input"].copy())
        return np.zeros((1, 1), dtype=np.float32), feeds["state"]


def _ramp(n: int, k: int):
    """A window whose every sample differs, from its head to its tail and
    from the windows before it."""
    import numpy as np

    return (np.arange(1, n + 1, dtype=np.float32) + 1000 * k) / 8192


class _WindowAlone(SileroModel):
    """The feed of an older binding, built here only: the window with nothing
    in front. No code of the product can build it. The tests that keep their
    proof of what the context buys feed the model through this."""

    def _input(self, x):
        return x.reshape(1, -1)


@pytest.mark.parametrize("rate", [8000, 16000])
def test_no_caller_can_ask_for_the_window_alone(rate):
    """The model takes its rate and its file, and no word about its feed."""
    with pytest.raises(TypeError):
        SileroModel(rate, context=False)
    with pytest.raises(TypeError):
        SileroModel(rate, context=True)
    with pytest.raises(TypeError):
        SileroModel(rate, None, False)


@pytest.mark.parametrize("rate, window, context", [(8000, 256, 32), (16000, 512, 64)])
def test_the_context_is_the_audio_just_before_each_window(rate, window, context):
    """The model's contract, and what every model gets: 4 ms in front of
    every window, the last samples of the window before it, silence at the
    start and after a reset."""
    model = SileroModel(rate)
    assert model.context_samples == context
    recorder = model._session = _Recorder()
    a, b, c, d = (_ramp(window, k) for k in range(4))
    model.process(a)
    model.process(b)
    model.process(c)
    model.reset()
    model.process(d)
    first, second, third, after_reset = recorder.inputs
    for read, sent in ((first, a), (second, b), (third, c), (after_reset, d)):
        assert read.shape == (1, context + window)
        assert read[0, context:].tobytes() == sent.tobytes()
    assert not first[0, :context].any()
    assert second[0, :context].tobytes() == a[-context:].tobytes()
    assert third[0, :context].tobytes() == b[-context:].tobytes()
    assert not after_reset[0, :context].any()


def test_the_count_of_context_samples_cannot_be_written():
    """The count is the rate's 4 ms and nothing a caller sets: writing it
    is refused, and the feed stays."""
    model = SileroModel(8000)
    with pytest.raises(AttributeError):
        model.context_samples = 0
    assert model.context_samples == 32
    recorder = model._session = _Recorder()
    model.process(_ramp(256, 0))
    assert recorder.inputs[0].shape == (1, 32 + 256)


@pytest.mark.parametrize("kind", ["ndarray", "bytearray", "array"])
def test_the_kept_context_is_the_models_own(kind):
    """A caller may reuse its buffer for the next window. The context kept
    from the last one must not change with it."""
    import numpy as np

    values = _ramp(256, 0)
    if kind == "ndarray":
        buffer = values.copy()
    elif kind == "bytearray":
        buffer = bytearray(values.tobytes())
    else:
        buffer = array.array("f", values.tolist())
    model = SileroModel(8000)
    recorder = model._session = _Recorder()
    model.process(buffer)
    if kind == "bytearray":
        buffer[:] = bytes(len(buffer))
    else:
        for i in range(len(buffer)):
            buffer[i] = 0.0
    model.process(buffer)
    assert recorder.inputs[1][0, :32].tobytes() == values[-32:].tobytes()
    assert not np.asarray(recorder.inputs[1][0, 32:]).any()


#: The speech fixture's first eight windows through silero-vad-lite 0.3.0, the
#: binding release that began to supply the context (the same model file),
#: taken on x86-64.
_WITH_CONTEXT = (0.0055, 0.0031, 0.9050, 0.9659, 0.9801, 0.9839, 0.9794, 0.9859)


def test_with_the_context_the_real_model_answers_as_the_contracts_feed_does():
    """The real session takes the longer input and scores speech as the
    binding with the context does. The window alone, built in this test
    only, scores the same audio far lower: the two feeds are told apart by
    tenths, the tolerance is for another CPU."""
    import struct
    import wave

    sample = os.path.join(os.path.dirname(__file__), "..", "..", "lid", "tests", "fixtures", "greek-8k.wav")
    with wave.open(sample, "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
        pcm = w.readframes(256 * 8)
    windows = [array.array("f", (v / 32768.0 for v in struct.unpack("<256h", pcm[i:i + 512])))
               for i in range(0, len(pcm), 512)]
    with_context, alone = SileroModel(8000), _WindowAlone(8000)
    assert [with_context.process(w) for w in windows] == pytest.approx(_WITH_CONTEXT, abs=1e-3)
    scores = [alone.process(w) for w in windows]
    assert max(abs(a - b) for a, b in zip(scores, _WITH_CONTEXT, strict=True)) > 0.5
