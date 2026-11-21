"""Windows reads for the computer tool's checks: the window in front, the
window under a point, and whether its program has more rights than this one.

Imported only on Windows (``environment.desktop``). Every function is declared
with its argument and result types before it is called: a pointer returned
through ctypes' default ``c_int`` would be cut to 32 bits, and reading it kills
the process instead of raising.

The rule behind :func:`blocks_input` is SendInput's (learn.microsoft.com,
"SendInput function", Remarks): "This function is subject to UIPI.
Applications are permitted to inject input only into applications that are at
an equal or lesser integrity level." The level is the last sub-authority of the
token's integrity label (learn.microsoft.com, "Mandatory Integrity Control",
and "TOKEN_MANDATORY_LABEL structure").
"""

from __future__ import annotations

import ctypes
import ntpath
from ctypes import wintypes

from environment import Window

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25          # TOKEN_INFORMATION_CLASS.TokenIntegrityLevel
GA_ROOT = 2                         # GetAncestor: the root window, walking parents
ERROR_ACCESS_DENIED = 5
SECURITY_MANDATORY_HIGH_RID = 0x3000


class SID_AND_ATTRIBUTES(ctypes.Structure):  # the Windows SDK's name
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):  # the Windows SDK's name
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class _Denied(Exception):
    """The system refused to open the process or its token."""


_LIBS = None


def _libs():
    global _LIBS
    if _LIBS is not None:
        return _LIBS
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    hwnd, handle, dword = wintypes.HWND, wintypes.HANDLE, wintypes.DWORD
    for fn, args, res in (
        (user32.GetForegroundWindow, [], hwnd),
        (user32.WindowFromPoint, [wintypes.POINT], hwnd),
        (user32.GetAncestor, [hwnd, wintypes.UINT], hwnd),
        (user32.GetWindowThreadProcessId, [hwnd, ctypes.POINTER(dword)], dword),
        # GetWindowTextW reads another process's caption without sending that
        # window a message, so a hung window cannot hang the read
        # (learn.microsoft.com, "GetWindowTextW function", Remarks).
        (user32.GetWindowTextW, [hwnd, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (kernel32.OpenProcess, [dword, wintypes.BOOL, dword], handle),
        (kernel32.CloseHandle, [handle], wintypes.BOOL),
        (kernel32.GetCurrentProcess, [], handle),
        (kernel32.QueryFullProcessImageNameW, [handle, dword, wintypes.LPWSTR, ctypes.POINTER(dword)], wintypes.BOOL),
        (advapi32.OpenProcessToken, [handle, dword, ctypes.POINTER(handle)], wintypes.BOOL),
        (advapi32.GetTokenInformation, [handle, ctypes.c_int, ctypes.c_void_p, dword, ctypes.POINTER(dword)],
         wintypes.BOOL),
        (advapi32.GetSidSubAuthorityCount, [ctypes.c_void_p], ctypes.POINTER(ctypes.c_ubyte)),
        (advapi32.GetSidSubAuthority, [ctypes.c_void_p, dword], ctypes.POINTER(dword)),
    ):
        fn.argtypes, fn.restype = args, res
    _LIBS = (user32, kernel32, advapi32)
    return _LIBS


def _token_level(advapi32, kernel32, process) -> int:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            raise _Denied
        raise OSError("OpenProcessToken failed")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError("GetTokenInformation gave no size")
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
            raise OSError("GetTokenInformation failed")
        sid = TOKEN_MANDATORY_LABEL.from_buffer(buf).Label.Sid
        count = advapi32.GetSidSubAuthorityCount(sid).contents.value
        return int(advapi32.GetSidSubAuthority(sid, count - 1).contents.value)
    finally:
        kernel32.CloseHandle(token)


_OWN_LEVEL: int | None = None


def _own_level() -> int:
    global _OWN_LEVEL
    if _OWN_LEVEL is None:
        _user32, kernel32, advapi32 = _libs()
        _OWN_LEVEL = _token_level(advapi32, kernel32, kernel32.GetCurrentProcess())
    return _OWN_LEVEL


def _program(kernel32, pid: int) -> str:
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)):
            return ""
        return ntpath.basename(buf.value)
    finally:
        kernel32.CloseHandle(process)


def _describe(hwnd) -> Window:
    user32, kernel32, _advapi32 = _libs()
    if not hwnd:
        # No window in front: the secure desktop of a UAC prompt or the lock screen.
        return Window(id="none", pid=None, program="", title="")
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return Window(id=int(hwnd), pid=int(pid.value) or None,
                  program=_program(kernel32, pid.value) if pid.value else "", title=buf.value)


def front_window() -> Window:
    user32, _kernel32, _advapi32 = _libs()
    return _describe(user32.GetForegroundWindow())


def window_at(x: int, y: int) -> Window:
    """The top-level window under a point of the desktop: where a click goes."""
    user32, _kernel32, _advapi32 = _libs()
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return _describe(user32.GetAncestor(hwnd, GA_ROOT) if hwnd else hwnd)


def blocks_input(window: Window) -> bool | None:
    """True when ``window``'s program runs at a higher integrity level than
    this one, so Windows drops this tool's input to it. A process or token the
    system refuses to open counts as higher while this one runs below High (a
    normal satellite reading an administrator program). ``None`` when unknown."""
    if not window.pid:
        return None
    _user32, kernel32, advapi32 = _libs()
    own = _own_level()
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, window.pid)
    if not process:
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            return True if own < SECURITY_MANDATORY_HIGH_RID else None
        return None
    try:
        level = _token_level(advapi32, kernel32, process)
    except _Denied:
        return True if own < SECURITY_MANDATORY_HIGH_RID else None
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(process)
    return level > own
