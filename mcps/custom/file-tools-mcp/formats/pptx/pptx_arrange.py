"""Arranging a slide's shapes: their paint order, aligning and distributing
them by the boxes the geometry reads (slide coordinates), and connector
ends glued to shapes, which follow them when they move.

A shape whose box comes from its layout (a placeholder with no position of
its own) is moved with its offset and size written together: python-pptx's
``left`` alone writes an offset with no size and a top of 0.
"""

import math
from typing import Any

from catalog import KeySpec, OpSpec
from formats.pptx.pptx_opkit import (
    G_SHAPES,
    SHAPE_ONE_OF,
    SHAPE_REF,
    PptxJob,
    plural,
    shape_of,
    slide_key,
    slide_of,
)
from ooxml import order
from results import Code, NoOp, OpCtx, OpError

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_SHAPE_TAGS = frozenset({"sp", "pic", "graphicFrame", "grpSp", "cxnSp", "contentPart", "AlternateContent"})
SIDES = ("top", "left", "bottom", "right")
# Connection sites of the rectangle family, in the preset definitions'
# order (python-pptx's begin_connect uses the same): top, left, bottom,
# right, each at the middle of its side.
_RECT_SITES = {"top": 0, "left": 1, "bottom": 2, "right": 3}
_RECT_PRESETS = frozenset({None, "rect", "roundRect"})
ALIGNS = ("left", "center", "right", "top", "middle", "bottom")
ORDERS = ("front", "back", "forward", "backward")


def _local(element: Any) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def shape_ref(slide: Any, ref: Any, position: int) -> Any:
    """A shape a list names: an integer is a shape_index, a string a name,
    a whole text or a digit index, and {"shape_id": n} an id."""
    if isinstance(ref, dict):
        if set(ref) - {"shape_id", "shape_name", "shape_index"} or len(ref) != 1:
            raise OpError(Code.BAD_VALUE, f"a shape reference {ref} takes one of shape_id, shape_name, shape_index",
                          hint='Write {"shape_id": 5}, a name, or a shape_index.')
        return shape_of(slide, ref, position)
    if isinstance(ref, int) and not isinstance(ref, bool):
        return shape_of(slide, {"shape_index": ref}, position)
    try:
        return shape_of(slide, {"shape_name": ref}, position)
    except OpError:
        if str(ref).strip().isdigit():
            return shape_of(slide, {"shape_index": int(ref)}, position)
        raise


def _geometry(job: PptxJob, slide: Any, position: int) -> dict[int, Any]:
    from formats.pptx import pptx_geometry

    return {s.shape_id: s for s in pptx_geometry.slide_geometry(job.prs, slide, position).walk()}


def _in_group(shape: Any) -> bool:
    return _local(shape._element.getparent()) == "grpSp"


def place(shape: Any, x: int, y: int, cx: int, cy: int) -> None:
    """Write the shape's offset and size together."""
    shape.left, shape.top, shape.width, shape.height = int(x), int(y), max(int(cx), 0), max(int(cy), 0)


def inherits_box(shape: Any) -> bool:
    """A placeholder whose position and size come from its layout: it has
    no ``a:xfrm`` of its own (python-pptx reads the layout's for it)."""
    if not shape.is_placeholder:
        return False
    sp_pr = shape._element.find(f"{{{P_NS}}}spPr")
    return sp_pr is None or sp_pr.find(f"{{{A_NS}}}xfrm") is None


def site(box: Any, side: str) -> tuple[int, int]:
    """The middle of a box's side, turned with the shape's rotation."""
    mx, my = box.x + box.cx / 2, box.y + box.cy / 2
    dx, dy = {"top": (0, -box.cy / 2), "left": (-box.cx / 2, 0), "bottom": (0, box.cy / 2),
              "right": (box.cx / 2, 0)}[side]
    turn = math.radians(box.rot)
    return (round(mx + dx * math.cos(turn) - dy * math.sin(turn)),
            round(my + dx * math.sin(turn) + dy * math.cos(turn)))


def facing(start: Any, end: Any) -> tuple[str, str]:
    """The sides two boxes face each other with."""
    dx = (end.x + end.cx / 2) - (start.x + start.cx / 2)
    dy = (end.y + end.cy / 2) - (start.y + start.cy / 2)
    if abs(dx) >= abs(dy):
        return ("right", "left") if dx >= 0 else ("left", "right")
    return ("bottom", "top") if dy >= 0 else ("top", "bottom")


