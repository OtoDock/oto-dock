"""In-band DTMF detection for transports without native digit events.

AudioSocket has no DTMF TLV and there is no AMI event listener, so keypad
digits reach the daemon only as in-band tones in the 8 kHz PCM — and only
when the trunk actually passes them in-band (RFC2833-signalled digits never
enter the audio; PIN-gated AudioSocket routes need inband DTMF on the trunk).
Twilio delivers digits as `dtmf` events (`FRAME_DTMF`) and never needs this.

The detector is a windowed Goertzel bank over the standard 4×4 grid:
row 697/770/852/941 Hz × col 1209/1336/1477/1633 Hz. 20 ms windows with a
10 ms hop (tones straddle window boundaries — overlap keeps a mid-window
onset from costing a whole window); a digit registers on a 2-of-3 window
majority (≈40 ms sustained, the ITU minimum), then holds until two
consecutive non-matching windows pass (release gap) so one keypress can't
register twice. Per-window qualification: both tones above an absolute
floor, each dominant over its group's runner-up, forward/reverse twist
within 8 dB, and the pair carrying most of the window's energy (speech
rejection).

Only the PIN gate instantiates this, so there is zero steady-state cost.
"""

from __future__ import annotations

import numpy as np

_ROW_FREQS = (697.0, 770.0, 852.0, 941.0)
_COL_FREQS = (1209.0, 1336.0, 1477.0, 1633.0)
_DIGITS = ("123A", "456B", "789C", "*0#D")

_WINDOW_S = 0.020
_HOP_S = 0.010
#: Minimum tone amplitude, full-scale relative (~-34 dBFS).
_MIN_TONE_AMP = 0.02
#: A qualifying tone must beat its group's runner-up by this power ratio.
_DOMINANCE = 4.0
#: Max row/col power imbalance (≈8 dB either way).
_MAX_TWIST = 6.3
#: The tone pair must carry at least this share of the window's total energy.
_MIN_PAIR_SHARE = 0.55
#: Windows of agreement (out of the last 3) to register a digit.
_MAJORITY = 2
#: Consecutive non-matching windows before the same digit can re-register.
_RELEASE_WINDOWS = 2


class DtmfDetector:
    """Stateful in-band detector: feed PCM16 chunks, collect digits."""

    def __init__(self, sample_rate: int = 8000):
        self._rate = sample_rate
        self._n = int(sample_rate * _WINDOW_S)
        self._hop = int(sample_rate * _HOP_S)
        n = np.arange(self._n)
        freqs = np.array(_ROW_FREQS + _COL_FREQS)
        # 8×N complex bank — one matrix-vector product per window.
        self._bank = np.exp(-2j * np.pi * np.outer(freqs, n) / sample_rate)
        # Pure-tone power at amplitude a is (a·N/2)² in this normalization.
        self._floor = (_MIN_TONE_AMP * self._n / 2.0) ** 2
        self._buf = np.empty(0, dtype=np.float64)
        self._recent: list[str | None] = []
        self._active: str | None = None
        self._release = 0

    def feed(self, pcm: bytes) -> str:
        """Consume a PCM16 chunk (any length); return newly registered
        digits ("" almost always, one digit on a keypress)."""
        if not pcm:
            return ""
        samples = np.frombuffer(
            pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float64) / 32768.0
        self._buf = np.concatenate([self._buf, samples])
        out = []
        while len(self._buf) >= self._n:
            window = self._buf[: self._n]
            self._buf = self._buf[self._hop:]
            digit = self._classify(window)
            registered = self._advance(digit)
            if registered:
                out.append(registered)
        return "".join(out)

    def reset(self) -> None:
        self._buf = np.empty(0, dtype=np.float64)
        self._recent = []
        self._active = None
        self._release = 0

    # -- internals ------------------------------------------------------------

    def _classify(self, window: np.ndarray) -> str | None:
        """One window → the digit whose tone pair qualifies, else None."""
        power = np.abs(self._bank @ window) ** 2
        rows, cols = power[:4], power[4:]
        ri, ci = int(np.argmax(rows)), int(np.argmax(cols))
        r, c = rows[ri], cols[ci]
        if r < self._floor or c < self._floor:
            return None
        row_rest = max(np.partition(rows, -2)[-2], 1e-12)
        col_rest = max(np.partition(cols, -2)[-2], 1e-12)
        if r / row_rest < _DOMINANCE or c / col_rest < _DOMINANCE:
            return None
        twist = r / c
        if twist > _MAX_TWIST or twist < 1.0 / _MAX_TWIST:
            return None
        # Speech rejection: the pair must dominate the whole window. Pure-tone
        # Goertzel power (a·N/2)² maps to time-domain energy a²·N/2 — scale by
        # 2/N before comparing against Σx².
        total = float(np.dot(window, window))
        pair_energy = (r + c) * 2.0 / self._n
        if total <= 0 or pair_energy / total < _MIN_PAIR_SHARE:
            return None
        return _DIGITS[ri][ci]

    def _advance(self, digit: str | None) -> str | None:
        """Majority + release-gap state machine; returns a digit the moment
        it registers."""
        self._recent = (self._recent + [digit])[-3:]
        if self._active is not None:
            if digit == self._active:
                self._release = 0
            else:
                self._release += 1
                if self._release >= _RELEASE_WINDOWS:
                    self._active = None
                    self._release = 0
            return None
        if digit is not None and self._recent.count(digit) >= _MAJORITY:
            self._active = digit
            self._release = 0
            return digit
        return None
