"""Lengths and spacing as the agents write them, in the units each format
needs.

A length is a number in the key's own unit (each key states it: DOCX
spacing is in points, its indents in inches, XLSX chart sizes in cm) or a
string with a unit: ``2.5cm``, ``1 in``, ``12pt``, ``96px``, ``914400emu``.
Conversions go through whole EMUs (914400 per inch), the unit OOXML stores,
so ``1cm`` is exactly 360000 EMU and never drifts through points.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from results import Code, OpError, short_repr

EMU_PER_UNIT = {
    "in": 914400,
    "cm": 360000,
    "mm": 36000,
    "pt": 12700,
    "pc": 152400,
    "px": 9525,  # 96 px per inch, as CSS and Office count them
    "emu": 1,
}
EMU_PER_TWIP = 635
_UNIT_ALIASES = {"inch": "in", "inches": "in", '"': "in"}
_REFUSED_UNITS = frozenset({"em", "rem", "%", "vw", "vh"})
_ACCEPTED = "cm, mm, in, pt, pc, px or emu"

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_LENGTH_RE = re.compile(rf"^\s*({_NUMBER})\s*([A-Za-z\"%]*)\s*$", re.ASCII)
_NUMBER_RE = re.compile(rf"^\s*{_NUMBER}\s*$", re.ASCII)
_COMMA_DECIMAL_RE = re.compile(r"^\s*[+-]?\d+,\d+", re.ASCII)

MULTIPLE = "x"
POINTS = "pt"


@dataclass(frozen=True)
class Spacing:
    """Line or paragraph spacing: ``kind`` is ``x`` (a multiple of the line
    height) or ``pt`` (points)."""

    kind: str
    value: float


def _bad(value, what: str, hint: str) -> OpError:
    return OpError(Code.BAD_VALUE, f"{short_repr(value)} is not {what}", hint=hint)


def comma_decimal(text: str) -> bool:
    """True when ``text`` reads as a number with a comma decimal point."""
    return bool(_COMMA_DECIMAL_RE.match(text))


def is_number_text(text: str) -> bool:
    return bool(_NUMBER_RE.match(text))


def _finite(number, value) -> float:
    """``number`` as a finite float; a value too large for a float (a
    400-digit integer) or not finite is refused, never raised."""
    try:
        result = float(number)
    except OverflowError:
        result = math.inf
    if not math.isfinite(result):
        raise _bad(value, "a finite number", "Give an ordinary number.")
    return result


def _whole_emu(number: float, per_unit: int, value) -> int:
    return round(_finite(number * per_unit, value))


def emu_of(value, *, default: str) -> int:
    """``value`` as whole EMUs; a bare number is in ``default``."""
    if isinstance(value, bool):
        raise _bad(value, "a length", f"Give a number or a string with a unit ({_ACCEPTED}).")
    if isinstance(value, (int, float)):
        return _whole_emu(_finite(value, value), EMU_PER_UNIT[default], value)
    if not isinstance(value, str):
        raise _bad(value, "a length", f"Give a number or a string with a unit ({_ACCEPTED}).")
    if comma_decimal(value):
        raise _bad(value, "a length", "Use a dot as the decimal separator, e.g. 2.5cm.")
    match = _LENGTH_RE.match(value)
    if not match:
        raise _bad(value, "a length", f"Give a number with one of {_ACCEPTED}, e.g. 2.5cm or 12pt.")
    number = _finite(float(match.group(1)), value)
    unit = match.group(2).lower() or default
    unit = _UNIT_ALIASES.get(unit, unit)
    if unit in _REFUSED_UNITS:
        raise _bad(value, "an absolute length", f"Relative units have no fixed size here: use {_ACCEPTED}.")
    if unit not in EMU_PER_UNIT:
        raise _bad(value, "a length", f"Unknown unit '{match.group(2)}': use {_ACCEPTED}.")
    return _whole_emu(number, EMU_PER_UNIT[unit], value)


def check_length(value, *, default: str = "pt") -> None:
    emu_of(value, default=default)


def to_emu(value, *, default: str) -> int:
    return emu_of(value, default=default)


def to_pt(value, *, default: str) -> float:
    return emu_of(value, default=default) / EMU_PER_UNIT["pt"]


def to_twips(value, *, default: str) -> int:
    return round(emu_of(value, default=default) / EMU_PER_TWIP)


def to_inches(value, *, default: str) -> float:
    return emu_of(value, default=default) / EMU_PER_UNIT["in"]


def to_cm(value, *, default: str) -> float:
    return emu_of(value, default=default) / EMU_PER_UNIT["cm"]


def to_px(value, *, default: str) -> float:
    return emu_of(value, default=default) / EMU_PER_UNIT["px"]


_MULTIPLE_RE = re.compile(rf"^\s*({_NUMBER})\s*(x|%)\s*$", re.ASCII | re.IGNORECASE)
_PERCENT = "%"


def parse_spacing(value, *, default: str = POINTS) -> Spacing:
    """``1.5x`` and ``150%`` are multiples of the line height; ``12pt`` and
    any other length are points; a bare number is ``default`` (``x`` or
    ``pt``, the key's own kind)."""
    if isinstance(value, bool):
        raise _bad(value, "a spacing", "Give a multiple such as 1.5x or a length such as 12pt.")
    if isinstance(value, (int, float)):
        number = _finite(value, value)
        return Spacing(MULTIPLE, number) if default == MULTIPLE else Spacing(POINTS, number)
    if isinstance(value, str):
        if comma_decimal(value):
            raise _bad(value, "a spacing", "Use a dot as the decimal separator, e.g. 1.5x.")
        match = _MULTIPLE_RE.match(value)
        if match:
            number = _finite(float(match.group(1)), value)
            if match.group(2) == _PERCENT:
                number /= 100
            return Spacing(MULTIPLE, number)
        if is_number_text(value):
            return parse_spacing(_finite(value, value), default=default)
        try:
            return Spacing(POINTS, to_pt(value, default=POINTS))
        except OpError:
            pass
    raise _bad(value, "a spacing", "Give a multiple such as 1.5x or 150%, or a length such as 12pt.")
