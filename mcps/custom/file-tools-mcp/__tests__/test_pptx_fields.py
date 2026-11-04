"""write_pptx slide numbers: a slide-number placeholder holding a live
``slidenum`` field on each numbered slide, new slides that follow the deck,
and cached numbers kept right when slides move.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import re
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx import Presentation
from pptx.oxml.ns import qn

import powerpoint as ppt_mod
from formats.pptx import pptx_fields


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


def _numbers(path: Path) -> list[list[str]]:
    """Per slide, the cached text of each slide-number field in a
    slide-number placeholder."""
    prs = Presentation(str(path))
    out = []
    for slide in prs.slides:
        texts = []
        for sp in pptx_fields._number_placeholders(slide.shapes._spTree):
            texts += [f.find(qn("a:t")).text for f in pptx_fields._fields(sp)]
        out.append(texts)
    return out


_DECK = [{"op": "add_slide", "layout": "title"},
         {"op": "add_slide", "layout": "title_and_content"},
         {"op": "add_slide", "layout": "two_content"}]


def test_every_slide_gets_a_live_number(tmp_path):
    f = tmp_path / "n.pptx"
    out = _write(f, _DECK + [{"op": "set_slide_number"}])
    assert _clean(out), out
    assert _numbers(f) == [["1"], ["2"], ["3"]]
    prs = Presentation(str(f))
    sp = pptx_fields._number_placeholders(prs.slides[1].shapes._spTree)[0]
    ph = pptx_fields._ph(sp)
    assert ph.get("idx") == "12" and ph.get("type") == "sldNum"
    assert sp.find(qn("p:spPr")).find(qn("a:xfrm")) is None  # the layout's box is inherited
    field = pptx_fields._fields(sp)[0]
    assert re.fullmatch(r"\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}",
                        field.get("id"))


def test_numbering_twice_adds_nothing(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number"}, {"op": "set_slide_number", "enabled": True}])
    assert _numbers(f) == [["1"], ["2"], ["3"]]


def test_one_slide_and_the_title_slide_skip(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number", "slide_index": -1}])
    assert _numbers(f) == [[], [], ["3"]]
    _write(f, [{"op": "set_slide_number", "skip_title_slide": True}], create_new=False)
    assert _numbers(f) == [[], ["2"], ["3"]]


def test_turning_numbers_off_removes_them_and_leaves_the_layouts(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number"}])
    before = [etree.tostring(lay._element) for lay in Presentation(str(f)).slide_layouts]
    out = _write(f, [{"op": "set_slide_number", "enabled": False}], create_new=False)
    assert _clean(out), out
    assert _numbers(f) == [[], [], []]
    assert [etree.tostring(lay._element) for lay in Presentation(str(f)).slide_layouts] == before


@pytest.mark.parametrize("off", ["false", "False", " false ", 0, False])
def test_enabled_false_as_a_word_or_a_number_turns_numbers_off(tmp_path, off):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number", "skip_title_slide": "true"},
                       {"op": "set_slide_number", "slide_index": 1, "enabled": off}])
    assert _numbers(f) == [[], [], ["3"]]


@pytest.mark.parametrize("word", ["no", "off", "0", "maybe"])
def test_a_word_that_is_not_true_or_false_is_refused(tmp_path, word):
    f = tmp_path / "n.pptx"
    out = _write(f, _DECK + [{"op": "set_slide_number", "enabled": word}])
    assert out.startswith("Nothing saved: ") and "'enabled' expects true or false" in out
    assert not f.exists()


def test_skip_title_slide_false_as_a_word_numbers_the_title_slide(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number", "skip_title_slide": "false", "enabled": "true"}])
    assert _numbers(f) == [["1"], ["2"], ["3"]]


def test_new_slides_follow_the_deck_title_slides_apart(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number", "skip_title_slide": True},
                       {"op": "add_slide", "layout": "title_only"},
                       {"op": "add_slide", "layout": "title"}])
    assert _numbers(f) == [[], ["2"], ["3"], ["4"], []]


def test_a_closing_title_slide_is_numbered_when_no_title_slide_was_kept_apart(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_and_content"},
               {"op": "add_slide", "layout": "title_and_content"},
               {"op": "set_slide_number"},
               {"op": "add_slide", "layout": "title"}])
    assert _numbers(f) == [["1"], ["2"], ["3"]]


def test_a_content_slide_follows_a_deck_of_numbered_title_slides(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, [{"op": "add_slide", "layout": "title"},
               {"op": "set_slide_number"},
               {"op": "add_slide", "layout": "title_only"}])
    assert _numbers(f) == [["1"], ["2"]]


def test_an_unnumbered_deck_stays_unnumbered(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "add_slide"}])
    assert _numbers(f) == [[], [], [], []]


def test_moving_slides_refreshes_the_cached_numbers(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number"}])
    out = _write(f, [{"op": "move_slide", "from_index": 2, "to_index": 0},
                     {"op": "delete_slide", "slide_index": 1}], create_new=False)
    assert _clean(out), out
    assert _numbers(f) == [["1"], ["2"]]


def test_an_edit_that_moves_no_slide_leaves_the_fields_alone(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number"}])
    prs = Presentation(str(f))
    sp = pptx_fields._number_placeholders(prs.slides[2].shapes._spTree)[0]
    pptx_fields._fields(sp)[0].find(qn("a:t")).text = "‹#›"
    prs.save(str(f))
    _write(f, [{"op": "set_text", "slide_index": 1, "text": "Edited"}], create_new=False)
    assert _numbers(f)[2] == ["‹#›"]


def test_the_first_slide_number_is_honoured(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK)
    prs = Presentation(str(f))
    prs.part._element.set("firstSlideNum", "5")
    prs.save(str(f))
    _write(f, [{"op": "set_slide_number"}], create_new=False)
    assert _numbers(f) == [["5"], ["6"], ["7"]]


def test_a_number_the_person_typed_is_kept_and_an_empty_one_filled(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number"}])
    prs = Presentation(str(f))
    typed = pptx_fields._number_placeholders(prs.slides[0].shapes._spTree)[0]
    pptx_fields._fill(typed, 0)
    field = pptx_fields._fields(typed)[0]
    para = field.getparent()
    para.remove(field)
    run = etree.SubElement(para, qn("a:r"))
    etree.SubElement(run, qn("a:t")).text = "Page one"
    empty = pptx_fields._number_placeholders(prs.slides[1].shapes._spTree)[0]
    body = empty.find(qn("p:txBody"))
    for p in body.findall(qn("a:p")):
        body.remove(p)
    etree.SubElement(body, qn("a:p"))
    prs.save(str(f))
    _write(f, [{"op": "set_slide_number"}], create_new=False)
    prs = Presentation(str(f))
    assert prs.slides[0].shapes[-1].text_frame.text == "Page one"
    assert _numbers(f)[1] == ["2"]


def test_a_bodyless_number_placeholder_gets_its_text_before_its_extension_list(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK + [{"op": "set_slide_number"}])
    prs = Presentation(str(f))
    sp = pptx_fields._number_placeholders(prs.slides[1].shapes._spTree)[0]
    sp.remove(sp.find(qn("p:txBody")))
    etree.SubElement(sp, qn("p:extLst"))
    prs.save(str(f))
    _write(f, [{"op": "set_slide_number"}], create_new=False)
    sp = pptx_fields._number_placeholders(Presentation(str(f)).slides[1].shapes._spTree)[0]
    assert [c.tag.rsplit("}", 1)[-1] for c in sp] == ["nvSpPr", "spPr", "txBody", "extLst"]
    assert _numbers(f)[1] == ["2"]


def _strip_layout_numbers(prs, layout_index):
    layout = prs.slide_layouts[layout_index]
    for sp in pptx_fields._number_placeholders(layout.shapes._spTree):
        sp.getparent().remove(sp)


def test_a_layout_without_a_number_gets_an_orphan_with_a_free_idx(tmp_path):
    prs = Presentation()
    _strip_layout_numbers(prs, 4)  # Comparison: its content placeholder 4 is the master's sldNum idx
    prs.slides.add_slide(prs.slide_layouts[4])
    f = tmp_path / "o.pptx"
    prs.save(str(f))
    out = _write(f, [{"op": "set_slide_number"}], create_new=False)
    assert _clean(out), out
    prs = Presentation(str(f))
    sp = pptx_fields._number_placeholders(prs.slides[0].shapes._spTree)[0]
    used = {ph.placeholder_format.idx for ph in prs.slide_layouts[4].placeholders}
    idx = int(pptx_fields._ph(sp).get("idx"))
    assert idx not in used and idx > 12
    master_ph = pptx_fields._number_placeholders(prs.slide_masters[0].shapes._spTree)[0]
    off = sp.find(qn("p:spPr")).find(qn("a:xfrm")).find(qn("a:off"))
    master_off = master_ph.find(qn("p:spPr")).find(qn("a:xfrm")).find(qn("a:off"))
    assert off.attrib == master_off.attrib
    assert _numbers(f) == [["1"]]


def test_no_number_anywhere_gets_a_bottom_right_box(tmp_path):
    prs = Presentation()
    _strip_layout_numbers(prs, 6)
    for sp in pptx_fields._number_placeholders(prs.slide_masters[0].shapes._spTree):
        sp.getparent().remove(sp)
    prs.slides.add_slide(prs.slide_layouts[6])
    f = tmp_path / "o.pptx"
    prs.save(str(f))
    _write(f, [{"op": "set_slide_number"}], create_new=False)
    prs = Presentation(str(f))
    sp = pptx_fields._number_placeholders(prs.slides[0].shapes._spTree)[0]
    off = sp.find(qn("p:spPr")).find(qn("a:xfrm")).find(qn("a:off"))
    assert int(off.get("x")) > prs.slide_width / 2 and int(off.get("y")) > prs.slide_height / 2
    assert sp.find(qn("p:txBody")).find(qn("a:p")).find(qn("a:pPr")).get("algn") == "r"


def test_a_master_that_hides_numbers_is_turned_on(tmp_path):
    f = tmp_path / "n.pptx"
    _write(f, _DECK)
    prs = Presentation(str(f))
    master = prs.slide_masters[0]._element
    hf = master.makeelement(qn("p:hf"), {"sldNum": "0", "hdr": "0"})
    master.find(qn("p:txStyles")).addprevious(hf)
    prs.save(str(f))
    _write(f, [{"op": "set_slide_number"}], create_new=False)
    hf = Presentation(str(f)).slide_masters[0]._element.find(qn("p:hf"))
    assert hf.get("sldNum") is None and hf.get("hdr") == "0"


def test_numbering_an_empty_deck_says_so(tmp_path):
    f = tmp_path / "n.pptx"
    out = _write(f, [{"op": "set_slide_number"}])
    assert "has no slides yet" in out
