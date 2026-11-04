"""write_pptx's object animations: entrance, emphasis and exit effects in a
slide's timing tree, and the clean-up that removes what names a shape.

The effect rows are PowerPoint-authored ``p:cTn`` rows from ppt-master's
preset table, a subset kept in ``pptx_animation_presets.json`` (its
``source`` names the project, its copyright and licence). The timing tree
that holds them (click groups, beats, ids, start conditions) is this
module's own code, and it appends to a tree another application wrote.

A slide's timing tree: the root (``tmRoot``) holds the main sequence
(``mainSeq``), whose children are click groups; a group holds beats (rows
that start together); a beat holds the effect rows. A row's ``nodeType``
says what starts it: a click, the row before it, or the row before it
ending.
"""

import functools
import json
from pathlib import Path
from typing import Any

from catalog import KeySpec, OpSpec
from formats.pptx.pptx_opkit import (
    G_VISUAL,
    SHAPE_ONE_OF,
    SHAPE_REF,
    PptxJob,
    plural,
    shape_of,
    slide_key,
    slide_of,
)
from results import Code, NoOp, OpCtx, OpError

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESETS_FILE = Path(__file__).with_name("pptx_animation_presets.json")

ANIMATION_TYPES = ("entrance", "emphasis", "exit")
EFFECTS = {
    "entrance": ("appear", "fade", "fly", "wipe", "zoom", "split", "wheel", "float", "rise_up", "dissolve", "blinds",
                 "grow_and_turn"),
    "emphasis": ("grow_shrink", "spin", "teeter", "transparency"),
    "exit": ("fade", "fly", "wipe", "zoom", "split", "dissolve", "float", "descend"),
}
TRIGGERS = ("on_click", "with_previous", "after_previous")
_NODE_TYPES = {"on_click": "clickEffect", "with_previous": "withEffect", "after_previous": "afterEffect"}
_BUILDS = ("bldP", "bldGraphic", "bldDgm", "bldOleChart")
MAX_MS = 4_294_967_295


def _q(local: str, ns: str = P_NS) -> str:
    return f"{{{ns}}}{local}"


def _local(element: Any) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


@functools.cache
def presets() -> dict[str, dict[str, Any]]:
    """The preset rows, read once on the first animation operation."""
    return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))["effects"]


# ---------------------------------------------------------------------------
# Reading a slide's timing
# ---------------------------------------------------------------------------


def timings(sld: Any) -> tuple[Any | None, list[Any]]:
    """(the slide's timing as its child: plain ``p:timing`` or its
    mc:AlternateContent wrapper, every ``p:timing`` tree under it). A
    wrapped timing's first tree is its Fallback's, the one readers see."""
    for child in sld:
        if _local(child) == "timing":
            return child, [child]
        if _local(child) == "AlternateContent" and child.find(f".//{_q('timing')}") is not None:
            trees = list(child.iter(_q("timing")))
            fallback = [t for t in trees if _local(t.getparent()) == "Fallback"]
            return child, fallback + [t for t in trees if t not in fallback]
    return None, []


def is_row(ctn: Any) -> bool:
    """An effect row: the ``p:cTn`` the animation pane lists."""
    return ctn.get("presetClass") is not None


def rows(tree: Any) -> list[Any]:
    return [ctn for ctn in tree.iter(_q("cTn")) if is_row(ctn)]


def count_animations(sld: Any) -> int:
    """The slide's effect rows, a wrapped tree's counted once."""
    _, trees = timings(sld)
    return len(rows(trees[0])) if trees else 0


def _targets(element: Any) -> set[int]:
    found = set()
    for target in element.iter(_q("spTgt")):
        try:
            found.add(int(target.get("spid", "")))
        except ValueError:
            continue
    return found


def _delay(ctn: Any) -> int:
    """A time node's largest numeric start delay."""
    conds = ctn.find(_q("stCondLst"))
    values = [int(c.get("delay")) for c in (conds if conds is not None else ()) if (c.get("delay") or "").isdigit()]
    return max(values, default=0)


