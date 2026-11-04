"""Word (DOCX) read and write handlers for the file-tools MCP.

The write tool runs on the shared batch: every operation is checked against
the catalogue before any work, the batch is atomic unless ``best_effort``,
and the document is saved once. The format's helpers live in
``formats.docx``.
"""

import contextlib
import os
from dataclasses import dataclass, field
from typing import Any

import units
from batch import WriteCall, batch_ops, handle_write_call, read_call
from catalog import Catalog, KeySpec, OpSpec
from checks import collect_baseline, register_blank
from equations import OMML_NS, latex_to_omml_element
from formats.docx import address as addr
from formats.docx.annotations import Ids
from formats.docx.captions import OPS as CAPTION_OPS, refresh_fields
from formats.docx.comments import OPS as COMMENT_OPS
from formats.docx.fields import (
    OPS as FIELD_OPS,
    PAGE_OPS,
    build_all,
    finish as finish_toc,
    link_style,
    link_target,
    marks_for_pages,
    names_for_pages,
    render as render_toc,
    write_probe,
)
from formats.docx.format import (
    alignment,
    apply_paragraph_format,
    apply_run_format,
    color_attrs,
    set_run_color,
)
from formats.docx.ids import (
    IdAllocator,
    ensure_w14_namespace,
    is_fallback_copy,
    new_text_id,
    paragraph_id,
    stamp_part,
    stamping_order,
)
from formats.docx.notes import OPS as NOTE_OPS
from formats.docx.numbering import BULLET, NUMBER, Lists, number_format
from formats.docx.opkit import (
    ALIGN,
    AT,
    COLOR,
    PLACE,
    RUN_KEYS,
    RUNS,
    TRACKED,
    add_field,
    cannot_track,
    created,
    equation_runs,
    fill_runs,
    first_text_rpr,
    one_of,
    paragraph_at,
    plain_props,
    put_back,
    revision_for,
    take_annotations,
    target_paragraph,
    text_base_rpr,
)
from formats.docx.package import (
    match_content_type_to_name,
    new_document,
    open_document,
    preflight,
)
from formats.docx.page import (
    check_orientation,
    check_page_size,
    set_orientation,
    set_page_size,
)
from formats.docx.patching import OPS as PATCH_OPS
from formats.docx.read import read_docx
from formats.docx.replace import (
    compile_pattern,
    fill_template,
    replace_all,
    tracking_is_on,
)
from formats.docx.revisions import (
    OPS as REVIEW_OPS,
    already_deleted,
    own_new_paragraph,
    track_delete_paragraph,
    track_new_paragraph,
    track_row,
    track_set_text,
)
from formats.docx.styles import (
    TABLE as TABLE_STYLE,
    apply_paragraph_style,
    ensure as ensure_style,
    resolve as resolve_style,
)
from formats.docx.targets import (
    Book,
    PartParent,
    cursor,
    field_balance,
    legacy_index,
    locate,
    remove_paragraph,
)
from ooxml.order import insert_child
from results import (
    BatchOutcome,
    Code,
    NoOp,
    OpCtx,
    OpError,
    Staged,
    Status,
    WriteReport,
)
from shared import _WRITE_OP_ADVICE
from tokens import flatten_data

KIND = "Document"
_DOCX_ADVICE = (_WRITE_OP_ADVICE + "; a search_and_replace with regex: true can also run this long: give plain "
                "text or a simpler pattern")
FMT = "docx"

__all__ = ["CATALOG", "handle_write_docx", "read_docx"]


# ---------------------------------------------------------------------------
# Write — the job and its helpers
# ---------------------------------------------------------------------------


@dataclass
class _DocxJob:
    """What one write_docx call's operations share."""
    doc: Any
    book: Book
    _lists: Lists | None = field(default=None, repr=False)
    _alloc: IdAllocator | None = field(default=None, repr=False)
    _ids: Ids | None = field(default=None, repr=False)
    # Tables of contents whose entries the batch's end builds.
    tocs: list = field(default_factory=list)
    # Bookmark names this call's add_bookmark operations make: folded -> as given.
    planned_bookmarks: dict = field(default_factory=dict)
    # Bookmarks whose page a cross-reference of this call shows.
    page_marks: set = field(default_factory=set)
    # Whether the batch's end numbers captions and refreshes cross-references,
    # and whether it takes every cross-reference's page from the layout
    # (update_fields), each one's old page kept for when there is none.
    refresh_fields: bool = False
    refresh_pages: bool = False
    page_fallback: dict = field(default_factory=dict)
    # Notes and comments this call made: (op index, kind, element, what it
    # adds, the comment it replies to), numbered when the batch ends.
    made_annotations: list = field(default_factory=list)

    def forget(self) -> None:
        """Read the document anew at the next use: XML was written that the
        call's id counters and list definitions have not seen."""
        self._ids = None
        self._lists = None
        self._alloc = None

    @property
    def ids(self) -> Ids:
        """Ids for the bookmarks, comments and revisions this call writes."""
        if self._ids is None:
            self._ids = Ids(self.doc)
        return self._ids

    @property
    def lists(self) -> Lists:
        if self._lists is None:
            self._lists = Lists(self.doc)
        return self._lists

    @property
    def alloc(self) -> IdAllocator:
        if self._alloc is None:
            self._alloc = IdAllocator(self.doc)
        return self._alloc


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_HYPERLINK_BLUE = "0563C1"
_TABLE_GRID = "Table Grid"
_LAST = -1


def _w(tag: str) -> str:
    return f"{{{_W_NS}}}{tag}"


def _table_at(job: _DocxJob, index: int, *, negative_is_last: bool = True) -> Any:
    """The table at ``index``; any negative index is the last table, as the
    tool has always read it (except where it never did: merge_table_cells)."""
    tables = job.doc.tables
    if not tables:
        raise OpError(Code.NOT_FOUND, "the document has no table", location=f"table_index {index}",
                      hint="Add one with add_table first.")
    if index < 0 and negative_is_last:
        return tables[-1]
    if not 0 <= index < len(tables):
        last = ", or -1 for the last" if negative_is_last else ""
        raise OpError(Code.NOT_FOUND,
                      f"table_index {index} does not exist (the document has {len(tables)} tables, "
                      f"so table_index runs from 0 to {len(tables) - 1}{last})",
                      location=f"table_index {index}",
                      hint="Read the document again for the current tables.")
    return tables[index]


def _target_table(job: _DocxJob, ctx: OpCtx, op: dict, default: int, *, destructive: bool = True,
                  negative_is_last: bool = True) -> Any:
    """The table an op acts on: its target (a table, or the table of a row
    or cell), else table_index."""
    from docx.table import Table

    one_of(op, "table_index")
    if op.get("target") is not None:
        located = locate(job.book, ctx, op, kinds=(addr.TABLE,), destructive=destructive)
        job.book.touch(located.part)
        return Table(located.element, PartParent(located.part))
    index = op.get("table_index", default)
    table = _table_at(job, index, negative_is_last=negative_is_last)
    if index >= 0:
        legacy_index(job.book, ctx, "table_index", index, table._tbl, tables=True)
    return table


def _section_at(job: _DocxJob, index: int) -> Any:
    sections = job.doc.sections
    if not -len(sections) <= index < len(sections):
        raise OpError(Code.NOT_FOUND,
                      f"section_index {index} does not exist (the document has {len(sections)} "
                      f"sections, so section_index runs from 0 to {len(sections) - 1}, or -1 for the last)",
                      location=f"section_index {index}",
                      hint="Read the document again for its sections.")
    return sections[index]


def _shading(color: Any, part: Any) -> Any:
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    attrs = " ".join(f'w:{name}="{value}"' for name, value in color_attrs(color, fill=True, part=part).items())
    # The clear pattern shows the fill; a missing or solid pattern draws the cell black or not at all.
    return parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" {attrs}/>')


# ---------------------------------------------------------------------------
# Write — headings & paragraphs
# ---------------------------------------------------------------------------


