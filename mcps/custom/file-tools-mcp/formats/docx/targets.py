"""Where an operation acts: ``target``, ``position`` and ``expect_text``.

A positional target names the block the agent's read showed: every
positional target of a call resolves against one snapshot of the document
taken before the first operation runs, so a batch planned from one read
stays right when an earlier operation inserts a paragraph. An id
(``#1A2B3C4D``) resolves live. The legacy index keys keep their live
meaning; when an earlier operation of the call moved the block an index
names, the operation warns.

``expect_text`` is the text the agent expects the target to start with.
Another program may renumber or drop ids between a read and a write
(LibreOffice drops them all): a gone id is found again by a unique match on
that text, a position whose text changed is refused rather than guessed,
and an operation that would overwrite or delete a paragraph someone else
changed fails (the lost update).

New blocks are placed here, and the call's paragraph ids are stamped at
save, in the parts an operation touched only.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from formats.docx import address as addr, annotations, ids
from formats.docx.read import plain_rendering, without_marks
from formats.docx.text import paragraph_text
from ooxml.order import insert_child
from results import Code, OpError

AFTER = "after"
BEFORE = "before"
START = "start"
END = "end"
POSITIONS = (AFTER, BEFORE, START, END)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"
_FINGERPRINT = 200
_CANDIDATES_SHOWN = 5
_LEADING_TAG = re.compile(r"^\[[^\]]*\]\s*")
_LEADING_MARKER = re.compile(r"^(?:\d+|[a-z]{1,3}|[ivxlcdm]+)[.)]\s+|^[•◦▪·\-–]\s+", re.IGNORECASE)
_CONTAINER_KINDS = (addr.PART, addr.CELL, addr.CONTROL, addr.CUSTOM, addr.TEXTBOX)


class PartParent:
    """A python-docx parent for a paragraph made by hand: what the
    paragraph's runs need is the part they live in (styles, pictures)."""

    def __init__(self, part: Any) -> None:
        self.part = part


