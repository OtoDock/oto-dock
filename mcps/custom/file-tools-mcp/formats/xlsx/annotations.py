"""write_xlsx operations that annotate cells: hyperlinks and comments."""

import re
from copy import copy

from catalog import KeySpec, OpSpec
from formats.xlsx.values import flag_key
from results import Code, NoOp, OpCtx, OpError
from units import to_pt

# Links a workbook may carry: a page, a mail draft, or a place inside the
# workbook. A file:, javascript: or similar link opens something the person
# did not ask for.
_SCHEMES = ("http://", "https://", "mailto:")
_LINK_COLOR = "FF0563C1"
_DEFAULT_AUTHOR = "OtoDock"
# An equation's LaTeX lives in a cell comment that starts with this marker.
_EQUATION_MARKER_RE = re.compile(r"^\s*(?:[^\n]{0,120}\n)?\s*LaTeX:", re.DOTALL)
_QUALIFIED_RE = re.compile(r"^(?:'((?:[^']|'')+)'|([^'!]+))!(.+)$")
_NAME_RE = re.compile(r"[^\W\d][\w.]*")
_SHEET = KeySpec("sheet", ("str",))
_CELL = KeySpec("cell", ("str",), required=True)


def _is_equation_comment(comment) -> bool:
    return comment is not None and bool(_EQUATION_MARKER_RE.match(comment.text or ""))


_AUTO_COLOR = "auto"


def _plain_text_color(font) -> bool:
    """True when the font has no colour of its own (none, automatic, or the
    theme's text colour every new cell gets)."""
    color = getattr(font, "color", None)
    if color is None or color.type == _AUTO_COLOR:
        return True
    return color.type == "theme" and color.theme == 1 and not color.tint


def _defined(wb, name: str) -> bool:
    folded = name.casefold()
    if any(n.casefold() == folded for n in wb.defined_names):
        return True
    return any(n.casefold() == folded for sheet in wb.worksheets for n in sheet.defined_names)


def _check_location(wb, ws, location: str) -> str:
    """An in-workbook link target: 'Sheet 2'!A1, Sheet2!A1:B4, A1 on the same
    sheet, or a defined name. A named sheet and a name must exist; anything
    else is refused, since the target is stored as it is given."""
    from formats.xlsx.refs import parse_ref

    text = location.strip().lstrip("#")
    m = _QUALIFIED_RE.match(text)
    if m:
        title = (m.group(1) or m.group(2)).replace("''", "'")
        if title not in wb.sheetnames:
            raise OpError(Code.NOT_FOUND, f"the link points at sheet '{title}', which does not exist",
                          hint=f"The sheets are: {', '.join(wb.sheetnames)}.")
        target = m.group(3)
    else:
        target = text
    if parse_ref(target) is not None:
        return text
    if not m and _NAME_RE.fullmatch(target):
        if not _defined(wb, target):
            raise OpError(Code.NOT_FOUND, f"the link points at no defined name '{target}'",
                          hint="Define the name first, or link to a cell such as 'Sheet 2'!A1.")
        return text
    raise OpError(Code.BAD_VALUE, f"'{location}' is not a cell, a range or a defined name",
                  hint="Give a place such as 'Sheet 2'!A1, B4:C9 or a defined name.")


def _op_add_hyperlink(job, ctx: OpCtx, op: dict) -> None:
    from openpyxl.worksheet.hyperlink import Hyperlink

    ws = job.sheet(op.get("sheet"))
    cell = ws[op["cell"]]
    url = op.get("url")
    location = op.get("location")
    if url is not None and location is not None:
        raise OpError(Code.ONE_OF, "give url or location, not both")
    link = Hyperlink(ref=cell.coordinate, tooltip=op.get("tooltip"))
    if url is not None:
        if not url.strip().lower().startswith(_SCHEMES):
            raise OpError(Code.BAD_VALUE, f"'{url}' is not an http, https or mailto link",
                          location=f"{ws.title}!{cell.coordinate}",
                          hint="Give a web address or a mailto: link, or a location inside the workbook.")
        link.target = url.strip()
    else:
        link.location = _check_location(job.wb, ws, location)
    cell.hyperlink = link
    label = op.get("text")
    if label is None and cell.value is None:
        label = url or location
    if label is not None:
        # A link's label is text, never a formula.
        cell.value = label
        cell.data_type = "s"
    font = cell.font
    if _plain_text_color(font):
        styled = copy(font)
        styled.color = _LINK_COLOR
        styled.underline = "single"
        cell.font = styled
    job.touch(ws, cell.row, cell.column)


