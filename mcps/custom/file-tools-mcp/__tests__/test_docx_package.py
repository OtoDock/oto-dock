"""Opening and naming Word packages: templates, macro-enabled files, A4 for
new documents, and the files that are refused."""

import asyncio
import io
import re
import zipfile

import pytest

pytest.importorskip("docx")

import word
from formats.docx import package as docx_package

VBA_BYTES = b"\xd0\xcf\x11\xe0 not a real project, only bytes to carry"
VBA_TYPE = "application/vnd.ms-office.vbaProject"
VBA_REL = "http://schemas.microsoft.com/office/2006/relationships/vbaProject"


def _blank_bytes() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Template body")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _rewrite(data: bytes, edits: dict[str, bytes | None], extra: dict[str, bytes] | None = None) -> bytes:
    """A copy of the zip ``data`` with members replaced (None drops one) and added."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename in edits:
                if edits[item.filename] is not None:
                    dst.writestr(item.filename, edits[item.filename])
            else:
                dst.writestr(item, src.read(item.filename))
        for name, body in (extra or {}).items():
            dst.writestr(name, body)
    return out.getvalue()


def _retyped(main_type: str, *, vba: bool = False) -> bytes:
    data = _blank_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        types = zf.read("[Content_Types].xml").decode()
        rels = zf.read("word/_rels/document.xml.rels").decode()
    types = types.replace(docx_package.DOCUMENT_MAIN, main_type)
    extra = {}
    if vba:
        types = types.replace(
            "</Types>", f'<Override PartName="/word/vbaProject.bin" ContentType="{VBA_TYPE}"/></Types>')
        rels = rels.replace(
            "</Relationships>",
            f'<Relationship Id="rIdVba1" Type="{VBA_REL}" Target="vbaProject.bin"/></Relationships>')
        extra["word/vbaProject.bin"] = VBA_BYTES
    return _rewrite(data, {"[Content_Types].xml": types.encode(),
                           "word/_rels/document.xml.rels": rels.encode()}, extra)


def _main_type(path) -> str:
    with zipfile.ZipFile(path) as zf:
        types = zf.read("[Content_Types].xml").decode()
    return re.search(r'PartName="/word/document.xml" ContentType="([^"]+)"', types).group(1)


def _type_map(path) -> dict[str, str]:
    """Part name to content type, whether a Default or an Override says it."""
    with zipfile.ZipFile(path) as zf:
        types = zf.read("[Content_Types].xml").decode()
        names = zf.namelist()
    defaults = dict(re.findall(r'<Default Extension="([^"]+)" ContentType="([^"]+)"', types))
    overrides = dict(re.findall(r'<Override PartName="/([^"]+)" ContentType="([^"]+)"', types))
    return {n: overrides.get(n, defaults.get(n.rsplit(".", 1)[-1], "")) for n in names}


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    """Paths resolve to themselves and the proxy hooks stay silent."""
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops, create_new=False, best_effort=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops,
                                               "create_new": create_new, "best_effort": best_effort}))


def test_template_is_read_and_edited_as_a_template(tmp_path):
    f = tmp_path / "letterhead.dotx"
    f.write_bytes(_retyped(docx_package.TEMPLATE_MAIN))
    assert "Template body" in word.read_docx(str(f))
    msg = _write(f, [{"op": "add_paragraph", "text": "Added line"}])
    assert "Warnings" not in msg
    assert _main_type(f) == docx_package.TEMPLATE_MAIN
    out = word.read_docx(str(f))
    assert "Template body" in out and "Added line" in out


def test_new_template_takes_the_template_content_type(tmp_path):
    f = tmp_path / "new.dotx"
    _write(f, [{"op": "add_heading", "text": "Letterhead"}])
    assert _main_type(f) == docx_package.TEMPLATE_MAIN
    assert "Letterhead" in word.read_docx(str(f))


@pytest.mark.parametrize("suffix, main_type", [
    (".docm", docx_package.MACRO_DOCUMENT_MAIN),
    (".dotm", docx_package.MACRO_TEMPLATE_MAIN),
])
def test_macro_project_rides_along_untouched(tmp_path, suffix, main_type):
    f = tmp_path / f"macros{suffix}"
    f.write_bytes(_retyped(main_type, vba=True))
    _write(f, [{"op": "add_paragraph", "text": "Edited"}])
    assert _main_type(f) == main_type
    assert _type_map(f)["word/vbaProject.bin"] == VBA_TYPE
    with zipfile.ZipFile(f) as zf:
        assert zf.read("word/vbaProject.bin") == VBA_BYTES
        rels = zf.read("word/_rels/document.xml.rels").decode()
    assert VBA_REL in rels and 'Target="vbaProject.bin"' in rels
    assert "Edited" in word.read_docx(str(f))


def test_template_renamed_docx_is_written_as_a_document(tmp_path):
    f = tmp_path / "plain.docx"
    f.write_bytes(_retyped(docx_package.TEMPLATE_MAIN))
    _write(f, [{"op": "add_paragraph", "text": "x"}])
    assert _main_type(f) == docx_package.DOCUMENT_MAIN


def test_macros_under_a_docx_name_are_refused(tmp_path):
    f = tmp_path / "renamed.docx"
    data = _retyped(docx_package.MACRO_DOCUMENT_MAIN, vba=True)
    f.write_bytes(data)
    with pytest.raises(Exception, match=r"holds macros.*\.docm"):
        _write(f, [{"op": "add_paragraph", "text": "x"}])
    assert f.read_bytes() == data
    assert "Template body" in word.read_docx(str(f))


def test_a_workbook_is_not_a_word_document(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "book.docx"
    wb = openpyxl.Workbook()
    wb.save(str(f))
    with pytest.raises(ValueError, match=r"not a Word document \(its main part is .*spreadsheetml"):
        word.read_docx(str(f))


def test_strict_open_xml_is_refused_with_the_fix(tmp_path):
    strict_rel = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
    f = tmp_path / "strict.docx"
    with zipfile.ZipFile(f, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/></Types>')
        zf.writestr("_rels/.rels",
                    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f'<Relationship Id="rId1" Type="{strict_rel}" Target="word/document.xml"/></Relationships>')
        zf.writestr("word/document.xml",
                    '<?xml version="1.0"?><w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"/>')
    with pytest.raises(ValueError, match="Strict Open XML.*normal Word document"):
        word.read_docx(str(f))


def test_new_documents_are_a4_with_one_inch_margins(tmp_path):
    f = tmp_path / "new.docx"
    _write(f, [{"op": "add_paragraph", "text": "Hello"}])
    from docx import Document

    section = Document(str(f)).sections[0]
    assert (section.page_width.twips, section.page_height.twips) == (11906, 16838)
    assert section.orientation == 0
    assert {section.top_margin.twips, section.bottom_margin.twips,
            section.left_margin.twips, section.right_margin.twips} == {1440}
    assert "**Page**: A4 portrait (8.27 x 11.69 in), margins 1 in" in word.read_docx(str(f))


def test_create_new_over_an_existing_file_starts_on_a4(tmp_path):
    from docx import Document

    f = tmp_path / "old.docx"
    Document().save(str(f))
    _write(f, [{"op": "add_paragraph", "text": "Fresh"}], create_new=True)
    assert Document(str(f)).sections[0].page_width.twips == 11906


def test_existing_documents_keep_their_page(tmp_path):
    from docx import Document

    f = tmp_path / "letter.docx"
    Document().save(str(f))
    _write(f, [{"op": "add_paragraph", "text": "More"}])
    section = Document(str(f)).sections[0]
    assert (section.page_width.twips, section.page_height.twips) == (12240, 15840)
    assert section.left_margin.twips == 1800


def test_letter_on_request(tmp_path):
    from docx import Document

    f = tmp_path / "us.docx"
    _write(f, [{"op": "set_page_setup", "page_size": "Letter"}])
    section = Document(str(f)).sections[0]
    assert (section.page_width.twips, section.page_height.twips) == (12240, 15840)
    assert "**Page**: Letter portrait (8.50 x 11.00 in)" in word.read_docx(str(f))


def test_handler_writes_a_template(tmp_path):
    f = tmp_path / "via-handler.dotx"
    msg = _write(f, [{"op": "add_paragraph", "text": "Body"}])
    assert msg.startswith("Document saved")
    assert _main_type(f) == docx_package.TEMPLATE_MAIN


def test_read_document_routes_the_template_and_macro_kinds():
    import docx_tool

    for ext in (".docx", ".dotx", ".docm", ".dotm"):
        assert docx_tool.READERS[ext] is docx_tool.read


def test_macro_project_relationships_and_ribbon_survive(tmp_path):
    data_type = "application/vnd.ms-word.vbaData+xml"
    data_rel = "http://schemas.microsoft.com/office/2006/relationships/wordVbaData"
    ui_rel = "http://schemas.microsoft.com/office/2007/relationships/ui/extensibility"
    vba_data = b'<?xml version="1.0"?><wne:vbaSuppData xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"/>'
    ribbon = b'<?xml version="1.0"?><customUI xmlns="http://schemas.microsoft.com/office/2009/07/customui"/>'
    data = _retyped(docx_package.MACRO_DOCUMENT_MAIN, vba=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        types = zf.read("[Content_Types].xml").decode()
        root_rels = zf.read("_rels/.rels").decode()
    types = types.replace("</Types>", f'<Override PartName="/word/vbaData.xml" ContentType="{data_type}"/></Types>')
    root_rels = root_rels.replace(
        "</Relationships>",
        f'<Relationship Id="rIdUi1" Type="{ui_rel}" Target="customUI/customUI14.xml"/></Relationships>')
    data = _rewrite(data, {"[Content_Types].xml": types.encode(), "_rels/.rels": root_rels.encode()}, {
        "word/_rels/vbaProject.bin.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{data_rel}" Target="vbaData.xml"/></Relationships>').encode(),
        "word/vbaData.xml": vba_data,
        "customUI/customUI14.xml": ribbon,
    })
    f = tmp_path / "ribbon.docm"
    f.write_bytes(data)
    _write(f, [{"op": "add_paragraph", "text": "Edited"}])
    with zipfile.ZipFile(f) as zf:
        assert zf.read("word/vbaProject.bin") == VBA_BYTES
        assert zf.read("word/vbaData.xml") == vba_data
        assert zf.read("customUI/customUI14.xml") == ribbon
        assert 'Target="vbaData.xml"' in zf.read("word/_rels/vbaProject.bin.rels").decode()
        assert ui_rel in zf.read("_rels/.rels").decode()
    assert _type_map(f)["word/vbaData.xml"] == data_type
    assert _main_type(f) == docx_package.MACRO_DOCUMENT_MAIN
