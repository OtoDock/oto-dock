"""Guarded XML patches on an open document: each action on python-docx and
python-pptx, an earlier edit seen and the save keeping the patch, every
refusal leaving the part as it was, and nothing that runs or fetches let in."""

import contextlib
import io
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).parent.parent))

from ooxml import patch
from results import OpError

docx = pytest.importorskip("docx")
pptx = pytest.importorskip("pptx")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
BODY = "word/document.xml"


def _doc(*texts):
    document = docx.Document()
    for text in texts:
        document.add_paragraph(text)
    return document


def _package(document):
    return document.part.package


def _texts(document):
    return [p.text for p in document.paragraphs]


def _snapshot(document) -> bytes:
    return etree.tostring(document.part._element)


def _refused(document, *args, match="one", fragment_in=""):
    before = _snapshot(document)
    with pytest.raises(patch.PatchError) as caught:
        patch.apply_patch(_package(document), *args, match=match)
    assert _snapshot(document) == before, "a refused patch changed the part"
    assert caught.value.hint and isinstance(caught.value, OpError)
    assert fragment_in in caught.value.message
    return caught.value


def test_replace_insert_and_remove_on_a_word_document():
    document = _doc("one", "two", "three")
    result = patch.apply_patch(_package(document), BODY, "//w:body/w:p[2]", "replace",
                               "<w:p><w:r><w:t>Δύο</w:t></w:r></w:p>")
    assert (result.part, result.matched, result.action, result.notes) == (BODY, 1, "replace", [])
    patch.apply_patch(_package(document), "/WORD/Document.xml", "//w:body/w:p[1]", "insert_after",
                      "<w:p><w:r><w:t>after one</w:t></w:r></w:p>")
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[last()]", "remove")
    assert _texts(document) == ["one", "after one", "Δύο"]


def test_an_earlier_edit_is_seen_and_the_save_keeps_the_patch():
    document = _doc("first")
    document.add_paragraph("added by an earlier operation")
    patch.apply_patch(_package(document), BODY, "//w:p[w:r/w:t='added by an earlier operation']", "insert_before",
                      "<w:p><w:r><w:t>patched</w:t></w:r></w:p>")
    buf = io.BytesIO()
    document.save(buf)
    reopened = docx.Document(io.BytesIO(buf.getvalue()))
    assert _texts(reopened) == ["first", "patched", "added by an earlier operation"]
    assert reopened.paragraphs[1].runs[0].text == "patched"


def test_insert_child_keeps_the_schema_order():
    document = _doc("x")
    paragraph = document.paragraphs[0]
    paragraph.style = "Heading 1"
    patch.apply_patch(_package(document), BODY, "//w:p[1]/w:pPr", "insert_child", '<w:jc w:val="center"/>')
    patch.apply_patch(_package(document), BODY, "//w:p[1]/w:pPr", "insert_child", "<w:keepNext/>")
    names = [etree.QName(c).localname for c in paragraph._p.pPr]
    assert names == ["pStyle", "keepNext", "jc"]


def test_match_all_and_its_limits():
    document = _doc("a", "b", "c")
    result = patch.apply_patch(_package(document), BODY, "//w:body/w:p", "insert_child",
                               '<w:pPr><w:jc w:val="right"/></w:pPr>', match="all")
    assert result.matched == 3
    assert "matched 3 elements and match is 'one'" in _refused(document, BODY, "//w:body/w:p", "remove").message


def test_a_slide_is_patched_too():
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Old"
    name = str(slide.part.partname)
    patch.apply_patch(prs.part.package, name, "//p:sp[1]/p:txBody/a:p/a:r/a:t", "replace",
                      "<a:t>Νέος τίτλος</a:t>")
    buf = io.BytesIO()
    prs.save(buf)
    assert pptx.Presentation(io.BytesIO(buf.getvalue())).slides[0].shapes.title.text == "Νέος τίτλος"


@pytest.mark.parametrize("part_name, fragment", [
    ("[Content_Types].xml", "plumbing"), ("word/_rels/document.xml.rels", "plumbing"),
    ("word/missing.xml", "has no part"),
])
def test_the_plumbing_and_unknown_parts_are_refused(part_name, fragment):
    document = _doc("x")
    _refused(document, part_name, "//*", "remove", fragment_in=fragment)


def test_a_binary_part_is_refused():
    document = _doc("x")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buf, "PNG")
    buf.seek(0)
    document.add_picture(buf)
    image = next(p for p in _package(document).iter_parts() if str(p.partname).startswith("/word/media/"))
    _refused(document, str(image.partname), "//*", "remove", fragment_in="not an XML part")


@pytest.mark.parametrize("xpath, fragment", [
    ("//w:p[", "not valid"), ("//w:p/w:r/w:t/text()", "must select elements"), ("//w:p/@w:rsidR", "matched no"),
    ("count(//w:p)", "calls count()"), ("//w:p[w:r[w:t]]", "nests a predicate"), ("//w:p[//w:t]", "searches the"),
    ("//w:p/following::w:p", "following axis"), ("/" + "/w:p" * 30, "more than 12 steps"),
    ("//w:tbl", "matched no element"), ("/w:document", "root element"), ("x" * 3000, "longer than"),
])
def test_bad_xpaths_are_refused(xpath, fragment):
    _refused(_doc("x"), BODY, xpath, "remove", fragment_in=fragment)


@pytest.mark.parametrize("xml, fragment", [
    ("<w:p>", "not well-formed"), ("text only", "text outside"), ("<w:p/>stray", "text outside"),
    ('<w:p mc:Ignorable="w99"/>', "not declared"), ("<?pi x?><w:p/>", "processing instruction"),
    ('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><w:p/>', "not well-formed"),
    ("<w:p><w:r><w:t>&e;</w:t></w:r></w:p>", "not well-formed"), ("<!-- c -->", "no element"),
])
def test_bad_fragments_are_refused(xml, fragment):
    _refused(_doc("x"), BODY, "//w:p[1]", "insert_after", xml, fragment_in=fragment)


def _field(instruction_pieces):
    runs = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    runs += "".join(f'<w:r><w:instrText xml:space="preserve">{piece}</w:instrText></w:r>'
                    for piece in instruction_pieces)
    runs += '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>x</w:t></w:r>'
    runs += '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    return f"<w:p>{runs}</w:p>"


@pytest.mark.parametrize("xml, fragment", [
    ('<w:p><w:fldSimple w:instr=" DDEAUTO excel x"><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>', "field element"),
    (_field(["DD", 'E "c:\\\\x"']), "field element"),
    (_field([' INCLUDEPICTURE "http://example.com/x.png" ']), "field element"),
    (_field([' INCLUDETEXT "\\\\\\\\server\\\\share\\\\x.docx" ']), "field element"),
    ('<w:altChunk r:id="rId1"/>', "w:altChunk"),
    ('<w:p><w:r><w:object><o:OLEObject ProgID="Package"/></w:object></w:r></w:p>', "embedded object"),
    ('<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic><pic:blipFill>'
     '<a:blip r:link="rId1"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>'
     "</w:r></w:p>", "r:link"),
    ('<w:p><w:r><w:pict><v:shape><v:imagedata src="http://example.com/x.png"/></v:shape></w:pict></w:r></w:p>',
     "VML"),
    ('<w:p><w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:entryMacro w:val="Evil"/></w:ffData></w:fldChar>'
     "</w:r></w:p>", "field element"),
    ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>IF </w:instrText></w:r>'
     '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>QUOTE "D"</w:instrText></w:r>'
     '<w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:instrText> = 1 "x" "y"</w:instrText></w:r>'
     '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>', "field element"),
])
def test_active_content_is_refused(xml, fragment):
    _refused(_doc("x"), BODY, "//w:p[1]", "insert_after", xml, fragment_in=fragment)


