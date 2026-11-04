"""The temporary copy a renderer reads: cached formula values dropped, A4
on a sheet with no paper, every other byte kept, and anything unexpected
refused so the caller renders without a copy."""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ooxml import render_copy

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _rewrite(body: str, *, drop_cached=True, a4=False, root=f'<worksheet xmlns="{MAIN}">', end="</worksheet>"):
    data, dropped, papered = render_copy.rewrite_sheet((root + body + end).encode(), drop_cached=drop_cached, a4=a4)
    return data.decode(), dropped, papered


def test_a_cached_value_after_a_formula_is_dropped_and_every_other_value_kept():
    out, dropped, _ = _rewrite(
        '<sheetData><row r="1"><c r="A1"><v>1</v></c><c r="B1" t="str"><f>A1&amp;"x"</f><v>1x</v></c>'
        '<c r="C1"><f t="shared" ref="C1:C2" si="0">A1*2</f><v>2</v></c></row>'
        '<row r="2"><c r="C2"><f t="shared" si="0"/><v>4</v></c><c r="D2" t="e"><f>1/0</f><v/></c>'
        '<c r="E2" t="inlineStr"><is><t>kept</t></is></c></row></sheetData>')
    assert dropped == 4
    assert '<c r="A1"><v>1</v></c>' in out and '<c r="B1" t="str"><f>A1&amp;"x"</f></c>' in out
    assert '<f t="shared" si="0"/></c>' in out and '<f>1/0</f></c>' in out and "<is><t>kept</t></is>" in out


def test_a_data_table_and_the_cells_of_an_array_keep_their_values():
    out, dropped, _ = _rewrite(
        '<sheetData><row r="1"><c r="A1"><f t="array" ref="A1:A2">ROW(1:2)</f><v>1</v></c>'
        '<c r="B1"><f t="dataTable" ref="B1:B2" dt2D="0" dtr="0" r1="A1"/><v>7</v></c></row>'
        '<row r="2"><c r="A2"><v>2</v></c></row></sheetData>')
    assert dropped == 1 and '<v>7</v>' in out and '<c r="A2"><v>2</v></c>' in out


def test_a_prefixed_worksheet_is_rewritten_with_its_prefix():
    out, dropped, papered = _rewrite(
        '<x:sheetData><x:row r="1"><x:c r="A1"><x:f>1+1</x:f><x:v>9</x:v></x:c></x:row></x:sheetData>'
        '<x:pageMargins left="1" right="1" top="1" bottom="1" header="0" footer="0"/>',
        a4=True, root=f'<x:worksheet xmlns:x="{MAIN}">', end="</x:worksheet>")
    assert dropped == 1 and papered == 1
    assert "<x:f>1+1</x:f></x:c>" in out and out.endswith('<x:pageSetup paperSize="9"/></x:worksheet>')


@pytest.mark.parametrize("body, expected", [
    ('<pageMargins left="1" right="1" top="1" bottom="1" header="0" footer="0"/><pageSetup orientation="landscape"/>',
     '<pageSetup orientation="landscape"/></worksheet>'),
    ('<pageSetup paperSize="1"/>', '<pageSetup paperSize="1"/>'),
    ('<pageSetup paperSize=""/>', '<pageSetup paperSize=""/>'),
    ('<pageMargins left="1" right="1" top="1" bottom="1" header="0" footer="0"/><headerFooter/><drawing r:id="rId1" '
     'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>',
     '<pageSetup paperSize="9"/><headerFooter/>'),
    ('<sheetData/><tableParts count="0"/>', '<sheetData/><pageSetup paperSize="9"/><tableParts count="0"/>'),
])
def test_a4_is_given_only_to_a_sheet_with_no_page_setup(body, expected):
    """A page setup that names no paper is Letter by the standard, in both
    engines: it is left alone."""
    out, _, _ = _rewrite(body, drop_cached=False, a4=True)
    assert expected in out and out.count("pageSetup") == 1


