"""Tracked changes: edits recorded as revisions, and accepting or
rejecting the revisions a document holds.

An insertion is a ``w:ins`` around new runs, a deletion a ``w:del`` around
the old ones (their ``w:t`` become ``w:delText``); a paragraph mark carries
its own ``w:ins``/``w:del`` in ``w:pPr/w:rPr``. Every revision takes its id
from the call's annotation counter. Range marks (bookmarks, comment ranges)
never move into a wrapper: a wrapper is split around them. Text is cut only
at word edges, so a changed number or word shows as that word replaced.
"""

import copy
import difflib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from formats.docx import annotations
from formats.docx.replace import _REPLACEMENT_BREAKS, _WORD, _Flat, _nodes, _set_text

W_NS = annotations.W_NS
_W = f"{{{W_NS}}}"
_W_R = _W + "r"
_W_T = _W + "t"
_W_P = _W + "p"
_W_RPR = _W + "rPr"
_W_PPR = _W + "pPr"
_W_ID = _W + "id"
_AUTHOR = _W + "author"
_DATE = _W + "date"
INS = "ins"
DEL = "del"
_INS, _DEL = _W + INS, _W + DEL
_MOVE_TO, _MOVE_FROM = _W + "moveTo", _W + "moveFrom"
_TOKENS = re.compile(rf"[{_WORD}]+|\s+|.", re.S)
DEFAULT_AUTHOR = "OtoDock"
ANONYMOUS_AUTHOR = "Author"

# Property-change records and the base children each restores on reject:
# a change record holds only the base properties, so the rest of the parent
# (a paragraph mark's own revision, a section's header references) stays.
_PROPERTY_CHANGES = {
    _W + "rPrChange": frozenset(_W + t for t in ("ins", "del", "moveFrom", "moveTo", "rPrChange")),
    _W + "pPrChange": frozenset(_W + t for t in ("rPr", "sectPr", "pPrChange")),
    _W + "sectPrChange": frozenset(_W + t for t in ("headerReference", "footerReference", "sectPrChange")),
    _W + "tblPrChange": frozenset({_W + "tblPrChange"}),
    _W + "tblPrExChange": frozenset({_W + "tblPrExChange"}),
    _W + "trPrChange": frozenset(_W + t for t in ("ins", "del", "trPrChange")),
    _W + "tcPrChange": frozenset(_W + t for t in ("cellIns", "cellDel", "cellMerge", "tcPrChange")),
    _W + "tblGridChange": frozenset({_W + "tblGridChange"}),
}
_DROPPED_RECORDS = frozenset({_W + "numberingChange"})
_MOVE_RANGES = frozenset(_W + t for t in ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart",
                                          "moveToRangeEnd"))
# Revisions this code counts and leaves as they are.
_LEFT_ALONE = frozenset(_W + t for t in ("cellIns", "cellDel", "cellMerge", "customXmlInsRangeStart",
                                         "customXmlDelRangeStart", "customXmlMoveFromRangeStart",
                                         "customXmlMoveToRangeStart"))
_W14_CONFLICTS = frozenset("{http://schemas.microsoft.com/office/word/2010/wordml}" + t
                           for t in ("conflictIns", "conflictDel"))
_ACTIVE_FIELDS = re.compile(r"^\s*(DDE|DDEAUTO|INCLUDETEXT|INCLUDEPICTURE)\b", re.IGNORECASE)


def _w(tag: str) -> str:
    return _W + tag


class Revision:
    """Who and when for the revisions one operation writes."""

    def __init__(self, doc: Any, ids: annotations.Ids, author: str = DEFAULT_AUTHOR) -> None:
        self.doc = doc
        self.ids = ids
        self.author = author or DEFAULT_AUTHOR
        self.date = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.anonymous = removes_personal_information(doc)

    def element(self, kind: str) -> Any:
        from docx.oxml import OxmlElement

        el = OxmlElement(f"w:{kind}")
        el.set(_W_ID, self.ids.next())
        # The author is required; a document that removes personal
        # information names everyone "Author", as Word does, and keeps no date.
        el.set(_AUTHOR, ANONYMOUS_AUTHOR if self.anonymous else self.author)
        if not self.anonymous:
            el.set(_DATE, self.date)
        return el

    def owns(self, wrapper: Any) -> bool:
        return wrapper.get(_AUTHOR) == self.author and not self.anonymous

    # What replace_all asks of a tracked edit.
    def skip(self, flat: _Flat, start: int, end: int) -> str | None:
        return skip_reason(flat, start, end, self)

    def apply(self, flat: _Flat, start: int, end: int, text: str) -> bool:
        if in_own_insertion(flat, start, self):
            from formats.docx.replace import _replace_span

            _replace_span(flat, start, end, text)
            return True
        return tracked_replace(flat, start, end, text, self)


