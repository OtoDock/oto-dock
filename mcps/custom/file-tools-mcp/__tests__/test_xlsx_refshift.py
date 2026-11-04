"""Row and column insert, delete and move, sheet rename and sheet delete:
every reference in the workbook keeps pointing at the same cells, the way
Excel rewrites them."""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

openpyxl = pytest.importorskip("openpyxl")

from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import PatternFill
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.table import Table, TableStyleInfo

from excel import handle_write_xlsx
from formats.xlsx.refs import parse_ref, rewrite_refs
from formats.xlsx.refshift import Axis, Edit, EditKind, shift_ref


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    async def _no_preview(path, filename=None):
        return None

    monkeypatch.setattr("shared._resolve_path", _ident)
    monkeypatch.setattr("shared._push_preview", _no_preview)
    monkeypatch.setattr("shared._notify_file_written", _no_preview)


def _write(path: Path, ops: list[dict]) -> str:
    return asyncio.run(handle_write_xlsx({"path": str(path), "operations": ops}))


def _core(path: Path, ops: list[dict], work_dir: Path):
    """Both workers run directly on a resolved batch, with no
    recalculation in between."""
    from batch import Rendered, WriteCall
    from excel import CATALOG, _finish_xlsx, _write_xlsx_core
    from results import Staged

    canonical, problems, _ = CATALOG.resolve_batch(list(enumerate(ops)))
    assert problems == []
    call = WriteCall(path=str(path), ops=canonical, work_dir=str(work_dir))
    staged = _write_xlsx_core(call)
    return _finish_xlsx(staged, call, Rendered()) if isinstance(staged, Staged) else staged


def _rows(kind: EditKind, at: int, count: int = 1, to: int = 0, sheet: str = "S") -> Edit:
    return Edit(sheet, Axis.ROW, kind, at, count, to)


def _cols(kind: EditKind, at: int, count: int = 1, to: int = 0, sheet: str = "S") -> Edit:
    return Edit(sheet, Axis.COL, kind, at, count, to)


def _shift(formula: str, edit: Edit, own: str = "S") -> str:
    out = rewrite_refs(formula, lambda r: shift_ref(r, own, edit))
    assert out is not None
    return out


# ---------------------------------------------------------------------------
# The index rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, at, count, to, i, expected", [
    (EditKind.INSERT, 3, 2, 0, 2, 2),
    (EditKind.INSERT, 3, 2, 0, 3, 5),
    (EditKind.DELETE, 3, 2, 0, 3, None),
    (EditKind.DELETE, 3, 2, 0, 4, None),
    (EditKind.DELETE, 3, 2, 0, 5, 3),
    (EditKind.MOVE, 2, 1, 5, 2, 4),
    (EditKind.MOVE, 2, 1, 5, 3, 2),
    (EditKind.MOVE, 2, 1, 5, 4, 3),
    (EditKind.MOVE, 2, 1, 5, 5, 5),
    (EditKind.MOVE, 5, 2, 2, 5, 2),
    (EditKind.MOVE, 5, 2, 2, 6, 3),
    (EditKind.MOVE, 5, 2, 2, 2, 4),
    (EditKind.MOVE, 5, 2, 2, 4, 6),
])
def test_line_map(kind, at, count, to, i, expected):
    assert _rows(kind, at, count, to).map(i) == expected


@pytest.mark.parametrize("kind, at, count, to, span, expected", [
    (EditKind.INSERT, 5, 2, 0, (1, 10), (1, 12)),
    (EditKind.INSERT, 1, 2, 0, (1, 10), (3, 12)),
    (EditKind.INSERT, 11, 2, 0, (1, 10), (1, 10)),
    (EditKind.DELETE, 5, 2, 0, (1, 10), (1, 8)),
    (EditKind.DELETE, 1, 3, 0, (2, 10), (1, 7)),
    (EditKind.DELETE, 9, 5, 0, (1, 10), (1, 8)),
    (EditKind.DELETE, 3, 3, 0, (3, 5), None),
    (EditKind.MOVE, 3, 1, 8, (1, 10), (1, 10)),
    (EditKind.MOVE, 5, 1, 12, (5, 10), (5, 9)),
    (EditKind.MOVE, 5, 2, 12, (5, 6), (10, 11)),
    (EditKind.MOVE, 2, 1, 6, (2, 5), (2, 5)),
    (EditKind.MOVE, 8, 1, 4, (2, 5), (2, 6)),
    (EditKind.MOVE, 2, 1, 9, (2, 5), (2, 4)),
])
def test_range_map(kind, at, count, to, span, expected):
    assert _rows(kind, at, count, to).map_span(*span) == expected


