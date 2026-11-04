"""write_docx on the shared batch: every operation and key of the 1.3.1 tool
still works, unknown ones fail before any work, a failure saves nothing
unless best_effort, and values take units and theme colours."""

import asyncio
import re
import zipfile

import pytest

pytest.importorskip("docx")

import word


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    """Paths resolve to themselves and the proxy hooks stay silent."""
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops, create_new=False, best_effort=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops,
                                               "create_new": create_new, "best_effort": best_effort}))


def _png(path) -> str:
    from PIL import Image

    Image.new("RGB", (8, 8), "navy").save(path)
    return str(path)


def _old_tool_ops(image: str) -> list[dict]:
    """Every operation with every key the 1.3.1 help named, plus the keys its
    code read without naming them (equation, header.background and
    header.color, stripe_colors), spelled as agents wrote them then."""
    return [
        {"op": "add_heading", "text": "Title", "level": 1},
        {"type": "add_paragraph", "text": "Body", "bold": True, "italic": True, "underline": True,
         "strikethrough": True, "color": "#1F4E79", "font_size": 11, "font_name": "Calibri",
         "alignment": "justify", "space_before": 6, "space_after": 6, "line_spacing": 1.5,
         "first_line_indent": 0.5, "left_indent": 0.25, "keep_together": True,
         "keep_with_next": False, "widow_control": True},
        {"action": "add_paragraph", "runs": [
            {"text": "Mixed ", "bold": True, "italic": False, "underline": False, "strikethrough": False,
             "color": "C00000", "font_size": 12, "font_name": "Arial"},
            {"equation": "x^2"}]},
        {"op": "insert_paragraph", "index": 0, "text": "First", "bold": True, "italic": True,
         "underline": True, "strikethrough": False, "color": "00FF00", "font_size": 9,
         "font_name": "Arial", "alignment": "center"},
        {"op": "set_paragraph_format", "index": 0, "alignment": "right", "space_before": 2,
         "space_after": 2, "line_spacing": 1.0, "first_line_indent": 0, "left_indent": 0,
         "keep_together": False, "keep_with_next": True, "widow_control": False},
        {"op": "add_equation", "latex": r"\frac{a}{b}", "alignment": "center"},
        {"op": "add_equation", "equation": "a+b"},
        {"op": "add_list", "items": ["a", {"text": "b", "level": 2}], "ordered": True, "level": 1},
        {"op": "add_table", "headers": ["A", "B"], "rows": [["1", "2"], ["3", "4"]], "style": "Table Grid"},
        {"op": "merge_table_cells", "table_index": 0, "start_row": 1, "start_col": 0, "end_row": 2,
         "end_col": 0},
        {"op": "add_table_row", "table_index": -1, "values": ["5", "6"]},
        {"op": "delete_table_row", "table_index": -1, "row_index": -1},
        {"op": "set_table_column_widths", "table_index": -1, "widths": [3, 3]},
        {"op": "style_table", "table_index": -1, "header": {"fill": "1F4E79", "font_color": "FFFFFF",
                                                            "bold": True},
         "alternating_colors": ["FFFFFF", "F2F2F2"], "row_fill": {"1": "FFFF00"},
         "border": {"color": "808080", "size": 4}},
        {"op": "style_table", "header": {"background": "#DDDDDD", "color": "#000000"},
         "stripe_colors": ["#FFFFFF", "#EEEEEE"], "border": True},
        {"op": "add_image", "image_path": image, "width_inches": 1.0, "height_inches": 1.0,
         "alignment": "center"},
        {"op": "add_section_break", "orientation": "landscape", "margin_top": 1, "margin_bottom": 1,
         "margin_left": 1, "margin_right": 1},
        {"op": "set_page_setup", "orientation": "landscape", "margin_top": 0.75, "margin_bottom": 0.75,
         "margin_left": 0.75, "margin_right": 0.75},
        {"op": "add_header", "text": "Header", "bold": True, "alignment": "right", "section_index": -1},
        {"op": "add_footer", "runs": [{"text": "Footer", "italic": True}], "alignment": "left"},
        {"op": "add_page_number", "position": "center", "format": "{page} of {total}", "font_size": 9,
         "section_index": -1},
        {"op": "add_hyperlink", "url": "https://example.com", "text": "Example", "color": "0563C1",
         "underline": True, "paragraph_index": 1},
        {"op": "add_hyperlink", "url": "https://example.org"},
        {"op": "add_table_of_contents", "title": "Contents", "levels": 3},
        {"op": "search_and_replace", "find": "Body", "replace": "Text", "match_case": False},
        {"op": "add_page_break"},
        {"op": "add_paragraph", "text": "after the break"},
    ]


