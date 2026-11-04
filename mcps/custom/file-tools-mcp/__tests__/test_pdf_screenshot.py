"""screenshot_document's additions: the contact sheet, the slide numbers a
deck's pages stand for, a workbook copy that prints one sheet, the new file
kinds, and protected PDFs."""

import asyncio
import base64
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pdf as pdf_mod
import render_engine
import shared
from formats.pdf import screenshot as shot


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(pdf_mod, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)


def _pdf(path: Path, pages: int, size=(595, 842), password: str | None = None) -> Path:
    doc = fitz.open()
    for n in range(pages):
        doc.new_page(width=size[0], height=size[1]).insert_text((72, 100), f"Page {n + 1}", fontsize=40)
    kwargs = {"encryption": fitz.PDF_ENCRYPT_AES_256, "user_pw": password, "owner_pw": password} if password else {}
    doc.save(str(path), **kwargs)
    doc.close()
    return path


def _shot(**args):
    return asyncio.run(pdf_mod.handle_screenshot_document(args))


def test_a_contact_sheet_is_one_labelled_image_of_every_page(tmp_path):
    from PIL import Image

    src = _pdf(tmp_path / "report.pdf", 9)
    items = _shot(path=str(src), grid=True)
    assert [i.type for i in items] == ["image", "text"]
    image = Image.open(io.BytesIO(base64.b64decode(items[0].data)))
    assert max(image.size) <= shot.SHEET_LONG_EDGE
    assert "9 of 9 page(s) (1–9), 6 per row" in items[1].text and "Page 1" not in items[1].text.split("\n")[0][:5]


def test_landscape_pages_get_four_columns_and_the_sheet_stops_at_its_cap(tmp_path):
    src = _pdf(tmp_path / "deck.pdf", 40, size=(960, 540))
    items = _shot(path=str(src), grid=True)
    text = items[-1].text
    assert "36 of 40 page(s)" in text and "4 per row" in text and "'37-40'" in text


def test_the_layout_fits_mixed_pages_and_few_pages():
    cols, rows, w, h = shot.layout([(595, 842)] * 2)
    assert (cols, rows) == (2, 1) and h > w
    cols, rows, w, h = shot.layout([(595, 842), (842, 595)])
    assert cols == 2 and h >= w * 842 / 595 - 1
    cols, rows, w, h = shot.layout([(960, 540)] * 36)
    assert rows * (h + shot.LABEL_PT + 10 + shot.GAP) + shot.GAP <= shot.SHEET_LONG_EDGE + 1


def test_a_deck_s_shown_slides_skip_the_hidden_ones(tmp_path):
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    for _ in range(3):
        prs.slides.add_slide(prs.slide_layouts[6])
    prs.slides[1]._element.set("show", "0")
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    assert shot.shown_slides(str(path)) == [0, 2]
    assert shot.shown_slides(str(tmp_path / "missing.pptx")) is None


