"""The schema order of OOXML children: the table, the type of an element by
its name or its parent, and an insert that keeps the order."""

import json
import os
import sys
from pathlib import Path

import pytest
from lxml import etree

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

from ooxml import order

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W, "p": P, "a": A, "x": X, "mc": MC}


def _xml(text: str):
    return etree.fromstring(text)


def _w(local: str):
    return etree.Element(f"{{{W}}}{local}")


def _locals(parent) -> list[str]:
    return [etree.QName(c).localname for c in parent if isinstance(c.tag, str)]


def test_the_table_names_its_source_and_covers_the_writers_namespaces():
    raw = json.loads(order.TABLE_FILE.read_text())
    assert "dotnet/Open-XML-SDK" in raw["source"] and raw["license"].startswith("MIT")
    for prefix in ("w", "w14", "w15", "m", "a", "pic", "wp", "c", "p", "p14", "x", "x14", "xdr"):
        assert prefix in raw["namespaces"], prefix


def test_a_paragraph_property_lands_in_schema_order():
    ppr = _xml(f'<w:pPr xmlns:w="{W}"><w:pStyle w:val="H"/><w:spacing/><w:rPr/></w:pPr>')
    order.insert_child(ppr, _w("jc"))
    order.insert_child(ppr, _w("keepNext"))
    order.insert_child(ppr, _w("pPrChange"))
    assert _locals(ppr) == ["pStyle", "keepNext", "spacing", "jc", "rPr", "pPrChange"]
    assert order.out_of_order(ppr) == []


def test_one_name_two_types_is_told_apart_by_the_parent():
    """``w:rPr`` under ``w:pPr`` starts with the change marks; under ``w:r``
    it has none."""
    doc = _xml(f'<w:p xmlns:w="{W}"><w:pPr><w:rPr><w:b/></w:rPr></w:pPr><w:r><w:rPr><w:b/></w:rPr></w:r></w:p>')
    para_rpr, run_rpr = doc.findall(".//w:rPr", NS)
    assert order.type_of(para_rpr) == "w:CT_ParaRPr/w:rPr" and order.type_of(run_rpr) == "w:CT_RPr/w:rPr"
    order.insert_child(para_rpr, _w("ins"))
    assert _locals(para_rpr) == ["ins", "b"]
    order.insert_child(run_rpr, _w("rStyle"))
    assert _locals(run_rpr) == ["rStyle", "b"]


def test_cell_shading_goes_before_no_wrap_and_choice_members_share_a_rank():
    tcpr = _xml(f'<w:tcPr xmlns:w="{W}"><w:tcW/><w:tcBorders/><w:noWrap/><w:vAlign/></w:tcPr>')
    order.insert_child(tcpr, _w("shd"))
    assert _locals(tcpr) == ["tcW", "tcBorders", "shd", "noWrap", "vAlign"]
    ranks = order.child_ranks(tcpr)
    assert ranks[f"{{{W}}}cellIns"] == ranks[f"{{{W}}}cellDel"] == ranks[f"{{{W}}}cellMerge"]


def test_table_borders_go_before_the_table_look():
    tblpr = _xml(f'<w:tblPr xmlns:w="{W}"><w:tblStyle/><w:tblW/><w:tblLook/></w:tblPr>')
    order.insert_child(tblpr, _w("tblBorders"))
    assert _locals(tblpr) == ["tblStyle", "tblW", "tblBorders", "tblLook"]


def test_a_slide_background_goes_before_the_shape_tree():
    csld = _xml(f'<p:cSld xmlns:p="{P}"><p:spTree/></p:cSld>')
    order.insert_child(csld, etree.Element(f"{{{P}}}bg"))
    assert _locals(csld) == ["bg", "spTree"]


def test_unknown_children_stay_in_place_and_ext_list_stays_last():
    csld = _xml(f'<p:cSld xmlns:p="{P}" xmlns:mc="{MC}"><mc:AlternateContent/><p:spTree/><!-- c -->'
                f'<p:extLst/></p:cSld>')
    order.insert_child(csld, etree.Element(f"{{{P}}}custDataLst"))
    assert _locals(csld) == ["AlternateContent", "spTree", "custDataLst", "extLst"]
    order.insert_child(csld, etree.Element("{urn:example}vendor"))
    assert _locals(csld) == ["AlternateContent", "spTree", "custDataLst", "vendor", "extLst"]
    assert order.out_of_order(csld) == []


def test_an_unknown_parent_appends():
    parent = etree.Element("{urn:example}box")
    etree.SubElement(parent, "{urn:example}a")
    order.insert_child(parent, etree.Element("{urn:example}b"))
    assert _locals(parent) == ["a", "b"] and order.type_of(parent) is None


