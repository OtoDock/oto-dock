"""The presentation as a package: opening and saving every PowerPoint kind,
the slide size with the layouts that follow it, and the deck's own layouts.

python-pptx opens a presentation or a macro presentation only and saves the
content type it loaded; templates, shows and their macro forms open here and
are saved as the kind the target path names.
"""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from formats.pptx.pptx_escape import NAME_CAP, escaped

PRESENTATION = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
PRESENTATION_MACRO = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
TEMPLATE = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
TEMPLATE_MACRO = "application/vnd.ms-powerpoint.template.macroEnabled.main+xml"
SHOW = "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml"
SHOW_MACRO = "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml"

_MAIN_TYPE_BY_EXT = {
    ".pptx": PRESENTATION, ".pptm": PRESENTATION_MACRO,
    ".potx": TEMPLATE, ".potm": TEMPLATE_MACRO,
    ".ppsx": SHOW, ".ppsm": SHOW_MACRO,
}
_MACRO_FREE = frozenset({PRESENTATION, TEMPLATE, SHOW})
_VBA_PROJECT = "http://schemas.microsoft.com/office/2006/relationships/vbaProject"

# PowerPoint's widescreen size: 13.333 x 7.5 in.
WIDE_CX = 12192000
WIDE_CY = 6858000
_EMU_PER_INCH = 914400

_registered = False
_TYPE = "type"
LAYOUT_TITLE = "title"


def _register_main_types() -> None:
    """python-pptx maps four of the six main content types to its
    presentation part; the macro template and the macro show load as plain
    parts without this."""
    global _registered
    if _registered:
        return
    from pptx.opc.package import PartFactory
    from pptx.parts.presentation import PresentationPart

    for content_type in _MAIN_TYPE_BY_EXT.values():
        PartFactory.part_type_for.setdefault(content_type, PresentationPart)
    _registered = True


def open_presentation(path: str) -> Any:
    """The presentation in ``path``, whichever PowerPoint kind it is."""
    from pptx.package import Package
    from pptx.parts.presentation import PresentationPart

    _register_main_types()
    part = Package.open(path).main_document_part
    if not isinstance(part, PresentationPart) or part.content_type not in _MAIN_TYPE_BY_EXT.values():
        raise ValueError(
            f"'{Path(path).name}' is not a PowerPoint presentation or template "
            f"(its main part is {part.content_type})")
    return part.presentation


def _set_main_content_type(prs: Any, content_type: str) -> None:
    # python-pptx keeps the content type in a lazy property cached in the
    # part's __dict__, beside the attribute it was loaded into; the writer
    # builds [Content_Types].xml from the property.
    part = prs.part
    part._content_type = content_type
    vars(part)["content_type"] = content_type


def prepare_kind(prs: Any, path: str) -> list[str]:
    """Make ``prs`` the kind ``path``'s extension names (any other extension
    keeps the kind that was loaded); the remarks the caller reports (macros
    a macro-free kind cannot hold)."""
    remarks = []
    ext = Path(path).suffix.lower()
    content_type = _MAIN_TYPE_BY_EXT.get(ext)
    if content_type is not None:
        _set_main_content_type(prs, content_type)
    if prs.part.content_type in _MACRO_FREE:
        vba = [rId for rId, rel in prs.part.rels.items() if rel.reltype == _VBA_PROJECT]
        for rId in vba:
            prs.part.rels.pop(rId)
        if vba:
            remarks.append(
                f"the macros were removed: a {ext or 'file of this kind'} cannot hold them; "
                f"save as {'.potm' if ext == '.potx' else '.ppsm' if ext == '.ppsx' else '.pptm'} "
                "to keep them")
    return remarks


def save_presentation(prs: Any, fh: Any, path: str) -> list[str]:
    """Save ``prs`` into ``fh`` as the kind ``path``'s extension names."""
    remarks = prepare_kind(prs, path)
    prs.save(fh)
    return remarks


# ---------------------------------------------------------------------------
# Slide size
# ---------------------------------------------------------------------------

_WIDE_SLACK = round(0.005 * _EMU_PER_INCH)


def slide_length(emu: int) -> int:
    """A slide side as EMU; within 0.005 in of 13.333 in it is PowerPoint's
    widescreen width exactly."""
    return WIDE_CX if abs(emu - WIDE_CX) <= _WIDE_SLACK else emu


# Top-level shapes whose size keeps its proportions when the slide changes
# shape: a stretched logo, chart or grouped drawing would be distorted.
_UNIFORM = frozenset({"pic", "graphicFrame", "grpSp", "contentPart"})


def _own_xfrm(shape_el: Any) -> Any:
    from pptx.oxml.ns import qn

    local = shape_el.tag.rsplit("}", 1)[-1]
    if local == "graphicFrame":
        return shape_el.find(qn("p:xfrm"))
    if local == "grpSp":
        return shape_el.find(f"{qn('p:grpSpPr')}/{qn('a:xfrm')}")
    return shape_el.find(f"{qn('p:spPr')}/{qn('a:xfrm')}")


