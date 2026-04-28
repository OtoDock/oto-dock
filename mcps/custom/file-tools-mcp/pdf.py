"""PDF read/write/edit and document conversion handlers for the file-tools MCP.

Enhanced reader with document properties, structure, and security info.
edit_pdf: 19 operations (merge, split, rotate, watermark, replace_text, redact,
annotate, encrypt, etc.)
pdf_to_images: render pages as PNG.
images_to_pdf: combine images into PDF.
write_pdf: HTML/Markdown → PDF via WeasyPrint.
convert_document: LibreOffice headless format conversion.
"""

import os
import re
from pathlib import Path

from shared import (
    _libreoffice_convert,
    _normalize_operations,
    _op_type,
    _push_image_preview,
    _push_preview,
    _resolve_path,
    _to_agents_relative,
    logger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_pages(pages_str, total: int) -> list[int]:
    """Parse page specification to list of 0-based indices.

    Accepts: "all", single int, "1-5", "1-5,8,10-12", or list of ints.
    Input page numbers are 1-based, output is 0-based.
    """
    if pages_str is None or pages_str == "all":
        return list(range(total))
    if isinstance(pages_str, int):
        return [pages_str] if pages_str < total else []
    if isinstance(pages_str, list):
        return [int(p) for p in pages_str if 0 <= int(p) < total]

    result = []
    for part in str(pages_str).split(","):
        part = part.strip()
        # Negative indexing: -1 = last page, -2 = second to last, etc.
        if part.startswith("-") and part[1:].isdigit():
            idx = total + int(part)
            if 0 <= idx < total:
                result.append(idx)
        elif "-" in part:
            a, b = part.split("-", 1)
            start = max(0, int(a) - 1)
            end = min(total, int(b))
            result.extend(range(start, end))
        elif part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < total:
                result.append(idx)
    return result


def _text_style_at(page, rect):
    """Dominant text style under `rect` → (fontsize, color, base14, baseline_y).

    Original embedded fonts are usually subset-encoded and can't be reused
    for new text, so replacements map to the closest Base-14 family from the
    span flags (serif/mono/bold/italic). Falls back to rect-derived metrics
    when no span overlaps (e.g. text inside a Form XObject).
    """
    import fitz

    best, best_overlap = None, 0.0
    for block in page.get_text("dict", clip=rect).get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                sr = fitz.Rect(span["bbox"])
                if not sr.intersects(rect):
                    continue
                overlap = abs((sr & rect).get_area())
                if overlap > best_overlap:
                    best, best_overlap = span, overlap

    if best is None:
        size = max(6.0, rect.height * 0.8)
        return size, (0, 0, 0), "helv", rect.y1 - rect.height * 0.22

    flags = best.get("flags", 0)
    italic, serif = bool(flags & 2), bool(flags & 4)
    mono, bold = bool(flags & 8), bool(flags & 16)
    if mono:
        base = "cobi" if bold and italic else "cobo" if bold else "coit" if italic else "cour"
    elif serif:
        base = "tibi" if bold and italic else "tibo" if bold else "tiit" if italic else "tiro"
    else:
        base = "hebi" if bold and italic else "hebo" if bold else "heit" if italic else "helv"

    c = best.get("color", 0)
    color = ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)
    origin = best.get("origin")
    baseline_y = float(origin[1]) if origin else rect.y1 - rect.height * 0.22
    return float(best.get("size", rect.height * 0.8)), color, base, baseline_y


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_pdf(path: str, pages: str | None) -> str:
    import fitz

    doc = fitz.open(path)
    meta = doc.metadata or {}
    total = doc.page_count
    result = [f"**PDF**: {Path(path).name} — {total} page(s)"]

    # Document properties
    props = []
    if meta.get("title"):
        props.append(f"Title: {meta['title']}")
    if meta.get("author"):
        props.append(f"Author: {meta['author']}")
    if meta.get("subject"):
        props.append(f"Subject: {meta['subject']}")
    if meta.get("creator"):
        props.append(f"Creator: {meta['creator']}")
    if meta.get("creationDate"):
        props.append(f"Created: {meta['creationDate'][:10]}")
    if props:
        result.append("**Properties**: " + " | ".join(props))

    # Page info
    if total > 0:
        p0 = doc[0]
        w_in = round(p0.rect.width / 72, 2)
        h_in = round(p0.rect.height / 72, 2)
        orient = "landscape" if w_in > h_in else "portrait"
        result.append(f"**Pages**: {total} pages, {w_in}x{h_in}in ({orient})")

    # File size
    file_size = os.path.getsize(path)
    if file_size > 1_000_000:
        result.append(f"**Size**: {file_size / 1_000_000:.1f} MB")
    else:
        result.append(f"**Size**: {file_size / 1_000:.0f} KB")

    # Content stats
    stats = []
    word_count = 0
    img_count = 0
    annot_count = 0
    for page in doc:
        text = page.get_text()
        word_count += len(text.split())
        img_count += len(page.get_images(full=False))
        annot_count += len(list(page.annots() or []))
    stats.append(f"~{word_count:,} words")
    if img_count:
        stats.append(f"{img_count} images")
    if annot_count:
        stats.append(f"{annot_count} annotations")
    # Form fields
    form_fields = 0
    for page in doc:
        widgets = list(page.widgets() or [])
        form_fields += len(widgets)
    if form_fields:
        stats.append(f"{form_fields} form fields")
    else:
        stats.append("No forms")
    result.append(f"**Content**: {' | '.join(stats)}")

    # TOC / bookmarks
    toc = doc.get_toc()
    if toc:
        result.append(f"**TOC**: {len(toc)} bookmarks")
        for level, title, page_num in toc[:10]:
            result.append(f"  {'  ' * (level - 1)}{title} (p.{page_num})")
        if len(toc) > 10:
            result.append(f"  ... and {len(toc) - 10} more")

    # Security
    if doc.is_encrypted:
        result.append("**Security**: Encrypted")
    else:
        result.append("**Security**: Not encrypted")

    result.append("")

    # Text extraction
    start, end = 0, total
    if pages:
        parts = pages.split("-")
        start = max(0, int(parts[0]) - 1)
        end = min(total, int(parts[-1]))
    for i in range(start, end):
        text = doc[i].get_text().strip()
        if text:
            result.append(f"--- Page {i + 1} ---")
            result.append(text)
            result.append("")
    if not any(doc[i].get_text().strip() for i in range(start, end)):
        result.append("(PDF appears to be scanned — no extractable text. Use edit_pdf with ocr operation.)")

    doc.close()
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Write PDF (HTML/Markdown → PDF via WeasyPrint)
# ---------------------------------------------------------------------------


