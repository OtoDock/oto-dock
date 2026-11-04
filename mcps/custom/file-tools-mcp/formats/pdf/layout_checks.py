"""A deck's text as LibreOffice draws it (the engine Collabora uses): the
rendered PDF's text placed against each shape's box, so overflow is
reported as the person will see it, not as the width tables estimate it.

Runs only in check_document with render: true (the parent renders the deck
before the check). Each rendered span (one font run) is given to the text
shape whose text holds it and whose box holds its middle, the nearest box
first; a span no shape claims is left out. A glyph's extent is taken from
the baseline (cap height above, descender below), not from PyMuPDF's line
box, which is a fifth taller than the glyphs of a title.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import checks

RENDERED_OVERFLOW = "rendered_overflow"
EMU_PER_PT = 12700
EMU_PER_IN = 914400
CAP_HEIGHT = 0.72
DESCENDER = 0.22
TOLERANCE = 0.15          # of the font size, plus one point
# A span continues a shape's text below its box when its glyphs start within
# this many font sizes of the text above (double line spacing still chains).
CONTINUES = 1.6
# Shapes that draw text of their own the check does not judge.
_OWN_TEXT = frozenset({"table", "chart", "picture", "graphic"})
ERROR_SHARE = 0.10        # of the box height
_BULLETS = re.compile(r"^[\s•▪◦‣⁃·\-–—*>]+|^\(?[0-9a-zA-Z]{1,3}[.)]\s+")
_SPACES = re.compile(r"\s+")


@dataclass
class Span:
    text: str
    x0: float
    x1: float
    baseline: float
    size: float

    @property
    def top(self) -> float:
        return self.baseline - CAP_HEIGHT * self.size

    @property
    def bottom(self) -> float:
        return self.baseline + DESCENDER * self.size


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return _SPACES.sub(" ", text).strip()


def _spans(page: Any, scale: float) -> list[Span]:
    """The page's text runs in slide points (the page is the slide drawn at
    ``scale`` points per slide point)."""
    import fitz

    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_MEDIABOX_CLIP
    found = []
    # Text past the page's edge is what the edge check looks for: no clip.
    for block in page.get_text("dict", flags=flags, clip=fitz.INFINITE_RECT()).get("blocks", []):
        for line in block.get("lines", []):
            if abs(line.get("dir", (1.0, 0.0))[1]) > 0.01:   # turned text
                continue
            for span in line.get("spans", []):
                text = _BULLETS.sub("", _norm(span.get("text", "")))
                if len(text) < 2 and not text.isalpha():
                    continue
                x0, _, x1, _ = span["bbox"]
                found.append(Span(text, x0 / scale, x1 / scale, span["origin"][1] / scale, span["size"] / scale))
    return found


def _box_pt(shape: Any) -> tuple[float, float, float, float]:
    box = shape.box
    return box.x / EMU_PER_PT, box.y / EMU_PER_PT, (box.x + box.cx) / EMU_PER_PT, (box.y + box.cy) / EMU_PER_PT


def _candidates(slide: Any) -> tuple[list[tuple[Any, str]], list[tuple[float, float, float, float]]]:
    """The text shapes the check judges, with their text, and the boxes of
    the shapes that draw text of their own (tables, charts, pictures,
    diagrams): a span inside one of those is that shape's."""
    from formats.pptx import pptx_geometry as geo

    shapes: list[tuple[Any, str]] = []
    obstacles: list[tuple[float, float, float, float]] = []
    for shape in slide.walk():
        if shape.hidden or shape.box is None:
            continue
        if shape.kind in _OWN_TEXT:
            obstacles.append(_box_pt(shape))
            continue
        if shape.frame is None or shape.kind == geo.KIND_GROUP:
            continue
        if shape.box.rot % 360 or shape.frame.vertical != "horz":
            continue
        text = _norm(" ".join(r.text for p in shape.paragraphs for r in p.runs))
        if text:
            shapes.append((shape, text))
    return shapes, obstacles


