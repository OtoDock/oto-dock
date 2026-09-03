"""PTY relay input buffering — no dropped bytes on a busy TUI.

The master fd is non-blocking, so a paste larger than the kernel PTY buffer
into a child that isn't reading yet hits partial writes / EAGAIN; the relay
must buffer and flush the remainder instead of dropping it (a dropped tail
breaks bracketed-paste framing and loses injected prompts — the delegate-wake
bug). Unix-only: exercises the real ``pty_relay`` backend.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

if sys.platform == "win32":  # pragma: no cover - unix backend under test
    pytest.skip("unix pty backend", allow_module_level=True)

from satellite.terminal.pty_relay import spawn_pty

_ENV = {"TERM": "xterm-256color", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


@pytest.mark.asyncio
async def test_large_write_survives_busy_child():
    child = (
        "import os,sys,time,tty\n"
        "tty.setraw(0)\n"
        "sys.stdout.write('READY');sys.stdout.flush()\n"
        "time.sleep(0.7)\n"
        "n=0\n"
        "while n < 200000:\n"
        "    n += len(os.read(0, 65536))\n"
        "sys.stdout.write('GOT:%d:END' % n);sys.stdout.flush()\n"
    )
    out = bytearray()
    proc = spawn_pty([sys.executable, "-u", "-c", child], env=_ENV,
                     on_output=out.extend)
    try:
        for _ in range(100):
            if b"READY" in bytes(out):
                break
            await asyncio.sleep(0.05)
        assert b"READY" in bytes(out)
        proc.write(b"x" * 200000)
        for _ in range(160):
            if b"GOT:200000:END" in bytes(out):
                break
            await asyncio.sleep(0.05)
        assert b"GOT:200000:END" in bytes(out)
    finally:
        proc.terminate()
        await asyncio.sleep(0.2)
