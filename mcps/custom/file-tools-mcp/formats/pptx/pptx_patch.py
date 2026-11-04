"""write_pptx's xml_patch: a guarded edit of one XML part of the deck (a
slide, its notes, a layout, a master or a theme) by XPath, for what no
other operation does.

The guard is ``ooxml.patch.apply_patch``, which refuses a child its parent
never holds. It does not see a required child gone or a list left empty,
and it cannot tell whether python-pptx still reads the part, so this op
reads the part back inside the guard (its ``verify``; a new problem only).
Every refusal puts the part back with the very same element objects,
attributes and (for the theme) bytes: the later operations of the batch
work through python-pptx objects that hold those elements. After a patch,
python-pptx's cached shape trees of the part are dropped, so the next
operation reads the tree the part holds. A patch that removes a shape also removes the animations and
connector glue that named it, as remove_shape does: either left behind
makes PowerPoint repair the file.
"""

import functools
from typing import Any

from catalog import KeySpec, OpSpec
from formats.pptx import pptx_animations
from formats.pptx.pptx_deck import UnknownLayout, resolve_layout
from formats.pptx.pptx_opkit import G_XML, PptxJob, choice, plural, slide_of
from ooxml import parts, patch
from results import Code, OpCtx, OpError

PARTS = ("slide", "notes", "layout", "master", "theme")
THEME_TYPE = "application/vnd.openxmlformats-officedocument.theme+xml"
_DONE = {"replace": "replaced", "remove": "removed", "insert_before": "inserted before",
         "insert_after": "inserted after", "insert_child": "inserted into"}


def _master(job: PptxJob, op: dict) -> tuple[Any, list[Any]]:
    """The master ``master`` (1-based) or the slide's names, and the slides
    it carries."""
    masters = list(job.prs.slide_masters)
    if op.get("master") is not None:
        number = op["master"]
        if not 1 <= number <= len(masters):
            raise OpError(Code.NOT_FOUND, f"master {number} does not exist (the deck has {len(masters)})",
                          hint=f"Use 1 to {len(masters)}, as read_document lists them.")
        master = masters[number - 1]
    elif op.get("slide_index") is not None:
        master = slide_of(job, op)[0].slide_layout.slide_master
    else:
        master = masters[0]
    return master, [s for s in job.prs.slides if s.slide_layout.slide_master is master]


def _target(job: PptxJob, op: dict) -> tuple[Any, list[Any], str]:
    """(the part to patch, the slides whose text it can change, where it
    is for messages)."""
    which = choice(op.get("part", "slide"), PARTS, "part")
    if which in ("slide", "notes"):
        if op.get("slide_index") is None:
            raise OpError(Code.BAD_VALUE, f"part {which} needs slide_index", hint="Give the slide's slide_index.")
        slide, position = slide_of(job, op)
        if which == "slide":
            return slide.part, [slide], f"slide_index {position}"
        if not slide.has_notes_slide:
            raise OpError(Code.NOT_FOUND, f"slide_index {position} has no speaker notes",
                          hint="Add them with set_speaker_notes first.")
        return slide.notes_slide.part, [], f"slide_index {position}, notes"
    if which == "layout":
        if op.get("layout") is not None:
            try:
                layout, _ = resolve_layout(job.prs, op["layout"], strict=True)
            except UnknownLayout as exc:
                raise OpError(Code.NOT_FOUND, str(exc), hint=exc.listing) from None
        elif op.get("slide_index") is not None:
            layout = slide_of(job, op)[0].slide_layout
        else:
            raise OpError(Code.BAD_VALUE, "part layout needs layout or slide_index",
                          hint="Give layout (a name or number, as read_document lists them) or a slide's "
                               "slide_index for its layout.")
        return layout.part, [s for s in job.prs.slides if s.slide_layout is layout], f"layout {layout.name!r}"
    master, slides = _master(job, op)
    if which == "master":
        return master.part, slides, "the master"
    theme = _theme_of(master)
    if theme is None:
        raise OpError(Code.NOT_FOUND, "the master has no theme part", hint="Patch the master instead.")
    # A crafted deck can point the master's theme relationship at any part.
    if (theme.content_type or "").split(";")[0].strip() != THEME_TYPE:
        raise OpError(Code.UNSUPPORTED, f"the master's theme relationship leads to {str(theme.partname).lstrip('/')}, "
                      "which is not a theme", hint="Patch that part through its own part kind, if it is one.")
    shared = [s for s in job.prs.slides if _theme_of(s.slide_layout.slide_master) is theme]
    return theme, shared, "the theme"


def _theme_of(master: Any) -> Any | None:
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    try:
        return master.part.part_related_by(RT.THEME)
    except KeyError:
        return None


