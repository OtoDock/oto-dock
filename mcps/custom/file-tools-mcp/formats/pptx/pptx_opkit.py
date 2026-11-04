"""What every write_pptx operation shares: the job, the helpers that find a
slide or a shape and read lengths and colours, the help's groups, and the
keys several operations declare.
"""

from dataclasses import dataclass, field
from typing import Any

import colors
import units
from catalog import KeySpec
from formats.pptx import pptx_text
from formats.pptx.pptx_escape import NAME_CAP, escaped
from results import Code, OpCtx, OpError

IN = "in"
PT = units.POINTS

# Groups of the help and of the tool description.
G_SLIDES = "SLIDES"
G_TEXT = "TEXT"
G_SHAPES = "SHAPES"
G_TABLES = "TABLES"
G_CHARTS = "CHARTS"
G_IMAGES = "IMAGES / EQUATIONS"
G_VISUAL = "VISUAL"
G_NOTES = "NOTES / LINKS / NUMBERS"
G_XML = "XML"

ALIGNMENTS = ("left", "center", "right", "justify")
ANCHORS = ("top", "middle", "bottom")

# The dark and light slots as PowerPoint's colour picker writes them (tx1,
# bg1, ...), so they follow a slide's colour map.
THEME_COLORS = {
    "dk1": "TEXT_1", "lt1": "BACKGROUND_1", "dk2": "TEXT_2", "lt2": "BACKGROUND_2", "accent1": "ACCENT_1",
    "accent2": "ACCENT_2", "accent3": "ACCENT_3", "accent4": "ACCENT_4", "accent5": "ACCENT_5",
    "accent6": "ACCENT_6", "hlink": "HYPERLINK", "folHlink": "FOLLOWED_HYPERLINK",
}

LIST_SHAPES = 12


def choice(value: str, choices: tuple[str, ...], what: str) -> str:
    """``value`` folded to one of ``choices``, or BAD_VALUE naming them."""
    import difflib

    folded = value.strip().casefold().replace(" ", "_").replace("-", "_")
    if folded in choices:
        return folded
    close = difflib.get_close_matches(folded, choices, n=1, cutoff=0.6)
    hint = (f"Did you mean '{close[0]}'? " if close else "") + f"Use one of: {', '.join(choices)}."
    raise OpError(Code.BAD_VALUE, f"unknown {what} '{value}'", hint=hint)


@dataclass
class PptxJob:
    """What one write_pptx call's operations share."""
    prs: Any
    path: str = ""
    renumber: bool = False   # the slide order changed: stale slide-number caches are rewritten
    # The shapes whose text this batch wrote, by slide part: a shrink-on-
    # overflow among them gets the scale its text needs.
    touched: dict[Any, set[int]] = field(default_factory=dict)

    def touch(self, slide: Any, shape: Any) -> None:
        self.touched.setdefault(slide.part, set()).add(shape.shape_id)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def slide_of(job: PptxJob, op: dict, default: int | None = None) -> tuple[Any, int]:
    """The slide ``slide_index`` names (negative counts from the end) and
    its position."""
    index = op.get("slide_index", default)
    count = len(job.prs.slides)
    position = index + count if index < 0 else index
    if not 0 <= position < count:
        hint = ("Add a slide first with add_slide." if count == 0 else
                f"Use 0 to {count - 1}, or -1 for the last slide.")
        raise OpError(Code.NOT_FOUND, f"slide_index {index} does not exist (the deck has {plural(count, 'slide')})",
                      location=f"slide_index {index}", hint=hint)
    return job.prs.slides[position], position


def from_end(index: int, count: int) -> int:
    """An index from 0, or from the end when negative (-1 = last), as Python
    lists read it."""
    return index + count if index < 0 else index


def name_of(text: str) -> str:
    """A name from the deck, safe on a line of the result."""
    return f"'{escaped(text, NAME_CAP)}'"


def shape_list(slide: Any) -> str:
    shown = [f"{i} id {s.shape_id} {name_of(s.name)}" for i, s in enumerate(slide.shapes)][:LIST_SHAPES]
    more = len(slide.shapes) - len(shown)
    return ", ".join(shown) + (f", +{more} more" if more > 0 else "") if shown else "none"


def walk_shapes(shapes: Any) -> Any:
    for shape in shapes:
        yield shape
        if shape.shape_type is not None and getattr(shape, "shapes", None) is not None:
            yield from walk_shapes(shape.shapes)


def shape_of(slide: Any, op: dict, position: int) -> Any:
    """The shape ``shape_id``, ``shape_name`` (a name, then a whole text) or
    ``shape_index`` names on the slide."""
    where = f"slide_index {position}"
    hint = f"Shapes on this slide (shape_index, id, name): {shape_list(slide)}."
    if op.get("shape_id") is not None:
        shape_id = op["shape_id"]
        found = next((s for s in walk_shapes(slide.shapes) if s.shape_id == shape_id), None)
        if found is None:
            raise OpError(Code.NOT_FOUND, f"no shape has id {shape_id}", location=f"{where}, shape_id {shape_id}",
                          hint=hint)
        return found
    name = op.get("shape_name")
    if name:
        for shape in slide.shapes:
            if shape.name == name:
                return shape
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text == name:
                return shape
        raise OpError(Code.NOT_FOUND, f"no shape is named '{name}' or holds that text",
                      location=f"{where}, shape_name '{name}'", hint=hint)
    index = op.get("shape_index")
    shapes = list(slide.shapes)
    if index is None or not 0 <= index < len(shapes):
        raise OpError(Code.NOT_FOUND, f"shape_index {index} does not exist (the slide has "
                      f"{plural(len(shapes), 'shape')})", location=f"{where}, shape_index {index}", hint=hint)
    return shapes[index]


