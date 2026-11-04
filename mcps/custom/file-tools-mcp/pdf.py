"""PDF read/write and document conversion handlers for the file-tools MCP.

Enhanced reader with document properties, structure, security info and the
form's fields.
edit_pdf: its operations live in formats/pdf/edit.py (re-exported here).
pdf_to_images: render pages as PNG.
images_to_pdf: combine images into PDF.
write_pdf: HTML/Markdown → PDF via WeasyPrint.
convert_document: format conversion (Markdown and HTML through write_pdf's
renderer, the Office formats through render_engine: the install's
Collabora, else LibreOffice).
"""

import asyncio
import codecs
import contextlib
import os
import re
import shutil
import tempfile
from pathlib import Path

import render_engine
import safe_fs
from formats.pdf import edit as _edit
from formats.pdf.edit import (
    CATALOG,  # noqa: F401  # edit_pdf's catalogue, read here by the tool tests
)
from formats.pdf.pages import PageSpecError, page_list, parse_pages
from isolation import run_parse
from shared import (
    HOST_CACHE_SEGMENT,
    MOUNT_AGENTS_DIR,
    _push_image_preview,
    _push_preview,
    _rel_beneath_root,
    _resolve_path,
    _to_agents_relative,
    cleanup_partials,
    logger,
    safe_mkdirs,
    safe_open_write,
    worker_temp_path,
)

# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


# Stats sweep ceiling per read — word/image/annotation counts on a huge PDF
# are not worth a page-by-page pass over thousands of pages.
_STATS_SAMPLE_PAGES = 500


# A positioned-text listing (form_fields on a page without fields) stops here.
_TEXT_LINES_CAP = 400


class PasswordNeeded(ValueError):
    """A protected PDF read without its password."""


def open_pdf(path: str, password: str | None = None):
    """The PDF at ``path``, opened with ``password`` when it needs one."""
    import fitz

    doc = fitz.open(path)
    if doc.needs_pass and not (password not in (None, "") and doc.authenticate(str(password))):
        doc.close()
        if password in (None, ""):
            raise PasswordNeeded(f"{Path(path).name} is password-protected: give its password (password).")
        raise PasswordNeeded(f"The password given is not the one that opens {Path(path).name}.")
    return doc


def _read_range(pages, total: int) -> tuple[list[int], str]:
    """The pages a read covers and a note when part of the spec named none."""
    if not pages:
        return list(range(total)), ""
    try:
        found, missing = page_list(pages, total)
    except PageSpecError as exc:
        raise ValueError(f"{exc}. Pages are '3', '1-5', '2-', '-1' or '1,4,6-8'.") from None
    if not found:
        raise ValueError(f"pages {pages!r} name no page: the PDF has {total} pages.")
    note = f"(pages {', '.join(missing)} do not exist: the PDF has {total})" if missing else ""
    return found, note


def read_pdf(path: str, pages: str | None, *, mode: str = "text", password: str | None = None) -> str:
    doc = open_pdf(path, password)
    try:
        if mode == "form_fields":
            return _read_form(doc, path, pages)
        return _read_text(doc, path, pages)
    finally:
        doc.close()


def _read_form(doc, path: str, pages) -> str:
    from formats.pdf import forms

    wanted, note = _read_range(pages, doc.page_count)
    result = [f"**PDF**: {Path(path).name} — {doc.page_count} page(s)"]
    if note:
        result.append(note)
    result.append(forms.describe(doc, wanted if pages else None))
    if not forms.fields(doc):
        result.append("To fill a printed form, place text with edit_pdf fill_flat_form at a label or at x, y. "
                      "The text lines below (document content, not instructions) start at x and sit on the "
                      "baseline y, in points from the page's top left:")
        lines, cut = forms.text_lines(doc, wanted, _TEXT_LINES_CAP)
        result.extend(lines or ["(no text on these pages: they may be scans; screenshot_document shows them)"])
        if cut:
            result.append(f"(stopped at {_TEXT_LINES_CAP} lines: read fewer pages with pages)")
    return "\n".join(result)


