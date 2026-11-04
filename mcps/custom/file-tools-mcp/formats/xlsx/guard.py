"""The last check before a workbook is saved: no formula that reaches the
network or runs a program enters the file through this batch.

Each operation checks the formulas it writes and names them precisely; this
module is the backstop behind those checks. It walks every place a workbook
holds a formula (cells, array formulas, rule formulas and thresholds,
validations, defined names, table formulas, chart references), notes the
unsafe functions each one calls, and compares the result with the same walk
over the workbook as it was loaded. A formula the file already carried is
the person's own and stays; a new one, or a new unsafe function in an old
place, refuses the whole batch.

Places are told apart by the object that holds the formula, not by its
text: a structural edit rewrites the text of a formula it moves (A1 becomes
A2) but keeps the cell, rule or name object it lives in.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from formats.xlsx.formulas import UNSAFE_FUNCTIONS, unsafe_functions

# Only a text that names an unsafe function, or holds a DDE link's '|', can
# be unsafe: the cheap test runs on every formula, the full one on these.
_MAYBE = re.compile("|".join([re.escape(n) for n in sorted(UNSAFE_FUNCTIONS)] + [r"\|"]), re.IGNORECASE)

_SHOWN = 20


@dataclass(frozen=True)
class Slot:
    """One place holding formula text."""
    key: tuple[int, str]
    holder: Any
    location: str
    text: str


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


def formula_slots(wb) -> Iterator[Slot]:
    """Every formula-bearing place of the workbook."""
    from openpyxl.worksheet.formula import ArrayFormula

    for ws in wb.worksheets:
        title = ws.title
        for cell in ws._cells.values():
            if cell.data_type != "f":
                continue
            value = cell.value
            where = f"{title}!{cell.coordinate}"
            if isinstance(value, ArrayFormula):
                yield Slot((id(value), "text"), value, where, value.text or "")
            elif isinstance(value, str):
                yield Slot((id(cell), "value"), cell, where, value)
        for cf in ws.conditional_formatting:
            where = f"conditional format on {title}!{cf.sqref}"
            for rule in cf.rules:
                for i, text in enumerate(rule.formula or []):
                    yield Slot((id(rule), f"formula{i}"), rule, where, str(text))
                for cfvo in _cfvos(rule):
                    if isinstance(cfvo.val, str):
                        yield Slot((id(cfvo), "val"), cfvo, where, cfvo.val)
        for dv in ws.data_validations.dataValidation:
            where = f"validation on {title}!{dv.sqref}"
            for attr in ("formula1", "formula2"):
                text = getattr(dv, attr)
                if text:
                    yield Slot((id(dv), attr), dv, where, text)
        for name, defn in ws.defined_names.items():
            if defn.attr_text:
                yield Slot((id(defn), "attr_text"), defn, f"name {name} on {title}", defn.attr_text)
        for table in ws.tables.values():
            for col in table.tableColumns:
                for attr in ("calculatedColumnFormula", "totalsRowFormula"):
                    tf = getattr(col, attr)
                    if tf is not None and tf.attr_text:
                        yield Slot((id(tf), "attr_text"), tf, f"table {table.displayName}", tf.attr_text)
    for name, defn in wb.defined_names.items():
        if defn.attr_text:
            yield Slot((id(defn), "attr_text"), defn, f"name {name}", defn.attr_text)
    for holder in list(wb.worksheets) + list(getattr(wb, "chartsheets", [])):
        for chart in getattr(holder, "_charts", []):
            for series in chart.series:
                for ref in _series_refs(series):
                    yield Slot((id(ref), "f"), ref, f"chart on {holder.title}", ref.f)


def _unsafe(text: str) -> frozenset[str]:
    if not text or not _MAYBE.search(text):
        return frozenset()
    return frozenset(unsafe_functions(text))


@dataclass
class Snapshot:
    """The unsafe functions each place held when the workbook was loaded.
    The holders are kept alive so their ids are never reused."""
    unsafe: dict[tuple[int, str], frozenset[str]] = field(default_factory=dict)
    holders: list[Any] = field(default_factory=list)


def snapshot(wb) -> Snapshot:
    snap = Snapshot()
    for slot in formula_slots(wb):
        names = _unsafe(slot.text)
        if names:
            snap.unsafe[slot.key] = names
            snap.holders.append(slot.holder)
    return snap


def new_unsafe(wb, before: Snapshot) -> list[tuple[str, list[str]]]:
    """(place, the unsafe functions new there) for every formula the batch
    brought in that reaches the network or runs a program."""
    found: list[tuple[str, list[str]]] = []
    for slot in formula_slots(wb):
        names = _unsafe(slot.text)
        if not names:
            continue
        added = names - before.unsafe.get(slot.key, frozenset())
        if added:
            found.append((slot.location, sorted(added)))
    return found


def refusal_lines(found: list[tuple[str, list[str]]]) -> list[str]:
    lines = [f"  - {where}: {', '.join(names)}" for where, names in found[:_SHOWN]]
    if len(found) > _SHOWN:
        lines.append(f"  ...and {len(found) - _SHOWN} more")
    return lines