def test_insert_clamps_a_range_and_drops_a_single_cell_past_the_edge():
    edit = _rows(EditKind.INSERT, 1, 1)
    assert edit.map_span(2, 1_048_576) == (3, 1_048_576)
    assert edit.map(1_048_576) is None


# ---------------------------------------------------------------------------
# Formula text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("formula, edit, expected", [
    ("=SUM(A1:A10)", _rows(EditKind.INSERT, 5, 2), "=SUM(A1:A12)"),
    ("=A5+$B$7*C$9", _rows(EditKind.INSERT, 5), "=A6+$B$8*C$10"),
    ("=A4+A5", _rows(EditKind.DELETE, 5), "=A4+#REF!"),
    ("=SUM(A1:A10)", _rows(EditKind.DELETE, 1, 10), "=SUM(#REF!)"),
    ("=SUM(B:B)+SUM(2:4)", _rows(EditKind.INSERT, 3), "=SUM(B:B)+SUM(2:5)"),
    ("=SUM(B:D)", _cols(EditKind.INSERT, 3), "=SUM(B:E)"),
    ("=SUM(B:D)", _cols(EditKind.DELETE, 2, 3), "=SUM(#REF!)"),
    ("=S!B5+'Other'!B5", _rows(EditKind.INSERT, 1), "=S!B6+'Other'!B5"),
    ('=IF(A5="A5",A5,0)', _rows(EditKind.INSERT, 1), '=IF(A6="A5",A6,0)'),
    ("=[1]S!A5+MyName+Table1[Col]", _rows(EditKind.INSERT, 1), "=[1]S!A5+MyName+Table1[Col]"),
    ("=SUM(S:T!A5)", _rows(EditKind.INSERT, 1), "=SUM(S:T!A5)"),
    ("=#REF!A5+A5", _rows(EditKind.INSERT, 1), "=#REF!A5+A6"),
    ("=LOG10(A5)", _rows(EditKind.INSERT, 1), "=LOG10(A6)"),
    ("=@A5:A9", _rows(EditKind.INSERT, 1), "=@A6:A10"),
    ("=SUM(a5:a9)", _rows(EditKind.INSERT, 1), "=SUM(A6:A10)"),
    ("=SUM(A1:A3)\r\n+ A5", _rows(EditKind.INSERT, 1), "=SUM(A2:A4)\r\n+ A6"),
    ("=A1048576+B1", _rows(EditKind.INSERT, 1), "=#REF!+B2"),
    ("=A2+Πωλήσεις!A2", _rows(EditKind.INSERT, 1, sheet="Πωλήσεις"), "=A2+Πωλήσεις!A3"),
])
def test_formula_rewrite(formula, edit, expected):
    assert _shift(formula, edit) == expected


def test_qualified_ref_to_the_edited_sheet_shifts_from_another_sheet():
    assert _shift("='S'!A5", _rows(EditKind.INSERT, 1), own="Other") == "='S'!A6"
    assert _shift("=A5", _rows(EditKind.INSERT, 1), own="Other") == "=A5"


def test_ref_to_a_deleted_cell_is_written_plain():
    assert _shift("=S!A5+S!B2", _rows(EditKind.DELETE, 5), own="Other") == "=#REF!+S!B2"


def test_parse_ref_rejects_names_and_out_of_grid():
    assert parse_ref("XFE1") is None
    assert parse_ref("R1C1") is None
    assert parse_ref("A1:B") is None
    assert parse_ref("Sheet1!A1:Sheet1!B2").parts[1].row == 2


