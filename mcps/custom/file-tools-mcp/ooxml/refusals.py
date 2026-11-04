"""Files the schema allows and Office still refuses or repairs: the first
rules, each one a writer of ours can trip.

- ``xlsx.office_refuses``:
  - a table whose name looks like a cell reference, holds a space or a
    character Excel does not allow, or is too long: Excel repairs the
    workbook and drops the table;
  - a number format with more than four sections, or with an unbalanced
    bracket or quote: Excel repairs the workbook and drops the format.
"""

from __future__ import annotations

import re

import checks
from checks import CheckContext, Finding, Severity

X_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CHECK = "office_refuses"
TABLE_NAME_MAX = 255
STYLES = "xl/styles.xml"
_A1 = re.compile(r"^\$?[A-Za-z]{1,3}\$?\d+$")
_R1C1 = re.compile(r"^[Rr](\d*)[Cc](\d*)$|^[RrCc]$")
# A letter of any script, an underscore or a backslash first, then letters,
# digits, underscores, periods and backslashes (Excel's own rule).
_TABLE_NAME = re.compile(r"^(?:[^\W\d]|\\)[\w.\\]*$")
_FIX_TABLE = ("Rename it: start with a letter (any alphabet) or an underscore, no spaces, and not like a cell "
              "reference such as A1.")


def _table_name_problem(name: str) -> str | None:
    if not name:
        return "has no name"
    if len(name) > TABLE_NAME_MAX:
        return f"has a name longer than {TABLE_NAME_MAX} characters"
    if _A1.match(name) or _R1C1.match(name):
        return f"is named '{name}', which reads as a cell reference"
    if not _TABLE_NAME.match(name):
        return f"is named '{name}', with a space or a character Excel does not allow in a table name"
    return None


def _tables(ctx: CheckContext) -> list[Finding]:
    found: list[Finding] = []
    for part in ctx.part_names():
        if not (part.startswith("xl/tables/") and part.endswith(".xml")):
            continue
        root = ctx.part_xml(part)
        if root is None or root.tag != f"{{{X_NS}}}table":
            continue
        for attribute in ("displayName", "name"):
            problem = _table_name_problem(root.get(attribute) or "")
            if problem:
                found.append(Finding(Severity.ERROR, "", location=f"part /{part}",
                                     message=f"a table {problem}, so Excel repairs the workbook and drops the table",
                                     fix=_FIX_TABLE, part=part, key=f"table_name:{root.get(attribute)}"))
                break
    return found


def format_sections(code: str) -> tuple[int, str | None]:
    """The number of sections of a number format code, and what is
    unbalanced in it (``None`` when nothing is)."""
    sections, depth, quoted, escaped = 1, 0, False, False
    for char in code:
        if escaped:
            escaped = False
        elif quoted:
            quoted = char != '"'
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = True
        elif char == "[":
            depth += 1
        elif char == "]":
            if not depth:
                return sections, "a ] without its ["
            depth -= 1
        elif char == ";" and not depth:
            sections += 1
    if quoted:
        return sections, "a quote that is never closed"
    if depth:
        return sections, "a [ that is never closed"
    return sections, None


def _number_formats(ctx: CheckContext) -> list[Finding]:
    root = ctx.part_xml(STYLES)
    if root is None:
        return []
    found: list[Finding] = []
    for node in root.iter(f"{{{X_NS}}}numFmt"):
        code = node.get("formatCode") or ""
        sections, unbalanced = format_sections(code)
        problem = unbalanced and f"has {unbalanced}" or (sections > 4 and f"has {sections} sections, past four")
        if problem:
            found.append(Finding(Severity.ERROR, "", location=f"number format {node.get('numFmtId')}",
                                 message=f"the number format {problem}, so Excel repairs the workbook and drops it",
                                 fix="Give at most four sections (positive;negative;zero;text) with every [ and "
                                     "quote closed.", excerpt=code, part=STYLES, key=f"number_format:{code}"))
    return found


def check_xlsx(ctx: CheckContext) -> list[Finding]:
    return [*_tables(ctx), *_number_formats(ctx)]


def register_checks() -> None:
    checks.register("xlsx", CHECK, check_xlsx, baseline=True)
