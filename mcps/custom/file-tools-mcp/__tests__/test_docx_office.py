"""What write_docx makes, as LibreOffice draws it: A4 pages, nine list
levels stepping in, numbered headings, a theme colour's fill, a page number
drawn like the footer words above it, Greek in a face that has its glyphs,
a template that converts, and a file another application saved, edited by
address."""

import asyncio
import re
import shutil
from pathlib import Path

import pytest

pytest.importorskip("docx")
fitz = pytest.importorskip("fitz")

import render_engine
import word

CORPUS = Path(__file__).parent / "corpus"
_A4_PT = (595, 842)


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops, create_new=True) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops, "create_new": create_new}))


def _pdf(path: Path, tmp_path: Path):
    out = tmp_path / "pdf"
    out.mkdir(exist_ok=True)
    return fitz.open(asyncio.run(render_engine.to_pdf(str(path), out_dir=str(out))))


def _words(page) -> dict[str, tuple[float, float]]:
    """Each word's first position on the page: (x, y) of its box."""
    found: dict[str, tuple[float, float]] = {}
    for x0, y0, _x1, _y1, text, *_ in page.get_text("words"):
        found.setdefault(text, (x0, y0))
    return found


@pytest.mark.office
def test_a_new_document_is_drawn_on_a4(tmp_path):
    f = tmp_path / "a4" / "a4.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_paragraph", "text": "On A4."}])
    page = _pdf(f, tmp_path)[0]
    assert tuple(round(v) for v in page.rect[2:]) == _A4_PT


@pytest.mark.office
def test_nine_list_levels_step_in_and_lists_restart(tmp_path):
    f = tmp_path / "lists" / "lists.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_list", "ordered": True,
                "items": [{"text": f"Level{n}", "level": n} for n in range(1, 10)]},
               {"op": "add_list", "ordered": True, "items": ["Again"]}])
    page = _pdf(f, tmp_path)[0]
    words = _words(page)
    xs = [words[f"Level{n}"][0] for n in range(1, 10)]
    assert all(b - a > 10 for a, b in zip(xs, xs[1:], strict=False)), xs
    text = page.get_text()
    assert "a." in text and "i." in text
    again = [line for line in text.splitlines() if line.strip()]
    assert "1." in again[again.index("Again") - 1]


@pytest.mark.office
def test_numbered_headings_and_a_theme_fill_are_drawn(tmp_path):
    f = tmp_path / "heads" / "heads.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_heading", "text": "Scope", "level": 1},
               {"op": "add_heading", "text": "Detail", "level": 2},
               {"op": "number_headings", "levels": 2},
               {"op": "add_table", "headers": ["Region", "Q1"], "rows": [["North", "10"]]},
               {"op": "style_table", "header": {"fill": "accent1", "font_color": "white"}}])
    page = _pdf(f, tmp_path)[0]
    words = _words(page)
    for number, heading in (("1.", "Scope"), ("1.1", "Detail")):
        assert abs(words[number][1] - words[heading][1]) < 2 and words[number][0] < words[heading][0], words
    fills = {tuple(round(c * 255) for c in d["fill"]) for d in page.get_drawings() if d.get("fill")}
    assert (0x4F, 0x81, 0xBD) in fills, fills


def _footer_spans(page) -> list[tuple[str, float, int]]:
    """The text spans in the bottom tenth of the page: (text, size, colour)."""
    bottom = page.rect.height * 0.9
    return [(span["text"], round(span["size"], 1), span["color"])
            for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])
            for span in line["spans"] if span["text"].strip() and span["bbox"][1] > bottom]


@pytest.mark.office
def test_a_page_number_is_drawn_like_the_footer_words_on_every_page(tmp_path):
    f = tmp_path / "pages" / "pages.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_paragraph", "text": "First page."}, {"op": "add_page_break"},
               {"op": "add_paragraph", "text": "Second page."},
               {"op": "add_footer", "text": "Internal", "font_size": 8, "color": "808080"},
               {"op": "add_page_number", "format": "Page {page} of {total}"}])
    pdf = _pdf(f, tmp_path)
    assert len(pdf) == 2
    for number, page in enumerate(pdf, 1):
        spans = _footer_spans(page)
        assert " ".join(text for text, _size, _colour in spans) == f"Internal Page {number} of 2", spans
        assert {(size, colour) for _text, size, colour in spans} == {(8.0, 0x808080)}, spans


