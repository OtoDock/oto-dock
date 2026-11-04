"""write_pptx's shape operations: shapes, groups, freeforms, connectors and
pictures.
"""

from typing import Any

import units
from catalog import KeySpec, OpSpec
from formats.pptx import (
    pptx_animations,
    pptx_arrange,
    pptx_effects,
    pptx_style,
    pptx_text,
)
from formats.pptx.pptx_opkit import (
    EXAMPLE_PNG,
    FILL,
    G_IMAGES,
    G_SHAPES,
    IN,
    LINE,
    SHAPE_ONE_OF,
    SHAPE_REF,
    TEXT_FRAME,
    PptxJob,
    apply_run_format,
    box_keys,
    emu,
    fill_solid,
    line_width,
    name_of,
    paragraph_align,
    plural,
    set_color,
    shape_of,
    slide_key,
    slide_of,
)
from ooxml import order
from results import Code, OpCtx, OpError

_SHAPE_NAMES = (
    ("rectangle", "RECTANGLE"), ("rounded_rectangle", "ROUNDED_RECTANGLE"), ("oval", "OVAL"),
    ("triangle", "ISOSCELES_TRIANGLE"), ("diamond", "DIAMOND"), ("arrow_right", "RIGHT_ARROW"),
    ("arrow_left", "LEFT_ARROW"), ("arrow_up", "UP_ARROW"), ("arrow_down", "DOWN_ARROW"),
    ("star", "STAR_5_POINT"), ("star_5", "STAR_5_POINT"), ("star_6", "STAR_6_POINT"),
    ("star_4", "STAR_4_POINT"), ("chevron", "CHEVRON"), ("pentagon", "PENTAGON"), ("hexagon", "HEXAGON"),
    ("cloud", "CLOUD"), ("heart", "HEART"), ("lightning", "LIGHTNING_BOLT"), ("plus", "CROSS"),
    ("flowchart_process", "FLOWCHART_PROCESS"), ("flowchart_decision", "FLOWCHART_DECISION"),
    ("flowchart_data", "FLOWCHART_DATA"), ("flowchart_terminator", "FLOWCHART_TERMINATOR"),
    ("callout_1", "RECTANGULAR_CALLOUT"), ("callout_2", "ROUNDED_RECTANGULAR_CALLOUT"),
)
# Every preset python-pptx draws, by its MSO_AUTO_SHAPE_TYPE name in snake
# case (a test holds the list to python-pptx's).
PRESETS = (
    "action_button_back_or_previous", "action_button_beginning", "action_button_custom", "action_button_document",
    "action_button_end", "action_button_forward_or_next", "action_button_help", "action_button_home",
    "action_button_information", "action_button_movie", "action_button_return", "action_button_sound", "arc",
    "balloon", "bent_arrow", "bent_up_arrow", "bevel", "block_arc", "can", "chart_plus", "chart_star", "chart_x",
    "chevron", "chord", "circular_arrow", "cloud", "cloud_callout", "corner", "corner_tabs", "cross", "cube",
    "curved_down_arrow", "curved_down_ribbon", "curved_left_arrow", "curved_right_arrow", "curved_up_arrow",
    "curved_up_ribbon", "decagon", "diagonal_stripe", "diamond", "dodecagon", "donut", "double_brace",
    "double_bracket", "double_wave", "down_arrow", "down_arrow_callout", "down_ribbon", "explosion1", "explosion2",
    "flowchart_alternate_process", "flowchart_card", "flowchart_collate", "flowchart_connector", "flowchart_data",
    "flowchart_decision", "flowchart_delay", "flowchart_direct_access_storage", "flowchart_display",
    "flowchart_document", "flowchart_extract", "flowchart_internal_storage", "flowchart_magnetic_disk",
    "flowchart_manual_input", "flowchart_manual_operation", "flowchart_merge", "flowchart_multidocument",
    "flowchart_offline_storage", "flowchart_offpage_connector", "flowchart_or", "flowchart_predefined_process",
    "flowchart_preparation", "flowchart_process", "flowchart_punched_tape", "flowchart_sequential_access_storage",
    "flowchart_sort", "flowchart_stored_data", "flowchart_summing_junction", "flowchart_terminator",
    "folded_corner", "frame", "funnel", "gear_6", "gear_9", "half_frame", "heart", "heptagon", "hexagon",
    "horizontal_scroll", "isosceles_triangle", "left_arrow", "left_arrow_callout", "left_brace", "left_bracket",
    "left_circular_arrow", "left_right_arrow", "left_right_arrow_callout", "left_right_circular_arrow",
    "left_right_ribbon", "left_right_up_arrow", "left_up_arrow", "lightning_bolt", "line_callout_1",
    "line_callout_1_accent_bar", "line_callout_1_border_and_accent_bar", "line_callout_1_no_border",
    "line_callout_2", "line_callout_2_accent_bar", "line_callout_2_border_and_accent_bar",
    "line_callout_2_no_border", "line_callout_3", "line_callout_3_accent_bar",
    "line_callout_3_border_and_accent_bar", "line_callout_3_no_border", "line_callout_4",
    "line_callout_4_accent_bar", "line_callout_4_border_and_accent_bar", "line_callout_4_no_border", "line_inverse",
    "math_divide", "math_equal", "math_minus", "math_multiply", "math_not_equal", "math_plus", "moon", "no_symbol",
    "non_isosceles_trapezoid", "notched_right_arrow", "octagon", "oval", "oval_callout", "parallelogram",
    "pentagon", "pie", "pie_wedge", "plaque", "plaque_tabs", "quad_arrow", "quad_arrow_callout", "rectangle",
    "rectangular_callout", "regular_pentagon", "right_arrow", "right_arrow_callout", "right_brace", "right_bracket",
    "right_triangle", "round_1_rectangle", "round_2_diag_rectangle", "round_2_same_rectangle", "rounded_rectangle",
    "rounded_rectangular_callout", "smiley_face", "snip_1_rectangle", "snip_2_diag_rectangle",
    "snip_2_same_rectangle", "snip_round_rectangle", "square_tabs", "star_10_point", "star_12_point",
    "star_16_point", "star_24_point", "star_32_point", "star_4_point", "star_5_point", "star_6_point",
    "star_7_point", "star_8_point", "striped_right_arrow", "sun", "swoosh_arrow", "tear", "trapezoid",
    "u_turn_arrow", "up_arrow", "up_arrow_callout", "up_down_arrow", "up_down_arrow_callout", "up_ribbon",
    "vertical_scroll", "wave",
)
_WORDS = {**{name: name.upper() for name in PRESETS}, **dict(_SHAPE_NAMES)}
SHAPE_TYPES = tuple(_WORDS)


