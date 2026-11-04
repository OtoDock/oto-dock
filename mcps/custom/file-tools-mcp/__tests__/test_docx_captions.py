"""Captions numbered by SEQ fields, cross-references (REF, PAGEREF),
update_fields, and a table of contents built from named styles (\\t)."""

import asyncio
import re
import zipfile

import pytest

pytest.importorskip("docx")

import checks
import word
from formats.docx import captions, fields
from formats.docx.package import open_document
from ooxml import validate

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
    from batch import Rendered

    async def render(staged, call, budget_s):
        return Rendered(note="no LibreOffice here")

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)
    monkeypatch.setattr(word, "render_toc", render)


def _write(path, ops, create_new=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops, "create_new": create_new}))


def _errors(path) -> list[str]:
    with checks.CheckContext(str(path), "docx") as ctx:
        return [f"{f.location}: {f.message}" for f in validate.validate_package(ctx)
                if f.severity is checks.Severity.ERROR]


def _texts(path) -> list[str]:
    return ["".join(t.text or "" for t in p.iter(_w("t"))) for p in open_document(str(path)).element.body.iter(_w("p"))]


def test_captions_are_numbered_in_document_order(tmp_path):
    f = tmp_path / "caps.docx"
    _write(f, [{"op": "add_paragraph", "text": "[chart A]"}, {"op": "add_paragraph", "text": "[chart B]"},
               {"op": "add_table", "headers": ["Item"], "rows": [["Pens"]]}], create_new=True)
    msg = _write(f, [{"op": "add_caption", "target": "p1", "text": "Second chart."},
                     {"op": "add_caption", "target": "p0", "text": "First chart.", "bookmark": "fig_first"},
                     {"op": "add_caption", "target": "t0", "label": "Table", "text": "Supplies."}])
    assert msg.startswith("Document saved"), msg
    texts = _texts(f)
    assert texts[:5] == ["[chart A]", "Figure 1. First chart.", "[chart B]", "Figure 2. Second chart.",
                         "Table 1. Supplies."]
    body = open_document(str(f)).element.body
    assert next(body.iterchildren(_w("tbl"))).getprevious().find(f".//{_w('instrText')}") is not None
    styles = {p.find(f"{_w('pPr')}/{_w('pStyle')}").get(_w("val")) for p in body.iter(_w("p"))
              if "SEQ" in "".join(i.text or "" for i in p.iter(_w("instrText")))}
    assert styles == {"Caption"}
    assert captions.bookmark_texts(body)["fig_first"] == "Figure 1"
    assert _errors(f) == []


def test_a_cross_reference_shows_the_caption_and_follows_renumbering(tmp_path):
    f = tmp_path / "refs.docx"
    _write(f, [{"op": "add_paragraph", "text": "[chart]"}, {"op": "add_paragraph", "text": "As shown in ."}],
           create_new=True)
    _write(f, [{"op": "add_caption", "target": "p0", "text": "Revenue.", "bookmark": "fig_rev"}])
    msg = _write(f, [{"op": "add_cross_reference", "bookmark": "FIG_REV", "target": "p2",
                      "after_text": "As shown in "}])
    assert msg.startswith("Document saved"), msg
    assert _texts(f)[2] == "As shown in Figure 1."
    xml = zipfile.ZipFile(f).read("word/document.xml").decode()
    assert "REF fig_rev \\h" in xml
    _write(f, [{"op": "insert_paragraph", "index": 0, "text": "[earlier chart]"}])
    _write(f, [{"op": "add_caption", "target": "p0", "text": "Earlier."}])
    texts = _texts(f)
    assert "Figure 2. Revenue." in texts and "As shown in Figure 2." in texts
    assert _errors(f) == []


def test_a_cross_reference_is_drawn_like_the_words_around_it(tmp_path):
    f = tmp_path / "dress.docx"
    _write(f, [{"op": "add_paragraph", "text": "[chart]"},
               {"op": "add_paragraph", "text": "Detail: , page .", "color": "4B5563", "italic": True}],
           create_new=True)
    _write(f, [{"op": "add_caption", "target": "p0", "text": "Revenue.", "bookmark": "fig_rev"}])
    msg = _write(f, [{"op": "add_cross_reference", "bookmark": "fig_rev", "target": "p2", "after_text": "Detail: "},
                     {"op": "add_cross_reference", "bookmark": "fig_rev", "target": "p2"}])
    assert msg.startswith("Document saved"), msg
    p = list(open_document(str(f)).element.body.iter(_w("p")))[2]
    shown = [r for r in p.iter(_w("r")) if r.find(_w("t")) is not None]
    assert "".join(r.find(_w("t")).text for r in shown) == "Detail: Figure 1, page .Figure 1"
    for r in shown:
        assert r.find(f"{_w('rPr')}/{_w('color')}").get(_w("val")) == "4B5563"
        assert r.find(f"{_w('rPr')}/{_w('i')}") is not None
    assert _errors(f) == []


