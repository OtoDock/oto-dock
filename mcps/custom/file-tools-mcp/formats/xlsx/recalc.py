"""Values beside the formulas a write saves.

openpyxl saves every formula with an empty value and drops the values the
file had, so a reader that does not calculate (a preview, a phone, a
data-only read, the agent's own readback) shows nothing. After the save,
LibreOffice recalculates a copy in the parent; this module reads the copy's
values and writes them into the saved file beside their formulas. Each
worksheet part it changes is rewritten cell by cell; every other part keeps
its bytes.

A value is written only where it can be trusted (see coverage): a
recalculated one where the engine has every function the formula and the
formulas it reads call, a value the file already had where the formula is
the same text in the same cell and reads nothing the batch changed. Every
other formula keeps an empty value, which Excel and LibreOffice compute
when the file opens.

An array formula that fills a range (``<f t="array" ref="D1:D3">``) keeps
its other cells as plain values in openpyxl's model: they are written from
the recalculated copy with the anchor's value, kept with a value kept from
before, and emptied otherwise, so no reader shows a stale figure.

Values are read from the XML itself, not through openpyxl: its data-only
mode turns a date result into a datetime and cannot tell a formula's value
from a constant.
"""

import os
import posixpath
import re
import tempfile
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from formats.xlsx import coverage, functions
from formats.xlsx.formulas import SPILLING, spills
from formats.xlsx.package import defined_names, sheet_targets, tables, workbook_part

NUMBER, TEXT, BOOL, ERROR = "n", "str", "b", "e"
# A value as stored: its kind and the <v> text, still XML-escaped.
Value = tuple[str, bytes]

NAME_ERROR = b"#NAME?"
# The engine that saved a recalculated workbook (docProps/app.xml).
_APPLICATION_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?Application>([^<]{1,200})</")
_APPLICATION_PEEK = 64 * 1024

ERRORS = frozenset({
    "#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#GETTING_DATA", "#SPILL!",
    "#CALC!", "#FIELD!", "#BLOCKED!", "#CONNECT!", "#BUSY!", "#UNKNOWN!",
})

_NAME = rb"(?:[A-Za-z_][\w.-]*:)?"
_CELL_RE = re.compile(rb"<(?P<p>" + _NAME + rb")c(?P<a>\s[^>]*?)?(?:/>|>(?P<b>.*?)</(?P=p)c>)", re.S)
_F_RE = re.compile(rb"<(?P<p>" + _NAME + rb")f(?P<a>\s[^>]*?)?(?:/>|>(?P<t>.*?)</(?P=p)f>)", re.S)
_V_RE = re.compile(rb"<(?P<p>" + _NAME + rb")v\s*(?:/>|>(?P<t>.*?)</(?P=p)v>)", re.S)
_ATTR_RE = re.compile(rb"""([A-Za-z_][\w.:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_DATA_START_RE = re.compile(rb"<" + _NAME + rb"sheetData\b[^>]*?(/?)>")
_DATA_END_RE = re.compile(rb"</" + _NAME + rb"sheetData>")
_ENTITY_RE = re.compile(r"&(#x[0-9A-Fa-f]+|#[0-9]+|lt|gt|amp|quot|apos);")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
# A cheap filter before the tokenizer: the names may follow a storage prefix.
_SPILL_HINT_RE = re.compile(r"(?:" + "|".join(sorted(SPILLING)) + r")\s*\(", re.IGNORECASE)

_METADATA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml"
_METADATA_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sheetMetadata"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
# The dynamic-array record Excel 365 writes: cm="1" on a formula cell points
# at it, and the formula spills.
_METADATA_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    b'xmlns:xda="http://schemas.microsoft.com/office/spreadsheetml/2017/dynamicarray">'
    b'<metadataTypes count="1"><metadataType name="XLDAPR" minSupportedVersion="120000" copy="1" '
    b'pasteAll="1" pasteValues="1" merge="1" splitFirst="1" rowColShift="1" clearFormats="1" '
    b'clearComments="1" assign="1" coerce="1" cellMeta="1"/></metadataTypes>'
    b'<futureMetadata name="XLDAPR" count="1"><bk><extLst><ext uri="{bdbb8cdc-fa1e-496e-a857-3c3f30c029c3}">'
    b'<xda:dynamicArrayProperties fDynamic="1" fCollapsed="0"/></ext></extLst></bk></futureMetadata>'
    b'<cellMetadata count="1"><bk><rc t="1" v="0"/></bk></cellMetadata></metadata>'
)
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def unescape(raw: bytes) -> str:
    """XML text as the characters it stands for."""
    def entity(m: re.Match) -> str:
        name = m.group(1)
        if name.startswith("#x"):
            return chr(int(name[2:], 16))
        if name.startswith("#"):
            return chr(int(name[1:]))
        return _ENTITIES[name]

    return _ENTITY_RE.sub(entity, raw.decode("utf-8", "replace"))


def attrs(raw: bytes | None) -> list[tuple[bytes, bytes]]:
    """An element's attributes as (name, value) pairs, values still
    escaped and double-quotable."""
    return [(m.group(1), m.group(2) if m.group(2) is not None else m.group(3).replace(b'"', b"&quot;"))
            for m in _ATTR_RE.finditer(raw or b"")]


def sheet_data_start(data: bytes) -> int:
    """Where a worksheet part's cells begin (its column widths and format
    properties come before), or the part's length."""
    region = _region(data)
    return region[0] if region is not None else len(data)