def test_unparseable_formula_is_reported(tmp_path):
    f = tmp_path / "dde.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "=cmd|'/c calc'!A0"
    wb.active["A2"] = "=B5"
    wb.save(f)
    msg = _write(f, [{"op": "insert_rows", "row": 1}])
    assert "could not be parsed" in msg
    ws = openpyxl.load_workbook(f).active
    assert ws["A3"].value == "=B6"


# ---------------------------------------------------------------------------
# Workbook structures
# ---------------------------------------------------------------------------


def _book(tmp_path: Path, build) -> Path:
    f = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    build(wb, ws)
    wb.save(f)
    return f


def test_formulas_on_every_sheet_and_names_follow_an_insert(tmp_path):
    def build(wb, ws):
        for r in range(1, 6):
            ws.cell(row=r, column=1, value=r)
        ws["B6"] = "=SUM(A1:A5)"
        summary = wb.create_sheet("Summary")
        summary["A1"] = "=Data!B6*2"
        summary["A2"] = "=Data!A3"
        wb.defined_names["Total"] = DefinedName("Total", attr_text="Data!$B$6")

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 3, "count": 2}])
    wb = openpyxl.load_workbook(f)
    assert wb["Data"]["B8"].value == "=SUM(A1:A7)"
    assert wb["Summary"]["A1"].value == "=Data!B8*2"
    assert wb["Summary"]["A2"].value == "=Data!A5"
    assert wb.defined_names["Total"].attr_text == "Data!$B$8"
    assert "insert_rows at row 3 (+2) on 'Data'" in msg
    assert "3 formulas, 1 name updated" in msg
    assert "shifted accordingly" not in msg


def test_delete_reports_the_cells_that_became_ref_errors(tmp_path):
    def build(wb, ws):
        ws["A5"] = 1
        ws["B1"] = "=A5*2"

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "delete_rows", "sheet": "Data", "row": 5}])
    assert openpyxl.load_workbook(f)["Data"]["B1"].value == "=#REF!*2"
    assert "1 reference became #REF! (Data!B1)" in msg


def test_merges_validations_and_conditional_formats_follow(tmp_path):
    def build(wb, ws):
        ws.merge_cells("A2:B3")
        dv = DataValidation(type="whole", operator="greaterThan", formula1="0")
        dv.add("C2:C10")
        ws.add_data_validation(dv)
        fill = PatternFill(start_color="FFFF0000", end_color="FFFF0000", fill_type="solid")
        ws.conditional_formatting.add("D2:D10", FormulaRule(formula=["$D2>100"], fill=fill))
        ws.conditional_formatting.add("E2:E10", CellIsRule(operator="greaterThan",
                                                           formula=["$A$1"], fill=fill))

    f = _book(tmp_path, build)
    _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 1}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert [str(r) for r in ws.merged_cells.ranges] == ["A3:B4"]
    assert str(ws.data_validations.dataValidation[0].sqref) == "C3:C11"
    rules = {str(cf.sqref): cf.rules for cf in ws.conditional_formatting}
    assert rules["D3:D11"][0].formula == ["$D3>100"]
    assert rules["E3:E11"][0].formula == ["$A$2"]
    priorities = sorted(r.priority for rs in rules.values() for r in rs)
    assert priorities == [1, 2]


def test_conditional_format_re_anchors_when_its_top_row_is_deleted(tmp_path):
    def build(wb, ws):
        fill = PatternFill(start_color="FFFF0000", end_color="FFFF0000", fill_type="solid")
        ws.conditional_formatting.add("C5:C9", FormulaRule(formula=["$C5>100"], fill=fill))

    f = _book(tmp_path, build)
    _write(f, [{"op": "delete_rows", "sheet": "Data", "row": 5}])
    ws = openpyxl.load_workbook(f)["Data"]
    cf = next(iter(ws.conditional_formatting))
    assert str(cf.sqref) == "C5:C8"
    assert cf.rules[0].formula == ["$C5>100"]


