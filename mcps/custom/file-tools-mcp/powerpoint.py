"""write_pptx: PowerPoint presentations (.pptx and its template, show and
macro kinds) built or edited by a batch of operations.

The catalogue (``CATALOG``) is the dispatch table: every operation's keys,
handler and help. A batch runs in a worker on the shared batch API: every
operation is validated before any work, a failed one leaves the file as it
was (unless the caller passes ``best_effort``), and the result lists what
each operation did.
"""

import os
import secrets
from typing import Any

from batch import WriteCall, handle_write_call
from catalog import Catalog, KeySpec, OpSpec
from checks import collect_baseline, register_blank
from equations import latex_to_png
from formats.pptx import (
    pptx_animations,
    pptx_arrange,
    pptx_charts,
    pptx_effects,
    pptx_fill,
    pptx_patch,
    pptx_shapes,
    pptx_text,
    pptx_theme,
    pptx_transitions,
)
from formats.pptx.pptx_deck import (
    UnknownLayout,
    new_presentation,
    open_presentation,
    prepare_kind,
    resolve_layout,
    set_slide_size,
    slide_length,
)
from formats.pptx.pptx_fields import (
    first_number,
    follows_numbering,
    is_title_slide,
    number_slide,
    refresh_numbers,
    unnumber_slide,
)
from formats.pptx.pptx_opkit import (
    ANCHORS,
    EXAMPLE_PNG,
    FILL,
    FONT,
    G_IMAGES,
    G_NOTES,
    G_SLIDES,
    G_TABLES,
    G_TEXT,
    IN,
    RUNS,
    SHAPE_ONE_OF,
    SHAPE_REF,
    TEXT,
    TEXT_FRAME,
    PptxJob,
    apply_run_format,
    box_keys,
    choice,
    emu,
    fill_solid,
    from_end,
    name_of,
    paragraph_align,
    plural,
    set_color,
    shape_of,
    slide_key,
    slide_of,
    walk_shapes,
)
from ooxml import parts
from results import Code, NoOp, OpCtx, OpError, WriteReport
from shared import _WRITE_OP_ADVICE

KIND = "Presentation"
FMT = "pptx"

_PLACEHOLDER_WORDS = {"title": 0, "body": 1, "subtitle": 1, "content": 1}
_BULLET = "•"
_TITLE_WORD = "title"




# ---------------------------------------------------------------------------
# Slides
# ---------------------------------------------------------------------------


