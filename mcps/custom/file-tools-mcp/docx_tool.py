"""The DOCX tools: write_docx and the DOCX reader of read_document.

The assembler (``server.py``) lists every format module's tools, routes each
tool to its handler and each file extension to its reader. This module
holds definitions and thin adapters only and never imports its format module
at top level: startup stays fast, and a worker that unpickles a reader
imports only what that reader needs.
"""

from mcp.types import Tool, ToolAnnotations

from kept_batches import FIX_PROPERTY


def read(path: str, options: dict) -> str:
    """read_document for DOCX (runs in the worker)."""
    from formats.docx.read import read_docx

    return read_docx(path, start_at=options.get("start_at"), end_at=options.get("end_at"),
                     outline_only=options.get("outline_only") in (True, "true", "True", 1))


TOOLS = [
    Tool(
        name="write_docx",
        description=(
            "Create or modify a Word document (.docx, .dotx, .docm, .dotm). operations: [{\"op\": "
            "\"<name>\", ...keys}], applied in order, saved once. Atomic: one failure saves nothing, every "
            "failure is listed; best_effort: true keeps what worked; fix resends the kept batch with just the "
            "fixes. {\"op\": \"help\"} lists each op's keys (name: \"<op>\" for one): read it before a new op.\n\n"
            "ADDRESSES: read_document prints each block's address (p12, t2/r3/c1, header0/p0, footnote1/p0) "
            "and id (#1A2B3C4D). Give one as target (position after, before, start, end); expect_text guards "
            "against later changes.\n\n"
            "PARAGRAPHS: add_heading, add_paragraph (text or runs), "
            "insert_paragraph, set_paragraph_text, delete_paragraph, set_paragraph_format, "
            "add_horizontal_rule.\n"
            "EQUATIONS: add_equation (latex), or a run {equation}.\n"
            "LISTS: add_list (levels 1-9), number_headings.\n"
            "TABLES: add_table, delete_table, merge_table_cells, add_table_row, delete_table_row, "
            "set_table_column_widths, style_table.\n"
            "IMAGES: add_image.\n"
            "PAGE: add_page_break (never last), add_section_break, set_page_setup. New documents are A4.\n"
            "HEADERS: add_header, add_footer, add_page_number (kind default, first, even).\n"
            "LINKS: add_hyperlink (url or bookmark), add_bookmark, add_caption, add_cross_reference, "
            "add_table_of_contents, update_table_of_contents, update_fields.\n"
            "REVIEW: add_footnote, add_endnote, delete_note, add_comment, reply_to_comment, resolve_comment, "
            "delete_comment, set_track_changes, accept_all_changes, reject_all_changes; author or track: true "
            "makes an edit a tracked change.\n"
            "EDITING: search_and_replace (formatting kept), fill_template ({{ key }} tokens), set_properties.\n"
            "XML: xml_patch, the last resort. Refused: fields, active content, Strict XML, an element of 257+ "
            "attributes, outside a target's block; huge parts take only short XPaths.\n\n"
            "UNITS: a bare number is in the key's unit, or give one (\"2cm\", \"12pt\"). Spacing is points "
            "(not twips); line_spacing a multiple (1.5x); indents, widths, margins inches. Colours: hex, "
            "names, or a theme slot (accent1)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path for the .docx, .dotx, .docm or .dotm file",
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
                "best_effort": {
                    "type": "boolean",
                    "description": "If true, save the operations that worked even when others fail. Default false: one failure saves nothing.",
                },
                "fix": FIX_PROPERTY,
            },
            "required": ["path"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
]

HANDLERS = {"write_docx": ("word", "handle_write_docx")}

# .doc reaches the modern reader until the legacy intake converts it first.
READERS = {".docx": read, ".dotx": read, ".docm": read, ".dotm": read, ".doc": read}

READ_PROPERTIES: dict[str, dict] = {
    "start_at": {
        "type": "string",
        "description": "DOCX: the body address to read from (p120, t3, t3/r40), from the continue line "
                       "or the outline; footnote1, endnote1 or comment1 reads the notes or comments.",
    },
    "end_at": {"type": "string", "description": "DOCX: the last body block to read (an address)."},
    "outline_only": {
        "type": "boolean",
        "description": "DOCX: only the overview: pages, counts, the outline with addresses, headers and footers.",
    },
}

READ_DESCRIPTION = (
    "For DOCX: every block prints with its address (p12, t2/r3/c1, header0/p0) and paragraph id "
    "(#1A2B3C4D), which write_docx takes as target; a long document reads in windows (start_at), "
    "equations as LaTeX, tracked changes as {+inserted+} {-deleted-}, then notes and comments."
)