def _inside(span: Span, box: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = box
    return x0 - 1 <= (span.x0 + span.x1) / 2 <= x1 + 1 and y0 - 1 <= span.baseline <= y1 + 1


def _claim(spans: list[Span], candidates: list[tuple[Any, str]],
           obstacles: list[tuple[float, float, float, float]]) -> dict[int, tuple[Any, list[Span]]]:
    """Each span given to the shape it belongs to: one whose text holds it,
    which it starts within horizontally, and which it sits in or continues
    directly below, line after line (an overflow runs down from the box;
    a word far below the box is another shape's). A span inside a table,
    chart or picture is left to that shape."""
    claimed: dict[int, tuple[Any, list[Span]]] = {}
    reach: dict[int, float] = {}
    for span in sorted(spans, key=lambda s: s.baseline):
        if any(_inside(span, box) for box in obstacles):
            continue
        middle = (span.x0 + span.x1) / 2
        best, best_gap = None, None
        for shape, text in candidates:
            if span.text not in text:
                continue
            x0, y0, x1, y1 = _box_pt(shape)
            if not (x0 - 2 <= span.x0 <= x1 and x0 - 2 <= middle <= x1 + 2):
                continue
            if span.top < y0 - span.size:
                continue
            bottom = reach.get(shape.shape_id, y1)
            if span.baseline <= y1:
                gap = 0.0
            elif span.top <= bottom + CONTINUES * span.size:
                gap = span.baseline - y1
            else:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = shape, gap
        if best is not None:
            claimed.setdefault(best.shape_id, (best, []))[1].append(span)
            reach[best.shape_id] = max(reach.get(best.shape_id, _box_pt(best)[3]), span.bottom)
    return claimed


def _where(slide: Any, shape: Any) -> str:
    return f"slide_index {slide.index}, shape_id {shape.shape_id} '{shape.name}'"


def _inches(points: float) -> str:
    return f"{points / 72:.2f} in"


def _excerpt(spans: list[Span]) -> str:
    text = " ".join(s.text for s in spans)
    return text if len(text) <= 60 else text[:60] + "…"


def rendered_overflow(ctx: checks.CheckContext) -> list[checks.Finding]:
    import fitz

    from formats.pptx import pptx_deck, pptx_geometry, pptx_measure

    if ctx.rendered_pdf is None:
        return []
    ctx.zip()   # the package's size caps, before python-pptx reads it
    prs = ctx.doc if ctx.doc is not None else pptx_deck.open_presentation(ctx.open())
    slides = [s for s in pptx_geometry.iter_slides(prs) if not s.hidden]
    if not slides:   # LibreOffice draws an empty deck as one blank page
        return []
    parts = [str(slide.part.partname).lstrip("/") for slide in prs.slides]
    slide_w, slide_h = (prs.slide_width or 0) / EMU_PER_PT, (prs.slide_height or 0) / EMU_PER_PT
    found: list[checks.Finding] = []
    with fitz.open(ctx.rendered_pdf) as doc:
        if doc.page_count != len(slides) or not slide_w:
            return [checks.Finding(checks.Severity.INFO, "", message=(
                f"the rendering has {doc.page_count} pages for {len(slides)} shown slides, so it could not be "
                "matched to them"), fix="Check the slides with screenshot_document.", key="unmatched")]
        for slide, page in zip(slides, doc, strict=True):
            scale = page.rect.width / slide_w
            spans = _spans(page, scale)
            shapes, obstacles = _candidates(slide)
            for shape, mine in _claim(spans, shapes, obstacles).values():
                found.extend(_judge(slide, shape, mine, slide_w, slide_h, parts[slide.index],
                                    pptx_measure.needs_room(shape)))
    return found


def _judge(slide: Any, shape: Any, spans: list[Span], slide_w: float, slide_h: float, part: str,
           boxed: bool) -> list[checks.Finding]:
    found = []
    x0, y0, x1, y1 = _box_pt(shape)
    size = max(s.size for s in spans)
    tolerance = TOLERANCE * size + 1.0
    fix = ("Enlarge the box (set_shape_format height), shorten the text, lower font_size, or give it "
           "autofit: shrink.")
    if boxed:
        below = max(s.bottom for s in spans) - y1
        above = y0 - min(s.top for s in spans)
        right = max(s.x1 for s in spans) - x1
        worst, side = max((below, "below"), (above, "above"), (right, "past the right of"))
        if worst > tolerance:
            share = worst / max(y1 - y0 if side != "past the right of" else x1 - x0, 1.0)
            severity = checks.Severity.ERROR if share > ERROR_SHARE else checks.Severity.WARNING
            found.append(checks.Finding(
                severity, "", location=_where(slide, shape),
                message=f"the text runs {_inches(worst)} {side} its box as LibreOffice draws it (the engine "
                        "Collabora uses)",
                fix=fix, excerpt=_excerpt(spans), part=part, key=f"box {shape.shape_id}"))
    edge = max(max(s.x1 for s in spans) - slide_w, max(s.bottom for s in spans) - slide_h,
               -min(s.x0 for s in spans), -min(s.top for s in spans))
    if edge > tolerance:
        found.append(checks.Finding(
            checks.Severity.ERROR, "", location=_where(slide, shape),
            message=f"the text runs {_inches(edge)} past the slide's edge as LibreOffice draws it",
            fix="Move or resize the box inside the slide, or shorten the text.", excerpt=_excerpt(spans),
            part=part, key=f"edge {shape.shape_id}"))
    return found


def register_all() -> None:
    checks.register("pptx", RENDERED_OVERFLOW, rendered_overflow, after_write=False, cost="render")


register_all()