def _op_add_comment(job, ctx: OpCtx, op: dict) -> None:
    from openpyxl.comments import Comment

    ws = job.sheet(op.get("sheet"))
    cell = ws[op["cell"]]
    text = op["text"]
    where = f"{ws.title}!{cell.coordinate}"
    if _EQUATION_MARKER_RE.match(text):
        raise OpError(Code.BAD_VALUE, "a comment that starts with 'LaTeX:' would read as an equation's source",
                      location=where, hint="Reword the start of the comment, or use add_equation.")
    if _is_equation_comment(cell.comment):
        raise OpError(Code.BAD_VALUE, "this cell holds an equation's LaTeX source in its comment",
                      location=where, hint="Comment on a neighbouring cell instead.")
    if cell.comment is not None:
        ctx.note(f"the existing comment on {where} was replaced")
    comment = Comment(text, op.get("author", _DEFAULT_AUTHOR))
    if op.get("width") is not None:
        comment.width = to_pt(op["width"], default="pt")
    if op.get("height") is not None:
        comment.height = to_pt(op["height"], default="pt")
    cell.comment = comment
    job.touch(ws, cell.row, cell.column)


def _op_remove_comment(job, ctx: OpCtx, op: dict) -> None:
    from openpyxl.utils import range_boundaries

    ws = job.sheet(op.get("sheet"))
    if op.get("cell") is not None:
        c1, r1, c2, r2 = range_boundaries(op["cell"])
    elif op.get("range") is not None:
        c1, r1, c2, r2 = range_boundaries(op["range"])
    elif op.get("all"):
        c1 = r1 = c2 = r2 = None
    else:
        raise NoOp("all: false removes nothing")
    removed = kept = 0
    for (row, col), cell in list(ws._cells.items()):
        if c1 is not None and not (r1 <= row <= r2 and c1 <= col <= c2):
            continue
        comment = getattr(cell, "comment", None)
        if comment is None:
            continue
        if _is_equation_comment(comment):
            kept += 1
            continue
        cell.comment = None
        removed += 1
    if kept:
        ctx.note(f"{kept} equation source comment(s) kept: removing one would detach its picture")
    if not removed:
        raise NoOp("no comments to remove there")
    job.declare_comments_gone(ctx, ws)
    ctx.note(f"{removed} comment(s) removed from '{ws.title}'")


OPS = [
    OpSpec("add_hyperlink", _op_add_hyperlink, (
        _SHEET, _CELL,
        KeySpec("url", ("str",), aliases=("link", "href"), help="http, https or mailto"),
        KeySpec("location", ("str",), help="a place in the workbook, e.g. 'Sheet 2'!A1"),
        KeySpec("text", ("str",), help="the cell's text; default the cell's value, else the link"),
        KeySpec("tooltip", ("str",)),
    ), "CELLS", one_of=(("url", "location"),), aliases=("add_link", "hyperlink"),
        note="url: http, https or mailto; location: a cell or name in this workbook; the cell gets the "
             "link look unless it already has a font colour",
        examples=({"op": "add_hyperlink", "cell": "A1", "url": "https://example.com", "text": "Site",
                   "tooltip": "Open the site"},
                  {"op": "add_hyperlink", "sheet": "Sheet", "cell": "A2", "location": "'Sheet'!C9"})),
    OpSpec("add_comment", _op_add_comment, (
        _SHEET, _CELL, KeySpec("text", ("str",), required=True),
        KeySpec("author", ("str",), default=_DEFAULT_AUTHOR),
        KeySpec("width", ("length",), unit="pt"), KeySpec("height", ("length",), unit="pt"),
    ), "CELLS", aliases=("add_note", "comment"),
        note="a comment (note) on one cell; an existing one is replaced",
        examples=({"op": "add_comment", "cell": "B2", "text": "Checked against the invoice",
                   "author": "Finance", "width": 200, "height": "3cm"},)),
    OpSpec("remove_comment", _op_remove_comment, (
        _SHEET, KeySpec("cell", ("str",)), KeySpec("range", ("str",)), flag_key("all"),
    ), "CELLS", one_of=(("cell", "range", "all"),), aliases=("remove_comments", "delete_comment"),
        note="cell, range or all: true; an equation's source comment is kept",
        setup=({"op": "add_comment", "cell": "B2", "text": "x"}, {"op": "add_comment", "cell": "C3", "text": "y"}),
        examples=({"op": "remove_comment", "cell": "B2"}, {"op": "remove_comment", "range": "A1:D9"},
                  {"op": "remove_comment", "all": True})),
]
