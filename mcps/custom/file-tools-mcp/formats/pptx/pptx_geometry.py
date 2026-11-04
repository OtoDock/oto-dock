"""What a deck looks like, as data: every slide's shapes with their boxes in
slide coordinates, their text frames, paragraphs and runs with the values a
viewer draws them with, their tables and charts.

A value a slide leaves out is resolved the way PowerPoint resolves it: a
placeholder from its layout placeholder, then the master's; text from the
shape's own list style, the layout's and master's placeholders, the
master's text styles and the presentation's default text style; colours
through the colour map and the theme; fonts through the theme's font
scheme. Lengths are EMU, font sizes and spacing points.
"""

import colorsys
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from formats.pptx.pptx_deck import LayoutEntry, list_layouts, open_presentation

EMU_PER_INCH = 914400
EMU_PER_PT = 12700
DEFAULT_INSETS = (91440, 45720, 91440, 45720)
DEFAULT_SIZE_PT = 18.0

# Placeholder types, as the p:ph type attribute writes them.
PH_TITLE = "title"
PH_CENTER_TITLE = "ctrTitle"
PH_SUBTITLE = "subTitle"
PH_BODY = "body"
PH_OBJECT = "obj"
PH_DATE = "dt"
PH_FOOTER = "ftr"
PH_NUMBER = "sldNum"
TITLE_TYPES = frozenset({PH_TITLE, PH_CENTER_TITLE})
_OWN_MASTER_TYPE = frozenset({PH_DATE, PH_FOOTER, PH_NUMBER})

KIND_TEXTBOX = "textbox"
KIND_SHAPE = "shape"
KIND_PICTURE = "picture"
KIND_CHART = "chart"
KIND_TABLE = "table"
KIND_GROUP = "group"
KIND_CONNECTOR = "connector"
KIND_GRAPHIC = "graphic"
KIND_INK = "ink"

AUTOFIT_NONE = "none"
AUTOFIT_SHRINK = "shrink"
AUTOFIT_RESIZE = "resize"

FILL_NONE = "none"
FILL_GRADIENT = "gradient"
FILL_PICTURE = "picture"
FILL_PATTERN = "pattern"

SOURCE_SLIDE = "slide"
SOURCE_LAYOUT = "layout"
SOURCE_MASTER = "master"

LINE_PCT = "pct"
LINE_PT = "pt"

_ALIGN = {"l": "left", "ctr": "center", "r": "right", "just": "justify", "dist": "distributed",
          "justLow": "justify"}
_SCHEME_SLOTS = ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5",
                 "accent6", "hlink", "folHlink")
_DEFAULT_CLR_MAP = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}
_TRUE = frozenset({"1", "true", "on"})
_SHAPE_TAGS = frozenset({"sp", "grpSp", "graphicFrame", "cxnSp", "pic", "contentPart"})
_CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"


def _qn(tag: str) -> str:
    from pptx.oxml.ns import qn

    return qn(tag)


def _local(el: Any) -> str:
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.rsplit("}", 1)[-1]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    cx: int
    cy: int
    rot: float = 0.0            # degrees, clockwise
    flip_h: bool = False
    flip_v: bool = False

    @property
    def right(self) -> int:
        return self.x + self.cx

    @property
    def bottom(self) -> int:
        return self.y + self.cy


@dataclass
class Run:
    text: str                   # an a:br is a run whose text is "\n"
    font: str
    size_pt: float              # before any autofit scale
    bold: bool = False
    italic: bool = False
    caps: str = ""              # "all", "small" or ""
    spacing_pt: float = 0.0
    lang: str | None = None
    color: str | None = None    # RRGGBB, "gradient" or "picture", None when no fill is set
    field: str | None = None    # "slidenum", "datetime", ...


@dataclass
class Paragraph:
    level: int
    runs: list[Run]
    align: str = "left"
    bullet: str | None = None   # the glyph, "autonum:<scheme>", "picture", or None
    start_at: int = 1           # an autonumber's first number
    indent_emu: int = 0
    margin_left_emu: int = 0
    line_spacing: tuple[str, float] = (LINE_PCT, 1.0)
    space_before_pt: float = 0.0
    space_after_pt: float = 0.0
    end_size_pt: float = DEFAULT_SIZE_PT     # the paragraph mark's size: an empty line's height

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class Frame:
    insets: tuple[int, int, int, int] = DEFAULT_INSETS   # left, top, right, bottom
    wrap: bool = True
    anchor: str = "t"
    autofit: str = AUTOFIT_NONE
    font_scale: float = 1.0
    line_reduction: float = 0.0
    columns: int = 1
    column_gap: int = 0
    vertical: str = "horz"


@dataclass
class ChartInfo:
    type: str
    title: str
    categories: list[str]
    series: list[tuple[str, list[Any]]]


@dataclass(frozen=True)
class Gradient:
    """A gradient fill: its stops (position 0-1, RRGGBB) and a linear one's
    angle in degrees (0 runs left to right, 90 top to bottom); None for a
    path gradient (radial, rectangular)."""
    stops: tuple[tuple[float, str], ...]
    angle: float | None


