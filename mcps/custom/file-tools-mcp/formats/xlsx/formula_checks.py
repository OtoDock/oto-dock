"""Checks of a workbook's formulas and numbers, read from the saved XML:
errors among the stored values, references to deleted cells or missing
sheets, spilling formulas saved as plain ones, numbers longer than Excel
keeps, numbers and dates wider than their column (Excel shows #####), and
formulas with no stored value.

Locations are the cell addresses write_xlsx takes (``Sheet1!B7``). Every
other text the file holds (formulas, sheet names, a number as its format
shows it) goes only into a finding's excerpt, never into its message.
"""

import math
import posixpath
import re

import checks
from formats.xlsx import numfmt, recalc
from formats.xlsx.formulas import NOT_IN_SHEET_NAMES, spills
from formats.xlsx.package import related, sheet_targets, workbook_part
from formats.xlsx.refs import quote_sheet

FMT = "xlsx"
_SHOWN = 20
_LONGEST = 1e15
_STRING_RE = re.compile(r'"(?:[^"]|"")*"')
_QUALIFIER_RE = re.compile(r"'((?:[^']|'')+)'!|(?<![\w.\]'#])([^\W\d][\w.]*)!")
_CHART_F_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?f>(.*?)</(?:[A-Za-z_][\w.-]*:)?f>", re.S)
_COL_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?col\b([^>]*?)/?>")
_FORMAT_PR_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?sheetFormatPr\b([^>]*?)/?>")
_MERGE_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?mergeCell\b[^>]*?ref=\"([A-Z]+[0-9]+):")
_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_TEXT_KINDS = frozenset({b"s", b"str", b"inlineStr", b"b", b"e", b"d"})

_ERROR_FIXES = {
    "#DIV/0!": "Guard the divisor: =IF(C7=0, 0, B7/C7), or wrap the formula in IFERROR.",
    "#REF!": "The formula points at cells that were deleted: rewrite the reference with set_formula.",
    "#NAME?": "A function or a name is misspelled or unknown: check the spelling, or define the name "
              "with define_name.",
    "#VALUE!": "An argument has the wrong type (text where a number belongs): check the cells it reads.",
    "#N/A": "A lookup found no match: check the lookup value, or wrap it in IFNA(..., \"\").",
    "#NUM!": "A number is out of the function's range: check the arguments.",
    "#NULL!": "Two ranges do not meet: separate them with a comma, not a space.",
    "#SPILL!": "The result needs empty cells to spill into: clear the cells below or to the right.",
    "#CALC!": "The calculation has no result (an empty array): give FILTER its if_empty argument.",
}
_ANY_ERROR_FIX = "Check the cells the formula reads."


def _sheets(ctx: checks.CheckContext) -> list[tuple[str, str, bytes]]:
    """(title, part, bytes) of each worksheet the checks may read."""
    out = []
    for title, part in sheet_targets(ctx.zip()).items():
        data = ctx.read_part(part)
        if data is not None:
            out.append((title, part, data))
    return out


def _place(title: str, coord: str) -> str:
    return f"{quote_sheet(title)}!{coord}"


def _capped(found: list[checks.Finding], what: str) -> list[checks.Finding]:
    if len(found) <= _SHOWN:
        return found
    rest = len(found) - _SHOWN
    return found[:_SHOWN] + [checks.Finding(
        checks.Severity.INFO, "", message=f"{rest} more {what} are not listed",
        fix="Fix the ones listed and run check_document again.", key=f"more {what}")]


def _formula_text(f) -> str:
    return "=" + recalc.unescape(f.group("t")) if f is not None and f.group("t") is not None else ""


# ---------------------------------------------------------------------------
# Errors among the stored values
# ---------------------------------------------------------------------------


def formula_error(ctx: checks.CheckContext) -> list[checks.Finding]:
    found = []
    for title, _part, data in _sheets(ctx):
        for coord, named, f, v in recalc.cells(data):
            if f is None or named.get(b"t") != b"e":
                continue
            value = recalc.value_of(b"e", v)
            if value is None:
                continue
            error = recalc.unescape(value[1])
            place = _place(title, coord)
            found.append(checks.Finding(
                checks.Severity.ERROR, "", location=place, message=f"the formula gives {error}",
                fix=_ERROR_FIXES.get(error, _ANY_ERROR_FIX), excerpt=_formula_text(f), key=f"{place} {error}"))
    return _capped(found, "formula errors")


