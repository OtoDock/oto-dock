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


# ---------------------------------------------------------------------------
# Spawns take the vfork path, sessions run below the
# proxy, and no more than SESSION_SPAWN_CONCURRENCY start at once.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402

import config  # noqa: E402
from core.sandbox import pty_relay  # noqa: E402


async def _pty_output(argv, timeout=5.0) -> bytes:
    loop = asyncio.get_running_loop()
    out, on_output = _collector()
    fut, on_exit = _exit_future(loop)
    spawn_pty(argv, env=_ENV, on_output=on_output, on_exit=on_exit)
    await asyncio.wait_for(fut, timeout)
    await asyncio.sleep(0.05)
    return bytes(out)


@pytest.mark.asyncio
async def test_pty_spawn_has_no_preexec_hook(monkeypatch):
    seen = []
    real = subprocess.Popen

    def recording(*a, **k):
        seen.append(k)
        return real(*a, **k)

    monkeypatch.setattr(pty_relay.subprocess, "Popen", recording)
    await _pty_output(["true"])
    assert seen and all(k.get("preexec_fn") is None for k in seen)
    assert seen[0].get("start_new_session") is True


@pytest.mark.asyncio
async def test_pty_child_leads_its_session_on_its_terminal_at_lower_priority():
    out = await _pty_output(["python3", "-c",
        "import os; print(os.getsid(0) == os.getpid(), "
        "os.getpgrp() == os.getpid(), os.tcgetpgrp(0) == os.getpgrp(), os.nice(0))"])
    assert f"True True True {pty_relay.SESSION_NICE}".encode() in out


@pytest.mark.asyncio
async def test_pty_child_starts_with_every_signal_at_default():
    out = await _pty_output(["grep", "-E", "^Sig(Ign|Blk)", "/proc/self/status"])
    assert b"SigIgn:\t0000000000000000" in out
    assert b"SigBlk:\t0000000000000000" in out


def test_pty_spawn_of_a_missing_binary_raises():
    async def go():
        spawn_pty(["no-such-binary-l2"], env=_ENV, on_output=lambda b: None)
    with pytest.raises(FileNotFoundError):
        asyncio.run(go())


@pytest.mark.asyncio
@pytest.mark.parametrize("pidfd", [True, False])
async def test_spawn_piped_speaks_like_an_asyncio_process(monkeypatch, pidfd):
    if not pidfd:
        monkeypatch.setattr(pty_relay.os, "pidfd_open", None, raising=False)
    elif not hasattr(os, "pidfd_open"):
        # A Python built without it (T1's): the raw syscall, same semantics.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)

        def _pidfd_open(pid, flags=0):
            fd = libc.syscall(434, pid, flags)
            if fd < 0:
                raise OSError(ctypes.get_errno(), "pidfd_open")
            return fd
        monkeypatch.setattr(pty_relay.os, "pidfd_open", _pidfd_open, raising=False)
    proc = await pty_relay.spawn_piped(
        pty_relay.niced(["sh", "-c", "cat; ps -o ni= -p $$ >&2"]),
        cwd=None, env=dict(_ENV), limit=1024 * 1024)
    assert (proc._pidfd is not None) is pidfd
    assert proc.returncode is None
    proc.stdin.write(b"hello\n")
    await proc.stdin.drain()
    assert await asyncio.wait_for(proc.stdout.readline(), 5) == b"hello\n"
    proc.stdin.close()
    assert await asyncio.wait_for(proc.wait(), 5) == 0
    assert proc.returncode == 0
    assert (await proc.stderr.read()).strip() == str(pty_relay.SESSION_NICE).encode()


@pytest.mark.asyncio
async def test_spawn_piped_of_a_missing_binary_raises():
    with pytest.raises(FileNotFoundError):
        await pty_relay.spawn_piped(["no-such-binary-l2"], cwd=None,
                                    env=dict(_ENV), limit=1024)


@pytest.mark.asyncio
async def test_a_cancelled_spawn_leaves_no_child(monkeypatch):
    import threading
    gate = threading.Event()
    spawned = []
    real = pty_relay._popen_piped

    def slow(argv, cwd, env):
        gate.wait(5)
        res = real(argv, cwd, env)
        spawned.append(res[0])
        return res

    monkeypatch.setattr(pty_relay, "_popen_piped", slow)
    task = asyncio.create_task(pty_relay.spawn_piped(
        ["sleep", "30"], cwd=None, env=dict(_ENV), limit=1024))
    await asyncio.sleep(0.05)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if spawned and spawned[0].poll() is not None:
            break
        await asyncio.sleep(0.05)
    assert spawned and spawned[0].poll() is not None


@pytest.mark.asyncio
async def test_spawn_slot_admits_the_configured_number_then_waits_the_settle(monkeypatch, caplog):
    monkeypatch.setattr(config, "SESSION_SPAWN_CONCURRENCY", 2)
    monkeypatch.setattr(pty_relay, "_SPAWN_SETTLE_S", 0.2)
    pty_relay._spawn_slots.clear()
    entered = []

    async def one(i):
        async with pty_relay.spawn_slot():
            entered.append((i, asyncio.get_running_loop().time()))

    t0 = asyncio.get_running_loop().time()
    with caplog.at_level("INFO", logger="claude-proxy.pty_relay"):
        await asyncio.gather(*(one(i) for i in range(3)))
    times = sorted(t - t0 for _i, t in entered)
    assert times[0] < 0.05 and times[1] < 0.05
    assert times[2] >= 0.18                    # the third waited out a settle
    waits = [r for r in caplog.records if "waited" in r.getMessage()]
    assert len(waits) == 1 and "(2 at once)" in waits[0].getMessage()


@pytest.mark.asyncio
async def test_a_failed_spawn_frees_its_slot_at_once_and_a_cancelled_waiter_takes_none(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SPAWN_CONCURRENCY", 1)
    monkeypatch.setattr(pty_relay, "_SPAWN_SETTLE_S", 5.0)
    pty_relay._spawn_slots.clear()
    with pytest.raises(OSError):
        async with pty_relay.spawn_slot():
            raise OSError("spawn failed")
    waiter = asyncio.create_task(asyncio.wait_for(_enter_slot(), 0.5))
    assert await waiter is True                # admitted: the failure freed it
    blocked = asyncio.create_task(_enter_slot())
    await asyncio.sleep(0.05)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    assert pty_relay._spawn_semaphore()._value == 0   # the settle still holds the one slot


async def _enter_slot() -> bool:
    async with pty_relay.spawn_slot():
        return True


@pytest.mark.asyncio
async def test_a_returncode_read_before_the_pidfd_reader_ran_releases_wait():
    """Reaping the child through ``returncode`` removes the pidfd reader; the
    pending ``wait()`` must still return."""
    class _Popen:
        pid = 4242

        def __init__(self):
            self.rc = None

        def poll(self):
            return self.rc

    r, w = os.pipe()
    popen = _Popen()
    proc = pty_relay.PipedProcess(popen, None, None, None, r)
    waiter = asyncio.ensure_future(proc.wait())
    await asyncio.sleep(0)
    popen.rc = 0
    assert proc.returncode == 0
    assert await asyncio.wait_for(waiter, 2) == 0
    os.close(w)
