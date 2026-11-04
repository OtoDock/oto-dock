"""read_document for PowerPoint: every slide by the slide_index the write
ops take, every shape by its shape_index, id and name, with its placeholder,
position, size and text; the deck's layouts; tables and charts.

Document text is shown escaped (controls, line separators and bidi marks
become escapes), so a deck cannot draw lines that pose as the read's own.
"""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from formats.pptx.pptx_deck import describe_layouts, open_presentation
from formats.pptx.pptx_escape import NAME_CAP, escaped
from formats.pptx.pptx_geometry import (
    KIND_CHART,
    KIND_GROUP,
    KIND_TABLE,
    TITLE_TYPES,
    Paragraph,
    Shape,
    Slide,
    inches,
    iter_slides,
)

DETAIL_OUTLINE = "outline"
DETAIL_SHAPES = "shapes"
DETAIL_TEXT = "text"
DETAILS = (DETAIL_OUTLINE, DETAIL_SHAPES, DETAIL_TEXT)

SHAPE_TEXT_CAP = 600
NOTES_CAP = 600
TABLE_ROWS = 10
TABLE_COLS = 8
CHART_VALUES = 8
CELL_CAP = 40
SHAPES_PER_SLIDE = 200
# Under the worker's 512 KB cap on a result, so a big deck ends with the
# slides left to read instead of a cut mid-line.
READ_BUDGET = 400_000

_PLACEHOLDER_WORDS = {
    "title": "title", "ctrTitle": "title", "subTitle": "subtitle", "body": "body", "obj": "content",
    "pic": "picture", "chart": "chart", "tbl": "table", "media": "media", "clipArt": "clip art",
    "dgm": "diagram", "dt": "date", "ftr": "footer", "sldNum": "slide number", "hdr": "header",
    "sldImg": "slide image",
}
_HEX = re.compile(r"[0-9A-F]{6}")


def parse_slides(spec: Any, count: int) -> list[int] | None:
    """The 0-based slide indexes ``spec`` names ("3", "2-5", "0,4", "-1"),
    or None for every slide; a malformed spec raises ValueError with the
    accepted forms."""
    if spec is None or str(spec).strip() == "":
        return None
    wanted: list[int] = []
    for token in str(spec).split(","):
        token = token.strip()
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if match:
            first, last = sorted((int(match.group(1)), int(match.group(2))))
            # Bounded by the deck before it is expanded.
            wanted.extend(range(min(first, count), min(last, count - 1) + 1))
        elif re.fullmatch(r"-?\d+", token):
            index = int(token)
            wanted.append(count + index if index < 0 else index)
        else:
            raise ValueError(f"slides '{spec}' is not a slide list: use slide indexes from 0 such as "
                             "'3', '2-5', '0,4' or '-1' for the last")
    seen: dict[int, None] = {}
    for index in wanted:
        if 0 <= index < count:
            seen.setdefault(index)
    return list(seen)


def _inch(emu: int) -> str:
    return f"{inches(emu):.2f}"


def _placeholder(shape: Shape) -> str:
    if shape.placeholder is None:
        return ""
    ph_type, idx = shape.placeholder
    word = _PLACEHOLDER_WORDS.get(ph_type) or escaped(ph_type, NAME_CAP)
    return f" [{word}]" if idx is None else f" [{word} {idx}]"


def _where(shape: Shape) -> str:
    if shape.box is None:
        return ""
    box = shape.box
    text = f" {_inch(box.x)},{_inch(box.y)} in {_inch(box.cx)}x{_inch(box.cy)} in"
    if box.rot:
        text += f" rot {box.rot:g}°"
    return text


def _label(shape: Shape) -> str:
    head = f"shape_index {shape.index}, " if shape.index is not None else ""
    kind = shape.kind if shape.placeholder is None else "placeholder"
    if shape.kind in (KIND_CHART, KIND_TABLE, KIND_GROUP):
        kind = shape.kind
    line = f"{head}id {shape.shape_id} '{escaped(shape.name, NAME_CAP)}' {kind}{_placeholder(shape)}"
    if shape.kind == KIND_CHART and shape.chart is not None:
        chart = shape.chart
        points = max((len(values) for _, values in chart.series), default=0)
        line += f" {chart.type}"
        if chart.title:
            line += f" \"{escaped(chart.title, NAME_CAP)}\""
        line += f", {len(chart.series)} series x {points} points"
    if shape.kind == KIND_TABLE and shape.table is not None:
        cols = max((len(r) for r in shape.table), default=0)
        line += f" {len(shape.table)}x{cols}"
    line += _where(shape)
    if shape.hidden:
        line += " hidden"
    return line