def not_computed(ctx: checks.CheckContext) -> list[checks.Finding]:
    places = []
    for title, _part, data in _sheets(ctx):
        for coord, named, f, v in recalc.cells(data):
            if f is not None and recalc.value_of(named.get(b"t", b"n"), v) is None:
                places.append(_place(title, coord))
    if not places:
        return []
    shown = ", ".join(places[:3]) + (f" and {len(places) - 3} more" if len(places) > 3 else "")
    return [checks.Finding(
        checks.Severity.INFO, "", location=places[0],
        message=f"{len(places)} formulas have no stored value: a reader that does not calculate shows them empty",
        fix="Excel computes them when the file opens; a write_xlsx edit stores the values it can compute.",
        excerpt=shown, key="not computed")]


# ---------------------------------------------------------------------------
# Broken references
# ---------------------------------------------------------------------------


def _missing(text: str, known: set[str]) -> list[str]:
    """The sheets ``text`` qualifies a reference with that are not
    ``known`` (case-folded); another workbook's sheets are not checked."""
    out: list[str] = []
    for m in _QUALIFIER_RE.finditer(_STRING_RE.sub('""', text)):
        name = m.group(1).replace("''", "'") if m.group(1) is not None else m.group(2)
        if NOT_IN_SHEET_NAMES & set(name):
            continue
        for end in name.split(":"):
            if end.casefold() not in known and end not in out:
                out.append(end)
    return out


_DELETED = "it refers to cells that were deleted (#REF!)"
_NO_SHEET = "it names a sheet the workbook does not have"


def _broken(text: str, known: set[str]) -> list[str]:
    """What is wrong with ``text``, in words of this module's own: the
    missing sheet's name stays in the excerpt (the formula)."""
    plain = _STRING_RE.sub('""', text)
    out = [_DELETED] if "#REF!" in plain else []
    if _missing(text, known):
        out.append(_NO_SHEET)
    return out


def broken_ref(ctx: checks.CheckContext) -> list[checks.Finding]:
    zf = ctx.zip()
    sheets = _sheets(ctx)
    known = {title.casefold() for title in sheet_targets(zf)}
    found = []
    for title, _part, data in sheets:
        for coord, _named, f, _v in recalc.cells(data):
            if f is None or f.group("t") is None:
                continue
            text = recalc.unescape(f.group("t"))
            place = _place(title, coord)
            for problem in _broken(text, known):
                found.append(checks.Finding(
                    checks.Severity.ERROR, "", location=place, message=f"the formula is broken: {problem}",
                    fix="Rewrite the reference with set_formula, pointing at cells and sheets that exist.",
                    excerpt="=" + text, key=f"{place} {problem}"))
    found += _broken_names(ctx, known) + _broken_charts(ctx, known)
    return _capped(found, "broken references")


def _broken_names(ctx: checks.CheckContext, known: set[str]) -> list[checks.Finding]:
    workbook = workbook_part(ctx.zip())
    root = ctx.part_xml(workbook) if workbook else None
    if root is None:
        return []
    found = []
    for defined in root.iter(f"{_MAIN}definedName"):
        name = defined.get("name", "")
        for problem in _broken(defined.text or "", known):
            found.append(checks.Finding(
                checks.Severity.ERROR, "", location=f"name {name}", message=f"the name is broken: {problem}",
                fix="Delete it with delete_name and define it again with define_name.",
                excerpt=defined.text or "", key=f"name {name} {problem}"))
    return found


def _chart_homes(ctx: checks.CheckContext) -> dict[str, str]:
    """The sheet each chart part is drawn on, by chart part name."""
    zf = ctx.zip()
    homes = {}
    for title, part in sheet_targets(zf).items():
        for drawing in related(zf, part, "/drawing"):
            for chart in related(zf, drawing, "/chart"):
                homes.setdefault(chart, title)
    return homes


def _broken_charts(ctx: checks.CheckContext, known: set[str]) -> list[checks.Finding]:
    found = []
    homes: dict[str, str] | None = None
    for part in ctx.part_names():
        if not (part.startswith("xl/charts/chart") and part.endswith(".xml")):
            continue
        data = ctx.read_part(part)
        for m in _CHART_F_RE.finditer(data or b""):
            text = recalc.unescape(m.group(1))
            for problem in _broken(text, known):
                if homes is None:
                    homes = _chart_homes(ctx)
                home = homes.get(part)
                where = f"a chart on {quote_sheet(home)}" if home else f"chart {posixpath.basename(part)}"
                found.append(checks.Finding(
                    checks.Severity.ERROR, "", location=where, message=f"a chart series is broken: {problem}",
                    fix="Remove the chart and add it again over cells that exist.", excerpt=text,
                    part=part, key=f"{where} {problem}"))
    return found


