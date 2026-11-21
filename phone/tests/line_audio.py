"""Audio as a phone line carries it, for the detector's tests and its bench.

A line passes 300 to 3400 Hz and codes each sample in G.711. The detector
scores speech from a clean recording far more easily than the same speech on
a line, so a test of what callers meet shapes its audio here first. Samples
are float arrays at the 16-bit scale until ``pcm16`` makes bytes of them.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from telephony.g711 import mulaw_to_pcm16, pcm16_to_mulaw

RATE = 8000


def _lowpass(cut_hz: float, rate: int, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cut_hz / rate * n) * 2 * cut_hz / rate * np.blackman(taps)
    return h / h.sum()


def _highpass(cut_hz: float, rate: int, taps: int) -> np.ndarray:
    h = -_lowpass(cut_hz, rate, taps)
    h[taps // 2] += 1.0
    return h


def read_wav(path: str | Path) -> np.ndarray:
    """A 16-bit WAV as samples at 8 kHz: mixed to one channel, and brought
    down from a multiple of 8 kHz behind a low-pass."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2 or rate % RATE:
        raise ValueError(f"{path}: 16-bit PCM at 8000 Hz or a multiple of it")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate == RATE:
        return x
    return np.convolve(x, _lowpass(3400, rate, 127), mode="same")[:: rate // RATE]


def at_level(x: np.ndarray, rms: float) -> np.ndarray:
    """``x`` at an overall level of ``rms``: a room, a noise."""
    return x * (rms / float(np.sqrt(np.mean(x ** 2))))


def at_voiced_level(x: np.ndarray, rms: float) -> np.ndarray:
    """``x`` with its voiced parts at ``rms``: a speaker. The pauses between
    words do not count, so a slow speaker is not made louder."""
    voiced = x[np.abs(x) > np.abs(x).max() * 0.05]
    return x * (rms / float(np.sqrt(np.mean(voiced ** 2))))


def pcm16(x: np.ndarray) -> bytes:
    return np.clip(np.round(x), -32768, 32767).astype("<i2").tobytes()


def line(x: np.ndarray) -> np.ndarray:
    """``x`` as a line carries it: the band 300 to 3400 Hz, then the phone
    service's own G.711 coder there and back."""
    band = np.convolve(np.convolve(x, _highpass(300, RATE, 255), mode="same"), _lowpass(3400, RATE, 127), mode="same")
    coded = mulaw_to_pcm16(pcm16_to_mulaw(pcm16(band)))
    return np.frombuffer(coded, dtype="<i2").astype(np.float64)