def test_every_old_operation_and_key_still_works(tmp_path):
    f = tmp_path / "replay.docx"
    msg = _write(f, _old_tool_ops(_png(tmp_path / "logo.png")))
    assert msg.startswith(f"Document saved: {f} (27 operations applied: "), msg
    assert "Failed:" not in msg and "unknown" not in msg


def test_a_failure_saves_nothing_unless_best_effort(tmp_path):
    f = tmp_path / "atomic.docx"
    _write(f, [{"op": "add_paragraph", "text": "base"}])
    before = f.read_bytes()
    ops = [{"op": "add_paragraph", "text": "new"}, {"op": "delete_paragraph", "index": 40}]
    msg = _write(f, ops)
    assert msg.startswith("Nothing saved:") and "op #1 delete_paragraph" in msg
    assert "paragraph index 40 does not exist (the document has 2 paragraphs" in msg
    assert f.read_bytes() == before
    msg = _write(f, ops, best_effort=True)
    assert "1 failed and may be partly applied" in msg
    from docx import Document

    assert [p.text for p in Document(str(f)).paragraphs] == ["base", "new"]


def test_unknown_keys_and_operations_fail_before_any_work(tmp_path):
    f = tmp_path / "typo.docx"
    msg = _write(f, [{"op": "add_paragraph", "text": "x", "font_colour": "red"},
                     {"op": "add_paragrph", "text": "y"}])
    assert msg.startswith("Nothing saved:") and not f.exists()
    assert "unknown key 'font_colour' (Did you mean 'color'?)" in msg
    assert "add_paragrph" in msg and "add_paragraph" in msg


def test_lengths_take_units_and_colours_take_theme_slots(tmp_path):
    from docx import Document

    f = tmp_path / "units.docx"
    msg = _write(f, [
        {"op": "add_paragraph", "text": "Theme", "color": {"theme": "accent1", "shade": 0.25},
         "space_after": "0.5cm", "left_indent": "2.54cm", "line_spacing": "18pt", "font_size": "10.5pt"},
        {"op": "add_paragraph", "text": "Named", "font_color": "darkred", "line_spacing": "150%"},
    ])
    assert "Failed:" not in msg
    doc = Document(str(f))
    p0, p1 = doc.paragraphs
    assert round(p0.paragraph_format.left_indent.inches, 3) == 1.0
    assert round(p0.paragraph_format.space_after.pt, 1) == 14.2
    assert round(p0.paragraph_format.line_spacing.pt) == 18
    assert p0.runs[0].font.size.pt == 10.5
    with zipfile.ZipFile(f) as zf:
        xml = zf.read("word/document.xml").decode()
    assert 'w:val="3B618E" w:themeColor="accent1" w:themeShade="BF"' in xml
    assert p1.runs[0].font.color.rgb == (0x8B, 0x00, 0x00)
    assert p1.paragraph_format.line_spacing == 1.5


@pytest.mark.parametrize("ops, message", [
    ([{"op": "add_paragraph", "text": "x", "alignment": "middle"}],
     "alignment 'middle' is not one of: left, center, right, justify"),
    ([{"op": "add_table", "rows": [["1"]]}], "a table needs at least one header cell"),
    ([{"op": "add_heading", "text": "x", "level": 12}], "level 12 is not a heading level"),
    ([{"op": "add_header", "text": "x", "section_index": 3}], "section_index 3 does not exist"),
    ([{"op": "delete_table_row"}], "the document has no table"),
])
def test_bad_values_fail_with_the_fix(tmp_path, ops, message):
    msg = _write(tmp_path / "bad.docx", ops)
    assert msg.startswith("Nothing saved:") and message in msg


def test_a_missing_table_style_falls_back_with_a_warning(tmp_path):
    from docx import Document

    f = tmp_path / "style.docx"
    msg = _write(f, [{"op": "add_table", "headers": ["A"], "rows": [["1", "extra"]], "style": "No Such"}])
    assert "the document has no table style 'No Such'; the table uses 'Table Grid'." in msg
    assert "1 cell past the 1 header columns were left out." in msg
    assert Document(str(f)).tables[0].style.name == "Table Grid"