_OFF_VALUES = ("0", "false", "off")


def removes_personal_information(doc: Any) -> bool:
    settings = annotations.first_related(doc, annotations.RT_SETTINGS)
    root = annotations.root_of_part(settings) if settings is not None else None
    if root is None:
        return False
    flag = root.find(_w("removePersonalInformation"))
    return flag is not None and flag.get(_w("val"), "true").lower() not in _OFF_VALUES


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def _copy_rpr(rpr: Any, ids: annotations.Ids) -> Any:
    """A run's properties for the second half of a split: the same, a
    formatting change it records included, under a fresh revision id."""
    clone = copy.deepcopy(rpr)
    for change in clone.iter(_w("rPrChange")):
        change.set(_W_ID, ids.next())
    return clone


def split_before(run: Any, child: Any, ids: annotations.Ids) -> Any:
    """Split ``run`` so ``child`` starts a run; returns that run."""
    contents = [c for c in run if c.tag != _W_RPR]
    if contents and contents[0] is child:
        return run
    new = run.makeelement(run.tag, dict(run.attrib))
    rpr = run.find(_W_RPR)
    if rpr is not None:
        new.append(_copy_rpr(rpr, ids))
    moving, node = [], child
    while node is not None:
        moving.append(node)
        node = node.getnext()
    for node in moving:
        new.append(node)
    run.addnext(new)
    return new


def _split_text(t: Any, at: int) -> Any:
    text = t.text or ""
    tail = t.makeelement(_W_T)
    _set_text(tail, text[at:])
    _set_text(t, text[:at])
    t.addnext(tail)
    return tail


def split_at(flat: _Flat, offset: int, ids: annotations.Ids) -> Any | None:
    """Split runs so a run starts at character ``offset`` of the flattened
    paragraph; returns that run, or None at the end of the text."""
    if offset >= len(flat.text):
        return None
    atom = flat.atoms[flat.owner[offset]]
    element = atom.element
    within = offset - atom.start
    if within:
        element = _split_text(element, within)
    return split_before(element.getparent(), element, ids)


def isolate(flat: _Flat, start: int, end: int, ids: annotations.Ids) -> list[Any]:
    """The runs that hold exactly characters ``start`` to ``end`` (one
    segment), after splitting at both ends; marks between them stay."""
    last_atom = flat.atoms[flat.owner[end - 1]]
    same = last_atom is flat.atoms[flat.owner[start]]
    split_at(flat, end, ids)
    first = split_at(flat, start, ids)
    # When both ends fall in one text node, the span is the run the second
    # split made.
    last_run = first if same else last_atom.element.getparent()
    runs, node = [], first
    while node is not None:
        if node.tag == _W_R:
            runs.append(node)
        if node is last_run:
            break
        node = node.getnext()
    return runs


def _as_deleted(run: Any) -> None:
    for t in run.iter(_W_T):
        t.tag = _w("delText")
    for instr in run.iter(_w("instrText")):
        instr.tag = _w("delInstrText")


def wrap(nodes: list[Any], kind: str, rev: Revision) -> list[Any]:
    """Consecutive sibling runs of ``nodes`` moved into ``w:ins``/``w:del``
    wrappers; a non-run sibling between them (a range mark) splits the
    wrapper and stays outside. Returns the wrappers."""
    wrappers: list[Any] = []
    group: list[Any] = []

    def close() -> None:
        if group:
            wrapper = rev.element(kind)
            group[0].addprevious(wrapper)
            for run in group:
                if kind == DEL:
                    _as_deleted(run)
                wrapper.append(run)
            wrappers.append(wrapper)
            group.clear()

    for node in nodes:
        if group and group[-1].getnext() is not node:
            close()
        if node.tag == _W_R:
            group.append(node)
        else:
            close()
    close()
    return wrappers


def mark_paragraph(p: Any, kind: str, rev: Revision) -> None:
    """The paragraph mark recorded as inserted or deleted."""
    from docx.oxml import OxmlElement

    from ooxml.order import insert_child

    ppr = p.find(_W_PPR)
    if ppr is None:
        ppr = insert_child(p, OxmlElement("w:pPr"))
    rpr = ppr.find(_W_RPR)
    if rpr is None:
        rpr = insert_child(ppr, OxmlElement("w:rPr"))
    for old in rpr.findall(_w(kind)):
        rpr.remove(old)
    insert_child(rpr, rev.element(kind))


def _new_run(text: str, base_rpr: Any) -> Any:
    from docx.oxml import OxmlElement

    from formats.docx.opkit import plain_props

    run = OxmlElement("w:r")
    props = plain_props(base_rpr)
    if props is not None:
        run.append(props)
    for node in _nodes(run, _REPLACEMENT_BREAKS.split(text)):
        run.append(node)
    return run


