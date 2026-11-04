"""write_pptx's theme operations: the deck's theme colours and fonts (what
PowerPoint's Design tab edits), ten ready theme kits, and the slide
background.

The kits' palettes come from theme-factory, part of anthropics/skills
(https://github.com/anthropics/skills, commit 3b3fad9, skills/theme-factory),
Copyright 2026 Anthropic, PBC, Apache License 2.0. Changed by OtoDock: each
four-colour palette derived into the theme's twelve colour slots (light text
lifted to a near-white, accent 1 darkened until white text reads 4.5:1 on
it, the other accents to 3:1 on the background), and the fonts swapped for
Office fonts the render engine draws with metric twins.
"""

from typing import Any

import colors
import fonts
from catalog import KeySpec, OpSpec
from formats.pptx import pptx_style
from formats.pptx.pptx_opkit import (
    G_VISUAL,
    PptxJob,
    fill_solid,
    slide_key,
    slide_of,
)
from ooxml import order
from results import Code, NoOp, OpCtx, OpError

SLOTS = ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink",
         "folHlink")
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _kit(dark: str, light: str, dark2: str, light2: str, accents: str, heading: str, body: str) -> dict[str, Any]:
    a1, a2, a3, a4, a5, a6 = accents.split()
    return {"colors": {"dk1": dark, "lt1": light, "dk2": dark2, "lt2": light2, "accent1": a1, "accent2": a2,
                       "accent3": a3, "accent4": a4, "accent5": a5, "accent6": a6, "hlink": a1, "folHlink": a5},
            "heading_font": heading, "body_font": body}


# Each kit reads: dark text 12:1 or more on the background, white text 4.5:1
# on accent 1, every other accent 3:1 on the background (a test checks).
KITS: dict[str, dict[str, Any]] = {
    "arctic_frost": _kit("23344D", "FAFAFA", "3A5782", "FAFAFA", "2673CE 909090 4A6FA5 5792D7 6F747C 5B82AF",
                         "Calibri", "Calibri"),
    "botanical_garden": _kit("4F1F12", "F5F3ED", "893520", "F5F3ED", "487957 C27A05 B7472A 719579 9F5F09 857A2E",
                             "Cambria", "Calibri"),
    "desert_rose": _kit("422031", "F5EDE5", "5D2E46", "E8D5C4", "A95151 B67A6A 5D2E46 BC7774 935F59 B0665E",
                        "Arial", "Arial"),
    "forest_canopy": _kit("213720", "FAF9F6", "2D4A2B", "FAF9F6", "6D7463 8A9466 2D4A2B 8C9283 6A7851 7C8464",
                          "Cambria", "Arial"),
    "golden_hour": _kit("302A26", "F5EEE6", "4A403A", "D4B896", "8E6200 C1666B 4A403A A18547 965456 A86436",
                        "Arial", "Arial"),
    "midnight_galaxy": _kit("2B1E3E", "EEEEFC", "2B1E3E", "E6E6FA", "4A4E8F 947CB7 2B1E3E 8386B5 746093 6F65A3",
                            "Arial", "Arial"),
    "modern_minimalist": _kit("2C3840", "FFFFFF", "36454F", "FFFFFF", "697888 939393 36454F 8A95A2 74787A 7E868E",
                              "Calibri", "Calibri"),
    "ocean_depths": _kit("1A2332", "F1FAEE", "1A2332", "F1FAEE", "287C7C 409DA0 1A2332 5B9793 35787F 348C8E",
                         "Calibri", "Calibri"),
    "sunset_boulevard": _kit("192E37", "F9EED5", "264653", "E9C46A", "C23C1B DB690F 264653 D16D4C A1571B CE5215",
                             "Cambria", "Calibri"),
    "tech_innovation": _kit("1E1E1E", "FFFFFF", "1E1E1E", "FFFFFF", "0066FF 00A3A3 1E1E1E 4A93FF 097B7B 0084D1",
                            "Calibri", "Calibri"),
}


def _theme_parts(prs: Any, master: int | None) -> list[tuple[Any, Any]]:
    """(master, its theme part) for every master, or the 1-based one named;
    a theme part two masters share is listed once."""
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    masters = list(prs.slide_masters)
    if master is not None:
        if not 1 <= master <= len(masters):
            raise OpError(Code.NOT_FOUND, f"master {master} does not exist (the deck has {len(masters)})",
                          hint=f"Use 1 to {len(masters)}, as read_document lists them, or leave master out.")
        masters = [masters[master - 1]]
    found, seen = [], set()
    for each in masters:
        try:
            part = each.part.part_related_by(RT.THEME)
        except KeyError:
            continue
        if id(part) not in seen:
            seen.add(id(part))
            found.append((each, part))
    return found


def _child(parent: Any, local: str, before: tuple[str, ...] = ()) -> Any:
    """``parent``'s ``a:<local>`` child, made in place when missing (before
    the first of ``before`` present)."""
    from lxml import etree

    tag = f"{{{A_NS}}}{local}"
    found = parent.find(tag)
    if found is not None:
        return found
    made = etree.Element(tag)
    anchor = next((c for c in parent if etree.QName(c).localname in before), None)
    if anchor is not None:
        anchor.addprevious(made)
    else:
        parent.append(made)
    return made