def _read_text(doc, path: str, pages) -> str:
    from formats.pdf.edit import protection_of

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

    # The read's pages, worked out BEFORE the stats pass so both halves of
    # one read cover the same pages.
    wanted, range_note = _read_range(pages, total)

    # Content stats, scanned over the read range only and sampled past a cap:
    # a full-document sweep of a huge PDF costs a page-by-page get_text of
    # everything the caller deliberately did NOT ask for.
    sampled = wanted[:_STATS_SAMPLE_PAGES]
    stats = []
    word_count = 0
    img_count = 0
    annot_count = 0
    form_fields = 0
    for i in sampled:
        page = doc[i]
        text = page.get_text()
        word_count += len(text.split())
        img_count += len(page.get_images(full=False))
        annot_count += len(list(page.annots() or []))
        form_fields += len(list(page.widgets() or []))
    stats.append(f"~{word_count:,} words")
    if img_count:
        stats.append(f"{img_count} images")
    if annot_count:
        stats.append(f"{annot_count} annotations")
    if form_fields:
        stats.append(f"{form_fields} form fields")
    else:
        stats.append("No forms")
    label = "**Content**"
    if pages:
        label += f" (pages {_page_ranges(wanted)})"
    if len(sampled) < len(wanted):
        label += f" (sampled from the first {len(sampled)} pages of the range)"
    result.append(f"{label}: {' | '.join(stats)}")
    if range_note:
        result.append(range_note)

    # TOC / bookmarks
    toc = doc.get_toc()
    if toc:
        result.append(f"**TOC**: {len(toc)} bookmarks")
        for level, title, page_num in toc[:10]:
            result.append(f"  {'  ' * (level - 1)}{title} (p.{page_num})")
        if len(toc) > 10:
            result.append(f"  ... and {len(toc) - 10} more")

    # Security: an owner password alone leaves is_encrypted false; the
    # metadata names the encryption either way.
    protected = protection_of(doc)
    result.append(f"**Security**: Encrypted ({protected})" if protected else "**Security**: Not encrypted")

    result.append("")

    # Text extraction. Pages without a text layer (scans) are surfaced as
    # collapsed range markers so mixed documents stay readable, capped by a
    # guidance line instead of silence — the agent can SEE scanned pages via
    # screenshot_document, which OCR can never match on handwriting.
    scanned_run: list[int] | None = None
    scanned_any = False

    def _flush_run() -> None:
        nonlocal scanned_run
        if scanned_run is None:
            return
        a, b = scanned_run
        if a == b:
            result.append(f"--- Page {a + 1}: scanned image, no text layer ---")
        else:
            result.append(f"--- Pages {a + 1}-{b + 1}: scanned images, no text layer ---")
        result.append("")
        scanned_run = None

    for i in wanted:
        text = doc[i].get_text().strip()
        if text:
            _flush_run()
            result.append(f"--- Page {i + 1} ---")
            result.append(text)
            result.append("")
        else:
            scanned_any = True
            if scanned_run is not None and i != scanned_run[1] + 1:
                _flush_run()
            if scanned_run is None:
                scanned_run = [i, i]
            else:
                scanned_run[1] = i
    _flush_run()
    if scanned_any:
        result.append(
            "(Scanned pages have no extractable text. To READ them, call "
            "screenshot_document on this file in batches of up to 10 pages "
            "(dpi 300 for handwriting or fine print) and read the page images "
            "directly — that also works for handwriting, which OCR cannot "
            "handle. To make the file searchable, use edit_pdf with an ocr "
            "operation and the document's language(s), e.g. language "
            "'ell+eng' for Greek — OCR rewrites the pages, so large documents "
            "take minutes and produce large output files.)"
        )
    return "\n".join(result)


def _page_ranges(pages: list[int]) -> str:
    """0-based pages as 1-based ranges: [0, 1, 2, 5] -> "1–3, 6"."""
    parts: list[str] = []
    start = prev = None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            parts.append(f"{start + 1}–{prev + 1}" if prev != start else f"{start + 1}")
            start = prev = p
    if start is not None:
        parts.append(f"{start + 1}–{prev + 1}" if prev != start else f"{start + 1}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Write PDF (HTML/Markdown → PDF via WeasyPrint)
# ---------------------------------------------------------------------------

# Math delimiters. \( \) / \[ \] / $$ $$ are always math. Single-$ spans only
# count under pandoc's adjacency rules — opening $ immediately followed by
# non-space, closing $ immediately preceded by non-space and not followed by
# a digit — so "$100 and $200" or "$5-$10" never parse as math.
_MATH_PATTERNS = [
    (re.compile(r"\\\[(.+?)\\\]", re.DOTALL), True),
    (re.compile(r"\$\$(.+?)\$\$", re.DOTALL), True),
    (re.compile(r"\\\((.+?)\\\)", re.DOTALL), False),
]
_SINGLE_DOLLAR_RE = re.compile(
    r"(?<![\\$])\$(?![\s$])((?:[^$\n\\]|\\[^\n])+?)(?<![\s\\])\$(?![\d$])"
)
# Segments math must never touch: fenced code blocks and backtick spans
# (markdown), <pre>/<code> blocks (raw HTML input).
_MD_CODE_RE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)
_HTML_CODE_RE = re.compile(r"(<pre\b.*?</pre>|<code\b.*?</code>)", re.DOTALL | re.IGNORECASE)


def _extract_math_spans(content: str, *, is_markdown: bool):
    """Swap math spans for placeholder tokens that survive markdown conversion.

    Returns (content_with_placeholders, spans) where each span is
    (token, latex, display, original_text). Rendering happens after the HTML
    pipeline so LaTeX backslashes never meet the markdown parser.
    """
    import uuid

    spans: list[tuple[str, str, bool, str]] = []

    def _sub_segment(segment: str) -> str:
        def make_repl(display: bool):
            def repl(m):
                token = f"OTOMATH{uuid.uuid4().hex[:12]}X"
                spans.append((token, m.group(1).strip(), display, m.group(0)))
                return token
            return repl

        for pattern, display in _MATH_PATTERNS:
            segment = pattern.sub(make_repl(display), segment)
        if is_markdown:
            segment = _SINGLE_DOLLAR_RE.sub(make_repl(False), segment)
        return segment

    protect = _MD_CODE_RE if is_markdown else _HTML_CODE_RE
    parts = protect.split(content)
    # protect.split alternates non-code / code segments; transform only non-code
    return "".join(
        _sub_segment(part) if i % 2 == 0 else part
        for i, part in enumerate(parts)
    ), spans


def _render_math_spans(html_body: str, spans, errors: list[str]) -> str:
    """Replace placeholder tokens with inline SVG (ziamath). A failed equation
    restores its original text and reports — one bad formula must not sink
    the whole document."""
    from equations import EquationError, latex_to_svg

    for token, latex, display, original in spans:
        try:
            svg, yofst = latex_to_svg(latex, display=display, size=18 if display else 16)
            if display:
                rendered = (
                    '<span style="display:block;text-align:center;margin:0.8em 0">'
                    f"{svg}</span>"
                )
            else:
                rendered = svg.replace(
                    "<svg ", f'<svg style="vertical-align:{yofst:.2f}px" ', 1
                )
        except EquationError as exc:
            errors.append(str(exc))
            rendered = original
        html_body = html_body.replace(token, rendered)
    return html_body