# ---------------------------------------------------------------------------
# Tracked replace
# ---------------------------------------------------------------------------


def trim(old: str, new: str) -> tuple[int, int, str]:
    """The part of ``old`` that changes and what replaces it, cut at word
    edges: (start, end) in ``old`` and the new middle."""
    a, b = _TOKENS.findall(old), _TOKENS.findall(new)
    head = 0
    while head < min(len(a), len(b)) and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < min(len(a), len(b)) - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    start = len("".join(a[:head]))
    end = len(old) - len("".join(a[len(a) - tail:])) if tail else len(old)
    return start, end, "".join(b[head:len(b) - tail])


def skip_reason(flat: _Flat, start: int, end: int, rev: Revision) -> str | None:
    """Why a match cannot become a tracked change, or None. A match inside
    this author's own insertion is edited in place, as Word does."""
    atom = flat.atoms[flat.owner[start]]
    if atom.in_field:
        return "a field's result"
    holder = atom.element.getparent().getparent()
    if holder.tag in (_MOVE_TO, _w("fldSimple")):
        return "moved text" if holder.tag == _MOVE_TO else "a field's result"
    if any(sdt.find(f"{_W}sdtPr/{_W}dataBinding") is not None for sdt in atom.element.iterancestors(_w("sdt"))):
        return "a content control filled from the document's properties"
    return None


def in_own_insertion(flat: _Flat, start: int, rev: Revision) -> bool:
    holder = flat.atoms[flat.owner[start]].element.getparent().getparent()
    return holder.tag == _INS and rev.owns(holder)


def _out_of_their_insertion(anchor: Any, before: bool, rev: Revision) -> tuple[Any, bool]:
    """Where new text goes when ``anchor`` sits in another author's
    insertion: their ``w:ins`` is split at that point (the second half a
    twin with its own id), and the new text goes between the halves, since a
    run belongs to one insertion only. Elsewhere, ``anchor`` itself."""
    holder = anchor.getparent()
    if holder is None or holder.tag != _INS or rev.owns(holder):
        return anchor, before
    first = anchor if before else anchor.getnext()
    moving = []
    while first is not None:
        moving.append(first)
        first = first.getnext()
    if moving:
        twin = holder.makeelement(holder.tag, dict(holder.attrib))
        twin.set(_W_ID, rev.ids.next())
        for node in moving:
            twin.append(node)
        holder.addnext(twin)
        if not len(holder):
            holder.getparent().remove(holder)
            return twin, True
    return holder, False


def _replace_tracked(flat: _Flat, start: int, end: int, middle: str, rev: Revision) -> None:
    """Characters ``start`` to ``end`` deleted and ``middle`` inserted after
    them, as tracked changes; inside another author's insertion the
    deletion nests in it (Word's form) and the insertion splits it."""
    if start < end:
        runs = isolate(flat, start, end, rev.ids)
        base = runs[0].find(_W_RPR)
        anchor, before = wrap(runs, DEL, rev)[-1], False
    else:
        following = split_at(flat, start, rev.ids)
        previous = flat.atoms[flat.owner[start - 1]].element.getparent() if start > 0 else None
        base = (previous if previous is not None else following).find(_W_RPR)
        anchor, before = (following, True) if following is not None else (previous, False)
    if middle:
        anchor, before = _out_of_their_insertion(anchor, before, rev)
        inserted = rev.element(INS)
        inserted.append(_new_run(middle, base))
        (anchor.addprevious if before else anchor.addnext)(inserted)


def tracked_replace(flat: _Flat, start: int, end: int, text: str, rev: Revision) -> bool:
    """Characters ``start`` to ``end`` replaced by ``text`` as a deletion and
    an insertion, cut at word edges; False when nothing changes."""
    head, tail, middle = trim(flat.text[start:end], text)
    start, end = start + head, start + tail
    if start == end and not middle:
        return False
    _replace_tracked(flat, start, end, middle, rev)
    return True


# ---------------------------------------------------------------------------
# Tracked paragraphs
# ---------------------------------------------------------------------------


def _live_runs(p: Any) -> list[Any]:
    """Runs of ``p`` that show now: not deleted, not a text box's Fallback
    copy, not inside another run."""
    found = []
    for run in p.iter(_W_R):
        ancestors = list(run.iterancestors())
        if any(a.tag in (_DEL, _MOVE_FROM) for a in ancestors):
            continue
        if any(a.tag == _W_R or a.tag.endswith("}Fallback") for a in ancestors[:ancestors.index(p)]):
            continue
        found.append(run)
    return found