def test_a_field_already_in_the_document_is_never_rewritten():
    """A patch never edits a field: even a harmless DATE field's code is
    refused as a target."""
    document = _with_body(_field(["DATE"]))
    _refused(document, BODY, "//w:instrText", "replace", '<w:instrText xml:space="preserve">DDEAUTO a b'
             "</w:instrText>", fragment_in="holds a field")


def test_a_new_element_beside_a_field_is_pointed_to_the_sibling_route():
    """insert_child into a body or shape tree that holds a field is refused;
    the refusal names insert_after on a child that holds none, which does
    the same and passes."""
    document = _with_body(_field(["DATE"]) + "<w:p><w:r><w:t>Plain</w:t></w:r></w:p>")
    error = _refused(document, BODY, "//w:body", "insert_child", "<w:p><w:r><w:t>New</w:t></w:r></w:p>",
                     fragment_in="holds a field")
    assert "insert_after" in error.hint and "paragraph" in error.hint, error.hint
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[2]", "insert_after",
                      "<w:p><w:r><w:t>New</w:t></w:r></w:p>")
    assert _texts(document)[-1] == "New"
    # A field in a paragraph's own runs keeps the plain advice: no sibling route there.
    error = _refused(document, BODY, "//w:body/w:p[1]", "insert_child", "<w:pPr/>", fragment_in="holds a field")
    assert "insert_after" not in error.hint, error.hint

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text_frame.paragraphs[0]._p.append(etree.fromstring(
        '<a:fld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}" type="slidenum"><a:t>1</a:t></a:fld>'))
    with pytest.raises(patch.PatchError) as caught:
        patch.apply_patch(prs.part.package, str(slide.part.partname), "//p:spTree", "insert_child",
                          '<p:sp><p:nvSpPr><p:cNvPr id="9" name="b"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/></p:sp>')
    assert "holds a field" in caught.value.message
    assert "insert_after" in caught.value.hint and "another shape" in caught.value.hint, caught.value.hint


def test_a_chart_xpath_on_a_slide_says_where_the_chart_is():
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    with pytest.raises(patch.PatchError) as caught:
        patch.apply_patch(prs.part.package, str(slide.part.partname), "//c:catAx/c:delete", "remove")
    assert "matched no element" in caught.value.message
    assert "own part" in caught.value.message and "format_chart" in caught.value.hint, caught.value
    # Any other XPath that matches nothing keeps the plain advice.
    with pytest.raises(patch.PatchError) as caught:
        patch.apply_patch(prs.part.package, str(slide.part.partname), "//p:sp[@id='x']", "remove")
    assert "own part" not in caught.value.message and "format_chart" not in caught.value.hint
    # The frame's own pointer (c:chart) sits on the slide: no chart advice for it.
    with pytest.raises(patch.PatchError) as caught:
        patch.apply_patch(prs.part.package, str(slide.part.partname), "//c:chart", "remove")
    assert "own part" not in caught.value.message
    # A Word document has no chart operation: the message stays, the hint names no operation.
    error = _refused(_doc("x"), BODY, "//c:valAx", "remove", fragment_in="own part")
    assert "format_chart" not in error.hint, error.hint


def test_a_program_action_on_a_slide_is_refused():
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    name = str(slide.part.partname)
    before = etree.tostring(slide.part._element)
    with pytest.raises(patch.PatchError, match="slide action"):
        patch.apply_patch(prs.part.package, name, "//p:spTree", "insert_child",
                          '<p:sp><p:nvSpPr><p:cNvPr id="9" name="b"><a:hlinkClick r:id="" '
                          'action="ppaction://program?file=calc.exe"/></p:cNvPr><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                          "<p:spPr/></p:sp>")
    assert etree.tostring(slide.part._element) == before


def test_no_new_relationship_and_no_new_error():
    document = _doc("x")
    message = _refused(document, BODY, "//w:p[1]", "insert_after",
                       '<w:p><w:hyperlink r:id="rId99"><w:r><w:t>x</w:t></w:r></w:hyperlink></w:p>').message
    assert "relationship id rId99, which the part does not have" in message
    document.paragraphs[0].style = "Heading 1"
    order = _refused(document, BODY, "//w:p[1]/w:pPr/w:pStyle", "insert_before", '<w:jc w:val="left"/>').message
    assert "puts <jc> after" not in order and "schema puts it before" in order


def test_a_relationship_the_part_has_may_be_used():
    document = docx.Document()
    paragraph = document.add_paragraph()
    rid = document.part.relate_to("https://example.com", "http://schemas.openxmlformats.org/officeDocument/2006/"
                                  "relationships/hyperlink", is_external=True)
    patch.apply_patch(_package(document), BODY, "//w:p[1]", "insert_child",
                      f'<w:hyperlink r:id="{rid}"><w:r><w:t>site</w:t></w:r></w:hyperlink>')
    assert paragraph._p.xpath("./w:hyperlink")[0].get(f"{{{patch.R_NS}}}id") == rid


def test_actions_and_match_are_checked():
    document = _doc("x")
    _refused(document, BODY, "//w:p", "append", "<w:p/>", fragment_in="not a patch action")
    _refused(_doc("a"), BODY, "/w:document/descendant-or-self::*[self::w:body or self::w:p]", "remove",
             match="all", fragment_in="one match holds another")
    _refused(document, BODY, "//w:p", "remove", match="some", fragment_in="match is 'some'")
    _refused(document, BODY, "//w:p", "remove", "<w:p/>", fragment_in="remove takes no xml")
    _refused(document, BODY, "//w:p", "replace", fragment_in="needs the xml")


def test_the_saved_package_is_clean():
    from checks import CheckContext
    from ooxml import validate

    document = _doc("a", "b")
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[2]", "replace",
                      '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>Β</w:t></w:r></w:p>')
    buf = io.BytesIO()
    document.save(buf)
    assert "word/document.xml" in zipfile.ZipFile(buf).namelist()
    with CheckContext("", "docx", data=buf.getvalue()) as ctx:
        assert [f for f in validate.validate_package(ctx) if f.severity.value != "info"] == []


def test_duplicate_ids_in_the_xml_are_renumbered():
    document = _doc("x")
    w14 = "http://schemas.microsoft.com/office/word/2010/wordml"
    first = document.paragraphs[0]._p
    first.set(f"{{{w14}}}paraId", "1A2B3C4D")
    result = patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "insert_after",
                               f'<w:p xmlns:w14="{w14}" w14:paraId="1A2B3C4D"><w:r><w:t>copy</w:t></w:r></w:p>')
    assert result.notes == ["1 paragraph ids already in the document were renumbered"]
    copy = document.paragraphs[1]._p.get(f"{{{w14}}}paraId")
    assert copy != "1A2B3C4D" and len(copy) == 8


def test_a_part_the_library_keeps_as_bytes_is_patched_and_written_back():
    document = _doc("x")
    theme = next(p for p in _package(document).iter_parts() if str(p.partname).endswith("theme1.xml"))
    assert getattr(theme, "_element", None) is None
    patch.apply_patch(_package(document), str(theme.partname), "//a:clrScheme/a:dk2/a:srgbClr", "replace",
                      '<a:srgbClr val="1F3864"/>')
    assert b'val="1F3864"' in theme.blob
    buf = io.BytesIO()
    document.save(buf)
    assert b'val="1F3864"' in zipfile.ZipFile(buf).read("word/theme/theme1.xml")


