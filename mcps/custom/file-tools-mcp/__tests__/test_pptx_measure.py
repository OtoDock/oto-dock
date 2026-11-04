"""The overflow measurement's rules, with fixed glyph widths (half the font
size per character) so they hold on any machine's fonts; the agreement with
LibreOffice is the office test's. And write_pptx's shrink-on-overflow,
which stores the scale its text needs.

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

import powerpoint as ppt_mod
from formats.pptx import pptx_measure
from formats.pptx.pptx_geometry import Box, Frame, Paragraph, Run, Shape

EMU_PT = 12700


def _half(text, family, size, bold, italic):
    return len(text) * size * 0.5


def _shape(paragraphs, *, width_pt=200.0, height_pt=100.0, insets=(0, 0, 0, 0), preset="rect", **frame):
    return Shape(slide_index=0, shape_id=2, name="Body", index=0, z=1, kind="textbox", placeholder=None,
                 preset=preset, box=Box(0, 0, int(width_pt * EMU_PT), int(height_pt * EMU_PT)), box_source="slide",
                 group_id=None, fill=None, frame=Frame(insets=tuple(int(i * EMU_PT) for i in insets), **frame),
                 paragraphs=paragraphs)


def _p(*texts, size=10.0, **kw):
    return Paragraph(level=0, runs=[Run(t, "Test", size) for t in texts], end_size_pt=size, **kw)


def _m(shape, **kw):
    return pptx_measure.measure(shape, widths=_half, **kw)


def test_words_fill_lines_greedily():
    # 10 pt text: 5 pt a character; 200 pt holds 40 characters.
    shape = _shape([_p("aaaaaaaaa " * 8)])          # eight 10-character words
    m = _m(shape)
    assert m.lines == 2 and m.needed_pt == pytest.approx(2 * 12.0)


def test_a_word_longer_than_a_line_breaks_between_its_characters():
    m = _m(_shape([_p("x" * 100)]))
    assert m.lines == 3


def test_line_breaks_and_empty_paragraphs_take_lines():
    shape = _shape([_p("one", "\n", "two"), _p(size=20.0), _p("end", "\n")])
    m = _m(shape)
    assert m.lines == 2 + 1 + 2
    assert m.needed_pt == pytest.approx(12 + 12 + 24 + 12 + 12)


def test_line_and_paragraph_spacing():
    pct = _shape([_p("a", line_spacing=("pct", 1.5)), _p("b", space_before_pt=6, space_after_pt=4)])
    assert _m(pct).needed_pt == pytest.approx(18 + 6 + 12)
    pts = _shape([_p("a", line_spacing=("pt", 30.0)), _p("b", line_spacing=("pt", 30.0))])
    assert _m(pts).needed_pt == pytest.approx(60)


def test_insets_presets_vertical_text_and_columns_change_the_room():
    assert _m(_shape([_p("a")], insets=(10, 5, 10, 5))).room_w_pt == pytest.approx(180)
    assert _m(_shape([_p("a")], preset="ellipse")).room_w_pt == pytest.approx(200 * 0.7071, rel=1e-3)
    vertical = _m(_shape([_p("a")], width_pt=50, height_pt=300, vertical="vert"))
    assert (vertical.room_w_pt, vertical.room_h_pt) == (pytest.approx(300), pytest.approx(50))
    columns = _m(_shape([_p("a")], columns=2, column_gap=int(20 * EMU_PT)))
    assert (columns.room_w_pt, columns.room_h_pt) == (pytest.approx(90), pytest.approx(200))


def test_a_hanging_bullet_keeps_every_line_at_the_margin():
    para = _p("aaaaaaaaa " * 8, bullet="•", margin_left_emu=int(20 * EMU_PT), indent_emu=-int(20 * EMU_PT))
    m = _m(_shape([para]))
    # 180 pt from the margin holds 36 characters: 3 words a line.
    assert m.lines == 3


def test_a_numbered_paragraph_starts_after_its_label():
    # Four words need 195 pt: one line from the indent (200 pt), two from
    # the margin (180 pt), where a hanging bullet or number puts the text.
    hanging = {"margin_left_emu": int(20 * EMU_PT), "indent_emu": -int(20 * EMU_PT)}
    for bullet in ("•", "autonum:arabicPeriod", "picture"):
        assert _m(_shape([_p("aaaaaaaaa " * 4, bullet=bullet, **hanging)])).lines == 2, bullet


def test_a_label_wider_than_the_indent_pushes_the_first_line():
    # "10." is 15 pt; the 10 pt indent holds "9." (10 pt) but not "10.".
    hanging = {"bullet": "autonum:arabicPeriod", "margin_left_emu": int(10 * EMU_PT), "indent_emu": -int(10 * EMU_PT)}
    items = [_p("a" * 30, **hanging) for _ in range(10)]
    widest = [pptx_measure.lay_out(p, 200.0, widths=_half, number=n)[1] for n, p in enumerate(items, 1)]
    assert widest[8] == pytest.approx(10 + 150) and widest[9] == pytest.approx(15 + 150)


def test_number_labels_and_list_numbers():
    label = pptx_measure.number_label
    assert [label("arabicPeriod", 10), label("romanUcParenR", 4), label("alphaLcParenBoth", 27),
            label("arabicPlain", 3), label("romanLcPeriod", 1994)] == ["10.", "IV)", "(aa)", "3", "mcmxciv."]
    num = {"bullet": "autonum:arabicPeriod"}
    paragraphs = [Paragraph(0, [], **num), Paragraph(1, [], **num), Paragraph(1, [], **num), Paragraph(0, [], **num),
                  Paragraph(0, []), Paragraph(0, [], **num, start_at=5)]
    assert pptx_measure._list_numbers(paragraphs) == [1, 1, 2, 2, 1, 5]


def test_the_shrink_line_reduction_is_subtracted_from_the_spacing():
    spaced = _shape([_p("a", line_spacing=("pct", 1.5))])
    assert _m(spaced, scale=1.0, line_reduction=0.2).needed_pt == pytest.approx(1.2 * 10 * 1.3)


def test_capitals_and_cjk():
    caps = Paragraph(0, [Run("abc def", "Test", 10.0, caps="all")], end_size_pt=10.0)
    assert _m(_shape([caps])).widest_pt == pytest.approx(35)
    cjk = _m(_shape([_p("漢" * 50)], width_pt=100))
    assert cjk.lines == 3


def test_over_is_the_share_past_the_room():
    m = _m(_shape([_p("aaaaaaaaa " * 40)], height_pt=60))
    assert m.lines == 10 and m.over == pytest.approx(120 / 60 - 1)


def test_the_shrink_step_is_the_largest_that_fits():
    shape = _shape([_p("aaaaaaaaa " * 40)], height_pt=60)
    scale, reduction, fits = pptx_measure.fit_scale(shape, widths=_half)
    assert fits and scale < 1 and reduction == 0.2
    bigger = pptx_measure.SCALES[pptx_measure.SCALES.index(scale) - 1]
    assert _m(shape, scale=bigger, line_reduction=0.2 if bigger < 0.9 else 0.1).over > 0
    assert pptx_measure.fit_scale(_shape([_p("a")]), widths=_half) == (1.0, 0.0, True)
    assert pptx_measure.fit_scale(_shape([_p("a " * 5000)], height_pt=20), widths=_half)[2] is False


def test_the_size_that_fits():
    shape = _shape([_p("aaaaaaaaa " * 40, size=10.0)], height_pt=60)
    size = pptx_measure.size_that_fits(shape, widths=_half)
    assert size is not None and size < 10
    assert _m(shape, scale=size / 10, line_reduction=0).over == 0
    assert _m(shape, scale=(size + 0.5) / 10, line_reduction=0).over > 0


def test_headroom_rises_with_capitals_and_serifs():
    plain = _shape([Paragraph(0, [Run("Results", "Calibri", 20.0)])])
    caps = _shape([Paragraph(0, [Run("RESULTS", "Calibri", 20.0)])])
    serif = _shape([Paragraph(0, [Run("Results", "Cambria", 20.0)])])
    assert pptx_measure.headroom(plain) < pptx_measure.headroom(caps) == pytest.approx(1.12)
    assert pptx_measure.headroom(serif) > pptx_measure.headroom(plain)


# ---------------------------------------------------------------------------
# write_pptx stores the scale a shrinking frame needs
# ---------------------------------------------------------------------------


async def _async_ident(p, writing=False, **kw):
    return p


@pytest.fixture
def _platform(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("shared._resolve_path", _async_ident)
    monkeypatch.setattr("shared._push_preview", _noop)
    monkeypatch.setattr("shared._notify_file_written", _noop)
    monkeypatch.setattr("shared._to_agents_relative", lambda p: p)


def _write(path, ops, create_new=True):
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "create_new": create_new, "operations": ops}))


def _fit(shape):
    body = shape.text_frame._bodyPr
    return body.find(qn("a:normAutofit"))


def test_shrink_stores_the_scale_its_text_needs(tmp_path, _platform):
    f = tmp_path / "s.pptx"
    long = "The board approved three new markets and a review of every supplier contract. " * 6
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_textbox", "slide_index": 0, "text": long, "width": 4, "height": 1.5, "font_size": 20,
                      "autofit": "shrink"},
                     {"op": "add_textbox", "slide_index": 0, "text": "Short", "top": 3, "autofit": "shrink"}])
    assert out.startswith("Presentation saved: "), out
    long_box, short_box = Presentation(str(f)).slides[0].shapes
    fit = _fit(long_box)
    assert fit is not None and 25000 <= int(fit.get("fontScale")) < 100000 and fit.get("lnSpcReduction")
    assert _fit(short_box).attrib == {}
    if int(fit.get("fontScale")) < 55000:
        assert "shrinks to" in out or "does not fit" in out


def test_text_too_long_even_shrunk_warns(tmp_path, _platform):
    f = tmp_path / "s.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_textbox", "slide_index": 0, "text": "word " * 2000, "width": 2, "height": 0.5,
                      "autofit": "shrink"}])
    assert "does not fit even shrunk to 25%" in out and "shape_id 2" in out


def test_a_shape_the_batch_did_not_touch_keeps_its_scale(tmp_path, _platform):
    f = tmp_path / "s.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "add_slide"},
               {"op": "add_textbox", "slide_index": 0, "text": "word " * 200, "autofit": "shrink"}])
    prs = Presentation(str(f))
    _fit(prs.slides[0].shapes[0]).set("fontScale", "61000")
    prs.save(str(f))
    _write(f, [{"op": "add_textbox", "slide_index": 1, "text": "other", "autofit": "shrink"}], create_new=False)
    assert _fit(Presentation(str(f)).slides[0].shapes[0]).get("fontScale") == "61000"


def test_a_line_feed_inside_run_text_breaks_the_line():
    shape = _shape([_p("one\ntwo\vthree")])
    assert _m(shape).lines == 3


_LONG_TITLE = "Every region grew its revenue and held its costs through the whole of the year " * 2


def test_a_title_set_by_add_slide_stores_its_shrink(tmp_path, _platform):
    f = tmp_path / "s.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_only", "title": _LONG_TITLE}])
    assert out.startswith("Presentation saved: "), out
    title = Presentation(str(f)).slides[0].shapes.title
    fit = _fit(title)
    assert fit is not None and int(fit.get("fontScale")) < 100000, out
    assert "pptx.overflow" not in out


def test_text_added_by_add_hyperlink_stores_its_shrink(tmp_path, _platform):
    f = tmp_path / "s.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_textbox", "slide_index": 0, "text": "Read", "width": 3, "height": 0.6, "font_size": 20,
                "autofit": "shrink"}])
    out = _write(f, [{"op": "add_hyperlink", "slide_index": 0, "shape_index": 0, "url": "https://example.com",
                      "text": " the whole report on every market we entered this year"}], create_new=False)
    assert out.startswith("Presentation saved: "), out
    fit = _fit(Presentation(str(f)).slides[0].shapes[0])
    assert fit is not None and int(fit.get("fontScale", "100000")) < 100000, out
