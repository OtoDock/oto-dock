"""Paragraph ids: Word's ``w14:paraId`` and ``w14:textId``.

An id is 8 hex digits for a value in 1..0x7FFFFFFF. A paraId is unique
across the whole package; Word gives a paragraph a fresh textId when its
text changes and keeps its paraId. A paragraph under ``mc:Fallback`` is a
copy of one in the matching ``mc:Choice`` and carries the same ids: it is
never addressed and never renumbered. Headers and footers are reached
through the section references, never through python-docx's
``section.header`` and ``section.footer``, which make a part for a section
that has none.
"""

import random
import re
from typing import Any

from lxml import etree

W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

_W_P = _W + "p"
_W_SECT_PR = _W + "sectPr"
_HDR_FTR_REFS = (_W + "headerReference", _W + "footerReference")
_PARA_ID = f"{{{W14_NS}}}paraId"
_TEXT_ID = f"{{{W14_NS}}}textId"
_MC_FALLBACK = f"{{{MC_NS}}}Fallback"
_MC_IGNORABLE = f"{{{MC_NS}}}Ignorable"

_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_RT_HDR_FTR = frozenset({_RT + "header", _RT + "footer"})
_RT_ID_PARTS = _RT_HDR_FTR | {
    _RT + "footnotes", _RT + "endnotes", _RT + "comments", _RT + "glossaryDocument",
    "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
}
# Roots a live paragraph sits under; a removed one sits under anything else.
_STORY_ROOTS = frozenset(_W + tag for tag in (
    "document", "hdr", "ftr", "footnotes", "endnotes", "comments", "glossaryDocument",
))

_ID_VALUES = etree.XPath(
    " | ".join(f"descendant-or-self::*/@{name}" for name in (
        "w14:paraId", "w14:textId", "w15:paraId", "w15:paraIdParent")),
    namespaces={"w14": W14_NS, "w15": W15_NS},
)
_HEX8 = re.compile(r"[0-9A-Fa-f]{8}")
_MAX_ID = 0x7FFFFFFF
_TEXT_ID_PLACEHOLDER = 0x77777777


def is_valid_id(value: str | None) -> bool:
    """8 hex digits (either case) for a value in 1..0x7FFFFFFF."""
    return value is not None and _HEX8.fullmatch(value) is not None and 0 < int(value, 16) <= _MAX_ID


