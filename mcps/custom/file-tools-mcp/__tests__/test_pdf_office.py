"""The PDF tools against the real engine (LibreOffice, in the image's test
runner): the new file kinds screenshot, a workbook prints one sheet, the
layout check reads a real rendering, and a filled Greek form shows its
values in another engine than MuPDF."""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.office

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import checks
import pdf as pdf_mod
import render_engine
import shared
from formats.pdf import layout_checks, screenshot as shot

_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "docm": "application/vnd.ms-word.document.macroEnabled.main+xml",
    "dotm": "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    "potx": "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
    "potm": "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
    "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
    "ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    "xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    "xltm": "application/vnd.ms-excel.template.macroEnabled.main+xml",
}
_BASE = {"dotx": "docx", "docm": "docx", "dotm": "docx", "potx": "pptx", "pptm": "pptx", "potm": "pptx",
         "ppsx": "pptx", "ppsm": "pptx", "xlsm": "xlsx", "xltx": "xlsx", "xltm": "xlsx"}


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(pdf_mod, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)


def _office_file(folder: Path, base: str) -> Path:
    path = folder / f"base.{base}"
    if base == "docx":
        import docx

        document = docx.Document()
        document.add_heading("Κεφάλαιο 1", 1)
        document.add_paragraph("Ένα έγγραφο από πρότυπο.")
        document.save(str(path))
    elif base == "pptx":
        import pptx

        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = "Τριμηνιαία ανασκόπηση"
        prs.save(str(path))
    else:
        import openpyxl

        wb = openpyxl.Workbook()
        wb.active["A1"] = "Έσοδα"
        wb.active["B1"] = 1250
        wb.save(str(path))
    return path


def _as_kind(src: Path, kind: str, folder: Path) -> Path:
    """``src`` with its main part's content type set to ``kind``'s."""
    out = folder / f"file.{kind}"
    base = src.suffix.lstrip(".")
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "[Content_Types].xml":
                data = data.replace(_TYPES[base].encode(), _TYPES[kind].encode())
            zout.writestr(info, data)
    return out


@pytest.mark.parametrize("kind", sorted(_BASE))
def test_each_new_kind_screenshots(tmp_path, kind):
    path = _as_kind(_office_file(tmp_path, _BASE[kind]), kind, tmp_path)
    items = asyncio.run(pdf_mod.handle_screenshot_document({"path": str(path), "grid": True}))
    assert items[0].type == "image", items[0].text


def test_a_workbook_prints_the_chosen_sheet_alone(tmp_path):
    import openpyxl

    from formats.xlsx.screenshot import prepare_for_screenshot

    wb = openpyxl.Workbook()
    wb.active.title = "Σύνοψη"
    wb.active["A1"] = "SUMMARY-SHEET"
    for name in ("Data", "Notes"):
        ws = wb.create_sheet(name)
        for row in range(1, 120):
            ws.cell(row=row, column=1, value=f"{name.upper()}-ROW-{row}")
    path = tmp_path / "book.xlsx"
    wb.save(str(path))
    work = tmp_path / "work"
    work.mkdir()
    copy, _ = prepare_for_screenshot(str(path), str(work))
    shot.select_sheet(copy, "Data")
    out = tmp_path / "out"
    out.mkdir()
    pdf = asyncio.run(render_engine.to_pdf(copy, out_dir=str(out)))
    text = "".join(page.get_text() for page in fitz.open(pdf))
    assert "DATA-ROW-1" in text and "DATA-ROW-119" in text
    assert "SUMMARY-SHEET" not in text and "NOTES-ROW" not in text


def _deck(path: Path) -> Path:
    import pptx
    from pptx.util import Inches, Pt

    prs = pptx.Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    prs.slides.add_slide(prs.slide_layouts[6])._element.set("show", "0")
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for name, top, text, fit in (
            ("Overflows", 1.0, "Αποτελέσματα τριμήνου: τα έσοδα αυξήθηκαν σε όλες τις περιοχές " * 3, "none"),
            ("Fits", 3.5, "Σύντομος τίτλος", "none"),
            ("Shrinks", 5.0, "Πολύ κείμενο που χωρά μόνο αν μικρύνει η γραμματοσειρά του πλαισίου " * 2, "shrink")):
        box = slide.shapes.add_textbox(Inches(1), Inches(top), Inches(5), Inches(0.8))
        box.name = name
        frame = box.text_frame
        frame.word_wrap = True
        frame.text = text
        frame.paragraphs[0].runs[0].font.size = Pt(20)
        frame.paragraphs[0].runs[0].font.name = "Calibri"
        body = frame._bodyPr
        for child in list(body):
            body.remove(child)
        if fit == "shrink":
            body.append(body.makeelement("{http://schemas.openxmlformats.org/drawingml/2006/main}normAutofit", {}))
    prs.save(str(path))
    return path