def _scale_tree(sp_tree: Any, sx: float, sy: float) -> None:
    from pptx.oxml.ns import qn

    for shape_el in sp_tree:
        xfrm = _own_xfrm(shape_el)
        if xfrm is None:
            continue
        off, ext = xfrm.find(qn("a:off")), xfrm.find(qn("a:ext"))
        if off is None or ext is None:
            continue
        x, y = int(off.get("x", 0)), int(off.get("y", 0))
        cx, cy = int(ext.get("cx", 0)), int(ext.get("cy", 0))
        if shape_el.tag.rsplit("}", 1)[-1] in _UNIFORM:
            s = min(sx, sy)
            ncx, ncy = round(cx * s), round(cy * s)
            nx = round((x + cx / 2) * sx - ncx / 2)
            ny = round((y + cy / 2) * sy - ncy / 2)
        else:
            nx, ny, ncx, ncy = round(x * sx), round(y * sy), round(cx * sx), round(cy * sy)
        off.set("x", str(nx))
        off.set("y", str(ny))
        ext.set("cx", str(ncx))
        ext.set("cy", str(ncy))


def set_slide_size(prs: Any, cx: int, cy: int) -> None:
    """Resize the slides and move every master's and layout's own shapes to
    the new size: boxes stretch with the slide, pictures, graphic frames and
    groups keep their proportions. Slides' own shapes stay where they are."""
    from pptx.oxml.ns import qn

    old_cx, old_cy = prs.slide_width, prs.slide_height
    prs.slide_width, prs.slide_height = cx, cy
    sld_sz = prs.part._element.find(qn("p:sldSz"))
    if sld_sz is not None and _TYPE in sld_sz.attrib:
        del sld_sz.attrib[_TYPE]  # e.g. screen4x3, which no longer holds
    if not old_cx or not old_cy or (old_cx, old_cy) == (cx, cy):
        return
    sx, sy = cx / old_cx, cy / old_cy
    for master in prs.slide_masters:
        _scale_tree(master.shapes._spTree, sx, sy)
        for layout in master.slide_layouts:
            _scale_tree(layout.shapes._spTree, sx, sy)


def _fresh_properties(prs: Any) -> None:
    """python-pptx's template carries its own history (a 2013 date, its
    author as the last editor, "generated using python-pptx", a 4:3 show):
    a new deck starts with none of it."""
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    props = prs.core_properties
    props.created = props.modified = now
    props.last_modified_by = ""
    props.comments = ""
    props.revision = 1
    for part in prs.part.package.iter_parts():
        if part.partname == "/docProps/app.xml":
            part._blob = part.blob.replace(b"<PresentationFormat>On-screen Show (4:3)</PresentationFormat>",
                                           b"<PresentationFormat>Widescreen</PresentationFormat>")


def new_presentation() -> Any:
    """A blank 16:9 presentation whose layouts fill the wide slide."""
    from pptx import Presentation

    prs = Presentation()
    set_slide_size(prs, WIDE_CX, WIDE_CY)
    _fresh_properties(prs)
    return prs


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LayoutEntry:
    number: int          # across every master, in document order
    layout: Any
    master_number: int   # 1-based
    name: str
    type: str | None     # the p:sldLayout type attribute
    placeholders: tuple[tuple[Any, int | None], ...]   # what a new slide gets: (type, idx);
                                                        # idx None when two read as the same


def _placeholder_word(ph_type: Any) -> str:
    from pptx.enum.shapes import PP_PLACEHOLDER as P

    return {
        P.TITLE: "title", P.CENTER_TITLE: "title", P.SUBTITLE: "subtitle",
        P.BODY: "body", P.OBJECT: "content", P.PICTURE: "picture", P.CHART: "chart",
        P.TABLE: "table", P.MEDIA_CLIP: "media", P.BITMAP: "clip art",
        P.ORG_CHART: "diagram", P.VERTICAL_BODY: "vertical body",
        P.VERTICAL_OBJECT: "vertical content", P.VERTICAL_TITLE: "vertical title",
    }.get(ph_type, str(getattr(ph_type, "name", ph_type)).lower().replace("_", " "))


def _placeholder_indexes(layout: Any) -> tuple[tuple[Any, int | None], ...]:
    """(type, idx) of the placeholders a new slide gets. A placeholder with
    no idx attribute reads as 0; when another one of the layout reads as the
    same number, the number names neither, so it is left out."""
    elements = [ph.element for ph in layout.iter_cloneable_placeholders()]
    numbers = [el.ph_idx for el in elements]
    return tuple(
        (el.ph_type, None if el.ph.get("idx") is None and numbers.count(n) > 1 else n)
        for el, n in zip(elements, numbers, strict=True))


def list_layouts(prs: Any) -> list[LayoutEntry]:
    entries = []
    for m, master in enumerate(prs.slide_masters, start=1):
        for layout in master.slide_layouts:
            entries.append(LayoutEntry(len(entries), layout, m, layout.name or "",
                                       layout._element.get("type"), _placeholder_indexes(layout)))
    return entries


def _clean_name(name: str) -> str:
    return escaped(" ".join(name.split()), NAME_CAP)


