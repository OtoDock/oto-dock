"""Paragraph and run formatting from an operation's keys.

Values arrive checked by the catalogue (lengths, spacings, colours) and are
converted here with each key's own unit: spacing before and after in points,
indents in inches. A key that is absent leaves the document's formatting as
it is; ``false`` turns a run format off explicitly.
"""

import weakref
from typing import Any

import units
from colors import Color, parse_color
from formats.docx.styles import apply_paragraph_style
from results import Code, OpError

ALIGNMENTS = ("left", "center", "right", "justify")
TAB_ALIGNMENTS = ("left", "center", "right", "decimal")
TAB_LEADERS = ("none", "dot", "hyphen", "underscore", "middle_dot", "heavy")

# Larger than any page: a value this big means points were confused with
# twips or EMUs, and every paragraph would land on its own page.
_MAX_SPACING_PT = 500
_MAX_LINE_MULTIPLE = 10

# Word's names for the theme slots in w:themeColor and w:themeFill.
_THEME_WORD_NAMES = {
    "dk1": "text1", "lt1": "background1", "dk2": "text2", "lt2": "background2",
    "accent1": "accent1", "accent2": "accent2", "accent3": "accent3", "accent4": "accent4",
    "accent5": "accent5", "accent6": "accent6", "hlink": "hyperlink", "folHlink": "followedHyperlink",
}

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{_W_NS}}}{tag}"


def alignment(name: str, key: str = "alignment") -> Any:
    """The python-docx alignment for ``name``, or a failure naming the choices."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    value = str(name).strip().lower()
    if value not in ALIGNMENTS:
        raise OpError(Code.BAD_VALUE, f"{key} '{name}' is not one of: {', '.join(ALIGNMENTS)}",
                      hint=f"Give {key} as one of: {', '.join(ALIGNMENTS)}.")
    return {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
            "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}[value]


def _theme_byte(amount: float) -> str:
    return f"{round(255 * (1 - amount)):02X}"


_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_THEME_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
_THEME_CACHE: "weakref.WeakKeyDictionary[Any, dict[str, str]]" = weakref.WeakKeyDictionary()
_FALLBACK_THEME = {"dk1": "000000", "lt1": "FFFFFF", "dk2": "1F497D", "lt2": "EEECE1", "accent1": "4F81BD",
                   "accent2": "C0504D", "accent3": "9BBB59", "accent4": "8064A2", "accent5": "4BACC6",
                   "accent6": "F79646", "hlink": "0000FF", "folHlink": "800080"}


def forget_theme(part: Any) -> None:
    """Read the theme's colours anew at the next use (a patch changed it)."""
    _THEME_CACHE.pop(getattr(part, "_document_part", part), None)


def theme_colors(part: Any) -> dict[str, str]:
    """The document theme's colour slots as hex, read from its theme part
    (cached on the document part); Office's defaults where it has none."""
    from lxml import etree

    doc_part = getattr(part, "_document_part", part)
    cached = _THEME_CACHE.get(doc_part)
    if cached is not None:
        return cached
    colors = dict(_FALLBACK_THEME)
    for rel in doc_part.rels.values():
        if rel.reltype != _THEME_REL or rel.is_external:
            continue
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        try:
            root = etree.fromstring(rel.target_part.blob, parser)
        except etree.XMLSyntaxError:
            break
        scheme = root.find(f".//{_DRAWING_NS}clrScheme")
        for slot in (scheme if scheme is not None else ()):
            name = etree.QName(slot).localname
            value = slot.find(_DRAWING_NS + "srgbClr")
            hex_value = value.get("val") if value is not None else None
            if hex_value is None:
                system = slot.find(_DRAWING_NS + "sysClr")
                hex_value = system.get("lastClr") if system is not None else None
            if name in colors and hex_value and len(hex_value) == 6:
                colors[name] = hex_value.upper()
        break
    _THEME_CACHE[doc_part] = colors
    return colors


def _resolved(color: Color, part: Any) -> str:
    """A theme colour's RGB with its tint or shade, as Word writes it beside
    the theme reference for readers that ignore themes."""
    base = theme_colors(part)[color.theme] if part is not None else _FALLBACK_THEME[color.theme]
    channels = [int(base[i:i + 2], 16) for i in (0, 2, 4)]
    if color.tint is not None:
        channels = [round(c + (255 - c) * color.tint) for c in channels]
    if color.shade is not None:
        channels = [round(c * (1 - color.shade)) for c in channels]
    return "".join(f"{c:02X}" for c in channels)