def _base_props(p: Any) -> bytes:
    from lxml import etree

    ppr = p.find(_W_PPR)
    if ppr is None:
        return b""
    kept = [c for c in ppr if c.tag not in (_W_RPR, _w("sectPr"), _w("pPrChange"))]
    return b"".join(etree.tostring(c, method="c14n") for c in kept)


def track_new_paragraph(p: Any, rev: Revision) -> None:
    """A paragraph this call wrote, recorded as inserted: its runs, and the
    paragraph mark Word would record. After a paragraph with the same
    properties that is the one before it (Enter at its end), so rejecting
    the change never restyles a neighbour, in Word or in LibreOffice;
    otherwise the new paragraph's own."""
    by_parent: dict[int, tuple[Any, list[Any]]] = {}
    for run in _live_runs(p):
        by_parent.setdefault(id(run.getparent()), (run.getparent(), []))[1].append(run)
    for _, runs in by_parent.values():
        wrap(runs, INS, rev)
    previous = p.getprevious()
    while previous is not None and previous.tag != _W_P and previous.tag in annotations.MARK_STARTS | \
            annotations.MARK_ENDS:
        previous = previous.getprevious()
    # A previous mark already recorded as a change (an insertion just before)
    # stays as it is, and this paragraph's own mark carries the new one.
    if previous is not None and previous.tag == _W_P and _base_props(previous) == _base_props(p) and \
            previous.find(f"{_W_PPR}/{_W_RPR}/{_W}del") is None and \
            previous.find(f"{_W_PPR}/{_W_RPR}/{_W}ins") is None and \
            previous.find(f"{_W_PPR}/{_w('sectPr')}") is None:
        mark_paragraph(previous, INS, rev)
    else:
        mark_paragraph(p, INS, rev)


@dataclass
class Deleted:
    """What deleting runs as a tracked change did: runs recorded as
    deleted, and runs of the author's own insertion removed outright."""

    recorded: int = 0
    removed: int = 0

    def __bool__(self) -> bool:
        return bool(self.recorded or self.removed)


def delete_runs(runs: list[Any], rev: Revision) -> Deleted:
    """``runs`` recorded as deleted: this author's own insertions are
    removed outright (a note or comment only they referred to goes with
    them), another author's get a deletion inside them."""
    by_parent: dict[int, tuple[Any, list[Any]]] = {}
    done = Deleted()
    for run in runs:
        holder = run.getparent()
        if holder.tag == _INS and rev.owns(holder):
            annotations.release(rev.doc, run)
            holder.remove(run)
            if not len(holder):
                holder.getparent().remove(holder)
            done.removed += 1
            continue
        by_parent.setdefault(id(holder), (holder, []))[1].append(run)
    for _, group in by_parent.values():
        done.recorded += len(group)
        wrap(group, DEL, rev)
    return done


def _mark_change(p: Any, kind: str) -> Any:
    rpr = p.find(f"{_W_PPR}/{_W_RPR}")
    return rpr.find(_w(kind)) if rpr is not None else None


def _previous_paragraph(p: Any) -> Any:
    node = p.getprevious()
    while node is not None and node.tag != _W_P:
        if node.tag not in annotations.MARK_STARTS | annotations.MARK_ENDS:
            return None
        node = node.getprevious()
    return node


def own_new_paragraph(p: Any, rev: Revision) -> Any:
    """When ``p`` is wholly this author's tracked insertion, the inserted
    paragraph mark that came with it (its own, or the one before it: see
    track_new_paragraph); else None."""
    def mark_only(run: Any) -> bool:
        return all(c.tag == _W_RPR or c.tag in annotations.REFERENCES for c in run)

    if any(not (r.getparent().tag == _INS and rev.owns(r.getparent())) and not mark_only(r) for r in _live_runs(p)):
        return None
    own = _mark_change(p, "ins")
    if own is not None:
        return own if rev.owns(own) else None
    if _mark_change(p, "del") is not None:
        return None
    previous = _previous_paragraph(p)
    mark = _mark_change(previous, "ins") if previous is not None else None
    return mark if mark is not None and rev.owns(mark) else None


def already_deleted(doc: Any, element: Any, op: dict, key: str) -> str | None:
    """Why a tracked delete of a row or paragraph changes nothing: its own
    mark records it as deleted already. None when it does not."""
    from formats.docx import address as addr

    holder = element.find(_w("trPr")) if element.tag == _w("tr") else element.find(f"{_W_PPR}/{_W_RPR}")
    if holder is None or holder.find(_DEL) is None:
        return None
    shown = addr.address_of(doc, element) or "the block"
    if op.get("target") is not None:
        return f"{shown} is already marked deleted; it goes when the change is accepted"
    return (f"{key} {op.get(key, 0)} names {shown}, which is already marked deleted: a deleted block stays in "
            "the document until the change is accepted, so give the next one's address from the read")


