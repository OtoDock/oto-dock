"""write_pptx and read_document on the presentation as a whole: the 16:9
default with layouts that follow the slide size, the kinds a path names
(templates, shows, macro decks), and layouts found by name in the deck
itself.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import io
import sys
import zipfile
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx import Presentation
from pptx.oxml.ns import qn

import powerpoint as ppt_mod
from formats.pptx import pptx_deck
from formats.pptx.pptx_read import read_deck


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
# 16:9 and the slide size
# ---------------------------------------------------------------------------


def _layout_boxes(prs) -> list[tuple]:
    boxes = []
    for master in prs.slide_masters:
        trees = [master.shapes._spTree] + [lay.shapes._spTree for lay in master.slide_layouts]
        for tree in trees:
            for el in tree:
                xfrm = pptx_deck._own_xfrm(el)
                if xfrm is not None and xfrm.find(qn("a:off")) is not None:
                    off, ext = xfrm.find(qn("a:off")), xfrm.find(qn("a:ext"))
                    boxes.append(tuple(int(v) for v in (off.get("x"), off.get("y"),
                                                       ext.get("cx"), ext.get("cy"))))
    return boxes


def test_a_new_presentation_is_16_9_and_its_layouts_fill_it(tmp_path):
    f = tmp_path / "wide.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_and_content"}])
    prs = Presentation(str(f))
    assert (prs.slide_width, prs.slide_height) == (12192000, 6858000)
    assert prs.part._element.find(qn("p:sldSz")).get("type") is None
    title = prs.slide_masters[0].placeholders[0]
    assert title.left + title.width > 0.9 * prs.slide_width
    assert abs((title.left + title.width / 2) - prs.slide_width / 2) < 914400 * 0.01


def test_a_new_presentation_carries_no_template_history(tmp_path):
    f = tmp_path / "fresh.pptx"
    _write(f, [{"op": "add_slide"}])
    props = Presentation(str(f)).core_properties
    assert props.last_modified_by == "" and props.comments == ""
    assert props.created.year >= 2026 and props.modified == props.created
    app = zipfile.ZipFile(f).read("docProps/app.xml")
    assert b"<PresentationFormat>Widescreen</PresentationFormat>" in app


def test_an_existing_4_3_deck_stays_4_3(tmp_path):
    f = tmp_path / "old.pptx"
    Presentation().save(str(f))
    _write(f, [{"op": "add_slide"}], create_new=False)
    prs = Presentation(str(f))
    assert (prs.slide_width, prs.slide_height) == (9144000, 6858000)


def test_slide_dimensions_move_the_layouts_and_back(tmp_path):
    original = _layout_boxes(Presentation())
    f = tmp_path / "s.pptx"
    _write(f, [{"op": "set_slide_dimensions", "width": 10, "height": 7.5}])
    prs = Presentation(str(f))
    assert (prs.slide_width, prs.slide_height) == (9144000, 6858000)
    back = _layout_boxes(prs)
    assert len(back) == len(original)
    assert all(abs(a - b) <= 2 for box_a, box_b in zip(back, original, strict=True)
               for a, b in zip(box_a, box_b, strict=True))


def test_the_widescreen_width_is_exact():
    assert pptx_deck.slide_length(round(13.333 * 914400)) == 12192000
    assert pptx_deck.slide_length(round(13.33 * 914400)) == 12192000
    assert pptx_deck.slide_length(9144000) == 9144000


def _onto_master(prs, shape):
    """Move a shape made on a slide onto the first master (python-pptx adds
    shapes to slides only); nothing is saved, so its relationships do not
    matter."""
    el = shape._element
    el.getparent().remove(el)
    prs.slide_masters[0].shapes._spTree.append(el)
    return el


def test_a_master_picture_keeps_its_proportions(tmp_path):
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGB", (40, 20), "red").save(logo)
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pic_el = _onto_master(prs, slide.shapes.add_picture(str(logo), 914400, 914400, 1828800, 914400))
    bar_el = _onto_master(prs, slide.shapes.add_shape(1, 0, 0, prs.slide_width, 457200))
    own = slide.shapes.add_textbox(914400, 914400, 914400, 914400)
    pptx_deck.set_slide_size(prs, pptx_deck.WIDE_CX, pptx_deck.WIDE_CY)
    xfrm = pptx_deck._own_xfrm(pic_el)
    off, ext = xfrm.find(qn("a:off")), xfrm.find(qn("a:ext"))
    assert (int(ext.get("cx")), int(ext.get("cy"))) == (1828800, 914400)
    assert int(off.get("x")) + 1828800 / 2 == pytest.approx((914400 + 1828800 / 2) * 4 / 3, abs=1)
    assert int(pptx_deck._own_xfrm(bar_el).find(qn("a:ext")).get("cx")) == pptx_deck.WIDE_CX
    assert (own.left, own.width) == (914400, 914400)


def test_an_extension_ext_is_left_alone():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box_el = _onto_master(prs, slide.shapes.add_shape(1, 914400, 914400, 914400, 914400))
    ext_lst = box_el.spPr.makeelement(qn("a:extLst"), {})
    ext_lst.append(ext_lst.makeelement(qn("a:ext"), {"uri": "{28A0092B-C50C-407E-A947-70E740481C1C}"}))
    box_el.spPr.append(ext_lst)
    pptx_deck.set_slide_size(prs, pptx_deck.WIDE_CX, pptx_deck.WIDE_CY)
    assert ext_lst[0].attrib == {"uri": "{28A0092B-C50C-407E-A947-70E740481C1C}"}


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------


def _main_type(path: Path) -> str:
    types = zipfile.ZipFile(path).read("[Content_Types].xml").decode()
    return next(t for t in pptx_deck._MAIN_TYPE_BY_EXT.values() if f'ContentType="{t}"' in types)


def _with_vba(path: Path) -> bytes:
    """A deck carrying a VBA project part, as a .pptm does."""
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    blob = b"\xd0\xcf\x11\xe0 not a real project"
    part = Part(PackURI("/ppt/vbaProject.bin"), "application/vnd.ms-office.vbaProject",
                prs.part.package, blob)
    prs.part.relate_to(part, pptx_deck._VBA_PROJECT)
    pptx_deck._set_main_content_type(prs, pptx_deck.PRESENTATION_MACRO)
    prs.save(str(path))
    return blob


@pytest.mark.parametrize("ext, kind", [
    (".potx", pptx_deck.TEMPLATE), (".potm", pptx_deck.TEMPLATE_MACRO),
    (".ppsx", pptx_deck.SHOW), (".ppsm", pptx_deck.SHOW_MACRO),
    (".pptm", pptx_deck.PRESENTATION_MACRO), (".PPTX", pptx_deck.PRESENTATION),
])
def test_a_path_saves_the_kind_its_extension_names_and_reopens(tmp_path, ext, kind):
    f = tmp_path / f"deck{ext}"
    out = _write(f, [{"op": "add_slide", "layout": "title"},
                     {"op": "set_text", "slide_index": 0, "text": "Kind"}])
    assert _clean(out), out
    assert _main_type(f) == kind
    out = _write(f, [{"op": "set_text", "slide_index": 0, "text": "Edited"}], create_new=False)
    assert _clean(out), out
    assert _main_type(f) == kind
    assert pptx_deck.open_presentation(str(f)).slides[0].shapes.title.text == "Edited"


def test_python_pptx_refuses_a_template_that_opens_here(tmp_path):
    f = tmp_path / "t.potx"
    _write(f, [{"op": "add_slide"}])
    with pytest.raises(ValueError):
        Presentation(str(f))
    assert len(pptx_deck.open_presentation(str(f)).slides) == 1


def test_a_template_copied_under_a_pptx_name_becomes_a_presentation(tmp_path):
    template = tmp_path / "brand.potx"
    _write(template, [{"op": "add_slide", "layout": "title"}])
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(template.read_bytes())
    out = _write(deck, [{"op": "add_slide", "layout": "title_only"}], create_new=False)
    assert _clean(out) and "Checks: no problems found." in out, out
    assert _main_type(deck) == pptx_deck.PRESENTATION
    assert len(Presentation(str(deck)).slides) == 2


def test_a_macro_deck_keeps_its_project(tmp_path):
    f = tmp_path / "m.pptm"
    blob = _with_vba(f)
    out = _write(f, [{"op": "add_slide"}], create_new=False)
    assert _clean(out), out
    z = zipfile.ZipFile(f)
    assert z.read("ppt/vbaProject.bin") == blob
    assert _main_type(f) == pptx_deck.PRESENTATION_MACRO


def test_macros_saved_under_a_pptx_name_are_removed_and_said(tmp_path):
    f = tmp_path / "m.pptx"
    _with_vba(f)
    out = _write(f, [{"op": "add_slide"}], create_new=False)
    assert "the macros were removed" in out and ".pptm" in out
    z = zipfile.ZipFile(f)
    assert not [n for n in z.namelist() if "vbaProject" in n]
    assert "vbaProject" not in z.read("[Content_Types].xml").decode()
    assert len(Presentation(str(f)).slides) == 2


def test_a_zip_that_is_no_presentation_is_refused(tmp_path):
    docx = pytest.importorskip("docx")
    f = tmp_path / "doc.pptx"
    docx.Document().save(str(f))
    with pytest.raises(Exception, match="not a PowerPoint presentation|not a PowerPoint"):
        pptx_deck.open_presentation(str(f))


def test_read_document_reads_a_template(tmp_path):
    f = tmp_path / "t.potx"
    _write(f, [{"op": "add_slide", "layout": "title"},
               {"op": "set_text", "slide_index": 0, "text": "Brand title"}])
    out = read_deck(str(f))
    assert "Brand title" in out and "13.33x7.5in" in out


def test_save_presentation_writes_into_a_stream():
    prs = pptx_deck.new_presentation()
    buf = io.BytesIO()
    assert pptx_deck.save_presentation(prs, buf, "x.potx") == []
    assert b"template.main+xml" in zipfile.ZipFile(io.BytesIO(buf.getvalue())).read(
        "[Content_Types].xml")


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


_TODAY = {"title": 0, "title_and_content": 1, "section_header": 2, "two_content": 3,
          "comparison": 4, "title_only": 5, "blank": 6, "content": 1, "image": 6}


@pytest.mark.parametrize("word, index", sorted(_TODAY.items()))
def test_the_old_words_give_the_layouts_they_always_gave(word, index):
    prs = Presentation()
    layout, remark = pptx_deck.resolve_layout(prs, word)
    assert layout == prs.slide_layouts[index] and remark == ""


def test_no_layout_key_is_blank(tmp_path):
    f = tmp_path / "b.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "add_slide", "layout": None}])
    assert _clean(out), out
    assert [s.slide_layout.name for s in Presentation(str(f)).slides] == ["Blank", "Blank"]


def _corporate() -> Presentation:
    """Renamed, reordered layouts, as a company template has them."""
    prs = Presentation()
    names = {"title": "Cover", "obj": "Text and chart", "blank": "Blank",
             "secHead": "Chapter", "twoObj": "Side by side"}
    for layout in prs.slide_layouts:
        t = layout._element.get("type")
        layout.name = names.get(t, layout.name)
    lst = prs.slide_masters[0]._element.find(qn("p:sldLayoutIdLst"))
    ids = list(lst)
    for el in ids:
        lst.remove(el)
    for el in reversed(ids):
        lst.append(el)
    return prs


def test_the_old_words_find_the_kind_in_a_company_template():
    prs = _corporate()
    for word, name in [("title", "Cover"), ("title_and_content", "Text and chart"),
                       ("section_header", "Chapter"), ("two_content", "Side by side")]:
        layout, remark = pptx_deck.resolve_layout(prs, word)
        assert layout.name == name and remark == "", word


def test_a_name_from_the_read_finds_its_layout(tmp_path):
    prs = _corporate()
    f = tmp_path / "corp.pptx"
    prs.save(str(f))
    out = _write(f, [{"op": "add_slide", "layout": "Chapter"},
                     {"op": "add_slide", "layout": "text AND chart"},
                     {"op": "add_slide", "layout": 0}], create_new=False)
    assert _clean(out), out
    got = [s.slide_layout.name for s in Presentation(str(f)).slides]
    assert got[:2] == ["Chapter", "Text and chart"]
    assert got[2] == pptx_deck.list_layouts(Presentation(str(f)))[0].name


def test_a_layout_named_blank_with_content_does_not_capture_the_word():
    prs = Presentation()
    content = prs.slide_layouts[1]
    content.name = "Blank"
    real_blank = prs.slide_layouts[6]
    real_blank.name = "Empty"
    assert pptx_deck.resolve_layout(prs, "blank")[0] == real_blank
    assert pptx_deck.resolve_layout(prs, "Blank")[0] == content


def test_greek_names_fold_without_accents():
    prs = Presentation()
    prs.slide_layouts[0].name = "Διαφάνεια τίτλου"
    assert pptx_deck.resolve_layout(prs, "ΔΙΑΦΑΝΕΙΑ ΤΙΤΛΟΥ")[0] == prs.slide_layouts[0]


def test_layouts_without_a_type_go_by_their_placeholders():
    prs = Presentation()
    for i, layout in enumerate(prs.slide_layouts):
        del layout._element.attrib["type"]
        layout.name = f"Layout {i}"
    expect = {"title": 0, "title_only": 5, "title_and_content": 1, "section_header": 1,
              "two_content": 3, "comparison": 4}
    for word, index in expect.items():
        layout, remark = pptx_deck.resolve_layout(prs, word)
        assert layout.name == f"Layout {index}" and f"no '{word}' layout" in remark, word
    assert pptx_deck.resolve_layout(prs, "blank")[0].name == "Layout 6"


def test_an_unknown_name_fails_the_batch_and_lists_the_layouts(tmp_path):
    f = tmp_path / "u.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title"},
                     {"op": "add_slide", "layout": "Agenda"},
                     {"op": "add_slide", "layout": 99},
                     {"op": "add_slide", "layout": -1}])
    assert out.startswith("Nothing saved: ") and out.count("no layout named") == 3
    assert "0 'Title Slide', 1 'Title and Content'" in out
    assert not f.exists()
    out = _write(f, [{"op": "add_slide", "layout": True}])
    assert out.startswith("Nothing saved: ") and "'layout' expects" in out


def test_best_effort_keeps_the_slides_whose_layout_resolved(tmp_path):
    f = tmp_path / "u.pptx"
    out = asyncio.run(ppt_mod.handle_write_pptx({"path": str(f), "create_new": True, "best_effort": True,
                                                 "operations": [{"op": "add_slide", "layout": "Agenda"},
                                                                {"op": "add_slide", "layout": "title"}]}))
    assert out.startswith("Presentation saved: ") and "no layout named 'Agenda'" in out
    assert [s.slide_layout.name for s in Presentation(str(f)).slides] == ["Title Slide"]


def test_two_masters_number_their_layouts_on(tmp_path):
    prs = Presentation()
    first = prs.slide_masters[0]
    import copy as _copy

    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.opc.packuri import PackURI
    from pptx.parts.slide import SlideLayoutPart, SlideMasterPart

    master_part = SlideMasterPart(PackURI("/ppt/slideMasters/slideMaster2.xml"),
                                  first.part.content_type, prs.part.package,
                                  _copy.deepcopy(first._element))
    master_part.relate_to(first.part.part_related_by(RT.THEME), RT.THEME)
    lst = master_part._element.find(qn("p:sldLayoutIdLst"))
    for el in list(lst):
        lst.remove(el)
    src = prs.slide_layouts[5]
    layout_part = SlideLayoutPart(PackURI("/ppt/slideLayouts/slideLayout99.xml"),
                                  src.part.content_type, prs.part.package,
                                  _copy.deepcopy(src._element))
    layout_part.relate_to(master_part, RT.SLIDE_MASTER)
    rid = master_part.relate_to(layout_part, RT.SLIDE_LAYOUT)
    lst.append(lst.makeelement(qn("p:sldLayoutId"), {"id": "2147483900", qn("r:id"): rid}))
    layout_part._element.cSld.set("name", "Second master title")
    prs.slides.part._element  # noqa: B018  # touch the part before relating
    rid_m = prs.part.relate_to(master_part, RT.SLIDE_MASTER)
    mlst = prs.part._element.find(qn("p:sldMasterIdLst"))
    mlst.append(mlst.makeelement(qn("p:sldMasterId"), {"id": "2147483899", qn("r:id"): rid_m}))
    f = tmp_path / "two.pptx"
    prs.save(str(f))
    out = read_deck(str(f))
    assert "11 Second master title (master 2): title 0" in out
    layout, remark = pptx_deck.resolve_layout(pptx_deck.open_presentation(str(f)), 11)
    assert layout.name == "Second master title" and remark == ""


def test_the_read_lists_the_layouts_with_their_placeholders(tmp_path):
    f = tmp_path / "r.pptx"
    _write(f, [{"op": "add_slide"}])
    out = read_deck(str(f))
    assert "**Layouts** (add_slide layout: a name or its number):" in out
    assert "  0 Title Slide: title 0, subtitle 1" in out
    assert "  1 Title and Content: title 0, content 1" in out
    assert "  6 Blank\n" in out


def test_placeholders_that_read_as_one_number_are_listed_without_it(tmp_path):
    prs = Presentation()
    layout = prs.slide_layouts[1]  # title (no idx: 0) and content (idx 1)
    for ph in layout.placeholders:
        ph.element.ph.attrib.pop("idx", None)
    f = tmp_path / "r.pptx"
    prs.save(str(f))
    out = read_deck(str(f))
    assert "  1 Title and Content: title, content\n" in out
    assert "  0 Title Slide: title 0, subtitle 1\n" in out


def test_the_layout_list_is_capped():
    prs = Presentation()
    entries = [pptx_deck.LayoutEntry(i, None, 1, f"L{i}", None, ()) for i in range(75)]
    lines = []
    original = pptx_deck.list_layouts
    try:
        pptx_deck.list_layouts = lambda _prs: entries
        lines = pptx_deck.describe_layouts(prs)
    finally:
        pptx_deck.list_layouts = original
    assert len(lines) == 1 + 60 + 1 and lines[-1].startswith("  +15 more")