# The children the schema requires (minOccurs 1 in the transitional XSDs)
# of the elements a patch on these five parts can break; the order table
# lists the children a parent may hold, not those it must.
_REQUIRED = {
    "p:sld": ("p:cSld",), "p:sldLayout": ("p:cSld",), "p:sldMaster": ("p:cSld", "p:clrMap"),
    "p:notes": ("p:cSld",), "p:cSld": ("p:spTree",),
    "p:spTree": ("p:nvGrpSpPr", "p:grpSpPr"), "p:grpSp": ("p:nvGrpSpPr", "p:grpSpPr"),
    "p:nvGrpSpPr": ("p:cNvPr", "p:cNvGrpSpPr", "p:nvPr"),
    "p:sp": ("p:nvSpPr", "p:spPr"), "p:nvSpPr": ("p:cNvPr", "p:cNvSpPr", "p:nvPr"),
    "p:pic": ("p:nvPicPr", "p:blipFill", "p:spPr"), "p:nvPicPr": ("p:cNvPr", "p:cNvPicPr", "p:nvPr"),
    "p:graphicFrame": ("p:nvGraphicFramePr", "p:xfrm", "a:graphic"),
    "p:nvGraphicFramePr": ("p:cNvPr", "p:cNvGraphicFramePr", "p:nvPr"),
    "p:cxnSp": ("p:nvCxnSpPr", "p:spPr"), "p:nvCxnSpPr": ("p:cNvPr", "p:cNvCxnSpPr", "p:nvPr"),
    "p:txBody": ("a:bodyPr", "a:p"), "a:graphic": ("a:graphicData",), "a:tbl": ("a:tblGrid",),
    "a:theme": ("a:themeElements",), "a:themeElements": ("a:clrScheme", "a:fontScheme", "a:fmtScheme"),
    "a:clrScheme": ("a:dk1", "a:lt1", "a:dk2", "a:lt2", "a:accent1", "a:accent2", "a:accent3", "a:accent4",
                    "a:accent5", "a:accent6", "a:hlink", "a:folHlink"),
    "a:fontScheme": ("a:majorFont", "a:minorFont"), "a:majorFont": ("a:latin", "a:ea", "a:cs"),
    "a:minorFont": ("a:latin", "a:ea", "a:cs"),
}
# Lists the schema needs at least one child in.
_NON_EMPTY = ("p:tnLst", "p:childTnLst", "p:subTnLst", "p:bldLst", "p:stCondLst", "p:endCondLst",
              "p:prevCondLst", "p:nextCondLst", "p:sldIdLst", "a:gsLst")
_CHECKED_NAMESPACES = {"p": pptx_animations.P_NS, "a": pptx_animations.A_NS}


def _clark(prefixed: str) -> str:
    prefix, _, local = prefixed.partition(":")
    return f"{{{_CHECKED_NAMESPACES[prefix]}}}{local}"


_REQUIRED_CLARK = {_clark(k): tuple(_clark(c) for c in v) for k, v in _REQUIRED.items()}
_NON_EMPTY_CLARK = frozenset(_clark(n) for n in _NON_EMPTY)


def _short(tag: str) -> str:
    namespace, _, local = tag[1:].partition("}")
    prefix = next((p for p, uri in patch.namespaces().items() if uri == namespace), None)
    return f"{prefix}:{local}" if prefix else tag


def _structure(root: Any) -> set[tuple[str, str]]:
    """Each (problem, element) the part shows: a required child missing or
    a list left empty."""
    found = set()
    for element in root.iter():
        tag = element.tag
        if not isinstance(tag, str):
            continue
        children = [c.tag for c in element if isinstance(c.tag, str)]
        for needed in _REQUIRED_CLARK.get(tag, ()):
            if needed not in children:
                found.add((f"<{_short(tag)}> needs a <{_short(needed)}>", _short(tag)))
        if tag in _NON_EMPTY_CLARK and not children:
            found.add((f"<{_short(tag)}> needs at least one child", _short(tag)))
    return found


def _owner(part: Any) -> Any | None:
    """The python-pptx object a slide, notes, layout or master part
    carries; None for a theme."""
    from pptx.parts.slide import (
        NotesSlidePart,
        SlideLayoutPart,
        SlideMasterPart,
        SlidePart,
    )

    for kind, name in ((SlidePart, "slide"), (NotesSlidePart, "notes_slide"), (SlideLayoutPart, "slide_layout"),
                       (SlideMasterPart, "slide_master")):
        if isinstance(part, kind):
            return getattr(part, name)
    return None


