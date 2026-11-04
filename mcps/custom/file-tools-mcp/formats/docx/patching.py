"""The last resort: a guarded XML patch of one part of the document.

``xml_patch`` hands an XPath and a fragment to ``ooxml.patch.apply_patch``,
which refuses what a patch must never do (the package's plumbing, fields
and anything inside a field's code, active content, Strict-conformance XML,
a part with an element of more than 256 attributes, an XPath that would
cost too much on the part) and leaves the part exactly as it was when it
refuses. A part is named (``styles``, ``header0``, ``word/numbering.xml``)
or reached through ``target``, an address: the XPath then starts at that
block (``./w:pPr``), is joined to the block's own path from the part's
root, and every element it selects must be the block or inside it; the
grammar already refuses every step up (``..``, ``parent::``, ``|``), and
the selection is checked again before anything changes.

``apply_patch`` does not check attribute values, required attributes or
what python-docx still reads, so the part is read back inside it (its
``verify``): every typed attribute python-docx declares, the children an
element must hold, the read of the part's stories, and every note and
comment with its mark in the text. A problem the part did not have before
is the refusal, and the guard puts the part back with the very same element
objects (and, for a part kept as bytes, its bytes): the later operations of
the call work through python-docx objects that hold those elements.
"""

import functools
from typing import Any

from catalog import KeySpec, OpSpec
from formats.docx import address as addr, annotations
from formats.docx.opkit import EXPECT, TARGET
from formats.docx.targets import locate
from results import Code, OpCtx, OpError

_W = f"{{{annotations.W_NS}}}"
_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
# Parts by the names people use, through the document part's relationships.
_NAMED_PARTS = {
    "styles": _RT + "styles",
    "numbering": _RT + "numbering",
    "settings": _RT + "settings",
    "fonttable": _RT + "fontTable",
    "theme": _RT + "theme",
    "footnotes": annotations.RT_FOOTNOTES,
    "endnotes": annotations.RT_ENDNOTES,
    "comments": annotations.RT_COMMENTS,
}
# Parts whose paragraphs take ids at the end of the call.
_STORY_ROOTS = frozenset(_W + tag for tag in ("document", "hdr", "ftr", "footnotes", "endnotes", "comments"))
_PATCH_KINDS = (addr.PARAGRAPH, addr.TABLE, addr.ROW, addr.CELL, addr.CONTROL, addr.CUSTOM, addr.PART)


def _named_part(doc: Any, name: str) -> Any:
    """The part ``name`` names (a short name, a header or footer address:
    header0, footer1.first), or ``name`` itself, a part name in the package
    (word/styles.xml) that apply_patch looks up and judges."""
    key = name.strip()
    folded = key.casefold()
    if folded in ("document", "body"):
        return doc.part
    if folded in _NAMED_PARTS:
        part = annotations.first_related(doc, _NAMED_PARTS[folded])
        if part is None:
            raise OpError(Code.NOT_FOUND, f"the document has no {key} part", location=f"part {key}",
                          hint="A patch never creates a part: use the operation that makes it (add_footnote, "
                               "add_comment, add_header ...).")
        return part
    if folded.startswith(("header", "footer")) and "/" not in folded:
        located = addr.root_of(doc, addr.parse(folded))
        return located[0]
    return key


def _step(element: Any, prefixes: dict[str, str]) -> str:
    """One step of an element's path from its part's root: its prefixed
    name and position among siblings of that name, or ``*[n]`` among all
    elements when its namespace has no prefix the patch knows."""
    uri, _, local = element.tag[1:].partition("}")
    parent = element.getparent()
    prefix = prefixes.get(uri)
    if parent is None:
        return f"{prefix}:{local}" if prefix else "*"
    if prefix:
        same = [c for c in parent.iterchildren(element.tag)]
        return f"{prefix}:{local}[{same.index(element) + 1}]"
    every = [c for c in parent.iterchildren() if isinstance(c.tag, str)]
    return f"*[{every.index(element) + 1}]"


def block_path(element: Any) -> str:
    """The absolute XPath of ``element`` in its part: one child step per
    level, each with its position, so it names that element and no other."""
    from ooxml import patch

    prefixes = {uri: prefix for prefix, uri in patch.namespaces().items()}
    chain = [element, *element.iterancestors()]
    return "/" + "/".join(_step(el, prefixes) for el in reversed(chain))


def _confined(root: Any, block: Any, xpath: str) -> None:
    """Refuse, before any change, an XPath that selects anything but the
    block or what is inside it."""
    from lxml import etree

    from ooxml import patch

    patch.check_xpath(xpath, *patch.part_size(root))
    try:
        found = etree.XPath(xpath, namespaces=patch.namespaces(), regexp=False, smart_strings=False)(root)
    except etree.XPathError:
        return  # apply_patch says what is wrong with it
    if not isinstance(found, list):
        return
    for node in found:
        if not isinstance(getattr(node, "tag", None), str):
            continue
        if node is not block and not any(a is block for a in node.iterancestors()):
            raise OpError(Code.BAD_VALUE, "the xpath selects an element outside the block it starts from",
                          hint="Start the XPath with ./ and keep it inside the target block.")


