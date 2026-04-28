"""Tone-check feedback loop: edit_image results carry numeric before→after
luminance stats for tonal ops, and the exposure direction is what the docs
promise (gamma > 1 brightens)."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import images


def _run(coro):
    return asyncio.run(coro)


def _gradient(tmp_path, name="grad.png"):
    """Horizontal 0→255 gradient — every tone zone represented."""
    from PIL import Image

    img = Image.new("RGB", (256, 64))
    px = img.load()
    for x in range(256):
        for y in range(64):
            px[x, y] = (x, x, x)
    p = tmp_path / name
    img.save(p)
    return str(p)


def _mean_luma(path):
    import numpy as np
    from PIL import Image

    arr = np.asarray(Image.open(path).convert("L"), dtype=float)
    return arr.mean()


def test_tone_check_line_present_for_tonal_ops(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    src = _gradient(tmp_path)
    out = str(tmp_path / "out.png")
    result = _run(images.handle_edit_image({
        "path": src, "output_path": out,
        "operations": [{"type": "exposure", "gamma": 1.3}],
    }))
    assert "Tone check (before → after)" in result
    assert "mean" in result and "clipped blacks" in result


def test_no_tone_check_for_geometry_only(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    src = _gradient(tmp_path)
    out = str(tmp_path / "out.png")
    result = _run(images.handle_edit_image({
        "path": src, "output_path": out,
        "operations": [{"type": "crop", "left": 0, "top": 0,
                        "right": 128, "bottom": 64}],
    }))
    assert "Tone check" not in result


def test_exposure_gamma_direction_matches_docs(tmp_path, monkeypatch):
    """gamma > 1 BRIGHTENS, < 1 DARKENS — the documented contract."""
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    src = _gradient(tmp_path)
    before = _mean_luma(src)

    brighter = str(tmp_path / "bright.png")
    _run(images.handle_edit_image({
        "path": src, "output_path": brighter,
        "operations": [{"type": "exposure", "gamma": 1.3}],
    }))
    assert _mean_luma(brighter) > before + 5

    darker = str(tmp_path / "dark.png")
    _run(images.handle_edit_image({
        "path": src, "output_path": darker,
        "operations": [{"type": "exposure", "gamma": 0.75}],
    }))
    assert _mean_luma(darker) < before - 5


def test_color_temperature_direction_matches_docs(tmp_path, monkeypatch):
    """>5500 warms (red up, blue down) per the skill."""
    import numpy as np
    from PIL import Image

    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    src = tmp_path / "gray.png"
    Image.new("RGB", (64, 64), (128, 128, 128)).save(src)
    out = str(tmp_path / "warm.png")
    _run(images.handle_edit_image({
        "path": str(src), "output_path": out,
        "operations": [{"type": "color_temperature", "temperature": 8000}],
    }))
    arr = np.asarray(Image.open(out), dtype=float)
    assert arr[..., 0].mean() > arr[..., 2].mean()  # warmer: R > B


def test_tone_stats_values_sane():
    from PIL import Image

    black = Image.new("RGB", (32, 32), (0, 0, 0))
    stats = images._tone_stats(black)
    assert stats["clip_black"] == 100.0
    assert stats["clip_white"] == 0.0
    white = Image.new("RGB", (32, 32), (255, 255, 255))
    stats = images._tone_stats(white)
    assert stats["clip_white"] == 100.0
    assert stats["highlights"] == 100.0


def test_analyze_image_reports_clipping(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    src = _gradient(tmp_path)
    result = _run(images.handle_analyze_image({"path": src}))
    assert "Clipped: blacks ≤5" in result
    assert "whites ≥250" in result


def test_blacks_level_direction_matches_docs(tmp_path, monkeypatch):
    """blacks level is RELATIVE: negative deepens, positive lifts, 0 no-op."""
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    from PIL import Image

    src = tmp_path / "veiled.png"
    Image.new("RGB", (64, 64), (100, 100, 100)).save(src)
    before = _mean_luma(src)

    deeper = str(tmp_path / "deep.png")
    _run(images.handle_edit_image({
        "path": str(src), "output_path": deeper,
        "operations": [{"type": "blacks", "level": -40}],
    }))
    assert _mean_luma(deeper) < before - 5

    lifted = str(tmp_path / "lift.png")
    _run(images.handle_edit_image({
        "path": str(src), "output_path": lifted,
        "operations": [{"type": "blacks", "level": 20}],
    }))
    assert _mean_luma(lifted) > before + 1

    noop = str(tmp_path / "noop.png")
    _run(images.handle_edit_image({
        "path": str(src), "output_path": noop,
        "operations": [{"type": "blacks", "level": 0}],
    }))
    assert abs(_mean_luma(noop) - before) < 1


def test_dehaze_runs_and_lowers_mean(tmp_path, monkeypatch):
    """dehaze imports scipy (a container dep — this guards the requirement)
    and darkens a veiled image, per the documented side effect."""
    pytest.importorskip("scipy")
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    from PIL import Image

    src = tmp_path / "hazy.png"
    Image.new("RGB", (64, 64), (170, 172, 168)).save(src)
    out = str(tmp_path / "dehazed.png")
    result = _run(images.handle_edit_image({
        "path": str(src), "output_path": out,
        "operations": [{"type": "dehaze", "strength": 0.3}],
    }))
    assert "Error" not in result
    assert "Tone check" in result
    assert _mean_luma(out) < _mean_luma(src)


def test_analyze_flags_hard_clipping(tmp_path, monkeypatch):
    """>3% pixels at the rails → explicit unrecoverable-detail suggestions."""
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    from PIL import Image

    blown = tmp_path / "blown.png"
    img = Image.new("RGB", (100, 100), (128, 128, 128))
    img.paste((255, 255, 255), (0, 0, 100, 30))
    img.save(blown)
    result = _run(images.handle_analyze_image({"path": str(blown)}))
    assert "Blown highlights" in result

    crushed = tmp_path / "crushed.png"
    img = Image.new("RGB", (100, 100), (128, 128, 128))
    img.paste((0, 0, 0), (0, 0, 100, 30))
    img.save(crushed)
    result = _run(images.handle_analyze_image({"path": str(crushed)}))
    assert "Crushed blacks" in result


def test_unparseable_operations_error_not_silent_copy(tmp_path, monkeypatch):
    """A non-empty operations arg that normalizes to nothing must FAIL, not
    save an unchanged copy that reads as success."""
    monkeypatch.setattr(images, "_resolve_path", lambda p, **kw: p)
    src = _gradient(tmp_path)
    out = str(tmp_path / "out.png")
    result = _run(images.handle_edit_image({
        "path": src, "output_path": out,
        "operations": ["grayscale", "sharpen"],  # bare strings — unusable
    }))
    assert result.startswith("Error:")
    assert "no edit was applied" in result
    assert not Path(out).exists()