def _op_add_heading(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    level = op.get("level", 1)
    if not 0 <= level <= 9:
        raise OpError(Code.BAD_VALUE, f"level {level} is not a heading level",
                      hint="Give level 1 to 9, or 0 for the document title.")
    style = "Title" if level == 0 else f"Heading {level}"
    p = cursor(job.book, ctx, op).paragraph()
    p.add_run(op["text"])
    apply_paragraph_style(p, style)
    created(job, ctx, p._p)


def _op_add_paragraph(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    runs_data = op.get("runs")
    equation_runs(runs_data or [])
    p = cursor(job.book, ctx, op).paragraph()
    fill_runs(p, runs_data, op)
    for warning in apply_paragraph_format(p, op):
        ctx.warn(warning)
    _track_new(job, ctx, op, p._p)
    created(job, ctx, p._p)


def _op_add_equation(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    omath = latex_to_omml_element(op["latex"], display=True)
    align = alignment(op.get("alignment", "center"))
    p = cursor(job.book, ctx, op).paragraph()
    omp = omath.makeelement(f"{{{OMML_NS}}}oMathPara", None, None)
    omp.append(omath)
    p._p.append(omp)
    p.alignment = align
    created(job, ctx, p._p)


def _op_insert_paragraph(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    one_of(op, "index")
    if op.get("target") is not None:
        paragraph = cursor(job.book, ctx, op).paragraph()
    else:
        index = op.get("index", 0)
        before = paragraph_at(job, index, allow_end=True)
        if before is not None:
            legacy_index(job.book, ctx, "index", index, before._p)
        new_p = OxmlElement("w:p")
        if before is not None:
            before._element.addprevious(new_p)
        else:
            # The body's own w:sectPr must stay its last child.
            job.doc.element.body.insert_element_before(new_p, "w:sectPr")
        paragraph = Paragraph(new_p, job.doc)
    run = paragraph.add_run(op.get("text", ""))
    apply_run_format(run, op)
    for warning in apply_paragraph_format(paragraph, op):
        ctx.warn(warning)
    _track_new(job, ctx, op, paragraph._p)
    created(job, ctx, paragraph._p)


def _track_new(job: _DocxJob, ctx: OpCtx, op: dict, p: Any) -> None:
    rev = revision_for(job, op, ctx)
    if rev is not None:
        track_new_paragraph(p, rev)


def _op_delete_paragraph(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    p = target_paragraph(job, ctx, op, "index")
    where = f"target {op['target']}" if op.get("target") is not None else f"index {op.get('index', 0)}"
    rev = revision_for(job, op, ctx)
    if rev is not None and (done := already_deleted(job.doc, p._p, op, "index")):
        raise NoOp(done)
    if rev is not None:
        why = ("it holds a text box" if addr.textboxes(p._p) else
               "it holds one end of a field that spans paragraphs" if field_balance(p._p) else
               "it ends a section" if p._p.find(f"{_w('pPr')}/{_w('sectPr')}") is not None else None)
        shown = addr.address_of(job.doc, p._p) or where
        mark = own_new_paragraph(p._p, rev) if why is None else None
        if mark is not None:
            # The author's own new paragraph goes outright, as Word removes
            # what one inserted oneself, with the mark that came with it.
            if mark.getparent() is not None and not any(mark is m for m in p._p.iter()):
                mark.getparent().remove(mark)
            remove_paragraph(job.book, ctx, p._p, where)
            ctx.note(f"{shown} was this author's own tracked insertion, so it is removed outright")
            return
        if why is None:
            track_delete_paragraph(p._p, rev)
            ctx.note(f"{shown} is marked deleted; it goes when the change is accepted")
            return
        cannot_track(ctx, op, why)
    remove_paragraph(job.book, ctx, p._p, where)


def _op_set_paragraph_format(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    p = target_paragraph(job, ctx, op, "index")
    for warning in apply_paragraph_format(p, op):
        ctx.warn(warning)


def _op_set_paragraph_text(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    """The paragraph's content replaced; its paragraph properties, the first
    run's formatting, its note and comment references and its range marks
    (bookmarks, comment ranges) kept."""
    p = target_paragraph(job, ctx, op, "index", default=None)
    runs_data = op.get("runs")
    equation_runs(runs_data or [])
    if field_balance(p._p) != 0:
        raise OpError(Code.BAD_VALUE, "the paragraph holds one end of a field that spans paragraphs (a table of "
                      "contents)", hint="Change the text with search_and_replace, which leaves fields whole.")
    rev = revision_for(job, op, ctx)
    if rev is not None and _set_text_tracked(job, ctx, op, p, rev):
        return
    base = plain_props(text_base_rpr(p._p))
    kept = take_annotations(p._p)
    for child in list(p._p):
        if child.tag != _w("pPr"):
            p._p.remove(child)
    fill_runs(p, runs_data, op, base_rpr=base)
    put_back(p._p, kept)
    new_text_id(p._p, job.alloc)


def _set_text_tracked(job: _DocxJob, ctx: OpCtx, op: dict, p: Any, rev: Any) -> bool:
    """set_paragraph_text as tracked changes: a word diff for plain text, or
    the old text deleted and the new inserted. False when it cannot track."""
    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    if any(a.tag == _w("comment") for a in p._p.iterancestors()):
        cannot_track(ctx, op, "the paragraph is inside a comment")
        return False
    formatted = any(op.get(key) is not None for key in (
        "runs", "bold", "italic", "underline", "strikethrough", "color", "font_size", "font_name"))
    scratch = Paragraph(OxmlElement("w:p"), p._parent)
    fill_runs(scratch, op.get("runs"), op, base_rpr=plain_props(text_base_rpr(p._p)))
    track_set_text(p._p, list(scratch._p), None if formatted else op.get("text", ""), rev)
    new_text_id(p._p, job.alloc)
    return True


# ---------------------------------------------------------------------------
# Write — lists
# ---------------------------------------------------------------------------


_LIST_LEVELS = 9
_LIST_ITEM_KEYS = ("text", "level")


def _op_add_list(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    items = op.get("items", [])
    kind = NUMBER if op.get("ordered", False) else BULLET
    default_level = op.get("level", 1)
    entries = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            unknown = sorted(set(item) - set(_LIST_ITEM_KEYS))
            if unknown:
                raise OpError(Code.UNKNOWN_KEY, f"items[{i}] has unknown key {', '.join(unknown)}",
                              location=f"items[{i}]",
                              hint="An item is text, or {\"text\": ..., \"level\": 1-9}.")
            text, lvl = item.get("text", ""), item.get("level", default_level)
        else:
            text, lvl = item, default_level
        if isinstance(lvl, str):
            with contextlib.suppress(ValueError):
                lvl = float(lvl.strip())
        if isinstance(lvl, bool) or not isinstance(lvl, (int, float)) or int(lvl) != lvl:
            raise OpError(Code.BAD_TYPE, f"items[{i}].level {lvl!r} is not a whole number",
                          location=f"items[{i}]", hint="Give level as 1 to 9.")
        entries.append((str(text), int(lvl)))
    fmt = number_format(op["number_format"]) if op.get("number_format") is not None else None
    start = op.get("start")
    if start is not None and start < 0:
        raise OpError(Code.BAD_VALUE, f"start {start} is below 0", hint="Give the first number, such as 1.")
    if op.get("continue", False) and (fmt is not None or start is not None):
        raise OpError(Code.BAD_VALUE, "continue carries on the previous list, so it takes no start or "
                      "number_format", hint="Give continue, or start and number_format for a new list.")
    if kind == BULLET and (fmt is not None or start is not None):
        raise OpError(Code.BAD_VALUE, "number_format and start are for numbered lists",
                      hint="Add ordered: true, or leave them out.")
    spot = cursor(job.book, ctx, op)
    num_id = job.lists.start_list(kind, carry_on=op.get("continue", False), start=start, number_format=fmt)
    made = []
    for text, lvl in entries:
        clamped = min(max(lvl, 1), _LIST_LEVELS)
        if clamped != lvl:
            ctx.warn(f"level {lvl} is outside 1-{_LIST_LEVELS}; written at level {clamped}.")
        paragraph = spot.paragraph()
        job.lists.add_item(text, kind, clamped, num_id, paragraph=paragraph)
        made.append(paragraph._p)
    created(job, ctx, *made)


_HEADING_STYLES = ("nested", "simple", "off")
_HEADINGS_OFF = "off"


def _op_number_headings(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    levels = op.get("levels", 3)
    if not 1 <= levels <= 9:
        raise OpError(Code.BAD_VALUE, f"levels {levels} is outside 1-9", hint="Give levels 1 to 9.")
    style = str(op.get("style", "nested")).strip().lower()
    if style not in _HEADING_STYLES:
        raise OpError(Code.BAD_VALUE, f"style '{op['style']}' is not one of: nested, simple, off",
                      hint="nested numbers 1, 1.1, 1.1.1; simple numbers each level on its own; off removes them.")
    heading_ids = [ensure_style(job.doc, f"Heading {n}") for n in range(1, levels + 1)]
    changed = job.lists.number_headings(heading_ids, nested=style == "nested", off=style == _HEADINGS_OFF)
    if not changed:
        raise NoOp("the headings already have no numbers")


def _op_set_properties(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    props = job.doc.core_properties
    given = 0
    for key, attr in {"title": "title", "author": "author", "subject": "subject", "keywords": "keywords",
                      "description": "comments", "category": "category"}.items():
        if op.get(key) is not None:
            setattr(props, attr, op[key])
            given += 1
    if not given:
        raise NoOp("no property was given")


# ---------------------------------------------------------------------------
# Write — tables
# ---------------------------------------------------------------------------


def _op_add_table(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    headers = op.get("headers", [])
    rows = op.get("rows", [])
    if not headers:
        raise OpError(Code.BAD_VALUE, "a table needs at least one header cell",
                      hint="Give headers: a list with one name per column.")
    style = op.get("style", _TABLE_GRID)
    try:
        style_id = resolve_style(job.doc, style, TABLE_STYLE).styleId
    except OpError:
        style_id = resolve_style(job.doc, _TABLE_GRID, TABLE_STYLE).styleId
        ctx.warn(f"the document has no table style '{style}'; the table uses '{_TABLE_GRID}'.")
    table = cursor(job.book, ctx, op).table(1 + len(rows), len(headers))
    table._tbl.tblPr.style = style_id
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = str(h)
    dropped = 0
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            if ci < len(headers):
                table.rows[ri + 1].cells[ci].text = str(val)
            else:
                dropped += 1
    if dropped:
        ctx.warn(f"{dropped} cell{'s' if dropped != 1 else ''} past the {len(headers)} header columns "
                 "were left out.")
    created(job, ctx, table._tbl)


def _op_delete_table(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    table = _target_table(job, ctx, op, _LAST)
    job.book.remove(ctx, table._tbl)


def _cell_index(table: Any, key: str, value: int, count: int) -> int:
    if not 0 <= value < count:
        raise OpError(Code.NOT_FOUND, f"{key} {value} does not exist (the table has {count})",
                      location=f"{key} {value}", hint="Rows and columns count from 0.")
    return value


def _op_merge_table_cells(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    table = _target_table(job, ctx, op, 0, negative_is_last=False)
    rows, cols = len(table.rows), len(table.columns)
    sr = _cell_index(table, "start_row", op["start_row"], rows)
    sc = _cell_index(table, "start_col", op["start_col"], cols)
    er = _cell_index(table, "end_row", op["end_row"], rows)
    ec = _cell_index(table, "end_col", op["end_col"], cols)
    table.cell(sr, sc).merge(table.cell(er, ec))


def _op_add_table_row(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    """A row at the end of the table, or after (or before) the target row."""
    one_of(op, "table_index")
    anchor = None
    address = addr.parse(op["target"]) if op.get("target") is not None else None
    if address is not None and address.segments and address.segments[-1][0] == addr.ROW:
        from docx.table import Table

        located = locate(job.book, ctx, op, kinds=(addr.ROW,))
        anchor = located.element
        table = Table(located.container, PartParent(located.part))
        job.book.touch(located.part)
    else:
        table = _target_table(job, ctx, op, _LAST)
    position = (op.get("position") or "after").strip().lower()
    if position not in ("after", "before"):
        raise OpError(Code.BAD_VALUE, f"position '{op['position']}' is not after or before",
                      hint="Give position: after or before (the target row).")
    new_row = table.add_row()
    if anchor is not None:
        (anchor.addprevious if position == "before" else anchor.addnext)(new_row._tr)
    values = op.get("values", [])
    for ci, val in enumerate(values[:len(new_row.cells)]):
        new_row.cells[ci].text = str(val)
    if len(values) > len(new_row.cells):
        ctx.warn(f"{len(values) - len(new_row.cells)} value(s) past the table's {len(new_row.cells)} "
                 "columns were left out.")
    rev = revision_for(job, op, ctx)
    if rev is not None:
        track_row(new_row._tr, "ins", rev)


def _remove_row(job: _DocxJob, ctx: OpCtx, op: dict, tr: Any) -> None:
    rev = revision_for(job, op, ctx)
    if rev is not None and (why := already_deleted(job.doc, tr, op, "row_index")):
        raise NoOp(why)
    if rev is not None and track_row(tr, "del", rev):
        ctx.note("the row is marked deleted; it goes when the change is accepted")
        return
    job.book.remove(ctx, tr)


def _op_delete_table_row(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    one_of(op, "table_index")
    one_of(op, "row_index")
    if op.get("target") is not None:
        located = locate(job.book, ctx, op, kinds=(addr.ROW,))
        job.book.touch(located.part)
        _remove_row(job, ctx, op, located.element)
        return
    table = _target_table(job, ctx, op, _LAST)
    count = len(table.rows)
    row_idx = op.get("row_index", _LAST)
    if row_idx < 0:
        row_idx = count - 1
    if not 0 <= row_idx < count:
        raise OpError(Code.NOT_FOUND, f"row_index {row_idx} does not exist (the table has {count} rows)",
                      location=f"row_index {row_idx}", hint="Rows count from 0, or -1 for the last.")
    _remove_row(job, ctx, op, table.rows[row_idx]._tr)


def _op_set_table_column_widths(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    from docx.shared import Emu

    table = _target_table(job, ctx, op, _LAST)
    widths = op.get("widths", [])
    if not widths:
        raise NoOp("no widths were given")
    for ci, w in enumerate(widths[:len(table.columns)]):
        width = Emu(units.to_emu(w, default="in"))
        table.columns[ci].width = width
        for cell in table.columns[ci].cells:
            cell.width = width
    if len(widths) > len(table.columns):
        ctx.warn(f"the table has {len(table.columns)} columns; the other widths were left out.")


def _set_shading(tc: Any, color: Any, part: Any) -> None:
    """One w:shd on the cell (replacing any), with the clear pattern Word
    needs to show the fill, placed in schema order."""
    tc_pr = tc.get_or_add_tcPr()
    for old in tc_pr.findall(_w("shd")):
        tc_pr.remove(old)
    insert_child(tc_pr, _shading(color, part))


def _op_style_table(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    table = _target_table(job, ctx, op, _LAST)
    header = op.get("header") or {}
    if header and table.rows:
        fill = header.get("fill") or None
        font_color = header.get("font_color") or None
        bold = header.get("bold", True)
        for cell in table.rows[0].cells:
            if fill is not None:
                _set_shading(cell._tc, fill, table.part)
            for p in cell.paragraphs:
                for run in p.runs:
                    if bold:
                        run.bold = True
                    if font_color is not None:
                        set_run_color(run, font_color)

    alt_colors = op.get("alternating_colors")
    if alt_colors and len(alt_colors) < 2:
        ctx.warn("alternating_colors needs two colours or more; the rows were left as they were.")
    elif alt_colors:
        for ri, row in enumerate(table.rows):
            if ri == 0 and header:
                continue
            color = alt_colors[ri % len(alt_colors)]
            if color == "":
                continue
            for cell in row.cells:
                _set_shading(cell._tc, color, table.part)

    row_fill = op.get("row_fill") or {}
    for row_str, color in row_fill.items():
        try:
            ri = int(row_str)
        except (TypeError, ValueError):
            raise OpError(Code.BAD_VALUE, f"row_fill key '{row_str}' is not a row number",
                          hint='Give row_fill as {"0": "#D9E1F2"}.') from None
        if not 0 <= ri < len(table.rows):
            ctx.warn(f"row_fill row {ri} does not exist (the table has {len(table.rows)} rows); it was skipped.")
            continue
        if color in (None, ""):
            continue
        for cell in table.rows[ri].cells:
            _set_shading(cell._tc, color, table.part)

    border_val = op.get("border")
    if border_val:
        bcolor, bsize = "000000", "4"
        if isinstance(border_val, dict):
            if border_val.get("color"):
                bcolor = color_attrs(border_val["color"], part=table.part)["val"]
            bsize = str(border_val.get("size", 4))
        tbl = table._tbl
        tbl_pr = tbl.find(_w("tblPr"))
        if tbl_pr is None:
            tbl_pr = insert_child(tbl, parse_xml(f'<w:tblPr {nsdecls("w")}/>'))
        for old in tbl_pr.findall(_w("tblBorders")):
            tbl_pr.remove(old)
        edges = "".join(f'<w:{edge} w:val="single" w:sz="{bsize}" w:space="0" w:color="{bcolor}"/>'
                        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
        insert_child(tbl_pr, parse_xml(f'<w:tblBorders {nsdecls("w")}>{edges}</w:tblBorders>'))


_RULE_STYLES = ("single", "double", "dotted", "dashed", "thick")


def _op_add_horizontal_rule(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    """An empty paragraph with a bottom border: the rule Word draws itself."""
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    style = str(op.get("style", "single")).strip().lower()
    if style not in _RULE_STYLES:
        raise OpError(Code.BAD_VALUE, f"style '{op['style']}' is not one of: {', '.join(_RULE_STYLES)}",
                      hint="Give single, double, dotted, dashed or thick.")
    eighths = round(units.to_pt(op.get("size", 1), default=units.POINTS) * 8)
    if not 2 <= eighths <= 96:
        raise OpError(Code.BAD_VALUE, "size is outside 0.25 to 12 points", hint="Give size in points, such as 1.")
    paragraph = cursor(job.book, ctx, op, allow_containers=False).paragraph()
    attrs = color_attrs(op["color"], part=paragraph.part) if op.get("color") else {"val": "auto"}
    color = " ".join(f'w:{"color" if name == "val" else name}="{value}"' for name, value in attrs.items())
    border = parse_xml(f'<w:pBdr {nsdecls("w")}><w:bottom w:val="{style}" w:sz="{eighths}" w:space="1" '
                       f'{color}/></w:pBdr>')
    insert_child(paragraph._p.get_or_add_pPr(), border)
    created(job, ctx, paragraph._p)


# ---------------------------------------------------------------------------
# Write — images
# ---------------------------------------------------------------------------


_IMAGE_WIDTH_IN = 4.0


def _op_add_image(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    from docx.shared import Emu

    height = (Emu(units.to_emu(op["height_inches"], default="in"))
              if op.get("height_inches") is not None else None)
    if op.get("width_inches") is not None:
        width = Emu(units.to_emu(op["width_inches"], default="in"))
    elif height is None:
        width = Emu(units.to_emu(_IMAGE_WIDTH_IN, default="in"))
    else:
        width = None  # python-docx scales it from the picture and the height
    align = alignment(op["alignment"]) if op.get("alignment") is not None else None
    paragraph = cursor(job.book, ctx, op).paragraph()
    picture = paragraph.add_run().add_picture(op["image_path"], width=width, height=height)
    # Word writes the four distances as 0; with none, LibreOffice and
    # Collabora space the picture 0.32 cm from the margin.
    for side in ("distT", "distB", "distL", "distR"):
        picture._inline.set(side, "0")
    if align is not None:
        paragraph.alignment = align
    created(job, ctx, paragraph._p)


# ---------------------------------------------------------------------------
# Write — page structure
# ---------------------------------------------------------------------------


def _op_add_page_break(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    from docx.enum.text import WD_BREAK

    paragraph = cursor(job.book, ctx, op, allow_containers=False).paragraph()
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    created(job, ctx, paragraph._p)


@dataclass
class _PageSetup:
    size: str | None
    orientation: str | None
    margins: dict[str, Any]


def _page_setup(op: dict) -> _PageSetup:
    """Size, orientation and margins from the op, every value checked."""
    from docx.shared import Emu

    margins = {m: Emu(units.to_emu(op[f"margin_{m}"], default="in"))
               for m in ("top", "bottom", "left", "right") if op.get(f"margin_{m}") is not None}
    try:
        size = check_page_size(op["page_size"]) if op.get("page_size") is not None else None
        orientation = (check_orientation(op["orientation"])
                       if op.get("orientation") is not None else None)
    except ValueError as exc:
        raise OpError(Code.BAD_VALUE, str(exc).rstrip("."),
                      hint="Give one of the values named.") from None
    return _PageSetup(size=size, orientation=orientation, margins=margins)


def _apply_page_setup(section: Any, setup: _PageSetup) -> None:
    if setup.size:
        set_page_size(section, setup.size)
    if setup.orientation:
        set_orientation(section, setup.orientation)
    for margin, value in setup.margins.items():
        setattr(section, f"{margin}_margin", value)


def _op_add_section_break(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    # Checked before the break exists: a bad value leaves no new section behind.
    setup = _page_setup(op)
    _apply_page_setup(job.doc.add_section(), setup)


def _op_set_page_setup(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    _apply_page_setup(job.doc.sections[-1], _page_setup(op))


# ---------------------------------------------------------------------------
# Write — headers & footers
# ---------------------------------------------------------------------------


_STORY_KINDS = addr.KINDS


def _story(job: _DocxJob, op: dict, which: str) -> Any:
    """The header or footer of the op's section and kind, its section set to
    show that kind (a different first page, odd and even pages)."""
    section = _section_at(job, op.get("section_index", _LAST))
    kind = str(op.get("kind", addr.DEFAULT_KIND)).strip().lower()
    if kind not in _STORY_KINDS:
        raise OpError(Code.BAD_VALUE, f"kind '{op['kind']}' is not one of: {', '.join(_STORY_KINDS)}",
                      hint="Give default, first (the first page) or even (even pages).")
    if kind == addr.FIRST_KIND:
        section.different_first_page_header_footer = True
        return getattr(section, f"first_page_{which}")
    if kind == addr.EVEN_KIND:
        job.doc.settings.odd_and_even_pages_header_footer = True
        return getattr(section, f"even_page_{which}")
    return getattr(section, which)


def _fill_story(job: _DocxJob, story: Any, op: dict) -> None:
    """The first paragraph of a header or footer, cleared and refilled."""
    runs_data = op.get("runs")
    equation_runs(runs_data or [])
    align = alignment(op["alignment"]) if op.get("alignment") is not None else None
    story.is_linked_to_previous = False
    p = story.paragraphs[0] if story.paragraphs else story.add_paragraph()
    p.clear()
    fill_runs(p, runs_data, op)
    if align is not None:
        p.alignment = align
    job.book.touch(story.part)


def _op_add_header(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    _fill_story(job, _story(job, op, "header"), op)


def _op_add_footer(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    _fill_story(job, _story(job, op, "footer"), op)


def _op_add_page_number(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    import copy

    footer = _story(job, op, "footer")
    align = alignment(op.get("position", "center"), "position")
    fmt = op.get("format", "{page}")
    look = {key: op[key] for key in ("font_size", "color") if op.get(key) is not None}
    footer.is_linked_to_previous = False
    job.book.touch(footer.part)
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    base = None
    # On a line of its own below the footer's words, drawn like them: their
    # paragraph style and run formatting, so their effective size too.
    if p.text.strip():
        base = plain_props(first_text_rpr(p))
        props = plain_props(p._p.find(_w("pPr")), "numPr", "jc")
        p = footer.add_paragraph()
        if props is not None:
            p._p.insert(0, props)
    else:
        look.setdefault("font_size", 10)
    p.alignment = align

    def dress(run: Any) -> None:
        if base is not None:
            insert_child(run._r, copy.deepcopy(base))
        apply_run_format(run, look)

    parts = fmt.replace("{page}", "\x00PAGE\x00").replace("{total}", "\x00NUMPAGES\x00").split("\x00")
    for part in parts:
        if not part:
            continue
        if part in ("PAGE", "NUMPAGES"):
            add_field(p, f" {part} ", "1", dress)
        else:
            dress(p.add_run(part))


# ---------------------------------------------------------------------------
# Write — hyperlinks
# ---------------------------------------------------------------------------


def _add_hyperlink_to_paragraph(paragraph: Any, url: str | None, text: str, color: Any, underline: bool,
                                bookmark: str | None = None) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    hyperlink = OxmlElement("w:hyperlink")
    if bookmark is not None:
        hyperlink.set(qn("w:anchor"), bookmark)
        hyperlink.set(qn("w:history"), "1")
    else:
        hyperlink.set(qn("r:id"), paragraph.part.relate_to(url, _HYPERLINK_REL, is_external=True))
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rStyle = OxmlElement("w:rStyle")
    rStyle.set(qn("w:val"), "Hyperlink")
    insert_child(rPr, rStyle)
    if color:
        c_elem = OxmlElement("w:color")
        for name, value in color_attrs(color, part=paragraph.part).items():
            c_elem.set(qn(f"w:{name}"), value)
        insert_child(rPr, c_elem)
    if underline:
        u_elem = OxmlElement("w:u")
        u_elem.set(qn("w:val"), "single")
        insert_child(rPr, u_elem)
    new_run.append(rPr)
    t_elem = OxmlElement("w:t")
    t_elem.set(qn("xml:space"), "preserve")
    t_elem.text = text
    new_run.append(t_elem)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _op_add_hyperlink(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    url, bookmark = op.get("url"), op.get("bookmark")
    if url is not None and bookmark is not None:
        raise OpError(Code.BAD_VALUE, "give url or bookmark, not both",
                      hint="url links out of the document; bookmark links to a place in it.")
    if bookmark is not None:
        bookmark = link_target(job, bookmark)
    one_of(op, "paragraph_index")
    pi = op.get("paragraph_index")
    if op.get("target") is None and pi is not None and not 0 <= pi < len(job.doc.paragraphs):
        ctx.warn(f"paragraph_index {pi} is past the end of the document ({len(job.doc.paragraphs)} paragraphs); "
                 "the link went into a new paragraph at the end.")
        paragraph = job.doc.add_paragraph()
        created(job, ctx, paragraph._p)
    elif op.get("target") is not None or pi is not None:
        paragraph = target_paragraph(job, ctx, op, "paragraph_index")
    else:
        paragraph = job.doc.add_paragraph()
        created(job, ctx, paragraph._p)
    if bookmark is not None:
        link_style(job.doc)
    _add_hyperlink_to_paragraph(paragraph, url, op.get("text", url or bookmark), op.get("color", _HYPERLINK_BLUE),
                                op.get("underline", True), bookmark)


# ---------------------------------------------------------------------------
# Write — table of contents
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Write — search & replace
# ---------------------------------------------------------------------------


_SCOPE_LABELS = {"body": "the body", "header": "headers", "footer": "footers", "footnote": "footnotes",
                 "endnote": "endnotes", "comment": "comments"}
_SHOWN_PLACES = 10


def _scope_paragraphs(job: _DocxJob, ctx: OpCtx, op: dict) -> list[Any] | None:
    """The paragraphs a target limits an edit to, or None for the scope."""
    if op.get("target") is None:
        return None
    located = locate(job.book, ctx, op, kinds=(addr.PARAGRAPH, addr.TABLE, addr.CELL, addr.PART, addr.CONTROL,
                                               addr.CUSTOM))
    if located.kind == addr.PARAGRAPH:
        return [located.element]
    return [p for p in located.element.iter(addr.W_P) if not is_fallback_copy(p)]


def _places(job: _DocxJob, elements: list[Any]) -> str:
    shown = [addr.address_of(job.doc, el) or "a note" for el in elements[:_SHOWN_PLACES]]
    more = f" and {len(elements) - _SHOWN_PLACES} more" if len(elements) > _SHOWN_PLACES else ""
    return ", ".join(shown) + more


def _after_edit(job: _DocxJob, ctx: OpCtx, paragraphs: list[Any], parts: list[Any], data_bound: int) -> None:
    """New text ids for changed paragraphs, the parts marked touched, and
    the warnings an edit of text owes."""
    for part in parts:
        if getattr(part, "element", None) is not None:
            job.book.touch(part)
    for p in paragraphs:
        if not is_fallback_copy(p) and p.getroottree().getroot() is not None:
            new_text_id(p, job.alloc)
    if data_bound:
        ctx.warn(f"{data_bound} edit{'s are' if data_bound != 1 else ' is'} inside a content control bound to the "
                 "document's properties: Word refills it from them when the file opens. Change the property "
                 "with set_properties instead.")


def _op_search_and_replace(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    try:
        pattern = compile_pattern(op["find"], match_case=op.get("match_case", True),
                                  whole_word=op.get("whole_word", False), regex=op.get("regex", False))
    except ValueError as exc:
        raise OpError(Code.BAD_VALUE, str(exc).rstrip("."), hint="Give find as the text to look for.") from None
    paragraphs = _scope_paragraphs(job, ctx, op)
    rev = revision_for(job, op, ctx)
    if rev is not None and op.get("scope") == "comments":
        cannot_track(ctx, op, "comments are not tracked")
        rev = None
    try:
        result = replace_all(job.doc, pattern, op.get("replace", ""), regex_template=op.get("regex", False),
                             scope=op.get("scope", "all"), limit=op.get("limit"), paragraphs=paragraphs,
                             tracked=rev)
    except ValueError as exc:
        raise OpError(Code.BAD_VALUE, str(exc).rstrip("."),
                      hint="Scopes are all, body, headers, footers, notes and comments.") from None
    skipped = [h.paragraph for h in result.hits if h.skipped]
    many = result.skipped != 1
    where_kind = ", ".join(result.skipped_because) or "a link, field, content control or tracked change"
    verb = ("lie in" if many else "lies in") if result.skipped_because else ("cross" if many else "crosses")
    skip_note = (f"{result.skipped} match{'es' if many else ''} {verb} {where_kind} and "
                 f"{'were' if many else 'was'} left as {'they are' if many else 'it is'}: "
                 f"{_places(job, skipped)}") if skipped else ""
    if result.total == 0:
        raise NoOp(skip_note or f"{op['find']!r} was not found")
    where = ", ".join(f"{n} in {_SCOPE_LABELS.get(scope, scope)}" for scope, n in result.by_scope.items())
    ctx.note(f"{result.total} replaced{' as tracked changes' if rev is not None else ''}: {where}")
    if skip_note:
        ctx.warn(skip_note + ".")
    _after_edit(job, ctx, [h.paragraph for h in result.hits if h.count], result.changed_parts, result.data_bound)


def _op_fill_template(job: _DocxJob, ctx: OpCtx, op: dict) -> None:
    data = flatten_data(op["data"])
    delimiters = op.get("delimiters", ["{{", "}}"])
    if len(delimiters) != 2:
        raise OpError(Code.BAD_VALUE, "delimiters needs two strings", hint='Give delimiters as ["{{", "}}"].')
    try:
        result = fill_template(job.doc, data, delimiters=(str(delimiters[0]), str(delimiters[1])),
                               scope=op.get("scope", "all"))
    except ValueError as exc:
        raise OpError(Code.BAD_VALUE, str(exc).rstrip("."), hint="Check delimiters, scope and the values.") from None
    unresolved = [f"{addr.address_of(job.doc, p) or scope}: {token}" for p, scope, token in result.unresolved]
    if unresolved and op.get("strict", False):
        raise OpError(Code.BAD_VALUE, f"{len(unresolved)} token(s) have no value: "
                      + "; ".join(unresolved[:_SHOWN_PLACES]),
                      hint="Add the keys to data, or send strict: false to keep them.")
    filled = {k: n for k, n in result.filled.items() if n}
    if not filled and not unresolved:
        raise NoOp("the document has no tokens to fill")
    if filled:
        total = sum(filled.values())
        ctx.note(f"filled {total} token{'s' if total != 1 else ''}: "
                 + ", ".join(f"{k} {n}" for k, n in list(filled.items())[:_SHOWN_PLACES]))
    if unresolved:
        ctx.warn(f"{len(unresolved)} token(s) have no value in data and stay in the document: "
                 + "; ".join(unresolved[:_SHOWN_PLACES]) + ".")
    if result.unused:
        ctx.warn("These keys of data appear nowhere in the document: " + ", ".join(result.unused[:20]) + ".")
    if result.skipped:
        places = "; ".join(f"{addr.address_of(job.doc, p) or scope}: {t}" for p, scope, t in result.skipped[:5])
        ctx.warn(f"{len(result.skipped)} token(s) cross a link or a field and were left as they are: {places}.")
    _after_edit(job, ctx, [], result.changed_parts, 0)


# ---------------------------------------------------------------------------
# The catalogue: what {"op": "help"} answers and what the batch dispatches to
# ---------------------------------------------------------------------------


_HELP_NOTES = (
    "UNITS: a bare number is in the key's own unit, or give a unit (\"2cm\", \"12pt\", \"0.5in\"). "
    "space_before and space_after are points (6 to 18 is typical; not twips). line_spacing is a "
    "multiple of the line height (1.0 single, 1.5x, 150%) or a length such as 14pt. Indents, image "
    "sizes, table widths and margins are inches. Colours: hex, CSS names, rgb(), or a theme slot such "
    "as accent1 (it follows the document's theme). New documents are A4 portrait with 1 inch margins.\n"
    "ADDRESSES: read_document prints every block with its address; give it as target. Positional targets "
    "name the document as the call began (the read's numbering), an id (#1A2B3C4D) names its paragraph "
    "wherever it moved, and expect_text guards against a document someone changed since the read. index "
    "keys count the document as it is now. Every new block is reported with its address."
)

_SPACING_KEYS = (
    KeySpec("space_before", ("length",), unit="pt"),
    KeySpec("space_after", ("length",), unit="pt"),
    KeySpec("line_spacing", ("spacing",), unit="x", help="a multiple, or a length for exact spacing"),
    KeySpec("first_line_indent", ("length",), unit="in"),
    KeySpec("left_indent", ("length",), unit="in"),
    KeySpec("keep_together", ("bool",)),
    KeySpec("keep_with_next", ("bool",)),
    KeySpec("widow_control", ("bool",)),
    KeySpec("style", ("str",), help="a paragraph style by name: Quote, Caption, the template's own"),
    KeySpec("tab_stops", ("list",), item=(
        KeySpec("position", ("length",), unit="in", required=True),
        KeySpec("alignment", ("str",), default="left", help="left | center | right | decimal"),
        KeySpec("leader", ("str",), default="none", help="none | dot | hyphen | underscore | middle_dot | heavy"),
    )),
    KeySpec("clear_tab_stops", ("bool",), help="remove the paragraph's own tab stops first"),
)
_PAGE_KEYS = (
    KeySpec("orientation", ("str",), help="portrait | landscape"),
    KeySpec("page_size", ("str",), help="a4 | letter | legal | a3 | a5"),
    KeySpec("margin_top", ("length",), unit="in"),
    KeySpec("margin_bottom", ("length",), unit="in"),
    KeySpec("margin_left", ("length",), unit="in"),
    KeySpec("margin_right", ("length",), unit="in"),
)
_TABLE_INDEX_LAST = KeySpec("table_index", ("int",), default=_LAST, help="0-based; -1 is the last table")
_SECTION_INDEX = KeySpec("section_index", ("int",), default=_LAST, help="-1 is the last section")
_KIND = KeySpec("kind", ("str",), default="default",
                help="default | first (the section's first page) | even (even pages; odd ones take default)")
_A_TABLE = ({"op": "add_table", "headers": ["Item", "Qty"], "rows": [["Pens", "4"], ["Ink", "2"]]},)

CATALOG = Catalog("write_docx", [
    OpSpec("add_heading", _op_add_heading, (
        KeySpec("text", ("str",), required=True),
        KeySpec("level", ("int",), default=1, help="1-9; 0 is the document title"), *PLACE,
    ), "PARAGRAPHS", note="at the end, or at target",
        examples=({"op": "add_heading", "text": "Introduction", "level": 1},)),
    OpSpec("add_paragraph", _op_add_paragraph, (
        KeySpec("text", ("str",), default=""), RUNS, *RUN_KEYS, ALIGN, *_SPACING_KEYS, *PLACE, *TRACKED,
    ), "PARAGRAPHS", note="at the end, or at target",
        examples=({"op": "add_paragraph", "text": "Hello", "bold": True, "color": "accent1",
                   "space_after": 6, "line_spacing": "1.5x", "alignment": "justify", "style": "Quote",
                   "tab_stops": [{"position": "6in", "alignment": "right", "leader": "dot"}]},
                  {"op": "add_paragraph", "runs": [{"text": "Key", "style": "Strong"}, {"text": " point"}],
                   "clear_tab_stops": True},
                  {"op": "add_paragraph", "runs": [{"text": "Energy ", "italic": True},
                                                   {"equation": "E = mc^2"}]},
                  {"op": "add_paragraph", "text": "Before the first", "target": "p0", "position": "before",
                   "expect_text": "Hello"}),
        setup=({"op": "add_paragraph", "text": "Hello"},)),
    OpSpec("insert_paragraph", _op_insert_paragraph, (
        KeySpec("index", ("int",), default=0, help="insert before this paragraph, 0-based, counted now"),
        KeySpec("text", ("str",), default=""), *RUN_KEYS, ALIGN, *_SPACING_KEYS, *PLACE, *TRACKED,
    ), "PARAGRAPHS", note="index inserts before that paragraph; target inserts after it unless position: before",
        setup=({"op": "add_paragraph", "text": "x"},),
        examples=({"op": "insert_paragraph", "index": 0, "text": "First", "italic": True},
                  {"op": "insert_paragraph", "target": "p0", "text": "Second", "track": True,
                   "author": "Legal"})),
    OpSpec("delete_paragraph", _op_delete_paragraph, (
        KeySpec("index", ("int",), default=0), *AT, *TRACKED,
    ), "PARAGRAPHS", setup=({"op": "add_paragraph", "text": "x"}, {"op": "add_paragraph", "text": "y"}),
        examples=({"op": "delete_paragraph", "index": 0}, {"op": "delete_paragraph", "target": "p0",
                                                           "expect_text": "x"},
                  {"op": "delete_paragraph", "target": "p1", "track": True})),
    OpSpec("set_paragraph_text", _op_set_paragraph_text, (
        KeySpec("index", ("int",)), KeySpec("text", ("str",), default=""), RUNS, *RUN_KEYS, *AT, *TRACKED,
    ), "PARAGRAPHS", note="replaces the text; the paragraph's style and the first run's formatting stay",
        setup=({"op": "add_paragraph", "text": "Old", "bold": True},),
        examples=({"op": "set_paragraph_text", "target": "p0", "text": "New", "expect_text": "Old"},
                  {"op": "set_paragraph_text", "index": 0, "runs": [{"text": "Mixed", "italic": True}]},
                  {"op": "set_paragraph_text", "target": "p0", "text": "Old and new", "track": True})),
    OpSpec("set_paragraph_format", _op_set_paragraph_format, (
        KeySpec("index", ("int",), default=0), ALIGN, *_SPACING_KEYS, *AT,
    ), "PARAGRAPHS", setup=({"op": "add_paragraph", "text": "x"},),
        examples=({"op": "set_paragraph_format", "index": 0, "alignment": "center", "space_after": 6},)),
    OpSpec("add_horizontal_rule", _op_add_horizontal_rule, (
        KeySpec("style", ("str",), default="single", help="single | double | dotted | dashed | thick"),
        KeySpec("size", ("length",), unit="pt", default=1),
        KeySpec("color", COLOR),
        *PLACE,
    ), "PARAGRAPHS", note="a line across the page, drawn as a paragraph border",
        examples=({"op": "add_horizontal_rule", "color": "accent1", "size": 1.5},)),
    OpSpec("add_equation", _op_add_equation, (
        KeySpec("latex", ("str",), required=True, aliases=("equation",)),
        KeySpec("alignment", ("str",), default="center", help="left | center | right | justify"), *PLACE,
    ), "EQUATIONS", note="display math as a native, Word-editable equation (amsmath subset)",
        detail="For inline math give a run {equation: '<latex>'} among a paragraph's runs. Invalid "
               "LaTeX fails the operation with the reason.",
        examples=({"op": "add_equation", "latex": r"\frac{a}{b}"},)),
    OpSpec("add_list", _op_add_list, (
        KeySpec("items", ("list",), default=[], help="text, or {text, level}"),
        KeySpec("ordered", ("bool",), default=False),
        KeySpec("level", ("int",), default=1, help="1-9"),
        KeySpec("continue", ("bool",), default=False,
                help="carry on the previous list of the same kind; else the list starts at 1"),
        KeySpec("start", ("int",), help="the first number of a numbered list"),
        KeySpec("number_format", ("str",), help="decimal | lowerLetter | upperLetter | lowerRoman | upperRoman"),
        *PLACE,
    ), "LISTS", examples=({"op": "add_list", "items": ["One", {"text": "Detail", "level": 2}],
                           "ordered": True},
                          {"op": "add_list", "items": ["Five", "Six"], "ordered": True, "start": 5,
                           "number_format": "upperRoman"})),
    OpSpec("number_headings", _op_number_headings, (
        KeySpec("levels", ("int",), default=3, help="Heading 1 to Heading N"),
        KeySpec("style", ("str",), default="nested", help="nested (1, 1.1, 1.1.1) | simple | off"),
    ), "LISTS", note="numbers the heading styles, so every heading of those levels is numbered",
        setup=({"op": "add_heading", "text": "Scope", "level": 1}, {"op": "add_heading", "text": "Detail", "level": 2},
               {"op": "number_headings", "levels": 2}),
        examples=({"op": "number_headings", "levels": 2}, {"op": "number_headings", "style": "off"})),
    OpSpec("add_table", _op_add_table, (
        KeySpec("headers", ("list",), default=[]),
        KeySpec("rows", ("list",), default=[], help="lists of cells"),
        KeySpec("style", ("str",), default=_TABLE_GRID), *PLACE,
    ), "TABLES", examples=_A_TABLE),
    OpSpec("delete_table", _op_delete_table, (_TABLE_INDEX_LAST, *AT), "TABLES", setup=_A_TABLE,
           examples=({"op": "delete_table", "target": "t0", "expect_text": "Item"},)),
    OpSpec("merge_table_cells", _op_merge_table_cells, (
        KeySpec("table_index", ("int",), default=0),
        KeySpec("start_row", ("int",), required=True), KeySpec("start_col", ("int",), required=True),
        KeySpec("end_row", ("int",), required=True), KeySpec("end_col", ("int",), required=True), *AT,
    ), "TABLES", setup=_A_TABLE,
        examples=({"op": "merge_table_cells", "start_row": 1, "start_col": 0, "end_row": 2, "end_col": 0},)),
    OpSpec("add_table_row", _op_add_table_row, (
        _TABLE_INDEX_LAST, KeySpec("values", ("list",), default=[]), *PLACE, *TRACKED,
    ), "TABLES", note="at the end of the table, or after (before) the target row", setup=_A_TABLE,
        examples=({"op": "add_table_row", "values": ["Paper", "10"]},
                  {"op": "add_table_row", "target": "t0/r0", "values": ["First", "1"], "track": True})),
    OpSpec("delete_table_row", _op_delete_table_row, (
        _TABLE_INDEX_LAST, KeySpec("row_index", ("int",), default=_LAST, help="-1 is the last row"), *AT,
        *TRACKED,
    ), "TABLES", setup=_A_TABLE, examples=({"op": "delete_table_row", "row_index": 1},
                                           {"op": "delete_table_row", "target": "t0/r1", "author": "Legal"})),
    OpSpec("set_table_column_widths", _op_set_table_column_widths, (
        _TABLE_INDEX_LAST, KeySpec("widths", ("list",), default=[], of=("length",), unit="in"), *AT,
    ), "TABLES", setup=_A_TABLE, examples=({"op": "set_table_column_widths", "widths": [4, "3cm"]},)),
    OpSpec("style_table", _op_style_table, (
        _TABLE_INDEX_LAST,
        KeySpec("header", ("dict",), fields=(
            KeySpec("fill", COLOR, aliases=("background",)),
            KeySpec("font_color", COLOR, aliases=("color",)),
            KeySpec("bold", ("bool",), default=True),
        )),
        KeySpec("alternating_colors", ("list",), of=COLOR, aliases=("stripe_colors",)),
        KeySpec("row_fill", ("dict",), help="{row number: colour}"),
        KeySpec("border", ("bool", "dict"), fields=(
            KeySpec("color", COLOR), KeySpec("size", ("int",), help="eighths of a point"),
        ), help="true, or {color, size}"), *AT,
    ), "TABLES", setup=_A_TABLE,
        examples=({"op": "style_table", "header": {"fill": "1F4E79", "font_color": "white"},
                   "alternating_colors": ["FFFFFF", "F2F2F2"], "row_fill": {"2": "lightyellow"},
                   "border": {"color": "808080", "size": 4}},)),
    OpSpec("add_image", _op_add_image, (
        KeySpec("image_path", ("path",), required=True),
        KeySpec("width_inches", ("length",), unit="in", aliases=("width",),
                help="4 in unless height_inches alone sets it from the picture's proportions"),
        KeySpec("height_inches", ("length",), unit="in", aliases=("height",)),
        ALIGN, *PLACE,
    ), "IMAGES", note="inline, at the end or at target", examples=({"op": "add_image", "image_path": "logo.png",
                                                       "width_inches": 2, "alignment": "center"},)),
    OpSpec("add_page_break", _op_add_page_break, PLACE, "PAGE",
           note="starts a new page after the content; never the last operation",
           examples=({"op": "add_page_break"},)),
    OpSpec("add_section_break", _op_add_section_break, _PAGE_KEYS, "PAGE",
           note="a new section on a new page",
           examples=({"op": "add_section_break", "orientation": "landscape", "page_size": "a4"},)),
    OpSpec("set_page_setup", _op_set_page_setup, _PAGE_KEYS, "PAGE", note="the last section",
           examples=({"op": "set_page_setup", "page_size": "letter", "orientation": "portrait",
                      "margin_left": "2cm"},)),
    OpSpec("add_header", _op_add_header, (
        KeySpec("text", ("str",), default=""), RUNS, *RUN_KEYS, ALIGN, _SECTION_INDEX, _KIND,
    ), "HEADERS", note="replaces the header's first paragraph",
        examples=({"op": "add_header", "text": "Annual report", "alignment": "right"},)),
    OpSpec("add_footer", _op_add_footer, (
        KeySpec("text", ("str",), default=""), RUNS, *RUN_KEYS, ALIGN, _SECTION_INDEX, _KIND,
    ), "HEADERS", note="replaces the footer's first paragraph",
        examples=({"op": "add_footer", "runs": [{"text": "Confidential", "italic": True}]},)),
    OpSpec("add_page_number", _op_add_page_number, (
        KeySpec("position", ("str",), default="center", help="left | center | right"),
        KeySpec("format", ("str",), default="{page}", help="'{page}' or '{page} of {total}'"),
        KeySpec("font_size", ("length",), unit="pt", help="default: the footer's text size, else 10"),
        KeySpec("color", COLOR, help="default: the footer's text colour"),
        _SECTION_INDEX, _KIND,
    ), "HEADERS", note="in the footer", examples=({"op": "add_page_number", "format": "{page} of {total}"},)),
    OpSpec("add_hyperlink", _op_add_hyperlink, (
        KeySpec("url", ("str",)),
        KeySpec("bookmark", ("str",), help="a bookmark of this document (add_bookmark), instead of a url"),
        KeySpec("text", ("str",), help="default: the url or the bookmark's name"),
        KeySpec("color", COLOR, default=_HYPERLINK_BLUE),
        KeySpec("underline", ("bool",), default=True),
        KeySpec("paragraph_index", ("int",), help="join this paragraph; else a new one at the end"), *AT,
    ), "LINKS", one_of=(("url", "bookmark"),),
        examples=({"op": "add_hyperlink", "url": "https://example.com", "text": "Example"},
                  {"op": "add_hyperlink", "bookmark": "_top", "text": "Back to the top"})),
    *FIELD_OPS,
    OpSpec("search_and_replace", _op_search_and_replace, (
        KeySpec("find", ("str",), required=True),
        KeySpec("replace", ("str",), default="", help="literal text; with regex, \\1 and \\g<name> refer to groups"),
        KeySpec("match_case", ("bool",), default=True),
        KeySpec("whole_word", ("bool",), default=False),
        KeySpec("regex", ("bool",), default=False),
        KeySpec("scope", ("str",), default="all", help="all | body | headers | footers | notes | comments"),
        KeySpec("limit", ("int",), help="at most this many replacements"),
        *AT, *TRACKED,
    ), "EDITING", aliases=("find_and_replace", "replace_text"),
        note="every occurrence, across runs, formatting kept; body at any depth, every header and footer, notes",
        detail="A match never bridges a link, a field, a content control or a tracked change: such matches "
               "are reported and left. target limits the edit to a paragraph, table, cell or header.",
        setup=({"op": "add_paragraph", "text": "a cat and a cat"},),
        examples=({"op": "search_and_replace", "find": "cat", "replace": "dog"},
                  {"op": "search_and_replace", "find": r"(\w+) cat", "replace": r"\1 tiger", "regex": True,
                   "match_case": False, "whole_word": False, "scope": "body", "limit": 1, "target": "p0"})),
    OpSpec("set_properties", _op_set_properties, (
        KeySpec("title", ("str",)), KeySpec("author", ("str",)), KeySpec("subject", ("str",)),
        KeySpec("keywords", ("str",)), KeySpec("description", ("str",)), KeySpec("category", ("str",)),
    ), "EDITING", note="the document's properties (File > Info)",
        examples=({"op": "set_properties", "title": "Annual report", "author": "Finance team"},)),
    OpSpec("fill_template", _op_fill_template, (
        KeySpec("data", ("dict",), required=True, help="{key: value}; nested objects are a.b"),
        KeySpec("delimiters", ("list",), of=("str",), default=["{{", "}}"]),
        KeySpec("strict", ("bool",), default=False, help="fail when a token has no value"),
        KeySpec("scope", ("str",), default="all", help="all | body | headers | footers | notes"),
    ), "EDITING", aliases=("merge_template",),
        note="fills {{ key }} tokens everywhere, even split across runs; reports tokens left and keys unused",
        setup=({"op": "add_paragraph", "text": "Dear {{ client.name }},"},),
        examples=({"op": "fill_template", "data": {"client": {"name": "Ada"}}, "strict": True,
                   "delimiters": ["{{", "}}"], "scope": "all"},)),
    *REVIEW_OPS,
    *NOTE_OPS,
    *COMMENT_OPS,
    *CAPTION_OPS,
    *PATCH_OPS,
], notes=_HELP_NOTES)


# Operations that leave the body's XML alone; every other operation touches it.
_OFF_BODY = frozenset({"search_and_replace", "fill_template", "set_properties", "add_header", "add_footer",
                       "add_page_number", "number_headings", "set_track_changes", "accept_all_changes",
                       "reject_all_changes", "add_footnote", "add_endnote", "delete_note", "add_comment",
                       "reply_to_comment", "resolve_comment", "delete_comment", "xml_patch"})
# Operations whose edit is a tracked change when the document records
# changes, and operations that change nothing a reviewer tracks.
_TRACKABLE = frozenset({"search_and_replace", "set_paragraph_text", "insert_paragraph", "add_paragraph",
                        "delete_paragraph", "add_footnote", "add_endnote", "delete_note", "add_table_row",
                        "delete_table_row"})
_UNTRACKED_BY_NATURE = frozenset({"set_properties", "set_track_changes", "accept_all_changes",
                                  "reject_all_changes", "add_comment", "reply_to_comment", "resolve_comment",
                                  "delete_comment", "add_bookmark", "update_table_of_contents"})


def _created_note(addresses: list[str], first_id: str | None) -> str:
    where = addresses[0] if len(addresses) == 1 else f"{addresses[0]} to {addresses[-1]}"
    ident = f" #{first_id}" if first_id and len(addresses) == 1 else (f" (first #{first_id})" if first_id else "")
    return f"made {where}{ident}"


def _finalize(job: _DocxJob, outcome: BatchOutcome) -> list[str]:
    """The tables of contents' entries, paragraph ids on the touched parts,
    then where each new block landed."""
    doc = job.doc
    if job.tocs:
        build_all(job, outcome)
    if job.refresh_fields:
        refresh_fields(job)
    if any(r.status is Status.APPLIED and r.op not in _OFF_BODY for r in outcome.results):
        job.book.touch(doc.part)
    alloc = job.alloc
    for part in job.book.touched:
        ensure_w14_namespace(part)
    fresh = doc.part.document
    for part in stamping_order(job.book.touched):
        stamp_part(part.element, alloc)
    by_op: dict[int, list[Any]] = {}
    for index, element in job.book.created:
        if element.getparent() is not None:
            by_op.setdefault(index, []).append(element)
    results = {r.index: r for r in outcome.results}
    where = addr.address_map(fresh) if by_op else {}
    for index, elements in by_op.items():
        addresses = [a for a in (where.get(el) for el in elements) if a]
        if addresses and index in results:
            results[index].notes.append(_created_note(addresses, paragraph_id(elements[0])))
    if tracking_is_on(doc):
        for r in outcome.results:
            if r.status is Status.APPLIED and r.op not in _TRACKABLE | _UNTRACKED_BY_NATURE:
                r.warnings.append("not recorded as a tracked change (the document records changes; this "
                                  "kind of edit cannot be tracked here)")
    _annotation_notes(job, results)
    last = outcome.results[-1] if outcome.results else None
    if last is not None and last.op == "add_page_break" and last.status is Status.APPLIED:
        return ["The last operation is add_page_break: it leaves a blank page at the end. Remove it "
                "unless a blank last page is wanted."]
    return []


def _run_docx(call: WriteCall) -> tuple[Any, _DocxJob, BatchOutcome, Any]:
    baseline = collect_baseline(FMT, path=call.path, create_new=call.create_new,
                                check_modules=call.check_modules,
                                ops=[op.get("op") for _, op in call.ops if isinstance(op, dict)])
    if call.edit_existing:
        preflight(call.path)
    doc = open_document(call.path) if call.edit_existing else new_document()
    match_content_type_to_name(doc, call.path)
    job = _DocxJob(doc, Book.for_call(doc, call.ops))
    named = {"add_bookmark": "name", "add_caption": "bookmark"}
    job.planned_bookmarks = {op[named[op["op"]]].casefold(): op[named[op["op"]]] for _, op in call.ops
                             if op.get("op") in named and isinstance(op.get(named[op["op"]]), str)}
    outcome = call.run(CATALOG, job, finalize=_finalize)
    return doc, job, outcome, baseline


def _annotation_notes(job: _DocxJob, results: dict) -> None:
    """The notes and comments the call made, named by the numbers the
    finished document gives them (a later one may sit before an earlier)."""
    numbers: dict[int, str] = {}
    for kind in {kind for _, kind, *_ in job.made_annotations}:
        for n, (_, el) in enumerate(addr.annotation_order(job.doc, kind), 1):
            numbers[id(el)] = f"{kind}{n}"
    for index, _kind, element, suffix, head in job.made_annotations:
        result = results.get(index)
        if result is None or id(element) not in numbers:
            continue
        thread = f", a reply in the thread of {numbers[id(head)]}" if head is not None and id(head) in numbers else ""
        result.notes.append(f"made {numbers[id(element)]}{thread}{suffix}")


def _write_docx_core(call: WriteCall) -> WriteReport:
    """Worker core: load (or start A4), the batch, one atomic save."""
    doc, _job, outcome, baseline = _run_docx(call)
    # Document.save goes through the package, which serializes what each part
    # holds now: right even after _finalize rebuilt a part's root.
    return call.commit(outcome, kind=KIND, fmt=FMT, save=doc.save, baseline=baseline)


def _stage_docx_core(call: WriteCall) -> Staged | WriteReport:
    """Worker A of a call that builds a table of contents: the batch, then
    the staged save and its page-label probe, or the commit at once when no
    entry needs a page number."""
    import os

    doc, job, outcome, baseline = _run_docx(call)
    names, marks = names_for_pages(job), marks_for_pages(job)
    if not (names or marks) or not outcome.should_save_main:
        return call.commit(outcome, kind=KIND, fmt=FMT, save=doc.save, baseline=baseline)
    probe = os.path.join(call.work_dir, "toc-probe.docx")
    staged = call.stage(outcome, save=doc.save, baseline=baseline,
                        facts={"toc_names": names, "page_marks": marks, "toc_probe": probe,
                               "page_fallback": dict(job.page_fallback)})
    write_probe(doc, probe, marks)
    return staged


def _finish_docx(staged: Staged, call: WriteCall, rendered: Any) -> WriteReport:
    return finish_toc(staged, call, rendered, kind=KIND, fmt=FMT)


async def handle_write_docx(args: dict) -> str:
    """The parent: validation, path resolution, the worker (two, with a
    LibreOffice layout between them, when a table of contents needs page
    numbers), the preview."""
    batch = await read_call(args, kind=KIND)
    if any(op["op"] in PAGE_OPS for _, op in batch_ops(batch.items, CATALOG)):
        return await handle_write_call(args, CATALOG, _stage_docx_core, kind=KIND, advice=_DOCX_ADVICE,
                                       render=render_toc, finish=_finish_docx, batch=batch)
    return await handle_write_call(args, CATALOG, _write_docx_core, kind=KIND, advice=_DOCX_ADVICE, batch=batch)


def _blank_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    new_document().save(buffer)
    return buffer.getvalue()


def _register_blank() -> None:
    from importlib.metadata import version

    register_blank(FMT, _blank_bytes, version=f"python-docx {version('python-docx')}")


_register_blank()


def contract_job(tmp_dir: str) -> _DocxJob:
    doc = new_document()
    return _DocxJob(doc, Book.for_call(doc, []))


def contract_fixtures(tmp_dir: str) -> dict[str, str]:
    from PIL import Image

    logo = os.path.join(tmp_dir, "logo.png")
    Image.new("RGB", (4, 4), "navy").save(logo)
    return {"logo.png": logo}
