"""Text frames as write_pptx sets them: line and paragraph spacing, real
bullets and numbering, the frame's insets, vertical anchor and autofit, and
each run's language and letter spacing.

Paragraph properties go where the schema puts a:pPr's children
(``ooxml.order``), so PowerPoint never has to repair them.
"""

import re
from typing import Any

import units
from catalog import KeySpec
from ooxml import order
from results import Code, OpCtx, OpError

_IN = "in"
_PT = units.POINTS

BULLET_NONE = "none"
BULLET_NUMBER = "number"
AUTOFIT_NONE = "none"
AUTOFIT_SHRINK = "shrink"
AUTOFIT_RESIZE = "resize"
AUTOFITS = (AUTOFIT_NONE, AUTOFIT_SHRINK, AUTOFIT_RESIZE)
ANCHORS = ("top", "middle", "bottom")
SIDES = ("left", "top", "right", "bottom")

_BULLET_TAGS = frozenset({"buNone", "buAutoNum", "buChar", "buBlip"})
NUMBER_STYLES = ("arabicPeriod", "arabicParenR", "arabicParenBoth", "arabicPlain", "alphaLcPeriod",
                 "alphaUcPeriod", "alphaLcParenR", "alphaUcParenR", "romanLcPeriod", "romanUcPeriod")
_DEFAULT_INDENT_IN = 0.375
_MAX_MARGIN = 6 * 914400
# OOXML's bounds: spcPct up to 13200%, spcPts and spcBef/spcAft up to 1584 pt,
# spc (letter spacing) within 4000 pt either way.
_MAX_MULTIPLE = 132.0
_MAX_POINTS = 1584.0
_MAX_LETTER_PT = 4000.0
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

PARAGRAPH_KEYS = (
    KeySpec("line_spacing", ("spacing",), unit=units.MULTIPLE,
            help="a multiple (1.2, '1.5x', '150%') or points ('18pt')"),
    KeySpec("space_before", ("length",), unit=_PT),
    KeySpec("space_after", ("length",), unit=_PT),
    KeySpec("bullet", ("str",), help="a bullet character such as '•', 'number', or 'none'"),
    KeySpec("number_style", ("str",), help=", ".join(NUMBER_STYLES)),
    KeySpec("start_at", ("int",), help="the first number"),
    KeySpec("bullet_indent", ("length",), unit=_IN, help="the hanging indent of a bullet"),
)
FRAME_KEYS = (
    KeySpec("insets", ("length", "dict"), unit=_IN, fields=tuple(KeySpec(side, ("length",)) for side in SIDES),
            help="one length for all sides, or {left, top, right, bottom}"),
    KeySpec("vertical_anchor", ("str",), help="top | middle | bottom"),
    KeySpec("autofit", ("str",), help="none | shrink (the text shrinks to the box) | resize (the box grows)"),
)
RUN_KEYS = (
    KeySpec("lang", ("str",), help="the text's language, e.g. el-GR or en-US, for spelling and hyphenation"),
    KeySpec("letter_spacing", ("float",), help="pt, negative to tighten"),
)


def _qn(tag: str) -> str:
    from pptx.oxml.ns import qn

    return qn(tag)


def _choice(value: str, choices: tuple[str, ...], what: str) -> str:
    folded = value.strip()
    for choice in choices:
        if folded.casefold() == choice.casefold():
            return choice
    raise OpError(Code.BAD_VALUE, f"unknown {what} '{value}'", hint=f"Use one of: {', '.join(choices)}.")


def _bullet(paragraph: Any, op: dict, style: str) -> None:
    from lxml import etree

    ppr = paragraph._p.get_or_add_pPr()
    for existing in list(ppr):
        if existing.tag.rsplit("}", 1)[-1] in _BULLET_TAGS:
            ppr.remove(existing)
    if style.strip().casefold() == BULLET_NONE:
        order.insert_child(ppr, etree.Element(_qn("a:buNone")))
        ppr.set("indent", "0")
        if op.get("bullet_indent") is not None:
            ppr.set("marL", "0")
        return
    if style.strip().casefold() == BULLET_NUMBER:
        attrs = {"type": _choice(op.get("number_style", NUMBER_STYLES[0]), NUMBER_STYLES, "number_style")}
        if op.get("start_at") is not None:
            if not 1 <= op["start_at"] <= 32767:
                raise OpError(Code.BAD_VALUE, f"start_at {op['start_at']} is out of range", hint="Use 1 to 32767.")
            attrs["startAt"] = str(op["start_at"])
        mark = etree.Element(_qn("a:buAutoNum"), attrs)
    else:
        char = style.strip()
        if len(char) != 1:
            raise OpError(Code.BAD_VALUE, f"bullet '{style}' is not one character",
                          hint="Give a bullet character such as '•' or '–', 'number' or 'none'.")
        mark = etree.Element(_qn("a:buChar"), char=char)
    order.insert_child(ppr, mark)
    if op.get("bullet_indent") is not None or ppr.get("marL") is None:
        indent = units.to_emu(op.get("bullet_indent", _DEFAULT_INDENT_IN), default=_IN)
        if not 0 <= indent * (paragraph.level + 1) <= _MAX_MARGIN:
            raise OpError(Code.BAD_VALUE, f"bullet_indent {op.get('bullet_indent')} is out of range",
                          hint="Use 0 to 6 in.")
        ppr.set("marL", str(indent * (paragraph.level + 1)))
        ppr.set("indent", str(-indent))


