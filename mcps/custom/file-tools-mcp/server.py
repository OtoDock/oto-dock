"""File-Tools MCP Server — document read/write/convert, image manipulation, live preview.

Dual transport: SSE at /sse (Claude CLI) + streamable HTTP at /mcp (Codex CLI).
Runs inside Docker with LibreOffice headless for conversions and Pillow for
image manipulation. Pushes Collabora previews to the dashboard via the proxy's
hook endpoint.

Modules: shared.py, excel.py, word.py, powerpoint.py, pdf.py, images.py
"""

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from shared import (
    MCP_PORT,
    _push_preview,
    _resolve_path,
    _to_agents_relative,
    logger,
    set_request_context,
)

# ===================================================================
# TOOL DEFINITIONS
# ===================================================================

TOOLS = [
    Tool(
        name="read_document",
        description=(
            "Read and extract text/data from a document file. "
            "Supports PDF, DOCX, XLSX, PPTX. Returns structured text output. "
            "For XLSX: supports range-based reading (start_cell/end_cell), "
            "formula view (show_formulas), and metadata (validations, merged cells, tables)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the document file",
                },
                "pages": {
                    "type": "string",
                    "description": "Page range for PDFs, e.g. '1-5'. Optional.",
                },
                "sheet": {
                    "type": "string",
                    "description": "Sheet name for XLSX files. Optional, defaults to first sheet.",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Max rows to read from XLSX. Default 500.",
                    "default": 500,
                },
                "start_cell": {
                    "type": "string",
                    "description": "Start cell for range reading (e.g. 'A1'). XLSX only.",
                },
                "end_cell": {
                    "type": "string",
                    "description": "End cell for range reading (e.g. 'D50'). XLSX only.",
                },
                "show_formulas": {
                    "type": "boolean",
                    "description": "Show raw formulas instead of computed values. XLSX only.",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="write_docx",
        description=(
            "Create or modify a Word document (.docx). Operations are applied "
            "sequentially in one save. Continues on error (partial success with report).\n\n"
            "PARAGRAPHS: add_heading (text, level), add_paragraph (text or runs array for "
            "mixed formatting, bold/italic/underline/color/font_size/font_name, alignment, "
            "space_before/space_after/line_spacing/first_line_indent), insert_paragraph "
            "(index, text, formatting), delete_paragraph (index), set_paragraph_format "
            "(index, spacing/indent/alignment/keep_together/keep_with_next).\n"
            "LISTS: add_list (items, ordered, level 1-5 for nesting).\n"
            "TABLES: add_table (headers, rows, style), merge_table_cells (table_index, "
            "start_row/col, end_row/col), add_table_row (table_index, values), "
            "delete_table_row (table_index, row_index), set_table_column_widths "
            "(table_index, widths in inches), style_table (header fill/color, "
            "alternating_colors, border).\n"
            "IMAGES: add_image (image_path, width_inches, height_inches — optional, alignment).\n"
            "PAGE STRUCTURE: add_page_break, add_section_break (orientation, margins), "
            "set_page_setup (orientation, margins).\n"
            "HEADERS/FOOTERS: add_header (text or runs, alignment, section_index), "
            "add_footer (same), add_page_number (position, format with {page}/{total}).\n"
            "LINKS & REFERENCES: add_hyperlink (url, text, color, paragraph_index), "
            "add_table_of_contents (title, levels — renders in Word/Collabora).\n"
            "EDITING: search_and_replace (find, replace, match_case — searches body, "
            "tables, headers, footers).\n\n"
            "UNITS: space_before/space_after are in POINTS (typical body spacing is "
            "6–18pt; they are NOT twips/EMUs — passing e.g. 180 means 2.5 inches and "
            "pushes each paragraph onto its own page). line_spacing is a MULTIPLE of the "
            "line height (1.0=single, 1.5, 2.0=double — NOT points). indents and table "
            "widths are in INCHES.\n"
            "NOTE: add_page_break starts a NEW page after the cursor; do NOT make it the "
            "final operation or the document ends with a blank page. Implausible spacing "
            "and a trailing page break are flagged in the result's warnings.\n\n"
            "Auto-previews in the dashboard after saving."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path for the .docx file",
                },
                "operations": {
                    "type": "array",
                    "description": "List of operations to apply",
                    "items": {"type": "object"},
                },
                "create_new": {
                    "type": "boolean",
                    "description": "If true, create a blank document even if the file exists (overwrite). Default false (edit existing).",
                },
            },
            "required": ["path", "operations"],
        },
    ),
    Tool(
        name="write_xlsx",
        description=(
            "Create or modify an Excel workbook (.xlsx). Operations are applied "
            "sequentially in one save. All operations continue even if one fails "
            "(partial success with error report).\n\n"
            "SHEETS: create_sheet, delete_sheet, rename_sheet (old_name, new_name), "
            "copy_sheet (source, new_name), protect_sheet (password, allow_* flags).\n"
            "CELLS: write_cells (individual cells array OR 2D data array with start_cell), "
            "set_formula (cell, formula — validated, blocks unsafe functions), "
            "merge_cells (range), unmerge_cells (range), clear_range (range, clear_styles), "
            "copy_range (source_range, target_start, target_sheet — copies data AND styles).\n"
            "ROWS/COLUMNS: insert_rows (row, count), insert_columns (column, count), "
            "delete_rows (row, count), delete_columns (column, count), "
            "set_column_width (column, width), set_row_height (row, height), "
            "auto_column_width (columns list or all), freeze_panes (cell).\n"
            "FORMATTING: set_style (range + bold/italic/underline/strikethrough/"
            "font_size/font_color/font_name/fill_color/border/number_format/"
            "alignment/wrap_text/text_rotation/protection).\n"
            "FEATURES: create_table (range, name, style — native Excel table with auto-styles), "
            "add_data_validation (range, validation_type: list/whole/decimal/date/textLength/custom, "
            "values for dropdowns, operator+min/max for numbers), "
            "conditional_format (range, rule_type: color_scale/data_bar/icon_set/cell_is/formula, params), "
            "auto_filter (range), add_chart (chart_type: bar/line/pie/scatter/area, "
            "data_range — categories in the FIRST COLUMN and one series per "
            "following column (vertical tables; a horizontal row reads as "
            "N one-point series), title, x_axis, y_axis, width+height in "
            "CENTIMETERS (default 15x7.5), position), "
            "add_image (image_path, cell, width, height), "
            "define_name (name, range — creates named range).\n\n"
            "Auto-previews in the dashboard after saving."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path for the .xlsx file",
                },
                "operations": {
                    "type": "array",
                    "description": "List of operations to apply",
                    "items": {"type": "object"},
                },
                "create_new": {
                    "type": "boolean",
                    "description": "If true, create a blank workbook even if the file exists (overwrite). Default false (edit existing).",
                },
            },
            "required": ["path", "operations"],
        },
    ),
    Tool(
        name="write_pptx",
        description=(
            "Create or modify a PowerPoint presentation (.pptx). Operations are applied "
            "sequentially in one save. Continues on error (partial success with report).\n\n"
            "SLIDES: add_slide (layout: title/title_and_content/section_header/blank/title_only), "
            "delete_slide, duplicate_slide, move_slide (from_index, to_index), "
            "set_slide_dimensions, set_core_properties (title, author, subject).\n"
            "TEXT: set_text (placeholder or runs array for mixed formatting), "
            "add_textbox (text or runs, alignment, auto_size, margin), "
            "add_bullet_points (items with levels, target shape/placeholder), "
            "format_text (find text on slide, apply formatting).\n"
            "SHAPES: add_shape (25+ types: rectangle, rounded_rectangle, oval, triangle, diamond, "
            "arrow_right/left/up/down, star, chevron, hexagon, cloud, heart, flowchart_process/"
            "decision/data/terminator — with text, fill_color, line_color, line_width, rotation), "
            "set_shape_format (fill/line/rotation/width/height/left/top by name or index — use to resize and reposition shapes), "
            "remove_shape, add_group_shape, add_freeform (arbitrary polygon from points), "
            "add_connector (line/arrow between positions).\n"
            "TABLES: add_table (headers, rows), format_table_cell (fill, font, alignment, "
            "merge, range support via end_row/end_col).\n"
            "CHARTS: add_chart (chart_type: column_clustered/bar_clustered/line/pie/doughnut/"
            "scatter/area/radar — categories, series with name+values+optional color hex; "
            "pie/doughnut take a per-slice 'colors' array; title, legend — style chart "
            "colors to match the deck's palette), "
            "update_chart_data (replace data in existing chart).\n"
            "IMAGES: add_image (image_path, left, top, width, height — both optional, maintains aspect ratio if only one given).\n"
            "VISUAL: set_background (solid color or gradient), set_transition "
            "(fade/push/wipe/split/reveal/cover/dissolve, duration, advance_after).\n"
            "NOTES: set_speaker_notes (text, append).\n"
            "LINKS: add_hyperlink (url on shape text or click action).\n"
            "NUMBERS: set_slide_number (enable/disable).\n\n"
            "Dimensions in inches. Auto-previews in the dashboard after saving."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path for the .pptx file",
                },
                "operations": {
                    "type": "array",
                    "description": "List of operations to apply",
                    "items": {"type": "object"},
                },
                "create_new": {
                    "type": "boolean",
                    "description": "If true, create a blank presentation even if the file exists (overwrite). Default false (edit existing).",
                },
            },
            "required": ["path", "operations"],
        },
    ),
    Tool(
        name="write_pdf",
        description=(
            "Create a PDF from HTML or Markdown content. Supports embedded images (via file paths), "
            "custom CSS, page size (A4/Letter), and margins. Auto-previews in the dashboard."
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
    ),
    Tool(
        name="convert_document",
        description=(
            "Convert a document between formats using LibreOffice. Common conversions: "
            "DOCX->PDF, XLSX->PDF, PPTX->PDF, CSV->XLSX, HTML->PDF. "
            "Markdown->PDF uses the built-in renderer. Auto-previews the output."
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
    ),
    Tool(
        name="edit_pdf",
        description=(
            "Edit an existing PDF file with batched operations. Continues on error.\n\n"
            "PAGES: merge (combine with other PDFs), split (extract pages to new file), "
            "rotate_page (90/180/270), delete_page, reorder_pages, insert_page (blank), "
            "crop_page (rect or auto-detect).\n"
            "CONTENT: add_text (position, font, color), add_image (position, rect), "
            "add_watermark (diagonal text, opacity, rotation, all pages), "
            "add_annotation (highlight/underline/strikeout/text_note/rectangle), "
            "replace_text ({find, replace, pages?, case_sensitive?} — edits "
            "EXISTING text: redacts each match and reinserts the replacement "
            "with matched size/color/family, shrink-to-fit; replace:'' deletes), "
            "redact ({find or rect, pages?, fill?} — permanently removes "
            "content under a fill box, privacy-grade).\n"
            "SECURITY: encrypt (AES-256, user/owner passwords, permissions), "
            "decrypt (remove password).\n"
            "OPTIMIZATION: compress (garbage collection + deflation, reports size reduction).\n"
            "METADATA: set_metadata (title, author, subject, keywords).\n"
            "EXTRACTION: extract_images (pull embedded images to files), "
            "ocr (OCR scanned pages via tesseract, makes PDF searchable).\n\n"
            "Auto-previews in dashboard after saving."
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
            },
            "required": ["path", "operations"],
        },
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
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="screenshot_document",
        description=(
            "Render document pages as images for visual inspection. "
            "Use this to SEE what a document looks like after creating or editing it. "
            "Supports PDF, DOCX, XLSX, PPTX and other Office formats. "
            "Pages are shown inline in the chat so you can review layout, sizing, and content.\n\n"
            "Workflow: create/edit document → screenshot_document → review layout → adjust if needed.\n"
            "Default renders page 1 at 150 DPI. Use pages='-1' for last page, 'all' for all pages."
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
                    "description": "For XLSX only: sheet name or 0-based index. Default: first sheet.",
                },
                "dpi": {
                    "type": "integer",
                    "description": "Resolution. Default 150 (good for review). Use 300 for detail.",
                    "default": 150,
                },
            },
            "required": ["path"],
        },
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
    ),
    Tool(
        name="edit_image",
        description=(
            "Edit an image with a pipeline of operations. Lightroom-style editing. Supports: "
            "BASIC: resize, crop, rotate, flip, convert_format (png/jpeg/webp), compress (quality 1-100). "
            "TONE: exposure (gamma — values >1.0 BRIGHTEN, <1.0 DARKEN; midtone-weighted, "
            "prefer it over brightness which multiplies linearly and clips highlights faster), "
            "highlights (amount -100 to +100), "
            "shadows (amount -100 to +100), whites, "
            "blacks (level is RELATIVE: positive lifts for the faded look, "
            "NEGATIVE deepens toward pure black, 0 is a no-op), brightness, contrast, "
            "clarity (midtone local contrast), dehaze (darkens overall — recheck exposure after). "
            "Results of tonal edits include a numeric 'Tone check' line "
            "(mean/median luminance, zone %, clipping — before → after): READ it and "
            "correct with one follow-up edit if the shift missed your target. "
            "COLOR: saturation, color_temperature, grayscale, sepia, invert, "
            "split_tone (shadows/midtones/highlights with hue+saturation+strength), "
            "hsl_adjust (per-color for red/orange/yellow/green/aqua/blue/purple/magenta). "
            "DETAIL: sharpness, denoise (strength 1-5), blur, sharpen, edge_enhance, emboss. "
            "EFFECTS: auto_enhance, add_text, add_border, paste_image, add_watermark, "
            "remove_background (AI-powered, outputs transparent PNG or custom bg color), "
            "blur_region (blur rectangular area for privacy — faces, plates, addresses). "
            "Operations are applied sequentially. The edited image is automatically shown "
            "inline in the chat — do NOT call display_image separately."
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
            },
            "required": ["path", "operations"],
        },
    ),
    Tool(
        name="preview_document",
        description=(
            "Show a live Collabora preview of a document in the user's chat. "
            "Supports PDF, DOCX, XLSX, PPTX, and other office formats. "
            "Write tools call this automatically — use this for manual previews "
            "(e.g., after downloading a file from Nextcloud)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the document to preview",
                },
            },
            "required": ["path"],
        },
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
    ),
    Tool(
        name="create_chart",
        description=(
            "Create a chart IMAGE (PNG) — shown inline in the chat and "
            "optionally saved as a file for embedding in documents "
            "(docx/pdf/xlsx cannot contain live charts).\n\n"
            "Routing: when the display_ui tool is available, prefer it for "
            "purely in-chat visualization (live, theme-matched). Use "
            "create_chart when you need the chart as an image FILE for a "
            "document, and as the in-chat chart path where display_ui isn't "
            "offered.\n\n"
            "Chart types: bar, column, horizontal_bar, line, pie, doughnut, "
            "scatter, area, heatmap, histogram.\n"
            "Styles: modern (default), dark, minimal, presentation.\n"
            "Required data key per chart type: most charts need per-series "
            "'values'; scatter needs 'x'+'y'; heatmap takes a top-level "
            "'data' 2-D array (or per-series 'values' rows). A wrong-keyed "
            "series returns an explicit error, never an empty plot.\n\n"
            "The chart appears inline in the chat automatically — "
            "do NOT call display_image separately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "description": "Chart type: bar, column, horizontal_bar, line, pie, doughnut, scatter, area, heatmap, histogram",
                },
                "title": {
                    "type": "string",
                    "description": "Chart title",
                },
                "categories": {
                    "type": "array",
                    "description": "Category labels (x-axis for bar/line/area, slice labels for pie)",
                    "items": {"type": "string"},
                },
                "series": {
                    "type": "array",
                    "description": (
                        "Data series. Most charts: {name, values, color?}. "
                        "Scatter: {name, x, y} (or 'values' as x plus 'y'). "
                        "Heatmap: per-row {name, values} — or use the "
                        "top-level 'data' instead."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Series name (legend label / heatmap row label)"},
                            "values": {"type": "array", "items": {"type": "number"}, "description": "Values (bar/column/line/area/pie/doughnut/histogram; heatmap row)"},
                            "x": {"type": "array", "items": {"type": "number"}, "description": "Scatter x values"},
                            "y": {"type": "array", "items": {"type": "number"}, "description": "Scatter y values"},
                            "color": {"type": "string", "description": "Optional color (hex)"},
                        },
                    },
                },
                "data": {
                    "type": "array",
                    "description": "Heatmap only: 2-D array of numbers (list of rows) — alternative to per-series 'values'.",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
                "x_label": {"type": "string", "description": "X-axis label"},
                "y_label": {"type": "string", "description": "Y-axis label"},
                "legend": {"type": "boolean", "description": "Show legend. Default true.", "default": True},
                "style": {
                    "type": "string",
                    "description": "Visual style: modern (default), dark, minimal, presentation",
                    "default": "modern",
                },
                "width": {"type": "number", "description": "Figure width in inches. Default 10.", "default": 10},
                "height": {"type": "number", "description": "Figure height in inches. Default 6.", "default": 6},
                "save_path": {
                    "type": "string",
                    "description": "Optional: save chart as PNG to this absolute path",
                },
            },
            "required": ["chart_type", "series"],
        },
    ),
]


