"""Excel (XLSX) read and write handlers for the file-tools MCP.

Provides read_xlsx (enhanced with range, formula view, metadata) and
handle_write_xlsx (27 operations covering full spreadsheet functionality).
"""

import re
import uuid
from copy import copy
from pathlib import Path

from shared import _normalize_operations, _op_type, _push_preview, _resolve_path, _to_agents_relative, logger

# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_xlsx(
    path: str,
    sheet: str | None,
    max_rows: int,
    start_cell: str | None = None,
    end_cell: str | None = None,
    show_formulas: bool = False,
) -> str:
    """Read an XLSX file and return structured text.

    Supports range-based reading, formula view, and metadata output.
    """
    from openpyxl import load_workbook
    from openpyxl.utils import column_index_from_string

    wb = load_workbook(path, read_only=False, data_only=not show_formulas)
    sheets = wb.sheetnames
    result = [f"**XLSX**: {Path(path).name} — Sheets: {', '.join(sheets)}"]
    result.append("")

    target = sheet if sheet and sheet in sheets else sheets[0]
    ws = wb[target]

    # Sheet metadata
    dims = ws.dimensions if ws.dimensions else "empty"
    result.append(f"### Sheet: {target} (range: {dims})")

    # Determine reading bounds
    min_row, min_col = 1, 1
    max_row_bound = None
    max_col_bound = None

    if start_cell:
        m = re.match(r"([A-Z]+)(\d+)", start_cell.upper())
        if m:
            min_col = column_index_from_string(m.group(1))
            min_row = int(m.group(2))
    if end_cell:
        m = re.match(r"([A-Z]+)(\d+)", end_cell.upper())
        if m:
            max_col_bound = column_index_from_string(m.group(1))
            max_row_bound = int(m.group(2))

    # Read data
    rows_data = []
    total_rows = 0

    iter_kw = {
        "min_row": min_row,
        "min_col": min_col,
        "values_only": not show_formulas,
    }
    if max_row_bound:
        iter_kw["max_row"] = max_row_bound
    if max_col_bound:
        iter_kw["max_col"] = max_col_bound

    if show_formulas:
        for row in ws.iter_rows(**iter_kw):
            total_rows += 1
            if total_rows <= max_rows:
                rows_data.append(
                    [str(c.value) if c.value is not None else "" for c in row]
                )
    else:
        for row in ws.iter_rows(**iter_kw):
            total_rows += 1
            if total_rows <= max_rows:
                rows_data.append(
                    [str(c) if c is not None else "" for c in row]
                )

    if rows_data:
        result.append("| " + " | ".join(rows_data[0]) + " |")
        result.append("| " + " | ".join(["---"] * len(rows_data[0])) + " |")
        for row in rows_data[1:]:
            result.append("| " + " | ".join(row) + " |")

    if total_rows > max_rows:
        result.append(f"\n(Showing first {max_rows} of {total_rows} rows)")

    # Data validations summary
    try:
        dv_list = ws.data_validations.dataValidation
        if dv_list:
            result.append(f"\n**Data Validations**: {len(dv_list)} rule(s)")
            for dv in dv_list:
                info = f"  - {dv.sqref}: {dv.type}"
                if dv.type == "list" and dv.formula1:
                    info += f" = {dv.formula1.strip(chr(34))}"
                if dv.prompt:
                    info += f' ("{dv.prompt}")'
                result.append(info)
    except Exception:
        pass

    # Merged cells
    try:
        if ws.merged_cells.ranges:
            merged = ", ".join(str(r) for r in ws.merged_cells.ranges)
            result.append(f"\n**Merged Cells**: {merged}")
    except Exception:
        pass

    # Tables
    try:
        if ws.tables:
            result.append(f"\n**Tables**: {len(ws.tables)}")
            for tname, tref in ws.tables.items():
                result.append(f"  - {tname}: {tref}")
    except Exception:
        pass

    wb.close()
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Write — helpers
# ---------------------------------------------------------------------------


def _ensure_ff(color_hex: str) -> str:
    """Ensure a hex color string has the FF opacity prefix for openpyxl."""
    c = str(color_hex).lstrip("#")
    if len(c) == 6:
        return "FF" + c
    return c