@pytest.mark.parametrize("part_name", ["customXml/item1.xml", "word/vbaData.xml", "customUI/customUI14.xml"])
def test_macro_and_extension_parts_are_refused_by_name(part_name):
    _refused(_doc("x"), part_name, "//*", "remove", fragment_in="never edits")


# ------------------------------------------------- the review's bypasses

def _with_body(xml_paragraphs: str):
    """A document whose body holds the given paragraphs (raw XML)."""
    from docx.oxml.parser import parse_xml

    document = docx.Document()
    body = document.element.body
    wrapper = parse_xml(f'<w:body xmlns:w="{W}" xmlns:r="{patch.R_NS}">{xml_paragraphs}</w:body>')
    for child in list(wrapper):
        body.insert(len(body) - 1, child)
    return document


_PICTURE_FIELD = _field(['INCLUDEPICTURE "https://example.com/a.png" \\* MERGEFORMATINET'])


def test_a_refused_patch_on_two_targets_leaves_the_part_byte_identical():
    """Two targets in one parent, a two-element fragment, a refusal: the
    order of the body is exactly as before."""
    document = _doc("A", "B", "C", "D")
    _refused(document, BODY, "//w:body/w:p[position()=1 or position()=3]", "replace",
             '<w:p><w:r><w:t>n1</w:t></w:r></w:p><w:p><w:hyperlink r:id="rId999"/></w:p>', match="all")
    assert _texts(document) == ["A", "B", "C", "D"]


def test_a_field_is_never_retargeted():
    document = _with_body(_PICTURE_FIELD)
    _refused(document, BODY, "//w:instrText", "replace",
             '<w:instrText xml:space="preserve">INCLUDEPICTURE "\\\\attacker.example\\share\\x.png" \\d'
             "</w:instrText>", fragment_in="field")


def test_a_field_paragraph_is_never_replaced_or_removed():
    document = _with_body(_PICTURE_FIELD)
    _refused(document, BODY, "//w:body/w:p[1]", "replace", "<w:p><w:r><w:t>plain</w:t></w:r></w:p>",
             fragment_in="field")
    _refused(document, BODY, "//w:body/w:p[1]", "remove", fragment_in="field")
    _refused(document, BODY, "//w:body/w:p[1]", "insert_child", "<w:r><w:t>more</w:t></w:r>", fragment_in="field")


def test_text_slipped_into_a_field_code_changes_its_signature():
    """No field element is touched, but a run added before the field's
    separator would extend its instruction: refused."""
    document = _with_body(_PICTURE_FIELD)
    first_run = "//w:body/w:p[1]/w:r[w:instrText]"
    _refused(document, BODY, first_run, "insert_after", '<w:r><w:t xml:space="preserve"> \\\\evil\\s</w:t></w:r>',
             fragment_in="field")


def test_a_table_of_contents_is_never_traded_for_a_crafted_field():
    toc = ('<w:sdt><w:sdtContent><w:p>' + _field(["TOC \\o \"1-3\""])[5:-6]
           + "</w:p><w:p>" + _field(["PAGEREF _Toc1 \\h"])[5:-6] + "</w:p></w:sdtContent></w:sdt>")
    document = _with_body(toc)
    crafted = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
               '<w:r><w:instrText>QUOTE 68 68 69 65 85 84 79</w:instrText></w:r>'
               '<w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:instrText> c:\\\\x\\\\cmd.exe "/c calc"'
               '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    _refused(document, BODY, "//w:body/w:sdt", "replace", crafted, fragment_in="field")


def test_an_unterminated_field_is_active_content():
    root = etree.fromstring(
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText>DDEAUTO c:\\\\cmd.exe "/c calc"</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>x</w:t></w:r></w:p></w:body></w:document>')
    found = patch.active_content(root)
    assert any(kind == "an unterminated field" and "DDEAUTO" in detail for kind, detail in found), found


@pytest.mark.parametrize("instruction", ["RD \\\\attacker\\s\\a.docx \\f", "ASK x question", "FILLIN question",
                                         "DDE excel x", "INCLUDETEXT x", "LINK Excel.Sheet.8 x"])
def test_every_field_is_refused_in_the_xml(instruction):
    _refused(_doc("x"), BODY, "//w:p[1]", "insert_after",
             f'<w:p><w:fldSimple w:instr=" {instruction} "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>',
             fragment_in="field")


def _slide_with_shapes():
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for n in range(2):
        slide.shapes.add_textbox(n * 914400, 0, 914400, 914400).text_frame.text = f"s{n}"
    return prs, slide


def _shape(shape_id: int) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="New {shape_id}"/><p:cNvSpPr txBox="1"/><p:nvPr/>'
            '</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>new</a:t></a:r></a:p></p:txBody></p:sp>')


def _ids(root, tag):
    return [n.get("id") for n in root.iter(tag)]


def test_shape_ids_already_in_the_slide_are_renumbered():
    prs, slide = _slide_with_shapes()
    cnv = "{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr"
    existing = _ids(slide.part._element, cnv)
    result = patch.apply_patch(prs.part.package, str(slide.part.partname), "//p:spTree", "insert_child",
                               _shape(int(existing[-1])) + _shape(int(existing[-1]) + 1) + _shape(int(existing[-1])),
                               )
    ids = _ids(slide.part._element, cnv)
    assert len(ids) == len(set(ids)), ids
    assert result.notes and "shape ids" in result.notes[0]


def test_drawing_ids_already_in_the_part_are_renumbered():
    from PIL import Image

    document = docx.Document()
    buf = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buf, "PNG")
    buf.seek(0)
    document.add_picture(buf)
    rid = document.element.body.xpath(".//a:blip")[0].get(f"{{{patch.R_NS}}}embed")
    picture = (f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="1" cy="1"/><wp:docPr id="{{}}" name="x"/>'
               f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
               f'<pic:nvPicPr><pic:cNvPr id="0" name="x"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>'
               f'<a:blip r:embed="{rid}"/></pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic>'
               "</wp:inline></w:drawing></w:r></w:p>")
    existing = _ids(document.element, patch._DOC_PR)
    top = max(int(i) for i in existing)
    result = patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "insert_after",
                               picture.format(top + 1) + picture.format(top))
    ids = _ids(document.element, patch._DOC_PR)
    assert len(ids) == len(set(ids)) == len(existing) + 2, ids
    assert result.notes and "drawing ids" in result.notes[0]


_WEB_EXTENSION = "application/vnd.ms-office.webextension+xml"
_ACTIVEX = "application/vnd.ms-office.activeX+xml"


@pytest.mark.parametrize("template, content_type, reltype, xml", [
    ("/word/webextensions/webextension%d.xml", _WEB_EXTENSION,
     "http://schemas.microsoft.com/office/2011/relationships/webextension",
     '<we:webextension xmlns:we="http://schemas.microsoft.com/office/webextensions/webextension/2010/11">'
     '<we:reference id="x" version="1" store="en-US" storeType="OMEX"/></we:webextension>'),
    ("/word/activeX/activeX%d.xml", _ACTIVEX,
     "http://schemas.openxmlformats.org/officeDocument/2006/relationships/control",
     '<ax:ocx xmlns:ax="http://schemas.microsoft.com/office/2006/activeX" ax:classid="{8856F961}">'
     '<ax:ocxPr ax:name="Location" ax:value="https://example.com"/></ax:ocx>'),
    ("/word/stuff/item%d.xml", "application/xml",
     "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml", "<data><x>1</x></data>"),
])
def test_parts_are_refused_by_what_they_are_not_where_they_sit(template, content_type, reltype, xml):
    from ooxml import parts

    document = _doc("x")
    part, _ = parts.add_part(document.part, reltype, content_type, template=template, element=etree.fromstring(xml))
    before = part.blob
    with pytest.raises(patch.PatchError, match="never edits"):
        patch.apply_patch(_package(document), str(part.partname), "/*/*", "remove")
    assert part.blob == before


