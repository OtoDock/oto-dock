"""Checks of a Word document's content: leftover template tokens and
placeholder text, headings that are empty or skip a level, tables and
pictures wider than the page's text area, tables of contents without
entries, out of date or without page numbers, links to bookmarks that do
not exist, tracked changes and comment threads still open.

Locations are the addresses write_docx takes as target; document text goes
only into a finding's excerpt.
"""

import re
from typing import Any

import checks
from formats.docx import address as addr, annotations

DOCUMENT_PART = "word/document.xml"
STYLES_PART = "word/styles.xml"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W14_PARA_ID = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"
_PARA_ID = re.compile(r"[0-9A-Fa-f]{8}")
_WP_EXTENT = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent"
_SKIPPED = (_W + "del", _W + "moveFrom", _W + "txbxContent")
_EMU_PER_TWIP = 635
_EMU_PER_CM = 360000
# Word lets a table reach a little into the margin before it looks wrong.
_SLACK_TWIPS = 144

# The authors' unfinished-work markers, spelled in pieces so this module
# never reads as unfinished work itself.
_MARKERS = ("TO" + "DO", "T" + "BD", "X" + "XX")
_TOKENS = re.compile(
    r"\{\{[^{}\n]{1,80}\}\}|\[\[[^\[\]\n]{1,80}\]\]|<<[^<>\n]{1,80}>>|\blorem ipsum\b"
    r"|Click or tap here to enter text\.?|\b(?:" + "|".join(_MARKERS) + r")\b",
    re.IGNORECASE,
)
_UPPER_ONLY = {m.lower() for m in _MARKERS}
_HEADING_NAME = re.compile(r"heading\s*(\d+)", re.IGNORECASE)


def _text(p: Any) -> str:
    """A paragraph's visible text: insertions in, deletions and text boxes out."""
    out = []
    for t in p.iter(_W + "t"):
        if any(a.tag in _SKIPPED for a in t.iterancestors()) or next(t.iterancestors(_W + "p")) is not p:
            continue
        out.append(t.text or "")
    return "".join(out)


def _where(address: str, p: Any) -> str:
    pid = p.get(_W14_PARA_ID) or ""
    return f"paragraph {address}" + (f" (#{pid})" if _PARA_ID.fullmatch(pid) else "")


def _outline_levels(ctx: checks.CheckContext) -> dict[str, int | None]:
    """Heading level by style id, from outline levels and Heading N names
    along each style's basedOn chain."""
    styles = ctx.part_xml(STYLES_PART)
    if styles is None:
        return {}
    info: dict[str, tuple[str, str | None, int | None]] = {}
    for style in styles.iter(_W + "style"):
        sid = style.get(_W + "styleId")
        if not sid:
            continue
        name = style.find(_W + "name")
        based = style.find(_W + "basedOn")
        lvl = style.find(f"{_W}pPr/{_W}outlineLvl")
        level = None
        if lvl is not None and (lvl.get(_W + "val") or "").isdigit():
            level = int(lvl.get(_W + "val"))
        info[sid] = (name.get(_W + "val") if name is not None else "",
                     based.get(_W + "val") if based is not None else None, level)
    levels: dict[str, int | None] = {}
    for sid in info:
        seen: set[str] = set()
        current: str | None = sid
        result = None
        while current and current in info and current not in seen:
            seen.add(current)
            name, based, outline = info[current]
            if outline is not None:
                result = outline + 1 if outline < 9 else None
                break
            match = _HEADING_NAME.fullmatch(name.strip())
            if match:
                result = min(int(match.group(1)), 9) or None
                break
            current = based
        levels[sid] = result
    return levels


def _heading_level(p: Any, levels: dict[str, int | None]) -> int | None:
    own = p.find(f"{_W}pPr/{_W}outlineLvl")
    if own is not None and (own.get(_W + "val") or "").isdigit():
        value = int(own.get(_W + "val"))
        return value + 1 if value < 9 else None
    style = p.find(f"{_W}pPr/{_W}pStyle")
    return levels.get(style.get(_W + "val")) if style is not None else None


def _body(ctx: checks.CheckContext) -> Any:
    root = ctx.part_xml(DOCUMENT_PART)
    return root.find(_W + "body") if root is not None else None


