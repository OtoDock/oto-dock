"""Pre-response breath: a subtle inhale before the agent starts speaking.

Humans audibly inhale before speaking after a pause — playing one right
before a response's first TTS audio converts "dead air, then voice" into
"presence, then voice". It is deliberately minimal: ONE neutral sample,
played only when the agent hasn't produced voice audio for
``breath_min_gap_s`` (a breath mid rapid exchange reads as uncanny), at a
low gain, mixed over the ambience bed like any other voice audio.

The asset is an 8 kHz / 16-bit / mono WAV (``assets/breath/inhale.wav``,
CC0 — see SOURCES.md there). Missing or malformed → no breath, never a
call failure.
"""

import logging
import os
import wave

import numpy as np

import config

logger = logging.getLogger("breath")

BREATH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "assets", "breath", "inhale.wav")

_pcm_cache: bytes | None = None
_load_failed = False


def load_breath(gain: float) -> bytes | None:
    """The inhale clip with ``gain`` baked in (None when disabled/missing)."""
    global _pcm_cache, _load_failed
    if gain <= 0:
        return None
    if _pcm_cache is None and not _load_failed:
        try:
            with wave.open(BREATH_PATH, "rb") as w:
                if (w.getframerate() != config.SAMPLE_RATE
                        or w.getnchannels() != config.CHANNELS
                        or w.getsampwidth() != config.SAMPLE_WIDTH):
                    raise ValueError(
                        f"not {config.SAMPLE_RATE}Hz/{config.CHANNELS}ch/"
                        f"{config.SAMPLE_WIDTH * 8}bit"
                    )
                _pcm_cache = w.readframes(w.getnframes())
            logger.info(
                f"Breath clip loaded: "
                f"{len(_pcm_cache) / (config.SAMPLE_RATE * config.SAMPLE_WIDTH):.2f}s"
            )
        except FileNotFoundError:
            _load_failed = True
            logger.warning(f"Breath clip missing ({BREATH_PATH})")
        except Exception as e:
            _load_failed = True
            logger.warning(f"Breath clip failed to load: {e}")
    if _pcm_cache is None:
        return None
    samples = np.frombuffer(_pcm_cache, dtype=np.int16).astype(np.float32) * gain
    # Crescendo envelope: a real inhale grows toward its peak right before
    # speech — start soft and rise toward full level near the clip's end
    # (which flows into the response's first TTS audio).
    t = np.linspace(0.0, 1.0, len(samples), dtype=np.float32)
    envelope = 0.35 + 0.65 * t ** 1.4
    # Tail release: without it the crescendo peaks on the very last sample
    # and the clip stops mid-breath (an audible cut). A short fast ramp to
    # zero over the final ~15% (~50ms) lets the inhale land instead.
    release = max(1, int(len(samples) * 0.15))
    envelope[-release:] *= np.linspace(1.0, 0.0, release, dtype=np.float32)
    samples = samples * envelope
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
