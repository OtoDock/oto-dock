"""write_xlsx's computed values: the recalculation in between the two
workers, the splice of values beside the formulas, the trust rules
(functions the engine lacks, outside data, values kept from before an
edit), the readback with values, and dynamic-array formulas.

The box tests stand a fake engine in for LibreOffice (it copies the staged
workbook and writes chosen values into the copy, as a recalculation would);
the office tests run the real one in the image.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import zipfile
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from excel import handle_write_xlsx
from formats.xlsx import computed, coverage, recalc
from ooxml import validate


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


def _sheet_xml(path: Path, part: str = "xl/worksheets/sheet1.xml") -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(part).decode()


class _Engine:
    """A stand-in recalculation: the staged copy with ``values`` written in,
    and a record of the calls."""

    def __init__(self, values: dict[str, dict[str, recalc.Value]]) -> None:
        self.values = values
        self.calls = 0

    async def __call__(self, path, *, out_dir, **kw):
        self.calls += 1
        out = os.path.join(out_dir, Path(path).stem + ".xlsx")
        shutil.copy(path, out)
        recalc.splice(out, self.values, {})
        return out


def _engine(monkeypatch, values) -> _Engine:
    engine = _Engine(values)
    monkeypatch.setattr("render_engine.recalc", engine)
    return engine


@pytest.fixture
def no_engine(monkeypatch):
    async def missing(path, *, out_dir, **kw):
        raise FileNotFoundError("libreoffice")

    monkeypatch.setattr("render_engine.recalc", missing)


_SUM_OPS = [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [[2], [3], ["=SUM(A1:A2)"]]}]


# ---------------------------------------------------------------------------
# The shape the splice relies on
# ---------------------------------------------------------------------------


def test_openpyxl_writes_a_formula_with_an_empty_value(tmp_path, no_engine):
    """The splice reads openpyxl's own output: a library bump that changes
    this shape must fail here, not corrupt workbooks."""
    path = tmp_path / "a.xlsx"
    _write(path, _SUM_OPS)
    assert '<c r="A3"><f>SUM(A1:A2)</f><v></v></c>' in _sheet_xml(path)


def test_a_cell_is_spliced_with_its_type(tmp_path):
    data = (b'<worksheet><sheetData><row r="1"><c r="A1" s="2"><f>B1/0</f><v></v></c>'
            b'<c r="B1"><f>"a&amp;b"</f><v/></c><c r="C1"><f>TRUE()</f></c><c r="D1"><v>4</v></c>'
            b'</row></sheetData></worksheet>')
    out = recalc.splice_part(data, {"A1": ("e", b"#DIV/0!"), "B1": ("str", b"a&amp;b"), "C1": ("b", b"1"),
                                    "D1": ("n", b"9")}, set())
    assert b'<c r="A1" s="2" t="e"><f>B1/0</f><v>#DIV/0!</v></c>' in out
    assert b'<c r="B1" t="str"><f>"a&amp;b"</f><v>a&amp;b</v></c>' in out
    assert b'<c r="C1" t="b"><f>TRUE()</f><v>1</v></c>' in out
    assert b'<c r="D1"><v>4</v></c>' in out  # a constant is never touched
    assert recalc.read_values(out) == {"A1": ("e", b"#DIV/0!"), "B1": ("str", b"a&amp;b"), "C1": ("b", b"1")}
    assert recalc.read_formulas(out) == {"A1": "B1/0", "B1": '"a&b"', "C1": "TRUE()"}


def test_libreoffice_output_is_read_with_prefixes_and_codes():
    data = (b'<x:worksheet xmlns:x="m"><x:sheetData><x:row r="3"><x:c r="A3" s="0" t="n">'
            b'<x:f aca="false">SUM(A1:A2)</x:f><x:v>5</x:v></x:c><x:c r="A4" t="e"><x:f>X()</x:f>'
            b'<x:v>Err:502</x:v></x:c><x:c r="A5" t="s"><x:f>Y()</x:f><x:v>0</x:v></x:c>'
            b'</x:row></x:sheetData></x:worksheet>')
    assert recalc.read_values(data) == {"A3": ("n", b"5")}  # an unknown error code and a shared string stay out


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def test_recalculated_values_are_saved_and_shown(tmp_path, monkeypatch):
    engine = _engine(monkeypatch, {"Sheet": {"A3": ("n", b"5")}})
    path = tmp_path / "a.xlsx"
    out = _write(path, _SUM_OPS)
    assert engine.calls == 1
    assert '<c r="A3"><f>SUM(A1:A2)</f><v>5</v></c>' in _sheet_xml(path)
    assert "Formulas: 1, computed: 1, errors: 0." in out
    assert "=SUM(A1:A2) → 5" in out


def test_without_an_engine_the_formulas_are_left_for_excel(tmp_path, no_engine):
    path = tmp_path / "a.xlsx"
    out = _write(path, _SUM_OPS)
    assert "Formulas: 1, computed: 0, errors: 0. Not recalculated (no calculation engine here)" in out
    assert "| 3 | =SUM(A1:A2) |" in out
    assert "<v></v>" in _sheet_xml(path)


def test_a_slow_calculation_is_said_in_plain_words(tmp_path, monkeypatch):
    async def slow(path, *, out_dir, **kw):
        await asyncio.sleep(1)

    monkeypatch.setattr("render_engine.recalc", slow)
    monkeypatch.setattr("batch.RENDER_BUDGET_S", 0.05)
    out = _write(tmp_path / "a.xlsx", _SUM_OPS)
    assert "Not recalculated (the calculation ran past 0.05 s and was skipped)" in out
    assert "render phase" not in out


def test_a_batch_without_formulas_is_not_recalculated(tmp_path, monkeypatch):
    engine = _engine(monkeypatch, {})
    path = tmp_path / "a.xlsx"
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [[1]]}])
    assert engine.calls == 0
    assert "Formulas:" not in out


def test_errors_are_counted_and_typed(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"A1": ("e", b"#DIV/0!")}})
    path = tmp_path / "a.xlsx"
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [["=1/0"]]}])
    assert "errors: 1" in out
    assert "=1/0 → #DIV/0!" in out
    assert '<c r="A1" t="e"><f>1/0</f><v>#DIV/0!</v></c>' in _sheet_xml(path)


def test_dates_show_as_dates(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"A1": ("n", b"46108")}})
    path = tmp_path / "a.xlsx"
    out = _write(path, [
        {"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [["=DATE(2026,3,27)"]]},
        {"op": "set_style", "sheet": "Sheet", "range": "A1", "number_format": "yyyy-mm-dd"},
    ])
    assert "=DATE(2026,3,27) → 2026-03-27" in out


def test_a_function_the_engine_lacks_withholds_its_value_and_its_readers(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"B1": ("e", b"#NAME?"), "B2": ("e", b"#NAME?"), "B3": ("n", b"4")}})
    path = tmp_path / "a.xlsx"
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
        [2, "=PERCENTOF(A1,A1:A2)"], [3, "=B1*2"], [4, "=A3"]]}])
    xml = _sheet_xml(path)
    assert "#NAME?" not in xml
    assert '<c r="B3"><f>A3</f><v>4</v></c>' in xml
    assert "Formulas: 3, computed: 1, errors: 0." in out
    assert "2 formulas use functions the calculation engine lacks" not in out
    assert "1 formulas use functions the calculation engine lacks (Sheet!B1)" in out


def _own_book(path: Path, formulas: dict[str, str]) -> None:
    """A workbook saved by someone else, holding formulas a batch may not
    write (the person's own)."""
    wb = openpyxl.Workbook()
    for coord, text in formulas.items():
        wb.active[coord] = text
    wb.active.title = "Sheet"
    wb.save(path)


def test_what_describes_the_server_is_never_saved(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"A1": ("str", b"file:///tmp/file-tools-work-x/staged.xlsx#$Sheet"),
                                    "A2": ("str", b"Linux 5.15"), "A3": ("e", b"#N/A"), "A4": ("n", b"3")}})
    path = tmp_path / "a.xlsx"
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
        ['=CELL("filename")'], ['=INFO("osversion")'], ['=INFO("directory")'], ["=1+2"]]}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {"A4": ("n", b"3")}
    assert "3 formulas ask about the computer that calculates them (CELL, INFO: Sheet!A1, Sheet!A2, Sheet!A3)" in out
    assert "file-tools-work" not in out and "Linux" not in out and "xlsx.formula_error" not in out


