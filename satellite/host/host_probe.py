"""Host probes: OS-user identity, well-known folders, PTY/display capability, sizing.

Cross-platform best-effort introspection the satellite reports to the proxy at
connect time (capabilities): the OS account + home dir, the well-known user folders
(XDG / Finder / Explorer), whether an interactive PTY is spawnable, whether a GUI
display is present, and the recommended concurrent-session ceiling from this host's
CPU/RAM. Pure functions over os / platform / psutil; no SessionManager state.
"""
import os
import platform
import re
import sys
from pathlib import Path


# XDG keys we surface. Linux's `XDG_DOWNLOAD_DIR` (singular) is renamed to
# ``downloads`` for cross-OS consistency with macOS / Windows where the
# folder is "Downloads".
_XDG_NAME_MAP = {
    "DESKTOP":   "desktop",
    "DOWNLOAD":  "downloads",
    "DOCUMENTS": "documents",
    "PICTURES":  "pictures",
    "MUSIC":     "music",
    "VIDEOS":    "videos",
}

# Match e.g. ``XDG_DESKTOP_DIR="$HOME/Desktop"`` per xdg-user-dirs spec.
_XDG_LINE_RE = re.compile(r'^\s*XDG_([A-Z]+)_DIR\s*=\s*"(.+)"\s*$')


def _detect_os_user_and_home() -> tuple[str, str]:
    """Return ``(os_user, home_dir)`` using cross-platform best-effort sources.

    ``home_dir`` is normalized to forward-slash form on Windows so the
    proxy can do consistent prefix matching across OSes; Unix paths are
    naturally forward-slash already. ``os_user`` is the real OS account
    the satellite process runs as (NOT the platform-side ``users.username``
    which is a different namespace).
    """
    home_dir = str(Path.home().resolve())
    is_windows = platform.system().lower() == "windows"
    if is_windows:
        home_dir = home_dir.replace("\\", "/")
        os_user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        return os_user, home_dir
    # Unix-like (Linux / macOS / BSD)
    os_user = ""
    try:
        import pwd  # type: ignore[import-not-found]
        os_user = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return os_user, home_dir


def _detect_user_dirs(home_dir: str) -> dict[str, str]:
    """Return well-known user directories per OS.

    Always returns the full set of supported keys so the proxy doesn't
    have to guess missing entries. Values are absolute forward-slash
    paths; existence on disk is NOT checked (a missing Desktop is
    surfaced to the agent as a hint, not an error).
    """
    sys_name = platform.system().lower()
    home = Path(home_dir)

    if sys_name == "linux":
        dirs: dict[str, str] = {}
        xdg_file = home / ".config" / "user-dirs.dirs"
        if xdg_file.is_file():
            try:
                for line in xdg_file.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    m = _XDG_LINE_RE.match(line)
                    if not m:
                        continue
                    key = _XDG_NAME_MAP.get(m.group(1))
                    if not key:
                        continue
                    value = m.group(2).replace("$HOME", home_dir).rstrip("/")
                    dirs[key] = value
            except OSError:
                pass
        # Fill any missing keys with the conventional fallback location.
        defaults = {
            "desktop":   str(home / "Desktop"),
            "downloads": str(home / "Downloads"),
            "documents": str(home / "Documents"),
            "pictures":  str(home / "Pictures"),
            "music":     str(home / "Music"),
            "videos":    str(home / "Videos"),
        }
        for k, v in defaults.items():
            dirs.setdefault(k, v)
        return dirs

    if sys_name == "darwin":
        # macOS uses "Movies" for the videos folder.
        return {
            "desktop":   str(home / "Desktop"),
            "downloads": str(home / "Downloads"),
            "documents": str(home / "Documents"),
            "pictures":  str(home / "Pictures"),
            "music":     str(home / "Music"),
            "videos":    str(home / "Movies"),
        }

    if sys_name == "windows":
        h = home_dir  # already forward-slash normalized
        return {
            "desktop":   f"{h}/Desktop",
            "downloads": f"{h}/Downloads",
            "documents": f"{h}/Documents",
            "pictures":  f"{h}/Pictures",
            "music":     f"{h}/Music",
            "videos":    f"{h}/Videos",
        }

    # Unknown OS — best-effort flat layout.
    return {
        "desktop":   str(home / "Desktop"),
        "downloads": str(home / "Downloads"),
        "documents": str(home / "Documents"),
        "pictures":  str(home / "Pictures"),
        "music":     str(home / "Music"),
        "videos":    str(home / "Videos"),
    }


