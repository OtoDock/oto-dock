"""Bulleted and numbered lists with real numbering.

Each kind (bullet, number) gets one nine-level definition per document,
made on first use. Every list an op writes is its own instance of that
definition, with every level's start overridden, so two numbered lists each
count from 1 (paragraphs sharing a style's single numbering otherwise count
on across the whole document). List paragraphs keep the document's list
style for their fonts and spacing; the numbering sets the marker and the
indent at every level.
"""

import secrets
from dataclasses import dataclass, field
from typing import Any

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NSDECL = f'xmlns:w="{_W_NS}"'

LEVELS = 9
INDENT_STEP_TWIPS = 360
HANGING_TWIPS = 360

BULLET = "bullet"
NUMBER = "number"

DEFINITION_NAMES = {BULLET: "Bullet list", NUMBER: "Numbered list"}
_FAMILY_STYLES = {BULLET: "List Bullet", NUMBER: "List Number"}
_FALLBACK_STYLE = "List Paragraph"

# Word's own default bullets: Symbol's dot, Courier New's "o", Wingdings'
# square. Every Word and LibreOffice build maps these three.
_BULLET_LEVELS = (("\uf0b7", "Symbol"), ("o", "Courier New"), ("\uf0a7", "Wingdings"))
_NUMBER_FORMATS = ("decimal", "lowerLetter", "lowerRoman")

_NUMBERING_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"


def _w(tag: str) -> str:
    return f"{{{_W_NS}}}{tag}"


def _parse(xml: str) -> Any:
    from docx.oxml import parse_xml

    return parse_xml(xml)


def _hex_id() -> str:
    return secrets.token_hex(4).upper()


_FORMAT_NAMES = {
    "decimal": "decimal", "1": "decimal", "lowerletter": "lowerLetter", "lower_letter": "lowerLetter",
    "a": "lowerLetter", "upperletter": "upperLetter", "upper_letter": "upperLetter",
    "lowerroman": "lowerRoman", "lower_roman": "lowerRoman", "i": "lowerRoman",
    "upperroman": "upperRoman", "upper_roman": "upperRoman", "decimalzero": "decimalZero",
    "decimal_zero": "decimalZero", "01": "decimalZero",
}
_CASED = {"A": "upperLetter", "I": "upperRoman"}
FORMATS = ("decimal", "lowerLetter", "upperLetter", "lowerRoman", "upperRoman", "decimalZero")


def number_format(value: Any) -> str:
    """Word's numFmt for ``value`` (decimal, lower_roman, "a", "I"...), or a
    failure naming the choices."""
    from results import Code, OpError

    text = str(value).strip()
    fmt = _CASED.get(text) or _FORMAT_NAMES.get(text.lower())
    if fmt is None:
        raise OpError(Code.BAD_VALUE, f"number_format '{value}' is not one of: {', '.join(FORMATS)}",
                      hint="Give decimal, lowerLetter, upperLetter, lowerRoman, upperRoman or decimalZero.")
    return fmt


def _level_xml(kind: str, ilvl: int, first_format: str | None = None) -> str:
    left = INDENT_STEP_TWIPS * (ilvl + 1)
    if kind == BULLET:
        char, font = _BULLET_LEVELS[ilvl % len(_BULLET_LEVELS)]
        return (
            f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
            f'<w:lvlText w:val="{char}"/><w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{left}" w:hanging="{HANGING_TWIPS}"/></w:pPr>'
            f'<w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:hint="default"/></w:rPr>'
            f"</w:lvl>"
        )
    fmt = first_format if (first_format and ilvl == 0) else _NUMBER_FORMATS[ilvl % len(_NUMBER_FORMATS)]
    return (
        f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
        f'<w:lvlText w:val="%{ilvl + 1}."/><w:lvlJc w:val="left"/>'
        f'<w:pPr><w:ind w:left="{left}" w:hanging="{HANGING_TWIPS}"/></w:pPr>'
        f"</w:lvl>"
    )