# ===================================================================
# READ_DOCUMENT DISPATCHER
# ===================================================================


async def _handle_read_document(args: dict) -> str:
    path = _resolve_path(args["path"])
    if not Path(path).exists():
        return f"Error: File not found: {args['path']}"

    ext = Path(path).suffix.lower()
    pages = args.get("pages")
    sheet = args.get("sheet")
    max_rows = int(args.get("max_rows", 500))

    try:
        if ext == ".pdf":
            from pdf import read_pdf

            return read_pdf(path, pages)
        elif ext in (".docx", ".doc"):
            from word import read_docx

            return read_docx(path)
        elif ext in (".xlsx", ".xls"):
            from excel import read_xlsx

            return read_xlsx(
                path,
                sheet,
                max_rows,
                start_cell=args.get("start_cell"),
                end_cell=args.get("end_cell"),
                show_formulas=args.get("show_formulas", False),
            )
        elif ext in (".pptx", ".ppt"):
            from powerpoint import read_pptx

            return read_pptx(path)
        else:
            return f"Error: Unsupported format '{ext}'. Supported: .pdf, .docx, .xlsx, .pptx"
    except Exception as exc:
        return f"Error reading {ext} file: {exc}"


async def _handle_preview_document(args: dict) -> str:
    path = _resolve_path(args["path"])
    if not Path(path).exists():
        return f"Error: File not found: {args['path']}"
    await _push_preview(path)
    return f"Preview pushed for: {_to_agents_relative(path)}"


