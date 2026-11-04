"""set_style changes only what it is given: a bold on a styled cell keeps
its face, size, colour and italic; alignment, protection and borders merge
the same way; the on/off styles can be turned off again."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

openpyxl = pytest.importorskip("openpyxl")

from openpyxl.styles import Alignment, Border, Font, Protection, Side

from excel import handle_write_xlsx


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    async def _no_preview(path, filename=None):
        return None

    monkeypatch.setattr("shared._resolve_path", _ident)
    monkeypatch.setattr("shared._push_preview", _no_preview)
    monkeypatch.setattr("shared._notify_file_written", _no_preview)


def _styled_book(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    for ref in ("A1", "A2", "B1"):
        ws[ref] = ref
    ws["A1"].font = Font(name="Georgia", size=14, color="FF1F4E79", italic=True)
    ws["A2"].font = Font(name="Arial", size=9)
    ws["A1"].alignment = Alignment(horizontal="right", wrap_text=True)
    ws["A1"].protection = Protection(locked=True, hidden=True)
    thick = Side(style="thick", color="FF000000")
    ws["A1"].border = Border(top=thick, left=thick)
    wb.save(path)


def _side(border, name: str):
    """A side's style; a side the file leaves out reads as None."""
    side = getattr(border, name)
    return None if side is None else side.style


def _style(path: Path, **keys) -> openpyxl.Workbook:
    asyncio.run(handle_write_xlsx({"path": str(path), "operations": [
        {"op": "set_style", **keys}]}))
    return openpyxl.load_workbook(path)


def test_bold_keeps_face_size_colour_and_italic(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    ws = _style(f, range="A1:A2", bold=True).active
    a1, a2 = ws["A1"].font, ws["A2"].font
    assert a1.b and a1.name == "Georgia" and a1.sz == 14 and a1.i
    assert a1.color.rgb == "FF1F4E79"
    assert a2.b and a2.name == "Arial" and a2.sz == 9


def test_font_size_keeps_decimals(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    assert _style(f, range="A1", font_size=10.5).active["A1"].font.sz == 10.5


def test_font_name_clears_the_theme_scheme(tmp_path):
    f = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    wb.active["C3"] = "default"
    wb.save(f)
    assert openpyxl.load_workbook(f).active["C3"].font.scheme == "minor"
    font = _style(f, range="C3", font_name="Arial").active["C3"].font
    assert font.name == "Arial" and font.scheme is None


def test_underline_and_strikethrough_turn_off(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    on = _style(f, range="A1", underline=True, strikethrough=True).active["A1"].font
    assert on.u == "single" and on.strike
    off = _style(f, range="A1", underline=False, strikethrough=False).active["A1"].font
    assert off.u is None and not off.strike
    assert off.name == "Georgia"


def test_alignment_merges(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    al = _style(f, range="A1", alignment={"vertical": "center"}).active["A1"].alignment
    assert al.vertical == "center" and al.horizontal == "right" and al.wrap_text


def test_alignment_string_keeps_wrap(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    al = _style(f, range="A1", alignment="center").active["A1"].alignment
    assert al.horizontal == "center" and al.wrap_text


def test_protection_merges_and_false_unlocks(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    pr = _style(f, range="A1", protection={"locked": False}).active["A1"].protection
    assert pr.locked is False and pr.hidden is True
    assert _style(f, range="A2", protection=False).active["A2"].protection.locked is False


def test_border_true_draws_a_box_and_false_clears(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    box = _style(f, range="B1", border=True).active["B1"].border
    assert all(_side(box, s) == "thin" for s in ("left", "right", "top", "bottom"))
    cleared = _style(f, range="B1", border=False).active["B1"].border
    assert all(_side(cleared, s) is None for s in ("left", "right", "top", "bottom"))


def test_border_named_side_keeps_the_others(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    b = _style(f, range="A1", border={"style": "medium", "bottom": True}).active["A1"].border
    assert _side(b, "bottom") == "medium"
    assert _side(b, "top") == "thick" and _side(b, "left") == "thick"
    assert _side(b, "right") is None


def test_border_without_a_true_side_draws_all_but_the_false_ones(tmp_path):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    b = _style(f, range="B1", border={"style": "thin", "left": False}).active["B1"].border
    assert _side(b, "left") is None
    assert _side(b, "right") == _side(b, "top") == _side(b, "bottom") == "thin"


def test_large_range_shares_one_font_per_look(tmp_path):
    f = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 401):
        ws.cell(row=r, column=1, value=r)
    wb.save(f)
    out = _style(f, range="A1:A400", bold=True, font_color="C00000")
    assert out.active["A400"].font.color.rgb == "FFC00000"
    assert len(out._fonts) <= 3


@pytest.mark.parametrize("keys, bad", [
    ({"protection": {"locked": False, "lockd": True}}, "'lockd'"),
    ({"alignment": {"horizontl": "center"}}, "'horizontl'"),
])
def test_unknown_keys_inside_a_style_value_fail_the_op(tmp_path, keys, bad):
    f = tmp_path / "s.xlsx"
    _styled_book(f)
    msg = asyncio.run(handle_write_xlsx({"path": str(f), "operations": [
        {"op": "set_style", "range": "A1", **keys}]}))
    assert "unknown key '" in msg and bad.strip("'") in msg and "Nothing saved" in msg
    a1 = openpyxl.load_workbook(f).active["A1"]
    assert a1.protection.locked is True and a1.alignment.horizontal == "right"