def _region(data: bytes) -> tuple[int, int] | None:
    start = _DATA_START_RE.search(data)
    if start is None or start.group(1):
        return None
    end = _DATA_END_RE.search(data, start.end())
    return None if end is None else (start.end(), end.start())


def cells(data: bytes):
    """(coordinate, attributes, <f> match or None, <v> match or None) of
    each cell of a worksheet part that names its coordinate."""
    region = _region(data)
    if region is None:
        return
    for m in _CELL_RE.finditer(data, *region):
        named = dict(attrs(m.group("a")))
        ref = named.get(b"r")
        if not ref:
            continue
        body = m.group("b") or b""
        yield (ref.decode("ascii", "replace"), named, _F_RE.search(body) if body else None,
               _V_RE.search(body) if body else None)


def _formula_cells(data: bytes):
    """(cell match, its attributes, its <f> match, its <v> match or None)
    for every cell of the part that holds a formula."""
    region = _region(data)
    if region is None:
        return
    for m in _CELL_RE.finditer(data, *region):
        body = m.group("b")
        if not body:
            continue
        f = _F_RE.search(body)
        if f is None:
            continue
        yield m, attrs(m.group("a")), f, _V_RE.search(body)


def value_of(kind: bytes, v: re.Match | None) -> Value | None:
    """A cell's stored value of type ``kind`` (its t attribute, b"n" when
    absent) from its <v> match."""
    return None if v is None else _value(kind, v.group("t"))


def read_formulas(data: bytes) -> dict[str, str]:
    """Each formula cell's text by coordinate (cells without a coordinate
    or a text, a shared formula's followers, are left out)."""
    out: dict[str, str] = {}
    for _m, cell_attrs, f, _v in _formula_cells(data):
        ref = dict(cell_attrs).get(b"r")
        if ref and f.group("t") is not None:
            out[ref.decode("ascii", "replace")] = unescape(f.group("t"))
    return out


def read_arrays(data: bytes) -> dict[str, coverage.Box]:
    """Each array formula filling more than its own cell: its range's box
    by anchor."""
    out: dict[str, coverage.Box] = {}
    for _m, cell_attrs, f, _v in _formula_cells(data):
        ref = dict(cell_attrs).get(b"r")
        f_named = dict(attrs(f.group("a")))
        if not ref or f_named.get(b"t") != b"array" or b":" not in f_named.get(b"ref", b""):
            continue
        box = coverage.range_box(f_named[b"ref"].decode("ascii", "replace"))
        if box is not None and (box[0], box[1]) != (box[2], box[3]):
            out[ref.decode("ascii", "replace")] = box
    return out


