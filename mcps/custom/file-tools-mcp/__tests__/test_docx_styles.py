"""Styles, tab stops, list numbers, heading numbers, header kinds, table
shading in schema order, the horizontal rule and document properties."""

import asyncio
import re
import zipfile

import pytest

pytest.importorskip("docx")

import word

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


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


def _write(path, ops, create_new=False, best_effort=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops,
                                               "create_new": create_new, "best_effort": best_effort}))


def _stripped(tmp_path, name="bare.docx"):
    """Like Word's blank template: only the base styles stored."""
    from docx import Document

    doc = Document()
    styles = doc.styles.element
    keep = {"Normal", "DefaultParagraphFont", "TableNormal", "NoList"}
    for style in list(styles.iterchildren(_w("style"))):
        if style.get(_w("styleId")) not in keep:
            styles.remove(style)
    f = tmp_path / name
    doc.save(str(f))
    return f


def test_paragraph_and_character_styles_by_name(tmp_path):
    from docx import Document

    f = tmp_path / "styles.docx"
    msg = _write(f, [{"op": "add_paragraph", "text": "Quoted", "style": "quote"},
                     {"op": "add_paragraph", "runs": [{"text": "Strong", "style": "Strong"}, {"text": " plain"}]},
                     {"op": "set_paragraph_format", "index": 1, "style": "Intense Quote"}], create_new=True)
    assert "Failed:" not in msg
    doc = Document(str(f))
    assert doc.paragraphs[0].style.name == "Quote"
    assert doc.paragraphs[1].style.name == "Intense Quote"
    assert doc.paragraphs[1].runs[0].style.name == "Strong"


def test_an_unknown_style_fails_with_the_closest_names(tmp_path):
    msg = _write(tmp_path / "x.docx", [{"op": "add_paragraph", "text": "x", "style": "Qoute"}])
    assert "style 'Qoute' is not a paragraph style of this document" in msg and "Did you mean 'Quote'" in msg


def test_built_in_styles_are_copied_into_a_bare_document(tmp_path):
    from docx import Document

    f = _stripped(tmp_path)
    msg = _write(f, [{"op": "add_heading", "text": "Chapter", "level": 2},
                     {"op": "add_table", "headers": ["A"]},
                     {"op": "add_paragraph", "text": "q", "style": "Quote"}])
    assert "Failed:" not in msg and "no table style" not in msg
    doc = Document(str(f))
    assert doc.paragraphs[0].style.name == "Heading 2"
    assert doc.tables[0].style.name == "Table Grid"
    ids = {s.get(_w("styleId")) for s in doc.styles.element.iterchildren(_w("style"))}
    assert {"Heading2", "Heading1", "TableGrid", "Quote"} <= ids or {"Heading2", "TableGrid", "Quote"} <= ids


def test_tab_stops_and_false_flags(tmp_path):
    from docx import Document

    f = tmp_path / "tabs.docx"
    _write(f, [{"op": "add_paragraph", "text": "Item\t12", "bold": True,
                "tab_stops": [{"position": "6in", "alignment": "right", "leader": "dot"},
                              {"position": "2cm"}]}], create_new=True)
    _write(f, [{"op": "set_paragraph_text", "index": 0, "text": "Plain", "bold": False}])
    doc = Document(str(f))
    stops = doc.paragraphs[0].paragraph_format.tab_stops
    assert [(round(t.position.inches, 2), t.alignment.name, t.leader.name) for t in stops] == [
        (0.79, "LEFT", "SPACES"), (6.0, "RIGHT", "DOTS")]
    assert doc.paragraphs[0].runs[0].bold is False
    msg = _write(f, [{"op": "set_paragraph_format", "index": 0, "clear_tab_stops": True}])
    assert "Failed:" not in msg and len(Document(str(f)).paragraphs[0].paragraph_format.tab_stops) == 0


def test_list_start_and_number_format(tmp_path):
    from formats.docx import read as docx_read

    f = tmp_path / "start.docx"
    msg = _write(f, [{"op": "add_list", "ordered": True, "items": ["five", "six"], "start": 5,
                      "number_format": "upperRoman"},
                     {"op": "add_list", "ordered": True, "items": ["a1"], "number_format": "a"}], create_new=True)
    assert "Failed:" not in msg
    body = re.sub(r"#[0-9A-F]{8}", "", docx_read.read_docx(str(f))).split("blocks):\n")[1].splitlines()
    assert body == ["p0 [List Number] V. five", "p1 [List Number] VI. six", "p2 [List Number] a. a1"]
    msg = _write(f, [{"op": "add_list", "items": ["x"], "start": 2}])
    assert "are for numbered lists" in msg
    msg = _write(f, [{"op": "add_list", "ordered": True, "items": ["x"], "start": 2, "continue": True}])
    assert "takes no start or number_format" in msg


