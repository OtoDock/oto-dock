"""Checks of a PowerPoint deck's slides: text that does not fit its box (or
wraps where it must not), shapes past the slide's edge, text covered by
other shapes or by other text, text too faint against what is behind it,
text too small to read on a shown slide, and, for a finished deck,
leftover template text, empty placeholders and stale slide numbers.

The text is measured on the fonts the render engine draws
(``pptx_measure``); covering and contrast are judged on the area the text
takes, not on its whole box. Locations are the addresses write_pptx takes;
document text goes only into a finding's excerpt.
"""

from __future__ import annotations

import math
import re
import weakref
from collections.abc import Iterator
from typing import Any

import checks

OVERFLOW = "overflow"
WRAP_TRAP = "wrap_trap"
OFF_SLIDE = "off_slide"
OVERLAP = "overlap"
CONTRAST = "contrast"
SMALL_TEXT = "small_text"
LEFTOVER = "leftover"
SLIDE_NUMBER = "slide_number"

EMU_PER_IN = 914400
EMU_PER_CM = 360000
EMU_PER_PT = 12700
# Past the edge by more than this, a shape is cut off where the slide ends.
EDGE_SLACK = int(0.5 * EMU_PER_CM)
OVERLAP_SLACK = EMU_PER_PT
COVERED_SHARE = 0.2
# A fill this transparent or more shows what is under it.
SEE_THROUGH = 0.5
CONTRAST_TEXT = 4.5
CONTRAST_LARGE = 3.0
# WCAG's large text: 18 pt, or 14 pt bold.
LARGE_PT, LARGE_BOLD_PT = 18.0, 14.0
# Where text sits on a gradient, its colour is read at this many steps.
GRADIENT_SAMPLES = 8
EXCERPT_CAP = 60
_LEFTOVER_MARKERS = ("TO" + "DO", "T" + "BD", "X" + "XX")
_TOKENS = re.compile(
    r"\{\{[^{}\n]{1,80}\}\}|\[\[[^\[\]\n]{1,80}\]\]|<<[^<>\n]{1,80}>>|\blorem ipsum\b"
    r"|\bClick to (?:add|edit) [a-z ]{1,30}\b|\b(?:" + "|".join(_LEFTOVER_MARKERS) + r")\b",
    re.IGNORECASE)
_UPPER_ONLY = frozenset(m.lower() for m in _LEFTOVER_MARKERS)
# Dates, footers and slide numbers are set muted on purpose.
_MUTED_PLACEHOLDERS = frozenset({"dt", "ftr", "sldNum"})
_TEXT_PLACEHOLDERS = frozenset({"title", "ctrTitle", "subTitle", "body", "obj"})
_TEXT_SHAPES = frozenset({"textbox", "shape"})
# Below these, text is hard to read on a shown slide.
SMALL_WARN_PT, SMALL_NOTE_PT = 10.0, 12.0
# A one-line note this low on the slide (a source, a footnote) may be small.
FOOTNOTE_BAND = 0.85
_OPAQUE_WORDS = frozenset({"gradient", "picture", "pattern"})
_HEX = re.compile(r"[0-9A-F]{6}")

_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------------------
# The deck, measured once per check run
# ---------------------------------------------------------------------------


class _Deck:
    def __init__(self, ctx: checks.CheckContext) -> None:
        from formats.pptx import pptx_deck, pptx_geometry

        ctx.zip()   # the package's size caps, before python-pptx reads it
        prs = ctx.doc if ctx.doc is not None else pptx_deck.open_presentation(ctx.open())
        self.width = prs.slide_width or 0
        self.height = prs.slide_height or 0
        self.first_number = _first_number(prs)
        self.slides = list(pptx_geometry.iter_slides(prs))
        self.parts = [str(slide.part.partname).lstrip("/") for slide in prs.slides]
        self._elements = [slide._element for slide in prs.slides]
        self._measures: dict[int, Any] = {}

    def element(self, slide: Any, shape: Any) -> Any:
        """A shape's XML element on its slide, found by its id."""
        for node in self._elements[slide.index].iter():
            if isinstance(node.tag, str) and node.tag.endswith("}cNvPr") and node.get("id") == str(shape.shape_id):
                return node.getparent().getparent()
        return None

    def measure(self, shape: Any) -> Any:
        from formats.pptx import pptx_measure

        key = id(shape)
        if key not in self._measures:
            self._measures[key] = pptx_measure.measure(shape)
        return self._measures[key]