_W_TC = _W + "tc"
# Word repairs a table cell that holds no paragraph.
_WORD_NEEDS_ONE_OF = {_W_TC: _W + "p"}
_DONE = {"replace": "replaced", "remove": "removed", "insert_before": "inserted before",
         "insert_after": "inserted after", "insert_child": "inserted into"}


@functools.cache
def _typed(cls: type) -> tuple[tuple[str, str], ...]:
    """(property, attribute) for each typed attribute python-docx declares
    on an element class: reading the property parses the value, so a value
    of the wrong type or a required attribute missing raises."""
    from docx.oxml.xmlchemy import BaseAttribute

    found: dict[str, str] = {}
    for klass in cls.__mro__:
        for name, value in vars(klass).items():
            getter = value.fget if isinstance(value, property) else None
            for cell in getattr(getter, "__closure__", None) or ():
                try:
                    held = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(held, BaseAttribute):
                    found.setdefault(name, held._attr_name)
    return tuple(found.items())


def _short(tag: str) -> str:
    from ooxml import patch

    uri, _, local = tag[1:].partition("}")
    prefix = next((p for p, u in patch.namespaces().items() if u == uri), None)
    return f"{prefix}:{local}" if prefix else local


def _structure(root: Any) -> dict[tuple[str, str], str]:
    """Each attribute python-docx cannot read and each element missing a
    child it must hold, keyed by the element's path."""
    from formats.docx.read import said
    from ooxml import order, validate

    found: dict[tuple[str, str], str] = {}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for prop, name in _typed(type(element)):
            try:
                getattr(element, prop)
            except Exception as exc:  # noqa: BLE001  # python-docx on a patched tree raises anything from its parser
                found[(block_path(element), name)] = f"<{_short(element.tag)}> {name} cannot be read ({said(exc)})"
        needed = validate.NEEDS_ONE_OF.get(element.tag) or _WORD_NEEDS_ONE_OF.get(element.tag)
        if needed is not None and next(element.iter(needed), None) is None:
            found[(block_path(element), "")] = f"<{_short(element.tag)}> holds no <{_short(needed)}>"
        elif needed is None and order.needs_children(element) and \
                not any(isinstance(c.tag, str) for c in element):
            found[(block_path(element), "")] = f"<{_short(element.tag)}> is empty where it needs a child"
    return found


def _pairs(doc: Any) -> dict[tuple[str, str], str]:
    """Each note or comment without its mark in the text, and each mark
    without its note or comment."""
    kinds = (addr.FOOTNOTE, addr.ENDNOTE, addr.COMMENT)
    marks: dict[str, set[str]] = {kind: set() for kind in kinds}
    ranges: set[str] = set()
    held: dict[str, set[str]] = {kind: set() for kind in kinds}
    for part in annotations.story_parts(doc):
        root = annotations.root_of_part(part)
        if root is None:
            continue
        for element in root.iter(*annotations.REFERENCES):
            marks[annotations.REFERENCES[element.tag]].add(element.get(annotations.W_ID) or "")
        ranges.update(element.get(annotations.W_ID) or "" for element in root.iter(_W + "commentRangeStart"))
        for kind in kinds:
            for element in root.iterchildren(_W + kind):
                if (element.get(_W + "type") or "normal") == "normal":
                    held[kind].add(element.get(annotations.W_ID) or "")
    found = {}
    for kind in kinds:
        anchored = marks[kind] | ranges if kind == addr.COMMENT else marks[kind]
        for missing in sorted(anchored - held[kind]):
            found[(kind, f"mark {missing}")] = f"{kind} {missing}'s mark in the text without its {kind}"
        for missing in sorted(held[kind] - marks[kind]):
            found[(kind, f"text {missing}")] = f"{kind} {missing} without its mark in the text"
    return found


def _problems(doc: Any, part: Any, root: Any) -> dict[tuple[str, str], str]:
    from formats.docx.read import unreadable

    found = {("structure",) + key: text for key, text in _structure(root).items()}
    found.update({("read", line): line for line in unreadable(doc, part)})
    found.update({("pair",) + key: text for key, text in _pairs(doc).items()})
    return found


def _resolved(doc: Any, part: Any) -> tuple[Any, Any] | None:
    """The part and its XML when a patch may reach it; None leaves the
    refusal to apply_patch, in its own words."""
    from ooxml import parts, patch

    try:
        if isinstance(part, str):
            patch.refuse_name(part.strip().lstrip("/"))
            part = patch.find_part(doc.part.package, part)
        return part, parts.xml_root(part)
    except (OpError, ValueError):
        return None


