"""Fonts: the family LibreOffice draws in place of an Office font, the font
file behind a family, and how wide a run of text set in it is.

Font files are found by walking the font folders and reading each file's
name and OS/2 tables with fontTools, never through ``fc-match``: the answer
must not depend on fontconfig being in the process, and it has to name the
file whose widths are measured. The index is built once and cached in the
system temp folder, keyed by the font folders' modification times, because
every worker is a fresh process. Widths are each face's advance widths
summed, with no kerning and no shaping: a measure for "does this fit", not a
layout engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from functools import lru_cache

FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts")
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
REGULAR = 400
BOLD = 700
_INDEX_VERSION = 1
_ITALIC_BIT = 0x01
_BOLD_BIT = 0x20

# Office family (casefolded) -> the family the image draws for it, and
# whether the two share their advance widths (a line then breaks where
# Office breaks it). Each row is what LibreOffice put in the PDF of a
# document set in that family, in the image; a family not listed is drawn
# in the first serif fallback present, whatever its name says (LibreOffice
# 25.2, as Collabora draws it).
ALIASES: dict[str, tuple[str, bool]] = {
    "calibri": ("Carlito", True),
    "cambria": ("Caladea", True),
    "arial": ("Liberation Sans", True),
    "arial narrow": ("Liberation Sans Narrow", True),
    "times new roman": ("Liberation Serif", True),
    "courier new": ("Liberation Mono", True),
    "calibri light": ("Noto Serif", False),
    "aptos": ("Noto Serif", False),
    "aptos display": ("Noto Serif", False),
    "aptos narrow": ("Noto Serif", False),
    "segoe ui": ("Noto Serif", False),
    "trebuchet ms": ("Noto Serif", False),
    "gill sans mt": ("Noto Serif", False),
    "century gothic": ("Noto Serif", False),
    "verdana": ("Noto Serif", False),
    "tahoma": ("Noto Serif", False),
    "book antiqua": ("Noto Serif", False),
    "lucida console": ("Noto Serif", False),
    "georgia": ("Noto Serif", False),
    "garamond": ("Noto Serif", False),
    "palatino linotype": ("Noto Serif", False),
    "constantia": ("Noto Serif", False),
    "cambria math": ("FreeSerif", False),
    "consolas": ("Noto Serif", False),
    "helvetica": ("Noto Serif", False),
}
# Where a character the drawn face lacks comes from, in order, by the drawn
# face's kind (Greek in Caladea is drawn in Noto Serif); the first serif
# family present also draws a family nobody ships.
SANS_FALLBACK = ("Noto Sans", "DejaVu Sans", "Liberation Sans", "FreeSans")
SERIF_FALLBACK = ("Noto Serif", "DejaVu Serif", "Liberation Serif", "FreeSerif")
MONO_FALLBACK = ("DejaVu Sans Mono", "Liberation Mono", "FreeMono")
_SERIF_FAMILIES = frozenset({"caladea"})


@dataclass(frozen=True)
class Face:
    """One face of a font file: its family, weight (100 to 900), slant, and
    where it is (``index`` is the face's number inside a .ttc)."""

    family: str
    weight: int
    italic: bool
    path: str
    index: int = 0


@dataclass(frozen=True)
class Resolved:
    """What text set in ``requested`` is drawn with: the face, and whether
    its widths are the requested font's (the font itself or a
    metric-compatible stand-in)."""

    requested: str
    face: Face
    metric_compatible: bool


def _font_files(dirs: tuple[str, ...]) -> Iterator[str]:
    for root in dirs:
        for folder, subdirs, names in os.walk(root):
            subdirs.sort()
            for name in sorted(names):
                if name.lower().endswith(FONT_SUFFIXES):
                    yield os.path.join(folder, name)


def _index_key(dirs: tuple[str, ...]) -> str:
    stamp = [_INDEX_VERSION]
    for root in dirs:
        for folder, subdirs, _ in os.walk(root):
            subdirs.sort()
            stamp.append([folder, os.stat(folder).st_mtime_ns])
    return hashlib.sha256(json.dumps(stamp).encode()).hexdigest()[:16]


def _faces_of(path: str) -> list[Face]:
    from fontTools.ttLib import TTCollection, TTFont, TTLibError

    try:
        if path.lower().endswith(".ttc"):
            with TTCollection(path, lazy=True) as collection:
                return [face for index, font in enumerate(collection.fonts)
                        if (face := _face(font, path, index)) is not None]
        with TTFont(path, lazy=True) as font:
            face = _face(font, path, 0)
            return [face] if face is not None else []
    except (TTLibError, OSError, KeyError, ValueError, AssertionError):
        return []


def _face(font, path: str, index: int) -> Face | None:
    if "cmap" not in font or "hmtx" not in font or "name" not in font:
        return None
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1)
    if not family:
        return None
    os2 = font["OS/2"] if "OS/2" in font else None
    selection = os2.fsSelection if os2 is not None else 0
    weight = os2.usWeightClass if os2 is not None else (BOLD if selection & _BOLD_BIT else REGULAR)
    if weight < 100:
        weight *= 100
    return Face(family=family, weight=weight, italic=bool(selection & _ITALIC_BIT), path=path, index=index)


