"""Lengths and spacing: every unit an agent writes, exact EMUs, and a
refusal that names the fix."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import units
from results import Code, OpError


@pytest.mark.parametrize("value, default, emu", [
    ("1in", "pt", 914400), ("2.54cm", "pt", 914400), ("25.4mm", "pt", 914400), ("72pt", "in", 914400),
    ("6pc", "pt", 914400), ("96px", "pt", 914400), ("914400emu", "pt", 914400), ("1cm", "pt", 360000),
    ('1"', "pt", 914400), ("1 inch", "pt", 914400), ("2 inches", "pt", 1828800), ("1 CM", "pt", 360000),
    (1, "in", 914400), (12, "pt", 152400), (2.5, "cm", 900000), ("1.5", "in", 1371600),
    ("-0.25in", "pt", -228600), (".5in", "pt", 457200), ("1e1pt", "pt", 127000),
])
def test_lengths_convert_exactly(value, default, emu):
    assert units.to_emu(value, default=default) == emu


def test_the_other_units_follow_from_emus():
    assert units.to_pt("1in", default="pt") == 72
    assert units.to_twips("1in", default="pt") == 1440
    assert units.to_twips(12, default="pt") == 240
    assert units.to_inches("2.54cm", default="in") == pytest.approx(1)
    assert units.to_cm(1, default="in") == pytest.approx(2.54)
    assert units.to_px("1in", default="pt") == pytest.approx(96)


@pytest.mark.parametrize("value, fix", [
    ("2,5cm", "dot as the decimal separator"),
    ("2em", "Relative units"),
    ("50%", "Relative units"),
    ("3furlongs", "Unknown unit"),
    ("wide", "e.g. 2.5cm"),
    (True, "a number or a string"),
    (None, "a number or a string"),
    ([1], "a number or a string"),
    (float("inf"), "ordinary number"),
    ("nan", "e.g. 2.5cm"),
])
def test_bad_lengths_are_refused_with_the_fix(value, fix):
    with pytest.raises(OpError) as caught:
        units.to_emu(value, default="pt")
    assert caught.value.code is Code.BAD_VALUE
    assert fix in caught.value.message + " " + caught.value.hint


@pytest.mark.parametrize("value, default, kind, number", [
    ("1.5x", "pt", "x", 1.5), ("150%", "pt", "x", 1.5), ("2X", "pt", "x", 2.0), ("12pt", "x", "pt", 12.0),
    ("0.5in", "x", "pt", 36.0), (1.15, "x", "x", 1.15), (12, "pt", "pt", 12.0), ("1.5", "x", "x", 1.5),
    ("18", "pt", "pt", 18.0),
])
def test_spacing_reads_multiples_and_lengths(value, default, kind, number):
    spacing = units.parse_spacing(value, default=default)
    assert spacing.kind == kind and spacing.value == pytest.approx(number)


@pytest.mark.parametrize("value", ["1,5x", "double", True, None, {}])
def test_bad_spacing_is_refused(value):
    with pytest.raises(OpError) as caught:
        units.parse_spacing(value)
    assert caught.value.code is Code.BAD_VALUE


def test_number_text_is_strict_ascii():
    assert units.is_number_text(" 12.5 ") and units.is_number_text("-3e2")
    assert not units.is_number_text("1_000") and not units.is_number_text("١٢")
    assert units.comma_decimal("2,5") and units.comma_decimal("1,000") and not units.comma_decimal("2.5")


@pytest.mark.parametrize("value, default", [(10 ** 400, "pt"), (1e308, "in"), (-(10 ** 400), "cm")])
def test_a_number_too_large_for_a_float_is_refused_not_raised(value, default):
    with pytest.raises(OpError) as caught:
        units.to_emu(value, default=default)
    assert caught.value.code is Code.BAD_VALUE
    with pytest.raises(OpError):
        units.parse_spacing(10 ** 400)


def test_a_refusal_echoes_a_capped_value():
    with pytest.raises(OpError) as caught:
        units.to_emu("9" * 500 + "furlongs", default="pt")
    assert len(caught.value.message) < 200