# ---------------------------------------------------------------------------
# Spills saved as plain formulas
# ---------------------------------------------------------------------------


def spill(ctx: checks.CheckContext) -> list[checks.Finding]:
    found = []
    for title, _part, data in _sheets(ctx):
        arrays = recalc.read_arrays(data)
        for coord, named, f, _v in recalc.cells(data):
            # An array formula over a range shows every value: Excel reads
            # it as the older kind of array.
            if f is None or f.group("t") is None or b"cm" in named or coord in arrays:
                continue
            text = recalc.unescape(f.group("t"))
            if not recalc.dynamic_anchors({coord: text}):
                continue
            place = _place(title, coord)
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=place,
                message=f"the formula spills ({spills('=' + text)}) but is saved as a plain formula: Excel "
                        "shows only its first value",
                fix="Write it again with set_formula: write_xlsx saves a spilling formula so Excel spills it.",
                excerpt="=" + text, key=f"{place} plain spill"))
    return _capped(found, "spilling formulas saved as plain ones")


# ---------------------------------------------------------------------------
# Numbers: too long, too wide
# ---------------------------------------------------------------------------


def long_number(ctx: checks.CheckContext) -> list[checks.Finding]:
    found = []
    for title, _part, data in _sheets(ctx):
        for coord, named, f, v in recalc.cells(data):
            if f is not None or named.get(b"t", b"n") != b"n":
                continue
            value = recalc.value_of(b"n", v)
            if value is None:
                continue
            number = float(value[1])
            if not number.is_integer() or abs(number) < _LONGEST:
                continue
            place = _place(title, coord)
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=place,
                message=f"the number {value[1].decode()} has more digits than the 15 Excel keeps: it shows in "
                        "scientific form and its last digits are lost",
                fix="If it is an identifier (an account, card or order number), write it as text.",
                key=f"{place} long number"))
    return _capped(found, "long numbers")


class _Styles:
    """What the width check needs from the styles part: each cell style's
    number format and font, and the default font."""

    def __init__(self, ctx: checks.CheckContext) -> None:
        from openpyxl.styles.numbers import BUILTIN_FORMATS

        self.formats: dict[int, str] = dict(BUILTIN_FORMATS)
        self.xfs: list[tuple[int, int]] = []
        self.fonts: list[tuple[str, float, bool]] = []
        zf = ctx.zip()
        workbook = workbook_part(zf)
        parts = related(zf, workbook, "/styles") if workbook else []
        root = ctx.part_xml(parts[0]) if parts else None
        if root is None:
            return
        for fmt in root.iter(f"{_MAIN}numFmt"):
            if fmt.get("numFmtId", "").isdigit():
                self.formats[int(fmt.get("numFmtId"))] = fmt.get("formatCode", "")
        for font in root.iterfind(f"{_MAIN}fonts/{_MAIN}font"):
            name = font.find(f"{_MAIN}name")
            size = font.find(f"{_MAIN}sz")
            self.fonts.append((name.get("val", "Calibri") if name is not None else "Calibri",
                               _float(size.get("val") if size is not None else None, 11.0),
                               font.find(f"{_MAIN}b") is not None))
        for xf in root.iterfind(f"{_MAIN}cellXfs/{_MAIN}xf"):
            self.xfs.append((int(xf.get("numFmtId", "0") or 0), int(xf.get("fontId", "0") or 0)))

    def of(self, index: int) -> tuple[str, tuple[str, float, bool]]:
        fmt_id, font_id = self.xfs[index] if 0 <= index < len(self.xfs) else (0, 0)
        font = self.fonts[font_id] if 0 <= font_id < len(self.fonts) else self.default_font
        return self.formats.get(fmt_id, "General"), font

    @property
    def default_font(self) -> tuple[str, float, bool]:
        return self.fonts[0] if self.fonts else ("Calibri", 11.0, False)


def _float(text, default: float) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _px(points: float) -> float:
    return points * 96 / 72


def _column_px(width: float, digit: int) -> int:
    """A stored column width (characters) in pixels, as Excel draws it."""
    return math.trunc(((256 * width + math.trunc(128 / digit)) / 256) * digit)


