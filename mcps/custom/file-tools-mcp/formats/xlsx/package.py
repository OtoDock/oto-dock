"""Workbook package rules openpyxl leaves to the caller.

openpyxl reads a Strict-conformance workbook as one with no sheets (a save
would then overwrite the person's data with a blank book), writes the plain
workbook content type whatever the file is called, and on a macro-enabled
save drops the VBA project's signature relationship, its content types and
the Office 2010 ribbon relationship. This module refuses the first and
restores the rest after openpyxl has written the package.
"""

import io
import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_STRICT_PREFIX = "http://purl.oclc.org/ooxml/"
_OFFICE_DOC_SUFFIX = "/officeDocument"

_WORKBOOK_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    ".xltm": "application/vnd.ms-excel.template.macroEnabled.main+xml",
}
_MACRO_EXTS = frozenset({".xlsm", ".xltm"})
_TEMPLATE_EXTS = frozenset({".xltx", ".xltm"})

# Root relationships of a ribbon customisation: Office 2007's and Office
# 2010's. openpyxl carries over only the first.
_CUSTOM_UI_TYPES = frozenset({
    "http://schemas.microsoft.com/office/2006/relationships/ui/extensibility",
    "http://schemas.microsoft.com/office/2007/relationships/ui/extensibility",
})
_VBA_RELS = "xl/_rels/vbaProject.bin.rels"

# Package parts come from the person's file: no entities, no DTD, no network.
_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
)

STRICT_MESSAGE = (
    "This workbook is saved in the Strict Open XML format, which this tool "
    "cannot read. Save it as a standard Excel workbook (.xlsx) and try again."
)


def _ext(path: str) -> str:
    return Path(path).suffix.lower()


def keeps_vba(path: str) -> bool:
    """A macro-enabled name: the load keeps the VBA project."""
    return _ext(path) in _MACRO_EXTS


def is_template(path: str) -> bool:
    return _ext(path) in _TEMPLATE_EXTS


def needs_package_pass(path: str) -> bool:
    """Only these names can come out of openpyxl with the wrong content type
    or without the parts a macro project needs; plain .xlsx saves are left
    exactly as openpyxl writes them."""
    return _ext(path) in (_MACRO_EXTS | _TEMPLATE_EXTS)