def _paragraph_line(p: Paragraph, indent: str) -> str:
    mark = "- " if p.bullet and not p.bullet.startswith("autonum") else "1. " if p.bullet else ""
    return f"{indent}{'  ' * p.level}{mark}{escaped(p.text)}"


def _run_detail(p: Paragraph, indent: str) -> list[str]:
    lines = []
    for run in p.runs:
        if run.text == "\n":
            continue
        style = [f"{escaped(run.font, NAME_CAP) or '?'} {run.size_pt:g}pt"]
        if run.bold:
            style.append("bold")
        if run.italic:
            style.append("italic")
        if run.color:
            style.append(f"#{run.color}" if _HEX.fullmatch(run.color) else escaped(run.color, NAME_CAP))
        if run.lang:
            style.append(escaped(run.lang, NAME_CAP))
        if run.field:
            style.append(f"field {escaped(run.field, NAME_CAP)}")
        lines.append(f"{indent}  · {escaped(run.text, NAME_CAP)} ({', '.join(style)})")
    return lines


def _text_lines(shape: Shape, indent: str, detail: str) -> list[str]:
    lines: list[str] = []
    budget = SHAPE_TEXT_CAP
    left_out = 0
    for p in shape.paragraphs:
        if not p.text.strip():
            continue
        if budget <= 0:
            left_out += len(p.text)
            continue
        line = _paragraph_line(p, indent)
        if len(p.text) > budget:
            left_out += len(p.text) - budget
            line = _paragraph_line(Paragraph(p.level, [], bullet=p.bullet), indent) + escaped(p.text[:budget]) + "…"
        budget -= len(p.text)
        lines.append(line)
        if detail == DETAIL_TEXT:
            lines.extend(_run_detail(p, indent))
    if left_out:
        lines.append(f"{indent}… (+{left_out} characters)")
    return lines


def _table_lines(shape: Shape, indent: str) -> list[str]:
    rows = shape.table or []
    lines = []
    for row in rows[:TABLE_ROWS]:
        cells = [escaped(c, CELL_CAP) for c in row[:TABLE_COLS]]
        more = " …" if len(row) > TABLE_COLS else ""
        lines.append(f"{indent}| {' | '.join(cells)} |{more}")
    if len(rows) > TABLE_ROWS:
        lines.append(f"{indent}… (+{len(rows) - TABLE_ROWS} rows)")
    return lines


def _chart_lines(shape: Shape, indent: str) -> list[str]:
    chart = shape.chart
    if chart is None:
        return []
    lines = []
    if chart.categories:
        shown = ", ".join(escaped(c, CELL_CAP) for c in chart.categories[:CHART_VALUES])
        more = ", …" if len(chart.categories) > CHART_VALUES else ""
        lines.append(f"{indent}categories: {shown}{more}")
    for name, values in chart.series:
        shown = ", ".join("" if v is None else f"{v:g}" if isinstance(v, (int, float)) else escaped(str(v), CELL_CAP)
                          for v in values[:CHART_VALUES])
        more = ", …" if len(values) > CHART_VALUES else ""
        lines.append(f"{indent}series '{escaped(name, NAME_CAP)}': {shown}{more}")
    return lines


def _shape_lines(shape: Shape, indent: str, detail: str) -> list[str]:
    lines = [f"{indent}{_label(shape)}"]
    inner = indent + "  "
    if shape.kind == KIND_TABLE:
        lines.extend(_table_lines(shape, inner))
    elif shape.kind == KIND_CHART:
        lines.extend(_chart_lines(shape, inner))
    else:
        lines.extend(_text_lines(shape, inner, detail))
    for child in shape.children:
        lines.extend(_shape_lines(child, inner, detail))
    return lines


def _title(slide: Any) -> str:
    for shape in slide.walk():
        if shape.placeholder is not None and shape.placeholder[0] in TITLE_TYPES and shape.text.strip():
            return shape.text
    return ""


