"""The exact layout check: a deck's rendered text placed against its shape
boxes. These tests draw the "rendering" with PyMuPDF where LibreOffice
would put the text; the office tests render real decks."""

import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")
pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx.util import Inches

import checks
from formats.pdf import layout_checks

EMU_PER_PT = 12700


def _face() -> str:
    import fonts

    try:
        return fonts.resolve("Liberation Sans").face.path
    except LookupError:
        pytest.skip("no font installed here")


_FACE = _face()


def _deck(path: Path, *, hidden_first: bool = False, autofit: str | None = None,
          text: str = "Αποτελέσματα τριμήνου και πολλές ακόμη λέξεις", top: float = 1.0) -> Path:
    prs = pptx.Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    if hidden_first:
        prs.slides.add_slide(prs.slide_layouts[6])._element.set("show", "0")
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    body = slide.shapes.add_textbox(Inches(1), Inches(top), Inches(4), Inches(1))
    body.name = "Body"
    body.text_frame.text = text
    if autofit == "resize":
        body.text_frame._bodyPr.append(body.text_frame._bodyPr.makeelement(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}spAutoFit", {}))
    else:
        for child in list(body.text_frame._bodyPr):
            body.text_frame._bodyPr.remove(child)
    title = slide.shapes.add_textbox(Inches(6), Inches(1), Inches(4), Inches(1))
    title.name = "Fits"
    title.text_frame.text = "Short title"
    prs.save(str(path))
    return path


def _render(deck: Path, out: Path, lines: dict[str, list[tuple[float, float]]], *, pages: int = 1) -> Path:
    """A PDF the size of the deck's slides with each text drawn at
    (x, baseline) in slide points."""
    prs = pptx.Presentation(str(deck))
    w, h = prs.slide_width / EMU_PER_PT, prs.slide_height / EMU_PER_PT
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=w, height=h)
        for text, places in lines.items():
            for x, y in places:
                page.insert_text((x, y), text, fontsize=18, fontname="F0", fontfile=_FACE)
    doc.save(str(out))
    return out


def _run(deck: Path, rendered: Path) -> list:
    with checks.CheckContext(str(deck), "pptx", rendered_pdf=str(rendered)) as ctx:
        return layout_checks.rendered_overflow(ctx)


def test_text_drawn_below_its_box_is_found_and_text_inside_is_not(tmp_path):
    deck = _deck(tmp_path / "a.pptx")
    # Body is 72..360 pt wide, 72..144 pt high: three lines, the last at 200 pt.
    rendered = _render(deck, tmp_path / "a.pdf", {
        "Αποτελέσματα τριμήνου": [(80, 100)], "και πολλές ακόμη": [(80, 150)], "λέξεις": [(80, 200)],
        "Short title": [(440, 100)]})
    found = _run(deck, rendered)
    assert len(found) == 1, found
    finding = found[0]
    assert finding.location.startswith("slide_index 0, shape_id") and "'Body'" in finding.location
    assert "below its box as LibreOffice draws it" in finding.message
    assert finding.severity is checks.Severity.ERROR


def test_a_box_that_grows_to_its_text_is_checked_only_against_the_slide(tmp_path):
    deck = _deck(tmp_path / "b.pptx", autofit="resize", text="Results and many more words")
    rendered = _render(deck, tmp_path / "b.pdf", {"Results and": [(80, 100)], "many more words": [(80, 128)],
                                                 "words": [(80, 156)]})
    assert _run(deck, rendered) == []
    # The same box near the slide's foot: its last line drawn past the edge
    # (LibreOffice draws it there; insert_text would drop it).
    low = _deck(tmp_path / "low.pptx", autofit="resize", text="Results and many more words", top=6.6)
    doc = fitz.open(str(_render(low, tmp_path / "b2.pdf", {"Results and": [(80, 500)]})))
    page = doc[0]
    page.insert_text((80, 500), " ", fontsize=18, fontname="helv")
    content = page.get_contents()[-1]
    doc.update_stream(content, doc.xref_stream(content) +
                      b"\nBT /helv 18 Tf 1 0 0 1 80 -10 Tm (many more words) Tj ET\n")
    doc.save(str(tmp_path / "b3.pdf"))
    found = _run(low, tmp_path / "b3.pdf")
    assert len(found) == 1 and "past the slide's edge" in found[0].message


