"""Addresses of the blocks of a Word document.

An address counts blocks from 0, per kind, among a container's children:
``p`` paragraphs, ``t`` tables, ``sdt`` block content controls, ``xml``
custom XML wrappers; inside a table ``r`` rows and ``c`` grid columns;
inside a paragraph ``textbox`` text boxes. Segments join with ``/``
(``t2/r3/c1/p0``). A path starts in the body unless it names a part:
``header<S>``, ``footer<S>``, with ``.first`` or ``.even`` for those kinds;
``footnote<N>``, ``endnote<N>``, ``comment<N>``, counted from 1 as a person
sees them (notes by the order of their references, comments by the order
of theirs, unreferenced ones after).
``#1A2B3C4D`` names a paragraph by its ``w14:paraId``; ``p12#1A2B3C4D``
carries both (the id wins). The enumeration that prints addresses in the
read and the one that resolves them are the same functions, so they never
disagree.

Header and footer parts are reached through each section's references,
never through python-docx's accessors, which create a part on read.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from results import Code, OpError

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_W = f"{{{_W_NS}}}"
_MC = f"{{{_MC_NS}}}"

W_P = _W + "p"
W_TBL = _W + "tbl"
W_TR = _W + "tr"
W_TC = _W + "tc"
W_SDT = _W + "sdt"
W_SDT_CONTENT = _W + "sdtContent"
W_CUSTOM_XML = _W + "customXml"
W_TXBX_CONTENT = _W + "txbxContent"
W_SECT_PR = _W + "sectPr"
W_VAL = _W + "val"
_MC_FALLBACK = _MC + "Fallback"

PARAGRAPH = "p"
TABLE = "t"
ROW = "r"
CELL = "c"
CONTROL = "sdt"
CUSTOM = "xml"
TEXTBOX = "textbox"
PART = "part"

BODY = "body"
HEADER = "header"
FOOTER = "footer"
FOOTNOTE = "footnote"
ENDNOTE = "endnote"
COMMENT = "comment"
ANNOTATION_PARTS = (FOOTNOTE, ENDNOTE, COMMENT)
DEFAULT_KIND = "default"
FIRST_KIND = "first"
EVEN_KIND = "even"
KINDS = (DEFAULT_KIND, FIRST_KIND, EVEN_KIND)
_VMERGE_CONTINUE = "continue"

_BLOCK_TAGS = {W_P: PARAGRAPH, W_TBL: TABLE, W_SDT: CONTROL, W_CUSTOM_XML: CUSTOM}
# What each segment may follow: a container holds blocks, a table rows, a
# row cells, a paragraph text boxes.
_CONTAINER_KINDS = frozenset({PART, CELL, CONTROL, CUSTOM, TEXTBOX})
_FOLLOWS = {PARAGRAPH: _CONTAINER_KINDS, TABLE: _CONTAINER_KINDS, CONTROL: _CONTAINER_KINDS,
            CUSTOM: _CONTAINER_KINDS, ROW: frozenset({TABLE}), CELL: frozenset({ROW}),
            TEXTBOX: frozenset({PARAGRAPH})}

_SEGMENT_RE = re.compile(r"(textbox|sdt|xml|p|t|r|c)(\d+)")
_PART_RE = re.compile(r"(body|header|footer|footnote|endnote|comment)(\d*)(?:\.(default|first|even))?")
_ID_RE = re.compile(r"[0-9a-f]{1,8}", re.IGNORECASE)
_EXAMPLES = "p12, t2/r3/c1/p0, header0/p0, footnote1/p0 or #1A2B3C4D"
_REL_FOR = {HEADER: "headerReference", FOOTER: "footerReference"}


def _w(tag: str) -> str:
    return _W + tag


def _bad(target: Any, why: str = "") -> OpError:
    reason = f" ({why})" if why else ""
    return OpError(Code.BAD_VALUE, f"target {target!r} is not an address{reason}",
                   hint=f"Use an address the read prints, such as {_EXAMPLES}.")


@dataclass(frozen=True)
class Address:
    """A parsed target: the part, the segments, and an optional paragraph id."""

    part: str = BODY
    # A header's or footer's section from 0; a note's or comment's number from 1.
    section: int | None = None
    kind: str = DEFAULT_KIND
    segments: tuple[tuple[str, int], ...] = ()
    para_id: str | None = None

    @property
    def positional(self) -> bool:
        return bool(self.segments) or self.part != BODY

    def text(self) -> str:
        head = "" if self.part == BODY else self.part_label()
        path = "/".join(f"{k}{n}" for k, n in self.segments)
        joined = "/".join(x for x in (head, path) if x)
        return joined + (f"#{self.para_id}" if self.para_id else "")

    def part_label(self) -> str:
        if self.part == BODY:
            return BODY
        suffix = "" if self.kind == DEFAULT_KIND else f".{self.kind}"
        return f"{self.part}{self.section}{suffix}"


def parse(target: Any) -> Address:
    """``target`` as an :class:`Address`, or a failure showing the shape."""
    if isinstance(target, bool):
        raise _bad(target)
    if isinstance(target, int):
        if target < 0:
            raise _bad(target, "addresses count from 0; the read shows the last one")
        return Address(segments=((PARAGRAPH, target),))
    if not isinstance(target, str):
        raise _bad(target)
    text = re.sub(r"\s+", "", target).lower()
    if text.isdigit():
        return Address(segments=((PARAGRAPH, int(text)),))
    para_id = None
    if "#" in text:
        text, _, raw_id = text.partition("#")
        if not _ID_RE.fullmatch(raw_id):
            raise _bad(target, "an id is up to 8 hex digits after #")
        para_id = raw_id.upper().rjust(8, "0")
    if not text:
        if para_id is None:
            raise _bad(target)
        return Address(para_id=para_id)
    pieces = text.split("/")
    part, section, kind = BODY, None, DEFAULT_KIND
    match = _PART_RE.fullmatch(pieces[0])
    if match:
        part = match.group(1)
        if part in ANNOTATION_PARTS:
            if not match.group(2) or match.group(3):
                raise _bad(target, f"name the {part}: {part}1 is the first")
            section = int(match.group(2))
            if section == 0:
                raise _bad(target, "notes and comments count from 1, as people see them")
        elif part != BODY:
            if not match.group(2):
                raise _bad(target, f"name the section: {part}0 is the first section's")
            section = int(match.group(2))
            kind = match.group(3) or DEFAULT_KIND
        elif match.group(2) or match.group(3):
            raise _bad(target)
        pieces = pieces[1:]
    segments: list[tuple[str, int]] = []
    previous = PART
    for piece in pieces:
        seg = _SEGMENT_RE.fullmatch(piece)
        if not seg:
            raise _bad(target, f"'{piece}' is not a segment")
        kind_name, number = seg.group(1), int(seg.group(2))
        if previous not in _FOLLOWS[kind_name]:
            raise _bad(target, f"'{piece}' cannot follow {previous if previous != PART else 'the start'}")
        segments.append((kind_name, number))
        previous = kind_name
    if para_id is not None and (not segments or segments[-1][0] != PARAGRAPH):
        raise _bad(target, "#id names a paragraph, so it follows a p segment")
    return Address(part=part, section=section, kind=kind, segments=tuple(segments), para_id=para_id)


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def blocks(container: Any) -> list[tuple[str, int, Any]]:
    """The container's blocks in order: (kind, number among that kind, element)."""
    counts: dict[str, int] = {}
    out = []
    for child in container.iterchildren():
        kind = _BLOCK_TAGS.get(child.tag)
        if kind is None:
            continue
        n = counts.get(kind, 0)
        counts[kind] = n + 1
        out.append((kind, n, child))
    return out


