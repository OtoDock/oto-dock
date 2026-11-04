"""Shape effects write_pptx sets: a gradient fill, transparency, a shadow, a
glow, soft edges and a dashed outline, written into the shape's ``spPr`` in
the schema's order.

An ``a:effectLst`` of the shape's own replaces the effects its style gives
(python-pptx's default style gives a shadow), so setting a glow alone drops
that shadow; ``shadow: true`` keeps one.
"""

from typing import Any

import colors
from catalog import KeySpec
from ooxml import order
from results import Code, OpCtx, OpError

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
EMU_PER_PT = 12700
_FILLS = ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill")
_SHADOWS = ("outerShdw", "prstShdw")
DASHES = {"solid": "SOLID", "dash": "DASH", "dot": "ROUND_DOT", "square_dot": "SQUARE_DOT", "dash_dot": "DASH_DOT",
          "long_dash": "LONG_DASH", "long_dash_dot": "LONG_DASH_DOT"}
_THEME_SLOTS = {"dk1": "tx1", "lt1": "bg1", "dk2": "tx2", "lt2": "bg2"}

KEYS = (
    KeySpec("gradient", ("dict", "list"), fields=(
        KeySpec("colors", ("list",), of=("color",), help="2 to 6"),
        KeySpec("angle", ("float",), help="degrees: 0 left to right, 90 top to bottom"),
        KeySpec("positions", ("list",), of=("float",), help="0-1, one per colour"),
    ), item=(KeySpec("color", ("color",)), KeySpec("position", ("float",), help="0-1")),
        help="a gradient fill: {colors, angle} or [{color, position}]"),
    KeySpec("transparency", ("float",), help="0-100 %, the fill's"),
    KeySpec("line_transparency", ("float",), help="0-100 %, the outline's"),
    KeySpec("shadow", ("bool", "dict"), fields=(
        KeySpec("color", ("color",)), KeySpec("blur", ("float",), help="pt"),
        KeySpec("distance", ("float",), help="pt"), KeySpec("angle", ("float",), help="degrees"),
        KeySpec("transparency", ("float",), help="0-100 %"),
    ), help="true: a soft shadow; false: none; or {color, blur, distance, angle, transparency}"),
    KeySpec("glow", ("dict",), fields=(
        KeySpec("color", ("color",)), KeySpec("size", ("float",), help="pt"),
        KeySpec("transparency", ("float",), help="0-100 %"),
    ), help="{color, size, transparency}"),
    KeySpec("soft_edges", ("float",), help="pt"),
    KeySpec("line_dash", ("str",), help=" | ".join(DASHES)),
)


def wants_effects(gradient: Any, transparency: Any, line_transparency: Any, shadow: Any, glow: Any,
                  soft_edges: Any, line_dash: Any) -> bool:
    return any(v is not None for v in (gradient, transparency, line_transparency, shadow, glow, soft_edges,
                                       line_dash))


def _el(local: str, **attrs: str) -> Any:
    from lxml import etree

    return etree.Element(f"{{{A_NS}}}{local}", **attrs)


