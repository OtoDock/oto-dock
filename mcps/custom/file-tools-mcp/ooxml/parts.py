"""Adding and copying package parts on python-docx and python-pptx packages.

Footnotes, comments, numbering and slide copies each need a part that the
libraries do not make: a new part with its content type and the relationship
that reaches it, or a copy of a part with the parts it owns (a slide's
charts with their workbooks, its notes) and the parts it only points at (its
layout, its pictures) shared. Content types follow each part (both libraries
write ``[Content_Types].xml`` from the parts at save).

A copy keeps its source's relationship ids (a fresh part holds none, so they
never collide), so every reference to one stays right, the ones a part
names in a plain attribute (SmartArt's ``relId``) or in a part we never
parse included; should an id be taken, the copy gets the next one and the
references inside the copied XML (attributes in the relationships
namespace, VML's ``o:relid``, SmartArt's ``relId``) are remapped. Partnames are numbered past every part the package already reaches
and every name this copy has handed out, so the parts of one copy never
collide before the caller relates the top part.
"""

from __future__ import annotations

import posixpath
import re
import secrets
from collections.abc import Callable, Iterable
from typing import Any

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
O_RELID = "{urn:schemas-microsoft-com:office:office}relid"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
PARA_ID = f"{{{W14_NS}}}paraId"
TEXT_ID = f"{{{W14_NS}}}textId"
# w14:paraId and w14:textId are 8 hex digits below 0x80000000.
_ID_LIMIT = 0x80000000

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_MS_REL = "http://schemas.microsoft.com/office/2011/relationships/"
# Parts a copy owns: each copy gets its own. Everything else (layouts,
# masters, pictures, media, styles) is shared with the original.
OWNED_RELTYPES = frozenset({
    _REL + "chart",
    _REL + "chartUserShapes",
    _REL + "package",
    _REL + "oleObject",
    _REL + "notesSlide",
    _REL + "comments",
    _REL + "tags",
    _REL + "themeOverride",
    _REL + "diagramData",
    _REL + "diagramLayout",
    _REL + "diagramQuickStyle",
    _REL + "diagramColors",
    "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing",
    _MS_REL + "chartStyle",
    _MS_REL + "chartColorStyle",
    "http://schemas.microsoft.com/office/2018/10/relationships/comments",
})
DIAGRAM_DATA = _REL + "diagramData"
# A SmartArt data part names its slide's relationship to the diagram's
# drawing in an unprefixed attribute, so a slide copy remaps it by hand.
DATA_MODEL_EXT = "{http://schemas.microsoft.com/office/drawing/2008/diagram}dataModelExt"
_XML_ROOTS = "_xml_roots"
_NUMBERED = re.compile(r"^(.*?)(\d+)(\.[^./]+)$")


def _library(part: Any) -> str:
    return type(part).__module__.split(".", 1)[0]


def _part_classes(part: Any) -> tuple[type, type]:
    """(Part, XmlPart) of the library ``part`` belongs to."""
    if _library(part) == "docx":
        from docx.opc.part import Part, XmlPart
    else:
        from pptx.opc.package import Part, XmlPart
    return Part, XmlPart


def template_of(partname: str) -> str:
    """A ``%d`` template for parts named like ``partname``:
    ``/ppt/charts/chart3.xml`` -> ``/ppt/charts/chart%d.xml``,
    ``/word/footnotes.xml`` -> ``/word/footnotes%d.xml``."""
    match = _NUMBERED.match(partname)
    if match:
        return f"{match.group(1)}%d{match.group(3)}"
    stem, ext = posixpath.splitext(partname)
    return f"{stem}%d{ext}"


def next_partname(package: Any, template: str, taken: Iterable[str] = ()) -> Any:
    """The first ``template % n`` (n from 1) that no part the package reaches
    and no name in ``taken`` uses, as the library's PackURI."""
    used = {str(p.partname) for p in package.iter_parts()} | {str(t) for t in taken}
    n = 1
    while template % n in used:
        n += 1
    return type(next(iter(package.iter_parts())).partname)(template % n)


def remap_rids(element: Any, mapping: dict[str, str]) -> int:
    """Rewrite every relationship id ``element`` holds (attributes in the
    relationships namespace, VML's ``o:relid``) through ``mapping``; the
    number rewritten."""
    count = 0
    prefix = f"{{{R_NS}}}"
    for node in element.iter():
        if not isinstance(node.tag, str):
            continue
        for name, value in list(node.attrib.items()):
            if (name.startswith(prefix) or name == O_RELID) and value in mapping:
                node.set(name, mapping[value])
                count += 1
    return count


def add_part(source: Any, reltype: str, content_type: str, *, template: str, element: Any = None,
             blob: bytes | None = None, taken: Iterable[str] = ()) -> tuple[Any, str]:
    """A new part (from an lxml ``element``, or binary ``blob``) named by
    ``template`` and related from ``source``: ``(part, rId)``."""
    from lxml import etree

    if (element is None) == (blob is None):
        raise ValueError("add_part takes an element or a blob")
    part_class, xml_class = _part_classes(source)
    partname = next_partname(source.package, template, taken)
    if element is not None:
        data = etree.tostring(element, xml_declaration=True, encoding="UTF-8", standalone=True)
        part = xml_class.load(partname=partname, content_type=content_type, blob=data, package=source.package)
    else:
        part = part_class.load(partname=partname, content_type=content_type, blob=blob, package=source.package)
    return part, source.relate_to(part, reltype)