def test_outside_data_refuses_the_recalculation(tmp_path, monkeypatch):
    engine = _engine(monkeypatch, {"Sheet": {"A2": ("n", b"1")}})
    path = tmp_path / "a.xlsx"
    _own_book(path, {"A1": '=RTD("prog.id",,"topic")'})
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A2", "values": [["=1+1"]]}])
    assert engine.calls == 0
    assert "This workbook reads outside data (RTD in Sheet!A1), so it was not recalculated and nothing " \
           "was fetched" in out
    assert "Not recalculated" not in out  # said once, as the warning


_FILE_REFERENCES = [
    "='/data/secret/[book.xlsx]Sheet1'!A1",
    "='file:///data/secret/[book.xlsx]Sheet1'!A1",
    "='file:///data/secret/book.xlsx'#$Sheet1.A1",
    "=SUM('file:///data/secret/book.xlsx'#$Sheet1.A1:B2)",
    "=[book.xlsx]Sheet1!A1",
    "=[1]!Rate*2",
]


@pytest.mark.parametrize("where", ["cell", "rule", "validation", "name"])
@pytest.mark.parametrize("formula", _FILE_REFERENCES)
def test_a_reference_to_another_file_refuses_the_recalculation(tmp_path, monkeypatch, formula, where):
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.datavalidation import DataValidation

    engine = _engine(monkeypatch, {})
    path = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    if where == "cell":
        ws["A1"] = formula
    elif where == "rule":
        ws.conditional_formatting.add("A1", FormulaRule(formula=[formula[1:] + "=1"], fill=None))
    elif where == "validation":
        dv = DataValidation(type="list", formula1=formula[1:])
        dv.add("A1")
        ws.add_data_validation(dv)
    else:
        wb.defined_names["outside"] = DefinedName("outside", attr_text=formula[1:])
    wb.save(path)
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "B2", "values": [["=1+1"]]}])
    assert engine.calls == 0, out
    assert "This workbook reads outside data (a reference to another file in " in out


@pytest.mark.parametrize(("source", "outside"), [
    (b'<cacheSource type="external" connectionId="1"/>', True),
    (b'<cacheSource type="consolidation"><consolidation/></cacheSource>', True),
    (b'<cacheSource type="worksheet"><worksheetSource ref="A1:B3" sheet="Sheet"/></cacheSource>', False),
])
def test_a_pivot_over_another_source_refuses_the_recalculation(tmp_path, monkeypatch, source, outside):
    engine = _engine(monkeypatch, {})
    path = tmp_path / "a.xlsx"
    _own_book(path, {"A1": "=1+1"})
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("xl/pivotCache/pivotCacheDefinition1.xml",
                    b'<pivotCacheDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    + source + b"</pivotCacheDefinition>")
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "B2", "values": [["=1+2"]]}])
    assert engine.calls == (0 if outside else 1), out
    assert ("reads outside data (pivot tables over another source)" in out) is outside


