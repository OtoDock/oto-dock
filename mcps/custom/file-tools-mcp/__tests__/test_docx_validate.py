"""Every write_docx operation leaves a package Office opens: each one, run
from its catalogue examples, saves, re-opens with python-docx and passes the
structural package checks with no error. Removing a picture loses no part,
and pictures keep their own drawing ids."""

import asyncio
import io

import pytest

pytest.importorskip("docx")

import checks
import word
from ooxml import validate

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:v="urn:schemas-microsoft-com:vml"')


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops, create_new=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops, "create_new": create_new}))


def _errors(source) -> list[str]:
    """The package checks' errors on a path or on bytes."""
    if isinstance(source, bytes):
        ctx = checks.CheckContext("", "docx", data=source)
    else:
        ctx = checks.CheckContext(str(source), "docx")
    with ctx:
        return [f"{f.location}: {f.message}" for f in validate.validate_package(ctx)
                if f.severity is checks.Severity.ERROR]


def _with_fixture(op: dict, fixtures: dict[str, str]) -> dict:
    return {k: fixtures.get(v, v) if isinstance(v, str) else v for k, v in op.items()}


def _cases():
    for spec in word.CATALOG.ops.values():
        for n, example in enumerate(spec.examples):
            yield pytest.param(spec, example, id=f"{spec.name}-{n}")


@pytest.mark.parametrize("spec, example", list(_cases()))
def test_every_operation_saves_a_valid_package(tmp_path, spec, example):
    from docx import Document

    fixtures = word.contract_fixtures(str(tmp_path))
    f = tmp_path / "op.docx"
    # Two calls: an example's address names the document its setup made.
    _write(f, [_with_fixture(op, fixtures) for op in spec.setup], create_new=True)
    msg = _write(f, [_with_fixture(example, fixtures)])
    assert msg.startswith("Document saved") and "Changed nothing:" not in msg, msg
    Document(str(f))
    assert _errors(f) == []
    assert "Checks: no problems found." in msg or "ERROR" not in msg.split("Checks:", 1)[-1]


def _picture_doc(tmp_path):
    fixtures = word.contract_fixtures(str(tmp_path))
    f = tmp_path / "pictures.docx"
    _write(f, [{"op": "add_paragraph", "text": "before"},
               {"op": "add_image", "image_path": fixtures["logo.png"]},
               {"op": "add_table", "headers": ["A"], "rows": [["1"]]},
               {"op": "add_header", "text": "Head"}], create_new=True)
    msg = _write(f, [{"op": "add_image", "image_path": fixtures["logo.png"], "target": "t0/r1/c0"},
                     {"op": "add_image", "image_path": fixtures["logo.png"], "target": "header0", "position": "end"}])
    assert msg.startswith("Document saved"), msg
    return f, fixtures


@pytest.mark.parametrize("ops", [
    [{"op": "delete_paragraph", "target": "p1"}],
    [{"op": "set_paragraph_text", "target": "p1", "text": "no picture now"}],
    [{"op": "delete_table", "target": "t0"}],
    [{"op": "delete_paragraph", "target": "header0/p1"}],
])
def test_removing_a_picture_reports_nothing_lost(tmp_path, ops):
    f, _ = _picture_doc(tmp_path)
    msg = _write(f, ops)
    assert msg.startswith("Document saved") and "Checks: no problems found." in msg, msg
    assert _errors(f) == []


def test_pictures_keep_their_own_drawing_ids(tmp_path):
    import zipfile

    f, fixtures = _picture_doc(tmp_path)
    msg = _write(f, [{"op": "add_image", "image_path": fixtures["logo.png"], "target": "p0", "position": "before"},
                     {"op": "add_image", "image_path": fixtures["logo.png"], "target": "header0"}])
    assert "Checks: no problems found." in msg, msg
    with checks.CheckContext(str(f), "docx") as ctx:
        assert [x for x in validate.validate_package(ctx) if "drawing" in x.message] == []
    with zipfile.ZipFile(f) as zf:
        assert zf.read("word/document.xml").count(b"<wp:docPr ") == 3


def test_a_text_box_fallback_copy_repeats_its_ids_without_an_error():
    from docx import Document
    from docx.oxml import parse_xml

    box = ('<w:txbxContent><w:p w14:paraId="1A2B3C4D" w14:textId="77777777"><w:r><w:t>Box</w:t></w:r></w:p>'
           "</w:txbxContent>")
    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {NS} w14:paraId="0000AAAA"><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing>'
        f"<wps:txbx>{box}</wps:txbx></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>{box}"
        f"</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"))
    buf = io.BytesIO()
    doc.save(buf)
    assert _errors(buf.getvalue()) == []
