"""The window in front on an X11 desktop, for the computer tool's checks.

Imported only on an X11 session (``environment.desktop``). It reads through a
python-xlib connection of its own: pyautogui keeps one module-level
connection, which python-xlib does not lock (``Xlib.threaded`` is never
imported), and these reads run in a helper thread while the runner sends input
through pyautogui's.

The window in front is the root window's ``_NET_ACTIVE_WINDOW`` (the EWMH
specification, "_NET_ACTIVE_WINDOW"), set by every current window manager.
Without one (a bare X server) it is the input focus raised to its top-level
window, the child of the root that holds it. PointerRoot and None are focus
values of their own, never windows.
"""

from __future__ import annotations

from environment import Window

_display = None


def _x_errors():
    from Xlib import error as xerror

    return xerror.XError


def _conn():
    global _display
    if _display is None:
        from Xlib import display as xdisplay

        _display = xdisplay.Display()  # $DISPLAY, the same server pyautogui drives
    return _display


def _top_level(win, root):
    """The child of the root that contains ``win`` (where a window manager
    reparents, its frame)."""
    while True:
        tree = win.query_tree()
        parent = tree.parent
        if not parent or parent.id == root.id:
            return win
        win = parent


def _text(d, win, name: str) -> str:
    prop = win.get_full_property(d.intern_atom(name), 0)
    if prop is None or not prop.value:
        return ""
    value = prop.value
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _describe(d, win) -> Window:
    title = _text(d, win, "_NET_WM_NAME") or (win.get_wm_name() or "")
    wm_class = win.get_wm_class()
    program = wm_class[1] if wm_class else ""
    pid_prop = win.get_full_property(d.intern_atom("_NET_WM_PID"), 0)
    pid = int(pid_prop.value[0]) if pid_prop is not None and len(pid_prop.value) else None
    return Window(id=win.id, pid=pid, program=str(program), title=str(title))


def front_window() -> Window:
    d = _conn()
    root = d.screen().root
    active = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), 0)
    if active is not None and len(active.value) and active.value[0]:
        wid = int(active.value[0])
        try:
            return _describe(d, d.create_resource_object("window", wid))
        except _x_errors():
            # Destroyed between the two reads: the id is still the window that was in front.
            return Window(id=wid, pid=None, program="", title="")
    focus = d.get_input_focus().focus
    if isinstance(focus, int):  # X.NONE (0) or X.PointerRoot (1)
        return Window(id=f"focus:{focus}", pid=None, program="", title="")
    return _describe(d, _top_level(focus, root))