def _normal(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _expect_variants(expected: str) -> list[str]:
    base = _normal(without_marks(expected))
    variants = [base]
    untagged = _LEADING_TAG.sub("", base)
    unmarked = _LEADING_MARKER.sub("", untagged)
    for v in (untagged, unmarked):
        if v not in variants:
            variants.append(v)
    return [v[:_FINGERPRINT] for v in variants]


def _paragraph_forms(doc: Any, p: Any) -> list[str]:
    return [_normal(without_marks(plain_rendering(doc, p))), _normal(paragraph_text(p))]


def _first_cell_paragraph(tbl: Any) -> Any | None:
    rows = addr.rows(tbl)
    if not rows:
        return None
    _, cells = addr.grid(rows[0])
    if not cells:
        return None
    return next(cells[0].tc.iter(addr.W_P), None)


def _fingerprint_paragraph(kind: str, element: Any) -> Any | None:
    """The paragraph whose text stands for the block: the paragraph itself,
    a table's or row's first cell's first paragraph, the first paragraph of
    a cell, header, footer, content control or text box."""
    if kind == addr.PARAGRAPH:
        return element
    if kind == addr.TABLE:
        return _first_cell_paragraph(element)
    if kind == addr.ROW:
        _, cells = addr.grid(element)
        return next(cells[0].tc.iter(addr.W_P), None) if cells else None
    return next(element.iter(addr.W_P), None)


def matches(doc: Any, kind: str, element: Any, expected: str) -> bool:
    """Whether the block starts with ``expected`` as the read prints it, or
    as plain text; ``""`` matches only an empty block."""
    element = _fingerprint_paragraph(kind, element)
    if element is None:
        return expected.strip() == ""
    forms = _paragraph_forms(doc, element)
    if expected.strip() == "":
        return all(not f for f in forms)
    variants = _expect_variants(expected)
    return any(form.startswith(v) for form in forms for v in variants if v)


@dataclass
class Book:
    """One call's view of where its operations act."""

    doc: Any
    snapshot: dict[str, Any] = field(default_factory=dict)
    removed: dict[int, int] = field(default_factory=dict)
    removed_ids: dict[str, int] = field(default_factory=dict)
    created: list[tuple[int, Any]] = field(default_factory=list)
    touched: list[Any] = field(default_factory=list)
    initial_paragraphs: list[Any] = field(default_factory=list)
    initial_tables: list[Any] = field(default_factory=list)

    @classmethod
    def for_call(cls, doc: Any, ops: list[tuple[int, dict]]) -> "Book":
        book = cls(doc)
        body = doc.element.body
        book.initial_paragraphs = list(body.iterchildren(addr.W_P))
        book.initial_tables = list(body.iterchildren(addr.W_TBL))
        for _, op in ops:
            target = op.get("target") if isinstance(op, dict) else None
            if target is None or isinstance(target, bool):
                continue
            key = str(target)
            if key in book.snapshot:
                continue
            try:
                address = addr.parse(target)
            except OpError:
                continue
            if address.para_id is not None and not address.segments:
                continue
            try:
                book.snapshot[key] = addr.walk(doc, address)
            except OpError as exc:
                exc.hint = ("Addresses name the document as this call began; a block the call creates gets its "
                            "address in the result. Use an index key for it now, or its address in the next call.")
                book.snapshot[key] = exc
        return book

    def touch(self, part: Any) -> None:
        if not any(part is p for p in self.touched):
            self.touched.append(part)

    def remove(self, ctx: Any, element: Any) -> None:
        from formats.docx import annotations

        took = annotations.describe(annotations.release(self.doc, element))
        if took:
            ctx.note(f"deleted with it: {took}, which only it referred to")
        self.removed[id(element)] = ctx.index
        for child in element.iter():
            self.removed.setdefault(id(child), ctx.index)
        for p in element.iter(addr.W_P):
            pid = ids.paragraph_id(p)
            if pid is not None:
                self.removed_ids.setdefault(pid, ctx.index)
        element.getparent().remove(element)

    def attached(self, part: Any, element: Any) -> bool:
        """Whether ``element`` still sits in ``part``'s XML: an earlier
        operation may have taken it out (a merge swallows a cell)."""
        # Not getroottree(): lxml keeps a removed element in its old document.
        ancestors = list(element.iterancestors())
        return bool(ancestors) and ancestors[-1] is part.element


_ANNOTATION_HOLDERS = frozenset(_W + tag for tag in ("footnote", "endnote", "comment"))


def _search(book: Book, kind: str, expected: str, near: Any = None) -> list[tuple[Any, Any]]:
    """Blocks that start with ``expected``: in the note or comment ``near``
    sits in, else in the body, headers and footers."""
    holder = next((a for a in near.iterancestors() if a.tag in _ANNOTATION_HOLDERS), None) \
        if near is not None else None
    roots = [(None, holder)] if holder is not None else addr.iter_paragraph_parts(book.doc)
    found = []
    for part, root in roots:
        tag = addr.W_TBL if kind == addr.TABLE else addr.W_P
        for el in root.iter(tag):
            if ids.is_fallback_copy(el):
                continue
            if matches(book.doc, kind, el, expected):
                found.append((part, el))
    return found


def _described(book: Book, hits: list[tuple[Any, Any]]) -> str:
    shown = [addr.address_of(book.doc, el) or "?" for _, el in hits[:_CANDIDATES_SHOWN]]
    more = f" and {len(hits) - _CANDIDATES_SHOWN} more" if len(hits) > _CANDIDATES_SHOWN else ""
    return ", ".join(shown) + more


def _rescue(book: Book, ctx: Any, kind: str, expected: str | None, why: str, *, destructive: bool,
            target: str, near: Any = None, near_part: Any = None) -> addr.Located:
    """A unique paragraph that starts with ``expected``, or a failure. The
    search stays in the note or comment ``near`` (the target's old block)
    sits in."""
    if expected is None:
        raise OpError(Code.NOT_FOUND, why, location=f"target {target}",
                      hint="Read the document again and use the new address, or give expect_text so a "
                           "moved paragraph can be found.")
    hits = [(part if part is not None else near_part, el) for part, el in _search(book, kind, expected, near)]
    if len(hits) == 1 and not destructive:
        part, el = hits[0]
        where = addr.address_of(book.doc, el) or "a paragraph"
        ctx.note(f"{why}; used {where}, which starts with {expected[:60]!r}")
        return addr.Located(kind, el, part, el.getparent(), where)
    if len(hits) == 1:
        where = addr.address_of(book.doc, hits[0][1]) or "a paragraph"
        raise OpError(Code.NOT_FOUND, f"{why}; {where} starts with {expected[:60]!r}",
                      location=f"target {target}",
                      hint=f"This operation changes its target, so it does not guess: read the document "
                           f"again, check {where}, and use it as target.")
    count = f"{len(hits)} paragraphs start with {expected[:60]!r} ({_described(book, hits)})" if hits else \
        f"no paragraph starts with {expected[:60]!r}"
    raise OpError(Code.NOT_FOUND, f"{why}, and {count}", location=f"target {target}",
                  hint="Read the document again and use the new address.")


def locate(book: Book, ctx: Any, op: dict, *, kinds: tuple[str, ...], destructive: bool = True) -> addr.Located:
    """The block ``op["target"]`` names, checked against ``expect_text``.

    ``kinds`` are the address kinds the operation accepts. An operation
    that changes or removes its target (``destructive``, the default) never
    acts on a guess; one that only places a new block beside a paragraph may
    find that paragraph again by its text.
    """
    target = op["target"]
    expected = op.get("expect_text")
    address = addr.parse(target)
    key = str(target)
    if address.para_id is not None:
        located = _by_id(book, ctx, address, expected, destructive=destructive, target=key)
    else:
        found = book.snapshot.get(key)
        if found is None:
            found = addr.walk(book.doc, address)
        if isinstance(found, OpError):
            raise found
        located = found
        if id(located.element) in book.removed:
            raise OpError(Code.NOT_FOUND, f"{key} was deleted by op #{book.removed[id(located.element)]}",
                          location=f"target {key}",
                          hint="Positional targets name the document as the call began; reach a later "
                               "paragraph by its address in the read.")
        if located.kind != addr.PART and not book.attached(located.part, located.element):
            raise OpError(Code.NOT_FOUND, f"{key} was removed by an earlier operation of this call (a merge "
                          "takes in the cells it covers)", location=f"target {key}",
                          hint="Target the block that took its place, such as the merged cell.")
        if expected is not None and not matches(book.doc, located.kind, located.element, expected):
            why = f"{key} does not start with {expected[:60]!r} now"
            if located.kind not in (addr.PARAGRAPH, addr.TABLE):
                raise OpError(Code.BAD_VALUE, why, location=f"target {key}",
                              hint="Read the document again and use the address it shows now.")
            located = _rescue(book, ctx, located.kind, expected, why, destructive=destructive, target=key,
                              near=located.element, near_part=located.part)
    if located.kind not in kinds:
        names = {addr.PARAGRAPH: "a paragraph (p3)", addr.TABLE: "a table (t1)", addr.ROW: "a row (t1/r2)",
                 addr.CELL: "a cell (t1/r2/c0)", addr.PART: "a header, footer, note or comment (header0, footnote1)",
                 addr.CONTROL: "a content control (sdt0)", addr.CUSTOM: "a custom XML block (xml0)",
                 addr.TEXTBOX: "a text box (p4/textbox0)"}
        accepted = " or ".join(names[k] for k in kinds)
        raise OpError(Code.BAD_VALUE, f"target {key} is {names[located.kind].split(' (')[0]}, not {accepted}",
                      location=f"target {key}", hint=f"Give {accepted}.")
    if located.kind == addr.TEXTBOX or _in_textbox(located.element):
        raise OpError(Code.UNSUPPORTED, f"{key} is inside a text box", location=f"target {key}",
                      hint="Text boxes can be changed with search_and_replace only.")
    if getattr(ctx, "op", None) not in IN_COMMENTS and _in_comment(located.element):
        raise OpError(Code.UNSUPPORTED, f"{key} is inside a comment, where only the text changes",
                      location=f"target {key}",
                      hint="Change a comment's words with set_paragraph_text or search_and_replace (scope: "
                           "comments); reply with reply_to_comment.")
    return located


# Inside a comment only these act: a comment keeps its paragraphs, so the
# reply threads, which name its last paragraph, stay linked.
IN_COMMENTS = frozenset({"set_paragraph_text", "set_paragraph_format", "reply_to_comment", "resolve_comment",
                         "delete_comment"})


def _in_comment(element: Any) -> bool:
    return element.tag == _W + "comment" or any(a.tag == _W + "comment" for a in element.iterancestors())


def _in_textbox(element: Any) -> bool:
    return any(a.tag == addr.W_TXBX_CONTENT for a in element.iterancestors())


def _by_id(book: Book, ctx: Any, address: addr.Address, expected: str | None, *, destructive: bool,
           target: str) -> addr.Located:
    if address.para_id in book.removed_ids:
        raise OpError(Code.NOT_FOUND, f"#{address.para_id} was deleted by op #{book.removed_ids[address.para_id]}",
                      location=f"target {target}", hint="Target another paragraph.")
    hits = ids.find_by_id(book.doc, address.para_id)
    if len(hits) > 1:
        raise OpError(Code.BAD_VALUE, f"two paragraphs share id {address.para_id} ({_described(book, hits)})",
                      location=f"target {target}", hint="Use the position instead.")
    if hits:
        part, el = hits[0]
        where = addr.address_of(book.doc, el) or target
        if expected is not None and not matches(book.doc, addr.PARAGRAPH, el, expected):
            now = _normal(plain_rendering(book.doc, el))[:40]
            if destructive:
                raise OpError(Code.BAD_VALUE,
                              f"#{address.para_id} now starts with {now!r}, not {expected[:40]!r}: someone changed "
                              "it since you read it", location=f"target {target}",
                              hint="Read it again before you change it.")
            ctx.warn(f"#{address.para_id} now starts with {now!r}, not {expected[:40]!r}.")
        return addr.Located(addr.PARAGRAPH, el, part, el.getparent(), where)
    if address.segments:
        try:
            located = addr.walk(book.doc, addr.Address(address.part, address.section, address.kind,
                                                       address.segments))
        except OpError:
            located = None
        if located is not None and expected is not None and matches(book.doc, located.kind, located.element,
                                                                    expected):
            ctx.note(f"#{address.para_id} is gone; used {located.address}, which starts as expected")
            return located
    return _rescue(book, ctx, addr.PARAGRAPH, expected, f"no paragraph has id {address.para_id} now",
                   destructive=destructive, target=target)


def legacy_index(book: Book, ctx: Any, key: str, index: int, element: Any, *, tables: bool = False) -> None:
    """Warn when ``index`` (the live count) now names a different block than
    it did when the call began, because an earlier operation moved it."""
    initial = book.initial_tables if tables else book.initial_paragraphs
    if element is None or not 0 <= index < len(initial) or initial[index] is element:
        return
    what = "table" if tables else "paragraph"
    seg = "t" if tables else "p"
    ctx.warn(f"{key} {index} counts the document as it is now: an earlier operation of this call added or "
             f"removed a {what} before it, so it is not the {what} the read showed as {seg}{index}. Use target: "
             f"\"{seg}{index}\" for the read's numbering.")


# ---------------------------------------------------------------------------
# Placing new blocks
# ---------------------------------------------------------------------------


def _container_bounds(kind: str, element: Any) -> tuple[Any, Any | None]:
    """(the element new blocks go into, the first child that must stay first)."""
    holder = addr.inner(kind, element) if kind in (addr.CONTROL, addr.CUSTOM) else element
    lead = None
    for tag in (_W + "tcPr", _W + "customXmlPr"):
        found = holder.find(tag)
        if found is not None:
            lead = found
    return holder, lead


class Cursor:
    """Where the next block of an operation goes: after the target (or
    before it, or at a container's start or end), each new block after the
    one before it. Without a target, at the end of the body."""

    def __init__(self, book: Book, part: Any, *, anchor: Any = None, mode: str = END, holder: Any = None,
                 lead: Any = None) -> None:
        self.book = book
        self.doc = book.doc
        self.part = part
        self.anchor = anchor
        self.mode = mode
        self.holder = holder if holder is not None else book.doc.element.body
        self.lead = lead

    def place(self, element: Any) -> None:
        """Put a new block where the cursor stands and move past it."""
        if self.anchor is not None:
            if self.mode == BEFORE:
                self.anchor.addprevious(element)
            else:
                self.anchor.addnext(element)
                self.anchor = element
        elif self.mode == START:
            if self.lead is not None:
                self.lead.addnext(element)
            else:
                self.holder.insert(0, element)
            self.anchor, self.mode = element, AFTER
        else:
            sect_pr = self.holder.find(addr.W_SECT_PR) if self.holder.tag == _W + "body" else None
            if sect_pr is not None:
                sect_pr.addprevious(element)
            else:
                self.holder.append(element)
        if element.getparent() is not None and element.getparent().tag == _W + "tc":
            _keep_cell_paragraph(element.getparent())

    def paragraph(self) -> Any:
        """A new, empty python-docx paragraph at the cursor."""
        from docx.oxml import OxmlElement
        from docx.text.paragraph import Paragraph

        if self.anchor is None and self.mode == END and self.holder is self.doc.element.body:
            return self.doc.add_paragraph()
        p = OxmlElement("w:p")
        self.place(p)
        return Paragraph(p, PartParent(self.part))

    def table(self, rows: int, cols: int) -> Any:
        """A new python-docx table at the cursor."""
        table = self.doc.add_table(rows=rows, cols=cols)
        if not (self.anchor is None and self.mode == END and self.holder is self.doc.element.body):
            self.place(table._tbl)
        return table


def cursor(book: Book, ctx: Any, op: dict, *, allow_containers: bool = True) -> Cursor:
    """The cursor for an operation's ``target`` and ``position``."""
    position = (op.get("position") or "").strip().lower() or None
    if position is not None and position not in POSITIONS:
        raise OpError(Code.BAD_VALUE, f"position '{op['position']}' is not one of: {', '.join(POSITIONS)}",
                      hint="Give position as after, before, start or end.")
    if op.get("target") is None:
        if position is not None:
            raise OpError(Code.BAD_VALUE, "position needs a target",
                          hint="Give target (an address from the read) with position.")
        return Cursor(book, book.doc.part)
    kinds = (addr.PARAGRAPH, addr.TABLE, addr.CONTROL, addr.CUSTOM)
    if allow_containers:
        kinds = (*kinds, addr.PART, addr.CELL)
    located = locate(book, ctx, op, kinds=kinds, destructive=False)
    book.touch(located.part)
    if located.kind in _CONTAINER_KINDS:
        mode = position or END
        if mode not in (START, END):
            raise OpError(Code.BAD_VALUE, f"{located.address} is a container: position is start or end",
                          hint="Give position: start or end, or target a block inside it.")
        holder, lead = _container_bounds(located.kind, located.element)
        return Cursor(book, located.part, mode=mode, holder=holder, lead=lead)
    mode = position or AFTER
    if mode not in (AFTER, BEFORE):
        raise OpError(Code.BAD_VALUE, f"{located.address} is a block: position is after or before",
                      hint="Give position: after or before; start and end are for containers "
                           "(a cell, header0, sdt0).")
    return Cursor(book, located.part, anchor=located.element, mode=mode)


def _keep_cell_paragraph(tc: Any) -> None:
    """A table cell ends with a paragraph (Word repairs a cell that ends with a
    table)."""
    from docx.oxml import OxmlElement

    last = tc[-1] if len(tc) else None
    if last is None or last.tag != addr.W_P:
        tc.append(OxmlElement("w:p"))


# ---------------------------------------------------------------------------
# Removing blocks
# ---------------------------------------------------------------------------


def field_balance(p: Any) -> int:
    """Field begins minus ends in a paragraph: not zero when a field (a TOC)
    spans paragraphs."""
    balance = 0
    for fld in p.iter(_W + "fldChar"):
        kind = fld.get(_W + "fldCharType")
        balance += 1 if kind == annotations.FIELD_BEGIN else -1 if kind == annotations.FIELD_END else 0
    return balance


def remove_paragraph(book: Book, ctx: Any, p: Any, where: str) -> None:
    """Delete a paragraph, keeping what the document's structure needs."""
    parent = p.getparent()
    if parent.tag in (_W + "tc", _W + "hdr", _W + "ftr", _W + "txbxContent", _W + "footnote", _W + "endnote") and \
            len(parent.findall(addr.W_P)) == 1 and parent.find(addr.W_TBL) is None:
        raise OpError(Code.BAD_VALUE, f"{where} is the only paragraph of its {_holder_name(parent.tag)}",
                      location=where, hint="Empty it with set_paragraph_text text: \"\" instead.")
    remaining = [c for c in parent.iterchildren(addr.W_P, addr.W_TBL, addr.W_SDT) if c is not p]
    if parent.tag in _MUST_END_WITH_PARAGRAPH and remaining and remaining[-1].tag == addr.W_TBL:
        raise OpError(Code.BAD_VALUE, f"{where} is the paragraph after the last table of its "
                      f"{_holder_name(parent.tag)}, which keeps the paragraph after its table (Word repairs one "
                      "that ends with a table)", location=where,
                      hint="Empty it with set_paragraph_text text: \"\" instead.")
    if field_balance(p) != 0:
        raise OpError(Code.BAD_VALUE, f"{where} holds one end of a field that spans paragraphs (a table of "
                      "contents)", location=where, hint="Delete every paragraph of the field together, or "
                      "leave it in place.")
    sect_pr = p.find(f"{_W}pPr/{_W}sectPr")
    if sect_pr is not None:
        previous = p.getprevious()
        while previous is not None and previous.tag != addr.W_P:
            previous = previous.getprevious()
        if previous is None or previous.find(f"{_W}pPr/{_W}sectPr") is not None:
            raise OpError(Code.BAD_VALUE, f"{where} ends a section, and no paragraph before it can carry the "
                          "section's settings", location=where,
                          hint="Delete the paragraphs around it instead, or keep this one.")
        from docx.oxml import OxmlElement

        ppr = previous.find(f"{_W}pPr")
        if ppr is None:
            ppr = insert_child(previous, OxmlElement("w:pPr"))
        insert_child(ppr, sect_pr)
        ctx.note(f"the section break {where} carried moved to the paragraph before it")
    book.remove(ctx, p)


_MUST_END_WITH_PARAGRAPH = frozenset(_W + tag for tag in ("tc", "hdr", "ftr", "txbxContent"))


def _holder_name(tag: str) -> str:
    return {_W + "tc": "table cell", _W + "hdr": "header", _W + "ftr": "footer",
            _W + "txbxContent": "text box", _W + "footnote": "footnote",
            _W + "endnote": "endnote"}.get(tag, "container")
