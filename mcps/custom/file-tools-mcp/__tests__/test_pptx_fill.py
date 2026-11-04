"""write_pptx's fill_template: {{ key }} tokens filled on every slide and in
the notes, a token split across runs filled in the first run's format, a
value's new lines kept as line breaks, tokens with no value left and
reported, keys used nowhere reported, strict refusing a missing value."""

import asyncio
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Pt

import powerpoint as ppt_mod


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("shared._resolve_path", _ident)
    monkeypatch.setattr("shared._push_preview", _noop)
    monkeypatch.setattr("shared._notify_file_written", _noop)
    monkeypatch.setattr("shared._to_agents_relative", lambda p: p)


def _write(path: Path, ops: list, create_new: bool = False) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx({"path": str(path), "create_new": create_new, "operations": ops}))


def _template(path: Path) -> Path:
    out = _write(path, [
        {"op": "add_slide", "layout": "title", "title": "{{ project.name }}"},
        {"op": "set_text", "slide_index": 0, "placeholder": "subtitle", "text": "Kickoff, {{date}}"},
        {"op": "add_slide", "layout": "title_only", "title": "Who does what"},
        {"op": "add_table", "slide_index": 1, "headers": ["Role", "Name"],
         "rows": [["Lead", "{{ lead }}"], ["Sponsor", "{{sponsor}}"]], "left": 1, "top": 2, "width": 6, "height": 1.5},
        {"op": "add_textbox", "slide_index": 1, "text": "Budget: {{ budget }}", "left": 1, "top": 4.5, "width": 6,
         "height": 0.6},
        {"op": "set_speaker_notes", "slide_index": 1, "text": "Introduce {{ lead }} first."},
    ], create_new=True)
    assert out.startswith("Presentation saved: "), out
    return path


def _split_title(path: Path) -> None:
    """The first slide's title as three runs, the token split across them
    as a person's edits split it in PowerPoint, the first run bold."""
    prs = Presentation(str(path))
    paragraph = prs.slides[0].shapes.title.text_frame.paragraphs[0]
    for run in list(paragraph.runs):
        paragraph._p.remove(run._r)
    for text in ("{{ proj", "ect.na", "me }} plan"):
        run = paragraph.add_run()
        run.text = text
    paragraph.runs[0].font.bold = True
    paragraph.runs[0].font.size = Pt(40)
    prs.save(str(path))


_DATA = {"project": {"name": "Harbour works"}, "date": "3 November 2026", "lead": "Maria Pappa",
         "sponsor": "Port authority", "budget": "1.2 M EUR"}


def _texts(path: Path) -> list[str]:
    prs = Presentation(str(path))
    out = []
    for slide in prs.slides:
        for element in slide._element.iter(qn("a:p")):
            out.append("".join(t.text or "" for t in element.iter(qn("a:t"))))
        if slide.has_notes_slide:
            out.append(slide.notes_slide.notes_text_frame.text)
    return out


def test_every_token_is_filled_on_the_slides_in_tables_and_in_the_notes(tmp_path):
    deck = _template(tmp_path / "kickoff.pptx")
    out = _write(deck, [{"op": "fill_template", "data": _DATA}])
    assert out.startswith("Presentation saved: "), out
    assert "filled 6 tokens" in out, out
    texts = _texts(deck)
    assert "Harbour works" in texts and "Kickoff, 3 November 2026" in texts
    assert "Maria Pappa" in texts and "Port authority" in texts and "Budget: 1.2 M EUR" in texts
    assert "Introduce Maria Pappa first." in texts
    assert not any("{{" in t for t in texts)


def test_a_token_split_across_runs_takes_the_first_runs_format(tmp_path):
    deck = _template(tmp_path / "kickoff.pptx")
    _split_title(deck)
    out = _write(deck, [{"op": "fill_template", "data": _DATA}])
    assert out.startswith("Presentation saved: "), out
    runs = Presentation(str(deck)).slides[0].shapes.title.text_frame.paragraphs[0].runs
    assert "".join(r.text for r in runs) == "Harbour works plan"
    assert runs[0].text.startswith("Harbour works") and runs[0].font.bold and runs[0].font.size == Pt(40)


def test_new_lines_in_a_value_are_line_breaks(tmp_path):
    deck = _template(tmp_path / "kickoff.pptx")
    out = _write(deck, [{"op": "fill_template", "data": {**_DATA, "budget": "1.2 M EUR\nof which 0.2 M reserve"}}])
    assert out.startswith("Presentation saved: "), out
    box = next(s for s in Presentation(str(deck)).slides[1].shapes if s.has_text_frame and "Budget" in s.text)
    paragraph = box.text_frame.paragraphs[0]._p
    assert [etree.QName(c).localname for c in paragraph if etree.QName(c).localname in ("r", "br")] == \
        ["r", "br", "r"]
    assert box.text_frame.text == "Budget: 1.2 M EUR\x0bof which 0.2 M reserve"


def test_a_token_with_no_value_stays_and_is_reported_and_strict_refuses(tmp_path):
    deck = _template(tmp_path / "kickoff.pptx")
    before = deck.read_bytes()
    data = {k: v for k, v in _DATA.items() if k != "sponsor"} | {"unused_key": "x"}
    out = _write(deck, [{"op": "fill_template", "data": data, "strict": True}])
    assert out.startswith("Nothing saved: ") and "1 token(s) have no value: slide_index 1: {{sponsor}}" in out, out
    assert deck.read_bytes() == before
    out = _write(deck, [{"op": "fill_template", "data": data}])
    assert out.startswith("Presentation saved: "), out
    assert "1 token(s) have no value in data and stay in the deck: slide_index 1: {{sponsor}}" in out
    assert "These keys of data appear nowhere in the deck: unused_key" in out
    assert "{{sponsor}}" in _texts(deck)


def test_scope_limits_the_fill_and_other_delimiters_work(tmp_path):
    deck = _template(tmp_path / "kickoff.pptx")
    out = _write(deck, [{"op": "fill_template", "data": _DATA, "scope": "notes"}])
    assert "filled 1 token" in out and "Introduce Maria Pappa first." in _texts(deck)
    assert "{{ lead }}" in _texts(deck)
    other = _write(tmp_path / "other.pptx", [{"op": "add_slide", "layout": "title", "title": "[[ who ]] and {{who}}"},
                                             {"op": "fill_template", "data": {"who": "Ada"},
                                              "delimiters": ["[[", "]]"]}], create_new=True)
    assert other.startswith("Presentation saved: "), other
    assert "Ada and {{who}}" in _texts(tmp_path / "other.pptx")


def test_a_deck_with_no_tokens_changes_nothing(tmp_path):
    deck = tmp_path / "plain.pptx"
    _write(deck, [{"op": "add_slide", "layout": "title", "title": "Plain"}], create_new=True)
    out = _write(deck, [{"op": "fill_template", "data": {"a": "b"}}])
    assert "Changed nothing:" in out and "the deck has no tokens to fill" in out, out


def test_a_value_that_holds_a_token_is_not_filled_again(tmp_path):
    deck = tmp_path / "loop.pptx"
    _write(deck, [{"op": "add_slide", "layout": "title", "title": "{{a}}"}], create_new=True)
    out = _write(deck, [{"op": "fill_template", "data": {"a": "{{b}}", "b": "no"}}])
    assert out.startswith("Presentation saved: "), out
    assert "{{b}}" in _texts(deck)


def test_fill_template_is_in_help():
    spec = ppt_mod.CATALOG.ops["fill_template"]
    assert {k.name for k in spec.keys} == {"data", "delimiters", "strict", "scope"}
