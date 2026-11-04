"""Structural validation of an OOXML package: the faults Office refuses or
repairs, found without a schema.

Checked: every part well formed; a content type for every part; every
internal relationship target present (resolved with ``posixpath``, as the
packaging rules say); every relationship id a part uses defined; unique ids
where Office needs them (``cNvPr`` per part, ``sldId`` per presentation from
256, ``w14:paraId`` across a document, below 0x80000000, sheet ids and
names); children in schema order (``ooxml.order``), after Markup
Compatibility processing (the elements and attributes of ignorable
namespaces dropped, ``mc:AlternateContent`` read as its Fallback). A Strict
conformance package is not validated, and says so.

``check_package`` and ``check_parts_lost`` are registered as checks for
docx, xlsx and pptx (``<fmt>.package``, ``<fmt>.parts_lost``).
"""

from __future__ import annotations

import posixpath
import unicodedata
from collections import Counter
from typing import Any
from urllib.parse import unquote

import checks
from checks import CheckContext, Finding, PartRefused, Severity
from ooxml import order

CONTENT_TYPES = "[Content_Types].xml"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
X_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_PREFIX = "http://purl.oclc.org/ooxml/"
EXTERNAL = "External"
RELS_SUFFIX = ".rels"
RELS_FOLDER = "_rels"
XML_SUFFIXES = (".xml", ".rels", ".vml")
# Parts a writer may drop on purpose: the application rebuilds them.
# Parts a writer drops or rewrites on purpose: the application rebuilds the
# calculation chain, and the shared strings live on in the sheets (openpyxl
# writes them again, or inline), so neither part's name says anything lost.
REBUILT_TYPES = frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"})
# Extensions that hold a vendor's round-trip data (their part is the
# vendor's, not the document's): their loss is no loss of content.
VENDOR_ROUNDTRIP_EXTENSIONS = frozenset({"GoogleSheetsCustomDataVersion1", "GoogleSheetsCustomDataVersion2"})
XM_NS = "http://schemas.microsoft.com/office/excel/2006/main"
# Attributes that carry the person's own words (a section's name, a title, a
# description, a table's or pivot table's alt text), and the extensions
# whose names are settings only.
NAMING_ATTRIBUTES = frozenset({"name", "title", "descr", "altText", "altTextSummary"})
NAMED_SETTINGS_EXTENSIONS = frozenset({
    "{B58B0392-4F1F-4190-BB64-5DF3571DCE5F}",  # Excel's calculation features
    "{05A4C25C-085E-4340-85A3-A5531E510DB2}",  # a theme's family
})
# A part this small is parsed to see whether it holds anything at all.
EMPTY_CHECK_MAX_BYTES = 64 * 1024
RELATIONSHIPS_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
# A relationships part matters only through the parts it leads to, which
# are counted themselves.
NOT_COUNTED_TYPES = REBUILT_TYPES | {RELATIONSHIPS_TYPE}
LOST_NAMES_SHOWN = 3
# A main document part's type names the file's kind (a document or a
# template, plain or with macros, a deck or a show): a template copied under
# a document's name is saved as the document, its main part retyped. Each
# family counts as one type, so that is no loss.
_OFD = "application/vnd.openxmlformats-officedocument"
MAIN_TYPE_FAMILIES = {
    content_type: family for content_type, family in (
        *((f"{_OFD}.wordprocessingml.{name}.main+xml", "main document")
          for name in ("document", "template")),
        *((f"application/vnd.ms-word.{name}.main+xml", "main document")
          for name in ("document.macroEnabled", "template.macroEnabledTemplate")),
        *((f"{_OFD}.spreadsheetml.{name}.main+xml", "main workbook") for name in ("sheet", "template")),
        *((f"application/vnd.ms-excel.{name}.main+xml", "main workbook")
          for name in ("sheet.macroEnabled", "template.macroEnabled")),
        *((f"{_OFD}.presentationml.{name}.main+xml", "main presentation")
          for name in ("presentation", "template", "slideshow")),
        *((f"application/vnd.ms-powerpoint.{name}.main+xml", "main presentation")
          for name in ("presentation.macroEnabled", "template.macroEnabled", "slideshow.macroEnabled")),
    )
}
# A part is parsed whole for the id and order checks up to this size
# (uncompressed); a larger one is streamed for well-formedness and
# relationship ids only, so a big sheet never holds the worker's memory.
STRUCTURE_MAX_BYTES = 8 * 1024 * 1024
_ID_LIMIT = 0x80000000
_FIRST_SLIDE_ID = 256
# Types the SDK's data models as a sequence while ECMA-376 lets their
# children come in any order (an unbounded choice): openpyxl writes a font as
# name, family, color, sz and Excel opens it as it is. Their order is never
# reported; insert_child still writes the canonical order.
ANY_ORDER_TYPES = frozenset({"x:CT_Font/x:font", "x:CT_RPrElt/x:rPr"})
PACKAGE_CHECK = "package"
PARTS_LOST_CHECK = "parts_lost"
FORMATS = ("docx", "xlsx", "pptx")


