"""Goertzel DTMF detector: synthesized tones, noise immunity, framing."""

import numpy as np

from telephony.dtmf_detect import DtmfDetector, _COL_FREQS, _DIGITS, _ROW_FREQS

RATE = 8000


def tone(digit: str, dur_s: float, amp: float = 0.3) -> bytes:
    for ri, row in enumerate(_DIGITS):
        if digit in row:
            f1, f2 = _ROW_FREQS[ri], _COL_FREQS[row.index(digit)]
            break
    t = np.arange(int(RATE * dur_s)) / RATE
    x = amp / 2 * np.sin(2 * np.pi * f1 * t) + amp / 2 * np.sin(2 * np.pi * f2 * t)
    return (x * 32767).astype("<i2").tobytes()


def silence(dur_s: float) -> bytes:
    return b"\x00\x00" * int(RATE * dur_s)


def feed_chunked(d: DtmfDetector, pcm: bytes, chunk: int = 320) -> str:
    return "".join(d.feed(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk))


def test_all_sixteen_symbols_decode():
    d = DtmfDetector(RATE)
    stream = b"".join(tone(c, 0.08) + silence(0.08) for c in "1234567890*#ABCD")
    assert feed_chunked(d, stream) == "1234567890*#ABCD"


def test_sub_40ms_tone_is_rejected():
    d = DtmfDetector(RATE)
    assert feed_chunked(d, silence(0.1) + tone("5", 0.02) + silence(0.2)) == ""


def test_long_tone_registers_once():
    d = DtmfDetector(RATE)
    assert feed_chunked(d, tone("7", 0.3) + silence(0.1)) == "7"


def test_repeat_digit_needs_release_gap():
    d = DtmfDetector(RATE)
    out = feed_chunked(d, tone("2", 0.08) + silence(0.06) + tone("2", 0.08)
                       + silence(0.1))
    assert out == "22"


def test_noise_and_speechlike_audio_no_false_positives():
    d = DtmfDetector(RATE)
    rng = np.random.default_rng(7)
    noise = np.convolve(rng.normal(0, 0.15, RATE * 5), np.ones(8) / 8, "same")
    pcm = (np.clip(noise, -1, 1) * 32767).astype("<i2").tobytes()
    assert feed_chunked(d, pcm) == ""


def test_single_tone_is_not_a_digit():
    # One frequency alone (voice harmonics, hold music) must not decode.
    t = np.arange(RATE) / RATE
    x = 0.3 * np.sin(2 * np.pi * 770.0 * t)
    pcm = (x * 32767).astype("<i2").tobytes()
    assert feed_chunked(DtmfDetector(RATE), pcm) == ""


def test_arbitrary_chunk_sizes_are_buffered():
    # AudioSocket TLV payload length is not guaranteed 320 bytes.
    d = DtmfDetector(RATE)
    stream = tone("3", 0.08) + silence(0.1)
    out, i, sizes = "", 0, [100, 322, 158, 320, 64]
    while i < len(stream):
        n = sizes.pop(0); sizes.append(n)
        out += d.feed(stream[i:i + n]); i += n
    assert out == "3"


def test_reset_clears_state():
    d = DtmfDetector(RATE)
    d.feed(tone("9", 0.03))  # mid-qualification
    d.reset()
    assert feed_chunked(d, tone("9", 0.08) + silence(0.1)) == "9"
