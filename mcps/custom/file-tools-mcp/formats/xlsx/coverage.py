"""Which formulas a recalculated value can be trusted for, and which values
a workbook had before an edit still hold.

A computed value is trusted when every function the formula calls is one
the engine computes (``functions.SUPPORTED``) and every formula it reads is
trusted too: LibreOffice answers #NAME? for a function it lacks, and a
formula reading that cell inherits a wrong value. A value the file had
before an edit still holds when the formula is the same text in the same
cell and nothing it reads changed. Both are the same walk: some cells are
tainted, then every formula that reads a tainted cell, to a fixed point.

A defined name stands for what it holds: an absolute range (Sheet!$B$1),
a constant, or a formula over such ranges, followed a few names deep. A
table stands for its whole range. A name that is relative, unqualified or
changed by the edit, a table reference with no table name, and anything
else not resolved may read anything.

The walk follows a reverse index: each small box a formula reads is indexed
cell by cell, so a tainted cell finds its readers at once; a larger box
(a column, a long range) is checked against the tainted cells once per
round. Its cost bounds the walk, never the depth of a chain: past
CHECK_BUDGET large-box checks, every large-box reader still pending counts
as tainted (and what reads it), which withholds more values but never
trusts a wrong one. Only a workbook past MAX_FORMULAS is not walked at all.

The probe helpers build the function table: one call of every known
function in a workbook, recalculated, and read back (a call the engine
does not know comes back #NAME?).
"""

import bisect
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from formats.xlsx.formulas import (
    FUTURE_FUNCTIONS,
    SPILLING,
    UNSAFE_FUNCTIONS,
    WORKSHEET_FUNCTIONS,
    bare_name,
)
from formats.xlsx.functions import SUPPORTED
from formats.xlsx.refs import MAX_COL, MAX_ROW, Part, locate_tokens, parse_ref

MAX_FORMULAS = 100_000
CHECK_BUDGET = 2_000_000
# A box of at most this many cells is indexed cell by cell.
SMALL_BOX = 64

Box = tuple[int, int, int, int]
Cell = tuple[str, str]

# Functions whose value describes the computer that calculates the
# workbook (its folders, its system): the engine's answer is the server's,
# never the person's, so it is not written.
ENVIRONMENT = frozenset({"CELL", "INFO"})
# Functions whose result depends on more than the cells they name: a
# formula calling one may read anything.
_ANYWHERE = frozenset({"INDIRECT", "OFFSET", "NOW", "TODAY", "RAND", "RANDBETWEEN", "RANDARRAY"}) | ENVIRONMENT
# A name followed by an opening bracket. String literals are not skipped:
# a function name inside a string only ever withholds a value.
_CALL_RE = re.compile(r"([A-Za-z_\\][A-Za-z0-9_.\\]*)\s*\(")
_COORD_RE = re.compile(r"([A-Z]{1,3})([0-9]{1,7})")
_NAME_RE = re.compile(r"[^\W\d][\w.]*")
_TABLE_REF_RE = re.compile(r"([^\W\d][\w.]*)\[")
# How many names deep a name is followed (a name holding a name).
NAME_DEPTH = 5


@dataclass
class Names:
    """The workbook's defined names (by (sheet title or None, name), both
    case-folded), its tables (by name: sheet title and range), and the
    names and tables the edit changed (a value from before that read one
    no longer holds)."""
    defined: dict[tuple[str | None, str], str] = field(default_factory=dict)
    tables: dict[str, tuple[str, str]] = field(default_factory=dict)
    stale: frozenset[str] = frozenset()

    def definition(self, name: str, sheet: str) -> str | None:
        key = name.casefold()
        local = self.defined.get((sheet.casefold(), key))
        return local if local is not None else self.defined.get((None, key))


def called(text: str) -> set[str]:
    """The functions ``text`` calls, bare and upper-cased."""
    return {bare_name(m.group(1)) for m in _CALL_RE.finditer(text)}


def computes(text: str, supported: frozenset[str] = SUPPORTED) -> bool:
    """True when the engine (``supported``: its functions) has every
    function ``text`` calls."""
    return called(text) <= supported


def describes_server(text: str) -> bool:
    """True when ``text`` calls a function whose value describes the
    computer that calculates it."""
    return bool(called(text) & ENVIRONMENT)


def lacks(text: str, sheet: str, names: "Names | None" = None, depth: int = 0,
          supported: frozenset[str] = SUPPORTED) -> bool:
    """True when ``text`` calls a function the engine (``supported``: its
    functions) lacks, itself or through the definition of a name it reads
    (followed NAME_DEPTH deep): the engine answers #NAME? for the name as
    for the function."""
    if not computes(text, supported):
        return True
    if names is None or not names.defined or depth >= NAME_DEPTH:
        return False
    tokens = locate_tokens(text if text.startswith("=") else "=" + text)
    for tok in tokens or ():
        if tok.type != "OPERAND" or tok.subtype != "RANGE" or not _NAME_RE.fullmatch(tok.value):
            continue
        if parse_ref(tok.value) is not None:
            continue
        definition = names.definition(tok.value, sheet)
        if definition is not None and lacks(definition, sheet, names, depth + 1, supported):
            return True
    return False


