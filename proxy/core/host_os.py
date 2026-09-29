"""The host operating system as a table — the facts the platform and the
satellite branch on, and the words the OS goes by.

Three vocabularies name one thing. The WIRE word is what a satellite
reports in its capabilities (``remote_machines.capabilities.os``,
``platform.system().lower()`` as it has always been): ``linux`` /
``darwin`` / ``windows``. The interpreter's word is ``sys.platform``
(``linux…`` / ``darwin`` / ``win32``); ``family_of`` maps it. The bootstrap
route's ``?os=`` values (``linux`` / ``macos`` / ``windows`` plus their
aliases) are the install FLAVOUR — which script — not the host;
``bootstrap_family`` folds them to a family.

Generic code asks a FACT on the row (``row.posix``, ``row.venv_bin``,
``row.case_insensitive`` …) and never compares the word. The table is
defined twice on purpose: ``satellite/config.py`` is the satellite's
boot-guard leaf and may import nothing but the standard library — a broken
vendored module would raise before the guard could roll an update back —
so it carries the same rows as this module's TWIN, pinned by the gate's
twin rule (``_table``, ``family_of``, ``of``) and by
``tests/core/test_host_os.py``, which imports both and compares every row.
The proxy's own host is Linux; this module answers for REMOTE machines by
their reported word. Core-seams phase 10.
"""

from __future__ import annotations

import dataclasses
import platform
import re
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The words (twin: satellite/config.py)
# ---------------------------------------------------------------------------

LINUX = "linux"
DARWIN = "darwin"
WINDOWS = "windows"
#: The three families a satellite runs on, keyed by their wire word.
FAMILIES = (LINUX, DARWIN, WINDOWS)

#: The per-user service manager that (re)starts the satellite.
SERVICE_SYSTEMD = "systemd"
SERVICE_LAUNCHD = "launchd"
SERVICE_SCHTASKS = "schtasks"

#: The display server a machine reports (``capabilities.display.server``).
DISPLAY_X11 = "x11"
DISPLAY_WAYLAND = "wayland"
DISPLAY_QUARTZ = "quartz"
DISPLAY_WINDOWS = "windows"
DISPLAY_NONE = "none"
DISPLAY_SERVERS = (DISPLAY_X11, DISPLAY_WAYLAND, DISPLAY_QUARTZ, DISPLAY_WINDOWS, DISPLAY_NONE)


@dataclass(frozen=True)
class HostOS:
    """One host family's facts. ``name`` is the wire word (``""`` on the
    ``OTHER`` row until ``host_os()`` fills it with the interpreter's)."""

    name: str
    dirname: str                # the per-user install root: .oto-dock | OtoDock
    exe_suffix: str             # "" | .exe
    venv_bin: str               # a venv's binary dir: bin | Scripts
    python_exe: str             # the interpreter a venv ships: python3 | python
    base_python: tuple          # the base interpreter under sys.base_prefix
    posix: bool                 # mode bits, signals, process groups, AF_UNIX, setproctitle, symlinked shims
    peercred: bool              # SO_PEERCRED on a unix socket (Linux)
    case_insensitive: bool      # the default filesystem folds case
    conpty: bool                # the PTY backend is ConPTY (pywinpty)
    locks_running_files: bool   # a running tree cannot be swapped in place (the update stages through runner.ps1)
    service_manager: str        # SERVICE_* or "" (none known)
    powershell: bool            # the install language: install.ps1 / uninstall.ps1 / the PowerShell bootstrap
    tray: bool                  # a system-tray icon
    display_server: str         # DISPLAY_* or "" (probed at connect)
    xdg_user_dirs: bool         # ~/.config/user-dirs.dirs names the well-known folders
    videos_folder: str          # Videos | Movies

    @property
    def script_suffix(self) -> str:
        """The install scripts' suffix (``uninstall.sh`` / ``uninstall.ps1``)."""
        return ".ps1" if self.powershell else ".sh"