def test_number_headings(tmp_path):
    from formats.docx import read as docx_read

    f = tmp_path / "heads.docx"
    _write(f, [{"op": "add_heading", "text": "Intro", "level": 1},
               {"op": "add_heading", "text": "Scope", "level": 2},
               {"op": "add_heading", "text": "Method", "level": 1},
               {"op": "number_headings", "levels": 2}], create_new=True)
    body = re.sub(r"#[0-9A-F]{8}", "", docx_read.read_docx(str(f))).split("blocks):\n")[1].splitlines()
    assert body == ["p0 [Heading 1] 1. Intro", "p1 [Heading 2] 1.1 Scope", "p2 [Heading 1] 2. Method"]
    _write(f, [{"op": "number_headings", "levels": 2, "style": "off"}])
    body = re.sub(r"#[0-9A-F]{8}", "", docx_read.read_docx(str(f))).split("blocks):\n")[1].splitlines()
    assert body[0] == "p0 [Heading 1] Intro"


def test_first_page_and_even_headers(tmp_path):
    from docx import Document

    f = tmp_path / "kinds.docx"
    msg = _write(f, [{"op": "add_header", "text": "Running head"},
                     {"op": "add_header", "text": "", "kind": "first"},
                     {"op": "add_footer", "text": "Even foot", "kind": "even"},
                     {"op": "add_page_number", "kind": "first", "format": "{page}"}], create_new=True)
    assert "Failed:" not in msg
    doc = Document(str(f))
    section = doc.sections[0]
    assert section.different_first_page_header_footer
    assert doc.settings.odd_and_even_pages_header_footer
    assert section.header.paragraphs[0].text == "Running head"
    assert section.first_page_header.paragraphs[0].text == ""
    assert section.even_page_footer.paragraphs[0].text == "Even foot"
    assert "PAGE" in "".join(t.text or "" for t in section.first_page_footer._element.iter(_w("instrText")))
    msg = _write(f, [{"op": "add_header", "text": "x", "kind": "odd"}])
    assert "kind 'odd' is not one of: default, first, even" in msg


def _children(element) -> list[str]:
    return [c.tag.split("}")[1] for c in element]