def reads_undefined(text: str, sheet: str, names: "Names | None" = None) -> bool:
    """True when ``text`` reads a name the workbook defines nowhere (no
    defined name, no table): Excel answers #NAME? for it as well."""
    defined = names.defined if names is not None else {}
    tables = names.tables if names is not None else {}
    tokens = locate_tokens(text if text.startswith("=") else "=" + text)
    for tok in tokens or ():
        if tok.type != "OPERAND" or tok.subtype != "RANGE" or not _NAME_RE.fullmatch(tok.value):
            continue
        if parse_ref(tok.value) is not None or tok.value.upper() in ("TRUE", "FALSE"):
            continue
        key = tok.value.casefold()
        if (sheet.casefold(), key) not in defined and (None, key) not in defined and key not in tables:
            return True
    return False


def coord_rc(coord: str) -> tuple[int, int]:
    """(row, column) of an A1 coordinate."""
    m = _COORD_RE.fullmatch(coord)
    if m is None:
        raise ValueError(f"{coord} is not a cell coordinate")
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col


def range_box(ref: str) -> Box | None:
    """The box of an A1 range ("D1:D3", "B2"), or None."""
    ends = [_COORD_RE.fullmatch(end) for end in ref.upper().replace("$", "").split(":")]
    if not 1 <= len(ends) <= 2 or None in ends:
        return None
    (r1, c1), (r2, c2) = coord_rc(ends[0].group(0)), coord_rc(ends[-1].group(0))
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def _box(parts: tuple[Part, ...]) -> Box | None:
    kinds = {p.kind for p in parts}
    if kinds == {"cell"}:
        rows = [p.row for p in parts]
        cols = [p.col for p in parts]
        return min(rows), min(cols), max(rows), max(cols)
    if kinds == {"col"}:
        cols = [p.col for p in parts]
        return 1, min(cols), MAX_ROW, max(cols)
    if kinds == {"row"}:
        rows = [p.row for p in parts]
        return min(rows), 1, max(rows), MAX_COL
    return None


def _area(box: Box) -> int:
    return (box[2] - box[0] + 1) * (box[3] - box[1] + 1)


def reads(text: str, sheet: str, names: Names | None = None, depth: int = 0) -> list[tuple[str, Box]] | None:
    """The boxes ``text`` reads, each with its sheet (case-folded); None
    when it may read any cell: a name or a table not resolved through
    ``names``, INDIRECT, OFFSET or a volatile function, a 3-D or external
    reference, a text the tokenizer refuses. Inside a name's definition
    (``depth`` > 0) every reference must be qualified and absolute."""
    if called(text) & _ANYWHERE:
        return None
    tokens = locate_tokens(text if text.startswith("=") else "=" + text)
    if tokens is None:
        return None
    out: list[tuple[str, Box]] = []
    for tok in tokens:
        if tok.type != "OPERAND" or tok.subtype != "RANGE":
            continue
        ref = parse_ref(tok.value)
        if ref is None:
            resolved = _resolve(tok.value, sheet, names, depth)
            if resolved is None:
                return None
            out.extend(resolved)
            continue
        if ref.external or ref.three_d:
            return None
        if ref.dead:
            continue
        if depth and (ref.sheet is None or not all(p.col_abs or p.kind == "row" for p in ref.parts)
                      or not all(p.row_abs or p.kind == "col" for p in ref.parts)):
            return None
        box = _box(ref.parts)
        if box is None:
            return None
        out.append(((ref.sheet if ref.sheet is not None else sheet).casefold(), box))
    return out


def _resolve(token: str, sheet: str, names: Names | None, depth: int) -> list[tuple[str, Box]] | None:
    """What a name or a table reference reads, or None."""
    if names is None or depth >= NAME_DEPTH:
        return None
    table = _TABLE_REF_RE.match(token)
    if table is not None:
        name = table.group(1).casefold()
        found = names.tables.get(name)
        if found is None or name in names.stale:
            return None
        box = range_box(found[1])
        return None if box is None else [(found[0].casefold(), box)]
    if not _NAME_RE.fullmatch(token) or token.casefold() in names.stale:
        return None
    definition = names.definition(token, sheet)
    if definition is None:
        return None
    return reads(definition, sheet, names, depth + 1)