def inner(kind: str, element: Any) -> Any:
    """The element whose children are the blocks inside a container block."""
    if kind == CONTROL:
        content = element.find(W_SDT_CONTENT)
        return content if content is not None else element
    return element


def _unwrap(element: Any, tag: str) -> Iterator[Any]:
    """Elements of ``tag`` under ``element``, entering content controls and
    custom XML wrappers (repeating sections hold rows, controls hold cells)."""
    for child in element.iterchildren():
        if child.tag == tag:
            yield child
        elif child.tag == W_SDT:
            content = child.find(W_SDT_CONTENT)
            if content is not None:
                yield from _unwrap(content, tag)
        elif child.tag == W_CUSTOM_XML:
            yield from _unwrap(child, tag)


def rows(tbl: Any) -> list[Any]:
    return list(_unwrap(tbl, W_TR))


def row_is_wrapped(tr: Any, tbl: Any) -> bool:
    return tr.getparent() is not tbl


def _int_val(element: Any, path: str, default: int) -> int:
    found = element.find(path)
    if found is None:
        return default
    try:
        return int(found.get(W_VAL, default))
    except (TypeError, ValueError):
        return default


def _vmerge(tc: Any) -> str | None:
    found = tc.find(f"{_w('tcPr')}/{_w('vMerge')}")
    if found is None:
        return None
    return found.get(W_VAL, _VMERGE_CONTINUE)


