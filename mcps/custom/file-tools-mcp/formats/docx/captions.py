"""Captions, cross-references and updating fields.

A caption is a Caption paragraph "Figure 1. text" whose number is a SEQ
field and whose "Figure 1" is a bookmark, so a cross-reference can show it.
A cross-reference is a REF field (the bookmark's words) or a PAGEREF field
(the page it is on). At the end of a call that added either, or asked to
update fields, every SEQ field is numbered again in document order and every
REF shows its bookmark's words; a PAGEREF takes its page from the
LibreOffice layout the table of contents uses, and keeps the page it showed
when there is no layout.
"""

import random
import re
from dataclasses import dataclass, field
from typing import Any

from catalog import KeySpec, OpSpec
from formats.docx import annotations
from formats.docx.fields import (
    _BOOKMARK_NAME,
    Toc,
    bookmark_names,
    find_tocs,
    link_target,
)
from formats.docx.notes import body_paragraph, find_text
from formats.docx.opkit import AT, POSITION, TARGET, add_field, created
from formats.docx.revisions import split_at
from formats.docx.styles import ensure
from formats.docx.targets import PartParent, cursor
from results import Code, NoOp, OpCtx, OpError

_W = f"{{{annotations.W_NS}}}"
_W_P = _W + "p"
_W_R = _W + "r"
_W_T = _W + "t"
_SEQ = re.compile(r"^\s*SEQ\s+(\S+)(.*)$", re.IGNORECASE | re.DOTALL)
_PAGEREF = re.compile(r"^\s*PAGEREF\s+(\S+)", re.IGNORECASE)
_REF_ONLY = re.compile(r"^\s*REF\s+(\S+)(.*)$", re.IGNORECASE | re.DOTALL)
_RESET = re.compile(r"\\r\s+(\d+)", re.IGNORECASE)
_CHAPTER = re.compile(r"\\s\s*(\d+)", re.IGNORECASE)
_FORMAT = re.compile(r"\\\*\s*(\w+)")
_PLACEHOLDER = "000"
_TABLE_LABELS = frozenset({"table", "tableau", "tabelle", "tabla", "πίνακας"})
_FORMATS = {"ARABIC": "decimal", "ROMAN": "upperRoman", "roman": "lowerRoman", "ALPHABETIC": "upperLetter",
            "alphabetic": "lowerLetter"}
_SHOW = ("text", "page")


def _w(tag: str) -> str:
    return _W + tag


@dataclass
class Field:
    """One field of the body: its instruction, the runs that show its
    value, its end run, the instructions of the fields around it."""

    instruction: str
    result: list = field(default_factory=list)
    end: Any = None
    simple: Any = None
    outer: list = field(default_factory=list)


def iter_fields(root: Any) -> list[Field]:
    """Every field under ``root``, complex and simple, in document order,
    deleted text left out."""
    found: list[Field] = []
    stack: list[dict] = []
    for el in root.iter(_W_R, _w("fldSimple")):
        if any(a.tag in (_w("del"), _w("moveFrom")) for a in el.iterancestors()):
            continue
        if el.tag == _w("fldSimple"):
            found.append(Field(el.get(_w("instr")) or "", list(el.iter(_W_R)), simple=el,
                               outer=[f["instr"] for f in stack]))
            continue
        if any(a.tag == _w("fldSimple") for a in el.iterancestors()):
            continue
        char = el.find(_w("fldChar"))
        instr = el.find(_w("instrText"))
        if char is not None:
            kind = char.get(_w("fldCharType"))
            if kind == annotations.FIELD_BEGIN:
                stack.append({"instr": "", "result": [], "in_result": False})
            elif kind == annotations.FIELD_SEPARATE and stack:
                stack[-1]["in_result"] = True
            elif kind == annotations.FIELD_END and stack:
                item = stack.pop()
                found.append(Field(item["instr"].strip(), item["result"], el, outer=[f["instr"] for f in stack]))
        elif instr is not None and stack:
            stack[-1]["instr"] += instr.text or ""
        elif stack and stack[-1]["in_result"]:
            stack[-1]["result"].append(el)
    return found