@dataclass
class Shape:
    slide_index: int
    shape_id: int
    name: str
    index: int | None           # shape_index among the slide's top-level shapes; None inside a group
    z: int                      # paint order over the whole tree
    kind: str
    placeholder: tuple[str, int | None] | None   # (p:ph type, idx; None when two read as one)
    preset: str | None          # prstGeom, "custom", None
    box: Box | None
    box_source: str
    group_id: int | None
    fill: str | None            # RRGGBB, gradient, picture, pattern, none, or None (nothing drawn)
    frame: Frame | None
    paragraphs: list[Paragraph]
    table: list[list[str]] | None = None
    chart: ChartInfo | None = None
    gradient: Gradient | None = None    # a gradient fill's stops and direction
    fill_alpha: float = 1.0             # the fill's opacity (an alpha on its colour), 0-1
    hidden: bool = False
    children: list["Shape"] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


@dataclass
class Slide:
    index: int
    layout_index: int | None
    layout_name: str
    hidden: bool
    background: str | None
    shapes: list[Shape]                 # top level, in paint order; groups hold their children
    notes: str = ""
    wrapped: int = 0                    # mc:AlternateContent objects (not shapes the ops address)
    transition: str | None = None      # the transition's name in set_transition's terms
    animations: int = 0                # effect rows in the slide's timing
    background_gradient: Gradient | None = None

    def walk(self) -> Iterator[Shape]:
        """Every shape, groups' children included, in paint order."""
        stack = list(reversed(self.shapes))
        while stack:
            shape = stack.pop()
            yield shape
            stack.extend(reversed(shape.children))


@dataclass
class Deck:
    width: int
    height: int
    slides: list[Slide]
    layouts: list[LayoutEntry]


# ---------------------------------------------------------------------------
# Theme and colours
# ---------------------------------------------------------------------------


class _Theme:
    def __init__(self, master: Any) -> None:
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT
        from pptx.oxml import parse_xml

        self.colors: dict[str, str] = {}
        self.fonts = {"major": "", "minor": ""}
        self.fill_styles: list[Any] = []
        self.bg_styles: list[Any] = []
        try:
            root = parse_xml(master.part.part_related_by(RT.THEME).blob)
        except (KeyError, ValueError, SyntaxError):
            return
        elements = root.find(_qn("a:themeElements"))
        if elements is None:
            return
        scheme = elements.find(_qn("a:clrScheme"))
        if scheme is not None:
            for slot in _SCHEME_SLOTS:
                el = scheme.find(_qn(f"a:{slot}"))
                if el is not None and len(el):
                    value = _plain_color(el[0])
                    if value:
                        self.colors[slot] = value
        fonts = elements.find(_qn("a:fontScheme"))
        if fonts is not None:
            for key, tag in (("major", "a:majorFont"), ("minor", "a:minorFont")):
                latin = fonts.find(f"{_qn(tag)}/{_qn('a:latin')}")
                if latin is not None:
                    self.fonts[key] = latin.get("typeface", "")
        fmt = elements.find(_qn("a:fmtScheme"))
        if fmt is not None:
            lst = fmt.find(_qn("a:fillStyleLst"))
            self.fill_styles = list(lst) if lst is not None else []
            lst = fmt.find(_qn("a:bgFillStyleLst"))
            self.bg_styles = list(lst) if lst is not None else []

    def font(self, typeface: str | None) -> str | None:
        if not typeface:
            return None
        if typeface.startswith("+mj"):
            return self.fonts["major"] or None
        if typeface.startswith("+mn"):
            return self.fonts["minor"] or None
        return typeface


def _plain_color(el: Any) -> str | None:
    """A colour element that needs no colour map: srgbClr, sysClr, prstClr,
    scrgbClr."""
    name = _local(el)
    if name == "srgbClr":
        value = (el.get("val") or "").upper()
        return value if re.fullmatch(r"[0-9A-F]{6}", value) else None
    if name == "sysClr":
        value = (el.get("lastClr") or "").upper()
        if re.fullmatch(r"[0-9A-F]{6}", value):
            return value
        return "000000" if el.get("val") == "windowText" else "FFFFFF" if el.get("val") == "window" else None
    if name == "scrgbClr":
        channels = [_int(el.get(c)) / 100000 for c in ("r", "g", "b")]
        return "".join(f"{round(_to_srgb(v) * 255):02X}" for v in channels)
    if name == "prstClr":
        import colors

        try:
            return colors.parse_color(el.get("val", "")).hex.upper()
        except Exception:  # noqa: BLE001  # a preset name CSS does not know draws as nothing we can name
            return None
    return None


def _to_srgb(linear: float) -> float:
    linear = min(max(linear, 0.0), 1.0)
    return 12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055