def test_search_without_a_match_changes_nothing(tmp_path):
    f = tmp_path / "find.docx"
    _write(f, [{"op": "add_paragraph", "text": "Hello"}])
    before = f.read_bytes()
    msg = _write(f, [{"op": "search_and_replace", "find": "absent", "replace": "x"}])
    assert "'absent' was not found" in msg and f.read_bytes() == before


def test_a_trailing_page_break_warns(tmp_path):
    msg = _write(tmp_path / "pb.docx", [{"op": "add_paragraph", "text": "x"}, {"op": "add_page_break"}])
    assert "leaves a blank page at the end" in msg


def test_help_for_one_operation_shows_its_example():
    out = asyncio.run(word.handle_write_docx({"path": "never.docx", "operations": [
        {"op": "help", "name": "set_page_setup"}]}))
    assert out.startswith("set_page_setup: ") and "page_size" in out and "example:" in out


# ---------------------------------------------------------------------------
# search_and_replace and fill_template
# ---------------------------------------------------------------------------

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _runs_doc(tmp_path, runs_xml: str, name="runs.docx"):
    from docx import Document
    from docx.oxml import parse_xml

    doc = Document()
    doc.element.body.insert(0, parse_xml(f"<w:p {W}>{runs_xml}</w:p>"))
    f = tmp_path / name
    doc.save(str(f))
    return f


def _r(text: str, props: str = "") -> str:
    return f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def test_replace_every_occurrence_across_runs_keeping_formatting(tmp_path):
    from docx import Document

    f = _runs_doc(tmp_path, _r("Pay the ", "<w:i/>") + _r("cli", "<w:b/>") + _r("ent", "<w:u w:val=\"single\"/>")
                  + _r(" and the client.", "<w:i/>"))
    msg = _write(f, [{"op": "search_and_replace", "find": "client", "replace": "customer"}])
    assert "2 replaced: 2 in the body" in msg
    runs = Document(str(f)).paragraphs[0].runs
    assert "".join(r.text for r in runs) == "Pay the customer and the customer."
    assert runs[0].italic and runs[1].bold and runs[1].text == "customer"


def test_case_insensitive_replace_is_literal(tmp_path):
    f = _runs_doc(tmp_path, _r("Total: (x) TOTAL"))
    _write(f, [{"op": "search_and_replace", "find": "total", "replace": r"\1 sum", "match_case": False}])
    from docx import Document

    assert Document(str(f)).paragraphs[0].text == r"\1 sum: (x) \1 sum"