def test_a_page_reference_without_a_layout_says_word_fills_it(tmp_path):
    f = tmp_path / "page.docx"
    _write(f, [{"op": "add_paragraph", "text": "Terms"}, {"op": "add_paragraph", "text": "See page "}],
           create_new=True)
    msg = _write(f, [{"op": "add_bookmark", "target": "p0", "name": "terms"},
                     {"op": "add_cross_reference", "bookmark": "terms", "show": "page", "target": "p1"}])
    assert "1 page number is missing" in msg, msg
    assert "PAGEREF terms \\h" in zipfile.ZipFile(f).read("word/document.xml").decode()
    assert "000" not in _texts(f)[1]


def test_page_references_take_their_page_from_the_layout(tmp_path, monkeypatch):
    from batch import Rendered

    async def render(staged, call, budget_s):
        assert staged.facts["page_marks"] == ["terms"]
        return Rendered(payload="layout.pdf")

    monkeypatch.setattr(word, "render_toc", render)
    monkeypatch.setattr(fields, "read_pages", lambda pdf, names, marks: {"terms": "4"})
    f = tmp_path / "page.docx"
    _write(f, [{"op": "add_paragraph", "text": "Terms"}, {"op": "add_paragraph", "text": "See page "}],
           create_new=True)
    msg = _write(f, [{"op": "add_bookmark", "target": "p0", "name": "terms"},
                     {"op": "add_cross_reference", "bookmark": "terms", "show": "page", "target": "p1"}])
    assert "1 page number from a LibreOffice layout" in msg, msg
    assert _texts(f)[1] == "See page 4"


def test_update_fields_keeps_an_old_page_when_there_is_no_layout(tmp_path, monkeypatch):
    f = tmp_path / "old.docx"
    _write(f, [{"op": "add_paragraph", "text": "Terms"}, {"op": "add_paragraph", "text": "See page "}],
           create_new=True)
    monkeypatch.setattr(fields, "read_pages", lambda pdf, names, marks: {"terms": "4"})

    async def render(staged, call, budget_s):
        from batch import Rendered

        return Rendered(payload="layout.pdf")

    monkeypatch.setattr(word, "render_toc", render)
    _write(f, [{"op": "add_bookmark", "target": "p0", "name": "terms"},
               {"op": "add_cross_reference", "bookmark": "terms", "show": "page", "target": "p1"}])
    assert _texts(f)[1] == "See page 4"

    async def none(staged, call, budget_s):
        from batch import Rendered

        return Rendered(note="no LibreOffice here")

    monkeypatch.setattr(word, "render_toc", none)
    msg = _write(f, [{"op": "update_fields"}])
    assert "updates" in msg and "PAGEREF" in msg, msg
    assert _texts(f)[1] == "See page 4"