def test_merge_inside_an_insert_grows_and_shrinks_on_delete(tmp_path):
    def build(wb, ws):
        ws["A2"] = "merged"
        ws.merge_cells("A2:A5")

    f = _book(tmp_path, build)
    _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 3}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert [str(r) for r in ws.merged_cells.ranges] == ["A2:A6"]
    _write(f, [{"op": "delete_rows", "sheet": "Data", "row": 3, "count": 4}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert list(ws.merged_cells.ranges) == []
    assert ws["A2"].value == "merged"


def test_tables_grow_and_gain_a_column(tmp_path):
    def build(wb, ws):
        ws.append(["Name", "Amount", "Tax"])
        for n in range(3):
            ws.append([f"r{n}", n, n * 0.2])
        t = Table(displayName="Sales", ref="A1:C4")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
        ws.add_table(t)
        ws["E1"] = "=SUM(Sales[Amount])"

    f = _book(tmp_path, build)
    _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 3},
               {"op": "insert_columns", "sheet": "Data", "column": "B"}])
    ws = openpyxl.load_workbook(f)["Data"]
    table = ws.tables["Sales"]
    assert table.ref == "A1:D5"
    assert [c.name for c in table.tableColumns] == ["Name", "Column1", "Amount", "Tax"]
    assert ws["B1"].value == "Column1"
    assert ws["F1"].value == "=SUM(Sales[Amount])"


def test_deleting_a_table_column_turns_its_structured_refs_to_ref_errors(tmp_path):
    def build(wb, ws):
        ws.append(["Name", "Amount"])
        ws.append(["a", 1])
        ws.add_table(Table(displayName="Sales", ref="A1:B2"))
        ws["D1"] = "=SUM(Sales[Amount])+SUM(Sales[Name])"

    f = _book(tmp_path, build)
    _write(f, [{"op": "delete_columns", "sheet": "Data", "column": "B"}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert ws.tables["Sales"].ref == "A1:A2"
    assert ws["C1"].value == "=SUM(#REF!)+SUM(Sales[Name])"


def test_deleting_every_data_row_of_a_table_is_refused(tmp_path):
    def build(wb, ws):
        ws.append(["Name", "Amount"])
        ws.append(["a", 1])
        ws.append(["b", 2])
        ws.add_table(Table(displayName="Sales", ref="A1:B3"))

    f = _book(tmp_path, build)
    before = f.read_bytes()
    msg = _write(f, [{"op": "delete_rows", "sheet": "Data", "row": 2, "count": 2}])
    assert "a table keeps at least one data row" in msg
    assert openpyxl.load_workbook(f)["Data"].tables["Sales"].ref == "A1:B3"
    assert before


def test_filter_freeze_dimensions_and_hyperlinks_follow(tmp_path):
    def build(wb, ws):
        ws.auto_filter.ref = "A1:C10"
        ws.freeze_panes = "B3"
        ws.row_dimensions[4].height = 30
        ws.column_dimensions["C"].width = 25
        ws["B5"] = "link"
        ws["B5"].hyperlink = "https://example.com"
        ws["B6"] = "inside"
        ws["B6"].hyperlink = openpyxl.worksheet.hyperlink.Hyperlink(ref="B6", location="Data!A5")

    f = _book(tmp_path, build)
    _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 2},
               {"op": "insert_columns", "sheet": "Data", "column": "A"}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert ws.auto_filter.ref == "B1:D11"
    assert ws.freeze_panes == "C4"
    assert ws.row_dimensions[5].height == 30
    assert ws.column_dimensions["D"].width == 25
    assert ws["C6"].hyperlink.target == "https://example.com"
    assert ws["C6"].hyperlink.ref == "C6"
    assert ws["C7"].hyperlink.location == "Data!B6"


def test_charts_and_pictures_follow_their_cells(tmp_path):
    def build(wb, ws):
        ws.append(["m", "v"])
        for n in range(4):
            ws.append([f"m{n}", n])
        chart = BarChart()
        chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=5), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=5))
        ws.add_chart(chart, "D2")
        other = wb.create_sheet("Dash")
        dash = BarChart()
        dash.add_data(Reference(ws, min_col=2, min_row=1, max_row=5), titles_from_data=True)
        other.add_chart(dash, "A1")

    f = _book(tmp_path, build)
    _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 1, "count": 2}])
    wb = openpyxl.load_workbook(f)
    chart = wb["Data"]._charts[0]
    assert chart.series[0].val.numRef.f == "'Data'!$B$4:$B$7"
    assert chart.anchor._from.row == 3
    assert wb["Dash"]._charts[0].series[0].val.numRef.f == "'Data'!$B$4:$B$7"