def row_length(row: Any) -> int:
    """How long a row plays: the latest end of its behaviours. A behaviour
    that runs indefinitely or repeats counts its own numeric duration, else
    nothing."""
    ends = [_delay(ctn) + int(ctn.get("dur")) for ctn in row.iter(_q("cTn"))
            if ctn is not row and (ctn.get("dur") or "").isdigit()]
    return max(ends, default=0)


def _main_sequence(tree: Any) -> Any | None:
    return next((ctn for ctn in tree.iter(_q("cTn")) if ctn.get("nodeType") == "mainSeq"), None)


def _next_id(tree: Any) -> int:
    ids = [int(c.get("id")) for c in tree.iter(_q("cTn")) if (c.get("id") or "").isdigit()]
    return max(ids, default=0) + 1


# ---------------------------------------------------------------------------
# Writing rows
# ---------------------------------------------------------------------------


def _element(xml: str) -> Any:
    from lxml import etree

    return etree.fromstring(xml)


def _sub(parent: Any, local: str, **attrs: str) -> Any:
    from lxml import etree

    return etree.SubElement(parent, _q(local), **attrs)


_SKELETON = (
    f'<p:timing xmlns:p="{P_NS}"><p:tnLst><p:par>'
    '<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
    '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst/></p:cTn>'
    '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
    '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
    '</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>'
)


def _main_sequence_for_writing(sld: Any) -> Any:
    """The slide's main sequence ``cTn``, the timing tree made when the
    slide has none."""
    carrier, trees = timings(sld)
    if carrier is not None and _local(carrier) == "AlternateContent":
        raise OpError(Code.UNSUPPORTED, "the slide's animations are written for newer PowerPoint versions only "
                      "(bounce or media timing), and this tool does not edit them",
                      hint="Run remove_animations on the slide first, or edit them in PowerPoint.")
    if carrier is None:
        timing = _element(_SKELETON)
        ext = next((c for c in sld if _local(c) == "extLst"), None)
        if ext is not None:
            ext.addprevious(timing)
        else:
            sld.append(timing)
        return _main_sequence(timing)
    tree = trees[0]
    main = _main_sequence(tree)
    if main is not None:
        return main
    root = next((c for c in tree.iter(_q("cTn")) if c.get("nodeType") == "tmRoot"), None)
    children = root.find(_q("childTnLst")) if root is not None else None
    if children is None:
        raise OpError(Code.UNSUPPORTED, "the slide's timing has a shape this tool cannot read",
                      hint="Run remove_animations on the slide first, then add the animations again.")
    seq = _element(_SKELETON).find(f".//{_q('seq')}")
    main = seq.find(_q("cTn"))
    main.set("id", str(_next_id(tree)))
    children.insert(0, seq)
    return main


def _scale(row: Any, base_ms: int, wanted_ms: int) -> None:
    """Every finite behaviour duration and inner start delay scaled to the
    wanted length (an instant ``dur="1"`` stays)."""
    ratio = wanted_ms / base_ms
    for ctn in row.iter(_q("cTn")):
        if ctn is row:
            continue
        dur = ctn.get("dur") or ""
        if dur.isdigit() and dur != "1":
            ctn.set("dur", str(max(1, round(int(dur) * ratio))))
        conds = ctn.find(_q("stCondLst"))
        for cond in conds if conds is not None else ():
            delay = cond.get("delay") or ""
            if delay.isdigit():
                cond.set("delay", str(round(int(delay) * ratio)))


def _group_id(tree: Any, shape_id: int) -> int:
    """The next free build group for the shape: an entrance and an exit on
    one shape are two groups."""
    used = [int(r.get("grpId")) for r in rows(tree) if (r.get("grpId") or "").isdigit() and shape_id in _targets(r)]
    return max(used) + 1 if used else 0


def _row(key: str, direction: str | None, *, node_type: str, delay_ms: int, duration_ms: int | None, shape_id: int,
         group: int, next_id: int) -> tuple[Any, int]:
    spec = presets()[key]
    row = _element(spec["directions"][direction] if direction else spec["row_xml"])
    row.set("id", str(next_id))
    row.set("nodeType", node_type)
    row.set("grpId", str(group))
    next_id += 1
    conds = row.find(_q("stCondLst"))
    for cond in list(conds):
        conds.remove(cond)
    _sub(conds, "cond", delay=str(delay_ms))
    if duration_ms and spec["duration_scalable"]:
        _scale(row, int(spec["default_duration_ms"]), duration_ms)
    for ctn in row.iter(_q("cTn")):
        if ctn is not row:
            ctn.set("id", str(next_id))
            next_id += 1
    for target in row.iter(_q("spTgt")):
        target.set("spid", str(shape_id))
    return row, next_id


