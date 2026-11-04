"""Footnotes and endnotes: added after given words or at a paragraph's end,
numbered as people see them, edited by address, deleted with their
reference, tracked when asked."""

import asyncio
import zipfile

import pytest

pytest.importorskip("docx")

import checks
import word
from formats.docx import address as addr
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


def _write(path, ops, create_new=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops, "create_new": create_new}))


def _errors(path) -> list[str]:
    with checks.CheckContext(str(path), "docx") as ctx:
        return [f"{f.location}: {f.message}" for f in validate.validate_package(ctx)
                if f.severity is checks.Severity.ERROR]


def _xml(path, name) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode()


def _doc(tmp_path, *texts):
    f = tmp_path / "report.docx"
    _write(f, [{"op": "add_paragraph", "text": t} for t in texts], create_new=True)
    return f


def _note_texts(path, kind="footnote") -> list[str]:
    doc = open_document(str(path))
    return ["".join(t.text or "" for t in el.iter(_w("t"))).strip() for _, el in addr.annotation_order(doc, kind)]


def test_a_footnote_goes_right_after_its_words(tmp_path):
    f = _doc(tmp_path, "Revenue grew 9% in the quarter.")
    msg = _write(f, [{"op": "add_footnote", "target": "p0", "after_text": "9%", "text": "Audited figures."}])
    assert "made footnote1" in msg, msg
    p = next(open_document(str(f)).element.body.iter(_w("p")))
    pieces = [("ref" if r.find(_w("footnoteReference")) is not None else "".join(t.text for t in r.iter(_w("t"))))
              for r in p.iter(_w("r"))]
    assert pieces == ["Revenue grew 9%", "ref", " in the quarter."]
    ref_run = next(r for r in p.iter(_w("r")) if r.find(_w("footnoteReference")) is not None)
    assert ref_run.find(f"{_w('rPr')}/{_w('rStyle')}").get(_w("val")) == "FootnoteReference"
    notes = _xml(f, "word/footnotes.xml")
    assert 'w:type="separator"' in notes and "Audited figures." in notes
    assert "<w:footnoteRef/>" in notes
    styles = _xml(f, "word/styles.xml")
    assert 'w:styleId="FootnoteText"' in styles and 'w:styleId="FootnoteReference"' in styles
    assert '<w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>' in \
        _xml(f, "word/settings.xml")
    assert _errors(f) == []


def test_notes_are_numbered_by_where_their_marks_stand(tmp_path):
    f = _doc(tmp_path, "First claim.", "Second claim.")
    _write(f, [{"op": "add_footnote", "target": "p1", "text": "Second source."}])
    msg = _write(f, [{"op": "add_footnote", "target": "p0", "text": "First source."}])
    assert "made footnote1" in msg, msg
    assert _note_texts(f) == ["First source.", "Second source."]
    msg = _write(f, [{"op": "set_paragraph_text", "target": "footnote2/p0", "text": "Second source, revised."}])
    assert msg.startswith("Document saved"), msg
    assert _note_texts(f) == ["First source.", "Second source, revised."]
    notes = _xml(f, "word/footnotes.xml")
    assert notes.count("<w:footnoteRef/>") == 2


def test_an_endnote_and_the_default_place(tmp_path):
    f = _doc(tmp_path, "Intro.", "Method.")
    _write(f, [{"op": "add_endnote", "text": "See the appendix."}])
    paragraphs = list(open_document(str(f)).element.body.iter(_w("p")))
    assert paragraphs[1].find(f".//{_w('endnoteReference')}") is not None
    assert _note_texts(f, "endnote") == ["See the appendix."]
    assert _errors(f) == []


def test_deleting_a_note_takes_its_reference(tmp_path):
    f = _doc(tmp_path, "Claim one.", "Claim two.")
    _write(f, [{"op": "add_footnote", "target": "p0", "text": "One."}, {"op": "add_footnote", "target": "p1",
                                                                          "text": "Two."}])
    msg = _write(f, [{"op": "delete_note", "target": "footnote1"}])
    assert "deleted the footnote and its reference" in msg, msg
    assert _note_texts(f) == ["Two."]
    assert _xml(f, "word/document.xml").count("footnoteReference") == 1
    assert _errors(f) == []


def test_a_tracked_note_deletion_waits_for_acceptance(tmp_path):
    f = _doc(tmp_path, "Claim.")
    _write(f, [{"op": "add_footnote", "text": "Source."}])
    msg = _write(f, [{"op": "delete_note", "target": "footnote1", "track": True}])
    assert "goes when the change is accepted" in msg, msg
    assert _note_texts(f) == ["Source."]
    _write(f, [{"op": "accept_all_changes"}])
    assert _note_texts(f) == []
    assert "footnoteReference" not in _xml(f, "word/document.xml")


def test_a_tracked_note_is_an_insertion(tmp_path):
    f = _doc(tmp_path, "Claim.")
    _write(f, [{"op": "add_footnote", "text": "Source.", "track": True, "author": "Editor"}])
    body = _xml(f, "word/document.xml")
    assert '<w:ins w:id=' in body and 'w:author="Editor"' in body
    _write(f, [{"op": "reject_all_changes"}])
    assert "footnoteReference" not in _xml(f, "word/document.xml")
    assert _note_texts(f) == []


@pytest.mark.parametrize("op, why", [
    ({"op": "add_footnote", "target": "p0", "after_text": "absent", "text": "x"}, "is not in the paragraph"),
    ({"op": "add_footnote", "target": "header0/p0", "text": "x"}, "cannot hold a footnote"),
    ({"op": "add_footnote", "target": "footnote1/p0", "text": "x"}, "cannot hold a footnote"),
    ({"op": "delete_note", "target": "p0"}, "is not a footnote or an endnote"),
    ({"op": "delete_note", "target": "footnote9"}, "footnote9 does not exist"),
])
def test_refusals(tmp_path, op, why):
    f = _doc(tmp_path, "Claim.")
    _write(f, [{"op": "add_header", "text": "Head"}, {"op": "add_footnote", "text": "Source."}])
    msg = _write(f, [op])
    assert msg.startswith("Nothing saved") and why in msg, msg


def test_a_note_mark_never_lands_inside_a_link(tmp_path):
    f = _doc(tmp_path, "Read more.")
    _write(f, [{"op": "add_hyperlink", "url": "https://example.com", "text": "the site", "paragraph_index": 0}])
    msg = _write(f, [{"op": "add_footnote", "target": "p0", "after_text": "the site", "text": "x"}])
    assert "is in a link" in msg or "crosses a link" in msg, msg


def test_a_note_goes_after_words_that_span_a_tracked_change(tmp_path):
    f = _doc(tmp_path, "Late interest is 8% a year.")
    msg = _write(f, [{"op": "search_and_replace", "find": "8%", "replace": "6%", "track": True},
                     {"op": "add_footnote", "target": "p0", "after_text": "6%", "text": "Simple interest."}])
    assert msg.startswith("Document saved"), msg
    msg = _write(f, [{"op": "add_endnote", "target": "p0", "after_text": "{-8-}{+6+}% a year", "text": "Per year."}])
    assert msg.startswith("Document saved"), msg
