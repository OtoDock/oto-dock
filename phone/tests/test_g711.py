"""G.711 μ-law codec — reference vectors + the CCITT invariants."""

import struct

import numpy as np
import pytest

from telephony.g711 import mulaw_to_pcm16, pcm16_to_mulaw


def _pcm(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


# Hand-derived CCITT reference pairs (bias 0x84, clip 32635, complement out).
REFERENCE = [
    (0, 0xFF),        # silence
    (32767, 0x80),    # positive clip
    (-32768, 0x00),   # negative clip
    (32124, 0x80),    # top segment representative encodes to the clip byte
    (-32124, 0x00),
    (8, 0xFE),        # first step above silence
    (-8, 0x7E),
]


@pytest.mark.parametrize("sample,byte", REFERENCE)
def test_encode_reference_vectors(sample, byte):
    assert pcm16_to_mulaw(_pcm(sample)) == bytes([byte])


def test_decode_reference_vectors():
    assert mulaw_to_pcm16(bytes([0xFF])) == _pcm(0)
    assert mulaw_to_pcm16(bytes([0x80])) == _pcm(32124)
    assert mulaw_to_pcm16(bytes([0x00])) == _pcm(-32124)


def test_encode_decode_encode_is_identity_for_all_bytes():
    # decode's outputs are exact segment representatives; re-encoding must
    # reproduce every byte value. Sole exception: μ-law has TWO zero codes,
    # and "negative zero" 0x7F decodes to 0, which re-encodes as positive
    # zero 0xFF (audioop behaved identically).
    everything = bytes(b for b in range(256) if b != 0x7F)
    assert pcm16_to_mulaw(mulaw_to_pcm16(everything)) == everything
    assert pcm16_to_mulaw(mulaw_to_pcm16(bytes([0x7F]))) == bytes([0xFF])


def test_roundtrip_quantization_error_is_segment_bounded():
    samples = np.arange(-32768, 32768, 17, dtype=np.int16)
    decoded = np.frombuffer(
        mulaw_to_pcm16(pcm16_to_mulaw(samples.tobytes())), dtype="<i2"
    ).astype(np.int32)
    err = np.abs(decoded - samples.astype(np.int32))
    # Clip region (|x| > 32635 → 32124) dominates at 644; below the clip the
    # top-segment step (1 << 10) bounds error at half a step; small samples
    # must be near-exact.
    assert err.max() <= 644
    small = np.abs(samples.astype(np.int32)) < 128
    assert err[small].max() <= 8  # first-segment boundary step


def test_encode_is_monotonic_in_magnitude():
    samples = np.arange(0, 32768, 33, dtype=np.int16)
    encoded = np.frombuffer(pcm16_to_mulaw(samples.tobytes()), dtype=np.uint8)
    # Positive μ-law bytes DECREASE as magnitude grows (complement encoding).
    assert (np.diff(encoded.astype(np.int16)) <= 0).all()


def test_empty_and_odd_input_edges():
    assert pcm16_to_mulaw(b"") == b""
    assert mulaw_to_pcm16(b"") == b""
    assert pcm16_to_mulaw(b"\x01") == b""          # not a whole sample
    assert pcm16_to_mulaw(b"\x00\x00\x01") == bytes([0xFF])  # odd tail dropped
