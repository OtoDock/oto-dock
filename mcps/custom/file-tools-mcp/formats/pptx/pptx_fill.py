"""write_pptx's fill_template: a deck's ``{{ key }}`` tokens filled from
data on every slide (text boxes, placeholders, groups, table cells) and in
the speaker notes.

A token may be split across runs, as PowerPoint splits text a person edits:
the value goes into the run the token starts in, in that run's format, and
the token's text leaves the runs after it. A token never crosses a line
break, a field (a slide number, a date) or a paragraph. A value's new lines
become line breaks. Values are filled in one pass, so a value that holds a
token is never filled again. The layouts and the master are never filled:
filling them would change every slide made from them.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from catalog import KeySpec, OpSpec
from formats.pptx.pptx_opkit import G_TEXT, PptxJob, choice
from results import Code, NoOp, OpCtx, OpError
from tokens import flatten_data, token_pattern

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P, _R, _T, _BR, _RPR, _FLD = (f"{{{A_NS}}}{name}" for name in ("p", "r", "t", "br", "rPr", "fld"))
SCOPE_ALL, SCOPE_SLIDES, SCOPE_NOTES = "all", "slides", "notes"
SCOPES = (SCOPE_ALL, SCOPE_SLIDES, SCOPE_NOTES)
_SHOWN = 8


@dataclass
class _Piece:
    """A run's text element and where its text sits in the paragraph."""
    t: Any
    start: int


@dataclass
class _Paragraph:
    """A paragraph's text as its runs show it; a break or a field is a
    newline, which no token crosses."""
    element: Any
    pieces: list[_Piece] = field(default_factory=list)
    text: str = ""

    @classmethod
    def read(cls, element: Any) -> _Paragraph:
        para = cls(element)
        parts = []
        for child in element:
            if child.tag == _R:
                t = child.find(_T)
                if t is not None and t.text:
                    para.pieces.append(_Piece(t, sum(map(len, parts))))
                    parts.append(t.text)
            elif child.tag in (_BR, _FLD):
                parts.append("\n")
        para.text = "".join(parts)
        return para

    def replace(self, start: int, end: int, value: str) -> None:
        """The text from ``start`` to ``end`` replaced by ``value``, in the
        format of the run it starts in."""
        hit = [p for p in self.pieces if p.start < end and p.start + len(p.t.text) > start]
        first, last = hit[0], hit[-1]
        before = first.t.text[:start - first.start]
        after = last.t.text[end - last.start:]
        lines = value.split("\n")
        if last is first:
            first.t.text = before + lines[0] + (after if len(lines) == 1 else "")
        else:
            first.t.text = before + lines[0]
            for middle in hit[1:-1]:
                _drop_run(middle.t.getparent())
            last.t.text = after
            if not after:
                _drop_run(last.t.getparent())
        run = first.t.getparent()
        for n, line in enumerate(lines[1:], start=1):
            run = _break_after(run, line + (after if last is first and n == len(lines) - 1 else ""))


def _drop_run(run: Any) -> None:
    run.getparent().remove(run)


def _break_after(run: Any, text: str) -> Any:
    """A line break and a run of ``text`` in ``run``'s format, after it."""
    props = run.find(_RPR)
    br = etree.Element(_BR)
    if props is not None:
        br.append(copy.deepcopy(props))
    following = etree.Element(_R)
    if props is not None:
        following.append(copy.deepcopy(props))
    etree.SubElement(following, _T).text = text
    run.addnext(br)
    br.addnext(following)
    return following


@dataclass
class FillResult:
    filled: dict[str, int] = field(default_factory=dict)
    unresolved: list[tuple[str, str]] = field(default_factory=list)   # (where, token)
    unused: list[str] = field(default_factory=list)


def _places(prs: Any, scope: str) -> list[tuple[str, Any, Any]]:
    """(where, slide, element) of each part the fill reads, in deck order."""
    out = []
    for index, slide in enumerate(prs.slides):
        if scope in (SCOPE_ALL, SCOPE_SLIDES):
            out.append((f"slide_index {index}", slide, slide._element))
        if scope in (SCOPE_ALL, SCOPE_NOTES) and slide.has_notes_slide:
            out.append((f"slide_index {index}, notes", slide, slide.notes_slide._element))
    return out


