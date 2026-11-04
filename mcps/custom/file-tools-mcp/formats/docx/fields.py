"""Bookmarks, internal links and the table of contents.

A table of contents is Word's TOC field with real entries: one paragraph
per heading (style TOC N), its number and text, a dot leader and a nested
PAGEREF field whose result is the heading's page, the whole entry a link to
the heading's ``_Toc`` bookmark. Entries are built when the batch ends, from
the headings as it left them. Page numbers come from a LibreOffice layout:
worker A stages the document and a probe copy whose every footer prints its
page label; the parent renders the probe; worker B reads which page each
entry's link lands on and writes that page's label. When no layout is
available the entries stay without numbers, and Word fills them when the
table is updated.
"""

import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from catalog import KeySpec, OpSpec
from formats.docx import address as addr, annotations
from formats.docx.notes import find_text
from formats.docx.opkit import AT, PLACE, add_field, created
from formats.docx.styles import CHARACTER, ensure
from formats.docx.targets import cursor, locate
from results import Code, NoOp, OpCtx, OpError

W_NS = annotations.W_NS
_W = f"{{{W_NS}}}"
_W_P = _W + "p"
_W_R = _W + "r"
_W_ID = _W + "id"
_W_NAME = _W + "name"
_BOOKMARK_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_TOC = re.compile(r"^\s*TOC\b", re.IGNORECASE)
_SWITCH = re.compile(r"\\([A-Za-z])(?:\s+\"([^\"]*)\")?")
_UNDERSTOOD = frozenset("ohznuwxpt")
_PLACEHOLDER_1_3 = "Update table of contents to see entries"
_PAGE_PLACEHOLDER = "000"
_TOC_OPS = frozenset({"add_table_of_contents", "update_table_of_contents"})
# Operations whose fields may need a page number from a layout: they take
# the two-worker path, and the first one's result carries the page note.
PAGE_OPS = _TOC_OPS | {"add_cross_reference", "update_fields"}
_LABEL = re.compile(r"~~pg:\s*(.+?)\s*:pg~~")
_MARK = re.compile(r"~~bm:(\S+?):bm~~")
DEFAULT_TITLE = "Table of Contents"
# A4 with 1 inch margins, in twips, when the section says nothing.
_DEFAULT_WIDTH = 9026


def _w(tag: str) -> str:
    return _W + tag


# ---------------------------------------------------------------------------
# Bookmarks and links
# ---------------------------------------------------------------------------


def bookmark_names(doc: Any) -> dict[str, str]:
    """Every bookmark name in every story part: folded name -> the name as
    the document writes it (Word compares names without regard to case,
    LibreOffice and Collabora with it, so a link names its bookmark in the
    bookmark's own case)."""
    names: dict[str, str] = {}
    for part in annotations.story_parts(doc):
        root = annotations.root_of_part(part)
        if root is not None:
            for b in root.iter(_w("bookmarkStart")):
                name = b.get(_W_NAME) or ""
                names.setdefault(name.casefold(), name)
    return names


def _bookmark(job: Any, p: Any, name: str, span: tuple | None = None) -> None:
    from docx.oxml import parse_xml

    from formats.docx.revisions import isolate

    value = job.ids.next()
    ns = f'xmlns:w="{W_NS}"'
    start = parse_xml(f'<w:bookmarkStart {ns} w:id="{value}"/>')
    start.set(_W_NAME, name)
    end = parse_xml(f'<w:bookmarkEnd {ns} w:id="{value}"/>')
    if span is not None:
        flat, s, e = span
        runs = isolate(flat, s, e, job.ids)
        runs[0].addprevious(start)
        runs[-1].addnext(end)
    else:
        annotations.put_start(start, p)
        p.append(end)


def _op_add_bookmark(job: Any, ctx: OpCtx, op: dict) -> None:
    name = op["name"]
    if not _BOOKMARK_NAME.fullmatch(name):
        raise OpError(Code.BAD_VALUE, f"name {name!r} is not a bookmark name Word accepts",
                      hint="Start with a letter; use letters, digits and _ only; at most 40 characters.")
    if name.casefold() in bookmark_names(job.doc):
        raise OpError(Code.BAD_VALUE, f"the document already has a bookmark named {name!r}",
                      hint="Bookmark names are unique, whatever their case: give another name.")
    located = locate(job.book, ctx, op, kinds=(addr.PARAGRAPH,))
    span = find_text(located.element, op["on_text"], "on_text") if op.get("on_text") is not None else None
    _bookmark(job, located.element, name, span)
    job.book.touch(located.part)
    ctx.note(f"bookmark {name} on {located.address}; link to it with add_hyperlink bookmark: {name!r}")


