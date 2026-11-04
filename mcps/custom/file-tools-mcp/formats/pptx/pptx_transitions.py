"""write_pptx's slide transitions: PowerPoint's transition gallery with the
direction, orientation and Morph options each effect takes, written as
PowerPoint writes them.

The registry is ppt-master's transition table:

    ppt-master, https://github.com/hugohe3/ppt-master, MIT License,
    Copyright (c) 2025-2026 Hugo He.
    Changed by OtoDock: the table kept for the direction, orientation and
    morph_by options, the writer re-implemented on lxml, and the seven
    transitions write_pptx already took left as they were written.

A transition PowerPoint 2007 knows is a plain ``p:transition``. A newer one
(the p14, p15 and p159 namespaces) is wrapped in ``mc:AlternateContent``:
the Choice holds the effect, the Fallback a fade, which LibreOffice and
older readers show.
"""

import difflib
from typing import Any

from catalog import KeySpec, OpSpec
from formats.pptx.pptx_opkit import (
    G_VISUAL,
    PptxJob,
    slide_key,
    slide_of,
)
from results import Code, OpCtx, OpError

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NAMESPACES = {
    "p14": "http://schemas.microsoft.com/office/powerpoint/2010/main",
    "p15": "http://schemas.microsoft.com/office/powerpoint/2012/main",
    "p159": "http://schemas.microsoft.com/office/powerpoint/2015/09/main",
}

_CARDINAL = {"left": None, "right": "r", "up": "u", "down": "d"}
_CORNERS = {**_CARDINAL, "up_left": "lu", "up_right": "ru", "down_left": "ld", "down_right": "rd"}
_LEFT_RIGHT = {"left": "l", "right": "r"}
_ACROSS = {"horizontal": None, "vertical": "vert"}


def _attr(attribute: str, values: dict[str, str | None]) -> dict[str, dict[str, str | None]]:
    return {name: {attribute: value} for name, value in values.items()}