def emu(op: dict, key: str, default: Any, unit: str = IN) -> int:
    return units.to_emu(op.get(key, default), default=unit)


def set_color(fmt: Any, value: Any, ctx: OpCtx) -> None:
    """Point a python-pptx colour at ``value``: an RGB colour, or a theme
    slot (with a tint or shade as its brightness)."""
    from pptx.dml.color import RGBColor
    from pptx.enum.dml import MSO_THEME_COLOR

    color = colors.parse_color(value)
    if color.theme is not None:
        fmt.theme_color = getattr(MSO_THEME_COLOR, THEME_COLORS[color.theme])
        if color.tint:
            fmt.brightness = color.tint
        elif color.shade:
            fmt.brightness = -color.shade
        return
    fmt.rgb = RGBColor(*color.rgb)
    if color.alpha is not None and color.alpha < 1:
        ctx.warn(f"the colour {value} is drawn opaque: transparency is not supported here yet")


def paragraph_align(value: str) -> Any:
    from pptx.enum.text import PP_ALIGN

    return getattr(PP_ALIGN, choice(value, ALIGNMENTS, "alignment").upper())


def apply_run_format(run: Any, fmt: dict, ctx: OpCtx) -> None:
    """A run's font keys (font_size, bold, italic, underline, color,
    font_name)."""
    from pptx.util import Pt

    if fmt.get("font_size"):
        if not 1 <= fmt["font_size"] <= 4000:
            raise OpError(Code.BAD_VALUE, f"font_size {fmt['font_size']:g} is out of range", hint="Use 1 to 4000 pt.")
        run.font.size = Pt(fmt["font_size"])
    if fmt.get("bold") is not None:
        run.font.bold = fmt["bold"]
    if fmt.get("italic") is not None:
        run.font.italic = fmt["italic"]
    if fmt.get("underline") is not None:
        run.font.underline = fmt["underline"]
    if fmt.get("color") is not None:
        set_color(run.font.color, fmt["color"], ctx)
    if fmt.get("font_name"):
        run.font.name = fmt["font_name"]
    pptx_text.format_run(run, fmt)


def line_width(line: Any, value: Any) -> None:
    """A line's width in points; 0 removes the line."""
    from pptx.util import Pt

    width = units.to_pt(value, default=PT)
    if width < 0 or width > 1584:
        raise OpError(Code.BAD_VALUE, f"line_width {value} is out of range", hint="Use 0 (no line) to 1584 pt.")
    if width == 0:
        line.fill.background()
    else:
        line.width = Pt(width)


def fill_solid(fill: Any, value: Any, ctx: OpCtx) -> None:
    fill.solid()
    set_color(fill.fore_color, value, ctx)


def slide_key(required: bool = True, default: Any = None) -> KeySpec:
    if default is not None:
        return KeySpec("slide_index", ("int",), default=default, help="0-based, -1 = last")
    return KeySpec("slide_index", ("int",), required=required, help="0-based, -1 = last")


def box_keys(left: float | None, top: float | None, width: float | None, height: float | None) -> tuple[KeySpec, ...]:
    keys = []
    for name, default in (("left", left), ("top", top), ("width", width), ("height", height)):
        keys.append(KeySpec(name, ("length",), unit=IN) if default is None else
                    KeySpec(name, ("length",), unit=IN, default=default))
    return tuple(keys)


SHAPE_REF = (
    KeySpec("shape_id", ("int",), help="the id read_document shows; stays when shapes move"),
    KeySpec("shape_name", ("str",), help="its name, or its whole text"),
    KeySpec("shape_index", ("int",), help="0-based"),
)
SHAPE_ONE_OF = (("shape_id", "shape_name", "shape_index"),)

FONT = (
    KeySpec("font_size", ("float",), help="pt"),
    KeySpec("bold", ("bool",)),
    KeySpec("italic", ("bool",)),
    KeySpec("underline", ("bool",)),
    KeySpec("color", ("color",), aliases=("font_color", "text_color")),
    KeySpec("font_name", ("str",)),
    *pptx_text.RUN_KEYS,
)
TEXT_FRAME = (*pptx_text.PARAGRAPH_KEYS, *pptx_text.FRAME_KEYS)
RUNS = KeySpec("runs", ("list",), item=(KeySpec("text", ("str",), default=""), *FONT),
                help="[{text, font keys}] for mixed formatting")
TEXT = KeySpec("text", ("str",), default="")
FILL = KeySpec("fill_color", ("color",))
LINE = (KeySpec("line_color", ("color",)), KeySpec("line_width", ("length",), unit=PT, help="0 = no line"))

EXAMPLE_PNG = "logo.png"
