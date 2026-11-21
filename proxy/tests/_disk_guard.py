"""The disk-write guard for the event loop's own thread (``loop_disk_guard``).

The rule it fences: no file is written, renamed, synced, removed or copied
while the loop waits for it, and no child process is waited for there. A
blocking write on the loop's thread holds every socket and request for as
long as the disk takes (a session's close once rewrote the security index on
the loop through three calls).

Armed for the calling thread inside ``active()``, every patched call made ON
that thread is recorded and raises ``LoopDiskViolation``. The error is an
``AssertionError`` on purpose: code under test that catches ``OSError`` (most
writers do) cannot swallow it, and the block's exit fails the test with every
recorded site anyway. Calls on other threads pass through untouched. Reads are
not guarded.

What it cannot see: a write through a file object opened before the block,
``os.write`` (the terminals write their PTY on the loop by design), a
``Popen``'s ``wait`` or ``communicate``, a name imported with ``from os
import ...`` (no proxy module does), and work a task the block spawned does
after the block ends: await that work inside the block. It guards the
calling thread only, so drive the app with ``httpx.ASGITransport`` on the
test's own loop, never Starlette's ``TestClient``, whose app runs on a
thread of its own.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator

from services.infra import safe_fs


class LoopDiskViolation(AssertionError):
    """A blocking disk write ran on the guarded thread."""


_WRITE_MODES = set("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

# (owner, attribute name, the condition the call is a write under: None for always).
_TARGETS: list[tuple[object, str, object]] = [
    (builtins, "open", "mode"),
    (io, "open", "mode"),
    (os, "open", "flags"),
    (os, "replace", None),
    (os, "rename", None),
    (os, "fsync", None),
    (os, "fdatasync", None),
    (os, "unlink", None),
    (os, "remove", None),
    (os, "rmdir", None),
    (os, "mkdir", None),
    (os, "makedirs", None),
    (os, "truncate", None),
    (os, "ftruncate", None),
    (os, "link", None),
    (os, "symlink", None),
    (os, "chmod", None),
    (os, "utime", None),
    (os, "system", None),
    (os, "pwrite", None),
    (os, "writev", None),
    (os, "posix_fallocate", None),
    (os, "sync", None),
    (os, "chown", None),
    (tarfile, "open", "mode"),
    (io, "FileIO", "class-mode"),
    (shutil, "rmtree", None),
    (shutil, "copy", None),
    (shutil, "copy2", None),
    (shutil, "copyfile", None),
    (shutil, "copytree", None),
    (shutil, "move", None),
    (pathlib.Path, "write_text", None),
    (pathlib.Path, "write_bytes", None),
    (pathlib.Path, "unlink", None),
    (pathlib.Path, "mkdir", None),
    (pathlib.Path, "rename", None),
    (pathlib.Path, "replace", None),
    (pathlib.Path, "rmdir", None),
    (pathlib.Path, "touch", None),
    (pathlib.Path, "chmod", None),
    (pathlib.Path, "symlink_to", None),
    (pathlib.Path, "hardlink_to", None),
    (safe_fs, "atomic_write_beneath", None),
    (safe_fs, "atomic_writer", None),
    (safe_fs, "rmtree_beneath", None),
    (safe_fs, "unlink_beneath", None),
    (safe_fs, "rename_beneath", None),
    (safe_fs, "move_beneath", None),
    (safe_fs, "copy_file_beneath", None),
    (safe_fs, "copytree_beneath", None),
    (safe_fs, "mkdirs_beneath", None),
    # A child the loop waits for (``docker cp``) holds it as a disk does.
    (subprocess, "run", None),
    (subprocess, "call", None),
    (subprocess, "check_call", None),
    (subprocess, "check_output", None),
]


def _is_write(kind: object, args: tuple, kwargs: dict) -> bool:
    if kind in ("mode", "class-mode"):
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
        return isinstance(mode, str) and bool(_WRITE_MODES & set(mode))
    if kind == "flags":
        flags = kwargs.get("flags", args[1] if len(args) > 1 else os.O_RDONLY)
        return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
    return True


def _caller() -> tuple[str, int, str]:
    """The first frame outside this module and the standard library's file
    modules: the code that asked for the write."""
    frame = sys._getframe(2)
    skip = (__file__, os.path.dirname(shutil.__file__))
    while frame is not None:
        name = frame.f_code.co_filename
        if name != skip[0] and not name.startswith(skip[1] + os.sep) and "safe_fs" not in name:
            return (name, frame.f_lineno, frame.f_code.co_name)
        frame = frame.f_back
    return ("?", 0, "?")


class LoopDiskGuard:
    """One test's guard. ``active()`` arms it for the calling thread."""

    def __init__(self) -> None:
        self.sites: list[tuple[str, int, str, str]] = []
        self._ident: int | None = None
        self._saved: list[tuple[object, str, object]] = []

    def _wrap(self, owner: object, name: str, kind: object):
        real = getattr(owner, name)
        guard = self
        if kind == "class-mode":
            # A class stays a class (``isinstance`` keeps working on the
            # files the block opens through it).
            class Guarded(real):  # type: ignore[valid-type, misc]
                def __init__(self, *args, **kwargs):
                    guard._check(name, kind, args, kwargs)
                    super().__init__(*args, **kwargs)
            Guarded.__wrapped__ = real  # type: ignore[attr-defined]
            return Guarded

        def guarded(*args, **kwargs):
            guard._check(name, kind, args, kwargs)
            return real(*args, **kwargs)

        guarded.__wrapped__ = real  # type: ignore[attr-defined]
        return guarded

    def _check(self, name: str, kind: object, args: tuple, kwargs: dict) -> None:
        if self._ident is not None and threading.get_ident() == self._ident \
                and _is_write(kind, args, kwargs):
            f, line, fn = _caller()
            self.sites.append((f, line, fn, name))
            raise LoopDiskViolation(f"blocking disk call {name} on the loop's thread: {f}:{line} ({fn})")

    @contextlib.contextmanager
    def active(self) -> Iterator[LoopDiskGuard]:
        """Arm the guard for the CURRENT thread for the block; fail at its end
        with every write recorded, swallowed or not."""
        self._ident = threading.get_ident()
        for owner, name, kind in _TARGETS:
            if not hasattr(owner, name):
                continue
            self._saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, self._wrap(owner, name, kind))
        try:
            yield self
        finally:
            self.disarm()
        if self.sites:
            lines = "\n".join(f"  {f}:{line} ({fn}) {name}" for f, line, fn, name in self.sites)
            raise LoopDiskViolation(f"disk writes on the loop's thread:\n{lines}")

    def disarm(self) -> None:
        self._ident = None
        while self._saved:
            owner, name, real = self._saved.pop()
            setattr(owner, name, real)
