"""How a workbook prints: the marked copy the rendering is made of (each
chart and picture a plain picture of its own colour at the same anchor, and
the first and last columns of a sheet with no page setup of its own marked
by two small pictures), and the check that reads where those land in the
rendered pages. The office tests render the final walk's two budgets."""

import asyncio
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

openpyxl = pytest.importorskip("openpyxl")
fitz = pytest.importorskip("fitz")

import checks
import shared
from formats.xlsx import print_checks, print_marks

PRINT = Path(__file__).parent / "print"
CUT = PRINT / "budget-cut-chart.xlsx"
FITTED = PRINT / "budget-one-page.xlsx"
A4 = (595, 842)
A3_LANDSCAPE = (1191, 842)


def _plan(path):
    with zipfile.ZipFile(path) as zf:
        return print_marks.plan(zf)


def test_the_plan_marks_each_chart_and_the_columns_of_a_sheet_with_no_setup():
    marks = _plan(CUT)
    assert [(m.kind, m.sheet, m.name, m.cell) for m in marks] == [
        ("chart", "Budget", "Chart 1", "J3"), ("first column", "Budget", "A", "A1"),
        ("last column", "Budget", "H", "H1")]
    assert len({m.colour for m in marks}) == 3
    # Fitted to one page wide by its author: only its chart is marked.
    assert [(m.kind, m.cell) for m in _plan(FITTED)] == [("chart", "A21")]


def _book(path, *, wide=True, fit=False, hidden_last=False, landscape=False, print_area=False):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Data"
    for col in range(1, 16 if wide else 3):
        sheet.cell(row=2, column=col, value=f"Header {col}")
        sheet.cell(row=3, column=col, value=col * 10)
    if hidden_last:
        sheet.column_dimensions["O"].hidden = True
    if fit:
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    if landscape:
        sheet.page_setup.orientation = "landscape"
    if print_area:
        sheet.print_area = "A1:O3"
    hidden = book.create_sheet("Hidden")
    hidden["A1"] = "x"
    hidden.sheet_state = "hidden"
    book.save(path)
    return path


@pytest.mark.parametrize("settings, marked", [
    ({}, [("first column", "A", "A2"), ("last column", "O", "O2")]),
    ({"hidden_last": True}, [("first column", "A", "A2"), ("last column", "N", "N2")]),
    ({"wide": False}, [("first column", "A", "A2"), ("last column", "B", "B2")]),
    ({"fit": True}, []), ({"landscape": True}, []), ({"print_area": True}, []),
])
def test_a_sheet_of_its_own_setup_is_left_to_its_author(tmp_path, settings, marked):
    marks = _plan(_book(tmp_path / "b.xlsx", **settings))
    assert [(m.kind, m.name, m.cell) for m in marks] == marked
    assert all(m.sheet == "Data" for m in marks), "a hidden sheet does not print"


def _marked(path, tmp_path) -> Path:
    folder = tmp_path / "marked"
    folder.mkdir()
    return Path(print_marks.marked_copy(str(path), str(folder), own_copy=True))


def _pictures(path) -> list[tuple[str, tuple[int, int, int], str]]:
    """(anchor cell, colour, anchor kind) of each picture the copy's
    drawings hold, in order."""
    from lxml import etree

    out = []
    with zipfile.ZipFile(path) as zf:
        for name in sorted(n for n in zf.namelist() if re.fullmatch(r"xl/drawings/[^/]+\.xml", n)):
            root = etree.fromstring(zf.read(name))
            rels = etree.fromstring(zf.read(name.replace("drawings/", "drawings/_rels/") + ".rels"))
            targets = {r.get("Id"): r.get("Target") for r in rels}
            for anchor in root:
                pic = anchor.find("{*}pic")
                assert anchor.find("{*}graphicFrame") is None, "a chart is left in the marked copy"
                if pic is None:
                    continue
                embed = pic.find(".//{*}blip").get("{http://schemas.openxmlformats.org/officeDocument/2006/"
                                                    "relationships}embed")
                media = "xl/" + targets[embed].lstrip("/").removeprefix("xl/").lstrip("./")
                data = zf.read(media.replace("xl/../", ""))
                pixmap = fitz.Pixmap(data)
                frm = anchor.find("{*}from")
                cell = openpyxl.utils.get_column_letter(int(frm.find("{*}col").text) + 1) + str(
                    int(frm.find("{*}row").text) + 1)
                out.append((cell, pixmap.pixel(0, 0), etree.QName(anchor).localname))
    return out


