"""Text written into a PDF: every text operation draws in a font
embedded in the file, so Greek and accents are text, not dots, in every
viewer."""

import asyncio
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import fonts
import pdf as pdf_mod
import shared
from formats.pdf import text as pdf_text

_GREEK = "Εγκρίθηκε από τη Διεύθυνση"


def _has_greek_font() -> bool:
    try:
        return not fonts.missing_glyphs(_GREEK, "Liberation Sans")
    except LookupError:
        return False


pytestmark = pytest.mark.skipif(not _has_greek_font(), reason="no font with Greek installed here")


async def _ident(p, writing=False, **kw):
    return p


async def _nothing(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _nothing)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _pdf(path: Path, text: str = "Draft v1", pages: int = 1) -> Path:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page().insert_text((72, 100), text, fontsize=12, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def _edit(path: Path, ops) -> str:
    return asyncio.run(pdf_mod.handle_edit_pdf({"path": str(path), "operations": ops}))


def _embedded(path: Path, page: int = 0) -> list:
    doc = fitz.open(str(path))
    return [f for f in doc.get_page_fonts(page) if f[1] == "ttf" and f[4].startswith("OtoF")]


def test_add_text_writes_greek_as_text_in_an_embedded_font(tmp_path):
    src = _pdf(tmp_path / "a.pdf")
    out = _edit(src, [{"op": "add_text", "text": _GREEK, "x": 72, "y": 200, "font_size": 14}])
    assert out.startswith("PDF saved:")
    assert _GREEK in fitz.open(str(src))[0].get_text()
    assert _embedded(src)


def test_a_base14_name_draws_its_metric_twin():
    assert pdf_text.style_of("helv") == ("Liberation Sans", False, False)
    assert pdf_text.style_of("tibo") == ("Liberation Serif", True, False)
    assert pdf_text.style_of("Courier-Oblique") == ("Liberation Mono", False, True)
    assert pdf_text.style_of("Noto Serif", bold=True) == ("Noto Serif", True, False)
    assert pdf_text.is_builtin("china-s") and pdf_text.is_builtin("zadb") and not pdf_text.is_builtin("helv")


def test_watermark_and_replace_text_write_greek(tmp_path):
    src = _pdf(tmp_path / "b.pdf", "Status: Draft")
    out = _edit(src, [{"op": "add_watermark", "text": "ΠΡΟΣΧΕΔΙΟ"},
                      {"op": "replace_text", "find": "Draft", "replace": "Τελικό"}])
    assert out.startswith("PDF saved:"), out
    text = fitz.open(str(src))[0].get_text()
    assert "ΠΡΟΣΧΕΔΙΟ" in text and "Τελικό" in text and "Draft" not in text


def test_two_calls_on_one_page_keep_each_text(tmp_path):
    """A font name means the same glyphs on a page: the second call's
    subset gets its own name, so neither text turns into empty glyphs."""
    src = _pdf(tmp_path / "c.pdf")
    _edit(src, [{"op": "add_text", "text": "abc", "y": 150}])
    _edit(src, [{"op": "add_text", "text": "Δέλτα", "y": 180}])
    text = fitz.open(str(src))[0].get_text()
    assert "abc" in text and "Δέλτα" in text and "\x00" not in text


def test_page_minus_one_is_the_last_page(tmp_path):
    src = _pdf(tmp_path / "d.pdf", pages=3)
    _edit(src, [{"op": "add_text", "page": -1, "text": "τέλος"}])
    doc = fitz.open(str(src))
    assert "τέλος" in doc[2].get_text() and "τέλος" not in doc[0].get_text()


def test_a_cjk_builtin_name_still_draws_cjk(tmp_path):
    src = _pdf(tmp_path / "e.pdf")
    out = _edit(src, [{"op": "add_text", "text": "中文", "font": "china-s"}])
    assert out.startswith("PDF saved:")
    assert "中文" in fitz.open(str(src))[0].get_text()


def test_a_font_that_is_not_installed_warns_and_draws_a_substitute(tmp_path):
    src = _pdf(tmp_path / "f.pdf")
    out = _edit(src, [{"op": "add_text", "text": "hello", "font": "Libertion Sans"}])
    assert out.startswith("PDF saved:") and "is not installed" in out
    assert "hello" in fitz.open(str(src))[0].get_text()


def _embedded_family(path: Path, page: int = 0) -> str:
    from fontTools.ttLib import TTFont

    doc = fitz.open(str(path))
    xref = _embedded(path, page)[0][0]
    font = TTFont(__import__("io").BytesIO(doc.extract_font(xref)[3]))
    return font["name"].getDebugName(1)


@pytest.mark.parametrize("family", ["Segoe UI", "Aptos", "Open Sans", "Verdana"])
def test_a_missing_sans_family_is_drawn_in_a_sans_face(tmp_path, family):
    """The office checks measure a family the image lacks in LibreOffice's
    fallback, a serif face; text the PDF writer draws keeps its kind."""
    src = _pdf(tmp_path / "g.pdf")
    out = _edit(src, [{"op": "add_text", "text": "Quarterly report", "font": family}])
    assert out.startswith("PDF saved:"), out
    drawn = _embedded_family(src)
    assert pdf_text.kind_of(drawn) == pdf_text.DEFAULT_FAMILY, drawn
    assert f"so the text is drawn in {drawn}" in out


def test_each_kind_of_missing_family_keeps_its_kind():
    assert pdf_text.kind_of(pdf_text.pick_face("x", "Georgia").face.family) == pdf_text.SERIF_FAMILY
    assert pdf_text.kind_of(pdf_text.pick_face("x", "Palatino Linotype").face.family) == pdf_text.SERIF_FAMILY
    assert pdf_text.kind_of(pdf_text.pick_face("x", "Consolas").face.family) == pdf_text.MONO_FAMILY
    # A metric twin stays: Arial is drawn in Liberation Sans.
    assert pdf_text.pick_face("x", "Arial").face.family == "Liberation Sans"


def test_a_subset_holds_only_the_characters_written():
    face = pdf_text.pick_face("Αβγ", "Liberation Sans").face
    small = pdf_text.subset_bytes(face, "Αβγ")
    assert len(small) < 40_000 < Path(face.path).stat().st_size
    assert pdf_text.subset_bytes(face, "Αβγ") == small  # the same text, the same bytes, the same name