def _in_boxes(coord: str, boxes: list[coverage.Box]) -> bool:
    row, col = coverage.coord_rc(coord)
    return any(b[0] <= row <= b[2] and b[1] <= col <= b[3] for b in boxes)


@dataclass
class Members:
    """The other cells of a sheet's array formulas: each range with the
    recalculated copy's values for it (None: empty its cells), and the
    cells the batch itself wrote (left alone)."""
    ranges: list[tuple[coverage.Box, dict[str, Value] | None]] = field(default_factory=list)
    skipped: set[tuple[int, int]] = field(default_factory=set)

    def decide(self, coord: str) -> tuple[bool, Value | None]:
        """(is it an array formula's other cell, its new value or None)."""
        row, col = coverage.coord_rc(coord)
        if (row, col) in self.skipped:
            return False, None
        for box, engine in self.ranges:
            if box[0] <= row <= box[2] and box[1] <= col <= box[3]:
                return True, None if engine is None else engine.get(coord)
        return False, None


def read_members(data: bytes, boxes: list[coverage.Box]) -> dict[str, Value]:
    """The stored values of the plain cells inside ``boxes`` (an array
    formula's other cells, in a recalculated copy)."""
    out: dict[str, Value] = {}
    if not boxes:
        return out
    for coord, named, f, v in cells(data):
        if f is None and _in_boxes(coord, boxes):
            value = value_of(named.get(b"t", b"n"), v)
            if value is not None:
                out[coord] = value
    return out


def read_values(data: bytes) -> dict[str, Value]:
    """Each formula cell's stored value by coordinate: numbers, text,
    booleans and Excel's error values; a cell without one is left out."""
    out: dict[str, Value] = {}
    for _m, cell_attrs, _f, v in _formula_cells(data):
        named = dict(cell_attrs)
        ref = named.get(b"r")
        if not ref or v is None:
            continue
        kind = named.get(b"t", b"n")
        raw = v.group("t")
        value = _value(kind, raw)
        if value is not None:
            out[ref.decode("ascii", "replace")] = value
    return out


def _value(kind: bytes, raw: bytes | None) -> Value | None:
    if kind == b"str":
        return TEXT, raw or b""
    if raw is None or not raw.strip():
        return None
    if kind == b"n":
        try:
            float(raw)
        except ValueError:
            return None
        return NUMBER, raw.strip()
    if kind == b"b" and raw.strip() in (b"0", b"1"):
        return BOOL, raw.strip()
    if kind == b"e" and unescape(raw.strip()) in ERRORS:
        return ERROR, raw.strip()
    return None


def shown(value: Value) -> str:
    """A value as the readback shows it."""
    kind, raw = value
    if kind == BOOL:
        return "TRUE" if raw == b"1" else "FALSE"
    text = unescape(raw)
    if kind == NUMBER:
        number = float(text)
        if number.is_integer() and abs(number) < 1e15:
            return str(int(number))
        return f"{number:.15g}"
    return text


def has_cm_cells(data: bytes) -> set[str]:
    """The formula cells carrying cell metadata (a dynamic array in a file
    Excel saved)."""
    out = set()
    for _m, cell_attrs, _f, _v in _formula_cells(data):
        named = dict(cell_attrs)
        if b"cm" in named and named.get(b"r"):
            out.add(named[b"r"].decode("ascii", "replace"))
    return out


def dynamic_anchors(formulas: dict[str, str]) -> set[str]:
    """The cells whose formula spills: a spilling function's result reaches
    the cell."""
    return {coord for coord, text in formulas.items()
            if _SPILL_HINT_RE.search(text) and spills("=" + text)}


# ---------------------------------------------------------------------------
# Splice
# ---------------------------------------------------------------------------