def _first_number(prs: Any) -> int:
    try:
        return int(prs.part._element.get("firstSlideNum", "1"))
    except ValueError:
        return 1


def _deck(ctx: checks.CheckContext) -> _Deck:
    deck = _CACHE.get(ctx)
    if deck is None:
        deck = _CACHE[ctx] = _Deck(ctx)
    return deck


def _leaves(slide: Any) -> Iterator[Any]:
    for shape in slide.walk():
        if shape.kind != "group" and not shape.hidden:
            yield shape


def _where(slide: Any, shape: Any) -> str:
    return f"slide_index {slide.index}, shape_id {shape.shape_id} '{shape.name}'"


def _inches(emu: float) -> str:
    return f"{emu / EMU_PER_IN:.2f} in"


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT_CAP else text[:EXCERPT_CAP] + "…"


def _finding(deck: _Deck, slide: Any, shape: Any, severity: checks.Severity, kind: str, message: str, fix: str,
             *, excerpt: str = "", key: str = "") -> checks.Finding:
    return checks.Finding(severity, "", location=_where(slide, shape), message=message, fix=fix, excerpt=excerpt,
                          part=deck.parts[slide.index], key=f"{kind} {shape.shape_id} {key}".strip())


# ---------------------------------------------------------------------------
# Where text sits
# ---------------------------------------------------------------------------


def _text_extent(deck: _Deck, shape: Any) -> tuple[float, float, float, float] | None:
    """The area the shape's text covers (EMU): its laid-out width and height
    placed in the box by the first paragraph's alignment and the frame's
    anchor."""
    from formats.pptx import pptx_measure

    if shape.box is None or shape.frame is None or not pptx_measure.has_text(shape):
        return None
    found = deck.measure(shape)
    if found is None:
        return None
    left, top, right, bottom = shape.frame.insets
    box = shape.box
    width = min(found.widest_pt * EMU_PER_PT, max(box.cx - left - right, 0))
    height = found.needed_pt * EMU_PER_PT
    align = shape.paragraphs[0].align if shape.paragraphs else "left"
    x0 = box.x + left
    if align == "center":
        x0 = box.x + left + (box.cx - left - right - width) / 2
    elif align == "right":
        x0 = box.x + box.cx - right - width
    anchor = shape.frame.anchor
    y0 = box.y + top
    if anchor == "ctr":
        y0 = box.y + top + (box.cy - top - bottom - height) / 2
    elif anchor == "b":
        y0 = box.y + box.cy - bottom - height
    return x0, y0, x0 + width, y0 + height


def _area(rect: tuple[float, float, float, float]) -> float:
    return max(rect[2] - rect[0], 0) * max(rect[3] - rect[1], 0)


def _intersection(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, ...]:
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


