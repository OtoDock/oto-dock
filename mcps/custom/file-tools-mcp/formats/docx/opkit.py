"""What the write_docx operations of several modules share: the paragraph
an operation targets, the runs a paragraph is filled with, a copy of
properties without change records, a complex field, and the keys many
operations declare.

``job`` is the write call's job (``word._DocxJob``): its ``doc``, its
``book`` of targets and its paragraph id allocator.
"""

from dataclasses import dataclass, field
from typing import Any

from catalog import KeySpec
from equations import latex_to_omml_element
from formats.docx import address as addr
from formats.docx.format import apply_run_format
from formats.docx.styles import apply_character_style
from formats.docx.targets import PartParent, legacy_index, locate
from ooxml.order import insert_child
from results import Code, OpCtx, OpError

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def paragraph_at(job: Any, index: int, *, allow_end: bool = False) -> Any:
    paras = job.doc.paragraphs
    top = len(paras) if allow_end else len(paras) - 1
    if not 0 <= index <= top:
        span = f"0 to {top}" if top >= 0 else "none"
        raise OpError(Code.NOT_FOUND,
                      f"paragraph index {index} does not exist (the document has {len(paras)} "
                      f"paragraphs, so index runs {span})",
                      location=f"index {index}",
                      hint="Read the document again for the current addresses.")
    return paras[index] if index < len(paras) else None


def one_of(op: dict, legacy: str) -> None:
    if op.get("target") is not None and op.get(legacy) is not None:
        raise OpError(Code.BAD_VALUE, f"give target or {legacy}, not both",
                      hint=f"target is an address from the read; {legacy} counts the document as it is now.")


def target_paragraph(job: Any, ctx: OpCtx, op: dict, legacy: str, *, destructive: bool = True,
                      default: int | None = 0) -> Any:
    """The paragraph an op acts on: its target, else the legacy index."""
    from docx.text.paragraph import Paragraph

    one_of(op, legacy)
    if op.get("target") is not None:
        located = locate(job.book, ctx, op, kinds=(addr.PARAGRAPH,), destructive=destructive)
        job.book.touch(located.part)
        return Paragraph(located.element, PartParent(located.part))
    index = op.get(legacy, default)
    if index is None:
        raise OpError(Code.MISSING_KEY, f"give target (an address from the read) or {legacy}",
                      hint=f"target names the paragraph as the read showed it; {legacy} counts paragraphs now.")
    paragraph = paragraph_at(job, index)
    legacy_index(job.book, ctx, legacy, index, paragraph._p)
    return paragraph


def created(job: Any, ctx: OpCtx, *elements: Any) -> None:
    job.book.created.extend((ctx.index, el) for el in elements)


def equation_runs(runs_data: list) -> list:
    """Each item's OMML element, or None for a text run. Converted up front so
    a bad equation fails the op before the paragraph exists."""
    return [latex_to_omml_element(rd["equation"], display=False) if rd.get("equation") else None
            for rd in runs_data]


# Tracked-change records a copy of run or paragraph properties must not
# carry: each holds a revision id, and on a paragraph mark (``w:pPr/w:rPr``)
# ``ins``/``del``/``moveFrom``/``moveTo`` mark the mark itself as a change.
CHANGE_RECORDS = frozenset(w(tag) for tag in ("rPrChange", "pPrChange", "ins", "del", "moveFrom", "moveTo"))


def plain_props(props: Any, *drop: str) -> Any:
    """A copy of a run's or a paragraph's properties for a new one, without
    any tracked-change record at any depth (a copy would repeat its revision
    id), without a section break, and without the ``drop`` children."""
    import copy

    if props is None:
        return None
    clone = copy.deepcopy(props)
    for child in [el for el in clone.iter() if el.tag in CHANGE_RECORDS]:
        child.getparent().remove(child)
    for tag in ("sectPr", *drop):
        for child in clone.findall(w(tag)):
            clone.remove(child)
    return clone


def first_text_rpr(p: Any) -> Any:
    """The run properties of the paragraph's first run of text, or of its
    first run."""
    runs = list(p._p.iter(w("r")))
    run = next((r for r in runs if r.find(w("t")) is not None), runs[0] if runs else None)
    return run.find(w("rPr")) if run is not None else None


