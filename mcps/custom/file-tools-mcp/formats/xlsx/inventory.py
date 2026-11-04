"""What a workbook holds beside its cells: the read's footer of defined
names, charts, conditional formats, frozen panes and hidden parts, and the
delete_name operation."""

import re

from catalog import KeySpec, OpSpec
from results import NoOp, OpCtx

# Every character that can end a line where the read is shown.
_BREAKS_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")

# Each footer list shows this many entries, then a count of the rest.
_CAP = 30
_TEXT_CAP = 100
_SHOWN_LINES = 8


def clean(text, cap: int = _TEXT_CAP) -> str:
    """File text for one line of the read: pipes, line breaks (every kind)
    and length tamed, so a value cannot start a line of its own."""
    value = str(text if text is not None else "")
    value = _BREAKS_RE.sub("⏎", value.replace("|", "\\|"))
    return value if len(value) <= cap else value[:cap] + "…"


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= _CAP:
        return lines
    return lines[:_CAP] + [f"  …and {len(lines) - _CAP} more"]


def _spans(intervals: list[tuple[int, int]], label) -> str:
    """'3-5, 9' style runs of hidden rows or columns, from (first, last)
    pairs."""
    runs: list[tuple[int, int]] = []
    for lo, hi in sorted(intervals):
        if runs and lo <= runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], max(hi, runs[-1][1]))
        else:
            runs.append((lo, hi))
    parts = [label(a) if a == b else f"{label(a)}-{label(b)}" for a, b in runs[:_SHOWN_LINES]]
    more = f" and {len(runs) - _SHOWN_LINES} more" if len(runs) > _SHOWN_LINES else ""
    return ", ".join(parts) + more


def _chart_kind(chart) -> str:
    name = type(chart).__name__.removesuffix("Chart").lower()
    if name == "bar":
        return "column" if getattr(chart, "type", None) == "col" else "bar"
    return name


def _chart_title(chart) -> str:
    title = getattr(chart, "title", None)
    if title is None or isinstance(title, str):
        return title or ""
    try:
        runs = [r.t for p in title.tx.rich.p for r in (p.r or [])]
        return "".join(runs)
    except AttributeError:
        return ""


def _chart_refs(chart) -> list[str]:
    refs: list[str] = []
    for series in chart.series:
        for src in (series.val, series.cat, series.xVal, series.yVal):
            if src is None:
                continue
            for ref in (getattr(src, "numRef", None), getattr(src, "strRef", None)):
                if ref is not None and ref.f and ref.f not in refs:
                    refs.append(ref.f)
    return refs


def _anchor_text(chart) -> str:
    anchor = getattr(chart, "anchor", None)
    if isinstance(anchor, str):
        return anchor
    marker = getattr(anchor, "_from", None)
    if marker is None:
        return "floating"
    from openpyxl.utils import get_column_letter

    return f"{get_column_letter(marker.col + 1)}{marker.row + 1}"


_VISIBLE = "visible"


def footer_lines(wb, ws) -> list[str]:
    """The read's sections about the workbook and the shown sheet beyond its
    cells. Names, titles and references are file content: tamed and capped."""
    out: list[str] = []
    names = [(name, defn.attr_text, None) for name, defn in wb.defined_names.items()]
    for sheet in wb.worksheets:
        names += [(name, defn.attr_text, sheet.title) for name, defn in sheet.defined_names.items()]
    if names:
        out.append(f"\n**Defined names** ({len(names)}):")
        out += _capped([f"  - {clean(n)}" + (f" (sheet '{clean(s)}')" if s else "") + f" = {clean(t)}"
                        for n, t, s in names])
    charts = list(getattr(ws, "_charts", []))
    if charts:
        out.append(f"\n**Charts** ({len(charts)}):")
        lines = []
        for chart in charts:
            title = _chart_title(chart)
            refs = ", ".join(clean(r) for r in _chart_refs(chart)[:4])
            lines.append(f"  - {_chart_kind(chart)} chart at {_anchor_text(chart)}"
                         + (f', title "{clean(title)}"' if title else "") + (f", data {refs}" if refs else ""))
        out += _capped(lines)
    rules = [(str(cf.sqref), rule.type) for cf in ws.conditional_formatting for rule in cf.rules]
    if rules:
        out.append(f"\n**Conditional formats** ({len(rules)}):")
        out += _capped([f"  - {clean(sqref)}: {clean(kind)}" for sqref, kind in rules])
    if ws.freeze_panes:
        out.append(f"\n**Frozen panes**: at {clean(ws.freeze_panes)}")
    hidden_sheets = [s.title for s in wb.worksheets if s.sheet_state != _VISIBLE]
    if hidden_sheets:
        shown = ", ".join(clean(t) for t in hidden_sheets[:_CAP])
        more = f" and {len(hidden_sheets) - _CAP} more" if len(hidden_sheets) > _CAP else ""
        out.append(f"\n**Hidden sheets** ({len(hidden_sheets)}): {shown}{more}")
    hidden_rows = [(i, i) for i, dim in ws.row_dimensions.items() if dim.hidden]
    if hidden_rows:
        out.append(f"\n**Hidden rows** ({len(hidden_rows)}): {_spans(hidden_rows, str)}")
    from openpyxl.utils import get_column_letter

    hidden_cols: list[tuple[int, int]] = []
    for dim in ws.column_dimensions.values():
        if dim.hidden and dim.min:
            hidden_cols.append((dim.min, max(dim.max or dim.min, dim.min)))
    if hidden_cols:
        count = sum(hi - lo + 1 for lo, hi in hidden_cols)
        out.append(f"\n**Hidden columns** ({count}): {_spans(hidden_cols, get_column_letter)}")
    return out


def _uses(wb, name: str) -> int:
    """Formula cells whose text names `name` as a whole word."""
    word = re.compile(rf"(?<![\w.!']){re.escape(name)}(?![\w(!])", re.IGNORECASE)
    count = 0
    for sheet in wb.worksheets:
        for cell in sheet._cells.values():
            if cell.data_type == "f" and isinstance(cell.value, str) and word.search(cell.value):
                count += 1
    return count


def _op_delete_name(job, ctx: OpCtx, op: dict) -> None:
    wb = job.wb
    name = op["name"]
    scope = op.get("sheet")
    names = job.sheet(scope).defined_names if scope else wb.defined_names
    match = next((n for n in list(names) if n.casefold() == name.casefold()), None)
    if match is None:
        elsewhere = [] if scope else [sheet.title for sheet in wb.worksheets
                                      if any(n.casefold() == name.casefold() for n in sheet.defined_names)]
        hint = f"; sheet '{elsewhere[0]}' has one of that name: pass sheet to delete it" if elsewhere else ""
        raise NoOp(f"no defined name '{name}'" + (f" on sheet '{scope}'" if scope else " in the workbook") + hint)
    del names[match]
    uses = _uses(wb, match)
    if uses:
        ctx.warn(f"{uses} formula(s) still use '{match}' and will show #NAME? until it is defined again")


OPS = [
    OpSpec("delete_name", _op_delete_name, (
        KeySpec("name", ("str",), required=True),
        KeySpec("sheet", ("str",), help="for a name scoped to one sheet"),
    ), "FEATURES", aliases=("remove_name", "delete_named_range", "remove_named_range"),
        note="removes a defined name; read_document lists them; formulas still using it are named",
        setup=({"op": "define_name", "name": "Rate", "range": "$B$1"},),
        examples=({"op": "delete_name", "name": "Rate"},
                  {"op": "delete_name", "name": "Local", "sheet": "Sheet"})),
]
