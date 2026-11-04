"""Addresses: the grammar, the table grid on irregular tables, header and
footer parts by reference, and the way back from an element to its address."""

import io
import zipfile

import pytest

pytest.importorskip("docx")

from formats.docx import address as addr
from results import OpError

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f'xmlns:w="{W_NS}"'


def _xml(fragment: str):
    from docx.oxml import parse_xml

    return parse_xml(fragment)


def _tc(text: str, props: str = "") -> str:
    return f"<w:tc><w:tcPr>{props}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


@pytest.mark.parametrize("target, segments, part, section, kind, para_id", [
    ("p12", (("p", 12),), "body", None, "default", None),
    (12, (("p", 12),), "body", None, "default", None),
    ("12", (("p", 12),), "body", None, "default", None),
    (" T2 / R3 / C1 / P0 ", (("t", 2), ("r", 3), ("c", 1), ("p", 0)), "body", None, "default", None),
    ("body/sdt0/p4", (("sdt", 0), ("p", 4)), "body", None, "default", None),
    ("header1.first/p0", (("p", 0),), "header", 1, "first", None),
    ("footer0", (), "footer", 0, "default", None),
    ("#1a2b3c4d", (), "body", None, "default", "1A2B3C4D"),
    ("p12#1A2B3C4D", (("p", 12),), "body", None, "default", "1A2B3C4D"),
    ("p8/textbox0/p1", (("p", 8), ("textbox", 0), ("p", 1)), "body", None, "default", None),
    ("t0/r1/c2/t0/r0/c0/p0", (("t", 0), ("r", 1), ("c", 2), ("t", 0), ("r", 0), ("c", 0), ("p", 0)),
     "body", None, "default", None),
    ("footnote2/p0", (("p", 0),), "footnote", 2, "default", None),
    ("endnote1", (), "endnote", 1, "default", None),
    ("comment3/p1", (("p", 1),), "comment", 3, "default", None),
])
def test_grammar(target, segments, part, section, kind, para_id):
    got = addr.parse(target)
    assert (got.segments, got.part, got.section, got.kind, got.para_id) == (segments, part, section, kind,
                                                                            para_id)


@pytest.mark.parametrize("target, why", [
    ("p12x", "'p12x' is not a segment"),
    (-1, "addresses count from 0"),
    ("t0/p1", "'p1' cannot follow t"),
    ("r1", "'r1' cannot follow the start"),
    ("header/p0", "name the section"),
    ("t0#1A2B3C4D", "#id names a paragraph"),
    ("#XYZ", "up to 8 hex digits"),
    ("footnote0/p0", "count from 1"),
    ("comment/p0", "name the comment"),
    ("endnote1.first", "name the endnote"),
    (True, ""),
])
def test_bad_addresses_name_the_shape(target, why):
    with pytest.raises(OpError) as caught:
        addr.parse(target)
    assert why in caught.value.message and "p12, t2/r3/c1/p0" in caught.value.hint


SPAN_TWO = '<w:gridSpan w:val="2"/>'
MERGE_START = '<w:vMerge w:val="restart"/>'


def _irregular_table_doc():
    from docx import Document

    doc = Document()
    tbl = _xml(
        f"<w:tbl {W}><w:tblPr/><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>"
        f"<w:tr>{_tc('A0')}{_tc('B0')}{_tc('C0')}</w:tr>"
        f"<w:tr><w:trPr><w:gridBefore w:val=\"1\"/></w:trPr>{_tc('B1')}{_tc('C1')}</w:tr>"
        f"<w:tr>{_tc('A2', SPAN_TWO)}{_tc('C2', MERGE_START)}</w:tr>"
        f"<w:sdt><w:sdtPr/><w:sdtContent><w:tr>{_tc('A3')}{_tc('B3')}{_tc('', '<w:vMerge/>')}</w:tr>"
        f"</w:sdtContent></w:sdt>"
        f"<w:tr>{_tc('A4')}<w:sdt><w:sdtPr/><w:sdtContent>{_tc('B4')}</w:sdtContent></w:sdt>{_tc('C4')}</w:tr>"
        f"</w:tbl>")
    doc.element.body.insert(0, tbl)
    return doc


def _text(tc) -> str:
    return "".join(t.text for t in tc.iter(f"{{{W_NS}}}t"))


