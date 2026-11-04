"""write_pptx operations on slide content: bullet lists and groups.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Inches

import powerpoint as ppt_mod


async def _async_ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _bypass_platform(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("shared._resolve_path", _async_ident)
    monkeypatch.setattr("shared._push_preview", _noop)
    monkeypatch.setattr("shared._notify_file_written", _noop)
    monkeypatch.setattr("shared._to_agents_relative", lambda p: p)


def _clean(out: str) -> bool:
    """Saved, with no failed operation and no warning."""
    return out.startswith("Presentation saved: ") and "Failed:" not in out and "Warnings:" not in out

def _write(path: Path, ops: list[dict], create_new: bool = True) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx(
        {"path": str(path), "create_new": create_new, "operations": ops}))


# ---------------------------------------------------------------------------
# Bullets
# ---------------------------------------------------------------------------


def _body(path: Path, slide: int = 0):
    prs = Presentation(str(path))
    return next(ph for ph in prs.slides[slide].placeholders if ph.placeholder_format.idx == 1)


def test_bullets_start_on_the_first_line(tmp_path):
    f = tmp_path / "b.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_and_content"},
                     {"op": "add_bullet_points", "slide_index": 0, "font_size": 20,
                      "items": ["One", {"text": "Two", "level": 1}, "Three"]}])
    assert _clean(out), out
    paras = _body(f).text_frame.paragraphs
    assert [(p.text, p.level) for p in paras] == [("One", 0), ("Two", 1), ("Three", 0)]
    assert all(r.font.size.pt == 20 for p in paras for r in p.runs)


def test_bullets_after_text_are_appended(tmp_path):
    f = tmp_path / "b.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_and_content"},
               {"op": "set_text", "slide_index": 0, "placeholder": "body", "text": "Intro"},
               {"op": "add_bullet_points", "slide_index": 0, "items": ["A", "B"]}])
    assert [p.text for p in _body(f).text_frame.paragraphs] == ["Intro", "A", "B"]


def test_an_empty_last_line_takes_the_first_item(tmp_path):
    f = tmp_path / "b.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_and_content"},
               {"op": "set_text", "slide_index": 0, "placeholder": "body", "text": "Intro\n"},
               {"op": "add_bullet_points", "slide_index": 0, "items": ["A"]}])
    assert [p.text for p in _body(f).text_frame.paragraphs] == ["Intro", "A"]


def test_the_first_item_drops_the_empty_lines_own_formatting(tmp_path):
    f = tmp_path / "b.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_and_content"},
               {"op": "set_text", "slide_index": 0, "placeholder": "body", "text": "Intro\n"}])
    prs = Presentation(str(f))
    body = next(ph for ph in prs.slides[0].placeholders if ph.placeholder_format.idx == 1)
    for para in body.text_frame.paragraphs:
        ppr = para._p.get_or_add_pPr()
        ppr.set("algn", "ctr")
        etree.SubElement(ppr, qn("a:buNone"))
    prs.save(str(f))
    _write(f, [{"op": "add_bullet_points", "slide_index": 0, "items": ["A", "B"]}], create_new=False)
    paras = _body(f).text_frame.paragraphs
    assert [p.text for p in paras] == ["Intro", "A", "B"]
    intro, first, second = (p._p.find(qn("a:pPr")) for p in paras)
    assert intro.get("algn") == "ctr" and intro.find(qn("a:buNone")) is not None
    assert etree.tostring(first) == etree.tostring(second)


def test_bullets_into_a_textbox(tmp_path):
    f = tmp_path / "b.pptx"
    _write(f, [{"op": "add_slide"},
               {"op": "add_textbox", "slide_index": 0, "text": ""},
               {"op": "add_bullet_points", "slide_index": 0, "shape_index": 0, "items": ["A", "B"]}])
    shape = Presentation(str(f)).slides[0].shapes[0]
    assert [p.text for p in shape.text_frame.paragraphs] == ["A", "B"]


# ---------------------------------------------------------------------------
# The style of a shape the tool draws
# ---------------------------------------------------------------------------


def _style_of(shape):
    fill = shape._element.spPr.find(qn("a:solidFill"))
    font = shape._element.find(f"{qn('p:style')}/{qn('a:fontRef')}/{qn('a:schemeClr')}")
    return (fill[0].get("val") if fill is not None and len(fill) else None), (font.get("val") if font is not None
                                                                             else None)


def test_a_shape_without_a_fill_is_solid_accent_one_under_text_that_reads(tmp_path):
    f = tmp_path / "s.pptx"
    out = _write(f, [{"op": "add_slide"},
                     {"op": "add_shape", "slide_index": 0, "text": "Label"},
                     {"op": "add_shape", "slide_index": 0, "text": "Small", "font_size": 12, "top": 3},
                     {"op": "add_shape", "slide_index": 0, "text": "Mine", "font_color": "#FFFFFF", "font_size": 12,
                      "top": 5},
                     {"op": "add_shape", "slide_index": 0, "left": 5},
                     {"op": "add_shape", "slide_index": 0, "text": "Set", "fill_color": "#C00000", "left": 5, "top": 3},
                     {"op": "add_shape", "slide_index": 0, "no_fill": True, "left": 5, "top": 5},
                     {"op": "add_freeform", "slide_index": 0, "points": [[8, 1], [10, 1], [9, 2.5]]}])
    assert out.startswith("Presentation saved: "), out
    label, small, mine, empty, filled, unfilled, freeform = Presentation(str(f)).slides[0].shapes
    # White reads on the default theme's accent 1 (#4F81BD) at 18 pt, not at 12 pt.
    assert _style_of(label) == ("accent1", "lt1")
    assert _style_of(small) == ("accent1", "dk1")
    assert _style_of(mine) == ("accent1", "lt1")                       # the op's colour is the person's
    assert _style_of(empty)[0] == "accent1" and _style_of(freeform)[0] == "accent1"
    assert filled.fill.fore_color.rgb is not None and _style_of(filled)[1] == "lt1"
    assert unfilled._element.spPr.find(qn("a:noFill")) is not None


def test_a_light_accent_takes_dark_text(tmp_path):
    import re

    f = tmp_path / "s.pptx"
    _write(f, [{"op": "add_slide"}])
    prs = Presentation(str(f))
    theme = prs.slide_master.part.part_related_by(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme")
    theme._blob = re.sub(rb'(<a:accent1>\s*<a:srgbClr val=")[0-9A-Fa-f]{6}', rb"\g<1>FFD966", theme.blob)
    prs.save(str(f))
    _write(f, [{"op": "add_shape", "slide_index": 0, "text": "Pale"}], create_new=False)
    assert _style_of(Presentation(str(f)).slides[0].shapes[0]) == ("accent1", "dk1")


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


def _shapes_deck(path: Path) -> None:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only: a placeholder to refuse
    for i, name in enumerate("ABCD"):
        box = slide.shapes.add_shape(1, Inches(1 + i), Inches(2 + i / 2), Inches(1.5), Inches(1))
        box.name = name
        box.text = f"text {name}"
    slide.shapes.add_table(2, 2, Inches(1), Inches(5), Inches(3), Inches(1)).name = "T"
    prs.save(str(path))


def _names(tree) -> list[str]:
    return [el.find(".//" + qn("p:cNvPr")).get("name") for el in tree
            if el.tag.rsplit("}", 1)[-1] in ("sp", "grpSp", "graphicFrame", "pic", "cxnSp")]


def test_a_group_holds_its_shapes_in_their_stacking_order(tmp_path):
    f = tmp_path / "g.pptx"
    _shapes_deck(f)
    before = {s.name: (s.left, s.top, s.width, s.height) for s in Presentation(str(f)).slides[0].shapes}
    out = _write(f, [{"op": "add_group_shape", "slide_index": 0, "name": "Pair",
                      "shapes": ["text C", "B"]}], create_new=False)
    assert _clean(out), out
    slide = Presentation(str(f)).slides[0]
    assert _names(slide.shapes._spTree) == ["Title 1", "A", "Pair", "D", "T"]
    group = next(s for s in slide.shapes if s.name == "Pair")
    assert [s.name for s in group.shapes] == ["B", "C"]
    for member in group.shapes:
        assert (member.left, member.top, member.width, member.height) == before[member.name]
    xfrm = group._element.grpSpPr.find(qn("a:xfrm"))
    off, ext = xfrm.find(qn("a:off")), xfrm.find(qn("a:ext"))
    assert off.attrib == xfrm.find(qn("a:chOff")).attrib
    assert ext.get("cx") == xfrm.find(qn("a:chExt")).get("cx")
    assert (group.left, group.top) == (before["B"][0], before["B"][1])


def test_indexes_name_members_before_anything_moves(tmp_path):
    f = tmp_path / "g.pptx"
    _shapes_deck(f)
    out = _write(f, [{"op": "add_group_shape", "slide_index": 0, "shapes": [4, "1"]}], create_new=False)
    assert _clean(out), out
    slide = Presentation(str(f)).slides[0]
    group = next(s for s in slide.shapes if s.shape_type == 6)
    assert [s.name for s in group.shapes] == ["A", "D"]


@pytest.mark.parametrize("shapes, message", [
    (["A"], "at least two shapes"),
    (["A", 1], "named twice"),
    (["A", "Title 1"], "does not group placeholders"),
    (["A", "T"], "does not group tables"),
    (["A", "Nope"], "no shape is named 'Nope'"),
    ("A", "'shapes' expects a list"),
])
def test_a_refused_group_leaves_the_slide_unchanged(tmp_path, shapes, message):
    f = tmp_path / "g.pptx"
    _shapes_deck(f)
    before = etree.tostring(Presentation(str(f)).slides[0]._element)
    out = _write(f, [{"op": "add_group_shape", "slide_index": 0, "shapes": shapes}], create_new=False)
    assert message in out
    assert etree.tostring(Presentation(str(f)).slides[0]._element) == before


def test_a_member_without_its_own_box_is_refused_before_it_moves(tmp_path):
    f = tmp_path / "g.pptx"
    _shapes_deck(f)
    prs = Presentation(str(f))
    shape = next(s for s in prs.slides[0].shapes if s.name == "B")
    xfrm = shape._element.spPr.find(qn("a:xfrm"))
    shape._element.spPr.remove(xfrm)
    prs.save(str(f))
    before = etree.tostring(Presentation(str(f)).slides[0]._element)
    out = _write(f, [{"op": "add_group_shape", "slide_index": 0, "shapes": ["A", "B"]}], create_new=False)
    assert "no position and size of its own" in out
    assert etree.tostring(Presentation(str(f)).slides[0]._element) == before


def test_a_group_of_a_picture_a_connector_and_a_chart(tmp_path):
    from PIL import Image

    img = tmp_path / "p.png"
    Image.new("RGB", (20, 20), "blue").save(img)
    f = tmp_path / "g.pptx"
    out = _write(f, [
        {"op": "add_slide"},
        {"op": "add_image", "slide_index": 0, "image_path": str(img), "left": 1, "top": 1, "width": 1},
        {"op": "add_connector", "slide_index": 0, "start_x": 2, "start_y": 2, "end_x": 4, "end_y": 3},
        {"op": "add_chart", "slide_index": 0, "chart_type": "pie", "categories": ["a", "b"],
         "series": [{"name": "S", "values": [1, 2]}], "left": 5, "top": 1, "width": 3, "height": 3},
        {"op": "add_group_shape", "slide_index": 0, "shapes": [0, 1, 2], "name": "All"},
    ])
    assert _clean(out), out
    slide = Presentation(str(f)).slides[0]
    group = slide.shapes[0]
    assert group.name == "All" and len(group.shapes) == 3
    assert (group.left, group.top) == (Inches(1), Inches(1))
    assert group.left + group.width == Inches(8)


def test_an_animated_member_is_named(tmp_path):
    f = tmp_path / "g.pptx"
    _shapes_deck(f)
    prs = Presentation(str(f))
    slide = prs.slides[0]
    b = next(s for s in slide.shapes if s.name == "B")
    timing = etree.SubElement(slide._element, qn("p:timing"))
    etree.SubElement(etree.SubElement(timing, qn("p:spTgt")), qn("p:txEl"))
    timing[0].set("spid", str(b.shape_id))
    prs.save(str(f))
    out = _write(f, [{"op": "add_group_shape", "slide_index": 0, "shapes": ["A", "B"]}], create_new=False)
    assert "'B' has an animation" in out


def test_a_group_without_shapes_is_empty_and_says_so(tmp_path):
    f = tmp_path / "g.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "add_group_shape", "slide_index": 0, "name": "G"}])
    assert "an empty group was added" in out
    assert Presentation(str(f)).slides[0].shapes[0].name == "G"


# ---------------------------------------------------------------------------
# Duplicating a slide
# ---------------------------------------------------------------------------


def _rich_slide(path: Path) -> None:
    from PIL import Image

    image = path.parent / "pic.png"
    Image.new("RGB", (30, 20), "green").save(image)
    out = _write(path, [
        {"op": "add_slide", "layout": "title_and_content", "title": "Source"},
        {"op": "add_bullet_points", "slide_index": 0, "items": ["One", "Two"]},
        {"op": "add_image", "slide_index": 0, "image_path": str(image), "left": 9, "top": 2, "width": 2},
        {"op": "add_chart", "slide_index": 0, "categories": ["Q1", "Q2"], "series": [{"name": "R", "values": [1, 2]}],
         "left": 6, "top": 4, "width": 4, "height": 3},
        {"op": "add_shape", "slide_index": 0, "text": "Link", "left": 1, "top": 6, "fill_color": "#1F4E79"},
        {"op": "add_hyperlink", "slide_index": 0, "shape_name": "Link", "url": "https://example.com"},
        {"op": "set_speaker_notes", "slide_index": 0, "text": "Say this"},
        {"op": "set_transition", "slide_index": 0, "transition": "wipe"},
        {"op": "add_slide", "layout": "blank"},
    ])
    assert out.startswith("Presentation saved: "), out
    prs = Presentation(str(path))
    run = prs.slides[0].notes_slide.notes_text_frame.paragraphs[0].runs[0]
    run.font.bold = True
    prs.save(str(path))


def test_a_duplicate_owns_its_chart_and_notes_and_shares_its_picture(tmp_path):
    f = tmp_path / "d.pptx"
    _rich_slide(f)
    out = _write(f, [{"op": "duplicate_slide", "slide_index": 0}], create_new=False)
    assert out.startswith("Presentation saved: ") and "Checks: no problems found." in out, out
    prs = Presentation(str(f))
    source, _, copy = prs.slides
    assert [s.name for s in copy.shapes] == [s.name for s in source.shapes]
    assert [s.shape_id for s in copy.shapes] == [s.shape_id for s in source.shapes]
    assert len(copy.placeholders) == len(source.placeholders)          # no doubled placeholders
    pic_src = next(s for s in source.shapes if s.shape_type == 13)
    pic_copy = next(s for s in copy.shapes if s.shape_type == 13)
    assert pic_copy.image.sha1 == pic_src.image.sha1
    assert copy.part.related_part(pic_copy._element.blip_rId) is source.part.related_part(pic_src._element.blip_rId)
    chart_src = next(s for s in source.shapes if s.has_chart).chart
    chart_copy = next(s for s in copy.shapes if s.has_chart).chart
    assert chart_copy.part is not chart_src.part
    assert copy.notes_slide.part is not source.notes_slide.part
    assert copy.notes_slide.notes_text_frame.text == "Say this"
    assert copy.notes_slide.notes_text_frame.paragraphs[0].runs[0].font.bold
    link = next(s for s in copy.shapes if s.name.startswith("Rectangle"))
    assert link.click_action.hyperlink.address == "https://example.com"
    assert copy._element.find(qn("p:transition")) is not None


def test_editing_the_copys_chart_leaves_the_source(tmp_path):
    f = tmp_path / "d.pptx"
    _rich_slide(f)
    out = _write(f, [{"op": "duplicate_slide", "slide_index": 0, "to_index": 1},
                     {"op": "update_chart_data", "slide_index": 1, "categories": ["X"],
                      "series": [{"name": "R", "values": [9]}]},
                     {"op": "set_speaker_notes", "slide_index": 1, "text": "Copy's own"}], create_new=False)
    assert out.startswith("Presentation saved: ") and "Failed:" not in out, out
    prs = Presentation(str(f))
    source, copy, _ = prs.slides
    assert list(next(s for s in source.shapes if s.has_chart).chart.plots[0].categories) == ["Q1", "Q2"]
    assert list(next(s for s in copy.shapes if s.has_chart).chart.plots[0].categories) == ["X"]
    assert source.notes_slide.notes_text_frame.text == "Say this"


def test_duplicates_in_one_batch_get_their_own_parts(tmp_path):
    f = tmp_path / "d.pptx"
    _rich_slide(f)
    out = _write(f, [{"op": "duplicate_slide", "slide_index": 0}, {"op": "duplicate_slide", "slide_index": 0},
                     {"op": "duplicate_slide", "slide_index": -1, "to_index": 0}], create_new=False)
    assert out.startswith("Presentation saved: ") and "Checks: no problems found." in out, out
    prs = Presentation(str(f))
    names = [str(s.part.partname) for s in prs.slides]
    assert len(names) == len(set(names)) == 5
    charts = [str(next(sh for sh in s.shapes if sh.has_chart).chart.part.partname)
              for s in prs.slides if any(sh.has_chart for sh in s.shapes)]
    assert len(charts) == len(set(charts)) == 4
    out = _write(f, [{"op": "duplicate_slide", "slide_index": 0, "to_index": 9}], create_new=False)
    assert "to_index 9 is outside the deck" in out and out.startswith("Nothing saved: ")


_P188 = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
_COMMENT_KINDS = {
    # 2006 comments: the relationship alone. 2018 modern comments: the
    # relationship and a commentRel in the slide's extLst that names it.
    "2006": ("/ppt/comments/comment1.xml", "application/vnd.openxmlformats-officedocument.presentationml.comments+xml",
             "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
             b'<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'),
    "2018": ("/ppt/comments/modernComment_100_1.xml", "application/vnd.ms-powerpoint.comments+xml",
             "http://schemas.microsoft.com/office/2018/10/relationships/comments",
             f'<p188:cmLst xmlns:p188="{_P188}"/>'.encode()),
}


@pytest.mark.parametrize("kind", sorted(_COMMENT_KINDS))
def test_a_duplicate_leaves_the_source_slides_comments_behind(tmp_path, kind):
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI

    f = tmp_path / "c.pptx"
    _write(f, [{"op": "add_slide", "title": "With a comment"}])
    prs = Presentation(str(f))
    name, content_type, reltype, blob = _COMMENT_KINDS[kind]
    comment = Part(PackURI(name), content_type, prs.part.package, blob)
    rid = prs.slides[0].part.relate_to(comment, reltype)
    if kind == "2018":
        ext_lst = etree.SubElement(prs.slides[0]._element, qn("p:extLst"))
        ext = etree.SubElement(ext_lst, qn("p:ext"), uri="{6950BFC3-D8DA-4A85-94F7-54DA5524770B}")
        etree.SubElement(ext, f"{{{_P188}}}commentRel", {qn("r:id"): rid})
    prs.save(str(f))
    out = _write(f, [{"op": "duplicate_slide", "slide_index": 0}], create_new=False)
    assert "Checks: no problems found." in out, out
    prs = Presentation(str(f))
    kinds = [[r.reltype.rsplit("/", 1)[-1] for r in s.part.rels.values()] for s in prs.slides]
    assert "comments" in kinds[0] and "comments" not in kinds[1]
    source, copy = (etree.tostring(s._element).decode() for s in prs.slides)
    assert ("commentRel" in source) is (kind == "2018")
    assert "commentRel" not in copy and "extLst" not in copy


def test_a_duplicate_gets_its_own_creation_id(tmp_path):
    f = tmp_path / "c.pptx"
    _write(f, [{"op": "add_slide", "title": "Named"}])
    prs = Presentation(str(f))
    p14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
    ext_lst = etree.SubElement(prs.slides[0]._element, qn("p:extLst"))
    ext = etree.SubElement(ext_lst, qn("p:ext"), uri="{BB962C8B-B14F-4D97-AF65-F5344CB8AC3E}")
    etree.SubElement(ext, f"{{{p14}}}creationId", val="1234567")
    prs.save(str(f))
    out = _write(f, [{"op": "duplicate_slide", "slide_index": 0}], create_new=False)
    assert "Checks: no problems found." in out, out
    ids = [s._element.find(f".//{{{p14}}}creationId").get("val") for s in Presentation(str(f)).slides]
    assert ids[0] == "1234567" and ids[1] != ids[0] and 0 < int(ids[1]) < 2 ** 32


def test_a_copy_made_after_a_text_op_gets_the_same_shrink(tmp_path):
    f = tmp_path / "s.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_only", "title": "Short"}])
    long = "Every region grew its revenue and held its costs through the whole of the year " * 2
    out = _write(f, [{"op": "set_text", "slide_index": 0, "placeholder": "title", "text": long},
                     {"op": "duplicate_slide", "slide_index": 0}], create_new=False)
    assert out.startswith("Presentation saved: "), out
    fits = [s.shapes.title.text_frame._bodyPr.find(qn("a:normAutofit")) for s in Presentation(str(f)).slides]
    scales = [fit.get("fontScale") for fit in fits]
    assert scales[0] is not None and scales == [scales[0]] * 2


_DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_DSP = "http://schemas.microsoft.com/office/drawing/2008/diagram"
_DRAWING_REL = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"


def _smartart_slide(path: Path) -> None:
    """A slide holding a SmartArt diagram whose relationship ids leave a
    gap, as PowerPoint's often do: its data part names the slide's
    relationship to the cached drawing (``dsp:dataModelExt/@relId``)."""
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI

    _write(path, [{"op": "add_slide", "layout": "blank"}])
    prs = Presentation(str(path))
    slide = prs.slides[0]
    package = prs.part.package
    gap = slide.part.relate_to(Part(PackURI("/ppt/media/gap1.bin"), "application/octet-stream", package, b"x"),
                               "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    ct = "application/vnd.openxmlformats-officedocument.drawingml."
    rids = {}
    for kind, root in (("Layout", "layoutDef"), ("QuickStyle", "styleDef"), ("Colors", "colorsDef")):
        blob = f'<dgm:{root} xmlns:dgm="{_DGM}"/>'.encode()
        part = Part(PackURI(f"/ppt/diagrams/{kind.lower()}1.xml"), f"{ct}diagram{kind}+xml", package, blob)
        rids[kind] = slide.part.relate_to(part, f"{rel}diagram{kind}")
    drawing = Part(PackURI("/ppt/diagrams/drawing1.xml"), "application/vnd.ms-office.drawingml.diagramDrawing+xml",
                   package, f'<dsp:drawing xmlns:dsp="{_DSP}"/>'.encode())
    rids["Drawing"] = slide.part.relate_to(drawing, _DRAWING_REL)
    data = (f'<dgm:dataModel xmlns:dgm="{_DGM}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<dgm:ptLst/><dgm:extLst><a:ext uri="http://schemas.microsoft.com/office/drawing/2008/diagram">'
            f'<dsp:dataModelExt xmlns:dsp="{_DSP}" relId="{rids["Drawing"]}"/></a:ext></dgm:extLst>'
            '</dgm:dataModel>').encode()
    rids["Data"] = slide.part.relate_to(Part(PackURI("/ppt/diagrams/data1.xml"), f"{ct}diagramData+xml", package,
                                             data), f"{rel}diagramData")
    slide.part.rels.pop(gap)
    frame = etree.fromstring(
        '<p:graphicFrame xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:nvGraphicFramePr><p:cNvPr id="4" name="Diagram 3"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
        '<p:xfrm><a:off x="914400" y="914400"/><a:ext cx="4572000" cy="2743200"/></p:xfrm>'
        f'<a:graphic><a:graphicData uri="{_DGM}"><dgm:relIds xmlns:dgm="{_DGM}" r:dm="{rids["Data"]}" '
        f'r:lo="{rids["Layout"]}" r:qs="{rids["QuickStyle"]}" r:cs="{rids["Colors"]}"/></a:graphicData></a:graphic>'
        '</p:graphicFrame>')
    slide.shapes._spTree.append(frame)
    prs.save(str(path))


def _drawing_rel_of(slide):
    data = next(r.target_part for r in slide.part.rels.values() if r.reltype.endswith("/diagramData"))
    rid = etree.fromstring(data.blob).find(f".//{{{_DSP}}}dataModelExt").get("relId")
    return slide.part.rels.get(rid)


def test_the_smartart_fixture_points_at_its_drawing(tmp_path):
    f = tmp_path / "a.pptx"
    _smartart_slide(f)
    rel = _drawing_rel_of(Presentation(str(f)).slides[0])
    assert rel is not None and rel.reltype == _DRAWING_REL


def test_a_duplicated_smartart_points_at_its_own_drawing(tmp_path):
    f = tmp_path / "a.pptx"
    _smartart_slide(f)
    out = _write(f, [{"op": "duplicate_slide", "slide_index": 0}], create_new=False)
    assert out.startswith("Presentation saved: "), out
    source, copy = Presentation(str(f)).slides
    rel = _drawing_rel_of(copy)
    assert rel is not None and rel.reltype == _DRAWING_REL
    assert rel.target_part is not _drawing_rel_of(source).target_part