def test_replace_reaches_every_header_and_adds_no_part(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("Acme in the body")
    section = doc.sections[0]
    section.different_first_page_header_footer = True
    section.first_page_header.paragraphs[0].text = "Acme cover"
    section.header.paragraphs[0].text = "Acme running"
    f = tmp_path / "heads.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "search_and_replace", "find": "Acme", "replace": "Globex"}])
    assert "3 replaced: 1 in the body, 2 in headers" in msg
    bare = tmp_path / "bare.docx"
    Document().save(str(bare))
    _write(bare, [{"op": "add_paragraph", "text": "x"}])
    with zipfile.ZipFile(bare) as zf:
        before = sorted(zf.namelist())
    msg = _write(bare, [{"op": "search_and_replace", "find": "absent", "replace": "y"}])
    assert "'absent' was not found" in msg
    with zipfile.ZipFile(bare) as zf:
        assert sorted(zf.namelist()) == before


def test_a_match_across_a_link_or_an_equation_is_left_and_reported(tmp_path):
    link = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    f = _runs_doc(tmp_path, _r("see the ") + f'<w:hyperlink xmlns:r="{link}" w:anchor="x">{_r("terms")}</w:hyperlink>'
                  + _r(" now"))
    msg = _write(f, [{"op": "search_and_replace", "find": "the terms", "replace": "our terms"}])
    assert msg.startswith("Nothing saved:")
    assert "1 match crosses a link, field, content control or tracked change and was left as it is: p0" in msg
    from docx import Document

    assert "".join(t.text for t in Document(str(f)).element.body.iter(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")) == "see the terms now"


def test_replace_can_be_limited_to_a_target(tmp_path):
    f = tmp_path / "scoped.docx"
    _write(f, [{"op": "add_paragraph", "text": "cat one"}, {"op": "add_paragraph", "text": "cat two"}],
           create_new=True)
    msg = _write(f, [{"op": "search_and_replace", "find": "cat", "replace": "dog", "target": "p1"}])
    assert "1 replaced" in msg
    from docx import Document

    assert [p.text for p in Document(str(f)).paragraphs] == ["cat one", "dog two"]


def test_a_changed_paragraph_gets_a_new_text_id(tmp_path):
    from docx import Document

    f = tmp_path / "tid.docx"
    _write(f, [{"op": "add_paragraph", "text": "old words"}, {"op": "add_paragraph", "text": "same"}],
           create_new=True)
    t14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
    before = [(p._p.get(t14 + "paraId"), p._p.get(t14 + "textId")) for p in Document(str(f)).paragraphs]
    _write(f, [{"op": "search_and_replace", "find": "old", "replace": "new"}])
    after = [(p._p.get(t14 + "paraId"), p._p.get(t14 + "textId")) for p in Document(str(f)).paragraphs]
    assert after[0][0] == before[0][0] and after[0][1] != before[0][1]
    assert after[1] == before[1]


def test_fill_template_fills_split_tokens_and_reports_the_rest(tmp_path):
    from docx import Document

    f = _runs_doc(tmp_path, _r("Dear {{ cli") + _r("ent.na", "<w:b/>") + _r("me }}, total {{amount}} {{missing}}"))
    msg = _write(f, [{"op": "fill_template", "data": {"client": {"name": "Ada"}, "amount": 12.5, "spare": "x"}}])
    assert "filled 2 tokens: client.name 1, amount 1" in msg
    assert "p0: {{missing}}" in msg and "appear nowhere in the document: spare" in msg
    assert Document(str(f)).paragraphs[0].text == "Dear Ada, total 12.5 {{missing}}"
    before = f.read_bytes()
    msg = _write(f, [{"op": "fill_template", "data": {"other": "y"}, "strict": True}])
    assert msg.startswith("Nothing saved:") and "{{missing}}" in msg and f.read_bytes() == before


def test_fill_template_refuses_a_list_value(tmp_path):
    f = _runs_doc(tmp_path, _r("{{x}}"))
    msg = _write(f, [{"op": "fill_template", "data": {"x": [1, 2]}}])
    assert "data value for 'x' is a list" in msg


def test_a_picture_sets_no_distance_from_the_text(tmp_path):
    """Word writes an inline picture's four distances as 0; with none,
    LibreOffice (and Collabora) space it 0.32 cm from the text."""
    f = tmp_path / "p.docx"
    out = _write(f, [{"op": "add_image", "image_path": _png(tmp_path / "x.png"), "width_inches": 1.0}],
                 create_new=True)
    assert "Nothing saved" not in out, out
    xml = zipfile.ZipFile(f).read("word/document.xml").decode()
    inline = xml[xml.index("<wp:inline"):xml.index(">", xml.index("<wp:inline"))]
    assert all(f'{side}="0"' in inline for side in ("distT", "distB", "distL", "distR")), inline


def _extent(path) -> tuple[int, int]:
    """The first picture's (cx, cy) in EMU."""
    xml = zipfile.ZipFile(path).read("word/document.xml").decode()
    at = xml.index("<wp:extent ")
    return int(re.search(r'cx="(\d+)"', xml[at:]).group(1)), int(re.search(r'cy="(\d+)"', xml[at:]).group(1))


@pytest.mark.parametrize("keys, size", [
    ({"height_inches": 1}, (2 * 914400, 914400)),
    ({"width_inches": 1}, (914400, 914400 // 2)),
    ({}, (4 * 914400, 2 * 914400)),
    ({"width_inches": 1, "height_inches": 1}, (914400, 914400)),
])
def test_a_picture_keeps_its_proportions_unless_both_sides_are_given(tmp_path, keys, size):
    from PIL import Image

    image = tmp_path / "wide.png"
    Image.new("RGB", (200, 100), "navy").save(image, dpi=(72, 72))
    f = tmp_path / "p.docx"
    out = _write(f, [{"op": "add_image", "image_path": str(image), **keys}], create_new=True)
    assert "Nothing saved" not in out, out
    assert _extent(f) == size