def test_a_chartsheet_with_no_page_setup_gets_a4():
    out, dropped, papered = _rewrite('<sheetPr/><sheetViews><sheetView workbookViewId="0"/></sheetViews>'
                                     '<drawing xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                                     'relationships" r:id="rId1"/>', a4=True,
                                     root=f'<chartsheet xmlns="{MAIN}">', end="</chartsheet>")
    assert papered == 1 and '</sheetViews><pageSetup paperSize="9"/><drawing' in out


def test_an_attribute_value_holding_a_bracket_does_not_end_the_tag():
    data = b'<v a=">">1</v>'
    assert render_copy.tag_end(data, 0) == data.index(b">1") + 1


@pytest.mark.parametrize("data", [
    b'<!DOCTYPE worksheet [<!ENTITY e "x">]><worksheet xmlns="' + MAIN.encode() + b'"/>',
    '<?xml version="1.0" encoding="UTF-16"?><worksheet/>'.encode("utf-16"),
    b'<?xml version="1.0" encoding="windows-1253"?><worksheet xmlns="' + MAIN.encode() + b'"/>',
    b'<worksheet xmlns="' + MAIN.encode() + b'"><sheetData>',
    b'<dialogsheet xmlns="' + MAIN.encode() + b'"/>',
])
def test_anything_unexpected_is_refused(data):
    with pytest.raises(render_copy.CopyRefused):
        render_copy.rewrite_sheet(data, drop_cached=True, a4=True)


def _package(path: Path, sheets: dict[str, str], *, rels_target_prefix="") -> Path:
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        f'{kind}" Target="{rels_target_prefix}{name}"/>'
        for i, (name, kind) in enumerate(
            [("worksheets/sheet1.xml", "worksheet"), ("chartsheets/sheet1.xml", "chartsheet"),
             ("macrosheets/sheet1.xml", "xlMacrosheet")], start=1))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/officeDocument" Target="/xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/_rels/workbook.xml.rels",
                    f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
                    '</Relationships>')
        for name, xml in sheets.items():
            zf.writestr(f"xl/{name}", xml)
        zf.writestr("xl/media/image1.png", b"\x89PNG keep me")
    return path


def test_the_package_copy_rewrites_only_worksheets(tmp_path):
    cell = '<sheetData><row r="1"><c r="A1"><f>1+1</f><v>2</v></c></row></sheetData>'
    sheets = {
        "worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}">{cell}</worksheet>',
        "chartsheets/sheet1.xml": f'<chartsheet xmlns="{MAIN}"><sheetPr/><pageSetup paperSize="1"/></chartsheet>',
        "macrosheets/sheet1.xml": f'<worksheet xmlns="{MAIN}">{cell}</worksheet>',
    }
    for prefix in ("", "/xl/"):
        src = _package(tmp_path / f"in{len(prefix)}.xlsx", sheets, rels_target_prefix=prefix)
        dst = tmp_path / f"out{len(prefix)}.xlsx"
        counts = render_copy.prepare(str(src), str(dst), drop_cached=True, a4=True)
        assert counts == {"sheets": 2, "values_dropped": 1, "paper_set": 1, "links_removed": 0}
        with zipfile.ZipFile(src) as before, zipfile.ZipFile(dst) as after:
            assert before.namelist() == after.namelist()
            for name in before.namelist():
                if name != "xl/worksheets/sheet1.xml":
                    assert before.read(name) == after.read(name), name
            assert b"<v>2</v>" not in after.read("xl/worksheets/sheet1.xml")