# Word numbers a drawing by wp:docPr/@id. The picture's own pic:cNvPr id
# is not one Word reads (python-docx writes 0 for every picture), and
# python-docx numbers each header and footer from 1, so the rule is per part.
_DOC_PR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"
_PIC_PREFIX = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
# Picture ids no application reads, and a SmartArt drawing (PowerPoint's
# own cache of the diagram, written with id 0 on every shape).
_UNCOUNTED_SHAPE_IDS = (_PIC_PREFIX, "{http://schemas.microsoft.com/office/drawing/2008/diagram}")
_ALTERNATE = f"{{{MC_NS}}}AlternateContent"
_CHOICE = f"{{{MC_NS}}}Choice"
_FIX_MALFORMED = "Office refuses the file: make the part again with the tool that wrote it."
_FIX_RELATIONSHIPS = "Office repairs the file on open and may drop what the part links to: make the part again."
_FIX_PARAGRAPH = "Word repairs the file on open: write the paragraph again (a new one gets a fresh id)."
_FIX_SLIDES = "PowerPoint repairs or refuses the file: copy the slide (a copy gets a fresh id) and delete the old one."


def _finding(fmt: str, severity: Severity, part: str, message: str, *, key: str, fix: str = "",
             check: str = PACKAGE_CHECK) -> Finding:
    return Finding(severity, f"{fmt}.{check}", location=f"part /{part}", message=message, fix=fix,
                   part=part, key=key)


def source_part(rels_name: str) -> str:
    """The part a relationships part belongs to: ``word/_rels/document.xml.rels``
    -> ``word/document.xml``; the package's own ``_rels/.rels`` -> ``""``."""
    folder, name = posixpath.split(rels_name)
    return posixpath.join(posixpath.dirname(folder), name[: -len(RELS_SUFFIX)]).lstrip("/")


def resolve_target(source: str, target: str) -> str:
    """A relationship target as a part name: relative to the source part's
    folder, or absolute from the package root."""
    if target.startswith("/"):
        return posixpath.normpath(target).lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target)).lstrip("/")