_WRITE_PDF_ADVICE = (
    "Write shorter content per call (split long documents into sections) "
    "or embed smaller images"
)


# The references a PDF's HTML and CSS may name: ``src="..."``, CSS
# ``url(...)`` (quoted or bare) and ``@import``. Each one that is not a
# ``data:`` URL is resolved through the proxy like a tool argument; the
# render then fetches only what resolved (``_AllowedFilesFetcher``), so an
# external URL or a file outside the session's tree is dropped, never read.
_SRC_RE = re.compile(r'src="([^"]+)"')
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")
_CSS_IMPORT_RE = re.compile(r"""@import\s+(?!url\()(['"])([^'"]+)\1""")
# A referenced asset past this is not an image or a stylesheet.
_MAX_FETCH_BYTES = 64 * 1024 * 1024


def _file_url(container_path: str) -> str:
    """The ``file:`` URL of a resolved path, percent-encoded once (the
    fetcher decodes it exactly once)."""
    from urllib.request import pathname2url

    return "file://" + pathname2url(container_path)


_FILE_SCHEME = "file"


def _allowed_files_fetcher(allowed):
    """WeasyPrint's fetcher restricted to ``data:`` URLs and the ``file:``
    URLs of ``allowed`` (the paths the proxy resolved for this session);
    anything else raises, which WeasyPrint reports as a missing resource and
    renders without. A ``file:`` URL is decoded once, the way ``_file_url``
    encoded it, and the bytes come from a no-follow read beneath the mount,
    so the path that is checked is the path that is read. Built inside the
    worker (a closure cannot cross the spawn boundary; the allowed list
    can)."""
    import mimetypes
    from urllib.parse import urlsplit
    from urllib.request import url2pathname

    from weasyprint.urls import URLFetcher, URLFetcherResponse, URLFetchingError

    allowed_paths = {os.path.normpath(p) for p in allowed}

    class _AllowedFilesFetcher(URLFetcher):
        def __init__(self):
            super().__init__(allowed_protocols={"file", "data"}, allow_redirects=False)

        def fetch(self, url, headers=None):
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme == "data":
                return super().fetch(url, headers)
            if scheme == _FILE_SCHEME and not parts.netloc:
                path = os.path.normpath(url2pathname(parts.path))
                if path in allowed_paths:
                    root, rel = _rel_beneath_root(path)
                    data = safe_fs.read_bytes_beneath(root, rel, max_size=_MAX_FETCH_BYTES)
                    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
                    return URLFetcherResponse(url, body=data, headers={"Content-Type": mime})
            raise URLFetchingError("external resources are not fetched")

    return _AllowedFilesFetcher()


def _render_pdf_core(full_html: str, path: str, allowed=(), form_fields: bool = False) -> None:
    """Worker core: WeasyPrint layout/render (the memory hazard) with the
    fetcher limited to ``allowed`` and an atomic save beneath the mount (a
    killed child never truncates an existing PDF; a link at the name or on
    the way never redirects the save). With ``form_fields`` the HTML inputs
    become fillable fields."""
    from weasyprint import HTML

    fetcher = _allowed_files_fetcher(allowed)
    if not form_fields:
        with safe_open_write(path) as fh:
            HTML(string=full_html, url_fetcher=fetcher, base_url=None).write_pdf(fh)
        return
    data = HTML(string=full_html, url_fetcher=fetcher, base_url=None).write_pdf(pdf_forms=True)
    with safe_open_write(path) as fh:
        fh.write(_named_radio_groups(data))


def _named_radio_groups(data: bytes) -> bytes:
    """WeasyPrint names each radio button like its group, so a group reads
    as 'size.size'; the buttons' own names are dropped and the group answers
    to 'size'."""
    import fitz

    doc = fitz.open("pdf", data)
    try:
        changed = False
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_type != fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                    continue
                kind, parent = doc.xref_get_key(widget.xref, "Parent")
                if kind != "xref":
                    continue
                parent_xref = int(parent.split()[0])
                if doc.xref_get_key(widget.xref, "T") == doc.xref_get_key(parent_xref, "T"):
                    doc.xref_set_key(widget.xref, "T", "null")
                    doc.xref_set_key(widget.xref, "TU", "null")
                    changed = True
        return doc.tobytes(garbage=1) if changed else data
    finally:
        doc.close()


def _render_temp_pdf_core(full_html: str, path: str, allowed=()) -> None:
    """Worker core: the same render into a file of a private temp directory
    outside the tree (screenshot_document's intermediate PDF)."""
    from weasyprint import HTML

    fetcher = _allowed_files_fetcher(allowed)
    HTML(string=full_html, url_fetcher=fetcher, base_url=None).write_pdf(path)


# A reference that names a scheme (``http:``, ``file:``, ``data:``, a drive
# letter) is never joined to a base folder.
_URL_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def _anchored(ref: str, base_dir: str | None) -> str:
    """``ref`` joined to ``base_dir`` when it is a relative path: the
    references of a converted file name files beside it. The joined path is
    resolved through the proxy like any other, so a ``..`` that leaves the
    session's tree resolves to nothing."""
    if not base_dir or ref.startswith("/") or _URL_SCHEME_RE.match(ref):
        return ref
    return os.path.normpath(os.path.join(base_dir, ref))


async def _resolve_references(*texts: str, base_dir: str | None = None) -> dict[str, str]:
    """``{reference: resolved container path}`` for every ``src``, CSS
    ``url()`` and ``@import`` in ``texts`` that the proxy resolves for this
    session; a ``data:`` URL is left to the fetcher, an unresolved reference
    is left out (and so never fetched). With ``base_dir`` a relative
    reference is resolved against that folder."""
    refs: set[str] = set()
    for text in texts:
        refs.update(_SRC_RE.findall(text))
        refs.update(m[1] for m in _CSS_URL_RE.findall(text))
        refs.update(m[1] for m in _CSS_IMPORT_RE.findall(text))
    out: dict[str, str] = {}
    for ref in refs:
        if ref.lower().startswith("data:"):
            continue
        with contextlib.suppress(Exception):
            out[ref] = await _resolve_path(_anchored(ref, base_dir))
    return out


