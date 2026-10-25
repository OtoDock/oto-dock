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
    monkeypatch.setattr(ppt_mod, "_resolve_path", _async_ident)
    monkeypatch.setattr("shared._resolve_path", _async_ident)
    monkeypatch.setattr(ppt_mod, "_to_agents_relative", lambda p: p)

    async def _noop_preview(path):
        pass

    monkeypatch.setattr(ppt_mod, "_push_preview", _noop_preview)


def _write(path: Path, ops: list[dict]) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx(
        {"path": str(path), "create_new": True, "operations": ops}))


def test_add_chart_series_color_applied(tmp_path):
    f = tmp_path / "deck.pptx"
    out = _write(f, [
        {"type": "add_slide", "layout": "blank"},
        {"type": "add_chart", "slide": 0, "chart_type": "column_clustered",
         "categories": ["Q1", "Q2"],
         "series": [{"name": "Rev", "values": [1, 2], "color": "#16BAC5"}]},
    ])
    assert "Warnings/Errors" not in out, out

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
    assert "Warnings/Errors" not in out, out

    from pptx import Presentation
    prs = Presentation(str(f))
    chart = next(s for s in prs.slides[0].shapes if s.has_chart).chart
    points = list(list(chart.series)[0].points)
    got = [str(p.format.fill.fore_color.rgb) for p in points]
    assert got == ["111111", "222222", "333333"]

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
    assert "Warnings/Errors" not in out, out
    first, second = _transition(f, 0), _transition(f, 1)
    assert first.get("spd") == "fast" and first.get("advTm") is None
    assert second.get("advTm") == "3000" and second.get("advClick") == "0"


def test_move_slide_takes_the_last_slide_as_minus_one(tmp_path):
    f = tmp_path / "m.pptx"
    ops = [{"type": "add_slide", "layout": "title_only"} for _ in range(3)]
    ops += [{"type": "set_text", "slide": i, "placeholder": 0, "text": f"s{i}"} for i in range(3)]
    ops += [{"type": "move_slide", "from_index": -1, "to_index": 0},
            {"type": "move_slide", "from_index": 7, "to_index": 0}]
    out = _write(f, ops)
    assert "move_slide: index out of range" in out
    from pptx import Presentation
    prs = Presentation(str(f))
    assert [s.shapes.title.text for s in prs.slides] == ["s2", "s0", "s1"]