def mc_processed(element: Any) -> Any:
    """A copy of ``element`` as an application that understands no extension
    sees it: ``mc:AlternateContent`` replaced by its Fallback's children, and
    the elements and attributes of the namespaces ``mc:Ignorable`` names
    dropped."""
    import copy

    tree = copy.deepcopy(element)
    ignorable: set[str] = set()
    for node in tree.iter():
        if not isinstance(node.tag, str):
            continue
        prefixes = node.get(f"{{{MC_NS}}}Ignorable")
        if prefixes:
            ignorable |= {node.nsmap.get(p) for p in prefixes.split()} - {None}
    for alternate in list(tree.iter(f"{{{MC_NS}}}AlternateContent")):
        parent = alternate.getparent()
        if parent is None:
            continue
        fallback = alternate.find(f"{{{MC_NS}}}Fallback")
        position = parent.index(alternate)
        for child in list(fallback) if fallback is not None else []:
            parent.insert(position, child)
            position += 1
        parent.remove(alternate)
    for node in list(tree.iter()):
        if not isinstance(node.tag, str):
            continue
        for name in [a for a in node.attrib if a.startswith("{") and a[1:].split("}", 1)[0] in ignorable]:
            del node.attrib[name]
        parent = node.getparent()
        if parent is not None and node.tag.startswith("{") and node.tag[1:].split("}", 1)[0] in ignorable:
            parent.remove(node)
    return tree


def _content_types(ctx: CheckContext) -> tuple[dict[str, str], dict[str, str]] | None:
    root = ctx.part_xml(CONTENT_TYPES)
    if root is None:
        return None
    defaults = {d.get("Extension", "").lower(): d.get("ContentType", "") for d in root.iter(f"{{{CT_NS}}}Default")}
    overrides = {part_key(d.get("PartName", "")): d.get("ContentType", "")
                 for d in root.iter(f"{{{CT_NS}}}Override")}
    return defaults, overrides


def part_key(name: str) -> str:
    """A part name as packages compare them: percent-decoded, NFC,
    case-insensitive, without the leading ``/``."""
    return unicodedata.normalize("NFC", unquote(name.lstrip("/"))).casefold()


def content_type_of(name: str, defaults: dict[str, str], overrides: dict[str, str]) -> str | None:
    """``overrides`` keyed by ``part_key``."""
    key = part_key(name)
    if key in overrides:
        return overrides[key]
    base = posixpath.basename(name)
    # "_rels/.rels" has the extension "rels": splitext would see none.
    return defaults.get(base.rsplit(".", 1)[1].lower()) if "." in base else None


def _is_xml(name: str, content_type: str | None) -> bool:
    return name.endswith(XML_SUFFIXES) or bool(content_type and content_type.endswith("xml"))


def validate_package(ctx: CheckContext) -> list[Finding]:
    """Every structural fault of the package ``ctx`` reads. Parts are parsed
    one at a time and freed; a part past STRUCTURE_MAX_BYTES is only
    streamed (well formed, relationship ids) and says so."""
    fmt = ctx.fmt
    archive = ctx.zip()
    names = [n for n in ctx.part_names() if not n.endswith("/")]
    present = {part_key(n) for n in names}
    types = _content_types(ctx)
    if types is None:
        return [_finding(fmt, Severity.ERROR, CONTENT_TYPES, "the package has no readable content types part",
                         key="content_types", fix="Office refuses the file: make it again from its source.")]
    defaults, overrides = types
    findings: list[Finding] = []
    for name in names:
        if name != CONTENT_TYPES and content_type_of(name, defaults, overrides) is None:
            findings.append(_finding(fmt, Severity.ERROR, name, "the part has no content type",
                                     key="no_content_type", fix="Declare it in [Content_Types].xml."))
    for name in overrides:
        if name not in present:
            findings.append(_finding(fmt, Severity.WARNING, CONTENT_TYPES,
                                     f"a content type is declared for /{name}, which is not in the package",
                                     key=f"dangling_override:{name}",
                                     fix="Office ignores the entry; act only if that part should be there."))
    xml_parts = [n for n in names if n != CONTENT_TYPES and _is_xml(n, content_type_of(n, defaults, overrides))]
    defined: dict[str, set[str]] = {}
    for name in [n for n in xml_parts if _is_rels(n)]:
        root = _parse(ctx, name, findings)
        if root is not None:
            defined.update(_relationships(fmt, name, root, present, findings))
    para_ids: Counter[str] = Counter()
    for name in [n for n in xml_parts if not _is_rels(n)]:
        if archive.getinfo(name).file_size > STRUCTURE_MAX_BYTES:
            _stream(ctx, fmt, name, defined.get(name, set()), findings)
            continue
        root = _parse(ctx, name, findings)
        if root is None:
            continue
        if isinstance(root.tag, str) and root.tag.startswith("{" + STRICT_PREFIX):
            return [*findings, Finding(Severity.INFO, f"{fmt}.{PACKAGE_CHECK}", message="the package is Strict "
                                       "Open XML, whose structure these checks do not validate")]
        _undefined_rids(fmt, name, root.iter(), defined.get(name, set()), findings)
        _part_ids(fmt, name, root, para_ids, findings)
        _order(fmt, name, root, findings)
        _empty(fmt, name, root, findings)
    for value, count in para_ids.items():
        if count > 1:
            findings.append(_finding(fmt, Severity.ERROR, "", f"paragraph id {value} is used {count} times",
                                     key=f"duplicate_para_id:{value}", fix=_FIX_PARAGRAPH))
    return findings