@pytest.mark.parametrize("fragment, label", [
    ('<p:timing><p:tnLst><p:par><p:cTn id="1"><p:childTnLst><p:cmd type="verb" cmd="0"><p:cBhvr>'
     '<p:cTn id="2"/><p:tgtEl><p:spTgt spid="2"/></p:tgtEl></p:cBhvr></p:cmd></p:childTnLst></p:cTn></p:par>'
     "</p:tnLst></p:timing>", "verb"),
])
def test_a_timing_that_runs_an_object_is_refused(fragment, label):
    prs, slide = _slide_with_shapes()
    before = etree.tostring(slide.part._element)
    with pytest.raises(patch.PatchError, match=label):
        patch.apply_patch(prs.part.package, str(slide.part.partname), "/p:sld", "insert_child", fragment)
    assert etree.tostring(slide.part._element) == before


@pytest.mark.parametrize("action", [" ppaction://program?file=calc.exe", "PPACTION://PROGRAM?file=x",
                                    "ppaction:// program?file=x", "ppaction://hlinkpres?slideindex=1",
                                    "ppaction://hlinkfile"])
def test_a_slide_action_that_opens_something_is_refused_however_it_is_spelled(action):
    prs, slide = _slide_with_shapes()
    fragment = _shape(77).replace('name="New 77"/>', f'name="New 77"><a:hlinkClick r:id="" action="{action}"/>'
                                                     "</p:cNvPr>")
    with pytest.raises(patch.PatchError, match="slide action"):
        patch.apply_patch(prs.part.package, str(slide.part.partname), "//p:spTree", "insert_child", fragment)


def _big_document(paragraphs: int):
    return _doc(*(f"paragraph {n}" for n in range(paragraphs)))


def _with_long_attributes(paragraphs: int, length: int):
    """The review's part whose text is in attributes: one w:rsidR of
    ``length`` characters on each paragraph."""
    document = _doc(*("p" for _ in range(paragraphs)))
    for paragraph in document.paragraphs:
        paragraph._p.set(f"{{{W}}}rsidR", "a" * length)
    return document


def _with_many_attributes(paragraphs: int, count: int):
    """Parsed from text: lxml's own set() would take time growing with
    the square of an element's attributes."""
    attributes = " ".join(f'x:a{n}="y"' for n in range(count))
    paragraph = f'<w:p xmlns:x="urn:x" {attributes}><w:r><w:t>p</w:t></w:r></w:p>'
    return _with_body(paragraph * paragraphs)


def _with_long_comments(paragraphs: int, length: int):
    document = _doc(*("p" for _ in range(paragraphs)))
    for paragraph in document.paragraphs:
        paragraph._p.append(etree.Comment("a" * length))
    return document


_NEEDLE = "a" * 1950 + "b"
_UNBOUNDED = [(lambda: _big_document(1000), xpath) for xpath in (
    "//w:p[descendant-or-self::node()/parent::*/descendant-or-self::node()/parent::*]",
    "//w:p[.//w:t]", "//w:p[../w:p]", "//w:p[ancestor::w:body]", "//*[w:r/descendant::w:t]",
    "//*//*//*//*//*//*", "//*/..//*", "/w:document/descendant::*/descendant::w:p", "//*//*",
    "//*|//*|//*", "//*/../*/../*", "//w:p/parent::*",
)] + [
    # The text of a part in its attributes or comments is priced like its
    # element text (the review's 14 s and 18 s shapes).
    (lambda: _with_long_attributes(2000, 10_000), f"//w:p[contains(@w:rsidR, '{_NEEDLE}')]"),
    (lambda: _with_long_attributes(2000, 10_000), f"//w:p/@*[contains(., '{_NEEDLE}')]"),
    (lambda: _with_many_attributes(1000, 1000), "//@*[" + " or ".join([". = 'x'"] * 120) + "]"),
    (lambda: _with_many_attributes(4000, 250), "//@*[" + " or ".join([". = 'x'"] * 120) + "]"),
    (lambda: _with_many_attributes(100, 10_000), "//w:body/w:p[1]"),
    (lambda: _with_long_comments(20, 1_000_000), f"//w:p/node()[contains(., '{_NEEDLE}')]"),
]


@pytest.mark.parametrize("build, xpath", _UNBOUNDED, ids=[x[:60] for _, x in _UNBOUNDED])
def test_an_xpath_whose_cost_has_no_bound_is_refused_before_it_runs(build, xpath):
    import time

    document = build()
    before = _snapshot(document)
    began = time.monotonic()
    with pytest.raises(patch.PatchError):
        patch.apply_patch(_package(document), BODY, xpath, "remove", match="all")
    assert time.monotonic() - began < 1.0
    assert _snapshot(document) == before


def test_the_size_counts_attributes_comments_and_their_text():
    """Elements, attributes, comments and processing instructions are
    nodes; their text, attribute values and tails are characters. A
    library paragraph's own text is its text node, not the paragraph's
    whole text."""
    root = etree.fromstring(f'<w:body xmlns:w="{W}"><w:p w:a="12345" w:b="1"><!--abcd--><?pi xyz?>'
                            "<w:r><w:t>hello</w:t></w:r>tail</w:p></w:body>")
    assert patch.part_size(root) == (8, 4, 5 + 1 + 4 + 3 + 5 + 4)
    document = _doc("x" * 1000)
    assert patch.part_size(document.part._element)[2] < 1100


def test_an_element_with_too_many_attributes_refuses_its_part():
    document = _with_many_attributes(1, patch.ATTRIBUTES_MAX + 1)
    _refused(document, BODY, "//w:body/w:p[1]", "remove", fragment_in="attributes")


@pytest.mark.parametrize("xpath", ["//w:p[POSITION() = 1]", "//w:p[Contains(., 'x')]", "//w:p[NOT(w:r)]",
                                   "//w:p[Text()]", "//w:body/Node()"])
def test_a_function_name_in_another_case_is_refused_by_name(xpath):
    with pytest.raises(patch.PatchError, match="which a patch does not allow"):
        patch.check_xpath(xpath, 1000, 6, 50_000)


def test_every_xpath_the_grammar_allows_stays_fast_on_a_large_part():
    import time

    for xpath in ("//w:p[w:r/w:t='paragraph 2999']", "//w:p[contains(string(), 'paragraph 1')][1]",
                  "//w:p[w:r][w:r][w:r][w:r][w:r][w:r]", "//w:body/w:p[position() > 2990]/w:r",
                  "//*/*/*/*/*", "//w:p[not(@w:rsidR)]", "//w:p[contains(string(), 'x')]/*",
                  "//w:p[w:r]/w:r/w:t", "/*/*/*/*/*/*/*/*/*/*/*", "//w:p/*[not(@w:val)]",
                  "/w:document/descendant::*[not(@w:val)]", "//w:p[position() = last()]"):
        document = _big_document(3000)
        patch.check_xpath(xpath, *patch.part_size(document.part._element))
        began = time.monotonic()
        with contextlib.suppress(patch.PatchError):
            patch.apply_patch(_package(document), BODY, xpath, "remove", match="all")
        assert time.monotonic() - began < 2.0, xpath


