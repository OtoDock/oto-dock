"""The schema check: clean on what Office and our writers save, a plain
warning per kind of fault, Markup Compatibility and Office's extensions
read as Office reads them, the tolerated deviations, its bounds, and the
schema set that never leaves its folder."""

import io
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import checks
from ooxml import schema_checks

W = schema_checks.W
CORPUS = Path(__file__).parent / "corpus"
# A test fixture written by hand with two faults on purpose.
HANDBUILT = "word-markup-handbuilt.docx"
FORMATS = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx", ".pptm": "pptx"}


def _check(data: bytes, fmt: str) -> list:
    with checks.CheckContext(f"x.{fmt}", fmt, data=data) as ctx:
        return schema_checks.check_schema(ctx)


def _package(fmt: str, parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for name, xml in parts.items():
            zf.writestr(name, xml)
    return buffer.getvalue()


def _document(body: str, *, extra_ns: str = "", root_attrs: str = "") -> bytes:
    return _package("docx", {"word/document.xml": f'<w:document xmlns:w="{W}" {extra_ns} {root_attrs}>'
                                                  f"<w:body>{body}</w:body></w:document>"})


@pytest.mark.parametrize("path", sorted(p for p in CORPUS.rglob("*") if p.suffix in FORMATS and p.name != HANDBUILT),
                         ids=lambda p: p.name)
def test_files_from_office_google_libreoffice_and_collabora_are_clean(path):
    assert [f for f in _check(path.read_bytes(), FORMATS[path.suffix]) if f.severity is checks.Severity.WARNING] == []


def test_the_blank_outputs_of_the_libraries_are_clean():
    docx = pytest.importorskip("docx")
    openpyxl = pytest.importorskip("openpyxl")
    pptx = pytest.importorskip("pptx")
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    outputs = []
    for fmt, save in (("docx", docx.Document().save), ("xlsx", openpyxl.Workbook().save)):
        buffer = io.BytesIO()
        save(buffer)
        outputs.append((fmt, buffer.getvalue()))
    deck = pptx.Presentation()
    data = CategoryChartData()
    data.categories = ["a", "b"]
    data.add_series("s", (1, 2))
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(4), Inches(3), data)
    buffer = io.BytesIO()
    deck.save(buffer)
    outputs.append(("pptx", buffer.getvalue()))
    for fmt, data in outputs:
        assert _check(data, fmt) == [], fmt


@pytest.mark.parametrize("body, kind, excerpt", [
    ('<w:p><w:r><w:t>x</w:t></w:r><w:pPr/></w:p>', schema_checks.NOT_EXPECTED, "w:pPr"),
    ('<w:p><w:pPr><w:jc w:val="middle"/></w:pPr></w:p>', schema_checks.BAD_VALUE, "w:jc (w:val) value 'middle'"),
    ('<w:p><w:pPr><w:jc/></w:pPr></w:p>', schema_checks.ATTRIBUTE_MISSING, "w:jc (w:val)"),
    ('<w:p w:bogus="1"/>', schema_checks.ATTRIBUTE_NOT_ALLOWED, "w:p (w:bogus)"),
    ('<w:p><w:r><w:rPr><w:bogus/></w:rPr></w:r></w:p>', schema_checks.NOT_EXPECTED, "w:bogus"),
    ('<w:tbl><w:tblPr/></w:tbl>', schema_checks.MISSING_CHILD, "w:tbl"),
])
def test_each_kind_of_fault_is_a_plain_warning_with_the_names_in_the_excerpt(body, kind, excerpt):
    findings = _check(_document(body), "docx")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is checks.Severity.WARNING and finding.check == "docx.schema"
    assert finding.key.startswith(kind + ":") and finding.excerpt.startswith(excerpt)
    assert finding.message.startswith("Office may repair this part: ") and finding.fix
    assert "w:" not in finding.message and "middle" not in finding.message
    assert finding.location.startswith("part /word/document.xml, near (approximately) /w:document/w:body/")


def test_one_finding_per_kind_with_its_count_and_a_key_without_it():
    one = _check(_document('<w:p><w:pPr><w:jc w:val="middle"/></w:pPr></w:p>'), "docx")
    three = _check(_document('<w:p><w:pPr><w:jc w:val="middle"/></w:pPr></w:p>' * 3
                             + '<w:p><w:pPr><w:jc/></w:pPr></w:p>'), "docx")
    assert len(three) == 2 and "(3 times)" in three[0].message
    assert one[0].key == three[0].key and three[1].key != three[0].key


