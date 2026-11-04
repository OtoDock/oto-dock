"""Excel (XLSX) handlers for the file-tools MCP: write_xlsx (its catalogue,
handlers and core) and read_xlsx, whose reader lives in formats.xlsx.read."""

import datetime
import functools
import io
import os
import re
import uuid
from copy import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from batch import Rendered, WriteCall, discard_deferred, handle_write_call
from catalog import Catalog, KeySpec, OpSpec
from checks import collect_baseline
from colors import parse_color
from equations import latex_to_png
from formats.xlsx import (
    annotations,
    computed,
    csv_import,
    guard,
    inventory,
    loss,
    printing,
    recalc,
    values,
)
from formats.xlsx.formulas import (
    normalize,
    parens_balanced,
    quote_sheet_names,
    unsafe_functions,
)
from formats.xlsx.package import (
    MacroParts,
    SheetParts,
    check_readable,
    finish_package,
    is_template,
    keeps_vba,
    needs_package_pass,
    read_macro_parts,
    sheet_parts,
)
from formats.xlsx.read import (
    _anchor_cell,
    _equation_latex,
    _escape_cell,
    _grid_lines,
    _merged_anchor_map,
    _parse_cell_ref,
    _preflight_density,
    read_xlsx as read_xlsx,
)
from formats.xlsx.refs import rewrite_refs
from formats.xlsx.refshift import (
    Axis,
    Edit,
    EditKind,
    ShiftReport,
    apply_edit,
    check_edit,
    copy_ref,
    delete_sheet as _cascade_delete_sheet,
    rename_sheet as _cascade_rename_sheet,
    shift_box,
)
from formats.xlsx.values import flag_key
from results import Code, NoOp, OpCtx, OpError, Staged, WriteReport
from shared import _WRITE_OP_ADVICE, logger
from units import to_cm, to_pt, to_px

# ---------------------------------------------------------------------------
# Write — helpers
# ---------------------------------------------------------------------------


# openpyxl's theme colour indexes (the first two pairs are swapped against
# the order of the theme's colour scheme).
_XL_THEME_INDEX = {
    "lt1": 0, "dk1": 1, "lt2": 2, "dk2": 3, "accent1": 4, "accent2": 5, "accent3": 6,
    "accent4": 7, "accent5": 8, "accent6": 9, "hlink": 10, "folHlink": 11,
}


def _xl_color(value):
    """A colour for openpyxl: 'AARRGGBB' (8-digit hex is ARGB here), or a
    theme Color with its tint (a shade is a negative tint)."""
    color = parse_color(value, argb=True)
    if color.theme is None:
        return color.argb
    from openpyxl.styles.colors import Color

    tint = color.tint if color.tint is not None else -(color.shade or 0.0)
    return Color(theme=_XL_THEME_INDEX[color.theme], tint=tint)


def _get_sheet(wb, op: dict):
    """Get worksheet from operation, defaulting to first sheet."""
    return _sheet_named(wb, op.get("sheet"))


def _sheet_named(wb, name: str | None):
    """A worksheet by name, the first one when no name is given."""
    if name is None:
        return wb[wb.sheetnames[0]]
    if name not in wb.sheetnames:
        raise OpError(Code.NOT_FOUND, f"sheet '{name}' does not exist", location=f"sheet '{name}'",
                      hint=f"The sheets are: {', '.join(wb.sheetnames)}.")
    return wb[name]


def _strip_leading_eq(s: str) -> str:
    """openpyxl serializes formula1/attr_text verbatim into the XML, where a
    leading '=' is invalid ST_Formula — but agents habitually write '=Name'."""
    return s[1:] if s.startswith("=") else s


# A single-item list value is a reference only on an explicit '=' or a
# full-string sheet-qualified range — a bare contains('!') test would turn
# the literal ["Yes!"] into a broken reference.
_SHEET_RANGE_RE = re.compile(
    r"(?:'(?:[^']|'')+'|[^\W\d][\w.]*)!"
    r"\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?"
)

# Typo-guard candidate shapes: only identifier-shaped references are checked
# against the workbook's defined names — 'B2' is a valid relative cell
# reference, not a name typo, and anything with '(' is a function call.
_BARE_NAME_RE = re.compile(r"[^\W\d][\w.]*")
_A1_REF_RE = re.compile(r"[A-Za-z]{1,3}\d+")


def _sqref_intersects(sqref_a, sqref_b) -> bool:
    """True when any member range of one MultiCellRange overlaps any member
    of the other. Plain min/max bounds arithmetic — DV sqrefs are sheet-local
    (title-less), so CellRange's title-aware set ops don't apply."""
    for a in sqref_a.ranges:
        for b in sqref_b.ranges:
            if (
                a.min_col <= b.max_col and b.min_col <= a.max_col
                and a.min_row <= b.max_row and b.min_row <= a.max_row
            ):
                return True
    return False

def _validate_formula(formula: str) -> str | None:
    """Validate a formula as the caller wrote it. Returns error string or
    None if valid."""
    if not parens_balanced(formula):
        return "Unbalanced parentheses in formula"
    return None


def _refuse_unsafe(text: str, where: str) -> None:
    """Every formula an operation stores passes here first, as the caller
    wrote it."""
    names = unsafe_functions(text)
    if not names:
        return
    what = ("a DDE link (app|topic!item)" if names[0] == "DDE" and "|" in text
            else f"the blocked function '{names[0]}'")
    raise OpError(Code.BAD_VALUE, f"the formula contains {what}, which reaches the network or runs a "
                  "program, so it is not written", location=where,
                  hint="Write the values themselves, or use functions that read the workbook only.")


def _table_at(ws, row: int, col: int) -> str | None:
    """The name of the table holding a cell, for a bare [@Column]."""
    from openpyxl.worksheet.cell_range import CellRange

    for table in ws.tables.values():
        rng = CellRange(table.ref)
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return table.displayName
    return None


def _store_formula(job: "_XlsxJob", ctx: OpCtx, ws, cell, text: str) -> str:
    """`text` in Excel's stored form, its notes and warnings recorded, and
    the cell kept for the end-of-batch sheet-name check."""
    where = f"{ws.title}!{cell.coordinate}"
    _refuse_unsafe(text, where)
    result = normalize(text, job.wb.sheetnames, table=_table_at(ws, cell.row, cell.column))
    if result.errors:
        raise OpError(Code.BAD_VALUE, result.errors[0], location=where)
    for note in result.notes:
        ctx.note(f"{where}: {note}")
    for warning in result.warnings:
        ctx.warn(f"{where}: {warning}")
    job.formula_cells.append((ctx.index, ctx.op, ws, cell))
    return result.text


def _stored_bare(job: "_XlsxJob", ctx: OpCtx, text: str, where: str) -> str:
    """A formula stored without '=' (validation, conditional format, name)."""
    _refuse_unsafe(text, where)
    result = normalize(text, job.wb.sheetnames, bare=True)
    if result.errors:
        raise OpError(Code.BAD_VALUE, result.errors[0], location=where)
    for note in result.notes:
        ctx.note(f"{where}: {note}")
    for warning in result.warnings:
        ctx.warn(f"{where}: {warning}")
    return result.text


# Named number-format presets (case-insensitive lookup). Anything not listed
# passes through verbatim as a raw Excel format code.
_NUMBER_FORMAT_PRESETS = {
    "date": "dd/mm/yyyy",
    "date-iso": "yyyy-mm-dd",
    "datetime": "dd/mm/yyyy hh:mm",
    "time": "hh:mm",
    "percent": "0.00%",
    "number": "#,##0.00",
    "integer": "#,##0",
    "currency": "€#,##0.00",
    "currency:usd": "$#,##0.00",
    "currency:gbp": "£#,##0.00",
    "text": "@",
}


def _resolve_number_format(nf):
    """Preset name → Excel format code; anything else passes through verbatim."""
    if isinstance(nf, str):
        return _NUMBER_FORMAT_PRESETS.get(nf.strip().casefold(), nf)
    return nf


# Strict ISO shapes — full-string matches, then VALIDATED by parsing
# ("2026-13-45" matches the regex but is not a date). re.ASCII: without it \d
# matches e.g. Arabic-Indic digits, which then fail fromisoformat and warn as
# "invalid ISO" for strings that were never meant as dates.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?", re.ASCII
)
_ISO_TIME_RE = re.compile(r"\d{2}:\d{2}(?::\d{2})?", re.ASCII)
# Excel has no timezones — silently dropping the offset would corrupt data.
_ISO_TZ_RE = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}[T ])?\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})",
    re.ASCII,
)
# Ambiguous day/month order ('27/03/2026') — NEVER guessed, warned instead.
_AMBIGUOUS_DATE_RE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", re.ASCII)

_TEMPORAL_KINDS = ("date", "datetime", "time")

# Decided display for auto-converted values: European dd/mm/yyyy.
_AUTO_DISPLAY = {"date": "dd/mm/yyyy", "datetime": "dd/mm/yyyy hh:mm"}


def _auto_display_format(kind: str, value) -> str:
    if kind == "time":
        return "hh:mm:ss" if value.second or value.microsecond else "hh:mm"
    return _AUTO_DISPLAY[kind]


def _coerce_cell_value(value, explicit_type=None):
    """(coerced value, kind) — kind ∈ date/datetime/time, a text-warned-*
    marker, or None (untouched).

    ONLY strings are ever examined: numbers, bools and None pass through
    byte-identical (raw Excel serials must keep working), as do formula
    strings and anything under explicit type "text". Explicit types
    date/datetime/time accept the SAME strict ISO — they never unlock
    guessing, they just turn a silent text landing into a warning."""
    if not isinstance(value, str) or explicit_type == "text" or value.startswith("="):
        return value, None
    if _ISO_TZ_RE.fullmatch(value):
        return value, "text-warned-tz"
    try:
        if _ISO_DATETIME_RE.fullmatch(value):
            parsed = datetime.datetime.fromisoformat(value)
            # Excel's 1900 date system starts at 1900-01-01 — earlier dates
            # save as serial <= 0 and silently corrupt ('1899-12-31' came
            # back as time(0, 0)).
            if parsed.year < 1900:
                return value, "text-warned-pre1900"
            return parsed, "datetime"
        if _ISO_DATE_RE.fullmatch(value):
            parsed = datetime.date.fromisoformat(value)
            if parsed.year < 1900:
                return value, "text-warned-pre1900"
            return parsed, "date"
        if _ISO_TIME_RE.fullmatch(value):
            return datetime.time.fromisoformat(value), "time"
    except ValueError:
        return value, "text-warned-invalid"
    if _AMBIGUOUS_DATE_RE.fullmatch(value):
        return value, "text-warned-ambiguous"
    if explicit_type in _TEMPORAL_KINDS:
        return value, "text-warned-invalid"
    return value, None


def _set_coerced(cell, value, kind, fmt=None, raw=None):
    """Assign a coerced value with format discipline: an explicit per-cell
    format always wins; a coerced date/datetime/time on a General cell gets
    the decided display format; a pre-existing explicit format (template
    workbooks) is NEVER overridden — openpyxl stamps its own ISO-ish default
    on datetime assignment, so the prior format is captured and restored.
    Exception: a Text-formatted target ('@' — pre-existing or requested via
    fmt) keeps the RAW string — a date serial displayed through '@' is
    exactly the incident symptom. Returns the effective kind for tallying."""
    prior = cell.number_format
    fmt = _resolve_number_format(fmt) if fmt else None
    if kind in _TEMPORAL_KINDS and (fmt or prior) == "@" and raw is not None:
        cell.value = raw
        if fmt:
            cell.number_format = fmt
        return "text-warned-textfmt"
    cell.value = value
    if fmt:
        cell.number_format = fmt
    elif kind in _TEMPORAL_KINDS:
        cell.number_format = (
            _auto_display_format(kind, value) if prior == "General" else prior
        )
    return kind


# Aggregate coercion reporting — one line per op (a 500-cell write must not
# emit 500 lines): conversions to notes, warned-text kinds to errors with a
# count and ONE example cell ref.
_COERCE_WARN_TEXT = {
    "text-warned-ambiguous": (
        "look like dates but were written as TEXT (e.g. {ex}) — write ISO "
        '2026-03-27, or pass type: "date"'
    ),
    "text-warned-tz": (
        "carry a timezone suffix and were written as TEXT (e.g. {ex}) — "
        "Excel has no timezones; convert and drop the offset"
    ),
    "text-warned-invalid": (
        "are not valid ISO dates/times and were written as TEXT (e.g. {ex}) "
        "— write strict ISO like 2026-03-27, 2026-03-27T14:30 or 14:30"
    ),
    "text-warned-pre1900": (
        "predate Excel's 1900 date system and were written as TEXT (e.g. "
        "{ex}) — Excel cannot store dates before 1900-01-01 as real dates"
    ),
    "text-warned-textfmt": (
        "target Text-formatted cells ('@') and were written as TEXT (e.g. "
        "{ex}) — clear the cell's Text format or pass format: 'date' to "
        "store real dates"
    ),
}


def _tally_coercion(tally: dict, kind: str, value, ref: str) -> None:
    if kind in _TEMPORAL_KINDS:
        tally["converted"] += 1
    else:
        tally["warned"].setdefault(kind, [0, value, ref])[0] += 1


def _flush_coercion(tally: dict, ctx: OpCtx, sheet: str) -> None:
    if tally["converted"]:
        ctx.note(
            f"on '{sheet}': {tally['converted']} ISO date/time value(s) "
            f"written as real dates (dd/mm/yyyy)"
        )
    for kind, (n, val, ref) in sorted(tally["warned"].items()):
        ctx.warn(f"{n} value(s) " + _COERCE_WARN_TEXT[kind].format(ex=f"'{val}' at {ref}"))


# ---------------------------------------------------------------------------
# Write — operation catalogue helpers
# ---------------------------------------------------------------------------
# The catalogue itself (CATALOG, at the end of the module) is built from the
# handlers; it is write_xlsx's dispatch table, key validation and help.

_NUMBER_FORMAT_HELP = (
    "number_format / format: preset (date, date-iso, datetime, time, "
    "percent, number, integer, currency, currency:usd, currency:gbp, text) "
    "or a raw Excel format code"
)


_CLASS_REPR_RE = re.compile(r"<class '(?:[\w.]+\.)?(\w+)'>")
_UNEXPECTED_KW_RE = re.compile(r"unexpected keyword argument '(\w+)'")


def _friendly_error(exc: BaseException) -> str:
    """Per-op failure text without library internals: openpyxl's class
    reprs and Python's keyword-argument phrasing meant nothing to a caller."""
    if isinstance(exc, KeyError) and exc.args:
        return f"missing key {exc.args[0]!r}"
    text = str(exc) or exc.__class__.__name__
    m = _UNEXPECTED_KW_RE.search(text)
    if m:
        return f"unknown parameter '{m.group(1)}'"
    return _CLASS_REPR_RE.sub(r"\1", text)