def test_array_formula_follows_and_a_partial_cut_is_refused(tmp_path):
    def build(wb, ws):
        ws["B3"] = ArrayFormula("B3:B5", "=A3:A5*2")

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 4}])
    assert "cut the array formula in B3:B5" in msg
    _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 1}])
    cell = openpyxl.load_workbook(f)["Data"]["B4"]
    assert cell.value.ref == "B4:B6" and cell.value.text == "=A4:A6*2"


def test_sparse_insert_never_builds_the_used_rectangle(tmp_path):

    f = tmp_path / "sparse.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1
    wb.active["Z100000"] = 2
    wb.save(f)
    seen = {}
    import formats.xlsx.refshift as xlsx_refshift

    real = xlsx_refshift.apply_edit

    def spy(wb, ws, edit):
        out = real(wb, ws, edit)
        seen["cells"] = len(ws._cells)
        return out

    xlsx_refshift_apply = spy
    import excel

    orig = excel.apply_edit
    excel.apply_edit = xlsx_refshift_apply
    try:
        _core(f, [{"op": "insert_rows", "row": 1}], tmp_path)
    finally:
        excel.apply_edit = orig
    assert seen["cells"] == 2
    assert openpyxl.load_workbook(f).active["Z100001"].value == 2


def test_insert_refuses_to_push_data_off_the_sheet(tmp_path):
    f = tmp_path / "edge.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1048576"] = "last"
    wb.save(f)
    msg = _write(f, [{"op": "insert_rows", "row": 1}])
    assert "A1048576 holds data" in msg


def test_readback_follows_a_later_insert(tmp_path):
    f = tmp_path / "rb.xlsx"
    msg = _write(f, [
        {"op": "write_cells", "cells": [{"cell": "B2", "value": "here"}]},
        {"op": "insert_rows", "row": 1, "count": 2},
    ])
    assert "Readback — verify placement — Sheet!B4:B4" in msg
    assert "| 4 | here |" in msg


def test_shift_budget_twenty_thousand_formulas(tmp_path):
    f = tmp_path / "big.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 20001):
        ws.cell(row=r, column=1, value=r)
        ws.cell(row=r, column=2, value=f"=A{r}*2+SUM($A$1:A{r})")
    wb.save(f)

    started = time.monotonic()
    import excel
    import formats.xlsx.refshift as xlsx_refshift

    timings = {}
    real = xlsx_refshift.apply_edit

    def timed(wb, ws, edit):
        t0 = time.monotonic()
        out = real(wb, ws, edit)
        timings["shift"] = time.monotonic() - t0
        return out

    orig = excel.apply_edit
    excel.apply_edit = timed
    try:
        _core(f, [{"op": "insert_rows", "row": 1}], tmp_path)
    finally:
        excel.apply_edit = orig
    assert timings["shift"] < 4.0, timings
    assert time.monotonic() - started < 60
    assert openpyxl.load_workbook(f).active["B20001"].value == "=A20001*2+SUM($A$2:A20001)"


# ---------------------------------------------------------------------------
# Moves
# ---------------------------------------------------------------------------