def build_index(dirs: tuple[str, ...] = FONT_DIRS) -> list[Face]:
    """Every face under ``dirs``, read from the files' own tables."""
    # fontTools warns about odd header fields of fonts Debian ships (a
    # creation date of 1970); they say nothing about the widths.
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    return [face for path in _font_files(dirs) for face in _faces_of(path)]


_LOCK = threading.Lock()
_INDEX: dict[tuple[str, ...], dict[str, list[Face]]] = {}


def faces(dirs: tuple[str, ...] = FONT_DIRS) -> dict[str, list[Face]]:
    """The faces under ``dirs`` by casefolded family, built once per
    process and cached on disk between processes."""
    with _LOCK:
        if dirs in _INDEX:
            return _INDEX[dirs]
        cache = os.path.join(tempfile.gettempdir(), f"ft-fonts-{_index_key(dirs)}.json")
        try:
            from checks import read_private_cache

            data = read_private_cache(cache)
            if data is None:
                raise ValueError(cache)
            found = [Face(**row) for row in json.loads(data)]
            # A cached face outside the font folders is never opened.
            if not all(_inside(face.path, dirs) for face in found):
                raise ValueError(cache)
        except (OSError, ValueError, TypeError):
            found = build_index(dirs)
            _write_cache(cache, found)
        by_family: dict[str, list[Face]] = {}
        for face in found:
            by_family.setdefault(face.family.casefold(), []).append(face)
        _INDEX[dirs] = by_family
        return by_family


def _inside(path: str, dirs: tuple[str, ...]) -> bool:
    real = os.path.realpath(path)
    return any(real.startswith(os.path.realpath(root) + os.sep) for root in dirs)


def _write_cache(cache: str, found: list[Face]) -> None:
    try:
        fd, temp = tempfile.mkstemp(dir=os.path.dirname(cache), prefix=".ft-fonts-", suffix=".partial")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([asdict(face) for face in found], fh)
        os.replace(temp, cache)
    except OSError:
        pass


def substitute(family: str, dirs: tuple[str, ...] = FONT_DIRS, *,
               fallback: tuple[str, ...] | None = None) -> tuple[str, bool]:
    """The family drawn for ``family`` and whether its widths are
    ``family``'s: the font itself when it is installed, else its alias,
    else the first serif fallback family present (what LibreOffice draws).

    A caller choosing a face of its own (the PDF writer) passes its
    ``fallback`` chain: it keeps only the metric-compatible aliases, since
    the others describe LibreOffice's fallback, not a choice of face."""
    index = faces(dirs)
    key = family.strip().casefold()
    if key in index:
        return index[key][0].family, True
    alias = ALIASES.get(key)
    if alias is not None and alias[0].casefold() in index and (fallback is None or alias[1]):
        return alias
    for name in SERIF_FALLBACK if fallback is None else fallback:
        if name.casefold() in index:
            return name, False
    raise LookupError(f"no font files under {', '.join(dirs)}")


def _closest(candidates: list[Face], bold: bool, italic: bool) -> Face:
    target = BOLD if bold else REGULAR
    return min(candidates, key=lambda f: (f.italic != italic, abs(f.weight - target), f.weight, f.path, f.index))