def _percent(value: Any, what: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not 0 <= value <= 100:
        raise OpError(Code.BAD_VALUE, f"{what} {value} is out of range", hint="Use 0 to 100 (%).")
    return float(value)


def _points(value: Any, what: str, top: float = 200.0) -> int:
    if not isinstance(value, int | float) or isinstance(value, bool) or not 0 <= value <= top:
        raise OpError(Code.BAD_VALUE, f"{what} {value} is out of range", hint=f"Use 0 to {top:g} pt.")
    return round(value * EMU_PER_PT)


def colour_element(value: Any, transparency: float | None = None) -> Any:
    """A DrawingML colour element for ``value`` (hex, a CSS name, a theme
    slot with its tint or shade), with an alpha for ``transparency``."""
    color = colors.parse_color(value)
    if color.theme is None:
        el = _el("srgbClr", val=color.hex)
    else:
        el = _el("schemeClr", val=_THEME_SLOTS.get(color.theme, color.theme))
        if color.tint:
            el.append(_el("lumMod", val=str(round((1 - color.tint) * 100000))))
            el.append(_el("lumOff", val=str(round(color.tint * 100000))))
        elif color.shade:
            el.append(_el("lumMod", val=str(round((1 - color.shade) * 100000))))
    alpha = transparency if transparency is not None else (
        (1 - color.alpha) * 100 if color.alpha is not None and color.alpha < 1 else None)
    if alpha:
        el.append(_el("alpha", val=str(round((100 - alpha) * 1000))))
    return el


def _set_alpha(colour: Any, transparency: float) -> None:
    for old in colour.findall(f"{{{A_NS}}}alpha"):
        colour.remove(old)
    if transparency:
        order.insert_child(colour, _el("alpha", val=str(round((100 - transparency) * 1000))))


def _gradient(value: Any) -> Any:
    if isinstance(value, list):
        stops = [(item.get("color"), item.get("position")) for item in value]
        angle = 90.0
    else:
        given = value.get("colors") or []
        positions = value.get("positions") or [None] * len(given)
        if len(positions) != len(given):
            raise OpError(Code.BAD_VALUE, "gradient positions need one per colour",
                          hint="Give as many positions as colours, or leave positions out.")
        stops = list(zip(given, positions, strict=True))
        angle = value.get("angle", 90.0)
    if not 2 <= len(stops) <= 6 or any(color is None for color, _ in stops):
        raise OpError(Code.BAD_VALUE, "a gradient takes 2 to 6 colours",
                      hint='Give {"colors": ["#1F4E79", "#2E75B6"], "angle": 90}.')
    gradient = _el("gradFill", rotWithShape="1")
    stop_list = _el("gsLst")
    gradient.append(stop_list)
    for n, (color, position) in enumerate(stops):
        at = position if position is not None else n / (len(stops) - 1)
        if not 0 <= at <= 1:
            raise OpError(Code.BAD_VALUE, f"gradient position {at} is out of range", hint="Use 0 to 1.")
        stop = _el("gs", pos=str(round(at * 100000)))
        stop.append(colour_element(color))
        stop_list.append(stop)
    gradient.append(_el("lin", ang=str(round(float(angle) * 60000) % 21600000), scaled="0"))
    return gradient


def _fill_of(sp_pr: Any) -> Any:
    return next((c for c in sp_pr if c.tag.rsplit("}", 1)[-1] in _FILLS), None)


def _style_fill(element: Any) -> Any:
    """A solid fill of the colour the shape's style gives, for an alpha to
    sit on; None when the style gives none."""
    ref = element.find(f"{{{P_NS}}}style/{{{A_NS}}}fillRef")
    if ref is None or ref.get("idx") == "0" or not len(ref):
        return None
    fill = _el("solidFill")
    from copy import deepcopy

    fill.append(deepcopy(ref[0]))
    return fill


def _effect_list(sp_pr: Any) -> Any:
    found = sp_pr.find(f"{{{A_NS}}}effectLst")
    if found is None:
        found = _el("effectLst")
        order.insert_child(sp_pr, found)
    return found


def _replace(effects: Any, local: str, new: Any | None) -> None:
    for old in effects.findall(f"{{{A_NS}}}{local}"):
        effects.remove(old)
    if new is not None:
        order.insert_child(effects, new)


def _shadow(value: Any) -> Any:
    spec = value if isinstance(value, dict) else {}
    blur = _points(spec.get("blur", 6), "shadow blur")
    distance = _points(spec.get("distance", 3), "shadow distance")
    angle = float(spec.get("angle", 45)) % 360
    shadow = _el("outerShdw", blurRad=str(blur), dist=str(distance), dir=str(round(angle * 60000)), algn="ctr",
                 rotWithShape="0")
    shadow.append(colour_element(spec.get("color", "000000"), _percent(spec.get("transparency", 60),
                                                                        "shadow transparency")))
    return shadow


def _glow(value: Any) -> Any:
    glow = _el("glow", rad=str(_points(value.get("size", 8), "glow size")))
    glow.append(colour_element(value.get("color", "accent1"), _percent(value.get("transparency", 40),
                                                                        "glow transparency")))
    return glow


def _dash(value: Any) -> str:
    word = str(value).strip().casefold().replace(" ", "_").replace("-", "_")
    if word not in DASHES:
        raise OpError(Code.BAD_VALUE, f"unknown line_dash '{value}'", hint=f"Use one of: {', '.join(DASHES)}.")
    return DASHES[word]


def _no_outline_colour() -> OpError:
    return OpError(Code.BAD_VALUE, "the outline has no colour of its own to make transparent",
                   hint="Give line_color with line_transparency.")


def check(*, outline_coloured: bool, gradient: Any = None, transparency: Any = None, line_transparency: Any = None,
          shadow: Any = None, glow: Any = None, soft_edges: Any = None, line_dash: Any = None) -> None:
    """Refuse a wrong effect value before the op draws or changes anything;
    ``outline_coloured`` says whether the outline will have a colour."""
    if gradient is not None:
        _gradient(gradient)
    if transparency is not None:
        _percent(transparency, "transparency")
    if line_transparency is not None:
        _percent(line_transparency, "line_transparency")
        if not outline_coloured:
            raise _no_outline_colour()
    if shadow is not None and shadow is not False:
        _shadow(shadow)
    if glow is not None:
        _glow(glow)
    if soft_edges is not None:
        _points(soft_edges, "soft_edges", 100)
    if line_dash is not None:
        _dash(line_dash)


def outline_coloured(shape: Any) -> bool:
    """Whether ``shape``'s outline has a colour of its own."""
    colour = shape._element.find(f"{{{P_NS}}}spPr/{{{A_NS}}}ln/{{{A_NS}}}solidFill")
    return colour is not None and len(colour) > 0


def apply(shape: Any, ctx: OpCtx, *, gradient: Any = None, transparency: Any = None, line_transparency: Any = None,
          shadow: Any = None, glow: Any = None, soft_edges: Any = None, line_dash: Any = None) -> None:
    """The effect keys on ``shape`` (a python-pptx shape with ``spPr``);
    nothing when none is given."""
    if not wants_effects(gradient, transparency, line_transparency, shadow, glow, soft_edges, line_dash):
        return
    element = shape._element
    sp_pr = element.find(f"{{{P_NS}}}spPr")
    if sp_pr is None:
        raise OpError(Code.UNSUPPORTED, "this shape has no shape properties to style",
                      hint="Effects work on shapes, text boxes and pictures.")
    if gradient is not None:
        old = _fill_of(sp_pr)
        if old is not None:
            sp_pr.remove(old)
        order.insert_child(sp_pr, _gradient(gradient))
    if transparency is not None:
        amount = _percent(transparency, "transparency")
        fill = _fill_of(sp_pr)
        if fill is None:
            fill = _style_fill(element)
            if fill is None:
                raise OpError(Code.BAD_VALUE, "the shape has no fill to make transparent",
                              hint="Give fill_color with transparency.")
            order.insert_child(sp_pr, fill)
        local = fill.tag.rsplit("}", 1)[-1]
        if local not in ("solidFill", "gradFill"):
            raise OpError(Code.UNSUPPORTED, "transparency works on a colour or gradient fill",
                          hint="Give fill_color or gradient with transparency.")
        holders = [fill[0]] if local == "solidFill" else [gs[0] for gs in fill.iter(f"{{{A_NS}}}gs")]
        for colour in holders:
            _set_alpha(colour, amount)
    if line_dash is not None:
        from pptx.enum.dml import MSO_LINE_DASH_STYLE

        shape.line.dash_style = getattr(MSO_LINE_DASH_STYLE, _dash(line_dash))
    if line_transparency is not None:
        amount = _percent(line_transparency, "line_transparency")
        colour = sp_pr.find(f"{{{A_NS}}}ln/{{{A_NS}}}solidFill")
        if colour is None or not len(colour):
            raise _no_outline_colour()
        _set_alpha(colour[0], amount)
    if shadow is None and glow is None and soft_edges is None:
        return
    if sp_pr.find(f"{{{A_NS}}}effectDag") is not None:
        raise OpError(Code.UNSUPPORTED, "the shape's effects are written as a graph this tool does not edit",
                      hint="Edit its effects in PowerPoint.")
    had_list = sp_pr.find(f"{{{A_NS}}}effectLst") is not None
    effects = _effect_list(sp_pr)
    if shadow is not None:
        for local in _SHADOWS:
            _replace(effects, local, None)
        if shadow is not False:
            _replace(effects, "outerShdw", _shadow(shadow))
    if glow is not None:
        _replace(effects, "glow", _glow(glow))
    if soft_edges is not None:
        _replace(effects, "softEdge", _el("softEdge", rad=str(_points(soft_edges, "soft_edges", 100))))
    ref = element.find(f"{{{P_NS}}}style/{{{A_NS}}}effectRef")
    if not had_list and shadow is None and ref is not None and ref.get("idx") not in (None, "0"):
        ctx.note("the shape's own effects replace the shadow its style gave; add shadow: true to keep one")
