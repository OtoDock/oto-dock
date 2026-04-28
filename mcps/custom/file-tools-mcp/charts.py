"""Inline chart rendering for the file-tools MCP.

Creates charts as PNG images using matplotlib and pushes them inline
to the dashboard chat via _push_image_preview. Optionally saves to file.
"""

import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import numpy as np

from shared import _push_image_preview, _resolve_path, _to_agents_relative

# ---------------------------------------------------------------------------
# Default styling
# ---------------------------------------------------------------------------

DEFAULT_COLORS = [
    "#4F46E5", "#F59E0B", "#10B981", "#EF4444",
    "#8B5CF6", "#06B6D4", "#EC4899", "#84CC16",
    "#F97316", "#3B82F6", "#14B8A6", "#A855F7",
]

# Comfortaa = the dashboard brand font (installed in the image via
# fonts-comfortaa); DejaVu fills glyphs Comfortaa lacks and is the full
# fallback wherever the font isn't installed (matplotlib warns, then falls
# back — output stays correct).
_FONTS = ["Comfortaa", "DejaVu Sans"]

# Shared geometry for the light styles: left-aligned bold title, no top/
# right/left spines, tick marks hidden, frameless legend. Grid visibility
# per-axis is decided by the renderers (value axis only for bar/line).
_LIGHT_BASE = {
    "font.family": _FONTS,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.grid": True,
    "grid.color": "#E5E7EB",
    "grid.linewidth": 0.9,
    "grid.linestyle": "-",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "axes.edgecolor": "#D1D5DB",
    "axes.linewidth": 1.0,
    "text.color": "#111827",
    "axes.labelcolor": "#4B5563",
    "xtick.color": "#6B7280",
    "ytick.color": "#6B7280",
    "xtick.major.size": 0,
    "ytick.major.size": 0,
    "font.size": 11,
    "axes.titlesize": 15,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.titlepad": 16,
    "axes.labelsize": 11,
    "legend.frameon": False,
    "legend.labelcolor": "#374151",
    "lines.linewidth": 2.6,
}

STYLES = {
    "modern": dict(_LIGHT_BASE),
    "dark": {
        **_LIGHT_BASE,
        "figure.facecolor": "#0F172A",
        "axes.facecolor": "#0F172A",
        "grid.color": "#334155",
        "axes.edgecolor": "#475569",
        "text.color": "#E2E8F0",
        "axes.labelcolor": "#CBD5E1",
        "xtick.color": "#94A3B8",
        "ytick.color": "#94A3B8",
        "legend.labelcolor": "#CBD5E1",
    },
    "minimal": {
        **_LIGHT_BASE,
        "axes.grid": False,
    },
    "presentation": {
        **_LIGHT_BASE,
        "font.size": 13.5,
        "axes.titlesize": 19,
        "axes.labelsize": 13.5,
        "xtick.labelsize": 12.5,
        "ytick.labelsize": 12.5,
        "lines.linewidth": 3.2,
    },
}


def _get_colors(series: list, n: int) -> list:
    """Get colors for series, using defaults if not specified."""
    colors = []
    for i in range(n):
        if i < len(series) and series[i].get("color"):
            colors.append(series[i]["color"])
        else:
            colors.append(DEFAULT_COLORS[i % len(DEFAULT_COLORS)])
    return colors