def _attr_bytes(pairs: list[tuple[bytes, bytes]]) -> bytes:
    return b"".join(b' %s="%s"' % (name, value) for name, value in pairs)


def _new_cell(m: re.Match, cell_attrs: list[tuple[bytes, bytes]], f: re.Match, v: re.Match | None,
              value: Value | None, dynamic: bool) -> bytes | None:
    body = m.group("b")
    rest = body[:f.start()] + body[f.end():]
    if v is not None:
        vm = _V_RE.search(rest)
        rest = rest[:vm.start()] + rest[vm.end():] if vm else rest
    if rest.strip():
        return None  # more than a formula and its value: not openpyxl's shape, left alone
    prefix = m.group("p")
    keep_cm = not dynamic
    kept = [(k, val) for k, val in cell_attrs if k != b"t" and (keep_cm or k != b"cm")]
    if value is not None and value[0] != NUMBER:
        kept.append((b"t", value[0].encode()))
    if dynamic:
        kept.append((b"cm", b"1"))
    new_f = f.group(0)
    f_attrs = attrs(f.group("a"))
    if dynamic and dict(f_attrs).get(b"t") != b"array":
        if f.group("t") is None:
            return None
        ref = dict(cell_attrs).get(b"r", b"")
        f_attrs = [(k, val) for k, val in f_attrs if k not in (b"t", b"ref", b"si")]
        f_attrs += [(b"t", b"array"), (b"ref", ref)]
        new_f = b"<%sf%s>%s</%sf>" % (prefix, _attr_bytes(f_attrs), f.group("t"), prefix)
    if value is not None:
        new_v = b"<%sv>%s</%sv>" % (prefix, value[1], prefix)
    else:
        new_v = v.group(0) if v is not None else b""
    return b"<%sc%s>%s%s</%sc>" % (prefix, _attr_bytes(kept), new_f, new_v, prefix)


def _member_cell(m: re.Match, cell_attrs: list[tuple[bytes, bytes]], value: Value | None) -> bytes:
    """An array formula's other cell holding ``value``, or emptied (its
    style kept)."""
    prefix = m.group("p")
    kept = [(k, val) for k, val in cell_attrs if k != b"t"]
    if value is None:
        return b"<%sc%s/>" % (prefix, _attr_bytes(kept))
    if value[0] != NUMBER:
        kept.append((b"t", value[0].encode()))
    return b"<%sc%s><%sv>%s</%sv></%sc>" % (prefix, _attr_bytes(kept), prefix, value[1], prefix, prefix)


def splice_part(data: bytes, values: dict[str, Value], dynamic: set[str],
                members: Members | None = None) -> bytes:
    """A worksheet part with ``values`` written beside their formulas, the
    ``dynamic`` anchors turned into dynamic-array formulas, and the plain
    ``members`` cells of array formulas given their value or emptied
    (None)."""
    region = _region(data)
    if region is None:
        return data
    out: list[bytes] = []
    pos = region[0]
    for m in _CELL_RE.finditer(data, *region):
        body = m.group("b")
        cell_attrs = attrs(m.group("a"))
        ref = dict(cell_attrs).get(b"r", b"").decode("ascii", "replace")
        f = _F_RE.search(body) if body else None
        if f is None and members is not None and ref:
            member, value = members.decide(ref)
            if member:
                out.append(data[pos:m.start()])
                out.append(_member_cell(m, cell_attrs, value))
                pos = m.end()
            continue
        if f is None or (ref not in values and ref not in dynamic):
            continue
        new = _new_cell(m, cell_attrs, f, _V_RE.search(body), values.get(ref), ref in dynamic)
        if new is None:
            continue
        out.append(data[pos:m.start()])
        out.append(new)
        pos = m.end()
    if not out:
        return data
    return data[:region[0]] + b"".join(out) + data[pos:]


