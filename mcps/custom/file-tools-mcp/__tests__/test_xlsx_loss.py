"""Round-trip loss: what saving through openpyxl would drop is refused
unless allow_loss, what it only simplifies is a warning. Each class has a
fixture that injects it into a workbook openpyxl wrote."""

import asyncio
import sys
import zipfile
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import checks
from excel import handle_write_xlsx
from formats.xlsx import formula_checks, loss

_SHEET = "xl/worksheets/sheet1.xml"
_EDIT = [{"op": "write_cells", "sheet": "Data", "start_cell": "A1", "values": [[1]]}]


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    async def _no_preview(path, filename=None):
        return None

    async def missing(path, *, out_dir, **kw):
        raise FileNotFoundError("libreoffice")

    monkeypatch.setattr("shared._resolve_path", _ident)
    monkeypatch.setattr("shared._push_preview", _no_preview)
    monkeypatch.setattr("shared._notify_file_written", _no_preview)
    monkeypatch.setattr("render_engine.recalc", missing)


def _write(path: Path, ops: list[dict], **extra) -> str:
    return asyncio.run(handle_write_xlsx({"path": str(path), "operations": ops, **extra}))


def _base(path: Path, *, image: bool = False, comment: bool = False) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = 1
    if image:
        from openpyxl.drawing.image import Image
        from PIL import Image as PILImage

        png = path.with_suffix(".png")
        PILImage.new("RGB", (4, 4), "red").save(png)
        ws.add_image(Image(str(png)), "C3")
    if comment:
        from openpyxl.comments import Comment

        ws["B2"].comment = Comment("hello", "me")
    wb.save(path)


