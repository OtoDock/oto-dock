"""Bookmarks, internal links and the table of contents: real entries linked
to their headings, PAGEREF page numbers filled from a layout or left for
Word, rebuilds that keep the field's switches and bookmarks."""

import asyncio
import re
import zipfile

import pytest

pytest.importorskip("docx")

import checks
import word
from formats.docx import fields
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

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


@pytest.fixture
def no_layout(monkeypatch):
    """The render phase finds no LibreOffice, as on a box without one."""
    from batch import Rendered

    async def render(staged, call, budget_s):
        return Rendered(note="no LibreOffice here")

    monkeypatch.setattr(word, "render_toc", render)


@pytest.fixture
def layout(monkeypatch):
    """A layout that puts every heading on page 'iv' or '7'."""
    from batch import Rendered

    async def render(staged, call, budget_s):
        return Rendered(payload="layout.pdf")

    def pages(pdf, names, marks=()):
        return {name: ("iv" if n == 0 else "7") for n, (name, _) in enumerate(names)}

    monkeypatch.setattr(word, "render_toc", render)
    monkeypatch.setattr(fields, "read_pages", pages)


def _write(path, ops, create_new=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops, "create_new": create_new}))


def _errors(path) -> list[str]:
    with checks.CheckContext(str(path), "docx") as ctx:
        return [f"{f.location}: {f.message}" for f in validate.validate_package(ctx)
                if f.severity is checks.Severity.ERROR]


def _report(tmp_path):
    f = tmp_path / "report.docx"
    _write(f, [{"op": "add_heading", "text": "Summary", "level": 1},
               {"op": "add_heading", "text": "Findings", "level": 1},
               {"op": "add_heading", "text": "Detail", "level": 2},
               {"op": "add_heading", "text": "Deep", "level": 3},
               {"op": "add_table", "headers": ["A"], "rows": [["1"]]},
               {"op": "number_headings", "levels": 2}], create_new=True)
    return f


def _entries(path) -> list[tuple[str, str, str]]:
    """(style, text, page) of each TOC entry."""
    doc = open_document(str(path))
    found = []
    for p in doc.element.body.iter(_w("p")):
        style = p.find(f"{_w('pPr')}/{_w('pStyle')}")
        if style is None or not style.get(_w("val")).startswith("TOC") or style.get(_w("val")) == "TOCHeading":
            continue
        texts = [t.text or "" for t in p.iter(_w("t"))]
        page = texts[-1] if len(texts) > 1 else ""
        found.append((style.get(_w("val")), texts[0], page))
    return found


def test_a_table_of_contents_lists_the_headings_linked_to_them(tmp_path, no_layout):
    f = _report(tmp_path)
    msg = _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before", "levels": 2}])
    assert "lists 3 headings" in msg and "their page numbers" not in msg, msg
    assert "Word fills them when the fields are updated" in msg, msg
    assert _entries(f) == [("TOC1", "1. Summary", ""), ("TOC1", "2. Findings", ""), ("TOC2", "2.1 Detail", "")]
    xml = zipfile.ZipFile(f).read("word/document.xml").decode()
    anchors = re.findall(r'<w:hyperlink w:history="1" w:anchor="(_Toc\d{9})"', xml) or \
        re.findall(r'<w:hyperlink w:anchor="(_Toc\d{9})"', xml)
    assert len(anchors) == 3
    for name in anchors:
        assert f'w:name="{name}"' in xml and f"PAGEREF {name} \\h" in xml
    assert 'w:styleId="TOC1"' in zipfile.ZipFile(f).read("word/styles.xml").decode()
    assert _errors(f) == []


def test_page_numbers_come_from_the_layout(tmp_path, layout):
    f = _report(tmp_path)
    msg = _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before", "levels": 1}])
    assert "from a LibreOffice layout" in msg, msg
    assert _entries(f) == [("TOC1", "1. Summary", "iv"), ("TOC1", "2. Findings", "7")]