def copy_part(part: Any, *, template: str | None = None, owned: Callable[[str], bool] | None = None,
              copies: dict[int, Any] | None = None, taken: set[str] | None = None) -> Any:
    """A copy of ``part`` in its package: the parts it owns copied too
    (``owned(reltype)``, default ``OWNED_RELTYPES``), the rest shared,
    external relationships kept, a relationship back to a part already
    copied pointed at its copy. The caller relates the returned part."""
    owned = owned or OWNED_RELTYPES.__contains__
    copies = {} if copies is None else copies
    taken = set() if taken is None else taken
    package = part.package
    partname = next_partname(package, template or template_of(str(part.partname)), taken)
    taken.add(str(partname))
    duplicate = type(part).load(partname=partname, content_type=part.content_type, blob=part.blob,
                                package=package)
    copies[id(part)] = duplicate
    mapping: dict[str, str] = {}
    for rid, rel in list(part.rels.items()):
        if rel.is_external:
            mapping[rid] = _relate_as(duplicate, rid, rel.reltype, rel.target_ref, is_external=True)
            continue
        target = rel.target_part
        if id(target) in copies:
            target = copies[id(target)]
        elif owned(rel.reltype):
            target = copy_part(target, owned=owned, copies=copies, taken=taken)
        mapping[rid] = _relate_as(duplicate, rid, rel.reltype, target)
    element = getattr(duplicate, "_element", None)
    if element is not None and any(old != new for old, new in mapping.items()):
        remap_rids(element, mapping)
    for rel in list(duplicate.rels.values()):
        if rel.reltype == DIAGRAM_DATA and not rel.is_external and any(o != n for o, n in mapping.items()):
            _remap_data_model(rel.target_part, mapping)
    return duplicate


def _relate_as(part: Any, rid: str, reltype: str, target: Any, *, is_external: bool = False) -> str:
    """Relate ``part`` to ``target`` under the id ``rid`` when it is free
    (a copy keeps its source's ids, so nothing that names one, in a plain
    attribute or a part we never parse, needs remapping); else under the
    library's next id."""
    rels = part.rels
    if rid in rels:
        return part.relate_to(target, reltype, is_external=is_external)
    if type(rels).__module__.startswith("pptx"):
        from pptx.opc.constants import RELATIONSHIP_TARGET_MODE as RTM
        from pptx.opc.package import _Relationship

        rels._rels[rid] = _Relationship(rels._base_uri, rid, reltype,
                                        target_mode=RTM.EXTERNAL if is_external else RTM.INTERNAL, target=target)
    else:
        rels.add_relationship(reltype, target, rid, is_external=is_external)
    return rid


def _remap_data_model(part: Any, mapping: dict[str, str]) -> None:
    root = xml_root(part)
    changed = False
    for node in root.iter(DATA_MODEL_EXT):
        value = node.get("relId")
        if value in mapping and mapping[value] != value:
            node.set("relId", mapping[value])
            changed = True
    if changed:
        flush_xml_roots(part.package, only=part)


def xml_root(part: Any) -> Any:
    """The parsed XML of ``part``: the library's own element for a part it
    parses, else one parse of the blob per package (hardened: no entities,
    no network, no DTD), shared by everyone who asks, written back by
    ``flush_xml_roots``. A writer that changes such a root flushes before
    its save."""
    element = getattr(part, "_element", None)
    if element is not None:
        return element
    if not _is_xml_type(part.content_type):
        raise ValueError(f"{part.partname} is not an XML part that can be edited")
    holder = part.package if part.package is not None else part
    roots = getattr(holder, _XML_ROOTS, None)
    if roots is None:
        roots = {}
        setattr(holder, _XML_ROOTS, roots)
    key = id(part)
    if key not in roots:
        roots[key] = (part, _parse_blob(part))
    return roots[key][1]


def _parse_blob(part: Any) -> Any:
    from lxml import etree

    import checks

    blob = part.blob or b""
    if not _is_xml_type(part.content_type) or checks.has_doctype(blob[:4096]):
        raise ValueError(f"{part.partname} is not an XML part that can be edited")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                             remove_blank_text=False)
    return etree.fromstring(blob, parser)


def _is_xml_type(content_type: str) -> bool:
    return content_type.endswith("+xml") or content_type.endswith("/xml")


def flush_xml_roots(package: Any, *, only: Any = None) -> int:
    """Write every parsed blob root (or ``only`` that part's) back into its
    part, so the library's save writes the edits; the number written."""
    from lxml import etree

    written = 0
    holder = package if package is not None else only
    for part, root in list(getattr(holder, _XML_ROOTS, {}).values()):
        if only is not None and part is not only:
            continue
        part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        written += 1
    return written


def used_para_ids(package: Any) -> set[str]:
    """Every ``w14:paraId`` and ``w14:textId`` in the package's XML parts."""
    used: set[str] = set()
    for part in package.iter_parts():
        element = getattr(part, "_element", None)
        if element is None:
            continue
        for node in element.iter():
            if isinstance(node.tag, str):
                used.update(v.upper() for v in (node.get(PARA_ID), node.get(TEXT_ID)) if v)
    return used


def refresh_para_ids(element: Any, used: set[str]) -> int:
    """Give every ``w14:paraId``/``w14:textId`` in ``element`` that ``used``
    already holds a fresh one (8 hex digits below 0x80000000), adding each to
    ``used``; the number changed."""
    changed = 0
    for node in element.iter():
        if not isinstance(node.tag, str):
            continue
        for name in (PARA_ID, TEXT_ID):
            value = node.get(name)
            if value is None:
                continue
            if value.upper() in used:
                value = _fresh_id(used)
                node.set(name, value)
                changed += 1
            used.add(value.upper())
    return changed


def _fresh_id(used: set[str]) -> str:
    while True:
        value = f"{secrets.randbelow(_ID_LIMIT - 1) + 1:08X}"
        if value not in used:
            return value