# name -> (namespace prefix, element, attributes, {option: {value: attributes}}).
# An attribute set to None is left out (the schema's default).
REGISTRY: dict[str, tuple[str, str, dict[str, str], dict[str, dict[str, dict[str, str | None]]]]] = {
    "fade": ("p", "fade", {}, {}),
    "push": ("p", "push", {"dir": "r"}, {"direction": _attr("dir", {**_CARDINAL, "left": "l"})}),
    "wipe": ("p", "wipe", {"dir": "r"}, {"direction": _attr("dir", _CARDINAL)}),
    "split": ("p", "split", {"orient": "horz", "dir": "out"},
              {"orientation": _attr("orient", {"horizontal": None, "vertical": "vert"}),
               "direction": _attr("dir", {"in": "in", "out": None})}),
    "reveal": ("p14", "reveal", {"dir": "r"}, {"direction": _attr("dir", {"left": None, "right": "r"})}),
    "cut": ("p", "cut", {"thruBlk": "0"}, {}),
    "random_bars": ("p", "randomBar", {"dir": "vert"}, {"orientation": _attr("dir", _ACROSS)}),
    "shape": ("p", "circle", {}, {}),
    "uncover": ("p", "pull", {"dir": "r"}, {"direction": _attr("dir", _CORNERS)}),
    "cover": ("p", "cover", {"dir": "r"}, {"direction": _attr("dir", _CORNERS)}),
    "flash": ("p14", "flash", {}, {}),
    "fall_over": ("p15", "prstTrans", {"prst": "fallOver", "invX": "1"},
                  {"direction": _attr("invX", {"left": None, "right": "1"})}),
    "drape": ("p15", "prstTrans", {"prst": "drape", "invX": "1"},
              {"direction": _attr("invX", {"left": None, "right": "1"})}),
    "curtains": ("p15", "prstTrans", {"prst": "curtains"}, {}),
    "wind": ("p15", "prstTrans", {"prst": "wind"}, {"direction": _attr("invX", {"left": "1", "right": None})}),
    "prestige": ("p15", "prstTrans", {"prst": "prestige"}, {}),
    "fracture": ("p15", "prstTrans", {"prst": "fracture"}, {}),
    "crush": ("p15", "prstTrans", {"prst": "crush"}, {}),
    "peel_off": ("p15", "prstTrans", {"prst": "peelOff", "invX": "1"},
                 {"direction": _attr("invX", {"left": None, "right": "1"})}),
    "page_curl": ("p15", "prstTrans", {"prst": "pageCurlSingle", "invX": "1"},
                  {"direction": _attr("invX", {"left": None, "right": "1"})}),
    "airplane": ("p15", "prstTrans", {"prst": "airplane"},
                 {"direction": _attr("invX", {"left": "1", "right": None})}),
    "origami": ("p15", "prstTrans", {"prst": "origami"}, {"direction": _attr("invX", {"left": "1", "right": None})}),
    "dissolve": ("p", "dissolve", {}, {}),
    "checkerboard": ("p", "checker", {"dir": "horz"}, {"direction": _attr("dir", {"across": None, "down": "vert"})}),
    "blinds": ("p", "blinds", {"dir": "vert"}, {"orientation": _attr("dir", _ACROSS)}),
    "clock": ("p", "wheel", {"spokes": "1"}, {}),
    "ripple": ("p14", "ripple", {}, {}),
    "honeycomb": ("p14", "honeycomb", {}, {}),
    "glitter": ("p14", "glitter", {}, {"direction": _attr("dir", {"left": "r", "right": None, "up": "d",
                                                                 "down": "u"})}),
    "vortex": ("p14", "vortex", {"dir": "r"}, {"direction": _attr("dir", _CARDINAL)}),
    "shred": ("p14", "shred", {"dir": "out"}, {"direction": _attr("dir", {"in": None, "out": "out"})}),
    "switch": ("p14", "switch", {"dir": "r"}, {"direction": _attr("dir", _LEFT_RIGHT)}),
    "flip": ("p14", "flip", {"dir": "r"}, {"direction": _attr("dir", _LEFT_RIGHT)}),
    "gallery": ("p14", "gallery", {"dir": "r"}, {"direction": _attr("dir", _LEFT_RIGHT)}),
    "cube": ("p14", "prism", {"dir": "r"}, {"direction": _attr("dir", _CARDINAL)}),
    "doors": ("p14", "doors", {"dir": "vert"}, {"orientation": _attr("dir", _ACROSS)}),
    "box": ("p", "zoom", {}, {"direction": _attr("dir", {"in": "in", "out": None})}),
    "comb": ("p", "comb", {"dir": "horz"}, {"orientation": _attr("dir", _ACROSS)}),
    "zoom": ("p14", "warp", {"dir": "in"}, {"direction": _attr("dir", {"in": "in", "out": None})}),
    "random": ("p", "random", {}, {}),
    "pan": ("p14", "pan", {"dir": "r"}, {"direction": _attr("dir", _CARDINAL)}),
    "ferris_wheel": ("p14", "ferris", {"dir": "r"}, {"direction": _attr("dir", _LEFT_RIGHT)}),
    "conveyor": ("p14", "conveyor", {"dir": "r"}, {"direction": _attr("dir", _LEFT_RIGHT)}),
    "rotate": ("p14", "prism", {"dir": "r", "isContent": "1"}, {"direction": _attr("dir", _CARDINAL)}),
    "window": ("p14", "window", {}, {"orientation": _attr("dir", _ACROSS)}),
    "orbit": ("p14", "prism", {"dir": "r", "isContent": "1", "isInverted": "1"},
              {"direction": _attr("dir", _CARDINAL)}),
    "fly_through": ("p14", "flythrough", {}, {"direction": _attr("dir", {"in": None, "out": "out"})}),
    "morph": ("p159", "morph", {"option": "byObject"},
              {"morph_by": _attr("option", {"object": "byObject", "word": "byWord", "character": "byChar"})}),
}
TRANSITIONS = tuple(REGISTRY)
ALIASES = {"pull": "uncover", "wheel": "clock"}
# The transitions write_pptx took before the registry, written as they were
# (no attributes) when no option is given: the schema's defaults.
_PLAIN_BEFORE = frozenset({"fade", "push", "wipe", "split", "cover", "dissolve"})


