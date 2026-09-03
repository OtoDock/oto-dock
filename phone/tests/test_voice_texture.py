"""Voice texture: grain + early reflections that glue the voice into the bed.

Pins the stateful bits — the echo ring buffer carries across frames, a send
gap drops the stale tail, and the pipeline only builds a texture when the
route actually has an ambience bed.
"""

import numpy as np

import config
from voice_texture import VoiceTexture
from pipeline_fakes import make_route


SAMPLES = config.FRAME_SIZE // config.SAMPLE_WIDTH  # 160 per 20ms frame


def _frame(values=0):
    return np.full(SAMPLES, values, dtype=np.int16).tobytes()


def test_silence_stays_silent_and_length_is_preserved():
    vt = VoiceTexture(0.4)
    out = vt.process(_frame(0))
    assert len(out) == config.FRAME_SIZE
    assert not np.any(np.frombuffer(out, dtype=np.int16))


def test_echo_tap_carries_across_frames():
    """An impulse in frame 1 reflects into frame 2 (23ms tap > one frame)."""
    vt = VoiceTexture(1.0)
    impulse = np.zeros(SAMPLES, dtype=np.int16)
    impulse[10] = 20000
    vt.process(impulse.tobytes())

    out2 = np.frombuffer(vt.process(_frame(0)), dtype=np.int16)
    tap = int(0.023 * config.SAMPLE_RATE)          # 184 samples
    echo_pos = 10 + tap - SAMPLES                  # lands in frame 2
    assert abs(int(out2[echo_pos])) > 500          # reflection present
    assert abs(int(out2[echo_pos])) < 20000        # ...at reduced gain


def test_send_gap_drops_stale_echo_tail(monkeypatch):
    vt = VoiceTexture(1.0)
    impulse = np.zeros(SAMPLES, dtype=np.int16)
    impulse[10] = 20000
    vt.process(impulse.tobytes())

    vt._last_t -= 0.2  # simulate a >100ms silence gap
    out2 = np.frombuffer(vt.process(_frame(0)), dtype=np.int16)
    assert not np.any(out2)  # history zeroed — no ghost echo


def test_grain_keeps_small_signals_untouched_and_compresses_peaks():
    vt = VoiceTexture(1.0)
    quiet = np.frombuffer(vt.process(_frame(300)), dtype=np.int16)
    assert abs(int(quiet[0]) - 300) <= 6           # small-signal ~unity
    vt2 = VoiceTexture(1.0)
    hot = np.frombuffer(vt2.process(_frame(30000)), dtype=np.int16)
    assert int(hot[0]) < 30000                      # peak softly compressed


def test_pipeline_builds_texture_only_with_ambience(make_pipeline):
    from config_manager import ConfigManager
    cfg = ConfigManager()
    cfg.load({"settings": {"voice_texture": "0.4"}, "routes": []})

    plain = make_pipeline(route=make_route(), cfg=cfg)
    assert plain._texture is None                   # no bed → no texture

    route = make_route()
    route.background_sound = "call_center"          # real shipped asset
    textured = make_pipeline(route=route, cfg=cfg)
    assert textured._ambience is not None
    assert textured._texture is not None

    cfg_off = ConfigManager()
    cfg_off.load({"settings": {"voice_texture": "0"}, "routes": []})
    disabled = make_pipeline(route=route, cfg=cfg_off)
    assert disabled._texture is None                # knob 0 → off