def _wrapper(parent: Any, ctn_id: int, *conditions: dict[str, str], beat_delay: int | None = None) -> Any:
    """A ``p:par`` time node under ``parent``'s childTnLst; its own
    childTnLst."""
    par = _sub(parent, "par")
    ctn = _sub(par, "cTn", id=str(ctn_id), fill="hold")
    conds = _sub(ctn, "stCondLst")
    if beat_delay is not None:
        _sub(conds, "cond", delay=str(beat_delay))
    for cond in conditions:
        tn = cond.pop("tn", None)
        node = _sub(conds, "cond", **cond)
        if tn is not None:
            _sub(node, "tn", val=tn)
    return _sub(ctn, "childTnLst")


def _children(par: Any) -> Any:
    return par.find(f"{_q('cTn')}/{_q('childTnLst')}")


def add_animation(sld: Any, shape_id: int, key: str, direction: str | None, trigger: str, delay_ms: int,
                  duration_ms: int | None) -> None:
    main = _main_sequence_for_writing(sld)
    tree = next(a for a in main.iterancestors() if _local(a) == "timing")
    next_id = _next_id(tree)
    groups_parent = main.find(_q("childTnLst"))
    if groups_parent is None:
        groups_parent = _sub(main, "childTnLst")
    groups = [g for g in groups_parent if _local(g) == "par"]
    row, _ = _row(key, direction, node_type=_NODE_TYPES[trigger], delay_ms=delay_ms, duration_ms=duration_ms,
                  shape_id=shape_id, group=_group_id(tree, shape_id), next_id=next_id + 2)
    if trigger == "on_click" or not groups:
        conditions = ({"delay": "indefinite"},) if trigger == "on_click" else (
            {"delay": "indefinite"}, {"evt": "onBegin", "delay": "0", "tn": main.get("id")})
        beats = _wrapper(groups_parent, next_id, *conditions)
        beat = _wrapper(beats, next_id + 1, beat_delay=0)
    else:
        beats = _children(groups[-1])
        last = [b for b in beats if _local(b) == "par"][-1]
        if trigger == "with_previous":
            beat = _children(last)
        else:
            beat_ctn = last.find(_q("cTn"))
            ends = [_delay(r) + row_length(r) for r in _children(last).iter(_q("cTn")) if is_row(r)]
            offset = min(_delay(beat_ctn) + max(ends, default=0), MAX_MS)
            beat = _wrapper(beats, next_id, beat_delay=offset)
    par = _sub(beat, "par")
    par.append(row)


# ---------------------------------------------------------------------------
# Removing what names a shape
# ---------------------------------------------------------------------------


def _is_click_group(group_ctn: Any) -> bool:
    conds = group_ctn.find(_q("stCondLst"))
    found = list(conds) if conds is not None else []
    return any(c.get("delay") == "indefinite" for c in found) and not any(c.get("evt") == "onBegin" for c in found)


def _prune(tree: Any) -> None:
    """Drop every time container a removal left empty, deepest first, in
    every sequence (an empty ``p:childTnLst`` breaks the schema); then make
    each click's new first row its click."""
    for container in reversed(list(tree.iter(_q("par"), _q("seq")))):
        ctn = container.find(_q("cTn"))
        if ctn is None or ctn.get("nodeType") == "tmRoot" or is_row(ctn):
            continue
        children = ctn.find(_q("childTnLst"))
        if children is not None and not len(children):
            container.getparent().remove(container)
    for seq_ctn in tree.iter(_q("cTn")):
        kind = seq_ctn.get("nodeType")
        if kind not in ("mainSeq", "interactiveSeq"):
            continue
        groups = seq_ctn.find(_q("childTnLst"))
        for group in groups if groups is not None else ():
            # Each group of a trigger plays on a click of the trigger.
            if kind == "interactiveSeq" or _is_click_group(group.find(_q("cTn"))):
                first = next((r for r in group.iter(_q("cTn")) if is_row(r)), None)
                if first is not None:
                    first.set("nodeType", "clickEffect")


