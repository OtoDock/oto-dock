"""Text written into a PDF in a font embedded in the file.

PyMuPDF's Base-14 fonts are not embedded and know only Latin text: Greek
came out as dots, and the glyphs any other text shows depend on the viewer.
Here every text an operation writes is drawn in a TrueType face the image
ships (``fonts``), subset to the characters written and embedded, so the
page shows the same glyphs everywhere. The Base-14 names agents send keep
working: each maps to its metric twin (Helvetica to Liberation Sans, Times
to Liberation Serif, Courier to Liberation Mono).

A subset is named after a hash of its bytes: PyMuPDF reuses whatever font a
page already holds under a name, so a name must always mean the same glyphs.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import fonts

DEFAULT_FAMILY = "Liberation Sans"
SERIF_FAMILY = "Liberation Serif"
MONO_FAMILY = "Liberation Mono"

# PyMuPDF's Base-14 short names and the PostScript names, casefolded:
# (family, bold, italic).
_BASE14: dict[str, tuple[str, bool, bool]] = {
    "helv": (DEFAULT_FAMILY, False, False), "heit": (DEFAULT_FAMILY, False, True),
    "hebo": (DEFAULT_FAMILY, True, False), "hebi": (DEFAULT_FAMILY, True, True),
    "tiro": (SERIF_FAMILY, False, False), "tiit": (SERIF_FAMILY, False, True),
    "tibo": (SERIF_FAMILY, True, False), "tibi": (SERIF_FAMILY, True, True),
    "cour": (MONO_FAMILY, False, False), "coit": (MONO_FAMILY, False, True),
    "cobo": (MONO_FAMILY, True, False), "cobi": (MONO_FAMILY, True, True),
    "helvetica": (DEFAULT_FAMILY, False, False), "helvetica-oblique": (DEFAULT_FAMILY, False, True),
    "helvetica-bold": (DEFAULT_FAMILY, True, False), "helvetica-boldoblique": (DEFAULT_FAMILY, True, True),
    "times-roman": (SERIF_FAMILY, False, False), "times-italic": (SERIF_FAMILY, False, True),
    "times-bold": (SERIF_FAMILY, True, False), "times-bolditalic": (SERIF_FAMILY, True, True),
    "times": (SERIF_FAMILY, False, False),
    "courier": (MONO_FAMILY, False, False), "courier-oblique": (MONO_FAMILY, False, True),
    "courier-bold": (MONO_FAMILY, True, False), "courier-boldoblique": (MONO_FAMILY, True, True),
}
# Kept as PyMuPDF draws them: the symbol fonts, and its CJK fonts (the only
# CJK the image can draw: it ships no CJK face).
BUILTIN = frozenset({"symb", "zadb", "symbol", "zapfdingbats", "china-s", "china-t", "japan", "korea"})
_FALLBACKS = {DEFAULT_FAMILY: fonts.SANS_FALLBACK, SERIF_FAMILY: fonts.SERIF_FALLBACK,
              MONO_FAMILY: fonts.MONO_FALLBACK}
# A space beside the text's own characters: a measured width may need it.
_ALWAYS = frozenset(" ")


def style_of(name: str | None, *, bold: bool = False, italic: bool = False) -> tuple[str, bool, bool]:
    """``(family, bold, italic)`` for a font name an operation was given: a
    Base-14 name is its twin's face, anything else a family name."""
    if not name:
        return DEFAULT_FAMILY, bold, italic
    known = _BASE14.get(str(name).strip().casefold())
    if known is not None:
        return known[0], known[1] or bold, known[2] or italic
    return str(name).strip(), bold, italic


def is_builtin(name: str | None) -> bool:
    return bool(name) and str(name).strip().casefold() in BUILTIN


def kind_of(family: str) -> str:
    """The family's kind, for the fallback chain: serif, mono or sans."""
    key = family.casefold()
    if "mono" in key or "cour" in key or "consol" in key:
        return MONO_FAMILY
    if ("serif" in key and "sans" not in key) or "times" in key or "roman" in key or "cambria" in key \
            or "georgia" in key or "garamond" in key or "caladea" in key or "palatino" in key \
            or "constantia" in key or "antiqua" in key:
        return SERIF_FAMILY
    return DEFAULT_FAMILY


@dataclass
class Chosen:
    """The face a text is drawn in, and what the agent should know about it."""

    face: fonts.Face
    warnings: list[str] = field(default_factory=list)


