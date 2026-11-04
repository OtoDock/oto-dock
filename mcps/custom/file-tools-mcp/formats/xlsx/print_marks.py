"""How a workbook prints, made visible in its rendering.

``marked_copy`` writes a copy of the workbook in which every chart and
picture of a printed worksheet is a plain picture of its own colour at the
same anchor, and, on a sheet with no page setup of its own, two 1 pt
pictures mark the first and the last column of its used range. The engine
draws the copy as it prints the workbook; ``print_checks`` reads on which
pages each colour lands. The marks change nothing that decides the pages:
a picture keeps its object's anchor and size, and the column marks sit
inside the used range.

``plan`` names the marks from the workbook alone, in the order the copy
gives them their colours, so the check finds them again from the file it
checks.
"""

from __future__ import annotations

import os
import posixpath
import re
import struct
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field

from lxml import etree

from formats.xlsx.package import related, sheet_targets, workbook_part
from formats.xlsx.refs import col_index, col_letters

CHART = "chart"
PICTURE = "picture"
FIRST_COLUMN = "first column"
LAST_COLUMN = "last column"

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_WORKSHEET_TYPE = "/worksheet"
_DRAWING_TYPE = R_NS + "/drawing"
_IMAGE_TYPE = R_NS + "/image"
_DRAWING_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.drawing+xml"
# Package parts come from the person's file: no entities, no DTD, no network.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
_ANCHORS = frozenset(f"{{{XDR_NS}}}{name}" for name in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"))
_HIDDEN_STATES = frozenset({"hidden", "veryHidden"})
_TRUE = frozenset({"1", "true"})


def _main(name: str) -> str:
    return f"{{{MAIN_NS}}}{name}"


# The worksheet elements the sheet's scan reads, by qualified name.
_CELL, _ROW, _COLUMN = _main("c"), _main("row"), _main("col")
_VALUES = frozenset({_main("v"), _main("f"), _main("is")})
_FIT, _SETUP, _BREAKS, _DRAWING = _main("pageSetUpPr"), _main("pageSetup"), _main("colBreaks"), _main("drawing")
_PRINT_AREA = "_xlnm.print_area"

# A mark is an 8 x 8 picture of one colour; the colours step by 51 in each
# channel (6 levels, 216 marks), far enough apart to survive an engine that
# re-encodes the pictures. Marks past the last colour are not made.
MARK_PX = 8
LEVELS = (0, 51, 102, 153, 204, 255)
MAX_MARKS = len(LEVELS) ** 3
# A column mark: 1 pt square, inside its cell.
COLUMN_MARK_EMU = 12700
_PICTURE_ID = 30000
_REL_PREFIX = "rIdPrintMark"
_MEDIA = "xl/media/print-mark-{}.png"
_NEW_DRAWING = "xl/drawings/print-marks-{}.xml"
# What the schema puts after a worksheet's drawing: a new <drawing> goes
# before the first of these, after the sheet's data and page setup.
_AFTER_DRAWING = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?(?:legacyDrawing|legacyDrawingHF|drawingHF|picture|oleObjects|"
                            rb"controls|webPublishItems|tableParts|extLst)\b")
_SHEET_END = re.compile(rb"</(?:[A-Za-z_][\w.-]*:)?worksheet>\s*$")
_CELL_REF = re.compile(r"([A-Z]{1,3})([0-9]+)$")


@dataclass(frozen=True)
class Mark:
    """One thing whose printed pages the check reads: a chart or a picture
    (``name`` is its name, ``cell`` its anchor's top-left cell, "" for an
    absolute anchor), or the first or last column of a sheet's used range
    (``name`` is the column's letter)."""

    kind: str
    sheet: str
    name: str
    cell: str
    colour: tuple[int, int, int]


@dataclass
class _Sheet:
    title: str
    part: str
    drawing: str | None
    objects: list[tuple[int, str, str, str]] = field(default_factory=list)  # anchor index, kind, name, cell
    columns: tuple[int, int, int] | None = None   # row, first column, last column (0-based)


def colour(index: int) -> tuple[int, int, int]:
    """The ``index``-th mark's colour."""
    n = len(LEVELS)
    return LEVELS[index // (n * n) % n], LEVELS[index // n % n], LEVELS[index % n]


def png(rgb: tuple[int, int, int]) -> bytes:
    """An 8 x 8 PNG of one colour."""
    raw = b"".join(b"\x00" + bytes(rgb) * MARK_PX for _ in range(MARK_PX))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", MARK_PX, MARK_PX, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def plan(zf: zipfile.ZipFile) -> list[Mark]:
    """The marks a copy of this workbook carries, in colour order."""
    marks: list[Mark] = []
    for sheet in _sheets(zf):
        for _index, kind, name, cell in sheet.objects:
            marks.append(Mark(kind, sheet.title, name, cell, colour(len(marks))))
        if sheet.columns is not None:
            row, first, last = sheet.columns
            for kind, col in ((FIRST_COLUMN, first), (LAST_COLUMN, last)):
                marks.append(Mark(kind, sheet.title, col_letters(col + 1), f"{col_letters(col + 1)}{row + 1}",
                                  colour(len(marks))))
    return marks[:MAX_MARKS]


def _sheets(zf: zipfile.ZipFile) -> Iterator[_Sheet]:
    """The printed worksheets in workbook order, with their objects and,
    for one with no page setup of its own, the columns to mark."""
    workbook = workbook_part(zf)
    if workbook is None:
        return
    root = etree.fromstring(zf.read(workbook), _PARSER)
    parts = sheet_targets(zf)
    worksheets = set(related(zf, workbook, _WORKSHEET_TYPE))
    titles = [s.get("name", "") for s in root.iter(f"{{{MAIN_NS}}}sheet")]
    print_areas = set()
    for name in root.iter(f"{{{MAIN_NS}}}definedName"):
        local = name.get("localSheetId")
        if name.get("name", "").casefold() == _PRINT_AREA and local is not None and local.isdigit() \
                and int(local) < len(titles):
            print_areas.add(titles[int(local)])
    seen_drawings: set[str] = set()
    for element in root.iter(f"{{{MAIN_NS}}}sheet"):
        title = element.get("name", "")
        part = parts.get(title, "")
        if part not in worksheets or element.get("state") in _HIDDEN_STATES or part not in zf.namelist():
            continue
        drawings = related(zf, part, "/drawing")
        sheet = _Sheet(title, part, drawings[0] if drawings else None)
        try:
            own_setup, columns, drawing_element = _read_sheet(zf, part)
        except etree.XMLSyntaxError:
            yield sheet   # unreadable: nothing to mark
            continue
        # A drawing another sheet shares, one that is missing or does not
        # parse, or a <drawing> with no part: the sheet is left unmarked
        # (a second <drawing> would break it).
        if sheet.drawing is None and drawing_element:
            yield sheet
            continue
        if sheet.drawing is not None:
            if sheet.drawing in seen_drawings or sheet.drawing not in zf.namelist():
                yield sheet
                continue
            seen_drawings.add(sheet.drawing)
            try:
                sheet.objects = _objects(etree.fromstring(zf.read(sheet.drawing), _PARSER))
            except etree.XMLSyntaxError:
                yield sheet
                continue
        if not own_setup and title not in print_areas:
            sheet.columns = columns
        yield sheet


def _read_sheet(zf: zipfile.ZipFile, part: str) -> tuple[bool, tuple[int, int, int] | None, bool]:
    """Whether the sheet has a page setup of its own (a fit to pages, a
    scale, an orientation or a column break); the row and the first and
    last visible columns of its used range (None for fewer than two); and
    whether it holds a <drawing>."""
    own = drawing = False
    hidden_columns: set[int] = set()
    used: set[int] = set()
    first_row: int | None = None
    with zf.open(part) as fh:
        for _event, element in etree.iterparse(fh, events=("end",), resolve_entities=False, no_network=True,
                                               load_dtd=False, huge_tree=False):
            tag = element.tag
            if tag == _CELL:
                if any(child.tag in _VALUES for child in element):
                    match = _CELL_REF.match(element.get("r", ""))
                    row_element = element.getparent()
                    if match and (row_element is None or row_element.get("hidden") not in _TRUE):
                        col, row = col_index(match.group(1)) - 1, int(match.group(2)) - 1
                        used.add(col)
                        first_row = row if first_row is None else min(first_row, row)
                element.clear(keep_tail=True)
            elif tag == _ROW:
                element.clear(keep_tail=True)
            elif tag == _COLUMN and element.get("hidden") in _TRUE:
                low, high = int(element.get("min", "0") or 0), int(element.get("max", "0") or 0)
                hidden_columns.update(range(max(low, 1) - 1, min(high, 16384)))
            elif tag == _FIT and element.get("fitToPage") in _TRUE:
                own = True
            elif tag == _SETUP and (element.get("orientation") or element.get("scale") not in (None, "100")):
                own = True
            elif tag == _BREAKS and len(element):
                own = True
            elif tag == _DRAWING:
                drawing = True
    visible = sorted(used - hidden_columns)
    if first_row is None or len(visible) < 2:
        return own, None, drawing
    return own, (first_row, visible[0], visible[-1]), drawing


def _objects(drawing: etree._Element) -> list[tuple[int, str, str, str]]:
    """(anchor index, kind, name, top-left cell) of each chart and picture
    the drawing anchors directly."""
    out = []
    for index, anchor in enumerate(child for child in drawing if child.tag in _ANCHORS):
        content = next((c for c in anchor if isinstance(c.tag, str) and etree.QName(c).localname in
                        ("pic", "graphicFrame", "sp", "grpSp", "cxnSp", "contentPart")), None)
        if content is None:
            continue
        local = etree.QName(content).localname
        if local == "graphicFrame" and content.find(f".//{{{C_NS}}}chart") is not None:
            kind = CHART
        elif local == "pic":
            kind = PICTURE
        else:
            continue
        props = content.find(f".//{{{XDR_NS}}}cNvPr")
        name = (props.get("name") if props is not None else "") or f"{kind} {index + 1}"
        out.append((index, kind, name, _anchor_cell(anchor)))
    return out


def _anchor_cell(anchor: etree._Element) -> str:
    start = anchor.find(f"{{{XDR_NS}}}from")
    if start is None:
        return ""
    try:
        col = int(start.findtext(f"{{{XDR_NS}}}col") or "")
        row = int(start.findtext(f"{{{XDR_NS}}}row") or "")
    except ValueError:
        return ""
    return f"{col_letters(col + 1)}{row + 1}"


# --- The marked copy ----------------------------------------------------------


def marked_copy(path: str, folder: str, *, own_copy: bool = False) -> str:
    """Worker core: the workbook at ``path`` (read beneath the mount unless
    ``own_copy``) copied into ``folder`` under its own name, with its marks;
    the copy's path."""
    import checks

    fileobj = open(path, "rb") if own_copy else None
    try:
        with checks.CheckContext(path, "xlsx", fileobj=fileobj) as ctx:
            zf = ctx.zip()
            parts = _marked_parts(zf)
            if not parts:
                return path
            target = os.path.join(folder, os.path.basename(path))
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
                for info in zf.infolist():
                    if info.filename not in parts:
                        out.writestr(info.filename, zf.read(info))
                for name, data in parts.items():
                    out.writestr(name, data)
    finally:
        if fileobj is not None:
            fileobj.close()
    return target


def _marked_parts(zf: zipfile.ZipFile) -> dict[str, bytes]:
    """The parts the marked copy replaces or adds, by name."""
    parts: dict[str, bytes] = {}
    index = 0
    new_drawings = 0
    for sheet in _sheets(zf):
        wanted = len(sheet.objects) + (2 if sheet.columns is not None else 0)
        if not wanted or index >= MAX_MARKS:
            continue
        if sheet.drawing is None:
            drawing_name = _NEW_DRAWING.format(new_drawings)
            new_drawings += 1
            drawing = etree.Element(f"{{{XDR_NS}}}wsDr", nsmap={"xdr": XDR_NS, "a": A_NS, "r": R_NS})
            rels = etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
            sheet_rels_name = _rels_name(sheet.part)
            sheet_rels = (etree.fromstring(zf.read(sheet_rels_name), _PARSER) if sheet_rels_name in zf.namelist()
                          else etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS}))
            rid = _free_id(sheet_rels, _REL_PREFIX)
            _relate(sheet_rels, rid, _DRAWING_TYPE, _relative(sheet.part, drawing_name))
            parts[sheet_rels_name] = etree.tostring(sheet_rels, xml_declaration=True, encoding="UTF-8",
                                                    standalone=True)
            parts[sheet.part] = _with_drawing(zf.read(sheet.part), rid)
            parts["[Content_Types].xml"] = _typed(parts.get("[Content_Types].xml") or zf.read("[Content_Types].xml"),
                                                  drawing_name)
        else:
            drawing_name = sheet.drawing
            drawing = etree.fromstring(zf.read(drawing_name), _PARSER)
            rels_name = _rels_name(drawing_name)
            rels = (etree.fromstring(zf.read(rels_name), _PARSER) if rels_name in zf.namelist()
                    else etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS}))
        anchors = [child for child in drawing if child.tag in _ANCHORS]
        for anchor_index, _kind, _name, _cell in sheet.objects:
            if index >= MAX_MARKS:
                break
            anchor = anchors[anchor_index]
            content = next(c for c in anchor if isinstance(c.tag, str) and etree.QName(c).localname in
                           ("pic", "graphicFrame"))
            rid = _media(parts, rels, drawing_name, index)
            content.addprevious(_picture(rid, index))
            anchor.remove(content)
            index += 1
        if sheet.columns is not None:
            row, first, last = sheet.columns
            for col in (first, last):
                if index >= MAX_MARKS:
                    break
                rid = _media(parts, rels, drawing_name, index)
                drawing.append(_column_anchor(row, col, _picture(rid, index)))
                index += 1
        parts[drawing_name] = etree.tostring(drawing, xml_declaration=True, encoding="UTF-8", standalone=True)
        parts[_rels_name(drawing_name)] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8",
                                                         standalone=True)
    if parts:
        content_types = parts.get("[Content_Types].xml") or zf.read("[Content_Types].xml")
        parts["[Content_Types].xml"] = _with_png(content_types)
    return parts


