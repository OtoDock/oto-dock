"""Page orientation and page size of a Word section.

Word draws the page from its width and height (``w:pgSz`` ``w:w``/``w:h``);
``w:orient`` alone does not turn it. The orientation therefore decides which
side is the long one, so a repeated request changes nothing, and a page is
landscape when it is wider than it is tall.
"""

from typing import Any

from formats.docx.package import PAGE_SIZES_TWIPS

ORIENTATIONS = ("portrait", "landscape")


def check_orientation(value: Any) -> str:
    name = str(value).strip().lower()
    if name not in ORIENTATIONS:
        raise ValueError(f"orientation '{value}' is not one of: portrait, landscape.")
    return name


def check_page_size(value: Any) -> str:
    name = str(value).strip().lower()
    if name not in PAGE_SIZES_TWIPS:
        raise ValueError(
            f"page_size '{value}' is not one of: {', '.join(PAGE_SIZES_TWIPS)}."
        )
    return name


def is_landscape(section: Any) -> bool:
    from docx.enum.section import WD_ORIENT

    width, height = section.page_width, section.page_height
    if width is not None and height is not None and width != height:
        return width > height
    return section.orientation == WD_ORIENT.LANDSCAPE


def set_orientation(section: Any, orientation: str) -> None:
    """Orient the section's page, the long side placed to match."""
    from docx.enum.section import WD_ORIENT

    landscape = check_orientation(orientation) == "landscape"
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    width, height = section.page_width, section.page_height
    if width is None or height is None:
        return
    long_, short = max(width, height), min(width, height)
    section.page_width, section.page_height = (long_, short) if landscape else (short, long_)


def set_page_size(section: Any, size: str) -> None:
    """Give the section a named paper size in its current orientation."""
    from docx.enum.section import WD_ORIENT
    from docx.shared import Twips

    short, long_ = PAGE_SIZES_TWIPS[check_page_size(size)]
    landscape = is_landscape(section)
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    if landscape:
        section.page_width, section.page_height = Twips(long_), Twips(short)
    else:
        section.page_width, section.page_height = Twips(short), Twips(long_)


def page_size_name(section: Any) -> str | None:
    """The named size the section's page has (either orientation), else None."""
    width, height = section.page_width, section.page_height
    if width is None or height is None:
        return None
    sides = sorted((round(width.twips), round(height.twips)))
    for name, (short, long_) in PAGE_SIZES_TWIPS.items():
        if abs(sides[0] - short) <= 20 and abs(sides[1] - long_) <= 20:
            return name
    return None