def _numbering_element(doc: Any) -> Any:
    """The document's ``w:numbering``, made when the document has none."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.parts.numbering import NumberingPart

    part = doc.part
    try:
        return part.part_related_by(RT.NUMBERING).element
    except KeyError:
        pass
    taken = {str(p.partname) for p in part.package.iter_parts()}
    name = "/word/numbering.xml"
    if name in taken:
        name = str(part.package.next_partname("/word/numbering%d.xml"))
    element = _parse(f"<w:numbering {_NSDECL}/>")
    numbering = NumberingPart(PackURI(name), _NUMBERING_CONTENT_TYPE, element, part.package)
    part.relate_to(numbering, RT.NUMBERING)
    return element


def _max_int_attr(elements: Any, attr: str) -> int:
    best = 0
    for element in elements:
        try:
            best = max(best, int(element.get(attr)))
        except (TypeError, ValueError):
            continue
    return best


def _definition_name(kind: str, first_format: str | None) -> str:
    return DEFINITION_NAMES[kind] if first_format is None else f"{DEFINITION_NAMES[kind]} ({first_format})"


def _is_ours(abstract: Any, kind: str, first_format: str | None = None) -> bool:
    """A definition this module made for ``kind``: its name, nine levels and
    the kind's first format. Another tool's definition that only shares the
    name is left alone."""
    name = abstract.find(_w("name"))
    if name is None or name.get(_w("val")) != _definition_name(kind, first_format):
        return False
    levels = abstract.findall(_w("lvl"))
    if len(levels) != LEVELS:
        return False
    fmt = levels[0].find(_w("numFmt"))
    first = "bullet" if kind == BULLET else (first_format or _NUMBER_FORMATS[0])
    return fmt is not None and fmt.get(_w("val")) == first


@dataclass
class Lists:
    """List numbering for one write: the definitions, and the last list of
    each kind (what ``continue`` carries on)."""

    doc: Any
    _numbering: Any = None
    _definitions: dict[str, str] = field(default_factory=dict)
    _last_num: dict[str, str] = field(default_factory=dict)
    _style_names: set[str] | None = None

    @property
    def numbering(self) -> Any:
        if self._numbering is None:
            self._numbering = _numbering_element(self.doc)
        return self._numbering

    def _definition_id(self, kind: str, first_format: str | None = None) -> str:
        key = f"{kind}:{first_format}"
        if key in self._definitions:
            return self._definitions[key]
        for abstract in self.numbering.findall(_w("abstractNum")):
            if _is_ours(abstract, kind, first_format):
                self._definitions[key] = abstract.get(_w("abstractNumId"))
                return self._definitions[key]
        levels = "".join(_level_xml(kind, i, first_format) for i in range(LEVELS))
        abstract_id = self._add_definition(_definition_name(kind, first_format), levels)
        self._definitions[key] = abstract_id
        return abstract_id

    def _add_definition(self, name: str, levels: str) -> str:
        abstract_id = str(_max_int_attr(
            self.numbering.findall(_w("abstractNum")), _w("abstractNumId")) + 1)
        abstract = _parse(
            f'<w:abstractNum {_NSDECL} w:abstractNumId="{abstract_id}">'
            f'<w:nsid w:val="{_hex_id()}"/><w:multiLevelType w:val="hybridMultilevel"/>'
            f'<w:tmpl w:val="{_hex_id()}"/><w:name w:val="{name}"/>{levels}</w:abstractNum>'
        )
        # Every w:abstractNum precedes every w:num in w:numbering.
        first_num = self.numbering.find(_w("num"))
        if first_num is not None:
            first_num.addprevious(abstract)
        else:
            cleanup = self.numbering.find(_w("numIdMacAtCleanup"))
            if cleanup is not None:
                cleanup.addprevious(abstract)
            else:
                self.numbering.append(abstract)
        return abstract_id

    def _next_num_id(self) -> int:
        # Above every numId anywhere: a paragraph pointing at a missing
        # numId would otherwise join a list that reuses the gap.
        best = _max_int_attr(self.numbering.findall(_w("num")), _w("numId"))
        for part in self.doc.part.package.iter_parts():
            element = getattr(part, "element", None)
            if element is not None:
                best = max(best, _max_int_attr(element.iter(_w("numId")), _w("val")))
        return best + 1

    def _new_num(self, kind: str, *, start: int | None = None, first_format: str | None = None) -> str:
        abstract_id = self._definition_id(kind, first_format)
        num_id = str(self._next_num_id())
        overrides = "".join(
            f'<w:lvlOverride w:ilvl="{i}"><w:startOverride w:val="{start if i == 0 and start is not None else 1}"/>'
            f"</w:lvlOverride>"
            for i in range(LEVELS)
        )
        num = _parse(
            f'<w:num {_NSDECL} w:numId="{num_id}">'
            f'<w:abstractNumId w:val="{abstract_id}"/>{overrides}</w:num>'
        )
        cleanup = self.numbering.find(_w("numIdMacAtCleanup"))
        if cleanup is not None:
            cleanup.addprevious(num)
        else:
            self.numbering.append(num)
        return num_id

    def _previous_num(self, kind: str) -> str | None:
        if kind in self._last_num:
            return self._last_num[kind]
        abstract_id = self._definition_id(kind)
        ours = {
            num.get(_w("numId"))
            for num in self.numbering.findall(_w("num"))
            if (ref := num.find(_w("abstractNumId"))) is not None
            and ref.get(_w("val")) == abstract_id
        }
        found = None
        for num_id in self.doc.element.body.iter(_w("numId")):
            if num_id.get(_w("val")) in ours:
                found = num_id.get(_w("val"))
        return found

    def start_list(self, kind: str, *, carry_on: bool = False, start: int | None = None,
                   number_format: str | None = None) -> str:
        """The numId the next list of ``kind`` writes its items with: the
        previous list's when ``carry_on``, else a new list that starts at
        ``start`` (1) with ``number_format`` on its first level."""
        num_id = self._previous_num(kind) if carry_on and start is None and number_format is None else None
        if num_id is None:
            num_id = self._new_num(kind, start=start, first_format=number_format)
        self._last_num[kind] = num_id
        return num_id

    def number_headings(self, style_ids: list[str], *, nested: bool, off: bool) -> bool:
        """Number heading styles 1, 1.1, 1.1.1 (or each level on its own), or
        take their numbers away; returns whether anything changed."""
        styles = self.doc.styles.element
        changed = False
        if off:
            for style_id in style_ids:
                ppr = styles.get_by_id(style_id).find(_w("pPr"))
                num_pr = ppr.find(_w("numPr")) if ppr is not None else None
                if num_pr is not None:
                    ppr.remove(num_pr)
                    changed = True
            return changed
        levels = []
        for i in range(LEVELS):
            text = ".".join(f"%{n + 1}" for n in range(i + 1)) if nested else f"%{i + 1}"
            text = text + "." if (i == 0 or not nested) else text
            indent = 432 + 144 * i
            pstyle = f'<w:pStyle w:val="{style_ids[i]}"/>' if i < len(style_ids) else ""
            levels.append(f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="decimal"/>{pstyle}'
                          f'<w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/>'
                          f'<w:pPr><w:ind w:left="{indent}" w:hanging="{indent}"/></w:pPr></w:lvl>')
        abstract_id = self._add_definition(f"Heading numbers ({'nested' if nested else 'simple'})", "".join(levels))
        num_id = str(self._next_num_id())
        num = _parse(f'<w:num {_NSDECL} w:numId="{num_id}"><w:abstractNumId w:val="{abstract_id}"/></w:num>')
        cleanup = self.numbering.find(_w("numIdMacAtCleanup"))
        if cleanup is not None:
            cleanup.addprevious(num)
        else:
            self.numbering.append(num)
        for i, style_id in enumerate(style_ids):
            style = styles.get_by_id(style_id)
            num_pr = style.get_or_add_pPr().get_or_add_numPr()
            num_pr.get_or_add_ilvl().val = i
            num_pr.get_or_add_numId().val = int(num_id)
        return True

    def item_style(self, kind: str) -> str | None:
        """The style every item of a ``kind`` list takes, if the document has
        one. One style for all levels: contextual spacing (no space between
        list items) holds only between paragraphs of the same style."""
        if self._style_names is None:
            from docx.enum.style import WD_STYLE_TYPE

            self._style_names = {
                s.name for s in self.doc.styles if s.type == WD_STYLE_TYPE.PARAGRAPH
            }
        for name in (_FAMILY_STYLES[kind], _FALLBACK_STYLE):
            if name in self._style_names:
                return name
        return None

    def add_item(self, text: str, kind: str, level: int, num_id: str, *, paragraph: Any = None) -> Any:
        """One list paragraph (``level`` 1-based): ``paragraph`` filled, or a new
        one at the end of the body."""
        if paragraph is None:
            paragraph = self.doc.add_paragraph()
        paragraph.add_run(text)
        style = self.item_style(kind)
        if style is not None:
            paragraph.style = style
        num_pr = paragraph._p.get_or_add_pPr().get_or_add_numPr()
        num_pr.get_or_add_ilvl().val = level - 1
        num_pr.get_or_add_numId().val = int(num_id)
        return paragraph


_ROMAN = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
          (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))
_PRIVATE_USE = range(0xE000, 0xF900)


def _roman(n: int) -> str:
    out = []
    for value, letters in _ROMAN:
        while n >= value:
            out.append(letters)
            n -= value
    return "".join(out)


def _letters(n: int) -> str:
    # Word repeats the letter: a..z, then aa..zz.
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


# Past these a letter or roman label is longer than any real list's and is
# shown as a number instead (a file can set a start of two billion).
_MAX_LETTER = 26 * 4
_MAX_ROMAN = 3999


def format_number(n: int, fmt: str) -> str:
    if fmt in ("lowerLetter", "upperLetter") and not 1 <= n <= _MAX_LETTER:
        return str(n)
    if fmt in ("lowerRoman", "upperRoman") and not 1 <= n <= _MAX_ROMAN:
        return str(n)
    if fmt == "lowerLetter":
        return _letters(n)
    if fmt == "upperLetter":
        return _letters(n).upper()
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt == "none":
        return ""
    return str(n)


@dataclass
class _Level:
    fmt: str
    text: str
    start: int


class ListMarkers:
    """The marker each list paragraph shows (``1.``, ``a.``, ``•``), counted
    in document order. Call :meth:`marker` once per paragraph, in order.

    Counters belong to the list instance (``w:num``) when it overrides its
    start, else to the definition it shares with other instances: Word
    continues such lists across instances.
    """

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self._loaded = False
        self._nums: dict[str, tuple[str, dict[int, int]]] = {}
        self._levels: dict[str, dict[int, _Level]] = {}
        self._style_numpr: dict[str, tuple[str | None, int | None]] = {}
        self._counters: dict[str, list[int]] = {}

    def _load(self) -> None:
        from docx.opc.constants import RELATIONSHIP_TYPE as RT

        self._loaded = True
        try:
            numbering = self.doc.part.part_related_by(RT.NUMBERING).element
        except KeyError:
            return
        for abstract in numbering.findall(_w("abstractNum")):
            levels = {}
            for lvl in abstract.findall(_w("lvl")):
                try:
                    ilvl = int(lvl.get(_w("ilvl")))
                except (TypeError, ValueError):
                    continue
                fmt = lvl.find(_w("numFmt"))
                text = lvl.find(_w("lvlText"))
                start = lvl.find(_w("start"))
                levels[ilvl] = _Level(
                    fmt.get(_w("val"), "decimal") if fmt is not None else "decimal",
                    text.get(_w("val"), "") if text is not None else "",
                    _safe_int(start.get(_w("val")) if start is not None else None, 1))
            self._levels[abstract.get(_w("abstractNumId"))] = levels
        for num in numbering.findall(_w("num")):
            ref = num.find(_w("abstractNumId"))
            overrides = {}
            for override in num.findall(_w("lvlOverride")):
                start = override.find(_w("startOverride"))
                if start is not None:
                    overrides[_safe_int(override.get(_w("ilvl")), 0)] = _safe_int(start.get(_w("val")), 1)
            self._nums[num.get(_w("numId"))] = (ref.get(_w("val")) if ref is not None else "", overrides)

    def _style_num(self, style_id: str | None) -> tuple[str | None, int | None]:
        if not style_id:
            return None, None
        if style_id in self._style_numpr:
            return self._style_numpr[style_id]
        styles = self.doc.styles.element
        found: tuple[str | None, int | None] = (None, None)
        seen: set[str] = set()
        current: str | None = style_id
        while current and current not in seen:
            seen.add(current)
            style = styles.get_by_id(current)
            if style is None:
                break
            num_pr = style.find(f"{_w('pPr')}/{_w('numPr')}")
            if num_pr is not None:
                num_id = num_pr.find(_w("numId"))
                ilvl = num_pr.find(_w("ilvl"))
                found = (num_id.get(_w("val")) if num_id is not None else None,
                         _safe_int(ilvl.get(_w("val")), 0) if ilvl is not None else None)
                break
            current = style.basedOn_val
        self._style_numpr[style_id] = found
        return found

    def marker(self, p: Any) -> str:
        """The paragraph's marker and a space, or "" when it is not in a list."""
        if not self._loaded:
            self._load()
        if not self._nums:
            return ""
        num_pr = p.find(f"{_w('pPr')}/{_w('numPr')}")
        num_id, ilvl = None, None
        if num_pr is not None:
            el = num_pr.find(_w("numId"))
            lvl = num_pr.find(_w("ilvl"))
            num_id = el.get(_w("val")) if el is not None else None
            ilvl = _safe_int(lvl.get(_w("val")), 0) if lvl is not None else None
        if num_id is None:
            style_num, style_lvl = self._style_num(p.style)
            num_id = style_num
            ilvl = ilvl if ilvl is not None else style_lvl
        if num_id is None or num_id == "0" or num_id not in self._nums:
            return ""
        ilvl = min(max(ilvl or 0, 0), LEVELS - 1)
        abstract_id, overrides = self._nums[num_id]
        levels = self._levels.get(abstract_id, {})
        key = f"num:{num_id}" if overrides else f"abstract:{abstract_id}"
        counters = self._counters.get(key)
        if counters is None:
            counters = [overrides.get(i, levels[i].start if i in levels else 1) - 1 for i in range(LEVELS)]
            self._counters[key] = counters
        counters[ilvl] += 1
        for deeper in range(ilvl + 1, LEVELS):
            counters[deeper] = overrides.get(deeper, levels[deeper].start if deeper in levels else 1) - 1
        level = levels.get(ilvl)
        if level is None:
            return ""
        if level.fmt == "bullet":
            char = level.text[:1]
            return ("•" if not char or ord(char) in _PRIVATE_USE or char in "o§·" else char) + " "
        text = level.text
        for i in range(LEVELS):
            token = f"%{i + 1}"
            if token in text:
                lvl_def = levels.get(i)
                text = text.replace(token, format_number(max(counters[i], 1), lvl_def.fmt if lvl_def else "decimal"))
        return f"{text} " if text else ""


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