def fill_runs(p: Any, runs_data: list | None, op: dict, base_rpr: Any = None) -> None:
    """A paragraph's runs from ``runs`` (text and inline equations), else one
    run of ``text`` with the operation's own run keys; ``base_rpr`` is the
    formatting each new run starts from."""
    import copy

    def new_run(text: str) -> Any:
        run = p.add_run(text)
        if base_rpr is not None:
            insert_child(run._r, copy.deepcopy(base_rpr))
        return run

    if runs_data:
        for rd, omath in zip(runs_data, equation_runs(runs_data), strict=True):
            if omath is not None:
                p._p.append(omath)
                continue
            run = new_run(rd.get("text", ""))
            apply_run_format(run, rd)
            if rd.get("style") is not None:
                apply_character_style(run, rd["style"])
    else:
        apply_run_format(new_run(op.get("text", "")), op)


def add_field(p: Any, instruction: str, shown: str, dress: Any = None) -> list:
    """A complex field as Word writes one: a run for each part (begin, the
    instruction, separate, the shown value, end), each dressed alike so the
    value is drawn like the words around it. The shown value is a
    placeholder Word and LibreOffice recompute; returns the runs."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def fld_char(kind: str) -> Any:
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), kind)
        return el

    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    value = OxmlElement("w:t")
    value.set(qn("xml:space"), "preserve")
    value.text = shown
    runs = []
    for el in (fld_char("begin"), instr, fld_char("separate"), value, fld_char("end")):
        run = p.add_run()
        if dress is not None:
            dress(run)
        run._r.append(el)
        runs.append(run)
    return runs


ALIGN = KeySpec("alignment", ("str",), help="left | center | right | justify")
# A colour key also takes "" for no colour, as it always has; the colour
# itself is checked when the operation runs.
COLOR = ("color", "str")
TARGET = KeySpec("target", ("str", "int"),
                  help="an address from the read: p12, t2/r3/c1/p0, header0/p0 or #1A2B3C4D")
POSITION = KeySpec("position", ("str",),
                    help="after (default) or before a block; start or end of a cell, header or content control")
EXPECT = KeySpec("expect_text", ("str",), help="the text the target starts with, as the read showed it")
PLACE = (TARGET, POSITION, EXPECT)
AT = (TARGET, EXPECT)
RUN_KEYS = (
    KeySpec("bold", ("bool",)),
    KeySpec("italic", ("bool",)),
    KeySpec("underline", ("bool",)),
    KeySpec("strikethrough", ("bool",)),
    KeySpec("color", COLOR, aliases=("font_color",)),
    KeySpec("font_size", ("length",), unit="pt"),
    KeySpec("font_name", ("str",)),
)
RUN_ITEM = (KeySpec("text", ("str",)), KeySpec("equation", ("str",), help="LaTeX, inline math"), *RUN_KEYS,
             KeySpec("style", ("str",), help="a character style: Strong, Emphasis, or the template's own"))
RUNS = KeySpec("runs", ("list",), item=RUN_ITEM,
                help="mixed formatting: [{text, bold?, ...} or {equation: '<latex>'}]")


# What a paragraph's new content must keep: its note and comment
# references, its range marks, and the own number of a note or comment.
_TEXT_LIKE = frozenset(w(tag) for tag in ("t", "tab", "br", "cr", "noBreakHyphen", "softHyphen", "sym",
                                          "delText", "drawing", "pict"))


def _mark_run(r: Any) -> bool:
    from formats.docx import annotations

    return any(c.tag in annotations.REFERENCES or c.tag in annotations.OWN_MARKS for c in r)


def text_base_rpr(p: Any) -> Any:
    """The run properties new text in ``p`` starts from: its first run of
    text that is not a note or comment mark, else its first other run."""
    runs = [r for r in p.iter(w("r")) if not _mark_run(r)]
    run = next((r for r in runs if r.find(w("t")) is not None), runs[0] if runs else None)
    return run.find(w("rPr")) if run is not None else None


@dataclass
class Kept:
    own: list = field(default_factory=list)
    starts: list = field(default_factory=list)
    ends: list = field(default_factory=list)
    references: list = field(default_factory=list)


def take_annotations(p: Any) -> Kept:
    """Detach what rewriting ``p``'s content must keep. Mark runs lose any
    text they carried."""
    from formats.docx import annotations

    kept = Kept(
        own=[r for r in p.iter(w("r")) if any(c.tag in annotations.OWN_MARKS for c in r)],
        starts=list(p.iter(*annotations.MARK_STARTS)),
        ends=list(p.iter(*annotations.MARK_ENDS)),
        references=[r for r in p.iter(w("r")) if any(c.tag in annotations.REFERENCES for c in r)],
    )
    for el in [*kept.own, *kept.starts, *kept.ends, *kept.references]:
        el.getparent().remove(el)
    for r in [*kept.own, *kept.references]:
        for child in [c for c in r if c.tag in _TEXT_LIKE]:
            r.remove(child)
    return kept


def put_back(p: Any, kept: Kept) -> None:
    """Return what ``take_annotations`` took: a note's own number and the
    range starts before the new content, the range ends and the references
    after it. A note's number keeps a space before its text."""
    from formats.docx import annotations

    lead = list(kept.own)
    if kept.own:
        first = next((t.text for t in p.iter(w("t")) if t.text), "")
        if not first[:1].isspace():
            from docx.oxml import OxmlElement
            from docx.oxml.ns import qn

            space = OxmlElement("w:r")
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = " "
            space.append(t)
            lead.append(space)
    for el in reversed([*lead, *kept.starts]):
        annotations.put_start(el, p)
    for el in [*kept.ends, *kept.references]:
        p.append(el)