def test_a_rebuild_keeps_bookmarks_switches_and_follows_new_headings(tmp_path, no_layout):
    f = _report(tmp_path)
    _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before", "levels": 2,
                "page_numbers": False}])
    before = set(re.findall(r"_Toc\d{9}", zipfile.ZipFile(f).read("word/document.xml").decode()))
    _write(f, [{"op": "add_heading", "text": "Annex", "level": 1}])
    msg = _write(f, [{"op": "update_table_of_contents"}])
    assert "lists 4 headings" in msg and "page numbers" not in msg.split("lists 4 headings")[1][:30], msg
    xml = zipfile.ZipFile(f).read("word/document.xml").decode()
    assert before <= set(re.findall(r"_Toc\d{9}", xml))
    assert '\\o "1-2" \\h \\z \\u \\n' in xml and "PAGEREF" not in xml
    assert [text for _, text, _ in _entries(f)] == ["1. Summary", "2. Findings", "2.1 Detail", "3. Annex"]


def test_editing_a_heading_keeps_its_toc_bookmark(tmp_path, no_layout):
    f = _report(tmp_path)
    _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before"}])
    msg = _write(f, [{"op": "set_paragraph_text", "target": "p2", "text": "Executive summary"}])
    assert msg.startswith("Document saved") and "broken_internal_link" not in msg, msg


def test_a_toc_with_switches_it_does_not_build_is_left_alone(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = f'xmlns:w="{W_NS}"'
    doc = Document()
    body = doc.element.body
    body.insert(0, parse_xml(f'<w:p {w}><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText '
                             f'xml:space="preserve"> TOC \\b "part2" </w:instrText></w:r><w:r><w:fldChar '
                             f'w:fldCharType="separate"/></w:r><w:r><w:t>Old entry</w:t></w:r></w:p>'))
    body.insert(1, parse_xml(f'<w:p {w}><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'))
    f = tmp_path / "custom.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "update_table_of_contents"}])
    assert "uses a switch these tools do not build (\\b)" in msg, msg
    assert "Old entry" in zipfile.ZipFile(f).read("word/document.xml").decode()


def test_a_1_3_toc_is_rebuilt_under_a_toc_heading(tmp_path, no_layout):
    from docx import Document
    from docx.oxml import parse_xml

    w = f'xmlns:w="{W_NS}"'
    doc = Document()
    doc.add_heading("Table of Contents", 1)
    body = doc.element.body
    body.insert(len(body) - 1, parse_xml(
        f'<w:p {w}><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o '
        f'"1-3" \\h \\z \\u </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>'
        f'Update table of contents to see entries</w:t><w:fldChar w:fldCharType="end"/></w:r></w:p>'))
    doc.add_heading("Real heading", 1)
    f = tmp_path / "old.docx"
    doc.save(str(f))
    _write(f, [{"op": "update_table_of_contents"}])
    paragraphs = open_document(str(f)).paragraphs
    assert paragraphs[0].style.name == "TOC Heading"
    assert [text for _, text, _ in _entries(f)] == ["Real heading"]


def test_internal_links_and_bookmarks(tmp_path):
    f = tmp_path / "links.docx"
    _write(f, [{"op": "add_paragraph", "text": "Payment terms apply."},
               {"op": "add_paragraph", "text": "See below."}], create_new=True)
    msg = _write(f, [{"op": "add_bookmark", "target": "p0", "name": "payment_terms", "on_text": "Payment terms"},
                     {"op": "add_hyperlink", "bookmark": "payment_terms", "text": "the terms", "target": "p1"}])
    assert msg.startswith("Document saved"), msg
    xml = zipfile.ZipFile(f).read("word/document.xml").decode()
    assert '<w:bookmarkStart w:id=' in xml and 'w:name="payment_terms"' in xml
    assert re.search(r'<w:hyperlink [^>]*w:anchor="payment_terms"', xml)
    msg = _write(f, [{"op": "add_bookmark", "target": "p1", "name": "PAYMENT_TERMS"}])
    assert "already has a bookmark named" in msg, msg
    msg = _write(f, [{"op": "add_hyperlink", "bookmark": "nowhere", "text": "x"}])
    assert "has no bookmark named 'nowhere'" in msg, msg
    msg = _write(f, [{"op": "add_bookmark", "target": "p1", "name": "1bad"}])
    assert "is not a bookmark name Word accepts" in msg, msg
    assert _errors(f) == []


def test_the_layout_reader_follows_each_entrys_link(tmp_path):
    fitz = pytest.importorskip("fitz")

    pdf = fitz.open()
    for _ in range(3):
        pdf.new_page()
    for index, label in enumerate(("ii", "1", "2")):
        pdf[index].insert_text((72, 800), f"~~pg:{label}:pg~~", fontsize=4)
    toc_page = pdf[0]
    toc_page.insert_text((72, 72), "1. Summary ........ 3", fontsize=11)
    toc_page.insert_text((72, 100), "2. Findings ....... 4", fontsize=11)
    for row, target in ((60, 1), (88, 2)):
        toc_page.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(70, row, 400, row + 16), "page": target,
                              "to": fitz.Point(72, 72)})
    path = tmp_path / "layout.pdf"
    pdf.save(str(path))
    pages = fields.read_pages(str(path), [("_Toc1", "1. Summary"), ("_Toc2", "2. Findings"), ("_Toc3", "Gone")])
    assert pages == {"_Toc1": "1", "_Toc2": "2"}