def _write_theme(part: Any, colours: dict[str, str], heading: str | None, body: str | None, name: str | None) -> None:
    from lxml import etree

    from ooxml import parts

    # The parse every editor of the theme shares (an xml_patch in the same
    # batch sees this write, and this one sees the patch's).
    root = parts.xml_root(part)
    elements = root.find(f"{{{A_NS}}}themeElements")
    if elements is None:
        raise OpError(Code.UNSUPPORTED, "the deck's theme has no theme elements to edit",
                      hint="Save the deck from PowerPoint or LibreOffice first.")
    if colours:
        scheme = _child(elements, "clrScheme", ("fontScheme", "fmtScheme"))
        for slot in SLOTS:
            if slot not in colours:
                continue
            holder = _child(scheme, slot, (*SLOTS[SLOTS.index(slot) + 1:], "extLst"))
            for old in list(holder):
                holder.remove(old)
            etree.SubElement(holder, f"{{{A_NS}}}srgbClr", val=colours[slot])
        if name:
            scheme.set("name", name)
    if heading or body:
        scheme = _child(elements, "fontScheme", ("fmtScheme",))
        for which, typeface in (("majorFont", heading), ("minorFont", body)):
            if not typeface:
                continue
            collection = _child(scheme, which, ("minorFont", "extLst") if which == "majorFont" else ("extLst",))
            latin = _child(collection, "latin", ("ea", "cs", "font", "extLst"))
            latin.set("typeface", typeface)
            for script in ("ea", "cs"):
                if collection.find(f"{{{A_NS}}}{script}") is None:
                    _child(collection, script, ("cs", "font", "extLst") if script == "ea" else ("font", "extLst"))\
                        .set("typeface", "")
        if name:
            scheme.set("name", name)
    if name:
        # The theme's own name, the one PowerPoint's Design tab shows.
        root.set("name", name)
    parts.flush_xml_roots(part.package, only=part)


def _hard_coded(master: Any) -> list[str]:
    """What the master's title and body styles set themselves, so does not
    follow the theme."""
    found = []
    styles = master._element.find(f"{{{P_NS}}}txStyles")
    for style, label in (("titleStyle", "titles"), ("bodyStyle", "body text")):
        node = styles.find(f"{{{P_NS}}}{style}") if styles is not None else None
        if node is None:
            continue
        faces = {e.get("typeface") for e in node.iter(f"{{{A_NS}}}latin")
                 if e.get("typeface") and not e.get("typeface").startswith("+")}
        if faces:
            found.append(f"{label} keep the font {', '.join(sorted(faces))} the master sets")
        if node.find(f".//{{{A_NS}}}defRPr/{{{A_NS}}}solidFill/{{{A_NS}}}srgbClr") is not None:
            found.append(f"{label} keep the colour the master sets")
    return found


def _drawn_as(font: str) -> str | None:
    twin = fonts.ALIASES.get(font.casefold())
    return twin[0] if twin and twin[0].casefold() != font.casefold() else None