def glue(connector: Any, end: str, shape: Any, geometry: Any, side: str) -> bool:
    """Put the connector's ``end`` ("start" or "end") on the shape's side,
    glued when the shape is of the rectangle family and not inside a
    group; whether it was glued."""
    from lxml import etree

    x, y = site(geometry.box, side)
    if end == "start":
        connector.begin_x, connector.begin_y = x, y
    else:
        connector.end_x, connector.end_y = x, y
    props = connector._element.find(f"{{{P_NS}}}nvCxnSpPr/{{{P_NS}}}cNvCxnSpPr")
    tag = f"{{{A_NS}}}{'stCxn' if end == 'start' else 'endCxn'}"
    for old in props.findall(tag):
        props.remove(old)
    if geometry.preset not in _RECT_PRESETS or _in_group(shape):
        return False
    order.insert_child(props, etree.Element(tag, id=str(shape.shape_id), idx=str(_RECT_SITES[side])))
    return True


def refollow(job: PptxJob, slide: Any, position: int, moved: set[int]) -> int:
    """Re-place the glued connector ends on the shapes in ``moved`` (on
    their sites again); how many connectors moved."""
    if not moved:
        return 0
    geometry = None
    count = 0
    for connector in slide.shapes:
        if _local(connector._element) != "cxnSp":
            continue
        props = connector._element.find(f"{{{P_NS}}}nvCxnSpPr/{{{P_NS}}}cNvCxnSpPr")
        touched = False
        for tag, which in (("stCxn", "start"), ("endCxn", "end")):
            link = props.find(f"{{{A_NS}}}{tag}") if props is not None else None
            if link is None or not (link.get("id") or "").isdigit() or int(link.get("id")) not in moved:
                continue
            geometry = geometry or _geometry(job, slide, position)
            target = geometry.get(int(link.get("id")))
            side = {v: k for k, v in _RECT_SITES.items()}.get(int(link.get("idx", "0")))
            if target is None or side is None:
                continue
            x, y = site(target.box, side)
            if which == "start":
                connector.begin_x, connector.begin_y = x, y
            else:
                connector.end_x, connector.end_y = x, y
            touched = True
        count += touched
    return count


def move_to(job: PptxJob, slide: Any, position: int, shape: Any, geometry: Any, x: int | None = None,
            y: int | None = None, cx: int | None = None, cy: int | None = None) -> bool:
    """Move or size a shape, an inherited placeholder getting its whole box
    (offset and size) from the geometry; whether it had inherited it."""
    box = geometry.box
    inherited = inherits_box(shape)
    place(shape, box.x if x is None else x, box.y if y is None else y, box.cx if cx is None else cx,
          box.cy if cy is None else cy)
    return inherited


# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------


def _siblings(element: Any) -> list[Any]:
    return [c for c in element.getparent() if _local(c) in _SHAPE_TAGS]