def set_value(f: Field, value: str) -> None:
    """Show ``value`` as the field's result: the first result run's text,
    the other result runs emptied; a run is made when there is none."""
    texts = [t for run in f.result for t in run.iter(_W_T)]
    if texts:
        texts[0].text = value
        for t in texts[1:]:
            t.text = ""
        return
    run = f.end.makeelement(_W_R) if f.end is not None else f.simple.makeelement(_W_R)
    t = run.makeelement(_W_T)
    t.text = value
    run.append(t)
    if f.end is not None:
        f.end.addprevious(run)
    else:
        f.simple.append(run)
    f.result.append(run)


def _format(n: int, switches: str) -> str:
    from formats.docx.numbering import format_number

    match = _FORMAT.search(switches)
    return format_number(n, _FORMATS.get(match.group(1), "decimal") if match else "decimal")


def bookmark_texts(root: Any) -> dict[str, str]:
    """Each bookmark's words in the body (deleted text left out)."""
    open_marks: dict[str, str] = {}
    texts: dict[str, list[str]] = {}
    for el in root.iter(_w("bookmarkStart"), _w("bookmarkEnd"), _W_T):
        if el.tag == _w("bookmarkStart"):
            open_marks[el.get(_w("id")) or ""] = el.get(_w("name")) or ""
            texts.setdefault(el.get(_w("name")) or "", [])
        elif el.tag == _w("bookmarkEnd"):
            open_marks.pop(el.get(_w("id")) or "", None)
        elif not any(a.tag in (_w("del"), _w("moveFrom")) for a in el.iterancestors()):
            for name in open_marks.values():
                texts[name].append(el.text or "")
    return {name: "".join(parts).strip() for name, parts in texts.items()}


def _field_paragraph(f: Field) -> Any:
    anchor = f.end if f.end is not None else f.simple
    return next(anchor.iterancestors(_W_P), None)


def refresh_fields(job: Any) -> None:
    """Number every SEQ field of the body again in document order (``\\r``
    sets the number, ``\\c`` repeats it, ``\\s N`` starts again after each
    heading of level N or above), show every REF's bookmark words, and
    leave the pages of PAGEREF fields outside a table of contents to the
    layout (their old page kept as the fallback), in the body, headers,
    footers, notes and comments."""
    from formats.docx.text import HeadingLevels

    body = job.doc.element.body
    levels = HeadingLevels(job.doc)
    paragraphs = list(body.iter(_W_P))
    position = {id(p): i for i, p in enumerate(paragraphs)}
    heads = [(i, level) for i, p in enumerate(paragraphs) if (level := levels.level(p))]
    counters: dict[str, int] = {}
    last: dict[str, int] = {}
    for f in iter_fields(body):
        match = _SEQ.match(f.instruction)
        if not match:
            continue
        key, switches = match.group(1).casefold(), match.group(2)
        at = position.get(id(_field_paragraph(f)), 0)
        chapter = _CHAPTER.search(switches)
        if chapter and any(last.get(key, -1) < i <= at and level <= int(chapter.group(1)) for i, level in heads):
            counters[key] = 0
        reset = _RESET.search(switches)
        if reset:
            counters[key] = int(reset.group(1))
        elif not re.search(r"\\c\b", switches, re.IGNORECASE):
            counters[key] = counters.get(key, 0) + 1
        set_value(f, _format(counters.get(key, 0), switches))
        last[key] = at
    job.book.touch(job.doc.part)
    words = bookmark_texts(body)
    for part in annotations.story_parts(job.doc):
        root = annotations.root_of_part(part)
        if root is None or root.tag in (_w("styles"), _w("numbering")):
            continue
        for f in iter_fields(root):
            ref = _REF_ONLY.match(f.instruction)
            if ref and not re.search(r"\\[nrwp]\b", ref.group(2), re.IGNORECASE) and ref.group(1) in words:
                set_value(f, words[ref.group(1)])
                job.book.touch(part)
                continue
            page = _PAGEREF.match(f.instruction)
            if page and job.refresh_pages and not any(o.strip().upper().startswith("TOC") for o in f.outer):
                old = "".join(t.text or "" for run in f.result for t in run.iter(_W_T))
                job.page_fallback.setdefault(page.group(1), old)
                set_value(f, _PLACEHOLDER)
                job.page_marks.add(page.group(1))
                job.book.touch(part)


