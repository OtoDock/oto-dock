"""Shape effects: gradient fills, transparency, shadow, glow, soft edges and
dashed outlines, in the schema's order; and the checks reading a
see-through fill as see-through.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx import Presentation

import checks
import powerpoint as ppt_mod
from ooxml import order

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


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


def _sp_pr(path, index=0):
    return Presentation(str(path)).slides[0].shapes[index]._element.find(f"{P}spPr")


_SHAPE = {"op": "add_shape", "slide_index": 0, "left": 1, "top": 1, "width": 3, "height": 1.5}


def test_a_gradient_in_either_form(tmp_path):
    f = tmp_path / "e.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {**_SHAPE, "gradient": {"colors": ["#0B2545", "accent1", "#8DA9C4"], "angle": 90}},
                     {**_SHAPE, "top": 4, "gradient": [{"color": "#C00000", "position": 0},
                                                       {"color": "#FFFFFF", "position": 1}]}])
    assert out.startswith("Presentation saved: ") and "pptx.package" not in out, out
    first = _sp_pr(f).find(f"{A}gradFill")
    stops = first.findall(f"{A}gsLst/{A}gs")
    assert [gs.get("pos") for gs in stops] == ["0", "50000", "100000"]
    assert stops[1][0].tag == f"{A}schemeClr" and first.find(f"{A}lin").get("ang") == "5400000"
    assert _sp_pr(f).find(f"{A}solidFill") is None and order.out_of_order(_sp_pr(f)) == []
    assert _sp_pr(f, 1).find(f"{A}gradFill/{A}gsLst/{A}gs/{A}srgbClr").get("val") == "C00000"


def test_transparency_on_a_colour_a_gradient_and_an_outline(tmp_path):
    f = tmp_path / "e.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {**_SHAPE, "transparency": 60},
                     {**_SHAPE, "top": 3, "gradient": {"colors": ["#0B2545", "#8DA9C4"]}, "transparency": 25},
                     {**_SHAPE, "top": 5, "line_color": "#C00000", "line_transparency": 50, "line_dash": "dash"}])
    assert out.startswith("Presentation saved: "), out
    assert _sp_pr(f).find(f"{A}solidFill/{A}schemeClr/{A}alpha").get("val") == "40000"
    assert [a.get("val") for a in _sp_pr(f, 1).iter(f"{A}alpha")] == ["75000", "75000"]
    line = _sp_pr(f, 2).find(f"{A}ln")
    assert line.find(f"{A}solidFill/{A}srgbClr/{A}alpha").get("val") == "50000"
    assert line.find(f"{A}prstDash").get("val") == "dash" and order.out_of_order(line) == []


def test_shadow_glow_and_soft_edges_in_the_schemas_order(tmp_path):
    f = tmp_path / "e.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {**_SHAPE, "soft_edges": 4, "glow": {"color": "#E07A1F", "size": 10},
                      "shadow": {"blur": 8, "distance": 4, "angle": 90, "transparency": 50}}])
    assert out.startswith("Presentation saved: ") and "pptx.package" not in out, out
    effects = _sp_pr(f).find(f"{A}effectLst")
    assert [c.tag.rsplit("}", 1)[-1] for c in effects] == ["glow", "outerShdw", "softEdge"]
    shadow = effects.find(f"{A}outerShdw")
    assert (shadow.get("blurRad"), shadow.get("dist"), shadow.get("dir")) == ("101600", "50800", "5400000")
    assert order.out_of_order(_sp_pr(f)) == []


def test_no_shadow_and_a_glow_alone_say_what_the_style_gave(tmp_path):
    f = tmp_path / "e.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"}, {**_SHAPE, "glow": {"size": 6}},
                     {**_SHAPE, "top": 4, "shadow": False}])
    assert "replace the shadow its style gave; add shadow: true to keep one" in out, out
    assert len(_sp_pr(f, 1).find(f"{A}effectLst")) == 0
    out = _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "shadow": True}],
                 create_new=False)
    assert [c.tag.rsplit("}", 1)[-1] for c in _sp_pr(f).find(f"{A}effectLst")] == ["glow", "outerShdw"]


def test_transparency_on_a_fill_its_style_gives(tmp_path):
    from pptx.util import Inches

    f = tmp_path / "e.pptx"
    _write(f, [{"op": "add_slide", "layout": "blank"}])
    prs = Presentation(str(f))
    prs.slides[0].shapes.add_shape(1, Inches(1), Inches(1), Inches(2), Inches(1))
    prs.save(str(f))
    out = _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "transparency": 30}],
                 create_new=False)
    assert out.startswith("Presentation saved: "), out
    colour = _sp_pr(f).find(f"{A}solidFill/{A}schemeClr")
    assert colour.get("val") == "accent1" and colour.find(f"{A}alpha").get("val") == "70000"


@pytest.mark.parametrize(("keys", "message"), [
    ({"gradient": {"colors": ["#FFFFFF"]}}, "a gradient takes 2 to 6 colours"),
    ({"transparency": 150}, "transparency 150 is out of range"),
    ({"line_transparency": 20}, "the outline has no colour of its own"),
    ({"line_dash": "wavy"}, "unknown line_dash 'wavy'"),
    ({"shadow": {"blur": -1}}, "shadow blur -1 is out of range"),
])
def test_a_wrong_effect_names_the_fix(tmp_path, keys, message):
    f = tmp_path / "e.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"}, {**_SHAPE, **keys}])
    assert out.startswith("Nothing saved: ") and message in out, out


def test_a_see_through_card_does_not_cover_text(tmp_path):
    f = tmp_path / "e.pptx"
    _write(f, [{"op": "add_slide", "layout": "blank"},
               {"op": "add_textbox", "slide_index": 0, "text": "Revenue grew", "left": 1, "top": 1, "width": 4,
                "height": 1, "autofit": "none", "font_size": 20},
               {**_SHAPE, "fill_color": "#1F4E79", "transparency": 80},
               {**_SHAPE, "left": 6, "fill_color": "#1F4E79"},
               {"op": "add_textbox", "slide_index": 0, "text": "Costs flat", "left": 6, "top": 1, "width": 4,
                "height": 1, "autofit": "none", "font_size": 20}])
    with checks.CheckContext(str(f), "pptx", data=f.read_bytes()) as ctx:
        found = checks.run_checks(ctx, phase="document", only=["pptx.overlap"])
    assert found == []
    _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_index": 1, "transparency": 10},
               {"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "top": 1}], create_new=False)
    with checks.CheckContext(str(f), "pptx", data=f.read_bytes()) as ctx:
        found = checks.run_checks(ctx, phase="document", only=["pptx.overlap"])
    assert [x.location.split(",")[1].strip().split()[1] for x in found] == ["2"]


def test_a_text_box_takes_effects(tmp_path):
    f = tmp_path / "e.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {"op": "add_textbox", "slide_index": 0, "text": "Glowing",
                      "glow": {"color": "accent2", "size": 5}}])
    assert out.startswith("Presentation saved: "), out
    assert _sp_pr(f).find(f"{A}effectLst/{A}glow") is not None


@pytest.mark.parametrize(("keys", "message"), [
    ({"shape_type": "rectangle", "adjustments": [0.1, 0.2]}, "this shape takes 0 adjustments, not 2"),
    ({"line_transparency": 40}, "the outline has no colour of its own"),
    ({"shadow": {"blur": 500}}, "shadow blur 500 is out of range"),
    ({"glow": {"size": 6, "transparency": 140}}, "glow transparency 140 is out of range"),
    ({"soft_edges": -2}, "soft_edges -2 is out of range"),
    ({"gradient": {"colors": ["#FFFFFF", "#000000"], "positions": [0, 3]}}, "gradient position 3 is out of range"),
])
def test_a_shape_that_fails_is_never_drawn(tmp_path, keys, message):
    f = tmp_path / "e.pptx"
    _write(f, [{"op": "add_slide", "layout": "blank"}])
    out = asyncio.run(ppt_mod.handle_write_pptx({
        "path": str(f), "best_effort": True,
        "operations": [{**_SHAPE, "text": "X", **keys}, {**_SHAPE, "top": 4, "text": "Y"}]}))
    assert message in out, out
    assert [s.text_frame.text for s in Presentation(str(f)).slides[0].shapes] == ["Y"]


def test_effects_on_an_existing_shape_are_checked_before_anything_changes(tmp_path):
    f = tmp_path / "e.pptx"
    _write(f, [{"op": "add_slide", "layout": "blank"}, {**_SHAPE, "fill_color": "#1F4E79"}])
    out = asyncio.run(ppt_mod.handle_write_pptx({
        "path": str(f), "best_effort": True,
        "operations": [{"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "fill_color": "#C00000",
                        "shadow": {"distance": 900}}, {**_SHAPE, "top": 4}]}))
    assert "shadow distance 900 is out of range" in out and out.startswith("Presentation saved: "), out
    assert _sp_pr(f).find(f"{A}solidFill/{A}srgbClr").get("val") == "1F4E79"