def track_delete_paragraph(p: Any, rev: Revision) -> None:
    delete_runs(_live_runs(p), rev)
    mark_paragraph(p, DEL, rev)


def track_row(tr: Any, kind: str, rev: Revision) -> bool:
    """A table row recorded as inserted or deleted: its own mark in
    ``w:trPr`` and its text wrapped as the change. Deleting a row this
    author inserted removes nothing here and returns False: the caller
    removes it outright."""
    from docx.oxml import OxmlElement

    from ooxml.order import insert_child

    trpr = tr.find(_w("trPr"))
    if kind == DEL and trpr is not None:
        mine = trpr.find(_INS)
        if mine is not None and rev.owns(mine):
            return False
        if trpr.find(_DEL) is not None:
            return True
    if trpr is None:
        trpr = insert_child(tr, OxmlElement("w:trPr"))
    insert_child(trpr, rev.element(kind))
    for p in tr.iter(_W_P):
        if kind == INS:
            by_parent: dict[int, tuple[Any, list[Any]]] = {}
            for run in _live_runs(p):
                by_parent.setdefault(id(run.getparent()), (run.getparent(), []))[1].append(run)
            for _, runs in by_parent.values():
                wrap(runs, INS, rev)
        else:
            delete_runs(_live_runs(p), rev)
    return True


def _text_only(p: Any) -> bool:
    """Whether ``p`` shows only text: no equation, picture, note or comment
    reference or field a word diff could not keep."""
    blocked = {_w("drawing"), _w("pict"), _w("object"), _w("fldChar"), _w("fldSimple"),
               "{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath",
               "{http://schemas.openxmlformats.org/officeDocument/2006/math}oMathPara",
               *annotations.REFERENCES}
    return not any(el.tag in blocked for el in p.iter() if isinstance(el.tag, str))


def track_set_text(p: Any, new_runs: list[Any], text: str | None, rev: Revision) -> str:
    """``p``'s text changed to ``text`` as tracked changes: a word diff for a
    paragraph of plain text, else every shown run deleted and ``new_runs``
    inserted after them. Returns "diff" or "whole"."""
    flat = _Flat(p)
    if text is not None and _text_only(p):
        old_tokens, new_tokens = _TOKENS.findall(flat.text), _TOKENS.findall(text)
        hunks = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_tokens, new_tokens,
                                                           autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            start, end = len("".join(old_tokens[:i1])), len("".join(old_tokens[:i2]))
            hunks.append((start, end, "".join(new_tokens[j1:j2])))
        if all(start == end or flat.one_segment(start, end) for start, end, _ in hunks) and \
                not any(skip_reason(flat, s, e, rev) for s, e, _ in hunks if s < e):
            for start, end, middle in reversed(hunks):
                _apply_hunk(flat, start, end, middle, rev)
            return "diff"
    shown = [r for r in _live_runs(p) if not any(c.tag in annotations.REFERENCES for c in r)]
    last = shown[-1] if shown else None
    delete_runs(shown, rev)
    if new_runs:
        inserted = rev.element(INS)
        for run in new_runs:
            inserted.append(run)
        anchor = None
        if last is not None:
            anchor = next((a for a in [last, *last.iterancestors()] if a.getparent() is not None and
                           a.getparent().tag == _W_P), None)
        if anchor is not None:
            anchor.addnext(inserted)
        else:
            p.append(inserted)
    return "whole"


def _apply_hunk(flat: _Flat, start: int, end: int, middle: str, rev: Revision) -> None:
    if start < end and in_own_insertion(flat, start, rev):
        from formats.docx.replace import _replace_span

        _replace_span(flat, start, end, middle)
        return
    _replace_tracked(flat, start, end, middle, rev)


# ---------------------------------------------------------------------------
# Accept and reject
# ---------------------------------------------------------------------------


