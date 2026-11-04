"""How a workbook prints, read from its rendering: a chart or picture cut
by a page break, and a sheet with no page setup of its own whose columns
run onto another page.

Runs only in check_document with render: true, on the rendering of the
marked copy ``print_marks`` makes (each chart and picture a picture of its
own colour, the first and last columns marked): a mark on two pages, or
past the edge of its page, was cut; two column marks on different pages
mean the columns did not fit across one page. A rendering that holds no
mark (the copy could not be made) gives no finding.
"""

from __future__ import annotations

from typing import Any

import checks
from formats.xlsx import print_marks
from formats.xlsx.refs import quote_sheet

FMT = "xlsx"
PRINT_LAYOUT = "print_layout"
# A mark's colour after the engine re-encodes it may differ by this much in a
# channel; the levels are 51 apart.
COLOUR_TOLERANCE = 20
EDGE_TOLERANCE_PT = 1.0
_FIT = "set_print_setup with fit_width: 1 and fit_height: 0"


def print_layout(ctx: checks.CheckContext) -> list[checks.Finding]:
    import fitz

    if ctx.rendered_pdf is None:
        return []
    marks = print_marks.plan(ctx.zip())
    if not marks:
        return []
    landed: dict[int, list[tuple[int, Any, Any]]] = {}
    with fitz.open(ctx.rendered_pdf) as doc:
        colours: dict[int, int | None] = {}
        for page in doc:
            for info in page.get_image_info(xrefs=True):
                xref = info.get("xref") or 0
                if not xref:
                    continue
                if xref not in colours:
                    colours[xref] = _mark_of(doc, xref, marks)
                index = colours[xref]
                if index is not None:
                    landed.setdefault(index, []).append((page.number, fitz.Rect(info["bbox"]), page.rect))
    if not landed:
        return []
    found = [f for index, mark in enumerate(marks) if mark.kind in (print_marks.CHART, print_marks.PICTURE)
             for f in _cut(mark, landed.get(index, []))]
    for index, mark in enumerate(marks):
        if mark.kind == print_marks.FIRST_COLUMN and index + 1 < len(marks):
            found.extend(_spilled(mark, landed.get(index, []), marks[index + 1], landed.get(index + 1, [])))
    return found


def _mark_of(doc: Any, xref: int, marks: list[print_marks.Mark]) -> int | None:
    """The mark a picture of the rendering is, by its size and colour."""
    import fitz

    try:
        pixmap = fitz.Pixmap(doc, xref)
        if pixmap.width != print_marks.MARK_PX or pixmap.height != print_marks.MARK_PX:
            return None
        if pixmap.colorspace is None or pixmap.colorspace.n != 3 or pixmap.alpha:
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap, 0)
        rgb = pixmap.pixel(pixmap.width // 2, pixmap.height // 2)[:3]
    except (RuntimeError, ValueError):
        return None
    best = min(range(len(marks)), key=lambda i: max(abs(a - b) for a, b in zip(marks[i].colour, rgb, strict=True)))
    if max(abs(a - b) for a, b in zip(marks[best].colour, rgb, strict=True)) > COLOUR_TOLERANCE:
        return None
    return best


def _where(mark: print_marks.Mark) -> str:
    sheet = quote_sheet(mark.sheet)
    return f"{sheet}!{mark.cell}" if mark.cell else sheet


def _pages(numbers: list[int]) -> str:
    shown = [str(n + 1) for n in numbers]
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def _cut(mark: print_marks.Mark, places: list[tuple[int, Any, Any]]) -> list[checks.Finding]:
    if not places:
        return []
    pages = sorted({number for number, _box, _page in places})
    past_edge = [number for number, box, page in places
                 if box.x0 < page.x0 - EDGE_TOLERANCE_PT or box.y0 < page.y0 - EDGE_TOLERANCE_PT
                 or box.x1 > page.x1 + EDGE_TOLERANCE_PT or box.y1 > page.y1 + EDGE_TOLERANCE_PT]
    if len(pages) > 1:
        message = f"the {mark.kind} '{mark.name}' is cut across printed pages {_pages(pages)}"
    elif past_edge:
        message = f"the {mark.kind} '{mark.name}' is cut at the edge of printed page {pages[0] + 1}"
    else:
        return []
    where = "add_chart's anchor" if mark.kind == print_marks.CHART else "add_image's cell"
    return [checks.Finding(
        checks.Severity.WARNING, "", location=_where(mark), message=message,
        fix=f"Fit the sheet to one page wide ({_FIT}), or put the {mark.kind} under the table ({where}).",
        key=f"cut {mark.sheet} {mark.name}")]


def _spilled(first: print_marks.Mark, first_places: list[tuple[int, Any, Any]], last: print_marks.Mark,
             last_places: list[tuple[int, Any, Any]]) -> list[checks.Finding]:
    if last.kind != print_marks.LAST_COLUMN or not first_places or not last_places:
        return []
    if {n for n, _b, _p in first_places} == {n for n, _b, _p in last_places}:
        return []
    return [checks.Finding(
        checks.Severity.WARNING, "", location=quote_sheet(first.sheet),
        message=f"the sheet's columns do not fit across one printed page: column {last.name} prints on another "
                f"page than column {first.name}",
        fix=f"Fit the sheet to one page wide ({_FIT}), or set orientation: landscape when it is only a little "
            "too wide.",
        key=f"columns {first.sheet}")]


def register_all() -> None:
    checks.register(FMT, PRINT_LAYOUT, print_layout, after_write=False, cost=checks.COST_RENDER)


register_all()
