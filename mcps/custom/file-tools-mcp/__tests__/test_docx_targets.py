"""Targets: addresses from the read, the call's snapshot, ids that survive
edits, expect_text and the lost update, where new blocks land, and the
structure Word needs kept."""

import asyncio
import re
import zipfile

import pytest

pytest.importorskip("docx")

import word

W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"


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


def _texts(path) -> list[str]:
    from docx import Document

    return [p.text for p in Document(str(path)).paragraphs]


def _doc_with(tmp_path, texts, name="doc.docx"):
    f = tmp_path / name
    _write(f, [{"op": "add_paragraph", "text": t} for t in texts], create_new=True)
    return f


def _id_of(path, index) -> str:
    from docx import Document

    return Document(str(path)).paragraphs[index]._p.get(W14)


def test_positional_targets_name_the_read_not_the_moving_document(tmp_path):
    f = _doc_with(tmp_path, [f"P{i}" for i in range(8)])
    msg = _write(f, [{"op": "insert_paragraph", "index": 0, "text": "new first"},
                     {"op": "delete_paragraph", "target": "p5"}])
    assert "Failed:" not in msg
    assert _texts(f) == ["new first", "P0", "P1", "P2", "P3", "P4", "P6", "P7"]


def test_a_legacy_index_after_a_shift_warns(tmp_path):
    f = _doc_with(tmp_path, [f"P{i}" for i in range(8)])
    msg = _write(f, [{"op": "insert_paragraph", "target": "p3", "text": "X", "position": "before"},
                     {"op": "delete_paragraph", "index": 7}])
    assert "index 7 counts the document as it is now" in msg and 'target: "p7"' in msg
    assert _texts(f) == ["P0", "P1", "P2", "X", "P3", "P4", "P5", "P7"]


def test_new_blocks_are_reported_with_final_addresses_and_ids(tmp_path):
    f = _doc_with(tmp_path, ["A", "B"])
    msg = _write(f, [{"op": "add_heading", "text": "Title", "target": "p0", "position": "before"},
                     {"op": "add_list", "items": ["x", "y"], "target": "p1"},
                     {"op": "insert_paragraph", "index": 0, "text": "Very first"}])
    assert re.search(r"op #0 add_heading: made p1 #[0-9A-F]{8}", msg)
    assert re.search(r"op #1 add_list: made p4 to p5 \(first #[0-9A-F]{8}\)", msg)
    assert "index 0 counts the document as it is now" in msg
    assert re.search(r"op #2 insert_paragraph: made p0 #[0-9A-F]{8}", msg)
    assert _texts(f) == ["Very first", "Title", "A", "B", "x", "y"]


def test_every_paragraph_of_a_touched_part_gets_an_id(tmp_path):
    from docx import Document

    doc = Document()
    for t in ("one", "two"):
        doc.add_paragraph(t)
    doc.sections[0].header.paragraphs[0].text = "head"
    f = tmp_path / "plain.docx"
    doc.save(str(f))
    with zipfile.ZipFile(f) as zf:
        header_before = zf.read("word/header1.xml")
    _write(f, [{"op": "add_paragraph", "text": "three"}])
    ids = [p._p.get(W14) for p in Document(str(f)).paragraphs]
    assert all(re.fullmatch(r"[0-9A-F]{8}", i) for i in ids) and len(set(ids)) == 3
    with zipfile.ZipFile(f) as zf:
        assert zf.read("word/header1.xml") == header_before


def test_an_id_follows_its_paragraph(tmp_path):
    f = _doc_with(tmp_path, ["Alpha", "Beta", "Gamma"])
    beta = _id_of(f, 1)
    msg = _write(f, [{"op": "insert_paragraph", "index": 0, "text": "New"},
                     {"op": "set_paragraph_text", "target": f"#{beta}", "text": "Beta 2", "expect_text": "Beta"}])
    assert "Failed:" not in msg
    assert _texts(f) == ["New", "Alpha", "Beta 2", "Gamma"]
    assert _id_of(f, 2) == beta


def test_the_lost_update_is_refused(tmp_path):
    f = _doc_with(tmp_path, ["Pay in 30 days"])
    pid = _id_of(f, 0)
    _write(f, [{"op": "set_paragraph_text", "target": f"#{pid}", "text": "Pay in 60 days"}])
    msg = _write(f, [{"op": "set_paragraph_text", "target": f"#{pid}", "text": "Pay in 45 days",
                      "expect_text": "Pay in 30 days"}])
    assert msg.startswith("Nothing saved:") and "someone changed it since you read it" in msg
    msg = _write(f, [{"op": "insert_paragraph", "target": f"#{pid}", "text": "Note",
                      "expect_text": "Pay in 30 days"}])
    assert "Document saved" in msg and "now starts with 'pay in 60 days'" in msg


