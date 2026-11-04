"""Transitions, animations, a theme kit and shape effects through
LibreOffice, the engine Collabora runs: a deck saved again by LibreOffice
keeps its transitions (a newer one as the fade it falls back to) and its
animations on the same shapes, and adds no package error the same deck
without motion does not get; the deck renders.

What these tests cannot see: PowerPoint opening the file without a repair,
Morph pairing, and how effects look in PowerPoint.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")
fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx import Presentation

import checks
import powerpoint as ppt_mod
import render_engine
import shared
from formats.pptx import pptx_animations, pptx_transitions

pytestmark = pytest.mark.office

TRANSITIONS = ("fade", "push", "cube", "drape", "morph", "wipe")
IMPRESS_PPTX = "pptx:Impress MS PowerPoint 2007 XML"


async def _same(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    monkeypatch.setattr(shared, "_resolve_path", _same)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _deck(path: Path, motion: bool) -> None:
    ops: list[dict] = [{"op": "set_theme", "kit": "ocean_depths"}]
    for n, _ in enumerate(TRANSITIONS):
        ops.append({"op": "add_slide", "layout": "title_only", "title": f"Slide {n + 1}"})
    ops += [{"op": "add_shape", "slide_index": 0, "text": f"Card {n}", "left": 1 + 4 * n, "top": 2.5, "width": 3,
             "height": 1.5, "shadow": True} for n in range(3)]
    ops.append({"op": "add_shape", "slide_index": 1, "text": "Band", "left": 1, "top": 2, "width": 11, "height": 2,
                "gradient": {"colors": ["accent1", "accent2"], "angle": 0}})
    if motion:
        ops += [{"op": "set_transition", "slide_index": n, "transition": name}
                for n, name in enumerate(TRANSITIONS)]
        ops += [{"op": "add_animation", "slide_index": 0, "shape_name": "Card 0", "effect": "fade"},
                {"op": "add_animation", "slide_index": 0, "shape_name": "Card 1", "effect": "fly",
                 "direction": "left", "trigger": "after_previous"},
                {"op": "add_animation", "slide_index": 0, "shape_name": "Card 2", "effect": "zoom",
                 "animation_type": "exit", "trigger": "on_click"}]
    out = asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "operations": ops, "create_new": True}))
    assert out.startswith("Presentation saved: "), out


def _round_trip(path: Path, tmp_path: Path) -> Path:
    out = tmp_path / f"lo-{path.stem}"
    out.mkdir()
    return Path(asyncio.run(render_engine._libreoffice_convert(str(path), IMPRESS_PPTX, str(out), extension="pptx")))


def _package_errors(path: Path) -> set[str]:
    with checks.CheckContext(str(path), "pptx", data=path.read_bytes()) as ctx:
        found = checks.run_checks(ctx, phase="document", only=["pptx.package"])
    return {f"{f.location} {f.message}" for f in found if f.severity is checks.Severity.ERROR}


def test_transitions_and_animations_survive_libreoffice(tmp_path):
    plain, moving = tmp_path / "plain.pptx", tmp_path / "moving.pptx"
    _deck(plain, motion=False)
    _deck(moving, motion=True)
    again_plain, again_moving = _round_trip(plain, tmp_path), _round_trip(moving, tmp_path)
    prs = Presentation(str(again_moving))
    kept = [pptx_transitions.read_transition(slide._element) for slide in prs.slides]
    # LibreOffice keeps the transitions it knows (cube is a p14 one),
    # saves Morph as its fade, and drops the p15 ones (drape) altogether.
    assert kept == ["fade", "push", "cube", None, "fade", "wipe"]
    first = prs.slides[0]
    # LibreOffice renumbers shape ids: compare the targets by their text.
    names = {shape.shape_id: shape.text_frame.text for shape in first.shapes if shape.has_text_frame}
    _, trees = pptx_animations.timings(first._element)
    rows = pptx_animations.rows(trees[0])
    targets = [names.get(next(iter(pptx_animations._targets(row)), None)) for row in rows]
    assert targets == ["Card 0", "Card 1", "Card 2"]
    assert [row.get("presetClass") for row in rows] == ["entr", "entr", "exit"]
    assert _package_errors(again_moving) <= _package_errors(again_plain)


def test_a_themed_deck_with_effects_renders(tmp_path):
    deck = tmp_path / "moving.pptx"
    _deck(deck, motion=True)
    out = tmp_path / "pdf"
    out.mkdir()
    pdf = fitz.open(asyncio.run(render_engine.to_pdf(str(deck), out_dir=str(out))))
    assert pdf.page_count == len(TRANSITIONS)
    assert "Card 1" in pdf[0].get_text() and "Band" in pdf[1].get_text()
    shutil.rmtree(out)