def test_the_marked_copy_puts_a_picture_of_its_colour_at_each_place(tmp_path):
    copy = _marked(CUT, tmp_path)
    marks = _plan(CUT)
    assert _pictures(copy) == [("J3", marks[0].colour, "oneCellAnchor"), ("A1", marks[1].colour, "oneCellAnchor"),
                               ("H1", marks[2].colour, "oneCellAnchor")]
    # The chart's anchor and size are kept: it lands where the chart did.
    with zipfile.ZipFile(copy) as zf:
        drawing = zf.read("xl/drawings/drawing1.xml").decode()
    assert '<ext cx="6480000" cy="3240000"/>' in drawing
    book = openpyxl.load_workbook(copy)
    assert len(book["Budget"]._images) == 3
    assert CUT.read_bytes() == (PRINT / "budget-cut-chart.xlsx").read_bytes()


def test_a_sheet_without_a_drawing_gets_one_for_its_marks(tmp_path):
    copy = _marked(_book(tmp_path / "b.xlsx"), tmp_path)
    marks = _plan(tmp_path / "b.xlsx")
    assert [(cell, colour) for cell, colour, _ in _pictures(copy)] == [(m.cell, m.colour) for m in marks]
    with zipfile.ZipFile(copy) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode()
    # The drawing goes after the page setup, as the schema orders them.
    assert sheet.index("<pageMargins") < sheet.index("drawing") < sheet.index("</worksheet>")
    assert len(openpyxl.load_workbook(copy)["Data"]._images) == 2


def test_a_sheet_whose_drawing_cannot_be_read_is_left_unmarked(tmp_path):
    broken = tmp_path / "broken.xlsx"
    with zipfile.ZipFile(CUT) as src, zipfile.ZipFile(broken, "w") as dst:
        for item in src.infolist():
            if item.filename != "xl/drawings/drawing1.xml":
                dst.writestr(item, src.read(item))
    # A second <drawing> beside the one the sheet names would break it.
    assert _plan(broken) == []
    folder = tmp_path / "marked"
    folder.mkdir()
    assert print_marks.marked_copy(str(broken), str(folder), own_copy=True) == str(broken)
    assert list(folder.iterdir()) == []


def _png(colour) -> bytes:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pixmap.set_rect(pixmap.irect, colour)
    return pixmap.tobytes("png")


def _pdf(path, pages: int, placed: list[tuple[int, tuple[float, float, float, float], tuple[int, int, int]]],
         size=A4):
    """A PDF of pages of ``size`` with each (page, rect, colour) drawn as a
    picture, clipped to its page as LibreOffice draws an object cut by a
    page break."""
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=size[0], height=size[1])
    for page, rect, colour in placed:
        doc[page].insert_image(fitz.Rect(rect), stream=_png(colour), keep_proportion=False, overlay=True)
    doc.save(str(path))
    return str(path)


def _findings(book, pdf) -> list[checks.Finding]:
    with open(book, "rb") as fh, checks.CheckContext(str(book), "xlsx", rendered_pdf=pdf, fileobj=fh) as ctx:
        return print_checks.print_layout(ctx)


def test_a_cut_chart_and_spilled_columns_are_reported(tmp_path):
    chart, first, last = (m.colour for m in _plan(CUT))
    pdf = _pdf(tmp_path / "r.pdf", 3, [(0, (54, 72, 55, 73), first), (1, (209, 72, 210, 73), last),
                                       (1, (335, 102, 845, 357), chart), (2, (-138, 102, 371, 357), chart)])
    found = _findings(CUT, pdf)
    assert [(f.severity, f.location) for f in found] == [(checks.Severity.WARNING, "Budget!J3"),
                                                        (checks.Severity.WARNING, "Budget")]
    cut, spill = found
    assert "the chart 'Chart 1' is cut across printed pages 2 and 3" in cut.message
    assert "set_print_setup" in cut.fix and "fit_width: 1" in cut.fix and "add_chart" in cut.fix
    assert "column H prints on another page than column A" in spill.message
    assert "set_print_setup" in spill.fix and "fit_width: 1" in spill.fix