def _table() -> dict:
    linux = HostOS(
        name=LINUX, dirname=".oto-dock", exe_suffix="", venv_bin="bin",
        python_exe="python3", base_python=("bin", "python3"), posix=True,
        peercred=True, case_insensitive=False, conpty=False,
        locks_running_files=False, service_manager=SERVICE_SYSTEMD,
        powershell=False, tray=False, display_server="", xdg_user_dirs=True,
        videos_folder="Videos",
    )
    darwin = HostOS(
        name=DARWIN, dirname=".oto-dock", exe_suffix="", venv_bin="bin",
        python_exe="python3", base_python=("bin", "python3"), posix=True,
        peercred=False, case_insensitive=True, conpty=False,
        locks_running_files=False, service_manager=SERVICE_LAUNCHD,
        powershell=False, tray=False, display_server=DISPLAY_QUARTZ,
        xdg_user_dirs=False, videos_folder="Movies",
    )
    windows = HostOS(
        name=WINDOWS, dirname="OtoDock", exe_suffix=".exe", venv_bin="Scripts",
        python_exe="python", base_python=("python.exe",), posix=False,
        peercred=False, case_insensitive=True, conpty=True,
        locks_running_files=True, service_manager=SERVICE_SCHTASKS,
        powershell=True, tray=True, display_server=DISPLAY_WINDOWS,
        xdg_user_dirs=False, videos_folder="Videos",
    )
    other = HostOS(
        name="", dirname=".oto-dock", exe_suffix="", venv_bin="bin",
        python_exe="python3", base_python=("bin", "python3"), posix=True,
        peercred=False, case_insensitive=False, conpty=False,
        locks_running_files=False, service_manager="", powershell=False,
        tray=False, display_server=DISPLAY_NONE, xdg_user_dirs=False,
        videos_folder="Videos",
    )
    return {LINUX: linux, DARWIN: darwin, WINDOWS: windows, "": other}


#: The rows by wire word; ``ROWS[""]`` is the row of an interpreter outside
#: the three families (POSIX facts, no service manager, no probes).
ROWS: dict = _table()
OTHER: HostOS = ROWS[""]


def family_of(sys_platform: str) -> str:
    """The family of an interpreter's ``sys.platform`` word, or ``""``."""
    if sys_platform == "win32":
        return WINDOWS
    if sys_platform == "darwin":
        return DARWIN
    if sys_platform.startswith("linux"):
        return LINUX
    return ""


def of(word) -> HostOS | None:
    """The row of a reported ``os`` word (folded: ``strip().lower()``), or
    ``None`` when unreported or unknown — an unreported OS never gets a
    POSIX fact."""
    w = str(word or "").strip().lower()
    if not w or w not in FAMILIES:
        return None
    return ROWS[w]


def host_os() -> HostOS:
    """The row of THIS interpreter. Outside the three families the ``OTHER``
    row, its name the interpreter's own ``platform.system().lower()`` — the
    wire word an exotic host always reported."""
    fam = family_of(sys.platform)
    if fam:
        return ROWS[fam]
    return dataclasses.replace(ROWS[""], name=platform.system().lower())


# ---------------------------------------------------------------------------
# The proxy's questions (not on the satellite)
# ---------------------------------------------------------------------------

#: The bootstrap route's canonical ``?os=`` words — the pairing modal's
#: command keys, in the order it shows them.
BOOTSTRAP_LINUX = "linux"
BOOTSTRAP_MACOS = "macos"
BOOTSTRAP_WINDOWS = "windows"
BOOTSTRAP_FLAVORS = (BOOTSTRAP_LINUX, BOOTSTRAP_MACOS, BOOTSTRAP_WINDOWS)
#: Every word the route accepts, folded to a family. ``linux`` and ``macos``
#: run the same bash bootstrap (``install.sh`` detects the host with
#: ``uname``); ``windows`` the PowerShell one.
_BOOTSTRAP_WORDS = {
    BOOTSTRAP_LINUX: LINUX, "unix": LINUX,
    BOOTSTRAP_MACOS: DARWIN,
    BOOTSTRAP_WINDOWS: WINDOWS, "win": WINDOWS, "win32": WINDOWS,
}


def bootstrap_family(word: str) -> str:
    """The family a ``?os=`` word names (the word already lower-cased by
    the route, which prints it back in its 400), or ``""``."""
    return _BOOTSTRAP_WORDS.get(word, "")


_VENV_BIN_RE = re.compile(r"venv/bin/([^/\s\"']+)")


def translate_venv(s: str, row: HostOS) -> str:
    """Rewrite a ``venv/bin/<binary>`` path for ``row``'s venv layout:
    ``venv/Scripts/<binary>.exe`` on Windows (``python3`` collapses to the
    interpreter Windows ships, ``python``); the identity for a POSIX row.
    Idempotent: an input already in the row's layout is left alone and an
    existing suffix is never doubled."""
    if row.venv_bin == "bin" and not row.exe_suffix and row.python_exe == "python3":
        return s

    def _replace(m: re.Match) -> str:
        binary = m.group(1)
        if binary == "python3":
            binary = row.python_exe
        if row.exe_suffix and not binary.endswith(row.exe_suffix):
            binary = f"{binary}{row.exe_suffix}"
        return f"venv/{row.venv_bin}/{binary}"

    return _VENV_BIN_RE.sub(_replace, s)
