"""write_pptx and read_document on decks other editors made (the files in
``corpus/pptx``): nothing personal in them, every old layout word gives a
slide, slide numbers reopen right with no clashing placeholder, an edit
leaves the other slides as they were, macros stay with a macro deck.

Needs python-pptx; skips where it's absent.
"""

import asyncio
import getpass
import re
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxml import etree
from pptx.oxml.ns import qn

import powerpoint as ppt_mod
from formats.pptx import pptx_deck, pptx_fields
from formats.pptx.pptx_read import read_deck

CORPUS = Path(__file__).parent / "corpus" / "pptx"
DECKS = sorted(p for p in CORPUS.iterdir() if p.suffix.lower() in pptx_deck._MAIN_TYPE_BY_EXT)
MACRO_DECKS = [p for p in DECKS if p.suffix.lower() in (".pptm", ".potm", ".ppsm")]


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


def _clean(out: str) -> bool:
    """Saved, with no failed operation and no warning."""
    return out.startswith("Presentation saved: ") and "Failed:" not in out and "Warnings:" not in out

def _edit(path: Path, ops: list[dict]) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx(
        {"path": str(path), "create_new": False, "operations": ops}))


def _copy(deck: Path, tmp_path: Path, name: str | None = None) -> Path:
    target = tmp_path / (name or deck.name)
    shutil.copyfile(deck, target)
    return target


def test_the_corpus_is_there():
    assert {d.name for d in DECKS} >= {"libreoffice-7.4-numbered.pptx", "macro-package.pptm"}


def _login() -> bytes | None:
    try:
        name = getpass.getuser()
    except (KeyError, OSError):  # a container user with no passwd entry
        return None
    return name.encode() if len(name) >= 5 else None


# Whoever made a file leaves an address, a home folder or a login name in
# its XML; the login of the machine running the tests is checked too.
_PERSONAL = [rb"[\w.+-]+@[\w-]+\.[a-z]{2,}", rb"/home/", rb"/Users/", rb"[A-Za-z]:\\Users\\"]
if _login():
    _PERSONAL.append(re.escape(_login()))


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_nothing_personal_in_a_corpus_file(deck):
    z = zipfile.ZipFile(deck)
    for name in z.namelist():
        if not name.endswith((".xml", ".rels")):
            continue
        data = z.read(name)
        for pattern in _PERSONAL:
            assert not re.search(pattern, data), (name, pattern)
    core = z.read("docProps/core.xml").decode()
    for field in ("creator", "lastModifiedBy"):
        assert re.search(rf"<(?:dc|cp):{field}>\s*</(?:dc|cp):{field}>|<(?:dc|cp):{field}/>", core) \
            or f":{field}" not in core, field


def _explicit_idx_clash(path: Path) -> list[tuple[int, str]]:
    clashes = []
    for i, slide in enumerate(pptx_deck.open_presentation(str(path)).slides):
        seen = [ph.get("idx") for ph in slide.shapes._spTree.iter(qn("p:ph")) if ph.get("idx")]
        clashes += [(i, v) for v in set(seen) if seen.count(v) > 1]
    return clashes


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_every_old_layout_word_gives_a_slide(tmp_path, deck):
    f = _copy(deck, tmp_path)
    before = len(pptx_deck.open_presentation(str(f)).slides)
    words = sorted(pptx_deck._LEGACY_TYPES)
    out = _edit(f, [{"op": "add_slide", "layout": w.replace(" ", "_")} for w in words])
    assert "no layout named" not in out, out
    assert len(pptx_deck.open_presentation(str(f)).slides) == before + len(words)
    assert _explicit_idx_clash(f) == []


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_the_read_lists_every_layout(deck):
    out = read_deck(str(deck))
    entries = pptx_deck.list_layouts(pptx_deck.open_presentation(str(deck)))
    shown = min(len(entries), pptx_deck._LIST_CAP)
    assert out.count("\n  ") >= shown
    assert "**Layouts**" in out


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_numbers_reopen_right_without_clashing_placeholders(tmp_path, deck):
    f = _copy(deck, tmp_path)
    out = _edit(f, [{"op": "set_slide_number"}, {"op": "move_slide", "from_index": -1, "to_index": 0}])
    assert _clean(out), out
    prs = pptx_deck.open_presentation(str(f))
    first = pptx_fields.first_number(prs)
    for position, slide in enumerate(prs.slides):
        fields = [fl for sp in pptx_fields._number_placeholders(slide.shapes._spTree)
                  for fl in pptx_fields._fields(sp)]
        assert [fl.find(qn("a:t")).text for fl in fields] == [str(first + position)], position
    assert _explicit_idx_clash(f) == []