def _empty(tree: Any) -> bool:
    root = next((c for c in tree.iter(_q("cTn")) if c.get("nodeType") == "tmRoot"), None)
    children = root.find(_q("childTnLst")) if root is not None else None
    return children is None or not len(children)


def _drop_in_tree(tree: Any, ids: set[int]) -> int:
    """Remove from one timing tree every node that names a shape in ``ids``;
    the effect rows removed."""
    removed = 0
    for row in list(rows(tree)):
        if _targets(row) & ids:
            par = row.getparent()
            par.getparent().remove(par)
            removed += 1
    for media in list(tree.iter(_q("video"), _q("audio"))):
        if _targets(media) & ids:
            media.getparent().remove(media)
    for seq in list(tree.iter(_q("seq"))):
        ctn = seq.find(_q("cTn"))
        if ctn is not None and ctn.get("nodeType") == "interactiveSeq":
            trigger = ctn.find(_q("stCondLst"))
            if trigger is not None and _targets(trigger) & ids:
                seq.getparent().remove(seq)
    for builds in tree.iter(_q("bldLst")):
        for build in list(builds):
            if _local(build) in _BUILDS and (build.get("spid") or "").isdigit() and int(build.get("spid")) in ids:
                builds.remove(build)
        if not len(builds):
            builds.getparent().remove(builds)
    _prune(tree)
    return removed


def drop_animations(sld: Any, ids: set[int]) -> int:
    """Remove every animation, media command, trigger and build entry that
    names a shape in ``ids`` (in both branches of a wrapped timing), and a
    timing left empty; the rows removed, a wrapped tree's counted once."""
    carrier, trees = timings(sld)
    if carrier is None:
        return 0
    counts = [_drop_in_tree(tree, ids) for tree in trees]
    if all(_empty(tree) for tree in trees):
        sld.remove(carrier)
    return counts[0]


def drop_glue(sld: Any, ids: set[int]) -> int:
    """Unglue every connector end attached to a shape in ``ids`` (the
    connector stays where it is drawn); the connectors unglued."""
    unglued = 0
    for cxn in sld.iter(_q("cNvCxnSpPr")):
        ends = [e for e in cxn if _local(e) in ("stCxn", "endCxn") and (e.get("id") or "").isdigit()
                and int(e.get("id")) in ids]
        for end in ends:
            cxn.remove(end)
        unglued += bool(ends)
    return unglued


def shape_ids(element: Any) -> set[int]:
    """The ids of a shape and of every shape inside it."""
    found = set()
    for node in element.iter(_q("cNvPr")):
        if (node.get("id") or "").isdigit():
            found.add(int(node.get("id")))
    return found


def removal_note(animations: int, connectors: int) -> str | None:
    parts = []
    if animations:
        parts.append(f"removed its {plural(animations, 'animation')}")
    if connectors:
        parts.append(f"unglued {plural(connectors, 'connector')}")
    return " and ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------


def _effect_key(op_type: Any, effect: Any) -> str:
    import difflib

    kind = str(op_type or "entrance").strip().casefold()
    if kind not in ANIMATION_TYPES:
        raise OpError(Code.BAD_VALUE, f"unknown animation_type '{op_type}'",
                      hint="Use entrance, emphasis or exit.")
    word = str(effect).strip().casefold().replace(" ", "_").replace("-", "_")
    if word in EFFECTS[kind]:
        return f"{kind}_{word}"
    close = difflib.get_close_matches(word, EFFECTS[kind], n=1, cutoff=0.6)
    hint = (f"Did you mean '{close[0]}'? " if close else "") + f"{kind.capitalize()} effects: " + \
        ", ".join(EFFECTS[kind]) + "."
    raise OpError(Code.BAD_VALUE, f"'{effect}' is not an {kind} effect", hint=hint)