def _interactive_pty_supported() -> bool:
    """Whether this host can spawn an interactive TUI under a PTY.

    Unix always can (``pty_relay`` is stdlib ``pty``/``fcntl``/``termios``).
    Windows needs the ``winpty_relay`` ConPTY backend, which requires the
    pywinpty wheel — markered ``sys_platform == "win32"`` in requirements, so a
    freshly-(re)built Windows venv has it; we still probe with ``find_spec`` so a
    box whose venv predates the dep advertises False (and falls back to ``-p``)
    rather than opening a PTY it can't drive.
    """
    if os.name == "posix":
        return True
    if sys.platform == "win32":
        import importlib.util
        return importlib.util.find_spec("winpty") is not None
    return False


def _detect_display() -> dict:
    """Best-effort probe: does this machine have an interactive GUI session a
    device-control MCP could drive?

    Stdlib-only — this runs at connect time, before any MCP is installed, so it
    must NOT import pyautogui/mss. It reports only what the proxy's placement
    filter needs (``has_display``) plus the display ``server`` and a coarse
    active/unlocked hint. Screen geometry (monitor count / primary size /
    DPR) is left to the computer-control MCP at action time, which has mss.

    Failure-mode bias: prefer a false POSITIVE over a false negative. A false
    positive degrades to an actionable "no display / locked" error at action
    time; a false negative would silently strip a usable capability.

    ``server``: ``"x11"`` | ``"wayland"`` | ``"quartz"`` | ``"windows"`` |
    ``"none"``. Wayland blocks synthetic input (the MCP returns an actionable
    error), but ``has_display`` is still True — this gate is about display
    PRESENCE, not input method.
    """
    system = platform.system().lower()
    server = "none"
    has_display = False
    try:
        if system == "linux":
            # systemd --user + linger boots WITHOUT a graphical session, so a
            # headless box has neither var set → has_display stays False. A
            # desktop login exports one of these into the user service env.
            if os.environ.get("WAYLAND_DISPLAY"):
                server, has_display = "wayland", True
            elif os.environ.get("DISPLAY"):
                server, has_display = "x11", True
        elif system == "darwin":
            # The satellite runs as a per-user LaunchAgent, which loads inside
            # the user's Aqua (GUI) login session → a desktop is present.
            server, has_display = "quartz", True
        elif system == "windows":
            # The satellite runs as the per-user interactive logon task (not
            # the session-0 service), so the user's desktop is present.
            server, has_display = "windows", True
    except Exception:
        return {"has_display": False, "server": "none", "session_active_unlocked": False}
    return {
        "has_display": has_display,
        "server": server,
        # Coarse connect-time hint only. The MCP performs the authoritative
        # locked / asleep (DPMS) / fast-user-switch check before each capture
        # or input event — that check is time-of-use, not time-of-connect.
        "session_active_unlocked": has_display,
    }


# Mirror of proxy/core/sandbox/host_resources.py, computed from THIS machine's psutil.
# The satellite HARD-ENFORCES this ceiling (physical safety — protects the host
# from OOM, and binds satellite-INITIATED otodock sessions the proxy never sees);
# the proxy additionally honors an admin per-machine override
# (remote_machines.max_sessions) via its own soft pre-check. Env-overridable.
_SAT_PER_SESSION_MB = int(os.environ.get("OTO_SAT_PER_SESSION_MB", "350"))
_SAT_MEM_SAFETY = float(os.environ.get("OTO_SAT_MEM_SAFETY", "0.75"))
_SAT_RESERVE_MB = int(os.environ.get("OTO_SAT_RESERVE_MB", "512"))
_SAT_SESSIONS_PER_CORE = float(os.environ.get("OTO_SAT_SESSIONS_PER_CORE", "2"))
_SAT_MAX_SESSIONS = int(os.environ.get("OTO_SAT_MAX_SESSIONS", "64"))


def _machine_resources() -> tuple[int, int]:
    """(cpu_count, mem_total_bytes) for this host via psutil (cross-platform);
    (cpu, 0) if psutil or the memory total is unavailable."""
    try:
        import psutil
        return (psutil.cpu_count() or 1), int(psutil.virtual_memory().total)
    except Exception:
        return (os.cpu_count() or 1), 0


def _recommended_max_sessions() -> int:
    """Conservative ceiling on concurrent local agent sessions this satellite can
    run, from its CPU + RAM (mirror of the proxy's host_resources formula)."""
    cpus, mem_bytes = _machine_resources()
    cpu_bound = int(cpus * _SAT_SESSIONS_PER_CORE)
    if mem_bytes <= 0:
        return max(1, min(cpu_bound, _SAT_MAX_SESSIONS))
    mem_mb = mem_bytes / 1024 / 1024
    mem_bound = int((mem_mb * _SAT_MEM_SAFETY - _SAT_RESERVE_MB) / max(1, _SAT_PER_SESSION_MB))
    return max(1, min(mem_bound, cpu_bound, _SAT_MAX_SESSIONS))
