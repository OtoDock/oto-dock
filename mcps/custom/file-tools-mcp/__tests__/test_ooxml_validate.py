"""Structural validation of OOXML packages: nothing found on what the
libraries and LibreOffice write untouched (a finding there is a bug in the
check), each fault found where it is, and what an edit lost."""

import io
import sys
import zipfile
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

import checks
from checks import CheckContext, Severity
from ooxml import validate

CORPUS = _HERE / "corpus" / "shared"
_CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
       'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
       'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>{extra}</Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
         '2006/relationships">{rels}</Relationships>')
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rel(rid: str, target: str, mode: str = "") -> str:
    extra = f' TargetMode="{mode}"' if mode else ""
    return f'<Relationship Id="{rid}" Type="urn:t" Target="{target}"{extra}/>'


def _package(parts: dict[str, str | bytes], ct_extra: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", _CT.format(extra=ct_extra))
        for name, data in parts.items():
            archive.writestr(name, data)
    return buf.getvalue()


def _check(data: bytes, fmt: str = "docx"):
    with CheckContext("", fmt, data=data) as ctx:
        return validate.validate_package(ctx)


def _serious(findings):
    return [f for f in findings if f.severity is not Severity.INFO]


def _docx_bytes() -> bytes:
    docx = pytest.importorskip("docx")
    buf = io.BytesIO()
    docx.Document().save(buf)
    return buf.getvalue()


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    return buf.getvalue()


def _docx_pictures_bytes() -> bytes:
    """Two pictures in the body and one each in the header and footer:
    python-docx numbers every story part's drawings from 1 and writes
    pic:cNvPr id="0" for each."""
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for _ in range(2):
        document.add_picture(io.BytesIO(_png()))
    section = document.sections[0]
    for story in (section.header, section.footer):
        story.paragraphs[0].add_run().add_picture(io.BytesIO(_png()))
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _xlsx_bytes() -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    buf = io.BytesIO()
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = 1
    workbook.save(buf)
    return buf.getvalue()


def _pptx_bytes() -> bytes:
    pptx = pytest.importorskip("pptx")
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Title"
    data = CategoryChartData()
    data.categories = ["A", "B"]
    data.add_series("S", (1, 2))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(4), Inches(3), data)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("make, fmt", [(_docx_bytes, "docx"), (_docx_pictures_bytes, "docx"), (_xlsx_bytes, "xlsx"),
                                       (_pptx_bytes, "pptx")])
def test_the_libraries_untouched_output_has_no_finding(make, fmt):
    assert _serious(_check(make(), fmt)) == []


_FORMATS = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}


@pytest.mark.parametrize("path", sorted(CORPUS.glob("collabora-*")), ids=lambda p: p.name)
def test_a_collabora_saved_file_has_no_finding(path):
    assert _serious(_check(path.read_bytes(), _FORMATS[path.suffix])) == []


@pytest.mark.parametrize("path", sorted(CORPUS.glob("libreoffice-*")), ids=lambda p: p.name)
def test_a_libreoffice_saved_file_has_no_error(path):
    """LibreOffice 7.4 repeats shape ids in its slide layouts: a warning,
    never an error."""
    found = _check(path.read_bytes(), _FORMATS[path.suffix])
    assert [f for f in found if f.severity is Severity.ERROR] == []
    assert {f.key.split(":")[0] for f in found if f.severity is Severity.WARNING} <= {"duplicate_shape_id"}


_SAVED_ELSEWHERE = sorted(p for p in CORPUS.iterdir() if p.suffix in _FORMATS
                          and p.name.startswith(("word-", "excel-", "powerpoint-", "google-")))


@pytest.mark.parametrize("path", _SAVED_ELSEWHERE, ids=lambda p: p.name)
def test_a_file_saved_by_office_or_google_has_no_finding(path):
    assert _serious(_check(path.read_bytes(), _FORMATS[path.suffix])) == []


def test_the_corpus_holds_both_kinds_of_file():
    assert len(list(CORPUS.glob("collabora-*"))) >= 2 and len(list(CORPUS.glob("libreoffice-*"))) == 3
    assert len(_SAVED_ELSEWHERE) >= 15