def test_an_inserted_element_declares_only_the_namespaces_it_uses():
    document = _doc("x")
    before = len(_snapshot(document))
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "insert_after", "<w:p/>")
    grown = len(_snapshot(document)) - before
    assert grown < 120, grown


# ------------------------------------------------- known bypass shapes

def _toc_document():
    return _with_body(_field(['TOC \\o "1-3"']) + _field(["PAGEREF _Toc1 \\h"]))


_BYPASSES = [
    # (document builder, xpath, action, xml, match)
    (_toc_document, "//w:body/w:p[1]", "replace", "<w:p><w:r><w:t>gone</w:t></w:r></w:p>", "one"),
    (lambda: _with_body(_PICTURE_FIELD), "//w:body/w:p[1]/w:r[last()]", "remove", None, "one"),
    (lambda: _with_body(_PICTURE_FIELD), "//w:body/w:p[1]/w:r[1]", "insert_before",
     '<w:r><w:fldChar w:fldCharType="begin"/></w:r>', "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after",
     '<w:p><w:r><w:instrText>DDE x</w:instrText></w:r></w:p>', "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after",
     '<w:p><w:r><w:delInstrText>INCLUDETEXT x</w:delInstrText></w:r></w:p>', "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after",
     '<w:p xmlns:z="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><z:fldSimple z:instr="DDE x"/></w:p>',
     "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after",
     '<w:p><mc:AlternateContent><mc:Choice Requires="w14"><w:r><w:t>a</w:t></w:r></mc:Choice><mc:Fallback>'
     '<w:fldSimple w:instr="DDEAUTO x"/></mc:Fallback></mc:AlternateContent></w:p>', "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after",
     '<w:p><w:r><w:pict><v:shape><v:imagedata o:href="\\\\attacker\\s\\x.png"/></v:shape></w:pict></w:r></w:p>',
     "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after", '<w:altChunk r:id="rId1"/>', "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after",
     '<w:p><w:r><w:object><o:OLEObject ProgID="Package" ShapeID="1"/></w:object></w:r></w:p>', "one"),
    (lambda: _doc("x"), "//w:p[1]", "insert_after", '<w:subDoc r:id="rId1"/>', "one"),
    (lambda: _doc("a", "b", "c"), "//w:body/w:p[position()<3]", "replace",
     "<w:p/><w:p><w:hyperlink r:id='rId404'/></w:p>", "all"),
]


@pytest.mark.parametrize("build, xpath, action, xml, match", _BYPASSES)
def test_known_bypass_shapes_are_refused_and_change_nothing(build, xpath, action, xml, match):
    document = build()
    _refused(document, BODY, xpath, action, xml, match=match)


def test_editing_text_beside_an_existing_text_box_is_accepted():
    """A paragraph holding a text box (VML in its Fallback) may be replaced
    by the same paragraph with other text: its active content is unchanged."""
    box = ('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing/></mc:Choice><mc:Fallback><w:pict>'
           '<v:shape id="Box 1" style="width:100pt;height:50pt"><v:textbox><w:txbxContent><w:p><w:r><w:t>{}</w:t>'
           "</w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>")
    paragraph = '<w:p xmlns:mc="{}" xmlns:v="{}"><w:r><w:t>{}</w:t></w:r>' + box + "</w:p>"
    document = _with_body(paragraph.format(patch.MC_NS, patch.V_NS, "before", "in the box"))
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "replace",
                      paragraph.format(patch.MC_NS, patch.V_NS, "after", "in the box, edited"))
    assert "after" in _texts(document)[0]
    _refused(document, BODY, "//w:body/w:p[1]", "replace",
             paragraph.format(patch.MC_NS, patch.V_NS, "x", "y").replace("width:100pt", "width:900pt"),
             fragment_in="VML")


def test_a_refusal_keeps_the_librarys_objects_attached():
    document = _doc("A", "B", "C")
    held = document.paragraphs[1]
    _refused(document, BODY, "//w:body/w:p[2]", "replace", '<w:p><w:hyperlink r:id="rId77"/></w:p>')
    held.add_run(" still attached")
    assert _texts(document) == ["A", "B still attached", "C"]


# ------------------------------------------------- Strict namespaces

_S = "http://purl.oclc.org/ooxml/wordprocessingml/main"
_SR = "http://purl.oclc.org/ooxml/officeDocument/relationships"
_STRICT = f'xmlns:s="{_S}" xmlns:sr="{_SR}"'


@pytest.mark.parametrize("xml", [
    f'<w:p {_STRICT}><s:fldSimple s:instr="DDEAUTO c:\\\\windows\\\\system32\\\\cmd.exe &quot;/c calc&quot;"/></w:p>',
    f'<w:p {_STRICT}><s:r><s:fldChar s:fldCharType="begin"/></s:r><s:r><s:instrText>INCLUDEPICTURE '
    '"\\\\\\\\a\\\\s\\\\x.png"</s:instrText></s:r><s:r><s:fldChar s:fldCharType="end"/></s:r></w:p>',
    f'<s:altChunk {_STRICT} sr:id="rId1"/>',
    f'<w:p {_STRICT}><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic><pic:blipFill>'
    '<a:blip sr:link="rId777"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>'
    "</w:r></w:p>",
    f'<w:p {_STRICT} mc:Ignorable="s"><w:r><w:t>only declared</w:t></w:r></w:p>',
])
def test_a_strict_namespace_in_the_xml_is_refused(xml):
    _refused(_doc("x"), BODY, "//w:p[1]", "insert_after", xml, fragment_in="Strict")


def test_a_strict_element_already_in_the_part_is_never_a_target():
    document = _with_body(f'<w:p><w:r><w:t>a</w:t></w:r><s:fldSimple xmlns:s="{_S}" s:instr="DATE"/></w:p>')
    _refused(document, BODY, "//w:body/w:p[1]", "remove", fragment_in="Strict")
    _refused(document, BODY, "//w:body/w:p[1]", "replace", "<w:p><w:r><w:t>b</w:t></w:r></w:p>",
             fragment_in="Strict")


# ------------------------------------------------- full-length signatures

_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def test_a_program_action_repointed_to_another_relationship_is_refused():
    prs, slide = _slide_with_shapes()
    run = slide.part.relate_to("notepad.exe", _RT + "hyperlink", is_external=True)
    evil = slide.part.relate_to("\\\\attacker.example\\share\\evil.exe", _RT + "hyperlink", is_external=True)
    c_nv_pr = slide.part._element.xpath("//p:sp[1]/p:nvSpPr/p:cNvPr")[0]
    c_nv_pr.append(etree.fromstring(f'<a:hlinkClick xmlns:a="{patch.A_NS}" xmlns:r="{patch.R_NS}" r:id="{run}" '
                                    'action="ppaction://program"/>'))
    before = etree.tostring(slide.part._element)
    with pytest.raises(patch.PatchError, match="slide action"):
        patch.apply_patch(prs.part.package, str(slide.part.partname), "//a:hlinkClick", "replace",
                          f'<a:hlinkClick r:id="{evil}" action="ppaction://program"/>')
    assert etree.tostring(slide.part._element) == before


def _two_outside_pictures():
    document = _doc("x")
    logo = document.part.relate_to("https://example.com/logo.png", _RT + "image", is_external=True)
    leak = document.part.relate_to("file://///attacker.example/share/x.png", _RT + "image", is_external=True)
    return document, logo, leak