def _modified(hex_value: str, el: Any) -> str:
    """The colour with the element's lumMod, lumOff, satMod, satOff, tint
    and shade applied (in sRGB: close to Office, exact enough to judge
    contrast)."""
    rgb = [int(hex_value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    for mod in el:
        name, val = _local(mod), _int(mod.get("val"), 100000) / 100000
        if name in ("lumMod", "lumOff"):
            h, lum, s = colorsys.rgb_to_hls(rgb[0], rgb[1], rgb[2])
            lum = lum * val if name == "lumMod" else lum + val
            rgb = list(colorsys.hls_to_rgb(h, min(max(lum, 0.0), 1.0), s))
        elif name in ("satMod", "satOff"):
            h, lum, s = colorsys.rgb_to_hls(rgb[0], rgb[1], rgb[2])
            s = s * val if name == "satMod" else s + val
            rgb = list(colorsys.hls_to_rgb(h, lum, min(max(s, 0.0), 1.0)))
        elif name == "tint":
            rgb = [c * val + (1 - val) for c in rgb]
        elif name == "shade":
            rgb = [c * val for c in rgb]
    return "".join(f"{round(min(max(c, 0.0), 1.0) * 255):02X}" for c in rgb)


@dataclass
class _Colors:
    theme: _Theme
    clr_map: dict[str, str]

    def resolve(self, el: Any, placeholder_color: str | None = None) -> str | None:
        """The RRGGBB of a colour element (srgbClr, schemeClr, ...)."""
        if el is None:
            return None
        name = _local(el)
        if name == "schemeClr":
            slot = el.get("val", "")
            if slot == "phClr":
                base = placeholder_color
            else:
                slot = self.clr_map.get(slot, slot)
                base = self.theme.colors.get(slot)
        else:
            base = _plain_color(el)
        return _modified(base, el) if base else None

    def fill(self, el: Any, placeholder_color: str | None = None) -> str | None:
        """What a fill element draws: RRGGBB, gradient, picture, pattern or
        none."""
        name = _local(el)
        if name == "solidFill":
            return self.resolve(el[0], placeholder_color) if len(el) else None
        return {"gradFill": FILL_GRADIENT, "blipFill": FILL_PICTURE, "pattFill": FILL_PATTERN,
                "noFill": FILL_NONE, "grpFill": None}.get(name)

    def gradient(self, el: Any, placeholder_color: str | None = None) -> Gradient | None:
        """A gradient fill's stops and direction; None for any other fill."""
        if el is None or _local(el) != "gradFill":
            return None
        gs_lst = el.find(_qn("a:gsLst"))
        stops = []
        for gs in gs_lst if gs_lst is not None else ():
            color = self.resolve(gs[0], placeholder_color) if len(gs) else None
            if color:
                stops.append((_int(gs.get("pos"), 0) / 100000, color))
        if not stops:
            return None
        lin = el.find(_qn("a:lin"))
        return Gradient(tuple(sorted(stops)), _int(lin.get("ang"), 0) / 60000 if lin is not None else None)


_FILL_TAGS = ("solidFill", "gradFill", "blipFill", "pattFill", "noFill", "grpFill")


def _fill_child(sp_pr: Any) -> Any:
    if sp_pr is None:
        return None
    for child in sp_pr:
        if _local(child) in _FILL_TAGS:
            return child
    return None


def _clr_map(el: Any) -> dict[str, str] | None:
    """A master's p:clrMap, or a slide's or layout's override (None when it
    keeps the master's)."""
    if el is None:
        return None
    if _local(el) == "clrMapOvr":
        override = el.find(_qn("a:overrideClrMapping"))
        return dict(override.attrib) if override is not None else None
    return dict(el.attrib)


# ---------------------------------------------------------------------------
# Placeholders: the layout and master shapes a slide placeholder inherits from
# ---------------------------------------------------------------------------


def _ph(el: Any) -> Any:
    for path in ("p:nvSpPr", "p:nvPicPr", "p:nvGraphicFramePr", "p:nvCxnSpPr", "p:nvGrpSpPr"):
        nv = el.find(_qn(path))
        if nv is not None:
            return nv.find(f"{_qn('p:nvPr')}/{_qn('p:ph')}")
    return None


def _ph_type(ph: Any) -> str:
    return ph.get("type", PH_OBJECT)


def _family(ph_type: str) -> str:
    return PH_TITLE if ph_type in TITLE_TYPES else ph_type


def _placeholders(tree: Any) -> list[tuple[Any, Any]]:
    return [(el, ph) for el in tree if _local(el) in _SHAPE_TAGS and (ph := _ph(el)) is not None]


def _match_layout(ph: Any, slide_phs: list[tuple[Any, Any]], layout_phs: list[tuple[Any, Any]]) -> Any:
    """The layout shape a slide placeholder takes what it leaves out from:
    PowerPoint links by idx; a placeholder without one (LibreOffice writes
    none) goes by type, the k-th of a type to the k-th."""
    family = _family(_ph_type(ph))
    if ph.get("idx") is not None:
        same = [(el, lph) for el, lph in layout_phs if _int(lph.get("idx"), 0) == _int(ph.get("idx"))]
        typed = [el for el, lph in same if _family(_ph_type(lph)) == family]
        if typed or same:
            return (typed or [el for el, _ in same])[0]
    same_family = [p for _, p in slide_phs if _family(_ph_type(p)) == family]
    ordinal = next((i for i, p in enumerate(same_family) if p is ph), 0)
    typed = [el for el, lph in layout_phs if _family(_ph_type(lph)) == family]
    if typed:
        return typed[min(ordinal, len(typed) - 1)]
    return None


def _match_master(ph_type: str, master_phs: list[tuple[Any, Any]]) -> Any:
    family = _family(ph_type)
    wanted = family if family in _OWN_MASTER_TYPE or family == PH_TITLE else PH_BODY
    return next((el for el, mph in master_phs if _family(_ph_type(mph)) == wanted), None)


def _shown_idx(placeholders: list[Any]) -> dict[int, int | None]:
    """Each placeholder's idx as an op can use it: None for one without an
    idx attribute when another reads as the same number."""
    numbers = [_int(ph.get("idx"), 0) for ph in placeholders]
    return {id(ph): None if ph.get("idx") is None and numbers.count(n) > 1 else n
            for ph, n in zip(placeholders, numbers, strict=True)}


# ---------------------------------------------------------------------------
# Text: the chain of list styles a paragraph takes its values from
# ---------------------------------------------------------------------------


def _lst_levels(lst: Any, level: int) -> list[Any]:
    """A list style's entries for a level: its lvlNpPr, then its defPPr."""
    if lst is None:
        return []
    found = [lst.find(_qn(f"a:lvl{level + 1}pPr")), lst.find(_qn("a:defPPr"))]
    return [el for el in found if el is not None]


@dataclass
class _TextSources:
    """Where a shape's paragraphs take what they leave out, nearest first."""
    lists: list[Any]                # lstStyle elements (shape, layout ph, master ph)
    styles: list[Any]               # txStyles entry, then the presentation's default
    body_prs: list[Any]             # bodyPr elements (shape, layout ph, master ph)
    font_ref: Any = None            # p:style/a:fontRef of a non-placeholder shape

    def levels(self, level: int) -> tuple[list[Any], list[Any]]:
        """(the shape's and its placeholders' list-style entries, the text
        styles' entries) for a level: a shape style's font sits between."""
        near = [el for lst in self.lists for el in _lst_levels(lst, level)]
        far = [el for lst in self.styles for el in _lst_levels(lst, level)]
        return near, far


def _first_attr(elements: Iterable[Any], name: str) -> str | None:
    for el in elements:
        if el is not None and el.get(name) is not None:
            return el.get(name)
    return None


def _first_child(elements: Iterable[Any], tags: tuple[str, ...]) -> Any:
    for el in elements:
        if el is None:
            continue
        for child in el:
            if _local(child) in tags:
                return child
    return None


def _spacing(el: Any, size_pt: float) -> float | None:
    if el is None:
        return None
    pts = el.find(_qn("a:spcPts"))
    if pts is not None:
        return _int(pts.get("val")) / 100
    pct = el.find(_qn("a:spcPct"))
    if pct is not None:
        return _int(pct.get("val")) / 100000 * size_pt * 1.2
    return None


_TEXT_FILLS = ("solidFill", "gradFill", "noFill", "pattFill", "blipFill")


def _typeface(elements: Iterable[Any]) -> str | None:
    for el in elements:
        latin = el.find(_qn("a:latin")) if el is not None else None
        if latin is not None:
            return latin.get("typeface")
    return None


def _paragraph(p: Any, sources: _TextSources, colors: _Colors, theme: _Theme) -> Paragraph:
    ppr = p.find(_qn("a:pPr"))
    level = min(max(_int(ppr.get("lvl"), 0) if ppr is not None else 0, 0), 8)
    near, far = sources.levels(level)
    chain = [ppr, *near, *far]
    near_rprs = [el.find(_qn("a:defRPr")) for el in [ppr, *near] if el is not None]
    far_rprs = [el.find(_qn("a:defRPr")) for el in far]
    def_rprs = [*near_rprs, *far_rprs]
    font_color = None
    if sources.font_ref is not None and len(sources.font_ref):
        font_color = colors.resolve(sources.font_ref[0])
    ref_font = theme.fonts.get(sources.font_ref.get("idx", "")) if sources.font_ref is not None else None
    runs: list[Run] = []
    for child in p:
        name = _local(child)
        if name not in ("r", "br", "fld"):
            continue
        rpr = child.find(_qn("a:rPr"))
        rchain = [rpr, *def_rprs]
        near_chain = [rpr, *near_rprs]
        size = _first_attr(rchain, "sz")
        # A shape style's font and colour come before the text styles'.
        latin = _typeface(near_chain)
        if latin is None and ref_font is None:
            latin = _typeface(far_rprs)
        fill = _first_child(near_chain, _TEXT_FILLS)
        if fill is None and font_color is None:
            fill = _first_child(far_rprs, _TEXT_FILLS)
        color = colors.fill(fill) if fill is not None else font_color
        if name == "br":
            text = "\n"
        else:
            t = child.find(_qn("a:t"))
            text = t.text or "" if t is not None else ""
        runs.append(Run(
            text=text,
            font=theme.font(latin) or ref_font or theme.fonts["minor"] or "",
            size_pt=_int(size, int(DEFAULT_SIZE_PT * 100)) / 100,
            bold=(_first_attr(rchain, "b") or "") in _TRUE,
            italic=(_first_attr(rchain, "i") or "") in _TRUE,
            caps={"all": "all", "small": "small"}.get(_first_attr(rchain, "cap") or "", ""),
            spacing_pt=_int(_first_attr(rchain, "spc"), 0) / 100,
            lang=_first_attr(rchain, "lang"),
            color=color if color not in (FILL_NONE,) else None,
            field=child.get("type") if name == "fld" else None,
        ))
    end_size = _int(_first_attr([p.find(_qn("a:endParaRPr")), *def_rprs], "sz"), int(DEFAULT_SIZE_PT * 100)) / 100
    first_size = runs[0].size_pt if runs else end_size
    bullet_el = _first_child(chain, ("buNone", "buChar", "buAutoNum", "buBlip"))
    bullet = None
    start_at = 1
    if bullet_el is not None:
        kind = _local(bullet_el)
        bullet = (bullet_el.get("char") if kind == "buChar" else
                  f"autonum:{bullet_el.get('type', '')}" if kind == "buAutoNum" else
                  FILL_PICTURE if kind == "buBlip" else None)
        if kind == "buAutoNum":
            start_at = max(_int(bullet_el.get("startAt"), 1), 1)
    line = (LINE_PCT, 1.0)
    ln = _first_child(chain, ("lnSpc",))
    if ln is not None:
        pts, pct = ln.find(_qn("a:spcPts")), ln.find(_qn("a:spcPct"))
        if pts is not None:
            line = (LINE_PT, _int(pts.get("val")) / 100)
        elif pct is not None:
            line = (LINE_PCT, _int(pct.get("val"), 100000) / 100000)
    return Paragraph(
        level=level, runs=runs,
        align=_ALIGN.get(_first_attr(chain, "algn") or "l", "left"),
        bullet=bullet, start_at=start_at,
        indent_emu=_int(_first_attr(chain, "indent"), 0),
        margin_left_emu=_int(_first_attr(chain, "marL"), 0),
        line_spacing=line,
        space_before_pt=_spacing(_first_child(chain, ("spcBef",)), first_size) or 0.0,
        space_after_pt=_spacing(_first_child(chain, ("spcAft",)), first_size) or 0.0,
        end_size_pt=end_size,
    )


def _frame(body_prs: list[Any]) -> Frame:
    insets = tuple(_int(_first_attr(body_prs, name), default)
                   for name, default in zip(("lIns", "tIns", "rIns", "bIns"), DEFAULT_INSETS, strict=True))
    fit = _first_child(body_prs, ("noAutofit", "normAutofit", "spAutoFit"))
    autofit, scale, reduction = AUTOFIT_NONE, 1.0, 0.0
    if fit is not None and _local(fit) == "normAutofit":
        autofit = AUTOFIT_SHRINK
        scale = _int(fit.get("fontScale"), 100000) / 100000
        reduction = _int(fit.get("lnSpcReduction"), 0) / 100000
    elif fit is not None and _local(fit) == "spAutoFit":
        autofit = AUTOFIT_RESIZE
    return Frame(
        insets=(insets[0], insets[1], insets[2], insets[3]),
        wrap=_first_attr(body_prs, "wrap") != "none",
        anchor=_first_attr(body_prs, "anchor") or "t",
        autofit=autofit, font_scale=scale, line_reduction=reduction,
        columns=max(_int(_first_attr(body_prs, "numCol"), 1), 1),
        column_gap=_int(_first_attr(body_prs, "spcCol"), 0),
        vertical=_first_attr(body_prs, "vert") or "horz",
    )


# ---------------------------------------------------------------------------
# Building the deck
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    """One slide's surroundings."""
    slide_index: int
    slide: Any
    colors: _Colors
    theme: _Theme
    layout_phs: list[tuple[Any, Any]]
    master_phs: list[tuple[Any, Any]]
    slide_phs: list[tuple[Any, Any]]
    shown_idx: dict[int, int | None]
    styles: dict[str, Any]          # "title", "body", "other" -> txStyles entries; "default"
    z: int = 0


def _xfrm(el: Any) -> Any:
    local = _local(el)
    if local == "graphicFrame":
        return el.find(_qn("p:xfrm"))
    if local == "grpSp":
        return el.find(f"{_qn('p:grpSpPr')}/{_qn('a:xfrm')}")
    return el.find(f"{_qn('p:spPr')}/{_qn('a:xfrm')}")


@dataclass(frozen=True)
class _Transform:
    """Child coordinates of a group to slide coordinates: an affine map of
    points (``x' = a x + c y + e``, ``y' = b x + d y + f``), the scale of
    sizes, and the rotation and flips groups add to their children."""
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0
    sx: float = 1.0
    sy: float = 1.0
    rot: float = 0.0
    flip_h: bool = False
    flip_v: bool = False

    def point(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def then(self, inner: "_Transform") -> "_Transform":
        """``inner`` applied first, then this."""
        return _Transform(
            a=self.a * inner.a + self.c * inner.b, b=self.b * inner.a + self.d * inner.b,
            c=self.a * inner.c + self.c * inner.d, d=self.b * inner.c + self.d * inner.d,
            e=self.a * inner.e + self.c * inner.f + self.e, f=self.b * inner.e + self.d * inner.f + self.f,
            sx=self.sx * inner.sx, sy=self.sy * inner.sy,
            rot=(self.rot + (-inner.rot if self.mirrored else inner.rot)) % 360,
            flip_h=self.flip_h != inner.flip_h, flip_v=self.flip_v != inner.flip_v)

    @property
    def mirrored(self) -> bool:
        return self.flip_h != self.flip_v

    def box(self, x: int, y: int, cx: int, cy: int, rot: float = 0.0, flip_h: bool = False,
            flip_v: bool = False) -> Box:
        """A shape's box: its centre mapped, its size scaled, its rotation
        and flips combined with the groups'."""
        mx, my = self.point(x + cx / 2, y + cy / 2)
        width, height = cx * self.sx, cy * self.sy
        turn = ((-rot if self.mirrored else rot) + self.rot) % 360
        return Box(round(mx - width / 2), round(my - height / 2), round(width), round(height), rot=turn,
                   flip_h=flip_h != self.flip_h, flip_v=flip_v != self.flip_v)

    def into(self, xfrm: Any) -> "_Transform":
        """The transform for the children of a group with this ``xfrm``:
        child space to the group's box, flipped and then turned about the
        box's centre, as PowerPoint draws a group."""
        off, ext = xfrm.find(_qn("a:off")), xfrm.find(_qn("a:ext"))
        ch_off, ch_ext = xfrm.find(_qn("a:chOff")), xfrm.find(_qn("a:chExt"))
        if None in (off, ext, ch_off, ch_ext):
            return self
        gx, gy, gcx, gcy = (_int(off.get("x")), _int(off.get("y")), _int(ext.get("cx")), _int(ext.get("cy")))
        ccx, ccy = _int(ch_ext.get("cx")), _int(ch_ext.get("cy"))
        sx = gcx / ccx if ccx else 1.0
        sy = gcy / ccy if ccy else 1.0
        local = _Transform(a=sx, d=sy, e=gx - _int(ch_off.get("x")) * sx, f=gy - _int(ch_off.get("y")) * sy,
                           sx=sx, sy=sy)
        mx, my = gx + gcx / 2, gy + gcy / 2
        flip_h, flip_v = xfrm.get("flipH") in _TRUE, xfrm.get("flipV") in _TRUE
        if flip_h or flip_v:
            mirror = _Transform(a=-1.0 if flip_h else 1.0, d=-1.0 if flip_v else 1.0,
                                e=2 * mx if flip_h else 0.0, f=2 * my if flip_v else 0.0, flip_h=flip_h, flip_v=flip_v)
            local = mirror.then(local)
        turn = _int(xfrm.get("rot"), 0) / 60000
        if turn:
            # Clockwise on a page whose y axis points down.
            cos, sin = math.cos(math.radians(turn)), math.sin(math.radians(turn))
            rotate = _Transform(a=cos, b=sin, c=-sin, d=cos, e=mx - mx * cos + my * sin, f=my - mx * sin - my * cos,
                                rot=turn)
            local = rotate.then(local)
        return self.then(local)


def _box_of(xfrm: Any, transform: _Transform) -> Box | None:
    if xfrm is None:
        return None
    off, ext = xfrm.find(_qn("a:off")), xfrm.find(_qn("a:ext"))
    if off is None or ext is None:
        return None
    return transform.box(_int(off.get("x")), _int(off.get("y")), _int(ext.get("cx")), _int(ext.get("cy")),
                         rot=_int(xfrm.get("rot"), 0) / 60000, flip_h=xfrm.get("flipH") in _TRUE,
                         flip_v=xfrm.get("flipV") in _TRUE)


def _c_nv_pr(el: Any) -> Any:
    for nv in el:
        found = nv.find(_qn("p:cNvPr"))
        if found is not None:
            return found
    return None


def _kind(el: Any) -> str:
    local = _local(el)
    if local == "sp":
        c_nv_sp = el.find(f"{_qn('p:nvSpPr')}/{_qn('p:cNvSpPr')}")
        return KIND_TEXTBOX if c_nv_sp is not None and c_nv_sp.get("txBox") in _TRUE else KIND_SHAPE
    if local == "graphicFrame":
        data = el.find(f"{_qn('a:graphic')}/{_qn('a:graphicData')}")
        uri = data.get("uri") if data is not None else ""
        return KIND_CHART if uri == _CHART_URI else KIND_TABLE if uri == _TABLE_URI else KIND_GRAPHIC
    return {"pic": KIND_PICTURE, "grpSp": KIND_GROUP, "cxnSp": KIND_CONNECTOR,
            "contentPart": KIND_INK}.get(local, KIND_SHAPE)


def _sources(el: Any, ph: Any, ctx: _Context, layout_el: Any, master_el: Any) -> _TextSources:
    def own(shape_el: Any, tag: str) -> Any:
        if shape_el is None:
            return None
        body = shape_el.find(_qn("p:txBody"))
        return body.find(_qn(tag)) if body is not None else None

    chain = [el, layout_el, master_el] if ph is not None else [el]
    if ph is None:
        styles = [ctx.styles.get("default")]
    else:
        family = _family(_ph_type(ph))
        key = "title" if family == PH_TITLE else "other" if family in _OWN_MASTER_TYPE else "body"
        styles = [ctx.styles.get(key), ctx.styles.get("default")]
    font_ref = None
    if ph is None:
        style = el.find(_qn("p:style"))
        font_ref = style.find(_qn("a:fontRef")) if style is not None else None
    return _TextSources(
        lists=[own(e, "a:lstStyle") for e in chain],
        styles=[s for s in styles if s is not None],
        body_prs=[own(e, "a:bodyPr") for e in chain],
        font_ref=font_ref,
    )


def _shape_fill(el: Any, chain: list[Any], colors: _Colors, theme: _Theme) -> tuple[str | None, Gradient | None]:
    """What the shape's fill draws, and a gradient's stops and direction."""
    if _local(el) == "pic":
        return FILL_PICTURE, None
    for shape_el in chain:
        if shape_el is None:
            continue
        found = _fill_child(shape_el.find(_qn("p:spPr")))
        if found is not None:
            return colors.fill(found), colors.gradient(found)
    style = el.find(_qn("p:style"))
    ref = style.find(_qn("a:fillRef")) if style is not None else None
    if ref is not None:
        idx = _int(ref.get("idx"), 0)
        color = colors.resolve(ref[0]) if len(ref) else None
        if idx == 0:
            return FILL_NONE, None
        styles = theme.fill_styles if idx < 1000 else theme.bg_styles
        pos = (idx if idx < 1000 else idx - 1000) - 1
        if 0 <= pos < len(styles):
            return colors.fill(styles[pos], placeholder_color=color), colors.gradient(styles[pos], color)
        return color, None
    return None, None


def _fill_alpha(el: Any) -> float:
    """The opacity of the shape's own solid or gradient fill (the most
    opaque stop of a gradient); 1 when it sets none."""
    sp_pr = el.find(_qn("p:spPr"))
    fill = _fill_child(sp_pr) if sp_pr is not None else None
    if fill is None or _local(fill) not in ("solidFill", "gradFill"):
        return 1.0
    colours = [fill[0]] if _local(fill) == "solidFill" and len(fill) else \
        [gs[0] for gs in fill.iter(_qn("a:gs")) if len(gs)]
    alphas = []
    for colour in colours:
        alpha = colour.find(_qn("a:alpha"))
        alphas.append(_int(alpha.get("val"), 100000) / 100000 if alpha is not None else 1.0)
    return max(alphas, default=1.0)


def _table(el: Any) -> list[list[str]]:
    tbl = el.find(f"{_qn('a:graphic')}/{_qn('a:graphicData')}/{_qn('a:tbl')}")
    rows = []
    if tbl is None:
        return rows
    for tr in tbl.iterchildren(_qn("a:tr")):
        cells = []
        for tc in tr.iterchildren(_qn("a:tc")):
            body = tc.find(_qn("a:txBody"))
            paras = [] if body is None else ["".join(t.text or "" for t in p.iter(_qn("a:t")))
                                             for p in body.iterchildren(_qn("a:p"))]
            cells.append("\n".join(paras).strip("\n"))
        rows.append(cells)
    return rows


def _chart(el: Any, slide: Any) -> ChartInfo | None:
    chart_ref = el.find(f"{_qn('a:graphic')}/{_qn('a:graphicData')}/{_qn('c:chart')}")
    if chart_ref is None:
        return None
    try:
        chart = slide.part.related_part(chart_ref.get(_qn("r:id"))).chart
    except (KeyError, AttributeError, ValueError):
        return None
    try:
        kind = chart.chart_type.name.lower()
    except Exception:  # noqa: BLE001  # a chart python-pptx cannot type still has series
        kind = "unknown"
    title = ""
    try:
        if chart.has_title and chart.chart_title.has_text_frame:
            title = chart.chart_title.text_frame.text
    except Exception:  # noqa: BLE001  # python-pptx on a user's chart raises anything from its parser
        title = ""
    categories: list[str] = []
    series: list[tuple[str, list[Any]]] = []
    try:
        plots = list(chart.plots)
        if plots:
            categories = [str(c) for c in plots[0].categories]
        for plot in plots:
            for s in plot.series:
                series.append((s.name or "", list(s.values)))
    except Exception:  # noqa: BLE001  # a series python-pptx cannot read leaves the rest
        pass
    return ChartInfo(kind, title, categories, series)


def _shape(el: Any, ctx: _Context, transform: _Transform, index: int | None, group_id: int | None) -> Shape:
    c_nv_pr = _c_nv_pr(el)
    shape_id = _int(c_nv_pr.get("id")) if c_nv_pr is not None else 0
    ctx.z += 1
    z = ctx.z
    kind = _kind(el)
    ph = _ph(el)
    layout_el = master_el = None
    placeholder = None
    box = _box_of(_xfrm(el), transform)
    source = SOURCE_SLIDE
    if ph is not None:
        placeholder = (_ph_type(ph), ctx.shown_idx.get(id(ph), _int(ph.get("idx"), 0)))
        layout_el = _match_layout(ph, ctx.slide_phs, ctx.layout_phs)
        master_el = _match_master(_ph_type(ph), ctx.master_phs)
        if box is None:
            for candidate, where in ((layout_el, SOURCE_LAYOUT), (master_el, SOURCE_MASTER)):
                if candidate is not None and (inherited := _box_of(_xfrm(candidate), _Transform())) is not None:
                    box, source = inherited, where
                    break
    preset = None
    sp_pr = el.find(_qn("p:spPr"))
    if sp_pr is not None:
        geom = sp_pr.find(_qn("a:prstGeom"))
        preset = geom.get("prst") if geom is not None else "custom" if sp_pr.find(_qn("a:custGeom")) is not None \
            else None
    sources = _sources(el, ph, ctx, layout_el, master_el)
    paragraphs: list[Paragraph] = []
    frame = None
    body = el.find(_qn("p:txBody"))
    if body is not None:
        frame = _frame(sources.body_prs)
        paragraphs = [_paragraph(p, sources, ctx.colors, ctx.theme) for p in body.iterchildren(_qn("a:p"))]
    fill, gradient = (_shape_fill(el, [el, layout_el, master_el] if ph is not None else [el], ctx.colors, ctx.theme)
                   if kind not in (KIND_GROUP, KIND_CONNECTOR, KIND_CHART, KIND_TABLE, KIND_GRAPHIC) else (None, None))
    shape = Shape(
        slide_index=ctx.slide_index, shape_id=shape_id,
        name=c_nv_pr.get("name", "") if c_nv_pr is not None else "",
        index=index, z=z, kind=kind, placeholder=placeholder, preset=preset, box=box, box_source=source,
        group_id=group_id, fill=fill, frame=frame, paragraphs=paragraphs,
        table=_table(el) if kind == KIND_TABLE else None,
        chart=_chart(el, ctx.slide) if kind == KIND_CHART else None,
        gradient=gradient,
        fill_alpha=_fill_alpha(el),
        hidden=c_nv_pr is not None and c_nv_pr.get("hidden") in _TRUE,
    )
    if kind == KIND_GROUP:
        xfrm = _xfrm(el)
        inner = transform.into(xfrm) if xfrm is not None else transform
        shape.children = [_shape(child, ctx, inner, None, shape_id) for child in el
                          if _local(child) in _SHAPE_TAGS]
    return shape


def _background(trees: list[Any], colors: _Colors, theme: _Theme) -> tuple[str | None, Gradient | None]:
    """What the slide's background draws, and a gradient's stops and
    direction."""
    for c_sld in trees:
        bg = c_sld.find(_qn("p:bg")) if c_sld is not None else None
        if bg is None:
            continue
        bg_pr = bg.find(_qn("p:bgPr"))
        if bg_pr is not None:
            found = _fill_child(bg_pr)
            if found is not None:
                return colors.fill(found), colors.gradient(found)
        ref = bg.find(_qn("p:bgRef"))
        if ref is not None:
            color = colors.resolve(ref[0]) if len(ref) else None
            idx = _int(ref.get("idx"), 0)
            pos = idx - 1001
            if 0 <= pos < len(theme.bg_styles):
                return (colors.fill(theme.bg_styles[pos], placeholder_color=color),
                        colors.gradient(theme.bg_styles[pos], color))
            return color, None
    return None, None


def _styles(prs: Any, master: Any) -> dict[str, Any]:
    tx = master._element.find(_qn("p:txStyles"))
    styles = {}
    if tx is not None:
        for key, tag in (("title", "p:titleStyle"), ("body", "p:bodyStyle"), ("other", "p:otherStyle")):
            styles[key] = tx.find(_qn(tag))
    styles["default"] = prs.part._element.find(_qn("p:defaultTextStyle"))
    return styles


def _slide_colors(slide: Any) -> tuple[_Theme, _Colors]:
    """The slide's theme, and its colours through the colour map its
    master, layout and own override give."""
    layout = slide.slide_layout
    master = layout.slide_master
    theme = _Theme(master)
    clr_map = {**_DEFAULT_CLR_MAP, **(_clr_map(master._element.find(_qn("p:clrMap"))) or {})}
    for el in (layout._element, slide._element):
        override = _clr_map(el.find(_qn("p:clrMapOvr")))
        if override:
            clr_map = override
    return theme, _Colors(theme, clr_map)


def scheme_color(slide: Any, slot: str) -> str | None:
    """A theme colour (``accent1``, ``lt1``, ``tx1``, ...) as RRGGBB on a
    slide."""
    from lxml import etree

    return _slide_colors(slide)[1].resolve(etree.Element(_qn("a:schemeClr"), val=slot))


def slide_geometry(prs: Any, slide: Any, slide_index: int, layout_numbers: dict[int, int] | None = None) -> Slide:
    from formats.pptx import pptx_animations, pptx_transitions

    layout = slide.slide_layout
    master = layout.slide_master
    theme, colors = _slide_colors(slide)
    tree = slide.shapes._spTree
    slide_phs = _placeholders(tree)
    ctx = _Context(
        slide_index=slide_index, slide=slide, colors=colors, theme=theme,
        layout_phs=_placeholders(layout.shapes._spTree), master_phs=_placeholders(master.shapes._spTree),
        slide_phs=slide_phs, shown_idx=_shown_idx([ph for _, ph in slide_phs]),
        styles=_styles(prs, master),
    )
    top = [el for el in tree if _local(el) in _SHAPE_TAGS]
    shapes = [_shape(el, ctx, _Transform(), i, None) for i, el in enumerate(top)]
    notes = ""
    try:
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
    except Exception:  # noqa: BLE001  # python-pptx on a user's notes raises anything from its parser
        notes = ""
    c_slds = [s._element.find(_qn("p:cSld")) for s in (slide, layout, master)]
    background, background_gradient = _background(c_slds, colors, theme)
    return Slide(
        index=slide_index,
        layout_index=(layout_numbers or {}).get(id(layout._element)),
        layout_name=layout.name or "",
        hidden=slide._element.get("show") in ("0", "false"),
        background=background,
        shapes=shapes, notes=notes,
        wrapped=sum(1 for el in tree if _local(el) == "AlternateContent"),
        transition=pptx_transitions.read_transition(slide._element),
        animations=pptx_animations.count_animations(slide._element),
        background_gradient=background_gradient,
    )


def iter_slides(prs: Any, slides: Iterable[int] | None = None) -> Iterator[Slide]:
    """Each slide's geometry, one at a time; ``slides`` limits them to those
    slide indexes."""
    numbers = {id(entry.layout._element): entry.number for entry in list_layouts(prs)}
    all_slides = list(prs.slides)
    wanted = range(len(all_slides)) if slides is None else [i for i in slides if 0 <= i < len(all_slides)]
    for i in wanted:
        yield slide_geometry(prs, all_slides[i], i, numbers)


def deck_geometry(prs: Any, slides: Iterable[int] | None = None) -> Deck:
    """The deck as data; ``slides`` limits it to those slide indexes."""
    return Deck(width=prs.slide_width or 0, height=prs.slide_height or 0, slides=list(iter_slides(prs, slides)),
                layouts=list_layouts(prs))


def load_geometry(path: str, slides: Iterable[int] | None = None) -> Deck:
    return deck_geometry(open_presentation(path), slides)


def inches(emu: int) -> float:
    return emu / EMU_PER_INCH