def test_markup_compatibility_and_office_extensions_are_read_as_office_reads_them():
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    w14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    vendor = 'xmlns:v1="urn:example:vendor"'
    body = ('<w:p w14:paraId="1A2B3C4D" w14:textId="77777777"><w:r><w:t>x</w:t></w:r></w:p>'
            '<mc:AlternateContent><mc:Choice Requires="w14"><w14:unknownThing/></mc:Choice>'
            '<mc:Fallback><w:p/></mc:Fallback></mc:AlternateContent>'
            '<mc:AlternateContent><mc:Choice Requires="w14"><w:bogus/></mc:Choice></mc:AlternateContent>'
            '<w:p><w:r><v1:mark/><w:t>y</w:t></w:r></w:p>')
    findings = _check(_document(body, extra_ns=f"{mc} {w14} {vendor}", root_attrs='mc:Ignorable="v1"'), "docx")
    assert findings == []


def test_an_extension_keeps_its_content_and_an_emptied_one_goes():
    x14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
    styles = (f'<styleSheet xmlns="{schema_checks.S}" xmlns:mc="http://schemas.openxmlformats.org/markup-'
              f'compatibility/2006" xmlns:x14ac="urn:example:ignored" mc:Ignorable="x14ac">'
              f'<extLst><ext uri="{{EB79DEF2-80B8-43e5-95BD-54CBDDF9020C}}" xmlns:x14="{x14}">'
              '<x14:slicerStyles defaultSlicerStyle="SlicerStyleLight1"/></ext>'
              '<ext uri="{0}"><x14ac:gone/></ext></extLst></styleSheet>')
    assert _check(_package("xlsx", {"xl/styles.xml": styles}), "xlsx") == []


def test_what_office_accepts_is_tolerated_and_nothing_more():
    margins = '<w:sectPr><w:pgMar w:top="1" w:right="1" w:bottom="1" w:left="1"/></w:sectPr>'
    assert _check(_document(margins), "docx") == []
    shared = (f'<sst xmlns="{schema_checks.S}" count="1" uniqueCount="1"><si><t xml:space="preserve"> a </t></si>'
              "</sst>")
    assert _check(_package("xlsx", {"xl/sharedStrings.xml": shared}), "xlsx") == []
    a = schema_checks.A

    def bullet(value: str) -> bytes:
        return _package("pptx", {"ppt/theme/theme1.xml": (
            f'<a:theme xmlns:a="{a}" name="t"><a:themeElements><a:clrScheme name="c">' + "".join(
                f'<a:{slot}><a:srgbClr val="000000"/></a:{slot}>' for slot in (
                    "dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
                    "hlink", "folHlink"))
            + '</a:clrScheme><a:fontScheme name="f"><a:majorFont><a:latin typeface="A"/><a:ea typeface=""/>'
            '<a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="A"/><a:ea typeface=""/>'
            '<a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="m"><a:fillStyleLst>'
            + '<a:noFill/>' * 3 + '</a:fillStyleLst><a:lnStyleLst>' + '<a:ln/>' * 3 + '</a:lnStyleLst>'
            '<a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3 + '</a:effectStyleLst>'
            '<a:bgFillStyleLst>' + '<a:noFill/>' * 3 + '</a:bgFillStyleLst></a:fmtScheme></a:themeElements>'
            f'<a:objectDefaults><a:spDef><a:spPr/><a:bodyPr/><a:lstStyle><a:lvl1pPr><a:buSzPct val="{value}"/>'
            '</a:lvl1pPr></a:lstStyle></a:spDef></a:objectDefaults></a:theme>')})

    assert _check(bullet("45000"), "pptx") == [] and _check(bullet("45%"), "pptx") == []
    assert [f.key for f in _check(bullet("abc"), "pptx")] == ["bad_value:a:buSzPct:val"]


