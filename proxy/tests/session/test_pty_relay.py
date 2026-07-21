"""Tests for core.sandbox.pty_relay — PTY-backed spawning for interactive CLI sessions.

DB-free: spawns small real processes (cat / python -c) under a real PTY and
exercises the write/output/scrollback/resize/terminate + EOF paths. Validates
the py3.10 controlling-terminal handling.
"""
import asyncio
import os

import pytest

from core.sandbox.pty_relay import spawn_pty

_ENV = {"TERM": "xterm-256color", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _collector():
    """Return (buf, on_output) where on_output appends to buf."""
    buf = bytearray()
    return buf, (buf.extend)


def _exit_future(loop):
    """Return (future, on_exit) where on_exit resolves the future once."""
    fut = loop.create_future()

    def on_exit(code):
        if not fut.done():
            fut.set_result(code)

    return fut, on_exit


@pytest.mark.asyncio
class TestPtyRelay:
    async def test_write_echo_and_scrollback(self):
        loop = asyncio.get_running_loop()
        out, on_output = _collector()
        fut, on_exit = _exit_future(loop)
        proc = spawn_pty(["cat"], env=_ENV, on_output=on_output, on_exit=on_exit)
        try:
            proc.write(b"hello\n")
            await asyncio.sleep(0.3)
            # Terminal echo + cat's own echo — either way "hello" is rendered.
            assert b"hello" in bytes(out)
            assert b"hello" in proc.scrollback()
        finally:
            proc.terminate()
        code = await asyncio.wait_for(fut, 5)
        assert code is not None  # killed by signal (negative) — just not hung

    async def test_self_exit_fires_on_exit_zero(self):
        loop = asyncio.get_running_loop()
        out, on_output = _collector()
        fut, on_exit = _exit_future(loop)
        spawn_pty(
            ["python3", "-c", "print('DONE123')"],
            env=_ENV, on_output=on_output, on_exit=on_exit,
        )
        code = await asyncio.wait_for(fut, 5)
        assert code == 0
        assert b"DONE123" in bytes(out)

    async def test_terminate_signals_child(self):
        loop = asyncio.get_running_loop()
        _, on_output = _collector()
        fut, on_exit = _exit_future(loop)
        # Sleeps far longer than the test — only terminate() ends it.
        proc = spawn_pty(
            ["python3", "-c", "import time; time.sleep(60)"],
            env=_ENV, on_output=on_output, on_exit=on_exit,
        )
        await asyncio.sleep(0.2)
        assert not proc.closed
        proc.terminate()
        code = await asyncio.wait_for(fut, 5)
        # Negative = died from a signal (SIGTERM -> -15).
        assert code is not None and code < 0

    async def test_resize_does_not_raise(self):
        loop = asyncio.get_running_loop()
        _, on_output = _collector()
        fut, on_exit = _exit_future(loop)
        proc = spawn_pty(["cat"], env=_ENV, on_output=on_output, on_exit=on_exit)
        try:
            proc.resize(40, 120)
            assert (proc.rows, proc.cols) == (40, 120)
        finally:
            proc.terminate()
        await asyncio.wait_for(fut, 5)

    async def test_scrollback_is_bounded(self):
        loop = asyncio.get_running_loop()
        _, on_output = _collector()
        fut, on_exit = _exit_future(loop)
        proc = spawn_pty(
            ["cat"], env=_ENV, scrollback_limit=64,
            on_output=on_output, on_exit=on_exit,
        )
        try:
            for _ in range(20):
                proc.write(b"0123456789ABCDEF\n")
            await asyncio.sleep(0.3)
            # Ring is chunk-granular; never grows unbounded past the limit.
            assert len(proc.scrollback()) <= 64
        finally:
            proc.terminate()
        await asyncio.wait_for(fut, 5)

    async def test_idempotent_close(self):
        loop = asyncio.get_running_loop()
        _, on_output = _collector()
        fut, on_exit = _exit_future(loop)
        proc = spawn_pty(["cat"], env=_ENV, on_output=on_output, on_exit=on_exit)
        proc.close()
        proc.close()  # must not raise or double-fire
        code = await asyncio.wait_for(fut, 5)
        assert code is not None

    async def test_missing_term_falls_back(self):
        loop = asyncio.get_running_loop()
        out, on_output = _collector()
        fut, on_exit = _exit_future(loop)
        # No TERM in env — spawn_pty injects a fallback rather than failing.
        spawn_pty(
            ["python3", "-c", "import os; print('TERM=' + os.environ.get('TERM', 'UNSET'))"],
            env={"PATH": _ENV["PATH"]}, on_output=on_output, on_exit=on_exit,
        )
        code = await asyncio.wait_for(fut, 5)
        assert code == 0
        assert b"TERM=xterm-256color" in bytes(out)


@pytest.mark.asyncio
async def test_large_write_survives_busy_child():
    """A paste larger than the kernel PTY buffer into a child that isn't
    reading yet must arrive intact: the non-blocking master returns partial
    writes / EAGAIN and the relay buffers + flushes the remainder (dropping it
    loses injected prompts — the delegate-wake bug)."""
    import sys

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
    out, on_output = _collector()
    loop = asyncio.get_running_loop()
    fut, on_exit = _exit_future(loop)
    proc = spawn_pty([sys.executable, "-u", "-c", child], env=_ENV,
                     on_output=on_output, on_exit=on_exit)
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
    await asyncio.wait_for(fut, 5)