def test_the_layout_check_reads_a_real_rendering(tmp_path):
    deck = _deck(tmp_path / "deck.pptx")
    out = tmp_path / "render"
    out.mkdir()
    pdf = asyncio.run(render_engine.to_pdf(str(deck), out_dir=str(out)))
    with checks.CheckContext(str(deck), "pptx", rendered_pdf=pdf) as ctx:
        found = layout_checks.rendered_overflow(ctx)
    names = [re.search(r"'([^']+)'", f.location).group(1) for f in found if f.location]
    assert names == ["Overflows"], [(f.location, f.message) for f in found]
    assert found[0].location.startswith("slide_index 1,") and "below its box" in found[0].message


def _rendered_findings(deck: Path, folder: Path) -> list:
    out = folder / f"render-{deck.stem}"
    out.mkdir()
    pdf = asyncio.run(render_engine.to_pdf(str(deck), out_dir=str(out)))
    with checks.CheckContext(str(deck), "pptx", rendered_pdf=pdf) as ctx:
        return layout_checks.rendered_overflow(ctx)


def test_titles_over_a_table_and_a_chart_are_not_reported(tmp_path):
    """The default template's title placeholders, a table and a chart whose
    text repeats the titles' words, and a slide number in the footer."""
    import pptx
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Revenue by Region 2024"
    table = slide.shapes.add_table(3, 3, Inches(0.5), Inches(1.8), Inches(9), Inches(1.5)).table
    for col, word in enumerate(("Revenue", "Region", "2024")):
        table.cell(0, col).text = word
        table.cell(1, col).text = "12%"
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "North and South revenue up 12%"
    data = CategoryChartData()
    data.categories = ["North", "South"]
    data.add_series("revenue", (12, 15))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(1.8), Inches(9), Inches(5), data)
    deck = tmp_path / "titles.pptx"
    prs.save(str(deck))
    found = _rendered_findings(deck, tmp_path)
    assert found == [], [(f.location, f.message) for f in found]


@pytest.mark.parametrize("name", ["pptx/libreoffice-7.4-numbered.pptx", "shared/google-slides-table.pptx",
                                  "shared/libreoffice-7.4-deck.pptx", "shared/powerpoint-2007-poi-sample.pptx",
                                  "shared/powerpoint-2016-poi-smartart.pptx", "shared/powerpoint-mac-2010-minimal.pptx"])
def test_the_corpus_decks_have_no_rendered_overflow(tmp_path, name):
    deck = tmp_path / Path(name).name
    shutil.copyfile(Path(__file__).parent / "corpus" / name, deck)
    found = _rendered_findings(deck, tmp_path)
    assert found == [], [(f.location, f.message) for f in found]


def test_a_filled_greek_form_shows_its_values_in_libreoffice(tmp_path, monkeypatch):
    pytest.importorskip("weasyprint")

    async def _nothing(*a, **k):
        return None

    monkeypatch.setattr(shared, "_push_preview", _nothing)
    form = tmp_path / "form.pdf"
    asyncio.run(pdf_mod.handle_write_pdf({"path": str(form), "content_type": "html", "form_fields": True,
                                          "content": '<p>Όνομα <input type="text" name="name" '
                                                     'style="width:8cm"></p>'}))
    out = asyncio.run(pdf_mod.handle_edit_pdf({"path": str(form), "operations": [
        {"op": "fill_form", "values": {"name": "Δημήτρης Παπαδόπουλος"}}]}))
    assert out.startswith("PDF saved:"), out
    profile = tmp_path / "profile"
    result = tmp_path / "lo"
    subprocess.run([shutil.which("soffice") or "soffice", "--headless", "--norestore",
                    f"-env:UserInstallation=file://{profile}", "--convert-to", "pdf:draw_pdf_Export",
                    "--outdir", str(result), str(form)], check=True, capture_output=True, timeout=120,
                   env={**os.environ, "HOME": str(tmp_path)})
    assert "Δημήτρης Παπαδόπουλος" in fitz.open(str(result / "form.pdf"))[0].get_text()