# ===================================================================
# MCP SERVER SETUP
# ===================================================================

mcp_server = Server("file-tools")

# Dual transport: SSE for Claude CLI, streamable HTTP for Codex CLI
sse = SseServerTransport("/messages/")
session_manager = StreamableHTTPSessionManager(app=mcp_server, stateless=True)


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handlers = {
        "read_document": _handle_read_document,
        "preview_document": _handle_preview_document,
    }

    # Lazy-load format handlers to keep startup fast
    if name == "write_docx":
        from word import handle_write_docx

        handlers["write_docx"] = handle_write_docx
    elif name == "write_xlsx":
        from excel import handle_write_xlsx

        handlers["write_xlsx"] = handle_write_xlsx
    elif name == "write_pptx":
        from powerpoint import handle_write_pptx

        handlers["write_pptx"] = handle_write_pptx
    elif name == "write_pdf":
        from pdf import handle_write_pdf

        handlers["write_pdf"] = handle_write_pdf
    elif name == "convert_document":
        from pdf import handle_convert_document

        handlers["convert_document"] = handle_convert_document
    elif name == "edit_pdf":
        from pdf import handle_edit_pdf

        handlers["edit_pdf"] = handle_edit_pdf
    elif name == "pdf_to_images":
        from pdf import handle_pdf_to_images

        handlers["pdf_to_images"] = handle_pdf_to_images
    elif name == "screenshot_document":
        from pdf import handle_screenshot_document

        handlers["screenshot_document"] = handle_screenshot_document
    elif name == "images_to_pdf":
        from pdf import handle_images_to_pdf

        handlers["images_to_pdf"] = handle_images_to_pdf
    elif name == "edit_image":
        from images import handle_edit_image

        handlers["edit_image"] = handle_edit_image
    elif name == "analyze_image":
        from images import handle_analyze_image

        handlers["analyze_image"] = handle_analyze_image
    elif name == "create_chart":
        from charts import handle_create_chart

        handlers["create_chart"] = handle_create_chart

    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        result = await handler(arguments)
        # Handlers may return a list of content items (e.g., screenshot_document
        # returns ImageContent + TextContent) or a plain string.
        if isinstance(result, list):
            return result
        return [TextContent(type="text", text=result)]
    except Exception as exc:
        logger.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=f"Error: {exc}")]