def test_out_of_order_names_each_misplaced_child():
    tcpr = _xml(f'<w:tcPr xmlns:w="{W}"><w:tcW/><w:noWrap/><w:shd/></w:tcPr>')
    assert order.out_of_order(tcpr) == [(f"{{{W}}}shd", f"{{{W}}}noWrap")]


def test_spreadsheet_and_drawing_types_are_known():
    sheet = _xml(f'<x:worksheet xmlns:x="{X}"><x:sheetData/><x:pageMargins/></x:worksheet>')
    order.insert_child(sheet, etree.Element(f"{{{X}}}sheetFormatPr"))
    order.insert_child(sheet, etree.Element(f"{{{X}}}mergeCells"))
    assert _locals(sheet) == ["sheetFormatPr", "sheetData", "mergeCells", "pageMargins"]
    sppr = _xml(f'<a:spPr xmlns:a="{A}"><a:xfrm/><a:ln/></a:spPr>')
    order.insert_child(sppr, etree.Element(f"{{{A}}}solidFill"))
    assert _locals(sppr) == ["xfrm", "solidFill", "ln"]


@pytest.mark.parametrize("library", ["docx", "pptx"])
def test_the_libraries_blank_documents_are_in_order(library):
    pytest.importorskip(library)
    if library == "docx":
        import docx

        root = docx.Document().element
    else:
        import pptx

        prs = pptx.Presentation()
        prs.slides.add_slide(prs.slide_layouts[1])
        root = prs.slides[0]._element
    problems = [(etree.QName(el).localname, order.out_of_order(el)) for el in root.iter() if order.out_of_order(el)]
    assert problems == []


def test_range_markup_is_in_order_before_the_table_properties_and_after_a_row():
    """A table is range markup, tblPr, tblGrid, then rows mixed with range
    markup: a bookmark or comment range that ends after the last row is
    where Word writes it."""
    tbl = _xml(f'<w:tbl xmlns:w="{W}"><w:bookmarkStart w:id="0" w:name="t"/><w:tblPr/><w:tblGrid/><w:tr/>'
               '<w:commentRangeStart w:id="1"/><w:tr/><w:bookmarkEnd w:id="0"/><w:commentRangeEnd w:id="1"/>'
               '</w:tbl>')
    assert order.out_of_order(tbl) == []
    late = _xml(f'<w:tbl xmlns:w="{W}"><w:tr/><w:tblPr/></w:tbl>')
    assert order.out_of_order(late) == [(f"{{{W}}}tblPr", f"{{{W}}}tr")]


def test_an_insert_takes_a_childs_first_place():
    tbl = _xml(f'<w:tbl xmlns:w="{W}"><w:tblPr/><w:tblGrid/><w:tr/></w:tbl>')
    order.insert_child(tbl, _w("bookmarkStart"))
    assert _locals(tbl) == ["bookmarkStart", "tblPr", "tblGrid", "tr"]
    assert order.child_ranks(tbl)[f"{{{W}}}bookmarkStart"] == 0


def test_the_table_regenerates_byte_for_byte():
    """The committed table is the generator's output on the pinned SDK
    data; runs where that data has been downloaded beside the tests."""
    source = Path(os.environ.get("FILETOOLS_OXSDK_DATA", "/nonexistent"))
    if not (source / "namespaces.json").is_file():
        pytest.skip("the SDK data is not downloaded here (FILETOOLS_OXSDK_DATA)")
    from ooxml import generate_order

    table = generate_order.build(source)
    assert generate_order.dump(table) == order.TABLE_FILE.read_text(encoding="utf-8")


def test_the_fewest_children_follow_the_sdks_occurrences():
    """No ``Occurs`` is exactly once; inside one a missing ``Min`` is 0."""
    from ooxml import generate_order

    item = {"Name": "x:CT_A/x:a"}
    assert generate_order.least(item) == 1
    assert generate_order.least({**item, "Occurs": [{}]}) == 0
    assert generate_order.least({**item, "Occurs": [{"Max": 1}]}) == 0
    assert generate_order.least({**item, "Occurs": [{"Min": 2, "Max": 5}]}) == 2
    choice = {"Kind": "Choice", "Occurs": [{"Min": 1}], "Items": [item, {**item, "Occurs": [{}]}]}
    assert generate_order.least(choice) == 0
    assert generate_order.least({"Kind": "Sequence", "Items": [item, item]}) == 2


@pytest.mark.parametrize("tag, required", [("p:childTnLst", True), ("p:bldLst", True), ("w:cols", False),
                                           ("w:body", False), ("w:tr", False)])
def test_the_table_knows_which_containers_need_a_child(tag, required):
    prefix, local = tag.split(":")
    namespace = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
                 "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}[prefix]
    assert order.needs_children(etree.Element(f"{{{namespace}}}{local}")) is required