def leftover_tokens(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = _body(ctx)
    if body is None:
        return []
    found = []
    for kind, address, el in addr.iter_addressed(body):
        if kind != addr.PARAGRAPH:
            continue
        text = _text(el)
        for match in _TOKENS.finditer(text):
            token = match.group()
            if token.lower() in _UPPER_ONLY and token != token.upper():
                continue
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=_where(address, el),
                message="leftover placeholder text", excerpt=token, part=DOCUMENT_PART, key=token.lower(),
                fix="Fill it with fill_template or search_and_replace, or delete it."))
    return found


def empty_heading(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = _body(ctx)
    if body is None:
        return []
    levels = _outline_levels(ctx)
    found = []
    for kind, address, el in addr.iter_addressed(body):
        if kind == addr.PARAGRAPH and _heading_level(el, levels) and not _text(el).strip():
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=_where(address, el), message="an empty heading",
                part=DOCUMENT_PART, key="empty_heading",
                fix="Give it text with set_paragraph_text, or delete it with delete_paragraph."))
    return found


def heading_jump(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = _body(ctx)
    if body is None:
        return []
    levels = _outline_levels(ctx)
    found = []
    previous = 0
    for kind, n, el in addr.blocks(body):
        if kind != addr.PARAGRAPH:
            continue
        level = _heading_level(el, levels)
        if not level or not _text(el).strip():
            continue
        if previous and level > previous + 1:
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=_where(f"p{n}", el),
                message=f"a level {level} heading follows a level {previous} heading",
                excerpt=_text(el)[:80], part=DOCUMENT_PART, key=f"jump {previous}-{level}",
                fix=f"Make it level {previous + 1}, or add the missing level above it."))
        previous = level
    return found


def _text_width(sect_pr: Any) -> int | None:
    """The text area's width in twips."""
    if sect_pr is None:
        return None
    size = sect_pr.find(_W + "pgSz")
    margins = sect_pr.find(_W + "pgMar")
    try:
        width = int(size.get(_W + "w"))
        left = int(margins.get(_W + "left", 0)) if margins is not None else 0
        right = int(margins.get(_W + "right", 0)) if margins is not None else 0
    except (AttributeError, TypeError, ValueError):
        return None
    return width - left - right


def _section_widths(body: Any) -> list[tuple[Any, int | None]]:
    """Each top-level block with its section's text width."""
    pending: list[Any] = []
    out: list[tuple[Any, int | None]] = []
    for child in body.iterchildren():
        if child.tag == _W + "sectPr":
            break
        pending.append(child)
        sect_pr = child.find(f"{_W}pPr/{_W}sectPr") if child.tag == _W + "p" else None
        if sect_pr is not None:
            width = _text_width(sect_pr)
            out.extend((el, width) for el in pending)
            pending = []
    width = _text_width(body.find(_W + "sectPr"))
    out.extend((el, width) for el in pending)
    return out


def _table_width(tbl: Any) -> int | None:
    grid = [c.get(_W + "w") for c in tbl.iterfind(f"{_W}tblGrid/{_W}gridCol")]
    if grid and all((g or "").isdigit() for g in grid):
        return sum(int(g) for g in grid)
    width = tbl.find(f"{_W}tblPr/{_W}tblW")
    if width is not None and width.get(_W + "type") == "dxa" and (width.get(_W + "w") or "").isdigit():
        return int(width.get(_W + "w"))
    return None


def _cm(twips: int) -> str:
    return f"{twips * _EMU_PER_TWIP / _EMU_PER_CM:.1f} cm"


