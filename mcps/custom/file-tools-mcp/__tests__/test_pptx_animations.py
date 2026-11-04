"""add_animation and remove_animations, and remove_shape cleaning what names
a removed shape: the timing tree's click groups, beats and rows, start
conditions, ids and build groups, on trees this tool made and on trees
written by hand after PowerPoint's.

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

import powerpoint as ppt_mod
from formats.pptx import pptx_animations as an

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


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


def _write(path, ops, create_new=True) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "operations": ops, "create_new": create_new}))


def _clean(out: str) -> bool:
    return out.startswith("Presentation saved: ") and "Checks: no problems found." in out


_CARDS = [{"op": "add_slide", "layout": "blank"},
          *({"op": "add_shape", "slide_index": 0, "text": f"Card {n}", "left": 1 + 3 * n, "top": 2, "width": 2.5,
             "height": 1.5} for n in range(4))]


def _sld(path, slide=0):
    return Presentation(str(path)).slides[slide]._element


def _rows(path, slide=0):
    _, trees = an.timings(_sld(path, slide))
    return an.rows(trees[0]) if trees else []


def _groups(path, slide=0):
    _, trees = an.timings(_sld(path, slide))
    main = an._main_sequence(trees[0])
    return [g for g in main.find(f"{P}childTnLst")]


@pytest.mark.parametrize("key", sorted(an.EFFECTS["entrance"]))
def test_every_entrance_effect_writes_a_valid_row(tmp_path, key):
    f = tmp_path / "a.pptx"
    out = _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": key}])
    assert _clean(out), out
    (row,) = _rows(f)
    assert row.get("presetClass") == "entr" and row.get("nodeType") == "clickEffect"
    assert an._targets(row) == {2}


@pytest.mark.parametrize(("kind", "key"), [(k, e) for k in ("emphasis", "exit") for e in an.EFFECTS[k]])
def test_every_emphasis_and_exit_effect_writes_a_valid_row(tmp_path, kind, key):
    f = tmp_path / "a.pptx"
    out = _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_index": 1, "effect": key,
                               "animation_type": kind}])
    assert _clean(out), out
    (row,) = _rows(f)
    assert row.get("presetClass") == {"emphasis": "emph", "exit": "exit"}[kind]


def test_the_presets_file_holds_the_effects_the_help_names():
    assert set(an.presets()) == {f"{kind}_{name}" for kind, names in an.EFFECTS.items() for name in names}
    assert "Hugo He" in an.json.loads(an.PRESETS_FILE.read_text(encoding="utf-8"))["source"]


def test_triggers_make_click_groups_beats_and_offsets(tmp_path):
    f = tmp_path / "a.pptx"
    out = _write(f, [*_CARDS,
                     {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fade"},
                     {"op": "add_animation", "slide_index": 0, "shape_index": 1, "effect": "fade",
                      "trigger": "with_previous", "delay": 250},
                     {"op": "add_animation", "slide_index": 0, "shape_index": 2, "effect": "wipe",
                      "trigger": "after_previous", "duration": 800},
                     {"op": "add_animation", "slide_index": 0, "shape_index": 3, "effect": "zoom"}])
    assert _clean(out), out
    groups = _groups(f)
    assert len(groups) == 2
    beats = [b for b in groups[0].find(f"{P}cTn/{P}childTnLst")]
    assert len(beats) == 2
    first, second = beats
    assert [r.get("nodeType") for r in first.iter(f"{P}cTn") if an.is_row(r)] == ["clickEffect", "withEffect"]
    # The second beat starts when the first ends: the 250 ms delay plus fade's 500 ms.
    assert an._delay(second.find(f"{P}cTn")) == 750
    wipe = next(r for r in second.iter(f"{P}cTn") if an.is_row(r))
    assert wipe.get("nodeType") == "afterEffect" and an.row_length(wipe) == 800
    ids = [c.get("id") for c in _sld(f).iter(f"{P}cTn")]
    assert len(ids) == len(set(ids))


def test_an_animation_that_starts_with_the_slide_waits_on_the_main_sequence(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fade",
                         "trigger": "after_previous"}])
    (group,) = _groups(f)
    conds = group.find(f"{P}cTn/{P}stCondLst")
    begin = next(c for c in conds if c.get("evt") == "onBegin")
    _, trees = an.timings(_sld(f))
    assert begin.find(f"{P}tn").get("val") == an._main_sequence(trees[0]).get("id")


def test_a_duration_scales_the_inner_delays_too(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fade",
                         "animation_type": "exit", "duration": 1000}])
    (row,) = _rows(f)
    # Fade out hides the shape at 499 of its 500 ms; at 1000 ms, at 998.
    hide = next(c for c in row.iter(f"{P}cTn") if c is not row and c.get("dur") == "1")
    assert an._delay(hide) == 998 and an.row_length(row) == 1000


def test_an_entrance_and_an_exit_on_one_shape_are_two_build_groups(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fade"},
               {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fade",
                "animation_type": "exit"}])
    assert [r.get("grpId") for r in _rows(f)] == ["0", "1"]


# A trigger as PowerPoint writes it: a click on shape {c} fades shape {d} in.
_TRIGGER = (
    '<p:seq concurrent="1" nextAc="seek"><p:cTn id="10" restart="whenNotActive" fill="hold" '
    'evtFilter="cancelBubble" nodeType="interactiveSeq"><p:stCondLst><p:cond evt="onClick" delay="0"><p:tgtEl>'
    '<p:spTgt spid="{c}"/></p:tgtEl></p:cond></p:stCondLst><p:endSync evt="end" delay="0"><p:rtn val="all"/>'
    '</p:endSync><p:childTnLst>'
    '<p:par><p:cTn id="11" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
    '<p:par><p:cTn id="12" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
    '<p:par><p:cTn id="13" presetID="10" presetClass="entr" presetSubtype="0" fill="hold" grpId="0" '
    'nodeType="clickEffect"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
    '<p:set><p:cBhvr><p:cTn id="14" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
    '<p:tgtEl><p:spTgt spid="{d}"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName>'
    '</p:attrNameLst></p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>'
    '<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="15" dur="500"/><p:tgtEl>'
    '<p:spTgt spid="{d}"/></p:tgtEl></p:cBhvr></p:animEffect></p:childTnLst></p:cTn></p:par>'
    '</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>'
    '</p:childTnLst></p:cTn><p:nextCondLst><p:cond evt="onClick" delay="0"><p:tgtEl><p:spTgt spid="{c}"/>'
    '</p:tgtEl></p:cond></p:nextCondLst></p:seq>'
)

_FOREIGN = (
    '<p:timing xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:tnLst><p:par>'
    '<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
    '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
    '<p:par><p:cTn id="3" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst>'
    '<p:par><p:cTn id="4" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
    '<p:par><p:cTn id="5" presetID="10" presetClass="entr" presetSubtype="0" fill="hold" grpId="0" '
    'nodeType="clickEffect"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
    '<p:set><p:cBhvr><p:cTn id="6" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
    '<p:tgtEl><p:spTgt spid="{a}"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName>'
    '</p:attrNameLst></p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>'
    '<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="7" dur="500"/><p:tgtEl>'
    '<p:spTgt spid="{a}"/></p:tgtEl></p:cBhvr></p:animEffect></p:childTnLst></p:cTn></p:par>'
    '<p:par><p:cTn id="8" presetID="10" presetClass="entr" presetSubtype="0" fill="hold" grpId="0" '
    'nodeType="withEffect"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
    '<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="9" dur="500"/><p:tgtEl>'
    '<p:spTgt spid="{b}"/></p:tgtEl></p:cBhvr></p:animEffect></p:childTnLst></p:cTn></p:par>'
    '</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>'
    '</p:childTnLst></p:cTn><p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl>'
    '</p:cond></p:prevCondLst><p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl>'
    '</p:cond></p:nextCondLst></p:seq>'
    f'{_TRIGGER}'
    '</p:childTnLst></p:cTn></p:par></p:tnLst><p:bldLst><p:bldP spid="{a}" grpId="0" animBg="1"/>'
    '<p:bldP spid="{b}" grpId="0" animBg="1"/><p:bldP spid="{d}" grpId="0" animBg="1"/></p:bldLst></p:timing>'
)


def _fill(template: str, **ids: int) -> str:
    for name, value in ids.items():
        template = template.replace("{" + name + "}", str(value))
    return template


def _with_foreign_timing(path) -> None:
    """Cards 0 and 1 animate together on a click, a click on card 2 fades
    card 3 in: a tree as PowerPoint writes it, by hand."""
    prs = Presentation(str(path))
    slide = prs.slides[0]
    ids = [s.shape_id for s in slide.shapes]
    slide._element.append(etree.fromstring(_fill(_FOREIGN, a=ids[0], b=ids[1], c=ids[2], d=ids[3])))
    prs.save(str(path))


def _with_trigger(path, trigger: int, target: int) -> None:
    """Add PowerPoint's trigger to the slide's timing tree: a click on the
    shape at ``trigger`` fades the one at ``target`` in."""
    prs = Presentation(str(path))
    slide = prs.slides[0]
    ids = [s.shape_id for s in slide.shapes]
    seq = etree.fromstring(f'<p:x xmlns:p="{P[1:-1]}">{_fill(_TRIGGER, c=ids[trigger], d=ids[target])}</p:x>')[0]
    _, trees = an.timings(slide._element)
    root = next(c for c in trees[0].iter(f"{P}cTn") if c.get("nodeType") == "tmRoot")
    root.find(f"{P}childTnLst").append(seq)
    for n, ctn in enumerate(seq.iter(f"{P}cTn")):
        ctn.set("id", str(100 + n))
    trees[0].append(etree.fromstring(f'<p:bldLst xmlns:p="{P[1:-1]}"><p:bldP spid="{ids[target]}" grpId="0" '
                                     'animBg="1"/></p:bldLst>'))
    prs.save(str(path))


_NON_EMPTY = ("tnLst", "childTnLst", "subTnLst", "bldLst", "stCondLst", "endCondLst", "prevCondLst",
              "nextCondLst")


def _empty_lists(path) -> list[str]:
    """The timing lists the schema needs one child in that have none."""
    return [etree.QName(e).localname for e in _sld(path).iter(*(f"{P}{n}" for n in _NON_EMPTY)) if not len(e)]


def test_an_animation_is_appended_to_a_tree_another_application_wrote(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _CARDS)
    _with_foreign_timing(f)
    out = _write(f, [{"op": "add_animation", "slide_index": 0, "shape_index": 3, "effect": "fly",
                      "direction": "left", "trigger": "after_previous"}], create_new=False)
    assert out.startswith("Presentation saved: ") and "pptx.package" not in out, out
    rows = _rows(f)
    # The new row joins the main sequence; the trigger's row stays last.
    assert len(rows) == 4 and rows[0].get("id") == "5" and rows[3].get("id") == "13"
    ids = [int(c.get("id")) for c in _sld(f).iter(f"{P}cTn")]
    assert len(ids) == len(set(ids)) and set(range(1, 16)) <= set(ids)
    assert int(rows[2].get("id")) > 15 and _empty_lists(f) == []


def test_removing_a_shape_removes_what_names_it(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _CARDS)
    _with_foreign_timing(f)
    prs = Presentation(str(f))
    shapes = list(prs.slides[0].shapes)
    connector = prs.slides[0].shapes.add_connector(1, 0, 0, 10, 10)
    connector.begin_connect(shapes[0], 1)
    connector.end_connect(shapes[3], 3)
    prs.save(str(f))
    assert _empty_lists(f) == []
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_index": 0}], create_new=False)
    assert _clean(out) and "removed its 1 animation and unglued 1 connector" in out, out
    rows = _rows(f)
    # The card that played with it now starts the click itself.
    assert len(rows) == 2 and rows[0].get("nodeType") == "clickEffect"
    sld = _sld(f)
    assert [b.get("spid") for b in sld.iter(f"{P}bldP")] == [str(shapes[1].shape_id), str(shapes[3].shape_id)]
    cxn = next(sld.iter(f"{P}cNvCxnSpPr"))
    assert cxn.find(f"{A}stCxn") is None and cxn.find(f"{A}endCxn") is not None
    assert _empty_lists(f) == []
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_name": "Card 2"}], create_new=False)
    assert _clean(out), out
    assert not any(c.get("nodeType") == "interactiveSeq" for c in _sld(f).iter(f"{P}cTn"))
    assert _empty_lists(f) == []
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_name": "Card 1"}], create_new=False)
    assert _clean(out), out
    assert an.timings(_sld(f)) == (None, [])


def test_removing_the_last_main_animation_keeps_a_trigger_whole(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_name": "Card 0", "effect": "fade"}])
    _with_trigger(f, trigger=2, target=1)
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_name": "Card 0"}], create_new=False)
    assert _clean(out) and "removed its 1 animation" in out, out
    assert _empty_lists(f) == []
    # PowerPoint writes a slide with triggers only without a main sequence.
    _, trees = an.timings(_sld(f))
    assert an._main_sequence(trees[0]) is None and len(_rows(f)) == 1
    out = _write(f, [{"op": "add_animation", "slide_index": 0, "shape_name": "Card 3", "effect": "fade"}],
                 create_new=False)
    assert _clean(out), out
    assert [r.get("nodeType") for r in _rows(f)] == ["clickEffect", "clickEffect"] and _empty_lists(f) == []


def test_removing_a_triggers_target_removes_its_sequence(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_name": "Card 0", "effect": "fade"}])
    _with_trigger(f, trigger=2, target=1)
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_name": "Card 1"}], create_new=False)
    assert _clean(out) and "removed its 1 animation" in out, out
    assert _empty_lists(f) == []
    assert not any(c.get("nodeType") == "interactiveSeq" for c in _sld(f).iter(f"{P}cTn"))
    assert len(_rows(f)) == 1 and [b.get("spid") for b in _sld(f).iter(f"{P}bldP")] == []
    out = _write(f, [{"op": "remove_animations", "slide_index": 0, "shape_name": "Card 0"}], create_new=False)
    assert "removed 1 animation" in out and an.timings(_sld(f)) == (None, [])


def test_remove_animations_leaves_a_trigger_valid(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, *({"op": "add_animation", "slide_index": 0, "shape_index": n, "effect": "fade"}
                          for n in range(2))])
    _with_trigger(f, trigger=2, target=3)
    out = _write(f, [{"op": "remove_animations", "slide_index": 0, "shape_index": 3}], create_new=False)
    assert "removed 1 animation" in out and _empty_lists(f) == []
    assert len(_rows(f)) == 2


def test_removing_a_group_removes_its_members_animations(tmp_path):
    f = tmp_path / "a.pptx"
    out = _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_name": "Card 0", "effect": "fade"},
                     {"op": "add_group_shape", "slide_index": 0, "shapes": ["Card 0", "Card 1"], "name": "Pair"},
                     {"op": "remove_shape", "slide_index": 0, "shape_name": "Pair"}])
    assert _clean(out) and "removed its 1 animation" in out, out
    assert an.timings(_sld(f)) == (None, [])


def test_a_wrapped_timing_is_refused_and_removable(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, _CARDS)
    prs = Presentation(str(f))
    sld = prs.slides[0]._element
    timing = _fill(_FOREIGN, a=2, b=3, c=4, d=5)
    sld.append(etree.fromstring(
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main">'
        f'<mc:Choice Requires="p14">{timing}</mc:Choice><mc:Fallback>{timing}</mc:Fallback>'
        '</mc:AlternateContent>'))
    prs.save(str(f))
    assert an.count_animations(_sld(f)) == 3
    out = _write(f, [{"op": "add_animation", "slide_index": 0, "shape_index": 3, "effect": "fade"}],
                 create_new=False)
    assert out.startswith("Nothing saved: ") and "remove_animations" in out
    out = _write(f, [{"op": "remove_shape", "slide_index": 0, "shape_index": 0}], create_new=False)
    assert "removed its 1 animation" in out and _empty_lists(f) == [], out
    out = _write(f, [{"op": "remove_animations", "slide_index": 0}], create_new=False)
    assert "removed 2 animations" in out and an.timings(_sld(f)) == (None, [])


def test_remove_animations_by_shape_and_for_the_slide(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, *({"op": "add_animation", "slide_index": 0, "shape_index": n, "effect": "fade"}
                          for n in range(3))])
    out = _write(f, [{"op": "remove_animations", "slide_index": 0, "shape_index": 1}], create_new=False)
    assert "removed 1 animation" in out and len(_rows(f)) == 2
    out = _write(f, [{"op": "remove_animations", "slide_index": 0}], create_new=False)
    assert "removed 2 animations" in out and an.timings(_sld(f)) == (None, [])
    out = _write(f, [{"op": "remove_animations", "slide_index": 0}], create_new=False)
    assert "has no animations" in out


@pytest.mark.parametrize(("op", "message"), [
    ({"effect": "fdae"}, "Did you mean 'fade'?"),
    ({"effect": "spin"}, "'spin' is not an entrance effect"),
    ({"effect": "fade", "direction": "left"}, "the entrance fade effect takes no direction"),
    ({"effect": "fly", "direction": "sideways"}, "Use one of: up, right, down, left"),
    ({"effect": "fade", "trigger": "later"}, "unknown trigger 'later'"),
    ({"effect": "fade", "animation_type": "intro"}, "unknown animation_type 'intro'"),
])
def test_a_wrong_animation_names_the_fix(tmp_path, op, message):
    f = tmp_path / "a.pptx"
    out = _write(f, [*_CARDS, {"op": "add_animation", "slide_index": 0, "shape_index": 0, **op}])
    assert out.startswith("Nothing saved: ") and message in out, out


def test_a_group_member_is_animated_through_its_group(tmp_path):
    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, {"op": "add_group_shape", "slide_index": 0, "shapes": ["Card 0", "Card 1"]}])
    prs = Presentation(str(f))
    member = next(s for s in prs.slides[0].shapes if s.shape_type == 6).shapes[0].shape_id
    out = _write(f, [{"op": "add_animation", "slide_index": 0, "shape_id": member, "effect": "fade"}],
                 create_new=False)
    assert "is inside a group" in out and out.startswith("Nothing saved: ")


def test_the_read_shows_each_slides_transition_and_animations(tmp_path):
    from formats.pptx import pptx_read

    f = tmp_path / "a.pptx"
    _write(f, [*_CARDS, {"op": "add_slide", "layout": "blank"},
               {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fade"},
               {"op": "add_animation", "slide_index": 0, "shape_index": 1, "effect": "fly", "direction": "left"},
               {"op": "set_transition", "slide_index": 0, "transition": "cube"}])
    text = pptx_read.read_deck(str(f))
    assert "--- slide_index 0 (layout 6 'Blank'), transition cube, 2 animations ---" in text
    assert "--- slide_index 1 (layout 6 'Blank') ---" in text