def _with_metadata(zin: zipfile.ZipFile, changed: dict[str, bytes]) -> None:
    """The dynamic-array metadata part, its content type and the workbook's
    relationship to it, added to ``changed`` unless the package has them."""
    workbook = workbook_part(zin)
    if workbook is None:
        return
    folder, name = posixpath.split(workbook)
    part = posixpath.join(folder, "metadata.xml")
    if part in zin.namelist():
        return
    types = etree.fromstring(zin.read("[Content_Types].xml"), _PARSER)
    etree.SubElement(types, f"{{{_CT_NS}}}Override", PartName="/" + part, ContentType=_METADATA_TYPE)
    rels_name = posixpath.join(folder, "_rels", name + ".rels")
    rels = etree.fromstring(zin.read(rels_name), _PARSER)
    taken = {rel.get("Id", "") for rel in rels}
    number = 1
    while f"rId{number}" in taken:
        number += 1
    etree.SubElement(rels, f"{{{_REL_NS}}}Relationship", Id=f"rId{number}", Type=_METADATA_REL,
                     Target="metadata.xml")
    changed["[Content_Types].xml"] = etree.tostring(types, xml_declaration=True, encoding="UTF-8",
                                                    standalone=True)
    changed[rels_name] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)
    changed[part] = _METADATA_XML


def splice(path: str, values: dict[str, dict[str, Value]], dynamic: dict[str, set[str]],
           members: dict[str, Members] | None = None) -> None:
    """The workbook at ``path`` rewritten with ``values`` (by sheet title,
    then cell) beside their formulas, the ``dynamic`` anchors spilling and
    the array formulas' other cells (``members``) filled or emptied; the
    parts it does not change keep their bytes."""
    members = members or {}
    with zipfile.ZipFile(path) as zin:
        targets = sheet_targets(zin)
        changed: dict[str, bytes] = {}
        for title, part in targets.items():
            written, anchors, others = values.get(title, {}), dynamic.get(title, set()), members.get(title)
            if written or anchors or others:
                data = zin.read(part)
                new = splice_part(data, written, anchors, others)
                if new is not data:
                    changed[part] = new
        if any(dynamic.values()):
            _with_metadata(zin, changed)
        if not changed:
            return
        fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".splice")
        try:
            with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w") as zout:
                names = set()
                for info in zin.infolist():
                    names.add(info.filename)
                    zout.writestr(info, changed[info.filename] if info.filename in changed
                                  else zin.read(info.filename))
                for name, data in changed.items():
                    if name not in names:
                        zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
            os.replace(temp, path)
        except BaseException:
            if os.path.exists(temp):
                os.unlink(temp)
            raise


# ---------------------------------------------------------------------------
# What worker B writes
# ---------------------------------------------------------------------------


@dataclass
class Carry:
    """What worker A knows about the values the file had: the formula
    cells whose text the batch left alone (by sheet title), the cells whose
    value it changed, and the names and tables it changed (case-folded)."""
    eligible: dict[str, list[str]] = field(default_factory=dict)
    changed: list[tuple[str, int, int]] = field(default_factory=list)
    names_changed: list[str] = field(default_factory=list)


@dataclass
class Filled:
    """What the splice wrote."""
    formulas: int = 0
    computed: int = 0
    carried: int = 0
    unsupported: list[str] = field(default_factory=list)
    environment: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    values: dict[coverage.Cell, Value] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _place(title: str, coord: str) -> str:
    from formats.xlsx.refs import quote_sheet

    return f"{quote_sheet(title)}!{coord}"


def _first(cells: list[str], shown_at_most: int = 3) -> str:
    more = f" and {len(cells) - shown_at_most} more" if len(cells) > shown_at_most else ""
    return ", ".join(cells[:shown_at_most]) + more


def _sheet_values(path: str, titles) -> dict[str, dict[str, Value]]:
    out: dict[str, dict[str, Value]] = {}
    with zipfile.ZipFile(path) as zf:
        targets = sheet_targets(zf)
        for title in titles:
            part = targets.get(title)
            if part is not None:
                out[title] = read_values(zf.read(part))
    return out