def test_an_openpyxl_workbook_survives_the_copy(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active["A1"] = 3
    book.active["A2"] = "=A1*2"
    book.create_sheet("Ελληνικά")["B2"] = "=SUM(Sheet!A1:A2)"
    src = tmp_path / "b.xlsx"
    book.save(src)
    dst = tmp_path / "c.xlsx"
    counts = render_copy.prepare(str(src), str(dst), drop_cached=True, a4=True)
    assert counts["sheets"] == 2 and counts["paper_set"] == 2
    again = openpyxl.load_workbook(io.BytesIO(dst.read_bytes()))
    assert again["Sheet"]["A2"].value == "=A1*2" and again["Ελληνικά"]["B2"].value == "=SUM(Sheet!A1:A2)"
    assert all(ws.page_setup.paperSize in (9, "9") for ws in again.worksheets)


def test_a_package_without_a_workbook_or_a_link_is_refused(tmp_path):
    src = tmp_path / "x.xlsx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    with pytest.raises(render_copy.CopyRefused, match="no workbook"):
        render_copy.prepare(str(src), str(tmp_path / "y.xlsx"), drop_cached=True, a4=False)
    link = tmp_path / "link.xlsx"
    link.symlink_to(src)
    with pytest.raises(OSError):
        render_copy.prepare(str(link), str(tmp_path / "z.xlsx"), drop_cached=True, a4=False)
    assert not (tmp_path / "y.xlsx").exists() and not (tmp_path / "y.xlsx.part").exists()


REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def _rels(*items: tuple[str, str, bool]) -> bytes:
    external = ' TargetMode="External"'
    body = "".join(f'<Relationship Id="rId{i}" Type="{REL}{kind}" Target="{target}"{external if outside else ""}/>'
                   for i, (kind, target, outside) in enumerate(items, start=1))
    return (f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}'
            "</Relationships>").encode()


def test_with_no_source_part_every_relationship_leaving_the_package_goes():
    data = _rels(("image", "http://10.0.0.5/a.png", True), ("hyperlink", "http://example.com/", True),
                 ("externalLinkPath", "file:///book.xlsx", True), ("image", "media/image1.png", False),
                 ("oleObject", "http://10.0.0.5/o.bin", True), ("attachedTemplate", "file:///t.dotx", True),
                 ("subDocument", "http://10.0.0.5/s.docx", True), ("video", "http://10.0.0.5/v.mp4", True),
                 ("aFChunk", "http://10.0.0.5/c.htm", True))
    out, removed = render_copy.unlink_relationships(data)
    assert removed == 8
    assert b"10.0.0.5" not in out and b"t.dotx" not in out and b"book.xlsx" not in out
    assert b"http://example.com/" not in out and b"media/image1.png" in out


def test_a_relationship_written_with_an_end_tag_goes_whole():
    data = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="' + REL.encode() + b'image" Target="http://h/x.png" TargetMode="External">'
            b'</Relationship><Relationship Id="rId2" Type="' + REL.encode() + b'styles" Target="styles.xml"/>'
            b'</Relationships>')
    out, removed = render_copy.unlink_relationships(data)
    assert removed == 1 and b"http://h" not in out and b'Id="rId2"' in out and out.count(b"</Relationship>") == 0


def test_a_document_copy_changes_only_its_relationship_parts(tmp_path):
    src = tmp_path / "linked.docx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/_rels/document.xml.rels", _rels(("image", "http://10.0.0.5/a.png", True)))
    dst = tmp_path / "copy.docx"
    counts = render_copy.prepare(str(src), str(dst), unlink=True)
    assert counts["links_removed"] == 1 and counts["sheets"] == 0
    with zipfile.ZipFile(dst) as zf:
        assert b"10.0.0.5" not in zf.read("word/_rels/document.xml.rels")
        assert zf.read("word/document.xml") == b"<w:document/>"


# The ways a linked resource got past the copy (review of round 1): each
# relationship below must go, unless it is an http(s) hyperlink that only
# hyperlink elements use.

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rel(rid: str, kind: str, target: str, mode: str = ' TargetMode="External"') -> str:
    return f'<Relationship Id="{rid}" Type="{REL}{kind}" Target="{target}"{mode}/>'


def _rels_part(*items: str) -> bytes:
    return (f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{"".join(items)}'
            "</Relationships>").encode()


def _slide(*uses: str) -> bytes:
    return (f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="{A_NS}" '
            f'xmlns:r="{R_NS}" xmlns:o="urn:schemas-microsoft-com:office:office">{"".join(uses)}</p:sld>').encode()