@pytest.mark.parametrize("formula", ["=Table1[Col]", "=SUM('Q1:Q4'!A1)", '="[1]Sheet1!A1"', "='My Sheet'!A1"])
def test_what_only_looks_like_another_file_is_recalculated(tmp_path, monkeypatch, formula):
    engine = _engine(monkeypatch, {})
    path = tmp_path / "a.xlsx"
    _own_book(path, {"A1": formula})
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "B2", "values": [["=1+1"]]}])
    assert engine.calls == 1


def test_functions_the_engine_keeps_in_are_recalculated_but_never_trusted(tmp_path, monkeypatch):
    engine = _engine(monkeypatch, {"Sheet": {"A1": ("e", b"#N/A"), "A2": ("n", b"2"), "A3": ("n", b"3")}})
    path = tmp_path / "a.xlsx"
    _own_book(path, {"A1": '=_xlfn.WEBSERVICE("http://example.com/")', "B1": "A2"})
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A2", "values": [
        ["=INDIRECT(B1)"], ["=1+2"]]}])
    assert engine.calls == 1
    values = recalc.read_values(_sheet_xml(path).encode())
    assert values == {"A3": ("n", b"3")}  # INDIRECT may read A1, which is WEBSERVICE's
    assert "reads outside data" not in out
    assert "1 formulas use functions the calculation engine lacks (Sheet!A1)" in out


def test_a_mismatch_writes_no_value(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"A3": ("n", b"5")}})
    monkeypatch.setattr(recalc, "read_formulas", lambda data: {})
    path = tmp_path / "a.xlsx"
    out = _write(path, _SUM_OPS)
    assert "the formulas' values were not written" in out
    assert "<v></v>" in _sheet_xml(path)


# ---------------------------------------------------------------------------
# Values kept from before the edit
# ---------------------------------------------------------------------------


def _book_with_values(tmp_path, monkeypatch) -> Path:
    """A workbook saved with its values: A3 = A1+A2, B1 = 10 * 2 (reads
    nothing), C1 a function the engine lacks reading A1."""
    _engine(monkeypatch, {"Sheet": {"A3": ("n", b"5"), "B1": ("n", b"20"), "C1": ("n", b"7")}})
    path = tmp_path / "book.xlsx"
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
        [2, "=10*2", "=PERCENTOF(A1,A1:A2)"], [3], ["=A1+A2"]]}])
    # The engine lacks PERCENTOF, so C1 has no value: give it the one Excel
    # would have saved.
    recalc.splice(str(path), {"Sheet": {"C1": ("n", b"7")}}, {})
    return path


def test_a_formatting_edit_keeps_every_value(tmp_path, monkeypatch, no_engine):
    path = _book_with_values(tmp_path, monkeypatch)
    monkeypatch.setattr("render_engine.recalc", _no_engine_call)
    out = _write(path, [{"op": "set_style", "sheet": "Sheet", "range": "A1:C3", "bold": True}])
    values = recalc.read_values(_sheet_xml(path).encode())
    assert values == {"A3": ("n", b"5"), "B1": ("n", b"20"), "C1": ("n", b"7")}
    assert "kept from before this edit: 3" in out


async def _no_engine_call(path, *, out_dir, **kw):
    raise FileNotFoundError("libreoffice")


def test_a_changed_input_drops_the_values_that_read_it(tmp_path, monkeypatch):
    path = _book_with_values(tmp_path, monkeypatch)
    monkeypatch.setattr("render_engine.recalc", _no_engine_call)
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A2", "values": [[30]]}])
    values = recalc.read_values(_sheet_xml(path).encode())
    assert values == {"B1": ("n", b"20")}


def test_a_recalculation_keeps_values_for_functions_the_engine_lacks(tmp_path, monkeypatch):
    path = _book_with_values(tmp_path, monkeypatch)
    _engine(monkeypatch, {"Sheet": {"A3": ("n", b"5"), "B1": ("n", b"20"), "C1": ("e", b"#NAME?"),
                                    "D1": ("n", b"1")}})
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "D1", "values": [[1]]}])
    values = recalc.read_values(_sheet_xml(path).encode())
    assert values["C1"] == ("n", b"7")
    assert "computed: 2, kept from before this edit: 1" in out


def test_a_moved_formula_keeps_no_value(tmp_path, monkeypatch):
    path = _book_with_values(tmp_path, monkeypatch)
    monkeypatch.setattr("render_engine.recalc", _no_engine_call)
    _write(path, [{"op": "insert_rows", "sheet": "Sheet", "row": 1, "count": 1}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {}


def test_a_changed_name_drops_the_values_that_read_names(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"A2": ("n", b"2"), "B1": ("n", b"3")}})
    path = tmp_path / "book.xlsx"
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [[2, "=1+2"], ["=rate"]]},
                  {"op": "define_name", "name": "rate", "range": "Sheet!$A$1"}])
    monkeypatch.setattr("render_engine.recalc", _no_engine_call)
    _write(path, [{"op": "delete_name", "name": "rate"},
                  {"op": "define_name", "name": "rate", "range": "Sheet!$B$1"}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {"B1": ("n", b"3")}


def test_a_formula_reading_names_keeps_its_value_beside_a_lacking_function(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"C1": ("n", b"60"), "D1": ("e", b"#NAME?"), "E1": ("n", b"7")}})
    path = tmp_path / "a.xlsx"
    out = _write(path, [
        {"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
            [12, 5, "=Rate*Hours", "=PERCENTOF(A1:A2,B1:B2)", "=SUM(Sales[Units])", None, "Region", "Units"],
            [1, 2, None, None, None, None, "North", 3], [4, 3, None, None, None, None, "South", 4]]},
        {"op": "define_name", "name": "Rate", "range": "Sheet!$A$1"},
        {"op": "define_name", "name": "Hours", "range": "Sheet!$B$1"},
        {"op": "create_table", "sheet": "Sheet", "range": "G1:H3", "name": "Sales"},
    ])
    assert recalc.read_values(_sheet_xml(path).encode()) == {"C1": ("n", b"60"), "E1": ("n", b"7")}
    assert "Formulas: 3, computed: 2, errors: 0." in out