def test_select_sheet_hides_every_other_sheet_and_names_them_when_unknown(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.title = "Σύνοψη"
    wb.create_sheet("Data")
    wb.create_sheet("Notes")
    path = tmp_path / "book.xlsx"
    wb.save(str(path))
    index, names = shot.select_sheet(str(path), "data")
    assert (index, names) == (1, ["Σύνοψη", "Data", "Notes"])
    with zipfile.ZipFile(path) as package:
        xml = package.read("xl/workbook.xml").decode()
    assert xml.count('state="hidden"') == 2 and 'activeTab="1"' in xml
    states = {s.title: s.sheet_state for s in openpyxl.load_workbook(str(path)).worksheets}
    assert states == {"Σύνοψη": "hidden", "Data": "visible", "Notes": "hidden"}
    with pytest.raises(ValueError, match="'Σύνοψη', 'Data', 'Notes'"):
        shot.select_sheet(str(path), "Missing")
    with pytest.raises(ValueError, match="index 0 to 2"):
        shot.select_sheet(str(path), 7)


def test_the_new_kinds_are_accepted():
    for ext in (".potx", ".potm", ".pptm", ".ppsx", ".ppsm", ".dotx", ".docm", ".dotm", ".xlsm", ".xltx", ".xltm"):
        assert ext in pdf_mod._SCREENSHOT_EXTS, ext


def test_a_protected_pdf_needs_its_password(tmp_path):
    src = _pdf(tmp_path / "locked.pdf", 2, password="pw-1")
    items = _shot(path=str(src))
    assert items[0].type == "text" and "password-protected" in items[0].text
    items = _shot(path=str(src), password="pw-1", grid=True)
    assert items[0].type == "image"


def test_pdf_to_images_takes_the_password(tmp_path, monkeypatch):
    async def _nothing(*a, **k):
        return None

    monkeypatch.setattr(pdf_mod, "_push_image_preview", _nothing)
    src = _pdf(tmp_path / "locked2.pdf", 2, password="pw-2")
    out = asyncio.run(pdf_mod.handle_pdf_to_images({"path": str(src), "output_dir": str(tmp_path / "pages"),
                                                    "password": "pw-2", "dpi": 30}))
    assert out.startswith("Rendered 2 pages")


def test_images_to_pdf_counts_the_images_it_placed_and_names_the_rest(tmp_path, monkeypatch):
    from PIL import Image

    async def _nothing(*a, **k):
        return None

    monkeypatch.setattr(pdf_mod, "_push_preview", _nothing)
    good = tmp_path / "a.png"
    Image.new("RGB", (20, 10), "red").save(good)
    out = asyncio.run(pdf_mod.handle_images_to_pdf({"images": [str(good), str(tmp_path / "gone.png")],
                                                    "output_path": str(tmp_path / "out.pdf")}))
    assert "(1 images, a4)" in out and "gone.png (not found)" in out
    assert fitz.open(str(tmp_path / "out.pdf")).page_count == 1


def _watch_parsing(monkeypatch):
    """Every fitz.open and zipfile.ZipFile of a file, and whether it ran
    inside a worker's target (run_parse) or in the server process."""
    import zipfile

    state = {"depth": 0, "outside": []}
    real_run, real_open, real_zip = pdf_mod.run_parse, fitz.open, zipfile.ZipFile

    async def _run(target, *a, **k):
        state["depth"] += 1
        try:
            return await real_run(target, *a, **k)
        finally:
            state["depth"] -= 1

    def _open(*a, **k):
        if a and isinstance(a[0], (str, Path)) and not state["depth"]:
            state["outside"].append(("fitz.open", str(a[0])))
        return real_open(*a, **k)

    class _Zip(real_zip):
        def __init__(self, file, *a, **k):
            if isinstance(file, (str, Path)) and not state["depth"]:
                state["outside"].append(("ZipFile", str(file)))
            super().__init__(file, *a, **k)

    async def _convert(path, fmt, out_dir, **k):
        out = os.path.join(out_dir, Path(path).stem + ".pdf")
        doc = real_open()
        for _ in range(3):
            doc.new_page(width=960, height=540).insert_text((60, 100), "page", fontsize=30)
        doc.save(out)
        return out

    monkeypatch.setattr(pdf_mod, "run_parse", _run)
    monkeypatch.setattr(fitz, "open", _open)
    monkeypatch.setattr(zipfile, "ZipFile", _Zip)
    monkeypatch.setattr(render_engine, "_libreoffice_convert", _convert)
    return state


def test_the_server_process_never_parses_the_file_it_screenshots(tmp_path, monkeypatch):
    pptx = pytest.importorskip("pptx")
    openpyxl = pytest.importorskip("openpyxl")
    document = _pdf(tmp_path / "doc.pdf", 4)
    prs = pptx.Presentation()
    for _ in range(3):
        prs.slides.add_slide(prs.slide_layouts[6])
    deck = tmp_path / "deck.pptx"
    prs.save(str(deck))
    wb = openpyxl.Workbook()
    wb.create_sheet("Data")
    book = tmp_path / "book.xlsx"
    wb.save(str(book))
    state = _watch_parsing(monkeypatch)
    assert _shot(path=str(document), grid=True)[0].type == "image"
    assert "slide_index" in _shot(path=str(deck), grid=True)[-1].text
    items = _shot(path=str(book), sheet="Data")
    assert "Sheet 'Data'" in items[-1].text
    items = _shot(path=str(book), sheet="Nope")
    assert "'Sheet', 'Data'" in items[-1].text
    assert state["outside"] == []


def test_the_screenshot_cores_run_in_a_real_worker(tmp_path, monkeypatch):
    """The cores pickle across the spawn boundary and return plain data."""
    openpyxl = pytest.importorskip("openpyxl")
    import isolation

    monkeypatch.delenv("FILETOOLS_ISOLATION_INLINE", raising=False)
    src = _pdf(tmp_path / "a.pdf", 3)
    sheet = asyncio.run(isolation.run_parse(shot.contact_sheet_core, str(src), "all", str(src), None))
    assert sheet["pages"] == [0, 1, 2] and sheet["data"][:4] == b"\x89PNG"
    wb = openpyxl.Workbook()
    wb.create_sheet("Data")
    book = tmp_path / "b.xlsx"
    wb.save(str(book))
    work = tmp_path / "work"
    work.mkdir()
    made = asyncio.run(isolation.run_parse(shot.workbook_core, str(book), str(work), "Data"))
    assert made["index"] == 1 and made["names"] == ["Sheet", "Data"]


def test_a_contact_sheet_with_a_wrong_password_says_so(tmp_path):
    src = _pdf(tmp_path / "locked3.pdf", 2, password="pw-3")
    items = _shot(path=str(src), grid=True, password="nope")
    assert items[0].type == "text" and "The password given is not the one that opens" in items[0].text
    assert "nope" not in items[0].text
    items = _shot(path=str(src), grid=True)
    assert "password-protected" in items[0].text