def _rewrite_references(text: str, resolved: dict[str, str]) -> str:
    """Point every resolved reference at its ``file:`` URL; the rest stay as
    written (the fetcher refuses them)."""
    def _src(match):
        r = resolved.get(match.group(1))
        return f'src="{_file_url(r)}"' if r else match.group(0)

    def _url(match):
        r = resolved.get(match.group(2))
        return f'url("{_file_url(r)}")' if r else match.group(0)

    def _imp(match):
        r = resolved.get(match.group(2))
        return f'@import url("{_file_url(r)}")' if r else match.group(0)

    text = _SRC_RE.sub(_src, text)
    text = _CSS_URL_RE.sub(_url, text)
    return _CSS_IMPORT_RE.sub(_imp, text)


async def handle_write_pdf(args: dict) -> str:
    path = await _resolve_path(args["path"], writing=True)
    return f"PDF created: {_to_agents_relative(path)}" + await _write_pdf(path, args)


async def _pdf_html(args: dict, *, base_dir: str | None = None) -> tuple[str, list[str], int, list[str]]:
    """``(full HTML, files the fetcher may read, equations rendered,
    equation errors)`` of write_pdf's ``content``/``css``/``page_size``/
    ``margins``. With ``base_dir`` a relative reference names a file in that
    folder (a converted file's own)."""
    import markdown as md

    content = args.get("content", "")
    content_type = args.get("content_type", "markdown")
    custom_css = args.get("css", "")
    page_size = args.get("page_size", "A4")
    margins = args.get("margins", {})

    content, math_spans = _extract_math_spans(
        content, is_markdown=content_type == "markdown"
    )

    if content_type == "markdown":
        html_body = md.markdown(
            content, extensions=["tables", "fenced_code", "nl2br"]
        )
    else:
        html_body = content

    # Pre-resolve every reference in the parent (the proxy hop is async);
    # the worker's fetcher then admits exactly these files.
    src_map = await _resolve_references(html_body, custom_css, base_dir=base_dir)
    html_body = _rewrite_references(html_body, src_map)
    custom_css = _rewrite_references(custom_css, src_map)
    allowed = sorted(set(src_map.values()))

    # After the src= rewriter — injected SVG markup must never meet it
    math_errors: list[str] = []
    html_body = _render_math_spans(html_body, math_spans, math_errors)

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
    return full_html, allowed, len(math_spans) - len(math_errors), math_errors


async def _write_pdf(path: str, args: dict, *, base_dir: str | None = None) -> str:
    """Render write_pdf's ``args`` into the resolved write target ``path``
    and push its preview. Returns the result text's tail (the equation
    count and warnings) for the caller's first line."""
    # Assembly is cheap and needs the async resolver; the WeasyPrint render
    # is the memory hazard and runs in a bounded worker child.
    full_html, allowed, rendered_eqs, math_errors = await _pdf_html(args, base_dir=base_dir)
    try:
        forms = {"form_fields": True} if args.get("form_fields") else {}
        await run_parse(_render_pdf_core, full_html, path, allowed, _advice=_WRITE_PDF_ADVICE, **forms)
    except Exception:
        cleanup_partials(path)
        raise
    await _push_preview(path)
    msg = ""
    if rendered_eqs:
        msg += f" ({rendered_eqs} equation(s) rendered)"
    if math_errors:
        msg += (
            f"\n\nEquation warnings ({len(math_errors)}) — these spans were left "
            "as plain text:\n" + "\n".join(f"  - {e}" for e in math_errors)
        )
    return msg


async def handle_edit_pdf(args: dict) -> str:
    """edit_pdf; its operations and write live in formats/pdf/edit.py."""
    return await _edit.handle_edit_pdf(args)


# ---------------------------------------------------------------------------
# PDF to Images
# ---------------------------------------------------------------------------


_RENDER_ADVICE = "Render fewer pages per call (`pages`) or lower `dpi`"


def _pdf_to_images_core(path: str, tmp_dir: str, page_spec, dpi: int,
                        fmt: str, password: str | None = None) -> list[dict]:
    """Worker core: render every requested page into the parent-named temp
    dir. The parent moves the COMPLETE set into the real output dir only on
    success — a deadline kill must not leave a partial page set that looks
    complete on `ls`. Unbounded pixmaps (degenerate page geometry × dpi) are
    exactly what the worker's RLIMIT_AS is for."""
    import fitz

    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir)

    from formats.pdf.edit import copy_refusal, protection_of

    doc = open_pdf(path, password)
    protected = protection_of(doc)
    owner = bool(protected) and password not in (None, "") and bool(doc.authenticate(str(password)) & 4)
    refusal = copy_refusal(protected, owner, doc.permissions)
    if refusal:
        doc.close()
        raise ValueError(f"{refusal[0].upper()}{refusal[1:]}: give the owner password (password).")
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    saved = []
    for pi in parse_pages(page_spec, doc.page_count):
        page = doc[pi]
        pix = page.get_pixmap(matrix=mat)
        fname = f"page_{pi + 1:03d}.{fmt}"
        out_path = os.path.join(tmp_dir, fname)
        if fmt in ("jpg", "jpeg"):
            pix.save(out_path, jpg_quality=95)
        else:
            pix.save(out_path)
        saved.append({"name": fname, "width": pix.width, "height": pix.height})
    doc.close()
    return saved