def _inject(path: Path, *, add: dict[str, bytes] | None = None, edit: dict | None = None) -> None:
    """Rewrite the package with parts added and parts edited
    (``edit[name](data) -> data``)."""
    with zipfile.ZipFile(path) as zin:
        members = [(info.filename, zin.read(info.filename)) for info in zin.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in members:
            zout.writestr(name, (edit or {}).get(name, lambda d: d)(data))
        for name, data in (add or {}).items():
            zout.writestr(name, data)


def _ext(uri: str, body: str = "") -> callable:
    def edit(data: bytes) -> bytes:
        return data.replace(b"</worksheet>", f'<extLst><ext uri="{uri}">{body}</ext></extLst></worksheet>'.encode())
    return edit


def _shape(data: bytes) -> bytes:
    return data.replace(b"</wsDr>", b'<absoluteAnchor><pos x="0" y="0"/><ext cx="1" cy="1"/><sp><nvSpPr/>'
                                    b'</sp><clientData/></absoluteAnchor></wsDr>')


_X14_ONLY = ('<x14:conditionalFormattings xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
             '<x14:conditionalFormatting><x14:cfRule type="iconSet" priority="1" id="{11111111-0000-0000-0000-'
             '000000000001}"/></x14:conditionalFormatting></x14:conditionalFormattings>')

_REFUSED = {
    "slicers": dict(add={"xl/slicers/slicer1.xml": b"<slicers/>"}),
    "form controls": dict(add={"xl/ctrlProps/ctrlProp1.xml": b"<formControlPr/>"}),
    "threaded comments": dict(add={"xl/threadedComments/threadedComment1.xml": b"<ThreadedComments/>"}),
    "data connections": dict(add={"xl/connections.xml": b"<connections/>"}),
    "check boxes in cells": dict(add={"xl/featurePropertyBag/featurePropertyBag.xml": b"<FeaturePropertyBags/>"}),
    "Office add-in panes": dict(add={"xl/webextensions/webextension1.xml": b"<we:webextension/>"}),
    "sparklines": dict(edit={_SHEET: _ext("{05C60535-1F16-4fd2-B633-F4F36F0B64E0}")}),
    "conditional formats only newer Excel reads": dict(
        edit={_SHEET: _ext("{78C0D931-6437-407d-A8EE-F0AAD7539E65}", _X14_ONLY)}),
    "linked data types": dict(edit={_SHEET: lambda d: d.replace(b'<c r="A1"', b'<c r="A1" vm="1"')}),
    "macros (the workbook is named .xlsx": dict(add={"xl/vbaProject.bin": b"\xd0\xcf\x11\xe0"}),
    "timelines": dict(add={"xl/timelines/timeline1.xml": b"<timelines/>"}),
    "ActiveX controls": dict(add={"xl/activeX/activeX1.xml": b"<ocx/>"}),
    "embedded objects": dict(add={"xl/embeddings/oleObject1.bin": b"\0"}),
    "query tables": dict(add={"xl/queryTables/queryTable1.xml": b"<queryTable/>"}),
    "a data model (Power Pivot)": dict(add={"xl/model/item.data": b"\0"}),
    "charts of the newer kinds": dict(
        add={"xl/charts/chartEx1.xml": b"<cx:chartSpace/>"},
        edit={"[Content_Types].xml": lambda d: d.replace(b"</Types>", b'<Override PartName="/xl/charts/chartEx1.xml"'
                                                                     b' ContentType="application/vnd.ms-office.'
                                                                     b'chartex+xml"/></Types>')}),
    "dropdowns that read another sheet": dict(edit={_SHEET: _ext("{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}")}),
    "pivot tables that read another workbook": dict(add={"xl/pivotCache/pivotCacheDefinition1.xml": (
        b'<pivotCacheDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://'
        b'schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId1"><cacheSource type="worksheet">'
        b'<worksheetSource ref="A1:C9" sheet="Data" r:id="rId2"/></cacheSource></pivotCacheDefinition>')}),
}


@pytest.mark.parametrize("what", sorted(_REFUSED))
def test_content_the_save_would_drop_is_refused(tmp_path, what):
    path = tmp_path / "book.xlsx"
    _base(path)
    _inject(path, **_REFUSED[what])
    before = path.read_bytes()
    out = _write(path, _EDIT)
    assert out.startswith("Nothing saved:"), out
    assert "send the batch again with allow_loss: true" in out
    assert f"  - {what}" in out
    assert path.read_bytes() == before


def test_shapes_are_refused_with_the_sheet_they_are_on(tmp_path):
    path = tmp_path / "book.xlsx"
    _base(path, image=True)
    _inject(path, edit={"xl/drawings/drawing1.xml": _shape})
    out = _write(path, _EDIT)
    assert "  - shapes and text boxes (on Data)" in out


def test_allow_loss_saves_and_says_what_was_dropped(tmp_path):
    path = tmp_path / "book.xlsx"
    _base(path)
    _inject(path, add={"xl/slicers/slicer1.xml": b"<slicers/>"})
    out = _write(path, _EDIT, allow_loss=True)
    assert out.startswith("Workbook saved"), out
    assert "dropped as agreed (allow_loss): slicers" in out
    assert "parts_lost" not in out
    with zipfile.ZipFile(path) as zf:
        assert not any(n.startswith("xl/slicers/") for n in zf.namelist())


def test_what_is_only_simplified_is_a_warning(tmp_path):
    path = tmp_path / "book.xlsx"
    _base(path, comment=True)
    _inject(path, add={"xl/printerSettings/printerSettings1.bin": b"\0" * 8},
            edit={"xl/drawings/commentsDrawing1.vml": lambda d: d.replace(b"width:144px", b"width:200px")})
    out = _write(path, _EDIT)
    assert out.startswith("Workbook saved"), out
    assert "simplified by the save: printer driver settings (the page setup is kept)" in out
    assert "simplified by the save: comment box sizes (they return to the default) (on Data)" in out


def test_a_workbook_saved_by_this_tool_loses_nothing(tmp_path):
    path = tmp_path / "book.xlsx"
    _base(path, image=True, comment=True)
    out = _write(path, _EDIT)
    assert "simplified" not in out and "Nothing saved" not in out
    assert loss.scan(str(path)) == []


def test_check_document_lists_what_an_edit_would_lose(tmp_path):
    formula_checks.register_all()
    path = tmp_path / "book.xlsx"
    _base(path)
    _inject(path, add={"xl/slicers/slicer1.xml": b"<slicers/>"})
    with checks.CheckContext(str(path), "xlsx") as ctx:
        found = [f for f in checks.run_checks(ctx, phase=checks.DOCUMENT) if f.check == "xlsx.roundtrip_loss"]
    assert [(f.severity, f.message) for f in found] == [
        (checks.Severity.WARNING, "an edit with write_xlsx would drop slicers")]


@pytest.mark.filterwarnings("ignore:.*extension is not supported:UserWarning")
def test_a_data_bar_extension_and_an_unknown_one_are_warnings(tmp_path):
    path = tmp_path / "book.xlsx"
    _base(path)
    rule_id = "{22222222-0000-0000-0000-000000000002}"
    base = (f'<conditionalFormatting sqref="A1"><cfRule type="dataBar" priority="1"><dataBar><cfvo type="min"/>'
            f'<cfvo type="max"/><color rgb="FF638EC6"/></dataBar><extLst><ext uri="{{B025F937-C7B1-47D3-B67F-'
            f'A62EFF666E3E}}"><x14:id>{rule_id}</x14:id></ext></extLst></cfRule>'
            f'</conditionalFormatting>')
    x14 = (f'<ext uri="{{78C0D931-6437-407d-A8EE-F0AAD7539E65}}"><x14:conditionalFormattings>'
           f'<x14:conditionalFormatting><x14:cfRule type="dataBar" id="{rule_id}"/></x14:conditionalFormatting>'
           f'</x14:conditionalFormattings></ext><ext uri="{{99999999-AAAA-BBBB-CCCC-DDDDDDDDDDDD}}"/>')

    def edit(data: bytes) -> bytes:
        data = data.replace(b"<worksheet ", b'<worksheet xmlns:x14="http://schemas.microsoft.com/office/'
                                            b'spreadsheetml/2009/9/main" ', 1)
        data = data.replace(b"<pageMargins", base.encode() + b"<pageMargins")
        return data.replace(b"</worksheet>", f"<extLst>{x14}</extLst></worksheet>".encode())

    _inject(path, edit={_SHEET: edit})
    out = _write(path, _EDIT)
    assert out.startswith("Workbook saved"), out
    assert "simplified by the save: data bar and icon details (the rules stay, drawn simpler) (on Data)" in out
    assert "simplified by the save: features of newer Excel versions this tool does not know (on Data)" in out


def test_sharepoint_properties_are_dropped_with_a_warning(tmp_path):
    path = tmp_path / "book.xlsx"
    _base(path)
    props = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><ds:datastoreItem ds:itemID="{1}" '
             b'xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml"/>')
    _inject(path, add={
        "customXml/item1.xml": b'<?xml version="1.0"?><p:properties xmlns:p="urn:x"/>',
        "customXml/itemProps1.xml": props,
        "customXml/_rels/item1.xml.rels": (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.'
            b'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            b'openxmlformats.org/officeDocument/2006/relationships/customXmlProps" Target="itemProps1.xml"/>'
            b'</Relationships>'),
    }, edit={"[Content_Types].xml": lambda d: d.replace(b"</Types>", b'<Override PartName="/customXml/'
             b'itemProps1.xml" ContentType="application/vnd.openxmlformats-officedocument.customXmlProperties'
             b'+xml"/></Types>')})
    out = _write(path, _EDIT)
    assert out.startswith("Workbook saved"), out
    assert ("dropped by the save: SharePoint document properties (the library sets them again on upload)"
            in out)
    assert "parts_lost" not in out
