"""format_chart: a chart's title, legend, data labels, axes, gaps and fonts,
read back through python-pptx, and each key refused where the chart type
does not take it.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx import Presentation
from pptx.enum.chart import XL_LABEL_POSITION, XL_LEGEND_POSITION

import powerpoint as ppt_mod


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


def _deck(path, chart_type="column_clustered"):
    return _write(path, [{"op": "add_slide", "layout": "blank"},
                         {"op": "add_chart", "slide_index": 0, "chart_type": chart_type, "categories": ["Q1", "Q2"],
                          "series": [{"name": "Revenue", "values": [120, 160]}]}])


def _chart(path):
    return next(s for s in Presentation(str(path)).slides[0].shapes if s.has_chart).chart


def test_a_column_chart_formatted_whole(tmp_path):
    f = tmp_path / "c.pptx"
    _deck(f)
    out = _write(f, [{"op": "format_chart", "slide_index": 0, "title": "Revenue ($k)", "title_size": 18,
                      "legend": {"position": "bottom", "font_size": 12},
                      "data_labels": {"show": ["value"], "number_format": "#,##0", "position": "outside_end",
                                      "font_size": 11, "color": "#595959"},
                      "value_axis": {"min": 0, "max": 200, "major_unit": 50, "number_format": "#,##0",
                                     "gridlines": False, "title": "$k"},
                      "category_axis": {"title": "Quarter"}, "gap_width": 60, "overlap": 0,
                      "font_name": "Calibri", "font_size": 12}], create_new=False)
    assert out.startswith("Presentation saved: ") and "pptx.package" not in out, out
    chart = _chart(f)
    assert chart.chart_title.text_frame.text == "Revenue ($k)"
    assert chart.legend.position == XL_LEGEND_POSITION.BOTTOM
    labels = chart.plots[0].data_labels
    assert labels.show_value and labels.number_format == "#,##0" and labels.position == XL_LABEL_POSITION.OUTSIDE_END
    axis = chart.value_axis
    assert (axis.minimum_scale, axis.maximum_scale, axis.major_unit) == (0, 200, 50)
    assert not axis.has_major_gridlines and axis.axis_title.text_frame.text == "$k"
    assert chart.category_axis.axis_title.text_frame.text == "Quarter" and chart.plots[0].gap_width == 60
    assert chart.font.name == "Calibri"


def test_labels_and_legend_go_away(tmp_path):
    f = tmp_path / "c.pptx"
    _deck(f, "pie")
    _write(f, [{"op": "format_chart", "slide_index": 0, "data_labels": {"show": ["percent", "category"],
                                                                         "position": "best_fit"}}],
           create_new=False)
    labels = _chart(f).plots[0].data_labels
    assert labels.show_percentage and labels.show_category_name and not labels.show_value
    _write(f, [{"op": "format_chart", "slide_index": 0, "legend": False, "data_labels": False}], create_new=False)
    chart = _chart(f)
    assert not chart.has_legend and not chart.plots[0].has_data_labels


@pytest.mark.parametrize(("chart_type", "keys", "message"), [
    ("pie", {"value_axis": {"max": 10}}, "this chart has no value axis"),
    ("line", {"gap_width": 50}, "gap_width is for bar and column charts"),
    ("column_clustered", {"smooth": True}, "smooth is for line charts"),
    ("column_stacked", {"data_labels": {"position": "outside_end"}}, "It takes center, inside_end, inside_base"),
    ("doughnut", {"data_labels": {"position": "center"}}, "Leave position out for this chart"),
    ("column_clustered", {"legend": {"position": "middle"}}, "unknown legend position 'middle'"),
    ("column_clustered", {"chart_index": 3}, "chart_index 3 does not exist"),
    ("column_clustered", {"value_axis": {"min": 10, "max": 5}}, "value_axis min 10 is not below its max 5"),
    ("column_clustered", {"value_axis": {"min": 500}}, "value_axis min 500 is not below its max 200"),
    ("column_clustered", {"value_axis": {"major_unit": 0}}, "value_axis major_unit 0 is out of range"),
    ("column_clustered", {"gap_width": 600}, "gap_width 600 is out of range"),
    ("bar_clustered", {"overlap": 150}, "overlap 150 is out of range"),
    ("column_clustered", {"title": "Sales", "title_size": 900}, "title_size 900 is out of range"),
])
def test_a_key_the_chart_does_not_take_is_refused(tmp_path, chart_type, keys, message):
    f = tmp_path / "c.pptx"
    _deck(f, chart_type)
    if "min" in keys.get("value_axis", {}) and "max" not in keys["value_axis"]:
        _write(f, [{"op": "format_chart", "slide_index": 0, "value_axis": {"max": 200}}], create_new=False)
    out = _write(f, [{"op": "format_chart", "slide_index": 0, **keys}], create_new=False)
    assert out.startswith("Nothing saved: ") and message in out, out
    assert "object has no attribute" not in out and "inclusive" not in out, out


def test_data_labels_true_shows_the_values(tmp_path):
    f = tmp_path / "c.pptx"
    _deck(f)
    out = _write(f, [{"op": "format_chart", "slide_index": 0, "data_labels": True, "legend": True}],
                 create_new=False)
    assert out.startswith("Presentation saved: "), out
    chart = _chart(f)
    assert chart.plots[0].has_data_labels and chart.plots[0].data_labels.show_value and chart.has_legend


@pytest.mark.parametrize("show, value_shown", [(True, True), (False, False)])
def test_data_labels_show_true_or_false_turns_the_value_labels_on_or_off(tmp_path, show, value_shown):
    f = tmp_path / "c.pptx"
    _deck(f)
    out = _write(f, [{"op": "format_chart", "slide_index": 0, "data_labels": {"show": show, "font_size": 9}}],
                 create_new=False)
    assert out.startswith("Presentation saved: "), out
    plot = _chart(f).plots[0]
    assert plot.has_data_labels is value_shown
    if value_shown:
        assert plot.data_labels.show_value and not plot.data_labels.show_percentage
        assert plot.data_labels.font.size.pt == 9


def test_a_scatter_charts_x_axis_takes_a_number_format(tmp_path):
    f = tmp_path / "c.pptx"
    _write(f, [{"op": "add_slide", "layout": "blank"},
               {"op": "add_chart", "slide_index": 0, "chart_type": "scatter",
                "series": [{"name": "Fit", "x": [1000, 2000], "y": [3, 5]}]}])
    out = _write(f, [{"op": "format_chart", "slide_index": 0, "category_axis": {"number_format": "#,##0"}}],
                 create_new=False)
    assert out.startswith("Presentation saved: "), out
    assert _chart(f).category_axis.tick_labels.number_format == "#,##0"


def test_a_line_chart_smooths(tmp_path):
    f = tmp_path / "c.pptx"
    _deck(f, "line")
    out = _write(f, [{"op": "format_chart", "slide_index": 0, "smooth": True,
                      "data_labels": {"position": "above"}}], create_new=False)
    assert out.startswith("Presentation saved: "), out
    assert all(s.smooth for s in _chart(f).plots[0].series)