def test_a_gone_id_is_found_again_by_its_text(tmp_path):
    f = _doc_with(tmp_path, ["Payment terms apply", "Other"])
    msg = _write(f, [{"op": "add_paragraph", "target": "#00ABCDEF", "expect_text": "Payment terms",
                      "text": "Note below the terms"}])
    assert "no paragraph has id 00ABCDEF now; used p0, which starts with 'Payment terms'" in msg
    assert _texts(f) == ["Payment terms apply", "Note below the terms", "Other"]
    for op in ({"op": "delete_paragraph"}, {"op": "set_paragraph_format", "alignment": "center"}):
        msg = _write(f, [{**op, "target": "#00ABCDEF", "expect_text": "Payment terms"}])
        assert msg.startswith("Nothing saved:") and "does not guess" in msg
    msg = _write(f, [{"op": "set_paragraph_format", "target": "#00ABCDEF", "alignment": "right"}])
    assert msg.startswith("Nothing saved:") and "give expect_text" in msg


def test_two_paragraphs_with_the_same_text_are_never_guessed(tmp_path):
    f = _doc_with(tmp_path, ["Signature:", "Body", "Signature:"])
    msg = _write(f, [{"op": "set_paragraph_format", "target": "p1", "expect_text": "Signature:",
                      "alignment": "center"}])
    assert msg.startswith("Nothing saved:") and "2 paragraphs start with 'Signature:' (p0, p2)" in msg


def test_a_position_deleted_earlier_in_the_call_is_named(tmp_path):
    f = _doc_with(tmp_path, ["A", "B"])
    msg = _write(f, [{"op": "delete_paragraph", "target": "p1"},
                     {"op": "set_paragraph_text", "target": "p1", "text": "x", "expect_text": "B"}])
    assert "p1 was deleted by op #0" in msg


def test_duplicate_ids_fail_naming_both(tmp_path):
    from docx import Document

    doc = Document()
    for t in ("one", "two"):
        doc.add_paragraph(t)._p.set(W14, "0000ABCD")
    f = tmp_path / "dup.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "set_paragraph_text", "target": "#0000ABCD", "text": "x"}])
    assert "two paragraphs share id 0000ABCD (p0, p1)" in msg


def test_blocks_land_in_cells_and_headers(tmp_path):
    from docx import Document

    f = tmp_path / "cells.docx"
    _write(f, [{"op": "add_table", "headers": ["A", "B"], "rows": [["1", "2"]]},
               {"op": "add_header", "text": "Head"}], create_new=True)
    msg = _write(f, [
        {"op": "add_paragraph", "text": "cell top", "target": "t0/r1/c0", "position": "start"},
        {"op": "add_table", "headers": ["x"], "target": "t0/r1/c1"},
        {"op": "add_paragraph", "text": "header end", "target": "header0"},
    ])
    assert "Failed:" not in msg
    doc = Document(str(f))
    cell = doc.tables[0].cell(1, 0)
    assert [p.text for p in cell.paragraphs] == ["cell top", "1"]
    other = doc.tables[0].cell(1, 1)._tc
    assert other[-1].tag.endswith("}p") and other.find(".//{*}tbl") is not None
    assert [p.text for p in doc.sections[0].header.paragraphs] == ["Head", "header end"]


@pytest.mark.parametrize("op, message", [
    ({"op": "add_paragraph", "text": "x", "target": "p0", "position": "start"}, "position is after or before"),
    ({"op": "add_paragraph", "text": "x", "target": "t0/r0/c0", "position": "after"}, "position is start or end"),
    ({"op": "add_paragraph", "text": "x", "position": "before"}, "position needs a target"),
    ({"op": "delete_paragraph", "target": "p0", "index": 0}, "give target or index, not both"),
    ({"op": "delete_paragraph", "target": "t0"}, "target t0 is a table, not a paragraph"),
    ({"op": "add_paragraph", "text": "x", "target": "p9"}, "p9 does not exist: there are 2 paragraphs"),
])
def test_bad_targets_name_the_fix(tmp_path, op, message):
    f = tmp_path / "t.docx"
    _write(f, [{"op": "add_paragraph", "text": "a"}, {"op": "add_table", "headers": ["h"]},
               {"op": "add_paragraph", "text": "b"}], create_new=True)
    msg = _write(f, [op])
    assert msg.startswith("Nothing saved:") and message in msg


def test_set_paragraph_text_keeps_style_and_the_first_runs_formatting(tmp_path):
    from docx import Document

    f = tmp_path / "keep.docx"
    _write(f, [{"op": "add_heading", "text": "Old title", "level": 2},
               {"op": "add_paragraph", "text": "Bold red", "bold": True, "color": "FF0000"}], create_new=True)
    _write(f, [{"op": "set_paragraph_text", "target": "p0", "text": "New title"},
               {"op": "set_paragraph_text", "target": "p1", "text": "Still bold", "italic": True}])
    doc = Document(str(f))
    assert doc.paragraphs[0].style.name == "Heading 2" and doc.paragraphs[0].text == "New title"
    run = doc.paragraphs[1].runs[0]
    assert run.text == "Still bold" and run.bold and run.italic and str(run.font.color.rgb) == "FF0000"


