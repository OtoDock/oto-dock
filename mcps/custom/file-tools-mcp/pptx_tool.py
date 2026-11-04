"""The PPTX tools: write_pptx and the PPTX reader of read_document.

The assembler (``server.py``) lists every format module's tools, routes each
tool to its handler and each file extension to its reader. This module
holds definitions and thin adapters only and never imports its format module
at top level: startup stays fast, and a worker that unpickles a reader
imports only what that reader needs.
"""

from mcp.types import Tool, ToolAnnotations

from kept_batches import FIX_PROPERTY


def read(path: str, options: dict) -> str:
    """read_document for PowerPoint (runs in the worker)."""
    from formats.pptx.pptx_read import read_deck

    return read_deck(path, slides=options.get("slides"), detail=options.get("detail"))


TOOLS = [
    Tool(
        name="write_pptx",
        description=(
            "Create or modify a PowerPoint presentation (.pptx; a path ending .potx or .potm saves a "
            "template, .ppsx or .ppsm a show, .pptm keeps macros). operations: [{\"op\": \"<name>\", "
            "...keys}], applied in order in one save. Atomic: one failure saves nothing, and each failure is "
            "listed with its fix; best_effort: true keeps the operations that worked; fix resends the kept "
            "batch with just the fixes. {\"op\": \"help\"} returns every operation with its keys "
            "(add name: \"<op>\" for one): read it before an operation you have not used. Slides are slide_index (0-based, -1 = last); shapes are shape_id, shape_name or "
            "shape_index, as read_document shows them.\n\n"
            "SLIDES: add_slide (layout: the deck's own layout name or number; title), delete_slide, "
            "duplicate_slide, move_slide, set_slide_dimensions, set_core_properties.\n"
            "TEXT: set_text (placeholder, text or runs), add_textbox, add_bullet_points (items with "
            "levels), format_text, fill_template ({{ key }} tokens); text also takes line_spacing, "
            "space_before/after, bullet, insets, vertical_anchor, autofit and lang.\n"
            "SHAPES: add_shape (every PowerPoint preset), set_shape_format; both take gradient, transparency, "
            "shadow, glow; remove_shape, add_group_shape, add_freeform, add_connector (glued to shapes), "
            "arrange_shape, align_shapes, distribute_shapes.\n"
            "TABLES: add_table, format_table_cell (fill, font, alignment, merge).\n"
            "CHARTS: add_chart, update_chart_data, format_chart (axes, labels, legend).\n"
            "IMAGES / EQUATIONS: add_image (crop, alt_text), add_equation.\n"
            "VISUAL: set_theme (theme colours and fonts, ten kits), set_background, set_transition (the "
            "gallery, Morph), add_animation, remove_animations.\n"
            "NOTES / LINKS / NUMBERS: set_speaker_notes, add_hyperlink, set_slide_number.\n"
            "XML: xml_patch (a slide, notes, layout, master or theme by XPath; refuses fields, active "
            "content, Strict XML and other parts).\n\n"
            "A new presentation is 16:9 (13.333 x 7.5 in). Lengths are inches unless a unit is given "
            "('2.5cm', '40pt'); colours take hex, CSS names or theme slots (accent1, dk1)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path for the presentation (.pptx, .pptm, .potx, .potm, .ppsx, .ppsm)",
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
                "best_effort": {
                    "type": "boolean",
                    "description": "If true, save the operations that worked even when others failed (a failed one may be partly applied). Default false: any failure saves nothing.",
                },
                "fix": FIX_PROPERTY,
            },
            "required": ["path"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ),
]

HANDLERS = {"write_pptx": ("powerpoint", "handle_write_pptx")}

# .ppt reaches the modern reader until the legacy intake converts it first.
READERS = {ext: read for ext in (".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".ppt")}

READ_PROPERTIES: dict[str, dict] = {
    "slides": {
        "type": "string",
        "description": "PPTX: the slides to read, as slide_index values from 0 like write_pptx takes "
                       "them: '3', '2-5', '0,4', '-1' for the last. Optional, default every slide.",
    },
    "detail": {
        "type": "string",
        "enum": ["outline", "shapes", "text"],
        "description": "PPTX: outline (each slide's title and layout), shapes (default: every shape "
                       "with its index, id, placeholder, position, size and text), or text (also each "
                       "run's font, size and colour).",
    },
}

READ_DESCRIPTION = ""
