"""Package-level rules of write_xlsx and the XLSX read: Strict workbooks are
refused before openpyxl can misread them, macro-enabled workbooks keep their
VBA project with its signature and ribbon, and every workbook is saved with
the content type its extension promises."""

import asyncio
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

openpyxl = pytest.importorskip("openpyxl")

from excel import handle_write_xlsx, read_xlsx

_SHEET_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_MACRO_MAIN = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
_TEMPLATE_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml"
_MACRO_TEMPLATE_MAIN = "application/vnd.ms-excel.template.macroEnabled.main+xml"
_SIGNATURE_TYPE = "application/vnd.ms-office.vbaProjectSignature"
_RIBBON_2010 = "http://schemas.microsoft.com/office/2007/relationships/ui/extensibility"
_VBA_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"VBAPROJECT" * 50
_SIG_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"SIGNATURE" * 20


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


def _rewrite(src: Path, dst: Path, edit) -> None:
    """Copy a package member by member, `edit(name, data) -> data | None`
    (None drops the member), then add what edit.extra holds."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = edit(info.filename, zin.read(info.filename))
            if data is not None:
                zout.writestr(info.filename, data)
        for name, data in getattr(edit, "extra", {}).items():
            zout.writestr(name, data)


def _macro_workbook(tmp_path: Path, name: str = "book.xlsm") -> Path:
    """A macro-enabled workbook as Excel lays one out: a VBA project with a
    signature (its own relationship part) and an Office 2010 ribbon."""
    base = tmp_path / "base.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "input"
    wb.save(base)
    target = tmp_path / name

    def edit(member: str, data: bytes) -> bytes:
        if member == "[Content_Types].xml":
            data = data.replace(_SHEET_MAIN.encode(), _MACRO_MAIN.encode())
            data = data.replace(b"</Types>", (
                '<Override PartName="/xl/vbaProject.bin" '
                'ContentType="application/vnd.ms-office.vbaProject"/>'
                f'<Override PartName="/xl/vbaProjectSignature.bin" ContentType="{_SIGNATURE_TYPE}"/>'
                '</Types>').encode())
        elif member == "xl/_rels/workbook.xml.rels":
            data = data.replace(b"</Relationships>", (
                b'<Relationship Id="rIdVba" Type="http://schemas.microsoft.com/office/'
                b'2006/relationships/vbaProject" Target="vbaProject.bin"/>'
                b"</Relationships>"))
        elif member == "_rels/.rels":
            data = data.replace(b"</Relationships>", (
                f'<Relationship Id="rIdUi" Type="{_RIBBON_2010}" '
                'Target="customUI/customUI14.xml"/></Relationships>').encode())
        return data

    edit.extra = {
        "xl/vbaProject.bin": _VBA_BYTES,
        "xl/vbaProjectSignature.bin": _SIG_BYTES,
        "xl/_rels/vbaProject.bin.rels": (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            b'relationships"><Relationship Id="rId1" Type="http://schemas.microsoft.com/'
            b'office/2006/relationships/vbaProjectSignature" '
            b'Target="vbaProjectSignature.bin"/></Relationships>'),
        "customUI/customUI14.xml": (
            b'<customUI xmlns="http://schemas.microsoft.com/office/2009/07/customui">'
            b"<ribbon/></customUI>"),
    }
    _rewrite(base, target, edit)
    return target


def _part(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name)


def _workbook_type(path: Path) -> str:
    types = _part(path, "[Content_Types].xml").decode()
    for ctype in (_MACRO_TEMPLATE_MAIN, _TEMPLATE_MAIN, _MACRO_MAIN, _SHEET_MAIN):
        if f'PartName="/xl/workbook.xml" ContentType="{ctype}"' in types:
            return ctype
    raise AssertionError(types)


def test_edited_xlsm_keeps_vba_signature_and_ribbon(tmp_path):
    book = _macro_workbook(tmp_path)
    _write(book, [{"op": "write_cells", "cells": [{"cell": "B2", "value": 42}]}])

    with zipfile.ZipFile(book) as zf:
        names = set(zf.namelist())
        assert zf.read("xl/vbaProject.bin") == _VBA_BYTES
        assert zf.read("xl/vbaProjectSignature.bin") == _SIG_BYTES
        assert b"vbaProjectSignature.bin" in zf.read("xl/_rels/vbaProject.bin.rels")
        root_rels = zf.read("_rels/.rels").decode()
        types = zf.read("[Content_Types].xml").decode()
        wb_rels = zf.read("xl/_rels/workbook.xml.rels").decode()
    assert "customUI/customUI14.xml" in names
    assert _RIBBON_2010 in root_rels and "customUI/customUI14.xml" in root_rels
    assert f'PartName="/xl/vbaProjectSignature.bin" ContentType="{_SIGNATURE_TYPE}"' in types
    assert "vbaProject" in wb_rels
    assert _workbook_type(book) == _MACRO_MAIN
    assert openpyxl.load_workbook(book).active["B2"].value == 42


def test_second_edit_of_an_xlsm_keeps_the_parts_again(tmp_path):
    book = _macro_workbook(tmp_path)
    _write(book, [{"op": "write_cells", "cells": [{"cell": "B2", "value": 1}]}])
    _write(book, [{"op": "write_cells", "cells": [{"cell": "B3", "value": 2}]}])
    assert _part(book, "xl/vbaProject.bin") == _VBA_BYTES
    assert _RIBBON_2010.encode() in _part(book, "_rels/.rels")
    assert _part(book, "_rels/.rels").count(b"customUI14.xml") == 1


@pytest.mark.parametrize("ext, ctype", [
    ("xlsx", _SHEET_MAIN),
    ("xlsm", _MACRO_MAIN),
    ("xltx", _TEMPLATE_MAIN),
    ("xltm", _MACRO_TEMPLATE_MAIN),
])
def test_new_workbook_carries_the_type_its_extension_promises(tmp_path, ext, ctype):
    book = tmp_path / f"new.{ext}"
    _write(book, [{"op": "write_cells", "cells": [{"cell": "A1", "value": "x"}]}])
    assert _workbook_type(book) == ctype
    assert openpyxl.load_workbook(book).active["A1"].value == "x"


def test_edited_template_stays_a_template(tmp_path):
    book = tmp_path / "plan.xltx"
    _write(book, [{"op": "write_cells", "cells": [{"cell": "A1", "value": "x"}]}])
    _write(book, [{"op": "write_cells", "cells": [{"cell": "A2", "value": "y"}]}])
    assert _workbook_type(book) == _TEMPLATE_MAIN
    assert openpyxl.load_workbook(book).template is True


def test_plain_xlsx_save_is_left_as_openpyxl_writes_it(tmp_path):
    book = tmp_path / "plain.xlsx"
    _write(book, [{"op": "write_cells", "cells": [{"cell": "A1", "value": 1}]}])
    assert _workbook_type(book) == _SHEET_MAIN
    with zipfile.ZipFile(book) as zf:
        assert not any(n.startswith("xl/vba") for n in zf.namelist())


def _strict(tmp_path: Path) -> Path:
    base = tmp_path / "base.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "keep me"
    wb.save(base)
    target = tmp_path / "strict.xlsx"

    def edit(member: str, data: bytes) -> bytes:
        if member == "xl/workbook.xml":
            data = data.replace(b"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                                b"http://purl.oclc.org/ooxml/spreadsheetml/main")
        elif member == "_rels/.rels":
            data = data.replace(
                b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
                b"http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument")
        return data

    _rewrite(base, target, edit)
    return target


def test_strict_workbook_is_refused_on_read(tmp_path):
    book = _strict(tmp_path)
    with pytest.raises(ValueError, match="Strict Open XML"):
        read_xlsx(str(book), None, 500)


def test_strict_workbook_is_refused_on_write_and_left_unchanged(tmp_path):
    book = _strict(tmp_path)
    before = book.read_bytes()
    # The worker child hands its refusal back as a RuntimeError.
    with pytest.raises(RuntimeError, match="Save it as a standard Excel workbook"):
        _write(book, [{"op": "create_sheet", "name": "New"}])
    assert book.read_bytes() == before


def test_dense_workbook_is_refused_before_an_edit(tmp_path, monkeypatch):
    import isolation

    book = tmp_path / "dense.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "BigData"
    for r in range(1, 201):
        for c in range(1, 21):
            wb.active.cell(row=r, column=c, value=f"cell-{r}-{c}")
    wb.save(book)
    before = book.read_bytes()
    monkeypatch.setattr(isolation, "worker_rss_budget_bytes", lambda: 512 * 1024)
    with pytest.raises(RuntimeError, match="too dense to edit"):
        _write(book, [{"op": "write_cells", "cells": [{"cell": "A1", "value": 1}]}])
    assert book.read_bytes() == before


# ---------------------------------------------------------------------------
# Operations that remove parts declare them, so the loss check stays quiet
# ---------------------------------------------------------------------------


def _parts_book(path: Path) -> None:
    from PIL import Image

    logo = path.parent / "logo.png"
    Image.new("RGB", (8, 4), (1, 2, 3)).save(logo)
    _write(path, [
        {"op": "write_cells", "start_cell": "A1", "data": [["m", "v"], ["a", 1], ["b", 2]]},
        {"op": "add_comment", "cell": "D4", "text": "first sheet note"},
        {"op": "create_sheet", "name": "Pics"},
        {"op": "add_image", "sheet": "Pics", "image_path": str(logo)},
        {"op": "add_chart", "sheet": "Pics", "data_range": "Sheet!A1:B3"},
        {"op": "add_comment", "sheet": "Pics", "cell": "C3", "text": "note"},
    ])


@pytest.mark.parametrize("ops", [
    [{"op": "delete_sheet", "name": "Pics"}],
    [{"op": "delete_sheet", "name": "Sheet"}],
    [{"op": "remove_comment", "sheet": "Pics", "all": True}],
    [{"op": "delete_rows", "sheet": "Pics", "row": 3}],
    [{"op": "remove_comment", "all": True}, {"op": "delete_sheet", "name": "Pics"}],
])
def test_removing_parts_on_purpose_is_not_a_loss(tmp_path, ops):
    from ooxml import validate

    # Another test may have imported the check module while the registry
    # was swapped out; registering again makes the loss check run here.
    validate.register_checks()
    book = tmp_path / "parts.xlsx"
    _parts_book(book)
    msg = _write(book, ops)
    assert msg.startswith("Workbook saved"), msg
    assert "Checks:" in msg and "parts_lost" not in msg, msg
