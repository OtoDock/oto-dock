"""Render/convert/write worker cores.

Spawn smokes: one REAL spawn per core proves picklability and child
import-safety (conftest's inline mode is delenv'd here). Wrapper behavior
(atomic-save cleanup, per-op containment, ocr-in-parent) runs inline.
"""

import asyncio
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import isolation  # noqa: E402
import pdf as pdf_mod  # noqa: E402
import shared  # noqa: E402
from isolation_targets import partial_write_target  # noqa: E402


async def _async_ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _bypass_platform(monkeypatch):
    """Identity path resolution (both namespaces — handlers resolve via
    their module import, `_resolve_or_mark` via shared's) + swallowed
    preview pushes."""
    monkeypatch.setattr(pdf_mod, "_resolve_path", _async_ident)
    monkeypatch.setattr(shared, "_resolve_path", _async_ident)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)

    async def _noop_preview(*a, **kw):
        pass

    monkeypatch.setattr(pdf_mod, "_push_preview", _noop_preview)
    monkeypatch.setattr(pdf_mod, "_push_image_preview", _noop_preview)


@pytest.fixture()
def spawn(monkeypatch):
    """Force the REAL spawn path for the per-core smokes."""
    monkeypatch.delenv("FILETOOLS_ISOLATION_INLINE", raising=False)


def _make_pdf(path: Path, n_pages: int = 1) -> None:
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page()
        page.insert_text(fitz.Point(72, 100), f"page {i + 1}", fontsize=12)
    doc.save(str(path))
    doc.close()


def _make_png(path: Path) -> None:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False)
    pix.clear_with(200)
    pix.save(str(path))


# ---------------------------------------------------------------------------
# Spawn smokes — one real child per core
# ---------------------------------------------------------------------------


def test_spawn_screenshot_render_core(tmp_path, spawn):
    src = tmp_path / "doc.pdf"
    _make_pdf(src, 2)
    out = asyncio.run(isolation.run_parse(
        pdf_mod._render_screenshot_core, str(src), "1", 72))
    assert out["total"] == 2
    assert len(out["images"]) == 1
    assert out["images"][0]["mime"] == "image/png"
    assert out["rendered"][0]["page"] == 1


def test_spawn_excel_prepare_core(tmp_path, spawn):
    openpyxl = pytest.importorskip("openpyxl")
    src = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Alpha"
    wb.create_sheet("Beta")
    wb.save(str(src))
    temp_path, names = asyncio.run(isolation.run_parse(
        pdf_mod._excel_prepare_for_screenshot, str(src), str(tmp_path)))
    assert Path(temp_path).exists()
    assert names == ["Alpha", "Beta"]
    assert pdf_mod._sheet_index_from_names(names, "Beta") == 1


def test_spawn_pdf_to_images_core(tmp_path, spawn):
    src = tmp_path / "doc.pdf"
    _make_pdf(src, 2)
    tmp_dir = str(tmp_path / "pages.otodock-tmp")
    saved = asyncio.run(isolation.run_parse(
        pdf_mod._pdf_to_images_core, str(src), tmp_dir, "all", 72, "png"))
    assert [s["name"] for s in saved] == ["page_001.png", "page_002.png"]
    assert all((Path(tmp_dir) / s["name"]).exists() for s in saved)


def test_spawn_images_to_pdf_core(tmp_path, spawn):
    img = tmp_path / "img.png"
    _make_png(img)
    out = tmp_path / "out.pdf"
    inserted = asyncio.run(isolation.run_parse(
        pdf_mod._images_to_pdf_core, [str(img)], str(out), "a4", "contain"))
    assert inserted == 1
    assert out.exists() and not Path(str(out) + ".otodock-tmp").exists()


def test_spawn_write_pdf_core(tmp_path, spawn):
    out = tmp_path / "out.pdf"
    html = "<!DOCTYPE html><html><head></head><body><p>hello</p></body></html>"
    asyncio.run(isolation.run_parse(pdf_mod._render_pdf_core, html, str(out)))
    assert out.exists() and out.read_bytes()[:5] == b"%PDF-"


def test_spawn_write_xlsx_core(tmp_path, spawn):
    import excel as excel_mod
    out = tmp_path / "book.xlsx"
    msg = asyncio.run(isolation.run_parse(
        excel_mod._write_xlsx_core, str(out), [], 0, True))
    assert "0 operations applied" in msg
    assert out.exists()


def test_spawn_write_docx_core(tmp_path, spawn):
    pytest.importorskip("docx")
    import word as word_mod
    out = tmp_path / "doc.docx"
    msg = asyncio.run(isolation.run_parse(
        word_mod._write_docx_core, str(out), [], 0, True))
    assert "0 operations applied" in msg
    assert out.exists()