def _fmt_num(v) -> str:
    """Compact value label: thousands separators, decimals only when needed."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f.is_integer():
        return f"{f:,.0f}"
    return f"{f:,.1f}"


def _value_grid(ax, value_axis: str):
    """Solid gridlines on the value axis only, drawn under the data."""
    ax.grid(axis="x" if value_axis == "y" else "y", visible=False)
    ax.set_axisbelow(True)


# ---------------------------------------------------------------------------
# Chart renderers
# ---------------------------------------------------------------------------


def _render_bar(ax, args: dict, horizontal: bool = False):
    import matplotlib.pyplot as plt

    cats = args.get("categories", [])
    series = args.get("series", [])
    colors = _get_colors(series, len(series))
    n_series = len(series)
    x = np.arange(len(cats))
    width = 0.62 if n_series == 1 else 0.8 / max(n_series, 1)

    # Value labels only where they stay readable: one series, few bars.
    label_bars = n_series == 1 and 0 < len(cats) <= 12
    label_color = plt.rcParams["text.color"]

    for i, s in enumerate(series):
        offset = 0 if n_series == 1 else (i - n_series / 2 + 0.5) * width
        vals = s.get("values", [])
        if horizontal:
            bars = ax.barh(x + offset, vals, width,
                           label=s.get("name", f"Series {i+1}"),
                           color=colors[i], zorder=3)
        else:
            bars = ax.bar(x + offset, vals, width,
                          label=s.get("name", f"Series {i+1}"),
                          color=colors[i], zorder=3)
        if label_bars:
            ax.bar_label(bars, labels=[_fmt_num(v) for v in vals],
                         padding=4, fontsize="small", color=label_color,
                         fontweight="bold")

    if horizontal:
        ax.set_yticks(x)
        ax.set_yticklabels(cats)
        ax.invert_yaxis()  # first category reads at the TOP
        _value_grid(ax, "x")
        if label_bars:
            ax.margins(x=0.10)
    else:
        ax.set_xticks(x)
        ax.set_xticklabels(cats, rotation=45 if len(cats) > 6 else 0, ha="right" if len(cats) > 6 else "center")
        _value_grid(ax, "y")
        if label_bars:
            ax.margins(y=0.12)  # bar sticky-edges keep the baseline pinned at 0


def _render_line(ax, args: dict):
    cats = args.get("categories", [])
    series = args.get("series", [])
    colors = _get_colors(series, len(series))
    markers = args.get("markers", True)

    # End-point value labels read well up to a few series.
    label_ends = 0 < len(series) <= 4

    for i, s in enumerate(series):
        vals = s.get("values", [])
        kw = {"label": s.get("name", f"Series {i+1}"), "color": colors[i],
              "zorder": 3}
        if markers:
            kw["marker"] = "o"
            kw["markersize"] = 6
            kw["markeredgecolor"] = "white"
            kw["markeredgewidth"] = 1.4
        ax.plot(range(len(vals)), vals, **kw)
        if label_ends and vals:
            ax.annotate(_fmt_num(vals[-1]), (len(vals) - 1, vals[-1]),
                        xytext=(8, 0), textcoords="offset points",
                        fontsize="small", fontweight="bold",
                        color=colors[i], va="center")

    if label_ends:
        ax.margins(x=0.06)
    _value_grid(ax, "y")

    if cats:
        ax.set_xticks(range(len(cats)))
        ax.set_xticklabels(cats, rotation=45 if len(cats) > 6 else 0, ha="right" if len(cats) > 6 else "center")


def _render_pie(ax, args: dict, doughnut: bool = False):
    import matplotlib.pyplot as plt

    series = args.get("series", [{}])
    s = series[0] if series else {}
    vals = s.get("values", [])
    cats = args.get("categories", [f"Slice {i+1}" for i in range(len(vals))])
    colors = _get_colors([{"color": c} for c in (args.get("colors") or [])], len(vals))
    if not colors or len(colors) < len(vals):
        colors = [DEFAULT_COLORS[i % len(DEFAULT_COLORS)] for i in range(len(vals))]

    # The slice edge is the figure background so slices read as separated on
    # dark styles too.
    edge = plt.rcParams["figure.facecolor"]
    wedgeprops = {"edgecolor": edge, "linewidth": 2.5}
    if doughnut:
        wedgeprops["width"] = 0.42

    total = sum(vals) or 1

    def _autopct(pct):
        # Hide labels on slivers where they'd collide with the edge.
        return f"{pct:.0f}%" if pct >= 4 else ""

    wedges, _texts, autotexts = ax.pie(
        vals, colors=colors, autopct=_autopct, startangle=90,
        counterclock=False, pctdistance=0.79 if doughnut else 0.7,
        wedgeprops=wedgeprops,
    )
    for t in autotexts:
        t.set_color("white")
        t.set_fontweight("bold")

    # Doughnut center: show the total for count data. Values that already sum
    # to ~100 (or ~1) are percentages — a "100 total" there is noise.
    if doughnut and not (abs(total - 100) < 1.5 or abs(total - 1) < 0.05):
        ax.text(0, 0.06, _fmt_num(total), ha="center", va="center",
                fontsize=22, fontweight="bold",
                color=plt.rcParams["text.color"])
        ax.text(0, -0.16, "total", ha="center", va="center",
                fontsize=10.5, color=plt.rcParams["xtick.color"])

    # Categories moved off the slices into a side legend — only an explicit
    # legend:false suppresses it.
    if args.get("legend", True):
        ax.legend(wedges, cats, loc="center left", bbox_to_anchor=(0.98, 0.5),
                  handlelength=1.0, handleheight=1.0)
    ax.set_aspect("equal")


def _render_scatter(ax, args: dict):
    series = args.get("series", [])
    colors = _get_colors(series, len(series))

    for i, s in enumerate(series):
        x_vals = s.get("x", s.get("values", []))
        y_vals = s.get("y", [])
        if not y_vals and args.get("categories"):
            y_vals = x_vals
            x_vals = list(range(len(y_vals)))
        sizes = s.get("sizes", 55)
        ax.scatter(x_vals, y_vals, s=sizes, label=s.get("name", f"Series {i+1}"),
                   color=colors[i], alpha=0.8, edgecolors="white",
                   linewidth=0.8, zorder=3)
    ax.set_axisbelow(True)


def _render_area(ax, args: dict):
    cats = args.get("categories", [])
    series = args.get("series", [])
    colors = _get_colors(series, len(series))
    x = range(len(cats)) if cats else None

    for i, s in enumerate(series):
        vals = s.get("values", [])
        xi = x if x and len(vals) == len(cats) else range(len(vals))
        ax.fill_between(xi, vals, alpha=0.25, color=colors[i],
                        label=s.get("name", f"Series {i+1}"), zorder=3)
        ax.plot(xi, vals, color=colors[i], linewidth=2, zorder=3)
    _value_grid(ax, "y")

    if cats:
        ax.set_xticks(range(len(cats)))
        ax.set_xticklabels(cats, rotation=45 if len(cats) > 6 else 0, ha="right" if len(cats) > 6 else "center")


def _render_heatmap(ax, args: dict):
    data = args.get("data", [])
    if not data:
        series = args.get("series", [])
        data = [s.get("values", []) for s in series]

    arr = np.array(data, dtype=float)
    cats = args.get("categories", [])
    row_labels = args.get("row_labels", [s.get("name", f"Row {i}") for i, s in enumerate(args.get("series", []))])

    cmap = args.get("colormap", "YlOrRd")
    ax.grid(False)  # gridlines over cells are noise
    im = ax.imshow(arr, cmap=cmap, aspect="auto")
    plt.colorbar(im, ax=ax, shrink=0.8)

    if cats:
        ax.set_xticks(range(len(cats)))
        ax.set_xticklabels(cats, rotation=45, ha="right")
    if row_labels:
        ax.set_yticks(range(len(row_labels)))
        ax.set_yticklabels(row_labels)

    # Annotate cells
    for i in range(arr.shape[0]):
        for j in range(arr.shape[1]):
            val = arr[i, j]
            text_color = "white" if val > (arr.max() + arr.min()) / 2 else "black"
            ax.text(j, i, f"{val:.0f}", ha="center", va="center",
                    color=text_color, fontsize=9)


def _render_histogram(ax, args: dict):
    series = args.get("series", [{}])
    colors = _get_colors(series, len(series))
    bins = int(args.get("bins", 20))

    for i, s in enumerate(series):
        vals = s.get("values", [])
        ax.hist(vals, bins=bins, alpha=0.75, color=colors[i],
                label=s.get("name", f"Series {i+1}"), edgecolor="white",
                zorder=3)
    _value_grid(ax, "y")


CHART_TYPES = (
    "bar", "column", "horizontal_bar", "line", "pie", "doughnut",
    "scatter", "area", "heatmap", "histogram",
)


def _validate_series(chart_type: str, args: dict) -> str | None:
    """Explicit shape validation so a wrong-keyed payload errors loudly
    instead of silently rendering an empty plot.

    Per chart_type — a blanket "every series needs 'values'" would wrongly
    reject two valid shapes the renderers accept: heatmap's top-level 2-D
    ``data`` (no per-series values) and scatter's ``values``-as-x /
    categories fallbacks.
    """
    series = args.get("series") or []

    if chart_type == "heatmap":
        data = args.get("data") or []
        if data:
            if not all(isinstance(r, (list, tuple)) and len(r) for r in data):
                return ("Error: heatmap 'data' must be a non-empty 2-D array "
                        "of numbers (a list of equal-length rows).")
            return None
        if not series:
            return ("Error: heatmap needs a top-level 'data' 2-D array, or "
                    "'series' rows each carrying 'values'.")
        for i, s in enumerate(series):
            if not isinstance(s, dict) or not s.get("values"):
                keys = sorted(s.keys()) if isinstance(s, dict) else type(s).__name__
                return (f"Error: heatmap series[{i}] has no 'values' (got: {keys}). "
                        "Provide per-row 'values' arrays, or a top-level 'data' "
                        "2-D array instead of series.")
        return None

    if not series:
        return f"Error: '{chart_type}' chart needs a non-empty 'series' array."

    if chart_type == "scatter":
        cats = args.get("categories") or []
        for i, s in enumerate(series):
            if not isinstance(s, dict):
                return f"Error: scatter series[{i}] must be an object, got {type(s).__name__}."
            # Mirror the renderer's fallbacks: x ← values, and y ← x with
            # index-x when top-level categories are present.
            x_vals = s.get("x", s.get("values", []))
            y_vals = s.get("y", [])
            if not y_vals and cats:
                y_vals = x_vals
            if not x_vals or not y_vals:
                return (f"Error: scatter series[{i}] needs 'x' + 'y' arrays "
                        f"(got keys: {sorted(s.keys())}). Also accepted: "
                        "'values' as x with a 'y' array, or 'values' plus "
                        "top-level 'categories'.")
        return None

    # bar / column / horizontal_bar / line / area / histogram / pie / doughnut:
    # every series needs non-empty 'values' (pie/doughnut render series[0]
    # only, but a wrong-keyed later series is still a mistake worth naming).
    for i, s in enumerate(series):
        if not isinstance(s, dict) or not s.get("values"):
            keys = sorted(s.keys()) if isinstance(s, dict) else type(s).__name__
            return (f"Error: {chart_type} series[{i}] has no 'values' (got: "
                    f"{keys}). Expected {{'name': str, 'values': [numbers], "
                    "'color'?: str}.")
    return None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle_create_chart(args: dict) -> str:
    """Create a chart and display it inline in the chat."""
    chart_type = args.get("chart_type", "bar")
    if chart_type not in CHART_TYPES:
        return (f"Error: Unknown chart type '{chart_type}'. "
                f"Supported: {', '.join(CHART_TYPES)}")
    err = _validate_series(chart_type, args)
    if err:
        return err
    title = args.get("title", "")
    x_label = args.get("x_label", "")
    y_label = args.get("y_label", "")
    show_legend = args.get("legend", True)
    style_name = args.get("style", "modern")
    fig_width = float(args.get("width", 10))
    fig_height = float(args.get("height", 6))
    save_path = args.get("save_path")

    # Apply style
    style_params = STYLES.get(style_name, STYLES["modern"])
    with plt.rc_context(style_params):
        fig, ax = plt.subplots(figsize=(fig_width, fig_height))

        # Render chart
        renderers = {
            "bar": lambda: _render_bar(ax, args),
            "column": lambda: _render_bar(ax, args),
            "horizontal_bar": lambda: _render_bar(ax, args, horizontal=True),
            "line": lambda: _render_line(ax, args),
            "pie": lambda: _render_pie(ax, args),
            "doughnut": lambda: _render_pie(ax, args, doughnut=True),
            "scatter": lambda: _render_scatter(ax, args),
            "area": lambda: _render_area(ax, args),
            "heatmap": lambda: _render_heatmap(ax, args),
            "histogram": lambda: _render_histogram(ax, args),
        }

        renderer = renderers.get(chart_type)
        if not renderer:
            plt.close(fig)
            return f"Error: Unknown chart type '{chart_type}'. Supported: {', '.join(renderers.keys())}"

        try:
            renderer()
        except Exception as exc:
            plt.close(fig)
            return f"Error rendering {chart_type} chart: {exc}"

        # Apply labels and title
        if title:
            ax.set_title(title, pad=15)
        if x_label:
            ax.set_xlabel(x_label)
        if y_label:
            ax.set_ylabel(y_label)

        # Legend (not for pie/doughnut/heatmap)
        series = args.get("series", [])
        if show_legend and chart_type not in ("pie", "doughnut", "heatmap") and len(series) > 1:
            ax.legend(loc="best", framealpha=0.9)

        fig.tight_layout()

        # Render to PNG bytes
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        png_bytes = buf.getvalue()

    # Optionally save to file
    saved_msg = ""
    if save_path:
        try:
            out = _resolve_path(save_path, writing=True)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(png_bytes)
            saved_msg = f" Saved to: {_to_agents_relative(out)}"
        except Exception as exc:
            saved_msg = f" Save failed: {exc}"

    # Push inline to dashboard
    caption = f"Chart: {title}" if title else f"{chart_type.replace('_', ' ').title()} chart"
    await _push_image_preview(png_bytes, "image/png", caption)

    return f"{chart_type} chart created ({int(fig_width * 150)}x{int(fig_height * 150)}px).{saved_msg}"
