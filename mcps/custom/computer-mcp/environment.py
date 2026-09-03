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
        except Exception:
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


def check_macos_permissions() -> dict:
    """Probe macOS Accessibility (input) + Screen Recording (capture) grants.

    Returns ``{"accessibility": bool|None, "screen_recording": bool|None,
    "notes": str}``. ``None`` => couldn't determine (pyobjc/Quartz missing or
    API unavailable). Non-macOS returns all-None with an explanatory note.
    Used by the ``check_permissions`` action so a first-run user gets a clear
    "go grant these two toggles" message instead of mysterious black frames /
    dead clicks.
    """
    if os_kind() != "darwin":
        return {
            "accessibility": None,
            "screen_recording": None,
            "notes": "Permission probes are macOS-only; this is not macOS.",
        }
    out: dict = {"accessibility": None, "screen_recording": None, "notes": ""}
    notes = []
    # Accessibility (synthetic input) via ApplicationServices.AXIsProcessTrusted.
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore

        out["accessibility"] = bool(AXIsProcessTrusted())
    except Exception:
        notes.append("could not read Accessibility status")
    # Screen Recording (capture) via Quartz preflight (macOS 10.15+).
    try:
        import Quartz  # type: ignore

        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is not None:
            out["screen_recording"] = bool(preflight())
        else:
            notes.append("Screen Recording preflight API unavailable (old macOS)")
    except Exception:
        notes.append("could not read Screen Recording status")
    if out["accessibility"] is False or out["screen_recording"] is False:
        notes.append(
            "Grant the satellite's host app BOTH Accessibility and Screen "
            "Recording under System Settings -> Privacy & Security, then "
            "restart the satellite."
        )
    out["notes"] = "; ".join(notes)
    return out
