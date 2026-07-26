"""edit_pdf text operations: replace_text + redact.

Both are redaction-based (content actually removed, not overlaid), with
style-matched reinsertion for replace_text. Needs pymupdf; skips where
it's absent (the MCP's venv / image CI has it).
"""

import asyncio
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pdf as pdf_mod  # noqa: E402


async def _async_ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _bypass_platform(monkeypatch):
    """Identity path resolution + swallowed preview push."""
    monkeypatch.setattr(pdf_mod, "_resolve_path", _async_ident)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)

    async def _noop_preview(path):
        pass

    monkeypatch.setattr(pdf_mod, "_push_preview", _noop_preview)


def _make_pdf(path: Path, texts_per_page: list[list[str]]):
    doc = fitz.open()
    for texts in texts_per_page:
        page = doc.new_page()
        y = 100
        for t in texts:
            page.insert_text(fitz.Point(72, y), t, fontsize=12, fontname="helv")
            y += 30
    doc.save(str(path))
    doc.close()


def _edit(path: Path, ops: list[dict]) -> str:
    return asyncio.run(
        pdf_mod.handle_edit_pdf({"path": str(path), "operations": ops}))


def _text(path: Path) -> str:
    doc = fitz.open(str(path))
    out = "".join(p.get_text() for p in doc)
    doc.close()
    return out


def test_replace_text_replaces_and_reports(tmp_path):
    f = tmp_path / "a.pdf"
    _make_pdf(f, [["Quarterly report for ACME Corp"],
                  ["ACME Corp confidential appendix"]])
    out = _edit(f, [{"type": "replace_text", "find": "ACME Corp",
                     "replace": "Umbrella Inc"}])
    assert "2 occurrence(s)" in out and "2 page(s)" in out
    txt = _text(f)
    assert "ACME Corp" not in txt
    assert txt.count("Umbrella Inc") == 2


def test_replace_text_empty_replace_deletes(tmp_path):
    f = tmp_path / "b.pdf"
    _make_pdf(f, [["keep this line", "REMOVE-ME entirely"]])
    out = _edit(f, [{"type": "replace_text", "find": "REMOVE-ME", "replace": ""}])
    assert "deleted" in out
    txt = _text(f)
    assert "REMOVE-ME" not in txt
    assert "keep this line" in txt


def test_replace_text_case_handling(tmp_path):
    f = tmp_path / "c.pdf"
    _make_pdf(f, [["Project Phoenix status"]])
    # Default: case-insensitive match works.
    out = _edit(f, [{"type": "replace_text", "find": "project phoenix",
                     "replace": "Project Dragon"}])
    assert "1 occurrence(s)" in out
    assert "Project Dragon" in _text(f)

    f2 = tmp_path / "c2.pdf"
    _make_pdf(f2, [["Project Phoenix status"]])
    # case_sensitive: wrong-case needle finds nothing.
    out = _edit(f2, [{"type": "replace_text", "find": "project phoenix",
                      "replace": "X", "case_sensitive": True}])
    assert "not found" in out
    assert "Project Phoenix" in _text(f2)


def test_replace_text_long_replacement_shrinks_but_lands(tmp_path):
    f = tmp_path / "d.pdf"
    _make_pdf(f, [["ID: X1"]])
    out = _edit(f, [{"type": "replace_text", "find": "X1",
                     "replace": "EXTREMELY-LONG-IDENTIFIER-REPLACEMENT"}])
    assert "1 occurrence(s)" in out
    assert "EXTREMELY-LONG-IDENTIFIER-REPLACEMENT" in _text(f)


def test_replace_text_not_found_is_reported(tmp_path):
    f = tmp_path / "e.pdf"
    _make_pdf(f, [["nothing to see"]])
    out = _edit(f, [{"type": "replace_text", "find": "ghost", "replace": "x"}])
    assert "not found" in out
    assert "PDF saved" in out  # continue-on-error: file still written


def test_redact_by_find_removes_content(tmp_path):
    f = tmp_path / "f.pdf"
    _make_pdf(f, [["public intro", "SSN 999-11-2222 here"]])
    out = _edit(f, [{"type": "redact", "find": "999-11-2222"}])
    assert "permanently removed" in out
    txt = _text(f)
    assert "999-11-2222" not in txt
    assert "public intro" in txt


def test_redact_requires_find_or_rect(tmp_path):
    f = tmp_path / "g.pdf"
    _make_pdf(f, [["text"]])
    out = _edit(f, [{"type": "redact"}])
    assert "needs 'find'" in out