def test_the_probe_labels_every_page_footer_including_a_first_section_without_one(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("One")
    doc.add_section()
    doc.sections[1].different_first_page_header_footer = True
    probe = tmp_path / "probe.docx"
    fields.write_probe(doc, str(probe))
    with zipfile.ZipFile(probe) as zf:
        footers = [zf.read(n).decode() for n in zf.namelist() if n.startswith("word/footer")]
    assert len(footers) == 2 and all("~~pg:" in x and " PAGE " in x for x in footers)


def test_a_heading_with_no_letters_takes_the_next_link(tmp_path):
    fitz = pytest.importorskip("fitz")

    pdf = fitz.open()
    for _ in range(4):
        pdf.new_page()
    for index, label in enumerate(("i", "1", "2", "3")):
        pdf[index].insert_text((72, 800), f"~~pg:{label}:pg~~", fontsize=4)
    toc_page = pdf[0]
    for row, (text, target) in enumerate((("1. Alpha", 1), ("***", 2), ("2. Beta", 3))):
        y = 72 + 28 * row
        toc_page.insert_text((72, y), text, fontsize=11)
        toc_page.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(70, y - 12, 400, y + 4), "page": target,
                              "to": fitz.Point(72, 72)})
    path = tmp_path / "layout.pdf"
    pdf.save(str(path))
    pages = fields.read_pages(str(path), [("_Toc1", "1. Alpha"), ("_Toc2", "***"), ("_Toc3", "2. Beta")])
    assert pages == {"_Toc1": "1", "_Toc2": "2", "_Toc3": "3"}
    pages = fields.read_pages(str(path), [("_Toc1", "1. Alpha"), ("_Toc9", "Missing"), ("_Toc3", "2. Beta")])
    assert pages == {"_Toc1": "1", "_Toc3": "3"}


def test_the_page_numbers_are_announced_only_when_written(tmp_path, layout):
    f = _report(tmp_path)
    msg = _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before", "levels": 1}])
    assert "2 page numbers from a LibreOffice layout" in msg, msg


def test_a_link_takes_the_bookmarks_own_case(tmp_path):
    f = tmp_path / "case.docx"
    _write(f, [{"op": "add_paragraph", "text": "Terms"}, {"op": "add_paragraph", "text": "See"}], create_new=True)
    _write(f, [{"op": "add_bookmark", "target": "p0", "name": "Target_A"},
               {"op": "add_hyperlink", "bookmark": "target_a", "text": "same call", "target": "p1"}])
    _write(f, [{"op": "add_hyperlink", "bookmark": "TARGET_A", "text": "later call", "target": "p1"}])
    xml = zipfile.ZipFile(f).read("word/document.xml").decode()
    assert xml.count('w:anchor="Target_A"') == 2 and "target_a" not in xml and "TARGET_A" not in xml


def test_updating_a_toc_outside_the_body_is_refused_with_its_reason(tmp_path, no_layout):
    f = _report(tmp_path)
    _write(f, [{"op": "add_header", "text": "Head"}, {"op": "add_table_of_contents"}])
    msg = _write(f, [{"op": "update_table_of_contents", "target": "header0/p0"}])
    assert "a table of contents entry in the body" in msg and "StopIteration" not in msg, msg