def test_a_part_past_the_cap_is_a_note(monkeypatch):
    monkeypatch.setattr(schema_checks, "SCHEMA_MAX_BYTES", 50)
    findings = _check(_document('<w:p><w:pPr><w:jc w:val="middle"/></w:pPr></w:p>'), "docx")
    assert [(f.severity, f.key) for f in findings] == [(checks.Severity.INFO, "skipped")]


def test_a_strict_package_is_a_note_and_other_roots_are_not_judged():
    strict = _package("docx", {"word/document.xml": '<w:document xmlns:w="http://purl.oclc.org/ooxml/'
                                                    'wordprocessingml/main"><w:bogus/></w:document>'})
    assert [f.key for f in _check(strict, "docx")] == ["strict"]
    other = _package("docx", {"word/people.xml": '<w15:people xmlns:w15="http://schemas.microsoft.com/office/'
                                                 'word/2012/wordml"><w15:bogus/></w15:people>',
                              "customXml/item1.xml": f'<w:document xmlns:w="{W}"><w:bogus/></w:document>'})
    assert _check(other, "docx") == []
    maps = _package("xlsx", {"xl/xmlMaps.xml": f'<MapInfo xmlns="{schema_checks.S}" SelectionNamespaces="">'
                                               '<Schema ID="S1"><anything/></Schema></MapInfo>'})
    assert _check(maps, "xlsx") == []


def test_the_check_stops_at_its_time_budget(monkeypatch):
    monkeypatch.setattr(schema_checks, "SCHEMA_BUDGET_S", 0.5)
    slow = "".join('<w:p><w:pPr/><w:pPr/></w:p>' for _ in range(4))
    began = time.monotonic()
    findings = _check(_package("docx", {"word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{slow}'
                                                             "</w:body></w:document>",
                                        "word/styles.xml": f'<w:styles xmlns:w="{W}"/>'}), "docx")
    assert time.monotonic() - began < 2
    assert [(f.severity, f.key, f.part) for f in findings] == [(checks.Severity.INFO, "stopped", "word/document.xml")]


def test_the_schemas_compile_once_per_process():
    schema_checks._schema("pptx")
    hits = schema_checks._schema.cache_info().hits
    schema_checks._schema("pptx")
    assert schema_checks._schema.cache_info().hits == hits + 1


def test_the_schema_set_never_reads_outside_its_folder(tmp_path):
    assert schema_checks._local_file((schema_checks.ECMA_DIR / "wml.xsd").as_uri())
    for url in ("http://www.example.com/x.xsd", (tmp_path / "x.xsd").as_uri(),
                (schema_checks.SCHEMA_DIR.parent / "schema_checks.py").as_uri(), "ftp://host/x.xsd"):
        assert schema_checks._local_file(url) is None
    (tmp_path / "x.xsd").write_text('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>')
    for location in ("http://www.example.com/x.xsd", (tmp_path / "x.xsd").as_uri(), "missing.xsd"):
        with pytest.raises(schema_checks.SchemaSetError):
            schema_checks.compile_schema('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                                         f'<xs:import namespace="urn:x" schemaLocation="{location}"/></xs:schema>',
                                         "probe.xsd")


def test_the_check_runs_in_check_document_and_after_a_write_that_patched_xml():
    for fmt in ("docx", "pptx"):
        spec = checks._REGISTRY[f"{fmt}.schema"]
        assert spec in checks._selected(fmt, checks.AFTER_WRITE, rendered=True) and spec.baseline
    assert checks._REGISTRY["xlsx.schema"].final_only
    bad = _document('<w:p><w:pPr><w:jc w:val="middle"/></w:pPr></w:p>')
    for ops, expected in ((None, 1), ({"xml_patch", "add_paragraph"}, 1), ({"add_paragraph"}, 0), (set(), 0)):
        with checks.CheckContext("x.docx", "docx", data=bad, ops=ops) as ctx:
            assert len(schema_checks.check_schema(ctx)) == expected, ops