def _is_rels(name: str) -> bool:
    return name.endswith(RELS_SUFFIX) and posixpath.basename(posixpath.dirname(name)) == RELS_FOLDER


def _too_large(fmt: str, name: str, exc: PartRefused) -> Finding:
    return Finding(Severity.INFO, f"{fmt}.{PACKAGE_CHECK}", location=f"part /{name}",
                   message=f"the part is too large for the checks to read ({exc})", part=name, key="too_large")


def _parse(ctx: CheckContext, name: str, findings: list[Finding]) -> Any:
    try:
        return ctx.part_xml(name, cache=False)
    except PartRefused as exc:
        findings.append(_too_large(ctx.fmt, name, exc))
        return None
    except Exception as exc:  # noqa: BLE001  # a malformed part is the finding
        findings.append(_finding(ctx.fmt, Severity.ERROR, name, f"the part is not well-formed XML: {exc}",
                                 key="malformed", fix=_FIX_MALFORMED))
        return None


def _stream(ctx: CheckContext, fmt: str, name: str, known: set[str], findings: list[Finding]) -> None:
    def nodes():
        for _, node in ctx.iterparse(name):
            yield node
            node.clear()

    try:
        _undefined_rids(fmt, name, nodes(), known, findings)
    except PartRefused as exc:
        findings.append(_too_large(fmt, name, exc))
        return
    except Exception as exc:  # noqa: BLE001  # a malformed part is the finding
        findings.append(_finding(fmt, Severity.ERROR, name, f"the part is not well-formed XML: {exc}",
                                 key="malformed", fix=_FIX_MALFORMED))
        return
    findings.append(Finding(Severity.INFO, f"{fmt}.{PACKAGE_CHECK}", location=f"part /{name}",
                            message="the part is too large for the id and order checks", part=name))


def _relationships(fmt: str, name: str, root: Any, present: set[str], findings: list[Finding]) -> dict[str, set[str]]:
    source = source_part(name)
    ids: Counter[str] = Counter()
    for rel in root.iter(f"{{{RELS_NS}}}Relationship"):
        rid = rel.get("Id", "")
        ids[rid] += 1
        if rel.get("TargetMode") == EXTERNAL:
            continue
        target = resolve_target(source, rel.get("Target", ""))
        if part_key(target) not in present:
            findings.append(_finding(fmt, Severity.ERROR, source or name,
                                     f"relationship {rid} points to /{target}, which is not in the package",
                                     key=f"missing_target:{rid}",
                                     fix="Office drops what uses it, or refuses the file: add the part or remove "
                                         "what uses the relationship."))
    for rid, count in ids.items():
        if count > 1:
            findings.append(_finding(fmt, Severity.ERROR, name, f"relationship id {rid} is defined {count} times",
                                     key=f"duplicate_rid:{rid}", fix=_FIX_RELATIONSHIPS))
    return {source: set(ids)} if source else {}