def test_a_vml_picture_repointed_to_another_outside_picture_is_refused():
    document, logo, leak = _two_outside_pictures()
    body = document.element.body
    body.insert(0, etree.fromstring(
        f'<w:p xmlns:w="{W}" xmlns:v="{patch.V_NS}" xmlns:r="{patch.R_NS}"><w:r><w:pict><v:shape>'
        f'<v:imagedata r:id="{logo}"/></v:shape></w:pict></w:r></w:p>'))
    message = _refused(document, BODY, "//v:imagedata", "replace", f'<v:imagedata r:id="{leak}"/>').message
    assert "v:imagedata[r:id=image:outside file://///attacker.example/share/x.png]" in message


def test_a_linked_picture_repointed_to_another_outside_picture_is_refused():
    document, logo, leak = _two_outside_pictures()
    picture = ('<w:p xmlns:w="{w}" xmlns:wp="{wp}" xmlns:a="{a}" xmlns:pic="http://schemas.openxmlformats.org/'
               'drawingml/2006/picture" xmlns:r="{r}"><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>'
               '<pic:pic><pic:blipFill><a:blip r:link="{rid}"/></pic:blipFill></pic:pic></a:graphicData>'
               "</a:graphic></wp:inline></w:drawing></w:r></w:p>")
    document.element.body.insert(0, etree.fromstring(picture.format(w=W, wp=patch.WP_NS, a=patch.A_NS,
                                                                    r=patch.R_NS, rid=logo)))
    _refused(document, BODY, "//a:blip", "replace", f'<a:blip r:link="{leak}"/>', fragment_in="r:link")


def test_text_slipped_into_a_long_field_code_changes_its_signature():
    """A field code longer than any shortened signature: a run added after a
    bookmark inside the code still changes it."""
    url = "https://example.com/" + "a" * 200 + ".png"
    paragraph = _field([f'INCLUDEPICTURE "{url}" \\d']).replace(
        '<w:r><w:fldChar w:fldCharType="separate"/>',
        '<w:bookmarkStart w:id="0" w:name="in_code"/><w:r><w:fldChar w:fldCharType="separate"/>')
    document = _with_body(paragraph)
    message = _refused(document, BODY, "//w:bookmarkStart", "insert_after",
                       '<w:r><w:t xml:space="preserve"> \\\\evil\\s</w:t></w:r>', fragment_in="field").message
    assert len(message) < 600


# ------------------------------------------------- absolute paths in predicates

@pytest.mark.parametrize("xpath", [
    "//w:p[/*/*/*/@* = /*/*/*/*/*]", "//w:p[contains(/, 'zz')]", "//w:p[ / ]", "//w:p[w:r = /*]",
    "//w:p[not(/*/*)]", "//w:p[w:r and /*]", "//w:p[w:r or/*]", "//w:p[w:r|/*]", "//w:p[* * /*]",
    "//w:p[position() * /*]", "//w:p[string(/)]", "//w:p[@w:x=/*/@w:y]", "//w:p[contains(w:r, /)]",
    "//w:p = /*/*/*/*", "//w:p[1] = /*/*/*", "/*/*[1]/w:p/text() = /*",
])
def test_an_absolute_path_inside_or_after_the_path_is_refused_before_it_runs(xpath):
    import time

    with pytest.raises(patch.PatchError):
        patch.check_xpath(xpath, 1000, 6)
    document = _big_document(1000)
    began = time.monotonic()
    _refused(document, BODY, xpath, "remove", match="all", fragment_in="")
    assert time.monotonic() - began < 1.0


@pytest.mark.parametrize("xpath", [
    "//*[not(@w:zzz)]", "//w:*[not(@w:zzz)]", "//node()[1]", "//*/*[not(@w:zzz)]", "//*[*]",
    "//*/self::*[w:r]", "/w:document/descendant::*/*[w:r]",
])
def test_a_predicate_on_every_element_after_a_search_is_refused(xpath):
    """libxml2 takes time growing with the cube of the part for these
    (measured); the same predicate on a named step stays linear."""
    with pytest.raises(patch.PatchError, match="tests every element"):
        patch.check_xpath(xpath, 1000, 6)


@pytest.mark.parametrize("xpath", ["//w:p[contains(., string(w:r))]", "//w:p[starts-with(., w:r)]",
                                   "//w:p[contains(., 'a' and 'b')]"])
def test_a_text_search_for_a_computed_value_is_refused(xpath):
    with pytest.raises(patch.PatchError):
        patch.check_xpath(xpath, 1000, 6)


def test_the_budget_counts_every_test_and_the_text_it_reads():
    """Sizes as part_size gives them: (nodes, depth, characters).
    80,000 Word paragraphs are (1_040_002, 6, 7_828_890)."""
    large = (1_040_002, 6, 7_828_890)
    chain = "//w:p[" + " or ".join(f"contains(., 'z{n}')" for n in range(90)) + "]"
    patch.check_xpath(chain, 1000, 6, 50_000)
    with pytest.raises(patch.PatchError, match="would visit about"):
        patch.check_xpath(chain, *large)
    with pytest.raises(patch.PatchError, match="would visit about"):
        patch.check_xpath("//w:p" + "[w:r]" * 5, *large)
    three = "//w:p[contains(., 'x') or contains(., 'y') or contains(., 'z')]"
    with pytest.raises(patch.PatchError, match="characters of this part's text"):
        patch.check_xpath(three, 10_000, 6, 8_000_000)
    for allowed in ("//w:p[contains(., 'Introduction')]", "//w:p[w:r/w:t='x']", "//w:p[position() = last()]",
                    "//w:p[@w:rsidR = '00AB']", "//w:body/w:p[3]"):
        patch.check_xpath(allowed, *large)
    long_needle = "/*/w:body[contains(., '" + "a" * 1960 + "b')]"
    patch.check_xpath(long_needle, 1000, 6, 50_000)
    with pytest.raises(patch.PatchError, match="characters of this part's text") as caught:
        patch.check_xpath(long_needle, *large)
    assert "shorter value" in caught.value.hint
    # A deep part: every ancestor reads the whole text again.
    with pytest.raises(patch.PatchError, match="characters of this part's text"):
        patch.check_xpath("/w:document/descendant::*[contains(., '" + "a" * 126 + "b')]", 26_242, 246, 192_000)


def test_a_comparison_of_two_values_reads_no_text():
    patch.check_xpath("//w:p[position() = last()]", 560_002, 6, 900_000_000)
    with pytest.raises(patch.PatchError, match="characters"):
        patch.check_xpath("//w:p[@w:val = 'x']", 560_002, 6, 900_000_000)


@pytest.mark.parametrize("xpath", [
    "//w:body/w:p[w:r = w:r]", "//w:body[w:p = w:p]", "//w:body[* != *]", "//*[w:r/w:t = w:r/w:t]",
    "//w:body[w:p < w:p]", "//w:body[text() = *]",
])
def test_a_predicate_comparing_two_node_sets_is_refused(xpath):
    _refused(_doc("x"), BODY, xpath, "remove", match="all", fragment_in="compares")


@pytest.mark.parametrize("xpath", [
    "//w:p[w:r/w:t='x']", "//w:p['x' = w:r/w:t]", "//w:p[position() = last()]", "//w:p[normalize-space() = w:r]",
    "//w:p[w:r = string(w:r)]", "//w:body/*[local-name() = 'p']", "//w:p[*/w:t]/w:r", "//w:p[./w:r]",
    "//w:p[w:r/text() = 'x']", "//w:p[@w:rsidR = '00AB']", "//w:p[position() mod 2 = 0]",
    "//w:p[position() = last() - 1]", "//w:p[position() > -1]", "//w:p[position() = 2 * 3]",
    "//w:p[last() - position() < 3]", "//w:p[position() = 10 div 2]", "//w:p[w:r | w:t]",
])
def test_ordinary_predicates_still_pass_the_grammar(xpath):
    patch.check_xpath(xpath, 1000, 6)


