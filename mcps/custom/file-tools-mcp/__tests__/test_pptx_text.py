"""write_pptx's text-frame keys, read back through the deck's geometry:
line and paragraph spacing, real bullets and numbering, insets, the
vertical anchor, autofit, and each run's language and letter spacing.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

import powerpoint as ppt_mod
from formats.pptx.pptx_geometry import load_geometry
from ooxml import order


async def _async_ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _bypass_platform(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("shared._resolve_path", _async_ident)
    monkeypatch.setattr("shared._push_preview", _noop)
    monkeypatch.setattr("shared._notify_file_written", _noop)
    monkeypatch.setattr("shared._to_agents_relative", lambda p: p)


def _write(path: Path, ops: list, create_new: bool = True) -> str:
    out = asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "create_new": create_new, "operations": ops}))
    return out


def _saved(out: str) -> bool:
    return out.startswith("Presentation saved: ") and "Failed:" not in out


def _ppr_in_order(paragraph_el) -> bool:
    ppr = paragraph_el.find(qn("a:pPr"))
    return ppr is None or order.out_of_order(ppr) == []


def test_a_textbox_takes_spacing_bullets_insets_anchor_autofit_and_language(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_textbox", "slide_index": 0, "text": "Πρώτο σημείο", "lang": "el-GR",
                      "line_spacing": "150%", "space_before": 6, "space_after": "12pt", "bullet": "–",
                      "bullet_indent": 0.25, "letter_spacing": -0.5,
                      "insets": {"left": 0, "top": "2mm", "right": 0.1, "bottom": 0},
                      "vertical_anchor": "middle", "autofit": "shrink"}])
    assert _saved(out), out
    shape = load_geometry(str(f)).slides[0].shapes[0]
    p = shape.paragraphs[0]
    assert p.line_spacing == ("pct", 1.5) and (p.space_before_pt, p.space_after_pt) == (6.0, 12.0)
    assert p.bullet == "–" and (p.margin_left_emu, p.indent_emu) == (Inches(0.25), -Inches(0.25))
    run = p.runs[0]
    assert (run.lang, run.spacing_pt) == ("el-GR", -0.5)
    frame = shape.frame
    assert frame.insets == (0, 72000, Inches(0.1), 0)
    assert (frame.anchor, frame.autofit) == ("ctr", "shrink")
    el = Presentation(str(f)).slides[0].shapes[0].text_frame.paragraphs[0]._p
    assert _ppr_in_order(el)


def test_numbering_with_a_style_and_a_start(tmp_path):
    f = tmp_path / "n.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_textbox", "slide_index": 0, "text": "Step", "bullet": "number",
                      "number_style": "alphaUcPeriod", "start_at": 3, "line_spacing": "18pt"}])
    assert _saved(out), out
    p = load_geometry(str(f)).slides[0].shapes[0].paragraphs[0]
    assert p.bullet == "autonum:alphaUcPeriod" and p.line_spacing == ("pt", 18.0)
    autonum = Presentation(str(f)).slides[0].shapes[0].text_frame.paragraphs[0]._p.find(".//" + qn("a:buAutoNum"))
    assert autonum.get("startAt") == "3"


def test_bullet_points_in_a_textbox_get_real_bullets_and_a_placeholder_keeps_its_own(tmp_path):
    f = tmp_path / "b.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_and_content"},
                     {"op": "add_textbox", "slide_index": 0, "text": "", "top": 5},
                     {"op": "add_bullet_points", "slide_index": 0, "shape_index": 2,
                      "items": ["A", {"text": "B", "level": 1}]},
                     {"op": "add_bullet_points", "slide_index": 0, "items": ["Body"], "space_after": 4}])
    assert _saved(out), out
    deck = load_geometry(str(f))
    box = deck.slides[0].shapes[2]
    assert [(p.text, p.bullet, p.level) for p in box.paragraphs] == [("A", "•", 0), ("B", "•", 1)]
    assert box.paragraphs[1].margin_left_emu == 2 * box.paragraphs[0].margin_left_emu
    body = deck.slides[0].shapes[1]
    assert body.paragraphs[0].bullet == "•" and body.paragraphs[0].space_after_pt == 4.0
    body_ppr = Presentation(str(f)).slides[0].placeholders[1].text_frame.paragraphs[0]._p.find(qn("a:pPr"))
    assert body_ppr is None or body_ppr.find(qn("a:buChar")) is None    # the layout's bullet, not a copy


def test_bullet_none_and_the_set_text_and_shape_paths(tmp_path):
    f = tmp_path / "s.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_and_content"},
                     {"op": "set_text", "slide_index": 0, "placeholder": "body", "text": "Intro\nMore",
                      "bullet": "none", "autofit": "resize"},
                     {"op": "add_shape", "slide_index": 0, "text": "Card", "line_spacing": 0.9, "lang": "en-GB",
                      "letter_spacing": 2, "vertical_anchor": "top", "insets": 0},
                     {"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "space_before": 3}])
    assert _saved(out), out
    deck = load_geometry(str(f))
    title, body, card = deck.slides[0].shapes
    assert [p.bullet for p in body.paragraphs] == [None, None] and body.frame.autofit == "resize"
    assert card.paragraphs[0].line_spacing == ("pct", 0.9) and card.paragraphs[0].runs[0].lang == "en-GB"
    assert card.paragraphs[0].runs[0].spacing_pt == 2.0
    assert card.frame.insets == (0, 0, 0, 0) and card.frame.anchor == "t"
    assert title.paragraphs[0].space_before_pt == 3.0


@pytest.mark.parametrize("op, message", [
    ({"bullet": "**"}, "bullet '**' is not one character"),
    ({"bullet": "number", "number_style": "greek"}, "unknown number_style 'greek'"),
    ({"bullet": "number", "start_at": 0}, "start_at 0 is out of range. Use 1 to 32767."),
    ({"bullet": "number", "start_at": 40000}, "start_at 40000 is out of range"),
    ({"letter_spacing": 9000}, "letter_spacing 9000 is out of range. Use -4000 to 4000 pt."),
    ({"line_spacing": "2000pt"}, "line_spacing 2000pt is out of range"),
    ({"line_spacing": 200}, "line_spacing 200 is out of range"),
    ({"line_spacing": 0}, "line_spacing 0 is out of range"),
    ({"space_after": -3}, "space_after -3 is out of range"),
    ({"bullet": "•", "bullet_indent": 9}, "bullet_indent 9 is out of range"),
    ({"font_size": 0.5}, "font_size 0.5 is out of range. Use 1 to 4000 pt."),
    ({"autofit": "grow"}, "unknown autofit 'grow'"),
    ({"vertical_anchor": "centre"}, "unknown vertical_anchor 'centre'"),
    ({"lang": "Greek"}, "lang 'Greek' is not a language tag"),
    ({"line_spacing": "wide"}, "line_spacing"),
])
def test_bad_text_values_name_the_fix_and_save_nothing(tmp_path, op, message):
    f = tmp_path / "x.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "add_textbox", "slide_index": 0, "text": "x", **op}])
    assert out.startswith("Nothing saved: ") and message in out, out
    assert not f.exists()


def test_text_keys_on_a_shape_without_text_fail(tmp_path):
    f = tmp_path / "p.pptx"
    from PIL import Image

    image = tmp_path / "i.png"
    Image.new("RGB", (10, 10)).save(image)
    out = _write(f, [{"op": "add_slide"}, {"op": "add_image", "slide_index": 0, "image_path": str(image)},
                     {"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "bullet": "•"}])
    assert "holds no text, so it has no spacing, bullets or insets" in out and out.startswith("Nothing saved: ")


def test_numbering_keys_without_a_bullet_warn(tmp_path):
    f = tmp_path / "w.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "add_textbox", "slide_index": 0, "text": "x", "start_at": 2}])
    assert out.startswith("Presentation saved: ") and "take effect with bullet" in out


def test_runs_take_their_own_language(tmp_path):
    f = tmp_path / "r.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_textbox", "slide_index": 0,
                "runs": [{"text": "Hello ", "lang": "en-US"}, {"text": "κόσμε", "lang": "el-GR", "bold": True}]}])
    runs = load_geometry(str(f)).slides[0].shapes[0].paragraphs[0].runs
    assert [(r.text, r.lang, r.bold) for r in runs] == [("Hello ", "en-US", False), ("κόσμε", "el-GR", True)]
    assert Pt(18) == Pt(runs[0].size_pt)


def test_paragraph_keys_on_a_shape_without_text_warn(tmp_path):
    f = tmp_path / "w.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_shape", "slide_index": 0, "bullet": "number", "line_spacing": 2, "insets": 0}])
    assert out.startswith("Presentation saved: ") and "take effect with text: the shape has none" in out
    assert load_geometry(str(f)).slides[0].shapes[0].frame.insets == (0, 0, 0, 0)



def test_a_textbox_line_feed_makes_paragraphs_and_a_run_line_feed_a_break(tmp_path):
    f = tmp_path / "lf.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_textbox", "slide_index": 0, "text": "One\nTwo\nThree", "bullet": "•",
                      "alignment": "center", "space_after": 6},
                     {"op": "add_textbox", "slide_index": 0, "top": 4,
                      "runs": [{"text": "Line one\nline two", "bold": True}, {"text": " tail"}]}])
    assert _saved(out), out
    bullets, runs = Presentation(str(f)).slides[0].shapes
    paras = bullets.text_frame.paragraphs
    assert [p.text for p in paras] == ["One", "Two", "Three"]
    assert all(p._p.find(".//" + qn("a:buChar")) is not None and p.alignment == 2 for p in paras)
    assert not any("\n" in (t.text or "") for t in bullets._element.iter(qn("a:t")))
    p = runs.text_frame.paragraphs[0]
    assert len(runs.text_frame.paragraphs) == 1 and p._p.find(qn("a:br")) is not None
    assert p.text == "Line one\vline two tail"
    deck = load_geometry(str(f))
    assert [len(s.paragraphs) for s in deck.slides[0].shapes] == [3, 1]


@pytest.mark.parametrize("alignment, expected", [("left", "l"), ("center", "ctr"), ("right", "r"),
                                                 ("justify", "just")])
def test_set_text_aligns_every_paragraph_it_writes(tmp_path, alignment, expected):
    """A title left-aligned without a patch: the key add_textbox has."""
    f = tmp_path / "a.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_and_content"},
                     {"op": "set_text", "slide_index": 0, "text": "Quarterly review", "alignment": alignment},
                     {"op": "set_text", "slide_index": 0, "placeholder": "body", "text": "One\nTwo",
                      "alignment": alignment},
                     {"op": "set_text", "slide_index": 0, "placeholder": "body",
                      "runs": [{"text": "Revenue "}, {"text": "up", "bold": True}], "alignment": alignment}])
    assert _saved(out), out
    slide = Presentation(str(f)).slides[0]
    for shape in slide.placeholders:
        for paragraph in shape.text_frame.paragraphs:
            assert paragraph._p.pPr is not None and paragraph._p.pPr.get("algn") == expected, shape.name
            assert _ppr_in_order(paragraph._p)


def test_set_text_refuses_an_alignment_it_does_not_know(tmp_path):
    f = tmp_path / "b.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_only"},
                     {"op": "set_text", "slide_index": 0, "text": "x", "alignment": "middle"}])
    assert out.startswith("Nothing saved") and "alignment" in out and "justify" in out, out


def test_set_text_without_alignment_keeps_the_layouts(tmp_path):
    f = tmp_path / "c.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_only"}, {"op": "set_text", "slide_index": 0, "text": "x"}])
    assert _saved(out), out
    paragraph = Presentation(str(f)).slides[0].shapes.title.text_frame.paragraphs[0]
    assert paragraph._p.pPr is None or paragraph._p.pPr.get("algn") is None
