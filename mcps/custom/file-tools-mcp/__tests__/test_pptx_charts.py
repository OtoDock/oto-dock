"""write_pptx add_chart color styling — series colors + pie slice colors.

Needs python-pptx; skips where it's absent (the MCP's venv / image CI).
"""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def test_add_chart_series_color_applied(tmp_path):
    f = tmp_path / "deck.pptx"
    out = _write(f, [
        {"type": "add_slide", "layout": "blank"},
        {"type": "add_chart", "slide": 0, "chart_type": "column_clustered",
         "categories": ["Q1", "Q2"],
         "series": [{"name": "Rev", "values": [1, 2], "color": "#16BAC5"}]},
    ])
    assert _clean(out), out

    from pptx import Presentation
    prs = Presentation(str(f))
    chart = next(s for s in prs.slides[0].shapes if s.has_chart).chart
    ser = list(chart.series)[0]
    assert str(ser.format.fill.fore_color.rgb) == "16BAC5"


def test_add_chart_pie_slice_colors_applied(tmp_path):
    f = tmp_path / "pie.pptx"
    out = _write(f, [
        {"type": "add_slide", "layout": "blank"},
        {"type": "add_chart", "slide": 0, "chart_type": "doughnut",
         "categories": ["a", "b", "c"],
         "series": [{"name": "S", "values": [5, 3, 2]}],
         "colors": ["#111111", "#222222", "#333333"]},
    ])
    assert _clean(out), out

    from pptx import Presentation
    prs = Presentation(str(f))
    chart = next(s for s in prs.slides[0].shapes if s.has_chart).chart
    points = list(list(chart.series)[0].points)
    got = [str(p.format.fill.fore_color.rgb) for p in points]
    assert got == ["111111", "222222", "333333"]

def test_a_column_series_colours_each_bar(tmp_path):
    f = tmp_path / "bars.pptx"
    out = _write(f, [
        {"op": "add_slide", "layout": "blank"},
        {"op": "add_chart", "slide_index": 0, "chart_type": "column_clustered", "categories": ["Lease", "Buy"],
         "series": [{"name": "Days", "values": [240, 70], "colors": ["#1F4E79", "#A6A6A6"]}]},
        {"op": "add_chart", "slide_index": 0, "chart_type": "line", "categories": ["Q1", "Q2"], "top": 4, "height": 3,
         "series": [{"name": "Margin", "values": [30, 37], "colors": ["#1F4E79", "#A6A6A6"]}]},
    ])
    assert "'Margin' takes one color" in out, out

    from pptx import Presentation
    prs = Presentation(str(f))
    chart = next(s for s in prs.slides[0].shapes if s.has_chart).chart
    got = [str(p.format.fill.fore_color.rgb) for p in list(chart.series)[0].points]
    assert got == ["1F4E79", "A6A6A6"]


def _transition(path, slide=0):
    from pptx import Presentation
    from pptx.oxml.ns import qn
    prs = Presentation(str(path))
    return prs.slides[slide]._element.find(qn("p:transition"))


def test_a_transition_duration_is_its_speed_not_an_auto_advance(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [
        {"type": "add_slide", "layout": "blank"},
        {"type": "add_slide", "layout": "blank"},
        {"type": "set_transition", "slide": 0, "transition": "fade", "duration": 400},
        {"type": "set_transition", "slide": 1, "transition": "push", "advance_after": 3000},
    ])
    assert _clean(out), out
    first, second = _transition(f, 0), _transition(f, 1)
    assert first.get("spd") == "fast" and first.get("advTm") is None
    assert second.get("advTm") == "3000" and second.get("advClick") == "0"


def test_move_slide_takes_the_last_slide_as_minus_one(tmp_path):
    f = tmp_path / "m.pptx"
    ops = [{"type": "add_slide", "layout": "title_only"} for _ in range(3)]
    ops += [{"type": "set_text", "slide": i, "placeholder": 0, "text": f"s{i}"} for i in range(3)]
    ops += [{"type": "move_slide", "from_index": -1, "to_index": 0}]
    out = _write(f, ops)
    assert _clean(out), out
    from pptx import Presentation
    prs = Presentation(str(f))
    assert [s.shapes.title.text for s in prs.slides] == ["s2", "s0", "s1"]


def test_a_move_out_of_range_fails_and_saves_nothing(tmp_path):
    f = tmp_path / "m.pptx"
    _write(f, [{"op": "add_slide", "layout": "title_only"}, {"op": "add_slide"}])
    before = f.read_bytes()
    out = _write(f, [{"op": "move_slide", "from_index": 1, "to_index": 0},
                     {"op": "move_slide", "from_index": 7, "to_index": 0}], create_new=False)
    assert out.startswith("Nothing saved: ") and "index out of range (the deck has 2 slides)" in out
    assert "Use 0 to 1, or -1 for the last slide." in out
    assert f.read_bytes() == before
    out = asyncio.run(ppt_mod.handle_write_pptx({"path": str(f), "best_effort": True, "operations": [
        {"op": "move_slide", "from_index": 1, "to_index": 0},
        {"op": "move_slide", "from_index": 7, "to_index": 0}]}))
    assert out.startswith("Presentation saved: ") and "1 failed" in out