def _pick(value: Any) -> str:
    """The registry name ``value`` names, or BAD_VALUE with the closest."""
    folded = str(value).strip().casefold().replace(" ", "_").replace("-", "_")
    folded = ALIASES.get(folded, folded)
    if folded in REGISTRY:
        return folded
    close = difflib.get_close_matches(folded, TRANSITIONS, n=3, cutoff=0.6)
    hint = (f"Did you mean '{close[0]}'? " if close else "") + \
        '{"op": "help", "name": "set_transition"} lists every transition.'
    raise OpError(Code.BAD_VALUE, f"unknown transition '{value}'", hint=hint)


def _words(values: list[str]) -> str:
    return values[0] if len(values) == 1 else ", ".join(values[:-1]) + " or " + values[-1]


def effect_attributes(name: str, chosen: dict[str, Any]) -> dict[str, str]:
    """The effect element's attributes for the options ``chosen`` (option ->
    value), or BAD_VALUE naming what the effect takes."""
    _, _, base, options = REGISTRY[name]
    attrs: dict[str, str | None] = {} if name in _PLAIN_BEFORE and not chosen else dict(base)
    for option, value in chosen.items():
        if option not in options:
            takes = _words(list(options)) if options else "no options"
            raise OpError(Code.BAD_VALUE, f"transition '{name}' takes no {option}",
                          hint=f"It takes {takes}." if options else "Leave the option out.")
        word = str(value).strip().casefold().replace(" ", "_").replace("-", "_")
        if word not in options[option]:
            raise OpError(Code.BAD_VALUE, f"transition '{name}' takes {option} {_words(list(options[option]))}",
                          hint=f"Use one of: {', '.join(options[option])}.")
        attrs.update(options[option][word])
    return {k: v for k, v in attrs.items() if v is not None}


def transition_xml(name: str, attrs: dict[str, str], *, speed: str, duration_ms: int,
                   advance_after: int | None) -> str:
    """The transition as PowerPoint writes it: plain, or wrapped with a fade
    for readers that do not know the effect."""
    prefix, element, _, _ = REGISTRY[name]
    timing = f' spd="{speed}"'
    if advance_after:
        timing += f' advClick="0" advTm="{int(advance_after)}"'
    effect_attrs = "".join(f' {k}="{v}"' for k, v in attrs.items())
    if prefix == "p":
        return (f'<p:transition xmlns:p="{P_NS}"{timing}><p:{element}{effect_attrs}/></p:transition>')
    p14 = NAMESPACES["p14"]
    declared = f' xmlns:{prefix}="{NAMESPACES[prefix]}"'
    return (f'<mc:AlternateContent xmlns:mc="{MC_NS}" xmlns:p="{P_NS}">'
            f'<mc:Choice{declared} Requires="{prefix}">'
            f'<p:transition xmlns:p14="{p14}"{timing} p14:dur="{int(duration_ms)}">'
            f'<{prefix}:{element}{effect_attrs}/></p:transition></mc:Choice>'
            f'<mc:Fallback><p:transition{timing}><p:fade/></p:transition></mc:Fallback>'
            f'</mc:AlternateContent>')


def _local(element: Any) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _wraps(element: Any, local: str) -> bool:
    """Whether ``element`` is an mc:AlternateContent whose branches hold a
    ``p:<local>``."""
    return _local(element) == "AlternateContent" and element.find(f".//{{{P_NS}}}{local}") is not None


def is_transition(element: Any) -> bool:
    return _local(element) == "transition" or _wraps(element, "transition")


def place_transition(sld: Any, transition: Any) -> None:
    """Replace the slide's transition (plain or wrapped) with ``transition``,
    placed before the slide's timing (plain or wrapped) and its extLst.
    ``ooxml.order`` cannot place it: it never looks inside
    mc:AlternateContent."""
    for child in list(sld):
        if is_transition(child):
            sld.remove(child)
    for child in sld:
        if _local(child) in ("timing", "extLst") or _wraps(child, "timing"):
            child.addprevious(transition)
            return
    sld.append(transition)


