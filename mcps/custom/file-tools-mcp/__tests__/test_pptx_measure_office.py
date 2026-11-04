"""The overflow measurement against LibreOffice's own layout. Two matrices
of text boxes are measured, rendered to PDF, and the lines LibreOffice drew
are counted:

- one paragraph in a 3.5 in box: families, sizes, Latin, Greek, capitals,
  digits, two line spacings;
- several paragraphs in boxes 2.5 and 5 in wide: bullets and numbers with
  a hanging indent, space before and after, line spacing in points, and
  wide insets.

The agreement rates are what the overflow check's verdicts rest on, within
what the matrices hold. And the shrink write_pptx stores, judged on the
fonts the image draws.

``FILETOOLS_MEASURE_REPORT`` names a file the rates and every disagreement
are written to (the second matrix's beside it, ``-rich`` added).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")
fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

import render_engine
from formats.pptx import pptx_deck, pptx_geometry, pptx_measure

pytestmark = pytest.mark.office

FAMILIES = ("Calibri", "Cambria", "Arial", "Times New Roman", "DejaVu Sans")
SIZES = (12, 18, 28)
SPACINGS = (1.0, 1.5)
TEXTS = {
    "short": "Revenue grew in every region",
    "medium": "Revenue grew in every region this quarter, led by EMEA, while costs stayed flat and margins widened.",
    "long": ("The board approved the plan for next year: three new markets, a hiring freeze in support functions, "
             "and a review of every supplier contract above fifty thousand euros before the end of March."),
    "greek": "Τα έσοδα αυξήθηκαν σε κάθε περιοχή αυτό το τρίμηνο, με πρώτη την Ευρώπη, ενώ το κόστος έμεινε σταθερό.",
    "greek_long": ("Το διοικητικό συμβούλιο ενέκρινε το σχέδιο για την επόμενη χρονιά: τρεις νέες αγορές, πάγωμα "
                   "προσλήψεων στις υποστηρικτικές λειτουργίες και έλεγχο κάθε σύμβασης προμηθευτή."),
    "caps": "QUARTERLY RESULTS AHEAD OF PLAN IN EVERY REGION AND EVERY PRODUCT LINE",
    "digits": "Q1 1,234,567.89 · Q2 2,345,678.90 · Q3 3,456,789.01 · Q4 4,567,890.12 · FY 11,604,926.92",
}
BOX_W, BOX_H = 3.5, 1.2
COL_GAP, ROW_GAP = 4.0, 10.6
COLUMNS, ROWS = 4, 1


def _cases() -> list[dict]:
    return [{"family": f, "size": s, "spacing": sp, "text": t}
            for f in FAMILIES for s in SIZES for sp in SPACINGS for t in TEXTS]


def _build(path: Path, cases: list[dict]) -> None:
    prs = pptx_deck.new_presentation()
    prs.slide_width, prs.slide_height = Inches(16.5), Inches(11)
    blank = pptx_deck.resolve_layout(prs, "blank")[0]
    per_slide = COLUMNS * ROWS
    slide = None
    for n, case in enumerate(cases):
        if n % per_slide == 0:
            slide = prs.slides.add_slide(blank)
        col, row = n % per_slide % COLUMNS, n % per_slide // COLUMNS
        box = slide.shapes.add_textbox(Inches(0.3 + col * COL_GAP), Inches(0.3 + row * ROW_GAP),
                                       Inches(BOX_W), Inches(BOX_H))
        box.name = f"case {n}"
        tf = box.text_frame
        tf.word_wrap = True
        body = tf._txBody.find(qn("a:bodyPr"))
        for child in list(body):
            body.remove(child)
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = TEXTS[case["text"]]
        run.font.size, run.font.name = Pt(case["size"]), case["family"]
        p.line_spacing = case["spacing"]
        case.update(slide=len(prs.slides) - 1, x=0.3 + col * COL_GAP, y=0.3 + row * ROW_GAP)
    prs.save(str(path))


def _drawn(page, case: dict) -> tuple[int, float]:
    """(lines LibreOffice drew in the box's column, the bottom of the last
    one in points below the box's top)."""
    scale = page.rect.width / (16.5 * 72)
    x0, y0 = case["x"] * 72 * scale, case["y"] * 72 * scale
    width = case.get("width", BOX_W)
    clip = fitz.Rect(x0 - 2, y0 - 2, x0 + (width + 0.4) * 72 * scale, y0 + (ROW_GAP - 0.2) * 72 * scale)
    baselines: list[tuple[float, float]] = []
    for block in page.get_text("dict", clip=clip)["blocks"]:
        for line in block.get("lines", []):
            # A bullet glyph sits on a baseline of its own.
            spans = [s for s in line["spans"] if s["text"].strip() not in ("", "•")]
            if spans:
                baselines.append((round(spans[0]["origin"][1], 1), max(s["bbox"][3] for s in spans)))
    tops = sorted({b for b, _ in baselines})
    bottom = max((b for _, b in baselines), default=y0)
    return len(tops), (bottom - y0) / scale


def _render(deck: Path, tmp_path: Path) -> Path:
    out = tmp_path / "pdf"
    out.mkdir()
    return Path(asyncio.run(render_engine.to_pdf(str(deck), out_dir=str(out))))


def test_the_measured_lines_agree_with_libreoffice(tmp_path):
    cases = _cases()
    deck = tmp_path / "matrix.pptx"
    _build(deck, cases)
    pdf = fitz.open(str(_render(deck, tmp_path)))
    prs = pptx_deck.open_presentation(str(deck))
    geometry = {s.index: s for s in pptx_geometry.iter_slides(prs)}
    same_lines = same_verdict = 0
    misses = []
    for n, case in enumerate(cases):
        shape = next(s for s in geometry[case["slide"]].shapes if s.name == f"case {n}")
        measured = pptx_measure.measure(shape)
        drawn, bottom = _drawn(pdf[case["slide"]], case)
        room_bottom = (BOX_H * 72) - 3.6
        drawn_over = bottom > room_bottom + 1.0
        same_lines += measured.lines == drawn
        same_verdict += (measured.over > 0) == drawn_over
        if measured.lines != drawn or (measured.over > 0) != drawn_over:
            misses.append({**case, "measured": measured.lines, "drawn": drawn, "measured_over": round(measured.over, 3),
                           "drawn_bottom": round(bottom, 1)})
    rates = {"cases": len(cases), "lines": same_lines / len(cases), "verdicts": same_verdict / len(cases),
             "misses": misses}
    if os.environ.get("FILETOOLS_MEASURE_REPORT"):
        Path(os.environ["FILETOOLS_MEASURE_REPORT"]).write_text(json.dumps(rates, indent=1, ensure_ascii=False))
    assert rates["lines"] >= 0.95, (rates["lines"], misses[:5])
    assert rates["verdicts"] >= 0.98, (rates["verdicts"], misses[:5])
    # A miss counts a line too many, never too few: the verdicts err safe.
    assert [m for m in misses if m["measured"] < m["drawn"]] == []


RICH_FAMILIES = ("Calibri", "Times New Roman", "DejaVu Sans")
RICH_SIZES = (14, 20)
RICH_WIDTHS = (2.5, 5.0)
RICH_KINDS = ("bullets", "numbers", "spaced", "points", "inset")
ITEMS = ("Revenue grew in every region this quarter", "Costs stayed flat while margins widened",
         "Τα έσοδα αυξήθηκαν σε κάθε περιοχή", "Hiring is frozen in support functions until March")
RICH_COLUMNS, RICH_GAP = 3, 5.4


def _rich_cases() -> list[dict]:
    return [{"family": f, "size": s, "width": w, "kind": k}
            for f in RICH_FAMILIES for s in RICH_SIZES for w in RICH_WIDTHS for k in RICH_KINDS]


def _bullet(paragraph, kind: str) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    ppr.set("marL", str(Inches(0.3)))
    ppr.set("indent", str(-Inches(0.3)))
    if kind == "numbers":
        etree.SubElement(ppr, qn("a:buAutoNum"), type="arabicPeriod")
    else:
        etree.SubElement(ppr, qn("a:buChar"), char="•")


def _build_rich(path: Path, cases: list[dict]) -> None:
    prs = pptx_deck.new_presentation()
    prs.slide_width, prs.slide_height = Inches(16.5), Inches(11)
    blank = pptx_deck.resolve_layout(prs, "blank")[0]
    slide = None
    for n, case in enumerate(cases):
        if n % RICH_COLUMNS == 0:
            slide = prs.slides.add_slide(blank)
        col = n % RICH_COLUMNS
        box = slide.shapes.add_textbox(Inches(0.3 + col * RICH_GAP), Inches(0.3), Inches(case["width"]),
                                       Inches(BOX_H))
        box.name = f"case {n}"
        tf = box.text_frame
        tf.word_wrap = True
        body = tf._txBody.find(qn("a:bodyPr"))
        for child in list(body):
            body.remove(child)
        kind = case["kind"]
        texts = [TEXTS["long"]] if kind == "inset" else list(ITEMS[:3] if kind in ("spaced", "points") else ITEMS)
        if kind == "inset":
            for side in ("lIns", "tIns", "rIns", "bIns"):
                body.set(side, str(Inches(0.3)))
        for i, text in enumerate(texts):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            if kind == "spaced":
                p.space_before, p.space_after = Pt(6), Pt(12)
            if kind == "points":
                p.line_spacing = Pt(case["size"] * 1.4)
            if kind in ("bullets", "numbers"):
                _bullet(p, kind)
            run = p.add_run()
            run.text = text
            run.font.size, run.font.name = Pt(case["size"]), case["family"]
        case.update(slide=len(prs.slides) - 1, x=0.3 + col * RICH_GAP, y=0.3,
                    bottom_inset=0.3 if kind == "inset" else 0.05)
    prs.save(str(path))


def test_the_measured_lines_agree_on_lists_spacing_and_insets(tmp_path):
    cases = _rich_cases()
    deck = tmp_path / "rich.pptx"
    _build_rich(deck, cases)
    pdf = fitz.open(str(_render(deck, tmp_path)))
    prs = pptx_deck.open_presentation(str(deck))
    geometry = {s.index: s for s in pptx_geometry.iter_slides(prs)}
    same_lines = same_verdict = 0
    misses = []
    for n, case in enumerate(cases):
        shape = next(s for s in geometry[case["slide"]].shapes if s.name == f"case {n}")
        measured = pptx_measure.measure(shape)
        drawn, bottom = _drawn(pdf[case["slide"]], case)
        drawn_over = bottom > BOX_H * 72 - case["bottom_inset"] * 72 + 1.0
        same_lines += measured.lines == drawn
        same_verdict += (measured.over > 0) == drawn_over
        if measured.lines != drawn or (measured.over > 0) != drawn_over:
            misses.append({**case, "measured": measured.lines, "drawn": drawn, "measured_over": round(measured.over, 3),
                           "drawn_bottom": round(bottom, 1)})
    rates = {"cases": len(cases), "lines": same_lines / len(cases), "verdicts": same_verdict / len(cases),
             "misses": misses}
    if os.environ.get("FILETOOLS_MEASURE_REPORT"):
        report = Path(os.environ["FILETOOLS_MEASURE_REPORT"])
        report.with_name(f"{report.stem}-rich{report.suffix}").write_text(
            json.dumps(rates, indent=1, ensure_ascii=False))
    assert rates["lines"] >= 0.9, (rates["lines"], misses[:5])
    assert rates["verdicts"] >= 0.95, (rates["verdicts"], misses[:5])
    assert [m for m in misses if m["measured"] < m["drawn"]] == []


async def _same(path, *args, **kwargs):
    return path


async def _nothing(*args, **kwargs):
    return None


def test_a_stored_shrink_is_the_largest_step_that_fits(tmp_path, monkeypatch):
    """Through write_pptx, on the fonts the image draws: the stored scale is
    the largest of PowerPoint's steps at which the text fits. LibreOffice
    draws a shrink of its own, so a render cannot judge the stored one."""
    import powerpoint
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _same)
    monkeypatch.setattr(shared, "_push_preview", _nothing)
    monkeypatch.setattr(shared, "_notify_file_written", _nothing)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)
    deck = tmp_path / "shrink.pptx"
    ops = [{"op": "add_slide", "layout": "blank"}]
    for n, (family, text) in enumerate((f, t) for f in FAMILIES for t in ("long", "greek_long", "medium")):
        ops.append({"op": "add_textbox", "slide_index": 0, "text": TEXTS[text], "font_name": family, "font_size": 24,
                    "left": 0.2 + (n % 5) * 2.6, "top": 0.2 + (n // 5) * 2.4, "width": 2.5, "height": 1.2,
                    "autofit": "shrink"})
    out = asyncio.run(powerpoint.handle_write_pptx({"path": str(deck), "create_new": True, "operations": ops}))
    assert out.startswith("Presentation saved: "), out
    prs = pptx_deck.open_presentation(str(deck))
    shrunk = 0
    for shape in pptx_geometry.slide_geometry(prs, prs.slides[0], 0).shapes:
        stored = shape.frame.font_scale
        assert pptx_measure.fit_scale(shape)[0] == pytest.approx(stored), shape.name
        assert pptx_measure.measure(shape).over == 0, shape.name
        if stored < 1:
            shrunk += 1
            bigger = pptx_measure.SCALES[pptx_measure.SCALES.index(round(stored, 3)) - 1]
            assert pptx_measure.measure(shape, scale=bigger, line_reduction=0.1 if bigger > 0.9 else 0.2).over > 0
    assert shrunk >= 10
