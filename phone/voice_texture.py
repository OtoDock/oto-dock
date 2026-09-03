"""Voice texture: glue the agent's voice into the ambience scene.

A clean studio-grade TTS voice floating over a textured background bed reads
as pasted-on. The classic mixing fix isn't more noise (the bed already
supplies the floor) — it's giving the voice the same ROOM as the scene:

- **Grain** — gentle soft-clip saturation (``tanh(x·drive)/drive``,
  small-signal unity so loudness is untouched) adds the harmonic dirt of a
  real phone mic.
- **Early reflections** — two feed-forward echo taps (~23ms / ~41ms at low
  gain) put the voice in a room instead of an anechoic void.

Applied per outgoing voice frame (TTS, fillers, breath — everything that
rides ``_out_frame``) BEFORE the bed is mixed under, and only when the
route has ambience enabled (blending targets scene-matching). One knob:
``voice_texture`` (0–1) scales both effects; 0 disables.

The echo history is a small ring buffer carried across frames; a >100ms
send gap zeroes it so a stale tail from a previous utterance can't leak
into the next one.
"""

import time

import numpy as np

import config


class VoiceTexture:
    """Per-call stateful voice processor (saturation + early reflections)."""

    def __init__(self, amount: float):
        amount = max(0.0, min(1.0, amount))
        self._drive = 1.0 + 1.5 * amount
        self._taps = [
            (int(0.023 * config.SAMPLE_RATE), 0.18 * amount),
            (int(0.041 * config.SAMPLE_RATE), 0.11 * amount),
        ]
        self._hist_n = max(d for d, _ in self._taps)
        self._hist = np.zeros(self._hist_n, dtype=np.float32)
        self._last_t = 0.0

    def process(self, frame: bytes) -> bytes:
        """Texture one voice frame (any whole-sample length)."""
        now = time.monotonic()
        if now - self._last_t > 0.1:
            self._hist[:] = 0.0  # new utterance — drop the stale echo tail
        self._last_t = now

        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        # Grain: soft-clip with small-signal unity (tanh(x·d)/d ≈ x for
        # small x), so only peaks pick up harmonics — no loudness shift.
        y = np.tanh(x * self._drive) / self._drive

        # Early reflections read the pre-echo signal (feed-forward combs)
        ext = np.concatenate([self._hist, y])
        out = y.copy()
        for delay, gain in self._taps:
            out += gain * ext[self._hist_n - delay:self._hist_n - delay + len(y)]
        self._hist = ext[-self._hist_n:]

        return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
