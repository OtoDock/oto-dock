"""xml_patch on a deck: the five parts it reaches, a refused patch leaving
the file byte for byte as it was (and the part as it was when best_effort
saves the rest), a successful one leaving a valid package, a removed
shape's animations and glue going with it, and the theme shared with
set_theme in one batch.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
import zipfile
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

import powerpoint as ppt_mod
from checks import CheckContext, Severity
from formats.pptx import pptx_animations as an
from ooxml import validate

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


async def _same(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _same)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _write(path, ops, create_new=False, best_effort=False) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "operations": ops, "create_new": create_new,
                                                  "best_effort": best_effort}))


def _problems(path) -> list[str]:
    with CheckContext(str(path), "pptx", data=Path(path).read_bytes()) as ctx:
        found = validate.validate_package(ctx)
    return [f"{f.location}: {f.message}" for f in found if f.severity is not Severity.INFO]


def _part(path, name) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read(name)


_BADGE = "//p:sp[p:nvSpPr/p:cNvPr/@name='Badge']"
_TABLE = "//p:graphicFrame[a:graphic/a:graphicData/a:tbl]"
_CHART = "//p:graphicFrame[a:graphic/a:graphicData/c:chart]"


@pytest.fixture
def deck(tmp_path):
    f = tmp_path / "d.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_only", "title": "Results"},
                     {"op": "add_shape", "slide_index": 0, "name": "Badge", "text": "Badge", "left": 1, "top": 2,
                      "width": 3, "height": 1.5},
                     {"op": "add_shape", "slide_index": 0, "name": "Other", "text": "Other", "left": 6, "top": 2,
                      "width": 3, "height": 1.5},
                     {"op": "add_table", "slide_index": 0, "headers": ["Region", "Q1"], "rows": [["EMEA", 12]],
                      "left": 1, "top": 3.8, "width": 4, "height": 1},
                     {"op": "add_chart", "slide_index": 0, "categories": ["Q1", "Q2"],
                      "series": [{"name": "Revenue", "values": [12, 15]}], "left": 6, "top": 3.8, "width": 4,
                      "height": 3.2},
                     {"op": "set_speaker_notes", "slide_index": 0, "text": "Lead with the number."}],
                 create_new=True)
    assert out.startswith("Presentation saved: "), out
    return f


def test_a_patch_changes_the_slide_and_the_package_stays_valid(deck):
    out = _write(deck, [{"op": "xml_patch", "slide_index": 0, "xpath": f"{_BADGE}/p:spPr/a:prstGeom",
                         "action": "replace", "xml": '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'},
                        {"op": "xml_patch", "slide_index": 0, "xpath": f"{_BADGE}/p:spPr", "action": "insert_child",
                         "xml": '<a:ln w="38100"><a:solidFill><a:schemeClr val="accent2"/></a:solidFill></a:ln>'}])
    assert out.startswith("Presentation saved: ") and "Checks: no problems found." in out, out
    assert "replaced 1 element in ppt/slides/slide1.xml (slide_index 0)" in out
    badge = next(s for s in Presentation(str(deck)).slides[0].shapes if s.name == "Badge")
    sp_pr = badge._element.find(f"{P}spPr")
    assert sp_pr.find(f"{A}prstGeom").get("prst") == "ellipse"
    # insert_child puts the outline in the schema's place, after the geometry and fill.
    assert [etree.QName(c).localname for c in sp_pr][-1] == "ln"
    assert _problems(deck) == []


# Each refusal: the op, and the words its message holds.
_REFUSED = [
    ({"xpath": f"{_BADGE}/p:txBody/a:p/a:r", "action": "insert_after",
      "xml": '<a:fld id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}" type="slidenum"><a:t>1</a:t></a:fld>'},
     "a patch never adds, changes or removes a field"),
    ({"xpath": f"{_BADGE}/p:nvSpPr/p:cNvPr", "action": "insert_child",
      "xml": '<a:hlinkClick r:id="" action="ppaction://program"/>'}, "runs, fetches or embeds something"),
    ({"xpath": f"{_BADGE}/p:spPr", "action": "insert_child",
      "xml": '<s:ln xmlns:s="http://purl.oclc.org/ooxml/drawingml/main"/>'}, "Strict-conformance namespace"),
    ({"xpath": f"{_BADGE}/p:spPr", "action": "insert_child",
      "xml": '<a:blipFill><a:blip r:embed="rId99"/></a:blipFill>'}, "relationship id rId99"),
    ({"xpath": f"{_BADGE}/p:spPr/a:prstGeom", "action": "insert_before", "xml": "<a:ln/>"},
     "where the schema puts it before"),
    ({"xpath": "/p:sld", "action": "remove"}, "the part's root element"),
    ({"xpath": "//p:sp//a:t", "action": "remove"}, "descendant"),
    ({"xpath": "//p:sp", "action": "remove"}, "matched 3 elements and match is 'one'"),
    ({"xpath": "//p:pic", "action": "remove"}, "matched no element"),
    ({"part": "master", "xpath": "//p:sldLayoutIdLst", "action": "remove"}, "the master lists no layout"),
    ({"part": "master", "xpath": "//p:sldLayoutIdLst/p:sldLayoutId", "action": "remove", "match": "all"},
     "the master lists no layout"),
    ({"part": "presentation", "xpath": "//p:sldIdLst", "action": "remove"}, "unknown part 'presentation'"),
    ({"xpath": _BADGE, "action": "replace", "xml": "<p:foo/>"}, "<p:spTree> never holds a <p:foo>"),
    ({"xpath": f"{_BADGE}/p:spPr", "action": "insert_child", "xml": "<a:t>x</a:t>"},
     "<p:spPr> never holds a <a:t>"),
    ({"xpath": f"{_BADGE}/p:spPr", "action": "remove"}, "<p:sp> needs a <p:spPr>"),
    ({"xpath": f"{_BADGE}/p:nvSpPr", "action": "remove"}, "<p:sp> needs a <p:nvSpPr>"),
    ({"xpath": "//p:spTree", "action": "remove"}, "<p:cSld> needs a <p:spTree>"),
    ({"xpath": "//p:cSld", "action": "remove"}, "<p:sld> needs a <p:cSld>"),
    ({"xpath": f"{_BADGE}/p:txBody/a:p", "action": "remove", "match": "all"}, "<p:txBody> needs a <a:p>"),
    ({"xpath": f"{_BADGE}", "action": "swap"}, "'swap' is not a patch action"),
    ({"xpath": f"{_BADGE}/p:nvSpPr/p:cNvPr", "action": "replace", "xml": '<p:cNvPr id="9"/>'},
     "the patch leaves ppt/slides/slide1.xml broken"),
    ({"xpath": f"{_BADGE}/p:spPr/a:xfrm/a:off", "action": "replace", "xml": '<a:off x="abc" y="0"/>'},
     "the patch leaves ppt/slides/slide1.xml broken"),
    ({"xpath": "//p:spTree", "action": "insert_child", "xml": '<x:foo xmlns:x="urn:example"/>'},
     "<p:spTree> never holds a <{urn:example}foo>"),
    ({"xpath": "//p:spTree", "action": "insert_child",
      "xml": '<p14:creationId xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main" val="1"/>'},
     "<p:spTree> never holds a <p14:creationId>"),
    # The walk's chart-axis patch on a slide: the axis is in the chart's own part.
    ({"xpath": "//c:catAx/c:delete", "action": "remove"}, "sit in the chart's own part"),
]


@pytest.mark.parametrize(("keys", "message"), _REFUSED)
def test_a_refused_patch_leaves_the_file_as_it_was(deck, keys, message):
    before = deck.read_bytes()
    out = _write(deck, [{"op": "xml_patch", "slide_index": 0, **keys}])
    assert out.startswith("Nothing saved: ") and message in out, out
    assert deck.read_bytes() == before


@pytest.mark.parametrize(("keys", "message"), [
    ({"xpath": f"{_BADGE}/p:spPr", "action": "insert_child",
      "xml": '<a:blipFill><a:blip r:embed="rId99"/></a:blipFill>'}, "relationship id rId99"),
    ({"xpath": f"{_BADGE}/p:spPr", "action": "remove"}, "<p:sp> needs a <p:spPr>"),
    ({"xpath": "//p:cSld", "action": "remove"}, "<p:sld> needs a <p:cSld>"),
    ({"xpath": f"{_BADGE}/p:nvSpPr/p:cNvPr", "action": "replace", "xml": '<p:cNvPr id="9"/>'},
     "the patch leaves ppt/slides/slide1.xml broken"),
    ({"xpath": f"{_TABLE}/a:graphic/a:graphicData", "action": "replace",
      "xml": '<a:graphicData><a:tbl><a:tblPr/><a:tblGrid><a:gridCol w="3657600"/></a:tblGrid></a:tbl></a:graphicData>'},
     "the patch leaves ppt/slides/slide1.xml broken"),
    ({"xpath": f"{_CHART}/a:graphic/a:graphicData", "action": "replace",
      "xml": '<a:graphicData><c:chart r:id="rId2"/></a:graphicData>'}, "the patch leaves ppt/slides/slide1.xml broken"),
    ({"xpath": f"{_CHART}/a:graphic/a:graphicData/c:chart", "action": "remove"},
     "the patch leaves ppt/slides/slide1.xml broken"),
])
def test_a_refused_patch_leaves_its_part_as_it_was_when_the_rest_is_saved(deck, keys, message):
    slide_before = _part(deck, "ppt/slides/slide1.xml")
    out = _write(deck, [{"op": "xml_patch", "slide_index": 0, **keys},
                        {"op": "set_speaker_notes", "slide_index": 0, "text": "Lead with the margin."}],
                 best_effort=True)
    assert out.startswith("Presentation saved: ") and message in out, out
    assert "1 failed and changed nothing" in out and "partly" not in out, out
    assert _part(deck, "ppt/slides/slide1.xml") == slide_before
    assert "Lead with the margin." in Presentation(str(deck)).slides[0].notes_slide.notes_text_frame.text


def _fill(path, name) -> str | None:
    shape = next(s for s in Presentation(str(path)).slides[0].shapes if s.name == name)
    colour = shape._element.find(f"{P}spPr/{A}solidFill/{A}schemeClr")
    return None if colour is None else colour.get("val")


_REFUSED_PATCH = {"op": "xml_patch", "slide_index": 0, "xpath": f"{_BADGE}/p:spPr", "action": "remove"}


def test_ops_after_a_refused_patch_still_reach_the_slide(deck):
    out = _write(deck, [{"op": "set_shape_format", "slide_index": 0, "shape_name": "Badge", "fill_color": "accent3"},
                        _REFUSED_PATCH,
                        {"op": "set_shape_format", "slide_index": 0, "shape_name": "Other", "fill_color": "accent2"}],
                 best_effort=True)
    assert "2 operations applied" in out and "<p:sp> needs a <p:spPr>" in out, out
    assert (_fill(deck, "Badge"), _fill(deck, "Other")) == ("accent3", "accent2")


def test_a_refused_patch_keeps_the_measure_of_text_written_before_it(deck, tmp_path):
    long = "A sentence long enough to need three lines in this small box at this size"
    ops = [{"op": "add_textbox", "slide_index": 0, "text": long, "left": 1, "top": 5, "width": 3, "height": 0.6,
            "font_size": 24, "autofit": "shrink"},
           {"op": "set_shape_format", "slide_index": 0, "shape_name": "Other", "fill_color": "accent2"}]
    plain = tmp_path / "plain.pptx"
    plain.write_bytes(deck.read_bytes())
    _write(plain, ops)
    out = _write(deck, [ops[0], _REFUSED_PATCH, ops[1]], best_effort=True)
    assert "2 operations applied" in out, out

    def scale(path):
        shapes = Presentation(str(path)).slides[0].shapes
        box = next(s for s in shapes if s.has_text_frame and s.text_frame.text == long)
        return box._element.find(f"{P}txBody/{A}bodyPr/{A}normAutofit").get("fontScale")

    assert scale(deck) == scale(plain) is not None and _fill(deck, "Other") == "accent2"


def test_ops_after_a_patch_that_replaces_the_shape_tree_reach_the_new_one(deck):
    with zipfile.ZipFile(deck) as z:
        tree = etree.fromstring(z.read("ppt/slides/slide1.xml")).find(f"{P}cSld/{P}spTree")
    out = _write(deck, [{"op": "set_shape_format", "slide_index": 0, "shape_name": "Badge", "fill_color": "accent3"},
                        {"op": "xml_patch", "slide_index": 0, "xpath": "//p:cSld/p:spTree", "action": "replace",
                         "xml": etree.tostring(tree).decode()},
                        {"op": "set_shape_format", "slide_index": 0, "shape_name": "Other", "fill_color": "accent2"}])
    assert out.startswith("Presentation saved: ") and "3 operations applied" in out, out
    # The tree put in is the one read before the batch: Badge's new fill went with the old tree.
    assert (_fill(deck, "Badge"), _fill(deck, "Other")) == ("accent1", "accent2")


def test_a_slide_added_after_a_layout_patch_takes_the_patched_layout(deck):
    # A patch never touches a tree that holds a field, so the layout's date
    # and number placeholders go first.
    prs = Presentation(str(deck))
    layout = next(x for x in prs.slide_layouts if x.name == "Title Only")
    tree = layout._element.find(f"{P}cSld/{P}spTree")
    for sp in [sp for sp in tree.findall(f"{P}sp") if sp.find(f".//{A}fld") is not None]:
        tree.remove(sp)
    prs.save(str(deck))
    tree = etree.fromstring(etree.tostring(tree))
    title = next(sp for sp in tree.findall(f"{P}sp") if sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph").get("type") == "title")
    tree.remove(title)
    out = _write(deck, [{"op": "xml_patch", "part": "layout", "layout": "Title Only", "xpath": "//p:cSld/p:spTree",
                         "action": "replace", "xml": etree.tostring(tree).decode()},
                        {"op": "add_slide", "layout": "Title Only", "title": "Next"}])
    assert out.startswith("Presentation saved: ") and "has no title placeholder" in out, out
    assert Presentation(str(deck)).slides[1].shapes.title is None


def test_a_layout_patch_python_pptx_cannot_read_is_refused(deck):
    layout_name = "ppt/slideLayouts/slideLayout6.xml"
    before = _part(deck, layout_name)
    out = _write(deck, [{"op": "xml_patch", "part": "layout", "layout": "Title Only",
                         "xpath": "//p:sp[p:nvSpPr/p:nvPr/p:ph/@type='title']/p:nvSpPr/p:cNvPr", "action": "replace",
                         "xml": '<p:cNvPr id="9"/>'},
                        {"op": "set_speaker_notes", "slide_index": 0, "text": "Lead with the margin."}],
                 best_effort=True)
    assert out.startswith("Presentation saved: ") and f"the patch leaves {layout_name} broken" in out, out
    assert _part(deck, layout_name) == before


def test_foreign_elements_are_kept_inside_an_extension_or_alternate_content(deck):
    p14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
    out = _write(deck, [{"op": "xml_patch", "slide_index": 0, "xpath": "/p:sld", "action": "insert_child",
                         "xml": f'<p:extLst><p:ext uri="{{BB962C8B-B14F-4D97-AF65-F5344CB8AC3E}}">'
                                f'<p14:creationId xmlns:p14="{p14}" val="1"/></p:ext></p:extLst>'},
                        {"op": "xml_patch", "slide_index": 0, "xpath": "//p:spTree", "action": "insert_child",
                         "xml": f'<mc:AlternateContent xmlns:p14="{p14}"><mc:Choice Requires="p14"><x:foo '
                                'xmlns:x="urn:example"/></mc:Choice><mc:Fallback/></mc:AlternateContent>'}])
    assert out.startswith("Presentation saved: "), out


def test_removing_a_shape_by_patch_removes_its_animations_and_glue(deck):
    _write(deck, [{"op": "add_animation", "slide_index": 0, "shape_name": "Badge", "effect": "fade"},
                  {"op": "add_animation", "slide_index": 0, "shape_name": "Other", "effect": "fade"},
                  {"op": "add_connector", "slide_index": 0, "start_shape": "Badge", "end_shape": "Other"}])
    out = _write(deck, [{"op": "xml_patch", "slide_index": 0, "xpath": _BADGE, "action": "remove"}])
    assert out.startswith("Presentation saved: ") and "Checks: no problems found." in out, out
    assert "is gone or renumbered" in out and "removed its 1 animation and unglued 1 connector" in out, out
    sld = Presentation(str(deck)).slides[0]._element
    assert an.count_animations(sld) == 1 and _problems(deck) == []


def test_the_notes_part(deck, tmp_path):
    out = _write(deck, [{"op": "xml_patch", "part": "notes", "slide_index": 0, "xpath": "//a:r/a:t",
                         "action": "replace", "xml": "<a:t>Lead with the margin.</a:t>", "match": "all"}])
    assert out.startswith("Presentation saved: "), out
    assert Presentation(str(deck)).slides[0].notes_slide.notes_text_frame.text == "Lead with the margin."
    other = tmp_path / "n.pptx"
    _write(other, [{"op": "add_slide"}], create_new=True)
    out = _write(other, [{"op": "xml_patch", "part": "notes", "slide_index": 0, "xpath": "//a:t", "action": "remove"}])
    assert "slide_index 0 has no speaker notes" in out and "set_speaker_notes" in out, out


def test_a_layout_by_name_and_by_the_slide_that_uses_it(deck):
    dark = ('<a:overrideClrMapping bg1="dk1" tx1="lt1" bg2="dk2" tx2="lt2" accent1="accent1" accent2="accent2" '
            'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
            'folHlink="folHlink"/>')
    out = _write(deck, [{"op": "xml_patch", "part": "layout", "slide_index": 0,
                         "xpath": "//p:clrMapOvr/a:masterClrMapping", "action": "replace", "xml": dark}])
    assert out.startswith("Presentation saved: ") and "(layout 'Title Only')" in out, out
    layout = Presentation(str(deck)).slides[0].slide_layout
    assert layout._element.find(f"{P}clrMapOvr/{A}overrideClrMapping").get("bg1") == "dk1"
    out = _write(deck, [{"op": "xml_patch", "part": "layout", "layout": "blank",
                         "xpath": "//p:clrMapOvr/a:masterClrMapping", "action": "replace", "xml": dark}])
    assert "(layout 'Blank')" in out and _problems(deck) == [], out
    out = _write(deck, [{"op": "xml_patch", "part": "layout", "xpath": "//p:cSld", "action": "remove"}])
    assert "part layout needs layout or slide_index" in out, out


@pytest.mark.parametrize("order", ["patch first", "set_theme first"])
def test_the_theme_shared_with_set_theme_in_one_batch(deck, order):
    patch = {"op": "xml_patch", "part": "theme", "xpath": "//a:clrScheme/a:accent2/a:srgbClr", "action": "replace",
             "xml": '<a:srgbClr val="E07A1F"/>'}
    theme = {"op": "set_theme", "colors": {"accent1": "#1F4E79"}, "name": "Northwind"}
    ops = [patch, theme] if order == "patch first" else [theme, patch]
    out = _write(deck, ops)
    assert out.startswith("Presentation saved: "), out
    prs = Presentation(str(deck))
    root = etree.fromstring(prs.slide_masters[0].part.part_related_by(RT.THEME).blob)
    scheme = root.find(f"{A}themeElements/{A}clrScheme")
    assert scheme.find(f"{A}accent1/{A}srgbClr").get("val") == "1F4E79"
    assert scheme.find(f"{A}accent2/{A}srgbClr").get("val") == "E07A1F"
    assert root.get("name") == "Northwind" and _problems(deck) == []


def test_a_refused_theme_patch_alone_leaves_the_theme_byte_for_byte(deck):
    theme_before = _part(deck, "ppt/theme/theme1.xml")
    out = _write(deck, [{"op": "xml_patch", "part": "theme", "xpath": "//a:clrScheme/a:accent1", "action": "remove"},
                        {"op": "set_speaker_notes", "slide_index": 0, "text": "Lead with the margin."}],
                 best_effort=True)
    assert out.startswith("Presentation saved: ") and "<a:clrScheme> needs a <a:accent1>" in out, out
    assert _part(deck, "ppt/theme/theme1.xml") == theme_before


def test_a_theme_relationship_to_another_part_is_refused(deck, tmp_path):
    crafted = tmp_path / "crafted.pptx"
    rels = "ppt/slideMasters/_rels/slideMaster1.xml.rels"
    with zipfile.ZipFile(deck) as src, zipfile.ZipFile(crafted, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == rels:
                data = data.replace(b'Target="../theme/theme1.xml"', b'Target="../presProps.xml"')
            out.writestr(item, data)
    before = crafted.read_bytes()
    out = _write(crafted, [{"op": "xml_patch", "part": "theme", "xpath": "//p:extLst", "action": "remove"}])
    assert out.startswith("Nothing saved: ") and "is not a theme" in out, out
    assert crafted.read_bytes() == before


def test_a_refused_theme_patch_leaves_the_theme_as_it_was(deck):
    theme_before = _part(deck, "ppt/theme/theme1.xml")
    out = _write(deck, [{"op": "set_theme", "colors": {"accent2": "#E07A1F"}},
                        {"op": "xml_patch", "part": "theme", "xpath": "//a:clrScheme/a:accent1", "action": "remove"},
                        {"op": "set_speaker_notes", "slide_index": 0, "text": "Lead with the margin."}],
                 best_effort=True)
    assert out.startswith("Presentation saved: ") and "<a:clrScheme> needs a <a:accent1>" in out, out
    root = etree.fromstring(_part(deck, "ppt/theme/theme1.xml"))
    scheme = root.find(f"{A}themeElements/{A}clrScheme")
    # The set_theme before it stays; the refused patch left nothing.
    assert scheme.find(f"{A}accent2/{A}srgbClr").get("val") == "E07A1F"
    assert scheme.find(f"{A}accent1") is not None and theme_before != _part(deck, "ppt/theme/theme1.xml")


def test_a_patched_text_gets_the_scale_its_box_needs(deck):
    out = _write(deck, [{"op": "add_textbox", "slide_index": 0, "text": "Short", "left": 1, "top": 5, "width": 3,
                         "height": 0.6, "font_size": 24, "autofit": "shrink"}])
    assert out.startswith("Presentation saved: "), out
    long = "A sentence long enough to need three lines in this small box at this size"
    out = _write(deck, [{"op": "xml_patch", "slide_index": 0,
                         "xpath": "//p:sp[p:txBody/a:p/a:r/a:t='Short']/p:txBody/a:p/a:r/a:t", "action": "replace",
                         "xml": f"<a:t>{long}</a:t>"}])
    assert out.startswith("Presentation saved: "), out
    box = next(s for s in Presentation(str(deck)).slides[0].shapes if s.has_text_frame and s.text_frame.text == long)
    scale = box._element.find(f"{P}txBody/{A}bodyPr/{A}normAutofit").get("fontScale")
    assert scale is not None and int(scale) < 100000


CORPUS = sorted((Path(__file__).parent / "corpus").glob("*/*.pptx"))


@pytest.mark.parametrize("source", CORPUS, ids=[c.name for c in CORPUS])
def test_a_patch_on_a_deck_another_application_saved(tmp_path, source):
    import shutil

    f = tmp_path / source.name
    shutil.copy(source, f)
    before = set(_problems(f))
    # A deck with no slide (the Mac one) takes the patch on its first layout.
    where = {"slide_index": 0} if len(Presentation(str(f)).slides) else {"part": "layout", "layout": 0}
    out = _write(f, [{"op": "xml_patch", **where, "xpath": "//p:spTree", "action": "insert_child",
                      "xml": '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Patched"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                             '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="457200"/></a:xfrm>'
                             '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:sp>'}])
    assert out.startswith("Presentation saved: "), out
    prs = Presentation(str(f))
    shapes = prs.slides[0].shapes if "slide_index" in where else prs.slide_layouts[0].shapes
    patched = next(s for s in shapes if s.name == "Patched")
    assert sum(s.shape_id == patched.shape_id for s in shapes) == 1
    assert set(_problems(f)) <= before


@pytest.mark.parametrize(("keys", "message"), [
    ({"part": "theme", "xpath": "//a:clrScheme/a:accent1", "action": "remove"}, "needs a <a:accent1>"),
    ({"xpath": f"{_BADGE}/p:spPr/a:prstGeom", "action": "insert_before", "xml": "<a:ln/>"}, "schema puts it before"),
    ({"part": "presentation", "xpath": "//p:sldIdLst", "action": "remove"}, "unknown part"),
    ({"xpath": f"{_BADGE}/p:nvSpPr/p:cNvPr", "action": "replace", "xml": '<p:cNvPr id="9"/>'}, "cannot be read"),
])
def test_every_refusal_is_marked_as_changing_nothing(deck, keys, message):
    from formats.pptx import pptx_patch
    from formats.pptx.pptx_opkit import PptxJob
    from results import OpCtx, OpError

    job = PptxJob(prs=Presentation(str(deck)), path=str(deck))
    with pytest.raises(OpError, match=message) as refused:
        pptx_patch._op_xml_patch(job, OpCtx(0, "xml_patch"), {"slide_index": 0, "match": "one", **keys})
    assert refused.value.unchanged is True


def test_help_names_what_a_patch_refuses(deck):
    name = "xml_patch"
    out = _write(deck, [{"op": "help", "name": name}])
    for words in ("fields", "runs, fetches or embeds", "Strict-conformance", "any part but these five",
                  "root element", "an element its parent never holds", "bounded on large parts"):
        assert words in out, words


@pytest.mark.office
def test_a_patched_deck_renders_and_survives_libreoffice(deck, tmp_path):
    import shutil

    fitz = pytest.importorskip("fitz")

    import render_engine

    dark = ('<a:overrideClrMapping bg1="dk1" tx1="lt1" bg2="dk2" tx2="lt2" accent1="accent1" accent2="accent2" '
            'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
            'folHlink="folHlink"/>')
    out = _write(deck, [{"op": "xml_patch", "slide_index": 0, "xpath": f"{_BADGE}/p:spPr/a:prstGeom",
                         "action": "replace", "xml": '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'},
                        {"op": "xml_patch", "part": "layout", "slide_index": 0,
                         "xpath": "//p:clrMapOvr/a:masterClrMapping", "action": "replace", "xml": dark},
                        {"op": "xml_patch", "part": "theme", "xpath": "//a:clrScheme/a:accent1/a:srgbClr",
                         "action": "replace", "xml": '<a:srgbClr val="E07A1F"/>'}])
    assert out.startswith("Presentation saved: "), out
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    pdf = fitz.open(asyncio.run(render_engine.to_pdf(str(deck), out_dir=str(pdf_dir))))
    assert pdf.page_count == 1 and "Badge" in pdf[0].get_text()
    # The dark layout draws the slide's background dark.
    pixel = pdf[0].get_pixmap(clip=fitz.Rect(5, 5, 6, 6)).pixel(0, 0)
    assert sum(pixel) < 200, pixel
    shutil.rmtree(pdf_dir)
    again_dir = tmp_path / "lo"
    again_dir.mkdir()
    again = asyncio.run(render_engine._libreoffice_convert(str(deck), "pptx:Impress MS PowerPoint 2007 XML",
                                                    str(again_dir), extension="pptx"))
    shapes = Presentation(again).slides[0].shapes
    badge = next(s for s in shapes if s.has_text_frame and s.text_frame.text == "Badge")
    assert badge._element.find(f"{P}spPr/{A}prstGeom").get("prst") == "ellipse"
