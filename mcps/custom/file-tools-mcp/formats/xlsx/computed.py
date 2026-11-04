"""The computed half of write_xlsx: what worker A hands on, the parent's
recalculation, and the readback with each formula's value.

Worker A applies the batch and stages the save with facts: the formula
count per sheet (the splice must find the same), the places the workbook
reads outside data (a recalculation is then refused, so nothing is
fetched), the formula cells the readback shows, and, in a file of the work
folder, which values the file had may still hold. The parent recalculates
the staged copy with LibreOffice; worker B writes the values (recalc.fill)
and fills them into the readback.
"""

import json
import os
import re
import zipfile
from typing import Any

from formats.xlsx import numfmt, recalc
from formats.xlsx.formulas import ENGINE_CONTAINED, reads_outside

# Past this many cells a batch keeps no record of the cells it changed, so
# no value the file had is kept: Excel computes those formulas on open.
CARRY_MAX_CELLS = 200_000
CARRY_FILE = "carry.json"
RECALC_FOLDER = "recalc"
_SHOWN_PLACES = 3
_OUTSIDE_PARTS = (("xl/externalLinks/", "links to other workbooks"), ("xl/connections.xml", "data connections"),
                  ("xl/queryTables/", "query tables"))
_MARK_RE = re.compile("\x00([0-9]+)\x00")


def formula_counts(wb) -> dict[str, int]:
    """Formula cells per sheet title."""
    return {ws.title: sum(1 for cell in ws._cells.values() if cell.data_type == "f") for ws in wb.worksheets}


def outside_parts(path: str) -> list[str]:
    """What the original package holds that reads outside the workbook:
    links to other workbooks, data connections, query tables, pivot tables
    over another source."""
    from formats.xlsx import loss

    found: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            for prefix, what in _OUTSIDE_PARTS:
                if any(name.startswith(prefix) for name in names):
                    found.append(what)
            if any(loss.outside_pivots(zf)):
                found.append("pivot tables over another source")
    except (OSError, zipfile.BadZipFile):
        pass
    return found


def outside_reads(wb, parts: list[str]) -> list[str]:
    """Where the workbook may read outside data in a way the engine was not
    shown to leave alone, the first few: 'RTD in Sheet1!B2', 'links to
    other workbooks'. ``parts`` is what ``outside_parts`` found in the
    original package."""
    from formats.xlsx.guard import formula_slots

    found: list[str] = list(parts)
    if getattr(wb, "_external_links", None) and "links to other workbooks" not in found:
        found.append("links to other workbooks")
    for slot in formula_slots(wb):
        names = [name for name in reads_outside(slot.text) if name not in ENGINE_CONTAINED]
        if names:
            found.append(f"{', '.join(names)} in {slot.location}")
            if len(found) >= _SHOWN_PLACES:
                break
    return found[:_SHOWN_PLACES]


# ---------------------------------------------------------------------------
# What the file had (carried values)
# ---------------------------------------------------------------------------


def _comparable(value: Any) -> Any:
    text = getattr(value, "text", None)
    if text is not None and hasattr(value, "ref"):
        return ("array", text, value.ref)
    if not isinstance(value, (str, int, float, bool)) and value is not None:
        return (type(value).__name__, str(value))
    return value


def cell_values(wb) -> dict[tuple[str, int, int], Any] | None:
    """Every non-empty cell's value by (title, row, column); None past
    CARRY_MAX_CELLS."""
    if sum(len(ws._cells) for ws in wb.worksheets) > CARRY_MAX_CELLS:
        return None
    out = {}
    for ws in wb.worksheets:
        for (row, col), cell in ws._cells.items():
            if cell.value is not None:
                out[(ws.title, row, col)] = _comparable(cell.value)
    return out


def names_of(wb) -> dict[tuple[str, str], str]:
    """Defined names and tables with what they stand for, by (where it
    belongs, its name case-folded): a formula reading one depends on
    whatever it names."""
    out = {("", name.casefold()): defn.attr_text for name, defn in wb.defined_names.items()}
    for ws in wb.worksheets:
        out.update({(ws.title, name.casefold()): defn.attr_text for name, defn in ws.defined_names.items()})
        out.update({("table", table.displayName.casefold()): table.ref for table in ws.tables.values()})
    return out


def carry_facts(wb, before: dict, names_before: dict[str, str]) -> recalc.Carry:
    """The formula cells whose text the batch left alone, the cells whose
    value it changed, and the names and tables it changed."""
    after = cell_values(wb) or {}
    changed = [key for key in before.keys() | after.keys() if before.get(key) != after.get(key)]
    eligible: dict[str, list[str]] = {}
    from openpyxl.utils import get_column_letter

    for ws in wb.worksheets:
        for (row, col), cell in ws._cells.items():
            if cell.data_type == "f" and before.get((ws.title, row, col)) == after.get((ws.title, row, col)):
                eligible.setdefault(ws.title, []).append(f"{get_column_letter(col)}{row}")
    names_after = names_of(wb)
    renamed = sorted({key[1] for key in names_before.keys() | names_after.keys()
                      if names_before.get(key) != names_after.get(key)})
    return recalc.Carry(eligible, changed, renamed)


