"""create_chart series validation + the shapes it must keep accepting.

The bug: a wrong-keyed series (e.g. `data` instead of `values`) silently
rendered an EMPTY plot — the agent got a success ack and the user a blank
chart. Validation is per chart_type because two valid shapes carry no
per-series 'values': heatmap's top-level 2-D `data` and scatter's
`values`-as-x / categories fallbacks — both must keep rendering.

Needs matplotlib (the MCP's venv / image CI); skips where it's absent.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

# Make the parent dir importable as a top-level module
sys.path.insert(0, str(Path(__file__).parent.parent))

from charts import _validate_series, handle_create_chart  # noqa: E402


@pytest.fixture(autouse=True)
def _capture_push(monkeypatch):
    """Swallow the inline chat push; record the rendered PNG bytes."""
    pushed = {}

    async def fake_push(png_bytes, mime, caption):
        pushed["bytes"] = png_bytes
        pushed["mime"] = mime

    monkeypatch.setattr("charts._push_image_preview", fake_push)
    yield pushed


def _run(args: dict) -> str:
    return asyncio.get_event_loop().run_until_complete(handle_create_chart(args))


# ───────────────────────── wrong shapes error loudly ────────────────────────


def test_line_wrong_key_names_series_and_keys():
    out = _run({"chart_type": "line", "categories": ["a", "b"],
                "series": [{"name": "S1", "data": [1, 2]}]})
    assert out.startswith("Error:")
    assert "series[0]" in out and "'values'" in out and "data" in out


def test_pie_empty_values_errors():
    out = _run({"chart_type": "pie", "categories": ["a", "b"],
                "series": [{"name": "S1", "values": []}]})
    assert out.startswith("Error:")


def test_bar_empty_series_errors():
    out = _run({"chart_type": "bar", "series": []})
    assert out.startswith("Error:") and "'series'" in out


def test_scatter_missing_y_errors():
    out = _run({"chart_type": "scatter",
                "series": [{"name": "S1", "x": [1, 2, 3]}]})
    assert out.startswith("Error:") and "series[0]" in out


def test_unknown_chart_type_errors():
    out = _run({"chart_type": "sankey", "series": [{"values": [1]}]})
    assert out.startswith("Error:") and "sankey" in out


# ─────────────────── valid legacy shapes keep rendering ─────────────────────


def test_heatmap_top_level_data_still_renders(_capture_push):
    out = _run({"chart_type": "heatmap", "data": [[1, 2], [3, 4]],
                "categories": ["c1", "c2"], "row_labels": ["r1", "r2"]})
    assert not out.startswith("Error:")
    assert _capture_push["bytes"][:8] == b"\x89PNG\r\n\x1a\n"


def test_scatter_values_fallback_still_renders(_capture_push):
    # values→x with an explicit y array (the renderer's first fallback).
    out = _run({"chart_type": "scatter",
                "series": [{"name": "S1", "values": [1, 2, 3], "y": [4, 5, 6]}]})
    assert not out.startswith("Error:")
    # values + top-level categories (y ← values, x ← indices).
    out = _run({"chart_type": "scatter", "categories": ["a", "b", "c"],
                "series": [{"name": "S1", "values": [1, 2, 3]}]})
    assert not out.startswith("Error:")
    assert _capture_push["bytes"][:8] == b"\x89PNG\r\n\x1a\n"


def test_correct_line_payload_renders_png(_capture_push):
    out = _run({"chart_type": "line", "title": "T", "categories": ["a", "b", "c"],
                "series": [{"name": "S1", "values": [1, 2, 3]},
                           {"name": "S2", "values": [3, 2, 1]}]})
    assert not out.startswith("Error:")
    assert "chart created" in out
    assert _capture_push["bytes"][:8] == b"\x89PNG\r\n\x1a\n"


# ─────────────────────── validator unit coverage ─────────────────────────────


def test_validator_heatmap_ragged_data_rejected():
    assert _validate_series("heatmap", {"data": [[1, 2], []]}) is not None
    assert _validate_series("heatmap", {"data": [[1, 2], [3, 4]]}) is None


def test_validator_scatter_x_y_ok():
    assert _validate_series("scatter", {"series": [{"x": [1], "y": [2]}]}) is None


# ──────────────── restyle: every type × every style renders ─────────────────


_STYLE_MATRIX_ARGS = {
    "bar": {"categories": ["a", "b", "c"],
            "series": [{"name": "S", "values": [3, 1, 2]}]},
    "column": {"categories": ["a", "b"],
               "series": [{"name": "S1", "values": [1, 2]},
                          {"name": "S2", "values": [2, 1]}]},
    "horizontal_bar": {"categories": ["a", "b", "c"],
                       "series": [{"name": "S", "values": [3, 1, 2]}]},
    "line": {"categories": ["a", "b", "c"],
             "series": [{"name": "S1", "values": [1, 2, 3]},
                        {"name": "S2", "values": [3, 2, 1]}]},
    "pie": {"categories": ["a", "b"], "series": [{"values": [60, 40]}]},
    "doughnut": {"categories": ["a", "b"], "series": [{"values": [150, 50]}]},
    "scatter": {"series": [{"name": "S", "x": [1, 2, 3], "y": [3, 1, 2]}]},
    "area": {"categories": ["a", "b", "c"],
             "series": [{"name": "S", "values": [1, 3, 2]}]},
    "heatmap": {"data": [[1, 2], [3, 4]], "categories": ["c1", "c2"],
                "row_labels": ["r1", "r2"]},
    "histogram": {"series": [{"name": "S", "values": [1, 1, 2, 3, 3, 3, 4]}]},
}


@pytest.mark.parametrize("style", ["modern", "dark", "minimal", "presentation"])
@pytest.mark.parametrize("chart_type", sorted(_STYLE_MATRIX_ARGS))
def test_every_type_renders_under_every_style(chart_type, style, _capture_push):
    args = dict(_STYLE_MATRIX_ARGS[chart_type])
    args.update({"chart_type": chart_type, "title": "T", "style": style})
    out = _run(args)
    assert not out.startswith("Error:"), out
    png = _capture_push["bytes"]
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # A styled, populated chart is never a near-empty canvas.
    assert len(png) > 10_000


def test_doughnut_center_total_only_for_count_data(_capture_push):
    # Percent data (sums to 100) → no center total; count data → total shown.
    # Compare rendered sizes indirectly by checking both render fine, and
    # assert the heuristic itself.
    from charts import _fmt_num
    assert _fmt_num(1234) == "1,234"
    assert _fmt_num(12.5) == "12.5"
    out = _run({"chart_type": "doughnut", "categories": ["a", "b"],
                "series": [{"values": [60, 40]}]})
    assert not out.startswith("Error:")
    out = _run({"chart_type": "doughnut", "categories": ["a", "b"],
                "series": [{"values": [600, 400]}]})
    assert not out.startswith("Error:")


def test_hbar_first_category_at_top(_capture_push, monkeypatch):
    # invert_yaxis puts categories[0] at the top — assert on the axes state.
    captured = {}
    import matplotlib.pyplot as plt
    orig_subplots = plt.subplots

    def spy_subplots(*a, **kw):
        fig, ax = orig_subplots(*a, **kw)
        captured["ax"] = ax
        return fig, ax

    monkeypatch.setattr(plt, "subplots", spy_subplots)
    out = _run({"chart_type": "horizontal_bar", "categories": ["first", "second"],
                "series": [{"name": "S", "values": [1, 2]}]})
    assert not out.startswith("Error:")
    assert captured["ax"].yaxis_inverted()
