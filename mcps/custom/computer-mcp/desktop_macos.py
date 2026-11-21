"""macOS reads for the computer tool's checks: the window that takes the keys,
and Secure Input with the program that holds it.

Imported only on macOS (``environment.desktop``). The window that takes the
keys is the accessibility API's focused application and its focused window
(Apple's "AXUIElement.h": ``kAXFocusedApplicationAttribute``,
``kAXFocusedWindowAttribute``), which the tool may read because it holds the
Accessibility grant already; Spotlight and a window-less app's menu are focused
applications too, which the front window of the window list is not. The id is
that application's front layer-0 window number in ``CGWindowListCopyWindowInfo``
(Apple's "Quartz Window Services"), so a title that changes is the same window.

Secure Input is Carbon's ``IsSecureEventInputEnabled`` (Apple's "Carbon Event
Manager", and Technical Note TN2150), which is true while any program has it
on. The program that holds it is the console session dictionary's
``kCGSSessionSecureInputPID``, an undocumented key that macOS's own ``ioreg``
output shows; when it is missing the holder is unknown.
"""

from __future__ import annotations

import ctypes

from environment import Window

_CARBON = "/System/Library/Frameworks/Carbon.framework/Carbon"
_secure_fn = None


def _windows_of(pid: int | None) -> list[dict]:
    import Quartz

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    rows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    out = []
    for row in rows:
        if int(row.get("kCGWindowLayer", 1)) != 0:
            continue
        if pid is not None and int(row.get("kCGWindowOwnerPID", -1)) != pid:
            continue
        out.append(row)
    return out


def _focused() -> tuple[int, str] | None:
    """(pid, title) of the focused application's focused window, or None when
    the accessibility API answers nothing."""
    import ApplicationServices as ax

    system = ax.AXUIElementCreateSystemWide()
    err, app = ax.AXUIElementCopyAttributeValue(system, ax.kAXFocusedApplicationAttribute, None)
    if err or app is None:
        return None
    err, pid = ax.AXUIElementGetPid(app, None)
    if err:
        return None
    title = ""
    err, win = ax.AXUIElementCopyAttributeValue(app, ax.kAXFocusedWindowAttribute, None)
    if not err and win is not None:
        err, value = ax.AXUIElementCopyAttributeValue(win, ax.kAXTitleAttribute, None)
        if not err and value:
            title = str(value)
    return int(pid), title


def front_window() -> Window:
    focused = _focused()
    if focused is not None:
        pid, title = focused
        rows = _windows_of(pid)
        row = rows[0] if rows else {}
        number = row.get("kCGWindowNumber")
        return Window(id=(pid, int(number)) if number is not None else (pid, title), pid=pid,
                      program=str(row.get("kCGWindowOwnerName", "")), title=title)
    rows = _windows_of(None)
    if not rows:
        return Window(id="none", pid=None, program="", title="")
    row = rows[0]
    pid = int(row.get("kCGWindowOwnerPID", 0)) or None
    return Window(id=(pid, int(row.get("kCGWindowNumber", 0))), pid=pid,
                  program=str(row.get("kCGWindowOwnerName", "")), title=str(row.get("kCGWindowName", "") or ""))


def secure_input() -> tuple[bool, int | None]:
    """(on, the pid of the program that holds it or None)."""
    global _secure_fn
    if _secure_fn is None:
        fn = ctypes.CDLL(_CARBON).IsSecureEventInputEnabled
        fn.argtypes, fn.restype = [], ctypes.c_bool
        _secure_fn = fn
    if not _secure_fn():
        return False, None
    import Quartz

    session = Quartz.CGSessionCopyCurrentDictionary() or {}
    holder = session.get("kCGSSessionSecureInputPID")
    return True, (int(holder) or None) if holder else None


def program_of(pid: int) -> str:
    rows = _windows_of(pid)
    return str(rows[0].get("kCGWindowOwnerName", "")) if rows else ""