# ===================================================================
# DUAL TRANSPORT ENDPOINTS — SSE (/sse) + Streamable HTTP (/mcp)
# ===================================================================


async def handle_sse(request):
    """SSE endpoint for Claude CLI and legacy MCP clients."""
    session_id = request.query_params.get("session_id", "")
    # Bind session_id + the per-session JWT (Authorization header) for this
    # request's contextvars BEFORE the SDK dispatches any tool. Forwarded on
    # every proxy callback in place of the retired master key.
    set_request_context(session_id, request.headers.get("authorization", ""))
    logger.info(f"SSE connection: session_id={session_id[:8] if session_id else '(none)'}...")
    async with sse.connect_sse(
        request.scope, request.receive, request._send,
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1],
            mcp_server.create_initialization_options(),
        )


async def mcp_asgi_app(scope, receive, send):
    """Streamable HTTP ASGI app for Codex CLI and modern MCP clients."""
    if scope["type"] == "http":
        from starlette.requests import Request
        request = Request(scope, receive, send)
        session_id = request.query_params.get("session_id", "")
        # Bind per-request session_id + JWT before the stateless SDK manager
        # spawns the per-request task group (contextvars propagate into it).
        set_request_context(session_id, request.headers.get("authorization", ""))
        logger.info(f"MCP request: session_id={session_id[:8] if session_id else '(none)'}...")
    await session_manager.handle_request(scope, receive, send)


async def handle_health(request):
    return JSONResponse({"status": "ok"})


@asynccontextmanager
async def lifespan(app):
    async with session_manager.run():
        yield


starlette_app = Starlette(
    routes=[
        # SSE transport (Claude CLI)
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        # Streamable HTTP transport (Codex CLI) — raw ASGI app, not Route endpoint
        Mount("/mcp", app=mcp_asgi_app),
        # Health check
        Route("/health", endpoint=handle_health),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    logger.info(f"File-Tools MCP starting on port {MCP_PORT}")
    uvicorn.run(starlette_app, host="0.0.0.0", port=MCP_PORT, log_level="info")
