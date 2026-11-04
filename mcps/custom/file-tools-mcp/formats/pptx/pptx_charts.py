"""write_pptx's chart operations."""

from typing import Any

from catalog import KeySpec, OpSpec
from formats.pptx.pptx_opkit import (
    G_CHARTS,
    PptxJob,
    box_keys,
    choice,
    emu,
    fill_solid,
    from_end,
    name_of,
    plural,
    set_color,
    slide_key,
    slide_of,
)
from results import Code, OpCtx, OpError

_CHART_NAMES = (
    ("column_clustered", "COLUMN_CLUSTERED"), ("column_stacked", "COLUMN_STACKED"),
    ("bar_clustered", "BAR_CLUSTERED"), ("bar_stacked", "BAR_STACKED"), ("line", "LINE"),
    ("line_markers", "LINE_MARKERS"), ("pie", "PIE"), ("pie_exploded", "PIE_EXPLODED"),
    ("doughnut", "DOUGHNUT"), ("scatter", "XY_SCATTER"), ("scatter_lines", "XY_SCATTER_LINES"),
    ("area", "AREA"), ("area_stacked", "AREA_STACKED"), ("radar", "RADAR"),
)
CHART_TYPES = tuple(name for name, _ in _CHART_NAMES)
_SLICED = frozenset({"pie", "pie_exploded", "doughnut"})
_LINED = ("line", "scatter", "radar")