def _original_cm(path: str, titles) -> dict[str, set[str]]:
    with zipfile.ZipFile(path) as zf:
        if not any(n.endswith("metadata.xml") for n in zf.namelist()):
            return {}
        targets = sheet_targets(zf)
        return {t: has_cm_cells(zf.read(targets[t])) for t in titles if t in targets}


def _sheet_parts(path: str, titles) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        targets = sheet_targets(zf)
        return {title: zf.read(targets[title]) for title in titles if title in targets}


def fill(staged: str, *, expected: dict[str, int], computed: str | None = None, original: str | None = None,
         carry: Carry | None = None) -> Filled:
    """Write into the staged workbook the values that can be trusted:
    ``computed`` is the recalculated copy (None when there was none),
    ``original`` the file before the edit with ``carry`` worker A's facts
    about it; ``expected`` is worker A's formula count per sheet, which the
    staged file must match or nothing is written."""
    done = Filled()
    with zipfile.ZipFile(staged) as zf:
        parts = {title: zf.read(part) for title, part in sheet_targets(zf).items()}
        names = coverage.Names(defined_names(zf), tables(zf),
                               frozenset(carry.names_changed) if carry is not None else frozenset())
    formulas = {title: read_formulas(data) for title, data in parts.items()}
    arrays = {title: read_arrays(data) for title, data in parts.items()}
    del parts
    done.formulas = sum(len(texts) for texts in formulas.values())
    found = {title: len(texts) for title, texts in formulas.items() if texts}
    if found != {title: n for title, n in expected.items() if n}:
        done.notes.append("the formulas' values were not written: the saved workbook did not match the edit, "
                          "so Excel computes them when the file opens")
        return done
    values: dict[str, dict[str, Value]] = {title: {} for title in formulas}
    computed_parts: dict[str, bytes] = {}
    from_engine: set[coverage.Cell] = set()
    if computed is not None:
        computed_parts = _sheet_parts(computed, formulas)
        results = {title: read_values(data) for title, data in computed_parts.items()}
        supported = functions.supported_by(application(computed))
        from_engine = _computed(done, formulas, values, results, arrays, names, supported)
    kept: set[coverage.Cell] = set()
    if original is not None and carry is not None:
        kept = _carried(done, formulas, values, original, carry, arrays, names)
    dynamic = {title: dynamic_anchors(texts) for title, texts in formulas.items()}
    if original is not None and carry is not None:
        for title, cm_cells in _original_cm(original, carry.eligible).items():
            dynamic.setdefault(title, set()).update(cm_cells & set(carry.eligible.get(title, ())))
    members = _members(arrays, from_engine, kept, computed_parts, carry)
    splice(staged, values, dynamic, members)
    for title, written in values.items():
        for coord, value in written.items():
            done.values[(title, coord)] = value
    return done


def _members(arrays: dict[str, dict[str, coverage.Box]], from_engine: set[coverage.Cell],
             kept: set[coverage.Cell], computed_parts: dict[str, bytes],
             carry: Carry | None) -> dict[str, Members]:
    """The other cells of each array formula: the recalculated copy's
    values when the anchor's value came from it, nothing when the anchor
    kept its value from before (its cells kept theirs), emptied otherwise.
    A cell the batch itself wrote is left alone."""
    out: dict[str, Members] = {}
    for title, anchors in arrays.items():
        ranges = [(box, (title, anchor) in from_engine) for anchor, box in anchors.items()
                  if (title, anchor) not in kept]
        if not ranges:
            continue
        engine = (read_members(computed_parts[title], [box for box, trusted in ranges if trusted])
                  if title in computed_parts else {})
        skipped = {(row, col) for name, row, col in (carry.changed if carry is not None else ()) if name == title}
        skipped |= {coverage.coord_rc(anchor) for anchor in anchors}
        out[title] = Members([(box, engine if trusted else None) for box, trusted in ranges], skipped)
    return out


