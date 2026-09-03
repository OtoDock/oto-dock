"""Input execution for the computer-control MCP — mouse, keyboard, scroll.

Built on PyAutoGUI (cross-platform). ``pyautogui`` is imported **lazily** the
first time an input action runs: importing it eagerly connects to the display
server and crashes on a headless box, which would break the test suite and any
mis-attached session. ``pyautogui.FAILSAFE`` is turned OFF (the default
corner-abort would kill a session mid-task) and replaced with our own
per-monitor bounds clamp (:func:`geometry.clamp_to_monitor`).

Coordinates arriving from the model are in the **model image space** of a
screenshot of some ``display``; the server resolves that display's
``(monitor, scale)`` (see :func:`screen.resolve_target`) and passes it in, and
we convert to a global OS coordinate via :func:`geometry.model_to_input_coords`
before every move/click/drag.

Key names follow xdotool's X-keysym convention (``Return``, ``ctrl+s``,
``alt+Tab``, ``Page_Down`` ...), matching Anthropic's ``computer_20250124``
tool, and are mapped to PyAutoGUI's names by :data:`KEY_MAP`.
"""

from __future__ import annotations

import time

import geometry
from environment import DeviceUnavailable, os_kind

_pyautogui = None


def _gui():
    """Lazily import + configure pyautogui. Raises a clean
    :class:`environment.DeviceUnavailable` if the GUI backend can't load
    (missing display, missing python-xlib, etc.) instead of a raw ImportError."""
    global _pyautogui
    if _pyautogui is not None:
        return _pyautogui
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


def click(model_x, model_y, monitor, scale, *, button="left", clicks=1, modifiers=None):
    g = _gui()
    x, y = _to_input(model_x, model_y, monitor, scale)
    mods = modifiers or []
    for m in mods:
        g.keyDown(m)
    try:
        g.click(x=x, y=y, clicks=clicks, button=button, interval=0.05)
    finally:
        for m in reversed(mods):
            g.keyUp(m)
    return x, y


def click_current(*, button="left", clicks=1, modifiers=None):
    """Click at the CURRENT cursor position (no coordinate supplied)."""
    g = _gui()
    mods = modifiers or []
    for m in mods:
        g.keyDown(m)
    try:
        g.click(clicks=clicks, button=button, interval=0.05)
    finally:
        for m in reversed(mods):
            g.keyUp(m)
    p = g.position()
    return int(p[0]), int(p[1])


def drag(end_x, end_y, monitor, scale, *, start=None, button="left", duration: float = 0.4):
    """Press at ``start`` (model coords, or current cursor if None), move to the
    end coordinate with the button held, release."""
    g = _gui()
    if start is not None:
        sx, sy = _to_input(start[0], start[1], monitor, scale)
        g.moveTo(sx, sy, duration=0.0)
    ex, ey = _to_input(end_x, end_y, monitor, scale)
    g.dragTo(ex, ey, duration=max(0.05, duration), button=button)
    return ex, ey


def mouse_down(model_x, model_y, monitor, scale, *, button="left"):
    g = _gui()
    if model_x is not None and model_y is not None:
        x, y = _to_input(model_x, model_y, monitor, scale)
        g.moveTo(x, y, duration=0.0)
    g.mouseDown(button=button)


def mouse_up(model_x, model_y, monitor, scale, *, button="left"):
    g = _gui()
    if model_x is not None and model_y is not None:
        x, y = _to_input(model_x, model_y, monitor, scale)
        g.moveTo(x, y, duration=0.0)
    g.mouseUp(button=button)


def scroll(direction: str, amount: int, model_x=None, model_y=None, monitor=None, scale=None):
    """Scroll ``amount`` clicks in ``direction`` (up/down/left/right). Moves to
    the coordinate first when supplied (so the scroll lands over the right
    pane)."""
    g = _gui()
    if model_x is not None and model_y is not None and monitor is not None and scale is not None:
        x, y = _to_input(model_x, model_y, monitor, scale)
        g.moveTo(x, y, duration=0.0)
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


def press_keys(combo: str):
    """Press an xdotool key combo (``ctrl+s``, ``Return``) once."""
    g = _gui()
    keys = parse_key_combo(combo)
    if len(keys) == 1:
        g.press(keys[0])
    else:
        g.hotkey(*keys)


def hold_keys(combo: str, duration: float):
    """Hold an xdotool key combo down for ``duration`` seconds, then release."""
    g = _gui()
    keys = parse_key_combo(combo)
    dur = max(0.0, min(float(duration), 30.0))
    for k in keys:
        g.keyDown(k)
    try:
        time.sleep(dur)
    finally:
        for k in reversed(keys):
            g.keyUp(k)


def type_text(text: str):
    """Type literal ``text``. ASCII goes through pyautogui's keystroke path;
    anything non-ASCII (accents, CJK, emoji — which pyautogui's US-keycode
    mapping can't produce) is delivered via a clipboard paste, since synthesizing
    arbitrary Unicode keystrokes is unreliable. IME / dead-key composition is not
    supported. Returns a short note describing the method used."""
    g = _gui()
    if text == "":
        return "typed empty string"
    if text.isascii():
        g.write(text, interval=0.01)
        return f"typed {len(text)} characters"
    # Non-ASCII -> clipboard paste (clobbers the clipboard; documented).
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception as e:
        raise DeviceUnavailable(
            "Non-ASCII text needs a clipboard, which is unavailable "
            "(install xclip/xsel on Linux). "
            f"({type(e).__name__}: {e})"
        ) from e
    paste = "command" if os_kind() == "darwin" else "ctrl"
    g.hotkey(paste, "v")
    return f"pasted {len(text)} characters via clipboard (non-ASCII)"


# --- platform edge detection ----------------------------------------------

def foreground_window_elevated() -> bool | None:
    """Windows-only best-effort: is the foreground window owned by an elevated
    (higher-integrity) process the non-elevated satellite can't drive (UAC /
    "Run as administrator")? Returns ``True`` / ``False`` when determinable,
    ``None`` otherwise (non-Windows, or any probe failure). Used to attach a
    hint when a click on such a window would silently no-op (UIPI)."""
    if os_kind() != "windows":
        return None
    try:
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            # Access denied opening the process is itself a strong signal it is
            # higher-integrity than us.
            return True
        try:
            TOKEN_QUERY = 0x0008
            token = ctypes.wintypes.HANDLE()
            if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
                return None
            try:
                TokenElevation = 20
                elevation = ctypes.wintypes.DWORD()
                ret_len = ctypes.wintypes.DWORD()
                ok = advapi32.GetTokenInformation(
                    token, TokenElevation, ctypes.byref(elevation),
                    ctypes.sizeof(elevation), ctypes.byref(ret_len),
                )
                if not ok:
                    return None
                return bool(elevation.value)
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None