def test_relationship_faults_are_found_in_their_part():
    data = _package({
        "_rels/.rels": _RELS.format(rels=_rel("rId1", "word/document.xml")),
        "word/document.xml": f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body><w:p><w:hyperlink r:id="rId9"/>'
                             '</w:p></w:body></w:document>',
        "word/_rels/document.xml.rels": _RELS.format(rels=_rel("rId1", "media/a.png") + _rel("rId1", "styles.xml")
                                                     + _rel("rId2", "../missing/x.xml")
                                                     + _rel("rId3", "https://example.com", "External")),
        "word/media/a.png": b"\x89PNG",
    }, ct_extra='<Override PartName="/word/gone.xml" ContentType="application/xml"/>')
    found = {(f.part, f.key, f.severity) for f in _check(data)}
    assert ("word/document.xml", "missing_target:rId1", Severity.ERROR) in found
    assert len(found) == 6
    assert ("word/document.xml", "missing_target:rId2", Severity.ERROR) in found
    assert ("word/_rels/document.xml.rels", "duplicate_rid:rId1", Severity.ERROR) in found
    assert ("word/document.xml", "undefined_rid:rId9", Severity.ERROR) in found
    assert ("word/media/a.png", "no_content_type", Severity.ERROR) in found
    assert ("[Content_Types].xml", "dangling_override:word/gone.xml", Severity.WARNING) in found
    assert not any("rId3" in key for _, key, _ in found)
    assert all(f.fix for f in _check(data))


def test_part_names_compare_as_packages_do():
    """Case, percent-encoding and Unicode normalization never make a part
    missing or untyped."""
    import unicodedata

    greek = unicodedata.normalize("NFD", "Εικόνα.png")
    data = _package({
        "_rels/.rels": _RELS.format(rels=_rel("rId1", "word/Document.XML")),
        "word/document.xml": f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body/></w:document>',
        "word/_rels/document.xml.rels": _RELS.format(rels=_rel("rId1", "media/%CE%95%CE%B9%CE%BA%CF%8C%CE%BD%CE%B1.png")),
        f"word/media/{greek}": b"\x89PNG",
    }, ct_extra='<Default Extension="PNG" ContentType="image/png"/>'
                '<Override PartName="/WORD/document.xml" ContentType="application/xml"/>')
    assert _serious(_check(data)) == []


def test_targets_resolve_with_posix_paths():
    assert validate.resolve_target("ppt/slides/slide1.xml", "../media/image1.png") == "ppt/media/image1.png"
    assert validate.resolve_target("word/document.xml", "/word/styles.xml") == "word/styles.xml"
    assert validate.source_part("word/_rels/document.xml.rels") == "word/document.xml"
    assert validate.source_part("_rels/.rels") == ""


def test_ids_office_needs_unique_are_checked():
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    w14 = "http://schemas.microsoft.com/office/word/2010/wordml"
    data = _package({
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{p_ns}" xmlns:r="{_R}"><p:sldIdLst>'
                                '<p:sldId id="12" r:id="rId1"/><p:sldId id="300" r:id="rId1"/><p:sldId id="300" '
                                'r:id="rId1"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": _RELS.format(rels=_rel("rId1", "slides/slide1.xml")),
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{p_ns}"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="2" name="a"/>'
                                 '</p:nvGrpSpPr><p:sp><p:nvSpPr><p:cNvPr id="2" name="b"/></p:nvSpPr></p:sp>'
                                 '</p:spTree></p:cSld></p:sld>',
        "word/document.xml": f'<w:document xmlns:w="{_W}" xmlns:w14="{w14}"><w:body><w:p w14:paraId="1A2B3C4D"/>'
                             '<w:p w14:paraId="1A2B3C4D"/><w:p w14:paraId="9FFFFFFF"/></w:body></w:document>',
    })
    keys = {f.key for f in _check(data, "pptx")}
    assert {"low_slide_id:12", "duplicate_slide_id:300", "duplicate_shape_id:2", "duplicate_para_id:1A2B3C4D",
            "bad_para_id:9FFFFFFF"} <= keys
    assert all(f.fix for f in _check(data, "pptx"))


_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"


def _drawing(docpr: str) -> str:
    return (f'<w:r><w:drawing><wp:inline><wp:docPr id="{docpr}" name="p"/><a:graphic><a:graphicData><pic:pic>'
            '<pic:nvPicPr><pic:cNvPr id="0" name="p"/></pic:nvPicPr></pic:pic></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r>')


