"""Filling a printed form: a PDF with no fields, where the answers go beside
the labels on the page.

Each entry is placed beside a label found on the page (right of it on its
baseline, or below it) or at x, y; every entry is found before anything is
drawn, so a label that is not there fails the operation and leaves the
page as it was.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from results import Code, OpCtx, OpError

DEFAULT_SIZE = 11.0
# Writing on a dotted or underscored line is the point of a printed form.
_LEADER_CHARS = frozenset("_.-–—…·")


@dataclass
class Spot:
    page: int
    x: float
    y: float          # the baseline
    size: float
    entry: dict


def _label_hits(doc: Any, label: str, page: int | None) -> list[tuple[int, Any, float, float]]:
    """Each place ``label`` sits, in reading order: (page, rect, baseline,
    size)."""
    import fitz

    pages = [page] if page is not None else range(doc.page_count)
    found = []
    for index in pages:
        target = doc[index]
        hits = target.search_for(label) or target.search_for(unicodedata.normalize("NFC", label)) \
            or target.search_for(unicodedata.normalize("NFD", label))
        for rect in sorted(hits, key=lambda r: (round(r.y0, 1), r.x0)):
            baseline, size = rect.y1 - rect.height * 0.22, rect.height * 0.8
            for block in target.get_text("dict", clip=rect + (-1, -1, 1, 1)).get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if fitz.Rect(span["bbox"]).intersects(rect) and span.get("text", "").strip():
                            baseline, size = float(span["origin"][1]), float(span["size"])
            found.append((index, rect, baseline, size))
    return found


def _locate(doc: Any, entry: dict, default_size: float | None) -> Spot | str:
    page = entry.get("page")
    total = doc.page_count
    if page is not None and not -total <= page < total:
        return f"page {page} does not exist (the PDF has {total} pages, page 0 to {total - 1})"
    if page is not None and page < 0:
        page += total
    label = entry.get("label")
    if label:
        hits = _label_hits(doc, label, page)
        occurrence = entry.get("occurrence", 1)
        where = f"page_index {page}" if page is not None else "every page"
        if not hits:
            return f"the label {label!r} was not found on {where}"
        if not 1 <= occurrence <= len(hits):
            return f"the label {label!r} is on {where} {len(hits)} time(s), so occurrence {occurrence} does not exist"
        index, rect, baseline, size = hits[occurrence - 1]
        size = entry.get("font_size") or default_size or size or DEFAULT_SIZE
        if entry.get("position", "right") == "below":
            return Spot(index, rect.x0, baseline + size * 1.4, size, entry)
        if entry.get("position", "right") != "right":
            return f"position {entry['position']!r} is not right or below"
        return Spot(index, rect.x1 + entry.get("gap", 6), baseline, size, entry)
    if entry.get("x") is None or entry.get("y") is None:
        return "an entry needs a label, or x and y"
    return Spot(page or 0, float(entry["x"]), float(entry["y"]), entry.get("font_size") or default_size
                or DEFAULT_SIZE, entry)


def _crossed(page: Any, x0: float, x1: float, y: float, size: float) -> bool:
    """Whether a text from x0 to x1 on baseline y runs into a word already
    there (leaders such as ____ or .... do not count)."""
    for word in page.get_text("words"):
        wx0, wy0, wx1, wy1, text = word[:5]
        if set(text) <= _LEADER_CHARS:
            continue
        if wy0 < y and wy1 > y - size * 0.7 and wx0 < x1 and wx1 > x0 + 0.5:
            return True
    return False


def fill(job: Any, ctx: OpCtx, op: dict) -> int:
    from formats.pdf import text as pdf_text
    from formats.pdf.edit import embedded_font, pdf_color

    doc = job.doc
    entries = op["entries"]
    if not entries:
        raise OpError(Code.MISSING_KEY, "no entries were given", location="entries",
                      hint='Give [{"label": "Name:", "text": "..."}, ...].')
    spots: list[Spot] = []
    problems: list[str] = []
    for number, entry in enumerate(entries):
        found = _locate(doc, entry, op.get("font_size"))
        if isinstance(found, str):
            problems.append(f"entry {number}: {found}")
        else:
            spots.append(found)
    if problems:
        raise OpError(Code.NOT_FOUND, "; ".join(problems), location="entries",
                      hint="read_document with mode form_fields lists the page's text with positions.")
    for spot in spots:
        entry = spot.entry
        text = str(entry["text"])
        page = doc[spot.page]
        family, bold, italic = pdf_text.style_of(entry.get("font") or op.get("font"),
                                                 bold=bool(entry.get("bold", False)))
        embedded = embedded_font(job, page, text, family, bold=bold, italic=italic, ctx=ctx)
        color = pdf_color(entry.get("color", op.get("color", [0, 0, 0])), "color")
        width = embedded.width(text, spot.size)
        if spot.x + width > page.rect.width:
            ctx.warn(f"{text[:40]!r} runs past the right edge of page_index {spot.page}")
        elif _crossed(page, spot.x, spot.x + width, spot.y, spot.size):
            ctx.warn(f"{text[:40]!r} runs into text already on page_index {spot.page}")
        pdf_text.draw(page, (spot.x, spot.y), text, size=spot.size, color=color, embedded=embedded)
    return len(spots)