def _rect(box: Any) -> tuple[float, float, float, float]:
    """A box's bounds on the slide, its rotation included."""
    if not box.rot % 180:
        return box.x, box.y, box.x + box.cx, box.y + box.cy
    turn = math.radians(box.rot)
    half_w = (abs(box.cx * math.cos(turn)) + abs(box.cy * math.sin(turn))) / 2
    half_h = (abs(box.cx * math.sin(turn)) + abs(box.cy * math.cos(turn))) / 2
    mx, my = box.x + box.cx / 2, box.y + box.cy / 2
    return mx - half_w, my - half_h, mx + half_w, my + half_h


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def overflow(ctx: checks.CheckContext) -> list[checks.Finding]:
    from formats.pptx import pptx_measure

    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        for shape in _leaves(slide):
            if shape.frame is None or not pptx_measure.has_text(shape) or not pptx_measure.needs_room(shape):
                continue
            result = deck.measure(shape)
            if result is None or result.over == 0:
                continue
            sizes = [r.size_pt for p in shape.paragraphs for r in p.runs if r.text.strip()]
            top_size = max(sizes) * result.scale
            left, top, right, bottom = shape.frame.insets
            need_in = _inches(result.needed_pt * EMU_PER_PT + top + bottom)
            message = (f"{result.lines} lines at {top_size:g} pt need {_inches(result.needed_pt * EMU_PER_PT)}; "
                       f"the box holds {_inches(result.room_h_pt * EMU_PER_PT)} ({result.over:.0%} over)")
            fits = pptx_measure.size_that_fits(shape)
            fix = f"Enlarge the box to {need_in} high (set_shape_format height), shorten the text"
            fix += f", or set font_size {fits:g} (fits)" if fits else ", or split it across slides"
            if pptx_measure.stores_shrink(shape):
                # PowerPoint draws the stored scale until the text is edited;
                # Collabora shrinks it itself. A scale far short is a real
                # overflow in PowerPoint.
                message += (f"; PowerPoint shows it at {result.scale:.0%} until the text is edited, "
                            "Collabora shrinks it")
                severity = checks.Severity.INFO if result.over <= pptx_measure.WARN_OVER else checks.Severity.WARNING
                fix = "Write the text again with write_pptx (autofit: shrink) so the scale is stored. " + fix
            else:
                severity = checks.Severity.WARNING if result.over <= pptx_measure.WARN_OVER else checks.Severity.ERROR
                fix += ", or give it autofit: shrink"
            found.append(_finding(deck, slide, shape, severity, OVERFLOW, message, fix + ".",
                                  excerpt=_excerpt(shape.text)))
    return found


def wrap_trap(ctx: checks.CheckContext) -> list[checks.Finding]:
    from formats.pptx import pptx_measure

    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        for shape in _leaves(slide):
            if shape.frame is None or shape.frame.wrap or not pptx_measure.has_text(shape):
                continue
            result = deck.measure(shape)
            if result is None or shape.frame.autofit == "resize":
                continue
            needed = result.widest_pt * pptx_measure.headroom(shape)
            if needed <= result.room_w_pt:
                continue
            found.append(_finding(
                deck, slide, shape, checks.Severity.WARNING, WRAP_TRAP,
                f"a line that must not wrap needs {_inches(needed * EMU_PER_PT)}; the box is "
                f"{_inches(result.room_w_pt * EMU_PER_PT)} wide, and LibreOffice and Collabora wrap it",
                f"Widen the box to {_inches(needed * EMU_PER_PT)} or more, or turn word_wrap on.",
                excerpt=_excerpt(shape.text)))
    return found


def off_slide(ctx: checks.CheckContext) -> list[checks.Finding]:
    from formats.pptx import pptx_measure

    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        for shape in _leaves(slide):
            if shape.box is None:
                continue
            x0, y0, x1, y1 = _rect(shape.box)
            if shape.frame is not None and shape.frame.autofit == "resize" and pptx_measure.has_text(shape):
                result = deck.measure(shape)
                if result is not None:
                    grown = result.needed_pt * EMU_PER_PT + shape.frame.insets[1] + shape.frame.insets[3]
                    y1 = max(y1, y0 + grown)
            past = {"left": -x0, "top": -y0, "right": x1 - deck.width, "bottom": y1 - deck.height}
            side, amount = max(past.items(), key=lambda item: item[1])
            if amount <= EDGE_SLACK:
                continue
            outside = x1 <= 0 or y1 <= 0 or x0 >= deck.width or y0 >= deck.height
            texty = pptx_measure.has_text(shape)
            if outside:
                severity, message = checks.Severity.WARNING, "the shape lies wholly outside the slide and never shows"
                fix = "Move it onto the slide with set_shape_format left and top, or remove it."
            else:
                # A picture bleeding past the edge is often meant to.
                severity = (checks.Severity.ERROR if texty else
                            checks.Severity.INFO if shape.kind == "picture" else checks.Severity.WARNING)
                message = f"the shape runs {amount / EMU_PER_CM:.1f} cm past the slide's {side} edge, so it is cut off"
                fix = "Move or shrink it with set_shape_format (left, top, width, height) to stay on the slide."
            found.append(_finding(deck, slide, shape, severity, OFF_SLIDE, message, fix, key=side))
    return found


