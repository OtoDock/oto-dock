"""The temporary copy of a package that a renderer reads, the person's
file never touched:
- ``unlink``: every relationship that leaves the package removed (a
  linked picture, video or sound, an object, a template, a subdocument, a
  frame, a file on the server), so neither engine fetches or reads it:
  LibreOffice 7.4 fetches a theme fill's linked picture and a workbook
  drawing's linked picture under every profile pin, and Collabora the
  drawing's. A relationship leaves the package when its mode is not
  Internal or its target names a scheme or a network path, whatever its
  Type says: both engines resolve a picture's ``r:link`` through any
  relationship. One kind stays: an http(s) hyperlink that only hyperlink
  elements of its source part use.
- ``drop_cached`` (a workbook): the cached values of formula cells dropped,
  so an engine that keeps a cached value on load computes the formula;
- ``a4`` (a workbook): A4 paper on every worksheet and chartsheet with no
  page setup, as the image's LibreOffice prints them; Collabora's default
  there is Letter. A page setup that names no paper is left alone: the
  standard's default there is Letter, and both engines follow it.

A worker core (it reads the package): every part is copied as it is,
except the relationship parts and worksheets it rewrites, by byte ranges
found with a streaming expat pass. Anything this pass does not expect (a
DOCTYPE, an entity, an encoding other than UTF-8, malformed XML, a package
without a workbook) raises ``CopyRefused``.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from xml.parsers import expat

MAIN_NAMESPACES = frozenset({"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                             "http://purl.oclc.org/ooxml/spreadsheetml/main"})
WORKSHEET_TYPE = "/worksheet"
CHARTSHEET_TYPE = "/chartsheet"
OFFICE_DOCUMENT_TYPE = "/officeDocument"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
A4 = "9"
# The worksheet and chartsheet children that come after pageSetup (ECMA-376
# CT_Worksheet, CT_Chartsheet).
AFTER_PAGE_SETUP = frozenset({
    "headerFooter", "rowBreaks", "colBreaks", "customProperties", "cellWatches", "ignoredErrors",
    "smartTags", "drawing", "legacyDrawing", "legacyDrawingHF", "drawingHF", "picture", "oleObjects",
    "controls", "webPublishItems", "tableParts", "extLst",
})
DATA_TABLE = "dataTable"
INTERNAL = "internal"
RELS_SUFFIX = ".rels"
_RELATIONSHIP_TYPES = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
                       "http://purl.oclc.org/ooxml/officeDocument/relationships/")
HYPERLINK_TYPES = frozenset(base + "hyperlink" for base in _RELATIONSHIP_TYPES)
ALT_CHUNK_TYPES = frozenset(base + "aFChunk" for base in _RELATIONSHIP_TYPES)
R_NAMESPACES = frozenset(base.rstrip("/") for base in _RELATIONSHIP_TYPES)
# The elements whose r:id names a hyperlink (WordprocessingML, DrawingML,
# SpreadsheetML; transitional and strict).
HYPERLINK_ELEMENTS = frozenset({
    ("http://schemas.openxmlformats.org/wordprocessingml/2006/main", "hyperlink"),
    ("http://purl.oclc.org/ooxml/wordprocessingml/main", "hyperlink"),
    ("http://schemas.openxmlformats.org/spreadsheetml/2006/main", "hyperlink"),
    ("http://purl.oclc.org/ooxml/spreadsheetml/main", "hyperlink"),
    *((ns, name) for ns in ("http://schemas.openxmlformats.org/drawingml/2006/main",
                            "http://purl.oclc.org/ooxml/drawingml/main")
      for name in ("hlinkClick", "hlinkHover", "hlinkMouseOver")),
})
WEB_SCHEMES = ("http", "https")
# A target that names a scheme (http:, file:, C:) or a network path.
_OUTSIDE_TARGET = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:|[/\\]{2}")
_COPY_CHUNK = 1024 * 1024
_SEP = " "


class CopyRefused(ValueError):
    """The package is not one this pass rewrites safely."""


def prepare(src: str, dst: str, *, drop_cached: bool = False, a4: bool = False,
            unlink: bool = False) -> dict[str, int]:
    """Worker core: ``src`` (opened with no link followed) copied to ``dst``
    with the relationship parts and worksheets rewritten; counts of what
    changed."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    counts = {"sheets": 0, "values_dropped": 0, "paper_set": 0, "links_removed": 0}
    with os.fdopen(fd, "rb") as fh, zipfile.ZipFile(fh) as zin:
        names = set(zin.namelist())
        sheets = worksheet_parts(zin) if drop_cached or a4 else set()
        charts = worksheet_parts(zin, CHARTSHEET_TYPE) if a4 else set()
        tmp = dst + ".part"
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    if unlink and info.filename.endswith(RELS_SUFFIX):
                        source = source_part(info.filename)
                        data, removed = unlink_relationships(
                            zin.read(info), _chunks(zin, source) if source in names else None)
                        counts["links_removed"] += removed
                        zout.writestr(_copy_info(info), data)
                        continue
                    if info.filename in sheets or info.filename in charts:
                        data, dropped, papered = rewrite_sheet(zin.read(info), drop_cached=drop_cached, a4=a4)
                        counts["sheets"] += 1
                        counts["values_dropped"] += dropped
                        counts["paper_set"] += papered
                        zout.writestr(_copy_info(info), data)
                        continue
                    with zin.open(info) as part, zout.open(_copy_info(info), "w") as out:
                        shutil.copyfileobj(part, out, _COPY_CHUNK)
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return counts


