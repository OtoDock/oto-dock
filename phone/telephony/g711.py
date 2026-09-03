"""G.711 μ-law codec (numpy-vectorized) for the Twilio media transport.

Python 3.13 removed the stdlib ``audioop`` module, so the mulaw↔PCM16
boundary transcode lives here on numpy (already a hard phone dependency).
Encode follows the CCITT G.711 reference (bias 0x84, clip 32635, complement
output); decode is a 256-entry table built from the inverse formula at
import. The classic invariant ``encode(decode(b)) == b`` holds for all 256
byte values (pinned by tests).
"""

from __future__ import annotations

import numpy as np

_BIAS = 0x84
_CLIP = 32635

# Segment upper bounds of the biased magnitude → exponent 0..7.
_SEG_BOUNDS = np.array(
    [0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF], dtype=np.int32
)


def _build_decode_table() -> np.ndarray:
    table = np.empty(256, dtype=np.int16)
    for byte in range(256):
        u = ~byte & 0xFF
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
        table[byte] = -magnitude if u & 0x80 else magnitude
    return table


_DECODE_TABLE = _build_decode_table()


def mulaw_to_pcm16(data: bytes) -> bytes:
    """Decode 8-bit μ-law bytes to 16-bit signed little-endian PCM."""
    if not data:
        return b""
    return _DECODE_TABLE[np.frombuffer(data, dtype=np.uint8)].tobytes()


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Encode 16-bit signed little-endian PCM to 8-bit μ-law.

    A trailing odd byte (not a whole sample) is dropped.
    """
    if len(pcm) < 2:
        return b""
    samples = np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0)
    magnitude = np.minimum(np.abs(samples), _CLIP) + _BIAS
    exponent = np.searchsorted(_SEG_BOUNDS, magnitude)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    encoded = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return encoded.astype(np.uint8).tobytes()