@dataclass
class GridCell:
    """One cell of a row by grid column: where it starts, how many columns it
    covers, and whether it continues the cell above (a vertical merge)."""

    tc: Any
    start: int
    span: int
    continues: bool


def grid(tr: Any) -> tuple[int, list[GridCell]]:
    """(columns skipped before the first cell, the row's cells by grid column)."""
    before = _int_val(tr, f"{_w('trPr')}/{_w('gridBefore')}", 0)
    col = before
    cells = []
    for tc in _unwrap(tr, W_TC):
        span = max(1, _int_val(tc, f"{_w('tcPr')}/{_w('gridSpan')}", 1))
        cells.append(GridCell(tc, col, span, _vmerge(tc) == _VMERGE_CONTINUE))
        col += span
    return before, cells


def cell_at(tbl: Any, row_index: int, col: int) -> Any:
    """The w:tc that shows at grid column ``col`` of row ``row_index``: a
    vertical continuation answers with the cell where the merge starts."""
    all_rows = rows(tbl)
    tr = all_rows[row_index]
    before, cells = grid(tr)
    if col < before:
        raise OpError(Code.NOT_FOUND, f"c{col} is empty space before the row's first cell",
                      hint=f"The row starts at c{before}.")
    for cell in cells:
        if cell.start <= col < cell.start + cell.span:
            if cell.continues and row_index > 0:
                return cell_at(tbl, row_index - 1, cell.start)
            return cell.tc
    width = cells[-1].start + cells[-1].span if cells else before
    raise OpError(Code.NOT_FOUND, f"c{col} does not exist (the row has {width} columns)",
                  hint=f"Columns count from 0: c0 to c{max(width - 1, 0)}.")


def textboxes(p: Any) -> list[Any]:
    """The paragraph's own text boxes, each once: the mc:Choice copy, or the
    VML box when that is the only one. A text box inside a text box belongs
    to its own paragraph."""
    out = []
    for content in p.iter(W_TXBX_CONTENT):
        if any(a.tag == _MC_FALLBACK for a in content.iterancestors()):
            continue
        nearest = next(a for a in content.iterancestors(W_P))
        if nearest is p:
            out.append(content)
    return out


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


def sections(doc: Any) -> list[Any]:
    """Every w:sectPr of the body in document order (python-docx's rule)."""
    body = doc.element.body
    return [*body.xpath("./w:p/w:pPr/w:sectPr"), *body.xpath("./w:sectPr")]


def _reference(sect_pr: Any, part: str, kind: str) -> str | None:
    for ref in sect_pr.iterchildren(_w(_REL_FOR[part])):
        if ref.get(_W + "type", DEFAULT_KIND) == kind:
            return ref.get(f"{{{_R_NS}}}id")
    return None