def test_a_name_reading_what_a_lacking_function_gives_is_withheld(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"A1": ("e", b"#NAME?"), "B1": ("n", b"4")}})
    path = tmp_path / "a.xlsx"
    _write(path, [
        {"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [["=PERCENTOF(C1,C1:C2)", "=Top*2"]]},
        {"op": "define_name", "name": "Top", "range": "Sheet!$A$1"},
    ])
    assert recalc.read_values(_sheet_xml(path).encode()) == {}


def test_a_name_kept_keeps_the_values_that_read_it(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"B1": ("n", b"24"), "C1": ("n", b"9")}})
    path = tmp_path / "book.xlsx"
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [[12, "=Rate*2", "=4+5"]]},
                  {"op": "define_name", "name": "Rate", "range": "Sheet!$A$1"}])
    monkeypatch.setattr("render_engine.recalc", _no_engine_call)
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "E5", "values": [[1]]},
                        {"op": "define_name", "name": "Other", "range": "Sheet!$E$5"}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {"B1": ("n", b"24"), "C1": ("n", b"9")}
    assert "kept from before this edit: 2" in out


def _scoped_book(path: Path, formula: str, local: str, value: bytes | None = None,
                 extra: dict[str, str] | None = None) -> Path:
    """Sheet "Data" (mixed case) with a sheet-scoped name and a workbook
    name of the same name pointing elsewhere, and ``formula`` in C1."""
    from openpyxl.workbook.defined_name import DefinedName

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    wb.create_sheet("Other")
    ws["A1"] = 10
    for coord, text in (extra or {}).items():
        ws[coord] = text
    ws["C1"] = formula
    ws.defined_names[local.split("=")[0]] = DefinedName(local.split("=")[0], attr_text=local.split("=")[1])
    wb.defined_names[local.split("=")[0]] = DefinedName(local.split("=")[0], attr_text="Other!$B$1")
    wb.save(path)
    if value is not None:
        recalc.splice(str(path), {"Data": {"C1": ("n", value)}}, {})
    return path