# ------------------------------------------------- nothing inside a field's code

_BEGIN = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
_SEPARATE = '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
_END = '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
_MARKERS = {"proofErr": '<w:proofErr w:type="spellStart"/>',
            "bookmarkStart": '<w:bookmarkStart w:id="7" w:name="in_code"/>'}


def _code(first: str, marker: str, second: str) -> str:
    return (f'{_BEGIN}<w:r><w:instrText xml:space="preserve">{first}</w:instrText></w:r>{marker}'
            f'<w:r><w:instrText xml:space="preserve">{second}</w:instrText></w:r>')


def _marked_field(name: str, marker: str) -> str:
    """The review's field: its code split around a proofing or bookmark
    mark, as Word writes them."""
    first = {"HYPERLINK": ' HYPERLINK "https://example.com/', "INCLUDEPICTURE": ' INCLUDEPICTURE "https://example.com/'}
    return (f'<w:p><w:r><w:t>before</w:t></w:r>{_code(first[name], _MARKERS[marker], "a.png" + chr(34) + " ")}'
            f"{_SEPARATE}<w:r><w:t>shown</w:t></w:r>{_END}<w:r><w:t>after</w:t></w:r></w:p>")


_INTO_CODE = {
    "delText": "<w:r><w:delText>file://///attacker.example/share/</w:delText></w:r>",
    "delText in w:del": '<w:del w:id="9" w:author="x"><w:r><w:delText>file://///attacker.example/share/</w:delText>'
                        "</w:r></w:del>",
    "sym": '<w:r><w:sym w:font="Symbol" w:char="0041"/></w:r>',
    "tab": "<w:r><w:tab/></w:r>",
    "noBreakHyphen": "<w:r><w:noBreakHyphen/></w:r>",
    "t": '<w:r><w:t xml:space="preserve">\\\\attacker.example\\share\\x.png </w:t></w:r>',
}


@pytest.mark.parametrize("field", ["HYPERLINK", "INCLUDEPICTURE"])
@pytest.mark.parametrize("marker", sorted(_MARKERS))
@pytest.mark.parametrize("inserted", sorted(_INTO_CODE))
def test_nothing_is_inserted_into_a_field_code(field, marker, inserted):
    """The review's retarget, which LibreOffice reads into the field's
    address: a run inserted beside a mark inside the code is refused."""
    document = _with_body(_marked_field(field, marker))
    _refused(document, BODY, f"//w:{marker}", "insert_after", _INTO_CODE[inserted], fragment_in="field's code")
    _refused(document, BODY, f"//w:{marker}", "insert_before", _INTO_CODE[inserted], fragment_in="field's code")


@pytest.mark.parametrize("marker", sorted(_MARKERS))
def test_a_mark_inside_a_field_code_is_never_replaced_or_removed(marker):
    document = _with_body(_marked_field("HYPERLINK", marker))
    _refused(document, BODY, f"//w:{marker}", "remove", fragment_in="field's code")
    _refused(document, BODY, f"//w:{marker}", "replace", _INTO_CODE["t"], fragment_in="field's code")


def test_a_paragraph_inside_a_field_code_is_never_a_target():
    """A field whose code runs across paragraphs: the middle paragraph is
    all code."""
    body = (f'<w:p>{_BEGIN}<w:r><w:instrText xml:space="preserve"> HYPERLINK </w:instrText></w:r></w:p>'
            '<w:p><w:proofErr w:type="gramStart"/></w:p>'
            '<w:p><w:r><w:instrText xml:space="preserve">"https://example.com/a" </w:instrText></w:r>'
            f"{_SEPARATE}<w:r><w:t>shown</w:t></w:r>{_END}</w:p><w:p><w:r><w:t>free</w:t></w:r></w:p>")
    document = _with_body(body)
    _refused(document, BODY, "//w:body/w:p[2]/w:proofErr", "insert_after", _INTO_CODE["delText"],
             fragment_in="field's code")
    _refused(document, BODY, "//w:body/w:p[2]", "insert_child", _INTO_CODE["delText"], fragment_in="field's code")
    _refused(document, BODY, "//w:body/w:p[2]/w:proofErr", "remove", fragment_in="field's code")
    _refused(document, BODY, "//w:body/w:p[2]", "replace", "<w:p/>", fragment_in="field's code")
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[4]/w:r", "replace", "<w:r><w:t>edited</w:t></w:r>")
    assert _texts(document)[-1] == "edited"


def test_a_nested_field_sits_in_its_outer_fields_code():
    inner = (f"{_BEGIN}<w:r><w:instrText>QUOTE 1</w:instrText></w:r>{_SEPARATE}<w:r><w:t>1</w:t></w:r>"
             '<w:proofErr w:type="spellEnd"/>' + _END)
    outer = (f"<w:p>{_BEGIN}<w:r><w:instrText>IF </w:instrText></w:r>{inner}"
             f'<w:r><w:instrText xml:space="preserve"> = 1 "a" "b"</w:instrText></w:r>{_SEPARATE}'
             f"<w:r><w:t>a</w:t></w:r>{_END}</w:p>")
    document = _with_body(outer)
    _refused(document, BODY, "//w:proofErr", "insert_after", _INTO_CODE["t"], fragment_in="field's code")


def test_after_an_unterminated_field_everything_is_code():
    document = _with_body(f'<w:p>{_BEGIN}<w:r><w:instrText>DATE</w:instrText></w:r></w:p>'
                          "<w:p><w:r><w:t>later</w:t></w:r></w:p>")
    _refused(document, BODY, "//w:body/w:p[2]/w:r", "insert_after", _INTO_CODE["t"], fragment_in="field's code")


def test_a_field_character_is_never_removed_or_moved():
    """The begin, separate and end runs (and the code's runs) hold field
    elements: no patch removes, replaces or moves them."""
    document = _with_body(_marked_field("HYPERLINK", "proofErr"))
    for index in (2, 3, 4, 5, 7):
        _refused(document, BODY, f"//w:body/w:p[1]/w:r[{index}]", "remove", fragment_in="field")
        _refused(document, BODY, f"//w:body/w:p[1]/w:r[{index}]", "replace", "<w:r><w:t>x</w:t></w:r>",
                 fragment_in="field")


def test_text_around_a_field_and_in_its_result_may_still_be_edited():
    document = _with_body(_marked_field("HYPERLINK", "proofErr"))
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]/w:r[1]", "insert_after",
                      "<w:r><w:t> and more</w:t></w:r>")
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]/w:r[w:t='shown']", "replace",
                      "<w:r><w:t>a new label</w:t></w:r>")
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]/w:r[last()]", "insert_after",
                      "<w:r><w:t> the end</w:t></w:r>")
    runs = [r.text for r in document.paragraphs[0].runs if r.text]
    assert runs[:2] == ["before", " and more"] and "a new label" in runs and runs[-1] == " the end"


_OPERANDS = {n: "+".join("." for _ in range(n)) for n in (2, 100, 600, 990)}