def _related(doc: Any, r_id: str) -> Any | None:
    """The part a reference names; None when the relationship is missing
    or external (Word opens such files and shows no header)."""
    rel = doc.part.rels.get(r_id)
    if rel is None or rel.is_external:
        return None
    return rel.target_part


def story_part(doc: Any, part: str, section: int, kind: str = DEFAULT_KIND) -> Any:
    """The header or footer part that section ``section`` shows for ``kind``:
    its own reference, else the nearest earlier section's (Word's rule)."""
    all_sections = sections(doc)
    if not 0 <= section < len(all_sections):
        raise OpError(Code.NOT_FOUND, f"section {section} does not exist (the document has "
                      f"{len(all_sections)})", hint="Sections count from 0.")
    for sect_pr in reversed(all_sections[:section + 1]):
        r_id = _reference(sect_pr, part, kind)
        if r_id is not None:
            part = _related(doc, r_id)
            if part is not None:
                return part
    label = {DEFAULT_KIND: part, FIRST_KIND: f"first-page {part}", EVEN_KIND: f"even-page {part}"}[kind]
    raise OpError(Code.NOT_FOUND, f"section {section} has no {label}",
                  hint=f"Add one with add_{part}" + (f" kind: {kind}." if kind != DEFAULT_KIND else "."))


def story_parts(doc: Any) -> list[tuple[str, str, Any, list[int]]]:
    """Each distinct header and footer part once: (part kind, story kind,
    part, sections that show it), in section order."""
    seen: dict[int, tuple[str, str, Any, list[int]]] = {}
    all_sections = sections(doc)
    for part_kind in (HEADER, FOOTER):
        for kind in KINDS:
            current = None
            for index, sect_pr in enumerate(all_sections):
                r_id = _reference(sect_pr, part_kind, kind)
                if r_id is not None:
                    current = _related(doc, r_id) or current
                if current is None:
                    continue
                entry = seen.setdefault(id(current), (part_kind, kind, current, []))
                entry[3].append(index)
    return sorted(seen.values(), key=lambda e: (e[0] != HEADER, e[3][0], KINDS.index(e[1])))


_REFERENCE = {FOOTNOTE: "footnoteReference", ENDNOTE: "endnoteReference", COMMENT: "commentReference"}
_NOTE_SEPARATORS = frozenset({"separator", "continuationSeparator", "continuationNotice"})


def annotation_order(doc: Any, kind: str) -> list[tuple[Any, Any]]:
    """(part, element) of every footnote, endnote or comment in the order
    people number them: by the first reference in document order (the body,
    then headers and footers, then notes, Fallback copies skipped), then the
    ones no reference names, in the part's order. Reads only."""
    from formats.docx import annotations

    reltype = {FOOTNOTE: annotations.RT_FOOTNOTES, ENDNOTE: annotations.RT_ENDNOTES,
               COMMENT: annotations.RT_COMMENTS}[kind]
    part = annotations.first_related(doc, reltype)
    root = annotations.root_of_part(part) if part is not None else None
    if root is None:
        return []
    by_id: dict[str, Any] = {}
    for el in root.iterchildren(_w(kind)):
        if kind != COMMENT and el.get(_W + "type") in _NOTE_SEPARATORS:
            continue
        by_id.setdefault(el.get(_W + "id"), el)
    stories = [doc.element.body]
    if kind == COMMENT:
        stories += [p.element for _, _, p, _ in story_parts(doc)]
        for rt in (annotations.RT_FOOTNOTES, annotations.RT_ENDNOTES):
            stories += [r for r in map(annotations.root_of_part, annotations.related(doc, rt)) if r is not None]
    order: list[Any] = []
    seen: set[int] = set()
    for story in stories:
        for ref in story.iter(_w(_REFERENCE[kind])):
            if next(ref.iterancestors(_MC_FALLBACK), None) is not None:
                continue
            el = by_id.get(ref.get(_W + "id"))
            if el is not None and id(el) not in seen:
                seen.add(id(el))
                order.append(el)
    order += [el for el in by_id.values() if id(el) not in seen]
    return [(part, el) for el in order]