def _opaque(shape: Any) -> bool:
    """Whether the shape hides what is under it: a picture, or a fill at
    least half opaque."""
    if shape.kind == "picture":
        return True
    fill = shape.fill or ""
    return (bool(_HEX.fullmatch(fill)) or fill in _OPAQUE_WORDS) and shape.fill_alpha > SEE_THROUGH


def overlap(ctx: checks.CheckContext) -> list[checks.Finding]:
    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        leaves = list(_leaves(slide))
        texts = [(s, _text_extent(deck, s)) for s in leaves]
        texts = [(s, e) for s, e in texts if e is not None]
        for shape, extent in texts:
            area = _area(extent)
            if not area:
                continue
            for other in leaves:
                if other.z <= shape.z or other.box is None or not _opaque(other):
                    continue
                share = _area(_intersection(extent, _rect(other.box))) / area
                if share > COVERED_SHARE:
                    found.append(_finding(
                        deck, slide, shape, checks.Severity.WARNING, OVERLAP,
                        f"shape_id {other.shape_id} covers {share:.0%} of this text",
                        "Move one of them with set_shape_format, or send the cover behind the text.",
                        excerpt=_excerpt(shape.text), key=f"covered {other.shape_id}"))
        for n, (shape, extent) in enumerate(texts):
            for other, other_extent in texts[n + 1:]:
                if shape.group_id is not None and shape.group_id == other.group_id:
                    continue
                x0, y0, x1, y1 = _intersection(extent, other_extent)
                if x1 - x0 > OVERLAP_SLACK and y1 - y0 > OVERLAP_SLACK:
                    found.append(_finding(
                        deck, slide, other, checks.Severity.WARNING, OVERLAP,
                        f"its text runs into the text of shape_id {shape.shape_id}",
                        "Move one of them with set_shape_format, or shorten one text.",
                        excerpt=_excerpt(other.text), key=f"text {shape.shape_id}"))
    return found


