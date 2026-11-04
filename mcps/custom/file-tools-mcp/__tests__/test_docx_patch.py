"""xml_patch on Word: a guarded patch of a named part or inside one block.
A refused patch leaves the file byte for byte as it was (and the part in
memory too); a patch that lands passes the package checks."""

import asyncio
import re
import zipfile

import pytest

pytest.importorskip("docx")

import checks
import word
from formats.docx import patching
from ooxml import validate

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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


def _errors(path) -> list[str]:
    with checks.CheckContext(str(path), "docx") as ctx:
        return [f"{f.location}: {f.message}" for f in validate.validate_package(ctx)
                if f.severity is checks.Severity.ERROR]


def _xml(path, name="word/document.xml") -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode()


@pytest.fixture
def doc(tmp_path):
    f = tmp_path / "patch.docx"
    _write(f, [{"op": "add_heading", "text": "Payment terms", "level": 1},
               {"op": "add_paragraph", "text": "Within 45 days."},
               {"op": "add_table", "headers": ["Item"], "rows": [["Pens"]]},
               {"op": "add_page_number", "format": "Page {page}"},
               {"op": "add_header", "text": "Draft"}], create_new=True)
    _write(f, [{"op": "add_footnote", "target": "p1", "text": "Note A."},
               {"op": "add_comment", "target": "p0", "text": "Check."}])
    return f


def test_a_patch_on_a_named_part_lands_and_validates(doc):
    msg = _write(doc, [{"op": "xml_patch", "part": "styles", "xpath": "//w:style[@w:styleId='Heading1']/w:rPr",
                        "action": "insert_child", "xml": "<w:caps/>"}])
    assert msg.startswith("Document saved") and "inserted into 1 element in word/styles.xml" in msg, msg
    assert "<w:caps/>" in _xml(doc, "word/styles.xml")
    assert _errors(doc) == []


def test_a_patch_inside_a_target_block(doc):
    msg = _write(doc, [{"op": "xml_patch", "target": "p0", "xpath": "./w:pPr", "action": "insert_child",
                        "xml": "<w:keepNext/>", "expect_text": "Payment terms"},
                       {"op": "xml_patch", "target": "t0/r1/c0", "xpath": ".//w:t", "action": "replace",
                        "xml": "<w:t>Ink</w:t>"}])
    assert msg.startswith("Document saved"), msg
    xml = _xml(doc)
    assert "<w:keepNext/>" in xml and ">Ink<" in xml and ">Pens<" not in xml
    assert _errors(doc) == []


def test_a_new_paragraph_gets_its_ids(doc):
    msg = _write(doc, [{"op": "xml_patch", "target": "p1", "xpath": ".", "action": "insert_after",
                        "xml": "<w:p><w:r><w:t>Added by patch.</w:t></w:r></w:p>"}])
    assert msg.startswith("Document saved"), msg
    out = word.read_docx(str(doc))
    assert "Added by patch." in out
    import re

    assert re.search(r"\np2#[0-9A-F]{8} Added by patch\.", out), out


