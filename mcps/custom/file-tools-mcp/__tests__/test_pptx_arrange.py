"""Arranging shapes: paint order, align and distribute by the geometry's
boxes, a placeholder moved with its whole box, connectors glued to shapes
following them, and pictures cropped, outlined and described.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx import Presentation
from pptx.util import Emu, Inches

import powerpoint as ppt_mod

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
ADEC = "{http://schemas.microsoft.com/office/drawing/2017/decorative}"


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


def _clean(out: str) -> bool:
    return out.startswith("Presentation saved: ") and "pptx.package" not in out


def _boxes(shape_list, slide=0):
    return [(s.left, s.top, s.width, s.height) for s in shape_list]


_THREE = [{"op": "add_slide", "layout": "blank"},
          {"op": "add_shape", "slide_index": 0, "text": "A", "left": 1, "top": 1, "width": 2, "height": 1},
          {"op": "add_shape", "slide_index": 0, "text": "B", "left": 4, "top": 2.5, "width": 1, "height": 2},
          {"op": "add_shape", "slide_index": 0, "text": "C", "left": 9, "top": 1.5, "width": 3, "height": 1}]


def _names(path):
    return [s.name for s in Presentation(str(path)).slides[0].shapes]


def test_the_paint_order_moves(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _THREE)
    first, second, third = _names(f)
    _write(f, [{"op": "arrange_shape", "slide_index": 0, "shape_index": 0, "order": "front"}], create_new=False)
    assert _names(f) == [second, third, first]
    _write(f, [{"op": "arrange_shape", "slide_index": 0, "shape_name": first, "order": "backward"}],
           create_new=False)
    assert _names(f) == [second, first, third]
    _write(f, [{"op": "arrange_shape", "slide_index": 0, "shape_name": third, "order": "back"}], create_new=False)
    assert _names(f) == [third, second, first]
    out = _write(f, [{"op": "arrange_shape", "slide_index": 0, "shape_name": third, "order": "back"}],
                 create_new=False)
    assert "is already at the back" in out


def test_the_paint_order_keeps_the_tree_head_and_its_extension_list(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _THREE)
    prs = Presentation(str(f))
    tree = prs.slides[0].shapes._spTree
    etree.SubElement(tree, f"{P}extLst")
    prs.save(str(f))
    out = _write(f, [{"op": "arrange_shape", "slide_index": 0, "shape_index": 0, "order": "front"},
                     {"op": "arrange_shape", "slide_index": 0, "shape_index": 1, "order": "back"}], create_new=False)
    assert _clean(out), out
    tags = [etree.QName(c).localname for c in Presentation(str(f)).slides[0].shapes._spTree]
    assert tags[:2] == ["nvGrpSpPr", "grpSpPr"] and tags[-1] == "extLst"


@pytest.mark.parametrize(("align", "expect"), [
    ("left", [(1, None), (1, None), (1, None)]), ("right", [(10, None), (11, None), (9, None)]),
    ("center", [(5.5, None), (6, None), (5, None)]), ("top", [(None, 1), (None, 1), (None, 1)]),
    ("bottom", [(None, 3.5), (None, 2.5), (None, 3.5)]), ("middle", [(None, 2.25), (None, 1.75), (None, 2.25)]),
])
def test_align_to_the_shapes(tmp_path, align, expect):
    f = tmp_path / "a.pptx"
    _write(f, _THREE)
    out = _write(f, [{"op": "align_shapes", "slide_index": 0, "shapes": [0, 1, 2], "align": align}],
                 create_new=False)
    assert _clean(out), out
    for shape, (x, y) in zip(Presentation(str(f)).slides[0].shapes, expect, strict=True):
        if x is not None:
            assert Emu(shape.left).inches == pytest.approx(x)
        if y is not None:
            assert Emu(shape.top).inches == pytest.approx(y)


def test_align_to_the_slide_and_refusals(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _THREE)
    out = _write(f, [{"op": "align_shapes", "slide_index": 0, "shapes": [{"shape_id": 2}], "align": "center",
                      "relative_to": "slide"}], create_new=False)
    assert _clean(out), out
    a = Presentation(str(f)).slides[0].shapes[0]
    assert Emu(a.left).inches == pytest.approx((13.333 - 2) / 2, abs=0.01)
    out = _write(f, [{"op": "align_shapes", "slide_index": 0, "shapes": [0], "align": "left"}], create_new=False)
    assert "align needs two or more shapes, or relative_to: slide" in out
    _write(f, [{"op": "add_group_shape", "slide_index": 0, "shapes": [0, 1]}], create_new=False)
    prs = Presentation(str(f))
    member = next(s for s in prs.slides[0].shapes if s.shape_type == 6).shapes[0].shape_id
    out = _write(f, [{"op": "align_shapes", "slide_index": 0, "shapes": [{"shape_id": member}, "C"],
                      "align": "top"}], create_new=False)
    assert "is inside a group" in out and out.startswith("Nothing saved: ")


def test_distribute_leaves_equal_gaps(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _THREE)
    out = _write(f, [{"op": "distribute_shapes", "slide_index": 0, "shapes": ["C", 0, 1]}], create_new=False)
    assert _clean(out), out
    a, b, c = Presentation(str(f)).slides[0].shapes
    gap1 = Emu(b.left).inches - Emu(a.left + a.width).inches
    gap2 = Emu(c.left).inches - Emu(b.left + b.width).inches
    assert gap1 == pytest.approx(gap2, abs=0.001) and Emu(a.left).inches == pytest.approx(1)
    assert Emu(c.left).inches == pytest.approx(9)
    out = _write(f, [{"op": "distribute_shapes", "slide_index": 0, "shapes": [0, 1]}], create_new=False)
    assert "distribute needs three or more shapes" in out


def test_a_placeholder_from_its_layout_moves_with_its_whole_box(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_only", "title": "Results"}])
    prs = Presentation(str(f))
    layout_title = prs.slides[0].slide_layout.placeholders[0]
    top, width, height = layout_title.top, layout_title.width, layout_title.height
    out = _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "left": 0.5}],
                 create_new=False)
    assert _clean(out) and "keeps a position of its own" in out and "Checks: no problems found." in out, out
    title = Presentation(str(f)).slides[0].shapes.title
    xfrm = title._element.spPr.find(f"{A}xfrm")
    assert xfrm.find(f"{A}off").get("y") == str(top) and xfrm.find(f"{A}ext").get("cx") == str(width)
    assert (title.left, title.top, title.width, title.height) == (Inches(0.5), top, width, height)


def _glue(path):
    cxn = next(Presentation(str(path)).slides[0].shapes._spTree.iter(f"{P}cNvCxnSpPr"))
    return [(etree.QName(c).localname, c.get("id"), c.get("idx")) for c in cxn]


def test_a_connector_glues_to_the_facing_sides_and_follows_moves(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _THREE)
    out = _write(f, [{"op": "add_connector", "slide_index": 0, "start_shape": "A", "end_shape": 2,
                      "arrow_end": True}], create_new=False)
    assert _clean(out), out
    assert _glue(f) == [("stCxn", "2", "3"), ("endCxn", "4", "1")]
    connector = Presentation(str(f)).slides[0].shapes[-1]
    assert (Emu(connector.begin_x).inches, Emu(connector.begin_y).inches) == (pytest.approx(3), pytest.approx(1.5))
    assert (Emu(connector.end_x).inches, Emu(connector.end_y).inches) == (pytest.approx(9), pytest.approx(2))
    _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_name": "C", "top": 4}], create_new=False)
    connector = Presentation(str(f)).slides[0].shapes[-1]
    assert Emu(connector.end_y).inches == pytest.approx(4.5)
    _write(f, [{"op": "align_shapes", "slide_index": 0, "shapes": ["A", "C"], "align": "top"}], create_new=False)
    connector = Presentation(str(f)).slides[0].shapes[-1]
    assert Emu(connector.begin_y).inches == pytest.approx(Emu(connector.end_y).inches)


def test_sides_given_and_a_shape_that_takes_no_glue(tmp_path):
    f = tmp_path / "a.pptx"
    out = _write(f, [*_THREE, {"op": "add_shape", "slide_index": 0, "shape_type": "oval", "left": 5, "top": 5},
                     {"op": "add_connector", "slide_index": 0, "start_shape": "B", "start_side": "bottom",
                      "end_shape": 3, "end_side": "top"}])
    assert _clean(out) and "without being glued" in out, out
    assert _glue(f) == [("stCxn", "3", "2")]


def test_a_connector_end_is_a_shape_or_a_point(tmp_path):
    f = tmp_path / "a.pptx"
    out = _write(f, [*_THREE, {"op": "add_connector", "slide_index": 0, "start_shape": "A", "start_x": 2}])
    assert "the start is given as a shape and as a point" in out
    out = _write(f, [*_THREE, {"op": "add_connector", "slide_index": 0, "start_side": "top"}])
    assert "start_side and end_side need start_shape or end_shape" in out


def test_a_picture_cropped_outlined_and_described(tmp_path):
    from PIL import Image

    image = tmp_path / "p.png"
    Image.new("RGB", (40, 20), "navy").save(image)
    f = tmp_path / "a.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {"op": "add_image", "slide_index": 0, "image_path": str(image), "width": 2,
                      "crop": {"left": 0.25, "right": 0.25}, "shape_type": "oval", "alt_text": "Our team"},
                     {"op": "add_image", "slide_index": 0, "image_path": str(image), "top": 4, "width": 1,
                      "decorative": True}])
    assert _clean(out), out
    portrait, ornament = Presentation(str(f)).slides[0].shapes
    assert (portrait.crop_left, portrait.crop_right) == (pytest.approx(0.25), pytest.approx(0.25))
    assert portrait._element.spPr.prstGeom.get("prst") == "ellipse"
    assert portrait._element.nvPicPr.cNvPr.get("descr") == "Our team"
    assert ornament._element.find(f".//{ADEC}decorative").get("val") == "1"
    out = _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_index": 1, "decorative": False,
                      "alt_text": "Divider"}], create_new=False)
    ornament = Presentation(str(f)).slides[0].shapes[1]
    assert ornament._element.find(f".//{ADEC}decorative") is None
    assert ornament._element.nvPicPr.cNvPr.get("descr") == "Divider"


@pytest.mark.parametrize(("keys", "message"), [
    ({"crop": {"left": 0.6, "right": 0.5}}, "crop takes shares of the picture"),
    ({"shape_index": 0, "crop": {"left": 0.1}}, "crop works on pictures"),
])
def test_a_wrong_crop_names_the_fix(tmp_path, keys, message):
    from PIL import Image

    image = tmp_path / "p.png"
    Image.new("RGB", (40, 20), "navy").save(image)
    f = tmp_path / "a.pptx"
    _write(f, [{"op": "add_slide", "layout": "blank"}, {"op": "add_shape", "slide_index": 0},
               {"op": "add_image", "slide_index": 0, "image_path": str(image), "width": 2}])
    out = _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_index": 1, **keys}], create_new=False)
    assert out.startswith("Nothing saved: ") and message in out, out


def test_a_name_another_shape_has_warns_for_morph(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _THREE)
    out = _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "name": "!!Card"},
                     {"op": "set_shape_format", "slide_index": 0, "shape_index": 1, "name": "!!Card"}],
                 create_new=False)
    assert "Morph pairs shapes by name" in out
    assert _names(f)[:2] == ["!!Card", "!!Card"]


def test_a_shape_is_named_when_drawn(tmp_path):
    f = tmp_path / "a.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {"op": "add_shape", "slide_index": 0, "text": "Step 1", "name": "!!Step"},
                     {"op": "set_shape_format", "slide_index": 0, "shape_name": "!!Step", "fill_color": "#1F4E79"}])
    assert _clean(out), out
    assert _names(f) == ["!!Step"]