@pytest.mark.parametrize("target, text", [
    ("t0/r0/c0", "A0"), ("t0/r1/c1", "B1"), ("t0/r1/c2", "C1"),
    ("t0/r2/c0", "A2"), ("t0/r2/c1", "A2"), ("t0/r2/c2", "C2"),
    ("t0/r3/c2", "C2"), ("t0/r3/c0", "A3"), ("t0/r4/c1", "B4"), ("t0/r4/c2", "C4"),
])
def test_irregular_tables_follow_the_grid(target, text):
    doc = _irregular_table_doc()
    assert _text(addr.walk(doc, addr.parse(target)).element) == text


def test_gaps_and_missing_cells_fail_with_the_fix():
    doc = _irregular_table_doc()
    with pytest.raises(OpError, match="empty space before the row's first cell"):
        addr.walk(doc, addr.parse("t0/r1/c0"))
    with pytest.raises(OpError, match="c3 does not exist"):
        addr.walk(doc, addr.parse("t0/r0/c3"))
    with pytest.raises(OpError, match="there are 5 rows"):
        addr.walk(doc, addr.parse("t0/r5"))


def test_rows_inside_a_repeating_section_count():
    doc = _irregular_table_doc()
    tbl = doc.element.body[0]
    all_rows = addr.rows(tbl)
    assert len(all_rows) == 5 and addr.row_is_wrapped(all_rows[3], tbl)


def _sections_doc():
    """Three sections: 0 has a header, 1 inherits it, 2 has its own and a first-page one."""
    from docx import Document

    doc = Document()
    doc.add_paragraph("one")
    doc.sections[0].header.paragraphs[0].text = "H-zero"
    doc.add_section()
    doc.add_paragraph("two")
    third = doc.add_section()
    doc.add_paragraph("three")
    third.header.is_linked_to_previous = False
    third.header.paragraphs[0].text = "H-two"
    third.different_first_page_header_footer = True
    third.first_page_header.is_linked_to_previous = False
    third.first_page_header.paragraphs[0].text = "H-two-first"
    return doc


def test_story_parts_by_reference():
    doc = _sections_doc()

    def _story_text(target):
        return _text(addr.walk(doc, addr.parse(target)).element)

    assert _story_text("header0/p0") == "H-zero"
    assert _story_text("header1/p0") == "H-zero"
    assert _story_text("header2/p0") == "H-two"
    assert _story_text("header2.first/p0") == "H-two-first"
    stories = [(k, kind, secs) for k, kind, _, secs in addr.story_parts(doc)]
    assert stories == [("header", "default", [0, 1]), ("header", "default", [2]), ("header", "first", [2])]
    with pytest.raises(OpError, match="section 0 has no first-page header"):
        addr.walk(doc, addr.parse("header0.first/p0"))
    with pytest.raises(OpError, match="section 0 has no footer"):
        addr.walk(doc, addr.parse("footer0/p0"))


