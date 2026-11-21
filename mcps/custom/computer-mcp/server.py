"""computer-control MCP — drive a satellite's screen, mouse, and keyboard.

A device-local MCP: `placement: satellite_only`, `requires_display: true`,
`device_capability: "computer"`. The framework gates attachment (only on a
display-having satellite whose owner granted "computer"). On such a computer
its calls run with no approval card in every mode (the grant is the consent);
only a turn sent with an API key shows the card, which lists every step. This
server only implements the tools.

ONE tool (`computer`) takes a list of 1 to 12 `steps` (Anthropic's
`computer_20250124` action names, plus `zoom`) and runs them in order in one
runner (`steps.py`): it waits until the screen is still after each step that
acts, checks the screen, the mouse and the window in front before each one,
stops before a step when something changed, and returns ONE fresh screenshot
(an inline `ImageContent`, the proven ha-mcp / file-tools path) plus one line
per step. A call with a top-level `action` runs nothing: the door
(`_list_refusal`) answers one sentence that asks for the action as a step.
`check_permissions` is a diagnostic.

The screenshot the model sees is in **model image space**: clicks must fall
within the reported `WxH` (0,0 = top-left). The server converts those back to
physical pixels and then to OS input coordinates per monitor (HiDPI + negative
multi-monitor origins handled in `geometry.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import threading
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations

import executor
import geometry
import steps
from _tool_args import ToolArgs
from environment import (
    DeviceUnavailable,
    check_macos_permissions,
    display_backend,
    os_kind,
    responsible_app,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("computer-mcp")

server = Server("computer")
TOOL_ARGS = ToolArgs()

_ACTIONS = (
    "screenshot", "zoom", "cursor_position", "mouse_move", "left_click", "right_click", "middle_click",
    "double_click", "triple_click", "left_click_drag", "scroll", "key", "hold_key", "type", "wait",
)
# The door's two keys: a step's own, which a call never carries at its top, and the list's.
_STEP_KEY = frozenset({"action"})
_LIST_KEY = frozenset({"steps"})
_STEPS_ONLY = (f"Nothing ran. This tool takes `steps`, a list of 1 to {steps.MAX_STEPS} actions. "
               "Send this action as one step.")

_BUTTON_FOR = {
    "left_click": "left", "right_click": "right", "middle_click": "middle",
    "double_click": "left", "triple_click": "left",
}
_CLICKS_FOR = {"double_click": 2, "triple_click": 3}


def _coord_mode() -> str:
    return geometry.normalize_coord_mode(os.environ.get("OTO_COMPUTER_COORD_MODE"))


def _settle_seconds() -> float:
    try:
        ms = int(os.environ.get("OTO_COMPUTER_SETTLE_MS", "700"))
    except (TypeError, ValueError):
        ms = 700
    return max(0.0, min(ms, 2_000) / 1000.0)   # the still wait's ceiling: a call stays under Codex's 60 s


def _err(msg: str) -> list:
    return [TextContent(type="text", text=f"⚠️ {msg}")]


# Subfolder for DELIBERATELY-saved screenshots. Intentionally NOT ".screenshots"
# — that dotted tree is the framework's MCP-output relocation destination and
# carries a credential-classifier *passthrough* in the proxy hook (a screenshot
# of a terminal / chrome://settings/passwords would become re-servable). A plain
# "screenshots/" folder is a normal synced workspace file, previewable in the
# dashboard under normal access rules.
_SAVE_SUBDIR = "screenshots"


def _alloc_save_path(display, prefix: str = "screen") -> tuple[str | None, str | None]:
    """Compute ``(abs_path, workspace_relative_path)`` for a deliberate save
    (``prefix`` is ``screen`` or ``zoom``), creating the folder. Returns
    ``(None, None)`` when the workspace dir is unavailable (save is then
    skipped — the inline image still returns)."""
    ws = os.environ.get("OTO_WORKSPACE_DIR")
    if not ws:
        return None, None
    subdir = os.path.join(ws, _SAVE_SUBDIR)
    try:
        os.makedirs(subdir, exist_ok=True)
    except OSError:
        return None, None
    idx = display if isinstance(display, int) else 1
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int((time.time() % 1) * 1000):03d}"
    fname = f"{prefix}-{idx}-{stamp}.png"
    return os.path.join(subdir, fname), os.path.join(_SAVE_SUBDIR, fname)


def _optional_coordinate(args: dict) -> tuple[float, float] | None:
    coord = args.get("coordinate")
    if coord is None:
        return None
    return float(coord[0]), float(coord[1])


def _do_step(args: dict, ctx: steps.Context) -> tuple[tuple[int, int] | None, executor.Typed | None]:
    """Run ONE step that acts or pauses (the runner has checked it). Returns
    the cursor's new point in the OS input space (``None`` when it did not
    move) and, for ``type``, what was typed. Raises DeviceUnavailable /
    ValueError on bad input or an unavailable device."""
    action = args.get("action")
    monitor, scale = ctx.monitor, ctx.scale
    if action == "mouse_move":
        coord = _optional_coordinate(args)
        return executor.move_to(coord[0], coord[1], monitor, scale), None
    if action in _BUTTON_FOR:
        button = _BUTTON_FOR[action]
        clicks = _CLICKS_FOR.get(action, 1)
        modifiers = executor.parse_key_combo(args["text"]) if args.get("text") else None
        coord = _optional_coordinate(args)
        if coord is None:
            executor.click_current(button=button, clicks=clicks, modifiers=modifiers)
            return None, None
        return executor.click(coord[0], coord[1], monitor, scale, button=button, clicks=clicks,
                              modifiers=modifiers), None
    if action == "left_click_drag":
        ex, ey = _optional_coordinate(args)
        sc = args.get("start_coordinate")
        start = (float(sc[0]), float(sc[1])) if isinstance(sc, (list, tuple)) and len(sc) == 2 else None
        return executor.drag(ex, ey, monitor, scale, start=start), None
    if action == "scroll":
        coord = _optional_coordinate(args)
        point = executor.scroll(
            args.get("scroll_direction"), int(args.get("scroll_amount", 3)),
            model_x=coord[0] if coord else None, model_y=coord[1] if coord else None,
            monitor=monitor, scale=scale,
        )
        return point, None
    if action == "key":
        executor.press_keys(args["text"])
        return None, None
    if action == "hold_key":
        executor.hold_keys(args["text"], float(args.get("duration", 1.0)), wait=ctx.wait)
        return None, None
    if action == "type":
        return None, executor.type_text(str(args["text"]), between=ctx.between, copy=ctx.copy)
    raise ValueError(f"Unknown action {action!r}.")


def _list_refusal(arguments) -> str | None:
    """The door: why a call runs nothing, or None. A top-level ``action``,
    with or without ``steps``, is refused with one sentence that asks for
    the action as a step. A malformed list is refused here with counts only,
    before the argument check, which would echo the whole list (typed text
    included)."""
    if not isinstance(arguments, dict):
        return None
    keys = set(arguments)
    if _STEP_KEY <= keys:
        return _STEPS_ONLY
    raw = arguments.get("steps")
    if _LIST_KEY <= keys:
        if not isinstance(raw, list):
            return "'steps' must be a list of steps."
        if not 1 <= len(raw) <= steps.MAX_STEPS:
            return (f"'steps' holds 1 to {steps.MAX_STEPS} steps (this call has {len(raw)}). "
                    "Send the rest in a new call, once you have seen the screen.")
        bad = [i for i, s in enumerate(raw, start=1) if not isinstance(s, dict)]
        if bad:
            return f"Step {bad[0]} must be an object with an 'action'."
        textless = [i for i, s in enumerate(raw, start=1) if s.get("text") is not None and not isinstance(s["text"], str)]
        if textless:
            return f"Step {textless[0]}: 'text' must be a string."
    return None


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "screenshot: the screen (only after the other steps). zoom: a region of the screen at full "
                "sharpness. cursor_position: where the cursor is. mouse_move, left_click, right_click, "
                "middle_click, double_click, triple_click (coordinate optional; text = keys to hold, e.g. "
                "'ctrl'). left_click_drag: to coordinate (start_coordinate optional). scroll: "
                "scroll_direction and scroll_amount, at coordinate. key: a key or combo. hold_key: a combo "
                "for duration seconds. type: literal text. wait: pause duration seconds."
            ),
        },
        "coordinate": {
            "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
            "description": "[x, y] in your last screenshot's pixel space.",
        },
        "start_coordinate": {
            "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
            "description": "Drag start [x, y]; defaults to the current cursor position.",
        },
        "text": {
            "type": "string",
            "description": ("Literal text for 'type' (at most 200 characters per step); an xdotool key combo "
                            "for 'key'/'hold_key'; keys to hold for a click."),
        },
        "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "scroll_amount": {"type": "integer", "description": "Scroll clicks, 1 to 50."},
        "duration": {"type": "number", "description": "Seconds for 'wait' and 'hold_key', at most 5."},
        "region": {
            "type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
            "description": "For 'zoom': [x0, y0, x1, y1] in your last screenshot's pixel space.",
        },
        "save": {
            "type": "boolean",
            "description": ("For 'screenshot' or 'zoom': also save the picture at full resolution in the "
                            "workspace 'screenshots/' folder, to show a person or keep. Only when that is wanted."),
        },
        "window": {
            "type": "string",
            "description": ("Part of the title or program name of the window this step expects in front. "
                            "Name it when an earlier step brings another window forward."),
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_ARGS.listed([
        Tool(
            name="computer",
            description=(
                "Control this machine's screen, mouse, and keyboard. Send 'steps', a list of 1 to 12 "
                "actions, also for one action. They run in order and ONE screenshot comes back at the end, "
                "with one line per step. The tool waits for the screen to settle after each step, and stops "
                "before a step when the screen under the next click changed, someone moved the mouse or "
                "another window came to the front: then read the screenshot and plan again. Coordinates are "
                "in the pixel space of your LAST screenshot (0,0 = top-left). Enter or Return may only be "
                "the last step that acts. Key names use xdotool syntax: 'Return', 'Escape', 'ctrl+s', "
                "'alt+Tab', 'Page_Down'. On a multi-monitor machine pass 'display' (1 = primary)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array", "minItems": 1, "maxItems": steps.MAX_STEPS, "items": _STEP_SCHEMA,
                        "description": "The actions, in order. Reads (screenshot, zoom, cursor_position) go last.",
                    },
                    "display": {
                        "type": "integer",
                        "description": "1-based monitor index (1 = primary). Defaults to primary.",
                    },
                },
                "required": ["steps"],
            },
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        ),
        Tool(
            name="check_permissions",
            description=(
                "Diagnose whether this machine can be controlled: reports the "
                "display backend (x11/wayland/quartz/windows/none) and, on macOS, "
                "whether Accessibility (input) and Screen Recording (capture) are "
                "granted. Call this first if screenshots are black or clicks do "
                "nothing."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
    ])


def _hooks() -> steps.Hooks:
    return steps.Hooks(dispatch=_do_step, coord_mode=_coord_mode(), settle_s=_settle_seconds(),
                       alloc_save_path=_alloc_save_path)


def _answer(result: steps.Result) -> list:
    if result.refusal is not None:
        return _err(result.refusal)
    blocks: list = [ImageContent(type="image", data=data, mimeType="image/png") for data in result.pictures]
    text = "\n".join(result.lines)
    if result.note:
        text = f"{text}\n{result.note}" if text else result.note
    blocks.append(TextContent(type="text", text=text))
    return blocks


def _display_arg(arguments: dict) -> int | None:
    display = arguments.get("display")
    if display is None:
        return None
    try:
        return int(display)
    except (TypeError, ValueError) as exc:
        raise ValueError("'display' must be an integer monitor index (1 = primary).") from exc


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> list | CallToolResult:
    hooks = _hooks()
    arrived = hooks.clock()
    if name == "computer" and (refusal := _list_refusal(arguments)) is not None:
        return _err(refusal)
    arguments, refusal = TOOL_ARGS.check(name, arguments)
    if refusal is not None:
        return refusal
    arguments = arguments or {}
    if name == "check_permissions":
        try:
            backend = display_backend()
            lines = [f"Platform: {os_kind()}", f"Display backend: {backend}"]
            if backend == "wayland":
                lines.append("Wayland blocks synthetic input — clicks/typing will be refused. Use an X11 session.")
            elif backend == "none":
                lines.append("No graphical display detected (headless / background service). Capture and input are unavailable.")
            if os_kind() == "darwin":
                perms = check_macos_permissions()
                lines.append(f"Accessibility (input): {perms['accessibility']}")
                lines.append(f"Screen Recording (capture): {perms['screen_recording']}")
                app = responsible_app()
                if app is not None:
                    lines.append(f"macOS asks for these on behalf of: {app[0]} ({app[1]})")
                if perms.get("notes"):
                    lines.append(perms["notes"])
            return [TextContent(type="text", text="\n".join(lines))]
        except Exception as e:  # diagnostics must never crash
            logger.exception("check_permissions failed")
            return _err(f"Could not read permission state: {type(e).__name__}: {e}")

    if name != "computer":
        return _err(f"Unknown tool: {name}")

    cancel = threading.Event()
    try:
        display = _display_arg(arguments)
        result = await asyncio.to_thread(steps.run, arguments["steps"], display, arrived=arrived,
                                         cancel=cancel, hooks=hooks)
        return _answer(result)
    except asyncio.CancelledError:
        cancel.set()  # the engine gave up: the runner sends nothing more
        raise
    except DeviceUnavailable as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001  # never crash the MCP on an action error, never log its message
        logger.warning("computer call failed: %s", type(e).__name__)
        return _err(f"Unexpected error performing the action: {type(e).__name__}.")


def _on_signal(signum, _frame) -> None:
    """SIGTERM or SIGHUP (the satellite ends a session's tools): stop the
    running call before its next event, give it a moment to release what it
    holds, then exit."""
    steps.cancel_all()
    deadline = time.monotonic() + 1.5
    while steps.busy() and time.monotonic() < deadline:
        time.sleep(0.05)
    # A call still busy after 1.5 s is inside the input library or its still
    # wait: releasing from this thread could share pyautogui's display
    # connection with it, so the process exits as it is.
    os._exit(128 + signum)


async def main():
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _on_signal)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