def test_a_sheet_scoped_name_is_found_whatever_the_case_of_its_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr("render_engine.recalc", _no_engine_call)
    path = _scoped_book(tmp_path / "a.xlsx", "=_xlfn.PERCENTOF(1,{1})*Rate", "Rate=Data!$A$1", b"10")
    out = _write(path, [{"op": "write_cells", "sheet": "Data", "start_cell": "A1", "values": [[50]]}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {}, out
    assert "kept from before this edit" not in out


def test_an_engine_value_behind_a_sheet_scoped_name_is_withheld(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Data": {"B1": ("e", b"#NAME?"), "C1": ("n", b"99")}})
    path = _scoped_book(tmp_path / "a.xlsx", "=Top*2", "Top=Data!$B$1",
                        extra={"B1": "=_xlfn.PERCENTOF(A1,A1:A2)"})
    out = _write(path, [{"op": "write_cells", "sheet": "Data", "start_cell": "E1", "values": [[1]]}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {}, out
    assert "Formulas: 2, computed: 0" in out


def _lacking_name_book(path: Path, also: bool) -> Path:
    from openpyxl.workbook.defined_name import DefinedName

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws["A1"], ws["B1"] = 1, 7
    ws["C1"] = "=Lk*2"
    if also:
        ws["D1"] = "=_xlfn.PERCENTOF(A1,B1)"
    wb.defined_names["Lk"] = DefinedName("Lk", attr_text="_xlfn.PERCENTOF(Sheet!$A$1,Sheet!$B$1)")
    wb.save(path)
    return path


@pytest.mark.parametrize("also", [False, True])
def test_a_name_calling_a_lacking_function_makes_its_readers_lacking(tmp_path, monkeypatch, also):
    _engine(monkeypatch, {"Sheet": {"C1": ("e", b"#NAME?"), "D1": ("e", b"#NAME?"), "E1": ("n", b"3")}})
    path = _lacking_name_book(tmp_path / "a.xlsx", also)
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "E1", "values": [["=1+2"]]}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {"E1": ("n", b"3")}, out
    assert "xlsx.formula_error" not in out and "errors: 0" in out
    assert "use functions the calculation engine lacks (Sheet!C1" in out


def test_names_and_tables_resolve_to_what_they_read():
    names = coverage.Names({(None, "rate"): "Sheet!$B$1", (None, "hours"): "Sheet!$C$1:$C$3", (None, "k"): "0.22",
                            (None, "rel"): "Sheet!B1", (None, "both"): "rate*hours", ("s2", "rate"): "S2!$A$1",
                            (None, "loop"): "loop"},
                           {"sales": ("Sheet", "E1:F9")})
    assert coverage.reads("rate*hours", "Sheet", names) == [("sheet", (1, 2, 1, 2)), ("sheet", (1, 3, 3, 3))]
    assert coverage.reads("both", "Sheet", names) == [("sheet", (1, 2, 1, 2)), ("sheet", (1, 3, 3, 3))]
    assert coverage.reads("k*2", "Sheet", names) == []
    assert coverage.reads("rate", "S2", names) == [("s2", (1, 1, 1, 1))]
    assert coverage.reads("SUM(Sales[Units])", "Sheet", names) == [("sheet", (1, 5, 9, 6))]
    for unresolved in ("rel", "loop", "nosuch", "[@Units]"):
        assert coverage.reads(unresolved, "Sheet", names) is None, unresolved
    stale = coverage.Names(names.defined, names.tables, frozenset({"rate"}))
    assert coverage.reads("rate*hours", "Sheet", stale) is None
    lookups = coverage.Names({(None, "lk"): "_xlfn.PERCENTOF(Sheet!$A$1,Sheet!$B$1)", (None, "outer"): "lk+1",
                              ("data", "local"): "_xlfn.REGEXTEST(Data!$A$1,\"a\")", (None, "local"): "1"})
    assert coverage.lacks("outer*2", "Sheet", lookups)
    assert coverage.lacks("local", "Data", lookups) and not coverage.lacks("local", "Other", lookups)
    assert not coverage.lacks("SUM(A1:A2)+rate", "Sheet", names)


# ---------------------------------------------------------------------------
# Array formulas that fill a range
# ---------------------------------------------------------------------------


def _array_book(path: Path) -> Path:
    """D1:D3 = {=A1:A3*2} as a file saved with its values: the anchor holds
    the formula, D2 and D3 plain values (as openpyxl keeps them)."""
    from openpyxl.worksheet.formula import ArrayFormula

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    for row, value in enumerate((1, 2, 3), start=1):
        ws.cell(row=row, column=1, value=value)
    ws["D1"] = ArrayFormula("D1:D3", "=A1:A3*2")
    ws["D2"], ws["D3"] = 4, 6
    ws["F1"] = "=D2+1"
    wb.save(path)
    recalc.splice(str(path), {"Sheet": {"D1": ("n", b"2"), "F1": ("n", b"5")}}, {})
    return path


def _plain(path: Path) -> dict[str, str]:
    """Each plain cell of column D and its stored value text (empty when
    it has none)."""
    out = {}
    for coord, _named, f, v in recalc.cells(_sheet_xml(path).encode()):
        if f is None and coord.startswith("D"):
            out[coord] = v.group("t").decode() if v is not None else ""
    return out


def test_an_array_formulas_cells_are_emptied_when_it_is_not_computed(tmp_path, no_engine):
    path = _array_book(tmp_path / "a.xlsx")
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A2", "values": [[10]]}])
    assert _plain(path) == {"D2": "", "D3": ""}
    assert recalc.read_values(_sheet_xml(path).encode()) == {}  # F1 reads D2, so it keeps nothing either
    assert "Formulas: 2, computed: 0" in out


def test_an_array_formulas_cells_keep_their_values_when_nothing_it_reads_changed(tmp_path, no_engine):
    path = _array_book(tmp_path / "a.xlsx")
    _write(path, [{"op": "set_style", "sheet": "Sheet", "range": "D1:D3", "bold": True}])
    assert _plain(path) == {"D2": "4", "D3": "6"}
    assert recalc.read_values(_sheet_xml(path).encode()) == {"D1": ("n", b"2"), "F1": ("n", b"5")}


def test_a_cell_the_batch_wrote_inside_an_array_range_is_kept(tmp_path, no_engine):
    path = _array_book(tmp_path / "a.xlsx")
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "cells": [{"cell": "A2", "value": 10},
                                                                    {"cell": "D3", "value": 99}]}])
    assert _plain(path) == {"D2": "", "D3": "99"}


# ---------------------------------------------------------------------------
# Dynamic arrays
# ---------------------------------------------------------------------------


def test_a_spilling_formula_is_saved_as_a_dynamic_array(tmp_path, no_engine):
    path = tmp_path / "a.xlsx"
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1",
                   "values": [[3], [1], [2], ["=SORT(A1:A3)"], ["=SUM(SORT(A1:A3))"]]}])
    xml = _sheet_xml(path)
    assert '<c r="A4" cm="1"><f t="array" ref="A4">_xlfn._xlws.SORT(A1:A3)</f><v></v></c>' in xml
    assert '<c r="A5"><f>SUM(_xlfn._xlws.SORT(A1:A3))</f>' in xml
    with zipfile.ZipFile(path) as zf:
        assert "xl/metadata.xml" in zf.namelist()
        assert b"sheetMetadata+xml" in zf.read("[Content_Types].xml")
        assert b"relationships/sheetMetadata" in zf.read("xl/_rels/workbook.xml.rels")
    from checks import CheckContext

    with CheckContext(str(path), "xlsx") as ctx:
        assert [f for f in validate.validate_package(ctx) if f.severity.value == "error"] == []


def test_a_second_edit_keeps_one_metadata_part(tmp_path, no_engine):
    path = tmp_path / "a.xlsx"
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1",
                   "values": [[3], [1], ["=SORT(A1:A2)"]]}])
    _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "B1", "values": [["=UNIQUE(A1:A2)"]]}])
    xml = _sheet_xml(path)
    assert '<c r="A3" cm="1">' in xml and '<c r="B1" cm="1">' in xml
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist().count("xl/metadata.xml") == 1


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def test_reads_names_its_boxes():
    assert coverage.reads("SUM(A1:B3)+'My Sheet'!C2+$D:$D", "S") == [
        ("s", (1, 1, 3, 2)), ("my sheet", (2, 3, 2, 3)), ("s", (1, 4, 1_048_576, 4))]
    assert coverage.reads("rate*2", "S") is None
    assert coverage.reads("OFFSET(A1,1,1)", "S") is None
    assert coverage.reads("Sheet1:Sheet3!A1", "S") is None


def test_spread_follows_readers_to_the_end_of_a_chain():
    formulas = {"S": {"A2": "A1", "A3": "A2", "B1": "C9"}}
    assert coverage.spread(formulas, cells=[("S", 1, 1)]) == {("S", "A2"), ("S", "A3")}
    assert coverage.spread(formulas) == set()
    chain = {"S": {f"A{n + 1}": f"A{n}" for n in range(1, 500)}, "T": {"B1": "SUM(A1:A3)"}}
    assert coverage.spread(chain, cells=[("S", 1, 1)]) == {("S", f"A{n}") for n in range(2, 501)}


