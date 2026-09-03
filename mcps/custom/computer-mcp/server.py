"""computer-control MCP — drive a satellite's screen, mouse, and keyboard.

A device-local MCP: `placement: satellite_only`,
`requires_display: true`, `device_capability: "computer"`. The framework gates
attachment (only on a display-having satellite whose owner granted "computer")
and the manifest sets `default_tier: "sensitive"` — every `mcp__computer__*`
call prompts for approval; this server only implements the tools.

Tool surface mirrors Anthropic's `computer_20250124` action set as ONE tool
(`computer`) with an `action` discriminator, plus a `check_permissions`
diagnostic. Every action returns a FRESH screenshot as an inline `ImageContent`
block (the proven ha-mcp / file-tools path — the CLI talks to the model
directly, so the image reaches the model unaltered) plus a short `TextContent`.
Screenshots are reactive — the result of the action — not a stream.

The screenshot the model sees is in **model image space**: clicks must fall
within the reported `WxH` (0,0 = top-left). The server converts those back to
physical pixels and then to OS input coordinates per monitor (HiDPI + negative
multi-monitor origins handled in `geometry.py`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

import executor
import geometry
import screen
from environment import (
    DeviceUnavailable,
    check_capture_supported,
    check_input_supported,
    check_macos_permissions,
    check_session_active,
    display_backend,
    os_kind,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("computer-mcp")

server = Server("computer")

# Actions that move/click/drag/scroll/type — i.e. need an input backend (and so
# are refused on Wayland / headless) as opposed to read-only `screenshot` /
# `cursor_position` / `wait`.
_INPUT_ACTIONS = {
    "mouse_move", "left_click", "right_click", "middle_click", "double_click",
    "triple_click", "left_click_drag", "left_mouse_down", "left_mouse_up",
    "scroll", "key", "hold_key", "type",
}
_COORD_ACTIONS = {
    "mouse_move", "left_click", "right_click", "middle_click", "double_click",
    "triple_click", "left_click_drag", "left_mouse_down", "left_mouse_up", "scroll",
}
_ALL_ACTIONS = _INPUT_ACTIONS | {"screenshot", "cursor_position", "wait"}

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
    return max(0.0, min(ms, 10_000) / 1000.0)


def _err(msg: str) -> list:
    return [TextContent(type="text", text=f"⚠️ {msg}")]


# Subfolder for DELIBERATELY-saved screenshots. Intentionally NOT ".screenshots"
# — that dotted tree is the framework's MCP-output relocation destination and
# carries a credential-classifier *passthrough* in the proxy hook (a screenshot
# of a terminal / chrome://settings/passwords would become re-servable). A plain
# "screenshots/" folder is a normal synced workspace file, previewable in the
# dashboard under normal access rules.
_SAVE_SUBDIR = "screenshots"


def _alloc_save_path(display) -> tuple[str | None, str | None]:
    """Compute ``(abs_path, workspace_relative_path)`` for a deliberate save,
    creating the folder. Returns ``(None, None)`` when the workspace dir is
    unavailable (save is then skipped — the inline image still returns)."""
    ws = os.environ.get("OTO_WORKSPACE_DIR")
    if not ws:
        return None, None
    subdir = os.path.join(ws, _SAVE_SUBDIR)
    try:
        os.makedirs(subdir, exist_ok=True)
    except Exception:
        return None, None
    idx = display if isinstance(display, int) else 1
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int((time.time() % 1) * 1000):03d}"
    fname = f"screen-{idx}-{stamp}.png"
    return os.path.join(subdir, fname), os.path.join(_SAVE_SUBDIR, fname)


def _screenshot_blocks(display, prefix: str, *, save: bool = False) -> list:
    """Capture `display` and return `[ImageContent, TextContent]`. The text
    states the model-space dimensions so the model knows its click bounds.

    ``save=True`` (only the deliberate ``screenshot`` action sets it) also
    persists the full-resolution PNG into the workspace ``screenshots/`` folder
    for dashboard viewing; the reactive result-frames after input actions stay
    inline-only/ephemeral."""
    save_abs, save_rel = _alloc_save_path(display) if save else (None, None)
    cap = screen.capture(display, _coord_mode(), save_path=save_abs)
    note = (
        f"{prefix}\n"
        f"Screenshot of display {cap.monitor.index} of {cap.monitor_count} "
        f"({cap.model_width}x{cap.model_height} px). Click coordinates are in "
        f"this image's pixel space: (0,0) top-left, max ({cap.model_width - 1},"
        f"{cap.model_height - 1})."
    )
    if save:
        if cap.saved_path and save_rel:
            note += f"\nSaved full-resolution screenshot to '{save_rel}' (viewable in the dashboard file preview)."
        else:
            note += "\n(Could not save to the workspace — returned inline only.)"
    return [
        ImageContent(type="image", data=cap.png_b64, mimeType="image/png"),
        TextContent(type="text", text=note),
    ]


def _require_coordinate(args: dict) -> tuple[float, float]:
    coord = args.get("coordinate")
    if not isinstance(coord, (list, tuple)) or len(coord) != 2:
        raise ValueError("This action requires 'coordinate': [x, y] in screenshot pixels.")
    return float(coord[0]), float(coord[1])


def _optional_coordinate(args: dict) -> tuple[float, float] | None:
    coord = args.get("coordinate")
    if coord is None:
        return None
    if not isinstance(coord, (list, tuple)) or len(coord) != 2:
        raise ValueError("'coordinate' must be [x, y] when provided.")
    return float(coord[0]), float(coord[1])


def _monitor_under(gx: int, gy: int) -> geometry.Monitor:
    """The monitor whose logical bounds contain (gx, gy); primary if none do."""
    monitors = screen.list_monitors()
    for m in monitors:
        if m.left <= gx < m.left + m.width and m.top <= gy < m.top + m.height:
            return m
    return monitors[0]


def _run_action(args: dict) -> list:
    """Synchronous dispatch. Raises DeviceUnavailable / ValueError on bad input
    or unavailable device; the caller turns those into actionable text."""
    action = args.get("action")
    if action not in _ALL_ACTIONS:
        raise ValueError(
            f"Unknown action {action!r}. Valid actions: {', '.join(sorted(_ALL_ACTIONS))}."
        )
    display = args.get("display")
    if display is not None:
        try:
            display = int(display)
        except (TypeError, ValueError):
            raise ValueError("'display' must be an integer monitor index (1 = primary).")

    # --- read-only actions -------------------------------------------------
    if action == "screenshot":
        check_capture_supported()
        check_session_active()
        return _screenshot_blocks(display, "Captured the screen.", save=bool(args.get("save", False)))

    if action == "wait":
        check_capture_supported()
        try:
            duration = float(args.get("duration", 1.0))
        except (TypeError, ValueError):
            raise ValueError("'duration' must be a number of seconds.")
        time.sleep(max(0.0, min(duration, 60.0)))
        return _screenshot_blocks(display, f"Waited {duration:g}s.")

    if action == "cursor_position":
        check_capture_supported()
        gx, gy = executor.cursor_position()
        mon = _monitor_under(gx, gy)
        scale = geometry.compute_scale(mon.phys_width, mon.phys_height, _coord_mode())
        mx, my = geometry.input_to_model_coords(gx, gy, monitor=mon, scale=scale)
        return _screenshot_blocks(
            display if display is not None else mon.index,
            f"Cursor is at ({mx:.0f}, {my:.0f}) in display {mon.index}'s screenshot space.",
        )

    # --- input actions: gate first ----------------------------------------
    check_input_supported()       # Wayland / no-display refusal (actionable)
    check_session_active()

    monitor = scale = None
    if action in _COORD_ACTIONS:
        monitor, scale = screen.resolve_target(display, _coord_mode())

    summary = ""
    if action == "mouse_move":
        mx, my = _require_coordinate(args)
        executor.move_to(mx, my, monitor, scale)
        summary = f"Moved cursor to ({mx:.0f}, {my:.0f})."
    elif action in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        button = _BUTTON_FOR[action]
        clicks = _CLICKS_FOR.get(action, 1)
        modifiers = executor.parse_key_combo(args["text"]) if args.get("text") else None
        coord = _optional_coordinate(args)
        if coord is None:
            executor.click_current(button=button, clicks=clicks, modifiers=modifiers)
            summary = f"{action.replace('_', ' ')} at current cursor position."
        else:
            executor.click(coord[0], coord[1], monitor, scale, button=button, clicks=clicks, modifiers=modifiers)
            summary = f"{action.replace('_', ' ')} at ({coord[0]:.0f}, {coord[1]:.0f})."
        if modifiers:
            summary += f" (held: {'+'.join(modifiers)})"
    elif action == "left_click_drag":
        ex, ey = _require_coordinate(args)
        start = None
        sc = args.get("start_coordinate")
        if isinstance(sc, (list, tuple)) and len(sc) == 2:
            start = (float(sc[0]), float(sc[1]))
        executor.drag(ex, ey, monitor, scale, start=start)
        summary = f"Dragged to ({ex:.0f}, {ey:.0f})" + (f" from ({start[0]:.0f}, {start[1]:.0f})." if start else " from the current position.")
    elif action == "left_mouse_down":
        coord = _optional_coordinate(args)
        executor.mouse_down(coord[0] if coord else None, coord[1] if coord else None, monitor, scale)
        summary = "Pressed the left mouse button down."
    elif action == "left_mouse_up":
        coord = _optional_coordinate(args)
        executor.mouse_up(coord[0] if coord else None, coord[1] if coord else None, monitor, scale)
        summary = "Released the left mouse button."
    elif action == "scroll":
        direction = args.get("scroll_direction")
        try:
            amount = int(args.get("scroll_amount", 3))
        except (TypeError, ValueError):
            raise ValueError("'scroll_amount' must be an integer number of clicks.")
        coord = _optional_coordinate(args)
        executor.scroll(
            direction, amount,
            model_x=coord[0] if coord else None, model_y=coord[1] if coord else None,
            monitor=monitor, scale=scale,
        )
        summary = f"Scrolled {direction} by {amount}."
    elif action == "key":
        text = args.get("text")
        if not text:
            raise ValueError("'key' requires 'text' (e.g. 'ctrl+s', 'Return', 'alt+Tab').")
        executor.press_keys(text)
        summary = f"Pressed key: {text}."
    elif action == "hold_key":
        text = args.get("text")
        if not text:
            raise ValueError("'hold_key' requires 'text' (the key combo to hold).")
        try:
            duration = float(args.get("duration", 1.0))
        except (TypeError, ValueError):
            raise ValueError("'duration' must be a number of seconds.")
        executor.hold_keys(text, duration)
        summary = f"Held {text} for {duration:g}s."
    elif action == "type":
        text = args.get("text")
        if text is None:
            raise ValueError("'type' requires 'text' (the literal characters to type).")
        summary = executor.type_text(str(text)).capitalize() + "."

    # Windows: hint when the foreground window is elevated (UIPI silently
    # eats input from our non-elevated process).
    if action in _INPUT_ACTIONS and os_kind() == "windows":
        if executor.foreground_window_elevated() is True:
            summary += (
                " NOTE: the active window appears to belong to an elevated "
                "(administrator) process — a non-elevated satellite cannot "
                "send input to it, so this action may have had no effect."
            )

    time.sleep(_settle_seconds())
    return _screenshot_blocks(display, summary)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="computer",
            description=(
                "Control this machine's screen, mouse, and keyboard. Work in a "
                "screenshot-first loop: take a screenshot, decide, act, then read "
                "the screenshot returned by your action to see the result. "
                "Coordinates are in the pixel space of the LAST screenshot you "
                "received (0,0 = top-left); never guess beyond its reported size. "
                "Key names use xdotool syntax: 'Return', 'Escape', 'ctrl+s', "
                "'alt+Tab', 'Page_Down'. Use 'type' for literal text and 'key' for "
                "shortcuts. On a multi-monitor machine pass 'display' (1 = primary)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_ALL_ACTIONS),
                        "description": (
                            "screenshot: capture the screen. cursor_position: report "
                            "the cursor location. mouse_move: move to coordinate. "
                            "left_click/right_click/middle_click/double_click/"
                            "triple_click: click (optional coordinate; optional 'text' "
                            "= modifier keys to hold, e.g. 'ctrl'). left_click_drag: "
                            "drag to coordinate (optional start_coordinate). "
                            "left_mouse_down/left_mouse_up: fine-grained drag control. "
                            "scroll: scroll_direction + scroll_amount at coordinate. "
                            "key: press a key/combo. hold_key: hold a combo for "
                            "duration. type: type literal text. wait: pause duration "
                            "seconds then screenshot."
                        ),
                    },
                    "coordinate": {
                        "type": "array", "items": {"type": "number"},
                        "minItems": 2, "maxItems": 2,
                        "description": "[x, y] in the last screenshot's pixel space.",
                    },
                    "start_coordinate": {
                        "type": "array", "items": {"type": "number"},
                        "minItems": 2, "maxItems": 2,
                        "description": "Optional drag start [x, y]; defaults to the current cursor position.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Literal text for 'type'; xdotool key combo for 'key'/'hold_key'; modifier keys to hold for click actions.",
                    },
                    "scroll_direction": {
                        "type": "string", "enum": ["up", "down", "left", "right"],
                        "description": "Direction for the 'scroll' action.",
                    },
                    "scroll_amount": {
                        "type": "integer",
                        "description": "Number of scroll clicks for the 'scroll' action.",
                    },
                    "duration": {
                        "type": "number",
                        "description": "Seconds for 'wait' and 'hold_key'.",
                    },
                    "display": {
                        "type": "integer",
                        "description": "1-based monitor index (1 = primary). Defaults to primary.",
                    },
                    "save": {
                        "type": "boolean",
                        "description": "Only for action='screenshot'. When true, also saves the full-resolution screenshot into the workspace 'screenshots/' folder so the user can view it in the dashboard. Use ONLY when the user explicitly wants to keep/see a screenshot — the normal reactive screenshots you get after each action are not saved.",
                    },
                },
                "required": ["action"],
            },
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
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
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
                if perms.get("notes"):
                    lines.append(perms["notes"])
            return [TextContent(type="text", text="\n".join(lines))]
        except Exception as e:  # diagnostics must never crash
            logger.exception("check_permissions failed")
            return _err(f"Could not read permission state: {type(e).__name__}: {e}")

    if name != "computer":
        return _err(f"Unknown tool: {name}")

    try:
        return await asyncio.to_thread(_run_action, arguments)
    except DeviceUnavailable as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))
    except Exception as e:  # never crash the MCP on an action error
        logger.exception("computer action failed")
        return _err(f"Unexpected error performing the action: {type(e).__name__}: {e}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