# ---------------------------------------------------------------------------
# Write — data validation
# ---------------------------------------------------------------------------

_DV_TYPES = ("list", "whole", "decimal", "date", "time", "textLength", "custom")

_DV_TYPE_ALIASES = {
    "dropdown": "list", "integer": "whole", "int": "whole", "number": "decimal",
    "float": "decimal", "text_length": "textLength", "textlength": "textLength",
    "length": "textLength", "formula": "custom", "expression": "custom",
}


def _dv_type(text) -> str:
    raw = str(text).strip()
    kind = _DV_TYPE_ALIASES.get(raw.lower(), raw)
    if kind not in _DV_TYPES:
        raise ValueError(
            f"validation_type '{text}' is not one of: {', '.join(_DV_TYPES)}"
        )
    return kind


def _dv_operator(text) -> str:
    key = str(text).strip().replace("_", "").replace(" ", "").lower()
    op = _CF_COMPARISONS.get(key)
    if not op:
        raise ValueError(
            f"operator '{text}' is not one of: between, notBetween, equal, "
            f"notEqual, greaterThan, greaterThanOrEqual, lessThan, "
            f"lessThanOrEqual (or > < >= <= = !=)"
        )
    return op


def _dv_bound(v, dv_type: str) -> str:
    """A validation bound as formula text. ISO dates/times become DATE()/
    TIME() — a bare '2026-01-31' inside the rule is text to Excel and the
    rule silently rejects everything."""
    s = _strip_leading_eq(str(v).strip())
    _refuse_unsafe(s, "a validation bound")
    if dv_type == "date":
        if _ISO_DATETIME_RE.fullmatch(s):
            raise ValueError(f"date bound '{v}' includes a time — use a date (YYYY-MM-DD)")
        if _ISO_DATE_RE.fullmatch(s):
            d = datetime.date.fromisoformat(s)
            return f"DATE({d.year},{d.month},{d.day})"
    if dv_type == "time" and _ISO_TIME_RE.fullmatch(s):
        t = datetime.time.fromisoformat(s)
        return f"TIME({t.hour},{t.minute},{t.second})"
    return s


# ---------------------------------------------------------------------------
# Write — conditional formatting
# ---------------------------------------------------------------------------

_CF_RULE_TYPES = ("cell_is", "formula", "color_scale", "data_bar", "icon_set")

_CF_RULE_ALIASES = {
    "cellis": "cell_is", "cell": "cell_is", "value": "cell_is",
    "expression": "formula", "colorscale": "color_scale", "colourscale": "color_scale",
    "databar": "data_bar", "iconset": "icon_set", "icons": "icon_set",
}

# Operator spellings → the cellIs operator; symbols and snake_case included.
_CF_COMPARISONS = {
    "greaterthan": "greaterThan", ">": "greaterThan", "gt": "greaterThan",
    "lessthan": "lessThan", "<": "lessThan", "lt": "lessThan",
    "between": "between", "notbetween": "notBetween",
    "equal": "equal", "equals": "equal", "=": "equal", "==": "equal", "eq": "equal",
    "notequal": "notEqual", "!=": "notEqual", "<>": "notEqual", "ne": "notEqual",
    "greaterthanorequal": "greaterThanOrEqual", ">=": "greaterThanOrEqual",
    "ge": "greaterThanOrEqual",
    "lessthanorequal": "lessThanOrEqual", "<=": "lessThanOrEqual", "le": "lessThanOrEqual",
}

# Text operators are not valid on a cellIs rule; Excel writes them as an
# expression rule over the range's top-left cell, so that is what we emit.
_CF_TEXT_OPERATORS = {
    "containstext": "containsText", "contains": "containsText",
    "notcontains": "notContains", "notcontainstext": "notContains",
    "doesnotcontain": "notContains",
    "beginswith": "beginsWith", "startswith": "beginsWith",
    "endswith": "endsWith",
}

_CF_TEXT_EXPR = {
    "containsText": "NOT(ISERROR(SEARCH({t},{tl})))",
    "notContains": "ISERROR(SEARCH({t},{tl}))",
    "beginsWith": "LEFT({tl},LEN({t}))={t}",
    "endsWith": "RIGHT({tl},LEN({t}))={t}",
}

_ICON_SETS = (
    "3Arrows", "3ArrowsGray", "3Flags", "3Signs", "3Symbols", "3Symbols2",
    "3TrafficLights1", "3TrafficLights2", "4Arrows", "4ArrowsGray", "4Rating",
    "4RedToBlack", "4TrafficLights", "5Arrows", "5ArrowsGray", "5Quarters", "5Rating",
)

# Characters that make an operand an expression rather than bare text.
_CF_EXPR_CHARS = set("()<>=+-*/&$!:")

# The keys the nested `params` form may carry (the flat rule keys).
_CF_PARAM_KEYS = (
    "operator", "formula", "fill_color", "font_color", "bold", "italic", "stop_if_true",
    "colors", "start_type", "start_value", "start_color", "mid_type", "mid_value", "mid_color",
    "end_type", "end_value", "end_color", "color", "show_value", "min_length", "max_length",
    "icon_style", "threshold_type", "values", "percent", "reverse",
)

# The nested `params` form uses openpyxl's own spellings; map them onto the
# flat keys so both shapes build the same rule.
_CF_PARAM_ALIASES = {
    "fill": "fill_color", "background": "fill_color", "stopIfTrue": "stop_if_true",
    "showValue": "show_value", "type": "threshold_type", "minLength": "min_length",
    "maxLength": "max_length", "value": "formula", "icon_set": "icon_style",
    "iconSet": "icon_style",
}


def _cf_operator(text) -> str:
    key = str(text).strip().replace("_", "").replace(" ", "").lower()
    op = _CF_COMPARISONS.get(key) or _CF_TEXT_OPERATORS.get(key)
    if not op:
        raise ValueError(
            f"operator '{text}' is not one of: greaterThan, lessThan, between, "
            f"notBetween, equal, notEqual, greaterThanOrEqual, lessThanOrEqual "
            f"(or > < >= <= = !=), containsText, notContains, beginsWith, endsWith"
        )
    return op


def _cf_operand(v) -> str:
    """One cell_is operand as formula text: numbers, references and
    expressions verbatim (a leading '=' stripped — it is invalid inside
    <formula>), bare text quoted, since an unquoted word is a #NAME
    reference in Excel."""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = _strip_leading_eq(str(v).strip())
    if not s:
        raise ValueError("an empty operand")
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s
    if s.upper() in ("TRUE", "FALSE"):
        return s.upper()
    try:
        float(s)
        return s
    except ValueError:
        pass
    if any(ch in _CF_EXPR_CHARS for ch in s) or _A1_REF_RE.fullmatch(s):
        return s
    return '"' + s.replace('"', '""') + '"'


def _cf_merge(op: dict) -> dict:
    """Flat rule parameters: the nested `params` form (openpyxl spellings,
    dict fill/font) folded under the top-level keys, which win."""
    flat: dict = {}
    params = op.get("params")
    if params is not None:
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        accepted = set(_CF_PARAM_KEYS)
        for k, v in params.items():
            if k == "font":
                if isinstance(v, dict):
                    for fk, fv in v.items():
                        if fk == "color":
                            flat["font_color"] = fv
                        elif fk in ("bold", "italic"):
                            flat[fk] = fv
                        else:
                            raise ValueError(f"params.font: unknown key '{fk}' — accepted: color, bold, italic")
                else:
                    raise ValueError("params.font must be an object {color?, bold?, italic?}")
                continue
            ck = _CF_PARAM_ALIASES.get(k, k)
            if ck == "fill_color" and isinstance(v, dict):
                v = v.get("color") or v.get("fill_color") or v.get("start_color")
            if ck not in accepted:
                raise ValueError(
                    f"params: unknown key '{k}' — accepted: {', '.join(sorted(accepted))}"
                )
            flat[ck] = v
    for k, v in op.items():
        if k not in ("op", "params", "sheet", "range", "rule_type") and v is not None:
            flat[k] = v
    return flat


def _color_scale_rule(p: dict, threshold):
    """A colour-scale rule from the flat parameters; `threshold` checks a
    text threshold the way a rule formula is checked."""
    from openpyxl.formatting.rule import ColorScaleRule

    kw: dict = {}
    colors = p.get("colors")
    if colors is not None:
        if not isinstance(colors, list) or len(colors) not in (2, 3):
            raise ValueError("colors must be [min_hex, max_hex] or [min_hex, mid_hex, max_hex]")
        kw = {"start_type": "min", "start_color": colors[0],
              "end_type": "max", "end_color": colors[-1]}
        if len(colors) == 3:
            kw.update(mid_type="percentile", mid_value=50, mid_color=colors[1])
    for key in ("start_type", "start_value", "start_color", "mid_type", "mid_value",
                "mid_color", "end_type", "end_value", "end_color"):
        if p.get(key) is not None:
            kw[key] = p[key]
    for key in ("start_value", "mid_value", "end_value"):
        if key in kw:
            kw[key] = threshold(kw[key])
    if not kw.get("start_color") or not kw.get("end_color"):
        raise ValueError("color_scale needs colors: [min_hex, max_hex] (or [min, mid, max])")
    kw.setdefault("start_type", "min")
    kw.setdefault("end_type", "max")
    if kw.get("mid_color") and not kw.get("mid_type"):
        kw["mid_type"] = "percentile"
        kw.setdefault("mid_value", 50)
    for key in ("start_color", "mid_color", "end_color"):
        if kw.get(key):
            kw[key] = _xl_color(kw[key])
    return ColorScaleRule(**kw)


_STRING_LITERAL_RE = re.compile(r'"(?:[^"]|"")*"')


def _conditional_format(ws, op: dict, ctx: OpCtx) -> None:
    """Build and add one rule. dxf rules (cell_is / formula) take their look
    ONLY from what was asked — there is no default fill."""
    from openpyxl.formatting.rule import (
        CellIsRule,
        DataBarRule,
        FormulaRule,
        IconSetRule,
    )
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.cell_range import MultiCellRange

    cf_range = str(op["range"]).strip()
    try:
        target = MultiCellRange(cf_range)
    except (ValueError, TypeError):
        raise ValueError(
            f"range '{cf_range}' is not a valid A1 range (e.g. 'B2:B10' or 'B2:B10 D2:D10')"
        ) from None
    raw_type = str(op["rule_type"]).strip().lower().replace("-", "_")
    rule_type = _CF_RULE_ALIASES.get(raw_type.replace("_", ""), raw_type)
    if rule_type not in _CF_RULE_TYPES:
        raise ValueError(
            f"rule_type '{op['rule_type']}' is not one of: {', '.join(_CF_RULE_TYPES)}"
        )
    p = _cf_merge(op)

    fill = font = None
    if p.get("fill_color") is not None:
        color = _xl_color(p["fill_color"])
        fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
    font_kw = {}
    if p.get("font_color") is not None:
        font_kw["color"] = _xl_color(p["font_color"])
    for key in ("bold", "italic"):
        if p.get(key) is not None:
            font_kw[key] = bool(p[key])
    if font_kw:
        font = Font(**font_kw)

    def stored(text: str) -> str:
        """A rule formula in Excel's stored form (bare, no '=')."""
        if _STRING_LITERAL_RE.fullmatch(text):
            return text
        _refuse_unsafe(text, f"{ws.title}!{cf_range}")
        result = normalize(text, ws.parent.sheetnames, bare=True)
        if result.errors:
            raise OpError(Code.BAD_VALUE, result.errors[0], location=f"{ws.title}!{cf_range}")
        for note in result.notes:
            ctx.note(note)
        for warning in result.warnings:
            ctx.warn(warning)
        return result.text

    def threshold(value):
        """A colour-scale, data-bar or icon threshold: a text value can be a
        formula Excel computes, so it passes the same check."""
        return stored(value) if isinstance(value, str) else value

    if rule_type in ("cell_is", "formula") and fill is None and font is None:
        raise ValueError(
            f"a {rule_type} rule has no visible style — pass fill_color and/or "
            f"font_color, bold, italic"
        )

    if rule_type == "cell_is":
        if p.get("operator") is None or p.get("formula") is None:
            raise ValueError(
                "cell_is needs operator (e.g. greaterThan) and formula (the "
                "value to compare with; [low, high] for between)"
            )
        operator = _cf_operator(p["operator"])
        raw = p["formula"]
        operands = [stored(_cf_operand(o)) for o in (raw if isinstance(raw, list) else [raw])]
        if operator in ("between", "notBetween"):
            if len(operands) != 2:
                raise ValueError(f"{operator} needs formula: [low, high]")
        elif len(operands) != 1:
            raise ValueError(f"{operator} takes a single value, got {len(operands)}")
        if operator in _CF_TEXT_EXPR:
            # Rule formulas are relative to the top-left of the range's
            # bounding box (openpyxl keeps the ranges as an unordered set).
            tl = (f"{get_column_letter(min(r.min_col for r in target.ranges))}"
                  f"{min(r.min_row for r in target.ranges)}")
            expr = _CF_TEXT_EXPR[operator].format(t=operands[0], tl=tl)
            rule = FormulaRule(formula=[expr], fill=fill, font=font)
        else:
            rule = CellIsRule(operator=operator, formula=operands, fill=fill, font=font)
    elif rule_type == "formula":
        raw = p.get("formula")
        if raw is None:
            raise ValueError(
                "formula rules need formula: an expression written for the "
                "range's top-left cell, e.g. '$C2>100'"
            )
        formulas = [stored(_strip_leading_eq(str(x).strip()))
                    for x in (raw if isinstance(raw, list) else [raw])]
        if not formulas or not formulas[0]:
            raise ValueError("formula must not be empty")
        rule = FormulaRule(formula=formulas, fill=fill, font=font)
    elif rule_type == "color_scale":
        rule = _color_scale_rule(p, threshold)
    elif rule_type == "data_bar":
        rule = DataBarRule(
            start_type=p.get("start_type") or "min", start_value=threshold(p.get("start_value")),
            end_type=p.get("end_type") or "max", end_value=threshold(p.get("end_value")),
            color=_xl_color(p.get("color") or "638EC6"), showValue=p.get("show_value"),
            minLength=p.get("min_length"), maxLength=p.get("max_length"),
        )
    else:
        style = str(p.get("icon_style") or "3TrafficLights1")
        if style not in _ICON_SETS:
            raise ValueError(f"icon_style '{style}' is not one of: {', '.join(_ICON_SETS)}")
        n = int(style[0])
        values = p.get("values")
        if values is None:
            values = [round(100 * i / n) for i in range(n)]
        if not isinstance(values, list) or len(values) != n:
            raise ValueError(f"icon_style {style} shows {n} icons, so values needs {n} thresholds")
        values = [threshold(v) for v in values]
        rule = IconSetRule(
            icon_style=style, type=p.get("threshold_type") or "percent", values=values,
            showValue=p.get("show_value"), percent=p.get("percent"), reverse=p.get("reverse"),
        )
    if p.get("stop_if_true"):
        rule.stopIfTrue = True
    ws.conditional_formatting.add(cf_range, rule)
    ctx.note(f"{rule_type} rule on {cf_range} on '{ws.title}'")


