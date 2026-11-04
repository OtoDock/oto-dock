"""The interpreters a script's first line names, on a Windows machine
(a script check or an app step, ``sessions/step_runner.argv_for``):
Git for Windows' ``bash.exe`` and ``sh.exe`` and its ``env.exe``, which the
baseline tools install, and the install's own Python. Found once per
process.

Never a bare ``bash`` from PATH: ``System32\\bash.exe`` is WSL's launcher,
which runs the script inside Linux, where a Windows path means nothing.
Never the Microsoft Store's ``python`` alias under ``WindowsApps``, which
cannot run a script and opens the Store instead.
"""

from __future__ import annotations

import functools
import os
import shutil
import sys
from pathlib import Path

#: The kinds ``argv_for`` asks for; a missing one is ``""``.
KINDS = ("bash", "sh", "env", "python")


def _git_roots() -> list[Path]:
    """Where Git for Windows may sit: its machine-wide and per-user install
    folders (where the baseline tools put it), then the root of the ``git``
    on PATH (``<root>\\cmd\\git.exe``, ``<root>\\bin\\git.exe`` or
    ``<root>\\mingw64\\bin\\git.exe``) for a custom install. The install
    folders come first because a PATH entry is one any program can write
    ahead of Git's, and the bash found here runs every script check."""
    roots: list[Path] = []
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]) / "Git")
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git")
    git = shutil.which("git")
    if git:
        here = Path(git).resolve().parent
        roots += [here.parent, here.parent.parent]
    return roots


def _outside_store(path: str) -> bool:
    return "\\windowsapps\\" not in path.lower().replace("/", "\\")


@functools.cache
def windows_interpreters() -> dict[str, str]:
    """``{kind: absolute path}`` for every kind in ``KINDS``."""
    tools = dict.fromkeys(KINDS, "")
    for root in _git_roots():
        bash = root / "bin" / "bash.exe"
        if bash.is_file():
            sh = root / "bin" / "sh.exe"
            env = root / "usr" / "bin" / "env.exe"
            tools.update(bash=str(bash), sh=str(sh if sh.is_file() else bash),
                         env=str(env) if env.is_file() else "")
            break
    base = Path(sys.base_prefix) / "python.exe"
    if base.is_file() and _outside_store(str(base)):
        tools["python"] = str(base)
    else:
        for name in ("python", "python3"):
            found = shutil.which(name)
            if found and _outside_store(found):
                tools["python"] = found
                break
    return tools