class Settled:
    """What accepting or rejecting did."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.left: dict[str, int] = {}
        self.warnings: list[str] = []
        self.parts: list[Any] = []

    def add(self, what: str, n: int = 1) -> None:
        self.counts[what] = self.counts.get(what, 0) + n

    @property
    def total(self) -> int:
        return sum(self.counts.values())


_SINGULAR = {"insertions": "insertion", "deletions": "deletion", "paragraph marks": "paragraph mark",
             "rows": "row", "formatting changes": "formatting change",
             "style or list definitions": "style or list definition"}


def counted(n: int, what: str) -> str:
    """'1 deletion', '2 deletions'."""
    return f"{n} {_SINGULAR.get(what, what) if n == 1 else what}"


def _by(author: str | None, el: Any) -> bool:
    return author is None or el.get(_AUTHOR) == author


def _unwrap(wrapper: Any) -> None:
    for child in list(wrapper):
        wrapper.addprevious(child)
    wrapper.getparent().remove(wrapper)


def _lift_out(doc: Any, wrapper: Any) -> None:
    """Range marks inside content about to go are put before it; notes and
    comments only it refers to are deleted."""
    for mark in [m for m in wrapper.iter(*annotations.MARK_STARTS, *annotations.MARK_ENDS)]:
        wrapper.addprevious(mark)
    annotations.release(doc, wrapper)


def _restore(change: Any) -> None:
    """Reject a property change: the parent's base properties become the
    old ones the record holds; what the record cannot hold stays."""
    parent = change.getparent()
    keep = _PROPERTY_CHANGES[change.tag]
    old = next(iter(change), None)
    for child in list(parent):
        if child.tag not in keep:
            parent.remove(child)
    parent.remove(change)
    if old is not None:
        from ooxml.order import insert_child

        for child in list(old):
            insert_child(parent, child)


def _paragraph_shows_nothing(p: Any) -> bool:
    return not any(el.tag in (_W_T, _w("tab"), _w("br"), _w("drawing"), _w("pict"), _w("object"), _w("sym"))
                   or el.tag in annotations.REFERENCES for el in p.iter() if isinstance(el.tag, str))


def _settle_mark(p: Any, result: Settled, chained: set[int]) -> None:
    """A paragraph mark whose deletion is accepted (or insertion rejected):
    the paragraph joins the next one, keeping its own properties, or goes
    when nothing of it shows."""
    from formats.docx.targets import field_balance

    if p.find(f"{_W_PPR}/{_W}sectPr") is not None:
        result.warnings.append("a paragraph mark that ends a section was left as it is: removing it would "
                               "remove the section break")
        return
    container = p.getparent()
    following = p.getnext()
    while following is not None and following.tag not in (_W_P, _w("tbl"), _w("sdt")):
        following = following.getnext()
    paragraphs = container.findall(_W_P)
    if _paragraph_shows_nothing(p) and len(paragraphs) > 1 and field_balance(p) == 0:
        target = following if following is not None and following.tag == _W_P else None
        for mark in [m for m in p.iter(*annotations.MARK_STARTS, *annotations.MARK_ENDS)]:
            if target is None:
                continue
            if mark.tag in annotations.MARK_STARTS:
                annotations.put_start(mark, target)
            else:
                target.append(mark)
        container.remove(p)
        return
    # A run of marks to settle (a chain of tracked new paragraphs) joins on.
    while following is not None and following.tag == _W_P:
        for child in list(following):
            if child.tag != _W_PPR:
                p.append(child)
        container.remove(following)
        if id(following) not in chained:
            break
        following = p.getnext()
        while following is not None and following.tag not in (_W_P, _w("tbl"), _w("sdt")):
            following = following.getnext()


def _settle_part(doc: Any, root: Any, accept: bool, author: str | None, result: Settled) -> bool:
    goes, stays = ((_DEL, _MOVE_FROM), (_INS, _MOVE_TO)) if accept else ((_INS, _MOVE_TO), (_DEL, _MOVE_FROM))
    changed = False
    for el in list(root.iter(*_LEFT_ALONE, *_W14_CONFLICTS)):
        result.left[el.tag.rsplit("}", 1)[1]] = result.left.get(el.tag.rsplit("}", 1)[1], 0) + 1
    # Rows first: a row's own revision sits in its w:trPr.
    for tr in list(root.iter(_w("tr"))):
        trpr = tr.find(_w("trPr"))
        marks = [m for m in (trpr if trpr is not None else []) if m.tag in (_INS, _DEL) and _by(author, m)]
        for mark in marks:
            if mark.tag in goes and tr.getparent() is not None:
                _lift_out(doc, tr)
                tr.getparent().remove(tr)
                result.add("rows")
                if tr is root:
                    return True
            else:
                trpr.remove(mark)
            changed = True
    # Paragraph marks are settled after the content around them.
    mark_paragraphs = []
    for p in list(root.iter(_W_P)):
        rpr = p.find(f"{_W_PPR}/{_W_RPR}")
        for mark in [m for m in (rpr if rpr is not None else []) if m.tag in (*goes, *stays) and _by(author, m)]:
            rpr.remove(mark)
            if mark.tag in goes:
                mark_paragraphs.append(p)
            result.add("paragraph marks")
            changed = True
    for wrapper in list(root.iter(*goes)):
        if wrapper.getparent() is None or wrapper.getparent().tag in (_W_RPR, _w("trPr")) or \
                not _by(author, wrapper):
            continue
        _lift_out(doc, wrapper)
        wrapper.getparent().remove(wrapper)
        result.add("deletions" if wrapper.tag in (_DEL, _MOVE_FROM) else "insertions")
        changed = True
    for wrapper in list(root.iter(*stays)):
        if wrapper.getparent() is None or wrapper.getparent().tag in (_W_RPR, _w("trPr")) or \
                not _by(author, wrapper):
            continue
        if wrapper.tag in (_DEL, _MOVE_FROM):
            for t in wrapper.iter(_w("delText")):
                t.tag = _W_T
            for instr in wrapper.iter(_w("delInstrText")):
                instr.tag = _w("instrText")
                if _ACTIVE_FIELDS.match(instr.text or ""):
                    result.warnings.append("a rejected deletion brought back a field that fetches or runs "
                                           f"something when the file opens ({(instr.text or '').split()[0]})")
        _unwrap(wrapper)
        result.add("insertions" if wrapper.tag in (_INS, _MOVE_TO) else "deletions")
        changed = True
    for el in [e for e in root.iter(*_MOVE_RANGES) if _by(author, e)]:
        el.getparent().remove(el)
        changed = True
    for change in [c for c in root.iter(*_PROPERTY_CHANGES, *_DROPPED_RECORDS) if _by(author, c)]:
        if accept or change.tag in _DROPPED_RECORDS:
            change.getparent().remove(change)
        else:
            _restore(change)
        result.add("formatting changes")
        changed = True
    chained = {id(p) for p in mark_paragraphs}
    for p in mark_paragraphs:
        if p.getparent() is not None:
            _settle_mark(p, result, chained)
    return changed


def settle_all(doc: Any, *, accept: bool, author: str | None = None) -> Settled:
    """Accept or reject every revision (``author``'s only, when given) in
    the document, its headers and footers, notes and comments. Style and
    numbering definitions are counted and left."""
    result = Settled()
    for part in annotations.story_parts(doc):
        root = annotations.root_of_part(part)
        if root is None:
            continue
        if root.tag in (_w("styles"), _w("numbering")):
            found = sum(1 for _ in root.iter(*_PROPERTY_CHANGES, _INS, _DEL))
            if found:
                result.left["style or list definitions"] = result.left.get("style or list definitions", 0) + found
            continue
        if _settle_part(doc, root, accept, author, result):
            result.parts.append(part)
    return result


def count(doc: Any) -> dict[str, int]:
    """Revisions per kind in every story part, and their authors."""
    found: dict[str, int] = {}
    authors: set[str] = set()
    names = {_INS: "insertions", _MOVE_TO: "insertions", _DEL: "deletions", _MOVE_FROM: "deletions"}
    for part in annotations.story_parts(doc):
        root = annotations.root_of_part(part)
        if root is None or root.tag in (_w("styles"), _w("numbering")):
            continue
        for el in root.iter(*names, *_PROPERTY_CHANGES):
            if el.tag in names and el.getparent().tag in (_W_RPR, _w("trPr")):
                kind = "paragraph marks" if el.getparent().tag == _W_RPR else "rows"
            elif el.tag in names:
                if next(el.iter(_W_R), None) is None:
                    continue
                kind = names[el.tag]
            else:
                kind = "formatting changes"
            found[kind] = found.get(kind, 0) + 1
            if el.get(_AUTHOR):
                authors.add(el.get(_AUTHOR))
    if found:
        found["authors"] = len(authors)
    return found


def authors(doc: Any) -> list[str]:
    names: list[str] = []
    for part in annotations.story_parts(doc):
        root = annotations.root_of_part(part)
        if root is None:
            continue
        for el in root.iter(_INS, _DEL, _MOVE_TO, _MOVE_FROM, *_PROPERTY_CHANGES):
            author = el.get(_AUTHOR)
            if author and author not in names:
                names.append(author)
    return names


def set_tracking(doc: Any, on: bool) -> bool:
    """The document's own switch for recording changes; returns whether it
    changed. Turning it off never creates the settings part."""
    settings_part = annotations.first_related(doc, annotations.RT_SETTINGS)
    if settings_part is None and not on:
        return False
    root = doc.settings.element
    flag = root.find(_w("trackRevisions"))
    was = flag is not None and flag.get(_w("val"), "true").lower() not in _OFF_VALUES
    if was == on:
        return False
    if flag is not None:
        root.remove(flag)
    if on:
        from docx.oxml import OxmlElement

        from ooxml.order import insert_child

        insert_child(root, OxmlElement("w:trackRevisions"))
    return True


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _op_set_track_changes(job: Any, ctx: Any, op: dict) -> None:
    from results import NoOp

    on = op.get("on", True)
    if not set_tracking(job.doc, on):
        raise NoOp(f"the document already {'records' if on else 'does not record'} changes")
    ctx.note("the document now records every change as a tracked change" if on
             else "the document no longer records changes")


def _mark_that_came_with(p: Any, author: str | None) -> tuple[Any, Any] | None:
    """The paragraph before ``p`` and its inserted mark, when ``p`` is a
    wholly inserted paragraph whose own mark records no change: the mark
    came with it (Enter at that paragraph's end), so settling ``p`` settles
    it too. None otherwise."""
    if p.tag != _W_P:
        return None
    own = p.find(f"{_W_PPR}/{_W_RPR}")
    if own is not None and (own.find(_INS) is not None or own.find(_DEL) is not None):
        return None
    if any(not any(a.tag in (_INS, _MOVE_TO) for a in run.iterancestors()) for run in _live_runs(p)):
        return None
    before = p.getprevious()
    while before is not None and before.tag not in (_W_P, _w("tbl"), _w("sdt")):
        before = before.getprevious()
    if before is None or before.tag != _W_P:
        return None
    mark = before.find(f"{_W_PPR}/{_W_RPR}/{_INS}")
    return (before, mark) if mark is not None and _by(author, mark) else None


def _settle(job: Any, ctx: Any, op: dict, *, accept: bool) -> None:
    from results import NoOp

    author = op.get("author")
    where = "the document"
    if op.get("target") is not None:
        from formats.docx import address as addr
        from formats.docx.targets import locate

        located = locate(job.book, ctx, op, kinds=(addr.PARAGRAPH, addr.TABLE, addr.ROW, addr.CELL,
                                                   addr.CONTROL, addr.PART))
        where = str(op["target"])
        result = Settled()
        came = _mark_that_came_with(located.element, author)
        if _settle_part(job.doc, located.element, accept, author, result) or came is not None:
            result.parts.append(located.part)
        if came is not None and came[1].getparent() is not None:
            before, mark = came
            mark.getparent().remove(mark)
            result.add("paragraph marks")
            if not accept and before.getparent() is not None:
                _settle_mark(before, result, set())
    else:
        result = settle_all(job.doc, accept=accept, author=author)
    for part in result.parts:
        job.book.touch(part)
    left = ", ".join(counted(n, what) for what, n in result.left.items())
    if not result.total:
        raise NoOp(f"{where} holds no tracked changes" + (f" by {author}" if author else "")
                   + (f" this can settle ({left} left as they are)" if left else ""))
    done = ", ".join(counted(n, what) for what, n in result.counts.items())
    ctx.note(f"{'accepted' if accept else 'rejected'}: {done}")
    if left:
        ctx.warn(f"left as they are, for Word to settle: {left}.")
    for warning in result.warnings:
        ctx.warn(warning + ".")


def _op_accept_all_changes(job: Any, ctx: Any, op: dict) -> None:
    _settle(job, ctx, op, accept=True)


def _op_reject_all_changes(job: Any, ctx: Any, op: dict) -> None:
    _settle(job, ctx, op, accept=False)


def _ops() -> list[Any]:
    from catalog import KeySpec, OpSpec
    from formats.docx.opkit import EXPECT

    author = KeySpec("author", ("str",), help="only this author's changes; default every author's")
    target = KeySpec("target", ("str", "int"), help="only the changes inside this block (p5, t0/r2, "
                                                     "footnote1); default the whole document")
    tracked = ({"op": "set_track_changes"},
               {"op": "insert_paragraph", "index": 1, "text": "Added clause.", "author": "Legal"},
               {"op": "search_and_replace", "find": "30 days", "replace": "45 days", "author": "Legal"})
    original = ({"op": "add_paragraph", "text": "Payment within 30 days."},)
    return [
        OpSpec("set_track_changes", _op_set_track_changes, (
            KeySpec("on", ("bool",), default=True),
        ), "REVIEW", note="the document records later edits, ours and Word's, as tracked changes",
            examples=({"op": "set_track_changes", "on": True},)),
        OpSpec("accept_all_changes", _op_accept_all_changes, (author, target, EXPECT), "REVIEW",
               setup=(*original, *tracked), examples=({"op": "accept_all_changes"},
                                                      {"op": "accept_all_changes", "author": "Legal"},
                                                      {"op": "accept_all_changes", "target": "p0"})),
        OpSpec("reject_all_changes", _op_reject_all_changes, (author, target, EXPECT), "REVIEW",
               setup=(*original, *tracked), examples=({"op": "reject_all_changes"},
                                                      {"op": "reject_all_changes", "target": "p1",
                                                       "expect_text": "Added clause."})),
    ]


OPS = _ops()