@pytest.mark.parametrize("xpath", [
    f"//w:body[not({_OPERANDS[100]})]", f"//w:body[not({_OPERANDS[600]})]", f"//w:p[not({_OPERANDS[990]})]",
    f"/w:document/descendant::*[not({_OPERANDS[100]})]", "//w:p[w:r * 1 = 2]", "//w:p[-w:r = 1]",
    "//w:p[w:r - 1 = 0]", "//w:p[@w:val + 0 = 1]", "//w:p[text() mod 2 = 0]", "//w:p[1 + . = 2]",
    "//w:p[(position() + 1) mod 2 = 0]", "//w:p[string() + 1 = 2]", "//w:p[normalize-space() div 2 = 1]",
    "//w:p[position() + w:r = 2]", "//w:p[w:r * 1 = w:r]", "//w:p[- w:r = 1]",
])
def test_arithmetic_reads_only_numbers_position_and_last(xpath):
    """The review's shapes: an arithmetic operand that is a node reads its
    whole text. Refused before evaluation, whatever the part's size."""
    import time

    began = time.monotonic()
    for size in ((1000, 6, 50_000), (560_000, 6, 4_600_000)):
        with pytest.raises(patch.PatchError) as caught:
            patch.check_xpath(xpath, *size)
        assert "arithmetic" in caught.value.message or "not valid" in caught.value.message, caught.value.message
    _refused(_big_document(1000), BODY, xpath, "remove", match="all")
    assert time.monotonic() - began < 1.0


# ------------------------------------------------- the shared names and the verify hook

def test_the_part_size_and_the_name_check_are_public():
    root = etree.fromstring(f'<w:body xmlns:w="{W}"><w:p w:a="1"/></w:body>')
    assert patch.part_size(root) == (3, 2, 1)
    with pytest.raises(patch.PatchError, match="plumbing"):
        patch.refuse_name("word/_rels/document.xml.rels")
    patch.refuse_name("word/document.xml")


def test_verify_sees_the_patched_part_and_its_refusal_puts_it_back():
    """The caller's read-back runs inside the guard: a PatchError it raises
    is the op's refusal, word for word, and the part is put back with the
    very same element objects."""
    document = _doc("A", "B", "C")
    held = document.paragraphs[1]
    seen = []

    def verify(root):
        seen.append([p.xpath("string(.)") for p in root.iter(f"{{{W}}}p")])
        raise patch.PatchError("the patch leaves word/document.xml broken: B is gone", "Keep B.")

    before = _snapshot(document)
    with pytest.raises(patch.PatchError, match="B is gone") as caught:
        patch.apply_patch(_package(document), BODY, "//w:body/w:p[2]", "replace",
                          "<w:p><w:r><w:t>new</w:t></w:r></w:p>", verify=verify)
    assert seen == [["A", "new", "C"]] and caught.value.hint == "Keep B." and caught.value.unchanged
    assert _snapshot(document) == before
    held.add_run(" still attached")
    assert _texts(document) == ["A", "B still attached", "C"]


def test_any_other_failure_of_verify_is_a_refusal_that_changes_nothing():
    document = _doc("A")

    def verify(root):
        raise ValueError("invalid literal for int() with base 10: 'abc'")

    before = _snapshot(document)
    with pytest.raises(patch.PatchError, match="invalid literal") as caught:
        patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "insert_after", "<w:p/>", verify=verify)
    assert "word/document.xml cannot be read" in caught.value.message and caught.value.hint
    assert caught.value.unchanged and _snapshot(document) == before


def test_verify_that_changes_the_tree_is_put_back_too():
    """A read that adds an element (python-docx's get_or_add) before the
    refusal: every element's own children and attributes come back."""
    document = _doc("A", "B")
    held = document.paragraphs[0]

    def verify(root):
        first = root.find(f".//{{{W}}}p")
        first.set(f"{{{W}}}rsidR", "00ABCDEF")
        first.insert(0, etree.SubElement(first, f"{{{W}}}pPr"))
        raise patch.PatchError("refused", "Fix it.")

    before = _snapshot(document)
    with pytest.raises(patch.PatchError):
        patch.apply_patch(_package(document), BODY, "//w:body/w:p[2]", "remove", verify=verify)
    assert _snapshot(document) == before
    held.add_run(" attached")
    assert _texts(document) == ["A attached", "B"]


def test_verify_reads_a_bytes_part_patched_and_its_refusal_keeps_the_old_bytes():
    document = _doc("x")
    theme = next(p for p in _package(document).iter_parts() if str(p.partname).endswith("theme1.xml"))
    blob = theme.blob
    read = []

    def verify(root):
        read.append(b'val="1F3864"' in theme.blob)
        raise patch.PatchError("refused", "Fix it.")

    with pytest.raises(patch.PatchError):
        patch.apply_patch(_package(document), str(theme.partname), "//a:clrScheme/a:dk2/a:srgbClr", "replace",
                          '<a:srgbClr val="1F3864"/>', verify=verify)
    assert read == [True] and theme.blob == blob
    patch.apply_patch(_package(document), str(theme.partname), "//a:clrScheme/a:dk2/a:srgbClr", "replace",
                      '<a:srgbClr val="1F3864"/>', verify=lambda root: None)
    assert b'val="1F3864"' in theme.blob


def test_verify_runs_only_on_a_patch_the_guard_let_through():
    calls = []
    _refused(_doc("x"), BODY, "//w:p[1]", "insert_after", '<w:p><w:hyperlink r:id="rId99"/></w:p>')
    with pytest.raises(patch.PatchError, match="rId99"):
        patch.apply_patch(_package(_doc("x")), BODY, "//w:p[1]", "insert_after",
                          '<w:p><w:hyperlink r:id="rId99"/></w:p>', verify=calls.append)
    assert calls == []


# ------------------------------------------------- a child its parent never holds

@pytest.mark.parametrize("xpath, action, xml", [
    ("//w:body/w:p[1]/w:pPr", "insert_before", "<w:keepNext/>"),
    ("//w:body/w:p[1]", "insert_child", "<w:keepNext/>"),
    ("//w:body/w:p[1]", "insert_after", "<w:p><w:r><w:jc/></w:r></w:p>"),
    ("//w:body/w:p[1]", "replace", "<w:p><w:tbl/></w:p>"),
])
def test_a_child_its_parent_never_holds_is_refused(xpath, action, xml):
    document = _doc("x")
    document.paragraphs[0].style = "Heading 1"
    _refused(document, BODY, xpath, action, xml, fragment_in="never holds")


def test_an_equation_and_extensions_are_children_a_paragraph_holds():
    m = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    document = _doc("x")
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "insert_child",
                      f'<m:oMath xmlns:m="{m}"><m:r><m:t>x</m:t></m:r></m:oMath>')
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "insert_after",
                      f'<w:p><m:oMathPara xmlns:m="{m}"><m:oMath><m:r><m:t>y</m:t></m:r></m:oMath></m:oMathPara></w:p>')
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "insert_child",
                      '<mc:AlternateContent><mc:Choice Requires="w14"><w:r><w:t>a</w:t></w:r></mc:Choice>'
                      "<mc:Fallback><w:r><w:t>a</w:t></w:r></mc:Fallback></mc:AlternateContent>")


def test_a_foreign_child_the_part_had_already_may_stay_in_a_replacement():
    """A paragraph from another application that already holds a child the
    schema does not list may be replaced by a copy that keeps it."""
    odd = '<w:p xmlns:x="urn:x"><x:note/><w:r><w:t>{}</w:t></w:r></w:p>'
    document = _with_body(odd.format("before"))
    patch.apply_patch(_package(document), BODY, "//w:body/w:p[1]", "replace", odd.format("after"))
    assert "after" in _texts(document)[0]
    _refused(document, BODY, "//w:body/w:p[1]", "insert_after", odd.format("one more"), fragment_in="never holds")