_LIST_CAP = 60


def describe_layouts(prs: Any) -> list[str]:
    """The read's lines: each layout's number and name, and the placeholders
    (word and index) a slide made from it gets."""
    entries = list_layouts(prs)
    many_masters = len({e.master_number for e in entries}) > 1
    lines = ["**Layouts** (add_slide layout: a name or its number):"]
    for e in entries[:_LIST_CAP]:
        label = f"  {e.number} {_clean_name(e.name) or '(unnamed)'}"
        if many_masters:
            label += f" (master {e.master_number})"
        if e.placeholders:
            label += ": " + ", ".join(_placeholder_word(t) + ("" if i is None else f" {i}")
                                      for t, i in e.placeholders)
        lines.append(label)
    if len(entries) > _LIST_CAP:
        lines.append(f"  +{len(entries) - _LIST_CAP} more: add_slide accepts any layout name")
    return lines


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(bare.casefold().replace("_", " ").replace("-", " ").split())


# The words add_slide has always taken, found by the layout type Office,
# LibreOffice and python-pptx write whatever the language; on python-pptx's
# own template they give the layouts they always gave.
_LEGACY_TYPES = {
    "title": ("title",),
    "title and content": ("obj", "tx"),
    "content": ("obj", "tx"),
    "section header": ("secHead",),
    "two content": ("twoObj", "twoColTx"),
    "comparison": ("twoTxTwoObj",),
    "title only": ("titleOnly",),
    "blank": ("blank",),
    "image": ("blank",),
}


def _shape_counts(entry: LayoutEntry) -> tuple[bool, bool, int]:
    """(has a title, looks like a title slide, content placeholders)."""
    from pptx.enum.shapes import PP_PLACEHOLDER as P

    kinds = [t for t, _ in entry.placeholders]
    titles = {P.TITLE, P.CENTER_TITLE, P.VERTICAL_TITLE}
    centred = P.CENTER_TITLE in kinds or P.SUBTITLE in kinds
    content = sum(1 for t in kinds if t not in titles and t != P.SUBTITLE)
    return any(t in titles for t in kinds), centred, content


def _by_placeholders(entries: list[LayoutEntry], word: str) -> LayoutEntry | None:
    def first(test: Callable[[bool, bool, int], bool]) -> LayoutEntry | None:
        return next((e for e in entries if test(*_shape_counts(e))), None)

    if word == LAYOUT_TITLE:
        return first(lambda titled, centred, content: titled and centred)
    if word == "title only":
        return first(lambda titled, centred, content: titled and not centred and content == 0)
    if word in ("title and content", "content", "section header"):
        return first(lambda titled, centred, content: titled and not centred and content == 1)
    if word == "two content":
        return first(lambda titled, centred, content: titled and not centred and content == 2)
    if word == "comparison":
        return (first(lambda titled, centred, content: titled and not centred and content >= 4)
                or first(lambda titled, centred, content: titled and not centred and content == 2))
    return None


def _listing(entries: list[LayoutEntry]) -> str:
    shown = ", ".join(f"{e.number} '{_clean_name(e.name)}'" for e in entries[:30])
    more = f", +{len(entries) - 30} more" if len(entries) > 30 else ""
    return f"This deck's layouts: {shown}{more}. Use a name or its number."


class UnknownLayout(LookupError):
    """A layout name or number the deck does not have."""

    def __init__(self, value: Any, listing: str) -> None:
        super().__init__(f"no layout named {value!r}")
        self.value = value
        self.listing = listing


def resolve_layout(prs: Any, value: Any, *, strict: bool = False) -> tuple[Any, str]:
    """The layout ``value`` names, and a remark when it had to be chosen
    another way (empty when it resolved as asked). An unknown name or
    number takes the plainest layout with a remark, or raises
    ``UnknownLayout`` when ``strict``; the seven old words never fail."""
    entries = list_layouts(prs)
    if not entries:
        raise ValueError("this presentation has no slide layouts")
    if value is None:
        value = "blank"
    word = ""
    if isinstance(value, str):
        text = value.strip()
        word = _fold(text)
        exact = next((e for e in entries if e.name.strip() == text), None)
        if exact is not None:
            return exact.layout, ""
        for layout_type in _LEGACY_TYPES.get(word, ()):
            typed = next((e for e in entries if e.type == layout_type), None)
            if typed is not None:
                return typed.layout, ""
        folded = next((e for e in entries if _fold(e.name) == word), None)
        if folded is not None:
            return folded.layout, ""
        if text.isdigit():
            value = int(text)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(entries):
        return entries[value].layout, ""

    fallback = (_by_placeholders(entries, word) if word in _LEGACY_TYPES else None) \
        or min(entries, key=lambda e: len(e.placeholders))
    if word in _LEGACY_TYPES:
        return fallback.layout, f"this deck has no '{value}' layout; used '{_clean_name(fallback.name)}'"
    if strict:
        raise UnknownLayout(value, _listing(entries))
    return fallback.layout, (f"no layout named {value!r} (used '{_clean_name(fallback.name)}'). "
                             + _listing(entries))