def link_target(job: Any, name: str) -> str:
    """The bookmark a link names, in the bookmark's own case: one the
    document has or this call's add_bookmark makes."""
    folded = name.casefold()
    if folded == "_top":
        return "_top"
    exact = bookmark_names(job.doc).get(folded) or job.planned_bookmarks.get(folded)
    if exact is not None:
        return exact
    raise OpError(Code.NOT_FOUND, f"the document has no bookmark named {name!r}",
                  hint="Add it with add_bookmark (in this call or before), or link to a URL.")


# ---------------------------------------------------------------------------
# Finding a table of contents
# ---------------------------------------------------------------------------


@dataclass
class Toc:
    """One TOC field of the body: the paragraphs it begins and ends in and
    its instruction."""

    begin: Any
    end: Any
    instruction: str
    op_index: int | None = None
    names: list[tuple[str, str]] = field(default_factory=list)

    def switches(self) -> dict[str, str]:
        return {m.group(1).lower(): m.group(2) or "" for m in _SWITCH.finditer(self.instruction)}

    def understood(self) -> bool:
        return set(self.switches()) <= _UNDERSTOOD

    def style_levels(self) -> dict[str, int]:
        """The \\t switch: style names (folded) and the level each lists at."""
        items = [x.strip() for x in re.split(r"[,;]", self.switches().get("t", ""))]
        found: dict[str, int] = {}
        for name, level in zip(items[::2], items[1::2], strict=False):
            if name and level.isdigit() and 1 <= int(level) <= 9:
                found[name.casefold()] = int(level)
        return found

    def levels(self) -> tuple[int, int]:
        switches = self.switches()
        if "o" not in switches and "t" in switches:
            return 1, 0
        span = switches.get("o", "1-3") or "1-9"
        low, _, high = span.partition("-")
        try:
            return max(1, int(low)), min(9, int(high or low))
        except ValueError:
            return 1, 3

    @property
    def page_numbers(self) -> bool:
        return "n" not in self.switches()

    @property
    def links(self) -> bool:
        return "h" in self.switches()


def find_tocs(doc: Any) -> list[Toc]:
    """Every TOC field in the body, in document order (inside a content
    control too), with the paragraphs where it begins and ends."""
    found: list[Toc] = []
    stack: list[dict] = []
    for p in doc.element.body.iter(_W_P):
        if next(p.iterancestors(_w("txbxContent")), None) is not None:
            continue
        for el in p.iter(_w("fldChar"), _w("instrText")):
            if el.tag == _w("instrText"):
                if stack:
                    stack[-1]["instr"] += el.text or ""
                continue
            kind = el.get(_w("fldCharType"))
            if kind == annotations.FIELD_BEGIN:
                stack.append({"instr": "", "begin": p})
            elif kind == annotations.FIELD_END and stack:
                item = stack.pop()
                if _TOC.match(item["instr"]):
                    found.append(Toc(item["begin"], p, item["instr"].strip()))
    return found


# ---------------------------------------------------------------------------
# Building the entries
# ---------------------------------------------------------------------------


def _text_width(doc: Any, toc: Toc) -> int:
    """The text width (twips) of the section the TOC sits in: the first
    section break at or after it, else the body's own."""
    body = doc.element.body
    top = toc.end
    while top.getparent() is not None and top.getparent() is not body:
        top = top.getparent()
    sect = None
    node = top
    while node is not None:
        if node.tag == _W_P and node.find(f"{_W}pPr/{_W}sectPr") is not None:
            sect = node.find(f"{_W}pPr/{_W}sectPr")
            break
        node = node.getnext()
    if sect is None:
        sect = body.find(_w("sectPr"))
    if sect is None:
        return _DEFAULT_WIDTH
    size, margins = sect.find(_w("pgSz")), sect.find(_w("pgMar"))
    try:
        width = int(size.get(_w("w"))) - int(margins.get(_w("left"))) - int(margins.get(_w("right")))
    except (AttributeError, TypeError, ValueError):
        return _DEFAULT_WIDTH
    return width if width > 1440 else _DEFAULT_WIDTH