def test_move_rows_carries_references_along(tmp_path):
    def build(wb, ws):
        for r, v in enumerate(["a", "b", "c", "d", "e"], start=1):
            ws.cell(row=r, column=1, value=v)
        ws["C1"] = "=A2"
        ws["C2"] = "=SUM(A1:A5)"
        ws.row_dimensions[2].height = 40

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "move_rows", "sheet": "Data", "row": 2, "to": 5}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert [ws.cell(row=r, column=1).value for r in range(1, 6)] == ["a", "c", "d", "b", "e"]
    assert ws["C1"].value == "=A4"
    assert ws["C4"].value == "=SUM(A1:A5)"
    assert ws.row_dimensions[4].height == 40
    assert "move_rows 2 (x1) before row 5 on 'Data'" in msg


def test_move_columns_by_letter(tmp_path):
    def build(wb, ws):
        ws.append(["a", "b", "c", "d"])
        ws["A3"] = "=D1"

    f = _book(tmp_path, build)
    _write(f, [{"op": "move_columns", "sheet": "Data", "column": "D", "to": "A"}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert [ws.cell(row=1, column=c).value for c in range(1, 5)] == ["d", "a", "b", "c"]
    assert ws["B3"].value == "=A1"


def test_move_refuses_to_cut_a_merged_range(tmp_path):
    def build(wb, ws):
        ws.merge_cells("B4:C6")

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "move_rows", "sheet": "Data", "row": 4, "count": 2, "to": 10}])
    assert "cut across the merged range B4:C6" in msg


def test_move_to_inside_the_block_is_refused_and_onto_itself_is_a_note(tmp_path):
    f = tmp_path / "m.xlsx"
    openpyxl.Workbook().save(f)
    assert "lies inside" in _write(f, [{"op": "move_rows", "row": 2, "count": 3, "to": 3}])
    same = _write(f, [{"op": "move_rows", "row": 2, "count": 3, "to": 5}])
    assert "Changed nothing:" in same and "the rows are already there" in same


# ---------------------------------------------------------------------------
# Sheet rename and delete
# ---------------------------------------------------------------------------


def test_rename_rewrites_every_reference_and_quotes_when_needed(tmp_path):
    def build(wb, ws):
        ws["A1"] = 5
        other = wb.create_sheet("Calc")
        other["A1"] = "=Data!A1*2"
        other["A2"] = "=SUM(Data:Calc!A1)"
        other["A3"] = '=INDIRECT("Data!A1")'
        wb.defined_names["Rate"] = DefinedName("Rate", attr_text="Data!$A$1")

    f = _book(tmp_path, build)
    _write(f, [{"op": "rename_sheet", "old_name": "Data", "new_name": "Input data"}])
    wb = openpyxl.load_workbook(f)
    calc = wb["Calc"]
    assert calc["A1"].value == "='Input data'!A1*2"
    assert calc["A2"].value == "=SUM('Input data:Calc'!A1)"
    assert calc["A3"].value == '=INDIRECT("Data!A1")'
    assert wb.defined_names["Rate"].attr_text == "'Input data'!$A$1"


def test_rename_refuses_a_case_insensitive_clash(tmp_path):
    def build(wb, ws):
        wb.create_sheet("Other")

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "rename_sheet", "old_name": "Data", "new_name": "other"}])
    assert "already exists" in msg
    assert openpyxl.load_workbook(f).sheetnames == ["Data", "Other"]


def test_delete_sheet_leaves_ref_errors_and_survives_a_later_insert(tmp_path):
    def build(wb, ws):
        ws["A1"] = 1
        calc = wb.create_sheet("Calc")
        calc["B2"] = "=Data!A1+1"
        calc["B3"] = "=B2"

    f = _book(tmp_path, build)
    msg = _write(f, [
        {"op": "delete_sheet", "name": "Data"},
        {"op": "insert_rows", "sheet": "Calc", "row": 1},
    ])
    calc = openpyxl.load_workbook(f)["Calc"]
    assert calc["B3"].value == "=#REF!+1"
    assert calc["B4"].value == "=B3"
    assert "delete_sheet 'Data'" in msg


def test_delete_sheet_moves_a_3d_endpoint_inward(tmp_path):
    def build(wb, ws):
        ws.title = "Jan"
        wb.create_sheet("Feb")
        mar = wb.create_sheet("Mar")
        total = wb.create_sheet("Total")
        total["A1"] = "=SUM(Jan:Mar!B2)"
        assert mar

    f = _book(tmp_path, build)
    _write(f, [{"op": "delete_sheet", "name": "Mar"}])
    assert openpyxl.load_workbook(f)["Total"]["A1"].value == "=SUM(Jan:Feb!B2)"