def _op_add_slide(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    try:
        layout, remark = resolve_layout(job.prs, op.get("layout"), strict=True)
    except UnknownLayout as exc:
        raise OpError(Code.NOT_FOUND, str(exc), hint=exc.listing) from None
    if remark:
        ctx.note(remark)
    slides = list(job.prs.slides)
    slide = job.prs.slides.add_slide(layout)
    # A new slide follows the deck's numbering, as PowerPoint does after
    # "Apply to All" (with or without the title slides).
    if follows_numbering(slides, is_title_slide(slide)):
        number_slide(job.prs, slide, first_number(job.prs) + len(slides))
    job.renumber = True
    if op.get("title"):
        if slide.shapes.title is None:
            ctx.warn(f"the layout {name_of(layout.name)} has no title placeholder, so the title was not set: "
                     "add a text box for it")
        else:
            slide.shapes.title.text = op["title"]
            job.touch(slide, slide.shapes.title)


def _op_delete_slide(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op, -1)
    # Declared, so the after-write check counts the slide and what only it
    # used (its notes, charts, pictures) as removed, not lost.
    ctx.removed(slide.part.partname)
    sld_lst = job.prs.slides._sldIdLst
    job.prs.part.drop_rel(sld_lst[position].rId)
    del sld_lst[position]
    job.renumber = True


# Comments belong to the slide they were left on: a duplicate starts
# without them (the 2006 comments and the 2018 modern ones, whose
# relationship the slide also names in a commentRel extension).
_COMMENT_RELTYPES = frozenset({
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    "http://schemas.microsoft.com/office/2018/10/relationships/comments",
})
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# PowerPoint names each slide with a creationId (co-authoring, Reuse
# Slides); a copy gets its own.
_CREATION_ID = "{http://schemas.microsoft.com/office/powerpoint/2010/main}creationId"


def _drop_references(element: Any, rids: set[str]) -> None:
    """Remove what names a relationship the part no longer has: the
    extension holding it, else the element itself."""
    from pptx.oxml.ns import qn

    for node in list(element.iter()):
        if not any(k.startswith(_R_NS) and v in rids for k, v in node.attrib.items()):
            continue
        holder = next((a for a in node.iterancestors() if a.tag == qn("p:ext")), node)
        parent = holder.getparent()
        if parent is None:
            continue
        parent.remove(holder)
        if parent.tag == qn("p:extLst") and len(parent) == 0 and parent.getparent() is not None:
            parent.getparent().remove(parent)


def _op_duplicate_slide(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    src, _ = slide_of(job, op, -1)
    # The copy owns its notes, charts (with their workbooks and styles),
    # tags and diagrams, and shares its layout, pictures and media; shape
    # ids stay, so animations, connectors and Morph names still match.
    copy_part = parts.copy_part(src.part)
    dropped = {rid for rid, rel in copy_part.rels.items() if rel.reltype in _COMMENT_RELTYPES}
    for rid in dropped:
        copy_part.rels.pop(rid)
    _drop_references(copy_part._element, dropped)
    for node in copy_part._element.iter(_CREATION_ID):
        node.set("val", str(secrets.randbits(32) or 1))
    if src.part in job.touched:
        job.touched[copy_part] = set(job.touched[src.part])
    sld_lst = job.prs.slides._sldIdLst
    sld_lst.add_sldId(job.prs.part.relate_to(copy_part, RT.SLIDE))
    if op.get("to_index") is not None:
        count = len(sld_lst)
        target = from_end(op["to_index"], count)
        if not 0 <= target < count:
            raise OpError(Code.NOT_FOUND, f"to_index {op['to_index']} is outside the deck (it has "
                          f"{plural(count, 'slide')} with the copy)", hint=f"Use 0 to {count - 1}, or -1 for the last.")
        element = sld_lst[-1]
        sld_lst.remove(element)
        sld_lst.insert(target, element)
    job.renumber = True


def _op_move_slide(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    sld_lst = job.prs.slides._sldIdLst
    n = len(sld_lst)
    asked = (op.get("from_index", 0), op.get("to_index", 0))
    from_idx, to_idx = (i + n if i < 0 else i for i in asked)
    if not (0 <= from_idx < n and 0 <= to_idx < n):
        raise OpError(Code.NOT_FOUND, f"index out of range (the deck has {plural(n, 'slide')})",
                      location=f"from_index {asked[0]}, to_index {asked[1]}",
                      hint=f"Use 0 to {n - 1}, or -1 for the last slide." if n else "Add a slide first.")
    if from_idx == to_idx:
        raise NoOp(f"slide {from_idx} is already there")
    el = sld_lst[from_idx]
    sld_lst.remove(el)
    sld_lst.insert(to_idx, el)
    job.renumber = True


def _op_set_slide_dimensions(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    set_slide_size(job.prs, slide_length(emu(op, "width", 13.333)), slide_length(emu(op, "height", 7.5)))


def _op_set_core_properties(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    props = job.prs.core_properties
    changed = False
    for key in ("title", "author", "subject", "keywords", "comments"):
        if op.get(key):
            setattr(props, key, op[key])
            changed = True
    if not changed:
        raise NoOp("no property was given")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


# What each placeholder word names when no placeholder has its number:
# LibreOffice writes placeholders without an idx, so every one reads as 0.
_PLACEHOLDER_KINDS = {
    "title": ("title", "ctrTitle"), "subtitle": ("subTitle", "body", "obj"),
    "body": ("body", "obj", "subTitle"), "content": ("obj", "body", "subTitle"),
}


def _placeholder(slide: Any, value: Any, position: int, ctx: OpCtx | None = None) -> Any:
    """The slide placeholder ``value`` names: a word or its idx (a word
    takes its number first, as it always did, then the first placeholder of
    its kind). With ``ctx``, a word it does not know names the body, as
    set_text always read it."""
    text = str(value).strip()
    word = text.casefold()
    if text.isdigit():
        ph_idx = int(text)
    elif word in _PLACEHOLDER_WORDS:
        ph_idx = _PLACEHOLDER_WORDS[word]
    elif ctx is not None:
        word, ph_idx = "body", 1
        ctx.note(f"placeholder '{value}' read as the body (placeholder 1)")
    else:
        raise OpError(Code.BAD_VALUE, f"unknown placeholder '{value}'",
                      hint="Use title, body, subtitle, content or a placeholder number from read_document.")
    placeholders = list(slide.placeholders)
    explicit = [p for p in placeholders if p._element.ph is not None and p._element.ph.get("idx") is not None]
    for shape in explicit if word in _PLACEHOLDER_KINDS else placeholders:
        if shape.placeholder_format.idx == ph_idx:
            return shape
    for kind in _PLACEHOLDER_KINDS.get(word, ()):
        for shape in placeholders:
            if shape._element.ph is not None and shape._element.ph.get("type", "obj") == kind:
                return shape
    present = ", ".join(f"{p._element.ph.get('idx', '-')} {name_of(p.name)}" for p in placeholders) or "none"
    raise OpError(Code.NOT_FOUND, f"the slide has no placeholder {ph_idx}",
                  location=f"slide_index {position}, placeholder {value!r}",
                  hint=f"Its placeholders (number, name; - has none): {present}. Use add_textbox for text elsewhere.")


def _add_run(paragraph: Any, text: str, fmt: dict, ctx: OpCtx) -> None:
    """``text`` as runs of ``paragraph``; a line feed inside it is a line
    break (a raw one in run text is not a new line in PowerPoint)."""
    for n, piece in enumerate(text.replace("\v", "\n").split("\n")):
        if n:
            paragraph.add_line_break()
        if piece or n == 0:
            run = paragraph.add_run()
            run.text = piece
            apply_run_format(run, fmt, ctx)


def _write_runs(frame: Any, op: dict, ctx: OpCtx) -> list[Any]:
    """The op's runs into the frame's first paragraph, or its text with the
    op's own font keys, a paragraph per line; the paragraphs written."""
    first = frame.paragraphs[0]
    runs_data = op.get("runs")
    if runs_data:
        for rd in runs_data:
            _add_run(first, rd.get("text", ""), rd, ctx)
        return [first]
    written = [first]
    for n, line in enumerate(op.get("text", "").split("\n")):
        paragraph = first if n == 0 else frame.add_paragraph()
        if n:
            written.append(paragraph)
        _add_run(paragraph, line, op, ctx)
    return written


def _op_set_text(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    shape = _placeholder(slide, op.get("placeholder", _TITLE_WORD), position, ctx)
    job.touch(slide, shape)
    tf = shape.text_frame
    if op.get("runs"):
        tf.text = ""
        _write_runs(tf, op, ctx)
    else:
        # A line feed starts a new paragraph, as it always did here.
        tf.text = op.get("text", "")
        for para in tf.paragraphs:
            for run in para.runs:
                apply_run_format(run, op, ctx)
    if op.get("alignment"):
        alignment = paragraph_align(op["alignment"])
        for para in tf.paragraphs:
            para.alignment = alignment
    pptx_text.format_frame(tf, op)
    pptx_text.format_paragraphs(list(tf.paragraphs), op, ctx)


def _op_add_textbox(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.enum.text import MSO_AUTO_SIZE

    slide, _ = slide_of(job, op)
    pptx_effects.check(outline_coloured=False, gradient=op.get("gradient"), transparency=op.get("transparency"),
                       line_transparency=op.get("line_transparency"), shadow=op.get("shadow"), glow=op.get("glow"),
                       soft_edges=op.get("soft_edges"), line_dash=op.get("line_dash"))
    box = slide.shapes.add_textbox(emu(op, "left", 1), emu(op, "top", 1), emu(op, "width", 6),
                                   emu(op, "height", 1))
    job.touch(slide, box)
    tf = box.text_frame
    tf.word_wrap = op.get("word_wrap", True)
    if op.get("auto_size"):
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    if op.get("margin") is not None:
        margin = emu(op, "margin", 0)
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = margin
    paragraphs = _write_runs(tf, op, ctx)
    if op.get("alignment"):
        for paragraph in paragraphs:
            paragraph.alignment = paragraph_align(op["alignment"])
    pptx_text.format_frame(tf, op)
    pptx_text.format_paragraphs(paragraphs, op, ctx)
    pptx_effects.apply(box, ctx, gradient=op.get("gradient"), transparency=op.get("transparency"),
                       line_transparency=op.get("line_transparency"), shadow=op.get("shadow"),
                       glow=op.get("glow"), soft_edges=op.get("soft_edges"), line_dash=op.get("line_dash"))


def _bullet_shape(slide: Any, op: dict, position: int) -> Any:
    """The shape whose text the items go into."""
    if any(op.get(k) is not None for k in ("shape_name", "shape_index", "shape_id")):
        shape = shape_of(slide, op, position)
        if not shape.has_text_frame:
            raise OpError(Code.BAD_VALUE, f"{name_of(shape.name)} holds no text", location=f"slide_index {position}",
                          hint="Name a text box, a shape or a text placeholder.")
        return shape
    return _placeholder(slide, op.get("placeholder", "body"), position)


def _op_add_bullet_points(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    slide, position = slide_of(job, op)
    target = _bullet_shape(slide, op, position)
    job.touch(slide, target)
    tf, placeholder = target.text_frame, target.is_placeholder
    written = []
    for n, item in enumerate(op.get("items", [])):
        if isinstance(item, dict):
            text, level = str(item.get("text", "")), item.get("level", 0)
        else:
            text, level = str(item), 0
        if isinstance(level, bool) or not isinstance(level, int):
            try:
                level = int(level)
            except (TypeError, ValueError):
                raise OpError(Code.BAD_VALUE, f"items[{n}].level {level!r} is not a whole number",
                              hint="Give a level from 0 to 8.") from None
        # The first item fills the frame's empty last line (a fresh
        # placeholder has one) instead of leaving it as an empty bullet; the
        # line's own paragraph properties (an intro line's "no bullet",
        # centring) go, so the item looks like the ones after it.
        last = tf.paragraphs[-1]
        if n == 0 and last.text == "":
            p = last
            for ppr in p._p.findall(qn("a:pPr")):
                p._p.remove(ppr)
        else:
            p = tf.add_paragraph()
        p.text = text
        p.level = min(max(level, 0), 8)
        for run in p.runs:
            if op.get("font_size"):
                run.font.size = Pt(op["font_size"])
            pptx_text.format_run(run, op)
        written.append(p)
    if not written:
        raise NoOp("items is empty")
    pptx_text.format_frame(tf, op)
    # A placeholder draws its layout's bullets; a text box or shape gets
    # real ones.
    pptx_text.format_paragraphs(written, op, ctx, bullet=None if placeholder else _BULLET)


def _op_format_text(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    find_text = op.get("find", "")
    target = None
    if any(op.get(k) is not None for k in ("shape_name", "shape_index", "shape_id")):
        target = shape_of(slide, op, position)
        if not target.has_text_frame:
            raise OpError(Code.BAD_VALUE, f"{name_of(target.name)} holds no text", location=f"slide_index {position}",
                          hint="Name a text box, a shape or a text placeholder.")
    hits = 0
    for shape in [target] if target is not None else slide.shapes:
        if not shape.has_text_frame:
            continue
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                if find_text in run.text:
                    apply_run_format(run, op, ctx)
                    job.touch(slide, shape)
                    hits += 1
    if not hits:
        if target is not None:
            raise NoOp(f"no text run of {name_of(target.name)} holds '{find_text}'" if find_text
                       else f"{name_of(target.name)} holds no text")
        raise NoOp(f"no text run holds '{find_text}'" if find_text else "the slide holds no text")


# ---------------------------------------------------------------------------
# Images and equations
# ---------------------------------------------------------------------------




def _op_add_equation(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    # LaTeX equation as a 4x-oversampled PNG picture (pptx has no reliable
    # native-math write path: python-pptx renders appended OMML as nothing).
    import tempfile

    from pptx.util import Emu

    slide, _ = slide_of(job, op)
    height_emu = emu(op, "height", 0.8)
    width = emu(op, "width", 0) if op.get("width") else None
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        latex_to_png(op["latex"], tmp.name, display=True, height_px=int(height_emu / 914400 * 96))
        slide.shapes.add_picture(tmp.name, emu(op, "left", 1), emu(op, "top", 2), width=width,
                                 height=None if width else Emu(height_emu))
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _op_add_table(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, _ = slide_of(job, op)
    headers = op.get("headers", [])
    rows = op.get("rows", [])
    cols = len(headers)
    if not cols:
        raise OpError(Code.BAD_VALUE, "headers is empty", hint="Give the table's column headers.")
    table = slide.shapes.add_table(1 + len(rows), cols, emu(op, "left", 1), emu(op, "top", 3),
                                   emu(op, "width", 8), emu(op, "height", 2)).table
    for ci, h in enumerate(headers):
        table.cell(0, ci).text = str(h)
    dropped = 0
    for ri, row in enumerate(rows):
        values = row if isinstance(row, list) else [row]
        for ci, val in enumerate(values):
            if ci < cols:
                table.cell(ri + 1, ci).text = "" if val is None else str(val)
            else:
                dropped += 1
    if dropped:
        ctx.warn(f"{plural(dropped, 'cell')} past the {plural(cols, 'header')} were left out")


def _op_format_table_cell(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.enum.text import MSO_ANCHOR

    slide, position = slide_of(job, op)
    ti = op.get("table_index", 0)
    tables = [s for s in slide.shapes if s.has_table]
    if not 0 <= from_end(ti, len(tables)) < len(tables):
        raise OpError(Code.NOT_FOUND, f"table_index {ti} does not exist (the slide has "
                      f"{plural(len(tables), 'table')})", location=f"slide_index {position}, table_index {ti}")
    table = tables[from_end(ti, len(tables))].table
    n_rows, n_cols = len(table.rows), len(table.columns)
    asked_row, asked_col = op.get("row", 0), op.get("col", 0)
    row, col = from_end(asked_row, n_rows), from_end(asked_col, n_cols)
    end_row = from_end(op.get("end_row", asked_row), n_rows)
    end_col = from_end(op.get("end_col", asked_col), n_cols)
    where = f"slide_index {position}, table_index {ti}, row {asked_row}, col {asked_col}"
    if not (0 <= row < n_rows and 0 <= col < n_cols):
        raise OpError(Code.NOT_FOUND, f"the cell does not exist (the table is {n_rows} rows by {n_cols} columns)",
                      location=where)
    if end_row >= n_rows or end_col >= n_cols:
        ctx.warn(f"the range was cut to the table's {n_rows} rows by {n_cols} columns")
    anchor = None
    if op.get("vertical_alignment"):
        word = choice(op["vertical_alignment"], ANCHORS, "vertical_alignment")
        anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}[word]
    alignment = paragraph_align(op["alignment"]) if op.get("alignment") else None
    for r in range(row, min(end_row, n_rows - 1) + 1):
        for c in range(col, min(end_col, n_cols - 1) + 1):
            cell = table.cell(r, c)
            if op.get("fill_color") is not None:
                fill_solid(cell.fill, op["fill_color"], ctx)
            for para in cell.text_frame.paragraphs:
                if alignment is not None:
                    para.alignment = alignment
                for run in para.runs:
                    apply_run_format(run, op, ctx)
            if anchor is not None:
                cell.vertical_anchor = anchor
    merge_to = op.get("merge_to")
    if merge_to:
        mr, mc = from_end(merge_to.get("row", row), n_rows), from_end(merge_to.get("col", col), n_cols)
        if not (0 <= mr < n_rows and 0 <= mc < n_cols):
            raise OpError(Code.NOT_FOUND, f"merge_to row {mr}, col {mc} is outside the table", location=where)
        table.cell(row, col).merge(table.cell(mr, mc))


# ---------------------------------------------------------------------------
# Notes, links, numbers, properties
# ---------------------------------------------------------------------------


def _op_set_speaker_notes(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, _ = slide_of(job, op)
    text = op.get("text", "")
    tf = slide.notes_slide.notes_text_frame
    if op.get("append") and tf.text.strip():
        tf.text = tf.text + "\n" + text
    else:
        tf.text = text


def _op_add_hyperlink(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    url = op["url"]
    shape = shape_of(slide, op, position)
    if shape.has_text_frame and op.get("text"):
        run = shape.text_frame.paragraphs[-1].add_run()
        run.text = op["text"]
        run.hyperlink.address = url
        set_color(run.font.color, op.get("color", "0563C1"), ctx)
        run.font.underline = True
        job.touch(slide, shape)
        return
    shape.click_action.hyperlink.address = url


def _op_set_slide_number(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slides = list(job.prs.slides)
    if not slides:
        raise NoOp("the presentation has no slides yet; add slides first, then number them")
    if op.get("slide_index") is not None:
        _, position = slide_of(job, op)
        positions = [position]
    else:
        positions = list(range(len(slides)))
    enabled = op.get("enabled", True)
    skip_title = op.get("skip_title_slide", False)
    first = first_number(job.prs)
    for position in positions:
        slide = slides[position]
        if enabled and not (skip_title and is_title_slide(slide)):
            number_slide(job.prs, slide, first + position)
        else:
            unnumber_slide(slide)
    job.renumber = True


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


CATALOG = Catalog("write_pptx", [
    # SLIDES
    OpSpec("add_slide", _op_add_slide, (
        KeySpec("layout", ("str", "int"), default="blank",
                help="a layout name as read_document lists them, or its number; title, title_and_content, "
                     "section_header, two_content, comparison, title_only and blank find that kind of layout"),
        KeySpec("title", ("str",), help="the new slide's title"),
    ), G_SLIDES, note="adds a slide at the end; it follows the deck's slide numbers",
        detail="A layout is found in this order: its exact name; then one of the seven words by the layout's kind "
               "(a layout merely named Content does not capture content); then its name with case and accents "
               "ignored; then its number. A name or number the deck does not have fails with the list.",
        examples=({"op": "add_slide", "layout": "title_and_content", "title": "Results"},
                  {"op": "add_slide", "layout": 0})),
    OpSpec("delete_slide", _op_delete_slide, (slide_key(default=-1),), G_SLIDES,
           examples=({"op": "delete_slide", "slide_index": 1},)),
    OpSpec("duplicate_slide", _op_duplicate_slide, (
        slide_key(default=-1),
        KeySpec("to_index", ("int",), help="where the copy goes, 0-based, -1 = last; default last"),
    ), G_SLIDES, note="the copy owns its notes and charts and shares its pictures",
        examples=({"op": "duplicate_slide", "slide_index": 0}, {"op": "duplicate_slide", "slide_index": 0,
                                                                "to_index": 1})),
    OpSpec("move_slide", _op_move_slide, (
        KeySpec("from_index", ("int",), default=0, help="0-based, -1 = last"),
        KeySpec("to_index", ("int",), default=0, help="0-based, -1 = last"),
    ), G_SLIDES, examples=({"op": "move_slide", "from_index": -1, "to_index": 0},)),
    OpSpec("set_slide_dimensions", _op_set_slide_dimensions, (
        KeySpec("width", ("length",), unit=IN, default=13.333),
        KeySpec("height", ("length",), unit=IN, default=7.5),
    ), G_SLIDES, note="the layouts' placeholders follow the new size; slides' own shapes stay where they are",
        examples=({"op": "set_slide_dimensions", "width": 10, "height": 7.5},)),
    OpSpec("set_core_properties", _op_set_core_properties, tuple(
        KeySpec(name, ("str",)) for name in ("title", "author", "subject", "keywords", "comments")),
        G_SLIDES, examples=({"op": "set_core_properties", "title": "Quarterly review", "author": "Finance"},)),
    # TEXT
    OpSpec("set_text", _op_set_text, (
        slide_key(),
        KeySpec("placeholder", ("str", "int"), default=_TITLE_WORD,
                help="title, body, subtitle, content, or a placeholder number from read_document"),
        TEXT, *FONT, RUNS,
        KeySpec("alignment", ("str",), help="left | center | right | justify; default the layout's"),
        *TEXT_FRAME,
    ), G_TEXT, note="replaces the placeholder's text",
        examples=({"op": "set_text", "slide_index": 0, "text": "Quarterly review", "bold": True},
                  {"op": "set_text", "slide_index": 0, "text": "Quarterly review", "alignment": "left"},
                  {"op": "set_text", "slide_index": 0, "placeholder": "body",
                   "runs": [{"text": "Revenue "}, {"text": "up", "bold": True, "color": "accent1"}]})),
    OpSpec("add_textbox", _op_add_textbox, (
        slide_key(), TEXT, *FONT, RUNS, *box_keys(1, 1, 6, 1),
        KeySpec("alignment", ("str",), help="left | center | right | justify"),
        KeySpec("word_wrap", ("bool",), default=True),
        KeySpec("auto_size", ("bool",), help="shrink the text to fit"),
        KeySpec("margin", ("length",), unit=IN, help="the four insets"), *TEXT_FRAME, *pptx_effects.KEYS,
    ), G_TEXT, examples=({"op": "add_textbox", "slide_index": 0, "text": "Source: annual report", "top": 6.8,
                          "height": 0.4, "font_size": 12, "alignment": "right", "margin": 0},
                         {"op": "add_textbox", "slide_index": 0, "text": "Πρώτο\vδεύτερο", "lang": "el-GR",
                          "line_spacing": "1.2x", "space_after": 6, "bullet": "number", "number_style":
                          "arabicParenR", "start_at": 3, "bullet_indent": 0.3, "letter_spacing": -0.5,
                          "insets": {"left": 0, "top": "2mm", "right": 0, "bottom": 0},
                          "vertical_anchor": "middle", "autofit": "shrink"})),
    OpSpec("add_bullet_points", _op_add_bullet_points, (
        slide_key(),
        KeySpec("items", ("list",), required=True, help="text, or {text, level (0-8)}"),
        KeySpec("font_size", ("float",), help="pt"),
        *SHAPE_REF,
        KeySpec("placeholder", ("str", "int"), default="body",
                help="title, body, subtitle, content or a number; without a shape key"),
        *pptx_text.RUN_KEYS, *TEXT_FRAME,
    ), G_TEXT, note="appends the items to a shape's or placeholder's text; a text box or shape gets real bullets",
        examples=({"op": "add_bullet_points", "slide_index": 0,
                   "items": ["Revenue up 12%", {"text": "EMEA led", "level": 1}]},)),
    OpSpec("format_text", _op_format_text, (slide_key(), KeySpec("find", ("str",), default=""), *SHAPE_REF, *FONT),
           G_TEXT, note="formats every run holding the text, on the slide or in the one shape a shape key names",
           examples=({"op": "format_text", "slide_index": 0, "find": "Quarterly", "color": "#C00000"},
                     {"op": "format_text", "slide_index": 0, "shape_id": 2, "bold": True})),
    *pptx_fill.OPS,
    # SHAPES
    *pptx_shapes.SHAPE_OPS,
    *pptx_arrange.OPS,
    # TABLES
    OpSpec("add_table", _op_add_table, (
        slide_key(), KeySpec("headers", ("list",), required=True), KeySpec("rows", ("list",), default=[],
                                                                        help="lists of cell values"),
        *box_keys(1, 3, 8, 2),
    ), G_TABLES, examples=({"op": "add_table", "slide_index": 0, "headers": ["Region", "Q1"],
                            "rows": [["EMEA", 12], ["APAC", 9]]},)),
    OpSpec("format_table_cell", _op_format_table_cell, (
        slide_key(), KeySpec("table_index", ("int",), default=0, help="0-based, -1 = last"),
        KeySpec("row", ("int",), default=0, help="0-based, -1 = last"),
        KeySpec("col", ("int",), default=0, help="0-based, -1 = last"),
        KeySpec("end_row", ("int",), help="a range"), KeySpec("end_col", ("int",), help="a range"),
        FILL, KeySpec("alignment", ("str",)), KeySpec("vertical_alignment", ("str",), help="top | middle | bottom"),
        *FONT,
        KeySpec("merge_to", ("dict",), fields=(KeySpec("row", ("int",)), KeySpec("col", ("int",)))),
    ), G_TABLES, examples=(
        {"op": "format_table_cell", "slide_index": 0, "row": 0, "col": 0, "end_col": 1, "fill_color": "#1F4E79",
         "color": "white", "bold": True, "alignment": "center", "vertical_alignment": "middle"},
        {"op": "format_table_cell", "slide_index": 0, "row": 1, "col": 0, "merge_to": {"row": 1, "col": 1}})),
    # CHARTS
    *pptx_charts.OPS,
    # IMAGES / EQUATIONS
    *pptx_shapes.IMAGE_OPS,
    OpSpec("add_equation", _op_add_equation, (
        slide_key(), KeySpec("latex", ("str",), required=True, aliases=("equation",)),
        KeySpec("left", ("length",), unit=IN, default=1), KeySpec("top", ("length",), unit=IN, default=2),
        KeySpec("height", ("length",), unit=IN, default=0.8), KeySpec("width", ("length",), unit=IN),
    ), G_IMAGES, note="a high-resolution picture; amsmath subset",
        examples=({"op": "add_equation", "slide_index": 0, "latex": "E = mc^2", "height": 0.6},)),
    # VISUAL
    *pptx_theme.OPS,
    *pptx_transitions.OPS,
    *pptx_animations.OPS,
    # NOTES / LINKS / NUMBERS
    OpSpec("set_speaker_notes", _op_set_speaker_notes, (slide_key(), TEXT, KeySpec("append", ("bool",))),
           G_NOTES, examples=({"op": "set_speaker_notes", "slide_index": 0, "text": "Lead with the number."},)),
    OpSpec("add_hyperlink", _op_add_hyperlink, (
        slide_key(), *SHAPE_REF, KeySpec("url", ("str",), required=True),
        KeySpec("text", ("str",), help="a linked run added to the shape's text; without it the whole shape links"),
        KeySpec("color", ("color",), default="0563C1"),
    ), G_NOTES, one_of=SHAPE_ONE_OF, examples=(
        {"op": "add_hyperlink", "slide_index": 0, "shape_name": "Card", "url": "https://example.com",
         "text": " (source)", "color": "accent1"},
        {"op": "add_hyperlink", "slide_index": 0, "shape_index": 0, "url": "https://example.com"},
        {"op": "add_hyperlink", "slide_index": 0, "shape_id": 3, "url": "https://example.com"})),
    OpSpec("set_slide_number", _op_set_slide_number, (
        KeySpec("enabled", ("bool",), default=True, help="false removes them"),
        slide_key(required=False), KeySpec("skip_title_slide", ("bool",), default=False),
    ), G_NOTES, note="a live slide number on every slide (or one); slides added later follow the deck",
        examples=({"op": "set_slide_number", "skip_title_slide": True},
                  {"op": "set_slide_number", "slide_index": 0, "enabled": False})),
    # XML
    *pptx_patch.OPS,
], key_aliases={"slide": "slide_index", "fill": "fill_color"}, notes=(
    "UNITS: positions and sizes are inches (a new presentation is 16:9, 13.333 x 7.5); a length also takes "
    "'2.5cm' or '40pt'; font sizes and line widths are points. COLOURS: hex, CSS names, rgb() or theme slots "
    "(accent1-6, dk1, lt1, dk2, lt2) with an optional tint or shade. KINDS: a path ending .potx or .potm saves "
    "a template, .ppsx or .ppsm a show, .pptm keeps macros."))


# ---------------------------------------------------------------------------
# The core and the handler
# ---------------------------------------------------------------------------


def _fit_shrinking_text(job: PptxJob) -> list[str]:
    """Each touched shape that shrinks its text on overflow gets the scale
    its text needs, so PowerPoint shows it fitted before anyone edits it."""
    from formats.pptx import pptx_geometry, pptx_measure

    warnings = []
    for position, slide in enumerate(job.prs.slides):
        ids = job.touched.get(slide.part)
        if not ids:
            continue
        elements = {s.shape_id: s for s in walk_shapes(slide.shapes) if s.shape_id in ids}
        for shape in pptx_geometry.slide_geometry(job.prs, slide, position).walk():
            if shape.shape_id not in elements or not pptx_measure.stores_shrink(shape):
                continue
            if not pptx_measure.has_text(shape):
                continue
            scale, reduction, fits = pptx_measure.fit_scale(shape)
            pptx_text.write_shrink(elements[shape.shape_id].text_frame, scale, reduction)
            where = f"slide_index {position}, shape_id {shape.shape_id} {name_of(shape.name)}"
            if not fits:
                warnings.append(f"{where}: the text does not fit even shrunk to {scale:.0%}: shorten it, "
                                "enlarge the box or split the slide")
            elif scale < pptx_measure.SHRINK_WARN_BELOW:
                warnings.append(f"{where}: the text shrinks to {scale:.0%} to fit: shorten it or enlarge the box")
    return warnings


def _finalize(job: PptxJob, outcome: Any) -> list[str]:
    if job.renumber:
        refresh_numbers(job.prs)
    return [*_fit_shrinking_text(job), *prepare_kind(job.prs, job.path)]


def _write_pptx_core(call: WriteCall) -> WriteReport:
    """Worker core: the deck loaded (or a new 16:9 one), the batch run, the
    one save committed."""
    baseline = collect_baseline(FMT, path=call.path, create_new=call.create_new, check_modules=call.check_modules,
                                ops=[op.get("op") for _, op in call.ops if isinstance(op, dict)])
    prs = open_presentation(call.path) if call.edit_existing else new_presentation()
    job = PptxJob(prs, call.path)
    outcome = call.run(CATALOG, job, finalize=_finalize)
    return call.commit(outcome, kind=KIND, fmt=FMT, save=prs.save, baseline=baseline)


def _blank_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    new_presentation().save(buf)
    return buf.getvalue()


def _register_blank() -> None:
    from importlib.metadata import version

    register_blank(FMT, _blank_bytes, version=f"python-pptx {version('python-pptx')} 16:9")


_register_blank()


async def handle_write_pptx(args: dict) -> str:
    return await handle_write_call(args, CATALOG, _write_pptx_core, kind=KIND, advice=_WRITE_OP_ADVICE)


# ---------------------------------------------------------------------------
# The key-coverage contract
# ---------------------------------------------------------------------------


def contract_job(tmp_dir: str) -> PptxJob:
    """A deck every example has a target in: slide 0 with a title, a body,
    a shape named Card, a table and a chart; slide 1 blank."""
    from pptx.util import Inches

    prs = new_presentation()
    slide = prs.slides.add_slide(resolve_layout(prs, "title_and_content")[0])
    slide.shapes.title.text = "Quarterly review"
    slide.placeholders[1].text_frame.text = "Revenue up"
    slide.shapes.add_shape(1, Inches(1), Inches(5), Inches(2), Inches(1)).name = "Card"
    slide.shapes.add_table(2, 2, Inches(4), Inches(5), Inches(4), Inches(1))
    prs.slides.add_slide(resolve_layout(prs, "blank")[0])
    return PptxJob(prs, os.path.join(tmp_dir, "contract.pptx"))


def contract_fixtures(tmp_dir: str) -> dict[str, str]:
    from PIL import Image

    image = os.path.join(tmp_dir, EXAMPLE_PNG)
    Image.new("RGB", (40, 20), (31, 78, 121)).save(image)
    return {EXAMPLE_PNG: image}
