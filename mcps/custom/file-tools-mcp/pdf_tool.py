"""The PDF and image tools, and the PDF reader of read_document.

The assembler (``server.py``) lists every format module's tools, routes each
tool to its handler and each file extension to its reader. This module
holds definitions and thin adapters only and never imports its format module
at top level: startup stays fast, and a worker that unpickles a reader
imports only what that reader needs.
"""

from mcp.types import Tool, ToolAnnotations

from kept_batches import FIX_PROPERTY


def read(path: str, options: dict) -> str:
    """read_document for PDF (runs in the worker)."""
    from pdf import read_pdf

    return read_pdf(path, options.get("pages"), mode=options.get("mode") or "text",
                    password=options.get("password"))


TOOLS = [
    Tool(
        name="write_pdf",
        description=(
            "Create a PDF from HTML or Markdown content. Supports embedded images (via file paths), "
            "custom CSS, page size (A4/Letter), and margins. Auto-previews in the dashboard.\n\n"
            "MATH: LaTeX equations render as publication-quality vector math. "
            "Delimiters: \\( ... \\) inline, \\[ ... \\] or $$ ... $$ display "
            "(preferred). Single-$ spans work in markdown under strict adjacency "
            "rules (no space inside the $s, closing $ not before a digit — dollar "
            "amounts like $100 are never treated as math); code blocks are never "
            "scanned. A failed equation is left as plain text and reported.\n\n"
            "FORMS: form_fields: true turns HTML inputs (text, textarea, checkbox, radio, select) into "
            "fillable PDF fields, named by their name attribute."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path for the output .pdf file",
                },
                "content": {
                    "type": "string",
                    "description": "HTML or Markdown content",
                },
                "content_type": {
                    "type": "string",
                    "enum": ["html", "markdown"],
                    "default": "markdown",
                },
                "css": {
                    "type": "string",
                    "description": "Optional custom CSS to add",
                },
                "page_size": {
                    "type": "string",
                    "enum": ["A4", "Letter"],
                    "default": "A4",
                },
                "form_fields": {
                    "type": "boolean",
                    "description": "Make the HTML inputs fillable PDF form fields (default false).",
                },
                "margins": {
                    "type": "object",
                    "description": 'Page margins, e.g. {"top": "2cm", "bottom": "2cm"}',
                    "properties": {
                        "top": {"type": "string"},
                        "bottom": {"type": "string"},
                        "left": {"type": "string"},
                        "right": {"type": "string"},
                    },
                },
            },
            "required": ["path", "content"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
    Tool(
        name="convert_document",
        description=(
            "Convert a document between formats. Common conversions: "
            "DOCX->PDF, XLSX->PDF, PPTX->PDF, CSV->XLSX. "
            "Markdown->PDF and HTML->PDF use the built-in renderer: an HTML file's "
            "pictures load only from this session's folders (a relative path names "
            "a file beside the HTML file), never from the web. Auto-previews the output."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "Absolute path to the source file",
                },
                "output_format": {
                    "type": "string",
                    "description": "Target format: pdf, docx, xlsx, pptx, html, csv, txt",
                },
                "output_path": {
                    "type": "string",
                    "description": "Optional output path. Defaults to same directory with new extension.",
                },
            },
            "required": ["input_path", "output_format"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
    Tool(
        name="edit_pdf",
        description=(
            "Edit an existing PDF with a batch of operations, applied in order and saved once. "
            "Atomic: if any operation fails, nothing is saved and every failure is listed; pass "
            "best_effort: true to keep the operations that worked, or fix to resend the kept batch with only the "
            "corrections. {\"op\": \"help\"} lists every operation "
            "with its keys; {\"op\": \"help\", \"name\": \"fill_form\"} shows one.\n\n"
            "PAGES: merge, split (pages to a new PDF), rotate_page, delete_page, reorder_pages, insert_page, "
            "crop_page.\n"
            "CONTENT: add_text (page, text, x, y, font_size, color, font: an embedded font, so Greek shows), "
            "add_image, add_watermark, add_annotation, replace_text ({find, replace}: redacts each match and "
            "writes the replacement in a matching font; replace '' deletes), redact ({find} or {rect}: removes "
            "the content for good).\n"
            "FORMS: fill_form ({values: {field name: value}, flatten?}: read_document with mode form_fields "
            "lists the names, kinds and options; text in any script, true/false for check boxes), "
            "flatten_form, fill_flat_form ({entries: [{label, text} or {page, x, y, text}]}: a printed form "
            "without fields, text placed beside its labels).\n"
            "SECURITY: encrypt (AES-256, user/owner password, permissions), decrypt (removes the password). Removing "
            "or replacing a protection needs the owner password.\n"
            "OPTIMIZE: compress. METADATA: set_metadata.\n"
            "EXTRACT: extract_images (to a folder), ocr ({language, pages?, dpi?, output_path?}: makes scanned "
            "pages searchable; set language to the document's, e.g. 'ell+eng' for Greek; printed text only, "
            "read handwriting through screenshot_document).\n\n"
            "Pages: page and numbers in a list count from 0, text such as '1-3' from 1. Coordinates are "
            "points from the page's top left. A password-protected PDF opens with password and stays "
            "protected. Auto-previews in the dashboard after saving."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the PDF file to edit",
                },
                "operations": {
                    "type": "array",
                    "description": "List of operations to apply",
                    "items": {"type": "object"},
                },
                "best_effort": {
                    "type": "boolean",
                    "description": "Save the operations that worked even when others fail (default false: "
                                   "one failure saves nothing).",
                },
                "password": {
                    "type": "string",
                    "description": "The password of a protected PDF; it stays protected after the edit.",
                },
                "fix": FIX_PROPERTY,
            },
            "required": ["path"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
    Tool(
        name="pdf_to_images",
        description=(
            "Render PDF pages as PNG or JPEG images. Useful for visually inspecting "
            "a PDF, creating thumbnails, or extracting page visuals. "
            "The first page is automatically shown inline in the chat."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the PDF",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Directory to save page images. Default: same dir with _pages suffix.",
                },
                "pages": {
                    "type": "string",
                    "description": "Pages to render: 'all', '1-5', '1,3,5'. Default: all.",
                    "default": "all",
                },
                "dpi": {
                    "type": "integer",
                    "description": "Resolution in DPI. Default 150.",
                    "default": 150,
                },
                "format": {
                    "type": "string",
                    "description": "Output format: png or jpg. Default png.",
                    "default": "png",
                },
                "password": {
                    "type": "string",
                    "description": "The password of a protected PDF.",
                },
            },
            "required": ["path"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
    Tool(
        name="screenshot_document",
        description=(
            "Render document pages as images for visual inspection. "
            "Use this to SEE what a document looks like after creating or editing it. "
            "Supports PDF, DOCX, XLSX, PPTX (templates and macro files too) and other Office formats, and HTML "
            "(rendered by the built-in renderer, as convert_document does). "
            "The pages come back to you only, so you can review layout, sizing and content: the person does not "
            "see them. To show the person a document, use preview_document.\n\n"
            "Workflow: create/edit document → screenshot_document → review layout → adjust if needed.\n"
            "Default renders page 1 at 150 DPI. Use pages='-1' for last page, up to 10 pages per call. "
            "grid: true shows every page (up to 36) as labelled thumbnails on ONE image: a whole deck or "
            "report at a glance; then look closer at one page without grid."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the document (PDF, DOCX, XLSX, PPTX, etc.)",
                },
                "pages": {
                    "type": "string",
                    "description": "Pages to render: '1' (default), '1-3', '-1' (last), 'all'.",
                    "default": "1",
                },
                "sheet": {
                    "type": "string",
                    "description": "For XLSX only: sheet name or 0-based index; that sheet alone prints, and "
                                   "pages count within it. Default: every sheet.",
                },
                "dpi": {
                    "type": "integer",
                    "description": "Resolution. Default 150 (good for review). Use 300 for detail.",
                    "default": 150,
                },
                "grid": {
                    "type": "boolean",
                    "description": "A contact sheet: every page asked for (default all, up to 36) as labelled "
                                   "thumbnails in one image.",
                },
                "password": {
                    "type": "string",
                    "description": "The password of a protected PDF.",
                },
            },
            "required": ["path"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    Tool(
        name="images_to_pdf",
        description=(
            "Combine one or more images into a PDF document. Supports page size "
            "(a4, letter, original) and fit mode (contain, cover, stretch). "
            "Auto-previews the result."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "images": {
                    "type": "array",
                    "description": "List of absolute image file paths",
                    "items": {"type": "string"},
                },
                "output_path": {
                    "type": "string",
                    "description": "Absolute path for the output PDF",
                },
                "page_size": {
                    "type": "string",
                    "description": "Page size: a4, letter, a3, or original (match image). Default a4.",
                    "default": "a4",
                },
                "fit": {
                    "type": "string",
                    "description": "How images fit: contain (default), cover, stretch.",
                    "default": "contain",
                },
            },
            "required": ["images", "output_path"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
    Tool(
        name="edit_image",
        description=(
            "Edit an image with a pipeline of operations (Lightroom-style), applied in order and saved once. "
            "Atomic: if any operation fails, nothing is saved and every failure is listed; pass best_effort: "
            "true to keep the operations that worked. {\"op\": \"help\"} lists every operation with its keys. "
            "Recipes: the skill `photo-editing-guide`.\n\n"
            "BASIC: resize, crop, rotate, flip, convert_format (png/jpeg/webp/tiff/bmp/gif), compress (quality 1-100).\n"
            "TONE: exposure (gamma: >1.0 BRIGHTENS, <1.0 DARKENS; midtone-weighted, prefer it over brightness, "
            "which multiplies linearly and clips highlights sooner), highlights and shadows (amount -100 to "
            "+100), whites, blacks (level is RELATIVE: positive lifts for the faded look, NEGATIVE deepens toward "
            "pure black, 0 changes nothing), brightness, contrast, clarity (midtone local contrast), dehaze "
            "(darkens overall: recheck exposure after). Tonal edits end with a 'Tone check' line (mean and median "
            "luminance, zones, clipping, before and after): READ it and correct with one follow-up edit if the "
            "shift missed your target.\n"
            "COLOR: saturation, color_temperature, grayscale, sepia, invert, split_tone (shadows/midtones/"
            "highlights with hue, saturation, strength), hsl_adjust (per colour: red, orange, yellow, green, aqua, "
            "blue, purple, magenta).\n"
            "DETAIL: sharpness, denoise (strength 1-5), blur, sharpen, edge_enhance, emboss.\n"
            "EFFECTS: auto_enhance, add_text, add_border, paste_image, add_watermark, remove_background (AI: a "
            "transparent PNG, or background_color), blur_region (a rectangle, for faces, plates, addresses).\n\n"
            "The edited image is shown inline in the chat: do NOT call display_images separately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the input image",
                },
                "output_path": {
                    "type": "string",
                    "description": "Optional output path. Defaults to overwriting the input.",
                },
                "operations": {
                    "type": "array",
                    "description": "List of image operations to apply in sequence",
                    "items": {"type": "object"},
                },
                "best_effort": {
                    "type": "boolean",
                    "description": "Save the operations that worked even when others fail (default false: "
                                   "one failure saves nothing).",
                },
            },
            "required": ["path", "operations"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
    Tool(
        name="analyze_image",
        description=(
            "Analyze an image — returns file info (format, dimensions, size, color mode), "
            "EXIF data (camera, date, exposure, ISO, GPS if present), "
            "luminance histogram, zone balance, color channels, dynamic range, "
            "color temperature, and suggested fixes. Use this BEFORE edit_image."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the image to analyze",
                },
            },
            "required": ["path"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
]

HANDLERS = {
    "write_pdf": ("pdf", "handle_write_pdf"),
    "convert_document": ("pdf", "handle_convert_document"),
    "edit_pdf": ("pdf", "handle_edit_pdf"),
    "pdf_to_images": ("pdf", "handle_pdf_to_images"),
    "screenshot_document": ("pdf", "handle_screenshot_document"),
    "images_to_pdf": ("pdf", "handle_images_to_pdf"),
    "edit_image": ("images", "handle_edit_image"),
    "analyze_image": ("images", "handle_analyze_image"),
}

READERS = {".pdf": read}

READ_PROPERTIES: dict[str, dict] = {
    "pages": {
        "type": "string",
        "description": "Page range for PDFs, e.g. '1-5', '2-', '-1', '1,4'. Optional.",
    },
    "mode": {
        "type": "string",
        "enum": ["text", "form_fields"],
        "description": "PDF: text (default), or form_fields: every fillable field with its kind, value and "
                       "options (a page without fields lists its text with positions, for fill_flat_form).",
    },
    "password": {
        "type": "string",
        "description": "PDF: the password of a protected PDF.",
    },
}

READ_DESCRIPTION = ""