def test_word_drawings_need_their_own_ids_in_a_part():
    """Word numbers a drawing by wp:docPr; the picture's own cNvPr id is
    not one Word reads."""
    body = "".join(f"<w:p>{_drawing(i)}</w:p>" for i in ("1", "2", "2"))
    data = _package({"word/document.xml": f'<w:document xmlns:w="{_W}" xmlns:wp="{_WP}" xmlns:pic="{_PIC}" '
                                          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                                          f'<w:body>{body}</w:body></w:document>'})
    found = [f for f in _check(data) if f.severity is not Severity.INFO]
    assert [(f.key, f.severity) for f in found] == [("duplicate_drawing_id:2", Severity.WARNING)]
    assert found[0].fix


def test_a_bookmark_that_spans_a_table_ends_after_its_last_row():
    docx = pytest.importorskip("docx")
    from docx.oxml.ns import qn
    from lxml import etree

    document = docx.Document()
    table = document.add_table(rows=2, cols=2)
    tbl = table._tbl
    start = etree.SubElement(tbl, qn("w:bookmarkStart"), {qn("w:id"): "0", qn("w:name"): "figures"})
    tbl.remove(start)
    tbl.insert(0, start)
    etree.SubElement(tbl, qn("w:bookmarkEnd"), {qn("w:id"): "0"})
    etree.SubElement(tbl, qn("w:commentRangeEnd"), {qn("w:id"): "1"})
    buf = io.BytesIO()
    document.save(buf)
    assert [f for f in _check(buf.getvalue()) if f.key.startswith("order:")] == []


def test_ids_repeated_in_an_alternate_branch_are_not_duplicates():
    """Word repeats a text box's paragraph ids in its VML Fallback copy, and
    PowerPoint a shape's id in each branch: only the branch an application
    keeps (the first Choice) counts."""
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    w14 = "http://schemas.microsoft.com/office/word/2010/wordml"
    box = '<w:p w14:paraId="0A0B0C0D"><w:r><w:t>in the box</w:t></w:r></w:p>'

    def document(last_id):
        return _package({"word/document.xml": (
            f'<w:document xmlns:w="{_W}" xmlns:w14="{w14}" xmlns:mc="{mc}" mc:Ignorable="w14"><w:body><w:p><w:r>'
            f'<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing>{box}</w:drawing></mc:Choice>'
            f'<mc:Fallback><w:pict>{box}</w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>'
            f'<w:p w14:paraId="{last_id}"/></w:body></w:document>')})

    assert [f.key for f in _check(document("11111111")) if f.key.startswith("duplicate")] == []
    assert [f.key for f in _check(document("0A0B0C0D")) if f.key.startswith("duplicate")] == [
        "duplicate_para_id:0A0B0C0D"]


def test_children_out_of_schema_order_are_found():
    data = _package({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body><w:tbl><w:tr><w:tc><w:tcPr>'
                                          '<w:tcW/><w:noWrap/><w:shd/></w:tcPr></w:tc></w:tr></w:tbl></w:body>'
                                          '</w:document>'})
    found = [f for f in _check(data) if f.key.startswith("order:")]
    assert len(found) == 1 and found[0].message == ("<shd> comes after <noWrap> in <tcPr>, where the schema puts it "
                                                    "before")


_PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DML = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _slide(timing: str = "", frame: str = "") -> bytes:
    sp_tree = (f'<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
               f"{frame}</p:spTree>")
    return _package({"ppt/slides/slide1.xml": f'<p:sld xmlns:p="{_PML}" xmlns:a="{_DML}"><p:cSld>{sp_tree}</p:cSld>'
                                              f"{timing}</p:sld>"})


def _table(rows: str) -> bytes:
    return _package({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body><w:tbl><w:tblPr/><w:tblGrid>'
                                          f'<w:gridCol w:w="100"/></w:tblGrid>{rows}</w:tbl><w:p/></w:body>'
                                          "</w:document>"})


_CELL = "<w:tc><w:p/></w:tc>"
_FRAME = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="2" name="t"/><p:cNvGraphicFramePr/><p:nvPr/>'
          '</p:nvGraphicFramePr><p:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></p:xfrm><a:graphic>'
          '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblGrid>'
          '<a:gridCol w="1"/></a:tblGrid><a:tr h="1">{cells}</a:tr></a:tbl></a:graphicData></a:graphic>'
          "</p:graphicFrame>")
