"""set_transition's registry: every transition PowerPoint's gallery has,
written plain or wrapped as PowerPoint writes it, with its options, placed
before the slide's timing whatever wraps either. The package validation
reads a wrapped transition's Fallback only, so these tests check the
Choice's names against the schema's own list.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx import Presentation

import powerpoint as ppt_mod
from formats.pptx import pptx_transitions as tr

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
ORDER_TABLE = Path(__file__).parent.parent / "ooxml" / "order_table.json"


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


def _children(path, slide=0) -> list[str]:
    sld = Presentation(str(path)).slides[slide]._element
    return [etree.QName(c).localname for c in sld]


def _carrier(path, slide=0):
    sld = Presentation(str(path)).slides[slide]._element
    return next(c for c in sld if tr.is_transition(c))


def _schema_children() -> set[str]:
    table = json.loads(ORDER_TABLE.read_text(encoding="utf-8"))
    rows = table["types"]["p:CT_SlideTransition/p:transition"]
    namespaces = table["namespaces"]
    out = set()
    for row in rows:
        prefix, _, local = row[0].partition(":")
        out.add(f"{{{namespaces[prefix]}}}{local}")
    return out


@pytest.mark.parametrize("name", tr.TRANSITIONS)
def test_every_transition_is_written_as_the_schema_knows_it(tmp_path, name):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "set_transition", "slide_index": 0, "transition": name}])
    assert out.startswith("Presentation saved: ") and "Checks: no problems found." in out, out
    prefix, element, _, _ = tr.REGISTRY[name]
    carrier = _carrier(f)
    if prefix == "p":
        assert etree.QName(carrier).localname == "transition"
        effect = carrier[0]
    else:
        choice = carrier.find(f"{MC}Choice")
        assert choice.nsmap[choice.get("Requires")] == tr.NAMESPACES[prefix]
        effect = choice.find(f"{P}transition")[0]
        fallback = carrier.find(f"{MC}Fallback/{P}transition")
        assert fallback is not None and fallback[0].tag == f"{P}fade"
        assert fallback.get("spd") and effect.getparent().get(f"{{{tr.NAMESPACES['p14']}}}dur")
    namespace = tr.P_NS if prefix == "p" else tr.NAMESPACES[prefix]
    assert effect.tag == f"{{{namespace}}}{element}"
    if prefix != "p159":
        assert effect.tag in _schema_children()


def test_the_transitions_it_took_before_are_written_as_they_were(tmp_path):
    f = tmp_path / "t.pptx"
    ops = [{"op": "add_slide"} for _ in range(6)]
    ops += [{"op": "set_transition", "slide_index": n, "transition": name}
            for n, name in enumerate(("fade", "push", "wipe", "split", "cover", "dissolve"))]
    _write(f, ops)
    for n, name in enumerate(("fade", "push", "wipe", "split", "cover", "dissolve")):
        effect = _carrier(f, n)[0]
        assert effect.tag == f"{P}{name}" and dict(effect.attrib) == {}


def test_reveal_is_the_newer_reveal_with_a_fade_for_older_readers(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "set_transition", "slide_index": 0, "transition": "reveal"}])
    carrier = _carrier(f)
    assert etree.QName(carrier).localname == "AlternateContent"
    assert carrier.find(f"{MC}Choice/{P}transition")[0].tag == f"{{{tr.NAMESPACES['p14']}}}reveal"


def test_options_set_the_effects_attributes(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "add_slide"}, {"op": "add_slide"},
                     {"op": "set_transition", "slide_index": 0, "transition": "push", "direction": "up"},
                     {"op": "set_transition", "slide_index": 1, "transition": "split", "orientation": "vertical",
                      "direction": "in"},
                     {"op": "set_transition", "slide_index": 2, "transition": "morph", "morph_by": "word"}])
    assert out.startswith("Presentation saved: "), out
    assert dict(_carrier(f, 0)[0].attrib) == {"dir": "u"}
    assert dict(_carrier(f, 1)[0].attrib) == {"orient": "vert", "dir": "in"}
    morph = _carrier(f, 2).find(f"{MC}Choice/{P}transition")[0]
    assert morph.get("option") == "byWord" and "Morph pairs shapes by name" in out


@pytest.mark.parametrize(("op", "message"), [
    ({"transition": "fade", "direction": "left"}, "transition 'fade' takes no direction"),
    ({"transition": "cube", "direction": "in"}, "transition 'cube' takes direction left, right, up or down"),
    ({"transition": "cubbe"}, "Did you mean 'cube'?"),
])
def test_a_wrong_option_names_what_the_transition_takes(tmp_path, op, message):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "set_transition", "slide_index": 0, **op}])
    assert out.startswith("Nothing saved: ") and message in out, out


def test_aliases_name_the_same_effect(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "set_transition", "slide_index": 0, "transition": "pull"}])
    assert _carrier(f)[0].tag == f"{P}pull"


_TIMING = (f'<p:timing xmlns:p="{tr.P_NS}"><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" '
           'nodeType="tmRoot"/></p:par></p:tnLst></p:timing>')


def _with_timing(path, wrapped: bool) -> None:
    prs = Presentation(str(path))
    sld = prs.slides[0]._element
    timing = _TIMING
    if wrapped:
        timing = (f'<mc:AlternateContent xmlns:mc="{tr.MC_NS}" xmlns:p="{tr.P_NS}" '
                  f'xmlns:p14="{tr.NAMESPACES["p14"]}"><mc:Choice Requires="p14">{_TIMING}</mc:Choice>'
                  f'<mc:Fallback>{_TIMING}</mc:Fallback></mc:AlternateContent>')
    sld.append(etree.fromstring(timing))
    prs.save(str(path))


@pytest.mark.parametrize(("wrapped_timing", "transition"), [
    (False, "cube"), (True, "push"), (True, "cube"), (False, "fade")])
def test_the_transition_goes_before_the_timing_whatever_wraps_either(tmp_path, wrapped_timing, transition):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}])
    _with_timing(f, wrapped_timing)
    out = _write(f, [{"op": "set_transition", "slide_index": 0, "transition": transition}], create_new=False)
    assert "Checks: no problems found." in out or "no new problems" in out, out
    names = _children(f)
    first_transition = next(n for n, c in enumerate(Presentation(str(f)).slides[0]._element) if tr.is_transition(c))
    timing_at = next(n for n, c in enumerate(Presentation(str(f)).slides[0]._element)
                     if etree.QName(c).localname == "timing" or tr._wraps(c, "timing"))
    assert first_transition < timing_at, names


def test_a_wrapped_transition_and_a_plain_one_replace_each_other(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "set_transition", "slide_index": 0, "transition": "vortex"}])
    _write(f, [{"op": "set_transition", "slide_index": 0, "transition": "wipe"}], create_new=False)
    sld = Presentation(str(f)).slides[0]._element
    assert [etree.QName(c).localname for c in sld if tr.is_transition(c)] == ["transition"]
    _write(f, [{"op": "set_transition", "slide_index": 0, "transition": "ripple"}], create_new=False)
    sld = Presentation(str(f)).slides[0]._element
    assert [etree.QName(c).localname for c in sld if tr.is_transition(c)] == ["AlternateContent"]


def test_the_read_names_each_slides_transition(tmp_path):
    f = tmp_path / "t.pptx"
    names = ("fade", "cube", "drape", "morph", "uncover")
    _write(f, [*({"op": "add_slide"} for _ in names),
               *({"op": "set_transition", "slide_index": n, "transition": name} for n, name in enumerate(names))])
    prs = Presentation(str(f))
    assert [tr.read_transition(s._element) for s in prs.slides] == list(names)


def test_an_exciting_transition_says_libreoffice_does_not_keep_it(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "set_transition", "slide_index": 0, "transition": "drape"}])
    assert "LibreOffice and Collabora do not play the drape transition" in out