def test_spread_follows_large_ranges_and_array_ranges():
    formulas = {"S": {"B1": "SUM(A1:A1000)", "C1": "B1*2", "D5": "SUM(E1:E100)", "F1": "D2", "G1": "F9"}}
    assert coverage.spread(formulas, cells=[("S", 700, 1)]) == {("S", "B1"), ("S", "C1")}
    # F1 reads D2, a cell of the array formula anchored at D1.
    arrays = {"S": {"D1": (1, 4, 3, 4)}}
    formulas["S"]["D1"] = "_xlfn.PERCENTOF(A1,A1:A2)"
    assert coverage.spread(formulas, formula_seeds=[("S", "D1")], arrays=arrays) == {("S", "D1"), ("S", "F1")}


def test_past_the_check_budget_only_large_readers_are_withheld(monkeypatch):
    monkeypatch.setattr(coverage, "CHECK_BUDGET", 0)
    formulas = {"S": {"B1": "SUM(A1:A1000)", "C1": "B1*2", "D1": "A2+1", "E1": "Z9"}}
    assert coverage.spread(formulas, cells=[("S", 9, 26)]) == {("S", "B1"), ("S", "C1"), ("S", "E1")}


def test_a_long_chain_from_a_lacking_function_withholds_only_the_chain(tmp_path, monkeypatch):
    chain = {"Sheet": {"C1": ("e", b"#NAME?"), "D1": ("n", b"5"),
                       **{f"E{n}": ("e", b"#NAME?") for n in range(1, 13)}}}
    _engine(monkeypatch, chain)
    path = tmp_path / "a.xlsx"
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
        [2, 3, "=PERCENTOF(A1,A1:A2)", "=SUM(A1:B1)", "=C1*2"],
        *[[None, None, None, None, f"=E{n - 1}+1"] for n in range(2, 13)]]}])
    assert "Formulas: 14, computed: 1, errors: 0." in out
    assert "=SUM(A1:B1) → 5" in out
    assert "1 formulas use functions the calculation engine lacks (Sheet!C1)" in out
    assert recalc.read_values(_sheet_xml(path).encode()) == {"D1": ("n", b"5")}


def test_the_readback_marker_never_reaches_a_result(tmp_path, no_engine):
    path = tmp_path / "a.xlsx"
    out = _write(path, _SUM_OPS)
    assert "\x00" not in out
    assert computed.strip_marks("a\x000\x00b") == "ab"


# ---------------------------------------------------------------------------
# The real engine
# ---------------------------------------------------------------------------


@pytest.mark.office
def test_the_function_table_matches_the_engine(tmp_path):
    import render_engine
    from formats.xlsx import functions

    names = coverage.probe_names()
    probe = tmp_path / "probe.xlsx"
    coverage.write_probe(str(probe), names)
    done = asyncio.run(render_engine.recalc(str(probe), out_dir=render_engine.output_folder(str(tmp_path), "r")))
    with zipfile.ZipFile(done) as zf:
        values = recalc.read_values(zf.read(next(iter(recalc.sheet_targets(zf).values()))))
    found = coverage.read_probe(values, names)
    assert found == functions.SUPPORTED, (
        f"the table is stale (gained {sorted(found - functions.SUPPORTED)}, lost "
        f"{sorted(functions.SUPPORTED - found)}): regenerate formats/xlsx/functions.py")


