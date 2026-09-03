"""Pre-response breath: gap gating + graceful degradation.

The inhale plays right before a response's first TTS audio, ONLY when the
agent hasn't produced voice audio for breath_min_gap_s — a breath mid rapid
exchange reads as uncanny, so the gate is the feature.
"""

import asyncio
import time

import numpy as np

import config
import breath
from config_manager import ConfigManager
from pipeline_fakes import make_route


def _cfg(**overrides):
    cfg = ConfigManager()
    settings = {"thinking_filler_delay_s": "0", "breath_min_gap_s": "1.5"}
    settings.update({k: str(v) for k, v in overrides.items()})
    cfg.load({"settings": settings, "routes": []})
    return cfg


def _breath_frames(n=3):
    return np.zeros(n * config.FRAME_SIZE // 2, dtype=np.int16).tobytes()


async def _run_tts(p):
    p.tts.start_streaming_context()
    task = asyncio.create_task(p._stream_tts_audio())
    await task


def test_breath_plays_after_gap_and_skips_mid_exchange(make_pipeline):
    p = make_pipeline(route=make_route(), cfg=_cfg())
    p._breath_pcm = _breath_frames(3)

    async def run():
        # long silence (fresh call: _last_voice_sent == 0) → breath + TTS
        await _run_tts(p)
        with_breath = len(p.conn.sent_audio)
        assert with_breath >= 4  # 3 breath frames + >=1 TTS frame

        # agent voice just now → gap below threshold → TTS only
        p.state._tts_playing = False
        p.state._last_voice_sent = time.monotonic()
        before = len(p.conn.sent_audio)
        await _run_tts(p)
        assert len(p.conn.sent_audio) - before < 3  # no breath frames

    asyncio.run(run())


def test_breath_disabled_or_missing_is_silent(make_pipeline):
    p = make_pipeline(route=make_route(), cfg=_cfg())
    assert p._breath_pcm is None or isinstance(p._breath_pcm, bytes)
    p._breath_pcm = None  # disabled / asset missing

    async def run():
        await _run_tts(p)
        assert len(p.conn.sent_audio) >= 1  # TTS alone still plays

    asyncio.run(run())


def test_load_breath_degrades_when_asset_missing(monkeypatch):
    monkeypatch.setattr(breath, "BREATH_PATH", "/nonexistent/inhale.wav")
    monkeypatch.setattr(breath, "_pcm_cache", None)
    monkeypatch.setattr(breath, "_load_failed", False)
    assert breath.load_breath(0.35) is None
    assert breath.load_breath(0.0) is None  # gain 0 = off


def test_load_breath_applies_gain():
    breath._pcm_cache = None
    breath._load_failed = False
    full = breath.load_breath(1.0)
    if full is None:  # asset not present in this checkout
        return
    half = breath.load_breath(0.5)
    a = np.frombuffer(full, dtype=np.int16).astype(np.float64)
    b = np.frombuffer(half, dtype=np.int16).astype(np.float64)
    assert 0.4 < (np.abs(b).mean() / max(np.abs(a).mean(), 1)) < 0.6


def test_load_breath_crescendo_envelope():
    """The clip rises toward its peak just before the tail release."""
    breath._pcm_cache = None
    breath._load_failed = False
    pcm = breath.load_breath(1.0)
    if pcm is None:  # asset not present in this checkout
        return
    s = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    q = len(s) // 4
    release = max(1, int(len(s) * 0.15))
    first_rms = np.sqrt((s[:q] ** 2).mean())
    peak_rms = np.sqrt((s[-release - q:-release] ** 2).mean())
    assert peak_rms > first_rms * 1.3


def test_load_breath_tail_release():
    """The final samples ramp to silence — the breath lands instead of cutting."""
    breath._pcm_cache = None
    breath._load_failed = False
    pcm = breath.load_breath(1.0)
    if pcm is None:  # asset not present in this checkout
        return
    s = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    release = max(1, int(len(s) * 0.15))
    tail = s[-release // 3:]                # deep inside the release ramp
    pre = s[-release - len(tail):-release]  # just before the release starts
    tail_rms = np.sqrt((tail ** 2).mean())
    pre_rms = np.sqrt((pre ** 2).mean())
    assert tail_rms < max(pre_rms, 1) * 0.5
    assert abs(s[-1]) <= 1  # last sample is (numerical) silence