def _get_sheet(wb, op: dict):
    """Get worksheet from operation, defaulting to first sheet."""
    name = op.get("sheet", wb.sheetnames[0])
    if name not in wb.sheetnames:
        raise ValueError(f"Sheet '{name}' not found. Available: {wb.sheetnames}")
    return wb[name]


def _parse_cell_ref(cell_str: str):
    """Parse 'A1' into (col_index, row_index) — both 1-based."""
    from openpyxl.utils import column_index_from_string

    m = re.match(r"([A-Z]+)(\d+)", cell_str.upper())
    if not m:
        raise ValueError(f"Invalid cell reference: {cell_str}")
    return column_index_from_string(m.group(1)), int(m.group(2))


# Formula safety
_UNSAFE_FUNCTIONS = {"INDIRECT", "WEBSERVICE", "DGET", "RTD"}


def _validate_formula(formula: str) -> str | None:
    """Validate a formula. Returns error string or None if valid."""
    upper = formula.upper()
    for func in _UNSAFE_FUNCTIONS:
        if func in upper:
            return f"Formula contains blocked function '{func}'"
    # Check balanced parentheses
    depth = 0
    for ch in formula:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth < 0:
            return "Unbalanced parentheses in formula"
    if depth != 0:
        return "Unbalanced parentheses in formula"
    return None


# ---------------------------------------------------------------------------
# Write — main handler
# ---------------------------------------------------------------------------


