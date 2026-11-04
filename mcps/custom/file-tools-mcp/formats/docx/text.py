"""What a Word paragraph says, and whether it is a heading.

python-docx's ``Paragraph.text`` reads only the runs and hyperlinks directly
under the paragraph, so text inside a tracked insertion, a smart tag, an
inline content control or a simple field is lost. The walk below follows
every inline container that holds visible text and skips the ones that
hold deleted text. Text box paragraphs live inside a run's drawing and are
not part of their host paragraph's text.
"""

import re
from typing import Any

from equations import OMML_NS, omml_to_latex

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W_R = _W + "r"
_W_SDT = _W + "sdt"
_W_SDT_CONTENT = _W + "sdtContent"
_W_VAL = _W + "val"
_OUTLINE_PATH = f"{_W}pPr/{_W}outlineLvl"
_M_OMATH = f"{{{OMML_NS}}}oMath"
_M_OMATH_PARA = f"{{{OMML_NS}}}oMathPara"

# Inline wrappers whose runs are visible text.
_TEXT_CONTAINERS = frozenset(_W + tag for tag in (
    "hyperlink", "ins", "moveTo", "smartTag", "customXml", "fldSimple", "dir", "bdo",
))


_HEADING_NAME = re.compile(r"heading\s*(\d+)", re.IGNORECASE)
_MAX_HEADING_LEVEL = 9
_BODY_TEXT_OUTLINE = 9


def _collect(element: Any, parts: list[str]) -> None:
    for child in element.iterchildren():
        tag = child.tag
        if tag == _W_R:
            parts.append(child.text)
        elif tag in _TEXT_CONTAINERS:
            _collect(child, parts)
        elif tag == _W_SDT:
            content = child.find(_W_SDT_CONTENT)
            if content is not None:
                _collect(content, parts)
        elif tag == _M_OMATH:
            latex = omml_to_latex(child)
            parts.append(f"${latex}$" if latex else "[equation]")
        elif tag == _M_OMATH_PARA:
            for omath in child.findall(_M_OMATH):
                latex = omml_to_latex(omath)
                parts.append(f"$${latex}$$" if latex else "[equation]")


def paragraph_text(p: Any) -> str:
    """The visible text of a ``w:p`` element: insertions in, deletions out,
    equations as LaTeX."""
    parts: list[str] = []
    _collect(p, parts)
    return "".join(parts)


def _outline_level(element: Any) -> int | None:
    lvl = element.find(_OUTLINE_PATH)
    if lvl is None:
        return None
    try:
        return int(lvl.get(_W_VAL))
    except (TypeError, ValueError):
        return None


def _level_from_outline(value: int) -> int | None:
    if 0 <= value < _BODY_TEXT_OUTLINE:
        return value + 1
    return None


class HeadingLevels:
    """Heading levels of one document's paragraphs, cached per style.

    A level comes from an outline level (the paragraph's own, else the
    nearest style in its basedOn chain that sets one; 9 means body text), or
    from a style named "Heading N". Within one style the outline level wins
    over the name: "TOC Heading" is based on Heading 1 but sets outline
    level 9, so it is not a heading.
    """

    def __init__(self, doc: Any) -> None:
        from docx.enum.style import WD_STYLE_TYPE

        self._styles = doc.styles.element
        default = self._styles.default_for(WD_STYLE_TYPE.PARAGRAPH)
        self._default_id = default.styleId if default is not None else None
        self._cache: dict[str | None, int | None] = {}

    def level(self, p: Any) -> int | None:
        own = _outline_level(p)
        if own is not None:
            return _level_from_outline(own)
        return self.style_level(p.style or self._default_id)

    def style_level(self, style_id: str | None) -> int | None:
        if style_id in self._cache:
            return self._cache[style_id]
        result = None
        seen: set[str] = set()
        current = style_id
        while current and current not in seen:
            seen.add(current)
            style = self._styles.get_by_id(current)
            if style is None:
                break
            outline = _outline_level(style)
            if outline is not None:
                result = _level_from_outline(outline)
                break
            match = _HEADING_NAME.fullmatch((style.name_val or "").strip())
            if match:
                number = int(match.group(1))
                result = min(number, _MAX_HEADING_LEVEL) if number >= 1 else None
                break
            current = style.basedOn_val
        self._cache[style_id] = result
        return result