_A_CELL = "<a:tc><a:txBody><a:bodyPr/><a:p/></a:txBody></a:tc>"
_PAR = '<p:par><p:cTn id="2"><p:childTnLst>{inner}</p:childTnLst></p:cTn></p:par>'
_LEAF = '<p:par><p:cTn id="3"/></p:par>'


@pytest.mark.parametrize("data, fmt, empty", [
    (_slide(f'<p:timing><p:tnLst><p:par><p:cTn id="1"><p:childTnLst/></p:cTn></p:par></p:tnLst></p:timing>'),
     "pptx", "childTnLst"),
    (_slide("<p:timing><p:tnLst/></p:timing>"), "pptx", "tnLst"),
    (_slide("<p:timing><p:bldLst/></p:timing>"), "pptx", "bldLst"),
    (_table(""), "docx", "tbl"),
    (_table("<w:tr/>"), "docx", "tr"),
    (_table("<w:tr><w:trPr/></w:tr>"), "docx", "tr"),
    (_slide(frame=_FRAME.format(cells="")), "pptx", "tr"),
], ids=["childTnLst", "tnLst", "bldLst", "table without rows", "row without cells", "row with only its properties",
        "slide table row without cells"])
def test_an_empty_container_office_repairs_is_an_error(data, fmt, empty):
    found = [f for f in _check(data, fmt) if f.key.startswith("empty:")]
    assert [(f.key.rsplit("}", 1)[-1], f.severity) for f in found] == [(empty, Severity.ERROR)], found
    assert found[0].fix