class _Tainted:
    """Tainted cells by sheet and column, and tainted boxes (array ranges),
    searchable by box."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[int, list[int]]] = {}
        self.boxes: dict[str, list[Box]] = {}

    def add(self, sheet: str, box: Box) -> None:
        if _area(box) > 1:
            self.boxes.setdefault(sheet, []).append(box)
            return
        rows = self.rows.setdefault(sheet, {}).setdefault(box[1], [])
        at = bisect.bisect_left(rows, box[0])
        if at == len(rows) or rows[at] != box[0]:
            rows.insert(at, box[0])

    def hits(self, sheet: str, box: Box) -> bool:
        r1, c1, r2, c2 = box
        for other in self.boxes.get(sheet, ()):
            if other[0] <= r2 and r1 <= other[2] and other[1] <= c2 and c1 <= other[3]:
                return True
        columns = self.rows.get(sheet)
        if not columns:
            return False
        if c2 - c1 + 1 <= len(columns):
            candidates = (columns.get(c) for c in range(c1, c2 + 1))
        else:
            candidates = (rows for c, rows in columns.items() if c1 <= c <= c2)
        for rows in candidates:
            if rows:
                at = bisect.bisect_left(rows, r1)
                if at < len(rows) and rows[at] <= r2:
                    return True
        return False


def spread(formulas: dict[str, dict[str, str]], *, cells: Iterable[tuple[str, int, int]] = (),
           formula_seeds: Iterable[Cell] = (), anywhere: bool = False,
           arrays: dict[str, dict[str, Box]] | None = None, names: Names | None = None) -> set[Cell] | None:
    """The formula cells (sheet title, coordinate) tainted by the seeds:
    ``cells`` (title, row, column), the formula cells ``formula_seeds``,
    and, with ``anywhere``, every formula that may read any cell. A
    formula reading anything is tainted by any seed. ``arrays`` gives an
    array formula's whole range (by title, then anchor): what reads one of
    its cells reads the formula; ``names`` resolves names and tables. None
    for a workbook past MAX_FORMULAS: every formula then counts as
    tainted."""
    tainted: set[Cell] = set(formula_seeds)
    changed = list(cells)
    if not (anywhere or tainted or changed):
        return set()
    if sum(len(f) for f in formulas.values()) > MAX_FORMULAS:
        return None
    arrays = arrays or {}
    by_cell: dict[str, dict[tuple[int, int], list[Cell]]] = {}
    large: list[tuple[Cell, str, Box]] = []
    queue: deque[Cell] = deque(tainted)
    for title, texts in formulas.items():
        for coord, text in texts.items():
            key = (title, coord)
            if key in tainted:
                continue
            boxes = reads(text, title, names)
            if boxes is None:
                tainted.add(key)
                queue.append(key)
                continue
            for sheet, box in boxes:
                if _area(box) > SMALL_BOX:
                    large.append((key, sheet, box))
                    continue
                index = by_cell.setdefault(sheet, {})
                for row in range(box[0], box[2] + 1):
                    for col in range(box[1], box[3] + 1):
                        index.setdefault((row, col), []).append(key)
    marks = _Tainted()

    def reach(sheet: str, box: Box) -> None:
        marks.add(sheet, box)
        index = by_cell.get(sheet)
        if not index:
            return
        if _area(box) <= len(index):
            found = (index.get((r, c), ()) for r in range(box[0], box[2] + 1) for c in range(box[1], box[3] + 1))
        else:
            found = (readers for (r, c), readers in index.items()
                     if box[0] <= r <= box[2] and box[1] <= c <= box[3])
        for readers in found:
            for reader in readers:
                if reader not in tainted:
                    tainted.add(reader)
                    queue.append(reader)

    for title, row, col in changed:
        reach(title.casefold(), (row, col, row, col))
    budget = CHECK_BUDGET
    while True:
        while queue:
            title, coord = queue.popleft()
            row, col = coord_rc(coord)
            reach(title.casefold(), arrays.get(title, {}).get(coord) or (row, col, row, col))
        pending = []
        for entry in large:
            key, sheet, box = entry
            if key in tainted:
                continue
            budget -= 1
            if budget < 0 or marks.hits(sheet, box):
                tainted.add(key)
                queue.append(key)
            else:
                pending.append(entry)
        large = pending
        if not queue:
            return tainted


# ---------------------------------------------------------------------------
# The probe behind functions.SUPPORTED
# ---------------------------------------------------------------------------


def probe_names() -> list[str]:
    """Every function a probe asks about: openpyxl's list of Excel's
    functions and every newer one this module knows, without those that
    reach the network or run a program and those that describe the
    computer (their value is never the engine's to give)."""
    from openpyxl.utils.formulas import FORMULAE

    known = set(FORMULAE) | FUTURE_FUNCTIONS | WORKSHEET_FUNCTIONS | SPILLING
    return sorted(n for n in known if n not in UNSAFE_FUNCTIONS and n not in ENVIRONMENT)


def stored_call(name: str) -> str:
    """``=NAME()`` with the prefix Excel stores the function under."""
    if name in WORKSHEET_FUNCTIONS:
        return f"=_xlfn._xlws.{name}()"
    if name in FUTURE_FUNCTIONS or name in SPILLING:
        return f"=_xlfn.{name}()"
    return f"={name}()"


def write_probe(path: str, names: list[str]) -> None:
    """A workbook with one call of each name, down column A."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row, name in enumerate(names, start=1):
        ws.cell(row=row, column=1, value=stored_call(name))
    wb.save(path)


def read_probe(values: dict[str, tuple[str, bytes]], names: list[str]) -> frozenset[str]:
    """The names the engine computes, from the recalculated probe's values
    (``recalc.read_values`` of its sheet): any answer but #NAME?."""
    out = set()
    for row, name in enumerate(names, start=1):
        value = values.get(f"A{row}")
        if value is not None and value != ("e", b"#NAME?"):
            out.add(name)
    return frozenset(out)
