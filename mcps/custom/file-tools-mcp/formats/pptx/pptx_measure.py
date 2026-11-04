"""How much room a text frame's text needs, measured from the widths of the
fonts the render engine draws, and whether it fits its box.

Lines are filled word by word from per-glyph advance widths, a single line
that must not wrap gets a widening margin, and 5 % over is a warning, more an
error: the method of ppt-master's text measurement
(https://github.com/hugohe3/ppt-master), written here on python-pptx
geometry and the advance widths of the fonts LibreOffice draws (Greek
included), with LibreOffice's line height for PowerPoint files.

LibreOffice lays a PowerPoint file's lines out as PowerPoint does, at 1.2
times the font size per line whatever the font (measured in the file-tools
image across seven families, four sizes and three spacings), so the line
height needs no font metrics; the widths come from ``fonts.advance_width``.
All lengths here are points.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from formats.pptx.pptx_geometry import (
    AUTOFIT_RESIZE,
    AUTOFIT_SHRINK,
    EMU_PER_PT,
    LINE_PT,
    Paragraph,
    Run,
    Shape,
)

LINE_FACTOR = 1.2
TAB_PT = 72.0
# A line that comes within this of its room's edge wraps in LibreOffice
# (its own rounding of glyph positions).
WRAP_SLACK_PT = 0.5
WARN_OVER = 0.05
# A single line that must not wrap is widened before it is compared with its
# box: the widths here leave out kerning and LibreOffice wraps a line that
# is too long for a box with wrap off.
SANS_HEADROOM = (1.06, 1.12)    # mixed case, all capitals
SERIF_HEADROOM = (1.12, 1.36)
_SERIF_HINTS = ("serif", "times", "cambria", "caladea", "georgia", "garamond", "book antiqua", "palatino")
# PowerPoint's shrink-on-overflow steps and the line reduction it pairs them with.
SCALES = (1.0, 0.925, 0.85, 0.775, 0.7, 0.625, 0.55, 0.475, 0.4, 0.325, 0.25)
SHRINK_WARN_BELOW = 0.55
# The share of a box a preset's text area takes (presetShapeDefinitions'
# text rectangle); the others use the whole box.
PRESET_TEXT_AREA = {
    "diamond": (0.5, 0.5), "triangle": (0.5, 0.5), "rtTriangle": (0.5, 0.333),
    "ellipse": (0.7071, 0.7071), "hexagon": (0.7071, 0.7071), "octagon": (0.7071, 0.7071),
    "teardrop": (0.7071, 0.7071), "pentagon": (0.6, 0.667), "cloud": (0.6, 0.55),
    "flowChartDecision": (0.5, 0.5), "flowChartConnector": (0.7071, 0.7071), "cloudCallout": (0.6, 0.55),
    "wedgeEllipseCallout": (0.7071, 0.7071), "star4": (0.4, 0.4), "star5": (0.5, 0.5), "star6": (0.6, 0.6),
    "heart": (0.6, 0.6), "rightArrow": (0.75, 0.5), "leftArrow": (0.75, 0.5), "upArrow": (0.5, 0.75),
    "downArrow": (0.5, 0.75), "leftRightArrow": (0.6, 0.5), "donut": (0.5, 0.5), "can": (1.0, 0.75),
}

Widths = Callable[[str, str, float, bool, bool], float]


@lru_cache(maxsize=4096)
def _font_width(text: str, family: str, size_pt: float, bold: bool, italic: bool) -> float:
    import fonts

    return fonts.advance_width(text, family, size_pt, bold=bold, italic=italic)


@dataclass
class Measure:
    """What a frame's text needs: its lines and height, and the room it has."""
    lines: int
    needed_pt: float          # the text's height, paragraph spacing included, insets excluded
    room_w_pt: float
    room_h_pt: float
    widest_pt: float          # the widest line as laid out
    scale: float = 1.0

    @property
    def over(self) -> float:
        """How far past its room the text runs (0.29 = 29 % over); 0 when it fits."""
        if self.room_h_pt <= 0:
            return math.inf if self.needed_pt > 0 else 0.0
        return max(self.needed_pt / self.room_h_pt - 1.0, 0.0)