def _remove_conditional_format(ws, op: dict, ctx: OpCtx) -> None:
    """Drop every rule whose cells intersect `range` (or all of them) and
    rebuild the sheet's list — priorities renumber from 1 in the original
    order."""
    from openpyxl.formatting.formatting import ConditionalFormattingList
    from openpyxl.worksheet.cell_range import MultiCellRange

    target = None
    if op.get("range"):
        try:
            target = MultiCellRange(str(op["range"]).strip())
        except (ValueError, TypeError):
            raise ValueError(f"range '{op['range']}' is not a valid A1 range") from None
    elif not op.get("all"):
        raise ValueError("provide range or all: true")
    kept = ConditionalFormattingList()
    removed = 0
    for cf in ws.conditional_formatting:
        if target is None or _sqref_intersects(cf.sqref, target):
            removed += len(cf.rules)
            continue
        for rule in cf.rules:
            rule.priority = 0
            kept.add(str(cf.sqref), rule)
    ws.conditional_formatting = kept
    if removed == 0:
        raise NoOp(f"no conditional-format rules intersect {op['range']}" if target is not None
                   else f"'{ws.title}' has no conditional-format rules")
    ctx.note(f"{removed} rule(s) removed from '{ws.title}'")


# ---------------------------------------------------------------------------
# Write — charts
# ---------------------------------------------------------------------------

_SHEET_QUALIFIED_RE = re.compile(r"^(?:'((?:[^']|'')+)'|([^'!]+))!(.+)$", re.DOTALL)

_CHART_KINDS = ("column", "bar", "line", "pie", "doughnut", "scatter", "area")

_CHART_KIND_ALIASES = {
    "col": "column", "columns": "column", "vertical_bar": "column",
    "horizontal_bar": "bar", "bars": "bar", "lines": "line",
    "donut": "doughnut", "xy": "scatter",
}

_LEGEND_POSITIONS = {
    "r": "r", "l": "l", "t": "t", "b": "b", "tr": "tr",
    "right": "r", "left": "l", "top": "t", "bottom": "b", "top_right": "tr",
}


def _parse_ref_range(wb, ws, text, label: str):
    """(worksheet, (min_col, min_row, max_col, max_row)) for 'B2:D10',
    '$B$2:$D$10', 'Data!B2:D10' or "'My Sheet'!B2:D10". An unqualified
    range belongs to `ws`; a chart may read any sheet of the workbook."""
    from openpyxl.utils.cell import range_boundaries

    s = str(text).strip()
    m = _SHEET_QUALIFIED_RE.match(s)
    if m:
        title = (m.group(1) or m.group(2)).replace("''", "'")
        if title not in wb.sheetnames:
            raise ValueError(
                f"{label}: sheet '{title}' not found. Available: {wb.sheetnames}"
            )
        ws = wb[title]
        s = m.group(3)
    try:
        bounds = range_boundaries(s.replace("$", "").upper())
    except (ValueError, TypeError):
        raise ValueError(
            f"{label} '{text}' is not a valid A1 range (e.g. 'B2:D10' or 'Data!B2:D10')"
        ) from None
    return ws, bounds


def _parse_anchor(text) -> str:
    """A single cell for chart placement, normalised to 'K23'."""
    m = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d+)", str(text).strip())
    if not m:
        raise ValueError(f"anchor '{text}' is not a cell reference (e.g. 'K23')")
    return f"{m.group(1).upper()}{int(m.group(2))}"


def _chart_kind(text) -> tuple[str, bool]:
    """(kind, stacked) from a chart_type spelling; `column_stacked` and
    `stacked_bar` shapes fold into the stacked flag."""
    raw = str(text or "column").strip().lower().replace("-", "_").replace(" ", "_")
    stacked = raw.endswith("_stacked") or raw.startswith("stacked_")
    for suffix in ("_stacked", "_clustered", "_chart"):
        raw = raw.removesuffix(suffix)
    raw = raw.removeprefix("stacked_")
    kind = _CHART_KIND_ALIASES.get(raw, raw)
    if kind not in _CHART_KINDS:
        raise ValueError(
            f"chart_type '{text}' is not one of: {', '.join(_CHART_KINDS)} "
            "(bar = horizontal bars, column = vertical)"
        )
    return kind, stacked


def _series_length(bounds, label: str) -> int:
    """Point count of a one-column or one-row range; anything 2-D is an error
    (a block reads as one series per column only through data_range)."""
    c1, r1, c2, r2 = bounds
    if c1 != c2 and r1 != r2:
        raise ValueError(
            f"{label} must be a single column or a single row (e.g. 'B2:B7'), "
            f"not a block"
        )
    return (r2 - r1 + 1) if c1 == c2 else (c2 - c1 + 1)


def _free_row(ws) -> int:
    """First row of a literal chart-data block: below the used range with one
    blank row between; row 1 on an empty sheet (max_row reads 1 there)."""
    if not any(c.value is not None for c in ws._cells.values()):
        return 1
    return ws.max_row + 2


def _add_chart(job, ws, op: dict, ctx: OpCtx) -> None:
    """Build and place one chart. Data comes from `data_range` (first column =
    categories, header row = series names) or from `categories` + `series`,
    each a range string referenced in place or a literal list written to a
    data block. Every series is built through Series(): a one-row values
    range must stay ONE series (chart.add_data splits it per column)."""
    kind, stacked = _chart_kind(op.get("chart_type"))
    stacked = bool(op.get("stacked")) or stacked
    anchor = _parse_anchor(op.get("anchor") or "E1")
    warnings: list[str] = []

    # (values Reference, title text | None, title_from_data)
    if op.get("data_range") is not None:
        entries, cats = _entries_from_data_range(job.wb, ws, op)
    else:
        entries, cats = _entries_from_series(job, ws, op, ctx)

    if not entries:
        raise ValueError("no series — the chart would be empty")

    chart = _chart_with_series(kind, entries, cats)
    _style_chart(chart, op, kind, stacked, warnings)
    if kind in ("pie", "doughnut") and len(entries) > 1:
        warnings.append(f"{kind} charts show only the first series ({len(entries)} given)")

    ws.add_chart(chart, anchor)
    ctx.note(f"{kind} chart, {len(entries)} series, anchored at {anchor} on '{ws.title}'")
    for warning in warnings:
        ctx.warn(warning)


def _entries_from_data_range(wb, ws, op: dict) -> tuple[list[tuple], object]:
    """(series entries, categories) of a `data_range` block: categories in
    the first column, one series per further column."""
    from openpyxl.chart import Reference

    entries: list[tuple] = []
    ws_d, (c1, r1, c2, r2) = _parse_ref_range(wb, ws, op["data_range"], "data_range")
    tfd = bool(op.get("titles_from_data", True))
    if c2 - c1 + 1 < 2:
        raise ValueError(
            f"data_range '{op['data_range']}' has a single column — a chart "
            f"needs categories in the first column and at least one value "
            f"column (e.g. A1:B7), or pass categories + series"
        )
    if tfd and r2 - r1 + 1 < 2:
        raise ValueError(
            f"data_range '{op['data_range']}' is only a header row — add "
            f"data rows or pass titles_from_data: false"
        )
    first = r1 + 1 if tfd else r1
    cats = Reference(ws_d, min_col=c1, min_row=first, max_row=r2)
    for n, col in enumerate(range(c1 + 1, c2 + 1), start=1):
        ref = Reference(ws_d, min_col=col, min_row=r1 if tfd else first, max_row=r2)
        entries.append((ref, None if tfd else f"Series {n}", tfd))
    return entries, cats


def _series_specs(op: dict) -> list[dict]:
    """The `series` argument as checked `{name?, values}` objects."""
    series_in = op.get("series")
    if isinstance(series_in, (dict, str)):
        series_in = [series_in]
    if not isinstance(series_in, list) or not series_in:
        raise ValueError(
            "series must be a list of {name?, values} objects (or range strings)"
        )
    specs: list[dict] = []
    for i, item in enumerate(series_in):
        if isinstance(item, str):
            item = {"values": item}
        if not isinstance(item, dict):
            raise ValueError(
                f"series[{i}] must be an object {{name?, values}} or a range "
                f"string, got {type(item).__name__}"
            )
        item = {
            {"title": "name", "label": "name", "data": "values", "range": "values",
             "y": "values", "y_values": "values"}.get(k, k): v
            for k, v in item.items()
        }
        unknown = sorted(k for k in item if k not in ("name", "values"))
        if unknown:
            raise ValueError(
                f"series[{i}]: unknown key(s) {', '.join(repr(k) for k in unknown)}"
                f" — accepted: name, values"
            )
        if item.get("values") is None:
            raise ValueError(f"series[{i}] has no values")
        specs.append(item)
    return specs


def _entries_from_series(job, ws, op: dict, ctx: OpCtx) -> tuple[list[tuple], object]:
    """(series entries, categories) of `categories` + `series`: a range is
    referenced in place, a literal list written to a data block below the
    sheet's used range with write_cells' coercion."""
    from openpyxl.chart import Reference
    from openpyxl.utils import get_column_letter

    entries: list[tuple] = []
    cats = None
    n_points: int | None = None
    specs = _series_specs(op)

    categories = op.get("categories")
    cat_literal = isinstance(categories, list)
    literal_cols = [i for i, sp in enumerate(specs) if isinstance(sp["values"], list)]
    block_row = None
    block_col = 1
    tally = {"converted": 0, "warned": {}}

    wb = job.wb

    def _label(row: int, col: int, text) -> None:
        """A category or a series name: always text, never a formula."""
        cell = ws.cell(row=row, column=col, value=text)
        if isinstance(text, str):
            cell.data_type = "s"

    def _put(row: int, col: int, raw):
        """Literal data lands with the same coercion as write_cells; a
        formula among the values passes the same checks."""
        cell = ws.cell(row=row, column=col)
        if isinstance(raw, str) and raw.startswith("=") and len(raw) > 1:
            cell.value = _store_formula(job, ctx, ws, cell, raw)
            return
        val, kind_ = _coerce_cell_value(raw)
        kind_ = _set_coerced(cell, val, kind_, raw=raw)
        if kind_ is not None:
            _tally_coercion(tally, kind_, raw, f"{get_column_letter(col)}{row}")

    if cat_literal or literal_cols:
        block_row = _free_row(ws)

    if categories is None:
        pass
    elif cat_literal:
        n_points = len(categories)
        ws.cell(row=block_row, column=block_col, value="Category")
        for ri, cat in enumerate(categories):
            _label(block_row + 1 + ri, block_col, cat)
        cats = Reference(ws, min_col=block_col, min_row=block_row + 1,
                         max_row=block_row + n_points)
    else:
        ws_c, b = _parse_ref_range(wb, ws, categories, "categories")
        n_points = _series_length(b, "categories")
        cats = Reference(ws_c, min_col=b[0], min_row=b[1], max_col=b[2], max_row=b[3])

    for i, sp in enumerate(specs):
        name = sp.get("name")
        name = str(name) if name is not None else f"Series {i + 1}"
        vals = sp["values"]
        if isinstance(vals, list):
            if n_points is None:
                n_points = len(vals)
            if len(vals) != n_points:
                raise ValueError(
                    f"series '{name}' has {len(vals)} values but there are "
                    f"{n_points} categories"
                )
            col = block_col + (1 if cat_literal else 0) + literal_cols.index(i)
            _label(block_row, col, name)
            for ri, v in enumerate(vals):
                _put(block_row + 1 + ri, col, v)
            ref = Reference(ws, min_col=col, min_row=block_row + 1,
                            max_row=block_row + n_points)
        else:
            label = f"series '{name}' values"
            ws_v, b = _parse_ref_range(wb, ws, vals, label)
            length = _series_length(b, label)
            if n_points is None:
                n_points = length
            elif length != n_points:
                raise ValueError(
                    f"series '{name}' covers {length} cells but there are "
                    f"{n_points} categories"
                )
            ref = Reference(ws_v, min_col=b[0], min_row=b[1], max_col=b[2], max_row=b[3])
        entries.append((ref, name, False))

    if block_row is not None:
        n_block_cols = (1 if cat_literal else 0) + len(literal_cols)
        last_row = block_row + (n_points or 0)
        job.touch(ws, block_row, block_col, last_row, block_col + n_block_cols - 1)
        ctx.note(
            f"literal chart data written to "
            f"{get_column_letter(block_col)}{block_row}:"
            f"{get_column_letter(block_col + n_block_cols - 1)}{last_row} on '{ws.title}'"
        )
    _flush_coercion(tally, ctx, ws.title)
    return entries, cats


def _chart_with_series(kind: str, entries: list[tuple], cats):
    """The chart object of `kind` with its series and categories."""
    from openpyxl.chart import (
        AreaChart,
        BarChart,
        DoughnutChart,
        LineChart,
        PieChart,
        ScatterChart,
        Series,
    )

    chart = {
        "column": BarChart, "bar": BarChart, "line": LineChart, "pie": PieChart,
        "doughnut": DoughnutChart, "scatter": ScatterChart, "area": AreaChart,
    }[kind]()
    if kind in ("column", "bar"):
        chart.type = "col" if kind == "column" else "bar"
    if kind == "scatter":
        if cats is None:
            raise ValueError("scatter charts need categories (the X values)")
        for ref, title, tfd in entries:
            chart.series.append(Series(ref, xvalues=cats, title=title, title_from_data=tfd))
    else:
        for ref, title, tfd in entries:
            chart.series.append(Series(ref, title=title, title_from_data=tfd))
        if cats is not None:
            chart.set_categories(cats)
    return chart


_LEGEND_OFF = ("false", "none", "off", "hide")


