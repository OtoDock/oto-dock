"""Page specifications, as the PDF tools take them.

A string counts pages from 1 (``"3"``, ``"1-3"``, ``"2-"`` to the end,
``"-1"`` or ``"last"`` the last, ``"1,4,6-8"``, ``"all"``); an int or a list of ints
counts from 0, negative ones from the end. These are the conventions
edit_pdf, pdf_to_images and screenshot_document have always had.
"""

from __future__ import annotations

from typing import Any


class PageSpecError(ValueError):
    """A page specification that cannot be read."""


def _index(value: Any, total: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise PageSpecError(f"'{value}' is not a page number") from None
    if number < 0:
        number += total
    return number if 0 <= number < total else None


def page_list(spec: Any, total: int) -> tuple[list[int], list[str]]:
    """The 0-based pages ``spec`` names, in its order, and the parts of it
    that name no page of a ``total``-page document."""
    if spec is None or (isinstance(spec, str) and spec.strip().casefold() in ("", "all")):
        return list(range(total)), []
    if isinstance(spec, bool):
        raise PageSpecError("pages must be a page number, a range such as '1-3', a list or 'all'")
    if isinstance(spec, int):
        found = _index(spec, total)
        return ([found], []) if found is not None else ([], [str(spec)])
    if isinstance(spec, list):
        pages: list[int] = []
        missing: list[str] = []
        for value in spec:
            found = _index(value, total)
            if found is None:
                missing.append(str(value))
            else:
                pages.append(found)
        return pages, missing
    pages = []
    missing = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if part.casefold() == "last":
            part = "-1"
        if part.startswith("-") and part[1:].isdigit():
            found = total + int(part)
            if 0 <= found < total:
                pages.append(found)
            else:
                missing.append(part)
        elif "-" in part:
            first, last = (p.strip() for p in part.split("-", 1))
            if not first.isdigit() or (last and not last.isdigit()):
                raise PageSpecError(f"'{part}' is not a page range (write it as '2-5' or '2-')")
            start = int(first) - 1
            end = int(last) if last else total
            if start >= total or end < 1 or end <= start:
                missing.append(part)
                continue
            pages.extend(range(max(0, start), min(total, end)))
            if end > total:
                missing.append(f"{total + 1}-{end}")
        elif part.isdigit():
            number = int(part) - 1
            if 0 <= number < total:
                pages.append(number)
            else:
                missing.append(part)
        else:
            raise PageSpecError(f"'{part}' is not a page number or range")
    return pages, missing


def parse_pages(spec: Any, total: int) -> list[int]:
    """The 0-based pages ``spec`` names; the parts naming no page are left
    out. Raises PageSpecError for a spec that cannot be read."""
    return page_list(spec, total)[0]


def whole(ctx: Any, value: Any, key: str) -> int:
    """``value`` as the whole number earlier versions took (``int()``,
    toward zero), with a note when a fraction was dropped."""
    if isinstance(value, float) and not value.is_integer():
        number = int(value)
        ctx.note(f"{key} {value:g} was taken as {number}")
        return number
    return int(value)