def _copy_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    out.compress_type = zipfile.ZIP_DEFLATED
    out.external_attr = info.external_attr
    return out


def worksheet_parts(zin: zipfile.ZipFile, kind: str = WORKSHEET_TYPE) -> set[str]:
    """The worksheet part names (or chartsheets', with ``kind``), found
    through the package's relationships (dialog and macro sheets are left
    alone)."""
    names = set(zin.namelist())
    workbook = next((target for kind, target in _relationships(zin, "_rels/.rels", "")
                     if kind.endswith(OFFICE_DOCUMENT_TYPE)), None)
    if workbook is None or workbook not in names:
        raise CopyRefused("the package has no workbook part")
    folder, base = posixpath.split(workbook)
    rels = posixpath.join(folder, "_rels", base + ".rels")
    return {target for found, target in _relationships(zin, rels, workbook)
            if found.endswith(kind) and target in names}


def _relationships(zin: zipfile.ZipFile, rels_name: str, source: str) -> list[tuple[str, str]]:
    try:
        data = zin.read(rels_name)
    except KeyError:
        return []
    found: list[tuple[str, str]] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        if name.split(_SEP)[:2] == [RELS_NS, "Relationship"] and attrs.get("TargetMode") != "External":
            target = attrs.get("Target", "")
            if target.startswith("/"):
                part = posixpath.normpath(target).lstrip("/")
            else:
                part = posixpath.normpath(posixpath.join(posixpath.dirname(source), target)).lstrip("/")
            found.append((attrs.get("Type", ""), part))

    parser = _parser()
    parser.StartElementHandler = start
    _parse(parser, data)
    return found


def source_part(rels_name: str) -> str:
    """The part a relationships part belongs to (``word/_rels/document.xml.rels``
    -> ``word/document.xml``; the package's own ``_rels/.rels`` -> "")."""
    folder, name = posixpath.split(rels_name)
    return posixpath.join(posixpath.dirname(folder), name[: -len(RELS_SUFFIX)]).lstrip("/")


def _chunks(zin: zipfile.ZipFile, name: str) -> Iterable[bytes]:
    with zin.open(name) as part:
        while chunk := part.read(_COPY_CHUNK):
            yield chunk


@dataclass
class _Relationship:
    rid: str
    kind: str
    target: str
    leaves: bool
    start: int
    end: int


def leaves_package(mode: str | None, target: str) -> bool:
    """True when a relationship points outside the package: any mode but
    Internal, or a target naming a scheme or a network path."""
    return (mode is not None and mode.strip().lower() != INTERNAL) or bool(_OUTSIDE_TARGET.match(target.strip()))


