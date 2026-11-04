"""edit_pdf: its operations, their catalogue and the write that commits them.

The batch runs on one PyMuPDF document and is atomic: one failed operation
saves nothing, unless the caller passes ``best_effort``. Files an operation
writes beside the PDF (``split``, ``extract_images``, an OCR copy) are
staged and land only when the batch commits. The document is saved exactly
once: a second save of a document opened from an encrypted file writes a
broken file, and that one save keeps the file's own encryption unless an
operation sets or removes it.

A batch with an ``ocr`` operation runs in a thread of the server process,
not in a worker: OCR holds the CPU for minutes, and a worker would hold the
install's one parse permit (a 2 GB install has one) for as long, starving
every read.
"""

from __future__ import annotations

import os
import posixpath
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from batch import WriteCall, batch_ops, handle_write_call, read_call
from catalog import Catalog, KeySpec, OpSpec
from formats.pdf.pages import PageSpecError, page_list, whole
from results import Code, NoOp, OpCtx, OpError, Status, WriteReport

KIND = "PDF"
FMT = "pdf"
ADVICE = "Split the operation list into smaller edit_pdf calls or work on a smaller file"

# A4 in points, insert_page's default.
A4_W, A4_H = 595.0, 842.0
# OCR in the server process: the files it takes and the raster of one page.
OCR_MAX_BYTES = 200 * 1024 * 1024
OCR_MAX_PIXELS = 25_000_000
# Stop OCR this long before the call's end, so the save still fits.
OCR_SAVE_MARGIN_S = 45.0
_ANNOTATION_TYPES = ("highlight", "underline", "strikeout", "text_note", "rectangle")
# The PDF Info dictionary's text fields set_metadata writes, and the dates it keeps.
INFO_KEYS = ("title", "author", "subject", "keywords", "creator", "producer")
_INFO_DATES = ("creationDate", "modDate")


class Protection(Enum):
    """What the one save does with encryption."""

    KEEP = "keep"        # the file's own, unchanged (none when it had none)
    SET = "set"          # new settings from an encrypt operation
    REMOVE = "remove"    # none, a decrypt operation asked for it


@dataclass
class PdfJob:
    """What one edit_pdf call's operations share."""

    doc: Any
    path: str
    protected: str = ""                       # the file's encryption, "" when it has none
    protection: Protection = Protection.KEEP
    encryption: dict = field(default_factory=dict)
    compress: dict | None = None
    side_only: set[int] = field(default_factory=set)
    deadline: float | None = None             # time.monotonic() by which OCR stops
    ocr_page: Any = None                      # (page, language, dpi) -> one-page PDF bytes
    owner: bool = False                       # the owner password was given
    restricted: tuple[str, ...] = ()          # what the file's owner does not allow, in words
    permissions: int | None = None            # the owner's permission bits, None with the owner password
    password: str | None = None               # the password the file was opened with


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count(doc: Any) -> str:
    n = doc.page_count
    return f"the PDF has {n} page{'s' if n != 1 else ''}: page 0 to {n - 1}, or -1 for the last"


def _page(job: PdfJob, ctx: OpCtx, op: dict, key: str = "page") -> Any:
    """The page an operation names by its 0-based index (negative from the
    end, as the document's own indexing has always taken it)."""
    index = whole(ctx, op.get(key, 0), key)
    total = job.doc.page_count
    if not -total <= index < total:
        raise OpError(Code.NOT_FOUND, f"page {index} does not exist ({_count(job.doc)})",
                      location=f"page {index}", hint="Read the PDF for its page count.")
    return job.doc[index]


def _pages(job: PdfJob, ctx: OpCtx, op: dict, key: str = "pages", default: Any = "all") -> list[int]:
    """The pages an operation names; a spec that names none fails, one that
    names some is applied to those with a warning about the rest."""
    spec = op.get(key, default)
    try:
        pages, missing = page_list(spec, job.doc.page_count)
    except PageSpecError as exc:
        raise OpError(Code.BAD_VALUE, str(exc), location=key,
                      hint="Pages are '3', '1-3', '2-', '-1' (1-based text) or 0-based numbers.") from None
    if not pages:
        raise OpError(Code.NOT_FOUND, f"pages {spec!r} name no page ({_count(job.doc)})", location=key,
                      hint="Text page numbers count from 1, numbers in a list from 0.")
    if missing:
        ctx.warn(f"pages {', '.join(missing)} do not exist and were skipped ({_count(job.doc)})")
    return pages


def _rect(value: Any, key: str = "rect") -> Any:
    import fitz

    if not isinstance(value, list) or len(value) != 4 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        raise OpError(Code.BAD_VALUE, f"{key} must be four numbers [x0, y0, x1, y1] in points", location=key,
                      hint="Points from the page's top left, e.g. [72, 72, 300, 200].")
    rect = fitz.Rect(*value)
    if rect.is_empty or rect.is_infinite:
        raise OpError(Code.BAD_VALUE, f"{key} {value} encloses no area", location=key,
                      hint="x1 must be right of x0 and y1 below y0.")
    return rect


def pdf_color(value: Any, key: str) -> tuple[float, float, float]:
    from colors import parse_color

    return tuple(parse_color(value, allow_theme=False).fractions[:3])


def _position(op: dict) -> tuple[float, float] | None:
    """``position`` as [x, y] or {x, y}, None when it is not given."""
    position = op.get("position")
    if position is None:
        return None
    if isinstance(position, dict) and {"x", "y"} <= set(position):
        return float(position["x"]), float(position["y"])
    if isinstance(position, list) and len(position) == 2:
        return float(position[0]), float(position[1])
    raise OpError(Code.BAD_VALUE, "position must be [x, y] or {\"x\": ..., \"y\": ...} in points",
                  location="position", hint="Or give x and y.")


def _warn_unprotected(job: PdfJob, ctx: OpCtx, what: str) -> None:
    if job.protected:
        ctx.warn(f"{what} has no password: only the PDF itself keeps its protection")


def protected_copy(job: PdfJob, pages: list[int] | None = None) -> tuple[Any, dict]:
    """A copy of the document as it stands (``pages`` of it, every page when
    None) and the save arguments that give it the protection the PDF has at
    this point of the batch: the file's own (method, permissions and both
    passwords, kept from its encryption dictionary), the new one an encrypt
    set, or none after a decrypt. Lifting an owner's restrictions needs the
    owner password, so no copy is ever the open door around it."""
    import fitz

    if job.protection is Protection.KEEP and job.protected:
        # A copy with the encryption kept: tobytes() with the default would
        # drop it, and break the one later save of the document.
        copy = fitz.open("pdf", job.doc.tobytes(encryption=fitz.PDF_ENCRYPT_KEEP))
        if copy.needs_pass:
            copy.authenticate(job.password or "")
        if pages is not None:
            copy.select(pages)
        return copy, {"encryption": fitz.PDF_ENCRYPT_KEEP}
    copy = fitz.open()
    for index in (pages if pages is not None else range(job.doc.page_count)):
        copy.insert_pdf(job.doc, from_page=index, to_page=index)
    return copy, dict(job.encryption) if job.protection is Protection.SET else {}