@pytest.mark.parametrize("op, why", [
    ({"xpath": "//w:p[1]", "action": "remove", "part": "[Content_Types].xml"}, "plumbing"),
    ({"xpath": "./..", "action": "remove", "target": "p0"}, "steps up"),
    ({"xpath": "./../w:p[2]", "action": "remove", "target": "p0"}, "steps up"),
    ({"xpath": "parent::*", "action": "remove", "target": "p0"}, "starts at the block"),
    ({"xpath": "./parent::*", "action": "remove", "target": "p0"}, "axis"),
    ({"xpath": "./following-sibling::w:p", "action": "remove", "target": "p0"}, "axis"),
    ({"xpath": "./ancestor::w:body", "action": "remove", "target": "p0"}, "axis"),
    ({"xpath": "./w:r | /w:document", "action": "remove", "target": "p0"}, "|"),
    ({"xpath": "//w:p", "action": "remove", "target": "p0"}, "starts at the block"),
    ({"xpath": "//w:fldChar", "action": "remove", "part": "footer0", "match": "all"}, "field"),
    ({"xpath": "//w:p[1]/w:r[1]", "action": "insert_after", "part": "document",
      "xml": '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'}, "field"),
    ({"xpath": "/w:document/w:body/w:p[1]/w:r[1]", "action": "insert_after", "part": "document",
      "xml": '<w:r><w:object><v:shape xmlns:v="urn:schemas-microsoft-com:vml"/></w:object></w:r>'}, "runs, fetches"),
    ({"xpath": "/w:document/w:body/w:p[2]/w:r[1]", "action": "insert_after", "part": "document",
      "xml": "<w:pPr><w:jc w:val=\"center\"/></w:pPr>"}, "schema"),
    ({"xpath": "/w:document/w:body/w:p[1]/w:r[1]", "action": "insert_after", "part": "document",
      "xml": '<w:r xmlns:s="http://purl.oclc.org/ooxml/wordprocessingml/main"><s:t>x</s:t></w:r>'}, "Strict"),
    ({"xpath": "/w:document/w:body/w:p[1]/w:r[1]", "action": "insert_before", "part": "document",
      "xml": "<w:keepNext/>"}, "never holds"),
    ({"xpath": ".", "action": "insert_child", "target": "p1", "xml": "<w:keepNext/>"}, "never holds"),
    ({"xpath": "//w:p[1]", "action": "remove", "part": "settings2"}, "no part"),
    ({"xpath": ".", "action": "insert_child", "target": "p1", "xml": "<w:pPr><w:pStyle/></w:pPr>"},
     "w:val"),
    ({"xpath": "/w:document/w:body/w:sectPr/w:pgSz", "action": "replace", "part": "document",
      "xml": '<w:pgSz w:w="abc" w:h="x"/>'}, "w:w"),
    ({"xpath": "/w:document/w:body/w:tbl/w:tr[2]/w:tc/w:p", "action": "remove", "part": "document"},
     "w:tc"),
    ({"xpath": "/w:footnotes/w:footnote[@w:id='1']", "action": "remove", "part": "footnotes"},
     "delete_note"),
    ({"xpath": "/w:document/w:body/w:p[2]/w:r[w:footnoteReference]", "action": "remove", "part": "document"},
     "delete_note"),
    ({"xpath": "/w:document/w:body/w:p[1]/w:r[w:commentReference]", "action": "remove", "part": "document"},
     "delete_comment"),
])
def test_a_refused_patch_leaves_the_file_byte_identical(doc, op, why):
    before = doc.read_bytes()
    msg = _write(doc, [{"op": "xml_patch", **op}])
    assert msg.startswith("Nothing saved") and why in msg, msg
    assert doc.read_bytes() == before


def test_a_refused_patch_leaves_the_part_in_memory_as_it_was(doc):
    from lxml import etree

    from formats.docx.package import open_document
    from results import OpCtx, OpError

    document = open_document(str(doc))
    job = word._DocxJob(document, word.Book.for_call(document, []))
    before = etree.tostring(document.element)
    with pytest.raises(OpError, match="field"):
        patching._op_xml_patch(job, OpCtx(0, "xml_patch"), {
            "xpath": "//w:p[1]/w:r[1]", "action": "insert_after",
            "xml": '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'})
    assert etree.tostring(document.element) == before


def test_the_block_guard_refuses_what_the_grammar_would_let_through(doc, monkeypatch):
    from formats.docx.package import open_document
    from ooxml import patch
    from results import OpCtx, OpError

    monkeypatch.setattr(patch, "check_xpath", lambda *a, **k: None)
    document = open_document(str(doc))
    job = word._DocxJob(document, word.Book.for_call(document, [(0, {"target": "p0"})]))
    with pytest.raises(OpError, match="outside the block it starts from"):
        patching._op_xml_patch(job, OpCtx(0, "xml_patch"), {"target": "p0", "xpath": "./..", "action": "remove"})


def test_a_block_path_names_one_element(doc):
    from formats.docx.package import open_document

    document = open_document(str(doc))
    cell_p = next(document.element.body.iter(f"{{{W_NS}}}tbl")).findall(f".//{{{W_NS}}}p")[1]
    path = patching.block_path(cell_p)
    assert path == "/w:document[1]/w:body[1]/w:tbl[1]/w:tr[2]/w:tc[1]/w:p[1]" or path.startswith("/w:document")
    from lxml import etree

    from ooxml import patch

    assert etree.XPath(path, namespaces=patch.namespaces())(document.element) == [cell_p]