def read_relationships(data: bytes) -> list[_Relationship]:
    """Every relationship of a relationships part, its attributes decoded
    and its byte range kept; ``CopyRefused`` for anything expat does not
    read (a DOCTYPE, another encoding, malformed XML)."""
    parser = _parser()
    found: list[_Relationship] = []
    pending: list[_Relationship] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        if name.split(_SEP)[:2] != [RELS_NS, "Relationship"]:
            return
        at = parser.CurrentByteIndex
        close = tag_end(data, at)
        target = attrs.get("Target", "")
        rel = _Relationship(attrs.get("Id", ""), attrs.get("Type", ""), target,
                            leaves_package(attrs.get("TargetMode"), target), at, close)
        if data[close - 2:close] == b"/>":
            found.append(rel)
        else:
            pending.append(rel)

    def end(name: str) -> None:
        if pending and name.split(_SEP)[:2] == [RELS_NS, "Relationship"]:
            rel = pending.pop()
            rel.end = tag_end(data, parser.CurrentByteIndex)
            found.append(rel)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    _parse(parser, data)
    return found


def scan_relationships(data: bytes) -> tuple[bool, bool]:
    """(any relationship leaves the package, any is an altChunk part): the
    look the parent takes before a conversion. ``CopyRefused`` when the
    part cannot be read, which the caller takes as "leaves"."""
    rels = read_relationships(data)
    return any(r.leaves for r in rels), any(r.kind in ALT_CHUNK_TYPES or r.kind.lower().endswith("/afchunk")
                                            for r in rels)


def _web_hyperlink(rel: _Relationship) -> bool:
    scheme = rel.target.strip().split(":", 1)[0].lower() if ":" in rel.target else ""
    return rel.kind in HYPERLINK_TYPES and scheme in WEB_SCHEMES


def hyperlink_only(chunks: Iterable[bytes], rids: set[str]) -> set[str]:
    """Of ``rids``, those the source part uses at least once and only as the
    r:id of a hyperlink element."""
    parser = _parser()
    hyperlink: set[str] = set()
    other: set[str] = set()

    def start(name: str, attrs: dict[str, str]) -> None:
        parts = name.split(_SEP)
        element = (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
        for key, value in attrs.items():
            rid = value.strip()
            if rid not in rids:
                continue
            key_parts = key.split(_SEP)
            if element in HYPERLINK_ELEMENTS and len(key_parts) > 1 and key_parts[0] in R_NAMESPACES \
                    and key_parts[1] == "id":
                hyperlink.add(rid)
            else:
                other.add(rid)

    parser.StartElementHandler = start
    head = True
    for chunk in chunks:
        if head:
            _check_encoding(chunk)
            head = False
        try:
            parser.Parse(chunk, False)
        except expat.ExpatError as exc:
            raise CopyRefused(f"the part is not well formed ({exc})") from None
    try:
        parser.Parse(b"", True)
    except expat.ExpatError as exc:
        raise CopyRefused(f"the part is not well formed ({exc})") from None
    return hyperlink - other


def unlink_relationships(data: bytes, source: Iterable[bytes] | bytes | None = None) -> tuple[bytes, int]:
    """A relationships part without the relationships that leave the
    package, but for http(s) hyperlinks only hyperlink elements of the
    ``source`` part use (None: the package's own part, or a source not in
    the package, which keeps none); the bytes and how many went."""
    rels = read_relationships(data)
    candidates = {r.rid for r in rels if r.leaves and _web_hyperlink(r)}
    kept: set[str] = set()
    if candidates and source is not None:
        kept = hyperlink_only([source] if isinstance(source, bytes) else source, candidates)
    cuts = sorted((r.start, r.end) for r in rels if r.leaves and r.rid not in kept)
    if not cuts:
        return data, 0
    out = bytearray()
    last = 0
    for begin, finish in cuts:
        out += data[last:begin]
        last = finish
    out += data[last:]
    return bytes(out), len(cuts)


def _parser() -> expat.XMLParserType:
    parser = expat.ParserCreate(namespace_separator=_SEP)
    parser.namespace_prefixes = True

    def refuse(*_args) -> None:
        raise CopyRefused("the part declares a document type or an entity")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    return parser


def _check_encoding(data: bytes) -> None:
    head = data[:200].lstrip(b"\xef\xbb\xbf")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:200] or (
            head.startswith(b"<?xml") and b"encoding=" in head.split(b"?>", 1)[0]
            and not any(enc in head.split(b"?>", 1)[0].lower() for enc in (b'"utf-8"', b"'utf-8'"))):
        raise CopyRefused("the part is not UTF-8")


