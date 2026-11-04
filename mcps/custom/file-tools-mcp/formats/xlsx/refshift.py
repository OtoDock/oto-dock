"""Structural edits that keep a workbook's references on the same cells.

Row and column insert, delete and move, sheet rename and sheet delete, the
way Excel does them: every formula, defined name, validation and
conditional format, chart series, table, merged range, filter, pivot,
dimension, freeze pane, picture anchor, hyperlink and page break that points
into the edited sheet follows its cells. openpyxl moves nothing but the
cells (and builds the whole used rectangle to do it), so the cells are
re-keyed sparsely here and everything else is rewritten.
"""

import re
from collections.abc import Callable, Iterator
from copy import copy
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from formats.xlsx.refs import (
    MAX_COL,
    MAX_ROW,
    REF_ERROR,
    Part,
    Ref,
    col_letters,
    quote_sheet,
    rewrite_refs,
)


class Axis(Enum):
    ROW = "row"
    COL = "column"


class EditKind(Enum):
    INSERT = "insert"
    DELETE = "delete"
    MOVE = "move"


@dataclass(frozen=True)
class Edit:
    """One structural edit of `sheet`: lines [at, at + count) inserted
    before line `at`, deleted, or moved to land before line `to` (in the
    numbering before the move)."""
    sheet: str
    axis: Axis
    kind: EditKind
    at: int
    count: int = 1
    to: int = 0

    @property
    def limit(self) -> int:
        return MAX_ROW if self.axis is Axis.ROW else MAX_COL

    @property
    def attr(self) -> str:
        return "row" if self.axis is Axis.ROW else "col"

    def map(self, i: int) -> int | None:
        """Where line i goes; None when it is deleted or pushed off the sheet."""
        at, n = self.at, self.count
        if self.kind is EditKind.INSERT:
            j = i + n if i >= at else i
            return j if j <= self.limit else None
        if self.kind is EditKind.DELETE:
            if i < at:
                return i
            return None if i < at + n else i - n
        to = self.to
        if to > at + n:
            if at <= i < at + n:
                return i + (to - at - n)
            if at + n <= i < to:
                return i - n
        elif to < at:
            if at <= i < at + n:
                return i - (at - to)
            if to <= i < at:
                return i + n
        return i

    def unmap(self, j: int) -> int | None:
        """The line that ends up at j, or None for an inserted line."""
        at, n = self.at, self.count
        if self.kind is EditKind.INSERT:
            if j < at:
                return j
            return None if j < at + n else j - n
        if self.kind is EditKind.DELETE:
            return j if j < at else j + n
        for i in range(min(at, self.to), max(at + n, self.to) + 1):
            if self.map(i) == j:
                return i
        return j

    def map_span(self, lo: int, hi: int) -> tuple[int, int] | None:
        """A range's new bounds by Excel's rule: an insert inside grows it,
        a delete keeps the surviving lines; a move keeps a range whose lines
        still form one block covering that block, and otherwise takes the
        moved block out and puts it back. None when nothing is left."""
        at, n = self.at, self.count
        if self.kind is EditKind.INSERT:
            new_lo = lo + n if lo >= at else lo
            new_hi = hi + n if hi >= at else hi
            if new_lo > self.limit:
                return None
            return new_lo, min(new_hi, self.limit)
        if self.kind is EditKind.DELETE:
            new_lo = lo if lo < at else (at if lo < at + n else lo - n)
            new_hi = hi - n if hi >= at + n else (at - 1 if hi >= at else hi)
            return (new_lo, new_hi) if new_lo <= new_hi else None
        image = self._moved_image(lo, hi)
        if image is not None:
            return image
        removed = Edit(self.sheet, self.axis, EditKind.DELETE, at, n).map_span(lo, hi)
        if removed is None:
            return None
        dest = self.to if self.to < at else self.to - n
        return Edit(self.sheet, self.axis, EditKind.INSERT, dest, n).map_span(*removed)

    def _moved_image(self, lo: int, hi: int) -> tuple[int, int] | None:
        """For a move: the block [lo, hi]'s lines land on, when they still
        form one block (a reorder inside a range keeps the range whole);
        None when the move splits them."""
        at, n, to = self.at, self.count, self.to
        if to > at + n:
            regions = [(1, at - 1), (at, at + n - 1), (at + n, to - 1), (to, self.limit)]
        else:
            regions = [(1, to - 1), (to, at - 1), (at, at + n - 1), (at + n, self.limit)]
        pieces = []
        for a, b in regions:
            x, y = max(a, lo), min(b, hi)
            if x <= y:
                pieces.append((self.map(x), self.map(y)))
        pieces.sort()
        for (_, prev_hi), (next_lo, _) in zip(pieces, pieces[1:], strict=False):
            if next_lo != prev_hi + 1:
                return None
        return pieces[0][0], pieces[-1][1]

    def describe(self) -> str:
        what = "rows" if self.axis is Axis.ROW else "columns"
        if self.axis is Axis.ROW:
            first, last = str(self.at), str(self.at + self.count - 1)
        else:
            first, last = col_letters(self.at), col_letters(self.at + self.count - 1)
        span = first if self.count == 1 else f"{first}-{last}"
        return f"{what} {span}"


_SINGULAR = {
    "formulas": "formula", "names": "name", "merged ranges": "merged range",
    "validations": "validation", "conditional formats": "conditional format",
    "tables": "table", "table formulas": "table formula", "hyperlinks": "hyperlink",
    "row heights": "row height", "column widths": "column width",
    "freeze panes": "freeze pane", "pictures and charts": "picture or chart",
    "filters": "filter", "print settings": "print setting",
    "pivot tables": "pivot table", "pivot sources": "pivot source",
}