def test_moving_a_row_to_just_above_its_total_keeps_it_in_the_sum(tmp_path):
    def build(wb, ws):
        for r, (item, v) in enumerate([("Rent", 1000), ("Staff", 5000), ("Travel", 300)], start=2):
            ws.cell(row=r, column=1, value=item)
            ws.cell(row=r, column=2, value=v)
        ws["A5"] = "Total"
        ws["B5"] = "=SUM(B2:B4)"

    f = _book(tmp_path, build)
    _write(f, [{"op": "move_rows", "sheet": "Data", "row": 2, "to": 5}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert [ws.cell(row=r, column=1).value for r in range(2, 6)] == ["Staff", "Travel", "Rent", "Total"]
    assert ws["B5"].value == "=SUM(B2:B4)"


def test_summary_counts_read_as_sentences():
    from formats.xlsx.refshift import ShiftReport

    report = ShiftReport()
    report.bump("formulas", 3)
    report.bump("names")
    report.ref_errors.append("Data!B1")
    assert report.summary() == ": 3 formulas, 1 name updated; 1 reference became #REF! (Data!B1)"


# ---------------------------------------------------------------------------
# References joined to a function by ':'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("formula, expected", [
    ("=SUM(A2:INDEX(A:A,6))", "=SUM(A3:INDEX(A:A,6))"),
    ("=SUM(INDEX(A:A,2):A6)", "=SUM(INDEX(A:A,2):A7)"),
    ("=SUM(S!A2:INDEX(S!A:A,COUNTA(S!A:A)))", "=SUM(S!A3:INDEX(S!A:A,COUNTA(S!A:A)))"),
    ("=SUM($B$2:XLOOKUP(6,A:A,B:B))", "=SUM($B$3:XLOOKUP(6,A:A,B:B))"),
])
def test_reference_joined_to_a_function_shifts(formula, expected):
    assert _shift(formula, _rows(EditKind.INSERT, 1)) == expected


def test_insert_counts_formulas_joined_to_a_function(tmp_path):
    def build(wb, ws):
        for r in range(2, 7):
            ws.cell(row=r, column=1, value=r)
        ws["C1"] = "=SUM(A2:INDEX(A:A,6))"
        ws["C2"] = "=SUM(INDEX(A:A,2):A6)"

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 1}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert ws["C2"].value == "=SUM(A3:INDEX(A:A,6))"
    assert ws["C3"].value == "=SUM(INDEX(A:A,2):A7)"
    assert "2 formulas updated" in msg


def test_rename_reaches_a_reference_joined_to_a_function(tmp_path):
    def build(wb, ws):
        ws["A2"] = 1
        calc = wb.create_sheet("Calc")
        calc["A1"] = "=SUM(Data!A2:INDEX(Data!A:A,5))"

    f = _book(tmp_path, build)
    _write(f, [{"op": "rename_sheet", "old_name": "Data", "new_name": "New data"}])
    calc = openpyxl.load_workbook(f)["Calc"]
    assert calc["A1"].value == "=SUM('New data'!A2:INDEX('New data'!A:A,5))"


# ---------------------------------------------------------------------------
# Filter criteria follow their columns
# ---------------------------------------------------------------------------


def _filter_cols(af) -> dict[int, list[str]]:
    return {fc.colId: list(fc.filters.filter) for fc in af.filterColumn}