@pytest.mark.parametrize("data, fmt", [
    (_slide(f"<p:timing><p:tnLst>{_PAR.format(inner=_LEAF)}</p:tnLst></p:timing>"), "pptx"),
    (_table(f"<w:tr>{_CELL}</w:tr>"), "docx"),
    (_table(f'<w:tr><w:sdt><w:sdtContent>{_CELL}</w:sdtContent></w:sdt></w:tr>'), "docx"),
    (_package({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body><w:p/><w:sectPr><w:cols/></w:sectPr>'
                                    "</w:body></w:document>"}), "docx"),
    (_package({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body/></w:document>'}), "docx"),
    (_slide(frame=_FRAME.format(cells=_A_CELL)), "pptx"),
])
def test_containers_that_may_be_empty_or_are_full_are_not_reported(data, fmt):
    assert [f for f in _check(data, fmt) if f.key.startswith("empty:")] == []


def test_markup_compatibility_is_processed_before_the_order_check():
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    data = _package({"word/document.xml": (
        f'<w:document xmlns:w="{_W}" xmlns:mc="{mc}" xmlns:ext="urn:x" mc:Ignorable="ext"><w:body><w:p><w:pPr>'
        '<ext:thing/><w:jc/><mc:AlternateContent><mc:Choice Requires="ext"><w:keepNext/></mc:Choice>'
        '<mc:Fallback><w:spacing/></mc:Fallback></mc:AlternateContent></w:pPr></w:p></w:body></w:document>')})
    found = [f for f in _check(data) if f.key.startswith("order:")]
    assert [f.message for f in found] == ["<spacing> comes after <jc> in <pPr>, where the schema puts it before"]


def test_malformed_strict_and_large_parts():
    bad = _package({"word/document.xml": "<w:document"})
    assert [(f.key, bool(f.fix)) for f in _check(bad)] == [("malformed", True)]
    strict = _package({"word/document.xml": '<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"/>'})
    assert [(f.severity, "Strict" in f.message) for f in _check(strict)] == [(Severity.INFO, True)]


def test_a_large_part_is_streamed(monkeypatch):
    monkeypatch.setattr(validate, "STRUCTURE_MAX_BYTES", 10)
    data = _package({"word/document.xml": f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body><w:hyperlink '
                                          'r:id="rId4"/></w:body></w:document>'})
    found = _check(data)
    assert {f.key for f in found if f.severity is Severity.ERROR} == {"undefined_rid:rId4"}
    assert any("too large" in f.message for f in found)


@pytest.mark.parametrize("structure_max", [5, 100_000])
def test_a_part_past_what_the_checks_read_is_named_not_called_malformed(monkeypatch, structure_max):
    """Streamed (past the structure size) or parsed whole, a part the
    context refuses for its size is an INFO line, never an error."""
    monkeypatch.setattr(checks, "PART_MAX_BYTES", 4_000)
    monkeypatch.setattr(validate, "STRUCTURE_MAX_BYTES", structure_max)
    body = "<w:p/>" * 2_000
    data = _package({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'})
    found = _check(data)
    assert [f.severity for f in found] == [Severity.INFO] and found[0].key == "too_large"
    assert "too large for the checks" in found[0].message and "well-formed" not in found[0].message


def test_a_write_that_drops_parts_or_extensions_is_reported():
    ext = "{78C0D931-6437-407d-A8EE-F0AAD7539E65}"
    before = _package({
        "xl/workbook.xml": f'<workbook xmlns="urn:x"><extLst><ext uri="{ext}">{_CF_EXT}</ext></extLst></workbook>',
        "xl/vbaProject.bin": b"\x00", "xl/calcChain.xml": "<calcChain/>",
    }, ct_extra='<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/>'
                '<Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.calcChain+xml"/>')
    after = _package({"xl/workbook.xml": '<workbook xmlns="urn:x"/>'})
    with CheckContext("", "xlsx", data=before) as ctx:
        snapshot = validate.inventory(ctx)
    with CheckContext("", "xlsx", data=after) as ctx:
        ctx.baseline_snapshot = snapshot
        found = validate.check_parts_lost(ctx)
    assert {f.key for f in found} == {"lost_type:application/vnd.ms-office.vbaProject", f"lost_ext:{ext}"}
    assert all(f.severity is Severity.ERROR and f.check == "xlsx.parts_lost" for f in found)
    with CheckContext("", "xlsx", data=after) as ctx:
        assert validate.check_parts_lost(ctx) == []


_CF_EXT = ('<x14:conditionalFormattings xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" '
           'xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main"><x14:conditionalFormatting>'
           "<xm:sqref>A1:A9</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings>")


def _lost(before: bytes, after: bytes, fmt: str, removed=()):
    with CheckContext("", fmt, data=before) as ctx:
        snapshot = validate.inventory(ctx)
    with CheckContext("", fmt, data=after, removed=frozenset(removed)) as ctx:
        ctx.baseline_snapshot = snapshot
        return validate.check_parts_lost(ctx)


_MAIN_TYPES = {
    "docx": ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"),
    "xlsx": ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
    "pptx": ("ppt/presentation.xml",
             "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
             "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"),
}


def _retyped(data: bytes, part: str, old: str, new: str) -> bytes:
    """The package with ``part``'s declared type changed from old to new."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            body = src.read(item)
            if item.filename == "[Content_Types].xml":
                assert f'PartName="/{part}" ContentType="{new}"'.encode() in body, body[:600]
                body = body.replace(f'PartName="/{part}" ContentType="{new}"'.encode(),
                                    f'PartName="/{part}" ContentType="{old}"'.encode())
            dst.writestr(item, body)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["docx", "xlsx", "pptx"])
def test_a_template_saved_as_a_document_loses_no_part(fmt):
    """A template copied under a document's name is saved as that document:
    its main part keeps its name and changes its type, and nothing is lost.
    A main part that is really gone is still reported."""
    part, template, document = _MAIN_TYPES[fmt]
    after = {"docx": _docx_bytes, "xlsx": _xlsx_bytes, "pptx": _pptx_bytes}[fmt]()
    before = _retyped(after, part, template, document)
    assert _lost(before, after, fmt) == []
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(after)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            if item.filename != part:
                dst.writestr(item, src.read(item))
    gone = _lost(before, out.getvalue(), fmt)
    assert any(f.location == f"part /{part}" and "template.main" in f.message for f in gone), gone


def _saved(document) -> bytes:
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _deck() -> bytes:
    """Two slides; the first has a picture of its own, a chart and notes."""
    pptx = pytest.importorskip("pptx")
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = pptx.Presentation()
    first = prs.slides.add_slide(prs.slide_layouts[6])
    first.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1))
    data = CategoryChartData()
    data.categories = ["A", "B"]
    data.add_series("S", (1, 2))
    first.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(3), Inches(1), Inches(4), Inches(3), data)
    first.notes_slide.notes_text_frame.text = "say this"
    prs.slides.add_slide(prs.slide_layouts[1]).shapes.title.text = "Second"
    return _saved(prs)


def _without_first_slide(data: bytes) -> bytes:
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation(io.BytesIO(data))
    slides = prs.slides._sldIdLst
    prs.part.drop_rel(slides[0].rId)
    del slides[0]
    return _saved(prs)


def test_a_deleted_slide_and_what_only_it_used_are_not_a_loss():
    before = _deck()
    after = _without_first_slide(before)
    assert _lost(before, after, "pptx", removed={"ppt/slides/slide1.xml"}) == []
    undeclared = {f.key for f in _lost(before, after, "pptx")}
    assert "lost_type:application/vnd.openxmlformats-officedocument.presentationml.slide+xml" in undeclared
    assert "lost_type:image/png" in undeclared
    assert not any("relationships" in key for key in undeclared)


def test_a_loss_beside_a_removal_is_still_named():
    before = _deck()
    source = zipfile.ZipFile(io.BytesIO(_without_first_slide(before)))
    stripped = io.BytesIO()
    with zipfile.ZipFile(stripped, "w") as archive:
        for item in source.infolist():
            if not item.filename.startswith("ppt/slideLayouts/slideLayout1"):
                archive.writestr(item, source.read(item))
    found = _lost(before, stripped.getvalue(), "pptx", removed={"/ppt/slides/slide1.xml"})
    assert [f.key for f in found] == [
        "lost_type:application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"]
    assert "ppt/slideLayouts/slideLayout1.xml" in found[0].message and found[0].severity is Severity.ERROR


def test_a_deleted_sheet_and_its_drawing_are_not_a_loss():
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl.drawing.image import Image as SheetImage

    book = openpyxl.Workbook()
    book.active["A1"] = 1
    second = book.create_sheet("Two")
    second.add_image(SheetImage(io.BytesIO(_png())), "B2")
    before = _saved(book)
    book = openpyxl.load_workbook(io.BytesIO(before))
    second = book["Two"]
    second.add_image(SheetImage(io.BytesIO(_png())), "B2")
    del book["Two"]
    after = _saved(book)
    assert _lost(before, after, "xlsx", removed={"xl/worksheets/sheet2.xml"}) == []
    assert {f.key for f in _lost(before, after, "xlsx")} >= {
        "lost_type:application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"}


def test_a_removed_header_picture_is_not_a_loss():
    docx = pytest.importorskip("docx")
    from docx.oxml.ns import qn

    document = docx.Document()
    document.add_paragraph("Body")
    header = document.sections[0].header
    header.paragraphs[0].add_run().add_picture(io.BytesIO(_png()))
    before = _saved(document)
    document = docx.Document(io.BytesIO(before))
    header = document.sections[0].header
    blip = header._element.xpath(".//a:blip")[0]
    rid = blip.get(qn("r:embed"))
    image = header.part.related_parts[rid].partname
    drawing = blip.xpath("ancestor::w:drawing")[0]
    drawing.getparent().remove(drawing)
    header.part.drop_rel(rid)
    after = _saved(document)
    assert _lost(before, after, "docx", removed={str(image)}) == []
    assert [f.key for f in _lost(before, after, "docx")] == ["lost_type:image/png"]


def test_an_extension_goes_with_the_part_that_carried_it():
    ext = "{78C0D931-6437-407d-A8EE-F0AAD7539E65}"
    root_rels = _RELS.format(rels=_rel("rId1", "xl/workbook.xml"))
    book_rels = _RELS.format(rels=_rel("rId1", "worksheets/sheet1.xml") + _rel("rId2", "worksheets/sheet2.xml"))
    sheet = '<worksheet xmlns="urn:x"><extLst><ext uri="{uri}">' + _CF_EXT + "</ext></extLst></worksheet>"
    before = _package({"_rels/.rels": root_rels, "xl/workbook.xml": "<workbook/>",
                       "xl/_rels/workbook.xml.rels": book_rels,
                       "xl/worksheets/sheet1.xml": sheet.format(uri="{keep}"),
                       "xl/worksheets/sheet2.xml": sheet.format(uri=ext)})
    after = _package({"_rels/.rels": root_rels, "xl/workbook.xml": "<workbook/>",
                      "xl/_rels/workbook.xml.rels": _RELS.format(rels=_rel("rId1", "worksheets/sheet1.xml")),
                      "xl/worksheets/sheet1.xml": sheet.format(uri="{keep}")})
    assert _lost(before, after, "xlsx", removed={"xl/worksheets/sheet2.xml"}) == []
    assert {f.key for f in _lost(before, after, "xlsx")} == {"lost_type:application/xml", f"lost_ext:{ext}"}


def test_a_part_nothing_reached_before_is_not_a_loss():
    """A package's orphan parts (no relationship leads to them) are dropped
    by every library on save: the file never showed them."""
    root_rels = _RELS.format(rels=_rel("rId1", "word/document.xml"))
    before = _package({"_rels/.rels": root_rels, "word/document.xml": "<document/>", "word/orphan.xml": "<x/>"})
    after = _package({"_rels/.rels": root_rels, "word/document.xml": "<document/>"})
    assert _lost(before, after, "docx") == []


_WORKBOOKS = sorted(CORPUS.glob("*.xlsx"))


@pytest.mark.parametrize("path", _WORKBOOKS, ids=lambda p: p.name)
def test_an_ordinary_edit_of_a_corpus_workbook_loses_nothing(path):
    """One cell written with openpyxl, as write_xlsx saves it: shared
    strings rebuilt, empty parts, settings-only extensions and a vendor's
    round-trip data are no loss."""
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(io.BytesIO(path.read_bytes()))
    book.active["AZ500"] = 1
    found = _lost(path.read_bytes(), _saved(book), "xlsx")
    assert [f for f in found if f.severity is Severity.ERROR] == [], [f.message for f in found]


def test_an_extension_with_content_is_still_a_loss():
    settings = '<x15:workbookPr xmlns:x15="http://schemas.microsoft.com/office/spreadsheetml/2010/11/main" a="1"/>'
    before = _package({"xl/workbook.xml": f'<workbook xmlns="urn:x"><extLst><ext uri="{{S}}">{settings}</ext>'
                                          f'<ext uri="{{C}}">{_CF_EXT}</ext></extLst></workbook>'})
    after = _package({"xl/workbook.xml": '<workbook xmlns="urn:x"/>'})
    assert [f.key for f in _lost(before, after, "xlsx")] == ["lost_ext:{C}"]


def test_an_empty_part_is_no_loss_and_a_full_one_is():
    ct = ('<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'custom-properties+xml"/>')
    rels = _RELS.format(rels=_rel("rId1", "xl/workbook.xml") + _rel("rId2", "docProps/custom.xml"))
    props = '<Properties xmlns="urn:p">{}</Properties>'
    after = _package({"_rels/.rels": _RELS.format(rels=_rel("rId1", "xl/workbook.xml")), "xl/workbook.xml": "<w/>"})
    empty = _package({"_rels/.rels": rels, "xl/workbook.xml": "<w/>", "docProps/custom.xml": props.format("")},
                     ct_extra=ct)
    full = _package({"_rels/.rels": rels, "xl/workbook.xml": "<w/>",
                     "docProps/custom.xml": props.format('<property name="Client"/>')}, ct_extra=ct)
    assert _lost(empty, after, "xlsx") == []
    assert [f.severity for f in _lost(full, after, "xlsx")] == [Severity.ERROR]


_SECTIONS = ('<p14:sectionLst xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main">'
             '<p14:section name="Quarterly results" id="{6A1C1D8E-5F2B-4C7E-9A63-1B2C3D4E5F60}"><p14:sldIdLst>'
             '<p14:sldId id="256"/></p14:sldIdLst></p14:section></p14:sectionLst>')
_FEATURES = ('<xcalcf:calcFeatures xmlns:xcalcf="http://schemas.microsoft.com/office/spreadsheetml/2018/calcfeatures">'
             '<xcalcf:feature name="microsoft.com:RD"/></xcalcf:calcFeatures>')


def test_an_extension_whose_content_is_in_attributes_is_a_loss():
    """PowerPoint's sections hold the person's own names in attributes;
    Excel's calculation features name settings only."""
    sections = "{521415D9-36F7-43E2-AB2F-B90AF26B5E84}"
    before = _package({"ppt/presentation.xml": f'<presentation xmlns="urn:x"><extLst><ext uri="{sections}">'
                                               f"{_SECTIONS}</ext></extLst></presentation>"})
    after = _package({"ppt/presentation.xml": '<presentation xmlns="urn:x"/>'})
    found = _lost(before, after, "pptx")
    assert [(f.key, f.severity) for f in found] == [(f"lost_ext:{sections}", Severity.ERROR)]
    features = "{B58B0392-4F1F-4190-BB64-5DF3571DCE5F}"
    before = _package({"xl/workbook.xml": f'<workbook xmlns="urn:x"><extLst><ext uri="{features}">{_FEATURES}'
                                          "</ext></extLst></workbook>"})
    assert [f for f in _lost(before, _package({"xl/workbook.xml": '<workbook xmlns="urn:x"/>'}), "xlsx")
            if f.severity is Severity.ERROR] == []


_X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"


@pytest.mark.parametrize("uri, part, ext", [
    ("{504A1905-F514-4f6f-8877-14C23A59335A}", "xl/tables/table1.xml",
     f'<x14:table xmlns:x14="{_X14}" altText="Quarterly sales" altTextSummary="Sales by region and month"/>'),
    ("{962EF5D1-5CA2-4c93-8EF4-DBF5C05439D2}", "xl/pivotTables/pivotTable1.xml",
     f'<x14:pivotTableDefinition xmlns:x14="{_X14}" altText="Revenue pivot" hideValuesRow="1"/>'),
    ("{C}", "xl/workbook.xml", "Text right inside the extension"),
    ("{D}", "xl/workbook.xml", '<x:a xmlns:x="urn:y"/>Text after an element'),
])
def test_alt_text_and_loose_text_in_an_extension_are_content(uri, part, ext):
    root = part.rsplit("/", 1)[-1].split(".")[0].rstrip("1")
    before = _package({part: f'<{root} xmlns="urn:x"><extLst><ext uri="{uri}">{ext}</ext></extLst></{root}>'})
    after = _package({part: f'<{root} xmlns="urn:x"/>'})
    assert [(f.key, f.severity) for f in _lost(before, after, "xlsx")] == [(f"lost_ext:{uri}", Severity.ERROR)]


def test_a_part_whose_root_holds_only_text_is_not_empty():
    ct = '<Override PartName="/customXml/item9.xml" ContentType="application/xml"/>'
    rels = _RELS.format(rels=_rel("rId1", "word/document.xml") + _rel("rId2", "customXml/item9.xml"))
    after = _package({"_rels/.rels": _RELS.format(rels=_rel("rId1", "word/document.xml")),
                      "word/document.xml": "<document/>"})
    before = _package({"_rels/.rels": rels, "word/document.xml": "<document/>",
                       "customXml/item9.xml": "<note>Client account 12345, confidential text</note>"}, ct_extra=ct)
    assert [f.severity for f in _lost(before, after, "docx")] == [Severity.ERROR]


def test_the_checks_are_registered_for_every_package_format(check_registry):
    validate.register_checks()
    for fmt in ("docx", "xlsx", "pptx"):
        assert {f"{fmt}.package", f"{fmt}.parts_lost"} <= set(checks.check_ids(fmt))
    assert checks._REGISTRY["xlsx.parts_lost"].snapshot is validate.inventory


def test_an_edit_that_loses_a_part_says_so_after_the_write(check_registry, monkeypatch, tmp_path):
    """The snapshot is taken on the original before the load and reaches
    the check after the write."""
    validate.register_checks()
    original = tmp_path / "book.xlsx"
    original.write_bytes(_package({"xl/workbook.xml": "<workbook/>", "xl/vbaProject.bin": b"\x00"},
                                  ct_extra='<Override PartName="/xl/vbaProject.bin" '
                                           'ContentType="application/vnd.ms-office.vbaProject"/>'))
    baseline = checks.collect_baseline("xlsx", path=str(original))
    assert isinstance(baseline, checks.Baseline) and "xlsx.parts_lost" in baseline.snapshots
    with CheckContext("", "xlsx", data=_package({"xl/workbook.xml": "<workbook/>"})) as ctx:
        found = checks.run_checks(ctx, phase=checks.AFTER_WRITE, baseline=baseline)
    assert [f.key for f in found if f.check == "xlsx.parts_lost"] == ["lost_type:application/vnd.ms-office.vbaProject"]