def test_update_fields_renumbers_and_refreshes(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = f'xmlns:w="{W_NS}"'
    doc = Document()
    body = doc.element.body
    for n in (7, 3):
        body.insert(len(body) - 1, parse_xml(
            f'<w:p {w}><w:r><w:t xml:space="preserve">Figure </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/>'
            f'</w:r><w:r><w:instrText xml:space="preserve"> SEQ Figure \\* ARABIC </w:instrText></w:r><w:r>'
            f'<w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{n}</w:t></w:r><w:r>'
            f'<w:fldChar w:fldCharType="end"/></w:r></w:p>'))
    f = tmp_path / "stale.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "update_fields"}])
    assert "updates 2 fields (SEQ)" in msg, msg
    assert _texts(f)[:2] == ["Figure 1", "Figure 2"]
    msg = _write(tmp_path / "plain.docx", [{"op": "add_paragraph", "text": "x"}], create_new=True)
    msg = _write(tmp_path / "plain.docx", [{"op": "update_fields"}])
    assert "no field to update" in msg, msg


@pytest.mark.parametrize("op, why", [
    ({"op": "add_caption", "label": "1abc"}, "not a caption label"),
    ({"op": "add_caption", "bookmark": "9no"}, "not a bookmark name"),
    ({"op": "add_cross_reference", "bookmark": "missing"}, "no bookmark named"),
    ({"op": "add_cross_reference", "bookmark": "_top"}, "top of the document"),
    ({"op": "add_cross_reference", "bookmark": "fig_x", "show": "number"}, "not text or page"),
])
def test_refusals(tmp_path, op, why):
    f = tmp_path / "refuse.docx"
    _write(f, [{"op": "add_paragraph", "text": "x"}, {"op": "add_caption", "bookmark": "fig_x"}], create_new=True)
    msg = _write(f, [op])
    assert msg.startswith("Nothing saved") and why in msg, msg


def test_a_toc_from_named_styles(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = f'xmlns:w="{W_NS}"'
    doc = Document()
    doc.add_heading("Chapter", 1)
    doc.add_paragraph("Annex A", style="Title")
    body = doc.element.body
    body.insert(0, parse_xml(
        f'<w:p {w}><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o '
        f'"1-1" \\h \\t "Title,2" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r><w:t>old</w:t></w:r></w:p>'))
    body.insert(1, parse_xml(f'<w:p {w}><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'))
    f = tmp_path / "tstyles.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "update_table_of_contents"}])
    assert "lists 2 headings" in msg, msg
    entries = [(p.find(f"{_w('pPr')}/{_w('pStyle')}").get(_w("val")), "".join(t.text or "" for t in p.iter(_w("t"))))
               for p in open_document(str(f)).element.body.iter(_w("p"))
               if p.find(f"{_w('pPr')}/{_w('pStyle')}") is not None and
               re.fullmatch(r"TOC\d", p.find(f"{_w('pPr')}/{_w('pStyle')}").get(_w("val")) or "")]
    assert [(style, text.split("000")[0]) for style, text in entries] == [("TOC1", "Chapter"), ("TOC2", "Annex A")]


def _seq_paragraph(w: str, label: str, switches: str, shown: str) -> str:
    return (f'<w:p {w}><w:pPr><w:pStyle w:val="Caption"/></w:pPr><w:r><w:t xml:space="preserve">{label} </w:t></w:r>'
            f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> SEQ {label} '
            f'{switches} </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{shown}</w:t>'
            '</w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')


def test_a_sequence_with_a_chapter_switch_starts_again_at_each_chapter(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = f'xmlns:w="{W_NS}"'
    doc = Document()
    body = doc.element.body
    doc.add_heading("One", 1)
    for shown in ("7", "8"):
        body.insert(len(body) - 1, parse_xml(_seq_paragraph(w, "Figure", "\\* ARABIC \\s 1", shown)))
    doc.add_heading("Two", 1)
    doc.add_heading("Two point one", 2)
    body.insert(len(body) - 1, parse_xml(_seq_paragraph(w, "Figure", "\\* ARABIC \\s 1", "9")))
    body.insert(len(body) - 1, parse_xml(_seq_paragraph(w, "Table", "\\c", "5")))
    f = tmp_path / "chapters.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "update_fields"}])
    assert msg.startswith("Document saved"), msg
    assert [t for t in _texts(f) if t.startswith(("Figure", "Table"))] == ["Figure 1", "Figure 2", "Figure 1",
                                                                           "Table 0"]


def test_references_in_a_header_are_refreshed(tmp_path, monkeypatch):
    from docx import Document
    from docx.oxml import parse_xml

    from batch import Rendered

    w = f'xmlns:w="{W_NS}"'
    f = tmp_path / "header.docx"
    _write(f, [{"op": "add_paragraph", "text": "[chart]"}, {"op": "add_header", "text": "Header"}],
           create_new=True)
    _write(f, [{"op": "add_caption", "target": "p0", "text": "Revenue.", "bookmark": "fig_rev"}])
    doc = Document(str(f))
    header = doc.sections[0].header._element
    for instruction, shown in ((" REF fig_rev \\h ", "Figure 9"), (" PAGEREF fig_rev \\h ", "7")):
        header.append(parse_xml(
            f'<w:p {w}><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve">'
            f'{instruction}</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{shown}'
            '</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'))
    doc.save(str(f))

    async def render(staged, call, budget_s):
        return Rendered(payload="layout.pdf")

    monkeypatch.setattr(word, "render_toc", render)
    monkeypatch.setattr(fields, "read_pages", lambda pdf, names, marks: {"fig_rev": "4"})
    msg = _write(f, [{"op": "update_fields"}])
    assert msg.startswith("Document saved"), msg
    texts = ["".join(t.text or "" for t in p.iter(_w("t")))
             for p in Document(str(f)).sections[0].header._element.iter(_w("p"))]
    assert "Figure 1" in texts and "4" in texts, texts