def _annotation_root(doc: Any, address: Address) -> tuple[Any, Any]:
    found = annotation_order(doc, address.part)
    number = address.section or 0
    if not 1 <= number <= len(found):
        plural = {FOOTNOTE: "footnotes", ENDNOTE: "endnotes", COMMENT: "comments"}[address.part]
        raise OpError(Code.NOT_FOUND, f"{address.part}{number} does not exist: the document has {len(found)} "
                      f"{plural}", location=f"target {address.text()}",
                      hint="Read the document again for the current addresses.")
    return found[number - 1]


def root_of(doc: Any, address: Address) -> tuple[Any, Any]:
    """(python-docx part, container element) where the address's path starts."""
    if address.part == BODY:
        return doc.part, doc.element.body
    if address.part in ANNOTATION_PARTS:
        return _annotation_root(doc, address)
    part = story_part(doc, address.part, address.section or 0, address.kind)
    return part, part.element


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass
class Located:
    """What an address names: its kind, the element, the part it lives in,
    and for a block the container element it is a child of."""

    kind: str
    element: Any
    part: Any
    container: Any | None
    address: str


def _not_found(address: Address, done: str, what: str, count: int) -> OpError:
    where = f" in {done}" if done else ""
    return OpError(Code.NOT_FOUND,
                   f"{address.text()} does not exist: there {'is' if count == 1 else 'are'} {count} "
                   f"{what}{where}",
                   location=f"target {address.text()}",
                   hint="Read the document again for the current addresses.")


_NAMES = {PARAGRAPH: "paragraphs", TABLE: "tables", CONTROL: "content controls", CUSTOM: "custom XML blocks",
          ROW: "rows", TEXTBOX: "text boxes"}


def walk(doc: Any, address: Address) -> Located:
    """Follow the address's path from its part; positions only (no id)."""
    part, element = root_of(doc, address)
    kind, container = PART, None
    done = ""
    for seg_kind, n in address.segments:
        if seg_kind in (PARAGRAPH, TABLE, CONTROL, CUSTOM):
            holder = inner(kind, element) if kind in (CONTROL, CUSTOM) else element
            found = [el for k, _, el in blocks(holder) if k == seg_kind]
            if n >= len(found):
                raise _not_found(address, done, _NAMES[seg_kind], len(found))
            container, element = holder, found[n]
        elif seg_kind == ROW:
            found = rows(element)
            if n >= len(found):
                raise _not_found(address, done, _NAMES[ROW], len(found))
            container, element = element, found[n]
        elif seg_kind == CELL:
            tbl = container
            row_index = rows(tbl).index(element)
            try:
                tc = cell_at(tbl, row_index, n)
            except OpError as exc:
                exc.location = f"target {address.text()}"
                raise
            container, element = None, tc
        elif seg_kind == TEXTBOX:
            found = textboxes(element)
            if n >= len(found):
                raise _not_found(address, done, _NAMES[TEXTBOX], len(found))
            container, element = element, found[n]
        kind = seg_kind
        done = f"{done}/{seg_kind}{n}".lstrip("/")
    return Located(kind, element, part, container, address.text())


def iter_paragraph_parts(doc: Any) -> Iterator[tuple[Any, Any]]:
    """(part, root element) of the body and of each header and footer part."""
    yield doc.part, doc.element.body
    for _, _, part, _ in story_parts(doc):
        yield part, part.element