def test_sheet_filter_criterion_follows_a_column_move(tmp_path):
    def build(wb, ws):
        ws.append(list("ABCDEFG"))
        ws.auto_filter.ref = "A1:G5"
        ws.auto_filter.add_filter_column(3, ["keep"])

    f = _book(tmp_path, build)
    _write(f, [{"op": "move_columns", "sheet": "Data", "column": "B", "to": "F"}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert ws["C1"].value == "D"
    assert ws.auto_filter.ref == "A1:G5"
    assert _filter_cols(ws.auto_filter) == {2: ["keep"]}


def test_sheet_filter_criterion_on_a_deleted_column_is_dropped(tmp_path):
    def build(wb, ws):
        ws.append(list("ABCD"))
        ws.auto_filter.ref = "A1:D5"
        ws.auto_filter.add_filter_column(1, ["x"])
        ws.auto_filter.add_filter_column(3, ["y"])

    f = _book(tmp_path, build)
    msg = _write(f, [{"op": "delete_columns", "sheet": "Data", "column": "B"}])
    ws = openpyxl.load_workbook(f)["Data"]
    assert ws.auto_filter.ref == "A1:C5"
    assert _filter_cols(ws.auto_filter) == {2: ["y"]}
    assert "criterion on column B was removed with its column" in msg


def _sales(ws) -> None:
    from openpyxl.worksheet.filters import AutoFilter

    ws.append(["Name", "Price", "Qty"])
    for n in range(4):
        ws.append([f"n{n}", n, n * 2])
    table = Table(displayName="Sales", ref="A1:C5")
    table.autoFilter = AutoFilter(ref="A1:C5")
    table.autoFilter.add_filter_column(2, ["4"])
    ws.add_table(table)


@pytest.mark.parametrize("op, ref, criteria", [
    ({"op": "insert_columns", "column": "B"}, "A1:D5", {3: ["4"]}),
    ({"op": "delete_columns", "column": "A"}, "A1:B5", {1: ["4"]}),
    ({"op": "delete_columns", "column": "C"}, "A1:B5", {}),
    ({"op": "move_columns", "column": "C", "to": "A"}, "A1:C5", {0: ["4"]}),
])
def test_table_filter_criterion_follows_its_column(tmp_path, op, ref, criteria):
    f = _book(tmp_path, lambda wb, ws: _sales(ws))
    _write(f, [{**op, "sheet": "Data"}])
    table = openpyxl.load_workbook(f)["Data"].tables["Sales"]
    assert table.autoFilter.ref == ref
    assert _filter_cols(table.autoFilter) == criteria


def test_print_titles_are_counted_only_when_they_move(tmp_path):
    def build(wb, ws):
        ws["A1"] = "head"
        ws.print_title_rows = "1:2"

    f = _book(tmp_path, build)
    below = _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 5}])
    assert "print setting" not in below
    above = _write(f, [{"op": "insert_rows", "sheet": "Data", "row": 1}])
    assert "1 print setting updated" in above
    assert openpyxl.load_workbook(f)["Data"].print_title_rows == "$2:$3"


def test_refusals_name_the_op_once(tmp_path):
    f = tmp_path / "once.xlsx"
    msg = _write(f, [{"op": "insert_rows", "row": 2, "count": 0},
                     {"op": "move_rows", "row": 2, "count": 3, "to": 3}])
    assert "op #0 insert_rows: sheet 'Sheet': count must be 1 or more" in msg
    assert "op #1 move_rows: sheet 'Sheet': to (3) lies inside" in msg
    assert "insert_rows: insert_rows" not in msg and "move_rows: move_rows" not in msg


@pytest.mark.parametrize("formula, dcol, drow, expected", [
    ("=A1+$B$2+C$3+$D4", 1, 1, "=B2+$B$2+D$3+$D5"),
    ("=SUM(A:A)+SUM(2:3)", 2, 1, "=SUM(C:C)+SUM(3:4)"),
    ("=A1", -1, 0, "=#REF!"),
    ("=XFD1+A1048576", 1, 1, "=#REF!+#REF!"),
    ("=Other!B2+'My S'!C3", 0, -1, "=Other!B1+'My S'!C2"),
    ("=#REF!+Name+Table1[Col]", 3, 3, "=#REF!+Name+Table1[Col]"),
])
def test_copy_rule(formula, dcol, drow, expected):
    from formats.xlsx.refshift import copy_ref

    assert rewrite_refs(formula, lambda r: copy_ref(r, dcol, drow)) == expected