def _forget(part: Any) -> None:
    """Drop what python-pptx cached from the part's tree (its shapes,
    placeholders, background, a master's layout list): a cache made before
    a patch holds elements the patch may have taken out."""
    from pptx.util import lazyproperty

    owner = _owner(part)
    if owner is None:
        return
    for kind in type(owner).__mro__:
        for name, value in vars(kind).items():
            if isinstance(value, lazyproperty):
                owner.__dict__.pop(name, None)


def _said(exc: BaseException) -> str:
    """An exception's message with namespace URIs as their prefixes."""
    import re

    text = str(exc) or type(exc).__name__
    return re.sub(r"\{([^}]*)\}(\w+)", lambda m: _short(m.group(0)), text).rstrip(".")


def _frame(shape: Any) -> Any:
    """What a graphic frame holds, read as format_chart and the table
    operations read it."""
    if shape.has_chart:
        return len(shape.chart.plots)
    if shape.has_table:
        return len(shape.table.rows), len(shape.table.columns)
    return None


# What the operations read of every shape: its id, name and turn, its box,
# its kind (the shrink step reads it), its text, its placeholder and, for a
# graphic frame, its chart or table.
_READS = (lambda s: (s.shape_id, s.name, s.rotation), lambda s: (s.left, s.top, s.width, s.height),
          lambda s: s.shape_type, lambda s: s.text_frame.text if s.has_text_frame else None,
          lambda s: (s.placeholder_format.type, s.placeholder_format.idx) if s.is_placeholder else None,
          lambda s: _frame(s) if s._element.tag == f"{{{pptx_animations.P_NS}}}graphicFrame" else None)


def _reading(part: Any) -> set[str]:
    """What python-pptx cannot read in the part's shapes (groups' members
    included), one line each, and a master that lists no layout: a later
    operation of this or another call would fail on it."""
    owner = _owner(part)
    if owner is None:
        return set()
    found = set()
    if owner.part.content_type.endswith("slideMaster+xml"):
        # Read from the XML: python-pptx's slide_layouts adds the list it misses.
        held = owner._element.find(f"{{{pptx_animations.P_NS}}}sldLayoutIdLst")
        if held is None or not len(held):
            found.add("the master lists no layout, so no slide can be added with it")
    try:
        shapes = list(owner.shapes) + list(owner.placeholders)
    except Exception as exc:  # noqa: BLE001  # python-pptx on a patched tree raises anything from its parser
        return found | {f"its shapes cannot be read ({_said(exc)})"}
    while shapes:
        shape = shapes.pop()
        cnvpr = next(shape._element.iter(f"{{{pptx_animations.P_NS}}}cNvPr"), None)
        label = f"shape id {cnvpr.get('id') if cnvpr is not None else '?'}"
        for read in _READS:
            try:
                read(shape)
            except Exception as exc:  # noqa: BLE001  # python-pptx on a patched tree raises anything from its parser
                found.add(f"{label} cannot be read ({_said(exc)})")
        if shape._element.tag == f"{{{pptx_animations.P_NS}}}grpSp":
            try:
                shapes.extend(shape.shapes)
            except Exception as exc:  # noqa: BLE001  # python-pptx on a patched tree raises anything from its parser
                found.add(f"{label} cannot be read ({_said(exc)})")
    return found


def _shape_ids(slide: Any) -> set[int]:
    return pptx_animations.shape_ids(slide._element.find(f"{{{pptx_animations.P_NS}}}cSld"))


