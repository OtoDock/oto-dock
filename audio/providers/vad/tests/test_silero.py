"""SileroVad tests — constructs the bundled ONNX model and runs offline.

silero-vad-lite bundles its own model, so this needs no external file or
network. Silence must not trigger SPEECH_START. Skipped entirely on lean
installs without the ``localmodels`` extra (e.g. the proxy venv).
"""

import pytest

pytest.importorskip("silero_vad_lite")

from audio.providers.vad.base import VadEvent, VadState
from audio.providers.vad.silero import SileroVad


def _make_vad() -> SileroVad:
    return SileroVad(
        threshold=0.5,
        silence_duration_ms=800,
        speech_pad_ms=64,
        min_energy_rms=200.0,
        bargein_threshold=0.7,
        bargein_debounce_ms=200,
        bargein_chunk_ratio=0.6,
        bargein_silence_duration_ms=1200,
    )


def test_constructs_and_starts_idle():
    vad = _make_vad()
    assert vad.state == VadState.IDLE


def test_silence_does_not_trigger_speech():
    vad = _make_vad()
    silence = b"\x00" * 320  # one AudioSocket frame of digital silence
    for _ in range(50):  # ~1s of silence
        assert vad.process(silence) in (VadEvent.NONE,)
    assert vad.state == VadState.IDLE


def test_reset_returns_to_idle():
    vad = _make_vad()
    vad.process(b"\x00" * 320)
    vad.reset()
    assert vad.state == VadState.IDLE