def color_attrs(value: Any, *, fill: bool = False, part: Any = None) -> dict[str, str]:
    """The w:color (or, with ``fill``, w:shd) attributes for a colour key:
    a theme slot becomes Word's theme reference with its tint or shade, so it
    follows the document's theme, plus its RGB from ``part``'s theme; any
    other colour is a fixed hex value."""
    color: Color = parse_color(value)
    prefix = "Fill" if fill else "Color"
    if color.theme is None:
        return {"fill" if fill else "val": color.hex}
    attrs = {"fill" if fill else "val": _resolved(color, part), f"theme{prefix}": _THEME_WORD_NAMES[color.theme]}
    if color.tint is not None:
        attrs[f"theme{prefix}Tint" if fill else "themeTint"] = _theme_byte(color.tint)
    if color.shade is not None:
        attrs[f"theme{prefix}Shade" if fill else "themeShade"] = _theme_byte(color.shade)
    return attrs


def set_run_color(run: Any, value: Any) -> None:
    color_el = run._r.get_or_add_rPr().get_or_add_color()
    for name in ("val", "themeColor", "themeTint", "themeShade"):
        color_el.attrib.pop(_w(name), None)
    for name, attr_value in color_attrs(value, part=run.part).items():
        color_el.set(_w(name), attr_value)


def apply_run_format(run: Any, fmt: dict) -> None:
    """Run formatting from the run keys of an operation or of one item of
    ``runs``."""
    from docx.shared import Pt

    for flag in ("bold", "italic", "underline", "strikethrough"):
        if fmt.get(flag) is not None:
            on = bool(fmt[flag])
            if flag == "strikethrough":
                run.font.strike = on
            else:
                setattr(run, flag, on)
    if fmt.get("font_name"):
        run.font.name = fmt["font_name"]
    if fmt.get("font_size") is not None:
        run.font.size = Pt(units.to_pt(fmt["font_size"], default=units.POINTS))
    if fmt.get("color"):
        set_run_color(run, fmt["color"])


def apply_paragraph_format(p: Any, op: dict) -> list[str]:
    """Spacing, indents, keep options and alignment from the operation;
    returns warnings for values that are almost certainly a unit mistake."""
    from docx.shared import Emu, Pt

    warnings: list[str] = []
    pf = p.paragraph_format
    if op.get("style") is not None:
        apply_paragraph_style(p, op["style"])
    if op.get("clear_tab_stops"):
        pf.tab_stops.clear_all()
    for stop in op.get("tab_stops") or []:
        _add_tab_stop(pf, stop)
    if op.get("alignment") is not None:
        p.alignment = alignment(op["alignment"])
    for key in ("space_before", "space_after"):
        if op.get(key) is not None:
            points = units.to_pt(op[key], default=units.POINTS)
            setattr(pf, key, Pt(points))
            if points > _MAX_SPACING_PT:
                warnings.append(
                    f"{key} {points:g}pt is taller than a page. Spacing is in points (6 to 18 is "
                    "typical), not twips: give a smaller value."
                )
    if op.get("line_spacing") is not None:
        spacing = units.parse_spacing(op["line_spacing"], default=units.MULTIPLE)
        if spacing.kind == units.MULTIPLE:
            pf.line_spacing = spacing.value
            if spacing.value > _MAX_LINE_MULTIPLE:
                warnings.append(
                    f"line_spacing {spacing.value:g} is a multiple of the line height (1.0 single, "
                    "2.0 double). Give 1.5x for one and a half lines, or 14pt for exact spacing."
                )
        else:
            pf.line_spacing = Pt(spacing.value)
    for key in ("first_line_indent", "left_indent"):
        if op.get(key) is not None:
            setattr(pf, key, Emu(units.to_emu(op[key], default="in")))
    for key in ("keep_together", "keep_with_next", "widow_control"):
        if op.get(key) is not None:
            setattr(pf, key, bool(op[key]))
    return warnings


def _add_tab_stop(pf: Any, stop: dict) -> None:
    from docx.enum.text import WD_TAB_ALIGNMENT, WD_TAB_LEADER
    from docx.shared import Emu

    align = str(stop.get("alignment", "left")).strip().lower()
    leader = str(stop.get("leader", "none")).strip().lower()
    if align not in TAB_ALIGNMENTS:
        raise OpError(Code.BAD_VALUE, f"tab stop alignment '{stop['alignment']}' is not one of: "
                      f"{', '.join(TAB_ALIGNMENTS)}", hint="Give left, center, right or decimal.")
    if leader not in TAB_LEADERS:
        raise OpError(Code.BAD_VALUE, f"tab stop leader '{stop['leader']}' is not one of: {', '.join(TAB_LEADERS)}",
                      hint="Give dot for a dotted leader, as in a table of contents.")
    pf.tab_stops.add_tab_stop(
        Emu(units.to_emu(stop["position"], default="in")),
        getattr(WD_TAB_ALIGNMENT, align.upper()),
        {"none": WD_TAB_LEADER.SPACES, "dot": WD_TAB_LEADER.DOTS, "hyphen": WD_TAB_LEADER.DASHES,
         "underscore": WD_TAB_LEADER.LINES, "middle_dot": WD_TAB_LEADER.MIDDLE_DOT,
         "heavy": WD_TAB_LEADER.HEAVY}[leader])