def format_paragraphs(paragraphs: list[Any], op: dict, ctx: OpCtx, bullet: str | None = None) -> None:
    """The paragraph keys on each paragraph an operation wrote; ``bullet``
    is the operation's own default bullet."""
    from pptx.util import Emu, Pt

    style = op.get("bullet") or bullet
    if style is None and any(op.get(k) is not None for k in ("number_style", "start_at", "bullet_indent")):
        ctx.warn("number_style, start_at and bullet_indent take effect with bullet")
    for paragraph in paragraphs:
        if op.get("line_spacing") is not None:
            spacing = units.parse_spacing(op["line_spacing"], default=units.MULTIPLE)
            top = _MAX_MULTIPLE if spacing.kind == units.MULTIPLE else _MAX_POINTS
            if not 0 < spacing.value <= top:
                raise OpError(Code.BAD_VALUE, f"line_spacing {op['line_spacing']} is out of range",
                              hint=f"Use a multiple above 0 up to {top:g}, or points up to {_MAX_POINTS:g}pt.")
            paragraph.line_spacing = spacing.value if spacing.kind == units.MULTIPLE else Pt(spacing.value)
        for key in ("space_before", "space_after"):
            if op.get(key) is not None:
                points = units.to_pt(op[key], default=_PT)
                if not 0 <= points <= _MAX_POINTS:
                    raise OpError(Code.BAD_VALUE, f"{key} {op[key]} is out of range", hint=f"Use 0 to {_MAX_POINTS:g} pt.")
                setattr(paragraph, key, Emu(units.to_emu(op[key], default=_PT)))
        if style is not None:
            _bullet(paragraph, op, style)


def format_frame(frame: Any, op: dict) -> None:
    """The frame keys: insets, vertical anchor, autofit."""
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE

    insets = op.get("insets")
    if insets is not None:
        if isinstance(insets, dict):
            for side in SIDES:
                if insets.get(side) is not None:
                    setattr(frame, f"margin_{side}", units.to_emu(insets[side], default=_IN))
        else:
            margin = units.to_emu(insets, default=_IN)
            frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = margin
    if op.get("vertical_anchor"):
        word = _choice(op["vertical_anchor"], ANCHORS, "vertical_anchor")
        frame.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}[word]
    if op.get("autofit"):
        word = _choice(op["autofit"], AUTOFITS, "autofit")
        frame.auto_size = {AUTOFIT_NONE: MSO_AUTO_SIZE.NONE, AUTOFIT_SHRINK: MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE,
                           AUTOFIT_RESIZE: MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT}[word]


def format_run(run: Any, fmt: dict) -> None:
    """A run's language and letter spacing."""
    if fmt.get("lang"):
        lang = fmt["lang"].strip()
        if not _LANG_RE.match(lang):
            raise OpError(Code.BAD_VALUE, f"lang '{lang}' is not a language tag",
                          hint="Give a tag such as el-GR, en-US or de-DE.")
        run.font._rPr.set("lang", lang)
    if fmt.get("letter_spacing") is not None:
        if abs(fmt["letter_spacing"]) > _MAX_LETTER_PT:
            raise OpError(Code.BAD_VALUE, f"letter_spacing {fmt['letter_spacing']:g} is out of range",
                          hint=f"Use -{_MAX_LETTER_PT:g} to {_MAX_LETTER_PT:g} pt.")
        run.font._rPr.set("spc", str(round(fmt["letter_spacing"] * 100)))


def wants_paragraphs(op: dict) -> bool:
    """Whether the operation sets anything on paragraphs."""
    return any(op.get(name) is not None for name in (
        "line_spacing", "space_before", "space_after", "bullet", "number_style", "start_at", "bullet_indent"))


def wants_text(op: dict) -> bool:
    """Whether the operation sets anything on a text frame."""
    return any(op.get(name) is not None for name in (
        "line_spacing", "space_before", "space_after", "bullet", "number_style", "start_at", "bullet_indent",
        "insets", "vertical_anchor", "autofit"))


_AUTOFIT_TAGS = frozenset({"noAutofit", "normAutofit", "spAutoFit"})


def write_shrink(frame: Any, scale: float, line_reduction: float) -> None:
    """Shrink-on-overflow on the frame with the scale it shows the text at,
    so PowerPoint draws it fitted before anyone edits it."""
    from lxml import etree

    body_pr = frame._bodyPr
    for child in list(body_pr):
        if child.tag.rsplit("}", 1)[-1] in _AUTOFIT_TAGS:
            body_pr.remove(child)
    fit = etree.Element(_qn("a:normAutofit"))
    if scale < 1:
        fit.set("fontScale", str(round(scale * 100000)))
        if line_reduction:
            fit.set("lnSpcReduction", str(round(line_reduction * 100000)))
    order.insert_child(body_pr, fit)