@pytest.mark.parametrize("rels, source, gone, kept", [
    # a hyperlink-typed relationship a picture uses
    (_rels_part(_rel("rId1", "hyperlink", "http://10.0.0.5/p.png")), _slide('<a:blip r:link="rId1"/>'),
     ["rId1"], []),
    # the same relationship used by a hyperlink and by a picture
    (_rels_part(_rel("rId1", "hyperlink", "http://10.0.0.5/p.png")),
     _slide('<a:hlinkClick r:id="rId1"/>', '<a:blip r:embed="rId1"/>'), ["rId1"], []),
    # a hyperlink to a file on the server, used by a hyperlink only
    (_rels_part(_rel("rId1", "hyperlink", "file:///agents/other/secret.png")), _slide('<a:hlinkClick r:id="rId1"/>'),
     ["rId1"], []),
    # a character reference in TargetMode, and a '>' in the target
    (_rels_part(_rel("rId1", "image", "http://10.0.0.5/p.png?a>b", ' TargetMode="&#69;xternal"')),
     _slide('<a:blip r:link="rId1"/>'), ["rId1"], []),
    # a URL or a local path under the default (internal) mode
    (_rels_part(_rel("rId1", "image", "http://10.0.0.5/p.png", ""), _rel("rId2", "image", "C:\\pics\\p.png", "")),
     _slide('<a:blip r:link="rId1"/>', '<a:blip r:link="rId2"/>'), ["rId1", "rId2"], []),
    # a picture a VML shape names in another attribute
    (_rels_part(_rel("rId1", "hyperlink", "http://10.0.0.5/p.png")), _slide('<a:hlinkClick r:id="rId1"/>',
                                                                          '<o:thing o:relid="rId1"/>'), ["rId1"], []),
    # an external relationship nothing uses, and a hyperlink type that only ends like one
    (_rels_part(_rel("rId1", "hyperlink", "http://example.com/"),
                f'<Relationship Id="rId2" Type="urn:x/hyperlink" Target="http://10.0.0.5/q" TargetMode="External"/>'),
     _slide('<a:hlinkClick r:id="rId2"/>'), ["rId1", "rId2"], []),
    # what stays: an https hyperlink a hyperlink element uses, and every internal part
    (_rels_part(_rel("rId1", "hyperlink", "https://example.com/a"), _rel("rId2", "image", "../media/image1.png", "")),
     _slide('<a:hlinkClick r:id="rId1"/>', '<a:blip r:embed="rId2"/>'), [], ["rId1", "rId2"]),
])
def test_only_an_http_hyperlink_that_only_hyperlinks_use_stays(rels, source, gone, kept):
    out, removed = render_copy.unlink_relationships(rels, source)
    assert removed == len(gone)
    for rid in gone:
        assert f'Id="{rid}"'.encode() not in out
    for rid in kept:
        assert f'Id="{rid}"'.encode() in out


def test_a_package_level_external_relationship_goes():
    out, removed = render_copy.unlink_relationships(_rels_part(_rel("rId9", "hyperlink", "https://example.com/")), None)
    assert removed == 1 and b"rId9" not in out


@pytest.mark.parametrize("rels, outside, chunk", [
    (_rels_part(_rel("rId1", "image", "media/a.png", "")), False, False),
    (_rels_part(_rel("rId1", "hyperlink", "https://example.com/")), True, False),
    (_rels_part(_rel("rId1", "image", "http://h/a.png?x>y", ' TargetMode="&#69;xternal"')), True, False),
    (_rels_part(_rel("rId1", "image", "file:///etc/hostname", "")), True, False),
    (_rels_part(_rel("rId1", "aFChunk", "afchunk.htm", "")), False, True),
])
def test_the_scan_says_whether_a_part_leaves_the_package(rels, outside, chunk):
    assert render_copy.scan_relationships(rels) == (outside, chunk)


@pytest.mark.parametrize("rels", [
    b'<!DOCTYPE r [<!ENTITY e "x">]><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
    '<?xml version="1.0" encoding="UTF-16"?><Relationships/>'.encode("utf-16"),
    b"<Relationships><Relationship",
])
def test_a_part_the_scan_cannot_read_is_refused(rels):
    with pytest.raises(render_copy.CopyRefused):
        render_copy.scan_relationships(rels)