def copy_refusal(job_protected: str, owner: bool, permissions: int | None) -> str:
    """Why content may not be copied out of a protected PDF into files that
    cannot carry its protection (images), "" when it may: its owner allows
    copying, or the owner password was given."""
    import fitz

    if not job_protected or owner or permissions is None or permissions & fitz.PDF_PERM_COPY:
        return ""
    return ("the file's owner does not allow copying its content out, and image files cannot keep its "
            "protection")


def embedded_font(job: PdfJob, page: Any, text: str, family: str, *, bold: bool = False,
                  italic: bool = False, ctx: OpCtx | None = None) -> Any:
    """``text``'s face, subset and embedded through ``page``; the face's
    warnings go to ``ctx``."""
    from formats.pdf import text as pdf_text

    chosen = pdf_text.pick_face(text, family, bold=bold, italic=italic)
    if ctx is not None:
        for warning in chosen.warnings:
            ctx.warn(warning)
    return pdf_text.embed(page, chosen.face, text)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _op_merge(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    files = op.get("files", [])
    if not files:
        raise NoOp("no files were given to merge")
    position = op.get("position", "end")
    if isinstance(position, str) and position not in ("start", "end"):
        raise OpError(Code.BAD_VALUE, f"position '{position}' is not start, end or a page index",
                      location="position", hint="Give start, end, or the 0-based page to insert before.")
    if isinstance(position, int) and not 0 <= position <= job.doc.page_count:
        raise OpError(Code.NOT_FOUND, f"position {position} is past the end ({_count(job.doc)})",
                      location="position", hint="Give a page index from 0 to the page count.")
    sources = []
    try:
        for name in files:
            if not os.path.isfile(name):
                raise OpError(Code.NOT_FOUND, f"the file {Path(name).name} does not exist", location="files",
                              hint="Check the path, or write the PDF first.")
            src = fitz.open(name)
            sources.append(src)
            if protection_of(src):
                raise OpError(Code.UNSUPPORTED, f"{Path(name).name} is protected, and its pages would lose that "
                              "protection here", location="files",
                              hint="Remove its protection first: edit_pdf on that file with a decrypt op and its "
                                   "owner password, then merge it.")
        start = 0 if position == "start" else position if isinstance(position, int) else None
        for src in sources:
            if start is None:
                job.doc.insert_pdf(src)
            else:
                job.doc.insert_pdf(src, start_at=start)
                start += src.page_count
    finally:
        for src in sources:
            src.close()


def _op_split(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    pages = _pages(job, ctx, op)
    dest = op["output_path"]
    temp = ctx.side_temp(dest, ".pdf")
    out, protection = protected_copy(job, pages)
    try:
        out.save(temp, **protection)
    finally:
        out.close()
    ctx.defer_write(dest, temp)
    ctx.note(f"{len(pages)} page(s) go to {Path(dest).name}"
             + (", which keeps the PDF's protection" if protection else ""))


def _op_rotate_page(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    degrees = whole(ctx, op.get("degrees", 90), "degrees")
    if degrees % 90:
        raise OpError(Code.BAD_VALUE, f"degrees {degrees} is not a quarter turn", location="degrees",
                      hint="Give 90, 180, 270 (or 0).")
    for index in _pages(job, ctx, op):
        page = job.doc[index]
        page.set_rotation((page.rotation + degrees) % 360 if op.get("relative") else degrees % 360)


def _op_delete_page(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    if op.get("pages") is None:
        raise NoOp("no pages were named")
    pages = sorted(set(_pages(job, ctx, op)), reverse=True)
    if len(pages) >= job.doc.page_count:
        raise OpError(Code.BAD_VALUE, "this would delete every page, and a PDF needs at least one",
                      location="pages", hint="Keep at least one page.")
    for index in pages:
        job.doc.delete_page(index)


def _op_reorder_pages(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    order = [whole(ctx, i, "order") if isinstance(i, float) else i for i in op.get("order", [])]
    if not order:
        raise NoOp("no order was given")
    total = job.doc.page_count
    bad = [str(i) for i in order if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < total]
    if bad:
        raise OpError(Code.NOT_FOUND, f"order names pages that do not exist: {', '.join(bad)} ({_count(job.doc)})",
                      location="order", hint="List 0-based page indexes, each page once.")
    job.doc.select(order)


def _op_insert_page(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    position = whole(ctx, op.get("position", -1), "position")
    total = job.doc.page_count
    if not -1 <= position <= total:
        raise OpError(Code.NOT_FOUND, f"position {position} is past the end ({_count(job.doc)})",
                      location="position", hint="Give -1 for the end, or a 0-based page index.")
    job.doc.insert_page(position, width=op.get("width", A4_W), height=op.get("height", A4_H))


AUTO_RECT = "auto"


def _op_crop_page(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    rect = op.get("rect")
    if rect is None:
        raise NoOp("no rect was given")
    auto = isinstance(rect, str)
    if auto and rect != AUTO_RECT:
        raise OpError(Code.BAD_VALUE, f"rect '{rect}' is not 'auto' or four numbers", location="rect",
                      hint="Give [x0, y0, x1, y1] in points, or 'auto'.")
    box = None if auto else _rect(rect)
    cropped = 0
    for index in _pages(job, ctx, op):
        page = job.doc[index]
        if auto:
            blocks = page.get_text("blocks")
            if not blocks:
                continue
            target = fitz.Rect(min(b[0] for b in blocks) - 10, min(b[1] for b in blocks) - 10,
                               max(b[2] for b in blocks) + 10, max(b[3] for b in blocks) + 10) & page.mediabox
        else:
            target = box
        try:
            page.set_cropbox(target)
        except ValueError as exc:
            raise OpError(Code.BAD_VALUE, f"page {index}: {exc}", location=f"page {index}",
                          hint="The rect must lie inside the page.") from None
        cropped += 1
    if not cropped:
        raise NoOp("no page has text to crop around")


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def _op_add_text(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    from formats.pdf import text as pdf_text

    page = _page(job, ctx, op)
    text = op.get("text", "")
    if not text:
        raise NoOp("the text is empty")
    point = _position(op) or (float(op.get("x", 72)), float(op.get("y", 72)))
    size = op.get("font_size", 12)
    color = pdf_color(op.get("color", [0, 0, 0]), "color")
    font = op.get("font", "helv")
    if pdf_text.is_builtin(font):
        page.insert_text(fitz.Point(*point), text, fontsize=size, fontname=font, color=color)
        return
    family, bold, italic = pdf_text.style_of(font, bold=op.get("bold", False), italic=op.get("italic", False))
    embedded = embedded_font(job, page, text, family, bold=bold, italic=italic, ctx=ctx)
    pdf_text.draw(page, point, text, size=size, color=color, embedded=embedded)


def _op_add_image(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    page = _page(job, ctx, op)
    image = op["image_path"]
    if not os.path.isfile(image):
        raise OpError(Code.NOT_FOUND, f"the image {Path(image).name} does not exist", location="image_path",
                      hint="Check the path.")
    if op.get("rect") is not None:
        rect = _rect(op["rect"])
    elif op.get("position") is not None:
        x, y = _position(op)
        rect = fitz.Rect(x, y, x + 150, y + 150)
    else:
        rect = fitz.Rect(50, 50, 200, 200)
    try:
        page.insert_image(rect, filename=str(image))
    except (RuntimeError, ValueError) as exc:
        raise OpError(Code.BAD_VALUE, f"the image could not be placed: {exc}", location="image_path",
                      hint="Use a PNG or JPEG file.") from None


def _op_add_watermark(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    from formats.pdf import text as pdf_text

    text = op.get("text", "DRAFT")
    if not text:
        raise NoOp("the text is empty")
    pages = _pages(job, ctx, op)
    size = op.get("font_size", 60)
    color = pdf_color(op.get("color", [0.8, 0.8, 0.8]), "color")
    rotation = op.get("rotation", 45)
    opacity = op.get("opacity", 0.3)
    if not 0 <= opacity <= 1:
        raise OpError(Code.BAD_VALUE, f"opacity {opacity} is outside 0 to 1", location="opacity",
                      hint="0 is invisible, 1 solid; 0.3 is the default.")
    family, bold, italic = pdf_text.style_of(op.get("font"), bold=op.get("bold", False))
    for index in pages:
        page = job.doc[index]
        embedded = embedded_font(job, page, text, family, bold=bold, italic=italic,
                                 ctx=ctx if index == pages[0] else None)
        width = embedded.width(text, size)
        center = fitz.Point(page.rect.width / 2, page.rect.height / 2)
        # Straight text centred on the page, then turned as one around the
        # centre (insert_text's own rotate takes only quarter turns).
        matrix = fitz.Matrix(1, 1)
        matrix.prerotate(rotation)
        pdf_text.draw(page, (center.x - width / 2, center.y + size * 0.35), text, size=size, color=color,
                      embedded=embedded, morph=(center, matrix), opacity=opacity)


def _text_style_at(page: Any, rect: Any) -> tuple[float, tuple[float, float, float], str, bool, bool, float]:
    """The dominant text style under ``rect``: (size, colour, family, bold,
    italic, baseline y). A document's own fonts are usually subsets that
    cannot draw new text, so a replacement is drawn in the Liberation face
    of the same kind; with no span under the rect the metrics come from the
    rect (text inside a form XObject)."""
    import fitz

    from formats.pdf import text as pdf_text

    best, best_overlap = None, 0.0
    for block in page.get_text("dict", clip=rect).get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                box = fitz.Rect(span["bbox"])
                if not box.intersects(rect):
                    continue
                overlap = abs((box & rect).get_area())
                if overlap > best_overlap:
                    best, best_overlap = span, overlap
    if best is None:
        return max(6.0, rect.height * 0.8), (0, 0, 0), pdf_text.DEFAULT_FAMILY, False, False, \
            rect.y1 - rect.height * 0.22
    flags = best.get("flags", 0)
    italic, serif, mono, bold = bool(flags & 2), bool(flags & 4), bool(flags & 8), bool(flags & 16)
    family = pdf_text.MONO_FAMILY if mono else pdf_text.SERIF_FAMILY if serif else pdf_text.DEFAULT_FAMILY
    c = best.get("color", 0)
    color = ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)
    origin = best.get("origin")
    baseline = float(origin[1]) if origin else rect.y1 - rect.height * 0.22
    return float(best.get("size", rect.height * 0.8)), color, family, bold, italic, baseline


def _op_replace_text(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    from formats.pdf import text as pdf_text

    find = op["find"]
    replace = op.get("replace", "")
    case_sensitive = op.get("case_sensitive", False)
    total, pages_hit = 0, 0
    for index in _pages(job, ctx, op):
        page = job.doc[index]
        hits = page.search_for(find)
        if case_sensitive:
            # search_for matches case-insensitively; keep the hits whose
            # text holds the exact form.
            hits = [r for r in hits if find in page.get_text("text", clip=fitz.Rect(r) + (-1, -1, 1, 1))]
        if not hits:
            continue
        # Style and free width are read before the redaction removes the
        # spans they come from.
        words = page.get_text("words")
        plans = []
        for r in hits:
            size, color, family, bold, italic, baseline = _text_style_at(page, r)
            # The replacement may run past the original up to the next word
            # on the line (less a space), or to the right margin.
            next_x0 = min((w[0] for w in words if w[1] < r.y1 and w[3] > r.y0 and w[0] >= r.x1 - 1),
                          default=page.rect.x1 - 36)
            plans.append((fitz.Rect(r), size, color, family, bold, italic, baseline,
                          max(r.width, next_x0 - r.x0 - 0.25 * size)))
            page.add_redact_annot(r)
        # IMAGE_NONE: figures the hit overlaps stay whole.
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        if replace:
            for r, size, color, family, bold, italic, baseline, room in plans:
                embedded = embedded_font(job, page, replace, family, bold=bold, italic=italic, ctx=ctx)
                fs = size
                while fs > 5 and embedded.width(replace, fs) > room:
                    fs -= 0.5
                pdf_text.draw(page, (r.x0, baseline), replace, size=fs, color=color, embedded=embedded)
        total += len(hits)
        pages_hit += 1
    if not total:
        raise NoOp(f"'{find}' was not found on the pages searched")
    what = "deleted" if not replace else f"replaced with '{replace}'"
    ctx.note(f"{total} occurrence(s) of '{find}' {what} on {pages_hit} page(s)")


def _op_redact(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    find = op.get("find")
    rect = _rect(op["rect"]) if op.get("rect") is not None else None
    fill = pdf_color(op.get("fill", [0, 0, 0]), "fill")
    count = 0
    for index in _pages(job, ctx, op):
        page = job.doc[index]
        targets = page.search_for(find) if find else [rect]
        if not targets:
            continue
        for r in targets:
            page.add_redact_annot(r, fill=fill)
        # Privacy-grade: image pixels under the region are blanked too.
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        count += len(targets)
    if not count:
        raise NoOp(f"'{find}' was not found on the pages searched" if find else "nothing was under the rect")
    ctx.note(f"{count} region(s) permanently removed")


def _op_add_annotation(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    page = _page(job, ctx, op)
    kind = op.get("annotation_type", "highlight")
    if kind not in _ANNOTATION_TYPES:
        raise OpError(Code.BAD_VALUE, f"annotation_type '{kind}' is not one of {', '.join(_ANNOTATION_TYPES)}",
                      location="annotation_type", hint="Pick one of the listed types.")
    r = _rect(op.get("rect", [100, 700, 400, 720]))
    color = pdf_color(op.get("color", [1, 1, 0]), "color")
    content = op.get("content", "")
    if kind == "text_note":
        page.add_text_annot(fitz.Point(r.x0, r.y0), content)
        return
    annot = {"highlight": page.add_highlight_annot, "underline": page.add_underline_annot,
             "strikeout": page.add_strikeout_annot, "rectangle": page.add_rect_annot}[kind](r)
    annot.set_colors(stroke=color)
    if content:
        annot.set_info(content=content)
    annot.update()


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


def _op_fill_form(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    from formats.pdf import forms

    filled = forms.fill(job.doc, op["values"], ctx)
    flattened = forms.flatten(job.doc, ctx) if op.get("flatten") else 0
    ctx.note(f"{filled} field(s) filled" + (f", {flattened} field(s) flattened" if flattened else ""))


def _op_flatten_form(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    from formats.pdf import forms

    flattened = forms.flatten(job.doc, ctx)
    if not flattened:
        raise NoOp("the PDF has no form fields")
    ctx.note(f"{flattened} field(s) flattened into the page")


def _op_fill_flat_form(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    from formats.pdf import flat

    placed = flat.fill(job, ctx, op)
    ctx.note(f"{placed} text(s) placed")


# ---------------------------------------------------------------------------
# Security and optimisation
# ---------------------------------------------------------------------------


def _owner_only(job: PdfJob, what: str) -> None:
    """Refuse ``what`` on a protected file unless its owner password was
    given: the user password opens a file, the owner password governs its
    protection."""
    if job.protected and not job.owner:
        raise OpError(Code.BAD_VALUE, f"{what} this PDF's protection needs its owner password, and none of the "
                      "passwords given is it", location="password",
                      hint="Give the owner password as decrypt's password or as edit_pdf's password argument.")


def _lifted(job: PdfJob, ctx: OpCtx) -> None:
    if job.restricted:
        ctx.note(f"the owner's restrictions are lifted ({', '.join(job.restricted)})")


def _op_encrypt(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    if job.protection is not Protection.REMOVE:
        _owner_only(job, "Replacing")
    user = str(op.get("user_password", "") or "")
    owner = str(op.get("owner_password", "") or user)
    if not user and not owner:
        raise OpError(Code.MISSING_KEY, "encrypt needs a user_password or an owner_password",
                      location="user_password", hint="Give the password that opens the file (user_password).")
    raw = op.get("permissions", {})
    perms = {p: True for p in raw} if isinstance(raw, list) else dict(raw)
    flags = fitz.PDF_PERM_ACCESSIBILITY
    if perms.get("print", True):
        flags |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
    if perms.get("copy", True):
        flags |= fitz.PDF_PERM_COPY
    if perms.get("annotate", True):
        flags |= fitz.PDF_PERM_ANNOTATE
    if perms.get("modify", False):
        flags |= fitz.PDF_PERM_MODIFY
    if job.protection is Protection.KEEP:
        _lifted(job, ctx)
    job.protection = Protection.SET
    job.encryption = {"user_pw": user, "owner_pw": owner, "permissions": flags,
                      "encryption": fitz.PDF_ENCRYPT_AES_256}


def _op_decrypt(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    if not job.protected and job.protection is not Protection.SET:
        raise NoOp("the PDF has no password to remove")
    if job.protection is Protection.KEEP:
        _owner_only(job, "Removing")
        _lifted(job, ctx)
    job.protection = Protection.REMOVE
    job.encryption = {}


def _op_compress(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    job.compress = {"garbage": whole(ctx, op.get("garbage", 4), "garbage"), "deflate": op.get("deflate", True),
                    "deflate_images": op.get("deflate_images", True), "deflate_fonts": op.get("deflate_fonts", True)}


def _op_set_metadata(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    meta = dict(job.doc.metadata or {})
    given = {"title": op.get("title"), "author": op.get("author"), "subject": op.get("subject"),
             "keywords": op.get("keywords"), "creator": op.get("creator"), "producer": op.get("producer")}
    changed = {key: value for key, value in given.items() if value}
    if not changed:
        raise NoOp("no metadata value was given")
    meta.update(changed)
    # PyMuPDF replaces the whole Info dictionary: the fields not given are
    # written back as they were.
    job.doc.set_metadata({key: meta[key] for key in (*INFO_KEYS, *_INFO_DATES) if meta.get(key)})


# ---------------------------------------------------------------------------
# Extraction and OCR
# ---------------------------------------------------------------------------


def _op_extract_images(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    folder = op.get("output_dir")
    if folder is None:
        raise OpError(Code.MISSING_KEY, "extract_images needs output_dir", location="output_dir",
                      hint="Name the folder the images go to.")
    refusal = copy_refusal(job.protected, job.owner, job.permissions)
    if refusal:
        raise OpError(Code.BAD_VALUE, refusal, location="password",
                      hint="Give the owner password as edit_pdf's password argument.")
    count = 0
    for index in _pages(job, ctx, op):
        for number, image in enumerate(job.doc[index].get_images(full=True), start=1):
            try:
                found = job.doc.extract_image(image[0])
            except (RuntimeError, ValueError):
                ctx.warn(f"an image on page {index} could not be read and was skipped")
                continue
            if not found or not found.get("image"):
                continue
            dest = ctx.side_dest(folder, f"page{index + 1}_img{number}.{found['ext']}")
            temp = ctx.side_temp(dest)
            with open(temp, "wb") as fh:
                fh.write(found["image"])
            ctx.defer_write(dest, temp)
            count += 1
    if not count:
        raise NoOp("the pages hold no images")
    _warn_unprotected(job, ctx, "an extracted image")
    ctx.note(f"extracted {count} image(s) to {Path(folder).name}")


def ocr_raster_dpi(page: Any, requested: Any) -> int:
    """The raster resolution for the OCR of ``page``: never past the scan's
    own resolution (a 300 dpi raster of a 150 dpi scan is several times the
    bytes for no gain: an 84-page scan once grew to 536 MB), never past
    OCR_MAX_PIXELS for the page (a large sheet at 300 dpi is over 100 MP in
    the server's memory), 72 to 600."""
    cap = int(requested) if requested else 300
    try:
        images = page.get_images(full=True)
        if images:
            width_px = max(img[2] for img in images)
            cap = min(cap, max(72, round(width_px * 72.0 / max(page.rect.width, 1.0))))
    except Exception:  # noqa: BLE001  # PyMuPDF on a user's PDF raises anything from its parser
        pass
    area_in = max(page.rect.width * page.rect.height / (72.0 * 72.0), 1e-6)
    cap = min(cap, int((OCR_MAX_PIXELS / area_in) ** 0.5))
    return max(72, min(cap, 600))


def tesseract_page(page: Any, language: str, dpi: int) -> bytes:
    """One page as a one-page PDF with Tesseract's invisible text layer. The
    raster stays RGB: a grey pixmap gives an empty text layer on the pinned
    PyMuPDF."""
    return page.get_pixmap(dpi=dpi).pdfocr_tobytes(language=language)


def _installed_languages() -> set[str] | None:
    import fitz

    try:
        folder = fitz.get_tessdata()
    except Exception:  # noqa: BLE001  # no Tesseract data: the OCR call itself reports it
        return None
    if not folder or not os.path.isdir(folder):
        return None
    return {name[:-len(".traineddata")] for name in os.listdir(folder) if name.endswith(".traineddata")}


def _op_ocr(job: PdfJob, ctx: OpCtx, op: dict) -> None:
    import fitz

    language = op.get("language", "eng")
    if job.ocr_page is tesseract_page:
        installed = _installed_languages()
        unknown = [code for code in language.split("+") if installed is not None and code not in installed]
        if unknown:
            raise OpError(Code.BAD_VALUE, f"the OCR language {'+'.join(unknown)} is not installed",
                          location="language",
                          hint=f"Installed: {', '.join(sorted(installed or ()))}; e.g. 'ell+eng' for Greek.")
    pages = _pages(job, ctx, op)
    dpi = whole(ctx, op["dpi"], "dpi") if op.get("dpi") is not None else None
    dest = op.get("output_path")
    target, protection = (protected_copy(job) if dest else (job.doc, {}))
    failed: list[str] = []
    done = 0
    try:
        for count, index in enumerate(pages):
            if job.deadline is not None and time.monotonic() > job.deadline - OCR_SAVE_MARGIN_S:
                rest = ",".join(str(i + 1) for i in pages[count:])
                ctx.warn(f"OCR stopped after {done} page(s) so the call ends in time; run ocr again with "
                         f"pages '{rest}'")
                break
            page = target[index]
            try:
                data = job.ocr_page(page, language, ocr_raster_dpi(page, dpi))
                with fitz.open("pdf", data) as scanned:
                    target.delete_page(index)
                    target.insert_pdf(scanned, from_page=0, to_page=0, start_at=index)
                done += 1
            except Exception as exc:  # noqa: BLE001  # a page's OCR failure is reported for that page; the rest run
                failed.append(f"page {index}: {exc}")
        if failed and not done:
            raise OpError(Code.ERROR, f"OCR failed on every page ({failed[0]})", location="pages",
                          hint="Check the language and that the pages are scans.")
        for line in failed:
            ctx.warn(f"OCR failed on {line}")
        if dest:
            temp = ctx.side_temp(dest, ".pdf")
            target.save(temp, **protection)
            ctx.defer_write(dest, temp)
            job.side_only.add(ctx.index)
    finally:
        if target is not job.doc:
            target.close()
    ctx.note(f"{done} page(s) made searchable" + (f" in {Path(dest).name}" if dest else ""))


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

_PAGES = KeySpec("pages", ("str", "int", "list"), default="all",
                 help="'3', '1-3', '2-', '-1' count from 1; numbers in a list from 0")
_PAGE = KeySpec("page", ("int", "float"), default=0, help="0-based; -1 is the last page")
_COLOR = ("color", "list")
_PASSWORD = ("str", "int", "float")
_POSITION = KeySpec("position", ("list", "dict"), help="[x, y] or {x, y}: the start, as x and y")
_EXAMPLE_PDF = "other.pdf"
_EXAMPLE_PNG = "logo.png"

_OPS = [
    OpSpec("merge", _op_merge, (
        KeySpec("files", ("list",), of=("path",), default=[], help="PDFs appended in order"),
        KeySpec("position", ("str", "int"), default="end", help="start | end | the 0-based page to insert before"),
    ), "PAGES", note="Appends other PDFs; a protected one is refused (its pages would lose the protection).",
        examples=({"op": "merge", "files": [_EXAMPLE_PDF]},)),
    OpSpec("split", _op_split, (
        _PAGES,
        KeySpec("output_path", ("out_path",), required=True, help="the new PDF"),
    ), "PAGES", side_only=True, note="Copies pages into a new PDF, which keeps this PDF's protection; this PDF "
                                     "is unchanged.",
        examples=({"op": "split", "pages": "1", "output_path": "part.pdf"},)),
    OpSpec("rotate_page", _op_rotate_page, (
        KeySpec("pages", ("str", "int", "list"), default="all", aliases=("page",),
                help="'3', '1-3', '2-', '-1' count from 1; numbers in a list from 0"),
        KeySpec("degrees", ("int", "float"), default=90, aliases=("angle",), help="90 | 180 | 270 | 0"),
        KeySpec("relative", ("bool",), default=False, help="add to the page's rotation instead of setting it"),
    ), "PAGES", examples=({"op": "rotate_page", "pages": "1", "degrees": 90},)),
    OpSpec("delete_page", _op_delete_page, (
        KeySpec("pages", ("str", "int", "list"), aliases=("page",),
                help="'3', '1-3', '2-', '-1' count from 1; numbers in a list from 0"),
    ), "PAGES", setup=({"op": "insert_page"},), examples=({"op": "delete_page", "pages": "-1"},)),
    OpSpec("reorder_pages", _op_reorder_pages, (
        KeySpec("order", ("list",), of=("int", "float"), default=[], help="0-based page indexes in the new order"),
    ), "PAGES", examples=({"op": "reorder_pages", "order": [0]},)),
    OpSpec("insert_page", _op_insert_page, (
        KeySpec("position", ("int", "float"), default=-1, help="-1 the end, else the 0-based page to insert before"),
        KeySpec("width", ("float",), default=A4_W, unit="pt"),
        KeySpec("height", ("float",), default=A4_H, unit="pt"),
    ), "PAGES", note="A blank page (A4 by default).", examples=({"op": "insert_page", "position": 0},)),
    OpSpec("crop_page", _op_crop_page, (
        _PAGES,
        KeySpec("rect", ("str", "list"), help="[x0, y0, x1, y1] in points, or 'auto' (around the text)"),
    ), "PAGES", examples=({"op": "crop_page", "pages": "1", "rect": [0, 0, 400, 400]},)),
    OpSpec("add_text", _op_add_text, (
        _PAGE,
        KeySpec("text", ("str",), default="", help="line breaks start new lines"),
        KeySpec("x", ("float",), default=72, unit="pt", help="from the left"),
        KeySpec("y", ("float",), default=72, unit="pt", help="the baseline, from the top"),
        _POSITION,
        KeySpec("font_size", ("float",), default=12, unit="pt"),
        KeySpec("color", _COLOR, default=[0, 0, 0]),
        KeySpec("font", ("str",), default="helv",
                help="a family (Liberation Sans, Noto Serif, Carlito, ...) or a Base-14 name (helv, tiro, cour)"),
        KeySpec("bold", ("bool",), default=False),
        KeySpec("italic", ("bool",), default=False),
    ), "CONTENT", note="Writes text in an embedded font: Greek and accents show in every viewer.",
        examples=({"op": "add_text", "page": 0, "text": "Εγκρίθηκε", "x": 72, "y": 90, "font_size": 14},)),
    OpSpec("add_image", _op_add_image, (
        _PAGE,
        KeySpec("image_path", ("path",), required=True, aliases=("path", "image")),
        KeySpec("rect", ("list",), help="[x0, y0, x1, y1] in points; the image keeps its proportions"),
        _POSITION,
    ), "CONTENT", examples=({"op": "add_image", "page": 0, "image_path": _EXAMPLE_PNG,
                             "rect": [72, 72, 172, 112]},)),
    OpSpec("add_watermark", _op_add_watermark, (
        KeySpec("text", ("str",), default="DRAFT"),
        _PAGES,
        KeySpec("font_size", ("float",), default=60, unit="pt"),
        KeySpec("color", _COLOR, default=[0.8, 0.8, 0.8]),
        KeySpec("rotation", ("float",), default=45, help="degrees, counter-clockwise"),
        KeySpec("opacity", ("float",), default=0.3, help="0 to 1"),
        KeySpec("font", ("str",), help="a family or a Base-14 name; Liberation Sans by default"),
        KeySpec("bold", ("bool",), default=False),
    ), "CONTENT", examples=({"op": "add_watermark", "text": "ΠΡΟΣΧΕΔΙΟ"},)),
    OpSpec("add_annotation", _op_add_annotation, (
        _PAGE,
        KeySpec("annotation_type", ("str",), default="highlight", aliases=("type",),
                help=" | ".join(_ANNOTATION_TYPES)),
        KeySpec("rect", ("list",), default=[100, 700, 400, 720], help="[x0, y0, x1, y1] in points"),
        KeySpec("color", _COLOR, default=[1, 1, 0]),
        KeySpec("content", ("str",), default="", help="the note's text"),
    ), "CONTENT", note="Send it with op (type is an alias of annotation_type).",
        examples=({"op": "add_annotation", "page": 0, "annotation_type": "rectangle", "rect": [72, 72, 200, 100]},)),
    OpSpec("replace_text", _op_replace_text, (
        KeySpec("find", ("str",), required=True),
        KeySpec("replace", ("str",), default="", help="'' deletes"),
        _PAGES,
        KeySpec("case_sensitive", ("bool",), default=False),
    ), "CONTENT", note="Removes each match and writes the replacement in a matching embedded face, "
                       "shrunk to fit.",
        setup=({"op": "add_text", "text": "Draft"},),
        examples=({"op": "replace_text", "find": "Draft", "replace": "Τελικό"},)),
    OpSpec("redact", _op_redact, (
        KeySpec("find", ("str",)),
        KeySpec("rect", ("list",), help="[x0, y0, x1, y1] in points"),
        _PAGES,
        KeySpec("fill", _COLOR, default=[0, 0, 0]),
    ), "CONTENT", one_of=(("find", "rect"),), note="Removes the content for good (text and image pixels).",
        setup=({"op": "add_text", "text": "secret"},),
        examples=({"op": "redact", "find": "secret"}, {"op": "redact", "rect": [72, 60, 200, 80]})),
    OpSpec("fill_form", _op_fill_form, (
        KeySpec("values", ("dict",), required=True, aliases=("fields",),
                help="{field name: value}; read_document mode form_fields lists the names"),
        KeySpec("flatten", ("bool",), default=False, help="draw the values into the page; nothing stays fillable"),
    ), "FORMS", note="Fills fillable fields by name. Text in any script, true or false for check boxes, a "
                     "state or option for radios and lists. Every value is checked first.",
        detail="The values are drawn in an embedded font, so Greek shows in every viewer.",
        examples=({"op": "fill_form", "values": {"name": "Δημήτρης Παπαδόπουλος", "agree": True}},)),
    OpSpec("flatten_form", _op_flatten_form, (), "FORMS",
           note="Draws every field's value into the page; nothing stays fillable.",
           examples=({"op": "flatten_form"},)),
    OpSpec("fill_flat_form", _op_fill_flat_form, (
        KeySpec("entries", ("list",), required=True, item=(
            KeySpec("text", ("str", "int", "float"), required=True),
            KeySpec("label", ("str",), help="text on the page to write beside"),
            KeySpec("page", ("int",), help="0-based; with a label, every page is searched when left out"),
            KeySpec("occurrence", ("int",), default=1, help="which match of the label, from 1"),
            KeySpec("position", ("str",), default="right", help="right | below the label"),
            KeySpec("gap", ("float",), default=6, unit="pt"),
            KeySpec("x", ("float",), unit="pt"),
            KeySpec("y", ("float",), unit="pt", help="the baseline, from the top"),
            KeySpec("font_size", ("float",), unit="pt"),
            KeySpec("color", _COLOR),
            KeySpec("font", ("str",)),
            KeySpec("bold", ("bool",)),
        )),
        KeySpec("font", ("str",), default="Liberation Sans"),
        KeySpec("font_size", ("float",), unit="pt", help="the label's size when left out, else 11"),
        KeySpec("color", _COLOR, default=[0, 0, 0]),
    ), "FORMS", note="Fills a printed form that has no fields: each text beside a label found on the page, "
                     "or at x, y.",
        setup=({"op": "add_text", "text": "Name:", "x": 72, "y": 100},),
        examples=({"op": "fill_flat_form", "entries": [{"label": "Name:", "text": "Μαρία"},
                                                       {"page": 0, "x": 72, "y": 140, "text": "X"}]},)),
    OpSpec("encrypt", _op_encrypt, (
        KeySpec("user_password", _PASSWORD, aliases=("password",), help="opens the file"),
        KeySpec("owner_password", _PASSWORD, help="lifts the restrictions; the user password when left out"),
        KeySpec("permissions", ("dict", "list"), default={},
                help="print, copy, annotate (allowed by default), modify (not): {\"copy\": false} or a list"),
    ), "SECURITY", note="AES-256, applied when the file is saved; replacing a protection needs the owner password.",
        examples=({"op": "encrypt", "user_password": "open-sesame"},)),
    OpSpec("decrypt", _op_decrypt, (
        KeySpec("password", _PASSWORD, default=""),
    ), "SECURITY", note="Removes the protection; needs the owner password (the tool's password argument opens a "
                        "file and keeps its protection).",
        examples=({"op": "decrypt", "password": "open-sesame"},)),
    OpSpec("compress", _op_compress, (
        KeySpec("garbage", ("int", "float"), default=4, help="0 to 4"),
        KeySpec("deflate", ("bool",), default=True),
        KeySpec("deflate_images", ("bool",), default=True),
        KeySpec("deflate_fonts", ("bool",), default=True),
    ), "OPTIMIZE", note="Applied when the file is saved; the result gives the sizes.",
        examples=({"op": "compress"},)),
    OpSpec("set_metadata", _op_set_metadata, tuple(
        KeySpec(key, ("str",)) for key in INFO_KEYS),
        "METADATA", examples=({"op": "set_metadata", "title": "Ετήσια έκθεση"},)),
    OpSpec("extract_images", _op_extract_images, (
        _PAGES,
        KeySpec("output_dir", ("out_path",), help="a folder; <name>_images beside the PDF when left out"),
    ), "EXTRACT", side_only=True, note="Saves the embedded images as files; this PDF is unchanged.",
        examples=({"op": "extract_images", "output_dir": "images"},)),
    OpSpec("ocr", _op_ocr, (
        KeySpec("language", ("str",), default="eng", help="Tesseract codes: eng, ell, 'ell+eng' for Greek"),
        _PAGES,
        KeySpec("dpi", ("int", "float"), help="the raster, capped at the scan's own; 300 at most by default"),
        KeySpec("output_path", ("out_path",), help="a searchable copy; this PDF stays as it is"),
    ), "EXTRACT", note="Makes scanned pages searchable (printed text; read handwriting through "
                       "screenshot_document instead).",
        examples=({"op": "ocr", "language": "ell+eng", "pages": "1"},)),
]

CATALOG = Catalog("edit_pdf", _OPS, color_theme=False, notes=(
    "Pages: '3', '1-3', '2-', '-1' (text) count from 1; page and numbers in a list count from 0. "
    "Coordinates are points from the page's top left (72 points = 1 inch); y of text is its baseline."))


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def protection_of(doc: Any) -> str:
    """The file's encryption as PyMuPDF names it, "" for none. An owner
    password alone leaves ``needs_pass`` and ``is_encrypted`` false; only
    the metadata says the file is protected."""
    meta = doc.metadata or {}
    return str(meta.get("encryption") or "") or ("encrypted" if doc.needs_pass else "")


def _decrypt_passwords(call: WriteCall) -> list[str]:
    return [str(op.get("password")) for _, op in call.ops
            if op.get("op") == "decrypt" and op.get("password") not in (None, "")]


def _refused(call: WriteCall, reason: str) -> WriteReport:
    import shared

    state = "is unchanged" if call.existed else "was not created"
    return WriteReport(f"Nothing saved: {shared._to_agents_relative(call.path)} {state}. {reason}", False)


@dataclass
class Opened:
    doc: Any
    owner: bool = False
    permissions: int | None = None
    password: str | None = None


# Permission bits (ISO 32000-1, table 22) the edit operations need, with the
# words a result uses for them; an operation is allowed when any of its bits is.
PERM_WORDS = {"print": "printing it", "modify": "changing its content", "annotate": "adding comments",
              "form": "filling its form", "assemble": "changing its pages", "copy": "copying its content out"}


def _bits() -> dict[str, int]:
    import fitz

    return {"print": fitz.PDF_PERM_PRINT, "modify": fitz.PDF_PERM_MODIFY, "annotate": fitz.PDF_PERM_ANNOTATE,
            "form": fitz.PDF_PERM_FORM, "assemble": fitz.PDF_PERM_ASSEMBLE, "copy": fitz.PDF_PERM_COPY}


_NEEDS = {
    "add_text": ("modify",), "add_image": ("modify",), "add_watermark": ("modify",), "replace_text": ("modify",),
    "redact": ("modify",), "fill_flat_form": ("modify",), "flatten_form": ("modify",), "set_metadata": ("modify",),
    "crop_page": ("modify",), "ocr": ("modify",), "add_annotation": ("annotate", "modify"),
    "fill_form": ("form", "annotate", "modify"), "merge": ("assemble", "modify"),
    "rotate_page": ("assemble", "modify"), "delete_page": ("assemble", "modify"),
    "reorder_pages": ("assemble", "modify"), "insert_page": ("assemble", "modify"),
}


def restrictions(permissions: int | None) -> tuple[str, ...]:
    """What a file's permission bits forbid, in words (None: no limits)."""
    if permissions is None:
        return ()
    bits = _bits()
    return tuple(PERM_WORDS[name] for name in PERM_WORDS if not permissions & bits[name])


def forbids(op: str, permissions: int | None) -> str:
    """The words for what ``op`` needs and the file's owner does not allow,
    "" when it is allowed (None: no limits)."""
    needs = _NEEDS.get(op)
    if not needs or permissions is None:
        return ""
    bits = _bits()
    return "" if any(permissions & bits[name] for name in needs) else PERM_WORDS[needs[0]]


def _open(call: WriteCall) -> tuple[Opened | None, WriteReport | None]:
    """The PDF opened with the best of the passwords given. Each
    ``authenticate`` replaces the last one's rights (a wrong password after
    the owner's takes them back), so every candidate is tried, then the
    best is authenticated last."""
    import fitz

    doc = fitz.open(call.path)
    if not protection_of(doc):
        return Opened(doc), None
    given = call.extra.get("password")
    candidates = list(dict.fromkeys(([str(given)] if given not in (None, "") else []) + _decrypt_passwords(call)))
    levels = {p: doc.authenticate(p) for p in candidates}
    owner_pw = next((p for p, level in levels.items() if level & 4), None)
    user_pw = next((p for p, level in levels.items() if level & 2), None)
    if doc.needs_pass and owner_pw is None and user_pw is None:
        doc.close()
        reason = ("Its password is not the one given: give the password that opens it as edit_pdf's "
                  "password argument." if candidates else
                  "It is password-protected: give its password as edit_pdf's password argument (it stays "
                  "protected after the edit), or add a decrypt op to remove the password.")
        return None, _refused(call, reason)
    # The owner's restrictions, as a reader without the owner password has them.
    doc.authenticate(user_pw if user_pw is not None else "")
    permissions = doc.permissions
    if owner_pw is not None:
        doc.authenticate(owner_pw)
    return Opened(doc, owner_pw is not None, permissions, owner_pw if owner_pw is not None else user_pw), None


def _save_to(job: PdfJob):
    import fitz

    def save(path: str) -> None:
        kwargs: dict[str, Any] = dict(job.compress or {})
        if job.protection is Protection.SET:
            kwargs.update(job.encryption)
        elif job.protection is Protection.KEEP and job.protected:
            kwargs["encryption"] = fitz.PDF_ENCRYPT_KEEP
        job.doc.save(path, **kwargs)

    return save


def _signed(doc: Any) -> bool:
    try:
        return bool(doc.get_sigflags() > 0 and doc.get_sigflags() & 1)
    except (AttributeError, RuntimeError):
        return False


def edit_pdf_core(call: WriteCall, *, ocr_page: Any = None) -> WriteReport:
    """The write core: open (with the password when the file needs one), run
    the batch, save once, commit."""
    if not os.path.isfile(call.path):
        return _refused(call, "File not found: edit_pdf edits an existing PDF; write_pdf makes a new one.")
    deadline = call.extra.get("deadline")
    if deadline is not None and os.path.getsize(call.path) > OCR_MAX_BYTES:
        return _refused(call, f"It is over {OCR_MAX_BYTES // (1024 * 1024)} MB, and OCR runs in the server "
                              "process: split it (edit_pdf split) and OCR the parts.")
    opened, refusal = _open(call)
    if refusal is not None or opened is None:
        return refusal
    doc = opened.doc
    try:
        protected = protection_of(doc)
        # The owner's restrictions bind an edit made without the owner password.
        limits = opened.permissions if protected and not opened.owner else None
        job = PdfJob(doc, call.path, protected=protected, deadline=deadline, ocr_page=ocr_page or tesseract_page,
                     owner=opened.owner, restricted=restrictions(opened.permissions if protected else None),
                     permissions=limits, password=opened.password)
        outcome = call.run(CATALOG, job)
        for result in outcome.results:
            if result.index in job.side_only:
                result.side_only = True
            refused = forbids(result.op, limits) if result.status is Status.APPLIED else ""
            if refused and job.protection is Protection.KEEP:
                result.warnings.append(f"the file's owner does not allow {refused}; the edit was made, and the "
                                       "file keeps its restrictions (give the owner password to edit it as its "
                                       "owner)")
        size_before = os.path.getsize(call.path)
        staged = call.stage(outcome, save_to=_save_to(job))
        warnings: list[str] = []
        hints = ""
        if staged.staged_path:
            if job.compress is not None:
                after = os.path.getsize(staged.staged_path)
                compress = next(r for r in reversed(outcome.results) if r.op == "compress")
                share = (1 - after / max(size_before, 1)) * 100
                compress.notes.append(f"{size_before / 1024:.0f} KB to {after / 1024:.0f} KB ({share:.0f}% smaller)")
            if job.protected and job.protection is Protection.KEEP:
                hints = "The PDF kept its password protection."
            if _signed(doc):
                warnings.append("the PDF carries digital signatures, and this edit invalidates them")
        return call.commit_staged(staged, kind=KIND, fmt=FMT, warnings=warnings, hints=hints)
    finally:
        doc.close()


def _ocr_in_batch(ops: list[tuple[int, dict]]) -> bool:
    return any(op.get("op") == "ocr" for _, op in ops)


def _named(item: dict) -> str | None:
    for key in CATALOG.dispatch_keys:
        value = item.get(key)
        if isinstance(value, str):
            name = CATALOG.lookup(value)
            if name:
                return name
    return None


def with_default_folders(items: list[Any], path: Any) -> list[Any]:
    """``items`` with extract_images' default folder written in from the
    agent's own ``path`` (``<name>_images`` beside the PDF), so the proxy
    judges it like a folder the agent named. A changed item is a new dict:
    a fix's items share the kept batch's."""
    if not isinstance(path, str):
        return items
    folder = posixpath.join(posixpath.dirname(path), posixpath.splitext(posixpath.basename(path))[0] + "_images")
    return [{**item, "output_dir": folder}
            if isinstance(item, dict) and _named(item) == "extract_images" and item.get("output_dir") is None
            else item for item in items]


async def handle_edit_pdf(args: dict) -> str:
    batch = await read_call(args, kind=KIND)
    batch.items = with_default_folders(batch.items, args.get("path"))
    # OCR starts tesseract, which a worker's memory cap would break: that
    # batch runs in a thread of this process.
    return await handle_write_call(args, CATALOG, edit_pdf_core, kind=KIND, advice=ADVICE, carry=("password",),
                                   in_thread=_ocr_in_batch(batch_ops(batch.items, CATALOG)), batch=batch)


# ---------------------------------------------------------------------------
# The key-coverage test's hooks
# ---------------------------------------------------------------------------


def _stub_ocr(page: Any, language: str, dpi: int) -> bytes:
    import fitz

    out = fitz.open()
    out.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    return out.tobytes()


def contract_job(tmp_dir: str) -> PdfJob:
    """A three-page PDF with text on its first page and a form with a text
    field and a check box; OCR stubbed (no Tesseract in the key-coverage test)."""
    import fitz

    doc = fitz.open()
    for _ in range(3):
        doc.new_page()
    doc[0].insert_text((72, 100), "Name:", fontsize=12)
    widget = fitz.Widget()
    widget.field_name, widget.field_type = "name", fitz.PDF_WIDGET_TYPE_TEXT
    widget.rect = fitz.Rect(120, 88, 300, 104)
    doc[0].add_widget(widget)
    check = fitz.Widget()
    check.field_name, check.field_type = "agree", fitz.PDF_WIDGET_TYPE_CHECKBOX
    check.rect = fitz.Rect(72, 120, 86, 134)
    doc[0].add_widget(check)
    return PdfJob(doc, os.path.join(tmp_dir, "keys.pdf"), ocr_page=_stub_ocr)


def contract_fixtures(tmp_dir: str) -> dict[str, str]:
    import fitz
    from PIL import Image

    image = os.path.join(tmp_dir, _EXAMPLE_PNG)
    Image.new("RGB", (40, 20), (31, 78, 121)).save(image)
    other = os.path.join(tmp_dir, _EXAMPLE_PDF)
    with fitz.open() as doc:
        doc.new_page()
        doc.save(other)
    return {_EXAMPLE_PNG: image, _EXAMPLE_PDF: other}