def address_of(doc: Any, element: Any) -> str | None:
    """The address the read prints for ``element`` (a block, row or cell), or
    None when it lives where no address reaches."""
    chain = []
    node = element
    while node is not None:
        chain.append(node)
        node = node.getparent()
    root = chain[-1]
    head = ""
    if root is not doc.element and root.tag in _ANNOTATION_ROOTS:
        kind = _ANNOTATION_ROOTS[root.tag]
        holder = chain[-2] if len(chain) > 1 else None
        for n, (_, el) in enumerate(annotation_order(doc, kind), 1):
            if el is holder:
                head, start = f"{kind}{n}", el
                break
        else:
            return None
        if element is holder:
            return head
    elif root is not doc.element:
        for part_kind, kind, part, secs in story_parts(doc):
            if part.element is root:
                suffix = "" if kind == DEFAULT_KIND else f".{kind}"
                head = f"{part_kind}{secs[0]}{suffix}"
                break
        else:
            return None
        start = root
    else:
        start = doc.element.body
    path = _path_to(start, element)
    if path is None:
        return None
    return "/".join(x for x in (head, path) if x)


_ANNOTATION_ROOTS = {_w("footnotes"): FOOTNOTE, _w("endnotes"): ENDNOTE, _w("comments"): COMMENT}


def _path_to(container: Any, target: Any) -> str | None:
    """The segments from ``container`` (a container element) to ``target``."""
    for kind, n, el in blocks(container):
        seg = f"{kind}{n}"
        if el is target:
            return seg
        if kind == PARAGRAPH:
            if any(target is d for d in el.iter(target.tag)):
                for b, box in enumerate(textboxes(el)):
                    sub = _path_to(box, target)
                    if sub is not None:
                        return f"{seg}/{TEXTBOX}{b}/{sub}"
            continue
        if kind == TABLE:
            if not any(target is d for d in el.iter(target.tag)):
                continue
            for r, tr in enumerate(rows(el)):
                if tr is target:
                    return f"{seg}/{ROW}{r}"
                if not any(target is d for d in tr.iter(target.tag)):
                    continue
                _, cells = grid(tr)
                for cell in cells:
                    if cell.tc is target:
                        return f"{seg}/{ROW}{r}/{CELL}{cell.start}"
                    sub = _path_to(cell.tc, target)
                    if sub is not None:
                        return f"{seg}/{ROW}{r}/{CELL}{cell.start}/{sub}"
            continue
        if any(target is d for d in el.iter(target.tag)):
            sub = _path_to(inner(kind, el), target)
            if sub is not None:
                return f"{seg}/{sub}"
    return None


def iter_addressed(container: Any, prefix: str = "") -> Iterator[tuple[str, str, Any]]:
    """Every block under ``container`` with its address, in document order:
    (kind, address, element) for paragraphs and tables at any depth, inside
    cells, content controls, custom XML and text boxes. Works on any parsed
    tree, python-docx's or a plain lxml one."""
    for kind, n, el in blocks(container):
        address = f"{prefix}/{kind}{n}" if prefix else f"{kind}{n}"
        if kind == PARAGRAPH:
            yield kind, address, el
            for b, box in enumerate(textboxes(el)):
                yield from iter_addressed(box, f"{address}/{TEXTBOX}{b}")
        elif kind == TABLE:
            yield kind, address, el
            for r, tr in enumerate(rows(el)):
                _, cells = grid(tr)
                for cell in cells:
                    if not cell.continues:
                        yield from iter_addressed(cell.tc, f"{address}/{ROW}{r}/{CELL}{cell.start}")
        else:
            yield from iter_addressed(inner(kind, el), address)


def address_map(doc: Any) -> dict[Any, str]:
    """Every paragraph's and table's address in one pass over the body and
    each header and footer part: what the result notes need for many new
    blocks at once."""
    found = {el: address for _, address, el in iter_addressed(doc.element.body)}
    for part_kind, kind, part, secs in story_parts(doc):
        head = f"{part_kind}{secs[0]}" + ("" if kind == DEFAULT_KIND else f".{kind}")
        found.update({el: address for _, address, el in iter_addressed(part.element, head)})
    for kind in ANNOTATION_PARTS:
        for n, (_, el) in enumerate(annotation_order(doc, kind), 1):
            found[el] = f"{kind}{n}"
            found.update({x: address for _, address, x in iter_addressed(el, f"{kind}{n}")})
    return found