def test_watermark_single_run_diagonal(tmp_path):
    # Regression: rotate+bogus-morph used to shear each glyph individually.
    # The watermark must come out as ONE span with an upward text direction.
    f = tmp_path / "h.pdf"
    _make_pdf(f, [["body text"]])
    out = _edit(f, [{"type": "add_watermark", "text": "CONFIDENTIAL"}])
    assert "Warnings/Errors" not in out
    doc = fitz.open(str(f))
    spans = [s for b in doc[0].get_text("dict")["blocks"]
             for line in b.get("lines", []) for s in line["spans"]
             if "CONFIDENTIAL" in s["text"]]
    dirs = [line["dir"] for b in doc[0].get_text("dict")["blocks"]
            for line in b.get("lines", [])
            if any("CONFIDENTIAL" in s["text"] for s in line["spans"])]
    doc.close()
    assert len(spans) == 1  # one straight run, not per-glyph fragments
    dx, dy = dirs[0]
    assert dx > 0.5 and dy < -0.5  # reads upward left→right (↗)


# --- scanned-page handling (read markers + OCR raster policy) ----------------

def _make_scanned_pdf(path: Path, n_pages: int, dpi: int = 150,
                      text_pages: dict[int, list[str]] | None = None):
    """PDF where each page is a full-page raster (like a scanner's output),
    except indices in ``text_pages`` which get a real text layer."""
    text_pages = text_pages or {}
    doc = fitz.open()
    for i in range(n_pages):
        if i in text_pages:
            page = doc.new_page()
            y = 100
            for t in text_pages[i]:
                page.insert_text(fitz.Point(72, y), t, fontsize=12, fontname="helv")
                y += 30
        else:
            src = fitz.open()
            p = src.new_page()
            p.insert_text(fitz.Point(72, 100), f"raster page {i + 1}",
                          fontsize=14, fontname="helv")
            pix = p.get_pixmap(dpi=dpi)
            page = doc.new_page(width=p.rect.width, height=p.rect.height)
            page.insert_image(page.rect, stream=pix.tobytes("jpeg", jpg_quality=70))
            src.close()
    doc.save(str(path))
    doc.close()


def test_read_pdf_marks_scanned_ranges(tmp_path):
    f = tmp_path / "mixed.pdf"
    _make_scanned_pdf(f, 4, text_pages={0: ["intro text"], 3: ["closing text"]})
    out = pdf_mod.read_pdf(str(f), None)
    assert "--- Page 1 ---" in out and "intro text" in out
    assert "--- Pages 2-3: scanned images, no text layer ---" in out
    assert "--- Page 4 ---" in out and "closing text" in out
    assert "screenshot_document" in out  # vision-first guidance


def test_read_pdf_all_scanned_hint(tmp_path):
    f = tmp_path / "scan.pdf"
    _make_scanned_pdf(f, 3)
    out = pdf_mod.read_pdf(str(f), None)
    assert "--- Pages 1-3: scanned images, no text layer ---" in out
    assert "screenshot_document" in out
    assert "edit_pdf" in out and "'ell+eng'" in out  # OCR + language steer


def test_ocr_raster_dpi_caps_at_source(tmp_path):
    f = tmp_path / "cap.pdf"
    _make_scanned_pdf(f, 1, dpi=150)
    doc = fitz.open(str(f))
    page = doc[0]
    # Auto (None) caps the 300 default at the ~150 dpi source raster.
    assert abs(pdf_mod._ocr_raster_dpi(page, None) - 150) <= 2
    # An explicit request below the source is honored as-is.
    assert pdf_mod._ocr_raster_dpi(page, 100) == 100
    # Absurd requests clamp to the 600 ceiling (then the source cap).
    assert pdf_mod._ocr_raster_dpi(page, 10_000) <= 600
    doc.close()
    # A text-only page has no source raster — the default stays 300.
    t = tmp_path / "txt.pdf"
    _make_pdf(t, [["plain text"]])
    doc = fitz.open(str(t))
    assert pdf_mod._ocr_raster_dpi(doc[0], None) == 300
    doc.close()


def _tessdata_available() -> bool:
    try:
        return bool(fitz.get_tessdata())
    except Exception:
        return False


@pytest.mark.skipif(not _tessdata_available(), reason="no tessdata installed")
def test_ocr_op_capped_output_keeps_text_layer(tmp_path):
    f = tmp_path / "scan2.pdf"
    _make_scanned_pdf(f, 2, dpi=150)
    out_path = tmp_path / "scan2_ocr.pdf"
    res = _edit(f, [{"type": "ocr", "language": "eng",
                     "output_path": str(out_path)}])
    assert "failed" not in res
    assert out_path.exists()
    text = _text(out_path)
    assert "raster page" in text  # OCR produced a searchable layer
    # RGB raster capped at the 150 dpi source: the old unconditional 300 dpi
    # swap quadrupled the pixel count for zero OCR gain (536MB incident).
    assert out_path.stat().st_size < f.stat().st_size * 6