@dataclass
class ShiftReport:
    """What one structural edit changed, for the result text."""
    counts: dict[str, int] = field(default_factory=dict)
    ref_errors: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def bump(self, what: str, n: int = 1) -> None:
        if n:
            self.counts[what] = self.counts.get(what, 0) + n

    def summary(self) -> str:
        parts = [f"{n} {what if n != 1 else _SINGULAR.get(what, what)}"
                 for what, n in self.counts.items()]
        text = (": " + ", ".join(parts) + " updated") if parts else ""
        if self.ref_errors:
            shown = ", ".join(self.ref_errors[:5])
            more = f" and {len(self.ref_errors) - 5} more" if len(self.ref_errors) > 5 else ""
            n = len(self.ref_errors)
            text += (f"; {n} reference{'s' if n != 1 else ''} became {REF_ERROR} "
                     f"({shown}{more})")
        return text

    def warnings(self) -> list[str]:
        out = []
        if self.unparsed:
            shown = ", ".join(self.unparsed[:5])
            more = f" and {len(self.unparsed) - 5} more" if len(self.unparsed) > 5 else ""
            out.append(
                f"{shown}{more}: these formulas could not be parsed, so their "
                "references were not updated. Check them by hand."
            )
        return out


# ---------------------------------------------------------------------------
# One reference
# ---------------------------------------------------------------------------


def _dead(ref: Ref) -> str:
    """A reference to cells that are gone. Excel writes `Sheet2!#REF!` and
    `#REF!A1`, but LibreOffice (and so Collabora) reads those as #NAME? or
    Err:509; the plain form reads as #REF! in both."""
    return REF_ERROR


def _targets(ref: Ref, own: str | None, sheet: str) -> bool:
    if ref.dead or ref.external or ref.three_d:
        return False
    target = ref.sheet if ref.sheet is not None else own
    return target is not None and target.casefold() == sheet.casefold()


def shift_ref(ref: Ref, own: str | None, edit: Edit) -> str | None:
    """New text for a reference after `edit`, or None when it is unchanged.
    `own` is the sheet an unqualified reference belongs to (None for text
    where an unqualified reference has no sheet, as a workbook name)."""
    if not _targets(ref, own, edit.sheet):
        return None
    attr = edit.attr
    parts = list(ref.parts)
    other_kind = "col" if edit.axis is Axis.ROW else "row"
    if all(p.kind == other_kind for p in parts):
        return None
    if len(parts) == 2:
        a, b = (getattr(p, attr) for p in parts)
        span = edit.map_span(min(a, b), max(a, b))
        if span is None:
            return _dead(ref)
        lo, hi = span
        new_a, new_b = (lo, hi) if a <= b else (hi, lo)
        parts = [replace(parts[0], **{attr: new_a}), replace(parts[1], **{attr: new_b})]
    else:
        new_parts = []
        for p in parts:
            j = edit.map(getattr(p, attr))
            if j is None:
                return _dead(ref)
            new_parts.append(replace(p, **{attr: j}))
        parts = new_parts
    new = replace(ref, parts=tuple(parts), at=ref.at)
    text = new.text()
    return None if text == ref.text() else text


def copy_ref(ref: Ref, dcol: int, drow: int) -> str | None:
    """A reference in a formula copied `dcol` columns right and `drow` rows
    down, by Excel's copy rule: relative parts move, $-anchored parts stay,
    a reference pushed past an edge of the sheet becomes #REF!. Returns None
    when it does not change."""
    if ref.dead:
        return None
    parts = []
    for part in ref.parts:
        col, row = part.col, part.row
        if part.kind in ("cell", "col") and not part.col_abs:
            col += dcol
        if part.kind in ("cell", "row") and not part.row_abs:
            row += drow
        if (part.kind != "row" and not 1 <= col <= MAX_COL) or (part.kind != "col" and not 1 <= row <= MAX_ROW):
            return REF_ERROR
        parts.append(replace(part, col=col, row=row))
    text = replace(ref, parts=tuple(parts)).text()
    return None if text == ref.text() else text


# ---------------------------------------------------------------------------
# Structured table references
# ---------------------------------------------------------------------------

_STRUCT_RE = re.compile(r"^([^\W\d][\w.]*)\[(.*)\]$", re.DOTALL)
_STRUCT_COL_RE = re.compile(r"\[((?:[^\[\]']|'.)+)\]")


def _struct_kill(text: str, dead_tables: set[str],
                 dead_cols: dict[str, set[str]]) -> str | None:
    """`#REF!` for a structured reference to a deleted table or column."""
    m = _STRUCT_RE.match(text)
    if m is None:
        return None
    table = m.group(1).casefold()
    if table in dead_tables:
        return REF_ERROR
    gone = dead_cols.get(table)
    if not gone:
        return None
    inner = "[" + m.group(2) + "]"
    names = [c for c in _STRUCT_COL_RE.findall(inner) if not c.startswith("#")]
    if not names and not m.group(2).startswith("[") and not m.group(2).startswith("#"):
        names = [m.group(2)]
    for name in names:
        if re.sub(r"'(.)", r"\1", name).strip().casefold() in gone:
            return REF_ERROR
    return None


# ---------------------------------------------------------------------------
# Walking every formula of the workbook
# ---------------------------------------------------------------------------


@dataclass
class _Rewriter:
    """Rewrites formula text through one reference function, caching by
    (text, own sheet) and counting what changed."""
    fn: Callable[[Ref, str | None], str | None]
    report: ShiftReport
    other: Callable[[str], str | None] | None = None
    prefilter: Callable[[str, str | None], bool] | None = None
    cache: dict = field(default_factory=dict)

    def __call__(self, text: str, own: str | None, *, bare: bool, where: str) -> str | None:
        """New text, or None when unchanged."""
        if not isinstance(text, str) or not text:
            return None
        if self.prefilter is not None and not self.prefilter(text, own):
            return None
        key = (text, own, bare)
        if key in self.cache:
            new = self.cache[key]
        else:
            new = rewrite_refs(text, lambda r: self.fn(r, own), bare=bare, other=self.other)
            self.cache[key] = new
        if new is None:
            self.report.unparsed.append(where)
            return None
        if new is text or new == text:
            return None
        if new.count(REF_ERROR) > text.count(REF_ERROR):
            self.report.ref_errors.append(where)
        return new