def _parse(parser: expat.XMLParserType, data: bytes) -> None:
    _check_encoding(data)
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise CopyRefused(f"the part is not well formed ({exc})") from None


def tag_end(data: bytes, start: int) -> int:
    """The offset just past the tag that starts at ``start`` (quotes
    honoured: an attribute value may hold '>')."""
    quote = 0
    for index in range(start + 1, len(data)):
        byte = data[index]
        if quote:
            if byte == quote:
                quote = 0
        elif byte in (0x22, 0x27):
            quote = byte
        elif byte == 0x3E:
            return index + 1
    raise CopyRefused("a tag does not end")


def rewrite_sheet(data: bytes, *, drop_cached: bool, a4: bool) -> tuple[bytes, int, int]:
    """One worksheet or chartsheet: each ``<v>`` after an ``<f>`` in its
    cell removed (``drop_cached``; a data table's kept) and an A4
    ``pageSetup`` given to a sheet with none (``a4``). The bytes, the values
    dropped, 1 when the paper was set."""
    parser = _parser()
    cuts: list[tuple[int, int]] = []
    inserts: list[tuple[int, bytes]] = []
    depth = 0
    root_prefix = ""
    cell: dict[str, bool] | None = None
    value_start = -1
    page_setup = False
    first_after = -1

    def local(name: str) -> tuple[str, str, str]:
        parts = name.split(_SEP)
        if len(parts) == 1:
            return "", parts[0], ""
        return parts[0], parts[1], parts[2] if len(parts) > 2 else ""

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, root_prefix, cell, value_start, page_setup, first_after
        ns, tag, prefix = local(name)
        at = parser.CurrentByteIndex
        if depth == 0:
            if ns not in MAIN_NAMESPACES or tag not in ("worksheet", "chartsheet"):
                raise CopyRefused("the part is not a worksheet or chartsheet")
            root_prefix = prefix
        main = ns in MAIN_NAMESPACES
        if depth == 1 and main:
            if tag == "pageSetup":
                page_setup = True
            elif tag in AFTER_PAGE_SETUP and first_after < 0:
                first_after = at
        if main and tag == "c":
            cell = {"f": False, "table": False}
        elif main and cell is not None and tag == "f":
            cell["f"] = True
            cell["table"] = attrs.get("t") == DATA_TABLE
        elif main and cell is not None and tag == "v" and drop_cached and cell["f"] and not cell["table"]:
            # An empty element's end event comes after its tag: cut it whole here.
            tag_close = tag_end(data, at)
            if data[tag_close - 2:tag_close] == b"/>":
                cuts.append((at, tag_close))
            else:
                value_start = at
        depth += 1

    def end(name: str) -> None:
        nonlocal depth, cell, value_start
        ns, tag, _prefix = local(name)
        depth -= 1
        at = parser.CurrentByteIndex
        main = ns in MAIN_NAMESPACES
        if main and tag == "v" and value_start >= 0:
            cuts.append((value_start, tag_end(data, at)))
            value_start = -1
        elif main and tag == "c":
            cell = None
        if depth == 0 and a4 and not page_setup:
            qname = f"{root_prefix}:pageSetup" if root_prefix else "pageSetup"
            inserts.append((first_after if first_after >= 0 else at, f'<{qname} paperSize="{A4}"/>'.encode()))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    _parse(parser, data)
    if not cuts and not inserts:
        return data, 0, 0
    edits = sorted([(a, b, b"") for a, b in cuts] + [(a, a, text) for a, text in inserts])
    out = bytearray()
    last = 0
    for begin, finish, text in edits:
        out += data[last:begin]
        out += text
        last = finish
    out += data[last:]
    return bytes(out), len(cuts), 1 if inserts else 0
