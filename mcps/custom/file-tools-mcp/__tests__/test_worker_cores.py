"""Render/convert/write worker cores.

Spawn smokes: one REAL spawn per core proves picklability and child
import-safety (conftest's inline mode is delenv'd here). Wrapper behavior
(atomic-save cleanup, per-op containment, ocr-in-parent) runs inline.
"""

import asyncio
import re
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import isolation
import pdf as pdf_mod
import shared


async def _async_ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _bypass_platform(monkeypatch):
    """Identity path resolution (both namespaces — handlers resolve via
    their module import, the batch writers via shared's) + swallowed
    preview pushes."""
    monkeypatch.setattr(pdf_mod, "_resolve_path", _async_ident)
    monkeypatch.setattr(shared, "_resolve_path", _async_ident)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)

    async def _noop_preview(*a, **kw):
        pass

    monkeypatch.setattr(pdf_mod, "_push_preview", _noop_preview)
    monkeypatch.setattr(pdf_mod, "_push_image_preview", _noop_preview)


_MCP_DIR = Path(__file__).resolve().parent.parent


def _bypass_everywhere(monkeypatch):
    """Identity resolution and silent previews in every loaded module of the
    folder: a writer calls them through its own import or through shared."""
    async def _noop(*a, **kw):
        pass

    for mod in list(sys.modules.values()):
        if Path(getattr(mod, "__file__", "") or "/").resolve().parent != _MCP_DIR:
            continue
        if hasattr(mod, "_resolve_path"):
            monkeypatch.setattr(mod, "_resolve_path", _async_ident)
        for name in ("_push_preview", "_push_image_preview", "_notify_file_written"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, _noop)


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
    from formats.pdf.screenshot import select_sheet
    from formats.xlsx.screenshot import prepare_for_screenshot

    work = tmp_path / "work"
    work.mkdir()
    temp_path, names = asyncio.run(isolation.run_parse(prepare_for_screenshot, str(src), str(work)))
    assert Path(temp_path).exists()
    assert names == ["Alpha", "Beta"]
    assert select_sheet(temp_path, "Beta") == (1, ["Alpha", "Beta"])


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


def test_spawn_temp_pdf_core(tmp_path, spawn):
    out = tmp_path / "shot.pdf"
    html = "<!DOCTYPE html><html><head></head><body><p>hello</p></body></html>"
    asyncio.run(isolation.run_parse(pdf_mod._render_temp_pdf_core, html, str(out)))
    assert out.read_bytes()[:5] == b"%PDF-"


@pytest.mark.parametrize("tool, name, library", [
    ("write_xlsx", "book.xlsx", "openpyxl"),
    ("write_docx", "doc.docx", "docx"),
    ("write_pptx", "deck.pptx", "pptx"),
])
def test_spawn_write_core_through_its_handler(tmp_path, spawn, monkeypatch, tool, name, library):
    """A writer's handler with no operations and create_new runs its core in
    a real child and creates the file; the first result line keeps its
    frozen form."""
    pytest.importorskip(library)
    import importlib

    import server

    module, function = server.HANDLER_TARGETS[tool]
    handler = getattr(importlib.import_module(module), function)
    _bypass_everywhere(monkeypatch)
    out = tmp_path / name
    msg = asyncio.run(handler({"path": str(out), "operations": [], "create_new": True}))
    assert re.match(r"^\w+ saved: .* \(0 operations applied", msg), msg
    assert out.exists()


def test_spawn_edit_pdf_core(tmp_path, spawn):
    src = tmp_path / "doc.pdf"
    _make_pdf(src)
    from batch import WriteCall

    work = tmp_path / "work"
    work.mkdir()
    call = WriteCall(path=str(src), ops=[(0, {"op": "rotate_page", "pages": "all", "degrees": 90})],
                     work_dir=str(work), original=None)
    report = asyncio.run(isolation.run_parse(pdf_mod._edit.edit_pdf_core, call))
    assert report.saved and "1 operations applied" in report.text
    doc = fitz.open(str(src))
    assert doc[0].rotation == 90
    doc.close()


# ---------------------------------------------------------------------------
# Wrapper behavior (inline)
# ---------------------------------------------------------------------------


def test_write_pdf_failure_cleans_tmp_and_keeps_original(tmp_path, monkeypatch):
    """A core dying mid-atomic-save must leave the original file intact and
    no staging temp behind (the helper's ``.<name>.<hex>.partial`` beside
    it: removed by the helper on the failure, and by the wrapper after a
    kill)."""
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-ORIGINAL")

    monkeypatch.setattr(pdf_mod, "_render_pdf_core", partial_write_target1)
    with pytest.raises(RuntimeError, match="boom-mid-save"):
        asyncio.run(pdf_mod.handle_write_pdf(
            {"path": str(target), "content": "# hi"}))
    assert target.read_bytes() == b"%PDF-ORIGINAL"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".partial")]


def partial_write_target1(full_html, path, allowed=()):
    with shared.safe_open_write(path) as fh:
        fh.write(b"%PDF-PART")
        raise RuntimeError("boom-mid-save")


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
        "best_effort": True,
        "operations": [
            {"type": "merge", "files": ["/missing/nope.pdf"]},
            {"type": "rotate_page", "pages": "all", "degrees": 90},
        ],
    }))
    assert re.search(r"[Oo]p #0 merge", msg) and "Cannot resolve" in msg
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

    monkeypatch.setattr(isolation, "run_parse", _no_spawn)
    monkeypatch.setattr(pdf_mod, "run_parse", _no_spawn)
    src = tmp_path / "doc.pdf"
    _make_pdf(src)
    msg = asyncio.run(pdf_mod.handle_edit_pdf({
        "path": str(src),
        "operations": [{"action": "ocr", "pages": "1"}],
    }))
    # Tesseract may be missing on a dev box: the call still completes through
    # the in-parent path, saved or with the OCR failure named.
    assert msg.startswith("PDF saved") or "OCR failed" in msg
