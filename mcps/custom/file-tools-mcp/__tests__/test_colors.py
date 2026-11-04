"""Colours: one grammar for every format, theme slots kept for the formats
that have a theme, and a refusal that names the accepted forms."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from colors import CSS_NAMES, THEME_SLOTS, Color, parse_color
from results import Code, OpError


@pytest.mark.parametrize("value, hexcode", [
    ("#1F4E79", "1F4E79"), ("1f4e79", "1F4E79"), ("#F00", "FF0000"), ("abc", "AABBCC"),
    ("navy", "000080"), ("RebeccaPurple", "663399"), (" white ", "FFFFFF"),
    ("rgb(31, 78, 121)", "1F4E79"), ("rgb(100%, 0%, 0%)", "FF0000"), ("RGBA(0,0,255,0.5)", "0000FF"),
    ("hsl(0, 100%, 50%)", "FF0000"), ("hsl(120deg 100% 25%)", "008000"), ("hsv(240, 100%, 100%)", "0000FF"),
    ([31, 78, 121], "1F4E79"), ((255, 0, 0), "FF0000"), ([1, 1, 1], "FFFFFF"), ([0.5, 0, 0], "800000"),
    ([0, 0, 0], "000000"),
])
def test_accepted_forms(value, hexcode):
    assert parse_color(value).hex == hexcode


def test_alpha_and_argb():
    assert parse_color("#FF000080").alpha == pytest.approx(128 / 255)
    argb = parse_color("80FF0000", argb=True)
    assert argb.hex == "FF0000" and argb.argb == "80FF0000"
    assert parse_color("#1F4E79").argb == "FF1F4E79"
    assert parse_color("rgba(0, 0, 0, 0.25)").alpha == 0.25
    assert parse_color([1, 0, 0, 0.5]).alpha == 0.5


def test_fractions_for_pdf():
    assert parse_color("#FF8000").fractions == pytest.approx((1.0, 128 / 255, 0.0))


def test_theme_slots_and_their_office_names():
    for slot in THEME_SLOTS:
        assert parse_color(slot).theme == slot
    assert parse_color("Accent1").theme == "accent1"
    assert parse_color("text1").theme == "dk1" and parse_color("background2").theme == "lt2"
    assert parse_color("hyperlink").theme == "hlink" and parse_color("followedHyperlink").theme == "folHlink"
    tinted = parse_color({"theme": "accent2", "tint": 0.4})
    assert tinted == Color(theme="accent2", tint=0.4)
    assert parse_color({"theme": "dk2", "shade": 0.25}).shade == 0.25


def test_a_theme_colour_has_no_rgb_and_a_pdf_refuses_one():
    with pytest.raises(OpError, match="no fixed RGB"):
        _ = parse_color("accent1").hex
    with pytest.raises(OpError) as caught:
        parse_color("accent1", allow_theme=False)
    assert caught.value.code is Code.BAD_VALUE and "#1F4E79" in caught.value.hint
    assert parse_color("#1F4E79", allow_theme=False).hex == "1F4E79"


@pytest.mark.parametrize("value", [
    "#GGG", "#12345", "blurple", "rgb(1, 2)", "rgb(300, 0, 0)", "hsl(0, 200%, 50%)", [0.5, 128, 0],
    [1, 2], ["1", 2, 3], [True, 0, 0], 12, None, {"theme": "accent9"}, {"theme": "accent1", "tint": 2},
    {"theme": "accent1", "tint": 0.2, "shade": 0.2}, {"color": "red"}, {"theme": 1},
])
def test_refusals_name_the_accepted_forms(value):
    with pytest.raises(OpError) as caught:
        parse_color(value)
    assert caught.value.code is Code.BAD_VALUE
    assert caught.value.hint


def test_every_css_name_is_six_hex_digits():
    assert len(CSS_NAMES) == 148
    assert all(len(v) == 6 and int(v, 16) >= 0 for v in CSS_NAMES.values())


def test_a_huge_alpha_is_refused_not_raised():
    with pytest.raises(OpError):
        parse_color([0, 0, 0, 10 ** 400])


def test_a_colour_refusal_echoes_a_capped_value():
    with pytest.raises(OpError) as caught:
        parse_color("x" * 5000)
    assert len(caught.value.message) < 200
