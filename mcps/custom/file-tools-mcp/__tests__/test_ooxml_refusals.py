"""What Office refuses though the schema allows it: a table named like a
cell reference or with a space, a number format past four sections or with
an unbalanced bracket or quote. Each reproduced with openpyxl, as our
writer would save it; nothing found on the corpus."""

import io
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

from checks import CheckContext, Severity
from ooxml import refusals

openpyxl = pytest.importorskip("openpyxl")


def _book(table_name=None, number_format=None) -> bytes:
    from openpyxl.worksheet.table import Table

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Item", "Cost"])
    sheet.append(["a", 1])
    if table_name is not None:
        sheet.add_table(Table(displayName=table_name, ref="A1:B2"))
    if number_format is not None:
        sheet["B2"].number_format = number_format
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _found(data: bytes):
    with CheckContext("", "xlsx", data=data) as ctx:
        return refusals.check_xlsx(ctx)


def _renamed(data: bytes, old: str, new: str) -> bytes:
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            blob = source.read(item)
            target.writestr(item, blob.replace(old.encode(), new.encode()) if item.filename.startswith("xl/tables/")
                            else blob)
    return out.getvalue()


@pytest.mark.parametrize("name, fragment", [
    ("A1", "reads as a cell reference"), ("XFD1048576", "reads as a cell reference"), ("R1C1", "cell reference"),
    ("1st", "with a space or a character"), ("x" * 300, "longer than 255"),
])
def test_a_table_name_excel_drops(name, fragment):
    """openpyxl saves each of these names as it is given."""
    found = _found(_book(table_name=name))
    assert len(found) == 1 and found[0].severity is Severity.ERROR and fragment in found[0].message
    assert found[0].fix.startswith("Rename it")


def test_a_table_name_with_a_space_from_another_writer():
    """openpyxl refuses a space itself; a file made elsewhere may hold one."""
    found = _found(_renamed(_book(table_name="Q1sales"), "Q1sales", "Q 1 sales"))
    assert len(found) == 1 and "with a space" in found[0].message


@pytest.mark.parametrize("code, fragment", [
    ("0;0;0;0;0", "5 sections"), ("[Red0.00", "a [ that is never closed"), ('0.00 "units', "quote that is never"),
    ("0]", "a ] without its ["),
])
def test_a_number_format_excel_drops(code, fragment):
    found = _found(_book(number_format=code))
    assert [f.severity for f in found] == [Severity.ERROR] and fragment in found[0].message
    assert found[0].excerpt == code


@pytest.mark.parametrize("name", ["Sales_2026", "_x", "Table1", "\\backslash", "A1B", "Πίνακας1", "Été_2026",
                                  "表1", "Πωλήσεις.Q1"])
def test_names_and_formats_excel_keeps(name):
    assert _found(_book(table_name=name, number_format='#,##0.00;[Red]-#,##0.00;"–";@')) == []


def test_sections_ignore_quoted_and_bracketed_semicolons():
    assert refusals.format_sections('"a;b";[$-408]0;0') == (3, None)
    assert refusals.format_sections(r"0\;0;0") == (2, None)


@pytest.mark.parametrize("path", sorted((_HERE / "corpus" / "shared").glob("*.xlsx")), ids=lambda p: p.name)
def test_the_corpus_trips_nothing(path):
    assert _found(path.read_bytes()) == []