def _new_name(doc: Any, wanted: str | None, prefix: str) -> str:
    taken = bookmark_names(doc)
    if wanted:
        if not _BOOKMARK_NAME.fullmatch(wanted):
            raise OpError(Code.BAD_VALUE, f"bookmark {wanted!r} is not a bookmark name Word accepts",
                          hint="Start with a letter; use letters, digits and _ only; at most 40 characters.")
        if wanted.casefold() in taken:
            raise OpError(Code.BAD_VALUE, f"the document already has a bookmark named {wanted!r}",
                          hint="Give another name, or leave bookmark out for one made for you.")
        return wanted
    rng = random.Random()
    while True:
        name = f"{prefix}{rng.randint(100_000_000, 999_999_999)}"
        if name.casefold() not in taken:
            return name


def _op_add_caption(job: Any, ctx: OpCtx, op: dict) -> None:
    from docx.oxml import parse_xml

    label = (op.get("label") or "Figure").strip()
    if not label or not re.fullmatch(r"[^\W\d_][\w .-]{0,39}", label):
        raise OpError(Code.BAD_VALUE, f"label {label!r} is not a caption label",
                      hint="Give a word such as Figure, Table or Equation.")
    position = op.get("position")
    if position is None and op.get("target") is not None:
        position = "before" if label.casefold() in _TABLE_LABELS else "after"
    spot = cursor(job.book, ctx, {"target": op.get("target"), "position": position,
                                  "expect_text": op.get("expect_text")}, allow_containers=False)
    p = spot.paragraph()
    p._p.get_or_add_pPr().style = ensure(job.doc, "Caption")
    name = _new_name(job.doc, op.get("bookmark"), "_Ref")
    p.add_run(f"{label} ")
    runs = add_field(p, f" SEQ {label.split()[0]} \\* ARABIC ", "1")
    value = job.ids.next()
    ns = f'xmlns:w="{annotations.W_NS}"'
    start = parse_xml(f'<w:bookmarkStart {ns} w:id="{value}"/>')
    start.set(_w("name"), name)
    p._p.find(_W_R).addprevious(start)
    runs[-1]._r.addnext(parse_xml(f'<w:bookmarkEnd {ns} w:id="{value}"/>'))
    text = op.get("text", "")
    if text:
        p.add_run(f"{op.get('separator', '. ')}{text}")
    job.refresh_fields = True
    created(job, ctx, p._p)
    ctx.note(f"the caption's bookmark is {name!r}: add_cross_reference with that bookmark shows its label and "
             "number")


def _dressed_like(source: Any) -> Any:
    """A dress for a field's runs: the run properties of the words where
    the field goes, as Word inserts it (without their tracked formatting)."""
    import copy

    rpr = source.find(_W + "rPr") if source is not None else None
    if rpr is None:
        return None

    def dress(run: Any) -> None:
        mine = copy.deepcopy(rpr)
        for change in mine.findall(_W + "rPrChange"):
            mine.remove(change)
        run._r.insert(0, mine)

    return dress