def _op_add_chart(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.chart.data import CategoryChartData, XyChartData
    from pptx.enum.chart import XL_CHART_TYPE

    slide, _ = slide_of(job, op)
    kind = choice(op.get("chart_type", "column_clustered"), CHART_TYPES, "chart_type")
    series_list = op.get("series", [])
    if kind.startswith("scatter"):
        chart_data = XyChartData()
        for s in series_list:
            xy_series = chart_data.add_series(s.get("name", "Series"))
            for xi, yi in zip(s.get("x", s.get("values", [])), s.get("y", []), strict=False):
                xy_series.add_data_point(xi, yi)
    else:
        chart_data = CategoryChartData()
        chart_data.categories = op.get("categories", [])
        for s in series_list:
            chart_data.add_series(s.get("name", "Series"), s.get("values", []))
    frame = slide.shapes.add_chart(getattr(XL_CHART_TYPE, dict(_CHART_NAMES)[kind]), emu(op, "left", 1),
                                   emu(op, "top", 2), emu(op, "width", 8), emu(op, "height", 4.5), chart_data)
    chart = frame.chart
    if op.get("title"):
        chart.has_title = True
        chart.chart_title.text_frame.text = op["title"]
    if op.get("has_legend") is not None:
        chart.has_legend = op["has_legend"]
    _color_chart(chart, kind in _SLICED, kind.startswith(_LINED), op, ctx)


def _color_chart(chart: Any, sliced: bool, lined: bool, op: dict, ctx: OpCtx) -> None:
    """The op's colours on a chart: per series, per slice (``colors``) for
    pie and doughnut, or per bar (a series' ``colors``). Without them, decks
    keep Office's default blue amid custom-styled slides."""
    series_list = op.get("series", [])
    if sliced:
        slice_colors = op.get("colors") or (series_list[0].get("colors") if series_list else None) or []
        if chart.series:
            for point, col in zip(chart.series[0].points, slice_colors, strict=False):
                fill_solid(point.format.fill, col, ctx)
        return
    for s_def, s_obj in zip(series_list, chart.series, strict=False):
        name = name_of(s_def.get("name", "Series"))
        if s_def.get("color") is not None:
            if lined:
                set_color(s_obj.format.line.color, s_def["color"], ctx)
                try:
                    fill_solid(s_obj.marker.format.fill, s_def["color"], ctx)
                except (AttributeError, ValueError):
                    ctx.warn(f"the markers of {name} keep their colour")
            else:
                fill_solid(s_obj.format.fill, s_def["color"], ctx)
        if s_def.get("colors"):
            if lined:
                ctx.warn(f"{name} takes one color: colors colours bars and slices")
                continue
            for point, col in zip(s_obj.points, s_def["colors"], strict=False):
                fill_solid(point.format.fill, col, ctx)


def _op_update_chart_data(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.chart.data import CategoryChartData

    slide, position = slide_of(job, op)
    ci = op.get("chart_index", 0)
    charts = [s for s in slide.shapes if s.has_chart]
    if not 0 <= from_end(ci, len(charts)) < len(charts):
        raise OpError(Code.NOT_FOUND, f"chart_index {ci} does not exist (the slide has "
                      f"{plural(len(charts), 'chart')})", location=f"slide_index {position}, chart_index {ci}")
    ci = from_end(ci, len(charts))
    chart_data = CategoryChartData()
    chart_data.categories = op.get("categories", [])
    for s in op.get("series", []):
        chart_data.add_series(s.get("name", "Series"), s.get("values", []))
    chart = charts[ci].chart
    try:
        chart.replace_data(chart_data)
    except (KeyError, ValueError, IndexError) as exc:
        raise OpError(Code.UNSUPPORTED, f"this chart's data cannot be replaced this way: {exc}",
                      hint="update_chart_data takes category charts; remove an XY chart and add it again.") from None
    try:
        kind = chart.chart_type.name
    except (KeyError, ValueError, AttributeError, NotImplementedError):
        kind = ""
    _color_chart(chart, kind.startswith(("PIE", "DOUGHNUT")), kind.startswith(("LINE", "XY_", "RADAR")), op, ctx)


# Where data labels may sit, by chart family: PowerPoint repairs a file whose
# label position the chart type does not take.
_LABEL_POSITIONS = {
    "clustered": ("outside_end", "inside_end", "center", "inside_base"),
    "stacked": ("center", "inside_end", "inside_base"),
    "line": ("center", "left", "right", "above", "below"),
    "pie": ("center", "inside_end", "outside_end", "best_fit"),
    "none": (),
}
_LEGEND_POSITIONS = ("right", "left", "top", "bottom", "corner")
_LABEL_SHOWS = ("value", "percent", "category", "series")


def _family(kind: str) -> str:
    if kind.startswith(("DOUGHNUT", "AREA", "RADAR")):
        return "none"
    if kind.startswith("PIE"):
        return "pie"
    if kind.startswith(("LINE", "XY_")):
        return "line"
    if "STACKED" in kind:
        return "stacked"
    return "clustered"


def _chart_on(slide: Any, position: int, ci: int) -> Any:
    charts = [s for s in slide.shapes if s.has_chart]
    if not 0 <= from_end(ci, len(charts)) < len(charts):
        raise OpError(Code.NOT_FOUND, f"chart_index {ci} does not exist (the slide has "
                      f"{plural(len(charts), 'chart')})", location=f"slide_index {position}, chart_index {ci}",
                      hint="Add a chart first with add_chart." if not charts else
                      f"Use 0 to {len(charts) - 1}, or -1 for the last.")
    return charts[from_end(ci, len(charts))].chart


def _font(font: Any, size: Any, ctx: OpCtx, color: Any = None, name: Any = None, what: str = "font_size") -> None:
    from pptx.util import Pt

    if size is not None:
        if not 1 <= size <= 400:
            raise OpError(Code.BAD_VALUE, f"{what} {size} is out of range", hint="Use 1 to 400 pt.")
        font.size = Pt(size)
    if color is not None:
        set_color(font.color, color, ctx)
    if name:
        font.name = name


def _axis(chart: Any, which: str, spec: dict, ctx: OpCtx) -> None:
    try:
        axis = chart.value_axis if which == "value_axis" else chart.category_axis
    except ValueError:
        raise OpError(Code.BAD_VALUE, f"this chart has no {which.replace('_', ' ')}",
                      hint="Pie and doughnut charts have no axes: leave the axis keys out.") from None
    if which == "value_axis":
        low = spec["min"] if spec.get("min") is not None else axis.minimum_scale
        high = spec["max"] if spec.get("max") is not None else axis.maximum_scale
        if low is not None and high is not None and low >= high:
            raise OpError(Code.BAD_VALUE, f"value_axis min {low:g} is not below its max {high:g}",
                          hint="Give a min below the max (or both).")
        if spec.get("major_unit") is not None and spec["major_unit"] <= 0:
            raise OpError(Code.BAD_VALUE, f"value_axis major_unit {spec['major_unit']:g} is out of range",
                          hint="Give a step above 0, in the axis's own units.")
        for key, attr in (("min", "minimum_scale"), ("max", "maximum_scale"), ("major_unit", "major_unit")):
            if spec.get(key) is not None:
                setattr(axis, attr, spec[key])
    if spec.get("number_format"):
        axis.tick_labels.number_format = spec["number_format"]
        axis.tick_labels.number_format_is_linked = False
    if spec.get("title") is not None:
        axis.has_title = bool(spec["title"])
        if spec["title"]:
            axis.axis_title.text_frame.text = spec["title"]
    if spec.get("gridlines") is not None:
        axis.has_major_gridlines = bool(spec["gridlines"])
    if spec.get("visible") is not None:
        axis.visible = bool(spec["visible"])
    _font(axis.tick_labels.font, spec.get("font_size"), ctx)


def _labels(chart: Any, kind: str, spec: Any, ctx: OpCtx) -> None:
    from pptx.enum.chart import XL_LABEL_POSITION

    plot = chart.plots[0]
    if spec is False:
        plot.has_data_labels = False
        return
    if spec is True:
        spec = {}
    if spec.get("show") is False:
        plot.has_data_labels = False
        return
    plot.has_data_labels = True
    labels = plot.data_labels
    shows = spec.get("show")
    shows = ["value"] if shows is True or not shows else shows
    shows = [shows] if isinstance(shows, str) else shows
    unknown = [s for s in shows if s not in _LABEL_SHOWS]
    if unknown:
        raise OpError(Code.BAD_VALUE, f"data_labels show {', '.join(map(str, unknown))} is not a label part",
                      hint=f"Use {', '.join(_LABEL_SHOWS)}.")
    labels.show_value = "value" in shows
    labels.show_percentage = "percent" in shows
    labels.show_category_name = "category" in shows
    labels.show_series_name = "series" in shows
    if spec.get("number_format"):
        labels.number_format = spec["number_format"]
        labels.number_format_is_linked = False
    if spec.get("position"):
        allowed = _LABEL_POSITIONS[_family(kind)]
        word = str(spec["position"]).strip().casefold()
        if word not in allowed:
            raise OpError(Code.BAD_VALUE, f"this chart's labels take no position '{spec['position']}'",
                          hint=f"It takes {', '.join(allowed)}." if allowed else "Leave position out for this chart.")
        labels.position = getattr(XL_LABEL_POSITION, word.upper())
    _font(labels.font, spec.get("font_size"), ctx, spec.get("color"))


def _op_format_chart(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.enum.chart import XL_LEGEND_POSITION

    slide, position = slide_of(job, op)
    chart = _chart_on(slide, position, op.get("chart_index", 0))
    try:
        kind = chart.chart_type.name
    except (KeyError, ValueError, AttributeError, NotImplementedError):
        kind = ""
    if op.get("title") is not None:
        chart.has_title = bool(op["title"])
        if op["title"]:
            chart.chart_title.text_frame.text = op["title"]
    if op.get("title_size") is not None and chart.has_title:
        for paragraph in chart.chart_title.text_frame.paragraphs:
            for run in paragraph.runs:
                _font(run.font, op["title_size"], ctx, what="title_size")
    legend = op.get("legend")
    if legend is not None:
        chart.has_legend = legend is not False
        if isinstance(legend, dict):
            if legend.get("position"):
                word = str(legend["position"]).strip().casefold()
                if word not in _LEGEND_POSITIONS:
                    raise OpError(Code.BAD_VALUE, f"unknown legend position '{legend['position']}'",
                                  hint=f"Use {', '.join(_LEGEND_POSITIONS)}.")
                chart.legend.position = getattr(XL_LEGEND_POSITION, word.upper())
                chart.legend.include_in_layout = False
            _font(chart.legend.font, legend.get("font_size"), ctx)
    if op.get("data_labels") is not None:
        _labels(chart, kind, op["data_labels"], ctx)
    for which in ("value_axis", "category_axis"):
        if op.get(which) is not None:
            _axis(chart, which, op[which], ctx)
    plot = chart.plots[0]
    for key, low, high in (("gap_width", 0, 500), ("overlap", -100, 100)):
        if op.get(key) is not None:
            if not hasattr(plot, key):
                raise OpError(Code.BAD_VALUE, f"{key} is for bar and column charts",
                              hint=f"Leave {key} out for this chart.")
            if not low <= op[key] <= high:
                raise OpError(Code.BAD_VALUE, f"{key} {op[key]} is out of range", hint=f"Use {low} to {high} (%).")
            setattr(plot, key, op[key])
    if op.get("smooth") is not None:
        if _family(kind) != "line":
            raise OpError(Code.BAD_VALUE, "smooth is for line charts", hint="Leave smooth out for this chart.")
        for series in plot.series:
            series.smooth = bool(op["smooth"])
    _font(chart.font, op.get("font_size"), ctx, name=op.get("font_name"))


_AXIS_FIELDS = (KeySpec("title", ("str",)), KeySpec("gridlines", ("bool",)), KeySpec("visible", ("bool",)),
                KeySpec("font_size", ("float",), help="pt"))
FORMAT_KEYS = (
    KeySpec("title", ("str",), help="the chart's title ('' removes it)"),
    KeySpec("title_size", ("float",), help="pt"),
    KeySpec("legend", ("bool", "dict"), fields=(
        KeySpec("position", ("str",), help=" | ".join(_LEGEND_POSITIONS)), KeySpec("font_size", ("float",)),
    ), help="false hides it; {position, font_size}"),
    KeySpec("data_labels", ("bool", "dict"), fields=(
        KeySpec("show", ("bool", "str", "list"),
                help=" | ".join(_LABEL_SHOWS) + " (a list for several); true is value, false hides them"),
        KeySpec("number_format", ("str",), help="Excel's: '0.0%', '#,##0'"),
        KeySpec("position", ("str",), help="outside_end, inside_end, center, inside_base (bars); above, below, "
                                          "left, right (lines); best_fit (pie)"),
        KeySpec("font_size", ("float",)), KeySpec("color", ("color",)),
    ), help="false hides them; {show, number_format, position, font_size, color}"),
    KeySpec("value_axis", ("dict",), fields=(
        KeySpec("min", ("float",)), KeySpec("max", ("float",)), KeySpec("major_unit", ("float",)),
        KeySpec("number_format", ("str",)), *_AXIS_FIELDS), help="not on pie or doughnut charts"),
    KeySpec("category_axis", ("dict",), fields=(KeySpec("number_format", ("str",), help="dates and scatter x"),
                                                *_AXIS_FIELDS)),
    KeySpec("gap_width", ("int",), help="0-500 %, bars and columns"),
    KeySpec("overlap", ("int",), help="-100 to 100 %, bars and columns"),
    KeySpec("smooth", ("bool",), help="line charts"),
    KeySpec("font_name", ("str",)), KeySpec("font_size", ("float",), help="pt, the whole chart's text"),
)


OPS: tuple[OpSpec, ...] = (
    OpSpec("add_chart", _op_add_chart, (
        slide_key(),
        KeySpec("chart_type", ("str",), default="column_clustered", help=", ".join(CHART_TYPES)),
        KeySpec("categories", ("list",), default=[]),
        KeySpec("series", ("list",), required=True, item=(
            KeySpec("name", ("str",), default="Series"), KeySpec("values", ("list",)),
            KeySpec("color", ("color",)), KeySpec("x", ("list",)), KeySpec("y", ("list",)),
            KeySpec("colors", ("list",), of=("color",), help="one per bar or slice"))),
        KeySpec("colors", ("list",), of=("color",), help="one per slice, pie and doughnut"),
        KeySpec("title", ("str",)), KeySpec("has_legend", ("bool",)), *box_keys(1, 2, 8, 4.5),
    ), G_CHARTS, note="scatter series take x and y", examples=(
        {"op": "add_chart", "slide_index": 0, "chart_type": "column_clustered", "categories": ["Q1", "Q2"],
         "series": [{"name": "Revenue", "values": [12, 15], "color": "#1F4E79"}], "title": "Revenue", "has_legend": False},
        {"op": "add_chart", "slide_index": 0, "chart_type": "doughnut", "categories": ["A", "B"],
         "series": [{"name": "Share", "values": [60, 40]}], "colors": ["#1F4E79", "#9DC3E6"]},
        {"op": "add_chart", "slide_index": 0, "chart_type": "scatter",
         "series": [{"name": "Fit", "x": [1, 2], "y": [3, 5]}]})),
    OpSpec("update_chart_data", _op_update_chart_data, (
        slide_key(), KeySpec("chart_index", ("int",), default=0, help="0-based, -1 = last"),
        KeySpec("categories", ("list",), default=[]),
        KeySpec("series", ("list",), required=True, item=(
            KeySpec("name", ("str",), default="Series"), KeySpec("values", ("list",)),
            KeySpec("color", ("color",)), KeySpec("colors", ("list",), of=("color",), help="one per bar or slice"))),
        KeySpec("colors", ("list",), of=("color",), help="one per slice, pie and doughnut"),
    ), G_CHARTS, note="category charts; colours as add_chart takes them", setup=(
        {"op": "add_chart", "slide_index": 0, "categories": ["Q1"], "series": [{"name": "R", "values": [1]}]},),
        examples=({"op": "update_chart_data", "slide_index": 0, "categories": ["Q1", "Q2"],
                   "series": [{"name": "Revenue", "values": [12, 15], "color": "#1F4E79"}]},
                  {"op": "update_chart_data", "slide_index": 0, "chart_index": -1, "categories": ["A", "B"],
                   "series": [{"name": "Share", "values": [60, 40], "colors": ["#1F4E79", "#9DC3E6"]}],
                   "colors": ["#1F4E79", "#9DC3E6"]})),
    OpSpec("format_chart", _op_format_chart, (
        slide_key(), KeySpec("chart_index", ("int",), default=0, help="0-based, -1 = last"), *FORMAT_KEYS,
    ), G_CHARTS, note="axes, labels, legend and fonts of a chart already on the slide",
        setup=({"op": "add_chart", "slide_index": 0, "categories": ["Q1", "Q2"],
                "series": [{"name": "Revenue", "values": [120, 160]}]},),
        examples=({"op": "format_chart", "slide_index": 0, "chart_index": -1, "title": "Revenue ($k)",
                   "title_size": 18, "legend": {"position": "bottom", "font_size": 12},
                   "data_labels": {"show": "value", "number_format": "#,##0", "position": "outside_end",
                                   "font_size": 11, "color": "#595959"},
                   "value_axis": {"min": 0, "max": 200, "major_unit": 50, "number_format": "#,##0",
                                  "gridlines": False, "title": "$k", "visible": True, "font_size": 11},
                   "category_axis": {"title": "Quarter", "gridlines": False, "visible": True, "font_size": 11},
                   "gap_width": 80, "overlap": 0, "font_name": "Calibri", "font_size": 12},
                  {"op": "format_chart", "slide_index": 0, "legend": False, "data_labels": False})),
)
