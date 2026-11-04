"""The screenshot copy of a workbook: page setup patched into each sheet
part in schema order, everything else kept byte for byte (so stored values
render)."""

import asyncio
import sys
import zipfile
from pathlib import Path

import openpyxl
import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).parent.parent))

from formats.xlsx import recalc
from formats.xlsx.screenshot import lay_out, prepare_for_screenshot
from ooxml.order import out_of_order

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_SHEET = "xl/worksheets/sheet1.xml"


def _book(path: Path, columns: int, *, formula: bool = False, chartsheet: bool = False,
          paper: str | None = None) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    if paper is not None:
        ws.page_setup.paperSize = paper
    ws.sheet_properties.tabColor = "FF0000"
    for col in range(1, columns + 1):
        ws.cell(row=1, column=col, value=col)
    if formula:
        ws["A2"] = "=SUM(A1:C1)"
    if chartsheet:
        wb.create_chartsheet("Chart")
    wb.save(path)
    if formula:
        recalc.splice(str(path), {"Sales": {"A2": ("n", b"6")}}, {})
    return path


def _setup(path: Path) -> tuple[etree._Element, dict[str, str]]:
    with zipfile.ZipFile(path) as zf:
        root = etree.fromstring(zf.read(_SHEET))
    setup = root.find(f"{_MAIN}pageSetup")
    return root, dict(setup.attrib) if setup is not None else {}


@pytest.mark.parametrize(("columns", "orientation", "fit"), [(3, "portrait", True), (15, "landscape", True),
                                                             (25, "landscape", False)])
def test_the_page_fits_the_columns(tmp_path, columns, orientation, fit):
    src = _book(tmp_path / "in.xlsx", columns)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    copy, names = prepare_for_screenshot(str(src), str(out_dir))
    assert names == ["Sales"] and Path(copy).name == "in.xlsx"
    root, setup = _setup(Path(copy))
    assert setup["orientation"] == orientation and setup["paperSize"] == "9"
    assert ("fitToWidth" in setup) is fit
    fit_flag = root.find(f"{_MAIN}sheetPr/{_MAIN}pageSetUpPr")
    assert (fit_flag is not None and fit_flag.get("fitToPage") == "1") is fit
    assert out_of_order(root) == [] and out_of_order(root.find(f"{_MAIN}sheetPr")) == []


def test_a_sheet_keeps_its_own_paper(tmp_path):
    src = _book(tmp_path / "in.xlsx", 3, paper=openpyxl.worksheet.worksheet.Worksheet.PAPERSIZE_LETTER)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    copy, _names = prepare_for_screenshot(str(src), str(out_dir))
    assert _setup(Path(copy))[1]["paperSize"] == "1"
    custom = (b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/>'
              b'<pageSetup paperWidth="100mm" paperHeight="150mm"/></worksheet>')
    assert b"paperSize" not in lay_out(custom)


def test_cells_and_other_parts_keep_their_bytes(tmp_path):
    src = _book(tmp_path / "in.xlsx", 3, formula=True, chartsheet=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    copy, names = prepare_for_screenshot(str(src), str(out_dir))
    assert names == ["Sales", "Chart"]
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(copy) as b:
        assert a.namelist() == b.namelist()
        for name in a.namelist():
            if name != _SHEET:
                assert a.read(name) == b.read(name), name
        before, after = a.read(_SHEET), b.read(_SHEET)
    region = before[before.index(b"<sheetData"):before.index(b"</sheetData>")]
    assert region in after
    assert recalc.read_values(after) == {"A2": ("n", b"6")}


def test_a_sheet_without_a_dimension_is_measured_by_its_cells():
    data = (b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            b'<row r="1"><c r="A1"><v>1</v></c><c r="Z1"><v>2</v></c></row></sheetData></worksheet>')
    out = lay_out(data)
    assert b'orientation="landscape"' in out and b"fitToWidth" not in out
    assert b'<c r="Z1"><v>2</v></c>' in out


def test_a_file_that_is_no_package_raises(tmp_path):
    xls = tmp_path / "old.xls"
    xls.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 64)
    with pytest.raises(zipfile.BadZipFile):
        prepare_for_screenshot(str(xls), str(tmp_path))


@pytest.mark.office
def test_a_recalculated_workbook_shows_its_values(tmp_path, monkeypatch):
    import fitz

    import render_engine
    from excel import handle_write_xlsx

    async def _ident(p, writing=False, **kw):
        return p

    async def _none(path, filename=None):
        return None

    monkeypatch.setattr("shared._resolve_path", _ident)
    monkeypatch.setattr("shared._push_preview", _none)
    monkeypatch.setattr("shared._notify_file_written", _none)
    book = tmp_path / "book.xlsx"
    asyncio.run(handle_write_xlsx({"path": str(book), "operations": [
        {"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [[400, 1307, "=A1+B1"]]}]}))
    out_dir = tmp_path / "shot"
    out_dir.mkdir()
    copy, _names = prepare_for_screenshot(str(book), str(out_dir))
    pdf = asyncio.run(render_engine.to_pdf(copy, out_dir=render_engine.output_folder(str(tmp_path), "pdf")))
    with fitz.open(pdf) as doc:
        text = "".join(page.get_text() for page in doc)
    assert "1707" in text