def _op_set_transition(job: PptxJob, ctx: OpCtx, op: dict) -> None:
    from lxml import etree

    slide, _ = slide_of(job, op)
    transition = op.get("transition", "fade")
    if isinstance(transition, dict):
        duration = transition.get("duration", op.get("duration", 1000))
        advance_after = transition.get("advance_after", op.get("advance_after"))
        name = transition.get("type", "fade")
    else:
        duration, advance_after, name = op.get("duration", 1000), op.get("advance_after"), transition
    name = _pick(name)
    given = (("direction", op.get("direction")), ("orientation", op.get("orientation")),
             ("morph_by", op.get("morph_by")))
    chosen = {key: value for key, value in given if value is not None}
    attrs = effect_attributes(name, chosen)
    # duration is the effect's speed; only advance_after advances the slide
    # by itself (advTm).
    speed = "fast" if duration <= 500 else "med" if duration <= 1000 else "slow"
    xml = transition_xml(name, attrs, speed=speed, duration_ms=duration, advance_after=advance_after)
    place_transition(slide._element, etree.fromstring(xml))
    if REGISTRY[name][0] == "p15":
        ctx.note(f"LibreOffice and Collabora do not play the {name} transition, and a deck saved there loses it.")
    if name == "morph":
        ctx.note("Morph pairs shapes by name across this slide and the one before: give a pair the same name, "
                 "or '!!name' to force it (set_shape_format name). LibreOffice shows a fade.")


def read_transition(sld: Any) -> str | None:
    """The registry name of the slide's transition, as the read shows it."""
    for child in sld:
        if not is_transition(child):
            continue
        holder = child
        if _local(child) == "AlternateContent":
            holder = next((t for t in child.iter(f"{{{P_NS}}}transition")), child)
        for effect in holder:
            local = _local(effect)
            namespace = effect.tag[1:].split("}", 1)[0] if isinstance(effect.tag, str) else ""
            prefix = next((k for k, v in NAMESPACES.items() if v == namespace), "p")
            for key, (want_prefix, element, base, _) in REGISTRY.items():
                if (want_prefix, element) == (prefix, local) and (
                        element not in ("prstTrans", "prism") or
                        all(effect.get(a) == base.get(a) for a in ("prst", "isContent", "isInverted"))):
                    return key
            return local or None
        return None
    return None


OPS: tuple[OpSpec, ...] = (
    OpSpec("set_transition", _op_set_transition, (
        slide_key(),
        KeySpec("transition", ("str", "dict"), default="fade", aliases=("effect", "name"),
                help=" | ".join(TRANSITIONS), fields=(
                    KeySpec("type", ("str",), aliases=("name", "effect")), KeySpec("duration", ("int",)),
                    KeySpec("advance_after", ("int",)))),
        KeySpec("duration", ("int",), default=1000, help="ms, the effect's speed"),
        KeySpec("advance_after", ("int",), help="ms: the slide advances by itself"),
        KeySpec("direction", ("str",), help="left, right, up, down, in, out, across... as the transition takes; "
                                           "the way the motion goes (push up is PowerPoint's From Bottom)"),
        KeySpec("orientation", ("str",), help="horizontal | vertical"),
        KeySpec("morph_by", ("str",), help="object | word | character (morph)"),
    ), G_VISUAL, note="morph pairs shapes by name with the slide before ('!!name' forces a pair); LibreOffice "
                      "shows Morph as a fade, and neither plays nor keeps the Exciting ones (drape, curtains...)",
        examples=({"op": "set_transition", "slide_index": 0, "transition": "push", "duration": 400},
                  {"op": "set_transition", "slide_index": 0, "transition": {"type": "fade", "advance_after": 3000}},
                  {"op": "set_transition", "slide_index": 0, "transition": "cube", "direction": "left"},
                  {"op": "set_transition", "slide_index": 1, "transition": "morph", "morph_by": "word"})),
)
