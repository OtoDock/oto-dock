"""The vendored identifier runs offline and names the six supported
languages; the real Greek sample comes out Greek."""

import hashlib
import os
import wave

import numpy as np
import pytest

pytest.importorskip("onnxruntime")

from audio.providers.lid import ecapa

SAMPLE = os.path.join(os.path.dirname(__file__), "fixtures", "greek-8k.wav")


def test_the_model_file_ships_with_its_labels():
    assert os.path.isfile(ecapa.default_model_path())
    assert ecapa.identifier_available()
    model = ecapa.load_model(num_threads=1)
    for code in ("en", "el", "de", "es", "fr", "it"):
        assert code in model.labels
    assert len(model.labels) == 107


def test_the_greek_sample_is_greek_at_one_two_and_three_seconds():
    with wave.open(SAMPLE, "rb") as w:
        assert w.getframerate() == 8000
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    from audio.providers.lid.tracker import Upsampler2x
    x = Upsampler2x().process(pcm)
    ident = ecapa.EcapaIdentifier(num_threads=1)
    for seconds in (1.0, 2.0, 3.0):
        scores = ident.scores(x[: int(16000 * seconds)])
        assert ident.labels[int(scores.argmax())] == "el"


def test_a_missing_file_is_reported_once_whoever_loads_it(caplog, tmp_path):
    missing = str(tmp_path / "nothing.onnx")
    with pytest.raises(Exception):  # noqa: B017 — the runtime's own error for a missing file
        ecapa.load_model(missing)
    assert ecapa.identifier_available(missing) is False
    assert ecapa.prewarm_language_identifier(missing) is False
    assert sum("unavailable" in r.message for r in caplog.records) == 1


def test_the_model_file_is_the_one_the_thresholds_were_set_against():
    """VERSIONS.md calls the file locked: the rule's thresholds hold for
    these exact weights and this quantization, so a swap is a tuning change
    that re-runs the benchmark, never a silent replacement."""
    with open(ecapa.default_model_path(), "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    assert digest == "5a9d5daa8a7aba4d1aec92db603377db0443f6869ce8a6cbc339252690190c9d"


def test_an_input_shorter_than_the_graphs_padding_is_padded_not_raised_on():
    ident = ecapa.EcapaIdentifier(num_threads=1)
    for n in (0, 100, 4000):
        scores = ident.scores(np.zeros(n, dtype=np.float32))
        assert scores.shape == (107,)


def test_loaded_model_never_loads():
    ecapa._models.clear()
    assert ecapa.loaded_model(num_threads=1) is None
    model = ecapa.load_model(num_threads=1)
    assert ecapa.loaded_model(num_threads=1) is model
    assert ecapa.loaded_model(num_threads=2) is None
    assert ecapa.EcapaIdentifier(model=model).labels == model.labels