def _headings(doc: Any, toc: Toc, low: int, high: int) -> list[tuple[Any, int, str, str]]:
    """(paragraph, level, number label, text) of each heading the TOC
    lists, in document order: outline levels low to high, and the styles
    its \\t switch names at their levels, in the body, tables and content
    controls, never text boxes or the TOC itself."""
    from docx.styles import BabelFish

    from formats.docx.numbering import ListMarkers
    from formats.docx.text import HeadingLevels, paragraph_text

    by_style = toc.style_levels()
    style_names = {s.styleId: BabelFish.internal2ui(s.name_val or s.styleId or "").casefold()
                   for s in doc.styles.element.iterchildren(_w("style")) if s.styleId} if by_style else {}
    levels = HeadingLevels(doc)
    markers = ListMarkers(doc)
    inside = False
    found = []
    for p in doc.element.body.iter(_W_P):
        if next(p.iterancestors(_w("txbxContent")), None) is not None or \
                next(p.iterancestors("{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"),
                     None) is not None:
            continue
        label = markers.marker(p).strip()
        if p is toc.begin:
            inside = True
        if inside:
            if p is toc.end:
                inside = False
            continue
        level = levels.level(p)
        if not level or not low <= level <= high:
            style = p.find(f"{_W}pPr/{_W}pStyle")
            level = by_style.get(style_names.get(style.get(_w("val")), "")) if style is not None else None
            if not level:
                continue
        text = " ".join(paragraph_text(p).split())
        if text:
            found.append((p, level, label, text))
    return found


def _toc_name(p: Any, taken: set[str]) -> tuple[str, bool]:
    """(the heading's ``_Toc`` bookmark name, whether it is new)."""
    for start in p.iter(_w("bookmarkStart")):
        name = start.get(_W_NAME) or ""
        if name.startswith("_Toc"):
            return name, False
    rng = random.Random()
    while True:
        name = f"_Toc{rng.randint(100_000_000, 999_999_999)}"
        if name.casefold() not in taken:
            taken.add(name.casefold())
            return name, True


def _entry(doc: Any, level: int, label: str, text: str, name: str, toc: Toc, width: int) -> Any:
    from docx.oxml import parse_xml

    style = ensure(doc, f"TOC {level}")
    ns = f'xmlns:w="{W_NS}"'
    p = parse_xml(f'<w:p {ns}><w:pPr><w:pStyle w:val="{style}"/><w:tabs>'
                  f'<w:tab w:val="right" w:leader="dot" w:pos="{width}"/></w:tabs></w:pPr></w:p>')
    holder = p
    if toc.links:
        holder = parse_xml(f'<w:hyperlink {ns} w:history="1"/>')
        holder.set(_w("anchor"), name)
        p.append(holder)

    def run(text_value: str) -> Any:
        r = parse_xml(f'<w:r {ns}><w:t xml:space="preserve"/></w:r>')
        r.find(_w("t")).text = text_value
        holder.append(r)
        return r

    run(f"{label} {text}" if label else text)
    if toc.page_numbers:
        holder.append(parse_xml(f'<w:r {ns}><w:tab/></w:r>'))
        for piece in ('<w:fldChar w:fldCharType="begin"/>',
                      f'<w:instrText xml:space="preserve"> PAGEREF {name} \\h </w:instrText>',
                      '<w:fldChar w:fldCharType="separate"/>'):
            holder.append(parse_xml(f'<w:r {ns}>{piece}</w:r>'))
        run(_PAGE_PLACEHOLDER)
        holder.append(parse_xml(f'<w:r {ns}><w:fldChar w:fldCharType="end"/></w:r>'))
    return p


def _field_runs(instruction: str) -> list[Any]:
    from docx.oxml import parse_xml

    ns = f'xmlns:w="{W_NS}"'
    instr = parse_xml(f'<w:r {ns}><w:instrText xml:space="preserve"/></w:r>')
    instr.find(_w("instrText")).text = f" {instruction} "
    return [parse_xml(f'<w:r {ns}><w:fldChar w:fldCharType="begin" w:dirty="false"/></w:r>'), instr,
            parse_xml(f'<w:r {ns}><w:fldChar w:fldCharType="separate"/></w:r>')]