def _op_add_animation(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    shape = shape_of(slide, op, position)
    if shape._element.getparent().tag == _q("grpSp"):
        raise OpError(Code.UNSUPPORTED, f"shape_id {shape.shape_id} is inside a group, and PowerPoint animates "
                      "a group as one", hint="Animate the group, or ungroup the shapes first.")
    key = _effect_key(op.get("animation_type"), op["effect"])
    spec = presets()[key]
    direction = op.get("direction")
    if direction is not None:
        word = str(direction).strip().casefold().replace(" ", "_").replace("-", "_")
        choices = list(spec.get("directions", {}))
        if word not in choices:
            takes = ", ".join(choices) if choices else "no direction"
            raise OpError(Code.BAD_VALUE, f"the {key.replace('_', ' ', 1)} effect takes {takes}",
                          hint="Leave direction out." if not choices else f"Use one of: {takes}.")
        direction = word
    trigger = str(op.get("trigger", "on_click")).strip().casefold().replace(" ", "_").replace("-", "_")
    if trigger not in TRIGGERS:
        raise OpError(Code.BAD_VALUE, f"unknown trigger '{op.get('trigger')}'",
                      hint="Use on_click, with_previous or after_previous.")
    delay = op.get("delay", 0)
    duration = op.get("duration")
    if not 0 <= delay <= MAX_MS or (duration is not None and not 1 <= duration <= 600_000):
        raise OpError(Code.BAD_VALUE, "delay or duration is out of range",
                      hint="Use a delay of 0 or more ms, and a duration of 1 to 600000 ms.")
    add_animation(slide._element, shape.shape_id, key, direction, trigger, delay, duration)


def _op_remove_animations(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    slide, position = slide_of(job, op)
    sld = slide._element
    if op.get("shape_id") is not None or op.get("shape_name") or op.get("shape_index") is not None:
        shape = shape_of(slide, op, position)
        removed = drop_animations(sld, shape_ids(shape._element))
        if not removed:
            raise NoOp(f"shape_id {shape.shape_id} has no animations")
        ctx.note(f"removed {plural(removed, 'animation')}")
        return
    carrier, _ = timings(sld)
    if carrier is None:
        raise NoOp(f"slide_index {position} has no animations")
    removed = count_animations(sld)
    sld.remove(carrier)
    ctx.note(f"removed {plural(removed, 'animation')}")


_EFFECT_HELP = "; ".join(f"{kind}: {', '.join(names)}" for kind, names in EFFECTS.items())

OPS: tuple[OpSpec, ...] = (
    OpSpec("add_animation", _op_add_animation, (
        slide_key(), *SHAPE_REF,
        KeySpec("effect", ("str",), required=True, help=_EFFECT_HELP),
        KeySpec("animation_type", ("str",), default="entrance", help="entrance | emphasis | exit"),
        KeySpec("trigger", ("str",), default="on_click", help="on_click | with_previous | after_previous"),
        KeySpec("delay", ("int",), default=0, help="ms after its trigger"),
        KeySpec("duration", ("int",), help="ms; each effect has its own default"),
        KeySpec("direction", ("str",), help="fly and wipe: up, down, left, right...; zoom: in, out...; "
                                           "split: horizontal_in...; blinds: horizontal | vertical"),
    ), G_VISUAL, one_of=SHAPE_ONE_OF,
        note="appended to the slide's animation order; LibreOffice and Collabora play them too",
        examples=({"op": "add_animation", "slide_index": 0, "shape_name": "Card", "effect": "fade"},
                  {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fly", "direction": "left",
                   "trigger": "after_previous", "duration": 400},
                  {"op": "add_animation", "slide_index": 0, "shape_index": 0, "effect": "fade",
                   "animation_type": "exit", "trigger": "on_click"},
                  {"op": "add_animation", "slide_index": 0, "shape_id": 4, "effect": "spin",
                   "animation_type": "emphasis", "trigger": "with_previous", "delay": 200})),
    OpSpec("remove_animations", _op_remove_animations, (
        slide_key(), *SHAPE_REF,
    ), G_VISUAL, note="a shape's animations, or with no shape every animation on the slide",
        setup=({"op": "add_animation", "slide_index": 0, "shape_name": "Card", "effect": "fade"},),
        examples=({"op": "remove_animations", "slide_index": 0, "shape_name": "Card"},
                  {"op": "remove_animations", "slide_index": 0, "shape_id": 4},
                  {"op": "remove_animations", "slide_index": 0, "shape_index": 2},
                  {"op": "remove_animations", "slide_index": 0})),
)
