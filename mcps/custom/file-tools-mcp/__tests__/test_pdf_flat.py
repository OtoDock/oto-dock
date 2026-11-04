"""Filling a printed form (no fields): text placed beside the labels on the
page, or at x, y; every entry found before anything is drawn."""

import asyncio
import sys
import unicodedata
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import fonts
import pdf as pdf_mod
import shared


def _face() -> str | None:
    try:
        face = fonts.resolve("Liberation Sans").face
    except LookupError:
        return None
    return face.path if not fonts.missing_glyphs("Όνομα", "Liberation Sans") else None


pytestmark = pytest.mark.skipif(_face() is None, reason="no font with Greek installed here")


async def _ident(p, writing=False, **kw):
    return p


async def _nothing(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _nothing)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _printed_form(path: Path, labels=(("Όνομα:", 100), ("Ημερομηνία:", 140), ("Υπογραφή:", 180))) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    for text, y in labels:
        page.insert_text((72, y), text, fontsize=11, fontname="F0", fontfile=_face())
        page.insert_text((160, y), "____________________", fontsize=11, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def _edit(path: Path, ops) -> str:
    return asyncio.run(pdf_mod.handle_edit_pdf({"path": str(path), "operations": ops}))


def test_text_goes_beside_each_label_on_its_baseline(tmp_path):
    src = _printed_form(tmp_path / "a.pdf")
    out = _edit(src, [{"op": "fill_flat_form", "entries": [
        {"label": "Όνομα:", "text": "Μαρία Κωνσταντίνου"},
        {"label": "Ημερομηνία:", "text": "30/10/2026"},
    ]}])
    assert out.startswith("PDF saved:") and "2 text(s) placed" in out
    assert "runs into" not in out  # the underscores are the line to write on
    page = fitz.open(str(src))[0]
    words = {w[4]: w for w in page.get_text("words")}
    label, name = words["Όνομα:"], words["Μαρία"]
    assert name[0] > label[2] and abs(name[3] - label[3]) < 2


def test_below_occurrence_and_coordinates(tmp_path):
    src = _printed_form(tmp_path / "b.pdf", labels=(("Τηλέφωνο:", 100), ("Τηλέφωνο:", 200)))
    out = _edit(src, [{"op": "fill_flat_form", "font_size": 10, "entries": [
        {"label": "Τηλέφωνο:", "occurrence": 2, "text": "210 1234567"},
        {"label": "Τηλέφωνο:", "position": "below", "text": "κινητό"},
        {"page": 0, "x": 400, "y": 300, "text": "X", "bold": True},
    ]}])
    assert out.startswith("PDF saved:"), out
    words = {w[4]: w for w in fitz.open(str(src))[0].get_text("words")}
    assert abs(words["1234567"][3] - 200) < 4
    assert words["κινητό"][1] > 100
    assert abs(words["X"][0] - 400) < 1


def test_a_label_that_is_not_there_changes_nothing(tmp_path):
    src = _printed_form(tmp_path / "c.pdf")
    before = src.read_bytes()
    out = _edit(src, [{"op": "fill_flat_form", "entries": [
        {"label": "Όνομα:", "text": "ok"}, {"label": "Διεύθυνση:", "text": "x"},
        {"label": "Όνομα:", "occurrence": 3, "text": "y"}, {"text": "no place"}]}])
    assert out.startswith("Nothing saved:")
    assert "'Διεύθυνση:' was not found" in out and "occurrence 3 does not exist" in out
    assert "needs a label, or x and y" in out
    assert src.read_bytes() == before


def test_a_decomposed_label_is_found(tmp_path):
    src = _printed_form(tmp_path / "d.pdf")
    out = _edit(src, [{"op": "fill_flat_form", "entries": [
        {"label": unicodedata.normalize("NFD", "Όνομα:"), "text": "Νίκος"}]}])
    assert out.startswith("PDF saved:"), out


def test_text_running_into_other_text_or_off_the_page_is_warned(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Name:", fontsize=11)
    page.insert_text((140, 100), "Printed", fontsize=11)
    src = tmp_path / "e.pdf"
    doc.save(str(src))
    out = _edit(src, [{"op": "fill_flat_form", "entries": [
        {"label": "Name:", "text": "A long answer"}, {"page": 0, "x": 560, "y": 300, "text": "far right text"}]}])
    assert "runs into text already" in out and "past the right edge" in out
