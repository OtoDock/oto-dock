"""Styles by the names people use, and Word's built-in styles on demand.

A file Word saved from its blank template stores only the styles it used;
the rest are latent, known to Word by name but absent from styles.xml. An
operation that needs one of them (a heading level, Table Grid, a list
style, Quote) gets its definition copied from python-docx's own template,
with the styles it is based on and its linked character style.
"""

import copy
import difflib
from typing import Any

from results import Code, OpError

PARAGRAPH = "paragraph"
CHARACTER = "character"
TABLE = "table"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TYPE_OF = {PARAGRAPH: 1, CHARACTER: 2, TABLE: 3}
_TEMPLATE: list[Any] = []


# Word's own definitions of built-in styles python-docx's template lacks,
# as Word writes them when one is first used.
_HIDDEN = '<w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/>'
_EXTRA_STYLES = "".join([
    '<w:style w:type="paragraph" w:styleId="FootnoteText"><w:name w:val="footnote text"/>'
    f'<w:basedOn w:val="Normal"/>{_HIDDEN}<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:rPr><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>',
    '<w:style w:type="character" w:styleId="FootnoteReference"><w:name w:val="footnote reference"/>'
    f'<w:basedOn w:val="DefaultParagraphFont"/>{_HIDDEN}<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
    '</w:style>',
    '<w:style w:type="paragraph" w:styleId="EndnoteText"><w:name w:val="endnote text"/>'
    f'<w:basedOn w:val="Normal"/>{_HIDDEN}<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:rPr><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>',
    '<w:style w:type="character" w:styleId="EndnoteReference"><w:name w:val="endnote reference"/>'
    f'<w:basedOn w:val="DefaultParagraphFont"/>{_HIDDEN}<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
    '</w:style>',
    '<w:style w:type="paragraph" w:styleId="CommentText"><w:name w:val="annotation text"/>'
    f'<w:basedOn w:val="Normal"/>{_HIDDEN}<w:pPr><w:spacing w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:rPr><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>',
    '<w:style w:type="character" w:styleId="CommentReference"><w:name w:val="annotation reference"/>'
    f'<w:basedOn w:val="DefaultParagraphFont"/>{_HIDDEN}<w:rPr><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr>'
    '</w:style>',
    '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>'
    '<w:basedOn w:val="DefaultParagraphFont"/><w:uiPriority w:val="99"/><w:unhideWhenUsed/>'
    '<w:rPr><w:color w:val="0563C1" w:themeColor="hyperlink"/><w:u w:val="single"/></w:rPr></w:style>',
    *(f'<w:style w:type="paragraph" w:styleId="TOC{n}"><w:name w:val="toc {n}"/><w:basedOn w:val="Normal"/>'
      f'<w:next w:val="Normal"/><w:autoRedefine/><w:uiPriority w:val="39"/><w:unhideWhenUsed/>'
      f'<w:pPr><w:spacing w:after="100"/><w:ind w:left="{220 * (n - 1)}"/></w:pPr>'
      '</w:style>' for n in range(1, 10)),
])


def _template_styles() -> Any:
    """python-docx's template styles, with Word's definitions of the
    built-ins it lacks (note, comment, table of contents, hyperlink)."""
    from docx import Document
    from docx.oxml import parse_xml

    if not _TEMPLATE:
        styles = Document().styles.element
        ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        for extra in parse_xml(f"<w:styles {ns}>{_EXTRA_STYLES}</w:styles>"):
            if styles.get_by_id(extra.get(_W + "styleId")) is None:
                styles.append(extra)
        _TEMPLATE.append(styles)
    return _TEMPLATE[0]


def _ui_name(style: Any) -> str:
    from docx.styles import BabelFish

    return BabelFish.internal2ui(style.name_val or style.styleId or "")


def _find(styles: Any, value: str, kind: str) -> Any | None:
    """A style of ``kind`` by UI name, stored name or id, case-insensitive."""
    wanted = value.strip().casefold()
    for style in styles.iterchildren(_W + "style"):
        if (style.get(_W + "type") or PARAGRAPH) != kind:
            continue
        if wanted in {_ui_name(style).casefold(), (style.name_val or "").casefold(),
                      (style.styleId or "").casefold()}:
            return style
    return None


def _renumber(doc: Any, style: Any) -> None:
    """A list style from the template points at the template's own list
    numbers, which in this document are another list or none: give it a
    list of its own kind, at the level its name says (List Number 2: 2)."""
    from formats.docx.numbering import BULLET, NUMBER, Lists

    num_pr = style.find(f"{_W}pPr/{_W}numPr")
    if num_pr is None:
        return
    name = (style.name_val or "").strip()
    kind = NUMBER if "number" in name.casefold() else BULLET
    level = int(name.rsplit(" ", 1)[1]) - 1 if name.rsplit(" ", 1)[-1].isdigit() else 0
    num_id = Lists(doc).start_list(kind)
    num_pr.get_or_add_ilvl().val = max(level, 0)
    num_pr.get_or_add_numId().val = int(num_id)


def _copy_builtin(doc: Any, source: Any) -> Any:
    """Copy ``source`` from the template into the document, with what it
    rests on; returns the copy."""
    styles = doc.styles.element
    taken = {s.styleId for s in styles.iterchildren(_W + "style")}
    pending = [source]
    copied = None
    while pending:
        style = pending.pop(0)
        if style.styleId in taken:
            continue
        clone = copy.deepcopy(style)
        _renumber(doc, clone)
        styles.append(clone)
        taken.add(style.styleId)
        copied = copied if copied is not None else clone
        template = _template_styles()
        for ref in (style.find(_W + "basedOn"), style.find(_W + "link"), style.find(_W + "next")):
            if ref is not None and ref.get(_W + "val") not in taken:
                found = template.get_by_id(ref.get(_W + "val"))
                if found is not None:
                    pending.append(found)
    return copied


def resolve(doc: Any, value: str, kind: str, *, key: str = "style") -> Any:
    """The document's style of ``kind`` named ``value``: its own, else a
    built-in copied in, else a failure with the closest names."""
    styles = doc.styles.element
    found = _find(styles, value, kind)
    if found is not None:
        return found
    builtin = _find(_template_styles(), value, kind)
    if builtin is not None and styles.get_by_id(builtin.styleId) is None:
        return _copy_builtin(doc, builtin)
    names = [_ui_name(s) for s in styles.iterchildren(_W + "style") if (s.get(_W + "type") or PARAGRAPH) == kind]
    close = difflib.get_close_matches(value, names, n=3, cutoff=0.5)
    hint = f"Did you mean {' or '.join(repr(c) for c in close)}? " if close else ""
    shown = ", ".join(sorted(names)[:30])
    raise OpError(Code.BAD_VALUE, f"{key} '{value}' is not a {kind} style of this document",
                  hint=f"{hint}The document's {kind} styles: {shown}.")


def _document_of(item: Any) -> Any:
    return item.part._document_part.document


def apply_paragraph_style(paragraph: Any, value: str) -> None:
    doc = _document_of(paragraph)
    style = resolve(doc, value, PARAGRAPH)
    paragraph._p.get_or_add_pPr().style = style.styleId


def apply_character_style(run: Any, value: str) -> None:
    doc = _document_of(run)
    style = resolve(doc, value, CHARACTER)
    run._r.get_or_add_rPr().style = style.styleId


def ensure(doc: Any, name: str, kind: str = PARAGRAPH) -> str:
    """The id of the style ``name``, copied in from the built-ins when the
    document lacks it."""
    return resolve(doc, name, kind).styleId