def wide_blocks(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = _body(ctx)
    if body is None:
        return []
    found = []
    tables = paragraphs = 0
    for el, text_width in _section_widths(body):
        if el.tag == _W + "tbl":
            address = f"t{tables}"
            tables += 1
            width = _table_width(el)
            if text_width and width and width > text_width + _SLACK_TWIPS:
                found.append(checks.Finding(
                    checks.Severity.WARNING, "", location=f"table {address}",
                    message=f"the table is {_cm(width)} wide; the page's text area is {_cm(text_width)}",
                    part=DOCUMENT_PART, key=f"wide table {address}",
                    fix="Narrow its columns with set_table_column_widths, or put it in a landscape section."))
        elif el.tag == _W + "p":
            address = f"p{paragraphs}"
            paragraphs += 1
            for extent in el.iter(_WP_EXTENT):
                try:
                    width = int(extent.get("cx")) // _EMU_PER_TWIP
                except (TypeError, ValueError):
                    continue
                if text_width and width > text_width + _SLACK_TWIPS:
                    found.append(checks.Finding(
                        checks.Severity.WARNING, "", location=_where(address, el),
                        message=f"a picture is {_cm(width)} wide; the page's text area is {_cm(text_width)}",
                        part=DOCUMENT_PART, key=f"wide picture {address}",
                        fix="Add it again with a smaller width_inches, or put it in a landscape section."))
    return found


# ---------------------------------------------------------------------------
# Tables of contents, internal links, changes and comments
# ---------------------------------------------------------------------------

_RELS = "word/_rels/document.xml.rels"
_PKG_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_RT_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
_W15 = "{http://schemas.microsoft.com/office/word/2012/wordml}"
_TOC_INSTR = re.compile(r"^\s*TOC\b", re.IGNORECASE)
_REF_INSTR = re.compile(r"^\s*(?:PAGE)?REF\s+(\S+)", re.IGNORECASE)
_TOC_SWITCH = re.compile(r"\\([A-Za-z])(?:\s+\"([^\"]*)\")?")
_TOC_UNDERSTOOD = frozenset("ohznuwxp")
_LABEL = re.compile(r"^(?:[0-9ivxlcdm]+[.)]?)(?:\.[0-9]+)*\.?\s+", re.IGNORECASE)


def _related(ctx: checks.CheckContext, reltype: str) -> list[str]:
    """Part names the document part relates to by ``reltype``."""
    rels = ctx.part_xml(_RELS)
    if rels is None:
        return []
    names = []
    for rel in rels.iter(_PKG_RELS):
        if rel.get("Type") == reltype and rel.get("TargetMode") != "External":
            target = rel.get("Target") or ""
            names.append(target.lstrip("/") if target.startswith("/") else f"word/{target}")
    return names


def _story_roots(ctx: checks.CheckContext) -> list[tuple[str, Any]]:
    found = []
    for name in [DOCUMENT_PART, *(n for kind in ("header", "footer", "footnotes", "endnotes", "comments")
                                 for n in _related(ctx, _RT + kind))]:
        root = ctx.part_xml(name)
        if root is not None:
            found.append((name, root))
    return found


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _tocs(body: Any) -> list[tuple[str, list[Any]]]:
    """(instruction, the field's paragraphs) of each TOC in the body."""
    found = []
    stack: list[dict] = []
    paragraphs = [p for p in body.iter(_W + "p") if next(p.iterancestors(_W + "txbxContent"), None) is None]
    for index, p in enumerate(paragraphs):
        for el in p.iter(_W + "fldChar", _W + "instrText"):
            if el.tag == _W + "instrText":
                if stack:
                    stack[-1]["instr"] += el.text or ""
                continue
            kind = el.get(_W + "fldCharType")
            if kind == annotations.FIELD_BEGIN:
                stack.append({"instr": "", "start": index})
            elif kind == annotations.FIELD_END and stack:
                item = stack.pop()
                if _TOC_INSTR.match(item["instr"]):
                    found.append((item["instr"].strip(), paragraphs[item["start"]:index + 1]))
    return found


def _entry_words(p: Any) -> str:
    """A TOC entry's words: its text up to the first tab, which the dot
    leader and the page number follow."""
    out = []
    for el in p.iter(_W + "t", _W + "tab"):
        if any(a.tag in _SKIPPED for a in el.iterancestors()):
            continue
        if el.tag == _W + "tab":
            if el.getparent().tag == _W + "r":
                break
            continue
        out.append(el.text or "")
    return "".join(out)


def _toc_entries(paragraphs: list[Any]) -> list[tuple[int, str, Any]]:
    """(level, text, paragraph) of each entry: a paragraph styled TOC N."""
    entries = []
    for p in paragraphs:
        style = p.find(f"{_W}pPr/{_W}pStyle")
        value = (style.get(_W + "val") or "") if style is not None else ""
        match = re.fullmatch(r"TOC(\d)", value, re.IGNORECASE)
        if match:
            entries.append((int(match.group(1)), _text(p), p))
    return entries


def _address_of(body: Any, p: Any) -> str:
    for _kind, address, el in addr.iter_addressed(body):
        if el is p:
            return address
    return ""


def toc_without_entries(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = _body(ctx)
    if body is None:
        return []
    found = []
    for _instruction, paragraphs in _tocs(body):
        if not _toc_entries(paragraphs):
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=_where(_address_of(body, paragraphs[0]), paragraphs[0]),
                message="a table of contents without entries", part=DOCUMENT_PART, key="toc_without_entries",
                fix="Rebuild it with update_table_of_contents (it lists the headings), or add headings first."))
    return found