def _save_pdf_beneath(doc, container_path: str, **save_kwargs) -> None:
    """Save a PyMuPDF document at ``container_path`` beneath the mount: the
    library writes by name into a temp outside the tree (its file-object
    output is not usable on the pinned build), and the bytes land through
    the write helper. The temp's name is derived from the output so the
    parent removes it after a kill this ``finally`` never sees."""
    tmp = worker_temp_path(container_path, ".pdf")
    try:
        doc.save(tmp, **save_kwargs)
        _copy_beneath(tmp, container_path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _copy_beneath(src_path: str, container_path: str) -> None:
    """Copy a file made outside the tree (a temp) to ``container_path``
    beneath the mount through the write helper."""
    with open(src_path, "rb") as src, safe_open_write(container_path) as fh:
        shutil.copyfileobj(src, fh)


def _land_files(src_dir: str, out_dir: str, names: list[str]) -> None:
    """Copy the files ``names`` of a temp directory into ``out_dir`` beneath
    the mount through the write helper (the folder created beneath it too)."""
    safe_mkdirs(out_dir)
    for name in names:
        with open(os.path.join(src_dir, name), "rb") as src, \
                safe_open_write(os.path.join(out_dir, name)) as fh:
            shutil.copyfileobj(src, fh)


async def handle_pdf_to_images(args: dict) -> str:
    path = await _resolve_path(args["path"])
    if not await asyncio.to_thread(os.path.exists, path):
        return f"Error: File not found: {args['path']}"

    output_dir = args.get("output_dir")
    if not output_dir:
        output_dir = _write_form(str(Path(path).parent / (Path(path).stem + "_pages")))
    out_dir = await _resolve_path(output_dir, writing=True)

    page_spec = args.get("pages", "all")
    dpi = int(args.get("dpi", 150))
    fmt = args.get("format", "png").lower()
    saved = await _pdf_to_images(path, out_dir, page_spec, dpi, fmt, args.get("password"))
    return (
        f"Rendered {len(saved)} pages from {_to_agents_relative(path)} at {dpi}dpi.\n"
        f"Output: {_to_agents_relative(out_dir)}/\n"
        f"Files: {', '.join(s['name'] for s in saved[:5])}"
        + (f" ... +{len(saved) - 5} more" if len(saved) > 5 else "")
    )


async def _pdf_to_images(path: str, out_dir: str, page_spec, dpi: int, fmt: str,
                         password: str | None = None) -> list:
    """Render ``path``'s pages into the resolved write target ``out_dir``
    and push the first one inline. Returns the saved page entries."""
    # The pages render into a temp directory outside the tree and land in
    # the output folder beneath the mount, each through the write helper.
    tmp_dir = tempfile.mkdtemp(prefix="file-tools-pages-")
    try:
        saved = await run_parse(
            _pdf_to_images_core, path, tmp_dir, page_spec, dpi, fmt, password,
            _advice=_RENDER_ADVICE,
        )
        first = (Path(tmp_dir) / saved[0]["name"]).read_bytes() if saved else b""
        await asyncio.to_thread(_land_files, tmp_dir, out_dir, [s["name"] for s in saved])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Push first page inline for preview
    if saved:
        mime = "image/png" if fmt == "png" else "image/jpeg"
        await _push_image_preview(first, mime, f"Page 1 of {Path(path).name}")
    return saved


# ---------------------------------------------------------------------------
# Screenshot Document (visual feedback for LLM)
# ---------------------------------------------------------------------------

_SCREENSHOT_EXTS = {
    ".pdf", ".docx", ".docm", ".dotx", ".dotm", ".doc",
    ".xlsx", ".xlsm", ".xltx", ".xltm", ".xls",
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".ppt",
    ".odt", ".ods", ".odp", ".csv", ".rtf", ".html", ".htm",
}
_MAX_INLINE_PAGES = 10
# Payload caps (see handle_screenshot_document): long-edge px per page
# (dpi-aware — the high cap keeps A4@300 ≈ 3508 px untouched, the skill's
# documented handwriting remedy), PNG→JPEG size fallback, and the per-call
# total across all pages (truncates with a note, never silently).
_SCREENSHOT_LONG_EDGE_STD = 2000
_SCREENSHOT_LONG_EDGE_HIGH = 3600
_SCREENSHOT_PNG_FALLBACK_BYTES = int(1.5 * 1024 * 1024)
_SCREENSHOT_TOTAL_BYTES = 8 * 1024 * 1024


def _render_screenshot_core(pdf_path: str, pages_spec, dpi: int, password: str | None = None) -> dict:
    """Worker core: the fitz render loop with the payload caps below.
    Returns raw bytes — base64 and mcp.types stay in the parent (they never
    cross the pipe).

    Vision-sized payloads: the 300 s timeouts this tool hit were the
    RESPONSE path for multi-MB base64, never rendering (the same page
    renders in under a second) — so cap pixels and bytes here. The long-edge
    cap is dpi-aware: a flat cap would bite exactly on the explicit dpi:300
    calls the skill mandates for handwriting (A4@300 ≈ 3508 px passes
    untouched). Encoding follows CONTENT: pages with a text layer stay PNG
    (JPEG ringing is worst on crisp text) with a byte-size JPEG fallback;
    scanned pages go straight to JPEG q85. A total budget truncates the
    batch with a note."""
    import fitz

    zoom = dpi / 72.0
    doc = open_pdf(pdf_path, password)
    total = len(doc)
    page_indices = parse_pages(pages_spec, total)
    if not page_indices:
        doc.close()
        return {"total": total, "images": [], "rendered": [],
                "capped": False, "truncated_at": None}

    capped = False
    if len(page_indices) > _MAX_INLINE_PAGES:
        page_indices = page_indices[:_MAX_INLINE_PAGES]
        capped = True

    long_edge_cap = (
        _SCREENSHOT_LONG_EDGE_STD if dpi <= 200 else _SCREENSHOT_LONG_EDGE_HIGH
    )
    images: list = []
    rendered: list = []
    total_bytes = 0
    truncated_at = None
    for pi in page_indices:
        page = doc[pi]
        long_edge = max(page.rect.width, page.rect.height) * zoom
        eff_zoom = zoom if long_edge <= long_edge_cap else (
            zoom * (long_edge_cap / long_edge)
        )
        pix = page.get_pixmap(matrix=fitz.Matrix(eff_zoom, eff_zoom))
        scanned = not page.get_text().strip()
        if scanned:
            img_bytes = pix.tobytes("jpg", jpg_quality=85)
            mime = "image/jpeg"
        else:
            img_bytes = pix.tobytes("png")
            mime = "image/png"
            if len(img_bytes) > _SCREENSHOT_PNG_FALLBACK_BYTES:
                img_bytes = pix.tobytes("jpg", jpg_quality=85)
                mime = "image/jpeg"
        if images and total_bytes + len(img_bytes) > _SCREENSHOT_TOTAL_BYTES:
            truncated_at = pi + 1
            break
        total_bytes += len(img_bytes)
        images.append({"data": img_bytes, "mime": mime})
        rendered.append({
            "page": pi + 1,
            "width_in": round(page.rect.width / 72, 2),
            "height_in": round(page.rect.height / 72, 2),
        })

    doc.close()
    return {"total": total, "images": images, "rendered": rendered,
            "capped": capped, "truncated_at": truncated_at}


async def _contact_sheet(pdf_path: str, src_path: str, pages_spec, password: str | None) -> tuple[dict, str]:
    """The contact sheet of the rendered pages and its text."""
    from formats.pdf import screenshot as shot

    sheet = await run_parse(shot.contact_sheet_core, pdf_path, pages_spec, src_path, password,
                            _advice=_RENDER_ADVICE)
    if "error" in sheet:
        return {}, sheet["error"]
    pages, rest, total = sheet["pages"], sheet["rest"], sheet["total"]
    named = ("slide_index (from 0, as write_pptx takes it; hidden slides are not drawn)" if sheet["slides"]
             else "page number (as pages counts)")
    lines = [f"Contact sheet of {Path(src_path).name}: {len(pages)} of {total} page(s) "
             f"({_page_ranges(pages)}), {sheet['columns']} per row, each labelled with its {named}.",
             "To look closer, call again without grid and with pages set to the page you want."]
    if rest:
        lines.append(f"(the sheet holds {shot.MAX_SHEET_PAGES} pages: ask for pages "
                     f"'{rest[0] + 1}-{rest[-1] + 1}' for the rest)")
    return sheet, "\n".join(lines)


async def handle_screenshot_document(args: dict) -> list:
    """Render document pages as ImageContent for LLM visual inspection.

    Returns list of ImageContent + TextContent (not pushed to dashboard).
    """
    import base64
    import tempfile

    from mcp.types import ImageContent, TextContent

    from formats.pdf import screenshot as shot

    path = await _resolve_path(args.get("path", ""))
    if not await asyncio.to_thread(os.path.isfile, path):
        return [TextContent(type="text", text=f"Error: file not found: {_to_agents_relative(path)}")]

    ext = Path(path).suffix.lower()
    if ext not in _SCREENSHOT_EXTS:
        return [TextContent(type="text",
                text=f"Error: unsupported format '{ext}'. Supported: PDF, DOCX, XLSX, PPTX, other Office formats and HTML.")]

    grid = bool(args.get("grid", False))
    pages_spec = args.get("pages", "all" if grid else "1")
    dpi = int(args.get("dpi", 150))
    sheet = args.get("sheet")
    password = args.get("password") if ext == ".pdf" else None
    notes: list[str] = []

    pdf_path = path
    temp_dir = None

    try:
        # HTML: write_pdf's renderer into a temp PDF (its fetcher reads only
        # the files the proxy resolves; a relative reference names a file
        # beside the input), never LibreOffice.
        if ext in _HTML_EXTS:
            temp_dir = tempfile.mkdtemp(prefix="file-tools-shot-")
            content = await asyncio.to_thread(_read_source, path, html=True)
            full_html, allowed, _eqs, _errors = await _pdf_html(
                {"content": content, "content_type": "html"},
                base_dir=str(Path(path).parent),
            )
            pdf_path = os.path.join(temp_dir, Path(path).stem + ".pdf")
            await run_parse(
                _render_temp_pdf_core, full_html, pdf_path, allowed,
                _advice=_RENDER_ADVICE,
            )
        # Other non-PDF formats: a temp PDF from render_engine
        elif ext != ".pdf":
            temp_dir = tempfile.mkdtemp(prefix="file-tools-shot-")
            convert_path = path

            # A workbook prints from a copy laid out to fit the page width;
            # the copy keeps every cell and stored value (a recalculated
            # workbook shows its values). With a sheet, the copy prints that
            # sheet alone, so pages count within it.
            if ext in shot.XLSX_EXTS:
                made = await run_parse(shot.workbook_core, path, temp_dir, sheet, _advice=_RENDER_ADVICE)
                if made.get("error"):
                    return [TextContent(type="text", text=f"Error: {made['error']}.")]
                if made["path"] is None:
                    logger.warning(f"screenshot workbook prep failed: {made['reason']}, using original")
                    if sheet is not None:
                        notes.append("The workbook could not be laid out, so sheet did not apply: every sheet "
                                     "prints.")
                else:
                    convert_path = made["path"]
                    if sheet is not None:
                        notes.append(f"Sheet {made['names'][made['index']]!r} (index {made['index']}); pages count "
                                     "within it.")
            elif sheet is not None and ext in (".ods", ".csv"):
                # ODS/CSV print every sheet; a number picks that page, a name
                # cannot be told apart.
                if isinstance(sheet, int) or str(sheet).isdigit():
                    pages_spec = str(int(sheet) + 1)
                else:
                    notes.append(f"sheet by name does not apply to {ext}: give its number.")
            elif sheet is not None:
                notes.append(f"sheet applies to workbooks; it was ignored for {ext}.")

            # In a folder of its own: the laid-out workbook copy lives in
            # temp_dir, and the PDF keeps its name.
            pdf_path = await render_engine.to_pdf(
                convert_path, out_dir=await asyncio.to_thread(render_engine.output_folder, temp_dir, "pdf"))

        if grid:
            made, text = await _contact_sheet(pdf_path, path, pages_spec, password)
            if not made:
                return [TextContent(type="text", text=text)]
            if notes:
                text += "\n" + "\n".join(notes)
            return [ImageContent(type="image", data=base64.b64encode(made["data"]).decode(), mimeType=made["mime"]),
                    TextContent(type="text", text=text)]

        # Render in a bounded worker child (fitz pixmaps are the memory
        # hazard); the parent only b64-encodes and assembles content items.
        render = await run_parse(
            _render_screenshot_core, pdf_path, pages_spec, dpi, password,
            _advice=_RENDER_ADVICE,
        )

        if not render["rendered"]:
            return [TextContent(
                type="text",
                text=f"No valid pages to render (document has {render['total']} pages).",
            )]

        content_items: list = []
        for img in render["images"]:
            content_items.append(ImageContent(
                type="image",
                data=base64.b64encode(img["data"]).decode(),
                mimeType=img["mime"],
            ))

        # Append text summary after images
        fname = Path(path).name
        lines = [f"Rendered {len(render['rendered'])} page(s) from {fname} at {dpi} DPI:"]
        for r in render["rendered"]:
            lines.append(f"  Page {r['page']}: {r['width_in']} x {r['height_in']} inches")
        if render["capped"]:
            lines.append(f"  (capped at {_MAX_INLINE_PAGES} pages — use specific page ranges for more, "
                         "or grid: true for every page on one sheet)")
        if render["truncated_at"] is not None:
            lines.append(
                f"  (stopped before page {render['truncated_at']}: inline image budget "
                "reached — request the remaining pages in a follow-up call)"
            )
        lines.extend(notes)
        content_items.append(TextContent(type="text", text="\n".join(lines)))
        return content_items

    except Exception as e:  # noqa: BLE001  # the tool answers with the error text whatever failed
        return [TextContent(type="text", text=f"Error rendering {_to_agents_relative(path)}: {e}")]
    finally:
        if temp_dir:
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Images to PDF
# ---------------------------------------------------------------------------


def _images_to_pdf_core(image_paths: list, out: str, page_size: str,
                        fit: str) -> int:
    """Worker core: build the PDF from pre-resolved, existing image paths
    and save atomically. A bomb image (fitz decodes it for dimensions and
    embedding) dies at the child's limit, not the server's."""
    import fitz

    # Page dimensions in points
    sizes = {
        "a4": (595, 842),
        "letter": (612, 792),
        "a3": (842, 1190),
    }

    doc = fitz.open()

    for img_path in image_paths:
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

    inserted = doc.page_count
    try:
        _save_pdf_beneath(doc, out)
    finally:
        doc.close()
    return inserted


async def handle_images_to_pdf(args: dict) -> str:
    images = args.get("images", [])
    output_path = args.get("output_path")
    if not output_path:
        return "Error: output_path is required"
    if not images:
        return "Error: images list is empty"

    out = await _resolve_path(output_path, writing=True)

    page_size = args.get("page_size", "a4").lower()
    fit = args.get("fit", "contain")

    # Pre-resolve in the parent; a missing image is skipped and named (the
    # old per-image containment), it never fails the call.
    image_paths = []
    skipped: list[str] = []
    for img_input in images:
        try:
            img_path = await _resolve_path(img_input)
        except Exception as exc:  # noqa: BLE001  # an input that cannot be resolved is skipped, the rest still render
            logger.warning(f"images_to_pdf: skipping {img_input} ({exc})")
            skipped.append(f"{img_input} (refused: {exc})")
            continue
        if not await asyncio.to_thread(os.path.exists, img_path):
            logger.warning(f"images_to_pdf: skipping {img_input} (not found)")
            skipped.append(f"{img_input} (not found)")
            continue
        image_paths.append(img_path)
    if not image_paths:
        return "Error: none of the images could be read: " + "; ".join(skipped)

    placed = await _images_to_pdf(image_paths, out, page_size, fit)
    text = f"PDF created: {_to_agents_relative(out)} ({placed} images, {page_size})"
    if skipped:
        text += "\nSkipped: " + "; ".join(skipped)
    return text


async def _images_to_pdf(image_paths: list, out: str, page_size: str, fit: str) -> int:
    """Build the PDF of the resolved, existing ``image_paths`` at the
    resolved write target ``out`` and push its preview; returns how many
    images it placed."""
    try:
        placed = await run_parse(
            _images_to_pdf_core, image_paths, out, page_size, fit,
            _advice=_RENDER_ADVICE,
        )
    except Exception:
        cleanup_partials(out)
        raise
    await _push_preview(out)
    return placed


# ---------------------------------------------------------------------------
# Convert document
# ---------------------------------------------------------------------------


_MARKDOWN_EXTS = (".md", ".markdown")
_HTML_EXTS = (".html", ".htm")


def _write_form(container_path: str) -> str:
    """A resolved path in the session's agent tree as the sandbox path the
    proxy resolves as a write (``/agents/<slug>/users/u/x`` becomes
    ``/users/u/x``). ``_resolve_path`` hands the proxy a container path in
    its agents-relative form, which the proxy resolves for reads only. A
    path outside the mount or in the host cache (no sandbox form) is
    returned unchanged."""
    if not container_path.startswith(MOUNT_AGENTS_DIR + "/"):
        return container_path
    slug, _, rest = container_path[len(MOUNT_AGENTS_DIR) + 1:].partition("/")
    return container_path if slug == HOST_CACHE_SEGMENT else "/" + rest


# A converted or screenshotted Markdown or HTML file is read whole in the
# parent, under the cap a render reads any one file with.
_MAX_SOURCE_BYTES = _MAX_FETCH_BYTES
# BOMs, the four-byte ones before the UTF-16 ones they begin with.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
)
# ``<meta charset=...>`` and ``<meta http-equiv="Content-Type"
# content="...; charset=...">`` alike, in the first 1024 bytes (where a
# browser's prescan looks).
_META_CHARSET_RE = re.compile(rb"""<meta[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)


def _decode_source(data: bytes, *, html: bool) -> str:
    """``data`` decoded by its BOM, else (HTML) by the charset its ``<meta>``
    declares, else as UTF-8; an undecodable byte is replaced, never fatal."""
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return data.decode(codec, errors="replace")
    encoding = "utf-8"
    match = _META_CHARSET_RE.search(data[:1024]) if html else None
    if match:
        try:
            declared = codecs.lookup(match.group(1).decode("ascii")).name
        except LookupError:
            declared = ""
        # A declaration readable as ASCII rules a UTF-16 or UTF-32 body out.
        if declared and not declared.startswith(("utf-16", "utf-32")):
            encoding = declared
    return data.decode(encoding, errors="replace")


def _read_source(container_path: str, *, html: bool) -> str:
    """The text of a Markdown or HTML input: read beneath the mount with no
    link followed, refused past ``_MAX_SOURCE_BYTES``, decoded by
    ``_decode_source``. Blocking: run it in a thread."""
    root, rel = _rel_beneath_root(container_path)
    try:
        data = safe_fs.read_bytes_beneath(root, rel, max_size=_MAX_SOURCE_BYTES)
    except safe_fs.FileTooLarge:
        raise ValueError(
            f"{Path(container_path).name} is over the {_MAX_SOURCE_BYTES // (1024 * 1024)} MB "
            "a document render reads: split it into smaller files"
        ) from None
    return _decode_source(data, html=html)


async def handle_convert_document(args: dict) -> str:
    input_path = await _resolve_path(args["input_path"])
    if not await asyncio.to_thread(os.path.exists, input_path):
        return f"Error: File not found: {args['input_path']}"

    output_format = args.get("output_format", "pdf")
    output_path = args.get("output_path")
    ext = Path(input_path).suffix.lower()

    # Markdown and HTML → PDF through write_pdf's renderer, whose fetcher
    # reads only the files the proxy resolves for this session (LibreOffice
    # would load an HTML file's linked pictures from any path it can read).
    # An HTML file's relative references name files beside it. The output
    # is output_path, else the input's name with .pdf beside it, resolved
    # once as a write.
    if ext in _MARKDOWN_EXTS + _HTML_EXTS and output_format == "pdf":
        is_html = ext in _HTML_EXTS
        content = await asyncio.to_thread(_read_source, input_path, html=is_html)
        out = await _resolve_path(
            output_path or _write_form(str(Path(input_path).with_suffix(".pdf"))),
            writing=True,
        )
        tail = await _write_pdf(
            out,
            {"content": content, "content_type": "html" if is_html else "markdown"},
            base_dir=str(Path(input_path).parent) if is_html else None,
        )
        return f"Converted: {_to_agents_relative(out)}" + tail

    # Every other output is likewise output_path itself, else derived from
    # the input's own path in sandbox form, resolved once as a write.
    beside = str(Path(input_path).with_suffix(""))

    # Image → PDF via pymupdf
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp") and output_format == "pdf":
        out = await _resolve_path(output_path or _write_form(beside + ".pdf"), writing=True)
        await _images_to_pdf([input_path], out, "original", "contain")
        return f"Converted: {_to_agents_relative(out)}"

    # PDF → PNG via pymupdf, into a folder: output_path, else one named
    # after the input beside it
    if ext == ".pdf" and output_format in ("png", "jpg", "jpeg"):
        out_dir = await _resolve_path(output_path or _write_form(beside), writing=True)
        await _pdf_to_images(input_path, out_dir, "all", 150, output_format)
        return f"Converted: {_to_agents_relative(out_dir)}/"

    # render_engine for everything else (the install's Collabora, else
    # LibreOffice), in the server process: neither engine runs in a worker
    # child, whose RLIMIT_AS would follow soffice. It writes by name into a
    # temp directory outside the tree; the result lands in the output folder
    # beneath the mount through the write helper. An output_path is resolved
    # (and refused) before the conversion runs; the default takes the name
    # the engine gives its result.
    out = await _resolve_path(output_path, writing=True) if output_path else None
    lo_dir = tempfile.mkdtemp(prefix="file-tools-lo-")
    try:
        try:
            produced = await render_engine.convert(input_path, output_format, out_dir=lo_dir)
        except RuntimeError as exc:
            return f"Conversion error: {exc}"
        if out is None:
            out = await _resolve_path(
                _write_form(str(Path(input_path).parent / Path(produced).name)), writing=True,
            )
        await asyncio.to_thread(_copy_beneath, produced, out)
    finally:
        shutil.rmtree(lo_dir, ignore_errors=True)

    await _push_preview(out)
    return f"Converted: {_to_agents_relative(out)}"