def _op_set_theme(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    import difflib

    colours: dict[str, str] = {}
    heading, body, name = op.get("heading_font"), op.get("body_font"), op.get("name")
    kit = op.get("kit")
    if kit is not None:
        word = str(kit).strip().casefold().replace(" ", "_").replace("-", "_")
        if word not in KITS:
            close = difflib.get_close_matches(word, KITS, n=1, cutoff=0.6)
            raise OpError(Code.BAD_VALUE, f"unknown theme kit '{kit}'",
                          hint=(f"Did you mean '{close[0]}'? " if close else "") + "Kits: " + ", ".join(KITS) + ".")
        colours.update(KITS[word]["colors"])
        heading = heading or KITS[word]["heading_font"]
        body = body or KITS[word]["body_font"]
        name = name or word.replace("_", " ").title()
    given = op.get("colors") or {}
    for slot in SLOTS:
        value = given.get(slot)
        if value is None:
            continue
        color = colors.parse_color(value)
        if color.theme is not None:
            raise OpError(Code.BAD_VALUE, f"colors.{slot} names a theme slot",
                          hint="A theme colour is a colour of its own: give hex or a CSS name.")
        colours[slot] = color.hex
    if not (colours or heading or body):
        raise OpError(Code.BAD_VALUE, "set_theme needs a kit, colors or a font",
                      hint='Give kit (for example "ocean_depths"), colors {accent1: ...} or heading_font/body_font.')
    targets = _theme_parts(job.prs, op.get("master"))
    if not targets:
        raise NoOp("the deck's masters have no theme part to edit")
    notes = []
    for master, part in targets:
        _write_theme(part, colours, heading, body, name)
        notes.extend(_hard_coded(master))
    recoloured = sum(pptx_style.repick_drawn(slide) for slide in job.prs.slides)
    for slide in job.prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                job.touch(slide, shape)
    if heading or body:
        shown = [f"{label} {font}" + (f" (drawn here as {_drawn_as(font)})" if _drawn_as(font) else "")
                 for label, font in (("headings", heading), ("body", body)) if font]
        ctx.note("the theme now uses " + " and ".join(shown))
    if recoloured:
        ctx.note(f"{recoloured} shape{'s' if recoloured != 1 else ''} drawn in the theme's accent took text that "
                 "reads on the new colour")
    for line in notes:
        ctx.warn(line)


def _stop_color(value: Any) -> str:
    """A gradient stop's colour element: srgbClr, or schemeClr for a theme
    slot with its tint or shade as a luminance change."""
    color = colors.parse_color(value)
    if color.theme is None:
        return f'<a:srgbClr val="{color.hex}"/>'
    slot = {"dk1": "tx1", "lt1": "bg1", "dk2": "tx2", "lt2": "bg2"}.get(color.theme, color.theme)
    mods = ""
    if color.tint:
        mods = f'<a:lumMod val="{round((1 - color.tint) * 100000)}"/><a:lumOff val="{round(color.tint * 100000)}"/>'
    elif color.shade:
        mods = f'<a:lumMod val="{round((1 - color.shade) * 100000)}"/>'
    return f'<a:schemeClr val="{slot}">{mods}</a:schemeClr>'


def _stop_position(value: float, ctx: OpCtx) -> int:
    """A stop's position from 0 to 1 as OOXML's thousandths of a percent; a
    value above 1 up to 100 reads as a percent."""
    if 1 < value <= 100:
        ctx.note(f"position {value:g} read as {value:g}%")
        value = value / 100
    if not 0 <= value <= 1:
        raise OpError(Code.BAD_VALUE, f"position {value:g} is out of range", hint="Use 0 to 1 (or a percent).")
    return round(value * 100000)


def _op_set_background(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.oxml import parse_xml

    slide, _ = slide_of(job, op)
    gradient = op.get("gradient")
    if gradient and len(gradient) >= 2:
        stops = "".join(
            f'<a:gs pos="{_stop_position(g.get("position", i / (len(gradient) - 1)), ctx)}">'
            f'{_stop_color(g.get("color", "FFFFFF"))}</a:gs>'
            for i, g in enumerate(gradient))
        grad = parse_xml(f'<a:gradFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                         f'rotWithShape="1"><a:gsLst>{stops}</a:gsLst>'
                         f'<a:lin ang="{round(op.get("angle", 270) * 60000) % 21600000}" scaled="1"/></a:gradFill>')
        # p:cSld/p:bg/p:bgPr, made in schema order by python-pptx: the fill
        # first, then the effect list the schema requires.
        bg_pr = slide._element.cSld.get_or_add_bgPr()
        for child in list(bg_pr):
            if child.tag.rsplit("}", 1)[-1] in ("solidFill", "gradFill", "noFill", "pattFill", "blipFill", "grpFill"):
                bg_pr.remove(child)
        order.insert_child(bg_pr, grad)
        return
    if gradient:
        ctx.warn("a gradient needs at least two stops: the background is the plain colour")
    fill_solid(slide.background.fill, op.get("color", "FFFFFF"), ctx)


OPS: tuple[OpSpec, ...] = (
    OpSpec("set_theme", _op_set_theme, (
        KeySpec("kit", ("str",), help=" | ".join(KITS)),
        KeySpec("colors", ("dict",), fields=tuple(KeySpec(slot, ("color",)) for slot in SLOTS),
                help="the slots to set: dk1, lt1, dk2, lt2 (text and backgrounds), accent1-6, hlink, folHlink"),
        KeySpec("heading_font", ("str",)), KeySpec("body_font", ("str",)),
        KeySpec("name", ("str",), help="the theme's name in PowerPoint's Design tab"),
        KeySpec("master", ("int",), help="1-based, as read_document lists them; default every master"),
    ), G_VISUAL, note="the deck's theme colours and fonts: whatever names them follows (theme colour slots, "
                      "theme fonts); a kit sets all twelve colours and two fonts, colors and fonts then override",
        examples=({"op": "set_theme", "kit": "ocean_depths"},
                  {"op": "set_theme", "colors": {"accent1": "#1F4E79", "accent2": "#E07A1F"},
                   "heading_font": "Cambria", "body_font": "Calibri", "name": "Northwind", "master": 1})),
    OpSpec("set_background", _op_set_background, (
        slide_key(), KeySpec("color", ("color",), default="FFFFFF"),
        KeySpec("gradient", ("list",), item=(KeySpec("color", ("color",), default="FFFFFF"),
                                             KeySpec("position", ("float",), help="0-1"))),
        KeySpec("angle", ("float",), default=270),
    ), G_VISUAL, aliases=("set_background_color",), examples=(
        {"op": "set_background", "slide_index": 0, "color": "#F7F9FC"},
        {"op": "set_background", "slide_index": 0, "gradient": [{"color": "#1F4E79"}, {"color": "#2E75B6"}]})),
)
