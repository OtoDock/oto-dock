"""Looped background-ambience beds mixed under call audio.

A route can select an ambience template (call center, office, city, nature)
that plays continuously through the whole call — under the greeting, the
caller's silence, thinking gaps, and the agent's speech — so the line never
goes dead-quiet. Continuity is what sells it: the pipeline's idle sender
emits bed frames whenever nothing else is being sent, and the paced
playback / filler paths mix the bed under their voice frames.

Templates are 8 kHz / 16-bit / mono WAV loops in ``assets/ambience/``
(seamless start↔end). Raw PCM is cached per template process-wide; each call
gets an :class:`AmbienceSource` with the global gain baked in and a random
start offset so concurrent calls don't sound cloned. A missing or malformed
asset degrades to no ambience — never a call failure.
"""

import logging
import os
import random
import wave

import numpy as np

import config

logger = logging.getLogger("ambience")

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "assets", "ambience")

# Route ``background_sound`` value → asset file. "off" / unknown → no bed.
TEMPLATES = {
    "call_center": "call-center.wav",
    "office": "office.wav",
    "city": "city.wav",
    "nature": "nature.wav",
}

# template → raw int16 PCM (no gain), loaded once per process
_pcm_cache: dict[str, bytes] = {}


def _load_pcm(template: str) -> bytes | None:
    """Load a template's PCM, enforcing the call audio format."""
    if template in _pcm_cache:
        return _pcm_cache[template]
    filename = TEMPLATES.get(template)
    if not filename:
        return None
    path = os.path.join(ASSETS_DIR, filename)
    try:
        with wave.open(path, "rb") as w:
            if (w.getframerate() != config.SAMPLE_RATE
                    or w.getnchannels() != config.CHANNELS
                    or w.getsampwidth() != config.SAMPLE_WIDTH):
                logger.warning(
                    f"Ambience '{template}': {path} is not "
                    f"{config.SAMPLE_RATE}Hz/{config.CHANNELS}ch/"
                    f"{config.SAMPLE_WIDTH * 8}bit — skipping"
                )
                return None
            pcm = w.readframes(w.getnframes())
    except FileNotFoundError:
        logger.warning(f"Ambience '{template}': asset missing ({path})")
        return None
    except Exception as e:
        logger.warning(f"Ambience '{template}': failed to load {path}: {e}")
        return None
    # Whole 320-byte frames only, so the loop wraps on a frame boundary.
    usable = len(pcm) - (len(pcm) % config.FRAME_SIZE)
    if usable < config.FRAME_SIZE:
        logger.warning(f"Ambience '{template}': asset shorter than one frame")
        return None
    _pcm_cache[template] = pcm[:usable]
    logger.info(
        f"Ambience '{template}' loaded: "
        f"{usable // (config.SAMPLE_RATE * config.SAMPLE_WIDTH)}s loop"
    )
    return _pcm_cache[template]


class AmbienceSource:
    """Per-call looped bed: gain-scaled frames + mixing under voice audio."""

    def __init__(self, pcm: bytes, gain: float):
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * gain
        self._samples = samples.astype(np.int16)
        self._n_frames = len(pcm) // config.FRAME_SIZE
        # Random start so concurrent calls on the same template don't sound
        # cloned; frame-aligned so next_frame stays a simple slice.
        self._pos = random.randrange(self._n_frames)

    def next_frame(self) -> bytes:
        """The next 20ms bed frame (wraps seamlessly at the loop end)."""
        start = self._pos * (config.FRAME_SIZE // config.SAMPLE_WIDTH)
        frame = self._samples[start:start + config.FRAME_SIZE // config.SAMPLE_WIDTH]
        self._pos = (self._pos + 1) % self._n_frames
        return frame.tobytes()

    def mix_into(self, voice_frame: bytes) -> bytes:
        """Mix the next bed frame under a voice frame (int32 sum, clipped)."""
        bed = np.frombuffer(self.next_frame(), dtype=np.int16).astype(np.int32)
        voice = np.frombuffer(voice_frame, dtype=np.int16).astype(np.int32)
        n = min(len(bed), len(voice))
        mixed = np.clip(voice[:n] + bed[:n], -32768, 32767).astype(np.int16)
        if len(voice) > n:  # partial trailing frame — keep the voice tail
            return mixed.tobytes() + voice_frame[n * config.SAMPLE_WIDTH:]
        return mixed.tobytes()


def load_ambience(template: str, gain: float) -> AmbienceSource | None:
    """Build a per-call source for ``template`` ('off'/unknown/broken → None)."""
    if not template or template == "off":
        return None
    pcm = _load_pcm(template)
    if pcm is None:
        return None
    return AmbienceSource(pcm, gain)
