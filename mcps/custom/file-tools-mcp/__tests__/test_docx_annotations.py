"""Notes and comment parts as the tools load them, the ids annotations
carry, and parts that only an operation adding the first note or comment
creates."""

import io
import zipfile

import pytest

pytest.importorskip("docx")

from formats.docx import annotations
from formats.docx.package import open_document

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f'xmlns:w="{W_NS}"'


def _with_parts(tmp_path, parts: dict[str, tuple[str, str, bytes]], name="doc.docx"):
    """A python-docx document saved with extra parts: {partname: (reltype,
    content type, bytes)} related from the document part."""
    from docx import Document
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    doc = Document()
    for partname, (reltype, content_type, blob) in parts.items():
        part = Part(PackURI(partname), content_type, blob, doc.part.package)
        doc.part.relate_to(part, reltype)
    path = tmp_path / name
    doc.save(str(path))
    return path


def _notes_xml(kind: str, *notes: str) -> bytes:
    return f'<w:{kind}s {W}>{"".join(notes)}</w:{kind}s>'.encode()


def _note(kind: str, note_id: int, text: str, extra: str = "") -> str:
    return f'<w:{kind} w:id="{note_id}"{extra}><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:{kind}>'


def test_notes_load_as_story_parts(tmp_path):
    from docx.text.paragraph import Paragraph

    path = _with_parts(tmp_path, {"/word/footnotes.xml": (
        annotations.RT_FOOTNOTES, annotations.CT_FOOTNOTES, _notes_xml("footnote", _note("footnote", 1, "Note")))})
    doc = open_document(str(path))
    part = annotations.notes_part(doc, annotations.FOOTNOTE)
    assert type(part).__name__ == "NotesPart"
    paragraph = Paragraph(part.element.find(f"{{{W_NS}}}footnote/{{{W_NS}}}p"), part)
    paragraph.style = "Quote"
    assert paragraph.style.name == "Quote"


def test_a_damaged_notes_part_still_opens_the_document(tmp_path):
    path = _with_parts(tmp_path, {"/word/footnotes.xml": (
        annotations.RT_FOOTNOTES, annotations.CT_FOOTNOTES, b"<w:footnotes oops")})
    doc = open_document(str(path))
    part = annotations.notes_part(doc, annotations.FOOTNOTE)
    assert annotations.root_of_part(part) is None
    with pytest.raises(Exception, match="damaged"):
        annotations.editable_root(part, "footnotes")


def test_ids_count_every_story_part(tmp_path):
    header = f'<w:hdr {W}><w:p><w:bookmarkStart w:id="5" w:name="h"/><w:bookmarkEnd w:id="5"/></w:p></w:hdr>'
    path = _with_parts(tmp_path, {
        "/word/footnotes.xml": (annotations.RT_FOOTNOTES, annotations.CT_FOOTNOTES, _notes_xml(
            "footnote", '<w:footnote w:id="1"><w:p><w:ins w:id="9" w:author="A"><w:r><w:t>x</w:t></w:r></w:ins>'
            '</w:p></w:footnote>')),
        "/word/header9.xml": (annotations.RT_HEADER,
                              "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
                              header.encode()),
    })
    doc = open_document(str(path))
    ids = annotations.Ids(doc)
    assert [ids.next(), ids.next()] == ["10", "11"]
    ids.bump(40)
    assert ids.next() == "41"


def test_the_id_counter_past_the_top_takes_the_lowest_free(tmp_path):
    from docx import Document

    doc = Document()
    p = doc.add_paragraph("x")._p
    from docx.oxml import parse_xml

    p.append(parse_xml(f'<w:bookmarkStart {W} w:id="2147483647" w:name="top"/>'))
    p.append(parse_xml(f'<w:bookmarkStart {W} w:id="0" w:name="zero"/>'))
    ids = annotations.Ids(doc)
    assert ids.next() == "1"
    assert ids.next() == "2"


