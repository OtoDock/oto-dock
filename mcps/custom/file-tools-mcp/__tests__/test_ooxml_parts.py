"""Adding and copying package parts: a slide copied with its picture shared
and its chart and notes owned, a new part related with its content type, the
partnames, the relationship ids and the paragraph ids."""

import io
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).parent.parent))

from ooxml import parts

pptx = pytest.importorskip("pptx")
docx = pytest.importorskip("docx")

from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.util import Inches

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")


def _deck():
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(io.BytesIO(_PNG), Inches(1), Inches(1), Inches(1), Inches(1))
    data = CategoryChartData()
    data.categories = ["A", "B"]
    data.add_series("S", (1, 2))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(3), Inches(1), Inches(4), Inches(3), data)
    slide.notes_slide.notes_text_frame.text = "say this"
    return prs, slide


def _copy_slide(prs, slide):
    duplicate = parts.copy_part(slide.part)
    rid = prs.part.relate_to(duplicate, RT.SLIDE)
    prs.slides._sldIdLst.add_sldId(rid)
    return duplicate


def test_a_slide_copy_shares_its_picture_and_owns_its_chart_and_notes():
    prs, slide = _deck()
    _copy_slide(prs, slide)
    buf = io.BytesIO()
    prs.save(buf)
    names = zipfile.ZipFile(buf).namelist()
    assert {"ppt/slides/slide1.xml", "ppt/slides/slide2.xml", "ppt/charts/chart1.xml", "ppt/charts/chart2.xml",
            "ppt/notesSlides/notesSlide1.xml", "ppt/notesSlides/notesSlide2.xml"} <= set(names)
    assert len([n for n in names if n.startswith("ppt/embeddings/")]) == 2
    assert len([n for n in names if n.startswith("ppt/media/")]) == 1

    reopened = pptx.Presentation(io.BytesIO(buf.getvalue()))
    first, second = reopened.slides
    picture = [s for s in second.shapes if s.shape_type == 13][0]
    assert picture.image.blob == _PNG
    charts = [s.chart for s in (first.shapes[1], second.shapes[1])]
    assert charts[0].part is not charts[1].part and list(charts[1].plots[0].categories) == ["A", "B"]
    assert second.notes_slide.notes_text_frame.text == "say this"
    assert second.notes_slide.part.part_related_by(RT.SLIDE) is second.part


def test_a_copy_of_a_copy_numbers_past_both():
    prs, slide = _deck()
    first_copy = _copy_slide(prs, slide)
    second_copy = _copy_slide(prs, slide)
    assert {str(first_copy.partname), str(second_copy.partname)} == {"/ppt/slides/slide2.xml", "/ppt/slides/slide3.xml"}
    buf = io.BytesIO()
    prs.save(buf)
    assert len(pptx.Presentation(io.BytesIO(buf.getvalue())).slides) == 3