@pytest.mark.office
def test_greek_is_drawn_in_a_face_with_its_glyphs(tmp_path):
    f = tmp_path / "greek" / "greek.docx"
    f.parent.mkdir()
    greek = "Η επιθεώρηση γίνεται κάθε χρόνο."
    _write(f, [{"op": "add_heading", "text": "Σημειώσεις", "level": 1},
               {"op": "add_paragraph", "text": greek, "font_name": "Calibri"}])
    page = _pdf(f, tmp_path)[0]
    assert greek in page.get_text().replace("\n", " ")
    fonts = {font[3] for font in page.get_fonts()}
    assert any("Carlito" in name for name in fonts), fonts


@pytest.mark.office
def test_a_template_converts(tmp_path):
    f = tmp_path / "tpl" / "letter.dotx"
    f.parent.mkdir()
    _write(f, [{"op": "add_heading", "text": "Letter", "level": 1},
               {"op": "add_paragraph", "text": "Template body"}])
    pdf = _pdf(f, tmp_path)
    page = pdf[0]
    assert tuple(round(v) for v in page.rect[2:]) == _A4_PT
    words = _words(page)
    assert words["Letter"][1] < words["Template"][1]
    sizes = {span["text"]: span["size"] for block in page.get_text("dict")["blocks"]
             for line in block.get("lines", []) for span in line["spans"]}
    assert sizes["Letter"] > sizes["Template body"]


@pytest.mark.office
@pytest.mark.parametrize("name", ["docx/libreoffice-7.4-lists.docx", "docx/word-markup-handbuilt.docx",
                                  "shared/collabora-26.04-walk.docx"])
def test_a_file_another_application_saved_still_opens_after_an_edit(tmp_path, name):
    f = tmp_path / "edit" / Path(name).name
    f.parent.mkdir()
    shutil.copy(CORPUS / name, f)
    msg = _write(f, [{"op": "insert_paragraph", "target": "p0", "text": "Added by address"}], create_new=False)
    assert msg.startswith("Document saved"), msg
    assert "Added by address" in _pdf(f, tmp_path)[0].get_text()


@pytest.mark.office
def test_the_table_of_contents_numbers_are_the_pages_its_headings_land_on(tmp_path):
    f = tmp_path / "toc" / "toc.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_heading", "text": "Opening", "level": 1}, {"op": "add_paragraph", "text": "Text."},
               {"op": "add_page_break"},
               {"op": "add_heading", "text": "Middle", "level": 1}, {"op": "add_paragraph", "text": "Text."},
               {"op": "add_page_break"},
               {"op": "add_heading", "text": "Closing", "level": 2}, {"op": "add_paragraph", "text": "Text."}])
    msg = _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before"},
                     {"op": "add_page_break", "target": "p0", "position": "before"}], create_new=False)
    assert "lists 3 headings" in msg and "3 page numbers from a LibreOffice layout" in msg, msg
    assert "missing" not in msg, msg
    pdf = _pdf(f, tmp_path)
    toc_text = " ".join(pdf[0].get_text().split())
    landed = {}
    for number, page in enumerate(pdf, 1):
        for heading in ("Opening", "Middle", "Closing"):
            if number > 1 and heading in page.get_text() and heading not in landed:
                landed[heading] = number
    assert landed == {"Opening": 2, "Middle": 3, "Closing": 4}, landed
    for heading, number in landed.items():
        assert re.search(rf"{heading}\W+{number}\b", toc_text), toc_text


@pytest.mark.office
def test_a_page_reference_names_the_page_its_bookmark_lands_on(tmp_path):
    f = tmp_path / "pageref" / "pageref.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_paragraph", "text": "See page "}, {"op": "add_page_break"},
               {"op": "add_page_break"}, {"op": "add_paragraph", "text": "[the chart]"}])
    msg = _write(f, [{"op": "add_caption", "target": "p3", "text": "Revenue.", "bookmark": "fig_rev"},
                     {"op": "add_cross_reference", "bookmark": "fig_rev", "show": "page", "target": "p0"}],
                 create_new=False)
    assert "1 page number from a LibreOffice layout" in msg, msg
    pdf = _pdf(f, tmp_path)
    landed = next(n for n, page in enumerate(pdf, 1) if "Figure 1. Revenue." in " ".join(page.get_text().split()))
    assert landed == 3
    assert f"See page {landed}" in " ".join(pdf[0].get_text().split())