def _op_arrange_shape(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    shape = shape_of(slide, op, position)
    word = str(op["order"]).strip().casefold()
    if word not in ORDERS:
        raise OpError(Code.BAD_VALUE, f"unknown order '{op['order']}'", hint="Use front, back, forward or backward.")
    element = shape._element
    shapes = _siblings(element)
    at = shapes.index(element)
    if (word in ("front", "forward") and at == len(shapes) - 1) or (word in ("back", "backward") and at == 0):
        raise NoOp(f"shape_id {shape.shape_id} is already at the {'front' if word in ('front', 'forward') else 'back'}")
    target = {"front": shapes[-1], "back": shapes[0], "forward": shapes[min(at + 1, len(shapes) - 1)],
              "backward": shapes[max(at - 1, 0)]}[word]
    if word in ("front", "forward"):
        target.addnext(element)
    else:
        target.addprevious(element)


def _op_align_shapes(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    shapes = [shape_ref(slide, ref, position) for ref in op["shapes"]]
    relative = str(op.get("relative_to", "shapes")).strip().casefold()
    if relative not in ("shapes", "slide"):
        raise OpError(Code.BAD_VALUE, f"unknown relative_to '{op.get('relative_to')}'", hint="Use shapes or slide.")
    if len(shapes) < (2 if relative == "shapes" else 1):
        raise OpError(Code.BAD_VALUE, "align needs two or more shapes, or relative_to: slide",
                      hint="Name the shapes to line up in shapes.")
    align = str(op["align"]).strip().casefold()
    if align not in ALIGNS:
        raise OpError(Code.BAD_VALUE, f"unknown align '{op['align']}'", hint=f"Use one of: {', '.join(ALIGNS)}.")
    for shape in shapes:
        if _in_group(shape):
            raise OpError(Code.UNSUPPORTED, f"shape_id {shape.shape_id} is inside a group",
                          hint="Align the group, or ungroup the shapes first.")
    geometry = _geometry(job, slide, position)
    boxes = [geometry[s.shape_id].box for s in shapes]
    if relative == "slide":
        lo_x, hi_x, lo_y, hi_y = 0, job.prs.slide_width, 0, job.prs.slide_height
    else:
        lo_x, hi_x = min(b.x for b in boxes), max(b.right for b in boxes)
        lo_y, hi_y = min(b.y for b in boxes), max(b.y + b.cy for b in boxes)
    inherited = 0
    for shape, box in zip(shapes, boxes, strict=True):
        x, y = box.x, box.y
        if align == "left":
            x = lo_x
        elif align == "right":
            x = hi_x - box.cx
        elif align == "center":
            x = round((lo_x + hi_x - box.cx) / 2)
        elif align == "top":
            y = lo_y
        elif align == "bottom":
            y = hi_y - box.cy
        else:
            y = round((lo_y + hi_y - box.cy) / 2)
        inherited += move_to(job, slide, position, shape, geometry[shape.shape_id], x, y)
    if inherited:
        ctx.note(f"{plural(inherited, 'placeholder')} now keep a position of their own, no longer the layout's")
    refollow(job, slide, position, {s.shape_id for s in shapes})


def _op_distribute_shapes(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    shapes = [shape_ref(slide, ref, position) for ref in op["shapes"]]
    if len(shapes) < 3:
        raise OpError(Code.BAD_VALUE, "distribute needs three or more shapes",
                      hint="Name at least three shapes; the outer two stay where they are.")
    direction = str(op.get("direction", "horizontal")).strip().casefold()
    if direction not in ("horizontal", "vertical"):
        raise OpError(Code.BAD_VALUE, f"unknown direction '{op.get('direction')}'",
                      hint="Use horizontal or vertical.")
    for shape in shapes:
        if _in_group(shape):
            raise OpError(Code.UNSUPPORTED, f"shape_id {shape.shape_id} is inside a group",
                          hint="Distribute the group, or ungroup the shapes first.")
    geometry = _geometry(job, slide, position)
    across = direction == "horizontal"
    ranked = sorted(shapes, key=lambda s: geometry[s.shape_id].box.x if across else geometry[s.shape_id].box.y)
    boxes = [geometry[s.shape_id].box for s in ranked]
    start = boxes[0].x if across else boxes[0].y
    end = boxes[-1].right if across else boxes[-1].y + boxes[-1].cy
    sizes = [b.cx if across else b.cy for b in boxes]
    gap = (end - start - sum(sizes)) / (len(boxes) - 1)
    at = start
    inherited = 0
    for shape, size in zip(ranked, sizes, strict=True):
        if across:
            inherited += move_to(job, slide, position, shape, geometry[shape.shape_id], x=round(at))
        else:
            inherited += move_to(job, slide, position, shape, geometry[shape.shape_id], y=round(at))
        at += size + gap
    if gap < 0:
        ctx.warn("the shapes are wider than the room between the outer two, so they overlap; move the outer two "
                 "apart first")
    if inherited:
        ctx.note(f"{plural(inherited, 'placeholder')} now keep a position of their own, no longer the layout's")
    refollow(job, slide, position, {s.shape_id for s in shapes})


_REFS_HELP = 'the shapes: shape_index numbers, names (or whole texts), or {"shape_id": n}'

OPS: tuple[OpSpec, ...] = (
    OpSpec("arrange_shape", _op_arrange_shape, (
        slide_key(), *SHAPE_REF,
        KeySpec("order", ("str",), required=True, help="front | back | forward | backward"),
    ), G_SHAPES, one_of=SHAPE_ONE_OF, note="the shape's place in the paint order: in front of or behind the others",
        examples=({"op": "arrange_shape", "slide_index": 0, "shape_name": "Card", "order": "back"},
                  {"op": "arrange_shape", "slide_index": 0, "shape_id": 4, "order": "forward"},
                  {"op": "arrange_shape", "slide_index": 0, "shape_index": 0, "order": "front"})),
    OpSpec("align_shapes", _op_align_shapes, (
        slide_key(), KeySpec("shapes", ("list",), required=True, help=_REFS_HELP),
        KeySpec("align", ("str",), required=True, help=" | ".join(ALIGNS)),
        KeySpec("relative_to", ("str",), default="shapes", help="shapes | slide"),
    ), G_SHAPES, note="lines the shapes' frames up (a turned shape by its unturned frame); glued connectors follow",
        examples=({"op": "align_shapes", "slide_index": 0, "shapes": ["Card", 0], "align": "top"},
                  {"op": "align_shapes", "slide_index": 0, "shapes": [{"shape_id": 4}], "align": "center",
                   "relative_to": "slide"})),
    OpSpec("distribute_shapes", _op_distribute_shapes, (
        slide_key(), KeySpec("shapes", ("list",), required=True, help=_REFS_HELP),
        KeySpec("direction", ("str",), default="horizontal", help="horizontal | vertical"),
    ), G_SHAPES, note="equal gaps between three or more shapes; the outer two stay",
        setup=({"op": "add_shape", "slide_index": 1, "left": 1}, {"op": "add_shape", "slide_index": 1, "left": 4},
               {"op": "add_shape", "slide_index": 1, "left": 9}),
        examples=({"op": "distribute_shapes", "slide_index": 1, "shapes": [0, 1, 2]},
                  {"op": "distribute_shapes", "slide_index": 1, "shapes": [0, 1, 2], "direction": "vertical"})),
)