def test_a_patch_that_breaks_the_schema_is_told_after_the_write(tmp_path, monkeypatch):
    import asyncio

    import pptx

    from powerpoint import handle_write_pptx

    async def nothing(*_args, **_kw):
        return None

    monkeypatch.setattr("shared._resolve_path", lambda p, **_kw: _async(p))
    monkeypatch.setattr("shared._push_preview", nothing)
    monkeypatch.setattr("shared._notify_file_written", nothing)
    path = tmp_path / "deck.pptx"
    deck = pptx.Presentation()
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "Title"
    deck.save(path)
    out = asyncio.run(handle_write_pptx({"path": str(path), "operations": [
        {"op": "xml_patch", "slide_index": 0, "xpath": "//p:sp[1]/p:txBody/a:p[1]", "action": "insert_child",
         "xml": '<a:pPr algn="middle"/>'}]}))
    assert "Checks: 1 warning" in out and "WARNING pptx.schema" in out, out
    assert "a value is not one the schema allows" in out and "(algn) value 'middle'" in out


async def _async(value):
    return value


def test_the_ecma_files_are_the_published_ones():
    import hashlib

    notice = (schema_checks.ECMA_DIR / "NOTICE").read_text()
    files = sorted(schema_checks.ECMA_DIR.glob("*.xsd"))
    assert len(files) == 26
    for path in files:
        assert f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" in notice


def test_a_validation_past_its_time_does_not_stop_the_next():
    """The shared schema stays held while a validation that ran past its
    time goes on; the next check compiles its own."""
    data = _document('<w:p><w:pPr><w:jc w:val="middle"/></w:pPr></w:p>')
    taken = schema_checks._LOCK.acquire(blocking=False)
    try:
        findings = _check(data, "docx")
    finally:
        if taken:
            schema_checks._LOCK.release()
    assert schema_checks._LOCK.locked() or taken
    assert [f.key for f in findings] == ["bad_value:w:jc:w:val"]


def test_the_check_keeps_to_its_callers_deadline():
    slow = "".join('<w:p><w:pPr/><w:pPr/></w:p>' for _ in range(4))
    data = _package("docx", {"word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{slow}</w:body></w:document>'})
    began = time.monotonic()
    with checks.CheckContext("x.docx", "docx", data=data) as ctx:
        ctx.deadline = time.monotonic() + 0.4
        findings = schema_checks.check_schema(ctx)
    assert time.monotonic() - began < 1.5
    assert [f.key for f in findings] == ["stopped"]


def test_the_module_registers_as_every_check_module_does():
    """The registry fixture re-registers a check module imported inside it
    through ``register_all``."""
    saved = checks._REGISTRY.pop("docx.schema")
    try:
        schema_checks.register_all()
        assert checks._REGISTRY["docx.schema"].fn is schema_checks.check_schema
    finally:
        checks._REGISTRY["docx.schema"] = saved


def test_after_a_patch_the_faults_the_file_had_are_told_apart(tmp_path, monkeypatch):
    """A file with its own schema fault, patched with a new one: the write
    lists the new fault as new and the old one as already in the file; a
    batch with no xml_patch reads no baseline for this check."""
    import asyncio
    import dataclasses

    import pptx

    from powerpoint import handle_write_pptx

    async def nothing(*_args, **_kw):
        return None

    monkeypatch.setattr("shared._resolve_path", lambda p, **_kw: _async(p))
    monkeypatch.setattr("shared._push_preview", nothing)
    monkeypatch.setattr("shared._notify_file_written", nothing)
    path = tmp_path / "deck.pptx"
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Title"
    slide.shapes.title.text_frame.paragraphs[0]._p.get_or_add_pPr().set("lvl", "99")
    deck.save(path)
    out = asyncio.run(handle_write_pptx({"path": str(path), "operations": [
        {"op": "xml_patch", "slide_index": 0, "xpath": "//p:sp[1]/p:txBody/a:p[1]/a:pPr", "action": "replace",
         "xml": '<a:pPr lvl="99" algn="middle"/>'}]}))
    assert "Checks: 1 warning; 1 already in the file before this edit" in out, out
    seen = []
    real = schema_checks.check_schema

    def spy(ctx):
        seen.append(ctx.ops)
        return real(ctx)

    monkeypatch.setitem(checks._REGISTRY, "pptx.schema", dataclasses.replace(checks._REGISTRY["pptx.schema"], fn=spy))
    asyncio.run(handle_write_pptx({"path": str(path), "operations": [
        {"op": "set_text", "slide_index": 0, "placeholder": "title", "text": "Again"}]}))
    assert seen and all(ops is not None and "xml_patch" not in ops for ops in seen)