def application(path: str) -> str:
    """The Application a package names (``docProps/app.xml``), "" when none."""
    with zipfile.ZipFile(path) as zf:
        try:
            data = zf.read("docProps/app.xml")
        except KeyError:
            return ""
    match = _APPLICATION_RE.search(data[:_APPLICATION_PEEK])
    return match.group(1).decode("utf-8", "replace") if match else ""


def _computed(done: Filled, formulas: dict[str, dict[str, str]], values: dict[str, dict[str, Value]],
              results: dict[str, dict[str, Value]], arrays: dict[str, dict[str, coverage.Box]],
              names: coverage.Names, supported: frozenset[str] = functions.SUPPORTED) -> set[coverage.Cell]:
    lacking = {(title, coord) for title, texts in formulas.items() for coord, text in texts.items()
               if coverage.lacks(text, title, names, supported=supported)}
    untrusted = lacking
    if lacking:
        spread = coverage.spread(formulas, formula_seeds=lacking, arrays=arrays, names=names)
    else:
        spread = set()
    # A #NAME? the table does not explain (the cell calls functions, every
    # one in the table, reads only names the workbook defines, and reads no
    # cell that lacks one): the engine lacks a function its table names
    # (another version), so it is one the engine lacks too. A name defined
    # nowhere stays an error: Excel answers #NAME? as well.
    unexplained = {(title, coord) for title, texts in formulas.items() for coord, text in texts.items()
                   if results.get(title, {}).get(coord) == (ERROR, NAME_ERROR) and coverage.called(text)
                   and not coverage.reads_undefined(text, title, names)} - (spread or set())
    if spread is not None and unexplained:
        lacking |= unexplained
        spread = coverage.spread(formulas, formula_seeds=lacking, arrays=arrays, names=names)
    if lacking:
        if spread is None:
            done.notes.append(f"the workbook holds more than {coverage.MAX_FORMULAS:,} formulas, too many to check "
                              "which recalculated values hold, so none was written: Excel computes them when the "
                              "file opens")
            untrusted = {(title, coord) for title, texts in formulas.items() for coord in texts}
        else:
            untrusted = spread
        server = {key for key in lacking if coverage.describes_server(formulas[key[0]][key[1]])}
        done.environment = [_place(title, coord) for title, coord in sorted(server)]
        done.unsupported = [_place(title, coord) for title, coord in sorted(lacking - server)]
    written: set[coverage.Cell] = set()
    for title, texts in formulas.items():
        got = results.get(title, {})
        for coord in texts:
            value = got.get(coord)
            if value is None or (title, coord) in untrusted:
                continue
            values[title][coord] = value
            written.add((title, coord))
            done.computed += 1
            if value[0] == ERROR:
                done.errors.append((_place(title, coord), unescape(value[1])))
    return written


def _carried(done: Filled, formulas: dict[str, dict[str, str]], values: dict[str, dict[str, Value]],
             original: str, carry: Carry, arrays: dict[str, dict[str, coverage.Box]],
             names: coverage.Names) -> set[coverage.Cell]:
    wanted = {(title, coord) for title, coords in carry.eligible.items() for coord in coords
              if coord in formulas.get(title, {}) and coord not in values.get(title, {})}
    if not wanted:
        return set()
    before = _sheet_values(original, {title for title, _ in wanted})
    have = {(title, coord) for title, coord in wanted if coord in before.get(title, {})}
    if not have:
        return set()
    tainted = coverage.spread(formulas, cells=carry.changed, anywhere=bool(carry.names_changed), arrays=arrays,
                              names=names)
    if tainted is None:
        done.notes.append(f"the workbook holds more than {coverage.MAX_FORMULAS:,} formulas, too many to check "
                          "which values from before this edit still hold, so none was kept")
        return set()
    kept = have - tainted
    for title, coord in sorted(kept):
        values[title][coord] = before[title][coord]
        done.carried += 1
    return kept