def _is_cjk(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")


def _serif(family: str) -> bool:
    name = family.casefold()
    return any(hint in name for hint in _SERIF_HINTS) and "sans" not in name


def room(shape: Shape) -> tuple[float, float]:
    """(width, height) in points the shape's text lays out in: its box less
    the insets, inside a preset's text area, turned for vertical text."""
    if shape.box is None or shape.frame is None:
        return 0.0, 0.0
    left, top, right, bottom = shape.frame.insets
    fw, fh = PRESET_TEXT_AREA.get(shape.preset or "", (1.0, 1.0))
    width = (shape.box.cx * fw - left - right) / EMU_PER_PT
    height = (shape.box.cy * fh - top - bottom) / EMU_PER_PT
    if shape.frame.vertical not in ("horz", ""):
        width, height = height, width
    columns = max(shape.frame.columns, 1)
    if columns > 1:
        width = (width - shape.frame.column_gap / EMU_PER_PT * (columns - 1)) / columns
        height *= columns
    return width, height


@dataclass
class _Piece:
    """A word (with what follows it) as one unit of a line."""
    width: float              # without its trailing spaces
    tail: float               # its trailing spaces
    size: float               # the largest run size in it
    chars: list[tuple[str, float, float]]   # (character, width, size) to split a word too long for a line
    forced_break: bool = False


def _run_text(run: Run) -> str:
    if run.caps == "all":
        return run.text.upper()
    return run.text


def _char_widths(run: Run, scale: float, widths: Widths) -> list[tuple[str, float, float]]:
    size = run.size_pt * scale
    spacing = run.spacing_pt * scale
    out = []
    for char in _run_text(run):
        if char == "\t":
            out.append((char, 0.0, size))
            continue
        shown, char_size = char, size
        if run.caps == "small" and char.islower():
            shown, char_size = char.upper(), size * 0.8
        out.append((char, widths(shown, run.font, char_size, run.bold, run.italic) + spacing, size))
    return out


def _pieces(paragraph: Paragraph, scale: float, widths: Widths) -> list[_Piece]:
    """The paragraph cut where a line may break: after spaces, hyphens and
    dashes, around each CJK character, and at a line break."""
    pieces: list[_Piece] = []
    current: list[tuple[str, float, float]] = []
    tail: list[tuple[str, float, float]] = []

    def close(forced: bool = False) -> None:
        nonlocal current, tail
        if current or tail or forced:
            chars = current + tail
            pieces.append(_Piece(sum(w for _, w, _ in current), sum(w for _, w, _ in tail),
                                 max((s for _, _, s in chars), default=0.0), chars, forced))
        current, tail = [], []

    for run in paragraph.runs:
        if run.text == "\n":
            close(forced=True)
            continue
        for char, width, size in _char_widths(run, scale, widths):
            if char in "\n\v":
                # A line feed or vertical tab in run text breaks the line as LibreOffice draws it.
                close(forced=True)
                continue
            if char == " ":
                tail.append((char, width, size))
                continue
            if tail:
                close()
            if _is_cjk(char):
                close()
                current.append((char, width, size))
                close()
                continue
            current.append((char, width, size))
            if char in "-–—/":
                close()
    close()
    return pieces


def _line_height(size: float, paragraph: Paragraph, line_reduction: float) -> float:
    kind, value = paragraph.line_spacing
    if kind == LINE_PT:
        return value
    # The shrink's line reduction is subtracted from the spacing (ECMA-376
    # normAutofit): 150 % less 20 % is 130 %.
    return LINE_FACTOR * size * max(value - line_reduction, 0.0)


_ROMAN = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
          (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def number_label(scheme: str, number: int) -> str:
    """The label an autonumber scheme (``arabicPeriod``, ``romanUcParenR``,
    ...) shows for ``number``."""
    if scheme.startswith("roman"):
        core, rest = "", number
        for value, letters in _ROMAN:
            count, rest = divmod(rest, value)
            core += letters * count
    elif scheme.startswith("alpha"):
        core = chr(ord("a") + (number - 1) % 26) * ((number - 1) // 26 + 1)
    else:
        core = str(number)
    if "Uc" in scheme:
        core = core.upper()
    if scheme.endswith("ParenBoth"):
        return f"({core})"
    if scheme.endswith("ParenR"):
        return f"{core})"
    return core if scheme.endswith("Plain") else f"{core}."


def _bullet_width(paragraph: Paragraph, scale: float, widths: Widths, number: int) -> float:
    if not paragraph.bullet or not paragraph.runs:
        return 0.0
    first = paragraph.runs[0]
    size = first.size_pt * scale
    if paragraph.bullet == "picture":
        return size
    label = paragraph.bullet
    if label.startswith("autonum:"):
        label = number_label(label.partition(":")[2], number)
    return widths(label, first.font, size, False, False)


def lay_out(paragraph: Paragraph, width: float, *, scale: float = 1.0, wrap: bool = True,
            line_reduction: float = 0.0, widths: Widths | None = None, number: int = 1) -> tuple[list[float], float]:
    """(each line's height, the widest line) of one paragraph laid out in
    ``width`` points; ``number`` is its place in a numbered list."""
    widths = widths or _font_width
    margin = paragraph.margin_left_emu / EMU_PER_PT
    indent = paragraph.indent_emu / EMU_PER_PT
    bullet = _bullet_width(paragraph, scale, widths, number)
    # The first line starts at the indent; a bullet hangs in a negative
    # indent, or pushes the first line's text along when it does not fit.
    first_start = margin + indent if bullet == 0 else max(margin, margin + indent + bullet)
    starts = (first_start, margin)
    heights: list[float] = []
    widest = 0.0
    line_w, line_size, line_index = 0.0, 0.0, 0

    def end_line() -> None:
        nonlocal line_w, line_size, line_index, widest
        size = line_size or paragraph.end_size_pt * scale
        heights.append(_line_height(size, paragraph, line_reduction))
        widest = max(widest, starts[min(line_index, 1)] + line_w)
        line_w, line_size, line_index = 0.0, 0.0, line_index + 1

    pieces = _pieces(paragraph, scale, widths)
    for piece in pieces:
        avail = max(width - starts[min(line_index, 1)] - WRAP_SLACK_PT, 1.0)
        if piece.forced_break and not piece.chars:
            end_line()
            continue
        tabbed = any(c == "\t" for c, _, _ in piece.chars)
        if tabbed:
            line_w = (math.floor(line_w / TAB_PT) + 1) * TAB_PT
        if wrap and line_w > 0 and line_w + piece.width > avail:
            end_line()
            avail = max(width - starts[1] - WRAP_SLACK_PT, 1.0)
        if wrap and piece.width > avail:
            # A word longer than a line breaks between its characters.
            for _, w, s in piece.chars:
                if line_w > 0 and line_w + w > avail:
                    end_line()
                    avail = max(width - starts[1] - WRAP_SLACK_PT, 1.0)
                line_w += w
                line_size = max(line_size, s)
        else:
            line_w += piece.width + piece.tail
            line_size = max(line_size, piece.size)
        if piece.forced_break:
            end_line()
    # The last line, or the empty line after a closing line break.
    if line_w > 0 or line_size or line_index == 0 or (pieces and pieces[-1].forced_break):
        end_line()
    return heights, widest


def measure(shape: Shape, *, scale: float | None = None, line_reduction: float | None = None,
            widths: Widths | None = None) -> Measure | None:
    """The shape's text measured at ``scale`` (default: the scale its
    shrink-on-overflow stores), or None for a shape with no text frame."""
    widths = widths or _font_width
    if shape.frame is None or shape.box is None:
        return None
    frame = shape.frame
    scale = frame.font_scale if scale is None else scale
    reduction = frame.line_reduction if line_reduction is None else line_reduction
    width, height = room(shape)
    lines = 0
    needed = 0.0
    widest = 0.0
    numbers = _list_numbers(shape.paragraphs)
    for n, paragraph in enumerate(shape.paragraphs):
        heights, line_widest = lay_out(paragraph, width, scale=scale, wrap=frame.wrap, line_reduction=reduction,
                                       widths=widths, number=numbers[n])
        lines += len(heights)
        widest = max(widest, line_widest)
        needed += sum(heights)
        if n:
            needed += paragraph.space_before_pt * scale
        if n < len(shape.paragraphs) - 1:
            needed += paragraph.space_after_pt * scale
    return Measure(lines, needed, width, height, widest, scale)


def _list_numbers(paragraphs: list[Paragraph]) -> list[int]:
    """Each paragraph's number in its numbered list: numbered paragraphs at
    one level count on, past deeper ones; another paragraph at that level
    or above starts the count again."""
    counts: dict[int, int] = {}
    numbers = []
    for paragraph in paragraphs:
        for level in [lv for lv in counts if lv > paragraph.level]:
            del counts[level]
        if paragraph.bullet and paragraph.bullet.startswith("autonum:"):
            counts[paragraph.level] = counts.get(paragraph.level, paragraph.start_at - 1) + 1
            numbers.append(counts[paragraph.level])
        else:
            counts.pop(paragraph.level, None)
            numbers.append(1)
    return numbers


def has_text(shape: Shape) -> bool:
    return any(p.text.strip() for p in shape.paragraphs)


def headroom(shape: Shape) -> float:
    """The widening a single line gets before it is compared with its box."""
    runs = [r for p in shape.paragraphs for r in p.runs if r.text.strip()]
    if not runs:
        return 1.0
    letters = [c for r in runs for c in _run_text(r) if c.isalpha()]
    caps = sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0
    low, high = SERIF_HEADROOM if any(_serif(r.font) for r in runs) else SANS_HEADROOM
    return low + (high - low) * caps


def fit_scale(shape: Shape, *, widths: Widths | None = None) -> tuple[float, float, bool]:
    """(font scale, line reduction, fits) PowerPoint's shrink-on-overflow
    would show the text at: the largest of its steps at which it fits."""
    widths = widths or _font_width
    for scale in SCALES:
        reduction = 0.0 if scale == 1.0 else 0.1 if scale > 0.9 else 0.2
        found = measure(shape, scale=scale, line_reduction=reduction, widths=widths)
        if found is not None and found.over == 0:
            return scale, reduction, True
    return SCALES[-1], 0.2, False


def size_that_fits(shape: Shape, *, widths: Widths | None = None) -> float | None:
    """The largest font size (points, by half points) at which every run
    set to it fits the box, or None when none from 6 pt does."""
    widths = widths or _font_width
    sizes = [r.size_pt for p in shape.paragraphs for r in p.runs if r.text.strip()]
    if not sizes:
        return None
    top = max(sizes)
    low, high = 6.0 / top, 1.0
    found = measure(shape, scale=low, line_reduction=0.0, widths=widths)
    if found is None or found.over > 0:
        return None
    for _ in range(14):
        mid = (low + high) / 2
        result = measure(shape, scale=mid, line_reduction=0.0, widths=widths)
        if result is not None and result.over == 0:
            low = mid
        else:
            high = mid
    return math.floor(top * low * 2) / 2


def needs_room(shape: Shape) -> bool:
    """Whether the text's size is the box's business: a box that grows to
    its text never overflows it."""
    return shape.frame is not None and shape.frame.autofit != AUTOFIT_RESIZE


def stores_shrink(shape: Shape) -> bool:
    return shape.frame is not None and shape.frame.autofit == AUTOFIT_SHRINK