def _cell_formula_slots(wb) -> Iterator[tuple[Any, Any, str]]:
    """(worksheet, cell, kind) for every formula cell; kind is "text",
    "array" or "table" (a what-if data table)."""
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    for ws in wb.worksheets:
        for cell in list(ws._cells.values()):
            if cell.data_type != "f":
                continue
            value = cell.value
            if isinstance(value, ArrayFormula):
                yield ws, cell, "array"
            elif isinstance(value, DataTableFormula):
                yield ws, cell, "table"
            elif isinstance(value, str):
                yield ws, cell, "text"


def _rewrite_everything(wb, rw: _Rewriter, *, sqref_sheet: str | None = None,
                        charts: bool = True) -> None:
    """Pass every formula-bearing text of the workbook through `rw`.
    Validation and conditional-format formulas of `sqref_sheet` are left to
    the caller, which re-anchors them while moving their ranges."""
    for ws in wb.worksheets:
        for cell in ws._cells.values():
            link = getattr(cell, "hyperlink", None)
            if link is not None and link.location:
                where = f"{quote_sheet(ws.title)}!{cell.coordinate}"
                new = rw(link.location, ws.title, bare=True, where=where)
                if new is not None:
                    link.location = new
                    rw.report.bump("hyperlinks")
    for ws, cell, kind in _cell_formula_slots(wb):
        where = f"{quote_sheet(ws.title)}!{cell.coordinate}"
        if kind == "text":
            new = rw(cell.value, ws.title, bare=False, where=where)
            if new is not None:
                cell.value = new
                rw.report.bump("formulas")
        elif kind == "array":
            new = rw(cell.value.text, ws.title, bare=False, where=where)
            if new is not None:
                cell.value.text = new
                rw.report.bump("formulas")
        else:
            for attr in ("r1", "r2"):
                old = getattr(cell.value, attr, None)
                new = rw(old, ws.title, bare=True, where=where) if old else None
                if new is not None:
                    setattr(cell.value, attr, new)

    for name, defn in list(wb.defined_names.items()):
        new = rw(defn.attr_text, None, bare=True, where=f"name {name}")
        if new is not None:
            defn.attr_text = new
            rw.report.bump("names")
    for ws in wb.worksheets:
        for name, defn in list(ws.defined_names.items()):
            new = rw(defn.attr_text, ws.title, bare=True, where=f"name {name}")
            if new is not None:
                defn.attr_text = new
                rw.report.bump("names")

    for ws in wb.worksheets:
        if sqref_sheet is not None and ws.title == sqref_sheet:
            continue
        _rewrite_dv_cf(ws, rw)

    for ws in wb.worksheets:
        for table in ws.tables.values():
            for col in table.tableColumns:
                for attr in ("calculatedColumnFormula", "totalsRowFormula"):
                    tf = getattr(col, attr)
                    if tf is not None and tf.attr_text:
                        new = rw(tf.attr_text, ws.title, bare=True,
                                 where=f"table {table.displayName}")
                        if new is not None:
                            tf.attr_text = new
                            rw.report.bump("table formulas")

    if not charts:
        return
    for holder in list(wb.worksheets) + list(getattr(wb, "chartsheets", [])):
        for chart in getattr(holder, "_charts", []):
            for series in chart.series:
                for ref in _series_refs(series):
                    new = rw(ref.f, holder.title, bare=True,
                             where=f"chart on {quote_sheet(holder.title)}")
                    if new is not None:
                        ref.f = new
                        rw.report.bump("chart series")


def _rewrite_dv_cf(ws, rw: _Rewriter) -> None:
    for dv in ws.data_validations.dataValidation:
        for attr in ("formula1", "formula2"):
            text = getattr(dv, attr)
            new = rw(text, ws.title, bare=True, where=f"validation on {ws.title}") if text else None
            if new is not None:
                setattr(dv, attr, new)
                rw.report.bump("validations")
    for cf in ws.conditional_formatting:
        for rule in cf.rules:
            changed = False
            if rule.formula:
                out = []
                for text in rule.formula:
                    new = rw(text, ws.title, bare=True, where=f"conditional format on {ws.title}")
                    out.append(text if new is None else new)
                    changed |= new is not None
                rule.formula = out
            for cfvo in _cfvos(rule):
                if isinstance(cfvo.val, str):
                    new = rw(cfvo.val, ws.title, bare=True,
                             where=f"conditional format on {ws.title}")
                    if new is not None:
                        cfvo.val = new
                        changed = True
            if changed:
                rw.report.bump("conditional formats")


def _cfvos(rule) -> list:
    out = []
    for attr in ("colorScale", "dataBar", "iconSet"):
        holder = getattr(rule, attr, None)
        if holder is not None:
            out.extend(holder.cfvo or [])
    return out


def _series_refs(series) -> Iterator[Any]:
    for src in (series.val, series.cat, series.xVal, series.yVal):
        if src is None:
            continue
        for ref in (getattr(src, "numRef", None), getattr(src, "strRef", None)):
            if ref is not None and ref.f:
                yield ref
    tx = series.tx
    if tx is not None and tx.strRef is not None and tx.strRef.f:
        yield tx.strRef


# ---------------------------------------------------------------------------
# Ranges on the edited sheet
# ---------------------------------------------------------------------------


def _shift_box(edit: Edit, min_col: int, min_row: int, max_col: int,
               max_row: int) -> tuple[int, int, int, int] | None:
    """(min_col, min_row, max_col, max_row) after the edit, or None."""
    if edit.axis is Axis.ROW:
        span = edit.map_span(min_row, max_row)
        return None if span is None else (min_col, span[0], max_col, span[1])
    span = edit.map_span(min_col, max_col)
    return None if span is None else (span[0], min_row, span[1], max_row)


def shift_box(edit: Edit, box: list[int]) -> list[int] | None:
    """A readback box [row1, col1, row2, col2] after the edit, or None."""
    out = _shift_box(edit, box[1], box[0], box[3], box[2])
    if out is None:
        return None
    return [out[1], out[0], out[3], out[2]]