def _read_back(doc: Any, part: Any, name: str, before: dict[tuple[str, str], str], root: Any) -> None:
    """The patched part read as Word's later operations read it; a problem
    it did not have ``before`` refuses the patch (the guard puts it back)."""
    from formats.docx.read import said

    try:
        new = [text for key, text in _problems(doc, part, root).items() if key not in before]
    except Exception as exc:  # noqa: BLE001  # python-docx on a patched tree raises anything from its parser
        new = [f"it cannot be read ({said(exc)})"]
    if not new:
        return
    if any(" mark in the text" in text for text in new):
        raise OpError(Code.BAD_VALUE, f"the patch leaves {new[0]}; nothing in {name.lstrip('/')} changed",
                      hint="Remove a note with delete_note and a comment with delete_comment: each takes its "
                           "mark and its text together.")
    raise OpError(Code.BAD_VALUE, f"the patch leaves {name.lstrip('/')} broken: {new[0]}, which Word repairs "
                  "or refuses; nothing in it changed",
                  hint="Keep every attribute an element needs, with a value of its type (a number where Word "
                       "wants one), and every child it must hold.")


def _op_xml_patch(job: Any, ctx: OpCtx, op: dict) -> None:
    from ooxml import parts, patch

    doc = job.doc
    xpath = op["xpath"].strip()
    if op.get("target") is not None:
        if op.get("part") is not None:
            raise OpError(Code.BAD_VALUE, "give target or part, not both",
                          hint="target names a block (its part follows); part names a whole part.")
        if not (xpath == "." or xpath.startswith("./")):
            raise OpError(Code.BAD_VALUE, f"with target, the xpath starts at the block: {xpath!r} does not start "
                          "with ./", hint="Give a path inside the block, such as ./w:pPr or .//w:t.")
        located = locate(job.book, ctx, op, kinds=_PATCH_KINDS)
        part, block = located.part, located.element
        xpath = block_path(block) + xpath[1:]
        root = parts.xml_root(part)
        _confined(root, block, xpath)
    else:
        part = _named_part(doc, op.get("part") or "document")
    name = part if isinstance(part, str) else str(part.partname)
    resolved = _resolved(doc, part)
    if resolved is None:
        patch.apply_patch(doc.part.package, name, xpath, op["action"], op.get("xml"),
                          match=op.get("match", patch.MATCH_ONE))
        raise OpError(Code.BAD_VALUE, f"{name} cannot be patched", hint="Patch an XML part of the document.")
    part, root = resolved
    before = _problems(doc, part, root)
    tracked = [found.element for found in job.book.snapshot.values()
               if isinstance(found, addr.Located) and found.part is part and found.kind != addr.PART]
    result = patch.apply_patch(doc.part.package, name, xpath, op["action"], op.get("xml"),
                               match=op.get("match", patch.MATCH_ONE),
                               verify=functools.partial(_read_back, doc, part, name, before))
    if root.tag in _STORY_ROOTS:
        job.book.touch(part)
    for element in tracked:
        if id(element) not in job.book.removed and not job.book.attached(part, element):
            job.book.removed[id(element)] = ctx.index
    # The patch may have written ids, list definitions or theme colours the
    # call has read already.
    job.forget()
    from formats.docx.format import forget_theme

    forget_theme(doc.part)
    ctx.note(f"{_DONE[result.action]} {result.matched} element{'s' if result.matched != 1 else ''} in "
             f"{result.part}")
    for note in result.notes:
        ctx.note(note)


def _ops() -> list[OpSpec]:
    paragraph = ({"op": "add_heading", "text": "Payment terms", "level": 1},
                 {"op": "add_paragraph", "text": "Within 45 days."})
    return [
        OpSpec("xml_patch", _op_xml_patch, (
            KeySpec("xpath", ("str",), required=True, help="XPath 1.0 with the usual prefixes (w, a, r, wp, mc); "
                                                           "with target it starts with ./"),
            KeySpec("action", ("str",), required=True,
                    help="replace | remove | insert_before | insert_after | insert_child"),
            KeySpec("xml", ("str",), help="the fragment to put in, prefixes as in the xpath"),
            KeySpec("match", ("str",), default="one", help="one (exactly one element) | all"),
            KeySpec("part", ("str",), default="document",
                    help="document, styles, numbering, settings, fontTable, theme, footnotes, endnotes, comments, "
                         "header0, footer1.first, or a part name such as word/styles.xml"),
            TARGET, EXPECT,
        ), "XML", note="the last resort when no operation does it; read_document shows a block's address and text, "
                 "its XML follows the Word schema (WordprocessingML)",
            detail="Refused, with nothing changed: the package's plumbing and parts that hold code or custom "
                   "data; adding, changing or removing a field, or anything inside a field's code; anything that "
                   "runs, fetches or embeds when the file opens; Strict-conformance XML; a part with an element "
                   "of more than 256 attributes; with target, anything outside the target block; an XPath that "
                   "steps up (.., parent::), joins paths (|) or would cost too much (a very large part allows "
                   "only short XPaths); a fragment out of the schema's order; a relationship id the part lacks.",
            setup=paragraph,
            examples=({"op": "xml_patch", "part": "styles",
                       "xpath": "//w:style[@w:styleId='Heading1']/w:rPr", "action": "insert_child",
                       "xml": "<w:caps/>"},
                      {"op": "xml_patch", "target": "p0", "xpath": "./w:pPr", "action": "insert_child",
                       "xml": "<w:keepNext/>", "expect_text": "Payment terms"})),
    ]


OPS = _ops()