def test_ids_written_by_a_patch_are_never_handed_out_again(doc):
    import re

    before = set(re.findall(r'w:id="(\d+)"', _xml(doc)))
    # The first replacement takes the next two ids; the patch writes the one
    # the second replacement would take.
    taken = str(max(int(i) for i in before) + 3)
    msg = _write(doc, [{"op": "search_and_replace", "find": "45", "replace": "60", "track": True},
                       {"op": "xml_patch", "target": "p1", "xpath": ".", "action": "insert_child",
                        "xml": f'<w:bookmarkStart w:id="{taken}" w:name="patched"/>'},
                       {"op": "xml_patch", "target": "p1", "xpath": ".", "action": "insert_child",
                        "xml": f'<w:bookmarkEnd w:id="{taken}"/>'},
                       {"op": "search_and_replace", "find": "Within", "replace": "In", "track": True}])
    assert msg.startswith("Document saved"), msg
    ids = [i for i in re.findall(r'w:id="(\d+)"', _xml(doc)) if i not in before]
    assert ids.count(taken) == 2 and len(ids) == len(set(ids)) + 1, ids


def test_a_patched_document_still_reads(doc):
    msg = _write(doc, [{"op": "xml_patch", "target": "p1", "xpath": ".", "action": "insert_child",
                        "xml": '<w:pPr><w:jc w:val="center"/></w:pPr>'}])
    assert msg.startswith("Document saved"), msg
    assert "Within 45 days." in word.read_docx(str(doc))


def test_a_note_id_a_mark_still_holds_is_never_handed_out_again(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    f = tmp_path / "dangling.docx"
    _write(f, [{"op": "add_paragraph", "text": "Within 45 days."}, {"op": "add_paragraph", "text": "Third."}],
           create_new=True)
    _write(f, [{"op": "add_footnote", "target": "p0", "text": "Note A."}])
    document = Document(str(f))
    notes = next(p for p in document.part.package.parts if str(p.partname) == "/word/footnotes.xml")
    root = parse_xml(notes.blob) if getattr(notes, "_element", None) is None else notes._element
    for note in root.findall(f"{{{W_NS}}}footnote"):
        if note.get(f"{{{W_NS}}}id") == "1":
            root.remove(note)
    if getattr(notes, "_element", None) is None:
        from lxml import etree

        notes._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    document.save(str(f))
    _write(f, [{"op": "add_footnote", "target": "p1", "text": "Note B."}])
    refs = re.findall(r'footnoteReference w:id="(\d+)"', _xml(f))
    assert len(refs) == 2 and len(set(refs)) == 2, refs


def test_a_theme_patch_changes_the_colour_later_operations_write(tmp_path):
    f = tmp_path / "theme.docx"
    _write(f, [{"op": "add_paragraph", "text": "Before."}], create_new=True)
    msg = _write(f, [{"op": "add_paragraph", "text": "Old accent.", "color": "accent1"},
                     {"op": "xml_patch", "part": "theme", "xpath": "/a:theme/a:themeElements/a:clrScheme/a:accent1/*",
                      "action": "replace", "xml": '<a:srgbClr val="123456"/>'},
                     {"op": "add_paragraph", "text": "New accent.", "color": "accent1"}])
    assert msg.startswith("Document saved"), msg
    colours = re.findall(r'<w:color w:val="([0-9A-F]{6})" w:themeColor="accent1"/>', _xml(f))
    assert colours[-1] == "123456", colours


def test_a_block_a_patch_replaced_is_named_as_gone(tmp_path):
    f = tmp_path / "gone.docx"
    _write(f, [{"op": "add_paragraph", "text": "One."}, {"op": "add_paragraph", "text": "Two."}], create_new=True)
    msg = _write(f, [{"op": "xml_patch", "target": "p1", "xpath": ".", "action": "replace",
                      "xml": "<w:p><w:r><w:t>Swapped.</w:t></w:r></w:p>"},
                     {"op": "set_paragraph_text", "target": "p1", "text": "Again."}])
    assert "p1 was deleted by op #0" in msg and "merge" not in msg, msg