def fill(prs: Any, data: dict[str, str], *, delimiters: Any = ("{{", "}}"), scope: str = SCOPE_ALL,
         touched: Any = None) -> FillResult:
    """Every token of the deck whose key ``data`` holds, filled; ValueError
    for delimiters that cannot work. ``touched(slide, element)`` is called
    for each slide element whose text changed."""
    pattern = token_pattern(delimiters)
    result = FillResult()
    for where, slide, root in _places(prs, scope):
        for element in list(root.iter(_P)):
            para = _Paragraph.read(element)
            edits = []
            for match in pattern.finditer(para.text):
                key = match.group("key").strip()
                if key in data:
                    edits.append((match.start(), match.end(), data[key]))
                    result.filled[key] = result.filled.get(key, 0) + 1
                elif key:
                    result.unresolved.append((where, match.group(0)))
            for start, end, value in reversed(edits):
                para.replace(start, end, value)
            if edits and touched is not None:
                touched(slide, element)
    result.unused = [key for key in data if key not in result.filled]
    return result


def _op_fill_template(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    data = flatten_data(op["data"])
    scope = choice(op.get("scope", SCOPE_ALL), SCOPES, "scope")
    try:
        result = fill(job.prs, data, delimiters=op.get("delimiters", ["{{", "}}"]), scope=scope,
                      touched=lambda slide, element: _touch(job, slide, element))
    except ValueError as exc:
        raise OpError(Code.BAD_VALUE, str(exc).rstrip("."), hint="Give delimiters as [\"{{\", \"}}\"].") from None
    unresolved = [f"{where}: {token}" for where, token in result.unresolved]
    if unresolved and op.get("strict", False):
        raise OpError(Code.BAD_VALUE, f"{len(unresolved)} token(s) have no value: " + "; ".join(unresolved[:_SHOWN]),
                      hint="Add the keys to data, or send strict: false to keep them.")
    if not result.filled and not unresolved:
        raise NoOp("the deck has no tokens to fill")
    if result.filled:
        total = sum(result.filled.values())
        ctx.note(f"filled {total} token{'s' if total != 1 else ''}: "
                 + ", ".join(f"{k} {n}" for k, n in list(result.filled.items())[:_SHOWN]))
    if unresolved:
        ctx.warn(f"{len(unresolved)} token(s) have no value in data and stay in the deck: "
                 + "; ".join(unresolved[:_SHOWN]) + ".")
    if result.unused:
        ctx.warn("These keys of data appear nowhere in the deck: " + ", ".join(result.unused[:20]) + ".")


def _touch(job: PptxJob, slide: Any, element: Any) -> None:
    """The shape holding a changed paragraph, as written by this batch (a
    shrink-on-overflow among them gets the scale its new text needs)."""
    shape = element.getparent()
    while shape is not None and etree.QName(shape).localname not in ("sp", "graphicFrame"):
        shape = shape.getparent()
    if shape is None:
        return
    for candidate in slide.shapes:
        if candidate._element is shape:
            job.touch(slide, candidate)
            return


OPS = (
    OpSpec("fill_template", _op_fill_template, (
        KeySpec("data", ("dict",), required=True, help="{key: value}; nested objects are a.b"),
        KeySpec("delimiters", ("list",), of=("str",), default=["{{", "}}"]),
        KeySpec("strict", ("bool",), default=False, help="fail when a token has no value"),
        KeySpec("scope", ("str",), default=SCOPE_ALL, help=" | ".join(SCOPES)),
    ), G_TEXT, aliases=("merge_template",),
        note="fills {{ key }} tokens on every slide (tables and groups too) and in the notes, even split across "
             "runs; reports tokens left and keys unused",
        setup=({"op": "add_slide", "layout": "title", "title": "{{ client.name }} kickoff"},),
        examples=({"op": "fill_template", "data": {"client": {"name": "Ada"}}, "strict": True,
                   "delimiters": ["{{", "}}"], "scope": "all"},)),
)