def _luminance(hex_value: str) -> float:
    channels = []
    for i in (0, 2, 4):
        c = int(hex_value[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(fg: str, bg: str) -> float:
    light, dark = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _behind(deck: _Deck, slide: Any, shape: Any, leaves: list[Any], extent: tuple[float, ...]) -> tuple[str, ...]:
    """The colours the text is read against: its own fill, else the topmost
    earlier shape under its middle, else the slide's background; a
    gradient's where the text sits, a picture's none."""
    if shape.fill and shape.fill != "none" and shape.fill_alpha > SEE_THROUGH:
        return _colours(shape.fill, shape.gradient, _rect(shape.box), extent)
    mx, my = (extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2
    under = [o for o in leaves if o.z < shape.z and o.box is not None and _opaque(o)]
    for other in sorted(under, key=lambda o: o.z, reverse=True):
        x0, y0, x1, y1 = _rect(other.box)
        if x0 <= mx <= x1 and y0 <= my <= y1:
            return () if other.kind == "picture" else _colours(other.fill, other.gradient, (x0, y0, x1, y1), extent)
    return _colours(slide.background, slide.background_gradient, (0, 0, deck.width, deck.height), extent)


def _colours(fill: str | None, gradient: Any, area: tuple[float, ...], extent: tuple[float, ...]) -> tuple[str, ...]:
    """A solid fill's colour, or a linear gradient's along the stretch of
    ``area`` the text covers (every stop of a path gradient)."""
    if fill != "gradient":
        return (fill,) if fill and _HEX.fullmatch(fill) else ()
    if gradient is None:
        return ()
    if gradient.angle is None:
        return tuple(colour for _, colour in gradient.stops)
    start, end = _span(gradient.angle, area, extent)
    return tuple(_colour_at(gradient.stops, start + (end - start) * k / GRADIENT_SAMPLES)
                 for k in range(GRADIENT_SAMPLES + 1))


def _span(angle: float, area: tuple[float, ...], extent: tuple[float, ...]) -> tuple[float, float]:
    """Where the text starts and ends along a linear gradient across
    ``area``: 0 at its first stop, 1 at its last."""
    turn = math.radians(angle)
    dx, dy = math.cos(turn), math.sin(turn)
    ends = [x * dx + y * dy for x in (area[0], area[2]) for y in (area[1], area[3])]
    low, high = min(ends), max(ends)
    if high - low <= 0:
        return 0.0, 1.0
    text = [x * dx + y * dy for x in (extent[0], extent[2]) for y in (extent[1], extent[3])]
    return max((min(text) - low) / (high - low), 0.0), min((max(text) - low) / (high - low), 1.0)


def _colour_at(stops: tuple[tuple[float, str], ...], at: float) -> str:
    """A gradient's colour at ``at``, mixed between its stops."""
    if at <= stops[0][0]:
        return stops[0][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:], strict=False):
        if at <= p1:
            k = (at - p0) / (p1 - p0) if p1 > p0 else 0.0
            return "".join(f"{round(int(c0[i:i + 2], 16) * (1 - k) + int(c1[i:i + 2], 16) * k):02X}"
                           for i in (0, 2, 4))
    return stops[-1][1]


def contrast(ctx: checks.CheckContext) -> list[checks.Finding]:
    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        leaves = list(_leaves(slide))
        for shape in leaves:
            if shape.placeholder is not None and shape.placeholder[0] in _MUTED_PLACEHOLDERS:
                continue
            extent = _text_extent(deck, shape)
            if extent is None:
                continue
            behind = _behind(deck, slide, shape, leaves, extent)
            if not behind:
                continue
            scale = shape.frame.font_scale if shape.frame is not None else 1.0
            worst = None
            for paragraph in shape.paragraphs:
                for run in paragraph.runs:
                    if not run.text.strip() or not run.color or not _HEX.fullmatch(run.color):
                        continue
                    size = run.size_pt * scale
                    large = size >= LARGE_PT or (run.bold and size >= LARGE_BOLD_PT)
                    need = CONTRAST_LARGE if large else CONTRAST_TEXT
                    # Against a gradient, the stop that reads worst.
                    ratio, colour = min((contrast_ratio(run.color, c), c) for c in behind)
                    if ratio < need and (worst is None or ratio / need < worst[0] / worst[1]):
                        worst = (ratio, need, run, colour)
            if worst is None:
                continue
            ratio, need, run, colour = worst
            on = f"#{colour}" if len(behind) == 1 else f"the gradient's #{colour}"
            found.append(_finding(
                deck, slide, shape, checks.Severity.WARNING, CONTRAST,
                f"text #{run.color} on {on} has a contrast of {ratio:.1f}:1; {need:g}:1 is the least that "
                "reads at this size",
                "Darken the text or lighten what is behind it (or the reverse), with format_text color or "
                "set_shape_format fill_color.",
                excerpt=_excerpt(run.text)))
    return found


def _table_sizes(deck: _Deck, slide: Any, shape: Any) -> list[tuple[float, str]]:
    """(size, text) of each table cell run with a size of its own; a cell
    without one takes the table's 18 pt."""
    element = deck.element(slide, shape)
    if element is None:
        return []
    from pptx.oxml.ns import qn

    sizes = []
    for run in element.iter(qn("a:r")):
        text = "".join(t.text or "" for t in run.iter(qn("a:t")))
        rpr = run.find(qn("a:rPr"))
        if text.strip() and rpr is not None and rpr.get("sz"):
            sizes.append((int(rpr.get("sz")) / 100, text))
    return sizes


def small_text(ctx: checks.CheckContext) -> list[checks.Finding]:
    """Body text and table cells below 12 pt (a note) or 10 pt (a warning),
    at the scale a shrink stores; dates, footers, slide numbers and a
    one-line note at the foot of the slide may be small."""
    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        for shape in _leaves(slide):
            if shape.placeholder is not None and shape.placeholder[0] in _MUTED_PLACEHOLDERS:
                continue
            if shape.kind == "table":
                sizes = _table_sizes(deck, slide, shape)
                fix_op = "format_table_cell font_size"
            else:
                extent = _text_extent(deck, shape)
                if extent is None:
                    continue
                result = deck.measure(shape)
                if result is not None and result.lines == 1 and extent[1] >= deck.height * FOOTNOTE_BAND:
                    continue
                scale = shape.frame.font_scale if shape.frame is not None else 1.0
                sizes = [(run.size_pt * scale, run.text) for paragraph in shape.paragraphs for run in paragraph.runs
                         if run.text.strip() and run.field is None]
                fix_op = "format_text font_size"
            if not sizes:
                continue
            size, text = min(sizes, key=lambda item: item[0])
            if size >= SMALL_NOTE_PT:
                continue
            severity = checks.Severity.WARNING if size < SMALL_WARN_PT else checks.Severity.INFO
            found.append(_finding(
                deck, slide, shape, severity, SMALL_TEXT,
                f"text at {size:g} pt is hard to read on a shown slide; {SMALL_NOTE_PT:g} pt is the least for "
                "body text",
                f"Set {fix_op} {SMALL_NOTE_PT:g} or more, and give the text room: a bigger box, fewer words, or a "
                "second slide.",
                excerpt=_excerpt(text), key="small"))
    return found


def leftover(ctx: checks.CheckContext) -> list[checks.Finding]:
    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        for shape in _leaves(slide):
            text = shape.text
            match = next((m for m in _TOKENS.finditer(text)
                          if m.group(0).lower() not in _UPPER_ONLY or m.group(0).isupper()), None)
            if match:
                fix = "Replace it with set_text" + (
                    "." if shape.placeholder is not None else ", or remove the shape with remove_shape.")
                found.append(_finding(
                    deck, slide, shape, checks.Severity.WARNING, LEFTOVER,
                    "template or unfinished text is left in the slide", fix,
                    excerpt=_excerpt(match.group(0)), key=match.group(0)[:40]))
            # Only a text shape is empty: a chart, table, picture or diagram
            # put in a content placeholder carries the placeholder too.
            elif (shape.kind in _TEXT_SHAPES and shape.placeholder is not None
                  and shape.placeholder[0] in _TEXT_PLACEHOLDERS and not text.strip()):
                found.append(_finding(
                    deck, slide, shape, checks.Severity.WARNING, LEFTOVER,
                    "an empty placeholder: PowerPoint shows its 'Click to add' prompt while editing",
                    "Fill it with set_text or add_bullet_points, or remove it with remove_shape.", key="empty"))
    return found


def slide_number(ctx: checks.CheckContext) -> list[checks.Finding]:
    deck = _deck(ctx)
    found = []
    for slide in deck.slides:
        expected = str(deck.first_number + slide.index)
        for shape in _leaves(slide):
            for paragraph in shape.paragraphs:
                for run in paragraph.runs:
                    if run.field == "slidenum" and run.text.strip() != expected:
                        found.append(_finding(
                            deck, slide, shape, checks.Severity.INFO, SLIDE_NUMBER,
                            f"the slide number shows {run.text.strip() or 'nothing'} until the deck is opened; "
                            f"the slide is number {expected}",
                            "Run set_slide_number once: it rewrites every number.", key="stale"))
    return found


def register_all() -> None:
    for name, fn in ((OVERFLOW, overflow), (WRAP_TRAP, wrap_trap), (OFF_SLIDE, off_slide), (OVERLAP, overlap),
                     (CONTRAST, contrast), (SMALL_TEXT, small_text)):
        checks.register("pptx", name, fn, baseline=True)
    checks.register("pptx", LEFTOVER, leftover, final_only=True, baseline=True)
    checks.register("pptx", SLIDE_NUMBER, slide_number, final_only=True)


register_all()
