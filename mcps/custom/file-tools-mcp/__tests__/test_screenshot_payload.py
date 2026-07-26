"""screenshot_document payload discipline: dpi-aware long-edge cap, content-
driven encoding (text layer → PNG, scanned → JPEG), and the total-budget
truncation note. Fixtures are built with fitz only — LibreOffice is
installed on neither the dev box nor CI.
"""

import asyncio
import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import fitz
import pytest
from PIL import Image

import pdf as pdf_mod


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setattr(pdf_mod, "_resolve_path", _ident)


def _text_pdf(path: Path, pages: int = 1, width: float = 595,
              height: float = 842) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), f"Hello page {i + 1}", fontsize=14)
    doc.save(str(path))
    doc.close()


def _scanned_pdf(path: Path) -> None:
    # A page whose only content is a full-bleed raster — no text layer.
    doc = fitz.open()
    page = doc.new_page()
    src = fitz.open()
    sp = src.new_page(width=200, height=200)
    sp.insert_text((20, 100), "raster me", fontsize=20)
    pix = sp.get_pixmap(matrix=fitz.Matrix(2, 2))
    page.insert_image(page.rect, pixmap=pix)
    src.close()
    doc.save(str(path))
    doc.close()


def _run(path, **args):
    return asyncio.run(
        pdf_mod.handle_screenshot_document({"path": str(path), **args})
    )


def _img_size(item) -> tuple[int, int]:
    return Image.open(io.BytesIO(base64.b64decode(item.data))).size


def test_text_page_stays_png(tmp_path):
    f = tmp_path / "t.pdf"
    _text_pdf(f)
    items = _run(f, dpi=150)
    assert items[0].mimeType == "image/png"


def test_low_dpi_cap_bites_on_oversize_page(tmp_path):
    # 2000pt-wide page at 150 dpi would be ~4167 px — capped to 2000.
    f = tmp_path / "wide.pdf"
    _text_pdf(f, width=2000, height=1000)
    items = _run(f, dpi=150)
    assert max(_img_size(items[0])) <= 2000


def test_high_dpi_a4_passes_untouched(tmp_path):
    # A4 at 300 dpi ≈ 2481×3508 px — the documented handwriting remedy must
    # come through at full resolution.
    f = tmp_path / "a4.pdf"
    _text_pdf(f)
    items = _run(f, dpi=300)
    assert max(_img_size(items[0])) >= 3500


def test_scanned_page_encodes_jpeg(tmp_path):
    f = tmp_path / "s.pdf"
    _scanned_pdf(f)
    items = _run(f, dpi=150)
    assert items[0].mimeType == "image/jpeg"


def test_total_budget_truncates_with_note(tmp_path, monkeypatch):
    f = tmp_path / "m.pdf"
    _text_pdf(f, pages=4)
    monkeypatch.setattr(pdf_mod, "_SCREENSHOT_TOTAL_BYTES", 1)
    items = _run(f, pages="all", dpi=150)
    # The first page always renders (never an empty result); the summary
    # names the truncation instead of silently dropping pages.
    assert items[0].mimeType.startswith("image/")
    assert "budget" in items[-1].text