TRACK = KeySpec("track", ("bool",), help="record it as a tracked change; default: as the document is set")
AUTHOR = KeySpec("author", ("str",), help="who the tracked change is by (giving one tracks the edit); "
                                         "default OtoDock")
TRACKED = (TRACK, AUTHOR)


def revision_for(job: Any, op: dict, ctx: OpCtx | None = None) -> Any:
    """The revision an operation records its edit as, or None: ``track``
    when given, else tracked when an ``author`` is given, else the
    document's own setting (or its protection, when that allows only
    tracked changes)."""
    from formats.docx.replace import _check_storable, tracking_is_on
    from formats.docx.revisions import Revision

    track = op.get("track")
    author = op.get("author") or ""
    if track is None:
        track = bool(author) or tracking_is_on(job.doc) or enforces_tracking(job.doc)
    if not track:
        if author and ctx is not None:
            ctx.warn("the author is ignored: the edit is not a tracked change (track: false)")
        if op.get("track") is False and ctx is not None and enforces_tracking(job.doc):
            ctx.warn("the document's protection requires every edit to be a tracked change, and this one is "
                     "not: Word keeps it, but a reviewer will not see it as a change")
        return None
    try:
        _check_storable(author, "The author")
    except ValueError as exc:
        raise OpError(Code.BAD_VALUE, str(exc).rstrip("."), hint="Give the author as plain text.") from None
    return Revision(job.doc, job.ids, author)


_ON_VALUES = ("1", "true", "on")


def enforces_tracking(doc: Any) -> bool:
    """Whether the document's protection is on and allows only tracked
    changes (``w:documentProtection w:edit="trackedChanges"``)."""
    from formats.docx import annotations

    settings = annotations.first_related(doc, annotations.RT_SETTINGS)
    root = annotations.root_of_part(settings) if settings is not None else None
    guard = root.find(w("documentProtection")) if root is not None else None
    if guard is None or guard.get(w("edit")) != "trackedChanges":
        return False
    return (guard.get(w("enforcement")) or "").lower() in _ON_VALUES


def cannot_track(ctx: OpCtx, op: dict, why: str) -> None:
    """An edit that cannot be a tracked change: refused when ``track: true``
    was asked for, else made untracked with a warning."""
    if op.get("track") is True:
        raise OpError(Code.UNSUPPORTED, f"this edit cannot be recorded as a tracked change: {why}",
                      hint="Pass track: false to make it untracked, or change the text with "
                           "search_and_replace.")
    ctx.warn(f"not recorded as a tracked change: {why}.")
