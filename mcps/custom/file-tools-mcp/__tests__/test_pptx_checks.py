"""The PowerPoint checks: overflow, the wrap trap, off-slide, covered and
colliding text, low contrast, and (in check_document) leftovers, empty
placeholders and stale slide numbers; each located by the address
write_pptx takes. Glyphs are half the font size wide, so the verdicts hold
on any machine's fonts.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

import checks
import powerpoint as ppt_mod
from formats.pptx import pptx_checks, pptx_measure

CORPUS = Path(__file__).parent / "corpus" / "pptx"


def _half(text, family, size, bold, italic):
    return len(text) * size * 0.5


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)
    monkeypatch.setattr(pptx_measure, "_font_width", _half)


def _write(path, ops, create_new=True) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "operations": ops, "create_new": create_new}))


def _findings(path, check: str) -> list[checks.Finding]:
    with checks.CheckContext(str(path), "pptx", data=Path(path).read_bytes()) as ctx:
        return checks.run_checks(ctx, phase="document", only=[f"pptx.{check}"])


# A text box python-pptx makes grows to its text; these keep their size.
_BOX = {"op": "add_textbox", "slide_index": 0, "left": 1, "top": 1, "width": 4, "height": 1, "font_size": 20,
        "margin": 0, "autofit": "none"}


@pytest.mark.parametrize("ext", [".ppsx", ".ppsm"])
def test_check_document_checks_a_show_write_pptx_saved(tmp_path, ext):
    f = tmp_path / f"show{ext}"
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "word " * 60}])
    out = asyncio.run(checks.handle_check_document({"path": str(f), "checks": ["pptx.overflow"]}))
    assert out.startswith(f"Checked {f} (pptx): 1 checks ran.") and "ERROR pptx.overflow at slide_index 0" in out, out


def test_text_past_its_box_is_an_error_with_the_numbers_and_the_fix(tmp_path):
    f = tmp_path / "o.pptx"
    out = _write(f, [{"op": "add_slide"}, {**_BOX, "text": "word " * 60}])
    assert "ERROR pptx.overflow at slide_index 0, shape_id 2 'TextBox 1'" in out, out
    found = _findings(f, "overflow")[0]
    # A 288 pt line holds five words of 40 pt and their spaces: 60 words make 12 lines of 24 pt.
    assert found.message == "12 lines at 20 pt need 4.00 in; the box holds 1.00 in (300% over)"
    assert "Enlarge the box to 4.00 in high" in found.fix and "font_size" in found.fix
    assert found.excerpt.startswith("word word") and found.part == "ppt/slides/slide1.xml"


def test_a_few_percent_over_is_a_warning(tmp_path):
    f = tmp_path / "o.pptx"
    # Four lines of 24 pt need 96 pt; a box of 93.6 pt is 2.6 % short.
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "w" * 28 * 4, "height": 1.3}])
    found = _findings(f, "overflow")
    assert [x.severity for x in found] == [checks.Severity.WARNING]


def test_a_box_that_grows_to_its_text_is_judged_where_it_ends(tmp_path):
    f = tmp_path / "g.pptx"
    _write(f, [{"op": "add_slide"}, {**_BOX, "top": 5, "text": "word " * 60, "autofit": "resize"}])
    assert _findings(f, "overflow") == []
    found = _findings(f, "off_slide")
    assert len(found) == 1 and "past the slide's bottom edge" in found[0].message


def test_text_that_fits_or_grows_its_box_is_fine(tmp_path):
    f = tmp_path / "o.pptx"
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "Revenue up"},
               {**_BOX, "top": 3, "text": "word " * 60, "autofit": "resize"}])
    assert _findings(f, "overflow") == []


def test_a_stored_shrink_a_little_short_is_information(tmp_path):
    from pptx import Presentation
    from pptx.oxml.ns import qn

    f = tmp_path / "o.pptx"
    # Four lines of 24 pt need 96 pt; at the stored 92.5 % (10 % less
    # spacing) they need 79.9 pt, and the box holds 77.8 pt: 2.7 % short.
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "w" * 28 * 4, "height": 1.08, "autofit": "shrink"}])
    prs = Presentation(str(f))
    fit = prs.slides[0].shapes[0].text_frame._bodyPr.find(qn("a:normAutofit"))
    fit.set("fontScale", "92500")
    fit.set("lnSpcReduction", "10000")
    prs.save(str(f))
    found = _findings(f, "overflow")
    assert [x.severity for x in found] == [checks.Severity.INFO] and "Collabora shrinks it" in found[0].message


def test_a_stored_shrink_far_short_is_a_warning(tmp_path):
    from pptx import Presentation
    from pptx.oxml.ns import qn

    f = tmp_path / "o.pptx"
    out = _write(f, [{"op": "add_slide"}, {**_BOX, "text": "word " * 2000, "autofit": "shrink"}])
    assert "does not fit even shrunk" in out
    found = _findings(f, "overflow")
    assert [x.severity for x in found] == [checks.Severity.WARNING]
    assert "PowerPoint shows it at 25% until the text is edited" in found[0].message
    # A scale left from shorter text: PowerPoint draws it far past the box.
    _write(f, [{"op": "add_slide"}, {**_BOX, "slide_index": 1, "text": "word " * 60, "autofit": "shrink"}],
           create_new=False)
    prs = Presentation(str(f))
    prs.slides[1].shapes[0].text_frame._bodyPr.find(qn("a:normAutofit")).set("fontScale", "92500")
    prs.save(str(f))
    stale = [x for x in _findings(f, "overflow") if x.location.startswith("slide_index 1")]
    assert [x.severity for x in stale] == [checks.Severity.WARNING]


def test_a_line_that_must_not_wrap_but_is_too_wide(tmp_path):
    f = tmp_path / "w.pptx"
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "A heading too long for its box", "word_wrap": False}])
    found = _findings(f, "wrap_trap")
    assert len(found) == 1 and "LibreOffice and Collabora wrap it" in found[0].message
    assert _findings(f, "overflow") == []


def test_a_box_that_does_not_wrap_still_runs_past_its_bottom(tmp_path):
    f = tmp_path / "w.pptx"
    _write(f, [{"op": "add_slide"},
               {**_BOX, "text": "\n".join(f"Line {n}" for n in range(8)), "height": 0.5, "word_wrap": False}])
    found = _findings(f, "overflow")
    assert [x.severity for x in found] == [checks.Severity.ERROR] and found[0].message.startswith("8 lines")


def test_shapes_past_the_edge(tmp_path):
    f = tmp_path / "e.pptx"
    _write(f, [{"op": "add_slide"},
               {**_BOX, "text": "Cut", "left": 11},                      # text to 15 in on a 13.333 in slide
               {"op": "add_shape", "slide_index": 0, "left": 12.5, "top": 6, "width": 2, "height": 1},
               {"op": "add_shape", "slide_index": 0, "left": 20, "top": 1},
               {"op": "add_shape", "slide_index": 0, "left": 12.4, "top": 1, "width": 1, "height": 1}])
    found = {x.location.split("'")[1]: x for x in _findings(f, "off_slide")}
    assert found["TextBox 1"].severity is checks.Severity.ERROR and "4.2 cm past the slide's right edge" in \
        found["TextBox 1"].message
    assert found["Rectangle 2"].severity is checks.Severity.WARNING
    assert "never shows" in found["Rectangle 3"].message
    assert "Rectangle 4" not in found                                   # inside the half-centimetre slack


def test_a_picture_past_the_edge_is_a_note(tmp_path):
    from PIL import Image

    image = tmp_path / "bleed.png"
    Image.new("RGB", (40, 20), "navy").save(image)
    f = tmp_path / "e.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_image", "slide_index": 0, "image_path": str(image), "left": -0.5, "top": 0, "width": 14.5}])
    assert [x.severity for x in _findings(f, "off_slide")] == [checks.Severity.INFO]


def test_a_turned_shape_is_judged_by_its_turned_bounds(tmp_path):
    f = tmp_path / "e.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_shape", "slide_index": 0, "left": 10.5, "top": 3, "width": 4, "height": 0.5,
                "rotation": 90}])
    assert _findings(f, "off_slide") == []


def test_a_later_opaque_shape_over_text_and_text_into_text(tmp_path):
    f = tmp_path / "c.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_shape", "slide_index": 0, "left": 0.9, "top": 0.9, "width": 6, "height": 2,
                "fill_color": "#F2F2F2"},                                  # a card behind: fine
               {**_BOX, "text": "Revenue grew in every region"},
               {"op": "add_shape", "slide_index": 0, "left": 1, "top": 1, "width": 1, "height": 0.5,
                "fill_color": "#C00000"},                                 # on top of the text
               {**_BOX, "top": 1.1, "left": 2, "text": "Costs flat"}])
    found = _findings(f, "overlap")
    messages = sorted(x.message for x in found)
    assert any(m.startswith("shape_id 4 covers") for m in messages)
    assert any(m == "its text runs into the text of shape_id 3" for m in messages)
    assert not any("shape_id 2 covers" in m for m in messages)


def test_grouped_text_does_not_collide_with_itself(tmp_path):
    f = tmp_path / "g.pptx"
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "Label"}, {**_BOX, "left": 1.5, "text": "Value"},
               {"op": "add_group_shape", "slide_index": 0, "shapes": [0, 1]}])
    assert _findings(f, "overlap") == []


def test_faint_text_and_large_text_thresholds(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"},
               {**_BOX, "text": "Faint note", "color": "#BBBBBB", "font_size": 14},
               {**_BOX, "top": 3, "text": "Big", "color": "#888888", "font_size": 40},
               {"op": "add_shape", "slide_index": 0, "left": 6, "top": 1, "width": 4, "height": 1, "text": "Readable",
                "fill_color": "#1F4E79", "font_color": "white"},
               {"op": "add_shape", "slide_index": 0, "left": 6, "top": 3, "width": 4, "height": 1, "text": "Lost",
                "fill_color": "#1F4E79", "font_color": "#203864"}])
    found = {x.location.split("'")[1]: x for x in _findings(f, "contrast")}
    assert set(found) == {"TextBox 1", "Rectangle 4"}
    assert "text #BBBBBB on #FFFFFF has a contrast of 1.9:1; 4.5:1" in found["TextBox 1"].message
    assert found["TextBox 1"].excerpt == "Faint note"


def test_large_text_is_wcags_eighteen_or_fourteen_bold(tmp_path):
    f = tmp_path / "t.pptx"
    # #888888 on white is 3.5:1: enough for large text, not for small.
    _write(f, [{"op": "add_slide"},
               {**_BOX, "text": "Large", "color": "#888888", "font_size": 18},
               {**_BOX, "top": 2.5, "text": "Bold", "color": "#888888", "font_size": 14, "bold": True},
               {**_BOX, "top": 4, "text": "Small", "color": "#888888", "font_size": 16}])
    assert [x.location.split("'")[1] for x in _findings(f, "contrast")] == ["TextBox 3"]


def test_text_on_a_gradient_is_judged_where_it_sits(tmp_path):
    f = tmp_path / "g.pptx"
    # 270 degrees runs bottom to top: navy at the foot, pale blue at the head.
    _write(f, [{"op": "add_slide"},
               {"op": "set_background", "slide_index": 0, "gradient": [{"color": "#0B2545"}, {"color": "#8DA9C4"}]},
               {**_BOX, "top": 0.3, "text": "White near the pale end", "color": "#FFFFFF"},
               {**_BOX, "top": 6.2, "text": "White near the navy end", "color": "#FFFFFF"}])
    found = _findings(f, "contrast")
    assert [x.location.split("'")[1] for x in found] == ["TextBox 1"] and "on the gradient's #" in found[0].message


def test_a_shape_the_tool_draws_reads_on_its_fill(tmp_path):
    f = tmp_path / "d.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_shape", "slide_index": 0, "text": "Approved", "left": 1, "top": 1, "width": 3, "height": 1},
               {"op": "add_shape", "slide_index": 0, "text": "Small print", "left": 5, "top": 1, "width": 3,
                "height": 1, "font_size": 12},
               {"op": "add_shape", "slide_index": 0, "text": "Filled", "left": 9, "top": 1, "width": 3, "height": 1,
                "fill_color": "#1F4E79"}])
    assert _findings(f, "contrast") == []


def test_white_on_the_default_style_of_a_file_made_elsewhere_still_warns(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    # python-pptx's default shape style: a light accent gradient (with its
    # saturation boost) under white text.
    f = tmp_path / "d.pptx"
    _write(f, [{"op": "add_slide"}])
    prs = Presentation(str(f))
    prs.slides[0].shapes.add_shape(1, Inches(1), Inches(1), Inches(3), Inches(1)).text = "Approved"
    prs.save(str(f))
    found = _findings(f, "contrast")
    assert len(found) == 1 and "#FFFFFF on the gradient's #" in found[0].message


def test_leftovers_and_empty_placeholders_are_for_the_finished_deck(tmp_path):
    f = tmp_path / "l.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_and_content", "title": "Dear {{ client }}"},
                     {**_BOX, "top": 5, "text": "a todo list is fine; TBD is not"}])
    assert "pptx.leftover" not in out                                     # final_only: not after a write
    found = sorted((x.location.split("'")[1], x.excerpt or x.message[:20]) for x in _findings(f, "leftover"))
    assert found == [("Content Placeholder 2", "an empty placeholder"), ("TextBox 3", "TBD"),
                     ("Title 1", "{{ client }}")]
    fixes = {x.location.split("'")[1]: x.fix for x in _findings(f, "leftover")}
    assert "remove_shape" not in fixes["Title 1"] and "remove_shape" in fixes["TextBox 3"]


def test_a_placeholder_holding_a_chart_table_picture_or_diagram_is_not_empty(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.shapes.placeholder import (
        ChartPlaceholder,
        PicturePlaceholder,
        TablePlaceholder,
    )

    f = tmp_path / "p.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_and_content", "title": f"Slide {n}"} for n in range(4)])
    prs = Presentation(str(f))
    content = [next(ph for ph in s.placeholders if ph.placeholder_format.idx == 1) for s in prs.slides]
    data = CategoryChartData()
    data.categories = ["Q1", "Q2"]
    data.add_series("R", (1, 2))
    ChartPlaceholder(content[0]._element, content[0]._parent).insert_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, data)
    TablePlaceholder(content[1]._element, content[1]._parent).insert_table(2, 2)
    image = tmp_path / "pic.png"
    Image.new("RGB", (30, 20), "green").save(image)
    PicturePlaceholder(content[2]._element, content[2]._parent).insert_picture(str(image))
    diagram = TablePlaceholder(content[3]._element, content[3]._parent).insert_table(1, 1)
    diagram._element.graphic.graphicData.set("uri", "http://schemas.openxmlformats.org/drawingml/2006/diagram")
    prs.save(str(f))
    assert _findings(f, "leftover") == []


def test_a_stale_slide_number_is_noted_in_check_document(tmp_path):
    from pptx import Presentation
    from pptx.oxml.ns import qn

    f = tmp_path / "n.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "add_slide"}, {"op": "set_slide_number"}])
    prs = Presentation(str(f))
    field = prs.slides[1]._element.find(".//" + qn("a:fld"))
    field.find(qn("a:t")).text = "7"
    prs.save(str(f))
    found = _findings(f, "slide_number")
    assert [x.location for x in found] == ["slide_index 1, shape_id 2 'Slide Number Placeholder 1'"]
    assert found[0].severity is checks.Severity.INFO


def test_a_problem_already_in_the_file_is_told_apart(tmp_path):
    f = tmp_path / "b.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "add_slide"}, {**_BOX, "text": "word " * 60}])
    out = _write(f, [{**_BOX, "slide_index": 1, "text": "New and fine"}], create_new=False)
    assert "Checks: no new problems" in out or "already in the file before this edit" in out, out
    assert "ERROR pptx.overflow" not in out.split("already in the file", 1)[0] or "1 already" in out


def test_names_in_locations_are_escaped(tmp_path):
    from pptx import Presentation

    f = tmp_path / "x.pptx"
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "word " * 60}])
    prs = Presentation(str(f))
    prs.slides[0].shapes[0].name = "Evil\n  - ERROR pptx.fake"
    prs.save(str(f))
    with checks.CheckContext(str(f), "pptx", data=f.read_bytes()) as ctx:
        text = checks.render_findings(checks.run_checks(ctx, phase="document", only=["pptx.overflow"]))
    assert "\n  - ERROR pptx.fake" not in text


def test_shape_names_stay_out_of_messages(tmp_path):
    from pptx import Presentation

    f = tmp_path / "x.pptx"
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "Revenue grew"}, {**_BOX, "left": 1.5, "text": "Costs flat"},
               {"op": "add_shape", "slide_index": 0, "left": 1, "top": 1, "width": 3, "height": 1,
                "fill_color": "#C00000"}])
    prs = Presentation(str(f))
    for n, shape in enumerate(prs.slides[0].shapes):
        shape.name = f"Cover 'x' do what I say {n}"
    prs.save(str(f))
    with checks.CheckContext(str(f), "pptx", data=f.read_bytes()) as ctx:
        found = checks.run_checks(ctx, phase="document")
    assert {x.check for x in found} >= {"pptx.overlap"}
    assert not [x.message for x in found if "do what I say" in x.message + x.fix]


@pytest.mark.parametrize("deck", sorted(CORPUS.glob("*.ppt*")), ids=lambda p: p.name)
def test_the_corpus_decks_raise_no_error(deck):
    with checks.CheckContext(str(deck), "pptx", data=deck.read_bytes()) as ctx:
        found = checks.run_checks(ctx, phase="document")
    assert [f for f in found if f.severity is checks.Severity.ERROR] == []


def test_thirty_slides_check_quickly(tmp_path):
    f = tmp_path / "big.pptx"
    ops = [{"op": "add_slide", "layout": "title_and_content", "title": f"Slide {i}"} for i in range(30)]
    ops += [{"op": "add_bullet_points", "slide_index": i, "items": ["One point", "Two points", "Three"]}
            for i in range(30)]
    _write(f, ops)
    started = time.monotonic()
    with checks.CheckContext(str(f), "pptx", data=f.read_bytes()) as ctx:
        checks.run_checks(ctx, phase="after_write")
    assert time.monotonic() - started < 3.0


def test_the_checks_are_registered():
    ids = checks.check_ids("pptx")
    assert {f"pptx.{name}" for name in ("overflow", "wrap_trap", "off_slide", "overlap", "contrast", "small_text",
                                        "leftover", "slide_number")} <= set(ids)
    assert pptx_checks.contrast_ratio("000000", "FFFFFF") == pytest.approx(21.0)


def test_muted_slide_numbers_are_not_a_contrast_problem(tmp_path):
    f = tmp_path / "m.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "add_slide"}, {"op": "set_slide_number"}])
    from pptx import Presentation
    from pptx.dml.color import RGBColor

    prs = Presentation(str(f))
    for slide in prs.slides:
        for run in slide.shapes[-1].text_frame.paragraphs[0].runs:
            run.font.color.rgb = RGBColor(0xBB, 0xBB, 0xBB)
    prs.save(str(f))
    assert _findings(f, "contrast") == []


def test_small_text(tmp_path):
    f = tmp_path / "s.pptx"
    _write(f, [{"op": "add_slide"},
               {**_BOX, "text": "Nine point body text", "font_size": 9},
               {**_BOX, "top": 2, "text": "Eleven point body text", "font_size": 11},
               {**_BOX, "top": 3, "text": "Twelve is fine", "font_size": 12},
               {**_BOX, "top": 6.6, "height": 0.4, "text": "Source: annual report", "font_size": 9},
               {"op": "add_table", "slide_index": 0, "headers": ["Region", "Revenue"], "rows": [["EMEA", "1.2"]],
                "left": 7, "top": 1, "width": 4, "height": 1},
               {"op": "format_table_cell", "slide_index": 0, "row": 1, "col": 0, "end_col": 1, "font_size": 8},
               {"op": "set_slide_number"}])
    found = {x.location.split("'")[1]: x for x in _findings(f, "small_text")}
    assert set(found) == {"TextBox 1", "TextBox 2", "Table 5"}
    assert found["TextBox 1"].severity is checks.Severity.WARNING and found["TextBox 1"].message.startswith(
        "text at 9 pt")
    assert found["TextBox 2"].severity is checks.Severity.INFO
    assert found["Table 5"].severity is checks.Severity.WARNING and "format_table_cell" in found["Table 5"].fix


def test_text_shrunk_small_is_small(tmp_path):
    f = tmp_path / "s.pptx"
    _write(f, [{"op": "add_slide"}, {**_BOX, "text": "word " * 150, "autofit": "shrink"}])
    found = _findings(f, "small_text")
    assert [x.severity for x in found] == [checks.Severity.WARNING]