def _columns(head: bytes, digit: int) -> tuple[dict[int, int | None], int]:
    """Pixel width by column (None for a hidden one) and the default."""
    default_chars = None
    m = _FORMAT_PR_RE.search(head)
    named = dict(recalc.attrs(m.group(1))) if m else {}
    if b"defaultColWidth" in named:
        default_chars = _float(named[b"defaultColWidth"].decode(), 0.0) or None
    if default_chars is None:
        base = _float(named.get(b"baseColWidth", b"8").decode(), 8.0)
        default_chars = math.trunc((base * digit + 5) / digit * 256) / 256
    widths: dict[int, int | None] = {}
    for col in _COL_RE.finditer(head):
        attr = dict(recalc.attrs(col.group(1)))
        lo, hi = int(_float(attr.get(b"min", b"0").decode(), 0)), int(_float(attr.get(b"max", b"0").decode(), 0))
        hidden = attr.get(b"hidden") in (b"1", b"true")
        px = None if hidden else _column_px(_float(attr.get(b"width", b"0").decode(), default_chars), digit)
        for index in range(max(lo, 1), min(hi, lo + 16_384) + 1):
            widths[index] = px
    return widths, _column_px(default_chars, digit)


def hash_overflow(ctx: checks.CheckContext) -> list[checks.Finding]:
    import fonts

    styles = _Styles(ctx)
    family, size, bold = styles.default_font
    digit = max(1, round(_px(fonts.advance_width("0", family, size, bold=bold))))
    found = []
    for title, _part, data in _sheets(ctx):
        head = data[:recalc.sheet_data_start(data)]
        widths, default_px = _columns(head, digit)
        merged = {m.group(1).decode() for m in _MERGE_RE.finditer(data)}
        for coord, named, _f, v in recalc.cells(data):
            if named.get(b"t", b"n") in _TEXT_KINDS or b"s" not in named or coord in merged:
                continue
            value = recalc.value_of(b"n", v)
            if value is None:
                continue
            code, (face, points, heavy) = styles.of(int(_float(named[b"s"].decode(), 0)))
            if code in ("General", "@"):
                continue
            text = numfmt.render(float(value[1]), code)
            if not text:
                continue
            col = _column_index(coord)
            width = widths.get(col, default_px)
            if width is None or _px(fonts.advance_width(text, face, points, bold=heavy)) <= width:
                continue
            place = _place(title, coord)
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=place,
                message="the number, as its format shows it, is wider than its column, so Excel shows ##### there",
                fix=f"Widen column {coord.rstrip('0123456789')} with set_column_width or auto_column_width, "
                    "or use a shorter number format.",
                excerpt=text, key=f"{place} hash"))
            if len(found) == _SHOWN:
                return found + [checks.Finding(
                    checks.Severity.INFO, "", message=f"the check stopped after {_SHOWN} cells shown as #####",
                    fix="Widen those columns and run check_document again.", key="more hash")]
    return found


def _column_index(coord: str) -> int:
    col = 0
    for ch in coord:
        if not ch.isalpha():
            break
        col = col * 26 + ord(ch.upper()) - 64
    return col


def roundtrip_loss(ctx: checks.CheckContext) -> list[checks.Finding]:
    from formats.xlsx import loss

    found = []
    for item in loss.scan_zip(ctx.zip(), posixpath.splitext(ctx.path)[1].lower()):
        found.append(checks.Finding(
            checks.Severity.WARNING if item.refuse else checks.Severity.INFO, "",
            location=", ".join(item.where) or "the workbook",
            message=f"an edit with write_xlsx would {'drop' if item.refuse else 'simplify'} {item.what}",
            fix="Make such edits in Excel, or edit with allow_loss: true once the person agreed to lose it."
            if item.refuse else "Nothing to do: the content stays, drawn simpler.",
            key=item.what))
    return found


def register_all() -> None:
    checks.register(FMT, "formula_error", formula_error, baseline=True)
    checks.register(FMT, "broken_ref", broken_ref, baseline=True)
    checks.register(FMT, "spill", spill, baseline=True)
    checks.register(FMT, "long_number", long_number, baseline=True)
    checks.register(FMT, "hash_overflow", hash_overflow, baseline=True)
    checks.register(FMT, "not_computed", not_computed, final_only=True)
    checks.register(FMT, "roundtrip_loss", roundtrip_loss, final_only=True)


register_all()