@pytest.mark.office
def test_a_footnote_is_drawn_at_the_foot_of_its_page(tmp_path):
    f = tmp_path / "notes" / "notes.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_paragraph", "text": "Revenue grew 9% in the quarter."}])
    _write(f, [{"op": "add_footnote", "target": "p0", "after_text": "9%", "text": "Audited figures."}],
           create_new=False)
    page = _pdf(f, tmp_path)[0]
    note = page.search_for("Audited figures.")
    claim = page.search_for("Revenue grew")
    assert note and claim and note[0].y0 > page.rect.height * 0.7 > claim[0].y0, (note, claim)


@pytest.mark.office
@pytest.mark.parametrize("settle, shown, gone", [("accept_all_changes", "45 days", "30 days"),
                                                  ("reject_all_changes", "30 days", "45 days")])
def test_settled_changes_draw_the_expected_text(tmp_path, settle, shown, gone):
    f = tmp_path / settle / "terms.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_paragraph", "text": "Payment within 30 days."}])
    _write(f, [{"op": "search_and_replace", "find": "30", "replace": "45", "track": True}], create_new=False)
    _write(f, [{"op": settle}], create_new=False)
    text = " ".join(_pdf(f, tmp_path)[0].get_text().split())
    assert shown in text and gone not in text, text


@pytest.mark.office
def test_a_toc_with_page_numbers_reads_as_up_to_date(tmp_path):
    import checks
    from formats.docx import document_checks  # noqa: F401  (registers the checks)

    f = tmp_path / "fresh" / "fresh.docx"
    f.parent.mkdir()
    _write(f, [{"op": "add_heading", "text": "Introduction", "level": 1}, {"op": "add_page_break"},
               {"op": "add_heading", "text": "Scope", "level": 1}])
    msg = _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before"}], create_new=False)
    assert "toc_out_of_date" not in msg, msg
    with checks.CheckContext(str(f), "docx") as ctx:
        assert checks.run_checks(ctx, phase="document", only=["docx.toc_out_of_date"]) == []


def _ink(page, band: tuple[float, float], dpi: int = 150) -> int:
    """The dark pixels of the page between two heights (points)."""
    clip = fitz.Rect(page.rect.x0, band[0], page.rect.x1, band[1])
    pixmap = page.get_pixmap(dpi=dpi, clip=clip, colorspace=fitz.csGRAY)
    return sum(1 for value in pixmap.samples if value < 128)


@pytest.mark.office
def test_an_equation_is_drawn_not_left_blank(tmp_path):
    """B17: the image's LibreOffice draws Office equations with its math
    module; without it the equation's place on the page stays white."""
    f = tmp_path / "e.docx"
    out = _write(f, [{"op": "add_paragraph", "text": "Above"},
                     {"op": "add_equation", "latex": r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"},
                     {"op": "add_paragraph", "text": "Below"}])
    assert "Nothing saved" not in out, out
    with _pdf(f, tmp_path) as doc:
        page = doc[0]
        words = {w[4]: w for w in page.get_text("words")}
        above, below = words["Above"], words["Below"]
        band = (above[3] + 1, below[1] - 1)
        assert band[1] - band[0] > 12, band
        # The quadratic formula's glyphs and fraction bar: hundreds of dark
        # pixels at 150 dpi; an empty band holds none.
        assert _ink(page, band) > 300, _ink(page, band)


@pytest.mark.office
def test_a_picture_starts_at_the_texts_left_edge(tmp_path):
    """A picture in its own paragraph sits where the text starts, as Word
    draws it: an inline picture with no distance attributes is spaced
    0.32 cm from the margin by LibreOffice."""
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGB", (400, 160), (16, 42, 84)).save(logo)
    f = tmp_path / "p.docx"
    out = _write(f, [{"op": "add_image", "image_path": str(logo), "width_inches": 2, "alignment": "left"},
                     {"op": "add_paragraph", "text": "Margin"}])
    assert "Nothing saved" not in out, out
    with _pdf(f, tmp_path) as doc:
        page = doc[0]
        text_x = next(w[0] for w in page.get_text("words") if w[4] == "Margin")
        picture_x = page.get_image_info()[0]["bbox"][0]
    assert abs(picture_x - text_x) < 1.5, (picture_x, text_x)