def _office_document(zf: zipfile.ZipFile) -> tuple[str, str] | None:
    """(relationship type, part name) of the package's main part."""
    try:
        root = etree.fromstring(zf.read("_rels/.rels"), _PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return None
    for rel in root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
        rtype = rel.get("Type", "")
        if rtype.endswith(_OFFICE_DOC_SUFFIX):
            return rtype, posixpath.normpath(rel.get("Target", "").lstrip("/"))
    return None


def is_strict(path: str) -> bool:
    """True for a Strict-conformance workbook: a Strict relationship type, a
    workbook root in a Strict namespace, or conformance="strict". A file
    that is not a readable package answers False so openpyxl reports its
    own error."""
    try:
        with zipfile.ZipFile(path) as zf:
            main = _office_document(zf)
            if main is None:
                return False
            rtype, part = main
            if rtype.startswith(_STRICT_PREFIX):
                return True
            try:
                with zf.open(part) as fh:
                    for _event, elem in etree.iterparse(
                        fh, events=("start",), resolve_entities=False,
                        no_network=True, load_dtd=False, huge_tree=False,
                    ):
                        return (
                            elem.tag.startswith("{" + _STRICT_PREFIX)
                            or elem.get("conformance") == "strict"
                        )
            except (KeyError, etree.XMLSyntaxError):
                return False
    except (OSError, zipfile.BadZipFile):
        return False
    return False


def check_readable(path: str) -> None:
    """Refuse a workbook openpyxl would misread, before any load."""
    if is_strict(path):
        raise ValueError(STRICT_MESSAGE)


@dataclass
class MacroParts:
    """What a macro-enabled original carries that openpyxl's writer drops:
    content-type overrides, the VBA project's own relationships and the
    ribbon relationships of the package root."""
    overrides: dict[str, str] = field(default_factory=dict)
    vba_rels: bytes | None = None
    root_rels: list[dict[str, str]] = field(default_factory=list)


def read_macro_parts(path: str) -> MacroParts:
    """Read the parts to restore, from the original, before it is replaced."""
    parts = MacroParts()
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            try:
                types = etree.fromstring(zf.read("[Content_Types].xml"), _PARSER)
                for ov in types.findall(f"{{{_CT_NS}}}Override"):
                    name = ov.get("PartName", "")
                    if name:
                        parts.overrides[name] = ov.get("ContentType", "")
            except (KeyError, etree.XMLSyntaxError):
                pass
            if _VBA_RELS in names:
                parts.vba_rels = zf.read(_VBA_RELS)
            main = _office_document(zf)
            if main is not None:
                root = etree.fromstring(zf.read("_rels/.rels"), _PARSER)
                for rel in root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
                    if rel.get("Type") in _CUSTOM_UI_TYPES:
                        parts.root_rels.append(dict(rel.attrib))
    except (OSError, zipfile.BadZipFile, KeyError, etree.XMLSyntaxError):
        return MacroParts()
    return parts


def _content_types(xml: bytes, names: set[str], ext: str, kept: MacroParts) -> bytes:
    """[Content_Types].xml with the workbook part typed for its extension and
    an override restored for every kept part openpyxl left untyped (or typed
    only through a Default by extension, as a VBA signature `.bin`)."""
    root = etree.fromstring(xml, _PARSER)
    overrides = {ov.get("PartName"): ov for ov in root.findall(f"{{{_CT_NS}}}Override")}
    workbook = overrides.get("/xl/workbook.xml")
    if workbook is not None and ext in _WORKBOOK_TYPES:
        workbook.set("ContentType", _WORKBOOK_TYPES[ext])
    for part_name, ctype in kept.overrides.items():
        if part_name in overrides or part_name.lstrip("/") not in names:
            continue
        etree.SubElement(
            root, f"{{{_CT_NS}}}Override", PartName=part_name, ContentType=ctype,
        )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _root_rels(xml: bytes, names: set[str], kept: MacroParts) -> bytes:
    root = etree.fromstring(xml, _PARSER)
    rels = root.findall(f"{{{_PKG_REL_NS}}}Relationship")
    ids = {rel.get("Id") for rel in rels}
    present = {(rel.get("Type"), rel.get("Target", "").lstrip("/")) for rel in rels}
    for attrs in kept.root_rels:
        target = attrs.get("Target", "").lstrip("/")
        if target not in names or (attrs.get("Type"), target) in present:
            continue
        rel_id = attrs.get("Id") or "rIdUi"
        n = 1
        while rel_id in ids:
            rel_id = f"rIdUi{n}"
            n += 1
        ids.add(rel_id)
        etree.SubElement(
            root, f"{{{_PKG_REL_NS}}}Relationship",
            Id=rel_id, Type=attrs.get("Type", ""), Target=attrs.get("Target", ""),
        )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def finish_package(data: bytes, path: str, kept: MacroParts | None) -> bytes:
    """The saved package with its content type matching `path`'s extension
    and the macro parts openpyxl dropped put back. Every other member is
    copied byte for byte."""
    ext = _ext(path)
    kept = kept or MacroParts()
    src = zipfile.ZipFile(io.BytesIO(data))
    names = set(src.namelist())
    out = io.BytesIO()
    with src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            member = src.read(info.filename)
            if info.filename == "[Content_Types].xml":
                extra = {_VBA_RELS} if kept.vba_rels and "xl/vbaProject.bin" in names else set()
                member = _content_types(member, names | extra, ext, kept)
            elif info.filename == "_rels/.rels" and kept.root_rels:
                member = _root_rels(member, names, kept)
            dst.writestr(info, member)
        if kept.vba_rels and "xl/vbaProject.bin" in names and _VBA_RELS not in names:
            dst.writestr(_VBA_RELS, kept.vba_rels)
    return out.getvalue()


_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_COMMENTS_SUFFIX = "/comments"
_VML_SUFFIX = "/vmlDrawing"


@dataclass(frozen=True)
class SheetParts:
    """A sheet's own parts as the original package names them (no leading
    slash): the worksheet, its comments and the drawing that shows them."""
    sheet: str
    comments: str | None = None
    vml: str | None = None


def _rels_of(zf: zipfile.ZipFile, part: str) -> list[tuple[str, str, str]]:
    """(id, type, resolved target) of a part's relationships."""
    folder, name = posixpath.split(part)
    try:
        root = etree.fromstring(zf.read(posixpath.join(folder, "_rels", name + ".rels")), _PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return []
    out = []
    for rel in root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        out.append((rel.get("Id", ""), rel.get("Type", ""), resolved))
    return out


def related(zf: zipfile.ZipFile, part: str, type_suffix: str) -> list[str]:
    """The parts ``part`` relates to by a relationship type ending in
    ``type_suffix`` ("/styles", "/drawing")."""
    return [target for _rid, rtype, target in _rels_of(zf, part) if rtype.endswith(type_suffix)]


def workbook_part(zf: zipfile.ZipFile) -> str | None:
    """The workbook part's name, or None when the package names none."""
    main = _office_document(zf)
    return None if main is None else main[1]


def sheet_targets(zf: zipfile.ZipFile) -> dict[str, str]:
    """Each worksheet's part name by sheet title, as the package names
    them; empty for a package that is not a readable workbook."""
    workbook = workbook_part(zf)
    if workbook is None:
        return {}
    try:
        root = etree.fromstring(zf.read(workbook), _PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return {}
    targets = {rid: target for rid, _type, target in _rels_of(zf, workbook)}
    out: dict[str, str] = {}
    for sheet in root.iter(f"{{{_SHEET_NS}}}sheet"):
        part = targets.get(sheet.get(_REL_ID, ""))
        if part is not None:
            out[sheet.get("name", "")] = part
    return out


def sheet_parts(path: str) -> dict[str, SheetParts]:
    """Each sheet's own parts, by sheet title, as the file names them; an
    empty map for a file that is not a readable package."""
    try:
        with zipfile.ZipFile(path) as zf:
            out: dict[str, SheetParts] = {}
            for title, part in sheet_targets(zf).items():
                comments = vml = None
                for _rid, rtype, target in _rels_of(zf, part):
                    if rtype.endswith(_COMMENTS_SUFFIX):
                        comments = target
                    elif rtype.endswith(_VML_SUFFIX):
                        vml = target
                out[title] = SheetParts(part, comments, vml)
            return out
    except (OSError, zipfile.BadZipFile, KeyError, etree.XMLSyntaxError):
        return {}


def defined_names(zf: zipfile.ZipFile) -> dict[tuple[str | None, str], str]:
    """Each defined name's text by (the title of the sheet it belongs to or
    None for the workbook, and its name; both case-folded, as Excel
    compares them)."""
    workbook = workbook_part(zf)
    if workbook is None:
        return {}
    try:
        root = etree.fromstring(zf.read(workbook), _PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return {}
    titles = [sheet.get("name", "") for sheet in root.iter(f"{{{_SHEET_NS}}}sheet")]
    out: dict[tuple[str | None, str], str] = {}
    for name in root.iter(f"{{{_SHEET_NS}}}definedName"):
        local = name.get("localSheetId")
        scope = (titles[int(local)].casefold() if local is not None and local.isdigit() and int(local) < len(titles)
                 else None)
        out[(scope, name.get("name", "").casefold())] = name.text or ""
    return out


def tables(zf: zipfile.ZipFile) -> dict[str, tuple[str, str]]:
    """Each table's (sheet title, range) by its name, case-folded."""
    out: dict[str, tuple[str, str]] = {}
    for title, part in sheet_targets(zf).items():
        for target in related(zf, part, "/table"):
            try:
                root = etree.fromstring(zf.read(target), _PARSER)
            except (KeyError, etree.XMLSyntaxError):
                continue
            name = root.get("displayName") or root.get("name") or ""
            if name and root.get("ref"):
                out[name.casefold()] = (title, root.get("ref"))
    return out