def _c14n(el) -> bytes:
    return etree.tostring(el, method="c14n")


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_an_edit_leaves_the_other_slides_as_they_were(tmp_path, deck):
    f = _copy(deck, tmp_path)
    before = [_c14n(s._element) for s in pptx_deck.open_presentation(str(f)).slides]
    out = _edit(f, [{"op": "add_textbox", "slide_index": 0, "text": "Ένα πλαίσιο", "top": 6}])
    assert _clean(out), out
    after = [_c14n(s._element) for s in pptx_deck.open_presentation(str(f)).slides]
    assert after[0] != before[0]
    assert after[1:] == before[1:]


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_a_corpus_deck_saves_as_a_template_and_back(tmp_path, deck):
    template = _copy(deck, tmp_path, "brand.potm" if deck in MACRO_DECKS else "brand.potx")
    out = _edit(template, [{"op": "add_slide", "layout": "title_only"}])
    assert _clean(out), out
    shutil.copyfile(template, tmp_path / "deck.pptm")
    _edit(tmp_path / "deck.pptm", [{"op": "add_slide"}])
    slides = len(pptx_deck.open_presentation(str(tmp_path / "deck.pptm")).slides)
    assert slides == len(pptx_deck.open_presentation(str(deck)).slides) + 2


def _project(path: Path) -> bytes | None:
    z = zipfile.ZipFile(path)
    names = [n for n in z.namelist() if n.lower().endswith("vbaproject.bin")]
    return z.read(names[0]) if names else None


@pytest.mark.parametrize("deck", MACRO_DECKS, ids=lambda d: d.name)
def test_a_macro_deck_keeps_its_project_and_a_pptx_name_drops_it(tmp_path, deck):
    project = _project(deck)
    assert project
    kept = _copy(deck, tmp_path)
    out = _edit(kept, [{"op": "add_slide", "layout": "title_only"}])
    assert _clean(out), out
    assert _project(kept) == project
    assert 'Extension="bin"' in zipfile.ZipFile(kept).read("[Content_Types].xml").decode()
    dropped = _copy(deck, tmp_path, "plain.pptx")
    out = _edit(dropped, [{"op": "add_slide"}])
    assert "the macros were removed" in out
    assert _project(dropped) is None


_LIBREOFFICE = CORPUS / "libreoffice-7.4-numbered.pptx"


def test_libreoffice_title_slides_are_recognised():
    prs = pptx_deck.open_presentation(str(_LIBREOFFICE))
    assert [pptx_fields.is_title_slide(s) for s in prs.slides] == [True, False, False, False, False]


def test_libreoffice_deck_keeps_its_title_slide_apart(tmp_path):
    f = _copy(_LIBREOFFICE, tmp_path)
    out = _edit(f, [{"op": "add_slide", "layout": "title"},
                    {"op": "add_slide", "layout": "title_and_content"},
                    {"op": "set_slide_number", "skip_title_slide": True}])
    assert _clean(out) and "Notes:" in out and "no 'title' layout" in out
    prs = pptx_deck.open_presentation(str(f))
    numbered = [pptx_fields.has_number(s) for s in prs.slides]
    assert numbered == [False, True, True, True, True, False, True]


def test_the_title_word_wants_a_title_beside_the_subtitle():
    from pptx.enum.shapes import PP_PLACEHOLDER as P

    subtitle_only = pptx_deck.LayoutEntry(0, "centred text", 1, "Centered Text", "objOnly",
                                          ((P.SUBTITLE, 0),))
    title_pair = pptx_deck.LayoutEntry(1, "title slide", 1, "Title Slide", "tx",
                                       ((P.TITLE, None), (P.SUBTITLE, None)))
    assert pptx_deck._by_placeholders([subtitle_only, title_pair], "title") is title_pair


def test_placeholder_words_find_libreoffice_placeholders_that_have_no_number(tmp_path):
    f = _copy(_LIBREOFFICE, tmp_path)
    out = _edit(f, [{"op": "add_slide", "layout": "title_and_content"},
                    {"op": "set_text", "slide_index": -1, "text": "Τίτλος"},
                    {"op": "add_bullet_points", "slide_index": -1, "items": ["Ένα", "Δύο"]},
                    {"op": "set_text", "slide_index": 0, "placeholder": "subtitle", "text": "Υπότιτλος"}])
    assert _clean(out), out
    prs = pptx_deck.open_presentation(str(f))
    added = prs.slides[-1]
    texts = [s.text_frame.text for s in added.placeholders if s.has_text_frame]
    assert texts[:2] == ["Τίτλος", "Ένα\nΔύο"]
    assert [s.text_frame.text for s in prs.slides[0].placeholders][1] == "Υπότιτλος"