def test_note_ids_are_their_own_space():
    from docx import Document
    from lxml import etree

    doc = Document()
    libreoffice = etree.fromstring(_notes_xml(
        "footnote", '<w:footnote w:type="separator" w:id="0"/>', '<w:footnote w:id="1" '
        'w:type="continuationSeparator"/>', _note("footnote", 2, "a")))
    assert annotations.next_note_id(doc, libreoffice, annotations.FOOTNOTE) == "3"
    word = etree.fromstring(_notes_xml("footnote", '<w:footnote w:type="separator" w:id="-1"/>',
                                       '<w:footnote w:type="continuationSeparator" w:id="0"/>'))
    assert annotations.next_note_id(doc, word, annotations.FOOTNOTE) == "1"
    doc.add_paragraph().add_run()._r.append(etree.fromstring(
        f'<w:footnoteReference xmlns:w="{annotations.W_NS}" w:id="4"/>'))
    assert annotations.next_note_id(doc, word, annotations.FOOTNOTE) == "5"


def test_parts_are_found_without_being_made(tmp_path):
    from docx import Document

    path = tmp_path / "plain.docx"
    Document().save(str(path))
    doc = open_document(str(path))
    before = len(list(doc.part.package.iter_parts()))
    assert annotations.comments_part(doc) is None
    assert annotations.notes_part(doc, annotations.FOOTNOTE) is None
    assert annotations.story_parts(doc)[0] is doc.part
    assert len(list(doc.part.package.iter_parts())) == before


def test_a_created_notes_part_has_the_separators_and_the_settings(tmp_path):
    doc = open_document(str(_with_parts(tmp_path, {})))
    part = annotations.ensure_notes_part(doc, annotations.FOOTNOTE)
    assert annotations.ensure_notes_part(doc, annotations.FOOTNOTE) is part
    buf = io.BytesIO()
    doc.save(buf)
    with zipfile.ZipFile(buf) as zf:
        notes = zf.read("word/footnotes.xml").decode()
        settings = zf.read("word/settings.xml").decode()
        types = zf.read("[Content_Types].xml").decode()
    assert 'w:type="separator" w:id="-1"' in notes and 'w:type="continuationSeparator" w:id="0"' in notes
    assert '<w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>' in settings
    assert annotations.CT_FOOTNOTES in types


def test_a_created_comments_part_avoids_a_taken_name(tmp_path):
    path = _with_parts(tmp_path, {"/word/comments.xml": (
        "http://example.com/other", "application/xml", b"<other/>")})
    doc = open_document(str(path))
    part = annotations.ensure_comments_part(doc)
    assert str(part.partname) == "/word/comments1.xml"
    assert annotations.comments_part(doc) is part


# --- what existing operations keep ------------------------------------------


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture
def hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops) -> str:
    import asyncio

    import word

    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops}))


def _annotated(tmp_path):
    """A heading with a TOC bookmark, a paragraph with a footnote and a
    comment, a bookmark that starts in it and ends in the next paragraph."""
    from docx import Document
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import parse_xml

    doc = Document()
    body = doc.element.body
    for xml in (
        f'<w:p {W}><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:bookmarkStart w:id="1" w:name="_Toc1"/>'
        f'<w:r><w:t>Terms</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>',
        f'<w:p {W}><w:commentRangeStart w:id="7"/><w:r><w:t>Fees are due</w:t></w:r><w:commentRangeEnd w:id="7"/>'
        f'<w:r><w:commentReference w:id="7"/></w:r><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
        f'<w:footnoteReference w:id="1"/></w:r><w:bookmarkStart w:id="3" w:name="span"/>'
        f'<w:r><w:t> monthly.</w:t></w:r></w:p>',
        f'<w:p {W}><w:r><w:t>Next</w:t></w:r><w:bookmarkEnd w:id="3"/></w:p>',
    ):
        body.insert(len(body) - 1, parse_xml(xml))
    notes = _notes_xml("footnote", '<w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>',
                       '<w:footnote w:type="continuationSeparator" w:id="0"><w:p/></w:footnote>',
                       '<w:footnote w:id="1"><w:p><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
                       '<w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> Per the 2025 schedule.</w:t>'
                       '</w:r></w:p></w:footnote>')
    comments = (f'<w:comments {W}><w:comment w:id="7" w:author="A" w:initials="A"><w:p><w:r><w:annotationRef/>'
                f'</w:r><w:r><w:t>Check the date.</w:t></w:r></w:p></w:comment></w:comments>').encode()
    for name, reltype, ctype, blob in (
            ("/word/footnotes.xml", annotations.RT_FOOTNOTES, annotations.CT_FOOTNOTES, notes),
            ("/word/comments.xml", annotations.RT_COMMENTS, annotations.CT_COMMENTS, comments)):
        doc.part.relate_to(Part(PackURI(name), ctype, blob, doc.part.package), reltype)
    path = tmp_path / "annotated.docx"
    doc.save(str(path))
    return path