async def handle_write_pdf(args: dict) -> str:
    import markdown as md
    from weasyprint import HTML

    path = _resolve_path(args["path"], writing=True)
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    content = args.get("content", "")
    content_type = args.get("content_type", "markdown")
    custom_css = args.get("css", "")
    page_size = args.get("page_size", "A4")
    margins = args.get("margins", {})

    if content_type == "markdown":
        html_body = md.markdown(
            content, extensions=["tables", "fenced_code", "nl2br"]
        )
    else:
        html_body = content

    def resolve_img_src(match):
        src = match.group(1)
        try:
            resolved = _resolve_path(src)
            return f'src="file://{resolved}"'
        except Exception:
            return match.group(0)

    html_body = re.sub(r'src="([^"]+)"', resolve_img_src, html_body)

    m_top = margins.get("top", "2cm")
    m_bottom = margins.get("bottom", "2cm")
    m_left = margins.get("left", "2cm")
    m_right = margins.get("right", "2cm")

    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: {page_size}; margin: {m_top} {m_right} {m_bottom} {m_left}; }}
body {{ font-family: 'Liberation Sans', 'DejaVu Sans', sans-serif; font-size: 12pt; line-height: 1.5; color: #333; }}
h1 {{ font-size: 24pt; margin-top: 0; }}
h2 {{ font-size: 18pt; }}
h3 {{ font-size: 14pt; }}
table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
th {{ background: #f0f0f0; font-weight: bold; }}
code {{ background: #f5f5f5; padding: 2px 4px; border-radius: 3px; font-size: 10pt; }}
pre {{ background: #f5f5f5; padding: 12px; border-radius: 5px; overflow-x: auto; }}
img {{ max-width: 100%; }}
{custom_css}
</style></head><body>{html_body}</body></html>"""

    HTML(string=full_html).write_pdf(path)
    await _push_preview(path)
    return f"PDF created: {_to_agents_relative(path)}"


# ---------------------------------------------------------------------------
# Edit PDF (19 operations)
# ---------------------------------------------------------------------------


async def handle_edit_pdf(args: dict) -> str:
    import fitz

    # edit_pdf ALWAYS rewrites the source in place at save time (even
    # extraction-only op sets go through the save→os.replace tail), so the
    # input is a write target — resolve it as one so the proxy's write-RBAC
    # fires instead of the read check.
    path = _resolve_path(args["path"], writing=True)
    if not Path(path).exists():
        return f"Error: File not found: {args['path']}"

    ops = _normalize_operations(args.get("operations"))
    doc = fitz.open(path)
    errors = []
    notes = []

    for idx, op in enumerate(ops):
        ot = _op_type(op)
        try:
            # =============================================================
            # PAGE OPERATIONS
            # =============================================================

            if ot == "merge":
                files = op.get("files", [])
                position = op.get("position", "end")
                for f in files:
                    src_path = _resolve_path(f)
                    if not Path(src_path).exists():
                        errors.append(f"Op #{idx} merge: file not found: {f}")
                        continue
                    src = fitz.open(src_path)
                    if position == "start":
                        doc.insert_pdf(src, to_page=-1, start_at=0)
                    elif isinstance(position, int):
                        doc.insert_pdf(src, start_at=position)
                    else:
                        doc.insert_pdf(src)
                    src.close()

            elif ot == "split":
                page_spec = op.get("pages", "all")
                output_path = op.get("output_path")
                if not output_path:
                    errors.append(f"Op #{idx} split: output_path required")
                    continue
                out = _resolve_path(output_path, writing=True)
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                page_indices = _parse_pages(page_spec, doc.page_count)
                new_doc = fitz.open()
                for pi in page_indices:
                    new_doc.insert_pdf(doc, from_page=pi, to_page=pi)
                new_doc.save(out)
                new_doc.close()

            elif ot == "rotate_page":
                page_spec = op.get("pages", "all")
                degrees = int(op.get("degrees", 90))
                for pi in _parse_pages(page_spec, doc.page_count):
                    doc[pi].set_rotation(degrees)

            elif ot == "delete_page":
                page_spec = op.get("pages", [])
                indices = _parse_pages(page_spec, doc.page_count)
                # Delete in reverse order to preserve indices
                for pi in sorted(indices, reverse=True):
                    doc.delete_page(pi)

            elif ot == "reorder_pages":
                order = op.get("order", [])
                if order:
                    doc.select([int(i) for i in order])

            elif ot == "insert_page":
                position = int(op.get("position", -1))
                width = float(op.get("width", 595))
                height = float(op.get("height", 842))
                doc.insert_page(position, width=width, height=height)

            elif ot == "crop_page":
                page_spec = op.get("pages", "all")
                rect = op.get("rect")
                for pi in _parse_pages(page_spec, doc.page_count):
                    page = doc[pi]
                    if rect == "auto":
                        # Auto-detect content boundaries
                        blocks = page.get_text("blocks")
                        if blocks:
                            x0 = min(b[0] for b in blocks) - 10
                            y0 = min(b[1] for b in blocks) - 10
                            x1 = max(b[2] for b in blocks) + 10
                            y1 = max(b[3] for b in blocks) + 10
                            page.set_cropbox(fitz.Rect(x0, y0, x1, y1))
                    elif rect and len(rect) == 4:
                        page.set_cropbox(fitz.Rect(*rect))

            # =============================================================
            # CONTENT OPERATIONS
            # =============================================================

            elif ot == "add_text":
                pi = int(op.get("page", 0))
                if pi >= doc.page_count:
                    errors.append(f"Op #{idx} add_text: page {pi} out of range")
                    continue
                page = doc[pi]
                text = op.get("text", "")
                x = float(op.get("x", 72))
                y = float(op.get("y", 72))
                font_size = float(op.get("font_size", 12))
                color = op.get("color", [0, 0, 0])
                if isinstance(color, list) and len(color) == 3:
                    color = tuple(float(c) for c in color)
                else:
                    color = (0, 0, 0)
                fontname = op.get("font", "helv")
                page.insert_text(
                    fitz.Point(x, y), text,
                    fontsize=font_size, fontname=fontname, color=color,
                )

            elif ot == "add_image":
                pi = int(op.get("page", 0))
                if pi >= doc.page_count:
                    errors.append(f"Op #{idx} add_image: page {pi} out of range")
                    continue
                page = doc[pi]
                img_path = _resolve_path(
                    op.get("image_path") or op.get("path") or op.get("image", "")
                )
                rect = op.get("rect", [50, 50, 200, 200])
                page.insert_image(fitz.Rect(*rect), filename=img_path)

            elif ot == "add_watermark":
                text = op.get("text", "DRAFT")
                page_spec = op.get("pages", "all")
                font_size = float(op.get("font_size", 60))
                color = op.get("color", [0.8, 0.8, 0.8])
                if isinstance(color, list):
                    color = tuple(float(c) for c in color)
                rotation = float(op.get("rotation", 45))
                opacity = float(op.get("opacity", 0.3))

                text_len = fitz.get_text_length(
                    text, fontname="helv", fontsize=font_size)
                for pi in _parse_pages(page_spec, doc.page_count):
                    page = doc[pi]
                    rect = page.rect
                    center = fitz.Point(rect.width / 2, rect.height / 2)
                    # Straight text centered on the page, then rotated as one
                    # unit around the center via morph. insert_text's own
                    # `rotate` only takes quarter turns — and combining it
                    # with a morph used to shear each glyph individually.
                    m = fitz.Matrix(1, 1)
                    m.prerotate(rotation)  # positive = the usual ↗ diagonal
                    page.insert_text(
                        fitz.Point(center.x - text_len / 2,
                                   center.y + font_size * 0.35),
                        text,
                        fontsize=font_size,
                        fontname="helv",
                        color=color,
                        overlay=True,
                        fill_opacity=opacity,
                        stroke_opacity=opacity,
                        morph=(center, m),
                    )

            elif ot == "replace_text":
                find = op.get("find", "")
                replace = op.get("replace", "")
                if not find:
                    errors.append(f"Op #{idx} replace_text: 'find' text required")
                    continue
                page_spec = op.get("pages", "all")
                case_sensitive = bool(op.get("case_sensitive", False))
                total, pages_hit = 0, 0

                for pi in _parse_pages(page_spec, doc.page_count):
                    page = doc[pi]
                    hits = page.search_for(find)
                    if case_sensitive:
                        # search_for matches case-insensitively; keep only
                        # hits whose underlying text contains the exact form.
                        hits = [
                            r for r in hits
                            if find in page.get_text(
                                "text", clip=fitz.Rect(r) + (-1, -1, 1, 1))
                        ]
                    if not hits:
                        continue

                    # Capture style + free width BEFORE redacting (redaction
                    # removes the spans the probes read), then redact, then
                    # reinsert.
                    words = page.get_text("words")
                    plans = []
                    for r in hits:
                        size, color, fontname, baseline_y = _text_style_at(page, r)
                        # The replacement may run past the original footprint
                        # up to the next word on the same line (minus one
                        # space width) — or to the right margin when the hit
                        # ends its line.
                        next_x0 = min(
                            (w[0] for w in words
                             if w[1] < r.y1 and w[3] > r.y0 and w[0] >= r.x1 - 1),
                            default=page.rect.x1 - 36,
                        )
                        max_w = max(r.width, next_x0 - r.x0 - 0.25 * size)
                        plans.append((fitz.Rect(r), size, color, fontname,
                                      baseline_y, max_w))
                        page.add_redact_annot(r)
                    # IMAGE_NONE: never damage figures the hit rect overlaps.
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

                    if replace:
                        for r, size, color, fontname, baseline_y, max_w in plans:
                            fs = size
                            # Shrink until the replacement fits the free width
                            # so it never collides with its neighbors.
                            while fs > 5 and fitz.get_text_length(
                                    replace, fontname=fontname,
                                    fontsize=fs) > max_w:
                                fs -= 0.5
                            page.insert_text(
                                fitz.Point(r.x0, baseline_y), replace,
                                fontsize=fs, fontname=fontname, color=color,
                            )
                    total += len(hits)
                    pages_hit += 1

                if total == 0:
                    errors.append(f"Op #{idx} replace_text: '{find}' not found")
                else:
                    what = "deleted" if not replace else f"replaced with '{replace}'"
                    notes.append(
                        f"replace_text: {total} occurrence(s) of '{find}' "
                        f"{what} across {pages_hit} page(s)")

            elif ot == "redact":
                find = op.get("find")
                rect = op.get("rect")
                if not find and not (rect and len(rect) == 4):
                    errors.append(
                        f"Op #{idx} redact: needs 'find' text or a 4-number 'rect'")
                    continue
                page_spec = op.get("pages", "all")
                fill = op.get("fill", [0, 0, 0])
                if isinstance(fill, list) and len(fill) == 3:
                    fill = tuple(float(c) for c in fill)
                else:
                    fill = (0, 0, 0)
                count = 0

                for pi in _parse_pages(page_spec, doc.page_count):
                    page = doc[pi]
                    targets = page.search_for(find) if find else [fitz.Rect(*rect)]
                    if not targets:
                        continue
                    for r in targets:
                        page.add_redact_annot(r, fill=fill)
                    # Privacy-grade: blank image pixels under the region too.
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
                    count += len(targets)

                if count == 0:
                    errors.append(
                        f"Op #{idx} redact: nothing matched"
                        + (f" '{find}'" if find else ""))
                else:
                    notes.append(f"redact: {count} region(s) permanently removed")

            elif ot == "add_annotation":
                pi = int(op.get("page", 0))
                if pi >= doc.page_count:
                    errors.append(f"Op #{idx} add_annotation: page {pi} out of range")
                    continue
                page = doc[pi]
                annot_type = op.get("annotation_type", "highlight")
                rect = op.get("rect", [100, 700, 400, 720])
                r = fitz.Rect(*rect)
                color = op.get("color", [1, 1, 0])
                if isinstance(color, list):
                    color = tuple(float(c) for c in color)
                content = op.get("content", "")

                if annot_type == "highlight":
                    annot = page.add_highlight_annot(r)
                elif annot_type == "underline":
                    annot = page.add_underline_annot(r)
                elif annot_type == "strikeout":
                    annot = page.add_strikeout_annot(r)
                elif annot_type == "text_note":
                    annot = page.add_text_annot(fitz.Point(r.x0, r.y0), content)
                elif annot_type == "rectangle":
                    annot = page.add_rect_annot(r)
                else:
                    errors.append(f"Op #{idx} add_annotation: unknown type '{annot_type}'")
                    continue

                if annot and color and annot_type != "text_note":
                    annot.set_colors(stroke=color)
                    annot.update()
                if annot and content and annot_type != "text_note":
                    annot.set_info(content=content)
                    annot.update()

            # =============================================================
            # SECURITY & OPTIMIZATION
            # =============================================================

            elif ot == "encrypt":
                user_pw = op.get("user_password") or op.get("password", "")
                owner_pw = op.get("owner_password", user_pw)
                perms_raw = op.get("permissions", {})

                # Accept both dict {"print": true} and list ["print", "copy"]
                if isinstance(perms_raw, list):
                    perms = {p: True for p in perms_raw}
                elif isinstance(perms_raw, dict):
                    perms = perms_raw
                else:
                    perms = {}

                perm_flags = fitz.PDF_PERM_ACCESSIBILITY
                if perms.get("print", True):
                    perm_flags |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
                if perms.get("copy", True):
                    perm_flags |= fitz.PDF_PERM_COPY
                if perms.get("annotate", True):
                    perm_flags |= fitz.PDF_PERM_ANNOTATE
                if perms.get("modify", False):
                    perm_flags |= fitz.PDF_PERM_MODIFY

                # Save encrypted — applied at save time, store params for later
                doc._encrypt_user_pw = user_pw
                doc._encrypt_owner_pw = owner_pw
                doc._encrypt_perms = perm_flags

            elif ot == "decrypt":
                password = op.get("password", "")
                if doc.is_encrypted:
                    if not doc.authenticate(password):
                        errors.append(f"Op #{idx} decrypt: incorrect password")

            elif ot == "compress":
                # Compression applied at save time — store params
                doc._compress_garbage = int(op.get("garbage", 4))
                doc._compress_deflate = op.get("deflate", True)
                doc._compress_deflate_images = op.get("deflate_images", True)
                doc._compress_deflate_fonts = op.get("deflate_fonts", True)

            elif ot == "set_metadata":
                new_meta = {}
                for key in ("title", "author", "subject", "keywords", "creator", "producer"):
                    if op.get(key):
                        new_meta[key] = op[key]
                if new_meta:
                    doc.set_metadata(new_meta)

            # =============================================================
            # EXTRACTION
            # =============================================================

            elif ot == "extract_images":
                page_spec = op.get("pages", "all")
                output_dir = op.get("output_dir")
                if not output_dir:
                    output_dir = str(Path(path).parent / (Path(path).stem + "_images"))
                out_dir = _resolve_path(output_dir, writing=True) if output_dir.startswith("/") else output_dir
                Path(out_dir).mkdir(parents=True, exist_ok=True)

                extracted = []
                for pi in _parse_pages(page_spec, doc.page_count):
                    page = doc[pi]
                    for img_idx, img in enumerate(page.get_images(full=True)):
                        xref = img[0]
                        try:
                            base_image = doc.extract_image(xref)
                            ext = base_image["ext"]
                            img_bytes = base_image["image"]
                            fname = f"page{pi + 1}_img{img_idx + 1}.{ext}"
                            img_path = Path(out_dir) / fname
                            img_path.write_bytes(img_bytes)
                            extracted.append(str(img_path))
                        except Exception as e:
                            logger.warning(f"Failed to extract image xref={xref}: {e}")

                if extracted:
                    errors.append(f"Op #{idx} extract_images: extracted {len(extracted)} images to {_to_agents_relative(out_dir)}")

            elif ot == "ocr":
                language = op.get("language", "eng")
                page_spec = op.get("pages", "all")
                output_path = op.get("output_path")

                for pi in _parse_pages(page_spec, doc.page_count):
                    page = doc[pi]
                    try:
                        # Render the page and let Tesseract produce a 1-page
                        # PDF with an invisible text layer, then swap it in.
                        # (A TextPage from get_textpage_ocr() is extraction-
                        # only — it never modifies the document, so the saved
                        # PDF would stay unsearchable.) The swap rasterizes
                        # the page at 300 dpi, which is a no-op in practice:
                        # the op targets scanned (already-raster) pages.
                        pix = page.get_pixmap(dpi=300)
                        ocr_pdf = fitz.open(
                            "pdf", pix.pdfocr_tobytes(language=language)
                        )
                        doc.delete_page(pi)
                        doc.insert_pdf(
                            ocr_pdf, from_page=0, to_page=0, start_at=pi
                        )
                        ocr_pdf.close()
                    except Exception as e:
                        errors.append(f"Op #{idx} ocr: page {pi} failed: {e}")

                if output_path:
                    out = _resolve_path(output_path, writing=True)
                    Path(out).parent.mkdir(parents=True, exist_ok=True)
                    doc.save(out)

            # =============================================================
            # UNKNOWN
            # =============================================================

            else:
                logger.warning(f"edit_pdf: unknown operation '{ot}', skipping")
                errors.append(f"Op #{idx}: unknown operation '{ot}'")

        except Exception as exc:
            errors.append(f"Op #{idx} {ot}: {exc}")
            logger.warning(f"edit_pdf op #{idx} '{ot}' failed: {exc}")

    # Save with encryption/compression if requested
    save_kwargs = {}

    # Encryption params
    if hasattr(doc, "_encrypt_user_pw"):
        save_kwargs["user_pw"] = doc._encrypt_user_pw
        save_kwargs["owner_pw"] = doc._encrypt_owner_pw
        save_kwargs["permissions"] = doc._encrypt_perms
        save_kwargs["encryption"] = fitz.PDF_ENCRYPT_AES_256

    # Compression params
    garbage = getattr(doc, "_compress_garbage", 0)
    if garbage:
        save_kwargs["garbage"] = garbage
    if getattr(doc, "_compress_deflate", False):
        save_kwargs["deflate"] = True
    if getattr(doc, "_compress_deflate_images", False):
        save_kwargs["deflate_images"] = True
    if getattr(doc, "_compress_deflate_fonts", False):
        save_kwargs["deflate_fonts"] = True

    # Save — pymupdf can't overwrite the source file directly, so save to temp then replace
    import tempfile

    size_before = os.path.getsize(path)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=str(Path(path).parent))
    os.close(tmp_fd)
    try:
        doc.save(tmp_path, **save_kwargs)
        doc.close()
        os.replace(tmp_path, path)
    except Exception:
        doc.close()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    size_after = os.path.getsize(path)

    await _push_preview(path)

    msg = f"PDF saved: {_to_agents_relative(path)} ({len(ops)} operations applied)"
    if garbage:
        pct = (1 - size_after / max(size_before, 1)) * 100
        msg += f"\nCompression: {size_before / 1024:.0f}KB → {size_after / 1024:.0f}KB ({pct:.0f}% reduction)"
    if notes:
        msg += "\n" + "\n".join(f"  - {n}" for n in notes)
    if errors:
        msg += f"\n\nWarnings/Errors ({len(errors)}):\n" + "\n".join(f"  - {e}" for e in errors)
    return msg


# ---------------------------------------------------------------------------
# PDF to Images
# ---------------------------------------------------------------------------


async def handle_pdf_to_images(args: dict) -> str:
    import fitz

    path = _resolve_path(args["path"])
    if not Path(path).exists():
        return f"Error: File not found: {args['path']}"

    output_dir = args.get("output_dir")
    if not output_dir:
        output_dir = str(Path(path).parent / (Path(path).stem + "_pages"))
    out_dir = _resolve_path(output_dir, writing=True)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    page_spec = args.get("pages", "all")
    dpi = int(args.get("dpi", 150))
    fmt = args.get("format", "png").lower()

    doc = fitz.open(path)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    page_indices = _parse_pages(page_spec, doc.page_count)
    saved = []

    for pi in page_indices:
        page = doc[pi]
        pix = page.get_pixmap(matrix=mat)
        fname = f"page_{pi + 1:03d}.{fmt}"
        out_path = Path(out_dir) / fname
        if fmt == "png":
            pix.save(str(out_path))
        elif fmt in ("jpg", "jpeg"):
            pix.save(str(out_path), jpg_quality=95)
        else:
            pix.save(str(out_path))
        saved.append({"path": _to_agents_relative(str(out_path)), "width": pix.width, "height": pix.height})

    doc.close()

    # Push first page inline for preview
    if saved:
        first_path = _resolve_path(saved[0]["path"])
        img_bytes = Path(first_path).read_bytes()
        mime = "image/png" if fmt == "png" else "image/jpeg"
        await _push_image_preview(img_bytes, mime, f"Page 1 of {Path(path).name}")

    return (
        f"Rendered {len(saved)} pages from {_to_agents_relative(path)} at {dpi}dpi.\n"
        f"Output: {_to_agents_relative(out_dir)}/\n"
        f"Files: {', '.join(Path(s['path']).name for s in saved[:5])}"
        + (f" ... +{len(saved) - 5} more" if len(saved) > 5 else "")
    )


# ---------------------------------------------------------------------------
# Screenshot Document (visual feedback for LLM)
# ---------------------------------------------------------------------------

_SCREENSHOT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
                    ".odt", ".ods", ".odp", ".csv", ".rtf", ".html"}
_MAX_INLINE_PAGES = 10


async def handle_screenshot_document(args: dict) -> list:
    """Render document pages as ImageContent for LLM visual inspection.

    Returns list of ImageContent + TextContent (not pushed to dashboard).
    """
    import base64
    import shutil
    import tempfile

    import fitz
    from mcp.types import ImageContent, TextContent

    path = _resolve_path(args.get("path", ""))
    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"Error: file not found: {_to_agents_relative(path)}")]

    ext = Path(path).suffix.lower()
    if ext not in _SCREENSHOT_EXTS:
        return [TextContent(type="text",
                text=f"Error: unsupported format '{ext}'. Supported: PDF, DOCX, XLSX, PPTX and other Office formats.")]

    pages_spec = args.get("pages", "1")
    dpi = int(args.get("dpi", 150))
    sheet = args.get("sheet")
    zoom = dpi / 72.0

    pdf_path = path
    temp_dir = None

    try:
        # For non-PDF: convert to temp PDF via LibreOffice
        if ext != ".pdf":
            temp_dir = tempfile.mkdtemp(dir=str(Path(path).parent))
            convert_path = path

            # Excel: set fit-to-width page setup + handle sheet selection
            if ext in (".xlsx", ".xls"):
                try:
                    convert_path = _excel_prepare_for_screenshot(
                        path, temp_dir, sheet
                    )
                    if sheet is not None:
                        # Sheet selection: override pages to target sheet
                        sheet_idx = _excel_sheet_index(path, sheet)
                        pages_spec = str(sheet_idx + 1)
                except Exception as e:
                    logger.warning(f"screenshot excel prep failed: {e}, using original")
                    convert_path = path
            elif sheet is not None and ext in (".ods", ".csv"):
                # ODS/CSV: just handle sheet selection (no page setup tweak)
                try:
                    sheet_idx = _excel_sheet_index(path, sheet)
                    pages_spec = str(sheet_idx + 1)
                except Exception as e:
                    logger.warning(f"screenshot sheet lookup failed: {e}")

            pdf_path = await _libreoffice_convert(convert_path, "pdf", temp_dir)

        # Open PDF and render pages
        doc = fitz.open(pdf_path)
        total = len(doc)
        page_indices = _parse_pages(pages_spec, total)

        if not page_indices:
            doc.close()
            return [TextContent(type="text", text=f"No valid pages to render (document has {total} pages).")]

        # Cap inline pages
        capped = False
        if len(page_indices) > _MAX_INLINE_PAGES:
            page_indices = page_indices[:_MAX_INLINE_PAGES]
            capped = True

        # Render pages as ImageContent (visible to LLM via vision, NOT pushed to dashboard)
        content_items: list = []
        rendered = []
        mat = fitz.Matrix(zoom, zoom)
        for pi in page_indices:
            page = doc[pi]
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode()
            w_in = round(page.rect.width / 72, 2)
            h_in = round(page.rect.height / 72, 2)
            content_items.append(ImageContent(
                type="image", data=b64, mimeType="image/png",
            ))
            rendered.append({"page": pi + 1, "width_in": w_in, "height_in": h_in})

        doc.close()

        # Append text summary after images
        fname = Path(path).name
        lines = [f"Rendered {len(rendered)} page(s) from {fname} at {dpi} DPI:"]
        for r in rendered:
            lines.append(f"  Page {r['page']}: {r['width_in']} x {r['height_in']} inches")
        if capped:
            lines.append(f"  (capped at {_MAX_INLINE_PAGES} pages — use specific page ranges for more)")
        content_items.append(TextContent(type="text", text="\n".join(lines)))
        return content_items

    except Exception as e:
        return [TextContent(type="text", text=f"Error rendering {_to_agents_relative(path)}: {e}")]
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def _excel_sheet_index(path: str, sheet) -> int:
    """Get 0-based sheet index from name or numeric index."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    names = wb.sheetnames
    wb.close()
    if isinstance(sheet, int) or (isinstance(sheet, str) and sheet.isdigit()):
        return int(sheet)
    return names.index(str(sheet)) if str(sheet) in names else 0


def _excel_prepare_for_screenshot(path: str, temp_dir: str, sheet=None) -> str:
    """Create a temp copy of an Excel file with fit-to-width page setup.

    Safeguards based on column count:
      <= 10 columns: portrait, fit to 1 page wide
      11-20 columns: landscape, fit to 1 page wide
      > 20 columns:  landscape, no fit (would be unreadably small)
    Rows always flow naturally across pages.
    """
    import openpyxl
    from openpyxl.worksheet.properties import PageSetupProperties

    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        # Count used columns
        max_col = ws.max_column or 1

        if max_col <= 10:
            # Portrait, fit all columns to 1 page wide
            ws.page_setup.orientation = "portrait"
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 0
            ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        elif max_col <= 20:
            # Landscape, fit all columns to 1 page wide
            ws.page_setup.orientation = "landscape"
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 0
            ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        else:
            # Too many columns — landscape but let it paginate
            ws.page_setup.orientation = "landscape"

    temp_path = os.path.join(temp_dir, Path(path).name)
    wb.save(temp_path)
    wb.close()
    return temp_path


# ---------------------------------------------------------------------------
# Images to PDF
# ---------------------------------------------------------------------------


async def handle_images_to_pdf(args: dict) -> str:
    import fitz

    images = args.get("images", [])
    output_path = args.get("output_path")
    if not output_path:
        return "Error: output_path is required"
    if not images:
        return "Error: images list is empty"

    out = _resolve_path(output_path, writing=True)
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    page_size = args.get("page_size", "a4").lower()
    fit = args.get("fit", "contain")

    # Page dimensions in points
    sizes = {
        "a4": (595, 842),
        "letter": (612, 792),
        "a3": (842, 1190),
    }

    doc = fitz.open()

    for img_input in images:
        img_path = _resolve_path(img_input)
        if not Path(img_path).exists():
            logger.warning(f"images_to_pdf: skipping {img_input} (not found)")
            continue

        # Get image dimensions
        img_doc = fitz.open(img_path)
        if img_doc.page_count == 0:
            img_doc.close()
            continue
        img_rect = img_doc[0].rect
        img_w, img_h = img_rect.width, img_rect.height
        img_doc.close()

        if page_size == "original":
            pw, ph = img_w, img_h
        else:
            pw, ph = sizes.get(page_size, (595, 842))
            # Auto-rotate page to match image orientation
            if (img_w > img_h and pw < ph) or (img_h > img_w and ph < pw):
                pw, ph = ph, pw

        page = doc.new_page(width=pw, height=ph)

        if fit == "stretch":
            img_rect = fitz.Rect(0, 0, pw, ph)
        elif fit == "cover":
            scale = max(pw / img_w, ph / img_h)
            new_w, new_h = img_w * scale, img_h * scale
            x = (pw - new_w) / 2
            y = (ph - new_h) / 2
            img_rect = fitz.Rect(x, y, x + new_w, y + new_h)
        else:  # contain
            scale = min(pw / img_w, ph / img_h)
            new_w, new_h = img_w * scale, img_h * scale
            x = (pw - new_w) / 2
            y = (ph - new_h) / 2
            img_rect = fitz.Rect(x, y, x + new_w, y + new_h)

        page.insert_image(img_rect, filename=img_path)

    doc.save(out)
    doc.close()

    await _push_preview(out)
    return f"PDF created: {_to_agents_relative(out)} ({len(images)} images, {page_size})"


# ---------------------------------------------------------------------------
# Convert document
# ---------------------------------------------------------------------------


async def handle_convert_document(args: dict) -> str:
    input_path = _resolve_path(args["input_path"])
    if not Path(input_path).exists():
        return f"Error: File not found: {args['input_path']}"

    output_format = args.get("output_format", "pdf")
    output_path = args.get("output_path")

    if output_path:
        output_dir = str(Path(_resolve_path(output_path, writing=True)).parent)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    else:
        output_dir = str(Path(input_path).parent)

    ext = Path(input_path).suffix.lower()

    # Markdown → PDF via WeasyPrint
    if ext in (".md", ".markdown") and output_format == "pdf":
        content = Path(input_path).read_text(encoding="utf-8")
        out = str(Path(output_dir) / (Path(input_path).stem + ".pdf"))
        await handle_write_pdf({
            "path": _to_agents_relative(out),
            "content": content,
            "content_type": "markdown",
        })
        return f"Converted: {_to_agents_relative(out)}"

    # Image → PDF via pymupdf
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp") and output_format == "pdf":
        out = str(Path(output_dir) / (Path(input_path).stem + ".pdf"))
        await handle_images_to_pdf({
            "images": [_to_agents_relative(input_path)],
            "output_path": _to_agents_relative(out),
            "page_size": "original",
        })
        return f"Converted: {_to_agents_relative(out)}"

    # PDF → PNG via pymupdf
    if ext == ".pdf" and output_format in ("png", "jpg", "jpeg"):
        out_dir = str(Path(output_dir) / Path(input_path).stem)
        await handle_pdf_to_images({
            "path": _to_agents_relative(input_path),
            "output_dir": _to_agents_relative(out_dir),
            "format": output_format,
            "dpi": 150,
        })
        return f"Converted: {_to_agents_relative(out_dir)}/"

    # LibreOffice headless for everything else
    try:
        out = await _libreoffice_convert(input_path, output_format, output_dir)
    except RuntimeError as exc:
        return f"Conversion error: {exc}"

    if output_path:
        final = _resolve_path(output_path, writing=True)
        Path(out).rename(final)
        out = final

    await _push_preview(out)
    return f"Converted: {_to_agents_relative(out)}"
