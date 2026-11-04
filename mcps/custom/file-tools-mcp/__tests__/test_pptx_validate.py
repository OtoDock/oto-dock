"""Every write_pptx operation saves a package Office opens: each example of
each operation (and richer batches) goes through the real handler, the
result re-opens with python-pptx, and ``ooxml.validate.validate_package``
finds nothing: relationships, content types, ids and schema order.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

import powerpoint as ppt_mod
from checks import CheckContext, Severity
from formats.pptx.pptx_deck import open_presentation
from ooxml import validate

CORPUS = Path(__file__).parent / "corpus" / "pptx"


async def _async_ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _bypass_platform(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("shared._resolve_path", _async_ident)
    monkeypatch.setattr("shared._push_preview", _noop)
    monkeypatch.setattr("shared._notify_file_written", _noop)
    monkeypatch.setattr("shared._to_agents_relative", lambda p: p)


def _write(path: Path, ops: list, create_new: bool = False) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "create_new": create_new, "operations": ops}))


def _problems(path: Path) -> list[str]:
    open_presentation(str(path))  # re-opens with the library (python-pptx refuses a template by name)
    with CheckContext(str(path), "pptx", data=path.read_bytes()) as ctx:
        findings = validate.validate_package(ctx)
    return [f"{f.severity.name} {f.location}: {f.message}" for f in findings if f.severity is not Severity.INFO]


def _examples() -> list:
    return [pytest.param(spec.name, n, example, id=f"{spec.name}-{n}")
            for spec in ppt_mod.CATALOG.ops.values() for n, example in enumerate(spec.examples)]


@pytest.mark.parametrize("name, number, example", _examples())
def test_each_operation_saves_a_valid_package(tmp_path, name, number, example):
    fixtures = ppt_mod.contract_fixtures(str(tmp_path))
    deck = tmp_path / "deck.pptx"
    ppt_mod.contract_job(str(tmp_path)).prs.save(str(deck))
    spec = ppt_mod.CATALOG.ops[name]
    op = {k: fixtures.get(v, v) if isinstance(v, str) else v for k, v in example.items()}
    out = _write(deck, [*spec.setup, op])
    assert out.startswith("Presentation saved: ") and "Failed:" not in out, out
    assert "pptx.package" not in out and "pptx.parts_lost" not in out, out
    assert _problems(deck) == []


_RICH = [
    {"op": "add_slide", "layout": "title", "title": "Board"},
    {"op": "set_text", "slide_index": 0, "placeholder": "subtitle", "text": "Q3\nΤρίτο τρίμηνο", "lang": "el-GR",
     "line_spacing": "1.1x", "space_after": 4, "autofit": "shrink", "insets": {"left": 0.1}},
    {"op": "set_background", "slide_index": 0, "gradient": [{"color": "accent1"}, {"color": "#FFFFFF"}]},
    {"op": "set_transition", "slide_index": 0, "transition": "push", "advance_after": 2000},
    {"op": "add_slide", "layout": "title_and_content", "title": "Points"},
    {"op": "add_bullet_points", "slide_index": 1, "items": ["One", {"text": "Two", "level": 2}], "bullet": "number"},
    {"op": "add_textbox", "slide_index": 1, "text": "Note", "bullet": "–", "letter_spacing": 1, "top": 6,
     "vertical_anchor": "bottom"},
    {"op": "add_shape", "slide_index": 1, "text": "A", "left": 8, "top": 2, "line_width": 0},
    {"op": "add_shape", "slide_index": 1, "text": "B", "left": 10, "top": 2, "fill_color": {"theme": "accent2",
                                                                                           "shade": 0.2}},
    {"op": "add_group_shape", "slide_index": 1, "shapes": ["A", "B"], "name": "Pair"},
    {"op": "add_connector", "slide_index": 1, "arrow_start": True, "arrow_end": True, "line_color": "dk1"},
    {"op": "add_freeform", "slide_index": 1, "points": [[1, 6], [2, 6], [1.5, 7]]},
    {"op": "add_table", "slide_index": 1, "headers": ["a", "b"], "rows": [[1, 2], [3, None]], "top": 4},
    {"op": "format_table_cell", "slide_index": 1, "table_index": -1, "row": -1, "col": -1, "fill_color": "red",
     "vertical_alignment": "middle", "merge_to": {"row": -1, "col": 0}},
    {"op": "add_chart", "slide_index": 1, "chart_type": "line_markers", "categories": ["a", "b"],
     "series": [{"name": "s", "values": [1, 2], "color": "accent3"}], "left": 6, "top": 4, "width": 4, "height": 3},
    {"op": "set_speaker_notes", "slide_index": 1, "text": "Notes"},
    {"op": "set_slide_number", "skip_title_slide": True},
    {"op": "duplicate_slide", "slide_index": 1},
    {"op": "move_slide", "from_index": -1, "to_index": 1},
    {"op": "delete_slide", "slide_index": -1},
    {"op": "set_slide_dimensions", "width": 10, "height": 7.5},
]


@pytest.mark.parametrize("suffix", [".pptx", ".potx", ".pptm"])
def test_a_rich_batch_saves_a_valid_package_of_each_kind(tmp_path, suffix):
    deck = tmp_path / f"board{suffix}"
    out = _write(deck, _RICH, create_new=True)
    assert out.startswith("Presentation saved: ") and "Failed:" not in out, out
    assert _problems(deck) == []


@pytest.mark.parametrize("source", sorted(CORPUS.glob("*.ppt*")), ids=lambda p: p.name)
def test_editing_a_corpus_deck_keeps_it_valid(tmp_path, source):
    deck = tmp_path / source.name
    shutil.copyfile(source, deck)
    out = _write(deck, [{"op": "add_slide", "layout": "title_and_content", "title": "Added"},
                        {"op": "add_bullet_points", "slide_index": -1, "items": ["x", "y"]},
                        {"op": "duplicate_slide", "slide_index": 0},
                        {"op": "set_slide_number"}, {"op": "delete_slide", "slide_index": 1}])
    assert out.startswith("Presentation saved: ") and "Failed:" not in out, out
    already = _problems(source)
    assert [p for p in _problems(deck) if p not in already] == []
    new = out.split("Already in the file before this edit:", 1)[0]
    assert "pptx.package" not in new and "pptx.parts_lost" not in new, out