def _undefined_rids(fmt: str, name: str, nodes: Any, known: set[str], findings: list[Finding]) -> None:
    prefix = f"{{{R_NS}}}"
    missing: set[str] = set()
    for node in nodes:
        if isinstance(node.tag, str):
            missing.update(v for a, v in node.attrib.items() if a.startswith(prefix) and v and v not in known)
    for rid in sorted(missing):
        findings.append(_finding(fmt, Severity.ERROR, name, f"the part uses relationship id {rid}, which its "
                                 "relationships do not define", key=f"undefined_rid:{rid}", fix=_FIX_RELATIONSHIPS))


def kept_branch(root: Any) -> Any:
    """The elements of ``root`` an application keeps: of each
    ``mc:AlternateContent`` only its first Choice. Word repeats a text box's
    paragraph ids in the Fallback copy and PowerPoint a shape's id in each
    branch, so ids are counted on this view."""
    stack = [root]
    while stack:
        node = stack.pop()
        if not isinstance(node.tag, str):
            continue
        yield node
        children = list(node)
        if node.tag == _ALTERNATE:
            choices = [c for c in children if c.tag == _CHOICE]
            children = choices[:1]
        stack.extend(reversed(children))


def _part_ids(fmt: str, name: str, root: Any, para_ids: Counter[str], findings: list[Finding]) -> None:
    shape_ids: Counter[str] = Counter()
    drawing_ids: Counter[str] = Counter()
    for node in kept_branch(root):
        if node.tag == _DOC_PR and node.get("id") is not None:
            drawing_ids[node.get("id")] += 1
        elif (node.tag.endswith("}cNvPr") and fmt != "docx" and not node.tag.startswith(_UNCOUNTED_SHAPE_IDS)
              and node.get("id") is not None):
            shape_ids[node.get("id")] += 1
        value = node.get(f"{{{W14_NS}}}paraId")
        if value:
            para_ids[value.upper()] += 1
            if not _below_limit(value):
                findings.append(_finding(fmt, Severity.ERROR, name, f"paragraph id {value} is not 8 hex digits "
                                         "below 80000000", key=f"bad_para_id:{value}", fix=_FIX_PARAGRAPH))
    for value, count in shape_ids.items():
        if count > 1:
            # Office opens it (LibreOffice 7.4 writes repeated ids in its layouts), but an
            # animation or connector that names the id may reach the wrong shape.
            findings.append(_finding(fmt, Severity.WARNING, name, f"shape id {value} is used {count} times in one "
                                     "part, so an animation or connector that names it may reach the wrong shape",
                                     key=f"duplicate_shape_id:{value}", fix="Give each shape its own id."))
    for value, count in drawing_ids.items():
        if count > 1:
            findings.append(_finding(fmt, Severity.WARNING, name, f"drawing id {value} is used {count} times in "
                                     "one part, so Word may report the file as damaged",
                                     key=f"duplicate_drawing_id:{value}",
                                     fix="Insert one of the pictures again: a new picture gets a fresh id."))
    if root.tag == f"{{{P_NS}}}presentation":
        findings += _slide_ids(fmt, name, root)
    if root.tag == f"{{{X_NS}}}workbook":
        findings += _sheet_ids(fmt, name, root)


def _below_limit(value: str) -> bool:
    try:
        return len(value) == 8 and int(value, 16) < _ID_LIMIT
    except ValueError:
        return False


def _slide_ids(fmt: str, name: str, root: Any) -> list[Finding]:
    findings: list[Finding] = []
    ids = Counter()
    for node in root.iter(f"{{{P_NS}}}sldId"):
        value = node.get("id", "")
        ids[value] += 1
        if not value.isdigit() or int(value) < _FIRST_SLIDE_ID:
            findings.append(_finding(fmt, Severity.ERROR, name, f"slide id {value} is below 256",
                                     key=f"low_slide_id:{value}", fix=_FIX_SLIDES))
    findings += [_finding(fmt, Severity.ERROR, name, f"slide id {v} is used {c} times", key=f"duplicate_slide_id:{v}",
                          fix=_FIX_SLIDES)
                 for v, c in ids.items() if c > 1]
    return findings