def _op_add_cross_reference(job: Any, ctx: OpCtx, op: dict) -> None:
    from docx.text.paragraph import Paragraph

    show = (op.get("show") or "text").strip().lower()
    if show not in _SHOW:
        raise OpError(Code.BAD_VALUE, f"show {show!r} is not text or page",
                      hint="text shows the bookmark's words (a caption's 'Figure 2'); page shows its page.")
    if op["bookmark"].casefold() == "_top":
        raise OpError(Code.BAD_VALUE, "_top marks the top of the document, not words or a page to show",
                      hint="Name a bookmark: a caption's, a heading's or one add_bookmark made.")
    name = link_target(job, op["bookmark"])
    part, p = body_paragraph(job, ctx, op, "a cross-reference")
    place = find_text(p, op["after_text"], "after_text", span=False) if op.get("after_text") is not None else None
    paragraph = Paragraph(p, PartParent(part))
    if place is not None:
        flat, _start, end = place
        source = flat.atoms[flat.owner[end - 1]].element.getparent()
    else:
        source = next((r for r in reversed(p.findall(_W_R)) if r.find(_W_T) is not None), None)
    dress = _dressed_like(source)
    if show == "page":
        runs = add_field(paragraph, f" PAGEREF {name} \\h ", _PLACEHOLDER, dress)
        job.page_marks.add(name)
    else:
        runs = add_field(paragraph, f" REF {name} \\h ", bookmark_texts(job.doc.element.body).get(name) or "?",
                         dress)
    if place is not None:
        following = split_at(flat, end, job.ids)
        anchor = flat.atoms[flat.owner[end - 1]].element.getparent()
        for run in runs:
            if following is not None:
                following.addprevious(run._r)
            else:
                anchor.addnext(run._r)
                anchor = run._r
    job.refresh_fields = True
    ctx.note(f"a cross-reference to {name!r} ({'its page' if show == 'page' else 'its words'})")


def _op_update_fields(job: Any, ctx: OpCtx, op: dict) -> None:
    body = job.doc.element.body
    fields = iter_fields(body)
    if not fields:
        raise NoOp("the document has no field to update")
    tocs: list[Toc] = [t for t in find_tocs(job.doc) if t.understood()]
    for toc in tocs:
        toc.op_index = ctx.index
        job.tocs.append(toc)
    job.refresh_fields = True
    job.refresh_pages = True
    kinds = sorted({(f.instruction.split() or ["?"])[0].upper() for f in fields})
    ctx.note(f"updates {len(fields)} field{'s' if len(fields) != 1 else ''} ({', '.join(kinds[:8])})")


def _ops() -> list[OpSpec]:
    figure = ({"op": "add_paragraph", "text": "[the chart]"},
              {"op": "add_caption", "text": "Revenue by month.", "bookmark": "fig_revenue"})
    return [
        OpSpec("add_caption", _op_add_caption, (
            KeySpec("label", ("str",), default="Figure", help="Figure, Table, Equation ..."),
            KeySpec("text", ("str",), default=""),
            KeySpec("separator", ("str",), default=". ", help="between the number and the text"),
            KeySpec("bookmark", ("str",), help="a name to cross-reference it by; default one made for you"),
            TARGET, POSITION,
            KeySpec("expect_text", ("str",), help="the text the target starts with, as the read showed it"),
        ), "LINKS", note="numbered in document order (a SEQ field); a table's caption goes before it, a "
                         "figure's after",
            setup=({"op": "add_paragraph", "text": "[the chart]"},),
            examples=({"op": "add_caption", "target": "p0", "text": "Revenue by month."},
                      {"op": "add_caption", "label": "Table", "text": "Fees.", "bookmark": "tab_fees"})),
        OpSpec("add_cross_reference", _op_add_cross_reference, (
            KeySpec("bookmark", ("str",), required=True, help="a caption's, a heading's or add_bookmark's name"),
            KeySpec("show", ("str",), default="text", help="text (Figure 2) | page"),
            KeySpec("after_text", ("str",), help="it goes right after these words; default the paragraph's end"),
            *AT,
        ), "LINKS", note="a REF or PAGEREF field; pages come from a LibreOffice layout",
            setup=(*figure, {"op": "add_paragraph", "text": "As shown in "}),
            examples=({"op": "add_cross_reference", "bookmark": "fig_revenue", "target": "p2"},
                      {"op": "add_cross_reference", "bookmark": "fig_revenue", "show": "page"})),
        OpSpec("update_fields", _op_update_fields, (), "LINKS",
               note="captions numbered again, cross-references and tables of contents refreshed",
               setup=(*figure,), examples=({"op": "update_fields"},)),
    ]


OPS = _ops()
