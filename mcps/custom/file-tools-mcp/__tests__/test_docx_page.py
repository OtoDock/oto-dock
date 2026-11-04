"""Page orientation and size: the long side follows the orientation, so a
repeated request changes nothing, and portrait is reachable again."""

import asyncio

import pytest

pytest.importorskip("docx")

import word

A4 = (11906, 16838)
LETTER = (12240, 15840)


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


def _pages(path) -> list[tuple[int, int, int]]:
    from docx import Document

    return [(s.page_width.twips, s.page_height.twips, int(s.orientation))
            for s in Document(str(path)).sections]


def test_landscape_twice_stays_landscape(tmp_path):
    f = tmp_path / "wide.docx"
    msg = _write(f, [{"op": "set_page_setup", "orientation": "landscape"},
                     {"op": "set_page_setup", "orientation": "landscape"}])
    assert "Warnings" not in msg
    assert _pages(f) == [(A4[1], A4[0], 1)]


def test_back_to_portrait(tmp_path):
    f = tmp_path / "turn.docx"
    _write(f, [{"op": "set_page_setup", "orientation": "landscape"}])
    _write(f, [{"op": "set_page_setup", "orientation": "Portrait"}])
    assert _pages(f) == [(A4[0], A4[1], 0)]


def test_section_break_after_a_landscape_section(tmp_path):
    f = tmp_path / "mixed.docx"
    _write(f, [
        {"op": "set_page_setup", "orientation": "landscape"},
        {"op": "add_paragraph", "text": "wide"},
        {"op": "add_section_break", "orientation": "landscape"},
        {"op": "add_paragraph", "text": "still wide"},
        {"op": "add_section_break", "orientation": "portrait"},
        {"op": "add_paragraph", "text": "tall"},
    ])
    assert _pages(f) == [(A4[1], A4[0], 1), (A4[1], A4[0], 1), (A4[0], A4[1], 0)]


def test_page_size_keeps_the_orientation(tmp_path):
    f = tmp_path / "letter.docx"
    _write(f, [{"op": "set_page_setup", "orientation": "landscape"},
               {"op": "set_page_setup", "page_size": "letter"}])
    assert _pages(f) == [(LETTER[1], LETTER[0], 1)]


def test_size_and_orientation_in_one_op(tmp_path):
    f = tmp_path / "a3.docx"
    _write(f, [{"op": "set_page_setup", "page_size": "A3", "orientation": "landscape",
                "margin_left": 0.5}])
    from docx import Document

    section = Document(str(f)).sections[0]
    assert (section.page_width.twips, section.page_height.twips) == (23811, 16838)
    assert section.left_margin.twips == 720


def test_section_break_with_a_page_size(tmp_path):
    f = tmp_path / "sizes.docx"
    _write(f, [{"op": "add_paragraph", "text": "A4"},
               {"op": "add_section_break", "page_size": "a5"}])
    assert _pages(f) == [(A4[0], A4[1], 0), (8391, 11906, 0)]


@pytest.mark.parametrize("op, message", [
    ({"op": "set_page_setup", "orientation": "sideways", "margin_left": 2},
     "orientation 'sideways' is not one of: portrait, landscape."),
    ({"op": "set_page_setup", "page_size": "B5"},
     "page_size 'B5' is not one of: a4, letter, legal, a3, a5."),
    ({"op": "add_section_break", "orientation": "sideways"},
     "orientation 'sideways' is not one of: portrait, landscape."),
    ({"op": "add_section_break", "margin_left": "abc"}, "margin_left"),
    ({"op": "set_page_setup", "orientation": "landscape", "margin_top": "wide"}, "margin_top"),
])
def test_bad_values_fail_the_op_and_change_nothing(tmp_path, op, message):
    from docx import Document

    f = tmp_path / "bad.docx"
    _write(f, [{"op": "add_paragraph", "text": "base"}])
    before = f.read_bytes()
    msg = _write(f, [op])
    assert msg.startswith("Nothing saved:") and message in msg
    assert f.read_bytes() == before
    msg = _write(f, [op, {"op": "add_paragraph", "text": "kept"}], best_effort=True)
    assert message in msg and "Document saved" in msg
    doc = Document(str(f))
    assert len(doc.sections) == 1
    section = doc.sections[0]
    assert (section.page_width.twips, section.page_height.twips) == A4
    assert section.left_margin.twips == 1440
    assert [p.text for p in doc.paragraphs] == ["base", "kept"]


def test_insert_at_the_end_keeps_the_section_properties_last(tmp_path):
    from docx import Document

    f = tmp_path / "end.docx"
    _write(f, [{"op": "add_paragraph", "text": "first"},
               {"op": "insert_paragraph", "index": 1, "text": "appended"}])
    doc = Document(str(f))
    body = doc.element.body
    assert body[-1].tag.endswith("}sectPr")
    assert [p.text for p in doc.paragraphs] == ["first", "appended"]


@pytest.mark.parametrize("width, height, orient, shown", [
    (12240, 15840, "landscape", "Letter portrait (8.50 x 11.00 in)"),
    (15840, 12240, None, "Letter landscape (11.00 x 8.50 in)"),
])
def test_read_takes_orientation_from_the_page_size(tmp_path, width, height, orient, shown):
    """Word draws the page from its width and height: a stale w:orient (what
    landscape asked twice used to leave) does not turn it."""
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document()
    pg_sz = doc.sections[0]._sectPr.find(qn("w:pgSz"))
    pg_sz.set(qn("w:w"), str(width))
    pg_sz.set(qn("w:h"), str(height))
    if orient:
        pg_sz.set(qn("w:orient"), orient)
    f = tmp_path / "stale.docx"
    doc.save(str(f))
    assert f"**Page**: {shown}" in word.read_docx(str(f))
