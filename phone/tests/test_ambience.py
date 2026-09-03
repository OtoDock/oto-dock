"""Background-ambience bed: loop cycling, gain, mixing, and the idle sender.

The AmbienceSource math is pure (no assets needed — PCM is injected); the
idle-sender test drives ``_ambience_loop`` against the FakeConn and pins the
"only when nothing else sent" guard.
"""

import asyncio
import contextlib
import time

import numpy as np

import config
from ambience import AmbienceSource, load_ambience
from pipeline_fakes import make_route


SAMPLES_PER_FRAME = config.FRAME_SIZE // config.SAMPLE_WIDTH


def _pcm(*frame_values: int) -> bytes:
    """PCM of N frames, each filled with a constant sample value."""
    out = b""
    for v in frame_values:
        out += np.full(SAMPLES_PER_FRAME, v, dtype=np.int16).tobytes()
    return out


def test_next_frame_cycles_and_wraps():
    src = AmbienceSource(_pcm(100, 200, 300), gain=1.0)
    src._pos = 0
    seen = [np.frombuffer(src.next_frame(), dtype=np.int16)[0]
            for _ in range(6)]
    assert seen == [100, 200, 300, 100, 200, 300]


def test_gain_scales_bed():
    src = AmbienceSource(_pcm(1000), gain=0.1)
    frame = np.frombuffer(src.next_frame(), dtype=np.int16)
    assert frame[0] == 100


def test_mix_into_sums_and_clips():
    src = AmbienceSource(_pcm(500), gain=1.0)
    voice = np.full(SAMPLES_PER_FRAME, 1000, dtype=np.int16).tobytes()
    mixed = np.frombuffer(src.mix_into(voice), dtype=np.int16)
    assert mixed[0] == 1500

    loud = AmbienceSource(_pcm(30000), gain=1.0)
    hot_voice = np.full(SAMPLES_PER_FRAME, 30000, dtype=np.int16).tobytes()
    clipped = np.frombuffer(loud.mix_into(hot_voice), dtype=np.int16)
    assert clipped[0] == 32767  # clipped, no int16 wraparound


def test_load_ambience_off_and_unknown_are_none():
    assert load_ambience("off", 0.12) is None
    assert load_ambience("", 0.12) is None
    assert load_ambience("no-such-template", 0.12) is None


def test_idle_sender_fills_gaps_at_full_rate_but_yields_to_voice(make_pipeline):
    """The ambience loop emits bed frames at (near) full frame rate while the
    line is idle — one per 20ms slot, NOT every other slot (regression: the
    loop once stamped its own sends into the voice timestamp, tripping its
    next iteration's idle check and halving the bed to an inaudible chop) —
    and stands down while a voice sender is stamping."""
    p = make_pipeline(route=make_route())
    p._ambience = AmbienceSource(_pcm(100, 200), gain=1.0)
    p.state._running = True

    async def run():
        task = asyncio.create_task(p._ambience_loop())
        await asyncio.sleep(0.2)  # ~10 idle frame slots
        idle_frames = len(p.conn.sent_audio)
        # full-rate bed: expect nearly every slot filled (the half-rate bug
        # produced ~5 here)
        assert idle_frames >= 8

        # Voice takes over: stamping _last_voice_sent suppresses the bed
        for _ in range(5):
            p.state._last_voice_sent = time.monotonic()
            await asyncio.sleep(0.02)
        suppressed = len(p.conn.sent_audio) - idle_frames
        assert suppressed <= 1  # at most one boundary frame

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_out_frame_mixes_bed_under_voice(make_pipeline):
    p = make_pipeline(route=make_route())
    p._ambience = AmbienceSource(_pcm(500), gain=1.0)
    voice = np.full(SAMPLES_PER_FRAME, 1000, dtype=np.int16).tobytes()
    mixed = np.frombuffer(p._out_frame(voice), dtype=np.int16)
    assert mixed[0] == 1500
    assert p.state._last_voice_sent > 0

    # ambience off → passthrough
    p2 = make_pipeline(route=make_route())
    assert p2._ambience is None
    assert p2._out_frame(voice) == voice
