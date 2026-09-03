"""Regression: Unix auto-update must NOT brick the satellite by losing its venv.

The 0.5.0→0.5.x brick: when requirements.txt changed (`reuse_venv=False`), the
Unix path swapped the dirs FIRST (moving the running venv to satellite.previous/),
then rebuilt the venv with `sys.executable` — whose path
(`…/satellite/venv/bin/python`) no longer existed after the swap → FileNotFoundError
(uncaught by the `except CalledProcessError`) → sat_root left with NO venv → the
satellite couldn't restart and ran a 0.5.0 zombie forever.

Fix: rebuild with `_resolve_base_python()` (the base interpreter, outside the venv,
which survives the swap) + broaden the rollback to OSError. These tests lock the
invariant that the rebuild interpreter is a real, non-venv python.
"""

import os
import sys

from satellite.transport.lifecycle_update import _resolve_base_python


def test_resolve_base_python_exists_on_disk():
    """The returned interpreter PATH must exist — that's the whole point: it has
    to survive the satellite-dir swap (unlike the venv python)."""
    p = _resolve_base_python()
    assert os.path.exists(p), f"base python {p!r} does not exist on disk"


def test_resolve_base_python_is_not_the_venv_interpreter():
    """Must NOT return `sys.executable` / a path inside the venv (sys.prefix) —
    that path is exactly what the dir-swap invalidates."""
    p = _resolve_base_python()
    assert p != sys.executable, "returned the venv python (sys.executable)"
    # In a venv, sys.prefix is the venv dir and sys.base_prefix is the base
    # install; the resolved interpreter must live outside the venv.
    if sys.prefix != sys.base_prefix:  # i.e. we are running inside a venv
        assert not p.startswith(sys.prefix + os.sep), (
            f"resolved python {p!r} is inside the venv {sys.prefix!r}"
        )


def test_resolve_base_python_under_base_prefix_when_available():
    """When the base install's interpreter exists, prefer it (it is
    definitionally outside the venv and is the stable choice)."""
    expected = os.path.join(
        sys.base_prefix,
        "Scripts" if sys.platform == "win32" else "bin",
        "python.exe" if sys.platform == "win32" else "python3",
    )
    if os.path.exists(expected):
        assert _resolve_base_python() == expected