def test_a_chart_past_the_edge_of_its_only_page_is_cut(tmp_path):
    chart, first, last = (m.colour for m in _plan(CUT))
    pdf = _pdf(tmp_path / "r.pdf", 1, [(0, (54, 72, 55, 73), first), (0, (209, 72, 210, 73), last),
                                       (0, (335, 102, 845, 357), chart)])
    found = _findings(CUT, pdf)
    assert len(found) == 1 and "is cut at the edge of printed page 1" in found[0].message


def test_a_workbook_that_prints_whole_has_no_finding(tmp_path):
    (chart,) = (m.colour for m in _plan(FITTED))
    pdf = _pdf(tmp_path / "r.pdf", 1, [(0, (54, 407, 862, 757), chart)], size=A3_LANDSCAPE)
    assert _findings(FITTED, pdf) == []
    # A rendering of an unmarked copy (the marks could not be made) says nothing.
    assert _findings(CUT, _pdf(tmp_path / "plain.pdf", 3, [])) == []


@pytest.fixture()
def hooks(check_registry, monkeypatch):
    async def resolve(value, writing=False, **kw):
        return value

    monkeypatch.setattr(shared, "_resolve_path", resolve)
    print_checks.register_all()


def _check(path, **args) -> str:
    return asyncio.run(checks.handle_check_document({"path": str(path), **args}))


def test_render_draws_the_marked_copy_and_reads_where_the_marks_land(hooks, tmp_path, monkeypatch):
    import render_engine

    book = tmp_path / "budget.xlsx"
    book.write_bytes(CUT.read_bytes())
    drawn = []

    async def to_pdf(path, *, out_dir, **kw):
        drawn.append([colour for _, colour, _ in _pictures(path)])
        chart, first, last = (m.colour for m in _plan(book))
        return _pdf(Path(out_dir) / "budget.pdf", 3,
                    [(0, (54, 72, 55, 73), first), (1, (209, 72, 210, 73), last),
                     (1, (335, 102, 845, 357), chart), (2, (-138, 102, 371, 357), chart)])

    monkeypatch.setattr(render_engine, "to_pdf", to_pdf)
    out = _check(book, render=True)
    assert drawn == [[m.colour for m in _plan(book)]]
    assert "xlsx.print_layout at Budget!J3: the chart 'Chart 1' is cut across printed pages 2 and 3" in out, out
    assert "reads the rendering" not in out


def test_a_copy_that_cannot_be_marked_is_drawn_as_it_is(hooks, tmp_path, monkeypatch):
    import render_engine

    book = tmp_path / "budget.xlsx"
    book.write_bytes(CUT.read_bytes())
    drawn = []

    def broken(*a, **k):
        raise RuntimeError("the drawing part is not XML")

    async def to_pdf(path, *, out_dir, **kw):
        drawn.append(path)
        return _pdf(Path(out_dir) / "budget.pdf", 1, [])

    monkeypatch.setattr(print_marks, "marked_copy", broken)
    monkeypatch.setattr(render_engine, "to_pdf", to_pdf)
    out = _check(book, render=True)
    assert drawn == [str(book)] and "print_layout" not in out.split("\n", 1)[1]
    assert "How the workbook prints could not be checked" in out, out


# ------------------------------------------------------------------ the engine


def _copied(source: Path, tmp_path) -> Path:
    book = tmp_path / source.name
    book.write_bytes(source.read_bytes())
    return book


@pytest.mark.office
def test_the_walk_budget_that_printed_badly_is_reported(hooks, tmp_path):
    out = _check(_copied(CUT, tmp_path), render=True)
    assert "xlsx.print_layout at Budget!J3: the chart 'Chart 1' is cut across printed pages" in out, out
    assert ("xlsx.print_layout at Budget: the sheet's columns do not fit across one printed page: column H prints "
            "on another page than column A") in out, out


@pytest.mark.office
def test_the_walk_budget_that_fits_one_page_is_clean(hooks, tmp_path):
    out = _check(_copied(FITTED, tmp_path), render=True)
    assert "print_layout" not in out, out