def _range_text(b: tuple[int, int, int, int]) -> str:
    first = f"{col_letters(b[0])}{b[1]}"
    last = f"{col_letters(b[2])}{b[3]}"
    return first if first == last else f"{first}:{last}"


def _shift_range_text(edit: Edit, text: str) -> str | None:
    """A sheet-local range string after the edit; '' when it vanished."""
    from openpyxl.utils.cell import range_boundaries

    try:
        b = range_boundaries(str(text))
    except (ValueError, TypeError):
        return None
    if None in b:
        return None
    out = _shift_box(edit, *b)
    return "" if out is None else _range_text(out)


def _shift_multi(edit: Edit, sqref) -> tuple[str, tuple[int, int] | None]:
    """(new sqref text, bounding top-left (row, col) before the edit)."""
    kept = []
    rows, cols = [], []
    for rng in sqref.ranges:
        rows.append(rng.min_row)
        cols.append(rng.min_col)
        out = _shift_box(edit, rng.min_col, rng.min_row, rng.max_col, rng.max_row)
        if out is not None:
            kept.append(_range_text(out))
    anchor = (min(rows), min(cols)) if rows else None
    return " ".join(sorted(set(kept))), anchor


def _reanchor(edit: Edit, text: str, sheet: str, old_tl: tuple[int, int],
              new_tl: tuple[int, int], rw: _Rewriter, where: str) -> str:
    """A validation or conditional-format formula, relative to the top-left
    cell of its range, after the edit. When that cell itself is gone, the
    formula is first carried to the cell that becomes the new top-left (so a
    same-row rule stays a same-row rule), then shifted."""
    from openpyxl.formula.translate import Translator, TranslatorError

    source = text
    axis_old = old_tl[0] if edit.axis is Axis.ROW else old_tl[1]
    axis_new = new_tl[0] if edit.axis is Axis.ROW else new_tl[1]
    if edit.map(axis_old) != axis_new:
        origin = edit.unmap(axis_new)
        if origin is not None and origin != axis_old:
            row, col = old_tl
            dest = (origin, col) if edit.axis is Axis.ROW else (row, origin)
            try:
                source = Translator("=" + text, origin=f"{col_letters(col)}{row}").translate_formula(
                    f"{col_letters(dest[1])}{dest[0]}")[1:]
            except (TranslatorError, ValueError):
                return REF_ERROR
    new = rw(source, sheet, bare=True, where=where)
    return source if new is None else new


def _shift_dv_cf(ws, edit: Edit, rw: _Rewriter) -> None:
    from openpyxl.formatting.formatting import ConditionalFormattingList
    from openpyxl.worksheet.cell_range import MultiCellRange

    report = rw.report
    kept_dv = []
    for dv in ws.data_validations.dataValidation:
        new_sqref, old_tl = _shift_multi(edit, dv.sqref)
        if not new_sqref:
            report.notes.append(f"a validation on {dv.sqref} was removed with its cells")
            continue
        if str(dv.sqref) != new_sqref:
            report.bump("validations")
        new_ranges = MultiCellRange(new_sqref)
        new_tl = (min(r.min_row for r in new_ranges.ranges), min(r.min_col for r in new_ranges.ranges))
        for attr in ("formula1", "formula2"):
            text = getattr(dv, attr)
            if text and old_tl is not None:
                setattr(dv, attr, _reanchor(edit, text, ws.title, old_tl, new_tl, rw,
                                            f"validation on {ws.title}"))
        dv.sqref = new_ranges
        kept_dv.append(dv)
    ws.data_validations.dataValidation = kept_dv

    rebuilt = ConditionalFormattingList()
    for cf in ws.conditional_formatting:
        new_sqref, old_tl = _shift_multi(edit, cf.sqref)
        if not new_sqref:
            report.notes.append(
                f"{len(cf.rules)} conditional-format rule(s) on {cf.sqref} were removed with their cells")
            continue
        if str(cf.sqref) != new_sqref:
            report.bump("conditional formats")
        ranges = MultiCellRange(new_sqref)
        new_tl = (min(r.min_row for r in ranges.ranges), min(r.min_col for r in ranges.ranges))
        for rule in cf.rules:
            if rule.formula and old_tl is not None:
                rule.formula = [
                    _reanchor(edit, t, ws.title, old_tl, new_tl, rw, f"conditional format on {ws.title}")
                    for t in rule.formula
                ]
            for cfvo in _cfvos(rule):
                if isinstance(cfvo.val, str):
                    new = rw(cfvo.val, ws.title, bare=True, where=f"conditional format on {ws.title}")
                    if new is not None:
                        cfvo.val = new
            rebuilt.add(new_sqref, rule)
    ws.conditional_formatting = rebuilt


# ---------------------------------------------------------------------------
# Checks before an edit
# ---------------------------------------------------------------------------


def _axis_span(edit: Edit, rng) -> tuple[int, int]:
    if edit.axis is Axis.ROW:
        return rng.min_row, rng.max_row
    return rng.min_col, rng.max_col


def _cuts(edit: Edit, lo: int, hi: int) -> bool:
    """True when the edit would change part of [lo, hi] but not all of it."""
    at, n = edit.at, edit.count
    if edit.kind is EditKind.INSERT:
        return lo < at <= hi
    if edit.kind is EditKind.DELETE:
        overlap = not (at + n - 1 < lo or at > hi)
        whole = at <= lo and hi <= at + n - 1
        return overlap and not whole
    inside = at <= lo and hi <= at + n - 1
    overlap = not (at + n - 1 < lo or at > hi)
    if overlap and not inside:
        return True
    return not inside and lo < edit.to <= hi