def _op_xml_patch(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    try:
        _patch(job, ctx, op)
    except OpError as exc:
        # Every refusal leaves the deck as it was; the batch's verdict reads
        # this to count it as a failure that changed nothing.
        exc.unchanged = True
        raise


def _read_back(part: Any, where: str, structure_before: set[tuple[str, str]], reading_before: set[str],
               root: Any) -> None:
    """The patched part read as the later operations read it; a problem it
    did not have before refuses the patch (the guard puts it back)."""
    _forget(part)
    name = str(part.partname).lstrip("/")
    try:
        broken = [problem for problem, _ in sorted(_structure(root) - structure_before)]
        broken += sorted(_reading(part) - reading_before)
    except Exception as exc:  # noqa: BLE001  # python-pptx on a patched tree raises anything from its parser
        broken = [_said(exc)]
    if broken:
        raise OpError(Code.BAD_VALUE, f"the patch leaves {name} broken: {broken[0]}, which PowerPoint repairs; "
                      "nothing in it changed", location=where,
                      hint="Keep every element and attribute its parent needs, and put in only elements the "
                           "parent holds.")


def _patch(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    part, slides, where = _target(job, op)
    root = parts.xml_root(part)
    patched = next((s for s in slides if s.part is part), None)
    ids_before = _shape_ids(patched) if patched is not None else set()
    check = functools.partial(_read_back, part, where, _structure(root), _reading(part))
    try:
        result = patch.apply_patch(job.prs.part.package, str(part.partname), op.get("xpath", ""),
                                   op.get("action", ""), op.get("xml"), match=op.get("match", patch.MATCH_ONE),
                                   verify=check)
    except OpError as exc:
        _forget(part)
        if not exc.location:
            exc.location = where
        raise
    ctx.note(f"{_DONE[result.action]} {plural(result.matched, 'element')} in {result.part} ({where})")
    for line in result.notes:
        ctx.note(line)
    gone = ids_before - _shape_ids(patched) if patched is not None else set()
    if gone:
        note = pptx_animations.removal_note(pptx_animations.drop_animations(patched._element, gone),
                                            pptx_animations.drop_glue(patched._element, gone))
        if note:
            ctx.note(f"shape id {', '.join(map(str, sorted(gone)))} is gone or renumbered: {note}")
    for slide in slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                job.touch(slide, shape)


_CARD = {"op": "add_shape", "slide_index": 0, "name": "Badge", "text": "Badge", "left": 1, "top": 1, "width": 3,
         "height": 1.5}

# A layout drawn dark: its text and background slots swapped.
_DARK = ('<a:overrideClrMapping bg1="dk1" tx1="lt1" bg2="dk2" tx2="lt2" accent1="accent1" accent2="accent2" '
         'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>')

OPS: tuple[OpSpec, ...] = (
    OpSpec("xml_patch", _op_xml_patch, (
        KeySpec("part", ("str",), default="slide", help=" | ".join(PARTS)),
        KeySpec("slide_index", ("int",), help="0-based, -1 = last: the slide or its notes, or the layout, master "
                                              "or theme it uses"),
        KeySpec("layout", ("str", "int"), help="part layout: a layout name or number, as read_document lists them"),
        KeySpec("master", ("int",), help="part master or theme: 1-based; default the slide's, else the first"),
        KeySpec("xpath", ("str",), required=True,
                help="XPath 1.0 selecting elements, with the usual prefixes (p, a, r, p14, c...)"),
        KeySpec("action", ("str",), required=True, help=" | ".join(patch.ACTIONS)),
        KeySpec("xml", ("str",), help="the element(s) to put in; every action but remove"),
        KeySpec("match", ("str",), default=patch.MATCH_ONE, help="one | all (the XPath may match several)"),
    ), G_XML, note="edits one part's XML where no other operation reaches; reach a shape by the name or id "
                    "read_document shows: //p:sp[p:nvSpPr/p:cNvPr/@name='Card']",
        detail="It refuses, leaving the part as it was: fields; anything that runs, fetches or embeds something "
               "when the file opens (macros, OLE and ActiveX objects, linked pictures, slide actions that run a "
               "program, outside references from anything but a hyperlink); Strict-conformance XML; a new "
               "relationship id; elements out of the schema's order; an element its parent never holds (an "
               "extension list or mc:AlternateContent holds any); a required child removed; a shape that can no "
               "longer be read; the part's root element; any part but these five. The XPath is one path with at "
               "most one descendant search and simple predicates, and its cost is bounded on large parts. Taken "
               "shape ids are renumbered; a removed shape's animations and connector glue go with it. Operations "
               "after it in the batch see the patched part.",
        setup=(_CARD, {**_CARD, "name": "Second", "text": "Second", "top": 3},
               {"op": "set_speaker_notes", "slide_index": 0, "text": "Lead with the number."}),
        examples=(
            {"op": "xml_patch", "slide_index": 0, "xpath": "//p:sp[p:nvSpPr/p:cNvPr/@name='Badge']/p:spPr/a:prstGeom",
             "action": "replace", "xml": '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'},
            {"op": "xml_patch", "slide_index": 0, "xpath": "//p:sp[p:nvSpPr/p:cNvPr/@name='Badge']/p:spPr",
             "action": "insert_child",
             "xml": '<a:ln w="38100"><a:solidFill><a:schemeClr val="accent2"/></a:solidFill></a:ln>'},
            {"op": "xml_patch", "slide_index": 0, "xpath": "//p:sp[p:txBody/a:p/a:r/a:t='Second']",
             "action": "remove"},
            {"op": "xml_patch", "part": "notes", "slide_index": 0, "xpath": "//a:r/a:t", "action": "replace",
             "xml": "<a:t>Lead with the margin.</a:t>", "match": "all"},
            {"op": "xml_patch", "part": "layout", "layout": "blank", "xpath": "//p:clrMapOvr/a:masterClrMapping",
             "action": "replace", "xml": _DARK},
            {"op": "xml_patch", "part": "theme", "master": 1, "xpath": "//a:clrScheme/a:accent1/a:srgbClr",
             "action": "replace", "xml": '<a:srgbClr val="1F4E79"/>'},
        )),
)