def _sheet_ids(fmt: str, name: str, root: Any) -> list[Finding]:
    sheets = list(root.iter(f"{{{X_NS}}}sheet"))
    ids = Counter(s.get("sheetId") for s in sheets)
    names = Counter((s.get("name") or "").casefold() for s in sheets)
    findings = [_finding(fmt, Severity.ERROR, name, f"sheet id {v} is used {c} times", key=f"duplicate_sheet_id:{v}",
                         fix="Excel repairs the file on open: copy the sheet's content to a new sheet.")
                for v, c in ids.items() if c > 1]
    findings += [_finding(fmt, Severity.ERROR, name, f"{c} sheets share one name", key=f"duplicate_sheet_name:{v}",
                          fix="Excel refuses the file: rename all but one of them.")
                 for v, c in names.items() if c > 1]
    return findings


def _order(fmt: str, name: str, root: Any, findings: list[Finding]) -> None:
    seen: set[tuple[str, str]] = set()
    for node in mc_processed(root).iter():
        if not isinstance(node.tag, str) or order.type_of(node) in ANY_ORDER_TYPES:
            continue
        for child, sibling in order.out_of_order(node):
            if (child, sibling) in seen:
                continue
            seen.add((child, sibling))
            findings.append(_finding(
                fmt, Severity.ERROR, name, f"<{_short(child)}> comes after <{_short(sibling)}> in "
                f"<{_short(node.tag)}>, where the schema puts it before", key=f"order:{child}:{sibling}",
                fix="Move it before; Office repairs or refuses a part out of order."))


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
# Containers the schema lets be empty that Word and PowerPoint repair when
# they are: a table needs a row, a row a cell.
NEEDS_ONE_OF = {
    f"{{{W_NS}}}tbl": f"{{{W_NS}}}tr",
    f"{{{W_NS}}}tr": f"{{{W_NS}}}tc",
    f"{{{_A_NS}}}tr": f"{{{_A_NS}}}tc",
}


def _empty(fmt: str, name: str, root: Any, findings: list[Finding]) -> None:
    """Elements that must hold a child and hold none: those the schema
    gives a content that cannot be empty (an animation's ``p:childTnLst``,
    a build list), and the tables and rows of ``NEEDS_ONE_OF``."""
    seen: set[str] = set()
    for node in mc_processed(root).iter():
        if not isinstance(node.tag, str) or node.tag in seen:
            continue
        needed = NEEDS_ONE_OF.get(node.tag)
        if needed is not None and next(node.iter(needed), None) is None:
            reason = f"Office needs at least one <{_short(needed)}> there"
        elif needed is None and order.needs_children(node) and not any(isinstance(c.tag, str) for c in node):
            reason = "the schema needs at least one child element there"
        else:
            continue
        seen.add(node.tag)
        findings.append(_finding(
            fmt, Severity.ERROR, name, f"<{_short(node.tag)}> is empty, where {reason}", key=f"empty:{node.tag}",
            fix="Remove the empty element or give it what it needs; Office repairs the file otherwise."))


