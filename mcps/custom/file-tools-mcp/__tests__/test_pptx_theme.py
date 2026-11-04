"""set_theme: the deck's theme colours and fonts, the ten kits and how
each reads, shapes the tool drew taking text that reads on a new accent,
and what a master keeps for itself.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

import powerpoint as ppt_mod
from formats.pptx import pptx_checks, pptx_geometry, pptx_theme

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
CORPUS = Path(__file__).parent / "corpus"


async def _same(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _same)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _write(path, ops, create_new=True) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "operations": ops, "create_new": create_new}))


def _theme(path, master=0):
    prs = Presentation(str(path))
    return etree.fromstring(prs.slide_masters[master].part.part_related_by(RT.THEME).blob)


def _slot(theme, slot) -> str:
    return theme.find(f"{A}themeElements/{A}clrScheme/{A}{slot}")[0].get("val")


@pytest.mark.parametrize("kit", sorted(pptx_theme.KITS))
def test_every_kit_reads(kit):
    c = pptx_theme.KITS[kit]["colors"]
    ratio = pptx_checks.contrast_ratio
    assert ratio(c["dk1"], c["lt1"]) >= 12
    assert ratio("FFFFFF", c["accent1"]) >= 4.5 or ratio(c["lt1"], c["accent1"]) >= 4.5
    assert all(ratio(c[f"accent{n}"], c["lt1"]) >= 3 for n in range(2, 7))
    assert ratio(c["dk1"], c["lt2"]) >= 4.5
    assert set(c) == set(pptx_theme.SLOTS)


def test_a_kit_writes_the_twelve_colours_and_two_fonts(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_only", "title": "Results"},
                     {"op": "set_theme", "kit": "ocean_depths"}])
    assert out.startswith("Presentation saved: ") and "Checks: no problems found." in out, out
    theme = _theme(f)
    kit = pptx_theme.KITS["ocean_depths"]
    assert {slot: _slot(theme, slot) for slot in pptx_theme.SLOTS} == kit["colors"]
    assert theme.find(f"{A}themeElements/{A}fontScheme/{A}majorFont/{A}latin").get("typeface") == "Calibri"
    assert theme.find(f"{A}themeElements/{A}clrScheme").get("name") == "Ocean Depths"
    # The name PowerPoint's Design tab shows is the theme's own.
    assert theme.get("name") == "Ocean Depths"
    assert theme.find(f"{A}themeElements/{A}fontScheme").get("name") == "Ocean Depths"
    prs = Presentation(str(f))
    assert pptx_geometry.scheme_color(prs.slides[0], "accent1") == kit["colors"]["accent1"]


def test_colours_and_fonts_follow_into_what_names_them(tmp_path):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide", "layout": "title_only", "title": "Results"},
                     {"op": "add_textbox", "slide_index": 0, "text": "Accent", "color": "accent2", "top": 3},
                     {"op": "set_theme", "colors": {"accent2": "#E07A1F"}, "heading_font": "Cambria",
                      "body_font": "Calibri", "name": "Northwind"}])
    assert out.startswith("Presentation saved: ") and "headings Cambria (drawn here as Caladea)" in out, out
    slide = pptx_geometry.load_geometry(str(f)).slides[0]
    title = next(s for s in slide.shapes if s.placeholder and s.placeholder[0] == "title")
    box = next(s for s in slide.shapes if s.kind == "textbox")
    assert title.paragraphs[0].runs[0].font == "Cambria"
    assert box.paragraphs[0].runs[0].color == "E07A1F"
    theme = _theme(f)
    assert _slot(theme, "accent1") == "4F81BD" and theme.find(f"{A}themeElements/{A}clrScheme").get("name") == \
        "Northwind"
    assert theme.get("name") == "Northwind"


def test_colours_alone_keep_the_themes_name(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}, {"op": "set_theme", "colors": {"accent1": "#1F4E79"}}])
    assert _theme(f).get("name") == "Office Theme"


def test_a_shape_drawn_in_the_accent_takes_text_that_reads_on_the_new_one(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide", "layout": "blank"}, {"op": "add_shape", "slide_index": 0, "text": "Label"},
               {"op": "add_shape", "slide_index": 0, "text": "Own", "fill_color": "#1F4E79", "top": 3}])
    out = _write(f, [{"op": "set_theme", "colors": {"accent1": "#FFD966"}}], create_new=False)
    assert "1 shape drawn in the theme's accent took text that reads on the new colour" in out, out
    drawn, own = Presentation(str(f)).slides[0].shapes
    font_ref = drawn._element.find(f"{P}style/{A}fontRef/{A}schemeClr")
    assert font_ref.get("val") == "dk1"
    assert own._element.find(f"{P}style/{A}fontRef/{A}schemeClr").get("val") == "lt1"
    assert pptx_checks.contrast_ratio("FFD966", "000000") > 4.5


def test_a_master_that_sets_its_own_font_is_named(tmp_path):
    f = tmp_path / "t.pptx"
    _write(f, [{"op": "add_slide"}])
    prs = Presentation(str(f))
    title_style = prs.slide_masters[0]._element.find(f"{P}txStyles/{P}titleStyle")
    defrpr = title_style.find(f".//{A}defRPr")
    latin = defrpr.find(f"{A}latin")
    latin.set("typeface", "Georgia")
    prs.save(str(f))
    out = _write(f, [{"op": "set_theme", "kit": "golden_hour"}], create_new=False)
    assert "titles keep the font Georgia the master sets" in out, out


@pytest.mark.parametrize(("op", "message"), [
    ({"kit": "ocean_depth"}, "Did you mean 'ocean_depths'?"),
    ({"colors": {"accent1": "accent2"}}, "colors.accent1 names a theme slot"),
    ({}, "set_theme needs a kit, colors or a font"),
    ({"kit": "ocean_depths", "master": 2}, "master 2 does not exist"),
])
def test_a_wrong_theme_names_the_fix(tmp_path, op, message):
    f = tmp_path / "t.pptx"
    out = _write(f, [{"op": "add_slide"}, {"op": "set_theme", **op}])
    assert out.startswith("Nothing saved: ") and message in out, out


def test_a_libreoffice_deck_takes_a_kit(tmp_path):
    f = tmp_path / "lo.pptx"
    shutil.copy(CORPUS / "shared" / "libreoffice-7.4-deck.pptx", f)
    out = _write(f, [{"op": "set_theme", "kit": "forest_canopy"}], create_new=False)
    assert out.startswith("Presentation saved: ") and "ERROR pptx.package" not in out.split("Already in")[0], out
    assert _slot(_theme(f), "accent1") == pptx_theme.KITS["forest_canopy"]["colors"]["accent1"]