def toc_out_of_date(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = _body(ctx)
    if body is None:
        return []
    levels = _outline_levels(ctx)
    found = []
    for instruction, paragraphs in _tocs(body):
        switches = {m.group(1).lower(): m.group(2) or "" for m in _TOC_SWITCH.finditer(instruction)}
        entries = _toc_entries(paragraphs)
        if not entries or not set(switches) <= _TOC_UNDERSTOOD:
            continue
        low, _, high = (switches.get("o") or "1-3").partition("-")
        try:
            span = range(int(low), int(high or low) + 1)
        except ValueError:
            continue
        inside = {id(p) for p in paragraphs}
        headings = []
        for p in body.iter(_W + "p"):
            if id(p) in inside or any(a.tag == _W + "txbxContent" for a in p.iterancestors()):
                continue
            level = _heading_level(p, levels)
            if level in span and _text(p).strip():
                headings.append((level, _norm(_text(p))))
        listed = [(level, _norm(_LABEL.sub("", _entry_words(p)))) for level, _text_value, p in entries]
        if len(listed) != len(headings) or any(
                lv != hl or not (et == ht or et.endswith(ht) or ht.endswith(et))
                for (lv, et), (hl, ht) in zip(listed, headings, strict=False)):
            found.append(checks.Finding(
                checks.Severity.WARNING, "", location=_where(_address_of(body, paragraphs[0]), paragraphs[0]),
                message=f"the table of contents lists {len(listed)} entries and the document has "
                        f"{len(headings)} headings it would list, or their words or levels differ",
                part=DOCUMENT_PART, key="toc_out_of_date",
                fix="Rebuild it with update_table_of_contents."))
    return found


def toc_without_page_numbers(ctx: checks.CheckContext) -> list[checks.Finding]:
    body = _body(ctx)
    if body is None:
        return []
    found = []
    for instruction, paragraphs in _tocs(body):
        if "\\n" in instruction:
            continue
        empty = 0
        for _level, _text_value, p in _toc_entries(paragraphs):
            instr = "".join(t.text or "" for t in p.iter(_W + "instrText"))
            if "PAGEREF" in instr.upper() and not re.search(r"\d|[ivxlcdm]$", _text(p).rstrip(), re.IGNORECASE):
                empty += 1
        if empty:
            found.append(checks.Finding(
                checks.Severity.INFO, "", location=_where(_address_of(body, paragraphs[0]), paragraphs[0]),
                message=f"{empty} table of contents entr{'ies have' if empty != 1 else 'y has'} no page number yet",
                part=DOCUMENT_PART, key="toc_without_page_numbers",
                fix="Word fills them when the table is updated; update_table_of_contents fills them from a "
                    "LibreOffice layout."))
    return found


def broken_internal_link(ctx: checks.CheckContext) -> list[checks.Finding]:
    roots = _story_roots(ctx)
    names = {"_top": "_top"}
    for _, root in roots:
        for b in root.iter(_W + "bookmarkStart"):
            names.setdefault((b.get(_W + "name") or "").casefold(), b.get(_W + "name") or "")
    body = _body(ctx)
    r_id = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    found = []
    for part, root in roots:
        for el in root.iter(_W + "hyperlink", _W + "instrText"):
            if el.tag == _W + "hyperlink":
                target = el.get(_W + "anchor")
                if not target or el.get(r_id):
                    continue
            else:
                match = _REF_INSTR.match(el.text or "")
                if not match:
                    continue
                target = match.group(1)
            exact = names.get(target.casefold())
            if exact == target or (exact is not None and target.casefold() == "_top"):
                continue
            p = next(el.iterancestors(_W + "p"), None)
            where = _where(_address_of(body, p), p) if part == DOCUMENT_PART and p is not None else part
            if exact is not None:
                found.append(checks.Finding(
                    checks.Severity.WARNING, "", location=where,
                    message=f"a link names its bookmark in another case than the bookmark ({exact!r}): Word "
                            "follows it, LibreOffice and Collabora do not",
                    excerpt=target, part=part, key=f"link case {target.casefold()}",
                    fix=f"Link to {exact!r} as it is written (add_hyperlink bookmark: {exact!r})."))
                continue
            found.append(checks.Finding(
                checks.Severity.ERROR, "", location=where, message="a link to a bookmark that does not exist",
                excerpt=target, part=part, key=f"link {target.casefold()}",
                fix="Point it at an existing bookmark, add the bookmark with add_bookmark, or rebuild the table "
                    "of contents with update_table_of_contents."))
    return found


def _many(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def tracked_changes_left(ctx: checks.CheckContext) -> list[checks.Finding]:
    counts = {"insertions": 0, "deletions": 0}
    authors: set[str] = set()
    for _, root in _story_roots(ctx):
        for el in root.iter(_W + "ins", _W + "del", _W + "moveTo", _W + "moveFrom"):
            if next(el.iter(_W + "r"), None) is None:
                continue
            counts["insertions" if el.tag in (_W + "ins", _W + "moveTo") else "deletions"] += 1
            if el.get(_W + "author"):
                authors.add(el.get(_W + "author"))
    total = sum(counts.values())
    if not total:
        return []
    by = f" by {', '.join(sorted(authors)[:5])}" if authors else ""
    return [checks.Finding(
        checks.Severity.INFO, "", location="the document",
        message=f"{_many(counts['insertions'], 'insertion')} and {_many(counts['deletions'], 'deletion')} "
                f"{'is' if total == 1 else 'are'} still tracked{by}",
        part=DOCUMENT_PART, key="tracked_changes_left",
        fix="Deliver it this way for review, or settle them with accept_all_changes or reject_all_changes.")]


def open_comments(ctx: checks.CheckContext) -> list[checks.Finding]:
    comments = [root for name in _related(ctx, _RT + "comments") if (root := ctx.part_xml(name)) is not None]
    if not comments:
        return []
    entries = {}
    for name in _related(ctx, _RT_EXTENDED):
        root = ctx.part_xml(name)
        if root is not None:
            entries.update({(e.get(_W15 + "paraId") or "").upper(): e for e in root.iter(_W15 + "commentEx")})
    open_threads = 0
    for root in comments:
        for comment in root.iter(_W + "comment"):
            paragraphs = comment.findall(_W + "p")
            key = (paragraphs[-1].get(_W14_PARA_ID) or "").upper() if paragraphs else ""
            entry = entries.get(key)
            if entry is not None and entry.get(_W15 + "paraIdParent"):
                continue
            if entry is None or entry.get(_W15 + "done") != "1":
                open_threads += 1
    if not open_threads:
        return []
    return [checks.Finding(
        checks.Severity.INFO, "", location="the document",
        message=f"{open_threads} comment thread{'s are' if open_threads != 1 else ' is'} still open",
        part=DOCUMENT_PART, key="open_comments",
        fix="Answer them with reply_to_comment, then resolve_comment or delete_comment.")]


def register_all() -> None:
    checks.register("docx", "leftover_tokens", leftover_tokens, final_only=True, baseline=True)
    checks.register("docx", "empty_heading", empty_heading, final_only=True, baseline=True)
    checks.register("docx", "heading_jump", heading_jump, baseline=True)
    checks.register("docx", "wide_blocks", wide_blocks, baseline=True)
    checks.register("docx", "toc_without_entries", toc_without_entries, final_only=True, baseline=True)
    checks.register("docx", "toc_out_of_date", toc_out_of_date, baseline=True)
    checks.register("docx", "toc_without_page_numbers", toc_without_page_numbers, final_only=True)
    checks.register("docx", "broken_internal_link", broken_internal_link, baseline=True)
    checks.register("docx", "tracked_changes_left", tracked_changes_left, final_only=True)
    checks.register("docx", "open_comments", open_comments, final_only=True)


register_all()