def test_table_targets(tmp_path):
    from docx import Document

    f = tmp_path / "tables.docx"
    _write(f, [{"op": "add_table", "headers": ["A"], "rows": [["1"], ["2"]]},
               {"op": "add_table", "headers": ["B"]}], create_new=True)
    msg = _write(f, [{"op": "add_table_row", "target": "t0/r1", "values": ["1.5"]},
                     {"op": "delete_table_row", "target": "t0/r2", "expect_text": "2"},
                     {"op": "delete_table", "target": "t1", "expect_text": "B"}])
    assert "Failed:" not in msg
    doc = Document(str(f))
    assert len(doc.tables) == 1
    assert [r.cells[0].text for r in doc.tables[0].rows] == ["A", "1", "1.5"]


def test_structure_is_kept(tmp_path):
    from docx import Document

    f = tmp_path / "structure.docx"
    _write(f, [{"op": "add_table", "headers": ["A"]}, {"op": "add_paragraph", "text": "one"},
               {"op": "add_section_break"}, {"op": "add_paragraph", "text": "two"},
               {"op": "add_table_of_contents"}], create_new=True)
    msg = _write(f, [{"op": "delete_paragraph", "target": "t0/r0/c0/p0"}])
    assert "the only paragraph of its table cell" in msg and "set_paragraph_text" in msg
    toc = next(i for i, t in enumerate(_texts(f)) if t.startswith("No headings yet"))
    msg = _write(f, [{"op": "delete_paragraph", "index": toc}])
    assert msg.startswith("Document saved") or "spans paragraphs" in msg
    msg = _write(f, [{"op": "delete_paragraph", "target": "p1"}])
    assert "Failed:" not in msg and "the section break p1 carried moved to the paragraph before it" in \
        msg.replace("target p1", "p1")
    assert len(Document(str(f)).sections) == 2


def _package_errors(path) -> list[str]:
    import checks
    from ooxml import validate

    with checks.CheckContext(str(path), "docx") as ctx:
        return [f"{f.location}: {f.message}" for f in validate.validate_package(ctx)
                if f.severity is checks.Severity.ERROR]


def test_a_moved_section_break_lands_in_schema_order(tmp_path):
    import copy

    from docx import Document
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = Document()
    body = doc.element.body
    body.insert(0, parse_xml(
        f'<w:p {w}><w:pPr><w:jc w:val="center"/><w:rPr><w:b/></w:rPr><w:pPrChange w:id="7" w:author="Reviewer">'
        f'<w:pPr/></w:pPrChange></w:pPr><w:r><w:t>keeps its own properties</w:t></w:r></w:p>'))
    ending = parse_xml(f'<w:p {w}><w:pPr/><w:r><w:t>ends the first section</w:t></w:r></w:p>')
    ending[0].append(copy.deepcopy(body.find(f"{{{body.nsmap['w']}}}sectPr")))
    body.insert(1, ending)
    body.insert(2, parse_xml(f'<w:p {w}><w:r><w:t>second section</w:t></w:r></w:p>'))
    f = tmp_path / "sections.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "delete_paragraph", "target": "p1"}])
    assert "moved to the paragraph before it" in msg, msg
    ppr = Document(str(f)).paragraphs[0]._p.pPr
    assert [child.tag.rsplit("}", 1)[1] for child in ppr] == ["jc", "rPr", "sectPr", "pPrChange"]
    assert _package_errors(f) == []


def test_new_text_keeps_the_formatting_but_not_its_tracked_change(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {w}><w:r><w:rPr><w:b/><w:rPrChange w:id="5" w:author="Reviewer"><w:rPr/></w:rPrChange></w:rPr>'
        f'<w:t>old words</w:t></w:r></w:p>'))
    f = tmp_path / "tracked.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "set_paragraph_text", "target": "p0", "runs": [{"text": "new "}, {"text": "words"}]}])
    assert msg.startswith("Document saved"), msg
    with zipfile.ZipFile(f) as zf:
        assert b"rPrChange" not in zf.read("word/document.xml")
    runs = Document(str(f)).paragraphs[0].runs
    assert [(r.text, r.bold) for r in runs] == [("new ", True), ("words", True)]
    assert _package_errors(f) == []


def test_a_field_that_spans_paragraphs_is_never_half_deleted(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {w}><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> TOC </w:instrText></w:r>'
        f'<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Entry one</w:t></w:r></w:p>'))
    doc.element.body.insert(1, parse_xml(f'<w:p {w}><w:r><w:t>Entry two</w:t></w:r>'
                                         f'<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'))
    f = tmp_path / "field.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "delete_paragraph", "target": "p0"}])
    assert msg.startswith("Nothing saved:") and "a field that spans paragraphs" in msg


def test_text_box_paragraphs_are_left_to_search_and_replace(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    wps = 'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {w} {wps}><w:r><w:drawing><wps:txbx><w:txbxContent><w:p><w:r><w:t>Boxed</w:t></w:r></w:p>'
        f"</w:txbxContent></wps:txbx></w:drawing></w:r></w:p>"))
    f = tmp_path / "box.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "set_paragraph_text", "target": "p0/textbox0/p0", "text": "x"}])
    assert "inside a text box" in msg and "search_and_replace" in msg
