"""write_pptx on the shared batch: every key of the 1.3.1 help still
resolves, unknown keys fail with the fix, a failed operation saves nothing
unless best_effort, what used to do nothing silently now says so, and the
operations the move fixed (freeform shapes, arrow ends, transition order,
theme colours, shapes by id).

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
from pptx.oxml.ns import qn
from pptx.util import Inches

import powerpoint as ppt_mod
from results import Code


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


def _write(path: Path, ops: list, create_new: bool = True, best_effort: bool = False) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx(
        {"path": str(path), "create_new": create_new, "best_effort": best_effort, "operations": ops}))


_DECK = [{"op": "add_slide", "layout": "title_and_content"},
         {"op": "set_text", "slide_index": 0, "text": "Title"},
         {"op": "add_shape", "slide_index": 0, "text": "Card", "left": 1, "top": 5}]


# ---------------------------------------------------------------------------
# Every key of the 1.3.1 help, and the keys its code read, still resolve
# ---------------------------------------------------------------------------

_RUN = {"font_size": 14, "bold": True, "italic": False, "underline": True, "color": "#1F4E79",
        "font_name": "Calibri"}
_TODAY = [
    {"type": "add_slide", "layout": "title"}, {"op": "add_slide", "layout": "image"},
    {"op": "delete_slide", "slide_index": -1}, {"op": "delete_slide", "slide": 0},
    {"op": "duplicate_slide", "slide_index": 0}, {"op": "move_slide", "from_index": -1, "to_index": 0},
    {"op": "set_slide_dimensions", "width": 13.333, "height": 7.5},
    {"op": "set_core_properties", "title": "t", "author": "a", "subject": "s", "keywords": "k", "comments": "c"},
    {"op": "set_text", "slide_index": 0, "placeholder": "title", "text": "x", **_RUN},
    {"op": "set_text", "slide": 0, "placeholder": 1, "runs": [{"text": "x", **_RUN}, {"text": "y", "font_color": "red"},
                                                               {"text": "z", "text_color": "blue"}]},
    {"op": "add_textbox", "slide_index": 0, "text": "x", "left": 1, "top": 1, "width": 6, "height": 1,
     "alignment": "center", "word_wrap": True, "auto_size": True, "margin": 0.1, **_RUN},
    {"op": "add_bullet_points", "slide_index": 0, "items": ["a", {"text": "b", "level": 1}], "font_size": 18,
     "placeholder": "body"},
    {"op": "add_bullet_points", "slide_index": 0, "items": ["a"], "shape_name": "x"},
    {"op": "add_bullet_points", "slide_index": 0, "items": ["a"], "shape_index": 1},
    {"op": "format_text", "slide_index": 0, "find": "x", **_RUN},
    {"op": "add_shape", "slide_index": 0, "shape_type": "chevron", "left": 1, "top": 1, "width": 2, "height": 1,
     "text": "x", "text_alignment": "left", "font_size": 12, "bold": True, "font_color": "white",
     "font_name": "Arial", "fill_color": "#000000", "no_fill": False, "line_color": "red", "line_width": 1,
     "rotation": 10},
    {"op": "add_shape", "slide_index": 0, "text_color": "white", "fill": "red"},
    {"op": "set_shape_format", "slide_index": 0, "shape_name": "x", "fill_color": "red", "no_fill": False,
     "line_color": "red", "line_width": 2, "rotation": 0, "left": 1, "top": 1, "width": 1, "height": 1},
    {"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "fill": "red"},
    {"op": "remove_shape", "slide_index": 0, "shape_index": 0},
    {"op": "add_group_shape", "slide_index": 0, "name": "G"},
    {"op": "add_freeform", "slide_index": 0, "points": [[1, 1], {"x": 2, "y": 2}], "close": True,
     "fill_color": "red", "line_color": "blue"},
    {"op": "add_connector", "slide_index": 0, "start_x": 1, "start_y": 1, "end_x": 2, "end_y": 2,
     "line_color": "red", "line_width": 1, "arrow_start": True, "arrow_end": True},
    {"op": "add_table", "slide_index": 0, "headers": ["a"], "rows": [["1"]], "left": 1, "top": 1, "width": 2,
     "height": 1},
    {"op": "format_table_cell", "slide_index": 0, "table_index": 0, "row": 0, "col": 0, "end_row": 1,
     "end_col": 1, "fill_color": "red", "alignment": "center", "vertical_alignment": "middle",
     "merge_to": {"row": 1, "col": 1}, **_RUN},
    {"op": "format_table_cell", "slide_index": 0, "fill": "red", "font_color": "white"},
    {"op": "format_table_cell", "slide_index": 0, "table_index": -1, "row": -1, "col": -1, "fill_color": "red"},
    {"op": "update_chart_data", "slide_index": 0, "chart_index": -1, "categories": ["a"],
     "series": [{"name": "s", "values": [1]}]},
    {"op": "add_chart", "slide_index": 0, "chart_type": "pie", "categories": ["a"],
     "series": [{"name": "s", "values": [1], "color": "red"}], "colors": ["red"], "title": "t",
     "has_legend": False, "left": 1, "top": 1, "width": 4, "height": 3},
    {"op": "add_chart", "slide_index": 0, "chart_type": "scatter", "series": [{"name": "s", "x": [1], "y": [2]}]},
    {"op": "update_chart_data", "slide_index": 0, "chart_index": 0, "categories": ["a"],
     "series": [{"name": "s", "values": [1]}]},
    {"op": "add_image", "slide_index": 0, "image_path": "a.png", "left": 1, "top": 1, "width": 2, "height": 1},
    {"op": "add_equation", "slide_index": 0, "latex": "x", "left": 1, "top": 1, "height": 0.8, "width": 2},
    {"op": "add_equation", "slide_index": 0, "equation": "x"},
    {"op": "set_background", "slide_index": 0, "color": "red"},
    {"op": "set_background", "slide_index": 0, "gradient": [{"color": "red", "position": 0}, {"color": "blue"}],
     "angle": 90},
    {"op": "set_background", "slide_index": 0, "gradient": [{"position": 0}, {"color": "blue"}]},
    {"op": "update_chart_data", "slide_index": 0, "categories": ["a"],
     "series": [{"name": "s", "values": [2], "color": "red"}]},
    {"op": "set_background_color", "slide_index": 0, "color": "red"},
    {"op": "set_transition", "slide_index": 0, "transition": "push", "duration": 400, "advance_after": 3000},
    {"op": "set_transition", "slide_index": 0, "effect": "wipe"},
    {"op": "set_transition", "slide_index": 0, "name": "fade"},
    {"op": "set_transition", "slide_index": 0, "transition": {"type": "fade", "duration": 500,
                                                               "advance_after": 1000}},
    {"op": "set_speaker_notes", "slide_index": 0, "text": "n", "append": True},
    {"op": "add_hyperlink", "slide_index": 0, "shape_name": "x", "url": "https://example.com", "text": "t",
     "color": "blue"},
    {"op": "set_slide_number", "enabled": False},
]


@pytest.mark.parametrize("op", _TODAY, ids=lambda op: op.get("op") or op.get("type"))
def test_every_documented_key_still_resolves(op):
    ops, problems, _ = ppt_mod.CATALOG.resolve_batch([(0, op)])
    assert problems == [] and len(ops) == 1, [p.message for p in problems]


def test_the_test_suites_op_lists_replay_with_no_unknown_op_or_key():
    """The op lists the pptx tests send (gathered from their source)."""
    import ast

    folder = Path(__file__).parent
    found = 0
    for test_file in sorted(folder.glob("test_pptx_*.py")):
        if test_file.name == Path(__file__).name:
            continue
        for node in ast.walk(ast.parse(test_file.read_text())):
            if isinstance(node, ast.Dict) and node.keys and all(isinstance(k, ast.Constant) for k in node.keys):
                keys = [k.value for k in node.keys]
                if keys[0] in ("op", "type") and isinstance(node.values[0], ast.Constant):
                    try:
                        op = ast.literal_eval(node)
                    except ValueError:
                        continue
                    if not ppt_mod.CATALOG.lookup(op.get("op") or op.get("type")):
                        continue
                    found += 1
                    _, problems, _ = ppt_mod.CATALOG.resolve_batch([(0, op)])
                    assert [p for p in problems if p.code in (Code.UNKNOWN_OP, Code.UNKNOWN_KEY)] == [], op
    assert found > 50


# ---------------------------------------------------------------------------
# Atomic batches, unknown keys, best_effort
# ---------------------------------------------------------------------------


_OPS_BEFORE_THE_SPLIT = (
    "add_slide", "delete_slide", "duplicate_slide", "move_slide", "set_slide_dimensions", "set_core_properties",
    "set_text", "add_textbox", "add_bullet_points", "format_text", "add_shape", "set_shape_format", "remove_shape",
    "add_group_shape", "add_freeform", "add_connector", "add_table", "format_table_cell", "add_chart",
    "update_chart_data", "add_image", "add_equation", "set_background", "set_transition", "set_speaker_notes",
    "add_hyperlink", "set_slide_number",
)


def test_the_operations_keep_their_order_in_the_help():
    """The catalogue gathers operations from several modules; the help and
    the unknown-op list keep their order (new ones fit in between)."""
    names = list(ppt_mod.CATALOG.ops)
    positions = [names.index(name) for name in _OPS_BEFORE_THE_SPLIT]
    assert positions == sorted(positions)


def test_an_unknown_key_fails_with_the_fix_before_any_work(tmp_path):
    f = tmp_path / "k.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "add_textbox", "slide_index": 0, "text": "x", "font_colour": "red"}])
    assert out.startswith(f"Nothing saved: {f} was not touched.")
    assert "unknown key 'font_colour' (Did you mean 'font_color'?)" in out or "font_colour" in out
    assert not f.exists()


def test_a_failed_op_saves_nothing_and_best_effort_keeps_the_rest(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _DECK)
    before = f.read_bytes()
    ops = [{"op": "set_text", "slide_index": 0, "text": "Changed"},
           {"op": "set_text", "slide_index": 4, "text": "Nowhere"}]
    out = _write(f, ops, create_new=False)
    assert out.startswith("Nothing saved: ") and "slide_index 4 does not exist (the deck has 1 slide)" in out
    assert f.read_bytes() == before
    out = _write(f, ops, create_new=False, best_effort=True)
    assert out.startswith("Presentation saved: ") and "1 failed" in out
    assert Presentation(str(f)).slides[0].shapes.title.text == "Changed"


def test_what_did_nothing_silently_now_says_so(tmp_path):
    f = tmp_path / "s.pptx"
    _write(f, _DECK)
    out = _write(f, [{"op": "delete_slide", "slide_index": 3}], create_new=False)
    assert "slide_index 3 does not exist" in out and out.startswith("Nothing saved: ")
    out = _write(f, [{"op": "add_slide", "layout": "blank"},
                     {"op": "set_text", "slide_index": 1, "text": "x"}], create_new=False)
    assert "the slide has no placeholder 0" in out and "Use add_textbox" in out
    out = _write(f, [{"op": "format_text", "slide_index": 0, "find": "absent", "bold": True}], create_new=False)
    assert "Changed nothing:" in out and "no text run holds 'absent'" in out
    assert out.startswith("Nothing saved: ")


def test_format_text_can_target_one_shape(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_and_content"},
               {"op": "set_text", "slide_index": 0, "placeholder": "title", "text": "Harbour plan"},
               {"op": "set_text", "slide_index": 0, "placeholder": "body", "text": "Harbour works start in May"}])
    deck = Presentation(str(f))
    title, body = deck.slides[0].shapes.title, deck.slides[0].placeholders[1]
    out = _write(f, [{"op": "format_text", "slide_index": 0, "shape_id": title.shape_id, "bold": True},
                     {"op": "format_text", "slide_index": 0, "shape_name": body.name, "find": "May",
                      "italic": True}], create_new=False)
    assert out.startswith("Presentation saved: "), out
    slide = Presentation(str(f)).slides[0]
    assert all(r.font.bold for p in slide.shapes.title.text_frame.paragraphs for r in p.runs)
    body_runs = [r for p in slide.placeholders[1].text_frame.paragraphs for r in p.runs]
    assert not any(r.font.bold for r in body_runs)
    out = _write(f, [{"op": "format_text", "slide_index": 0, "shape_id": title.shape_id, "find": "May",
                      "bold": True}], create_new=False)
    assert "Changed nothing:" in out and "holds 'May'" in out and title.name in out, out
    out = _write(f, [{"op": "format_text", "slide_index": 0, "shape_id": 999, "bold": True}], create_new=False)
    assert out.startswith("Nothing saved: ") and "999" in out, out


def test_format_text_names_its_shape_keys_in_help():
    names = {k.name for k in ppt_mod.CATALOG.ops["format_text"].keys}
    assert {"shape_id", "shape_name", "find"} <= names


def test_set_text_reads_an_unknown_word_as_the_body_and_keeps_paragraphs(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_and_content"},
                     {"op": "set_text", "slide_index": 0, "placeholder": "text", "text": "one\ntwo", "bold": True}])
    assert out.startswith("Presentation saved: ") and "read as the body" in out
    body = Presentation(str(f)).slides[0].placeholders[1].text_frame
    assert [p.text for p in body.paragraphs] == ["one", "two"]
    assert all(r.font.bold for p in body.paragraphs for r in p.runs)


# ---------------------------------------------------------------------------
# Shapes by id, theme colours, lengths with units
# ---------------------------------------------------------------------------


def test_a_shape_is_found_by_its_id_inside_a_group(tmp_path):
    f = tmp_path / "g.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_shape", "slide_index": 0, "text": "A"}, {"op": "add_shape", "slide_index": 0, "text": "B"},
               {"op": "add_group_shape", "slide_index": 0, "shapes": ["A", "B"]}])
    group = Presentation(str(f)).slides[0].shapes[0]
    child_id = group.shapes[1].shape_id
    out = _write(f, [{"op": "set_shape_format", "slide_index": 0, "shape_id": child_id, "fill_color": "#00FF00"}],
                 create_new=False)
    assert out.startswith("Presentation saved: "), out
    child = Presentation(str(f)).slides[0].shapes[0].shapes[1]
    assert str(child.fill.fore_color.rgb) == "00FF00"
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_id": 999}], create_new=False)
    assert "no shape has id 999" in out and "Shapes on this slide (shape_index, id, name): 0 id" in out


def test_theme_colours_stay_theme_colours(tmp_path):
    f = tmp_path / "c.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_shape", "slide_index": 0, "fill_color": {"theme": "accent2", "tint": 0.4},
                "text": "x", "font_color": "lt1"}])
    sp = Presentation(str(f)).slides[0].shapes[0]._element
    fill = sp.find(qn("p:spPr")).find(qn("a:solidFill"))[0]
    assert fill.tag == qn("a:schemeClr") and fill.get("val") == "accent2"
    assert fill.find(qn("a:lumMod")) is not None and fill.find(qn("a:lumOff")) is not None
    run_color = sp.find(".//" + qn("a:rPr")).find(qn("a:solidFill"))[0]
    assert run_color.get("val") == "bg1"


def test_lengths_take_units(tmp_path):
    f = tmp_path / "u.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_textbox", "slide_index": 0, "text": "x", "left": "2.54cm", "top": "72pt", "width": 3}])
    box = Presentation(str(f)).slides[0].shapes[0]
    assert (box.left, box.top, box.width) == (Inches(1), Inches(1), Inches(3))


# ---------------------------------------------------------------------------
# What the move fixed
# ---------------------------------------------------------------------------


def test_a_freeform_is_drawn(tmp_path):
    f = tmp_path / "f.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_freeform", "slide_index": 0, "points": [[1, 1], [3, 1], {"x": 2, "y": 2.5}],
                      "fill_color": "#E8EEF5"}])
    assert out.startswith("Presentation saved: "), out
    shape = Presentation(str(f)).slides[0].shapes[0]
    assert shape._element.find(".//" + qn("a:custGeom")) is not None
    assert (shape.left, shape.top, shape.width) == (Inches(1), Inches(1), Inches(2))
    out = _write(f, [{"op": "add_freeform", "slide_index": 0, "points": [[1, 1], ["x"]]}], create_new=False)
    assert "points[1] is not a point" in out


def test_both_arrow_ends_are_written_in_schema_order(tmp_path):
    f = tmp_path / "c.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_connector", "slide_index": 0, "arrow_end": True, "arrow_start": True,
                "line_color": "red"}])
    ln = Presentation(str(f)).slides[0].shapes[0]._element.find(".//" + qn("a:ln"))
    assert [c.tag.rsplit("}", 1)[-1] for c in ln] == ["solidFill", "headEnd", "tailEnd"]


def test_a_transition_goes_before_the_slides_timing(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}])
    prs = Presentation(str(f))
    etree.SubElement(prs.slides[0]._element, qn("p:timing"))
    prs.save(str(f))
    _write(f, [{"op": "set_transition", "slide_index": 0, "transition": "push"}], create_new=False)
    tags = [c.tag.rsplit("}", 1)[-1] for c in Presentation(str(f)).slides[0]._element]
    assert tags[-2:] == ["transition", "timing"]


def test_an_unknown_transition_or_shape_names_the_choices(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "set_transition", "slide_index": 0, "transition": "fdae"}])
    assert "unknown transition 'fdae'" in out and "Did you mean 'fade'?" in out
    out = _write(f, [{"op": "add_slide"}, {"op": "add_shape", "slide_index": 0, "shape_type": "Rounded Rectangle"}])
    assert out.startswith("Presentation saved: "), out


def test_add_slide_sets_the_title_it_is_given(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_only", "title": "Results"},
                     {"op": "add_slide", "layout": "blank", "title": "Lost"}])
    assert out.startswith("Presentation saved: ") and "has no title placeholder" in out
    assert Presentation(str(f)).slides[0].shapes.title.text == "Results"


def test_a_core_with_no_operations_creates_the_deck(tmp_path):
    f = tmp_path / "empty.pptx"
    out = _write(f, [])
    assert out.startswith(f"Presentation saved: {f} (0 operations applied")
    assert (Presentation(str(f)).slide_width, Presentation(str(f)).slide_height) == (12192000, 6858000)


def test_macros_removed_under_a_pptx_name_are_a_warning(tmp_path):
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI

    from formats.pptx import pptx_deck

    src = tmp_path / "m.pptm"
    prs = pptx_deck.new_presentation()
    part = Part(PackURI("/ppt/vbaProject.bin"), "application/vnd.ms-office.vbaProject", prs.part.package, b"x")
    prs.part.relate_to(part, pptx_deck._VBA_PROJECT)
    with open(src, "wb") as fh:
        pptx_deck.save_presentation(prs, fh, str(src))
    target = tmp_path / "m.pptx"
    target.write_bytes(src.read_bytes())
    out = _write(target, [{"op": "add_slide"}], create_new=False)
    assert "Warnings:" in out and "the macros were removed" in out


def test_a_gradient_background_sits_where_the_schema_puts_it(tmp_path):
    f = tmp_path / "g.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "set_background", "slide_index": 0, "angle": 90, "gradient": [
                         {"color": "#1F4E79"}, {"color": {"theme": "accent1", "tint": 0.4}, "position": 0.6},
                         {"color": "white"}]},
                     {"op": "set_background", "slide_index": 0,
                      "gradient": [{"color": "#000000"}, {"color": "#FFFFFF"}]}])
    assert out.startswith("Presentation saved: "), out
    c_sld = Presentation(str(f)).slides[0]._element.find(qn("p:cSld"))
    assert [c.tag.rsplit("}", 1)[-1] for c in c_sld][:2] == ["bg", "spTree"]
    bg_pr = c_sld.find(qn("p:bg")).find(qn("p:bgPr"))
    assert [c.tag.rsplit("}", 1)[-1] for c in bg_pr] == ["gradFill", "effectLst"]
    stops = bg_pr.find(qn("a:gradFill")).find(qn("a:gsLst"))
    assert [gs.get("pos") for gs in stops] == ["0", "100000"]     # the second call replaced the first
    from formats.pptx.pptx_geometry import load_geometry
    assert load_geometry(str(f)).slides[0].background == "gradient"


def test_a_theme_stop_stays_a_theme_colour(tmp_path):
    f = tmp_path / "g.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "set_background", "slide_index": 0,
                "gradient": [{"color": "accent1"}, {"color": {"theme": "lt1", "shade": 0.1}}]}])
    stops = Presentation(str(f)).slides[0]._element.find(".//" + qn("a:gsLst"))
    first, second = (gs[0] for gs in stops)
    assert (first.tag, first.get("val")) == (qn("a:schemeClr"), "accent1")
    assert second.get("val") == "bg1" and second.find(qn("a:lumMod")).get("val") == "90000"


def test_add_slide_help_states_the_order_layouts_are_found_in():
    text = ppt_mod.CATALOG.help("add_slide")
    assert "its exact name; then one of the seven words by the layout's kind" in text


def test_minus_one_names_the_last_table_chart_row_and_column(tmp_path):
    f = tmp_path / "m.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_table", "slide_index": 0, "headers": ["a", "b"], "rows": [["1", "2"]]},
                     {"op": "add_table", "slide_index": 0, "headers": ["c", "d"], "rows": [["3", "4"]], "top": 5},
                     {"op": "add_chart", "slide_index": 0, "categories": ["Q1"], "series": [{"name": "R", "values": [1]}]},
                     {"op": "format_table_cell", "slide_index": 0, "table_index": -1, "row": -1, "col": -1,
                      "fill_color": "#FF0000"},
                     {"op": "update_chart_data", "slide_index": 0, "chart_index": -1, "categories": ["Q1", "Q2"],
                      "series": [{"name": "R", "values": [5, 6]}]}])
    assert out.startswith("Presentation saved: ") and "Failed:" not in out, out
    shapes = Presentation(str(f)).slides[0].shapes
    second = [s for s in shapes if s.has_table][1].table
    assert str(second.cell(1, 1).fill.fore_color.rgb) == "FF0000"
    assert list(next(s for s in shapes if s.has_chart).chart.plots[0].categories) == ["Q1", "Q2"]
    out = _write(f, [{"op": "format_table_cell", "slide_index": 0, "table_index": -3, "fill_color": "red"}],
                 create_new=False)
    assert "table_index -3 does not exist (the slide has 2 tables)" in out


def test_line_width_zero_removes_the_outline(tmp_path):
    f = tmp_path / "l.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "add_shape", "slide_index": 0, "line_width": 0},
                     {"op": "add_shape", "slide_index": 0, "line_width": 2},
                     {"op": "add_connector", "slide_index": 0, "line_width": "0pt"}])
    assert out.startswith("Presentation saved: "), out
    first, second, connector = Presentation(str(f)).slides[0].shapes
    assert first._element.find(qn("p:spPr")).find(qn("a:ln")).find(qn("a:noFill")) is not None
    assert second.line.width == 25400
    assert connector._element.find(qn("p:spPr")).find(qn("a:ln")).find(qn("a:noFill")) is not None
    out = _write(f, [{"op": "add_shape", "slide_index": 0, "line_width": -1}], create_new=False)
    assert "line_width -1 is out of range" in out


def test_update_chart_data_takes_series_and_slice_colours(tmp_path):
    f = tmp_path / "c.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_chart", "slide_index": 0, "categories": ["Q1"], "series": [{"name": "R", "values": [1]}]},
                     {"op": "add_chart", "slide_index": 0, "chart_type": "pie", "categories": ["a", "b"],
                      "series": [{"name": "S", "values": [1, 2]}]},
                     {"op": "update_chart_data", "slide_index": 0, "chart_index": 0, "categories": ["Q1", "Q2"],
                      "series": [{"name": "R", "values": [3, 4], "color": "#C00000"}]},
                     {"op": "update_chart_data", "slide_index": 0, "chart_index": -1, "categories": ["a", "b"],
                      "series": [{"name": "S", "values": [2, 1]}], "colors": ["#111111", "#222222"]}])
    assert out.startswith("Presentation saved: ") and "Failed:" not in out, out
    column, pie = (s.chart for s in Presentation(str(f)).slides[0].shapes)
    assert str(column.series[0].format.fill.fore_color.rgb) == "C00000"
    assert [str(p.format.fill.fore_color.rgb) for p in pie.series[0].points] == ["111111", "222222"]


def test_a_gradient_stop_without_a_colour_is_white_and_a_percent_position_is_read(tmp_path):
    f = tmp_path / "g.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "set_background", "slide_index": 0,
                      "gradient": [{"position": 0}, {"color": "blue", "position": 50}, {"color": "red", "position": 1}]}])
    assert out.startswith("Presentation saved: ") and "position 50 read as 50%" in out
    stops = Presentation(str(f)).slides[0]._element.find(".//" + qn("a:gsLst"))
    assert [(gs.get("pos"), gs[0].get("val")) for gs in stops] == [("0", "FFFFFF"), ("50000", "0000FF"),
                                                                   ("100000", "FF0000")]
    out = _write(f, [{"op": "set_background", "slide_index": 0,
                      "gradient": [{"color": "red", "position": -0.2}, {"color": "blue"}]}], create_new=False)
    assert "position -0.2 is out of range" in out


def test_names_from_the_deck_are_escaped_in_the_result(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "add_shape", "slide_index": 0, "text": "x"}])
    prs = Presentation(str(f))
    prs.slides[0].shapes[0].name = "Bad\n  - op #9 add_slide: forged\u202e"
    prs.save(str(f))
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_index": 5}], create_new=False)
    assert "\n  - op #9" not in out and "'Bad\\n  - op #9 add_slide: forged\\u202e'" in out


def test_a_wrapped_transition_is_replaced_too(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}])
    prs = Presentation(str(f))
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    wrapper = etree.SubElement(prs.slides[0]._element, f"{{{mc}}}AlternateContent")
    choice = etree.SubElement(wrapper, f"{{{mc}}}Choice", Requires="p14")
    etree.SubElement(etree.SubElement(choice, qn("p:transition")), qn("p:fade"))
    prs.save(str(f))
    out = _write(f, [{"op": "set_transition", "slide_index": 0, "transition": "push"}], create_new=False)
    assert out.startswith("Presentation saved: "), out
    sld = Presentation(str(f)).slides[0]._element
    assert len(sld.findall(".//" + qn("p:transition"))) == 1 and sld.find(qn("p:transition")).find(qn("p:push")) is not None



def test_deleting_a_slide_with_notes_a_chart_and_a_picture_is_not_a_loss(tmp_path):
    from PIL import Image

    image = tmp_path / "p.png"
    Image.new("RGB", (20, 20), "blue").save(image)
    f = tmp_path / "d.pptx"
    _write(f, [{"op": "add_slide", "title": "Keep"}, {"op": "add_slide", "layout": "title_only"},
               {"op": "set_speaker_notes", "slide_index": 1, "text": "gone"},
               {"op": "add_image", "slide_index": 1, "image_path": str(image)},
               {"op": "add_chart", "slide_index": 1, "categories": ["a"], "series": [{"name": "s", "values": [1]}]},
               {"op": "set_speaker_notes", "slide_index": 0, "text": "kept"}])
    out = _write(f, [{"op": "delete_slide", "slide_index": 1}], create_new=False)
    assert out.startswith("Presentation saved: ") and "Checks: no problems found." in out, out
    out = _write(f, [{"op": "delete_slide", "slide_index": 0}], create_new=False)
    assert "Checks: no problems found." in out, out