def resolve(family: str, *, bold: bool = False, italic: bool = False,
            dirs: tuple[str, ...] = FONT_DIRS, fallback: tuple[str, ...] | None = None) -> Resolved:
    """The face text set in ``family`` is drawn with (``fallback`` as in
    ``substitute``). A family without a bold or italic face gets its
    nearest face (LibreOffice draws the missing style synthetically, at
    about the same widths)."""
    drawn, compatible = substitute(family, dirs, fallback=fallback)
    return Resolved(family, _closest(faces(dirs)[drawn.casefold()], bold, italic), compatible)


class _Metrics:
    def __init__(self, face: Face) -> None:
        from fontTools.ttLib import TTFont

        with TTFont(face.path, fontNumber=face.index, lazy=True) as font:
            self.units_per_em = font["head"].unitsPerEm
            self.cmap = dict(font.getBestCmap() or {})
            self.advances = {glyph: advance for glyph, (advance, _) in font["hmtx"].metrics.items()}
            self.notdef = self.advances.get(".notdef", self.units_per_em // 2)

    def advance(self, char: str) -> int | None:
        glyph = self.cmap.get(ord(char))
        return None if glyph is None else self.advances.get(glyph, self.notdef)


@lru_cache(maxsize=64)
def _metrics(face: Face) -> _Metrics:
    return _Metrics(face)


def _fallback_names(family: str) -> tuple[str, ...]:
    key = family.casefold()
    if "mono" in key:
        return MONO_FALLBACK
    if key in _SERIF_FAMILIES or ("serif" in key and "sans" not in key):
        return SERIF_FALLBACK
    return SANS_FALLBACK


def _fallbacks(face: Face, bold: bool, italic: bool, dirs: tuple[str, ...]) -> list[Face]:
    index = faces(dirs)
    found = [_closest(index[name.casefold()], bold, italic)
             for name in _fallback_names(face.family) if name.casefold() in index]
    return [f for f in found if f != face]


def face_for(char: str, family: str, *, bold: bool = False, italic: bool = False,
             dirs: tuple[str, ...] = FONT_DIRS) -> Face | None:
    """The face ``char`` is drawn with when set in ``family``: the drawn
    face, else the first fallback of its kind that has the character
    (``None`` when nobody has it: it shows as an empty box)."""
    face = resolve(family, bold=bold, italic=italic, dirs=dirs).face
    if _metrics(face).advance(char) is not None:
        return face
    return next((f for f in _fallbacks(face, bold, italic, dirs) if _metrics(f).advance(char) is not None), None)


def advance_width(text: str, family: str, size_pt: float, *, bold: bool = False, italic: bool = False,
                  dirs: tuple[str, ...] = FONT_DIRS) -> float:
    """The width in points of ``text`` set on one line in ``family`` at
    ``size_pt``. A character the drawn face lacks is measured in the face
    ``face_for`` names, as LibreOffice draws it from another font; one
    nobody has counts as the face's empty box."""
    face = resolve(family, bold=bold, italic=italic, dirs=dirs).face
    main = _metrics(face)
    others: list[_Metrics] | None = None
    total = 0.0
    for char in text:
        if char in "\n\r":
            continue
        advance = main.advance(char)
        if advance is not None:
            total += advance / main.units_per_em
            continue
        if others is None:
            others = [_metrics(f) for f in _fallbacks(face, bold, italic, dirs)]
        found = next(((m.advance(char), m.units_per_em) for m in others if m.advance(char) is not None), None)
        total += found[0] / found[1] if found else main.notdef / main.units_per_em
    return total * size_pt


def missing_glyphs(text: str, family: str, *, bold: bool = False, italic: bool = False,
                   dirs: tuple[str, ...] = FONT_DIRS, fallback: tuple[str, ...] | None = None) -> str:
    """The characters of ``text`` the face drawn for ``family`` (``fallback``
    as in ``substitute``) has no glyph for (each once, in order): those come
    from another font, or as boxes."""
    main = _metrics(resolve(family, bold=bold, italic=italic, dirs=dirs, fallback=fallback).face)
    return "".join(dict.fromkeys(c for c in text if not c.isspace() and main.advance(c) is None))