def test_spawn_write_pptx_core(tmp_path, spawn):
    pytest.importorskip("pptx")
    import powerpoint as pptx_mod
    out = tmp_path / "deck.pptx"
    msg = asyncio.run(isolation.run_parse(
        pptx_mod._write_pptx_core, str(out), [], 0, True))
    assert "0 operations applied" in msg
    assert out.exists()


def test_spawn_edit_pdf_core(tmp_path, spawn):
    src = tmp_path / "doc.pdf"
    _make_pdf(src)
    msg = asyncio.run(isolation.run_parse(
        pdf_mod._edit_pdf_core, str(src),
        [{"type": "rotate_page", "pages": "all", "degrees": 90}], 0))
    assert "1 operations applied" in msg
    doc = fitz.open(str(src))
    assert doc[0].rotation == 90
    doc.close()


# ---------------------------------------------------------------------------
# Wrapper behavior (inline)
# ---------------------------------------------------------------------------


def test_write_pdf_failure_cleans_tmp_and_keeps_original(tmp_path, monkeypatch):
    """A core dying mid-atomic-save must leave the original file intact and
    the wrapper must remove the orphaned deterministic tmp."""
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-ORIGINAL")

    monkeypatch.setattr(pdf_mod, "_render_pdf_core", partial_write_target1)
    with pytest.raises(RuntimeError, match="boom-mid-save"):
        asyncio.run(pdf_mod.handle_write_pdf(
            {"path": str(target), "content": "# hi"}))
    assert target.read_bytes() == b"%PDF-ORIGINAL"
    assert not Path(str(target) + ".otodock-tmp").exists()


def partial_write_target1(full_html, path):
    return partial_write_target(path)


def test_edit_pdf_per_op_containment(tmp_path, monkeypatch):
    """A bad path inside one op stays a per-op warning — the remaining ops
    apply and the file saves (the pre-resolve pass must not fail the call)."""
    async def _picky_resolve(p, writing=False, **kw):
        if "missing" in p:
            raise ValueError(f"Cannot resolve: {p}")
        return p

    monkeypatch.setattr(shared, "_resolve_path", _picky_resolve)
    src = tmp_path / "doc.pdf"
    _make_pdf(src)
    msg = asyncio.run(pdf_mod.handle_edit_pdf({
        "path": str(src),
        "operations": [
            {"type": "merge", "files": ["/missing/nope.pdf"]},
            {"type": "rotate_page", "pages": "all", "degrees": 90},
        ],
    }))
    assert "Op #0 merge" in msg and "Cannot resolve" in msg
    doc = fitz.open(str(src))
    assert doc[0].rotation == 90
    doc.close()


def test_render_bomb_dies_in_child_with_advice(tmp_path, spawn, monkeypatch):
    """The original kill vector: pdf_to_images has no long-edge cap, so a
    degenerate page geometry × dpi is a multi-GB pixmap. It must die at the
    worker's limit with the render advice — never in the server."""
    monkeypatch.setenv("FILETOOLS_PARSE_MEM_MB", "300")
    isolation.worker_rss_budget_bytes.cache_clear()

    src = tmp_path / "bomb.pdf"
    doc = fitz.open()
    doc.new_page(width=14000, height=14000)  # ~194in square page
    doc.save(str(src))
    doc.close()

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isolation.run_parse(
            pdf_mod._pdf_to_images_core, str(src),
            str(tmp_path / "out.otodock-tmp"), "all", 600, "png",
            _advice=pdf_mod._RENDER_ADVICE,
        ))
    msg = str(ei.value)
    assert "300MB" in msg  # C-level malloc failures map to the budget message
    assert "lower `dpi`" in msg
    isolation.worker_rss_budget_bytes.cache_clear()


def test_edit_pdf_ocr_runs_in_parent_not_worker(tmp_path, monkeypatch):
    """An ocr op set (aliased discriminator included) must never enter the
    worker pool — it runs via to_thread in the server process."""
    async def _no_spawn(*a, **kw):
        raise AssertionError("run_parse must not be called for ocr op sets")

    monkeypatch.setattr(pdf_mod, "run_parse", _no_spawn)
    src = tmp_path / "doc.pdf"
    _make_pdf(src)
    msg = asyncio.run(pdf_mod.handle_edit_pdf({
        "path": str(src),
        "operations": [{"action": "ocr", "pages": "1"}],
    }))
    # tesseract may be missing on dev boxes — the op may warn, but the call
    # must complete through the in-parent path and save.
    assert "PDF saved" in msg