def _slide_lines(slide: Slide, detail: str) -> list[str]:
    layout = f"layout {slide.layout_index} " if slide.layout_index is not None else "layout "
    head = f"--- slide_index {slide.index} ({layout}'{escaped(slide.layout_name, NAME_CAP)}')"
    if slide.hidden:
        head += " hidden"
    if slide.transition:
        head += f", transition {slide.transition}"
    if slide.animations:
        head += f", {slide.animations} animation{'' if slide.animations == 1 else 's'}"
    lines = [head + " ---"]
    if detail == DETAIL_OUTLINE:
        lines.append(f"  {escaped(_title(slide), NAME_CAP) or '(no title)'}")
        return lines
    for shape in slide.shapes[:SHAPES_PER_SLIDE]:
        lines.extend(_shape_lines(shape, "  ", detail))
    if len(slide.shapes) > SHAPES_PER_SLIDE:
        lines.append(f"  … (+{len(slide.shapes) - SHAPES_PER_SLIDE} more shapes not shown)")
    if slide.wrapped:
        lines.append(f"  (+{slide.wrapped} objects in compatibility wrappers not shown: equations, 3D models "
                     "or ink PowerPoint wraps for older versions)")
    if slide.notes.strip():
        lines.append(f"  Notes: {escaped(slide.notes.strip(), NOTES_CAP)}")
    return lines


def render(width: int, height: int, slides: Iterable[Slide], *, name: str, total: int, wanted: list[int],
           layout_lines: list[str], title: str = "", author: str = "", detail: str = DETAIL_SHAPES,
           partial: bool = False, budget: int = READ_BUDGET) -> str:
    """The read's text. ``slides`` is drawn lazily: the read stops at
    ``budget`` characters and names the slides left to read."""
    out = [f"**PPTX**: {escaped(name, NAME_CAP)} — {total} slide(s), {round(inches(width), 2):g}x"
           f"{round(inches(height), 2):g}in"]
    meta = []
    if title:
        meta.append(f"Title: {escaped(title, NAME_CAP)}")
    if author:
        meta.append(f"Author: {escaped(author, NAME_CAP)}")
    if meta:
        out.append("**Properties**: " + " | ".join(meta))
    out.extend(layout_lines)
    out.append("Slides are slide_index from 0 and shapes shape_index from 0, as write_pptx takes them; "
               "id is the shape's id, which stays when shapes move.")
    if partial:
        out.append(f"Showing {len(wanted)} of {total} slides.")
    out.append("")
    size = sum(len(line) + 1 for line in out)
    shown = 0
    for slide in slides:
        block = [*_slide_lines(slide, detail), ""]
        block_size = sum(len(line) + 1 for line in block)
        if shown and size + block_size > budget:
            rest = wanted[shown:]
            out.append(f"The read stops here at {budget:,} characters, after {shown} of {len(wanted)} slides. "
                       f"Read the rest with slides: \"{rest[0]}-{rest[-1]}\" (or detail: outline for "
                       "every slide's title).")
            break
        out.extend(block)
        size += block_size
        shown += 1
    return "\n".join(out).rstrip("\n") + "\n"


def read_deck(path: str, *, slides: Any = None, detail: str | None = None) -> str:
    detail = (detail or DETAIL_SHAPES).strip().casefold()
    if detail not in DETAILS:
        raise ValueError(f"detail '{detail}' is not one of {', '.join(DETAILS)}")
    prs = open_presentation(path)
    total = len(prs.slides)
    wanted = parse_slides(slides, total)
    indexes = list(range(total)) if wanted is None else wanted
    title = author = ""
    try:
        title, author = prs.core_properties.title or "", prs.core_properties.author or ""
    except Exception:  # noqa: BLE001  # python-pptx on a user's deck raises anything from its parser
        title = author = ""
    try:
        layout_lines = describe_layouts(prs)
    except Exception:  # noqa: BLE001  # python-pptx on a user's deck raises anything from its parser
        layout_lines = []
    return render(prs.slide_width or 0, prs.slide_height or 0, iter_slides(prs, indexes), name=Path(path).name,
                  total=total, wanted=indexes, layout_lines=layout_lines, title=title, author=author,
                  detail=detail, partial=wanted is not None)
