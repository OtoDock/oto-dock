"""Host probes: OS-user identity, well-known folders, PTY/display capability, sizing.

Cross-platform best-effort introspection the satellite reports to the proxy at
connect time (capabilities): the OS account + home dir, the well-known user folders
(XDG / Finder / Explorer), whether an interactive PTY is spawnable, whether a GUI
display is present, and the recommended concurrent-session ceiling from this host's
CPU/RAM. Pure functions over os / platform / psutil; no SessionManager state.
"""
import os
import re
import time
from pathlib import Path

from .. import config

# A monotonic gap this much shorter than the sleep clock's gap means the
# system slept (the monotonic clock stops in suspend, the sleep clock does
# not); a gap the two clocks share is a daemon that did not run.
_SLEEP_EXCESS_S = 10.0


def sleep_clock() -> float | None:
    """A clock in seconds that keeps counting while the system sleeps, for
    telling a suspend from a starved daemon beside ``time.monotonic()`` (which
    stops in suspend on Linux and macOS): ``CLOCK_BOOTTIME`` on Linux,
    ``CLOCK_MONOTONIC`` on macOS (Apple's counts sleep; Python's
    ``monotonic()`` there is mach absolute time and does not). ``None`` where
    the host has no second clock: Windows, whose monotonic clock itself runs
    through a sleep (so would the wall clock, and a time step there would
    read as a sleep), and a Linux without ``CLOCK_BOOTTIME``."""
    name = config.HOST.name
    if name == config.LINUX and hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    if name == config.DARWIN and hasattr(time, "CLOCK_MONOTONIC"):
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    return None


def gap_is_sleep(mono_gap: float, sleep_gap: float | None) -> bool | None:
    """Whether a late heartbeat tick was a system sleep (the sleep clock ran
    on while the monotonic clock stood) rather than a daemon that did not run
    (both clocks moved together: a paused host, a starved process). ``None``
    when the host has no sleep clock: the two cannot be told apart."""
    if sleep_gap is None:
        return None
    return sleep_gap - mono_gap > _SLEEP_EXCESS_S


def late_tick_line(mono_gap: float, sleep_gap: float | None, expected_s: float) -> str:
    """The heartbeat's reconnect line for a tick that came ``mono_gap``
    seconds after the last one (``expected_s`` apart), naming the cause when
    the clocks can tell it."""
    verdict = gap_is_sleep(mono_gap, sleep_gap)
    if verdict is None:
        return (f"A {mono_gap:.0f}s gap between heartbeats (a sleep or a daemon that did "
                f"not run; this host cannot tell; expected a ~{expected_s:.0f}s tick); "
                "forcing reconnect")
    if verdict:
        return (f"The system slept for about {sleep_gap:.0f}s (a {mono_gap:.1f}s tick gap, "
                f"expected ~{expected_s:.0f}s); forcing reconnect")
    return (f"The daemon did not run for {mono_gap:.1f}s (expected a ~{expected_s:.0f}s tick; "
            "the host was paused or the process starved, a blocked loop shows in the "
            "watchdog's stack lines); forcing reconnect")


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
    if not config.HOST.posix:
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
    host = config.HOST
    home = Path(home_dir)

    if host.xdg_user_dirs:
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
            "videos":    str(home / host.videos_folder),
        }
        for k, v in defaults.items():
            dirs.setdefault(k, v)
        return dirs

    if not host.posix:
        h = home_dir  # already forward-slash normalized
        return {
            "desktop":   f"{h}/Desktop",
            "downloads": f"{h}/Downloads",
            "documents": f"{h}/Documents",
            "pictures":  f"{h}/Pictures",
            "music":     f"{h}/Music",
            "videos":    f"{h}/Videos",
        }

    # The flat layout: macOS (``Movies`` for the videos folder) and any host
    # outside the three families.
    return {
        "desktop":   str(home / "Desktop"),
        "downloads": str(home / "Downloads"),
        "documents": str(home / "Documents"),
        "pictures":  str(home / "Pictures"),
        "music":     str(home / "Music"),
        "videos":    str(home / host.videos_folder),
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
    if config.HOST.conpty:
        import importlib.util
        return importlib.util.find_spec("winpty") is not None
    return config.HOST.posix


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
    host = config.HOST
    server = config.DISPLAY_NONE
    has_display = False
    if host.display_server:
        # The table knows: macOS runs the satellite as a per-user
        # LaunchAgent inside the Aqua login session (quartz), Windows as
        # the interactive logon task (windows), a host outside the three
        # families reports none.
        server = host.display_server
        has_display = server != config.DISPLAY_NONE
    elif os.environ.get("WAYLAND_DISPLAY"):
        # Linux is probed: systemd --user + linger boots WITHOUT a
        # graphical session, so a headless box has neither var set →
        # has_display stays False. A desktop login exports one of these
        # into the user service env.
        server, has_display = config.DISPLAY_WAYLAND, True
    elif os.environ.get("DISPLAY"):
        server, has_display = config.DISPLAY_X11, True
    return {
        "has_display": has_display,
        "server": server,
        # Coarse connect-time hint only. The MCP performs the authoritative
        # locked / asleep (DPMS) / fast-user-switch check before each capture
        # or input event — that check is time-of-use, not time-of-connect.
        "session_active_unlocked": has_display,
    }


# The satellite's own session ceiling, computed from THIS machine's psutil (the
# proxy admits its local sessions by a different rule — the two-gate memory budget
# in proxy/core/concurrency.py — so nothing here mirrors it).
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
    except (ImportError, OSError, RuntimeError):
        return (os.cpu_count() or 1), 0


def _recommended_max_sessions() -> int:
    """Conservative ceiling on concurrent local agent sessions this satellite can
    run, from its CPU + RAM."""
    cpus, mem_bytes = _machine_resources()
    cpu_bound = int(cpus * _SAT_SESSIONS_PER_CORE)
    if mem_bytes <= 0:
        return max(1, min(cpu_bound, _SAT_MAX_SESSIONS))
    mem_mb = mem_bytes / 1024 / 1024
    mem_bound = int((mem_mb * _SAT_MEM_SAFETY - _SAT_RESERVE_MB) / max(1, _SAT_PER_SESSION_MB))
    return max(1, min(mem_bound, cpu_bound, _SAT_MAX_SESSIONS))