def preset_word(value: Any) -> str:
    """The MSO_AUTO_SHAPE_TYPE name ``value`` names, or BAD_VALUE with the
    closest words."""
    import difflib

    folded = str(value).strip().casefold().replace(" ", "_").replace("-", "_")
    if folded in _WORDS:
        return _WORDS[folded]
    close = difflib.get_close_matches(folded, SHAPE_TYPES, n=3, cutoff=0.6)
    hint = (f"Did you mean {', '.join(close)}? " if close else "") + \
        '{"op": "help", "name": "add_shape"} lists every shape type.'
    raise OpError(Code.BAD_VALUE, f"unknown shape_type '{value}'", hint=hint)


def _op_add_shape(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.shapes.autoshape import AutoShapeType

    slide, _ = slide_of(job, op)
    mso = getattr(MSO_SHAPE, preset_word(op.get("shape_type", "rectangle")))
    adjustments = op.get("adjustments")
    # Every check runs before the shape is drawn: under best_effort a
    # failed op leaves nothing behind.
    if adjustments:
        handles = len(AutoShapeType.default_adjustment_values(mso))
        if len(adjustments) > handles:
            raise OpError(Code.BAD_VALUE, f"this shape takes {plural(handles, 'adjustment')}, not "
                          f"{len(adjustments)}", hint="Give at most as many adjustments as it has handles.")
    pptx_effects.check(outline_coloured=op.get("line_color") is not None, gradient=op.get("gradient"),
                       transparency=op.get("transparency"), line_transparency=op.get("line_transparency"),
                       shadow=op.get("shadow"), glow=op.get("glow"), soft_edges=op.get("soft_edges"),
                       line_dash=op.get("line_dash"))
    shape = slide.shapes.add_shape(mso, emu(op, "left", 1), emu(op, "top", 1), emu(op, "width", 2),
                                   emu(op, "height", 1))
    if op.get("name") is not None:
        _rename(slide, shape, str(op["name"]), ctx)
    for n, value in enumerate(adjustments or ()):
        shape.adjustments[n] = value
    if op.get("text") or pptx_text.wants_text(op):
        job.touch(slide, shape)
    if op.get("text"):
        shape.text = op["text"]
        alignment = paragraph_align(op.get("text_alignment", "center"))
        for para in shape.text_frame.paragraphs:
            para.alignment = alignment
            for run in para.runs:
                apply_run_format(run, {"font_size": op.get("font_size"), "bold": op.get("bold"),
                                        "color": op.get("font_color"), "font_name": op.get("font_name"),
                                        "lang": op.get("lang"), "letter_spacing": op.get("letter_spacing")}, ctx)
        shape.text_frame.word_wrap = True
        pptx_text.format_paragraphs(list(shape.text_frame.paragraphs), op, ctx)
    elif pptx_text.wants_paragraphs(op):
        ctx.warn("line_spacing, space_before, space_after and bullet take effect with text: the shape has none")
    if pptx_text.wants_text(op):
        pptx_text.format_frame(shape.text_frame, op)
    if op.get("fill_color") is not None:
        fill_solid(shape.fill, op["fill_color"], ctx)
    elif op.get("no_fill"):
        shape.fill.background()
    elif op.get("gradient") is None:
        pptx_style.solid_default(slide, shape, size=op.get("font_size"), bold=bool(op.get("bold")),
                                 text_color_set=op.get("font_color") is not None)
    if op.get("line_color") is not None:
        set_color(shape.line.color, op["line_color"], ctx)
    if op.get("line_width") is not None:
        line_width(shape.line, op["line_width"])
    if op.get("rotation"):
        shape.rotation = op["rotation"]
    pptx_effects.apply(shape, ctx, gradient=op.get("gradient"), transparency=op.get("transparency"),
                       line_transparency=op.get("line_transparency"), shadow=op.get("shadow"),
                       glow=op.get("glow"), soft_edges=op.get("soft_edges"), line_dash=op.get("line_dash"))


def _op_set_shape_format(job: PptxJob, ctx: OpCtx, op: dict) -> None:

    slide, position = slide_of(job, op)
    shape = shape_of(slide, op, position)
    pptx_effects.check(outline_coloured=op.get("line_color") is not None or pptx_effects.outline_coloured(shape),
                       gradient=op.get("gradient"), transparency=op.get("transparency"),
                       line_transparency=op.get("line_transparency"), shadow=op.get("shadow"), glow=op.get("glow"),
                       soft_edges=op.get("soft_edges"), line_dash=op.get("line_dash"))
    if op.get("fill_color") is not None:
        fill_solid(shape.fill, op["fill_color"], ctx)
    if op.get("no_fill"):
        shape.fill.background()
    if op.get("line_color") is not None:
        set_color(shape.line.color, op["line_color"], ctx)
    if op.get("line_width") is not None:
        line_width(shape.line, op["line_width"])
    if op.get("rotation") is not None:
        shape.rotation = op["rotation"]
    if op.get("name") is not None:
        _rename(slide, shape, str(op["name"]), ctx)
    if op.get("crop") is not None:
        _crop(shape, op["crop"])
    _alt_text(shape, op.get("alt_text"), op.get("decorative"))
    pptx_effects.apply(shape, ctx, gradient=op.get("gradient"), transparency=op.get("transparency"),
                       line_transparency=op.get("line_transparency"), shadow=op.get("shadow"),
                       glow=op.get("glow"), soft_edges=op.get("soft_edges"), line_dash=op.get("line_dash"))
    box = {key: units.to_emu(op[key], default=IN) for key in ("left", "top", "width", "height")
           if op.get(key) is not None}
    if box:
        from formats.pptx import pptx_geometry

        geometry = {s.shape_id: s for s in pptx_geometry.slide_geometry(job.prs, slide, position).walk()}[
            shape.shape_id]
        if pptx_arrange.move_to(job, slide, position, shape, geometry, box.get("left"), box.get("top"),
                                box.get("width"), box.get("height")):
            ctx.note("the placeholder now keeps a position of its own, no longer the layout's")
        pptx_arrange.refollow(job, slide, position, {shape.shape_id})
    if pptx_text.wants_text(op) or any(op.get(k) is not None for k in ("width", "height")):
        job.touch(slide, shape)
    if pptx_text.wants_text(op):
        if not shape.has_text_frame:
            raise OpError(Code.BAD_VALUE, f"{name_of(shape.name)} holds no text, so it has no spacing, bullets or insets",
                          location=f"slide_index {position}", hint="Give the text keys to a text box or a shape.")
        pptx_text.format_frame(shape.text_frame, op)
        pptx_text.format_paragraphs(list(shape.text_frame.paragraphs), op, ctx)


def _rename(slide: Any, shape: Any, name: str, ctx: OpCtx) -> None:
    if any(other.name == name and other.shape_id != shape.shape_id for other in slide.shapes):
        ctx.warn(f"another shape on the slide is named {name_of(name)}: Morph pairs shapes by name, so give each "
                 "its own")
    shape.name = name


def _op_remove_shape(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    el = shape_of(slide, op, position)._element
    # An animation or a glued connector that still names the shape makes
    # PowerPoint repair the file.
    ids = pptx_animations.shape_ids(el)
    animations = pptx_animations.drop_animations(slide._element, ids)
    connectors = pptx_animations.drop_glue(slide._element, ids)
    el.getparent().remove(el)
    note = pptx_animations.removal_note(animations, connectors)
    if note:
        ctx.note(note)


def _group_member(slide: Any, ref: Any, position: int) -> Any:
    """A shape named by its name, its whole text or its shape_index; a digit
    string that names no shape is an index."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        return shape_of(slide, {"shape_index": ref}, position)
    try:
        return shape_of(slide, {"shape_name": ref}, position)
    except OpError:
        if ref.strip().isdigit():
            return shape_of(slide, {"shape_index": int(ref)}, position)
        raise


def _groupable(shape: Any) -> None:
    """Refuse, before anything moves, a shape PowerPoint does not group or
    one python-pptx cannot size the group around."""
    label = name_of(shape.name)
    if shape._element.tag.rsplit("}", 1)[-1] == "contentPart":
        raise OpError(Code.UNSUPPORTED, f"{label} is ink and cannot be grouped")
    if shape.is_placeholder:
        raise OpError(Code.UNSUPPORTED, f"{label} is a placeholder, and PowerPoint does not group placeholders",
                      hint="Group the other shapes, or add the content as a text box.")
    if getattr(shape, "has_table", False) and shape.has_table:
        raise OpError(Code.UNSUPPORTED, f"{label} is a table, and PowerPoint does not group tables")
    if None in (shape.left, shape.top, shape.width, shape.height):
        raise OpError(Code.BAD_VALUE, f"{label} has no position and size of its own",
                      hint="Set them with set_shape_format first.")


def _op_add_group_shape(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.oxml.ns import qn

    slide, position = slide_of(job, op)
    refs = op.get("shapes")
    if refs is None:
        group = slide.shapes.add_group_shape()
        if op.get("name"):
            group.name = op["name"]
        ctx.warn("an empty group was added: pass shapes: [...] to group shapes")
        return
    members = [_group_member(slide, ref, position) for ref in refs]
    if len({id(m._element) for m in members}) != len(members):
        raise OpError(Code.BAD_VALUE, "a shape is named twice in shapes")
    if len(members) < 2:
        raise OpError(Code.BAD_VALUE, "a group needs at least two shapes")
    for member in members:
        _groupable(member)
    tree = slide.shapes._spTree
    order = {el: i for i, el in enumerate(tree)}
    # python-pptx stacks members in the order given; paint order keeps the
    # slide's stacking, and the group takes its topmost member's place.
    members.sort(key=lambda m: order[m._element])
    elements = {m._element for m in members}
    anchor = members[-1]._element.getnext()
    while anchor is not None and anchor in elements:
        anchor = anchor.getnext()
    group = slide.shapes.add_group_shape(members)
    if anchor is not None and anchor is not group._element:
        anchor.addprevious(group._element)
    if op.get("name"):
        group.name = op["name"]
    timing = slide._element.find(qn("p:timing"))
    if timing is not None:
        animated = {t.get("spid") for t in timing.iter(qn("p:spTgt"))}
        for member in members:
            if str(member.shape_id) in animated:
                ctx.warn(f"{name_of(member.name)} has an animation; PowerPoint may drop it inside a group")


def _point(p: Any, n: int) -> tuple[int, int]:
    try:
        if isinstance(p, (list, tuple)):
            x, y = p[0], p[1]
        else:
            x, y = p["x"], p["y"]
        return units.to_emu(x, default=IN), units.to_emu(y, default=IN)
    except (KeyError, IndexError, TypeError):
        raise OpError(Code.BAD_VALUE, f"points[{n}] is not a point",
                      hint="Give each point as [x, y] or {\"x\": x, \"y\": y} in inches.") from None


def _op_add_freeform(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, _ = slide_of(job, op)
    points = op.get("points", [])
    if len(points) < 2:
        raise OpError(Code.BAD_VALUE, "a freeform needs at least 2 points")
    coords = [_point(p, n) for n, p in enumerate(points)]
    builder = slide.shapes.build_freeform(*coords[0])
    builder.add_line_segments(coords[1:], close=op.get("close", True))
    shape = builder.convert_to_shape()
    if op.get("fill_color") is not None:
        fill_solid(shape.fill, op["fill_color"], ctx)
    else:
        pptx_style.solid_default(slide, shape)
    if op.get("line_color") is not None:
        set_color(shape.line.color, op["line_color"], ctx)


def _op_add_connector(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from lxml import etree
    from pptx.enum.shapes import MSO_CONNECTOR_TYPE
    from pptx.oxml.ns import qn

    slide, position = slide_of(job, op)
    for end in ("start", "end"):
        if op.get(f"{end}_shape") is not None and (op.get(f"{end}_x") is not None or op.get(f"{end}_y") is not None):
            raise OpError(Code.BAD_VALUE, f"the {end} is given as a shape and as a point",
                          hint=f"Give {end}_shape or {end}_x and {end}_y, not both.")
    connector = slide.shapes.add_connector(
        MSO_CONNECTOR_TYPE.STRAIGHT, emu(op, "start_x", 1), emu(op, "start_y", 1),
        emu(op, "end_x", 5), emu(op, "end_y", 1))
    _glue_ends(job, ctx, slide, position, connector, start=op.get("start_shape"), end=op.get("end_shape"),
               start_side=op.get("start_side"), end_side=op.get("end_side"))
    if op.get("line_color") is not None:
        set_color(connector.line.color, op["line_color"], ctx)
    if op.get("line_width") is not None:
        line_width(connector.line, op["line_width"])
    ln = connector.line._get_or_add_ln() if op.get("arrow_start") or op.get("arrow_end") else None
    for key, tag in (("arrow_start", "a:headEnd"), ("arrow_end", "a:tailEnd")):
        if op.get(key):
            order.insert_child(ln, etree.Element(qn(tag), type="arrow"))


def _side(value: Any) -> str | None:
    if value is None:
        return None
    word = str(value).strip().casefold()
    if word not in pptx_arrange.SIDES:
        raise OpError(Code.BAD_VALUE, f"unknown side '{value}'", hint="Use top, left, bottom or right.")
    return word


def _glue_ends(job: PptxJob, ctx: OpCtx, slide: Any, position: int, connector: Any, *, start: Any, end: Any,
               start_side: Any, end_side: Any) -> None:
    """Put the connector's ends on the shapes named, glued where the shape
    allows it."""
    if start is None and end is None:
        if start_side is not None or end_side is not None:
            raise OpError(Code.BAD_VALUE, "start_side and end_side need start_shape or end_shape",
                          hint="Name the shapes the connector joins.")
        return
    shapes = {name: pptx_arrange.shape_ref(slide, ref, position) for name, ref in (("start", start), ("end", end))
              if ref is not None}
    from formats.pptx import pptx_geometry

    geometry = {s.shape_id: s for s in pptx_geometry.slide_geometry(job.prs, slide, position).walk()}
    boxes = {name: geometry[shape.shape_id] for name, shape in shapes.items()}
    sides = {"start": _side(start_side), "end": _side(end_side)}
    if len(boxes) == 2:
        facing = pptx_arrange.facing(boxes["start"].box, boxes["end"].box)
        sides = {"start": sides["start"] or facing[0], "end": sides["end"] or facing[1]}
    loose = []
    for name, shape in shapes.items():
        side = sides[name] or "right"
        if not pptx_arrange.glue(connector, name, shape, boxes[name], side):
            loose.append(f"shape_id {shape.shape_id}")
    if loose:
        ctx.note(f"the connector meets {' and '.join(loose)} at the side of its box without being glued: only "
                 "rectangles, rounded rectangles, text boxes, pictures and placeholders outside a group take glue")


def _alt_text(shape: Any, alt_text: Any, decorative: Any) -> None:
    """A shape's alt text (cNvPr descr) and its decorative flag (the Office
    extension PowerPoint writes)."""
    from lxml import etree

    c_nv_pr = next(iter(shape._element.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr")))
    if alt_text is not None:
        c_nv_pr.set("descr", str(alt_text))
    if decorative is None:
        return
    a_ns, adec = "http://schemas.openxmlformats.org/drawingml/2006/main", DECORATIVE_NS
    ext_lst = c_nv_pr.find(f"{{{a_ns}}}extLst")
    for ext in list(ext_lst if ext_lst is not None else ()):
        if ext.get("uri") == DECORATIVE_URI:
            ext_lst.remove(ext)
    if decorative:
        if ext_lst is None:
            ext_lst = etree.Element(f"{{{a_ns}}}extLst")
            order.insert_child(c_nv_pr, ext_lst)
        ext = etree.SubElement(ext_lst, f"{{{a_ns}}}ext", uri=DECORATIVE_URI)
        etree.SubElement(ext, f"{{{adec}}}decorative", nsmap={"adec": adec}, val="1")
    elif ext_lst is not None and not len(ext_lst):
        c_nv_pr.remove(ext_lst)


def _crop(picture: Any, crop: Any) -> None:
    if not hasattr(picture, "crop_left"):
        raise OpError(Code.BAD_VALUE, "crop works on pictures", hint="Name a picture, or leave crop out.")
    sides = {side: crop.get(side, 0) for side in ("left", "top", "right", "bottom")}
    if any(not 0 <= v <= 0.9 for v in sides.values()) or sides["left"] + sides["right"] >= 1 or \
            sides["top"] + sides["bottom"] >= 1:
        raise OpError(Code.BAD_VALUE, "crop takes shares of the picture from 0 to 0.9, with left + right and "
                      "top + bottom under 1", hint='Write {"left": 0.1, "right": 0.1}.')
    picture.crop_left, picture.crop_top = sides["left"], sides["top"]
    picture.crop_right, picture.crop_bottom = sides["right"], sides["bottom"]


def _op_add_image(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from pptx.enum.shapes import MSO_SHAPE

    slide, _ = slide_of(job, op)
    width = emu(op, "width", 0) if op.get("width") else None
    height = emu(op, "height", 0) if op.get("height") else None
    picture = slide.shapes.add_picture(op["image_path"], emu(op, "left", 1), emu(op, "top", 1), width=width,
                                       height=height)
    if op.get("crop") is not None:
        _crop(picture, op["crop"])
    if op.get("shape_type") is not None:
        picture.auto_shape_type = getattr(MSO_SHAPE, preset_word(op["shape_type"]))
    _alt_text(picture, op.get("alt_text"), op.get("decorative"))


DECORATIVE_NS = "http://schemas.microsoft.com/office/drawing/2017/decorative"
DECORATIVE_URI = "{C183D7F6-B498-43B3-948B-1728B52AA6E4}"
_CROP = KeySpec("crop", ("dict",), fields=tuple(KeySpec(side, ("float",), help="0-0.9 of the picture")
                                                for side in ("left", "top", "right", "bottom")),
                help="pictures: {left, top, right, bottom} shares cut off")
_ALT = (KeySpec("alt_text", ("str",), help="what a screen reader says"),
        KeySpec("decorative", ("bool",), help="true: a screen reader skips it"))

SHAPE_OPS: tuple[OpSpec, ...] = (
    OpSpec("add_shape", _op_add_shape, (
        slide_key(),
        KeySpec("shape_type", ("str",), default="rectangle",
                help="any PowerPoint preset in snake case: " + ", ".join(SHAPE_TYPES)),
        KeySpec("name", ("str",), help="its name, to find it later; Morph pairs shapes by name"),
        KeySpec("adjustments", ("list",), of=("float",),
                help="the preset's handles in order, as fractions (0.5 is 50 %: a rounded corner, an arrow's head)"),
        *box_keys(1, 1, 2, 1),
        KeySpec("text", ("str",)),
        KeySpec("text_alignment", ("str",), default="center"),
        KeySpec("font_size", ("float",), help="pt"),
        KeySpec("bold", ("bool",)),
        KeySpec("font_color", ("color",), aliases=("text_color",)),
        KeySpec("font_name", ("str",)),
        FILL, KeySpec("no_fill", ("bool",)), *LINE,
        KeySpec("rotation", ("float",), help="degrees"), *pptx_effects.KEYS, *pptx_text.RUN_KEYS, *TEXT_FRAME,
    ), G_SHAPES, note="without fill_color or no_fill: the theme's accent 1, with text that reads on it",
        examples=({"op": "add_shape", "slide_index": 0, "shape_type": "chevron", "text": "Build",
                            "fill_color": "#1F4E79", "font_color": "white", "line_width": 0},
                           {"op": "add_shape", "slide_index": 0, "shape_type": "rounded_rectangle", "text": "Q3",
                            "name": "Quarter card",
                            "gradient": {"colors": ["#0B2545", "#2E75B6"], "angle": 90},
                            "shadow": {"blur": 8, "distance": 3, "transparency": 60}})),
    OpSpec("set_shape_format", _op_set_shape_format, (
        slide_key(), *SHAPE_REF, FILL, KeySpec("no_fill", ("bool",)), *LINE,
        KeySpec("rotation", ("float",), help="degrees"), *pptx_effects.KEYS, *box_keys(None, None, None, None),
        KeySpec("name", ("str",), help="renames it; Morph pairs shapes by name ('!!name' forces a pair)"),
        _CROP, *_ALT, *TEXT_FRAME,
    ), G_SHAPES, one_of=SHAPE_ONE_OF, note="restyles, resizes and moves a shape", examples=(
        {"op": "set_shape_format", "slide_index": 0, "shape_name": "Card", "fill_color": "accent2", "top": 2},
        {"op": "set_shape_format", "slide_index": 0, "shape_index": 0, "width": 4},
        {"op": "set_shape_format", "slide_index": 0, "shape_id": 2, "rotation": 5},
        {"op": "set_shape_format", "slide_index": 0, "shape_name": "Card", "transparency": 30, "line_color": "#1F4E79",
         "line_transparency": 40, "line_dash": "dash", "glow": {"color": "accent2", "size": 6},
         "soft_edges": 2})),
    OpSpec("remove_shape", _op_remove_shape, (slide_key(), *SHAPE_REF), G_SHAPES, one_of=SHAPE_ONE_OF,
           examples=({"op": "remove_shape", "slide_index": 0, "shape_name": "Card"},
                     {"op": "remove_shape", "slide_index": 0, "shape_index": 2},
                     {"op": "remove_shape", "slide_index": 0, "shape_id": 2})),
    OpSpec("add_group_shape", _op_add_group_shape, (
        slide_key(),
        KeySpec("shapes", ("list",), of=("str", "int"),
                help="names, whole texts or shape_index values, two or more; not placeholders or tables"),
        KeySpec("name", ("str",)),
    ), G_SHAPES, note="moves the shapes into one group",
        setup=({"op": "add_shape", "slide_index": 1, "text": "A"}, {"op": "add_shape", "slide_index": 1, "text": "B"}),
        examples=({"op": "add_group_shape", "slide_index": 1, "shapes": ["A", "B"], "name": "Pair"},)),
    OpSpec("add_freeform", _op_add_freeform, (
        slide_key(),
        KeySpec("points", ("list",), required=True, help="[[x, y] or {x, y}, ...] in inches, 2 or more"),
        KeySpec("close", ("bool",), default=True), FILL, KeySpec("line_color", ("color",)),
    ), G_SHAPES, examples=({"op": "add_freeform", "slide_index": 0, "points": [[1, 1], [3, 1], {"x": 2, "y": 2.5}],
                            "fill_color": "#E8EEF5"},)),
    OpSpec("add_connector", _op_add_connector, (
        slide_key(),
        KeySpec("start_x", ("length",), unit=IN, default=1), KeySpec("start_y", ("length",), unit=IN, default=1),
        KeySpec("end_x", ("length",), unit=IN, default=5), KeySpec("end_y", ("length",), unit=IN, default=1),
        *LINE, KeySpec("arrow_start", ("bool",)), KeySpec("arrow_end", ("bool",)),
        KeySpec("start_shape", ("int", "str", "dict"), help='the shape it starts on: shape_index, name, or '
                                                            '{"shape_id": n}'),
        KeySpec("end_shape", ("int", "str", "dict"), help="the shape it ends on"),
        KeySpec("start_side", ("str",), help="top | left | bottom | right; default the sides that face"),
        KeySpec("end_side", ("str",), help="top | left | bottom | right"),
    ), G_SHAPES, note="a straight line or arrow; joined to shapes it stays glued to them when they move",
        examples=({"op": "add_connector", "slide_index": 0, "start_x": 2, "start_y": 3, "end_x": 6, "end_y": 3,
                   "line_color": "#7F7F7F", "line_width": 1.5, "arrow_start": True, "arrow_end": True},
                  {"op": "add_connector", "slide_index": 0, "start_shape": "Card", "end_shape": {"shape_id": 2},
                   "start_side": "top", "end_side": "bottom", "arrow_end": True})),
)

IMAGE_OPS: tuple[OpSpec, ...] = (
    OpSpec("add_image", _op_add_image, (
        slide_key(), KeySpec("image_path", ("path",), required=True), *box_keys(1, 1, None, None), _CROP,
        KeySpec("shape_type", ("str",), help="the picture's outline: oval, rounded_rectangle... (add_shape's types)"),
        *_ALT,
    ), G_IMAGES, note="width or height alone keeps the aspect ratio; a crop keeps the frame's size",
        examples=({"op": "add_image", "slide_index": 0, "image_path": EXAMPLE_PNG, "left": 9, "width": 3},
                  {"op": "add_image", "slide_index": 0, "image_path": EXAMPLE_PNG, "left": 1, "width": 2,
                   "crop": {"left": 0.1, "right": 0.1}, "shape_type": "oval", "alt_text": "Our logo"},
                  {"op": "add_image", "slide_index": 0, "image_path": EXAMPLE_PNG, "top": 5, "width": 1,
                   "decorative": True})),
)