def check_edit(wb, ws, edit: Edit) -> None:
    """Refuse an edit Excel refuses, before anything changes. The op loop
    names the op in front of the message."""
    from openpyxl.utils.cell import range_boundaries
    from openpyxl.worksheet.cell_range import CellRange
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    what = "rows" if edit.axis is Axis.ROW else "columns"
    if edit.count < 1:
        raise ValueError(f"count must be 1 or more")
    if not 1 <= edit.at <= edit.limit:
        raise ValueError(f"{edit.axis.value} {edit.at} is outside the sheet")
    if edit.kind is EditKind.MOVE:
        if not 1 <= edit.to <= edit.limit + 1:
            raise ValueError(f"to must be between 1 and {edit.limit + 1}")
        if edit.at < edit.to < edit.at + edit.count:
            raise ValueError(
                f"to ({edit.to}) lies inside the {what} being moved. "
                "Pick a destination before the first or after the last moved line."
            )
        if edit.at + edit.count - 1 > edit.limit:
            raise ValueError(f"the block runs past the edge of the sheet")
    if edit.kind is EditKind.INSERT:
        edge = edit.limit - edit.count
        for (r, c), cell in ws._cells.items():
            i = r if edit.axis is Axis.ROW else c
            if i > edge and cell.value is not None:
                raise ValueError(
                    f"{cell.coordinate} holds data and inserting would push it "
                    "off the sheet. Clear or move it first."
                )

    for rng in ws.merged_cells.ranges:
        if edit.kind is EditKind.MOVE and _cuts(edit, *_axis_span(edit, rng)):
            raise ValueError(
                f"{edit.describe()} cut across the merged range {rng}. "
                "Unmerge it first, or move every line it covers."
            )
    for cell in ws._cells.values():
        value = cell.value if cell.data_type == "f" else None
        if isinstance(value, (ArrayFormula, DataTableFormula)):
            ref = value.ref
            try:
                c1, r1, c2, r2 = range_boundaries(str(ref)) if ref else (cell.column, cell.row) * 2
            except (ValueError, TypeError):
                continue
            span = (r1, r2) if edit.axis is Axis.ROW else (c1, c2)
            if _cuts(edit, *span):
                kind = "array formula" if isinstance(value, ArrayFormula) else "data table"
                raise ValueError(
                    f"{edit.describe()} cut the {kind} in {ref or cell.coordinate}. "
                    "Clear it first, or include all of it."
                )
    for table in ws.tables.values():
        rng = CellRange(table.ref)
        lo, hi = _axis_span(edit, rng)
        if edit.kind is EditKind.MOVE:
            inside = edit.at >= lo and edit.at + edit.count - 1 <= hi
            if inside and edit.axis is Axis.ROW:
                header = 1 if table.headerRowCount is None else int(table.headerRowCount)
                totals = int(table.totalsRowCount or 0)
                data_lo, data_hi = lo + header, hi - totals
                inside = (edit.at >= data_lo and edit.at + edit.count - 1 <= data_hi
                          and data_lo <= edit.to <= data_hi + 1)
            elif inside:
                inside = lo <= edit.to <= hi + 1
            whole = edit.at <= lo and hi <= edit.at + edit.count - 1
            if not (inside or whole) and _cuts(edit, lo, hi):
                raise ValueError(
                    f"{edit.describe()} cut across table '{table.displayName}' "
                    f"({table.ref}). Move whole tables, or lines within one table's data."
                )
        if edit.kind is EditKind.DELETE and edit.axis is Axis.ROW:
            header = 1 if table.headerRowCount is None else int(table.headerRowCount)
            totals = int(table.totalsRowCount or 0)
            data_lo, data_hi = rng.min_row + header, rng.max_row - totals
            header_gone = header and edit.at <= rng.min_row < edit.at + edit.count
            if data_lo <= data_hi and not header_gone:
                survivors = sum(1 for r in range(data_lo, data_hi + 1) if edit.map(r) is not None)
                if survivors == 0:
                    raise ValueError(
                        f"rows {edit.at}-{edit.at + edit.count - 1} are every data row "
                        f"of table '{table.displayName}'; a table keeps at least one data row. "
                        "Leave one row, or clear the cells instead."
                    )


# ---------------------------------------------------------------------------
# Applying an edit
# ---------------------------------------------------------------------------


def _move_cells(ws, edit: Edit, report: ShiftReport) -> list[Any]:
    """Re-key the sheet's materialised cells; the moved cells."""
    cells = ws._cells
    moved: dict[tuple[int, int], Any] = {}
    dropped = 0
    for key in list(cells):
        r, c = key
        i = r if edit.axis is Axis.ROW else c
        j = edit.map(i)
        if j == i:
            continue
        cell = cells.pop(key)
        if j is None:
            if edit.kind is EditKind.INSERT:
                dropped += 1
            continue
        new_key = (j, c) if edit.axis is Axis.ROW else (r, j)
        if edit.axis is Axis.ROW:
            cell.row = j
        else:
            cell.column = j
        moved[new_key] = cell
    cells.update(moved)
    if dropped:
        report.notes.append(f"{dropped} formatted empty cell(s) at the edge of the sheet were dropped")
    return list(moved.values())


def _shift_merges(ws, edit: Edit, report: ShiftReport) -> list[str]:
    """Unmerge every merged range the edit changes; the ranges to merge again."""
    redo = []
    for rng in list(ws.merged_cells.ranges):
        out = _shift_box(edit, rng.min_col, rng.min_row, rng.max_col, rng.max_row)
        old = (rng.min_col, rng.min_row, rng.max_col, rng.max_row)
        if out == old:
            continue
        ws.unmerge_cells(str(rng.coord))
        report.bump("merged ranges")
        if out is None:
            report.notes.append(f"the merged range {rng.coord} was removed with its cells")
        elif out[0] != out[2] or out[1] != out[3]:
            redo.append(_range_text(out))
    return redo