def test_resolving_a_header_adds_no_part(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("x")
    with pytest.raises(OpError):
        addr.walk(doc, addr.parse("header0/p0"))
    buf = io.BytesIO()
    doc.save(buf)
    names = zipfile.ZipFile(buf).namelist()
    assert not any("header" in n or "footer" in n for n in names)


def test_address_of_is_the_inverse_of_walk():
    doc = _irregular_table_doc()
    doc.add_paragraph("after")
    for target in ("t0/r0/c0", "t0/r1/c1", "t0/r2/c0", "t0/r4/c1", "p0", "t0/r3"):
        located = addr.walk(doc, addr.parse(target))
        assert addr.address_of(doc, located.element) == target
    sec = _sections_doc()
    located = addr.walk(sec, addr.parse("header2.first/p0"))
    assert addr.address_of(sec, located.element) == "header2.first/p0"


def test_text_boxes_count_the_choice_copy_once():
    from docx import Document

    doc = Document()
    wps = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
    v = "urn:schemas-microsoft-com:vml"
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    box = f"<w:txbxContent><w:p><w:r><w:t>BOX</w:t></w:r></w:p></w:txbxContent>"
    p = _xml(
        f'<w:p {W} xmlns:mc="{mc}" xmlns:wps="{wps}" xmlns:v="{v}"><w:r><w:t>Host</w:t></w:r><w:r>'
        f"<mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><wps:txbx>{box}</wps:txbx></w:drawing>"
        f"</mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>{box}</v:textbox></v:shape></w:pict>"
        f"</mc:Fallback></mc:AlternateContent></w:r>"
        f"<w:r><w:pict><v:shape><v:textbox>{box.replace('BOX', 'VML')}</v:textbox></v:shape></w:pict></w:r></w:p>")
    doc.element.body.insert(0, p)
    boxes = addr.textboxes(doc.element.body[0])
    assert [_text(b) for b in boxes] == ["BOX", "VML"]
    located = addr.walk(doc, addr.parse("p0/textbox1/p0"))
    assert _text(located.element) == "VML"
    assert addr.address_of(doc, located.element) == "p0/textbox1/p0"


def _annotated_doc(tmp_path):
    """Two footnotes whose references come in the opposite order of their
    ids, LibreOffice's 0/1 separators, an unreferenced footnote, and two
    comments, one referenced from a footnote."""
    from docx import Document
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    from formats.docx import annotations
    from formats.docx.package import open_document

    doc = Document()
    body = doc.element.body
    for xml in (f'<w:p {W}><w:r><w:t>First</w:t></w:r><w:r><w:footnoteReference w:id="7"/></w:r>'
                f'<w:commentRangeStart w:id="40"/><w:r><w:t>x</w:t></w:r><w:commentRangeEnd w:id="40"/>'
                f'<w:r><w:commentReference w:id="40"/></w:r></w:p>',
                f'<w:p {W}><w:r><w:t>Second</w:t></w:r><w:r><w:footnoteReference w:id="2"/></w:r></w:p>'):
        body.insert(len(body) - 1, _xml(xml))
    notes = (f'<w:footnotes {W}><w:footnote w:type="separator" w:id="0"><w:p/></w:footnote>'
             f'<w:footnote w:type="continuationSeparator" w:id="1"><w:p/></w:footnote>'
             f'<w:footnote w:id="2"><w:p><w:r><w:t>Note two</w:t></w:r><w:r><w:commentReference w:id="41"/></w:r>'
             f'</w:p></w:footnote><w:footnote w:id="7"><w:p><w:r><w:t>Note seven</w:t></w:r></w:p></w:footnote>'
             f'<w:footnote w:id="9"><w:p><w:r><w:t>Orphan</w:t></w:r></w:p></w:footnote></w:footnotes>')
    comments = (f'<w:comments {W}><w:comment w:id="41" w:author="B"><w:p><w:r><w:t>On a note</w:t></w:r></w:p>'
                f'</w:comment><w:comment w:id="40" w:author="A"><w:p><w:r><w:t>On the body</w:t></w:r></w:p>'
                f'</w:comment></w:comments>')
    for name, reltype, ctype, xml in (
            ("/word/footnotes.xml", annotations.RT_FOOTNOTES, annotations.CT_FOOTNOTES, notes),
            ("/word/comments.xml", annotations.RT_COMMENTS, annotations.CT_COMMENTS, comments)):
        doc.part.relate_to(Part(PackURI(name), ctype, xml.encode(), doc.part.package), reltype)
    path = tmp_path / "annotated.docx"
    doc.save(str(path))
    return open_document(str(path))


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(f"{{{W_NS}}}t"))


def test_notes_and_comments_are_numbered_as_people_see_them(tmp_path):
    doc = _annotated_doc(tmp_path)
    notes = [_text(el) for _, el in addr.annotation_order(doc, addr.FOOTNOTE)]
    assert notes == ["Note seven", "Note two", "Orphan"]
    comments = [_text(el) for _, el in addr.annotation_order(doc, addr.COMMENT)]
    assert comments == ["On the body", "On a note"]
    assert addr.annotation_order(doc, addr.ENDNOTE) == []


def test_note_addresses_walk_and_come_back(tmp_path):
    doc = _annotated_doc(tmp_path)
    located = addr.walk(doc, addr.parse("footnote2/p0"))
    assert _text(located.element) == "Note two"
    assert addr.address_of(doc, located.element) == "footnote2/p0"
    comment = addr.walk(doc, addr.parse("comment2")).element
    assert addr.address_of(doc, comment) == "comment2"
    found = addr.address_map(doc)
    assert found[addr.walk(doc, addr.parse("footnote3/p0")).element] == "footnote3/p0"
    with pytest.raises(OpError, match="footnote4 does not exist: the document has 3 footnotes"):
        addr.walk(doc, addr.parse("footnote4/p0"))
    with pytest.raises(OpError, match="has 0 endnotes"):
        addr.walk(doc, addr.parse("endnote1"))


def test_finding_notes_creates_no_part(tmp_path):
    from docx import Document

    from formats.docx.package import open_document

    path = tmp_path / "plain.docx"
    Document().save(str(path))
    doc = open_document(str(path))
    before = len(list(doc.part.package.iter_parts()))
    assert addr.annotation_order(doc, addr.COMMENT) == []
    assert addr.address_map(doc) == {}
    assert len(list(doc.part.package.iter_parts())) == before
