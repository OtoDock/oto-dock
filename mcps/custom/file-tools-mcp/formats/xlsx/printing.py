"""write_xlsx's print setup: paper, orientation, fit, margins, titles,
headers and footers of one sheet."""

from catalog import KeySpec, OpSpec
from formats.xlsx.values import flag_key
from results import Code, OpCtx, OpError
from units import to_inches

# openpyxl's paper size codes (ECMA-376 ST_PaperSize).
_PAPER = {"letter": 1, "tabloid": 3, "legal": 5, "a3": 8, "a4": 9, "a5": 11}
_ORIENTATIONS = ("portrait", "landscape")
_CENTER = {"horizontal": (True, False), "vertical": (False, True), "both": (True, True)}
_MARGINS = ("top", "bottom", "left", "right", "header", "footer")
_PARTS = ("left", "center", "right")


def a4_by_default(wb) -> None:
    """Every sheet of a new workbook set to A4 paper unless the batch chose
    one: a sheet without a page setup prints on the reader's default,
    Letter in Collabora."""
    for ws in wb.worksheets:
        if not ws.page_setup.paperSize:
            ws.page_setup.paperSize = _PAPER["a4"]


def same_paper(wb, ws) -> None:
    """A sheet added to a workbook takes the paper its other sheets agree
    on (a workbook with sheets on A4 gets another A4 sheet)."""
    papers = {other.page_setup.paperSize for other in wb.worksheets if other is not ws}
    if len(papers) == 1 and None not in papers:
        ws.page_setup.paperSize = papers.pop()


def _words(options) -> str:
    return " | ".join(options)


def _op_set_print_setup(job, ctx: OpCtx, op: dict) -> None:
    from openpyxl.worksheet.properties import PageSetupProperties

    ws = job.sheet(op.get("sheet"))
    setup = ws.page_setup
    if op.get("orientation") is not None:
        orientation = op["orientation"].strip().lower()
        if orientation not in _ORIENTATIONS:
            raise OpError(Code.BAD_VALUE, f"orientation '{op['orientation']}' is not one of: {_words(_ORIENTATIONS)}")
        setup.orientation = orientation
    if op.get("paper") is not None:
        paper = _PAPER.get(op["paper"].strip().lower())
        if paper is None:
            raise OpError(Code.BAD_VALUE, f"paper '{op['paper']}' is not one of: A4 | Letter | A3 | Legal | A5 | "
                          "Tabloid")
        setup.paperSize = paper
    elif not setup.paperSize:
        setup.paperSize = _PAPER["a4"]
        ctx.note("paper set to A4 (pass paper to choose another)")
    if op.get("fit_width") is not None or op.get("fit_height") is not None:
        for key in ("fit_width", "fit_height"):
            if op.get(key) is not None and op[key] < 0:
                raise OpError(Code.BAD_VALUE, f"{key} must be 0 or more (0 = as many pages as needed)")
        setup.fitToWidth = op.get("fit_width", 1)
        setup.fitToHeight = op.get("fit_height", 0)
        props = ws.sheet_properties
        if props.pageSetUpPr is None:
            props.pageSetUpPr = PageSetupProperties(fitToPage=True)
        else:
            props.pageSetUpPr.fitToPage = True
    margins = op.get("margins")
    if margins is not None:
        for side in _MARGINS:
            if side in margins:
                inches = to_inches(margins[side], default="in")
                if inches < 0:
                    raise OpError(Code.BAD_VALUE, f"margins.{side} must be 0 or more")
                setattr(ws.page_margins, side, inches)
    if op.get("print_area") is not None:
        ws.print_area = op["print_area"] or None
    if op.get("print_title_rows") is not None:
        rows = op["print_title_rows"]
        # A single row number repeats that row.
        ws.print_title_rows = (f"{rows}:{rows}" if isinstance(rows, int) else rows) or None
    if op.get("print_title_columns") is not None:
        ws.print_title_cols = op["print_title_columns"] or None
    center = op.get("center")
    if center is not None:
        if isinstance(center, bool):
            horizontal, vertical = (True, False) if center else (False, False)
        else:
            pair = _CENTER.get(center.strip().lower())
            if pair is None:
                raise OpError(Code.BAD_VALUE, f"center '{center}' is not one of: true | false | "
                              f"{_words(_CENTER)}")
            horizontal, vertical = pair
        ws.print_options.horizontalCentered = horizontal
        ws.print_options.verticalCentered = vertical
    if op.get("gridlines") is not None:
        ws.print_options.gridLines = op["gridlines"]
    _set_header_footer(ws.oddHeader, op.get("header"))
    _set_header_footer(ws.oddFooter, op.get("footer"))


def _set_header_footer(target, value) -> None:
    """Text for the left, center and right parts of a header or footer (a
    plain string is the centre)."""
    if value is None:
        return
    parts = {"center": value} if isinstance(value, str) else value
    for part in _PARTS:
        if part in parts:
            getattr(target, part).text = parts[part] or None


_HEADER_FIELDS = tuple(KeySpec(part, ("str",)) for part in _PARTS)

OPS = [
    OpSpec("set_print_setup", _op_set_print_setup, (
        KeySpec("sheet", ("str",)),
        KeySpec("orientation", ("str",), help=_words(_ORIENTATIONS)),
        KeySpec("paper", ("str",), help="A4 | Letter | A3 | Legal | A5 | Tabloid; A4 when the sheet has none"),
        KeySpec("fit_width", ("int",), help="pages wide"),
        KeySpec("fit_height", ("int",), help="pages tall; 0 = as many as needed"),
        KeySpec("margins", ("dict",), fields=tuple(
            KeySpec(side, ("length",), unit="in") for side in _MARGINS)),
        KeySpec("print_area", ("str",), help="e.g. 'A1:F40'"),
        KeySpec("print_title_rows", ("str", "int"), help="rows repeated on every page, e.g. '1:2'"),
        KeySpec("print_title_columns", ("str",), help="e.g. 'A:B'"),
        KeySpec("center", ("bool", "str"), help="true | horizontal | vertical | both"),
        flag_key("gridlines"),
        KeySpec("header", ("str", "dict"), fields=_HEADER_FIELDS, help="text, or {left, center, right}"),
        KeySpec("footer", ("str", "dict"), fields=_HEADER_FIELDS, help="&P is the page, &N the page count"),
    ), "SHEETS", aliases=("page_setup", "print_setup", "set_page_setup"),
        note="how the sheet prints: only the given settings change",
        examples=({"op": "set_print_setup", "orientation": "landscape", "paper": "A4", "fit_width": 1,
                   "fit_height": 0, "margins": {"top": "2cm", "bottom": 0.75, "left": 0.5, "right": 0.5,
                                               "header": 0.3, "footer": 0.3},
                   "print_area": "A1:F40", "print_title_rows": "1:1", "print_title_columns": "A:A",
                   "center": "horizontal", "gridlines": False, "header": {"left": "Budget", "right": "&D"},
                   "footer": "Page &P of &N"},)),
]