def _shift_dimensions(ws, edit: Edit, report: ShiftReport) -> None:
    from openpyxl.utils import column_index_from_string

    if edit.axis is Axis.ROW:
        dims = ws.row_dimensions
        moved = {}
        for idx in list(dims):
            j = edit.map(idx)
            if j == idx:
                continue
            dim = dims.pop(idx)
            if j is not None:
                dim.index = j
                moved[j] = dim
        dims.update(moved)
        if moved:
            report.bump("row heights", len(moved))
        return

    dims = ws.column_dimensions
    bands = []
    for key in list(dims):
        dim = dims.pop(key)
        lo = dim.min or column_index_from_string(dim.index)
        hi = dim.max or lo
        if edit.kind is EditKind.MOVE:
            cuts = sorted({edit.at, edit.at + edit.count, edit.to})
            pieces, start = [], lo
            for p in cuts:
                if start < p <= hi:
                    pieces.append((start, p - 1))
                    start = p
            pieces.append((start, hi))
            for a, b in pieces:
                ja, jb = edit.map(a), edit.map(b)
                piece = copy(dim) if len(pieces) > 1 else dim
                bands.append((piece, min(ja, jb), max(ja, jb)))
        else:
            span = edit.map_span(lo, hi)
            if span is not None:
                bands.append((dim, span[0], span[1]))
    changed = 0
    for dim, lo, hi in bands:
        if (dim.min, dim.max) != (lo, hi):
            changed += 1
        dim.min, dim.max = lo, hi
        dim.index = col_letters(lo)
        dims[dim.index] = dim
    if changed:
        report.bump("column widths", changed)


def _shift_freeze(ws, edit: Edit, report: ShiftReport) -> None:
    pane = ws.sheet_view.pane
    if pane is None or pane.state not in ("frozen", "frozenSplit"):
        return
    rows, cols = int(pane.ySplit or 0), int(pane.xSplit or 0)
    k = rows if edit.axis is Axis.ROW else cols
    if not k:
        return
    if edit.kind is EditKind.INSERT:
        new_k = k + edit.count if edit.at <= k else k
    elif edit.kind is EditKind.DELETE:
        new_k = k - max(0, min(k, edit.at + edit.count - 1) - edit.at + 1)
    else:
        new_k = k
    if new_k == k:
        return
    rows, cols = (new_k, cols) if edit.axis is Axis.ROW else (rows, new_k)
    ws.freeze_panes = None if not (rows or cols) else f"{col_letters(cols + 1)}{rows + 1}"
    report.bump("freeze panes")


def _shift_anchor(anchor, edit: Edit) -> bool:
    """Move a picture or chart anchor with its cells; True when it moved."""
    from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, TwoCellAnchor

    def first_surviving(i: int) -> int:
        j = edit.map(i)
        return edit.at if j is None else j

    attr = edit.attr
    if isinstance(anchor, OneCellAnchor):
        marker = anchor._from
        old = getattr(marker, attr) + 1
        setattr(marker, attr, first_surviving(old) - 1)
        return getattr(marker, attr) + 1 != old
    if isinstance(anchor, TwoCellAnchor):
        if anchor.editAs == "absolute":
            return False
        frm, to = anchor._from, anchor.to
        lo, hi = getattr(frm, attr) + 1, getattr(to, attr) + 1
        if anchor.editAs == "oneCell":
            new_lo = first_surviving(lo)
            setattr(frm, attr, new_lo - 1)
            setattr(to, attr, new_lo - 1 + (hi - lo))
            return new_lo != lo
        span = edit.map_span(lo, hi)
        if span is None:
            span = (first_surviving(lo), first_surviving(lo))
        setattr(frm, attr, span[0] - 1)
        setattr(to, attr, span[1] - 1)
        return span != (lo, hi)
    return False


def _shift_objects(ws, edit: Edit, report: ShiftReport) -> None:
    """Shift picture and chart anchors."""
    from formats.xlsx.refs import parse_ref

    for obj in list(ws._images) + list(ws._charts):
        anchor = obj.anchor
        if isinstance(anchor, str):
            ref = parse_ref(anchor)
            if ref is None or len(ref.parts) != 1:
                continue
            part = ref.parts[0]
            i = getattr(part, edit.attr)
            j = edit.map(i)
            j = edit.at if j is None else j
            if j != i:
                obj.anchor = replace(part, **{edit.attr: j}).text()
                report.bump("pictures and charts")
        elif _shift_anchor(anchor, edit):
            report.bump("pictures and charts")


def _shift_sort_state(state, edit: Edit) -> None:
    if state is None:
        return
    if state.ref:
        state.ref = _shift_range_text(edit, state.ref) or None
    kept = []
    for cond in state.sortCondition or []:
        new = _shift_range_text(edit, cond.ref) if cond.ref else None
        if new:
            cond.ref = new
            kept.append(cond)
    state.sortCondition = kept


def _shift_auto_filter(af, edit: Edit, report: ShiftReport, label: str) -> None:
    """A sheet's or a table's auto filter: its range, the column each
    criterion sits on (`colId` counts from the range's first column, so a
    column edit inside the range moves it even when the range text stays
    the same) and its sort state. A criterion whose column is deleted goes
    with it, named in a note."""
    from openpyxl.utils.cell import range_boundaries

    if af is None or not af.ref:
        return
    old_min_col = range_boundaries(af.ref)[0]
    new = _shift_range_text(edit, af.ref)
    if new is None:
        return
    if new == "":
        af.ref = None
        af.filterColumn = []
        af.sortState = None
        report.bump("filters")
        return
    changed = new != af.ref
    af.ref = new
    new_min_col, _, new_max_col, _ = range_boundaries(new)
    if edit.axis is Axis.COL and af.filterColumn:
        kept = []
        for fc in af.filterColumn:
            old_col = old_min_col + fc.colId
            j = edit.map(old_col)
            if j is None or not new_min_col <= j <= new_max_col:
                report.notes.append(
                    f"the {label} criterion on column {col_letters(old_col)} "
                    "was removed with its column")
                changed = True
                continue
            if j - new_min_col != fc.colId:
                fc.colId = j - new_min_col
                changed = True
            kept.append(fc)
        af.filterColumn = kept
    _shift_sort_state(af.sortState, edit)
    if changed:
        report.bump("filters")


