"""add_shape's shape types: every preset python-pptx draws, by its name in
snake case, beside the words write_pptx took before; and adjustments.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx import Presentation
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE

import powerpoint as ppt_mod
from formats.pptx import pptx_shapes


async def _same(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _same)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _write(path, ops, create_new=True) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "operations": ops, "create_new": create_new}))


def test_the_presets_are_python_pptxs():
    assert set(pptx_shapes.PRESETS) == {m.name.lower() for m in MSO_AUTO_SHAPE_TYPE
                                        if m.name not in ("MIXED", "NOT_PRIMITIVE")}


def test_every_preset_draws(tmp_path):
    f = tmp_path / "p.pptx"
    words = pptx_shapes.SHAPE_TYPES
    ops = [{"op": "add_slide", "layout": "blank"}]
    ops += [{"op": "add_shape", "slide_index": 0, "shape_type": word, "left": 0.2 + (n % 20) * 0.65,
             "top": 0.2 + (n // 20) * 0.7, "width": 0.6, "height": 0.6} for n, word in enumerate(words)]
    out = _write(f, ops)
    assert out.startswith("Presentation saved: ") and "pptx.package" not in out, out[:400]
    assert len(Presentation(str(f)).slides[0].shapes) == len(words)


@pytest.mark.parametrize(("word", "geometry"), [
    ("oval", "ellipse"), ("arrow_right", "rightArrow"), ("pentagon", "homePlate"), ("star", "star5"),
    ("regular_pentagon", "pentagon"), ("cloud_callout", "cloudCallout"), ("right_arrow", "rightArrow"),
    ("Snip 2 Same Rectangle", "snip2SameRect"), ("flowchart_decision", "flowChartDecision"),
])
def test_words_old_and_new_name_their_geometry(tmp_path, word, geometry):
    f = tmp_path / "p.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"}, {"op": "add_shape", "slide_index": 0,
                                                              "shape_type": word}])
    assert out.startswith("Presentation saved: "), out
    prst = Presentation(str(f)).slides[0].shapes[0]._element.spPr.prstGeom
    assert prst.get("prst") == geometry


def test_adjustments_move_the_handles(tmp_path):
    f = tmp_path / "p.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {"op": "add_shape", "slide_index": 0, "shape_type": "rounded_rectangle", "adjustments": [0.5]},
                     {"op": "add_shape", "slide_index": 0, "shape_type": "right_arrow", "top": 3,
                      "adjustments": [0.6, 0.3]}])
    assert out.startswith("Presentation saved: "), out
    rounded, arrow = Presentation(str(f)).slides[0].shapes
    assert rounded.adjustments[0] == pytest.approx(0.5)
    assert (arrow.adjustments[0], arrow.adjustments[1]) == (pytest.approx(0.6), pytest.approx(0.3))


@pytest.mark.parametrize(("keys", "message"), [
    ({"shape_type": "snip_rect"}, "unknown shape_type 'snip_rect'"),
    ({"shape_type": "chevronn"}, "Did you mean chevron"),
    ({"shape_type": "oval", "adjustments": [0.1, 0.2]}, "this shape takes 0 adjustments, not 2"),
])
def test_a_wrong_shape_type_names_the_closest(tmp_path, keys, message):
    f = tmp_path / "p.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"}, {"op": "add_shape", "slide_index": 0, **keys}])
    assert out.startswith("Nothing saved: ") and message in out, out
    assert len(out) < 2000