def _xml_root(part: Any) -> Any:
    """The root of a part's XML: python-docx's own for an XmlPart, else parsed
    read-only from the bytes. None when the bytes are not XML."""
    element = getattr(part, "element", None)
    if element is not None:
        return element
    try:
        return etree.fromstring(part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return None


def _is_live(p: Any) -> bool:
    ancestors = list(p.iterancestors())
    return (ancestors[-1] if ancestors else p).tag in _STORY_ROOTS


class IdAllocator:
    """Fresh paragraph ids for one package: random values in 0x00000001..0x7FFFFFFF,
    never one already used anywhere in the package (paraIds and textIds of the
    document, every header and footer, footnotes, endnotes, comments and the
    glossary, and commentsExtended's w15:paraId and w15:paraIdParent), never one
    it handed out before, never the textId placeholder 77777777.

    The package is read on the first ``new``, so ids written before then count.
    ``rng`` needs only ``randint``.
    """

    def __init__(self, doc: Any, *, rng: random.Random | None = None) -> None:
        self._doc = doc
        self._rng = rng if rng is not None else random.Random()
        self._used: set[int] = {_TEXT_ID_PLACEHOLDER}
        self._owners: dict[int, Any] = {}
        self._scanned = False

    def _scan(self) -> None:
        self._scanned = True
        part = self._doc.part
        roots = [part.element]
        for rel in part.rels.values():
            if not rel.is_external and rel.reltype in _RT_ID_PARTS:
                roots.append(_xml_root(rel.target_part))
        for root in roots:
            if root is not None:
                self._used.update(int(v, 16) for v in _ID_VALUES(root) if _HEX8.fullmatch(v))

    def new(self) -> str:
        """A fresh id, upper-case: "1A2B3C4D"."""
        if not self._scanned:
            self._scan()
        while True:
            value = self._rng.randint(1, _MAX_ID)
            if value not in self._used:
                self._used.add(value)
                return f"{value:08X}"

    def reserve(self, value: str) -> None:
        """Never hand out ``value``. A value that is not 8 hex digits is ignored."""
        if _HEX8.fullmatch(value):
            self._used.add(int(value, 16))

    def _claim(self, value: str, p: Any) -> bool:
        """Whether ``p`` may keep ``value``: no other paragraph that ``stamp_part``
        kept it on still holds it."""
        key = int(value, 16)
        owner = self._owners.get(key)
        if owner is not None and owner is not p and paragraph_id(owner) == f"{key:08X}" and _is_live(owner):
            return False
        self._owners[key] = p
        self._used.add(key)
        return True


def paragraph_id(p: Any) -> str | None:
    """The paragraph's valid paraId, upper-case, else None."""
    value = p.get(_PARA_ID)
    return value.upper() if is_valid_id(value) else None


def is_fallback_copy(element: Any) -> bool:
    """Whether ``element`` sits under an ``mc:Fallback``."""
    return next(element.iterancestors(_MC_FALLBACK), None) is not None


def stamp(p: Any, alloc: IdAllocator) -> str:
    """Give the ``w:p`` a paraId and a textId where it lacks a valid one; returns
    its paraId, upper-case.

    A valid paraId is kept even when another paragraph shares it (``stamp_part``
    settles duplicates). The part's root must declare w14
    (``ensure_w14_namespace``), or lxml writes the attributes with a made-up
    prefix.
    """
    value = paragraph_id(p)
    if value is None:
        value = alloc.new()
        p.set(_PARA_ID, value)
    else:
        alloc.reserve(value)
    if not is_valid_id(p.get(_TEXT_ID)):
        p.set(_TEXT_ID, alloc.new())
    return value


def new_text_id(p: Any, alloc: IdAllocator) -> None:
    """The paragraph's text changed: a fresh textId. The paraId, present or not,
    is left as it is."""
    p.set(_TEXT_ID, alloc.new())


def stamp_part(root: Any, alloc: IdAllocator) -> int:
    """Stamp every ``w:p`` under ``root`` but fallback copies; returns how many
    paragraphs changed.

    A missing, malformed or out-of-range paraId gets a fresh one, and so does a
    duplicate: the first paragraph in document order keeps an id, counting
    paragraphs of parts stamped earlier through the same allocator. Stamping a
    part again changes nothing. A missing or invalid textId gets a fresh one.
    Fallback copies are left exactly as they are.
    """
    changed = 0
    for p in root.iter(_W_P):
        if is_fallback_copy(p):
            continue
        before = (p.get(_PARA_ID), p.get(_TEXT_ID))
        value = paragraph_id(p)
        if value is None or not alloc._claim(value, p):
            value = alloc.new()
            p.set(_PARA_ID, value)
            alloc._claim(value, p)
        if not is_valid_id(p.get(_TEXT_ID)):
            p.set(_TEXT_ID, alloc.new())
        changed += (p.get(_PARA_ID), p.get(_TEXT_ID)) != before
    return changed


_RT_ANNOTATIONS = (_RT + "comments", _RT + "footnotes", _RT + "endnotes")


def part_roots(doc: Any) -> list[tuple[Any, Any]]:
    """(part, root element) for the document part, then each distinct header
    and footer part the sections reference, in document order, then the
    comments, footnotes and endnotes parts, each part once.

    Reads the references only: no part is ever made. A reference to a missing
    or non-XML part is skipped.
    """
    part = doc.part
    root = part.element
    found = [(part, root)]
    seen = {id(part)}
    for sect_pr in root.iter(_W_SECT_PR):
        for ref in sect_pr.iterchildren(*_HDR_FTR_REFS):
            rel = part.rels.get(ref.get(_R_ID))
            if rel is None or rel.is_external or rel.reltype not in _RT_HDR_FTR:
                continue
            target = rel.target_part
            element = getattr(target, "element", None)
            if element is not None and id(target) not in seen:
                seen.add(id(target))
                found.append((target, element))
    for reltype in _RT_ANNOTATIONS:
        for rel in part.rels.values():
            if rel.is_external or rel.reltype != reltype:
                continue
            element = getattr(rel.target_part, "element", None)
            if element is not None and id(rel.target_part) not in seen:
                seen.add(id(rel.target_part))
                found.append((rel.target_part, element))
    return found


def stamping_order(parts: list[Any]) -> list[Any]:
    """Touched parts in the order their paragraphs are stamped: comments
    first, since ``commentsExtended`` and ``commentsIds`` name a comment's
    paragraph by its id and the first holder of a duplicate keeps it."""
    def rank(part: Any) -> int:
        root = getattr(part, "element", None)
        return 0 if root is not None and root.tag == _W + "comments" else 1

    return sorted(parts, key=rank)


def find_by_id(doc: Any, value: str) -> list[tuple[Any, Any]]:
    """Every (part, ``w:p``) whose paraId equals ``value`` (case-insensitive) in
    the body, headers, footers, notes and comments, fallback copies excluded. An invalid
    ``value`` finds nothing."""
    if not is_valid_id(value):
        return []
    wanted = value.upper()
    return [
        (part, p)
        for part, root in part_roots(doc)
        for p in root.iter(_W_P)
        if paragraph_id(p) == wanted and not is_fallback_copy(p)
    ]


def _prefix_for(nsmap: dict[str | None, str], uri: str, preferred: str) -> str:
    """The prefix ``nsmap`` binds to ``uri``, else ``preferred`` or a free variant."""
    for prefix, bound in nsmap.items():
        if prefix and bound == uri:
            return prefix
    prefix, n = preferred, 0
    while prefix in nsmap:
        n += 1
        prefix = f"{preferred}{n}"
    return prefix


def ensure_w14_namespace(part: Any) -> bool:
    """Make a python-docx XmlPart's root declare w14 and list it in
    ``mc:Ignorable``; returns True when the root was rebuilt.

    lxml cannot add a namespace declaration to an existing element, so the root
    is rebuilt: same tag, attributes and children, w14 (and mc when missing)
    added, ``mc:Ignorable`` keeping its prefixes in order with w14 last. The
    part then holds the new root, and so does every attribute of the part that
    held the old one. A Document or other proxy made from the old root keeps
    that root, now empty: take ``part.document`` again. A part with both is
    untouched.
    """
    from docx.oxml.parser import oxml_parser

    old = part.element
    nsmap = dict(old.nsmap)
    w14 = _prefix_for(nsmap, W14_NS, "w14")
    mc = _prefix_for(nsmap, MC_NS, "mc")
    ignorable = (old.get(_MC_IGNORABLE) or "").split()
    if nsmap.get(w14) == W14_NS and nsmap.get(mc) == MC_NS and w14 in ignorable:
        return False
    nsmap[w14] = W14_NS
    nsmap[mc] = MC_NS
    attrib = dict(old.attrib)
    attrib[_MC_IGNORABLE] = " ".join(ignorable if w14 in ignorable else [*ignorable, w14])
    # The parser's own makeelement keeps python-docx's element classes.
    new = oxml_parser.makeelement(old.tag, attrib=attrib, nsmap=nsmap)
    new.text = old.text
    new.extend(list(old))
    part._element = new
    # Part subclasses keep a second reference (CommentsPart._comments,
    # SettingsPart._settings).
    for name in [name for name, value in vars(part).items() if value is old]:
        setattr(part, name, new)
    return True