def _shift_filters_and_print(ws, edit: Edit, report: ShiftReport) -> None:
    from openpyxl.worksheet.print_settings import ColRange, RowRange

    _shift_auto_filter(ws.auto_filter, edit, report, "filter")

    area = ws._print_area
    if area is not None and area.ranges:
        out = []
        for rng in sorted(area.ranges, key=lambda r: (r.min_row, r.min_col)):
            b = _shift_box(edit, rng.min_col, rng.min_row, rng.max_col, rng.max_row)
            if b is not None:
                out.append(_range_text(b))
        before = sorted(str(r.coord) for r in area.ranges)
        if sorted(out) != before:
            ws.print_area = out or None
            report.bump("print settings")
    if edit.axis is Axis.ROW and ws._print_rows:
        lo, hi = ws._print_rows.min_row, ws._print_rows.max_row
        span = edit.map_span(lo, hi)
        if span != (lo, hi):
            ws._print_rows = None if span is None else RowRange(min_row=span[0], max_row=span[1])
            report.bump("print settings")
    if edit.axis is Axis.COL and ws._print_cols:
        from openpyxl.utils import column_index_from_string

        lo = column_index_from_string(ws._print_cols.min_col)
        hi = column_index_from_string(ws._print_cols.max_col)
        span = edit.map_span(lo, hi)
        if span != (lo, hi):
            ws._print_cols = None if span is None else ColRange(
                min_col=col_letters(span[0]), max_col=col_letters(span[1]))
            report.bump("print settings")

    breaks = ws.row_breaks if edit.axis is Axis.ROW else ws.col_breaks
    if breaks.brk:
        kept = []
        for brk in breaks.brk:
            j = edit.map(brk.id) if brk.id else None
            if j is not None:
                brk.id = j
                kept.append(brk)
        breaks.brk = kept


def _unique_header(existing: set[str]) -> str:
    n = 1
    while f"column{n}" in existing:
        n += 1
    existing.add(f"column{n}")
    return f"Column{n}"


def _shift_tables(ws, edit: Edit, report: ShiftReport, dead_tables: set[str],
                  dead_cols: dict[str, set[str]]) -> list[tuple[int, int, str]]:
    """Move, grow, shrink or remove the sheet's tables; returns the header
    cells (row, column, text) to write once the cells have moved."""
    from openpyxl.worksheet.cell_range import CellRange
    from openpyxl.worksheet.table import TableColumn

    headers: list[tuple[int, int, str]] = []
    for name in list(ws.tables):
        table = ws.tables[name]
        rng = CellRange(table.ref)
        header = 1 if table.headerRowCount is None else int(table.headerRowCount)
        totals = int(table.totalsRowCount or 0)
        new = _shift_box(edit, rng.min_col, rng.min_row, rng.max_col, rng.max_row)
        if edit.axis is Axis.ROW and edit.kind is EditKind.DELETE:
            if new is None or (header and edit.map(rng.min_row) is None):
                del ws.tables[name]
                dead_tables.add(table.displayName.casefold())
                report.notes.append(f"table '{table.displayName}' was removed with its header row")
                continue
            if totals and edit.map(rng.max_row) is None:
                table.totalsRowCount = 0
                totals = 0
                report.notes.append(f"table '{table.displayName}' lost its totals row")
        if new is None:
            del ws.tables[name]
            dead_tables.add(table.displayName.casefold())
            report.notes.append(f"table '{table.displayName}' was removed with its cells")
            continue
        if edit.axis is Axis.COL:
            cols = list(table.tableColumns)
            if edit.kind is EditKind.INSERT and rng.min_col < edit.at <= rng.max_col:
                offset = edit.at - rng.min_col
                names = {c.name.casefold() for c in cols if c.name}
                next_id = max((c.id or 0 for c in cols), default=0) + 1
                added = []
                for k in range(edit.count):
                    added.append(TableColumn(id=next_id + k, name=_unique_header(names)))
                cols[offset:offset] = added
                if header:
                    headers.extend(
                        (rng.min_row, edit.at + k, col.name) for k, col in enumerate(added))
                table.tableColumns = cols
            elif edit.kind is EditKind.DELETE:
                kept = []
                for k, col in enumerate(cols):
                    if edit.map(rng.min_col + k) is None:
                        dead_cols.setdefault(table.displayName.casefold(), set()).add(
                            (col.name or "").casefold())
                    else:
                        kept.append(col)
                table.tableColumns = kept
            elif edit.kind is EditKind.MOVE and rng.min_col <= edit.at <= rng.max_col:
                by_pos = {edit.map(rng.min_col + k): col for k, col in enumerate(cols)}
                table.tableColumns = [by_pos[p] for p in sorted(by_pos)]
        new_ref = _range_text(new)
        if new_ref != table.ref:
            table.ref = new_ref
            report.bump("tables")
        if table.autoFilter is not None:
            _shift_auto_filter(table.autoFilter, edit, report,
                               f"table '{table.displayName}' filter")
            last_row = new[3] - totals
            table.autoFilter.ref = _range_text((new[0], new[1], new[2], last_row))
        _shift_sort_state(table.sortState, edit)
    return headers


def _shift_array_refs(ws, edit: Edit) -> None:
    """The range an array formula or data table covers, on the edited sheet
    (its anchor cell has already moved)."""
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    for cell in ws._cells.values():
        if cell.data_type != "f" or not isinstance(cell.value, (ArrayFormula, DataTableFormula)):
            continue
        ref = cell.value.ref
        if ref:
            new = _shift_range_text(edit, ref)
            if new:
                cell.value.ref = new


