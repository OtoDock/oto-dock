"""Display / session / platform detection for the computer-control MCP.

The framework's ``has_display`` probe (satellite ``_detect_display``) already
gates *attachment* of this MCP, but a display can go away between attach and
action (screen lock, sleep, fast-user-switch) — and Wayland never supported
synthetic input in the first place. So the MCP **re-checks at call time** and
fails with an ACTIONABLE message rather than a silent no-op or a raw crash.

Stdlib-only + environment reads, so it imports cleanly in headless/test
contexts (the heavyweight backends — pyautogui, mss, Quartz — are imported
lazily by ``screen.py`` / ``executor.py``, never here).
"""

from __future__ import annotations

import os
import sys


class DeviceUnavailable(Exception):
    """Raised when the device cannot be captured or controlled right now.

    The message is user/agent-facing and must say WHAT is wrong and, where
    possible, what to do about it. The server turns this into an error
    ``TextContent`` — it is never allowed to crash the MCP.
    """


def os_kind() -> str:
    """``"linux"`` | ``"darwin"`` | ``"windows"`` | ``"<other>"``."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p.startswith("win"):
        return "windows"
    return p


def display_backend() -> str:
    """Best-effort detection of the active display server.

    ``"wayland"`` | ``"x11"`` | ``"quartz"`` | ``"windows"`` | ``"none"``.

    Mirrors the satellite-side ``_detect_display`` heuristics so the MCP's
    call-time view agrees with the attach-time gate. Linux biases toward a
    *false positive* (claiming a display when unsure) because a false negative
    would wrongly refuse a working session — actual capture/input failures are
    surfaced from the backend with their own actionable errors.
    """
    kind = os_kind()
    if kind == "darwin":
        return "quartz"
    if kind == "windows":
        return "windows"
    if kind == "linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        # systemd --user services boot without a graphical session (headless
        # box) — no $DISPLAY, no $WAYLAND_DISPLAY.
        return "none"
    return "none"


def is_wayland() -> bool:
    return display_backend() == "wayland"


def check_capture_supported() -> None:
    """Raise :class:`DeviceUnavailable` if a screenshot cannot be taken at all.

    Capture under Wayland is attempted (many setups capture via XWayland), so
    only a hard *no display* is refused here; an mss failure surfaces its own
    actionable error from :mod:`screen`.
    """
    backend = display_backend()
    if backend == "none":
        raise DeviceUnavailable(
            "This machine has no active graphical display (likely a headless "
            "server, or the satellite is running as a background service "
            "without a desktop session). Screen capture is unavailable."
        )


def check_input_supported() -> None:
    """Raise :class:`DeviceUnavailable` if synthetic mouse/keyboard input cannot
    be delivered. Hard-refuses Wayland (synthetic input is blocked by the
    compositor) with a clear next step, and refuses a missing display."""
    backend = display_backend()
    if backend == "none":
        raise DeviceUnavailable(
            "This machine has no active graphical display, so mouse/keyboard "
            "input cannot be delivered."
        )
    if backend == "wayland":
        raise DeviceUnavailable(
            "This session is Wayland, which blocks synthetic mouse/keyboard "
            "input for security. Screen capture may still work, but clicks and "
            "typing cannot be injected. Log the machine into an X11/Xorg "
            "session (on the login screen choose 'Ubuntu on Xorg' or the "
            "equivalent) to use computer control."
        )


def session_active_unlocked() -> bool | None:
    """Best-effort: is there an active, unlocked interactive session right now?

    Returns ``True`` / ``False`` when known, ``None`` when undetermined. The
    caller treats ``None`` as "proceed" (don't false-block on a metric we can't
    read); a definite ``False`` (e.g. macOS reports the console is not on this
    session) is surfaced as an actionable error before capture/input.
    """
    kind = os_kind()
    if kind == "darwin":
        try:
            import Quartz  # type: ignore  (pyobjc; present on a mac satellite)

            d = Quartz.CGSessionCopyCurrentDictionary()
            if not d:
                # No Core Graphics session dict => not on the console (ssh /
                # headless / login window).
                return False
            on_console = d.get("kCGSessionOnConsoleKey")
            if on_console is None:
                return None
            return bool(on_console)
        except Exception:  # noqa: BLE001  # pyobjc calls raise whatever the macOS bridge does
            return None
    # Linux/Windows: reliable lock-state detection needs more than stdlib and
    # varies by DE/config. Treat as unknown for v1 — capture/input will simply
    # produce a black frame or no-op error if the session is actually locked,
    # which the agent can see and react to.
    return None


def check_session_active() -> None:
    """Raise :class:`DeviceUnavailable` only when the session is DEFINITELY not
    active (``session_active_unlocked() is False``)."""
    if session_active_unlocked() is False:
        raise DeviceUnavailable(
            "No interactive desktop session is currently on the console "
            "(the machine may be at the login window, locked, or controlled "
            "over a remote/headless connection). Screen capture and input are "
            "unavailable until a user is logged in at the physical console."
        )


def check_macos_permissions(prompt: bool = False) -> dict:
    """Read macOS's Accessibility (input) and Screen Recording (capture)
    grants for this process.

    Returns ``{"accessibility": bool|None, "screen_recording": bool|None,
    "notes": str}``. ``None`` => couldn't determine (pyobjc missing or the API
    unavailable). Non-macOS returns all-None with an explanatory note. With
    ``prompt`` macOS is asked to show its two permission questions for what is
    not granted yet, which also lists the app in System Settings: the first
    action of a process asks once, the ``check_permissions`` tool never does.
    """
    if os_kind() != "darwin":
        return {
            "accessibility": None,
            "screen_recording": None,
            "notes": "Permission checks are macOS only. This is not macOS.",
        }
    out: dict = {"accessibility": None, "screen_recording": None, "notes": ""}
    notes = []
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        out["accessibility"] = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))
    except Exception:  # noqa: BLE001  # pyobjc calls raise whatever the macOS bridge does
        notes.append("Could not read the Accessibility status")
    try:
        import Quartz  # type: ignore

        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is not None:
            granted = bool(preflight())
            request = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
            if not granted and prompt and request is not None:
                granted = bool(request())
            out["screen_recording"] = granted
        else:
            notes.append("This macOS cannot report Screen Recording")
    except Exception:  # noqa: BLE001  # pyobjc calls raise whatever the macOS bridge does
        notes.append("Could not read the Screen Recording status")
    missing = [k for k, v in (("screen", out["screen_recording"]), ("accessibility", out["accessibility"]))
               if v is False]
    if missing:
        notes.append(grant_message(missing))
    out["notes"] = ". ".join(n.rstrip(".") for n in notes) + ("." if notes else "")
    return out


def responsible_app() -> tuple[str, str] | None:
    """``(name, path)`` of the app macOS credits this process's permissions
    to (the satellite's ``OtoDock Satellite.app``, or for an older install
    its Python), read from libSystem. ``None`` when unreadable."""
    if os_kind() != "darwin":
        return None
    try:
        import ctypes

        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        who = lib.responsibility_get_pid_responsible_for_pid
        who.argtypes, who.restype = [ctypes.c_int], ctypes.c_int
        buf = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(who(os.getpid()), buf, ctypes.c_uint32(4096)) <= 0:
            return None
        path = buf.value.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  # a private libSystem call: any failure means unknown
        return None
    bundle = path.split(".app/Contents/MacOS/")[0] + ".app" if ".app/Contents/MacOS/" in path else ""
    name = os.path.basename(bundle)[: -len(".app")] if bundle else os.path.basename(path)
    return name, bundle or path


def _macos_major() -> int:
    import platform

    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except ValueError:
        return 0


def grant_message(missing: list[str]) -> str:
    """What the person turns on, named as their macOS shows it. ``missing``
    holds ``"screen"`` and/or ``"accessibility"``."""
    major = _macos_major()
    lists = {
        "accessibility": "Device Control and Data Access" if major >= 27 else "Accessibility",
        "screen": "Screen & System Audio Recording" if major >= 15 else "Screen Recording",
    }
    can = {"screen": "see the screen", "accessibility": "control the mouse and keyboard"}
    app = responsible_app()
    start = ""
    if app is not None and app[1].endswith(".app"):
        who = app[0]
        where = who if who == "OtoDock Satellite" else f"{who} ({app[1]})"
    else:
        # A bare program is never the one to turn on: over SSH it is sshd,
        # and its switch would let every SSH sign-in see and drive the screen.
        who = where = "the satellite's app"
        if app is not None:
            start = "Start the satellite from its login item, not from a terminal. "
    return (
        f"macOS does not let {who} {' or '.join(can[m] for m in missing)} yet. {start}"
        f"Turn on {where} in System Settings, Privacy & Security, under "
        f"{' and under '.join(lists[m] for m in missing)}. "
        "Then start a new chat on this machine."
    )


# The first action of a process asks macOS to show its questions, once.
_asked = False


def check_macos_grants(input_needed: bool) -> None:
    """On a Mac, raise :class:`DeviceUnavailable` when macOS has DEFINITELY
    not granted what an action needs: Screen Recording for the screenshot
    every action returns, Accessibility for input. macOS drops the input and
    greys the capture without an error, so a pretend success would mislead.
    An unreadable status (``None``) never refuses."""
    global _asked
    if os_kind() != "darwin":
        return
    perms = check_macos_permissions(prompt=not _asked)
    _asked = True
    missing = []
    if perms["screen_recording"] is False:
        missing.append("screen")
    if input_needed and perms["accessibility"] is False:
        missing.append("accessibility")
    if missing:
        raise DeviceUnavailable(grant_message(missing))
