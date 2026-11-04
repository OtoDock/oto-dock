"""The Collabora backend against a real, private Collabora (the pinned tag
on a network of its own, this test's container the only other member):
nothing a document links to is fetched, A4 for a sheet with no paper, a
recalculation that replaces a stale cached value, the engine named in the
result. Skipped unless FILETOOLS_TEST_COLLABORA_URL names that Collabora
(service root included). Each finding has its positive control: the same
file sent to convert-to without the backend's copy."""

import asyncio
import io
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import engine_probes

import render_engine

URL = os.environ.get("FILETOOLS_TEST_COLLABORA_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not URL, reason="needs a private Collabora: set FILETOOLS_TEST_COLLABORA_URL")


@pytest.fixture
def collabora(tmp_path, monkeypatch):
    monkeypatch.setenv(render_engine.URL_SETTING, URL)
    render_engine.reset_backend()
    listener = engine_probes.Listener()
    for folder in ("in", "out"):
        (tmp_path / folder).mkdir()
    yield tmp_path, listener, listener.url(engine_probes.own_address(urlsplit(URL).hostname))
    listener.close()
    render_engine.reset_backend()


def _raw(path: Path, fmt: str, **fields) -> bytes:
    """``path`` sent to convert-to as it is."""
    import httpx

    with path.open("rb") as fh:
        answer = httpx.post(f"{URL}/cool/convert-to/{fmt}", files={"data": (path.name, fh)},
                            data={"lang": "en-US", **fields}, timeout=120, trust_env=False)
    answer.raise_for_status()
    return answer.content


def _engine_fetches_drawing_pictures() -> bool:
    """Whether convert-to loads a drawing's linked picture of its own accord:
    from 26.04.5.2 it does not, so the control that the backend's removal
    is what keeps the picture unfetched holds only on an older Collabora.
    The backend's half, nothing fetched from its copy, holds on every one."""
    import httpx

    version = httpx.get(f"{URL}/hosting/capabilities", timeout=30, trust_env=False).json().get("productVersion", "")
    return tuple(int(p) for p in re.findall(r"\d+", version)[:4]) < (26, 4, 5, 2)


def _page(pdf: bytes) -> tuple[int, int]:
    import fitz

    with fitz.open(stream=pdf, filetype="pdf") as document:
        return round(document[0].rect.width), round(document[0].rect.height)


def test_collabora_fetches_what_a_document_links_unless_the_backend_removes_it(collabora, caplog):
    tmp_path, listener, base = collabora
    book = engine_probes.drawing_picture_book(tmp_path / "in" / "book.xlsx", base)
    chunk = engine_probes.chunk_document(tmp_path / "in" / "chunk.docx", base)
    _raw(book, "xlsx")
    _raw(chunk, "pdf")
    time.sleep(1)
    if _engine_fetches_drawing_pictures():
        assert any("drawing-picture" in hit for hit in listener.hits), listener.hits
    assert any("chunk-style" in hit for hit in listener.hits), listener.hits
    listener.hits.clear()
    probes = [book, chunk, engine_probes.theme_picture_deck(tmp_path / "in" / "deck.pptx", base),
              engine_probes.function_book(tmp_path / "in" / "functions.xlsx", base),
              engine_probes.field_document(tmp_path / "in" / "fields.docx", base)]
    with caplog.at_level("INFO", logger="file-tools"):
        for probe in probes:
            pdf = asyncio.run(render_engine.to_pdf(str(probe), out_dir=str(tmp_path / "out")))
            assert engine_probes.CANARY.encode() not in Path(pdf).read_bytes()
            if probe.suffix == ".xlsx":
                done = asyncio.run(render_engine.recalc(str(probe), out_dir=str(tmp_path / "out")))
                with zipfile.ZipFile(done) as zf:
                    assert not any(engine_probes.CANARY.encode() in zf.read(n) for n in zf.namelist())
    time.sleep(1)
    assert listener.hits == []
    assert caplog.text.count("Collabora converted") >= len(probes), caplog.text


def test_a_sheet_with_no_paper_is_a4(collabora):
    import openpyxl

    tmp_path, _, _ = collabora
    path = tmp_path / "in" / "sheet.xlsx"
    book = openpyxl.Workbook()
    for row in range(1, 80):
        book.active.cell(row, 1, f"row {row}")
    book.save(path)
    assert _page(_raw(path, "pdf")) == (612, 792)
    pdf = asyncio.run(render_engine.to_pdf(str(path), out_dir=str(tmp_path / "out")))
    assert _page(Path(pdf).read_bytes()) == (595, 842)
    assert b'paperSize="9"' not in zipfile.ZipFile(path).read("xl/worksheets/sheet1.xml")


def test_a_recalculation_replaces_a_stale_cached_value(collabora):
    import openpyxl

    tmp_path, _, _ = collabora
    plain = tmp_path / "plain.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = "=1+1"
    book.save(plain)
    stale = tmp_path / "in" / "stale.xlsx"
    with zipfile.ZipFile(plain) as src, zipfile.ZipFile(stale, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"<f>1+1</f><v></v>", b"<f>1+1</f><v>5</v>")
            dst.writestr(item.filename, data)
    kept = openpyxl.load_workbook(io.BytesIO(_raw(stale, "xlsx")), data_only=True).active["A1"].value
    assert kept == 5
    done = asyncio.run(render_engine.recalc(str(stale), out_dir=str(tmp_path / "out")))
    assert openpyxl.load_workbook(done, data_only=True).active["A1"].value == 2
    with zipfile.ZipFile(done) as zf:
        application = zf.read("docProps/app.xml").decode().split("<Application>")[1].split("<")[0]
    assert render_engine.engine_of_application(application)[0] is render_engine.Engine.COLLABORA


def _drawing_variant(source: Path, target: Path, change) -> Path:
    """``source`` with its drawing's relationships part changed by ``change``."""
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "xl/drawings/_rels/drawing1.xml.rels":
                data = change(data)
            zout.writestr(item.filename, data)
    return target


DRAWING_VARIANTS = {
    "a character reference in TargetMode": lambda d: d.replace(b'TargetMode="External"', b'TargetMode="&#69;xternal"'),
    "the hyperlink type on a picture": lambda d: d.replace(b"relationships/image", b"relationships/hyperlink"),
    "a bracket in the target": lambda d: d.replace(b'drawing-picture.png"', b'drawing-picture.png?a>b"'),
}


@pytest.mark.parametrize("variant", list(DRAWING_VARIANTS))
def test_no_disguise_of_a_drawing_picture_gets_it_fetched(collabora, variant):
    tmp_path, listener, base = collabora
    book = engine_probes.drawing_picture_book(tmp_path / "plain.xlsx", base)
    probe = _drawing_variant(book, tmp_path / "in" / "book.xlsx", DRAWING_VARIANTS[variant])
    _raw(probe, "xlsx")
    time.sleep(1)
    if _engine_fetches_drawing_pictures():
        assert any("drawing-picture" in hit for hit in listener.hits), listener.hits
    listener.hits.clear()
    asyncio.run(render_engine.recalc(str(probe), out_dir=str(tmp_path / "out")))
    asyncio.run(render_engine.to_pdf(str(probe), out_dir=str(tmp_path / "out")))
    time.sleep(1)
    assert listener.hits == []