def test_table_shading_is_valid_and_a_rerun_changes_nothing(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    f = tmp_path / "shade.docx"
    _write(f, [{"op": "add_table", "headers": ["A", "B"], "rows": [["1", "2"]]}], create_new=True)
    doc = Document(str(f))
    tc_pr = doc.tables[0].rows[0].cells[0]._tc.get_or_add_tcPr()
    tc_pr.append(parse_xml(f'<w:vAlign xmlns:w="{W_NS}" w:val="center"/>'))
    doc.save(str(f))
    style = {"op": "style_table", "header": {"fill": "1F4E79"}, "alternating_colors": ["FFFFFF", "EEEEEE"],
             "border": True}
    _write(f, [style])
    with zipfile.ZipFile(f) as zf:
        first = zf.read("word/document.xml")
    _write(f, [style])
    with zipfile.ZipFile(f) as zf:
        second = zf.read("word/document.xml")
    assert re.sub(rb'w14:textId="\w+"', b"", first) == re.sub(rb'w14:textId="\w+"', b"", second)
    doc = Document(str(f))
    tc_pr = doc.tables[0].rows[0].cells[0]._tc.tcPr
    assert _children(tc_pr) == ["tcW", "shd", "vAlign"]
    shd = tc_pr.find(_w("shd"))
    assert (shd.get(_w("val")), shd.get(_w("color")), shd.get(_w("fill"))) == ("clear", "auto", "1F4E79")
    tbl_pr = doc.tables[0]._tbl.tblPr
    names = _children(tbl_pr)
    assert names.count("tblBorders") == 1 and names.index("tblBorders") < names.index("tblLook")


def test_horizontal_rule(tmp_path):
    from docx import Document

    f = tmp_path / "rule.docx"
    msg = _write(f, [{"op": "add_paragraph", "text": "Above"},
                     {"op": "add_horizontal_rule", "target": "p0", "position": "before"}], create_new=True)
    assert "Addresses name the document as this call began" in msg
    _write(f, [{"op": "add_paragraph", "text": "Above"},
               {"op": "add_horizontal_rule", "color": "808080", "size": 1.5, "style": "double"}], create_new=True)
    _write(f, [{"op": "add_horizontal_rule", "target": "p0", "position": "before"}])
    doc = Document(str(f))
    rule = doc.paragraphs[2]._p.pPr.find(_w("pBdr")).find(_w("bottom"))
    assert (rule.get(_w("val")), rule.get(_w("sz")), rule.get(_w("color"))) == ("double", "12", "808080")
    assert doc.paragraphs[0]._p.pPr.find(_w("pBdr")) is not None and doc.paragraphs[1].text == "Above"


def test_properties_and_a_new_documents_clean_ones(tmp_path):
    from docx import Document

    f = tmp_path / "props.docx"
    _write(f, [{"op": "add_paragraph", "text": "x"}], create_new=True)
    props = Document(str(f)).core_properties
    assert props.author == "" and props.comments == "" and props.created.year >= 2026
    _write(f, [{"op": "set_properties", "title": "Annual report", "author": "Finance", "description": "FY26"}])
    props = Document(str(f)).core_properties
    assert (props.title, props.author, props.comments) == ("Annual report", "Finance", "FY26")
    msg = _write(f, [{"op": "set_properties"}])
    assert "no property was given" in msg


def test_a_page_number_field_carries_its_size_in_every_run(tmp_path):
    from docx import Document

    f = tmp_path / "pn.docx"
    _write(f, [{"op": "add_page_number", "format": "Page {page} of {total}", "font_size": 9}], create_new=True)
    runs = Document(str(f)).sections[0].footer.paragraphs[0].runs
    assert len(runs) == 12
    assert {r.font.size.pt for r in runs} == {9.0}
    instr = [r._r.find(_w("instrText")).text for r in runs if r._r.find(_w("instrText")) is not None]
    assert instr == [" PAGE ", " NUMPAGES "]


def test_a_page_number_below_footer_words_is_drawn_like_them(tmp_path):
    from docx import Document

    f = tmp_path / "pn-beside.docx"
    _write(f, [{"op": "add_footer", "text": "Internal", "font_size": 8, "color": "808080", "alignment": "right"},
               {"op": "add_page_number", "format": "Page {page} of {total}"}], create_new=True)
    words, number = Document(str(f)).sections[0].footer.paragraphs
    assert words.text == "Internal"
    looks = {(r.font.size.pt, str(r.font.color.rgb)) for r in number.runs}
    assert looks == {(8.0, "808080")}
    assert number.alignment == 1 and words.alignment == 2
    _write(f, [{"op": "add_page_number", "format": "{page}", "font_size": 11, "color": "000000"}])
    last = Document(str(f)).sections[0].footer.paragraphs[-1]
    assert {(r.font.size.pt, str(r.font.color.rgb)) for r in last.runs} == {(11.0, "000000")}


def test_a_page_number_under_plain_footer_words_takes_their_size(tmp_path):
    from docx import Document

    f = tmp_path / "pn-plain.docx"
    _write(f, [{"op": "add_footer", "text": "Internal"},
               {"op": "add_page_number", "format": "Page {page} of {total}"}], create_new=True)
    words, number = Document(str(f)).sections[0].footer.paragraphs
    assert words.runs[0].font.size is None
    assert {r.font.size for r in number.runs} == {None}
    assert number.style.name == words.style.name


def test_a_page_number_copies_no_tracked_change_of_the_footer_words(tmp_path):
    import zipfile

    from docx import Document
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = Document()
    footer = doc.sections[0].footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0]._p
    for child in list(p):
        p.remove(child)
    for child in parse_xml(
            f'<w:p {w}><w:pPr><w:pStyle w:val="Footer"/><w:rPr><w:ins w:id="9" w:author="Reviewer"/>'
            f'<w:rPrChange w:id="10" w:author="Reviewer"><w:rPr/></w:rPrChange></w:rPr>'
            f'<w:pPrChange w:id="11" w:author="Reviewer"><w:pPr/></w:pPrChange></w:pPr>'
            f'<w:r><w:t>Internal</w:t></w:r></w:p>'):
        p.append(child)
    f = tmp_path / "pn-tracked.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "add_page_number"}])
    assert msg.startswith("Document saved"), msg
    with zipfile.ZipFile(f) as zf:
        part = next(n for n in zf.namelist() if n.startswith("word/footer"))
        xml = zf.read(part).decode()
    for revision in ('w:id="9"', 'w:id="10"', 'w:id="11"'):
        assert xml.count(revision) == 1, (revision, xml)


def test_a_table_of_contents_field_has_a_run_for_each_part(tmp_path):
    from docx import Document

    f = tmp_path / "toc.docx"
    _write(f, [{"op": "add_table_of_contents", "levels": 2}], create_new=True)
    paragraphs = Document(str(f)).paragraphs
    assert paragraphs[0].style.name == "TOC Heading"
    runs = paragraphs[1].runs
    parts = [(child.tag.rsplit("}", 1)[1], child.get(_w("fldCharType")))
             for r in runs for child in r._r if child.tag != _w("rPr")]
    assert parts == [("fldChar", "begin"), ("instrText", None), ("fldChar", "separate"), ("t", None)]
    assert all(len([c for c in r._r if c.tag != _w("rPr")]) == 1 for r in runs)
    assert runs[3].text.startswith("No headings yet") and runs[3].italic
    end = [c.get(_w("fldCharType")) for r in paragraphs[2].runs for c in r._r if c.tag == _w("fldChar")]
    assert end == ["end"]