def _missing(text: str, family: str, bold: bool, italic: bool) -> str:
    return fonts.missing_glyphs(text, family, bold=bold, italic=italic, fallback=_FALLBACKS[kind_of(family)])


def pick_face(text: str, family: str = DEFAULT_FAMILY, *, bold: bool = False, italic: bool = False) -> Chosen:
    """The face ``text`` is drawn in when set in ``family``: the family's own
    face (or its metric twin) when it has every character, else the first
    face of its kind's fallback chain that has them all, else the one
    missing the fewest (the missing characters named in a warning). A
    family the image lacks is drawn in a face of its kind, never in
    LibreOffice's serif fallback."""
    warnings: list[str] = []
    resolved = fonts.resolve(family, bold=bold, italic=italic, fallback=_FALLBACKS[kind_of(family)])
    if resolved.face.family.casefold() != family.casefold() and not resolved.metric_compatible:
        warnings.append(f"the font '{family}' is not installed, so the text is drawn in {resolved.face.family}")
    best, best_missing = resolved.face, _missing(text, family, bold, italic)
    if not best_missing:
        return Chosen(best, warnings)
    for name in _FALLBACKS[kind_of(family)]:
        try:
            face = fonts.resolve(name, bold=bold, italic=italic).face
        except LookupError:
            continue
        if face.family.casefold() != name.casefold():
            continue
        missing = _missing(text, name, bold, italic)
        if len(missing) < len(best_missing):
            best, best_missing = face, missing
        if not missing:
            break
    if best_missing:
        shown = " ".join(f"U+{ord(c):04X}" for c in best_missing[:12])
        warnings.append(f"no installed font has these characters, so they show as blanks: {shown}")
    return Chosen(best, warnings)


@lru_cache(maxsize=32)
def _subset(path: str, index: int, chars: frozenset[str]) -> bytes:
    from fontTools import subset
    from fontTools.ttLib import TTFont

    # fontTools logs every table it drops (an FFTM timestamp table, say).
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    font = TTFont(path, fontNumber=index, recalcTimestamp=False)
    options = subset.Options()
    options.notdef_outline = True
    options.name_IDs = ["*"]
    options.hinting = False
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes={ord(c) for c in chars})
    subsetter.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


@dataclass
class Embedded:
    """A face subset and embedded: its resource name, its PDF object, the
    PyMuPDF font (widths) and its glyph ids (appearance streams)."""

    name: str
    xref: int
    data: bytes
    font: Any
    glyphs: dict[int, int]
    units_per_em: int
    ascent: float          # of the em, above the baseline
    descent: float         # of the em, below the baseline (negative)

    def width(self, text: str, size: float) -> float:
        return float(self.font.text_length(text, fontsize=size))

    def codes(self, text: str) -> str:
        """``text`` as the hex glyph codes an Identity-H font draws; a
        character the subset lacks draws as the empty glyph 0."""
        return "".join(f"{self.glyphs.get(ord(c), 0):04x}" for c in text)


def subset_bytes(face: fonts.Face, text: str) -> bytes:
    return _subset(face.path, face.index, frozenset(text) | _ALWAYS)


def embed(page: Any, face: fonts.Face, text: str) -> Embedded:
    """``face`` subset to ``text``'s characters and embedded through
    ``page`` (PyMuPDF adds the font to that page's resources)."""
    import fitz
    from fontTools.ttLib import TTFont

    data = subset_bytes(face, text)
    name = "OtoF" + hashlib.sha256(data).hexdigest()[:10]
    xref = page.insert_font(fontname=name, fontbuffer=data)
    with TTFont(io.BytesIO(data), lazy=True) as tt:
        cmap = tt.getBestCmap() or {}
        glyphs = {code: tt.getGlyphID(glyph) for code, glyph in cmap.items()}
        upem = tt["head"].unitsPerEm
        hhea = tt["hhea"]
        ascent, descent = hhea.ascent / upem, hhea.descent / upem
    return Embedded(name, xref, data, fitz.Font(fontbuffer=data), glyphs, upem, ascent, descent)


def draw(page: Any, point: tuple[float, float], text: str, *, size: float, color: tuple[float, ...],
         embedded: Embedded, morph: Any = None, opacity: float = 1.0) -> None:
    """``text`` drawn on ``page`` at ``point`` (its baseline start) in an
    embedded face."""
    import fitz

    page.insert_text(fitz.Point(*point), text, fontsize=size, fontname=embedded.name, color=color,
                     morph=morph, fill_opacity=opacity, stroke_opacity=opacity, overlay=True)