def _parts_xml(path) -> dict[str, str]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n).decode() for n in zf.namelist() if n.endswith(".xml")}


def test_new_text_keeps_bookmarks_references_and_comment_ranges(tmp_path, hooks):
    path = _annotated(tmp_path)
    msg = _write(path, [{"op": "set_paragraph_text", "target": "p0", "text": "Payment terms"},
                        {"op": "set_paragraph_text", "target": "p1", "text": "Fees fall due each month."}])
    assert msg.startswith("Document saved"), msg
    doc = open_document(str(path))
    heading, fees, _ = doc.element.body.iterchildren(f"{{{W_NS}}}p")
    tags = [c.tag.rsplit("}", 1)[1] for c in heading]
    assert tags == ["pPr", "bookmarkStart", "r", "bookmarkEnd"]
    tags = [c.tag.rsplit("}", 1)[1] for c in fees]
    assert tags[:3] == ["commentRangeStart", "bookmarkStart", "r"]
    assert "commentReference" in _parts_xml(path)["word/document.xml"]
    assert fees.xpath("string(.)") == "Fees fall due each month."
    refs = fees.findall(f".//{{{W_NS}}}footnoteReference")
    assert len(refs) == 1 and refs[0].getparent().find(f"{{{W_NS}}}rPr") is not None
    new_run = fees.find(f"{{{W_NS}}}r")
    assert new_run.find(f"{{{W_NS}}}rPr/{{{W_NS}}}vertAlign") is None


def test_a_note_keeps_its_number_when_its_text_is_rewritten(tmp_path, hooks):
    path = _annotated(tmp_path)
    msg = _write(path, [{"op": "set_paragraph_text", "target": "footnote1/p0", "text": "Source: the ledger."}])
    assert msg.startswith("Document saved"), msg
    note = _parts_xml(path)["word/footnotes.xml"]
    assert note.index("<w:footnoteRef/>") < note.index("Source: the ledger.")
    assert '<w:t xml:space="preserve"> </w:t>' in note
    from lxml import etree

    root = etree.fromstring(note.encode())
    text_run = next(t.getparent() for t in root.iter(f"{{{W_NS}}}t") if t.text == "Source: the ledger.")
    assert text_run.find(f"{{{W_NS}}}rPr/{{{W_NS}}}vertAlign") is None


def test_deleting_a_paragraph_takes_its_note_and_comment_and_lifts_a_bookmark(tmp_path, hooks):
    path = _annotated(tmp_path)
    msg = _write(path, [{"op": "delete_paragraph", "target": "p1"}])
    assert "deleted with it: 1 footnote and 1 comment" in msg, msg
    xml = _parts_xml(path)
    assert "Per the 2025 schedule" not in xml["word/footnotes.xml"]
    assert "Check the date" not in xml["word/comments.xml"]
    doc = open_document(str(path))
    last = list(doc.element.body.iterchildren(f"{{{W_NS}}}p"))[-1]
    assert [c.tag.rsplit("}", 1)[1] for c in last][:2] == ["bookmarkStart", "r"]


def test_a_comment_holds_only_text_changes(tmp_path, hooks):
    path = _annotated(tmp_path)
    msg = _write(path, [{"op": "insert_paragraph", "target": "comment1/p0", "text": "More"}])
    assert "inside a comment, where only the text changes" in msg, msg
    msg = _write(path, [{"op": "set_paragraph_text", "target": "comment1/p0", "text": "Check the year."}])
    assert msg.startswith("Document saved"), msg
    comment = _parts_xml(path)["word/comments.xml"]
    assert comment.index("<w:annotationRef/>") < comment.index("Check the year.")


def test_a_note_keeps_its_last_paragraph(tmp_path, hooks):
    path = _annotated(tmp_path)
    msg = _write(path, [{"op": "delete_paragraph", "target": "footnote1/p0"}])
    assert "the only paragraph of its footnote" in msg, msg