def save_carry(work_dir: str, carry: recalc.Carry) -> None:
    with open(os.path.join(work_dir, CARRY_FILE), "w", encoding="utf-8") as fh:
        json.dump({"eligible": carry.eligible, "changed": carry.changed, "names_changed": carry.names_changed}, fh)


def load_carry(work_dir: str) -> recalc.Carry | None:
    try:
        with open(os.path.join(work_dir, CARRY_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    return recalc.Carry(data["eligible"], [tuple(c) for c in data["changed"]], data["names_changed"])


# ---------------------------------------------------------------------------
# The parent: the recalculation
# ---------------------------------------------------------------------------


def outside_note(outside: list[str]) -> str:
    return (f"This workbook reads outside data ({'; '.join(outside)}), so it was not recalculated and "
            "nothing was fetched: its formulas keep the values the file had, and Excel computes the rest "
            "when the file opens.")


async def render(staged, call, budget_s: float):
    """Recalculate the staged copy, into the work folder;
    refused when the workbook reads outside data. A failure is a reason in
    the note, never a failed write."""
    import asyncio

    import render_engine
    from batch import Rendered

    if staged.facts.get("outside"):
        return Rendered()
    folder = await asyncio.to_thread(render_engine.output_folder, call.work_dir, RECALC_FOLDER)
    try:
        # Worker A saved the staged copy with openpyxl, which writes no cached value.
        path = await render_engine.recalc(staged.staged_path, out_dir=folder, cached_values=False)
    except FileNotFoundError:
        return Rendered(note="no calculation engine here")
    except (OSError, RuntimeError, ValueError) as exc:
        return Rendered(note=str(exc) or exc.__class__.__name__)
    return Rendered(payload=path)


# ---------------------------------------------------------------------------
# Worker B: notes and the readback
# ---------------------------------------------------------------------------


def verdict(filled: recalc.Filled | None, why_not: str = "") -> str:
    """One line: how many formulas, how many computed, how many errors."""
    if filled is None:
        return ""
    parts = [f"Formulas: {filled.formulas}", f"computed: {filled.computed}"]
    if filled.carried:
        parts.append(f"kept from before this edit: {filled.carried}")
    parts.append(f"errors: {len(filled.errors)}")
    line = ", ".join(parts) + "."
    if why_not and filled.computed + filled.carried < filled.formulas:
        # The pipeline names its own phase; the agent reads the calculation.
        why_not = why_not.replace("the render phase", "the calculation").rstrip(".")
        line += f" Not recalculated ({why_not}): Excel computes them when the file opens."
    return line


def _places(places: list[str]) -> str:
    more = len(places) - _SHOWN_PLACES
    return ", ".join(places[:_SHOWN_PLACES]) + (f" and {more} more" if more > 0 else "")


def notes(filled: recalc.Filled) -> list[str]:
    out = list(filled.notes)
    if filled.unsupported:
        out.append(f"{len(filled.unsupported)} formulas use functions the calculation engine lacks "
                   f"({_places(filled.unsupported)}): their values are not verified here, and Excel computes them "
                   "when the file opens.")
    if filled.environment:
        out.append(f"{len(filled.environment)} formulas ask about the computer that calculates them (CELL, INFO: "
                   f"{_places(filled.environment)}): the server's answer is not saved, and Excel computes them "
                   "when the file opens.")
    return out


def _display(value: recalc.Value, code: str | None) -> str:
    """A value as the cell shows it: in its number format when it has one
    this module renders (dates as ISO dates), else as stored."""
    from formats.xlsx.read import _escape_cell

    text = recalc.shown(value)
    if value[0] == recalc.TEXT:
        return _escape_cell(text)
    if not code or value[0] != recalc.NUMBER:
        return text
    if numfmt.is_date(code):
        from openpyxl.utils.datetime import from_excel

        try:
            return _escape_cell(from_excel(float(text)))
        except (ValueError, OverflowError):
            return text
    return _escape_cell(numfmt.render(float(text), code) or text)


def constant_text(value, code: str) -> str:
    """A number the batch wrote as its cell shows it: in its number format
    when this module renders it, else as Excel's General format would."""
    from formats.xlsx.read import _escape_cell

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _escape_cell(value)
    raw = repr(float(value)) if isinstance(value, float) else str(value)
    return _display((recalc.NUMBER, raw.encode()), None if code == "General" else code)


def readback(text: str, marks: list, filled: recalc.Filled | None, why_not: str = "") -> str:
    """The staged readback with each marked formula's value after it
    (``=SUM(B2:E2) → 1707``), headed by the verdict line."""
    def value_of(m: re.Match) -> str:
        if filled is None:
            return ""
        title, coord, code = marks[int(m.group(1))]
        value = filled.values.get((title, coord))
        return "" if value is None else f" → {_display(value, code)}"

    body = _MARK_RE.sub(value_of, text)
    head = verdict(filled, why_not)
    return f"{head}\n{body}" if head and body else head or body


def strip_marks(text: str) -> str:
    return _MARK_RE.sub("", text)