def _short(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------
# What an edit lost
# ---------------------------------------------------------------------------


def inventory(ctx: CheckContext) -> dict[str, Any]:
    """The package as a later save must keep it: each part's content type,
    the relationships between parts (the package's own from ``""``) with
    their ids, the parts that hold nothing at all, every ``ext`` URI with
    the parts that carry it and whether it holds content of the document's
    own, and the relationships only a content-less extension names."""
    types = _content_types(ctx)
    defaults, overrides = types if types is not None else ({}, {})
    names = [n for n in ctx.part_names() if not n.endswith("/") and n != CONTENT_TYPES]
    present = {part_key(n): n for n in names}
    parts: dict[str, str] = {}
    links: dict[str, list[str]] = {}
    rel_targets: dict[str, dict[str, str]] = {}
    carriers: dict[str, dict[str, Any]] = {}
    empty: list[str] = []
    settings_refs: set[tuple[str, str]] = set()
    archive = ctx.zip()
    for name in names:
        content_type = content_type_of(name, defaults, overrides) or ""
        parts[name] = content_type
        if _is_rels(name):
            rel_targets[source_part(name)] = _targets(ctx, name, present)
            links[source_part(name)] = sorted(set(rel_targets[source_part(name)].values()))
        elif _is_xml(name, content_type) and not name.endswith(RELS_SUFFIX):
            if archive.getinfo(name).file_size <= EMPTY_CHECK_MAX_BYTES and _holds_nothing(ctx, name):
                empty.append(name)
            for _, node in ctx.iterparse(name, tags="{*}ext"):
                uri = node.get("uri")
                if uri:
                    content = uri not in VENDOR_ROUNDTRIP_EXTENSIONS and _has_content(node)
                    entry = carriers.setdefault(uri, {"parts": set(), "content": False})
                    entry["parts"].add(name)
                    entry["content"] = entry["content"] or content
                    if not content:
                        settings_refs.update((name, rid) for rid in _rids(node))
                node.clear()
    return {"parts": parts, "links": links, "rel_targets": rel_targets, "empty": sorted(empty),
            "ext": {uri: {"parts": sorted(e["parts"]), "content": e["content"]} for uri, e in sorted(carriers.items())},
            "settings_refs": sorted(settings_refs)}


def _holds_nothing(ctx: CheckContext, name: str) -> bool:
    try:
        root = ctx.part_xml(name, cache=False)
    except Exception:  # noqa: BLE001  # the package check reports a part it cannot read
        return False
    return root is not None and not (root.text or "").strip() and not any(isinstance(child.tag, str)
                                                                         for child in root)


def _rids(node: Any) -> set[str]:
    return {value for n in node.iter() if isinstance(n.tag, str)
            for attribute, value in n.attrib.items() if attribute.startswith(f"{{{R_NS}}}") and value}


def _has_content(ext: Any) -> bool:
    """Whether an ``ext`` holds content of the document's own: text
    anywhere in it, a cell reference or formula (the ``xm`` namespace), a
    relationship to another part, or a name, title, description or alt
    text (PowerPoint's sections and Excel's table alt text live in
    attributes). One that holds only settings (a default style, a
    calculation flag, a file id, the calculation features' names) does
    not."""
    named = ext.get("uri") not in NAMED_SETTINGS_EXTENSIONS
    if (ext.text or "").strip():
        return True
    for node in ext.iter():
        if node is ext:
            continue
        if (node.tail or "").strip():
            return True
        if not isinstance(node.tag, str):
            continue
        if (node.text or "").strip() or node.tag.startswith(f"{{{XM_NS}}}"):
            return True
        if any(attribute.startswith(f"{{{R_NS}}}") for attribute in node.attrib):
            return True
        if named and any(attribute.rsplit("}", 1)[-1] in NAMING_ATTRIBUTES for attribute in node.attrib):
            return True
    return False


def _targets(ctx: CheckContext, rels_name: str, present: dict[str, str]) -> dict[str, str]:
    try:
        root = ctx.part_xml(rels_name, cache=False)
    except Exception:  # noqa: BLE001  # the package check reports a relationships part it cannot read
        return {}
    source = source_part(rels_name)
    found = {}
    for rel in root.iter(f"{{{RELS_NS}}}Relationship"):
        if rel.get("TargetMode") == EXTERNAL:
            continue
        key = part_key(resolve_target(source, rel.get("Target", "")))
        if key in present:
            found[rel.get("Id", "")] = present[key]
    return found


def _kept(before: dict[str, Any], removed: frozenset[str]) -> set[str]:
    """The parts of the original a save should still hold: those the
    package's relationships led to, less the declared removals and what
    only they led to (a slide's notes and charts, a picture no other part
    shows). A package with no relationships of its own keeps every part."""
    links = before["links"]
    if "" not in links:
        return set(before["parts"]) - removed
    seen: set[str] = set()
    stack = [""]
    while stack:
        for target in links.get(stack.pop(), ()):
            if target not in seen and target not in removed:
                seen.add(target)
                stack.append(target)
    return seen


def _extension_data(before: dict[str, Any]) -> set[str]:
    """Parts reached only through relationships that content-less
    extensions name (a vendor's round-trip data): they go with them."""
    refs = {tuple(ref) for ref in before.get("settings_refs", ())}
    incoming: dict[str, set[tuple[str, str]]] = {}
    for source, targets in before.get("rel_targets", {}).items():
        for rid, target in targets.items():
            incoming.setdefault(target, set()).add((source, rid))
    return {target for target, sources in incoming.items() if sources and sources <= refs}


def check_package(ctx: CheckContext) -> list[Finding]:
    return validate_package(ctx)


def _counted_type(content_type: str) -> str:
    return MAIN_TYPE_FAMILIES.get(content_type, content_type)


def check_parts_lost(ctx: CheckContext) -> list[Finding]:
    """Parts and extensions with content of the document's own that the
    file had before this write and lacks now, beyond what the write's
    operations declared they removed. A part that held nothing, a part the
    application rebuilds, an extension of attribute settings and a vendor's
    round-trip data are no loss."""
    before = ctx.baseline_snapshot
    if not before:
        return []
    after = inventory(ctx)
    kept = _kept(before, ctx.removed)
    unheld = set(before.get("empty", ())) | _extension_data(before)
    expected: dict[str, list[str]] = {}
    for name in sorted(kept - unheld):
        content_type = before["parts"].get(name, "")
        if content_type not in NOT_COUNTED_TYPES:
            expected.setdefault(_counted_type(content_type), []).append(name)
    # An empty part that stays empty is not counted on either side: it never
    # stands in for a part with content of its type.
    still_empty = set(before.get("empty", ())) & set(after["empty"])
    counted = Counter(_counted_type(content_type) for name, content_type in after["parts"].items()
                      if name not in still_empty)
    findings: list[Finding] = []
    for counted_type, names in expected.items():
        lost = len(names) - counted.get(counted_type, 0)
        if lost <= 0:
            continue
        missing = [n for n in names if n not in after["parts"]] or names
        content_type = before["parts"].get(missing[0], "")
        shown = ", ".join(f"/{n}" for n in missing[:LOST_NAMES_SHOWN])
        more = f" and {len(missing) - LOST_NAMES_SHOWN} more" if len(missing) > LOST_NAMES_SHOWN else ""
        findings.append(Finding(
            Severity.ERROR, f"{ctx.fmt}.{PARTS_LOST_CHECK}", location=f"part /{missing[0]}",
            message=f"this write dropped {lost} part(s) of type {content_type or 'unknown'} the file had "
                    f"({shown}{more})",
            fix="The library does not keep this content: write it with an operation that does, or keep the "
                "original.", part=missing[0], key=f"lost_type:{content_type}"))
    for uri, entry in before["ext"].items():
        if entry["content"] and uri not in after["ext"] and any(part in kept for part in entry["parts"]):
            findings.append(Finding(
                Severity.ERROR, f"{ctx.fmt}.{PARTS_LOST_CHECK}", message=f"this write dropped the extension "
                f"{uri} the file had", fix="The library does not keep this extension; keep the original.",
                key=f"lost_ext:{uri}"))
    return findings


def register_checks() -> None:
    for fmt in FORMATS:
        checks.register(fmt, PACKAGE_CHECK, check_package, baseline=True)
        checks.register(fmt, PARTS_LOST_CHECK, check_parts_lost, snapshot=inventory)