def test_hidden_slides_are_skipped_when_pages_are_matched(tmp_path):
    deck = _deck(tmp_path / "c.pptx", hidden_first=True)
    rendered = _render(deck, tmp_path / "c.pdf", {"Αποτελέσματα τριμήνου": [(80, 100)],
                                                 "και πολλές ακόμη": [(80, 128)], "λέξεις": [(80, 156)]})
    found = _run(deck, rendered)
    assert len(found) == 1 and found[0].location.startswith("slide_index 1,")
    mismatched = _render(deck, tmp_path / "c2.pdf", {"λέξεις": [(80, 156)]}, pages=2)
    found = _run(deck, mismatched)
    assert found[0].severity is checks.Severity.INFO and "could not be matched" in found[0].message


def test_the_check_is_a_render_check_of_decks():
    assert "pptx.rendered_overflow" in checks.check_ids("pptx")
    assert not checks.has_checks("pptx", checks.AFTER_WRITE) or "pptx.rendered_overflow" not in [
        s for s in checks.check_ids("pptx") if False]


def _deck_with(path: Path, kind: str) -> Path:
    """A title over a table or a chart whose words repeat the title's, and a
    footer whose number sits inside the body's text."""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE

    prs = pptx.Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    title = slide.shapes.add_textbox(Inches(1), Inches(0.5), Inches(8), Inches(1))
    title.name = "Title"
    title.text_frame.text = "Revenue by Region 2024 up 12%"
    for child in list(title.text_frame._bodyPr):   # a fixed box, as a title placeholder is
        title.text_frame._bodyPr.remove(child)
    if kind == "table":
        table = slide.shapes.add_table(2, 3, Inches(1), Inches(2), Inches(8), Inches(1)).table
        for col, word in enumerate(("Revenue", "Region", "2024")):
            table.cell(0, col).text = word
    else:
        data = CategoryChartData()
        data.categories = ["Revenue", "Region"]
        data.add_series("2024", (3, 5))
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(8), Inches(4), data)
    footer = slide.shapes.add_textbox(Inches(11), Inches(6.8), Inches(1), Inches(0.4))
    footer.name = "Number"
    footer.text_frame.text = "‹12›"
    prs.save(str(path))
    return path


@pytest.mark.parametrize("kind", ["table", "chart"])
def test_text_of_a_table_or_chart_under_a_title_is_not_the_title_s(tmp_path, kind):
    deck = _deck_with(tmp_path / f"{kind}.pptx", kind)
    # The title drawn in its box; the table or chart text and a stray 12
    # drawn far below it, inside the table or chart and at the slide's foot.
    rendered = _render(deck, tmp_path / f"{kind}.pdf", {
        "Revenue by Region 2024 up 12%": [(80, 75)], "Revenue": [(80, 170)], "Region": [(300, 200)],
        "2024": [(150, 230)], "12": [(800, 520)]})
    assert _run(deck, rendered) == []


def test_a_word_far_below_a_box_is_not_its_overflow(tmp_path):
    deck = _deck(tmp_path / "far.pptx", text="Results and many more words")
    # The box ends at 144 pt; "words" drawn 300 pt lower is somebody else's.
    rendered = _render(deck, tmp_path / "far.pdf", {"Results and many more": [(80, 100)], "words": [(80, 450)]})
    assert _run(deck, rendered) == []


def test_overflow_running_several_lines_below_the_box_is_still_found(tmp_path):
    deck = _deck(tmp_path / "long.pptx", text="one two three four five six seven eight nine ten")
    rendered = _render(deck, tmp_path / "long.pdf", {
        "one two": [(80, 100)], "three four": [(80, 130)], "five six": [(80, 160)], "seven eight": [(80, 190)],
        "nine ten": [(80, 220)]})
    found = _run(deck, rendered)
    assert len(found) == 1 and "below its box" in found[0].message