def _style_chart(chart, op: dict, kind: str, stacked: bool, warnings: list[str]) -> None:
    """Title, axis titles, style, size, stacking, legend and data labels."""
    from openpyxl.chart.label import DataLabelList

    if op.get("title"):
        chart.title = str(op["title"])
    for key, axis, title in (("x_axis_title", "x_axis", op.get("x_axis_title")),
                             ("y_axis_title", "y_axis", op.get("y_axis_title"))):
        if title:
            if hasattr(chart, axis):
                getattr(chart, axis).title = str(title)
            else:
                warnings.append(f"{key} ignored — {kind} charts have no axes")
    if op.get("style") is not None:
        style = int(op["style"])
        if not 1 <= style <= 48:
            raise ValueError("style must be between 1 and 48")
        chart.style = style
    # A size of 0 means the default size, as it always did.
    width = to_cm(op.get("width") or 15, default="cm")
    height = to_cm(op.get("height") or 7.5, default="cm")
    if width <= 0 or height <= 0:
        raise ValueError("width and height are centimetres and must be positive")
    chart.width, chart.height = width, height
    if stacked:
        if kind in ("column", "bar"):
            chart.grouping = "stacked"
            chart.overlap = 100
        elif kind in ("line", "area"):
            chart.grouping = "stacked"
        else:
            warnings.append(f"stacked ignored — not meaningful for {kind} charts")
    legend = op.get("legend")
    if legend is False or (
        isinstance(legend, str) and legend.strip().lower() in _LEGEND_OFF
    ):
        chart.legend = None
    elif isinstance(legend, str):
        pos = _LEGEND_POSITIONS.get(legend.strip().lower())
        if pos is None:
            raise ValueError("legend must be r, l, t, b, tr or false")
        chart.legend.position = pos
    if op.get("show_percent") or op.get("show_values"):
        chart.dataLabels = DataLabelList()
        if op.get("show_percent"):
            chart.dataLabels.showPercent = True
        if op.get("show_values"):
            chart.dataLabels.showVal = True


def _quote_late_sheets(job) -> None:
    """After the batch: quote, in the formulas it wrote, the names of
    sheets created later in it. A formula naming a missing sheet is the
    broken_ref check's to report, on the saved file."""
    sheets = job.wb.sheetnames
    for _idx, _ot, ws, cell in job.formula_cells:
        if ws.title not in sheets or ws._cells.get((cell.row, cell.column)) is not cell:
            continue
        value = cell.value
        if isinstance(value, str) and value.startswith("="):
            quoted, _ = quote_sheet_names(value, sheets)
            if quoted != value:
                cell.value = quoted


# ---------------------------------------------------------------------------
# Write — main handler
# ---------------------------------------------------------------------------


KIND = "Workbook"
FMT = "xlsx"


async def handle_write_xlsx(args: dict) -> str:
    """Create or modify an Excel workbook with batched operations: the
    batch checked before any work, applied in a bounded worker (a full-DOM
    load of an existing workbook is the allocation profile that took the
    read path down), saved only when every operation worked (or with
    best_effort), previewed only when saved. A batch that leaves formulas
    in the workbook is recalculated in between, and the values are saved
    beside the formulas."""
    return await handle_write_call(args, CATALOG, _write_xlsx_core, kind=KIND, advice=_WRITE_OP_ADVICE,
                                   carry=("allow_loss",), render=computed.render, finish=_finish_xlsx)


@dataclass
class _XlsxJob:
    """What one write_xlsx call's operations share: the workbook, the call's
    work folder (equation pictures live there until the save), the readback
    boxes, and what the end-of-batch checks look at."""
    wb: Any
    work_dir: str = ""
    # Bounding box of value-writing ops per sheet, for the readback grid.
    touched: dict[str, list[int]] = field(default_factory=dict)
    # (op index, formula1) of list validations added by reference: checked
    # against the defined names after every op ran, so op order does not
    # matter.
    dv_name_refs: list[tuple[int, str]] = field(default_factory=list)
    formula_cells: list[tuple[int, str, Any, Any]] = field(default_factory=list)
    # Each loaded sheet's own parts as the original file names them, so an
    # operation that removes one can say so to the loss check.
    original_parts: dict[Any, SheetParts] = field(default_factory=dict)
    # What the save simplifies or drops (formats/xlsx/loss.py), said once
    # the batch ran.
    loss_warnings: list[str] = field(default_factory=list)

    def declare_comments_gone(self, ctx: OpCtx, ws) -> None:
        """Declare a sheet's original comments part (and the drawing that
        showed them) removed once the sheet holds no comment."""
        parts = self.original_parts.get(ws)
        if parts is None or parts.comments is None:
            return
        if any(getattr(cell, "comment", None) is not None for cell in ws._cells.values()):
            return
        ctx.removed(parts.comments)
        if parts.vml:
            ctx.removed(parts.vml)

    def sheet(self, name: str | None):
        """A worksheet by name, the first one when no name is given."""
        return _sheet_named(self.wb, name)

    def touch(self, ws, row1: int, col1: int, row2: int | None = None, col2: int | None = None):
        row2 = row2 if row2 is not None else row1
        col2 = col2 if col2 is not None else col1
        box = self.touched.setdefault(ws.title, [row1, col1, row2, col2])
        box[0] = min(box[0], row1)
        box[1] = min(box[1], col1)
        box[2] = max(box[2], row2)
        box[3] = max(box[3], col2)


# ---------------------------------------------------------------------------
# Write — sheet operations
# ---------------------------------------------------------------------------


