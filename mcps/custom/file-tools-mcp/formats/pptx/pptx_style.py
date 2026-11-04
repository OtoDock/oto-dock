"""The look of a shape write_pptx draws: solid accent 1 of the deck's theme
in place of python-pptx's light accent gradient, with text that reads on
it.
"""

from typing import Any

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_SLOTS = ("lt1", "dk1")


def text_slot(slide: Any, *, size: float | None = None, bold: bool = False) -> str | None:
    """The theme's light colour, or its dark one when light reads too faint
    on accent 1 at ``size``; None when the theme lacks a colour."""
    from formats.pptx import pptx_checks, pptx_geometry

    fill, light, dark = (pptx_geometry.scheme_color(slide, slot) for slot in ("accent1", "lt1", "dk1"))
    if not (fill and light and dark):
        return None
    size = size or pptx_geometry.DEFAULT_SIZE_PT
    large = size >= pptx_checks.LARGE_PT or (bold and size >= pptx_checks.LARGE_BOLD_PT)
    need = pptx_checks.CONTRAST_LARGE if large else pptx_checks.CONTRAST_TEXT
    on_light = pptx_checks.contrast_ratio(light, fill)
    return "lt1" if on_light >= need or on_light >= pptx_checks.contrast_ratio(dark, fill) else "dk1"


def _set_font_ref(font_ref: Any, slot: str) -> None:
    from lxml import etree

    for child in list(font_ref):
        font_ref.remove(child)
    etree.SubElement(font_ref, f"{{{A_NS}}}schemeClr", val=slot)


def solid_default(slide: Any, shape: Any, *, size: float | None = None, bold: bool = False,
                  text_color_set: bool = False) -> None:
    """Fill ``shape`` with the theme's accent 1. Its text is the theme's
    light colour, or the dark one when light reads too faint at ``size``; a
    colour the op gives stays the person's."""
    from pptx.enum.dml import MSO_THEME_COLOR

    shape.fill.solid()
    shape.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
    font_ref = shape._element.find(f"{{{P_NS}}}style/{{{A_NS}}}fontRef")
    if font_ref is None or text_color_set:
        return
    slot = text_slot(slide, size=size, bold=bold)
    if slot:
        _set_font_ref(font_ref, slot)


def repick_drawn(slide: Any) -> int:
    """Shapes drawn with the default look pick their text colour again
    against the slide's accent 1 (after the theme changed); how many
    changed."""
    changed = 0
    for sp in slide.shapes._spTree.iter(f"{{{P_NS}}}sp"):
        fill = sp.find(f"{{{P_NS}}}spPr/{{{A_NS}}}solidFill/{{{A_NS}}}schemeClr")
        font_ref = sp.find(f"{{{P_NS}}}style/{{{A_NS}}}fontRef")
        current = font_ref.find(f"{{{A_NS}}}schemeClr") if font_ref is not None else None
        if fill is None or fill.get("val") != "accent1" or current is None or current.get("val") not in _SLOTS:
            continue
        sizes = [int(r.get("sz")) / 100 for r in sp.iter(f"{{{A_NS}}}rPr") if (r.get("sz") or "").isdigit()]
        bold = any(r.get("b") in ("1", "true") for r in sp.iter(f"{{{A_NS}}}rPr"))
        slot = text_slot(slide, size=min(sizes) if sizes else None, bold=bold)
        if slot and slot != current.get("val"):
            _set_font_ref(font_ref, slot)
            changed += 1
    return changed