@pytest.mark.office
def test_the_engine_reaches_nothing_outside(tmp_path):
    """LibreOffice under the recalculation profile, given every way the
    contained functions can reach out (a literal or a computed URL, an
    external entity in FILTERXML's text, a DDE call and a DDE link, an
    INDIRECT target naming a file or a URL, a file and a FIFO, WEBSERVICE
    handed over by name or called through brackets, INDIRECT in a rule, a
    validation and a name) and every shape of a reference to another file
    the pre-scan refuses (a bracketed book in a path, a file URL, the
    'url'#$Sheet.A1 form, one inside a name): nothing connects to the
    listener, the canary files are never read (their access times stay at
    the epoch, while a read of our own moves them), the FIFO is never
    opened, and the secret appears nowhere in the output."""
    import socket
    import threading
    import time

    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.datavalidation import DataValidation

    import render_engine
    from formats.xlsx.formulas import ENGINE_CONTAINED

    assert ENGINE_CONTAINED == {"WEBSERVICE", "FILTERXML", "DDE", "INDIRECT"}  # what this test proves
    secret = "CANARY-7f3a9"
    canary = tmp_path / "canary.txt"
    canary.write_text(secret)
    other = openpyxl.Workbook()
    other.active.title = "Sheet1"
    other.active["A1"] = secret
    book_canary = tmp_path / "canary.xlsx"
    other.save(book_canary)
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    shelved = shelf / "book.xlsx"
    other.save(shelved)
    canaries = (canary, book_canary, shelved)
    for path in canaries:
        os.utime(path, (0, path.stat().st_mtime))
    fifo = tmp_path / "fifo.xml"
    os.mkfifo(fifo)
    finished = threading.Event()
    opened: list[str] = []

    def watch_fifo():
        fd = os.open(fifo, os.O_WRONLY)  # returns once something opens it for reading
        if not finished.is_set():
            opened.append("fifo")
        os.close(fd)

    hits: list[bytes] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.settimeout(0.2)
    url = f"http://127.0.0.1:{server.getsockname()[1]}"

    def serve():
        while not finished.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            hits.append(conn.recv(200))
            conn.close()

    for target in (watch_fifo, serve):
        threading.Thread(target=target, daemon=True).start()
    entity = '"<!DOCTYPE x [<!ENTITY e SYSTEM ""{}"">]><x>&e;</x>","//x"'
    formulas = {
        "A1": f'=_xlfn.WEBSERVICE("{url}/ws")',
        "A2": "=_xlfn.FILTERXML(" + entity.format(f"file://{canary}") + ")",
        "A3": "=_xlfn.FILTERXML(" + entity.format(f"{url}/entity") + ")",
        "A4": "=_xlfn.FILTERXML(" + entity.format(f"file://{fifo}") + ")",
        "A5": f'=DDE("soffice","file://{canary}","Sheet1.A1")',
        "A6": f'=soffice|"file://{canary}"!A1',
        "A7": "=INDIRECT(B7)", "B7": f"'file://{canary}'#$Sheet1.A1",
        "A8": "=INDIRECT(B8)", "B8": f"'{url}/book.ods'#$Sheet1.A1",
        "A9": "=INDIRECT(B9)", "B9": f"'file://{fifo}'#$Sheet1.A1",
        "A10": "=_xlfn.WEBSERVICE(B10)", "B10": f"{url}/computed",
        "A11": "=_xlfn.MAP(B11:B12,_xlfn._xleta.WEBSERVICE)", "B11": f"{url}/map",
        "A13": f'=(_xlfn.WEBSERVICE)("{url}/bare")',
        "A14": f"='{shelf}/[book.xlsx]Sheet1'!A1",
        "A15": f"='file://{shelf}/[book.xlsx]Sheet1'!A1",
        "A16": f"='file://{book_canary}'#$Sheet1.A1",
        "A17": f"=SUM('file://{book_canary}'#$Sheet1.A1:B2)",
        "A18": "=outside", "A19": "=indirect_name", "B20": f"'file://{book_canary}'#$Sheet1.A1",
    }
    wb = openpyxl.Workbook()
    for coord, text in formulas.items():
        wb.active[coord] = text
    ws = wb.active
    wb.defined_names["outside"] = DefinedName("outside", attr_text=f"'file://{book_canary}'#$Sheet1.A1")
    wb.defined_names["indirect_name"] = DefinedName("indirect_name", attr_text="INDIRECT(Sheet!$B$20)")
    ws.conditional_formatting.add("A20", FormulaRule(formula=['INDIRECT($B$20)="x"'], fill=None))
    ws.conditional_formatting.add("A21", FormulaRule(formula=[f'INDIRECT("{url}/rule")="x"'], fill=None))
    rule_list = DataValidation(type="list", formula1="INDIRECT($B$20)")
    rule_list.add("A22")
    ws.add_data_validation(rule_list)
    book = tmp_path / "probe.xlsx"
    wb.save(book)
    try:
        done = asyncio.run(render_engine.recalc(str(book), out_dir=render_engine.output_folder(str(tmp_path), "r")))
        time.sleep(1.0)
    finally:
        finished.set()
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
        server.close()
    with zipfile.ZipFile(done) as zf:
        output = b"".join(zf.read(name) for name in zf.namelist())
    assert hits == []
    assert opened == []
    assert [path.name for path in canaries if path.stat().st_atime != 0] == []
    assert secret.encode() not in output
    for path in canaries:
        path.read_bytes()
    assert [path.name for path in canaries if path.stat().st_atime == 0] == [], "a canary cannot see a read here"


@pytest.mark.office
def test_networkdays_intl_gets_a_computed_value(tmp_path):
    """Stored bare, as Excel stores it, the engine computes it: the working
    days of January 2026 (22) and the 10th working day after New Year's day
    (2026-01-15)."""
    path = tmp_path / "days.xlsx"
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
        ["2026-01-01", "2026-01-31", "=NETWORKDAYS.INTL(A1,B1,1)", "=WORKDAY.INTL(A1,10,1)"]]}])
    values = recalc.read_values(_sheet_xml(path).encode())
    assert values.get("C1") == ("n", b"22") and values.get("D1") == ("n", b"46037"), out
    assert "NETWORKDAYS.INTL(" in _sheet_xml(path) and "_xlfn.NETWORKDAYS" not in _sheet_xml(path)
    assert "lacks" not in out


@pytest.mark.office
def test_a_real_chain_withholds_only_what_reads_a_lacking_function(tmp_path):
    path = tmp_path / "a.xlsx"
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
        [2, 3, "=PERCENTOF(A1,A1:A2)", "=SUM(A1:B1)", "=C1*2"],
        *[[None, None, None, None, f"=E{n - 1}+1"] for n in range(2, 13)]]}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {"D1": ("n", b"5")}
    assert "Formulas: 14, computed: 1, errors: 0." in out


@pytest.mark.office
def test_a_real_array_formula_fills_its_range(tmp_path):
    path = _array_book(tmp_path / "a.xlsx")
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "A2", "values": [[10]]}])
    assert _plain(path) == {"D2": "20", "D3": "6"}
    assert recalc.read_values(_sheet_xml(path).encode()) == {"D1": ("n", b"2"), "F1": ("n", b"21")}
    assert "Formulas: 2, computed: 2, errors: 0." in out


@pytest.mark.office
def test_a_real_name_calling_a_lacking_function_saves_no_error(tmp_path):
    path = _lacking_name_book(tmp_path / "a.xlsx", also=True)
    out = _write(path, [{"op": "write_cells", "sheet": "Sheet", "start_cell": "E1", "values": [["=1+2"]]}])
    assert recalc.read_values(_sheet_xml(path).encode()) == {"E1": ("n", b"3")}, out
    assert "xlsx.formula_error" not in out
    assert "Formulas: 3, computed: 1, errors: 0." in out