def test_a_new_part_is_related_and_typed():
    document = docx.Document()
    element = etree.fromstring(f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"/></w:footnotes>')
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
    reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
    part, rid = parts.add_part(document.part, reltype, content_type, template="/word/footnotes%d.xml",
                               element=element)
    assert str(part.partname) == "/word/footnotes1.xml" and rid.startswith("rId")
    buf = io.BytesIO()
    document.save(buf)
    archive = zipfile.ZipFile(buf)
    assert content_type in archive.read("[Content_Types].xml").decode()
    assert b"footnotes1.xml" in archive.read("word/_rels/document.xml.rels")
    reopened = docx.Document(io.BytesIO(buf.getvalue()))
    assert reopened.part.part_related_by(reltype).blob.startswith(b"<?xml")
    with pytest.raises(ValueError):
        parts.add_part(document.part, reltype, content_type, template="/word/x%d.xml")


def test_templates_and_taken_names():
    assert parts.template_of("/ppt/charts/chart3.xml") == "/ppt/charts/chart%d.xml"
    assert parts.template_of("/word/footnotes.xml") == "/word/footnotes%d.xml"
    prs, _ = _deck()
    name = parts.next_partname(prs.part.package, "/ppt/slides/slide%d.xml", taken={"/ppt/slides/slide2.xml"})
    assert str(name) == "/ppt/slides/slide3.xml"


def test_relationship_ids_are_remapped_once_each():
    r = parts.R_NS
    element = etree.fromstring(
        f'<root xmlns:r="{r}" xmlns:o="urn:schemas-microsoft-com:office:office">'
        f'<a r:embed="rId1"/><b r:id="rId2" r:link="rId1"/><c o:relid="rId2"/><d id="rId1"/></root>')
    assert parts.remap_rids(element, {"rId1": "rId2", "rId2": "rId1"}) == 4
    a, b, c, d = element
    assert a.get(f"{{{r}}}embed") == "rId2" and b.get(f"{{{r}}}id") == "rId1" and b.get(f"{{{r}}}link") == "rId2"
    assert c.get(parts.O_RELID) == "rId1" and d.get("id") == "rId1"


def test_paragraph_ids_are_refreshed_where_taken():
    element = etree.fromstring(f'<w:body xmlns:w="{W}" xmlns:w14="{parts.W14_NS}">'
                               '<w:p w14:paraId="1A2B3C4D" w14:textId="77777777"/><w:p w14:paraId="0000AAAA"/></w:body>')
    used = {"1A2B3C4D"}
    assert parts.refresh_para_ids(element, used) == 1
    first, second = element
    fresh = first.get(parts.PARA_ID)
    assert fresh != "1A2B3C4D" and len(fresh) == 8 and int(fresh, 16) < 0x80000000
    assert second.get(parts.PARA_ID) == "0000AAAA" and {"77777777", "0000AAAA", fresh} <= used
    document = docx.Document()
    document.element.body.append(element)
    assert {"77777777", "0000AAAA", fresh} <= parts.used_para_ids(document.part.package)


DSP = "http://schemas.microsoft.com/office/drawing/2008/diagram"
DIAGRAM_DRAWING = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"


def test_a_smartart_copy_points_its_data_at_its_own_drawing():
    """A SmartArt data part names the slide's relationship to the diagram's
    drawing in an unprefixed relId; the copy's data names the copy's."""
    prs, slide = _deck()
    gap = slide.part.relate_to("https://example.com", RT.HYPERLINK, is_external=True)
    drawing, drawing_rid = parts.add_part(slide.part, DIAGRAM_DRAWING, "application/vnd.ms-office.drawingml."
                                          "diagramDrawing+xml", template="/ppt/diagrams/drawing%d.xml",
                                          element=etree.fromstring(f'<dsp:drawing xmlns:dsp="{DSP}"/>'))
    data = etree.fromstring(f'<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
                            f'xmlns:dsp="{DSP}"><dgm:extLst><a:ext xmlns:a="http://schemas.openxmlformats.org/'
                            f'drawingml/2006/main" uri="x"><dsp:dataModelExt relId="{drawing_rid}" minVer="x"/>'
                            "</a:ext></dgm:extLst></dgm:dataModel>")
    parts.add_part(slide.part, parts.DIAGRAM_DATA, "application/vnd.openxmlformats-officedocument.drawingml."
                   "diagramData+xml", template="/ppt/diagrams/data%d.xml", element=data)
    slide.part.drop_rel(gap)
    copy = parts.copy_part(slide.part)
    copied_data = next(r.target_part for r in copy.rels.values() if r.reltype == parts.DIAGRAM_DATA)
    copied_rid = next(rid for rid, r in copy.rels.items() if r.reltype == DIAGRAM_DRAWING)
    named = etree.fromstring(copied_data.blob).find(f".//{{{DSP}}}dataModelExt").get("relId")
    assert named == copied_rid == drawing_rid  # the copy keeps the source's ids
    assert copy.rels[copied_rid].target_part is not drawing
    original = next(r.target_part for r in slide.part.rels.values() if r.reltype == parts.DIAGRAM_DATA)
    assert etree.fromstring(original.blob).find(f".//{{{DSP}}}dataModelExt").get("relId") == drawing_rid


def test_modern_comments_are_owned_by_a_copy():
    assert "http://schemas.microsoft.com/office/2018/10/relationships/comments" in parts.OWNED_RELTYPES


def _rel_kinds(part):
    return {rid: (rel.reltype, rel.is_external) for rid, rel in part.rels.items()}


def test_a_copy_keeps_the_source_relationship_ids():
    """No id in the copied XML needs remapping, including ids a part names
    in a plain attribute (SmartArt's relId) or in a part we do not parse."""
    prs, slide = _deck()
    gap = slide.part.relate_to("https://example.com", RT.HYPERLINK, is_external=True)
    slide.part.relate_to("https://example.org", RT.HYPERLINK, is_external=True)
    slide.part.drop_rel(gap)
    copy = parts.copy_part(slide.part)
    assert _rel_kinds(copy) == _rel_kinds(slide.part)


def test_a_copied_corpus_smartart_points_at_its_own_drawing():
    from pathlib import Path

    deck = Path(__file__).parent / "corpus" / "shared" / "powerpoint-2016-poi-smartart.pptx"
    prs = pptx.Presentation(str(deck))
    slide = prs.slides[0]
    copy = parts.copy_part(slide.part)
    prs.part.relate_to(copy, RT.SLIDE)
    assert sorted(copy.rels) == sorted(slide.part.rels)
    data = next(r.target_part for r in copy.rels.values() if r.reltype == parts.DIAGRAM_DATA)
    rid = etree.fromstring(data.blob).find(f".//{{{DSP}}}dataModelExt").get("relId")
    assert copy.rels[rid].reltype == DIAGRAM_DRAWING
    assert copy.rels[rid].target_part is not slide.part.rels[rid].target_part


def test_a_word_part_copy_keeps_its_ids_too():
    document = docx.Document()
    header = document.sections[0].header
    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (2, 2)).save(png, "PNG")
    png.seek(0)
    header.paragraphs[0].add_run().add_picture(png)
    header.part.relate_to("https://example.com", "http://schemas.openxmlformats.org/officeDocument/2006/"
                          "relationships/hyperlink", is_external=True)
    copy = parts.copy_part(header.part)
    assert _rel_kinds(copy) == _rel_kinds(header.part)
