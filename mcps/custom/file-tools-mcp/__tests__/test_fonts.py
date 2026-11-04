"""Fonts: the index read from the files' own tables and cached between
processes, the stand-ins for Office fonts, the face picked for a weight and
slant, and advance widths with per-character fallback. The office tests
check the fonts the image ships, Greek included, against what LibreOffice
actually draws."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

import fonts


def _font(path: Path, family: str, widths: dict[str, int], *, style: str = "Regular", weight: int = 400,
          italic: bool = False, upem: int = 1000) -> Path:
    glyphs = [".notdef", *(f"u{ord(c):04x}" for c in widths)]
    builder = FontBuilder(upem, isTTF=True)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({ord(c): f"u{ord(c):04x}" for c in widths})
    builder.setupGlyf({g: TTGlyphPen(None).glyph() for g in glyphs})
    builder.setupHorizontalMetrics({".notdef": (250, 0), **{f"u{ord(c):04x}": (w, 0) for c, w in widths.items()}})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.updateHead(macStyle=(0x01 if weight >= 700 else 0) | (0x02 if italic else 0))
    builder.setupNameTable({"familyName": family, "styleName": style})
    builder.setupOS2(usWeightClass=weight, fsSelection=(0x01 if italic else 0) | (0x20 if weight >= 700 else 0))
    builder.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    builder.save(str(path))
    return path


@pytest.fixture()
def font_dir(tmp_path, monkeypatch):
    """A font folder of our own, its index cached in a temp folder of our
    own, and one fallback family."""
    root = tmp_path / "fonts"
    _font(root / "carlito" / "Carlito-Regular.ttf", "Carlito", {"A": 500, "B": 600, "Ω": 700})
    _font(root / "carlito" / "Carlito-Bold.ttf", "Carlito", {"A": 550, "B": 650}, style="Bold", weight=700)
    _font(root / "carlito" / "Carlito-Italic.ttf", "Carlito", {"A": 480}, style="Italic", italic=True)
    _font(root / "caladea" / "Caladea-Regular.ttf", "Caladea", {"A": 520})
    _font(root / "fallback" / "Wide.ttf", "Wide Fallback", {"A": 900, "中": 1000}, upem=2000)
    _font(root / "fallback" / "Serif.ttf", "Wide Serif", {"Ω": 800})
    (root / "broken.ttf").write_bytes(b"not a font")
    monkeypatch.setattr(fonts, "SANS_FALLBACK", ("Wide Fallback",))
    monkeypatch.setattr(fonts, "SERIF_FALLBACK", ("Wide Serif",))
    monkeypatch.setattr(fonts.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(fonts, "_INDEX", {})
    fonts._metrics.cache_clear()
    return (str(root),)


def test_the_index_reads_the_files_and_skips_what_is_not_a_font(font_dir):
    index = fonts.faces(font_dir)
    assert sorted(index) == ["caladea", "carlito", "wide fallback", "wide serif"]
    assert sorted((f.weight, f.italic) for f in index["carlito"]) == [(400, False), (400, True), (700, False)]


def test_the_index_is_cached_between_processes(font_dir, tmp_path, monkeypatch):
    fonts.faces(font_dir)
    caches = list(tmp_path.glob("ft-fonts-*.json"))
    assert len(caches) == 1 and len(json.loads(caches[0].read_text())) == 6
    monkeypatch.setattr(fonts, "_INDEX", {})

    def no_scan(dirs):
        raise AssertionError("the cache was not used")

    monkeypatch.setattr(fonts, "build_index", no_scan)
    assert sorted(fonts.faces(font_dir)) == ["caladea", "carlito", "wide fallback", "wide serif"]


def test_a_cache_others_could_write_is_rebuilt(font_dir, tmp_path, monkeypatch):
    fonts.faces(font_dir)
    cache = next(tmp_path.glob("ft-fonts-*.json"))
    cache.write_text("[]")
    cache.chmod(0o666)
    monkeypatch.setattr(fonts, "_INDEX", {})
    assert sorted(fonts.faces(font_dir)) == ["caladea", "carlito", "wide fallback", "wide serif"]


def test_a_new_font_folder_rebuilds_the_index(font_dir, tmp_path, monkeypatch):
    fonts.faces(font_dir)
    _font(Path(font_dir[0]) / "added" / "Added.ttf", "Added Sans", {"A": 1})
    monkeypatch.setattr(fonts, "_INDEX", {})
    assert "added sans" in fonts.faces(font_dir) and len(list(tmp_path.glob("ft-fonts-*.json"))) == 2


def test_a_cached_face_outside_the_font_folders_is_never_used(font_dir, tmp_path, monkeypatch):
    fonts.faces(font_dir)
    cache = next(tmp_path.glob("ft-fonts-*.json"))
    rows = json.loads(cache.read_text())
    rows[0]["path"] = str(tmp_path / "elsewhere.ttf")
    cache.write_text(json.dumps(rows))
    monkeypatch.setattr(fonts, "_INDEX", {})
    assert all(f.path.startswith(font_dir[0]) for faces in fonts.faces(font_dir).values() for f in faces)


def test_an_office_font_gets_its_stand_in(font_dir):
    assert fonts.substitute("Calibri", font_dir) == ("Carlito", True)
    assert fonts.substitute("  CARLITO ", font_dir) == ("Carlito", True)
    assert fonts.substitute("Cambria", font_dir) == ("Caladea", True)
    assert fonts.substitute("Calibri Light", font_dir) == ("Wide Serif", False)  # Noto Serif is not here
    assert fonts.substitute("Nobody Ships This", font_dir) == ("Wide Serif", False)
    with pytest.raises(LookupError, match="no font files"):
        fonts.substitute("Calibri", (str(Path(font_dir[0]) / "missing"),))


def test_a_caller_with_its_own_fallback_keeps_only_the_metric_twins(font_dir):
    # The PDF writer chooses a face by the family's kind: it takes the
    # metric-compatible stand-ins and none of LibreOffice's other rows.
    sans = ("Wide Fallback",)
    assert fonts.substitute("Calibri", font_dir, fallback=sans) == ("Carlito", True)
    assert fonts.substitute("Calibri Light", font_dir, fallback=sans) == ("Wide Fallback", False)
    assert fonts.substitute("Nobody Ships This", font_dir, fallback=sans) == ("Wide Fallback", False)
    assert fonts.resolve("Aptos", dirs=font_dir, fallback=sans).face.family == "Wide Fallback"
    assert fonts.missing_glyphs("A中", "Aptos", dirs=font_dir, fallback=sans) == ""


def test_the_closest_face_for_weight_and_slant(font_dir):
    assert fonts.resolve("Calibri", dirs=font_dir).face.weight == 400
    assert fonts.resolve("Calibri", bold=True, dirs=font_dir).face.path.endswith("Carlito-Bold.ttf")
    assert fonts.resolve("Calibri", italic=True, dirs=font_dir).face.path.endswith("Carlito-Italic.ttf")
    # No bold italic face: the italic one, drawn bold by LibreOffice.
    assert fonts.resolve("Calibri", bold=True, italic=True, dirs=font_dir).face.path.endswith("Carlito-Italic.ttf")
    resolved = fonts.resolve("Calibri", dirs=font_dir)
    assert resolved.requested == "Calibri" and resolved.metric_compatible


def test_advance_widths(font_dir):
    assert fonts.advance_width("AB", "Calibri", 10, dirs=font_dir) == pytest.approx(11.0)
    assert fonts.advance_width("AB", "Calibri", 10, bold=True, dirs=font_dir) == pytest.approx(12.0)
    assert fonts.advance_width("A\nB\r", "Calibri", 10, dirs=font_dir) == pytest.approx(11.0)
    assert fonts.advance_width("", "Calibri", 10, dirs=font_dir) == 0
    # 中 comes from the fallback (1000 of 2000 units); ✓ from nobody: the empty box.
    assert fonts.advance_width("A中", "Calibri", 10, dirs=font_dir) == pytest.approx(5.0 + 5.0)
    assert fonts.advance_width("✓", "Calibri", 10, dirs=font_dir) == pytest.approx(2.5)


def test_a_missing_character_comes_from_a_fallback_of_the_same_kind(font_dir):
    assert fonts.face_for("A", "Cambria", dirs=font_dir).family == "Caladea"
    assert fonts.face_for("Ω", "Cambria", dirs=font_dir).family == "Wide Serif"
    assert fonts.face_for("中", "Cambria", dirs=font_dir) is None  # the serif fallback lacks it too
    assert fonts.face_for("中", "Calibri", dirs=font_dir).family == "Wide Fallback"
    assert fonts.advance_width("AΩ", "Cambria", 10, dirs=font_dir) == pytest.approx(5.2 + 8.0)


def test_missing_glyphs(font_dir):
    assert fonts.missing_glyphs("AΩ B", "Calibri", dirs=font_dir) == ""
    assert fonts.missing_glyphs("A中✓中 x", "Calibri", dirs=font_dir) == "中✓x"


# ---------------------------------------------------------------- the image

_GREEK = "Ελληνικά: Αθήνα, ΩΣΤΕ ά έ ή ί ό ύ ώ ϊ ΰ"
_COVER_GREEK = ("Carlito", "Noto Sans", "Noto Serif", "Liberation Sans", "Liberation Serif", "Liberation Mono")


@pytest.mark.office
def test_the_image_ships_the_stand_ins_and_greek_is_drawn():
    assert fonts.substitute("Calibri") == ("Carlito", True)
    assert fonts.substitute("Cambria") == ("Caladea", True)
    for family in (*_COVER_GREEK, "Caladea"):
        for bold in (False, True):
            face = fonts.resolve(family, bold=bold).face
            assert face.family == family and face.weight == (700 if bold else 400), face
    for family in _COVER_GREEK:
        assert fonts.missing_glyphs(_GREEK, family) == "", family
    # Caladea has no Greek: LibreOffice draws it in Noto Serif.
    assert fonts.missing_glyphs("Αθήνα", "Cambria") == "Αθήνα"
    assert fonts.face_for("Α", "Cambria").family == "Noto Serif"
    assert fonts.advance_width("Office", "Calibri", 11) == pytest.approx(
        fonts.advance_width("Office", "Carlito", 11))


def _pdf_family(name: str) -> str:
    return name.split("+")[-1].removesuffix("-Regular")


@pytest.mark.office
def test_libreoffice_draws_what_the_alias_table_says(tmp_path):
    """Every row of the table, and the Greek of each, against the fonts
    LibreOffice embeds in the PDF of a document set in those families."""
    docx = pytest.importorskip("docx")
    fitz = pytest.importorskip("fitz")
    import render_engine

    families = [*sorted(fonts.ALIASES), "Nobody Ships This"]
    document = docx.Document()
    for number, family in enumerate(families):
        paragraph = document.add_paragraph()
        for text in (f"Latin{number:02d} ", "Ελληνικά"):
            paragraph.add_run(text).font.name = family
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    document.save(str(tmp_path / "in" / "fonts.docx"))
    out = asyncio.run(render_engine.to_pdf(str(tmp_path / "in" / "fonts.docx"), out_dir=str(tmp_path / "out")))
    drawn = {}
    with fitz.open(out) as pdf:
        for page in pdf:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    spans = [s for s in line["spans"] if s["text"].strip()]
                    if spans and spans[0]["text"].startswith("Latin"):
                        greek = next(s for s in spans if "Ελ" in s["text"])
                        drawn[families[int(spans[0]["text"][5:7])]] = (_pdf_family(spans[0]["font"]),
                                                                        _pdf_family(greek["font"]))
    assert sorted(drawn) == sorted(families)
    wrong = {}
    for family, (latin, greek) in drawn.items():
        expected = (fonts.substitute(family)[0].replace(" ", ""), fonts.face_for("Ε", family).family.replace(" ", ""))
        if (latin, greek) != expected:
            wrong[family] = ((latin, greek), expected)
    assert not wrong, wrong