def _shift_pivots(wb, edit: Edit, report: ShiftReport) -> None:
    seen = set()
    for ws in wb.worksheets:
        for pivot in getattr(ws, "_pivots", []):
            if ws.title.casefold() == edit.sheet.casefold() and pivot.location is not None:
                new = _shift_range_text(edit, pivot.location.ref)
                if new and new != pivot.location.ref:
                    pivot.location.ref = new
                    report.bump("pivot tables")
            cache = getattr(pivot, "cache", None)
            source = getattr(getattr(cache, "cacheSource", None), "worksheetSource", None)
            if source is None or id(cache) in seen or not source.ref:
                continue
            seen.add(id(cache))
            if (source.sheet or "").casefold() == edit.sheet.casefold():
                new = _shift_range_text(edit, source.ref)
                if new and new != source.ref:
                    source.ref = new
                    report.bump("pivot sources")


def _prefilter(sheet: str) -> Callable[[str, str | None], bool]:
    """A formula on another sheet can only reference `sheet` by naming it."""
    needles = {sheet.casefold(), sheet.replace("'", "''").casefold()}

    def keep(text: str, own: str | None) -> bool:
        if own is not None and own.casefold() == sheet.casefold():
            return True
        low = text.casefold()
        return any(n in low for n in needles)

    return keep


def apply_edit(wb, ws, edit: Edit) -> ShiftReport:
    """Run a checked structural edit on `ws` and rewrite every reference."""
    report = ShiftReport()
    dead_tables: set[str] = set()
    dead_cols: dict[str, set[str]] = {}
    redo = _shift_merges(ws, edit, report)
    headers = _shift_tables(ws, edit, report, dead_tables, dead_cols)

    def struct(text: str) -> str | None:
        if not (dead_tables or dead_cols):
            return None
        return _struct_kill(text, dead_tables, dead_cols)

    rw = _Rewriter(
        fn=lambda ref, own: shift_ref(ref, own, edit),
        report=report,
        other=struct,
        prefilter=None if (dead_tables or dead_cols) else _prefilter(edit.sheet),
    )
    _shift_dv_cf(ws, edit, rw)
    moved = _move_cells(ws, edit, report)
    for cell in moved:
        link = getattr(cell, "hyperlink", None)
        if link is not None and link.ref and ":" not in str(link.ref):
            link.ref = cell.coordinate
    for row, col, text in headers:
        ws.cell(row=row, column=col, value=text)
    _shift_array_refs(ws, edit)
    _rewrite_everything(wb, rw, sqref_sheet=ws.title)
    for text in redo:
        ws.merge_cells(text)
    _shift_dimensions(ws, edit, report)
    _shift_freeze(ws, edit, report)
    _shift_objects(ws, edit, report)
    _shift_filters_and_print(ws, edit, report)
    _shift_pivots(wb, edit, report)
    return report


# ---------------------------------------------------------------------------
# Sheet rename and delete
# ---------------------------------------------------------------------------


def rename_sheet(wb, ws, new_title: str) -> ShiftReport:
    """Rename a sheet and every reference that names it."""
    old = ws.title
    if new_title != old and any(
        other is not ws and other.title.casefold() == new_title.casefold()
        for other in wb.worksheets + list(getattr(wb, "chartsheets", []))
    ):
        raise ValueError(
            f"a sheet named '{new_title}' already exists (sheet names ignore case). "
            "Pick another name."
        )
    ws.title = new_title
    new = ws.title
    report = ShiftReport()

    def fn(ref: Ref, own: str | None) -> str | None:
        if ref.dead or ref.external or ref.sheet is None:
            return None
        first = new if ref.sheet.casefold() == old.casefold() else ref.sheet
        second = ref.sheet_to
        if second is not None and second.casefold() == old.casefold():
            second = new
        if first == ref.sheet and second == ref.sheet_to:
            return None
        return ref.with_qualifier(first, second).text()

    needles = {old.casefold(), old.replace("'", "''").casefold()}
    rw = _Rewriter(fn=fn, report=report,
                   prefilter=lambda text, own: any(n in text.casefold() for n in needles))
    _rewrite_everything(wb, rw)
    for other in wb.worksheets:
        for pivot in getattr(other, "_pivots", []):
            source = getattr(getattr(getattr(pivot, "cache", None), "cacheSource", None),
                             "worksheetSource", None)
            if source is not None and (source.sheet or "").casefold() == old.casefold():
                source.sheet = new
                report.bump("pivot sources")
    return report


def delete_sheet(wb, name: str) -> ShiftReport:
    """Delete a sheet; references to it become #REF! as in Excel, and a 3-D
    reference whose end sheet goes moves that end inward."""
    order = [s.title for s in wb.worksheets]
    gone = name.casefold()
    del wb[name]
    report = ShiftReport()

    def inward(first: str, last: str) -> tuple[str, str] | None:
        folded = [t.casefold() for t in order]
        try:
            i, j = folded.index(first.casefold()), folded.index(last.casefold())
        except ValueError:
            return first, last
        span = [t for t in order[min(i, j):max(i, j) + 1] if t.casefold() != gone]
        if not span:
            return None
        return span[0], span[-1]

    def fn(ref: Ref, own: str | None) -> str | None:
        if ref.dead or ref.external or ref.sheet is None:
            return None
        if ref.three_d:
            if gone not in (ref.sheet.casefold(), (ref.sheet_to or "").casefold()):
                return None
            span = inward(ref.sheet, ref.sheet_to or ref.sheet)
            if span is None:
                return _dead(ref)
            if span[0] == span[1]:
                return ref.with_qualifier(span[0]).text()
            return ref.with_qualifier(span[0], span[1]).text()
        if ref.sheet.casefold() != gone:
            return None
        return _dead(ref)

    needles = {gone, name.replace("'", "''").casefold()}
    rw = _Rewriter(fn=fn, report=report,
                   prefilter=lambda text, own: any(n in text.casefold() for n in needles))
    # A chart series written as #REF! text is not a form Excel is known to
    # accept, so charts keep their reference and the batch warns about it.
    _rewrite_everything(wb, rw, charts=False)
    return report


__all__ = [
    "Axis",
    "Edit",
    "EditKind",
    "Part",
    "ShiftReport",
    "apply_edit",
    "check_edit",
    "copy_ref",
    "delete_sheet",
    "rename_sheet",
    "shift_box",
    "shift_ref",
]
