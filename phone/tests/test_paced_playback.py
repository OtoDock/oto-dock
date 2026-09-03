"""Absolute-schedule paced sender (R5): sleep jitter must not accumulate.

Per-frame relative pacing drifted behind real time by the event loop's
oversleep on EVERY frame — inaudible into a PBX jitter buffer (it re-times),
but the duplex browser player buffers a fixed lead and repeated erosion
clicks (live [pcm] signature: 10-99 ms underruns every few seconds of
continuous audio). The schedule anchor makes lateness within the catch-up
window burst-correct so the receiver's buffer refills; telephony keeps
catch-up 0 — any lateness re-anchors, i.e. the old never-fast-forward
cadence, byte for byte.
"""

import asyncio
import time

import pytest

from pipeline.playback import PlaybackMixin
from pipeline.state import CallState


class _Sender(PlaybackMixin):
    def __init__(self, conn, catch_up):
        self.conn = conn
        self.state = CallState()
        self.state._tts_playing = True
        self._texture = None
        self._ambience = None
        self._frame_bytes_out = 320          # 20 ms at 8 kHz s16le
        self._byte_rate_out = 16000
        self._pace_catch_up_s = catch_up


class _HiccupConn:
    """drain() blocks once (5th frame) then is instant — one real lag spike."""

    def __init__(self, hiccup_s=0.3):
        self.peer_addr = "test:0"
        self.is_closed = False
        self.sent = 0
        self._hiccup_s = hiccup_s

    def send_audio(self, pcm):
        self.sent += 1

    async def drain(self):
        if self.sent == 5 and self._hiccup_s:
            s, self._hiccup_s = self._hiccup_s, 0.0
            await asyncio.sleep(s)


@pytest.mark.asyncio
async def test_catch_up_bursts_after_hiccup():
    """Duplex window (1 s): the lost 0.3 s is burst-recovered, so the whole
    0.5 s of audio still lands in ~0.5 s of wall time and the receiver's
    jitter buffer refills instead of staying eroded."""
    conn = _HiccupConn()
    p = _Sender(conn, catch_up=1.0)
    pcm = b"\x00" * (320 * 25)               # 0.5 s of audio
    t0 = time.monotonic()
    await p._paced_playback(pcm)
    wall = time.monotonic() - t0
    assert conn.sent == 25
    assert wall < 0.68                       # hiccup absorbed by the burst
    assert p._pace_stalls == 0               # within the catch-up window
    assert 0.15 < p._pace_max_behind_s < 0.6


@pytest.mark.asyncio
async def test_telephony_default_never_bursts():
    """Catch-up 0 (phone calls): the hiccup re-anchors — lost time is never
    recovered and cadence never exceeds one frame per frame-duration (a PBX
    plays frames as they arrive; a burst fast-forwards audibly)."""
    conn = _HiccupConn()
    p = _Sender(conn, catch_up=0.0)
    pcm = b"\x00" * (320 * 25)
    t0 = time.monotonic()
    await p._paced_playback(pcm)
    wall = time.monotonic() - t0
    assert conn.sent == 25
    assert wall > 0.70                       # 0.5s audio + the unrecovered 0.3s hiccup
    assert p._pace_stalls == 1               # the hiccup; ε re-anchors don't count


def test_telephony_is_the_class_default():
    assert PlaybackMixin._pace_catch_up_s == 0.0
