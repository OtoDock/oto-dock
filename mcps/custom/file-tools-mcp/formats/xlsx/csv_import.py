"""write_xlsx's import_csv: a CSV file's rows written as cells, numbers and
ISO dates typed, identifiers kept as text, nothing ever read as a formula."""

import csv
import io
import os
import re
import stat
from copy import copy

from catalog import KeySpec, OpSpec
from formats.xlsx.values import flag_key
from results import Code, OpCtx, OpError

# A single import is held in the worker's memory as openpyxl cells (about
# half a kilobyte each), so it is capped well inside the worker's budget.
MAX_CELLS = 300_000
MAX_ROW = 1_048_576
MAX_COL = 16_384
MAX_BYTES = 64 * 1024 * 1024
_SNIFF_BYTES = 64 * 1024
_DELIMITERS = ",;\t|"
_DEFAULT_ENCODING = "utf-8-sig"
_ENCODING_HINT = ("Pass encoding: 'cp1253' for Greek Windows files, 'latin-1' for Western European ones, "
                  "or save the file as UTF-8.")
# Excel keeps 15 significant digits: longer digit strings are identifiers
# (account numbers, barcodes) and stay text, as do codes with a leading zero.
_MAX_DIGITS = 15
_NUMBER_RE = {
    ".": re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", re.ASCII),
    ",": re.compile(r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?", re.ASCII),
}
_THOUSANDS = {".": ",", ",": "."}
_LEADING_ZERO_RE = re.compile(r"[+-]?0\d", re.ASCII)


def _decode(data: bytes, encoding: str | None) -> str:
    if encoding and encoding.strip().lower().replace("_", "-") in ("utf-8", "utf8"):
        encoding = None
    try:
        return data.decode(encoding or _DEFAULT_ENCODING)
    except UnicodeDecodeError:
        raise OpError(Code.BAD_VALUE, f"the file is not {encoding or 'UTF-8'} text",
                      hint=_ENCODING_HINT) from None
    except LookupError:
        raise OpError(Code.BAD_VALUE, f"'{encoding}' is not a known encoding", hint=_ENCODING_HINT) from None


def _delimiter(text: str, given: str | None) -> str:
    if given:
        if given in ("\\t", "tab"):
            return "\t"
        if len(given) != 1:
            raise OpError(Code.BAD_VALUE, f"delimiter '{given}' is not one character",
                          hint="Give one character, such as ',' ';' or '\\t'.")
        return given
    try:
        return csv.Sniffer().sniff(text[:_SNIFF_BYTES], delimiters=_DELIMITERS).delimiter
    except csv.Error:
        return ","


def _number(text: str, decimal: str):
    """The number `text` spells, or None (an identifier or not a number)."""
    stripped = text.strip()
    if not _NUMBER_RE[decimal].fullmatch(stripped):
        return None
    digits = sum(ch.isdigit() for ch in stripped)
    if digits > _MAX_DIGITS or _LEADING_ZERO_RE.match(stripped):
        return None
    plain = stripped.replace(_THOUSANDS[decimal], "").replace(decimal, ".")
    if "." in plain:
        return float(plain)
    return int(plain)


def _read_bounded(path: str) -> bytes:
    """At most MAX_BYTES of a regular file. Opened without blocking, so a
    pipe or a device is refused instead of waited on."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OpError(Code.BAD_VALUE, f"{os.path.basename(path)} is not a regular file",
                          hint="Import a CSV file saved on disk.")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_BYTES:
        raise OpError(Code.BAD_VALUE, f"the file is over the {MAX_BYTES // (1024 * 1024)} MB one import takes",
                      hint="Split the file and import each part.")
    return data


def _rows(text: str, delimiter: str) -> list[list[str]]:
    """The file's rows, read one at a time and stopped as soon as the cell
    count passes MAX_CELLS (the worker's memory is the bound, not the file
    size); trailing empty rows dropped."""
    rows: list[list[str]] = []
    cells = 0
    for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
        cells += len(row)
        if cells > MAX_CELLS:
            raise OpError(Code.BAD_VALUE, f"the file is over the {MAX_CELLS:,} cells one import takes "
                          f"(reached at row {len(rows) + 1})",
                          hint="Split the file and import each part with its own start_cell.")
        rows.append(row)
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    if not rows:
        raise OpError(Code.BAD_VALUE, "the file holds no rows")
    return rows


def _op_import_csv(job, ctx: OpCtx, op: dict) -> None:
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.utils import get_column_letter

    import excel

    ws = job.sheet(op.get("sheet"))
    path = op["csv_path"]
    text = _decode(_read_bounded(path), op.get("encoding"))
    rows = _rows(text, _delimiter(text, op.get("delimiter")))
    width = max(len(row) for row in rows)
    col0, row0 = excel._parse_cell_ref(op.get("start_cell", "A1"))
    if col0 + width - 1 > MAX_COL:
        raise OpError(Code.BAD_VALUE, f"{width} columns from {op.get('start_cell', 'A1')} run past column XFD, "
                      "the last one a sheet has", hint="Start further left, or split the file by columns.")
    if row0 + len(rows) - 1 > MAX_ROW:
        raise OpError(Code.BAD_VALUE, f"{len(rows)} rows from {op.get('start_cell', 'A1')} run past row "
                      f"{MAX_ROW:,}, the last one a sheet has", hint="Start higher up, or split the file.")
    header = op.get("header", True)
    typed = op.get("types", True)
    decimal = op.get("decimal", ".")
    if decimal not in _NUMBER_RE:
        raise OpError(Code.BAD_VALUE, f"decimal '{decimal}' is not '.' or ','")
    tally = {"converted": 0, "warned": {}}
    kept_text = 0
    stripped = 0
    for r, row in enumerate(rows):
        for c, raw in enumerate(row):
            if raw == "":
                continue
            raw, n = ILLEGAL_CHARACTERS_RE.subn("", raw)
            stripped += n
            cell = ws.cell(row=row0 + r, column=col0 + c)
            if not typed or (header and r == 0):
                cell.value = raw
                cell.data_type = "s"
                continue
            number = _number(raw, decimal)
            if number is not None:
                cell.value = number
                continue
            if raw.strip().isdigit():
                kept_text += 1
            value, kind = excel._coerce_cell_value(raw)
            if kind is not None and kind in excel._TEMPORAL_KINDS:
                excel._set_coerced(cell, value, kind, raw=raw)
                excel._tally_coercion(tally, kind, raw, cell.coordinate)
                continue
            if kind is not None:
                excel._tally_coercion(tally, kind, raw, cell.coordinate)
            # Text that looks like a formula stays text: a file's '=cmd|...'
            # must never become a formula.
            cell.value = raw
            cell.data_type = "s"
    last_row, last_col = row0 + len(rows) - 1, col0 + width - 1
    if header:
        for c in range(width):
            cell = ws.cell(row=row0, column=col0 + c)
            # The cell's own font made bold: a bare Font(bold=True) has no
            # name or size, and LibreOffice draws it in a serif face.
            font = copy(cell.font)
            font.b = True
            cell.font = font
        ws.freeze_panes = f"{get_column_letter(col0)}{row0 + 1}"
        ws.auto_filter.ref = f"{get_column_letter(col0)}{row0}:{get_column_letter(last_col)}{last_row}"
    job.touch(ws, row0, col0, last_row, last_col)
    ctx.note(f"{len(rows)} rows x {width} columns imported from {os.path.basename(path)} into "
             f"{get_column_letter(col0)}{row0}:{get_column_letter(last_col)}{last_row} on '{ws.title}'")
    if kept_text:
        ctx.note(f"{kept_text} digit value(s) with a leading zero or over {_MAX_DIGITS} digits kept as text")
    if stripped:
        ctx.note(f"{stripped} control character(s) a worksheet cannot hold were removed")
    excel._flush_coercion(tally, ctx, ws.title)


OPS = [
    OpSpec("import_csv", _op_import_csv, (
        KeySpec("csv_path", ("path",), required=True, aliases=("path", "file", "source")),
        KeySpec("sheet", ("str",)),
        KeySpec("start_cell", ("str",), default="A1"),
        KeySpec("delimiter", ("str",), help="default: detected among , ; tab |"),
        KeySpec("encoding", ("str",), default="utf-8", help="e.g. cp1253 for Greek Windows files"),
        KeySpec("decimal", ("str",), default=".", help="',' for numbers written 1.234,56"),
        flag_key("header", default=True, help="first row bold, frozen and filtered"),
        flag_key("types", default=True, help="false keeps every value as text"),
    ), "CELLS", aliases=("import", "load_csv"),
        note="numbers and strict ISO dates are typed; codes with a leading zero, digit strings over 15 "
             "digits and text that looks like a formula stay text; an empty field leaves the cell as it was",
        examples=({"op": "import_csv", "csv_path": "data.csv", "sheet": "Sheet", "start_cell": "A1",
                   "delimiter": ";", "encoding": "utf-8", "decimal": ",", "header": True, "types": True},)),
]