def rebuild(job: Any, toc: Toc) -> tuple[int, str]:
    """The TOC's entries made again from the document's headings; returns
    (entries, a reason it was left alone or "")."""
    from docx.oxml import parse_xml

    if not toc.understood():
        return 0, ("it uses a switch these tools do not build (" +
                   ", ".join(f"\\{s}" for s in sorted(set(toc.switches()) - _UNDERSTOOD)) + ")")
    if toc.begin.getparent() is not toc.end.getparent():
        return 0, "it begins and ends in different containers"
    doc = job.doc
    low, high = toc.levels()
    headings = _headings(doc, toc, low, high)
    taken = set(bookmark_names(doc))
    width = _text_width(doc, toc)
    parent = toc.begin.getparent()
    anchor = toc.begin.getprevious()
    span = [toc.begin]
    node = toc.begin
    while node is not toc.end:
        node = node.getnext()
        span.append(node)
    ppr = toc.begin.find(_w("pPr"))
    for el in span:
        parent.remove(el)
    entries = []
    names = []
    for p, level, label, text in headings:
        name, new = _toc_name(p, taken)
        if new:
            _bookmark(job, p, name)
        names.append((name, f"{label} {text}" if label else text))
        entries.append(_entry(doc, level, label, text, name, toc, width))
    first = entries[0] if entries else parse_xml(f'<w:p xmlns:w="{W_NS}"/>')
    if not entries and ppr is not None:
        first.insert(0, ppr)
    lead = first.find(_w("pPr"))
    for r in reversed(_field_runs(toc.instruction)):
        if lead is not None:
            lead.addnext(r)
        else:
            first.insert(0, r)
    if not entries:
        first.append(parse_xml(f'<w:r xmlns:w="{W_NS}"><w:rPr><w:i/></w:rPr><w:t>No headings yet: add headings, '
                               f'then update_table_of_contents.</w:t></w:r>'))
        entries = [first]
    closing = parse_xml(f'<w:p xmlns:w="{W_NS}"><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    blocks = [*entries, closing]
    for block in reversed(blocks):
        if anchor is not None:
            anchor.addnext(block)
        else:
            parent.insert(0, block)
    toc.begin, toc.end = blocks[0], closing
    toc.names = names if toc.page_numbers else []
    return len(headings), ""


def _title_style(job: Any, p: Any) -> None:
    """The TOC Heading style, unnumbered: it rests on Heading 1, which
    number_headings numbers, and a table of contents' title has no number."""
    from docx.oxml import parse_xml

    from ooxml.order import insert_child

    ppr = p.get_or_add_pPr()
    ppr.style = ensure(job.doc, "TOC Heading")
    old = ppr.find(_w("numPr"))
    if old is not None:
        ppr.remove(old)
    insert_child(ppr, parse_xml(f'<w:numPr xmlns:w="{W_NS}"><w:ilvl w:val="0"/><w:numId w:val="0"/></w:numPr>'))


def _restyle_title(job: Any, toc: Toc) -> None:
    """An older table of contents' title, a Heading 1 the TOC listed itself
    under, becomes a TOC Heading."""
    title = toc.begin.getprevious()
    style = title.find(f"{_W}pPr/{_W}pStyle") if title is not None and title.tag == _W_P else None
    if style is not None and style.get(_w("val")) == ensure(job.doc, "Heading 1") and \
            _PLACEHOLDER_1_3 in "".join(t.text or "" for t in toc.begin.getparent().iter(_w("t"))):
        _title_style(job, title)


def build_all(job: Any, outcome: Any) -> None:
    """The entries of every TOC the batch made or asked to update, from the
    headings as the batch left them. A failure is a warning on its
    operation, never a lost save."""
    results = {r.index: r for r in outcome.results}
    for toc in job.tocs:
        result = results.get(toc.op_index)
        try:
            count, reason = rebuild(job, toc)
        except Exception as exc:  # noqa: BLE001  # a TOC that cannot be built leaves the rest of the save whole
            reason, count = f"it could not be built ({exc or exc.__class__.__name__})", 0
        if result is None:
            continue
        if reason:
            result.warnings.append(f"the table of contents was left as it is: {reason}")
        else:
            result.notes.append(f"the table of contents lists {count} heading{'s' if count != 1 else ''}")
    job.book.touch(job.doc.part)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _op_add_table_of_contents(job: Any, ctx: OpCtx, op: dict) -> None:
    from docx.oxml import OxmlElement

    title = op.get("title", DEFAULT_TITLE)
    levels = op.get("levels", 3)
    if not 1 <= levels <= 9:
        raise OpError(Code.BAD_VALUE, f"levels {levels} is outside 1-9", hint="Give levels 1 to 9.")
    spot = cursor(job.book, ctx, op, allow_containers=False)
    made = []
    if title:
        heading = spot.paragraph()
        heading.add_run(title)
        _title_style(job, heading._p)
        made.append(heading._p)
    begin = spot.paragraph()
    switches = f'\\o "1-{levels}" \\h \\z \\u' + ("" if op.get("page_numbers", True) else " \\n")
    add_field(begin, f" TOC {switches} ", "", None)
    end_p = OxmlElement("w:p")
    begin._p.addnext(end_p)
    ends = [r for r in begin._p.iter(_W_R) if r.find(_w("fldChar")) is not None and
            r.find(_w("fldChar")).get(_w("fldCharType")) == annotations.FIELD_END]
    for r in ends:
        end_p.append(r)
    made += [begin._p, end_p]
    job.tocs.append(Toc(begin._p, end_p, f"TOC {switches}", ctx.index))
    created(job, ctx, *made)


def _op_update_table_of_contents(job: Any, ctx: OpCtx, op: dict) -> None:
    tocs = find_tocs(job.doc)
    if op.get("target") is not None:
        located = locate(job.book, ctx, op, kinds=(addr.PARAGRAPH,), destructive=False)
        if located.part is not job.doc.part:
            raise OpError(Code.BAD_VALUE, f"{located.address} is not in the body",
                          location=f"target {op['target']}",
                          hint="Target a table of contents entry in the body, or leave target out to update every "
                               "table of contents.")
        body = list(job.doc.element.body.iter(_W_P))
        at = next(i for i, p in enumerate(body) if p is located.element)
        tocs = [t for t in tocs if next(i for i, p in enumerate(body) if p is t.begin) <= at <=
                next(i for i, p in enumerate(body) if p is t.end)]
        if not tocs:
            raise OpError(Code.NOT_FOUND, f"{located.address} is not in a table of contents",
                          location=f"target {op['target']}", hint="Target one of the table's entries.")
    if not tocs:
        raise NoOp("the document has no table of contents")
    for toc in tocs:
        if not toc.understood():
            ctx.warn("a table of contents was left as it is: it uses a switch these tools do not build (" +
                     ", ".join(f"\\{s}" for s in sorted(set(toc.switches()) - _UNDERSTOOD)) + ")")
            continue
        _restyle_title(job, toc)
        toc.op_index = ctx.index
        job.tocs.append(toc)
    job.book.touch(job.doc.part)


# ---------------------------------------------------------------------------
# Page numbers: the probe, the render, the splice
# ---------------------------------------------------------------------------


def names_for_pages(job: Any) -> list[tuple[str, str]]:
    return [pair for toc in job.tocs for pair in toc.names]


def marks_for_pages(job: Any) -> list[str]:
    """Bookmarks whose page a cross-reference shows."""
    return sorted(job.page_marks)


def write_probe(doc: Any, path: str, marks: list[str] | tuple = ()) -> None:
    """Save ``doc`` (already staged) as a probe: every footer of every
    section prints its page label between ~~pg: and :pg~~ (ASCII, which
    every font draws), in a tiny font, and each bookmark in ``marks`` is
    followed by ~~bm:NAME:bm~~, so the page it lands on can be read."""
    from docx.oxml import parse_xml

    ns = f'xmlns:w="{W_NS}"'
    label = (f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:t xml:space="preserve">~~pg:</w:t></w:r>'
             f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:fldChar w:fldCharType="begin"/></w:r>'
             f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
             f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:fldChar w:fldCharType="separate"/></w:r>'
             f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:t>1</w:t></w:r>'
             f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:fldChar w:fldCharType="end"/></w:r>'
             f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:t>:pg~~</w:t></w:r>')
    done = set()
    even = doc.settings.odd_and_even_pages_header_footer
    sections = list(doc.sections)

    def defined_before(index: int, kind: str) -> bool:
        return any(ref.get(_w("type"), "default") == kind
                   for sect in sections[:index] for ref in sect._sectPr.iterchildren(_w("footerReference")))

    for index, section in enumerate(sections):
        kinds = [("default", section.footer)]
        if section.different_first_page_header_footer:
            kinds.append(("first", section.first_page_footer))
        if even:
            kinds.append(("even", section.even_page_footer))
        for kind, footer in kinds:
            if footer.is_linked_to_previous:
                # A linked footer shows an earlier section's, labelled there;
                # with none before it, the page would show no footer at all.
                if defined_before(index, kind):
                    continue
                footer.is_linked_to_previous = False
            part = footer.part
            if id(part) in done:
                continue
            done.add(id(part))
            p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
            for run in parse_xml(f"<w:p {ns}>{label}</w:p>"):
                p._p.append(run)
    wanted = set(marks)
    for start in list(doc.element.body.iter(_w("bookmarkStart"))) if wanted else []:
        name = start.get(_W_NAME) or ""
        if name in wanted:
            marker = parse_xml(f'<w:r {ns}><w:rPr><w:sz w:val="2"/></w:rPr><w:t/></w:r>')
            marker.find(_w("t")).text = f"~~bm:{name}:bm~~"
            start.addnext(marker)
    doc.save(path)


def _normal(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def read_pages(pdf_path: str, names: list[tuple[str, str]], marks: list[str] | tuple = ()) -> dict[str, str]:
    """{bookmark name: printed page label} for the TOC entries found in the
    rendered probe (each entry's link, in order, and the label printed on
    the page it lands on) and for the bookmarks in ``marks`` (the page their
    ~~bm: marker is drawn on)."""
    import fitz

    found: dict[str, str] = {}
    with fitz.open(pdf_path) as pdf:
        labels = {}
        marked: dict[str, int] = {}
        for index, page in enumerate(pdf):
            text = page.get_text()
            match = _LABEL.search(text)
            if match:
                labels[index] = match.group(1).strip()
            for name in _MARK.findall(text.replace("\n", "")):
                marked.setdefault(name, index)
        for name in marks:
            if name in marked and marked[name] in labels:
                found[name] = labels[marked[name]]
        links = []
        for index, page in enumerate(pdf):
            for link in page.get_links():
                target = link.get("page", -1)
                if link.get("kind") in (fitz.LINK_GOTO, fitz.LINK_NAMED) and isinstance(target, int) and target >= 0:
                    rect = link["from"]
                    links.append((index, rect.y0, rect.x0, target, _normal(page.get_textbox(rect))))
    links.sort()
    j = 0
    for name, text in names:
        want = _normal(text)[:24]
        if want:
            # The entry's own link, at or after the last one used; an entry
            # whose link is missing leaves the others theirs.
            k = next((k for k in range(j, len(links)) if links[k][4].startswith(want)), None)
        else:
            # Words with no letter or digit match any link: take the next one.
            k = j if j < len(links) else None
        if k is None:
            continue
        label = labels.get(links[k][3])
        if label:
            found[name] = label
        j = k + 1
    return found


def write_pages(path: str, pages: dict[str, str], fallback: dict[str, str] | None = None) -> tuple[int, int]:
    """Set each PAGEREF result still holding the placeholder in the saved
    file at ``path`` (body, headers, footers, notes, comments): the page
    found, or nothing. Returns (how many got a number, how many there
    were)."""
    from formats.docx.package import open_document

    doc = open_document(path)
    filled = total = 0
    roots = [annotations.root_of_part(part) for part in annotations.story_parts(doc)]
    for p in (p for root in roots if root is not None for p in root.iter(_W_P)):
        in_result, name = False, None
        for run in p.iter(_W_R):
            char = run.find(_w("fldChar"))
            instr = run.find(_w("instrText"))
            if instr is not None:
                match = re.match(r"\s*PAGEREF\s+(\S+)", instr.text or "")
                if match:
                    name = match.group(1)
            elif char is not None:
                kind = char.get(_w("fldCharType"))
                if kind == annotations.FIELD_SEPARATE and name:
                    in_result = True
                elif kind == annotations.FIELD_END:
                    in_result, name = False, None
            elif in_result:
                t = run.find(_w("t"))
                if t is not None and t.text == _PAGE_PLACEHOLDER:
                    t.text = pages.get(name or "") or (fallback or {}).get(name or "", "")
                    filled += bool(t.text)
                    total += 1
    doc.save(path)
    return filled, total


async def render(staged: Any, call: Any, budget_s: float) -> Any:
    """The parent's render phase: the probe as a PDF."""
    import render_engine
    from batch import Rendered

    probe = staged.facts.get("toc_probe")
    if not probe:
        return Rendered()
    out_dir = render_engine.output_folder(call.work_dir, "toc")
    pdf = await render_engine.to_pdf(probe, out_dir=out_dir, slot_wait_s=min(30.0, budget_s / 2),
                                     timeout_s=max(5.0, budget_s - 5.0))
    return Rendered(payload=pdf)


def finish(staged: Any, call: Any, rendered: Any, *, kind: str, fmt: str) -> Any:
    """Worker B: the page numbers written (or left empty), then the commit."""
    names = [tuple(pair) for pair in staged.facts.get("toc_names", [])]
    marks = list(staged.facts.get("page_marks", []))
    warnings = []
    pages: dict[str, str] = {}
    if rendered.payload:
        try:
            pages = read_pages(rendered.payload, names, marks)
        except Exception as exc:  # noqa: BLE001  # an unreadable layout leaves the numbers empty, never the save
            warnings.append(f"the page numbers could not be read from the layout ({exc})")
    elif rendered.note:
        warnings.append(rendered.note)

    def transform(path: str) -> None:
        filled, total = write_pages(path, pages, staged.facts.get("page_fallback"))
        if filled < total:
            missing = total - filled
            warnings.append(f"{missing} page number{'s are' if missing != 1 else ' is'} missing (table of contents "
                            "entries or cross-references): Word fills them when the fields are updated "
                            "(right-click, Update Field), Collabora with Tools, Update")
        if filled:
            toc_result = next((r for r in staged.outcome.results if r.op in PAGE_OPS), None)
            note = (f"{filled} page number{'s' if filled != 1 else ''} from a LibreOffice layout, "
                    "which can differ from Word's by a line; Word's Update Field refreshes them")
            if toc_result is not None:
                toc_result.notes.append(note)
            else:
                warnings.append(note)

    return call.commit_staged(staged, kind=kind, fmt=fmt, transform=transform, warnings=warnings)


def _ops() -> list[OpSpec]:
    headings = ({"op": "add_heading", "text": "Introduction", "level": 1},
                {"op": "add_heading", "text": "Scope", "level": 2})
    return [
        OpSpec("add_table_of_contents", _op_add_table_of_contents, (
            KeySpec("title", ("str",), default=DEFAULT_TITLE),
            KeySpec("levels", ("int",), default=3, help="1-9"),
            KeySpec("page_numbers", ("bool",), default=True),
            *PLACE,
        ), "LINKS", note="entries and page numbers from the headings as the call leaves them",
            setup=headings, examples=({"op": "add_table_of_contents", "levels": 2},
                                      {"op": "add_table_of_contents", "target": "p0", "position": "before",
                                       "title": "Contents", "page_numbers": False})),
        OpSpec("update_table_of_contents", _op_update_table_of_contents, AT, "LINKS",
               note="every table of contents (or the one target is in) rebuilt from the headings",
               setup=(*headings, {"op": "add_table_of_contents"}),
               examples=({"op": "update_table_of_contents"},)),
        OpSpec("add_bookmark", _op_add_bookmark, (
            KeySpec("name", ("str",), required=True, help="a letter first, then letters, digits, _"),
            KeySpec("on_text", ("str",), help="these words only; default the whole paragraph"),
            *AT,
        ), "LINKS", note="add_hyperlink bookmark: name links to it", setup=headings,
            examples=({"op": "add_bookmark", "target": "p1", "name": "scope"},)),
    ]


def link_style(doc: Any) -> str:
    return ensure(doc, "Hyperlink", CHARACTER)


OPS = _ops()