def _rels_name(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _relative(source: str, target: str) -> str:
    return posixpath.relpath(target, posixpath.dirname(source))


def _free_id(rels: etree._Element, prefix: str) -> str:
    taken = {rel.get("Id") for rel in rels}
    n = 0
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


def _relate(rels: etree._Element, rid: str, rtype: str, target: str) -> None:
    etree.SubElement(rels, f"{{{PKG_REL_NS}}}Relationship", Id=rid, Type=rtype, Target=target)


def _media(parts: dict[str, bytes], rels: etree._Element, drawing_name: str, index: int) -> str:
    media = _MEDIA.format(index)
    parts[media] = png(colour(index))
    rid = _free_id(rels, _REL_PREFIX)
    _relate(rels, rid, _IMAGE_TYPE, _relative(drawing_name, media))
    return rid


def _picture(rid: str, index: int) -> etree._Element:
    pic = etree.Element(f"{{{XDR_NS}}}pic")
    nv = etree.SubElement(pic, f"{{{XDR_NS}}}nvPicPr")
    etree.SubElement(nv, f"{{{XDR_NS}}}cNvPr", id=str(_PICTURE_ID + index), name=f"Print mark {index + 1}")
    etree.SubElement(nv, f"{{{XDR_NS}}}cNvPicPr")
    fill = etree.SubElement(pic, f"{{{XDR_NS}}}blipFill")
    etree.SubElement(fill, f"{{{A_NS}}}blip", {f"{{{R_NS}}}embed": rid})
    etree.SubElement(etree.SubElement(fill, f"{{{A_NS}}}stretch"), f"{{{A_NS}}}fillRect")
    shape = etree.SubElement(pic, f"{{{XDR_NS}}}spPr")
    etree.SubElement(etree.SubElement(shape, f"{{{A_NS}}}prstGeom", prst="rect"), f"{{{A_NS}}}avLst")
    return pic


def _column_anchor(row: int, col: int, picture: etree._Element) -> etree._Element:
    anchor = etree.Element(f"{{{XDR_NS}}}oneCellAnchor")
    start = etree.SubElement(anchor, f"{{{XDR_NS}}}from")
    for name, value in (("col", col), ("colOff", 0), ("row", row), ("rowOff", 0)):
        etree.SubElement(start, f"{{{XDR_NS}}}{name}").text = str(value)
    etree.SubElement(anchor, f"{{{XDR_NS}}}ext", cx=str(COLUMN_MARK_EMU), cy=str(COLUMN_MARK_EMU))
    anchor.append(picture)
    etree.SubElement(anchor, f"{{{XDR_NS}}}clientData")
    return anchor


def _with_drawing(sheet_xml: bytes, rid: str) -> bytes:
    """The worksheet with a <drawing> naming ``rid``, put where the schema
    orders it, without parsing the sheet's data (a sheet can be large)."""
    element = f'<drawing xmlns="{MAIN_NS}" xmlns:r="{R_NS}" r:id="{rid}"/>'.encode()
    data_end = max(sheet_xml.rfind(b"sheetData>"), sheet_xml.rfind(b"sheetData/>"))
    tail = _AFTER_DRAWING.search(sheet_xml, max(data_end, 0))
    if tail is not None:
        return sheet_xml[:tail.start()] + element + sheet_xml[tail.start():]
    end = _SHEET_END.search(sheet_xml)
    if end is None:
        raise ValueError("the worksheet part has no end tag")
    return sheet_xml[:end.start()] + element + sheet_xml[end.start():]


def _typed(content_types: bytes, drawing_name: str) -> bytes:
    root = etree.fromstring(content_types, _PARSER)
    etree.SubElement(root, f"{{{CT_NS}}}Override", PartName="/" + drawing_name, ContentType=_DRAWING_CONTENT_TYPE)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _with_png(content_types: bytes) -> bytes:
    root = etree.fromstring(content_types, _PARSER)
    if not any(d.get("Extension", "").casefold() == "png" for d in root.iter(f"{{{CT_NS}}}Default")):
        default = etree.Element(f"{{{CT_NS}}}Default", Extension="png", ContentType="image/png")
        root.insert(0, default)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