async def handle_write_xlsx(args: dict) -> str:
    """Create or modify an Excel workbook with batched operations."""
    from openpyxl import Workbook, load_workbook
    from openpyxl.chart import (
        AreaChart,
        BarChart,
        LineChart,
        PieChart,
        Reference,
        ScatterChart,
        Series,
    )
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.formatting.rule import (
        CellIsRule,
        ColorScaleRule,
        DataBarRule,
        FormulaRule,
        IconSetRule,
    )
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
    from openpyxl.utils import (
        column_index_from_string,
        get_column_letter,
        range_boundaries,
    )
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.worksheet.table import Table, TableStyleInfo

    path = _resolve_path(args["path"], writing=True)
    ops = _normalize_operations(args.get("operations"))
    create_new = args.get("create_new", False)

    if Path(path).exists() and not create_new:
        wb = load_workbook(path)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()

    errors = []

    for idx, op in enumerate(ops):
        ot = _op_type(op)
        try:
            # =============================================================
            # SHEET OPERATIONS
            # =============================================================

            if ot == "create_sheet":
                pos = op.get("position")
                wb.create_sheet(title=op["name"], index=pos)

            elif ot == "delete_sheet":
                name = op["name"]
                if len(wb.sheetnames) <= 1:
                    errors.append(f"Op #{idx} delete_sheet: cannot delete the last sheet")
                    continue
                if name in wb.sheetnames:
                    del wb[name]

            elif ot == "rename_sheet":
                old = op.get("old_name") or op.get("name")
                new = op["new_name"]
                if old in wb.sheetnames:
                    wb[old].title = new
                else:
                    errors.append(f"Op #{idx} rename_sheet: sheet '{old}' not found")

            elif ot == "copy_sheet":
                source = op.get("source") or op.get("name")
                target = op["new_name"]
                if source in wb.sheetnames:
                    copied = wb.copy_worksheet(wb[source])
                    copied.title = target
                else:
                    errors.append(f"Op #{idx} copy_sheet: sheet '{source}' not found")

            elif ot == "protect_sheet":
                ws = _get_sheet(wb, op)
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

            # =============================================================
            # CELL OPERATIONS
            # =============================================================

            elif ot == "write_cells":
                ws = _get_sheet(wb, op)
                # Format 1: individual cells
                cells_list = op.get("cells")
                if cells_list and isinstance(cells_list, list):
                    for c in cells_list:
                        ref = c.get("cell", "")
                        if ref:
                            ws[ref] = c.get("value", "")
                else:
                    # Format 2: 2D array
                    start = op.get("start_cell", "A1")
                    data = op.get("data", [])
                    start_col, start_row = _parse_cell_ref(start)
                    for ri, row in enumerate(data):
                        for ci, val in enumerate(row):
                            ws.cell(
                                row=start_row + ri,
                                column=start_col + ci,
                                value=val,
                            )

            elif ot == "set_formula":
                ws = _get_sheet(wb, op)
                formula = op["formula"]
                if not formula.startswith("="):
                    formula = "=" + formula
                err = _validate_formula(formula)
                if err:
                    errors.append(f"Op #{idx} set_formula: {err}")
                    continue
                ws[op["cell"]] = formula

            elif ot == "merge_cells":
                ws = _get_sheet(wb, op)
                ws.merge_cells(op["range"])

            elif ot == "unmerge_cells":
                ws = _get_sheet(wb, op)
                ws.unmerge_cells(op["range"])

            elif ot == "clear_range":
                ws = _get_sheet(wb, op)
                min_c, min_r, max_c, max_r = range_boundaries(op["range"])
                clear_styles = op.get("clear_styles", False)
                for row in ws.iter_rows(
                    min_row=min_r, max_row=max_r,
                    min_col=min_c, max_col=max_c,
                ):
                    for cell in row:
                        cell.value = None
                        if clear_styles:
                            cell.font = Font()
                            cell.border = Border()
                            cell.fill = PatternFill()
                            cell.number_format = "General"
                            cell.alignment = Alignment()
                            cell.protection = Protection()

            elif ot == "copy_range":
                ws_src = _get_sheet(wb, op)
                target_sheet = op.get("target_sheet")
                ws_dst = wb[target_sheet] if target_sheet and target_sheet in wb.sheetnames else ws_src

                src_range = op["source_range"]
                src_min_col, src_min_row, src_max_col, src_max_row = range_boundaries(src_range)

                tgt_col, tgt_row = _parse_cell_ref(op["target_start"])

                for row_off in range(src_max_row - src_min_row + 1):
                    for col_off in range(src_max_col - src_min_col + 1):
                        src_cell = ws_src.cell(
                            row=src_min_row + row_off,
                            column=src_min_col + col_off,
                        )
                        dst_cell = ws_dst.cell(
                            row=tgt_row + row_off,
                            column=tgt_col + col_off,
                        )
                        dst_cell.value = src_cell.value
                        if src_cell.has_style:
                            dst_cell.font = copy(src_cell.font)
                            dst_cell.border = copy(src_cell.border)
                            dst_cell.fill = copy(src_cell.fill)
                            dst_cell.number_format = src_cell.number_format
                            dst_cell.alignment = copy(src_cell.alignment)
                            dst_cell.protection = copy(src_cell.protection)

            # =============================================================
            # ROW / COLUMN OPERATIONS
            # =============================================================

            elif ot == "insert_rows":
                ws = _get_sheet(wb, op)
                ws.insert_rows(int(op["row"]), int(op.get("count", 1)))

            elif ot == "insert_columns":
                ws = _get_sheet(wb, op)
                col = op.get("column", 1)
                if isinstance(col, str):
                    col = column_index_from_string(col.upper())
                ws.insert_cols(int(col), int(op.get("count", 1)))

            elif ot == "delete_rows":
                ws = _get_sheet(wb, op)
                ws.delete_rows(int(op["row"]), int(op.get("count", 1)))

            elif ot == "delete_columns":
                ws = _get_sheet(wb, op)
                col = op.get("column", 1)
                if isinstance(col, str):
                    col = column_index_from_string(col.upper())
                ws.delete_cols(int(col), int(op.get("count", 1)))

            elif ot == "set_column_width":
                ws = _get_sheet(wb, op)
                ws.column_dimensions[op["column"].upper()].width = float(op["width"])

            elif ot == "set_row_height":
                ws = _get_sheet(wb, op)
                ws.row_dimensions[int(op["row"])].height = float(op["height"])

            elif ot == "auto_column_width":
                ws = _get_sheet(wb, op)
                columns = op.get("columns")  # list of column letters, or None for all
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

            elif ot == "freeze_panes":
                ws = _get_sheet(wb, op)
                ws.freeze_panes = op["cell"]

            # =============================================================
            # FORMATTING
            # =============================================================

            elif ot == "set_style":
                ws = _get_sheet(wb, op)
                min_c, min_r, max_c, max_r = range_boundaries(op["range"])

                # Font
                font_kw = {}
                if op.get("bold") is not None:
                    font_kw["bold"] = op["bold"]
                if op.get("italic") is not None:
                    font_kw["italic"] = op["italic"]
                if op.get("underline"):
                    val = op["underline"]
                    font_kw["underline"] = "single" if val is True else str(val)
                if op.get("strikethrough"):
                    font_kw["strike"] = True
                if op.get("font_size"):
                    font_kw["size"] = int(op["font_size"])
                fc = op.get("font_color") or op.get("color")
                if fc:
                    font_kw["color"] = _ensure_ff(fc)
                if op.get("font_name"):
                    font_kw["name"] = op["font_name"]
                font = Font(**font_kw) if font_kw else None

                # Fill
                fill = None
                fill_val = op.get("fill_color") or op.get("fill") or op.get("background")
                if fill_val:
                    fc_str = _ensure_ff(fill_val)
                    fill = PatternFill(start_color=fc_str, end_color=fc_str, fill_type="solid")

                # Alignment
                align = None
                align_val = op.get("alignment")
                wrap = op.get("wrap_text")
                rotation = op.get("text_rotation")
                if align_val or wrap is not None or rotation is not None:
                    if isinstance(align_val, dict):
                        akw = {
                            "horizontal": align_val.get("horizontal"),
                            "vertical": align_val.get("vertical"),
                            "wrap_text": align_val.get("wrap_text", wrap),
                            "text_rotation": align_val.get("text_rotation", rotation),
                            "indent": align_val.get("indent"),
                        }
                        akw = {k: v for k, v in akw.items() if v is not None}
                        align = Alignment(**akw)
                    elif align_val:
                        akw = {"horizontal": str(align_val)}
                        if wrap is not None:
                            akw["wrap_text"] = wrap
                        if rotation is not None:
                            akw["text_rotation"] = rotation
                        align = Alignment(**akw)
                    else:
                        akw = {}
                        if wrap is not None:
                            akw["wrap_text"] = wrap
                        if rotation is not None:
                            akw["text_rotation"] = rotation
                        align = Alignment(**akw)

                # Border
                border = None
                border_val = op.get("border")
                if border_val:
                    if isinstance(border_val, dict):
                        bstyle = border_val.get("style", "thin")
                        bcolor = str(border_val.get("color", "000000")).lstrip("#")
                        side = Side(style=bstyle, color=bcolor)
                        border = Border(
                            left=side if border_val.get("left", True) else Side(),
                            right=side if border_val.get("right", True) else Side(),
                            top=side if border_val.get("top", True) else Side(),
                            bottom=side if border_val.get("bottom", True) else Side(),
                        )
                    else:
                        side = Side(style="thin")
                        border = Border(left=side, right=side, top=side, bottom=side)

                # Number format
                nf = op.get("number_format")

                # Protection
                prot = None
                if op.get("protection"):
                    pv = op["protection"]
                    prot = Protection(**pv) if isinstance(pv, dict) else Protection(locked=bool(pv))

                for row in ws.iter_rows(
                    min_row=min_r, max_row=max_r,
                    min_col=min_c, max_col=max_c,
                ):
                    for cell in row:
                        if font:
                            cell.font = font
                        if fill:
                            cell.fill = fill
                        if align:
                            cell.alignment = align
                        if border:
                            cell.border = border
                        if nf:
                            cell.number_format = nf
                        if prot:
                            cell.protection = prot

            # =============================================================
            # FEATURES — Tables, Validation, Conditional Formatting
            # =============================================================

            elif ot == "create_table":
                ws = _get_sheet(wb, op)
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

            elif ot == "add_data_validation":
                ws = _get_sheet(wb, op)
                dv_range = op["range"]
                dv_type = op.get("validation_type", "list")
                dv = DataValidation(type=dv_type)

                if dv_type == "list":
                    values = op.get("values", [])
                    if isinstance(values, list):
                        dv.formula1 = '"' + ",".join(str(v) for v in values) + '"'
                    else:
                        dv.formula1 = str(values)  # cell range reference
                elif dv_type in ("whole", "decimal"):
                    dv.operator = op.get("operator", "between")
                    if op.get("min") is not None:
                        dv.formula1 = str(op["min"])
                    if op.get("max") is not None:
                        dv.formula2 = str(op["max"])
                elif dv_type == "textLength":
                    dv.operator = op.get("operator", "lessThanOrEqual")
                    if op.get("max") is not None:
                        dv.formula1 = str(op["max"])
                elif dv_type == "date":
                    dv.operator = op.get("operator", "between")
                    if op.get("min"):
                        dv.formula1 = str(op["min"])
                    if op.get("max"):
                        dv.formula2 = str(op["max"])
                elif dv_type == "custom":
                    dv.formula1 = op.get("formula", "")

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
                ws.add_data_validation(dv)

            elif ot == "conditional_format":
                ws = _get_sheet(wb, op)
                cf_range = op["range"]
                rule_type = op.get("rule_type", "")
                params = dict(op.get("params", {}))

                def _fill_from_dict(d):
                    color = _ensure_ff(d.get("color", "FFC7CE"))
                    return PatternFill(start_color=color, end_color=color, fill_type="solid")

                def _font_from_dict(d):
                    kw = {}
                    if "color" in d:
                        kw["color"] = _ensure_ff(d["color"])
                    if "bold" in d:
                        kw["bold"] = d["bold"]
                    return Font(**kw)

                # Convert dict fill/font to openpyxl objects
                if "fill" in params and isinstance(params["fill"], dict):
                    params["fill"] = _fill_from_dict(params["fill"])
                if "font" in params and isinstance(params["font"], dict):
                    params["font"] = _font_from_dict(params["font"])

                if rule_type == "color_scale":
                    ws.conditional_formatting.add(cf_range, ColorScaleRule(**params))
                elif rule_type == "data_bar":
                    ws.conditional_formatting.add(cf_range, DataBarRule(**params))
                elif rule_type == "icon_set":
                    ws.conditional_formatting.add(cf_range, IconSetRule(**params))
                elif rule_type == "cell_is":
                    ws.conditional_formatting.add(cf_range, CellIsRule(**params))
                elif rule_type == "formula":
                    ws.conditional_formatting.add(cf_range, FormulaRule(**params))
                else:
                    errors.append(f"Op #{idx} conditional_format: unknown rule_type '{rule_type}'")

            elif ot == "auto_filter":
                ws = _get_sheet(wb, op)
                ws.auto_filter.ref = op["range"]

            elif ot == "define_name":
                from openpyxl.workbook.defined_name import DefinedName

                name = op["name"]
                sheet_name = op.get("sheet", wb.sheetnames[0])
                cell_range = op["range"]
                ref = f"'{sheet_name}'!{cell_range}"
                defn = DefinedName(name, attr_text=ref)
                wb.defined_names.add(defn)

            # =============================================================
            # CHARTS
            # =============================================================

            elif ot == "add_chart":
                ws = _get_sheet(wb, op)
                chart_type = op.get("chart_type", "bar")
                chart_map = {
                    "bar": BarChart,
                    "line": LineChart,
                    "pie": PieChart,
                    "scatter": ScatterChart,
                    "area": AreaChart,
                }
                ChartClass = chart_map.get(chart_type, BarChart)
                chart = ChartClass()
                chart.title = op.get("title", "")
                if op.get("x_axis"):
                    chart.x_axis.title = op["x_axis"]
                if op.get("y_axis"):
                    chart.y_axis.title = op["y_axis"]
                if op.get("chart_style"):
                    chart.style = int(op["chart_style"])
                chart.width = float(op.get("width", 15))
                chart.height = float(op.get("height", 7.5))

                # Two modes:
                # Mode 1: data_range — reference existing data on the sheet
                # Mode 2: categories + series — inline data (written to hidden area)
                categories = op.get("categories")
                series_list = op.get("series")

                if categories and series_list:
                    # Mode 2: Write inline data to cells, then reference them
                    # Find a safe area: below all existing data
                    data_start_row = ws.max_row + 2 if ws.max_row else 1
                    data_start_col = 1

                    # Write header row: "Category", series names
                    ws.cell(row=data_start_row, column=data_start_col, value="Category")
                    for si, s in enumerate(series_list):
                        ws.cell(row=data_start_row, column=data_start_col + 1 + si,
                                value=s.get("name", f"Series {si + 1}"))

                    # Write data rows
                    for ri, cat in enumerate(categories):
                        ws.cell(row=data_start_row + 1 + ri, column=data_start_col, value=cat)
                        for si, s in enumerate(series_list):
                            vals = s.get("values", [])
                            if ri < len(vals):
                                ws.cell(row=data_start_row + 1 + ri,
                                        column=data_start_col + 1 + si,
                                        value=vals[ri])

                    # Build references
                    num_rows = len(categories)
                    num_series = len(series_list)
                    min_r = data_start_row
                    max_r = data_start_row + num_rows
                    min_c = data_start_col
                    max_c = data_start_col + num_series

                    if chart_type == "scatter":
                        x_values = Reference(ws, min_col=min_c, min_row=min_r + 1, max_row=max_r)
                        for col_idx in range(min_c + 1, max_c + 1):
                            y_values = Reference(ws, min_col=col_idx, min_row=min_r + 1, max_row=max_r)
                            s = Series(y_values, x_values)
                            title_cell = ws.cell(row=min_r, column=col_idx)
                            if title_cell.value:
                                s.title = str(title_cell.value)
                            chart.series.append(s)
                    else:
                        cats = Reference(ws, min_col=min_c, min_row=min_r + 1, max_row=max_r)
                        for col_idx in range(min_c + 1, max_c + 1):
                            vals = Reference(ws, min_col=col_idx, min_row=min_r, max_row=max_r)
                            chart.add_data(vals, titles_from_data=True)
                        chart.set_categories(cats)

                else:
                    # Mode 1: data_range — reference existing data
                    dr = op.get("data_range", "")
                    if not dr:
                        errors.append(f"Op #{idx} add_chart: provide data_range OR categories+series")
                        continue
                    rng = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", dr.upper())
                    if not rng:
                        errors.append(f"Op #{idx} add_chart: invalid data_range '{dr}'")
                        continue
                    min_c = column_index_from_string(rng.group(1))
                    min_r = int(rng.group(2))
                    max_c = column_index_from_string(rng.group(3))
                    max_r = int(rng.group(4))

                    if chart_type == "scatter":
                        x_values = Reference(ws, min_col=min_c, min_row=min_r + 1, max_row=max_r)
                        for col_idx in range(min_c + 1, max_c + 1):
                            y_values = Reference(ws, min_col=col_idx, min_row=min_r + 1, max_row=max_r)
                            s = Series(y_values, x_values)
                            title_cell = ws.cell(row=min_r, column=col_idx)
                            if title_cell.value:
                                s.title = str(title_cell.value)
                            chart.series.append(s)
                    else:
                        cats = Reference(ws, min_col=min_c, min_row=min_r + 1, max_row=max_r)
                        for col_idx in range(min_c + 1, max_c + 1):
                            vals = Reference(ws, min_col=col_idx, min_row=min_r, max_row=max_r)
                            chart.add_data(vals, titles_from_data=True)
                        chart.set_categories(cats)

                ws.add_chart(chart, op.get("position", "E1"))

            # =============================================================
            # IMAGES
            # =============================================================

            elif ot == "add_image":
                ws = _get_sheet(wb, op)
                img_path = _resolve_path(
                    op.get("image_path") or op.get("path") or op.get("image", "")
                )
                img = XlImage(img_path)
                if op.get("width"):
                    img.width = int(op["width"])
                if op.get("height"):
                    img.height = int(op["height"])
                ws.add_image(img, op.get("cell", "A1"))

            # =============================================================
            # UNKNOWN
            # =============================================================

            else:
                logger.warning(f"write_xlsx: unknown operation '{ot}', skipping")
                errors.append(f"Op #{idx}: unknown operation '{ot}'")

        except Exception as exc:
            errors.append(f"Op #{idx} {ot}: {exc}")
            logger.warning(f"write_xlsx op #{idx} '{ot}' failed: {exc}")

    # Save even if some operations failed (partial success)
    wb.save(path)
    await _push_preview(path)

    msg = f"Workbook saved: {_to_agents_relative(path)} ({len(ops)} operations applied)"
    if errors:
        msg += f"\n\nWarnings/Errors ({len(errors)}):\n" + "\n".join(f"  - {e}" for e in errors)
    return msg
