"""Input execution for the computer-control MCP — mouse, keyboard, scroll.

Built on PyAutoGUI (cross-platform). ``pyautogui`` is imported **lazily** the
first time an input action runs: importing it eagerly connects to the display
server and crashes on a headless box, which would break the test suite and any
mis-attached session. ``pyautogui.FAILSAFE`` is turned OFF (the default
corner-abort would kill a session mid-task) and replaced with our own
per-monitor bounds clamp (:func:`geometry.clamp_to_monitor`).

Coordinates arriving from the model are in the **model image space** of a
screenshot of some ``display``; the server resolves that display's
``(monitor, scale)`` (``steps._run_locked``, from the layout read once per
call) and passes it in, and
we convert to a global OS coordinate via :func:`geometry.model_to_input_coords`
before every move/click/drag.

Key names follow xdotool's X-keysym convention (``Return``, ``ctrl+s``,
``alt+Tab``, ``Page_Down`` ...), matching Anthropic's ``computer_20250124``
tool, and are mapped to PyAutoGUI's names by :data:`KEY_MAP`.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

import geometry
import screen
from environment import DeviceUnavailable, os_kind

_pyautogui = None

# What this module pressed down and has not released yet, recorded BEFORE the
# event goes out: a stop or an exception releases exactly these
# (:func:`release_held`), and nothing when nothing is held.
_held_keys: list[str] = []
_held_buttons: list[str] = []

# Key-by-key text goes in chunks of this many characters, with the runner asked
# between chunks whether to go on: a dialog that takes the keys mid-word gets
# at most this many of them (the proof on a virtual screen: ten went into a
# dialog that opened after the first one).
TYPE_CHUNK = 3


def _gui():
    """Lazily import + configure pyautogui. Raises a clean
    :class:`environment.DeviceUnavailable` if the GUI backend can't load
    (missing display, missing python-xlib, etc.) instead of a raw ImportError."""
    global _pyautogui
    if _pyautogui is not None:
        return _pyautogui
    screen.ensure_dpi_aware()  # before pyautogui's import sets an awareness of its own
    try:
        import pyautogui
    except Exception as e:  # display connection / backend import failure
        raise DeviceUnavailable(
            "The input backend (pyautogui) could not initialize. On Linux this "
            "usually means there is no reachable X11 display. "
            f"({type(e).__name__}: {e})"
        ) from e
    pyautogui.FAILSAFE = False        # we clamp ourselves; corner-abort would kill a run
    pyautogui.PAUSE = 0.0             # we insert our own settle delays
    _pyautogui = pyautogui
    return _pyautogui


# --- xdotool keysym -> pyautogui key name ----------------------------------
#
# Anthropic's computer tool emits xdotool-style names. PyAutoGUI has its own
# (mostly lowercase) vocabulary. Unmapped tokens fall through lowercased, which
# already covers letters, digits, and single punctuation chars that pyautogui
# accepts verbatim.
KEY_MAP: dict[str, str] = {
    "return": "enter",
    "kp_enter": "enter",
    "enter": "enter",
    "escape": "esc",
    "esc": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "tab": "tab",
    "space": "space",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "page_up": "pageup",
    "prior": "pageup",
    "page_down": "pagedown",
    "next": "pagedown",
    "home": "home",
    "end": "end",
    "caps_lock": "capslock",
    "num_lock": "numlock",
    "scroll_lock": "scrolllock",
    "print": "printscreen",
    "menu": "apps",
    # Modifiers (both bare and X _L/_R variants).
    "ctrl": "ctrl",
    "control": "ctrl",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
}

# Function keys F1..F24.
for _i in range(1, 25):
    KEY_MAP[f"f{_i}"] = f"f{_i}"


def _map_key(token: str) -> str:
    """Map one xdotool key token to a pyautogui key name."""
    t = token.strip().lower()
    if t in KEY_MAP:
        return KEY_MAP[t]
    # The super/meta key is OS-specific in pyautogui.
    if t in ("super", "super_l", "super_r", "meta", "meta_l", "meta_r", "win", "cmd", "command"):
        return "command" if os_kind() == "darwin" else "winleft"
    return t


def parse_key_combo(text: str) -> list[str]:
    """Parse an xdotool combo like ``"ctrl+shift+t"`` into pyautogui key names
    ``["ctrl","shift","t"]``. A lone ``"+"`` is preserved (it's the plus key).
    Raises ``ValueError`` on empty input."""
    if not text or not text.strip():
        raise ValueError("key/hold_key requires a non-empty 'text' (e.g. 'ctrl+s', 'Return').")
    raw = text.strip()
    if raw == "+":
        return ["+"]
    parts = [p for p in raw.split("+") if p != ""]
    if not parts:                 # string was all '+' separators
        return ["+"]
    return [_map_key(p) for p in parts]


# --- coordinate helper -----------------------------------------------------

def _to_input(model_x: float, model_y: float, monitor: geometry.Monitor, scale: float) -> tuple[int, int]:
    """Model click -> clamped, integer global OS coordinate."""
    gx, gy = geometry.model_to_input_coords(model_x, model_y, monitor=monitor, scale=scale)
    gx, gy = geometry.clamp_to_monitor(gx, gy, monitor)
    return int(round(gx)), int(round(gy))


# --- held keys and buttons -------------------------------------------------

def _key_down(g, key: str) -> None:
    _held_keys.append(key)
    g.keyDown(key)


def _key_up(g, key: str) -> None:
    g.keyUp(key)
    if key in _held_keys:
        _held_keys.remove(key)   # only once it is up: a failed release stays recorded


def _release(g, keys: list[str]) -> None:
    """Release ``keys`` in reverse order, each in its own ``try``."""
    errors: list[BaseException] = []
    for k in reversed(keys):
        try:
            _key_up(g, k)
        except Exception as e:  # noqa: BLE001  # every key gets its release attempt
            errors.append(e)
    if errors:
        raise errors[0]


def held() -> tuple[list[str], list[str]]:
    return list(_held_keys), list(_held_buttons)


def release_held() -> None:
    """Release whatever is still recorded as held (an exception mid-step, a
    signal). Sends nothing when nothing is held. Never raises."""
    if not (_held_keys or _held_buttons):
        return
    g = _pyautogui
    for k in reversed(list(_held_keys)):
        try:
            if g is not None:
                g.keyUp(k)
        except Exception:  # noqa: BLE001  # a release must never stop the next one
            pass
    for b in reversed(list(_held_buttons)):
        try:
            if g is not None:
                g.mouseUp(button=b)
        except Exception:  # noqa: BLE001  # a release must never stop the next one
            pass
    _held_keys.clear()
    _held_buttons.clear()


# --- actions ---------------------------------------------------------------

def cursor_position() -> tuple[int, int]:
    """Current global cursor position (OS input space)."""
    g = _gui()
    p = g.position()
    return int(p[0]), int(p[1])


def move_to(model_x, model_y, monitor, scale, *, duration: float = 0.0):
    g = _gui()
    x, y = _to_input(model_x, model_y, monitor, scale)
    g.moveTo(x, y, duration=max(0.0, duration))
    return x, y


def _click_at(g, x, y, *, button, clicks, modifiers):
    mods: list[str] = []
    try:
        for m in modifiers or []:
            _key_down(g, m)
            mods.append(m)
        # pyautogui sleeps ``interval`` after every click, the last one too:
        # only a double or triple click needs it.
        interval = 0.05 if clicks > 1 else 0.0
        if x is None:
            g.click(clicks=clicks, button=button, interval=interval)
        else:
            g.click(x=x, y=y, clicks=clicks, button=button, interval=interval)
    finally:
        _release(g, mods)


def click(model_x, model_y, monitor, scale, *, button="left", clicks=1, modifiers=None):
    g = _gui()
    x, y = _to_input(model_x, model_y, monitor, scale)
    _click_at(g, x, y, button=button, clicks=clicks, modifiers=modifiers)
    return x, y


def click_current(*, button="left", clicks=1, modifiers=None):
    """Click at the CURRENT cursor position (no coordinate supplied)."""
    g = _gui()
    _click_at(g, None, None, button=button, clicks=clicks, modifiers=modifiers)
    p = g.position()
    return int(p[0]), int(p[1])


def drag(end_x, end_y, monitor, scale, *, start=None, button="left", duration: float = 0.4):
    """Press at ``start`` (model coords, or current cursor if None), move to the
    end coordinate with the button held, release. pyautogui's ``dragTo``
    releases the button only when it succeeds, so a failure releases it here."""
    g = _gui()
    if start is not None:
        sx, sy = _to_input(start[0], start[1], monitor, scale)
        g.moveTo(sx, sy, duration=0.0)
    ex, ey = _to_input(end_x, end_y, monitor, scale)
    _held_buttons.append(button)
    try:
        g.dragTo(ex, ey, duration=max(0.05, duration), button=button)
    except BaseException:
        g.mouseUp(button=button)
        _held_buttons.remove(button)   # only once it is up: a failed release stays recorded
        raise
    _held_buttons.remove(button)
    return ex, ey


def scroll(direction: str, amount: int, model_x=None, model_y=None, monitor=None, scale=None):
    """Scroll ``amount`` clicks in ``direction`` (up/down/left/right). Moves to
    the coordinate first when supplied (so the scroll lands over the right
    pane), and returns that point (``None`` when it did not move)."""
    g = _gui()
    point = None
    if model_x is not None and model_y is not None and monitor is not None and scale is not None:
        point = _to_input(model_x, model_y, monitor, scale)
        g.moveTo(point[0], point[1], duration=0.0)
    d = (direction or "down").strip().lower()
    amt = abs(int(amount))
    if d == "up":
        g.scroll(amt)
    elif d == "down":
        g.scroll(-amt)
    elif d in ("left", "right"):
        hscroll = getattr(g, "hscroll", None)
        if hscroll is None:
            raise DeviceUnavailable("Horizontal scroll is not supported on this platform.")
        hscroll(amt if d == "right" else -amt)
    else:
        raise ValueError(f"scroll_direction must be up/down/left/right, got {direction!r}.")
    return point


def press_keys(combo: str):
    """Press an xdotool key combo (``ctrl+s``, ``Return``) once. The keys go
    down in order and come up in reverse in ``finally`` (pyautogui's
    ``hotkey`` releases nothing when a key fails)."""
    g = _gui()
    keys = parse_key_combo(combo)
    down: list[str] = []
    try:
        for k in keys:
            _key_down(g, k)
            down.append(k)
    finally:
        _release(g, down)


def hold_keys(combo: str, duration: float, *, wait: Callable[[float], object] = time.sleep):
    """Hold an xdotool key combo down for ``duration`` seconds, then release.
    ``wait`` is the runner's sleep, which ends early on a cancel."""
    g = _gui()
    keys = parse_key_combo(combo)
    dur = max(0.0, float(duration))
    down: list[str] = []
    try:
        for k in keys:
            _key_down(g, k)
            down.append(k)
        wait(dur)
    finally:
        _release(g, down)


_PASTED_KEYS = ("\n", "\r", "\t")


def types_through_clipboard(text: str) -> bool:
    """Text goes in one paste through the clipboard when it is not ASCII
    (accents, Greek, CJK, emoji: pyautogui's US-keycode mapping cannot produce
    them) or holds a line break or a tab, which typed key by key would press
    Enter or Tab in the middle of the text."""
    return not text.isascii() or any(k in text for k in _PASTED_KEYS)


@dataclass
class Typed:
    summary: str
    typed: int
    stopped: str | None = None   # the reason it stopped between chunks


def type_text(text: str, *, between: Callable[[], str | None] | None = None,
              copy: Callable[[str], object] | None = None) -> Typed:
    """Type literal ``text``. Plain ASCII goes through pyautogui's keystroke
    path in chunks of :data:`TYPE_CHUNK` characters, and ``between()`` is asked
    after each chunk whether to go on (a reason string stops it there). The
    rest (:func:`types_through_clipboard`) is delivered via one clipboard paste,
    written by ``copy`` (the runner's bounded write) or by pyperclip. IME /
    dead-key composition is not supported. The summary gives counts, never the
    text."""
    g = _gui()
    if text == "":
        return Typed("typed empty string", 0)
    if not types_through_clipboard(text):
        typed = 0
        try:
            while typed < len(text):
                chunk = text[typed:typed + TYPE_CHUNK]
                g.write(chunk, interval=0.01)
                typed += len(chunk)
                if typed < len(text) and between is not None:
                    reason = between()
                    if reason:
                        return Typed(f"typed {typed} of {len(text)} characters", typed, reason)
        except BaseException:
            # pyautogui presses Shift around a capital itself, with no finally.
            with contextlib.suppress(Exception):
                g.keyUp("shift")
            raise
        return Typed(f"typed {len(text)} characters", len(text))
    try:
        if copy is not None:
            copy(text)
        else:
            import pyperclip

            pyperclip.copy(text)
    except Exception as e:
        raise DeviceUnavailable(
            "This text goes in through the clipboard, which is unavailable "
            "(install xclip/xsel on Linux). "
            f"({type(e).__name__})"
        ) from e
    press_keys("command+v" if os_kind() == "darwin" else "ctrl+v")
    return Typed(f"pasted {len(text)} characters via clipboard", len(text))


def clipboard_text() -> str:
    import pyperclip

    return pyperclip.paste()


def set_clipboard_text(value: str) -> None:
    import pyperclip

    pyperclip.copy(value)