def _op_create_sheet(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    pos = op.get("position")
    ws = job.wb.create_sheet(title=op["name"], index=pos)
    printing.same_paper(job.wb, ws)


def _record_shift(ctx: OpCtx, label: str, report: ShiftReport) -> None:
    ctx.note(label + report.summary())
    for note in report.notes:
        ctx.note(note)
    for warning in report.warnings():
        ctx.warn(warning)


def _op_delete_sheet(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    name = op["name"]
    if name not in job.wb.sheetnames:
        raise NoOp(f"sheet '{name}' does not exist")
    if len(job.wb.sheetnames) <= 1:
        raise OpError(Code.BAD_VALUE, "a workbook keeps at least one sheet", location=f"sheet '{name}'",
                      hint="Add another sheet first, or clear this one with clear_range.")
    parts = job.original_parts.get(job.wb[name])
    if parts is not None:
        ctx.removed(parts.sheet)
    report = _cascade_delete_sheet(job.wb, name)
    job.touched.pop(name, None)
    _record_shift(ctx, f"delete_sheet '{name}'", report)


def _op_rename_sheet(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    old = op["old_name"]
    new = op["new_name"]
    if old not in job.wb.sheetnames:
        _sheet_named(job.wb, old)
    report = _cascade_rename_sheet(job.wb, job.wb[old], new)
    if old in job.touched:
        job.touched[job.wb[new].title] = job.touched.pop(old)
    if report.counts or report.ref_errors or report.unparsed:
        _record_shift(ctx, f"rename_sheet '{old}' to '{new}'", report)


def _op_copy_sheet(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    source = _sheet_named(job.wb, op["source"])
    # A copy adds the sheet's formulas to the file again: each passes the
    # same check as a formula written by hand.
    for cell in source._cells.values():
        if cell.data_type == "f" and isinstance(cell.value, str):
            _refuse_unsafe(cell.value, f"{source.title}!{cell.coordinate}")
    copied = job.wb.copy_worksheet(source)
    copied.title = op["new_name"]


def _op_protect_sheet(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    ws.protection.sheet = True
    if op.get("password"):
        ws.protection.password = op["password"]
    # Permission flags (True = allowed)
    if op.get("allow_formatting_cells"):
        ws.protection.formatCells = False
    if op.get("allow_formatting_columns"):
        ws.protection.formatColumns = False
    if op.get("allow_formatting_rows"):
        ws.protection.formatRows = False
    if op.get("allow_insert_columns"):
        ws.protection.insertColumns = False
    if op.get("allow_insert_rows"):
        ws.protection.insertRows = False
    if op.get("allow_sort"):
        ws.protection.sort = False
    if op.get("allow_filter"):
        ws.protection.autoFilter = False


# ---------------------------------------------------------------------------
# Write — cell operations
# ---------------------------------------------------------------------------


def _op_write_cells(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.utils import get_column_letter

    ws = _get_sheet(job.wb, op)
    tally = {"converted": 0, "warned": {}}
    # Format 1: individual cells
    cells_list = op.get("cells")
    if cells_list and isinstance(cells_list, list):
        for c in cells_list:
            ref = c.get("cell", "")
            if ref:
                raw = c.get("value", "")
                as_text = c.get("type") == "text"
                if (isinstance(raw, str) and raw.startswith("=")
                        and not as_text and len(raw) > 1):
                    raw = _store_formula(job, ctx, ws, ws[ref], raw)
                val, kind = _coerce_cell_value(raw, c.get("type"))
                kind = _set_coerced(
                    ws[ref], val, kind, c.get("format"),
                    raw=c.get("value"),
                )
                if as_text and isinstance(val, str):
                    # openpyxl makes any string that starts with '=' a
                    # formula; type text says it is not one.
                    ws[ref].data_type = "s"
                if kind is not None:
                    _tally_coercion(tally, kind, c.get("value"), ref)
                try:
                    col, row = _parse_cell_ref(ref)
                    job.touch(ws, row, col)
                except ValueError:
                    pass
    else:
        # Format 2: 2D array (no per-cell type here; strict ISO only)
        start = op.get("start_cell", "A1")
        data = op.get("data", [])
        start_col, start_row = _parse_cell_ref(start)
        n_cols = 0
        for ri, row in enumerate(data):
            n_cols = max(n_cols, len(row))
            for ci, val in enumerate(row):
                if isinstance(val, str) and val.startswith("=") and len(val) > 1:
                    val = _store_formula(
                        job, ctx, ws, ws.cell(row=start_row + ri, column=start_col + ci), val,
                    )
                cval, kind = _coerce_cell_value(val)
                if kind is None:
                    ws.cell(
                        row=start_row + ri,
                        column=start_col + ci,
                        value=cval,
                    )
                else:
                    kind = _set_coerced(
                        ws.cell(row=start_row + ri, column=start_col + ci),
                        cval, kind, raw=val,
                    )
                    _tally_coercion(
                        tally, kind, val,
                        f"{get_column_letter(start_col + ci)}"
                        f"{start_row + ri}",
                    )
        if data and n_cols:
            job.touch(ws, start_row, start_col,
                   start_row + len(data) - 1, start_col + n_cols - 1)
    _flush_coercion(tally, ctx, ws.title)


def _op_set_formula(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    formula = op["formula"]
    if not formula.startswith("="):
        formula = "=" + formula
    err = _validate_formula(formula)
    if err:
        raise OpError(Code.BAD_VALUE, err, location=f"{ws.title}!{op['cell']}")
    cell = ws[op["cell"]]
    cell.value = _store_formula(job, ctx, ws, cell, formula)
    try:
        col, row = _parse_cell_ref(op["cell"])
        job.touch(ws, row, col)
    except ValueError:
        pass


def _op_merge_cells(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    ws.merge_cells(op["range"])


def _op_unmerge_cells(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    ws.unmerge_cells(op["range"])


def _op_clear_range(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.styles.cell_style import StyleArray
    from openpyxl.utils import range_boundaries

    ws = _get_sheet(job.wb, op)
    min_c, min_r, max_c, max_r = range_boundaries(op["range"])
    clear_styles = op.get("clear_styles", False)
    for row in ws.iter_rows(
        min_row=min_r, max_row=max_r,
        min_col=min_c, max_col=max_c,
    ):
        for cell in row:
            cell.value = None
            if clear_styles:
                # The workbook's own default style: a bare Font() has no
                # name or size, and LibreOffice draws it in a serif face.
                cell._style = StyleArray()


_FORMULAS_ADJUST = "adjust"
_FORMULAS_VERBATIM = "verbatim"


def _op_copy_range(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.utils import get_column_letter, range_boundaries

    ws_src = _get_sheet(job.wb, op)
    ws_dst = _sheet_named(job.wb, op["target_sheet"]) if op.get("target_sheet") else ws_src
    mode = op.get("formulas", _FORMULAS_ADJUST)
    if mode not in (_FORMULAS_ADJUST, _FORMULAS_VERBATIM):
        raise OpError(Code.BAD_VALUE, f"formulas '{mode}' is not '{_FORMULAS_ADJUST}' or '{_FORMULAS_VERBATIM}'")
    src_min_col, src_min_row, src_max_col, src_max_row = range_boundaries(op["source_range"])
    tgt_col, tgt_row = _parse_cell_ref(op["target_start"])
    dcol, drow = tgt_col - src_min_col, tgt_row - src_min_row
    # Read the whole source before writing: the target may overlap it.
    snapshot = []
    for row_off in range(src_max_row - src_min_row + 1):
        for col_off in range(src_max_col - src_min_col + 1):
            src_cell = ws_src.cell(row=src_min_row + row_off, column=src_min_col + col_off)
            style = None
            if src_cell.has_style:
                style = (copy(src_cell.font), copy(src_cell.border), copy(src_cell.fill),
                         src_cell.number_format, copy(src_cell.alignment), copy(src_cell.protection))
            snapshot.append((row_off, col_off, src_cell.value, src_cell.data_type, style))
    kept: list[str] = []
    for row_off, col_off, value, data_type, style in snapshot:
        dst_cell = ws_dst.cell(row=tgt_row + row_off, column=tgt_col + col_off)
        if data_type == "f" and isinstance(value, str):
            if mode == _FORMULAS_ADJUST:
                # Excel's copy rule: relative references move with the cell,
                # $-anchored ones stay, one pushed off the sheet is #REF!.
                moved = rewrite_refs(value, lambda ref: copy_ref(ref, dcol, drow))
                if moved is None:
                    kept.append(dst_cell.coordinate)
                else:
                    value = moved
            value = _store_formula(job, ctx, ws_dst, dst_cell, value)
        dst_cell.value = value
        if style is not None:
            (dst_cell.font, dst_cell.border, dst_cell.fill, dst_cell.number_format,
             dst_cell.alignment, dst_cell.protection) = style
    if kept:
        ctx.warn(f"{len(kept)} formula(s) could not be read, so they were copied as they were "
                 f"(e.g. {kept[0]}); check their references")
    job.touch(ws_dst, tgt_row, tgt_col,
              tgt_row + (src_max_row - src_min_row),
              tgt_col + (src_max_col - src_min_col))
    ctx.note(f"copied to {get_column_letter(tgt_col)}{tgt_row} on '{ws_dst.title}'"
             + ("" if mode == _FORMULAS_VERBATIM else "; relative references in formulas moved with the cells"))


# ---------------------------------------------------------------------------
# Write — row / column operations
# ---------------------------------------------------------------------------


def _line_index(value, axis: Axis, key: str) -> int:
    """A 1-based row number, or a column letter or index."""
    from openpyxl.utils import column_index_from_string

    if axis is Axis.COL and isinstance(value, str) and not value.strip().isdigit():
        try:
            return column_index_from_string(value.strip().upper())
        except ValueError:
            raise ValueError(f"{key} '{value}' is not a column letter or number") from None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} '{value}' is not a number") from None


def _structural(job: _XlsxJob, ctx: OpCtx, sheet: str | None, axis: Axis, kind: EditKind,
                at_value, count: int, to_value=None) -> None:
    """insert/delete/move rows or columns, every reference following."""
    ws = _sheet_named(job.wb, sheet)
    key = "row" if axis is Axis.ROW else "column"
    name = f"{kind.value}_{'rows' if axis is Axis.ROW else 'columns'}"
    at = _line_index(at_value, axis, key)
    to = _line_index(to_value, axis, "to") if kind is EditKind.MOVE else 0
    edit = Edit(ws.title, axis, kind, at, count, to)
    try:
        check_edit(job.wb, ws, edit)
    except ValueError as exc:
        raise OpError(Code.BAD_VALUE, str(exc), location=f"sheet '{ws.title}'") from None
    if kind is EditKind.MOVE and to in (at, at + count):
        raise NoOp(f"the {key}s are already there")
    report = apply_edit(job.wb, ws, edit)
    if kind is EditKind.DELETE:
        job.declare_comments_gone(ctx, ws)
    box = job.touched.get(ws.title)
    if box is not None:
        moved = shift_box(edit, box)
        if moved is None:
            job.touched.pop(ws.title)
        else:
            job.touched[ws.title] = moved
    where = str(at) if axis is Axis.ROW else _column_label(at)
    sign = "+" if kind is EditKind.INSERT else "-"
    if kind is EditKind.MOVE:
        dest = str(to) if axis is Axis.ROW else _column_label(to)
        label = f"{name} {where} (x{count}) before {key} {dest} on '{ws.title}'"
    elif axis is Axis.ROW:
        label = f"{name} at row {at} ({sign}{count}) on '{ws.title}'"
    else:
        label = f"{name} at {where} ({sign}{count}) on '{ws.title}'"
    _record_shift(ctx, label, report)


def _column_label(idx: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(idx) if 1 <= idx <= 16384 else str(idx)


def _op_insert_rows(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    _structural(job, ctx, op.get("sheet"), Axis.ROW, EditKind.INSERT, op["row"], op.get("count", 1))


def _op_insert_columns(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    _structural(job, ctx, op.get("sheet"), Axis.COL, EditKind.INSERT, op["column"], op.get("count", 1))


def _op_delete_rows(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    _structural(job, ctx, op.get("sheet"), Axis.ROW, EditKind.DELETE, op["row"], op.get("count", 1))


def _op_delete_columns(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    _structural(job, ctx, op.get("sheet"), Axis.COL, EditKind.DELETE, op["column"], op.get("count", 1))


def _op_move_rows(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    _structural(job, ctx, op.get("sheet"), Axis.ROW, EditKind.MOVE, op["row"], op.get("count", 1),
                op["to"])


def _op_move_columns(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    _structural(job, ctx, op.get("sheet"), Axis.COL, EditKind.MOVE, op["column"], op.get("count", 1),
                op["to"])


def _op_set_column_width(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    column = _column_label(_line_index(op["column"], Axis.COL, "column"))
    ws.column_dimensions[column].width = float(op["width"])


def _op_set_row_height(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    ws.row_dimensions[op["row"]].height = to_pt(op["height"], default="pt")


def _column_list(value) -> list[str] | None:
    """Column letters from a list, or from text such as 'A', 'A, C' or
    'A:D'."""
    from openpyxl.utils import column_index_from_string, get_column_letter

    if value is None or isinstance(value, list):
        return value
    letters: list[str] = []
    for part in re.split(r"[,\s]+", value.strip()):
        if not part:
            continue
        if ":" in part:
            first, last = (column_index_from_string(x.strip().upper()) for x in part.split(":", 1))
            letters.extend(get_column_letter(i) for i in range(min(first, last), max(first, last) + 1))
        else:
            letters.append(part.upper())
    return letters


def _op_auto_column_width(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.utils import column_index_from_string, get_column_letter

    ws = _get_sheet(job.wb, op)
    columns = _column_list(op.get("columns"))  # column letters, or None for all
    if columns:
        for col_letter in columns:
            col_idx = column_index_from_string(col_letter.upper())
            max_len = 0
            for row in ws.iter_rows(min_col=col_idx, max_col=col_idx):
                for cell in row:
                    if cell.value is not None:
                        max_len = max(max_len, len(str(cell.value)))
            ws.column_dimensions[col_letter.upper()].width = max(max_len + 2, 8)
    else:
        for col_cells in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col_cells[0].column)
            for cell in col_cells:
                if cell.value is not None:
                    max_len = max(max_len, len(str(cell.value)))
            if max_len > 0:
                ws.column_dimensions[col_letter].width = max(max_len + 2, 8)


def _op_freeze_panes(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    ws.freeze_panes = op["cell"]


# ---------------------------------------------------------------------------
# Write — formatting
# ---------------------------------------------------------------------------


_BORDER_SIDES = ("left", "right", "top", "bottom")
# The border styles Excel draws, by their lower-case names.
_BORDER_STYLES = {name.lower(): name for name in (
    "thin", "medium", "thick", "dashed", "dotted", "double", "hair", "dashDot", "dashDotDot",
    "mediumDashed", "mediumDashDot", "mediumDashDotDot", "slantDashDot")}
_THIN = "thin"
_UNLOCKED = "unlocked"
_flag = values.to_flag


def _border_word(word: str) -> str | None:
    """The style a border given as one word draws on every side: a style
    name, the thin default for an on word or any other word (as a truthy
    value always drew), None for an off word."""
    folded = word.strip().lower()
    if folded in _BORDER_STYLES:
        return _BORDER_STYLES[folded]
    return _THIN if values.to_flag(word) else None


def _merged(current, changes: dict):
    """A copy of a cell's Font/Alignment/Protection/Border with only
    `changes` applied: set_style edits what it is given and keeps the rest
    (a bold on a styled cell must not reset its name, size and colour)."""
    new = copy(current)
    for key, value in changes.items():
        if not hasattr(new, key):
            raise ValueError(f"'{key}' is not a {type(current).__name__.lower()} property")
        setattr(new, key, value)
    return new


def _border_changes(border_val) -> dict:
    """The sides one set_style border value sets. No side named true draws
    every side not named false (the default box); once a side is named
    true, sides left unnamed keep what the cell has."""
    from openpyxl.styles import Side

    if isinstance(border_val, str):
        style = _border_word(border_val)
        side = Side(style=style) if style else Side()
        return {name: side for name in _BORDER_SIDES}
    if not isinstance(border_val, dict):
        side = Side(style=_THIN) if _flag(border_val) else Side()
        return {name: side for name in _BORDER_SIDES}
    bstyle = border_val.get("style", "thin")
    side = Side(style=bstyle, color=_xl_color(border_val.get("color", "000000")))
    named = {name: _flag(border_val[name]) for name in _BORDER_SIDES if name in border_val}
    any_true = any(named.values())
    changes = {}
    for name in _BORDER_SIDES:
        if name in named:
            changes[name] = side if named[name] else Side()
        elif not any_true:
            changes[name] = side
    return changes


def _op_set_style(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.styles import PatternFill
    from openpyxl.styles.cell_style import StyleArray
    from openpyxl.utils import range_boundaries

    ws = _get_sheet(job.wb, op)
    min_c, min_r, max_c, max_r = range_boundaries(op["range"])

    # Font
    font_kw: dict = {}
    if op.get("bold") is not None:
        font_kw["bold"] = op["bold"]
    if op.get("italic") is not None:
        font_kw["italic"] = op["italic"]
    if op.get("underline") is not None:
        val = op["underline"]
        if val is True or (isinstance(val, str) and val.strip().lower() in values.ON_WORDS):
            font_kw["underline"] = "single"
        elif not _flag(val):
            font_kw["underline"] = None
        else:
            font_kw["underline"] = str(val)
    if op.get("strikethrough") is not None:
        font_kw["strike"] = _flag(op["strikethrough"])
    if op.get("font_size"):
        font_kw["size"] = float(op["font_size"])
    fc = op.get("font_color")
    if fc:
        font_kw["color"] = _xl_color(fc)
    if op.get("font_name"):
        font_kw["name"] = op["font_name"]
        # A theme-scheme font takes its face from the theme, so a named face
        # would be ignored while the scheme stays.
        font_kw["scheme"] = None

    # Fill
    fill = None
    fill_val = op.get("fill_color")
    if fill_val:
        fc_str = _xl_color(fill_val)
        fill = PatternFill(start_color=fc_str, end_color=fc_str, fill_type="solid")

    # Alignment
    align_kw: dict = {}
    align_val = op.get("alignment")
    wrap = op.get("wrap_text")
    rotation = op.get("text_rotation")
    if isinstance(align_val, dict):
        align_kw = {
            "horizontal": align_val.get("horizontal"),
            "vertical": align_val.get("vertical"),
            "wrap_text": align_val.get("wrap_text", wrap),
            "text_rotation": align_val.get("text_rotation", rotation),
            "indent": align_val.get("indent"),
        }
        align_kw = {k: v for k, v in align_kw.items() if v is not None}
    else:
        if align_val:
            align_kw["horizontal"] = str(align_val)
        if wrap is not None:
            align_kw["wrap_text"] = wrap
        if rotation is not None:
            align_kw["text_rotation"] = rotation

    # Border
    border_kw = _border_changes(op["border"]) if op.get("border") is not None else {}
    border = op.get("border")
    if isinstance(border, str) and border.strip().lower() not in (
            *_BORDER_STYLES, *values.ON_WORDS, *values.OFF_WORDS):
        ctx.note(f"border '{border}' is not a style name, so a thin box was drawn")

    # Number format — named preset or raw Excel code
    nf = op.get("number_format")
    if nf:
        nf = _resolve_number_format(nf)

    # Protection
    prot_kw: dict = {}
    if op.get("protection") is not None:
        pv = op["protection"]
        if isinstance(pv, dict):
            prot_kw = {k: pv[k] for k in ("locked", "hidden") if k in pv}
        elif isinstance(pv, str) and pv.strip().lower() == _UNLOCKED:
            prot_kw = {"locked": False}
        else:
            prot_kw = {"locked": _flag(pv)}

    # One merged object per distinct existing style, keyed by the style
    # indexes (reading cell.style_id would register a style as a side
    # effect), so a large range costs a copy per distinct look.
    fonts: dict[int, Any] = {}
    aligns: dict[int, Any] = {}
    borders: dict[int, Any] = {}
    prots: dict[int, Any] = {}
    for row in ws.iter_rows(
        min_row=min_r, max_row=max_r,
        min_col=min_c, max_col=max_c,
    ):
        for cell in row:
            style = cell._style
            if style is None:
                # An unstyled cell's array is made on first style access.
                style = cell._style = StyleArray()
            if font_kw:
                if style.fontId not in fonts:
                    fonts[style.fontId] = _merged(cell.font, font_kw)
                cell.font = fonts[style.fontId]
            if fill:
                cell.fill = fill
            if align_kw:
                if style.alignmentId not in aligns:
                    aligns[style.alignmentId] = _merged(cell.alignment, align_kw)
                cell.alignment = aligns[style.alignmentId]
            if border_kw:
                if style.borderId not in borders:
                    borders[style.borderId] = _merged(cell.border, border_kw)
                cell.border = borders[style.borderId]
            if nf:
                cell.number_format = nf
            if prot_kw:
                if style.protectionId not in prots:
                    prots[style.protectionId] = _merged(cell.protection, prot_kw)
                cell.protection = prots[style.protectionId]


# ---------------------------------------------------------------------------
# Write — features — tables, validation, conditional formatting
# ---------------------------------------------------------------------------


def _op_create_table(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.worksheet.table import Table, TableStyleInfo

    ws = _get_sheet(job.wb, op)
    table_range = op["range"]
    table_name = op.get("name") or f"Table_{uuid.uuid4().hex[:8]}"
    style = op.get("style", "TableStyleMedium9")
    tab = Table(displayName=table_name, ref=table_range)
    tab.tableStyleInfo = TableStyleInfo(
        name=style,
        showFirstColumn=op.get("show_first_column", False),
        showLastColumn=op.get("show_last_column", False),
        showRowStripes=op.get("show_row_stripes", True),
        showColumnStripes=op.get("show_column_stripes", False),
    )
    ws.add_table(tab)


def _table_headers(wb) -> list[str]:
    """Each new table's header row made what Excel requires (text, filled,
    unique), with a line for each table changed: Excel repairs a table whose
    headers are numbers, blanks or repeats when it opens the file."""
    from openpyxl.utils import range_boundaries

    warnings: list[str] = []
    for ws in wb.worksheets:
        for table in ws.tables.values():
            if table.tableColumns:
                continue
            c1, r1, c2, _r2 = range_boundaries(table.ref)
            seen: set[str] = set()
            changed: list[str] = []
            for n, col in enumerate(range(c1, c2 + 1), start=1):
                cell = ws.cell(row=r1, column=col)
                value = cell.value
                text = (f"Column{n}" if value is None or str(value).strip() == ""
                        else str(int(value)) if isinstance(value, float) and value.is_integer() else str(value))
                base, k = text, 2
                while text.casefold() in seen:
                    text, k = f"{base}{k}", k + 1
                seen.add(text.casefold())
                if text != value:
                    cell.value = text
                    changed.append(f"{cell.coordinate} '{text}'")
            if changed:
                warnings.append(f"table {table.displayName}: a table needs a different text header in every column, "
                                f"so these headers were set: {', '.join(changed[:6])}"
                                + (f" and {len(changed) - 6} more" if len(changed) > 6 else ""))
    return warnings


_LIST_VALIDATION = "list"


def _op_add_data_validation(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.worksheet.datavalidation import DataValidation

    ws = _get_sheet(job.wb, op)
    dv_range = op["range"]
    if op.get("validation_type") is None:
        if op.get("values") is None:
            raise ValueError(
                "validation_type is required (list | whole | decimal | "
                "date | time | textLength | custom); values alone "
                "implies list"
            )
        dv_type = "list"
    else:
        dv_type = _dv_type(op["validation_type"])
    dv = DataValidation(type=dv_type)

    if dv_type == _LIST_VALIDATION:
        values = op.get("values")
        if values is None or values == [] or values == "":
            raise ValueError(
                "list validation needs values: an array of items or "
                "a reference string ('=Names', \"'Data'!$B$2:$B$50\")"
            )
        if (
            isinstance(values, list)
            and len(values) == 1
            and (
                str(values[0]).startswith("=")
                or _SHEET_RANGE_RE.fullmatch(str(values[0]))
            )
        ):
            # The incident shape: values: ["=SupplierList"] was
            # quote-wrapped into a literal one-item text list.
            values = str(values[0])
        if isinstance(values, list):
            items = [str(v).replace('"', '""') for v in values]
            joined = ",".join(items)
            dv.formula1 = '"' + joined + '"'
            if len(joined) > 255:
                ctx.warn(
                    f"literal list is {len(joined)} chars — Excel "
                    f"caps in-formula lists at 255; put the items "
                    f"in cells and reference the range instead"
                )
            if any("," in str(v) for v in values):
                ctx.warn(
                    "literal items containing commas are split "
                    "into separate entries by Excel; put the "
                    "items in cells and reference the range "
                    "instead"
                )
        else:
            # Named range or sheet-qualified range reference
            ref = _stored_bare(job, ctx, _strip_leading_eq(str(values)), f"validation on {dv_range}")
            dv.formula1 = ref
            job.dv_name_refs.append((ctx.index, ref))
    elif dv_type == "custom":
        if not op.get("formula"):
            raise ValueError("custom validation needs formula")
        dv.formula1 = _stored_bare(job, ctx, _strip_leading_eq(str(op["formula"])),
                                   f"validation on {dv_range}")
    else:
        # whole / decimal / date / time / textLength: a rule with
        # no bound accepts nothing and says nothing — refuse it.
        default_op = "lessThanOrEqual" if dv_type == "textLength" else "between"
        operator = _dv_operator(op.get("operator") or default_op)
        dv.operator = operator
        if operator in ("between", "notBetween"):
            if op.get("min") is None or op.get("max") is None:
                raise ValueError(
                    f"{dv_type} validation with {operator} needs min and max"
                )
            dv.formula1 = _dv_bound(op["min"], dv_type)
            dv.formula2 = _dv_bound(op["max"], dv_type)
        else:
            bound = next(
                (op[k] for k in ("value", "formula", "max", "min")
                 if op.get(k) is not None),
                None,
            )
            if bound is None:
                raise ValueError(
                    f"{dv_type} validation with {operator} needs value"
                )
            dv.formula1 = _dv_bound(bound, dv_type)

    if op.get("allow_blank") is not None:
        dv.allow_blank = op["allow_blank"]
    if op.get("show_error"):
        dv.showErrorMessage = True
        dv.error = op.get("error_message", "")
        dv.errorTitle = op.get("error_title", "Invalid input")
        dv.errorStyle = op.get("error_style", "stop")
    if op.get("show_prompt"):
        dv.showInputMessage = True
        dv.prompt = op.get("prompt_message", "")
        dv.promptTitle = op.get("prompt_title", "")

    dv.add(dv_range)
    # Re-running a corrected op must replace the rule on the same
    # cells, not stack a second one — stacked same-sqref rules of
    # any type put Excel into repair.
    sq = str(dv.sqref)
    ws.data_validations.dataValidation = [
        r for r in ws.data_validations.dataValidation
        if str(r.sqref) != sq
    ]
    for existing in ws.data_validations.dataValidation:
        if _sqref_intersects(dv.sqref, existing.sqref):
            ctx.warn(
                f"the new rule overlaps the validation at "
                f"{existing.sqref} — Excel allows one rule per "
                f"cell; use remove_data_validation first"
            )
    ws.add_data_validation(dv)


def _op_remove_data_validation(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.worksheet.cell_range import MultiCellRange

    ws = _get_sheet(job.wb, op)
    rules = ws.data_validations.dataValidation
    if op.get("range"):
        target = MultiCellRange(op["range"])
        keep = [
            r for r in rules
            if not _sqref_intersects(r.sqref, target)
        ]
        removed = len(rules) - len(keep)
        if removed == 0:
            raise NoOp(f"no validation rules intersect {op['range']}")
        ws.data_validations.dataValidation = keep
    elif op.get("all"):
        removed = len(rules)
        if removed == 0:
            raise NoOp(f"'{ws.title}' has no validation rules")
        ws.data_validations.dataValidation = []
    else:
        raise NoOp("all: false removes nothing")
    ctx.note(f"{removed} rule(s) removed from '{ws.title}'")


def _op_conditional_format(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    _conditional_format(ws, op, ctx)


def _op_remove_conditional_format(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    _remove_conditional_format(ws, op, ctx)


def _op_auto_filter(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    ws.auto_filter.ref = op["range"]


def _op_define_name(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.workbook.defined_name import DefinedName

    name = op["name"]
    cell_range = _stored_bare(job, ctx, _strip_leading_eq(str(op["range"])), f"name {name}")
    if "!" in cell_range:
        # Already sheet-qualified — prefixing again produces an
        # invalid double-qualified reference.
        ref = cell_range
    else:
        sheet_name = op.get("sheet") or job.wb.sheetnames[0]
        ref = f"'{sheet_name}'!{cell_range}"
    defn = DefinedName(name, attr_text=ref)
    job.wb.defined_names.add(defn)


# ---------------------------------------------------------------------------
# Write — charts
# ---------------------------------------------------------------------------


def _op_add_chart(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    ws = _get_sheet(job.wb, op)
    _add_chart(job, ws, op, ctx)


# ---------------------------------------------------------------------------
# Write — images
# ---------------------------------------------------------------------------


def _op_add_image(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    from openpyxl.drawing.image import Image as XlImage

    ws = _get_sheet(job.wb, op)
    img = XlImage(op["image_path"])
    # A size of 0 means the picture's own size, as it always did.
    if op.get("width"):
        img.width = max(round(to_px(op["width"], default="px")), 1)
    if op.get("height"):
        img.height = max(round(to_px(op["height"], default="px")), 1)
    ws.add_image(img, op.get("cell", "A1"))


def _op_add_equation(job: _XlsxJob, ctx: OpCtx, op: dict) -> None:
    # LaTeX equation as a floating PNG anchored at a cell, plus
    # the source LaTeX in a cell comment (xlsx has no in-cell
    # math). The comment is the machine-readable source of truth;
    # picture and comment both survive later edits of the file.
    import tempfile

    from openpyxl.comments import Comment
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.utils import get_column_letter

    ws = _get_sheet(job.wb, op)
    latex = op.get("latex") or ""
    cell = op.get("cell", "A1")
    height_px = max(round(to_px(op.get("height", 40), default="px")), 1)
    col, row = _parse_cell_ref(cell)
    # A covered merged cell can't hold the marker comment and
    # would detach it from the picture — anchor both at the
    # merge anchor.
    row, col = _merged_anchor_map(ws).get((row, col), (row, col))
    cell = f"{get_column_letter(col)}{row}"
    # One equation per cell: an existing marked equation is
    # replaced. With several images at the anchor there is no
    # safe way to tell the equation from e.g. a logo — refuse.
    existing = [i for i in ws._images if _anchor_cell(i) == (col, row)]
    prior = ws[cell].comment
    if prior is not None and _equation_latex(prior.text or "") is not None and existing:
        if len(existing) > 1:
            raise ValueError(
                f"{len(existing)} images are anchored at {cell}; "
                "cannot replace the equation safely — move or "
                "remove the other image(s) first"
            )
        ws._images.remove(existing[0])
    # Rendered at 4x the display height; scale back for placement. The
    # file lives in the call's work folder: openpyxl reads it at save.
    fd, png = tempfile.mkstemp(suffix=".png", prefix="equation-", dir=job.work_dir or None)
    os.close(fd)
    w_px, h_px = latex_to_png(latex, png, display=True, height_px=height_px)
    # Comment before picture: if either step fails, nothing is
    # left half-placed (an unmarked picture could never be
    # replaced later).
    ws[cell].comment = Comment(f"LaTeX: {latex}", "file-tools")
    img = XlImage(png)
    img.width = max(w_px // 4, 1)
    img.height = max(h_px // 4, 1)
    ws.add_image(img, cell)
    job.touch(ws, row, col)


# ---------------------------------------------------------------------------
# Write — the core
# ---------------------------------------------------------------------------


def _finalize(job: _XlsxJob, outcome) -> list[str]:
    """The batch-level checks: list validations naming no defined name.
    Formulas and charts naming a missing sheet are the broken_ref check's."""
    wb = job.wb
    warnings: list[str] = []
    # A list validation referencing a defined name that exists nowhere in
    # the workbook renders as a dead dropdown with no error anywhere. Excel
    # names are case-insensitive: compare casefolded.
    if job.dv_name_refs:
        known = {str(n).casefold() for n in wb.defined_names}
        for ws_named in wb.worksheets:
            known |= {str(n).casefold() for n in ws_named.defined_names}
        for op_idx, ref in job.dv_name_refs:
            if "(" in ref or not _BARE_NAME_RE.fullmatch(ref) or _A1_REF_RE.fullmatch(ref):
                continue
            if ref.casefold() not in known:
                warnings.append(
                    f"op #{op_idx} add_data_validation: the list references '{ref}' but no "
                    f"defined name in the workbook matches it, so the dropdown will be empty"
                )
    _quote_late_sheets(job)
    return warnings + _table_headers(wb) + job.loss_warnings


def _readback(job: _XlsxJob, marks: list | None = None) -> str:
    """A coordinate-labeled grid of each touched range, so the agent sees at
    once whether values landed in the intended cells. Values come from the
    in-memory workbook: a data-only re-read of a file openpyxl just wrote
    would show every formula blank. With ``marks``, each formula cell is
    followed by a marker worker B replaces with its computed value."""
    from openpyxl.utils import get_column_letter

    wb = job.wb
    blocks: list[str] = []
    readback_rows_cap, readback_cols_cap = 15, 10
    for sheet_name, (r1, c1, r2, c2) in job.touched.items():
        if sheet_name not in wb.sheetnames:
            continue  # sheet renamed/deleted after the write
        ws = wb[sheet_name]
        n_rows = min(r2 - r1 + 1, readback_rows_cap)
        n_cols = min(c2 - c1 + 1, readback_cols_cap)

        def cell_text(r: int, c: int, ws=ws) -> str:
            cell_obj = ws.cell(row=r, column=c)
            if cell_obj.value is None:
                # An equation cell has no value — show a placeholder so the
                # model doesn't read a successful add_equation as a failed
                # write and retry it.
                try:
                    cm = cell_obj.comment
                except Exception:  # noqa: BLE001  # openpyxl on a user's workbook raises anything from its parser
                    cm = None
                if cm is not None and _equation_latex(cm.text or "") is not None:
                    return "[equation]"
            if marks is not None and cell_obj.data_type == "f":
                code = cell_obj.number_format
                marks.append((ws.title, cell_obj.coordinate, None if code == "General" else code))
                return _escape_cell(cell_obj.value) + f"\x00{len(marks) - 1}\x00"
            return computed.constant_text(cell_obj.value, cell_obj.number_format)

        span = f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"
        header = f"Readback — verify placement — {sheet_name}!{span}"
        if n_rows < r2 - r1 + 1 or n_cols < c2 - c1 + 1:
            header += f" (showing first {n_rows} row(s) × {n_cols} column(s))"
        blocks.append(header + ":\n" + "\n".join(_grid_lines(cell_text, r1, c1, n_rows, n_cols)))
    return "\n\n".join(blocks)


def _unsafe_refusal(call: WriteCall, found: list[tuple[str, list[str]]]) -> str:
    """The answer when the backstop finds a formula no operation's own
    check refused: the whole batch is refused, best_effort or not."""
    from shared import _to_agents_relative

    state = "is unchanged" if call.existed else "was not created"
    return "\n".join([
        f"Nothing saved: {_to_agents_relative(call.path)} {state}. The batch would store a "
        "formula that reaches the network or runs a program (listed below), so none of it was "
        "saved. Write the values themselves, or use functions that read the workbook only.",
        "Failed:",
        *guard.refusal_lines(found),
    ])


def _write_xlsx_core(call: WriteCall) -> Staged | WriteReport:
    """Worker A: load (or start blank), apply the batch, then commit at
    once when the workbook holds no formula, or stage the save for the
    recalculation and worker B."""
    from openpyxl import Workbook, load_workbook

    path = call.path
    macro_parts: MacroParts | None = None
    losses: list[loss.Loss] = loss.scan(path) if call.edit_existing else []
    allow_loss = values.to_flag(call.extra.get("allow_loss", False))
    if loss.refused(losses) and not allow_loss:
        from shared import _to_agents_relative

        return WriteReport(loss.refusal(_to_agents_relative(path), call.existed, loss.refused(losses)), False, [])
    baseline = collect_baseline(FMT, path=path, create_new=call.create_new,
                                check_modules=call.check_modules)
    if call.edit_existing:
        # Charts, images and comments all survive the load+save round trip
        # on the pinned openpyxl (charts are re-read from xl/charts and
        # rewritten with their anchors and series).
        check_readable(path)
        keep_vba = keeps_vba(path)
        _preflight_density(
            path, "edit",
            extra_bytes=Path(path).stat().st_size if keep_vba else 0,
        )
        wb = load_workbook(path, keep_vba=keep_vba)
        if keep_vba:
            macro_parts = read_macro_parts(path)
        by_title = sheet_parts(path)
    else:
        wb = Workbook()
    wb.template = is_template(path)
    job = _XlsxJob(wb, work_dir=call.work_dir, loss_warnings=loss.warnings(losses, allowed=allow_loss))
    if call.edit_existing:
        job.original_parts = {ws: by_title[ws.title] for ws in wb.worksheets if ws.title in by_title}
    before = guard.snapshot(wb)
    cells_before = computed.cell_values(wb) if call.edit_existing else None
    names_before = computed.names_of(wb) if cells_before is not None else {}
    try:
        outcome = call.run(CATALOG, job, finalize=_finalize)
        outcome.removed.extend(loss.declared(losses, allowed=allow_loss))
        if not call.edit_existing:
            printing.a4_by_default(wb)
        if outcome.should_commit:
            found = guard.new_unsafe(wb, before)
            if found:
                discard_deferred(outcome.deferred)
                return WriteReport(_unsafe_refusal(call, found), False, [])

        def save(fh) -> None:
            if not needs_package_pass(path):
                wb.save(fh)
                return
            buf = io.BytesIO()
            wb.save(buf)
            fh.write(finish_package(buf.getvalue(), path, macro_parts))

        counts = computed.formula_counts(wb)
        if any(counts.values()):
            # Excel computes on open the formulas the splice leaves empty.
            wb.calculation.fullCalcOnLoad = True
        if not outcome.should_save_main or not any(counts.values()):
            return call.commit(outcome, kind=KIND, fmt=FMT, save=save, readback=lambda: _readback(job),
                               baseline=baseline)
        marks: list = []
        text = _readback(job, marks)
        outside = computed.outside_reads(wb, computed.outside_parts(path) if call.edit_existing else [])
        if cells_before is not None:
            computed.save_carry(call.work_dir, computed.carry_facts(wb, cells_before, names_before))
        return call.stage(outcome, save=save, readback=lambda: text, baseline=baseline,
                          facts={"formulas": counts, "outside": outside, "marks": marks})
    finally:
        if wb.vba_archive is not None:
            wb.vba_archive.close()


def _finish_xlsx(staged: Staged, call: WriteCall, rendered: Rendered) -> WriteReport:
    """Worker B: the values written beside the formulas (recalculated, or
    kept from the file when they still hold), the readback with them, the
    commit."""
    facts = staged.facts
    outside = facts.get("outside") or []
    warnings = [computed.outside_note(outside)] if outside else []
    why_not = "" if outside else rendered.note
    done: list[recalc.Filled] = []

    def splice(path: str) -> None:
        carry = computed.load_carry(call.work_dir)
        filled = recalc.fill(path, expected=facts["formulas"], computed=rendered.payload,
                             original=call.path if carry is not None else None, carry=carry)
        done.append(filled)
        warnings.extend(computed.notes(filled))

    return call.commit_staged(
        staged, kind=KIND, fmt=FMT, transform=splice, warnings=warnings,
        readback=lambda _path: computed.readback(staged.readback, facts["marks"], done[0] if done else None,
                                                 why_not))


# ---------------------------------------------------------------------------
# Write — the catalogue
# ---------------------------------------------------------------------------


def _friendly(spec: OpSpec) -> OpSpec:
    """The operation with its on/off words read as booleans first, and an
    unexpected failure in plain words: openpyxl's class reprs and Python's
    keyword-argument phrasing mean nothing to a caller."""
    handler = spec.handler

    @functools.wraps(handler)
    def run(job, ctx, op):
        values.loosen(spec.keys, op)
        try:
            handler(job, ctx, op)
        except (OpError, NoOp):
            raise
        except ValueError as exc:
            raise OpError(Code.BAD_VALUE, _friendly_error(exc)) from exc
        except (KeyError, TypeError, AttributeError) as exc:
            logger.warning(f"write_xlsx op #{ctx.index} '{ctx.op}' failed: {exc}")
            raise OpError(Code.ERROR, _friendly_error(exc),
                          hint=f'{{"op": "help", "name": "{ctx.op}"}} shows the full shape.') from exc
    return replace(spec, handler=run)


_SHEET = KeySpec("sheet", ("str",))
_RANGE = KeySpec("range", ("str",), required=True, help="A1 range, e.g. 'B2:D10'")
_COUNT = KeySpec("count", ("int",), default=1)
_LINE = ("str", "int")
_COLOR = ("color",)

_DATES_NOTE = (
    "ADDRESSING: A1 notation, columns are letters and rows 1-based numbers; ranges 'B2:D10' or "
    "sheet-qualified 'Data!A1:C7'; sheet defaults to the first sheet. Colours: hex (#1F4E79; 8 digits are ARGB), CSS names, rgb(), "
    "theme slots (accent1). DATES: strict ISO strings ('2026-03-27', '2026-03-27T14:30', '14:30') "
    "become real dates displayed dd/mm/yyyy; '27/03/2026'-style text lands as TEXT with a warning."
)

_OP_SPECS = [
    # --- sheets ---
    OpSpec("create_sheet", _op_create_sheet, (
        KeySpec("name", ("str",), required=True),
        KeySpec("position", ("int",), help="0-based index; default last"),
    ), "SHEETS", aliases=("add_sheet", "new_sheet"),
        examples=({"op": "create_sheet", "name": "Data", "position": 0},)),
    OpSpec("delete_sheet", _op_delete_sheet, (KeySpec("name", ("str",), required=True),), "SHEETS",
           aliases=("remove_sheet",),
           note="refuses to delete the last sheet; formulas and names that read it become #REF!, "
                "charts that read it are named in a warning",
           setup=({"op": "create_sheet", "name": "Old"},),
           examples=({"op": "delete_sheet", "name": "Old"},)),
    OpSpec("rename_sheet", _op_rename_sheet, (
        KeySpec("old_name", ("str",), required=True, aliases=("name",)),
        KeySpec("new_name", ("str",), required=True),
    ), "SHEETS",
        note="formulas, names, validations, conditional formats and charts that name the sheet follow "
             "the new name",
        examples=({"op": "rename_sheet", "old_name": "Sheet", "new_name": "Summary"},)),
    OpSpec("copy_sheet", _op_copy_sheet, (
        KeySpec("source", ("str",), required=True, aliases=("name",)),
        KeySpec("new_name", ("str",), required=True),
    ), "SHEETS", aliases=("duplicate_sheet",),
        note="copies cells and styles; charts and images are not copied",
        examples=({"op": "copy_sheet", "source": "Sheet", "new_name": "Copy"},)),
    OpSpec("protect_sheet", _op_protect_sheet, (
        _SHEET, KeySpec("password", ("str",)),
        *(flag_key(k, default=False) for k in (
            "allow_formatting_cells", "allow_formatting_columns", "allow_formatting_rows",
            "allow_insert_columns", "allow_insert_rows", "allow_sort", "allow_filter")),
    ), "SHEETS", aliases=("protect",),
        examples=({"op": "protect_sheet", "password": "pw", "allow_sort": True, "allow_filter": True,
                   "allow_formatting_cells": True, "allow_formatting_columns": True,
                   "allow_formatting_rows": True, "allow_insert_columns": True,
                   "allow_insert_rows": True},)),
    # --- cells ---
    OpSpec("write_cells", _op_write_cells, (
        _SHEET,
        KeySpec("cells", ("list",), item=(
            KeySpec("cell", ("str",), required=True, help="A1 address"),
            KeySpec("value", ("any",), default=""),
            KeySpec("type", ("str",), help="date | datetime | time | text"),
            KeySpec("format", ("str",), aliases=("number_format",)),
        )),
        KeySpec("data", ("list",), aliases=("rows", "values"), help="2-D row-major array"),
        KeySpec("start_cell", ("str",), default="A1"),
    ), "CELLS", one_of=(("cells", "data"),),
        aliases=("write_cell", "set_cell", "set_cells", "write", "set_value", "update_cells"),
        note="cells: [{cell, value, type?, format?}] OR data: 2D row-major array + start_cell",
        detail=(
            "data[0][0] lands AT start_cell (default A1), data[0][1] one column to its right. "
            "Formulas inline as '=SUM(B2:B9)'. Strict ISO strings ('2026-03-27', "
            "'2026-03-27T14:30', '14:30') become real dates displayed dd/mm/yyyy; "
            "'27/03/2026'-style text is never guessed and lands as TEXT with a warning. Per-cell "
            "type: date | datetime | time (same strict ISO, warns on non-ISO) | text (opt out); "
            f"{_NUMBER_FORMAT_HELP} — wins over the automatic date display."
        ),
        examples=(
            {"op": "write_cells", "cells": [{"cell": "A1", "value": "Total"},
                                            {"cell": "B1", "value": "2026-03-27", "type": "date",
                                             "format": "date"}]},
            {"op": "write_cells", "sheet": "Sheet", "start_cell": "A2", "data": [[1, 2], [3, "=A3+B3"]]},
        )),
    OpSpec("set_formula", _op_set_formula, (
        _SHEET, KeySpec("cell", ("str",), required=True), KeySpec("formula", ("str",), required=True),
    ), "CELLS", aliases=("add_formula", "write_formula"),
        note="'=' is auto-prepended; functions that reach the network or run a program (WEBSERVICE, "
             "IMAGE, RTD, CALL, DDE links...) are refused here and wherever a formula is stored",
        examples=({"op": "set_formula", "cell": "C1", "formula": "SUM(A1:B1)"},)),
    OpSpec("merge_cells", _op_merge_cells, (_SHEET, _RANGE), "CELLS", aliases=("merge",),
           examples=({"op": "merge_cells", "range": "A1:B1"},)),
    OpSpec("unmerge_cells", _op_unmerge_cells, (_SHEET, _RANGE), "CELLS", aliases=("unmerge",),
           setup=({"op": "merge_cells", "range": "A1:B1"},),
           examples=({"op": "unmerge_cells", "range": "A1:B1"},)),
    OpSpec("clear_range", _op_clear_range, (
        _SHEET, _RANGE, flag_key("clear_styles", default=False),
    ), "CELLS", aliases=("clear", "clear_cells"),
        note="clear_styles: true also resets fonts, fills, borders and formats",
        examples=({"op": "clear_range", "range": "A1:B2", "clear_styles": True},)),
    OpSpec("copy_range", _op_copy_range, (
        _SHEET, KeySpec("source_range", ("str",), required=True),
        KeySpec("target_start", ("str",), required=True), KeySpec("target_sheet", ("str",)),
        KeySpec("formulas", ("str",), default=_FORMULAS_ADJUST,
                help="adjust (relative references move, as in Excel) | verbatim"),
    ), "CELLS", aliases=("copy", "copy_cells"), note="copies values AND styles",
        setup=({"op": "write_cells", "cells": [{"cell": "A1", "value": 1}, {"cell": "B1", "value": "=A1*2"}]},),
        examples=({"op": "copy_range", "source_range": "A1:B1", "target_start": "A3", "target_sheet": "Sheet",
                   "formulas": "adjust"},)),
    # --- rows / columns ---
    OpSpec("insert_rows", _op_insert_rows, (
        _SHEET, KeySpec("row", ("int",), required=True), _COUNT,
    ), "ROWS / COLUMNS",
        note="inserts before row; references everywhere follow their cells (formulas, names, merges, "
             "validations, conditional formats, tables, filters, charts, pictures, row heights, "
             "freeze panes)",
        examples=({"op": "insert_rows", "row": 2, "count": 2},)),
    OpSpec("delete_rows", _op_delete_rows, (
        _SHEET, KeySpec("row", ("int",), required=True), _COUNT,
    ), "ROWS / COLUMNS", note="references to deleted cells become #REF!; ranges shrink",
        examples=({"op": "delete_rows", "row": 2, "count": 1},)),
    OpSpec("insert_columns", _op_insert_columns, (
        _SHEET, KeySpec("column", _LINE, required=True, help="letter or 1-based index"), _COUNT,
    ), "ROWS / COLUMNS", note="column: letter or 1-based index; references follow their cells",
        examples=({"op": "insert_columns", "column": "B", "count": 1},)),
    OpSpec("delete_columns", _op_delete_columns, (
        _SHEET, KeySpec("column", _LINE, required=True, help="letter or 1-based index"), _COUNT,
    ), "ROWS / COLUMNS",
        note="column: letter or 1-based index; references to deleted cells become #REF!",
        examples=({"op": "delete_columns", "column": 2},)),
    OpSpec("move_rows", _op_move_rows, (
        _SHEET, KeySpec("row", ("int",), required=True), _COUNT, KeySpec("to", ("int",), required=True),
    ), "ROWS / COLUMNS", aliases=("move_row",),
        note="cuts rows row..row+count-1 and inserts them before row `to` (numbered as before the "
             "move); references follow the moved cells",
        setup=({"op": "write_cells", "start_cell": "A1", "data": [["a"], ["b"], ["c"]]},),
        examples=({"op": "move_rows", "row": 1, "count": 1, "to": 4},)),
    OpSpec("move_columns", _op_move_columns, (
        _SHEET, KeySpec("column", _LINE, required=True), _COUNT, KeySpec("to", _LINE, required=True),
    ), "ROWS / COLUMNS", aliases=("move_column", "move_cols"),
        note="column and to: letters or 1-based indexes; cuts the columns and inserts them before "
             "column `to`",
        setup=({"op": "write_cells", "start_cell": "A1", "data": [["a", "b", "c"]]},),
        examples=({"op": "move_columns", "column": "C", "count": 1, "to": "A"},)),
    OpSpec("set_column_width", _op_set_column_width, (
        _SHEET, KeySpec("column", _LINE, required=True), KeySpec("width", ("float",), required=True,
                                                                  help="in characters"),
    ), "ROWS / COLUMNS", examples=({"op": "set_column_width", "column": "A", "width": 18},)),
    OpSpec("set_row_height", _op_set_row_height, (
        _SHEET, KeySpec("row", ("int",), required=True),
        KeySpec("height", ("length",), required=True, unit="pt"),
    ), "ROWS / COLUMNS", examples=({"op": "set_row_height", "row": 1, "height": 28},)),
    OpSpec("auto_column_width", _op_auto_column_width, (
        _SHEET, KeySpec("columns", ("list", "str"), of=("str",),
                        help="column letters, a list or 'A, C' or 'A:D'; omit for every column"),
    ), "ROWS / COLUMNS", aliases=("autofit", "auto_fit", "auto_fit_columns", "autofit_columns"),
        setup=({"op": "write_cells", "cells": [{"cell": "A1", "value": "a longer heading"}]},),
        examples=({"op": "auto_column_width", "columns": ["A"]},)),
    OpSpec("freeze_panes", _op_freeze_panes, (
        _SHEET, KeySpec("cell", ("str",), required=True),
    ), "ROWS / COLUMNS", aliases=("freeze", "freeze_pane"), note="'B2' freezes row 1 and column A",
        examples=({"op": "freeze_panes", "cell": "B2"},)),
    # --- formatting ---
    OpSpec("set_style", _op_set_style, (
        _SHEET, _RANGE,
        flag_key("bold"), flag_key("italic"),
        KeySpec("underline", ("bool", "str"), help="true, false or single | double"),
        flag_key("strikethrough"), KeySpec("font_size", ("float",)),
        KeySpec("font_color", _COLOR, aliases=("color",)), KeySpec("font_name", ("str",)),
        KeySpec("fill_color", _COLOR, aliases=("fill", "background")),
        KeySpec("border", ("bool", "str", "dict"), help="true, false, a style name, or an object", fields=(
            KeySpec("style", ("str",), default="thin"), KeySpec("color", _COLOR, default="000000"),
            *(flag_key(side) for side in _BORDER_SIDES),
        )),
        KeySpec("number_format", ("str",), aliases=("format",)),
        KeySpec("alignment", ("str", "dict"), fields=(
            KeySpec("horizontal", ("str",)), KeySpec("vertical", ("str",)),
            flag_key("wrap_text"), KeySpec("text_rotation", ("float",)),
            KeySpec("indent", ("float",)),
        )),
        flag_key("wrap_text"), KeySpec("text_rotation", ("float",)),
        KeySpec("protection", ("bool", "str", "dict"), help="true, false, locked | unlocked, or an object",
                fields=(
            flag_key("locked"), flag_key("hidden"),
        )),
    ), "FORMATTING", aliases=("format", "format_cells", "format_range", "style", "style_range", "apply_style"),
        note="only the given properties change, the rest of each cell's style is kept",
        detail=(
            "border: true (a thin box), false (no border) or {style: thin|medium|thick|dashed|double, "
            "color, left, right, top, bottom} — name sides true to draw only those (the others keep "
            "what they have), false to clear them; with no side named true every side not named "
            "false is drawn. alignment: 'center' or {horizontal, vertical, wrap_text, text_rotation, "
            "indent}; bold, italic, underline and strikethrough accept false to turn the style off; "
            f"protection: true/false or {{locked, hidden}}; {_NUMBER_FORMAT_HELP}."
        ),
        examples=({"op": "set_style", "range": "A1:B2", "bold": True, "italic": False, "underline": True,
                   "strikethrough": False, "font_size": 12, "font_color": "#1F4E79", "font_name": "Arial",
                   "fill_color": "DDEBF7", "border": {"style": "thin", "color": "999999", "bottom": True,
                                                      "top": False, "left": False, "right": False},
                   "number_format": "number",
                   "alignment": {"horizontal": "center", "vertical": "top", "wrap_text": True,
                                 "text_rotation": 0, "indent": 0},
                   "wrap_text": True, "text_rotation": 0, "protection": {"locked": False, "hidden": False}},)),
    # --- features ---
    OpSpec("create_table", _op_create_table, (
        _SHEET, KeySpec("range", ("str",), required=True, help="the header row included"),
        KeySpec("name", ("str",)), KeySpec("style", ("str",), default="TableStyleMedium9"),
        flag_key("show_first_column", default=False),
        flag_key("show_last_column", default=False),
        flag_key("show_row_stripes", default=True),
        flag_key("show_column_stripes", default=False),
    ), "FEATURES", aliases=("add_table", "table"),
        note="range includes the header row; style e.g. TableStyleMedium9",
        setup=({"op": "write_cells", "start_cell": "A1", "data": [["Item", "Qty"], ["a", 1]]},),
        examples=({"op": "create_table", "range": "A1:B2", "name": "Items", "style": "TableStyleLight9",
                   "show_first_column": False, "show_last_column": False, "show_row_stripes": True,
                   "show_column_stripes": False},)),
    OpSpec("add_data_validation", _op_add_data_validation, (
        _SHEET, _RANGE,
        KeySpec("validation_type", ("str",), aliases=("type", "kind"),
                help="list | whole | decimal | date | time | textLength | custom"),
        KeySpec("values", ("list", "str"), aliases=("options", "items", "list")),
        KeySpec("operator", ("str",)), KeySpec("value", ("any",)), KeySpec("min", ("any",)),
        KeySpec("max", ("any",)), KeySpec("formula", ("str",)),
        flag_key("allow_blank"), flag_key("show_error"),
        KeySpec("error_title", ("str",), default="Invalid input"), KeySpec("error_message", ("str",)),
        KeySpec("error_style", ("str",), default="stop"), flag_key("show_prompt"),
        KeySpec("prompt_title", ("str",)), KeySpec("prompt_message", ("str",)),
    ), "FEATURES", aliases=("add_validation", "add_dropdown", "data_validation"),
        note="validation_type: list | whole | decimal | date | time | textLength | custom",
        detail=(
            "list: values = array of literal items, or a reference string ('=SupplierList', "
            "\"'Data'!$B$2:$B$50\"). whole/decimal/date/time: operator (between | notBetween need "
            "min + max; greaterThan, lessThan, equal, notEqual, greaterThanOrEqual, lessThanOrEqual "
            "need value) — date/time bounds as ISO '2026-01-31' / '14:30'. textLength: max (or "
            "operator + value). custom: formula. show_error + error_title/error_message/error_style "
            "(stop|warning|information), show_prompt + prompt_title/prompt_message, allow_blank. "
            "Re-adding a rule on the same range replaces it; Excel allows one rule per cell."
        ),
        examples=(
            {"op": "add_data_validation", "range": "B2:B9", "validation_type": "list",
             "values": ["Yes", "No"], "allow_blank": True, "show_error": True, "error_title": "Pick one",
             "error_message": "Choose Yes or No", "error_style": "stop", "show_prompt": True,
             "prompt_title": "Answer", "prompt_message": "Yes or No"},
            {"op": "add_data_validation", "range": "C2:C9", "validation_type": "whole",
             "operator": "between", "min": 1, "max": 10},
            {"op": "add_data_validation", "range": "D2:D9", "validation_type": "decimal",
             "operator": "greaterThan", "value": 0},
            {"op": "add_data_validation", "range": "E2:E9", "validation_type": "custom",
             "formula": "LEN(E2)<10"},
        )),
    OpSpec("remove_data_validation", _op_remove_data_validation, (
        _SHEET, KeySpec("range", ("str",)), flag_key("all"),
    ), "FEATURES", one_of=(("range", "all"),),
        aliases=("remove_validation", "clear_validation", "clear_data_validation"),
        note="range removes every rule whose cells intersect it ('B2:B50 D2:D50' allowed); all: true "
             "clears the sheet",
        setup=({"op": "add_data_validation", "range": "B2:B9", "values": ["a", "b"]},),
        examples=({"op": "remove_data_validation", "range": "B2"},
                  {"op": "remove_data_validation", "all": True})),
    OpSpec("conditional_format", _op_conditional_format, (
        _SHEET, _RANGE,
        KeySpec("rule_type", ("str",), required=True, aliases=("type", "rule"),
                help="cell_is | formula | color_scale | data_bar | icon_set"),
        KeySpec("operator", ("str",)), KeySpec("formula", ("any",), aliases=("value",)),
        KeySpec("fill_color", _COLOR, aliases=("fill", "background")), KeySpec("font_color", _COLOR),
        flag_key("bold"), flag_key("italic"),
        flag_key("stop_if_true", aliases=("stopIfTrue",)),
        KeySpec("colors", ("list",), of=_COLOR),
        KeySpec("start_type", ("str",)), KeySpec("start_value", ("any",)), KeySpec("start_color", _COLOR),
        KeySpec("mid_type", ("str",)), KeySpec("mid_value", ("any",)), KeySpec("mid_color", _COLOR),
        KeySpec("end_type", ("str",)), KeySpec("end_value", ("any",)), KeySpec("end_color", _COLOR),
        KeySpec("color", _COLOR), flag_key("show_value", aliases=("showValue",)),
        KeySpec("min_length", ("int",)), KeySpec("max_length", ("int",)),
        KeySpec("icon_style", ("str",), aliases=("icon_set", "iconSet")),
        KeySpec("threshold_type", ("str",)), KeySpec("values", ("list",)),
        flag_key("percent"), flag_key("reverse"),
        KeySpec("params", ("dict",), help="the older nested form, same keys"),
    ), "FEATURES", reads_whole_op=True,
        aliases=("add_conditional_format", "add_conditional_formatting", "conditional_formatting"),
        note="rule_type: cell_is | formula | color_scale | data_bar | icon_set",
        detail=(
            "cell_is: operator (greaterThan, lessThan, between, notBetween, equal, notEqual, "
            "greaterThanOrEqual, lessThanOrEqual — or > < >= <= = !=; containsText, notContains, "
            "beginsWith, endsWith) + formula (a number, a cell ref, or text — quoted automatically "
            "unless it looks like a cell ref; between takes [low, high]) + at least one style: "
            "fill_color, font_color, bold, italic. formula: formula written for the range's top-left "
            "cell ('$C2>100') + the same style keys. color_scale: colors [min_hex, max_hex] or [min, "
            "mid, max] (or start_/mid_/end_ type|value|color with types min|max|num|percent|"
            "percentile|formula). data_bar: color (default 638EC6), show_value. icon_set: icon_style "
            "(3TrafficLights1, 3Arrows, 3Symbols, 4Arrows, 4Rating, 5Rating, 5Arrows…), "
            "threshold_type (percent | num | percentile), values (one per icon), reverse, show_value. "
            "stop_if_true on any rule. params: {…} the older nested form, same keys."
        ),
        examples=(
            {"op": "conditional_format", "range": "B2:B9", "rule_type": "cell_is", "operator": ">",
             "formula": 100, "fill_color": "FFC7CE", "font_color": "9C0006", "bold": True,
             "italic": False, "stop_if_true": True},
            {"op": "conditional_format", "range": "C2:C9", "rule_type": "color_scale",
             "colors": ["F8696B", "FFEB84", "63BE7B"]},
            {"op": "conditional_format", "range": "D2:D9", "rule_type": "color_scale",
             "start_type": "num", "start_value": 0, "start_color": "FFFFFF", "mid_type": "percentile",
             "mid_value": 50, "mid_color": "FFEB84", "end_type": "num", "end_value": 10,
             "end_color": "63BE7B"},
            {"op": "conditional_format", "range": "E2:E9", "rule_type": "data_bar", "color": "638EC6",
             "show_value": True, "min_length": 0, "max_length": 100},
            {"op": "conditional_format", "range": "F2:F9", "rule_type": "icon_set",
             "icon_style": "3Arrows", "threshold_type": "percent", "values": [0, 33, 67],
             "percent": True, "reverse": False},
            {"op": "conditional_format", "range": "G2:G9", "rule_type": "formula",
             "params": {"formula": "$G2>5", "fill": "FFEB9C"}},
        )),
    OpSpec("remove_conditional_format", _op_remove_conditional_format, (
        _SHEET, KeySpec("range", ("str",)), flag_key("all"),
    ), "FEATURES", one_of=(("range", "all"),),
        aliases=("remove_conditional_formatting", "clear_conditional_formatting", "clear_conditional_format"),
        note="range removes every rule whose cells intersect it; all: true clears the sheet",
        setup=({"op": "conditional_format", "range": "B2:B9", "rule_type": "data_bar"},),
        examples=({"op": "remove_conditional_format", "range": "B2"},
                  {"op": "remove_conditional_format", "all": True})),
    OpSpec("auto_filter", _op_auto_filter, (_SHEET, _RANGE), "FEATURES",
           aliases=("add_filter", "set_filter", "autofilter", "add_auto_filter", "set_auto_filter"),
           examples=({"op": "auto_filter", "range": "A1:C9"},)),
    OpSpec("define_name", _op_define_name, (
        KeySpec("name", ("str",), required=True),
        KeySpec("range", ("str",), required=True, aliases=("value", "ref", "refers_to")),
        _SHEET,
    ), "FEATURES", aliases=("add_named_range", "create_named_range", "named_range"),
        note="a range containing '!' is used as-is; a bare range is qualified with sheet",
        examples=({"op": "define_name", "name": "Rate", "range": "$B$1", "sheet": "Sheet"},)),
    # --- charts ---
    OpSpec("add_chart", _op_add_chart, (
        _SHEET,
        KeySpec("chart_type", ("str",), default="column", aliases=("type", "kind")),
        KeySpec("anchor", ("str",), default="E1", aliases=("position", "cell", "at")),
        KeySpec("title", ("str",)),
        KeySpec("x_axis_title", ("str",), aliases=("x_axis", "x_label", "x_title")),
        KeySpec("y_axis_title", ("str",), aliases=("y_axis", "y_label", "y_title")),
        KeySpec("width", ("length",), default=15, unit="cm"),
        KeySpec("height", ("length",), default=7.5, unit="cm"),
        KeySpec("style", ("int",), aliases=("chart_style",), help="1-48"),
        flag_key("stacked", default=False),
        KeySpec("legend", ("bool", "str"), help="r | l | t | b | tr | false"),
        flag_key("show_percent", aliases=("show_percentage",)),
        flag_key("show_values", aliases=("show_value",)),
        flag_key("titles_from_data", default=True),
        KeySpec("data_range", ("str",), aliases=("range", "data")),
        KeySpec("categories", ("str", "list"), aliases=("categories_range", "labels")),
        KeySpec("series", ("list", "dict", "str")),
    ), "CHARTS", one_of=(("data_range", "series"),), aliases=("create_chart", "insert_chart", "chart"),
        note="chart_type: column (vertical) | bar (HORIZONTAL) | line | pie | doughnut | scatter | area; "
             "anchor: top-left cell (default E1)",
        detail=(
            "DATA, one of: data_range 'A1:C7' or 'Data!A1:C7' — column 1 = categories, row 1 = series "
            "names (titles_from_data: false ⇒ no header row), one series per further column. OR "
            "categories + series: categories = a range string ('A2:A7') or a list of labels; series = "
            "[{name?, values}] where values is a one-column/one-row range string ('B2:B7') or a list "
            "of numbers (a bare range string or a list of range strings also works). Ranges are "
            "referenced in place, never copied; literal lists are written to a data block below the "
            "sheet's used range. Sheet-qualified ranges may read any sheet. OPTIONS: title, "
            "x_axis_title, y_axis_title, width + height in cm (default 15 x 7.5), style 1-48, stacked "
            "(bar/column/line/area), legend (r | l | t | b | tr | false), show_percent and show_values "
            "(data labels). Renaming a sheet later updates the chart's references; deleting it is "
            "warned about."
        ),
        setup=({"op": "write_cells", "start_cell": "A1", "data": [["m", "v"], ["jan", 1], ["feb", 2]]},),
        examples=(
            {"op": "add_chart", "data_range": "A1:B3", "chart_type": "line", "anchor": "D2",
             "title": "Trend", "x_axis_title": "Month", "y_axis_title": "Value", "width": 12,
             "height": "6cm", "style": 10, "stacked": False, "legend": "b", "show_values": True,
             "titles_from_data": True},
            {"op": "add_chart", "chart_type": "pie", "categories": ["a", "b"],
             "series": [{"name": "Share", "values": [3, 4]}], "show_percent": True, "legend": False},
        )),
    # --- images ---
    OpSpec("add_image", _op_add_image, (
        _SHEET, KeySpec("image_path", ("path",), required=True, aliases=("path", "image")),
        KeySpec("cell", ("str",), default="A1", aliases=("anchor",)),
        KeySpec("width", ("length",), unit="px"), KeySpec("height", ("length",), unit="px"),
    ), "IMAGES", aliases=("insert_image", "add_picture"),
        note="cell: top-left anchor (default A1); width/height in pixels",
        examples=({"op": "add_image", "image_path": "logo.png", "cell": "B2", "width": 40, "height": 20},)),
    OpSpec("add_equation", _op_add_equation, (
        _SHEET, KeySpec("latex", ("str",), required=True, aliases=("equation", "formula")),
        KeySpec("cell", ("str",), default="A1", aliases=("anchor",)),
        KeySpec("height", ("length",), default=40, unit="px"),
    ), "IMAGES", aliases=("equation", "add_latex"),
        note="LaTeX rendered as a picture at cell (default A1), height in px (default 40); the source "
             "is kept in a cell comment and re-running at the same cell replaces it",
        examples=({"op": "add_equation", "latex": "x^2", "cell": "B2", "height": 30},)),
]

_OP_SPECS += [*annotations.OPS, *csv_import.OPS, *inventory.OPS, *printing.OPS]

CATALOG = Catalog(
    "write_xlsx", [_friendly(spec) for spec in _OP_SPECS],
    key_aliases={"sheet_name": "sheet", "worksheet": "sheet"}, notes=_DATES_NOTE, color_argb=True,
)


def contract_job(tmp_dir: str) -> _XlsxJob:
    """A fresh job for the key-coverage contract."""
    from openpyxl import Workbook

    return _XlsxJob(Workbook(), work_dir=tmp_dir)


def contract_fixtures(tmp_dir: str) -> dict[str, str]:
    """The files the contract's examples read."""
    from PIL import Image

    logo = os.path.join(tmp_dir, "logo.png")
    Image.new("RGB", (8, 4), (31, 78, 121)).save(logo)
    data = os.path.join(tmp_dir, "data.csv")
    with open(data, "w", encoding="utf-8") as fh:
        fh.write("id;amount;day\n007;1.234,5;2026-03-27\n")
    return {"logo.png": logo, "data.csv": data}