@pytest.mark.office
def test_a_real_recalculation(tmp_path):
    path = tmp_path / "a.xlsx"
    out = _write(path, [
        {"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [
            [2, "=SUM(A1:A2)", "=A1/0", '="n="&A1', "=A1>1", "=DATE(2026,3,27)", "=PERCENTOF(A1,A1:A2)"],
            [3]]},
        {"op": "set_style", "sheet": "Sheet", "range": "F1", "number_format": "yyyy-mm-dd"},
    ])
    values = recalc.read_values(_sheet_xml(path).encode())
    assert values == {"B1": ("n", b"5"), "C1": ("e", b"#DIV/0!"), "D1": ("str", b"n=2"), "E1": ("b", b"1"),
                      "F1": ("n", b"46108")}
    assert "Formulas: 6, computed: 5, errors: 1." in out
    assert "=SUM(A1:A2) → 5" in out and "=DATE(2026,3,27) → 2026-03-27" in out


def test_values_show_in_their_number_format(tmp_path, monkeypatch):
    _engine(monkeypatch, {"Sheet": {"A3": ("n", b"0.300319488817891"), "B3": ("n", b"1234567")}})
    path = tmp_path / "a.xlsx"
    out = _write(path, [
        {"op": "write_cells", "sheet": "Sheet", "start_cell": "A1", "values": [[1, 1], [2, 2], ["=A1/A2", "=B1*B2"]]},
        {"op": "set_style", "sheet": "Sheet", "range": "A3", "number_format": "0.0%"},
        {"op": "set_style", "sheet": "Sheet", "range": "B3", "number_format": "$#,##0"},
    ])
    assert "=A1/A2 → 30.0%" in out and "=B1*B2 → $1,234,567" in out


# ---------------------------------------------------------------------------
# The engine that computed the values
# ---------------------------------------------------------------------------

COLLABORA_APP = "Collabora_Office/26.04.5.1$Linux_X86_64 LibreOffice_project/22ce52fb5173821dd90782638afcbfb6b6d7cb40"


def test_the_table_follows_the_engine_the_file_names():
    from formats.xlsx import functions

    assert functions.supported_by(COLLABORA_APP) is functions.COLLABORA
    assert functions.supported_by("LibreOffice/25.2.3.2$Linux_X86_64 LibreOffice_project/x") is functions.SUPPORTED
    for other in ("Collabora_Office/24.04.1.1$Linux", "LibreOffice/7.4.7.2$Linux", "LibreOffice/6.0.7.3$Linux",
                  "Microsoft Excel", "", None):
        assert functions.supported_by(other) is functions.EVERY_ENGINE
    assert "TEXTBEFORE" in functions.COLLABORA and "TEXTBEFORE" not in functions.EVERY_ENGINE
    assert functions.SUPPORTED <= functions.COLLABORA


def _computed_book(tmp_path, application: str, values: dict[str, str]) -> str:
    """A workbook as an engine saves it after recalculating: each formula
    with the value given, and the engine's name."""
    error = ' t="e"'
    cells = "".join(f'<c r="{coord}"{error if v.startswith("#") else ""}><f>X</f><v>{v}</v></c>'
                    for coord, v in values.items())
    path = tmp_path / "computed.xlsx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                    'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                    '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                    '<sheet name="Sheet" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
                    f'2006/main"><sheetData><row r="1">{cells}</row></sheetData></worksheet>')
        zf.writestr("docProps/app.xml", '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
                    f'extended-properties"><Application>{application}</Application></Properties>')
    return str(path)


def test_a_collabora_value_of_a_function_only_collabora_has_is_written(tmp_path):
    import openpyxl

    staged = tmp_path / "staged.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = '=_xlfn.TEXTBEFORE("20-x","-")'
    book.active["B1"] = "=A1*2"
    book.save(staged)
    for application, written in ((COLLABORA_APP, {"A1", "B1"}), ("LibreOffice/25.2.3.2$Linux", set())):
        book.save(staged)
        done = recalc.fill(str(staged), expected={"Sheet": 2},
                           computed=_computed_book(tmp_path, application, {"A1": "20", "B1": "40"}))
        assert {coord for (_title, coord) in done.values} == written, application


def test_a_name_error_where_the_table_says_the_engine_computes_is_not_trusted(tmp_path):
    import openpyxl

    staged = tmp_path / "staged.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = "=SUM(1,2)"
    book.active["B1"] = "=A1*2"
    book.active["C1"] = "=1+1"
    book.save(staged)
    done = recalc.fill(str(staged), expected={"Sheet": 3},
                       computed=_computed_book(tmp_path, COLLABORA_APP, {"A1": "#NAME?", "B1": "#NAME?", "C1": "2"}))
    assert {coord for (_title, coord) in done.values} == {"C1"}
    assert done.unsupported and not done.errors


def test_a_name_defined_nowhere_stays_an_error(tmp_path):
    """Excel answers #NAME? for a name the workbook defines nowhere: that
    is the file's error, not a function the engine lacks."""
    import openpyxl

    staged = tmp_path / "staged.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = "=Totl*2"
    book.active["B1"] = "=SUM(Totl,1)"
    book.save(staged)
    done = recalc.fill(str(staged), expected={"Sheet": 2},
                       computed=_computed_book(tmp_path, COLLABORA_APP, {"A1": "#NAME?", "B1": "#NAME?"}))
    assert {coord for (_title, coord) in done.values} == {"A1", "B1"}
    assert sorted(place for place, _ in done.errors) == ["Sheet!A1", "Sheet!B1"] and not done.unsupported
    book = openpyxl.Workbook()
    book.active["C1"] = "=SUM(1,2)"
    book.save(staged)
    done = recalc.fill(str(staged), expected={"Sheet": 1},
                       computed=_computed_book(tmp_path, COLLABORA_APP, {"C1": "#NAME?"}))
    assert not done.values and not done.errors and done.unsupported == ["Sheet!C1"]
