"""Session process spawning off the event loop: PTYs for interactive CLI
sessions, pipes for the headless ones (:func:`spawn_piped`).

Spawns a subprocess attached to a pseudo-terminal so it renders its native
interactive TUI (Claude Code = inline/Ink; Codex = alt-screen/Ratatui) instead
of the headless ``-p`` stream. The proxy drives it over the PTY master fd:
keystrokes in (:meth:`PtyProcess.write`), rendered bytes out (``on_output``),
window resize (:meth:`PtyProcess.resize` → SIGWINCH). A bounded scrollback ring
replays recent output to a reconnecting viewer.

This is the low-level mechanism only — argv/env assembly (mirroring the ``-p``
spawn minus ``-p``/stream-json, plus ``TERM``) and the session registry / lease
/ drainer live in ``interactive_session.py`` (the platform bwrap profile + a PTY
runs interactive TUIs on this host).

Host quirks handled here:
  * No ``preexec_fn`` anywhere: it forces a real ``fork()`` of the whole proxy
    with the GIL held (a loop stall that grows with RSS, and a child that
    deadlocks before exec freezes the loop for good). ``Popen`` without it
    takes CPython's vfork path. The PTY's controlling terminal is taken by a
    small exec shim instead (``_PTY_EXEC_SHIM``); ``start_new_session`` makes
    the child a session leader first, and Popen dups the slave onto 0/1/2.
  * ``TERM`` must be present in the child env; callers include it (the sandbox
    env overrides don't set it — ``sandbox.get_env_overrides``).
  * Session trees run below the proxy (``SESSION_NICE``, plus the session's
    autogroup nice where the kernel groups by session), so a spawn wave's
    interpreter and MCP starts never take the event loop's core, and at most
    ``config.SESSION_SPAWN_CONCURRENCY`` sessions start at once
    (:func:`spawn_slot`).

Today this ships a bare PTY + a server-side scrollback ring. tmux-backed
persistence (survives a proxy restart) + multi-viewer is a future enhancement;
the spawn seam here stays clean for it.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable, Optional, Sequence

import config

logger = logging.getLogger("claude-proxy.pty_relay")

# Session trees run at this nice value, below the proxy.
SESSION_NICE = 10
# A spawn slot also covers the child's first second after exec, when its
# interpreter and MCP starts burn the most CPU.
_SPAWN_SETTLE_S = 1.0
# How often wait() polls where the kernel or the Python build has no pidfd.
_WAIT_POLL_S = 0.05

# The PTY child's first program (``python -I -S -c``, stdlib only): every
# signal back to its default and an empty mask (Python ignores SIGPIPE and
# SIGXFSZ at startup, and an ignored signal survives exec), the terminal on
# fd 0 taken as the controlling tty (Popen already made the child a session
# leader), the priority lowered, then the session's argv exec'd.
_PTY_EXEC_SHIM = """
import fcntl, os, signal, sys, termios
for s in signal.valid_signals():
    if s not in (signal.SIGKILL, signal.SIGSTOP):
        try:
            signal.signal(s, signal.SIG_DFL)
        except (OSError, RuntimeError, ValueError):
            pass
signal.pthread_sigmask(signal.SIG_SETMASK, ())
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.nice(int(sys.argv[1]))
try:
    with open("/proc/self/autogroup", "w") as f:
        f.write(sys.argv[1])
except OSError:
    pass
os.execvp(sys.argv[2], sys.argv[2:])
"""

DEFAULT_SCROLLBACK_BYTES = 256 * 1024
DEFAULT_ROWS, DEFAULT_COLS = 24, 80
_READ_CHUNK = 65536

# on_output(rendered_bytes) / on_exit(exit_code|None) — either may be sync or
# return a coroutine (it's scheduled on the loop).
OutputCb = Callable[[bytes], "Optional[Awaitable[None]]"]
ExitCb = Callable[["Optional[int]"], "Optional[Awaitable[None]]"]


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """TIOCSWINSZ on a PTY fd (delivers SIGWINCH to the foreground TUI)."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PtyProcess:
    """A subprocess on a PTY master, integrated with the asyncio loop.

    Construct via :func:`spawn_pty`. The master fd is registered with the loop
    (``add_reader``); output is forwarded to ``on_output`` and mirrored into a
    bounded scrollback ring (:meth:`scrollback`). ``on_exit`` fires exactly once
    when the child's tty closes or it is killed. :meth:`close` is idempotent.
    """

    def __init__(
        self,
        *,
        popen: subprocess.Popen,
        master_fd: int,
        rows: int,
        cols: int,
        on_output: OutputCb,
        on_exit: Optional[ExitCb],
        scrollback_limit: int,
    ) -> None:
        self._popen = popen
        self.pid = popen.pid
        self.master_fd = master_fd
        self.rows = rows
        self.cols = cols
        self._on_output = on_output
        self._on_exit = on_exit
        self._scrollback_limit = scrollback_limit
        self._scrollback: "deque[bytes]" = deque()
        self._scrollback_len = 0
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._reader_active = False
        # Input awaiting the (non-blocking) master fd: the kernel PTY buffer
        # is small and a busy TUI drains it between frames, so a large paste
        # hits partial writes / EAGAIN. The remainder is buffered and flushed
        # via add_writer — dropping it breaks bracketed-paste framing and
        # loses injected prompts (delegate wakes).
        self._write_buf = bytearray()
        self._writer_armed = False

    # -- attach ---------------------------------------------------------------
    def _attach(self) -> None:
        """Register the master fd with the loop (called once by spawn_pty)."""
        os.set_blocking(self.master_fd, False)
        self._loop.add_reader(self.master_fd, self._on_readable)
        self._reader_active = True

    # -- output: child -> proxy ----------------------------------------------
    def _on_readable(self) -> None:
        try:
            while True:
                try:
                    data = os.read(self.master_fd, _READ_CHUNK)
                except BlockingIOError:
                    return  # drained for now; loop will call us again
                except OSError as exc:
                    if exc.errno == errno.EIO:  # slave closed → child gone
                        data = b""
                    else:
                        raise
                if not data:
                    self._on_eof()
                    return
                self._remember(data)
                out = self._on_output(data)
                if asyncio.iscoroutine(out):
                    self._loop.create_task(out)
        except Exception:  # never let a reader callback take down the loop
            logger.exception("pty %s: read loop error", self.pid)
            self._on_eof()

    def _remember(self, data: bytes) -> None:
        self._scrollback.append(data)
        self._scrollback_len += len(data)
        while self._scrollback_len > self._scrollback_limit and self._scrollback:
            self._scrollback_len -= len(self._scrollback.popleft())

    def scrollback(self) -> bytes:
        """Recent rendered output, for replay to a reconnecting viewer."""
        return b"".join(self._scrollback)

    # -- input: proxy -> child ------------------------------------------------
    def write(self, data: bytes) -> None:
        """Write raw bytes (keystrokes) to the PTY → the TUI's stdin.

        Never drops accepted input: what the non-blocking master fd doesn't
        take now (partial write / EAGAIN) is buffered and flushed when the fd
        turns writable."""
        if self._closed or not data:
            return
        self._write_buf.extend(data)
        self._flush_write_buf()

    def _flush_write_buf(self) -> None:
        while self._write_buf:
            try:
                n = os.write(self.master_fd, bytes(self._write_buf))
            except (BlockingIOError, InterruptedError):
                self._arm_writer()
                return
            except OSError as exc:
                logger.warning(
                    "pty %s: write failed, %d byte(s) dropped: %s",
                    self.pid, len(self._write_buf), exc,
                )
                self._write_buf.clear()
                break
            if n <= 0:
                self._arm_writer()
                return
            del self._write_buf[:n]
        self._unarm_writer()

    def _arm_writer(self) -> None:
        if not self._writer_armed and not self._closed:
            self._loop.add_writer(self.master_fd, self._flush_write_buf)
            self._writer_armed = True

    def _unarm_writer(self) -> None:
        if self._writer_armed:
            with contextlib.suppress(Exception):
                self._loop.remove_writer(self.master_fd)
            self._writer_armed = False

    def resize(self, rows: int, cols: int) -> None:
        """Relay a client window resize to the PTY (SIGWINCH to the TUI)."""
        if self._closed:
            return
        self.rows, self.cols = rows, cols
        try:
            _set_winsize(self.master_fd, rows, cols)
        except OSError as exc:
            logger.debug("pty %s: resize failed: %s", self.pid, exc)

    # -- lifecycle ------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def _on_eof(self) -> None:
        # The child closed its tty (exited). Tear down without re-signalling.
        if self._closed:
            return
        self.close(signal_child=False)

    def terminate(self) -> None:
        """Public kill — lease takeover, mode toggle, or idle reap."""
        self.close(signal_child=True)

    def close(self, *, signal_child: bool = True) -> None:
        """Stop reading, optionally kill the process group, reap, fire on_exit.

        Idempotent. ``signal_child=False`` when the child already exited (EOF).
        """
        if self._closed:
            return
        self._closed = True
        if self._reader_active:
            with contextlib.suppress(Exception):  # pragma: no cover - loop teardown races
                self._loop.remove_reader(self.master_fd)
            self._reader_active = False
        if self._write_buf:
            logger.warning(
                "pty %s: closing with %d unflushed input byte(s)",
                self.pid, len(self._write_buf),
            )
            self._write_buf.clear()
        self._unarm_writer()
        if signal_child:
            self._signal_group(signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.close(self.master_fd)
        self._loop.create_task(self._reap_and_notify())

    def _signal_group(self, sig: "signal.Signals") -> None:
        # The child is its own session/group leader (start_new_session), so
        # its pgid == pid; signal the whole group to take down the TUI and
        # anything it spawned. bwrap's --die-with-parent is the backstop.
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.debug("pty %s: killpg(%s) failed: %s", self.pid, sig, exc)

    async def _reap_and_notify(self) -> None:
        code: Optional[int] = None
        try:
            code = await self._loop.run_in_executor(None, self._wait_child)
        except Exception:
            logger.exception("pty %s: reap failed", self.pid)
        if self._on_exit is not None:
            try:
                res = self._on_exit(code)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.exception("pty %s: on_exit callback failed", self.pid)

    def _wait_child(self) -> Optional[int]:
        # Runs in a threadpool. SIGTERM was already sent in close() (or the
        # child exited on its own); escalate to SIGKILL if it lingers.
        try:
            return self._popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)
            try:
                return self._popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return None
        except ChildProcessError:  # already reaped elsewhere
            return None


def spawn_pty(
    argv: Sequence[str],
    *,
    env: dict,
    cwd: Optional[str] = None,
    rows: int = DEFAULT_ROWS,
    cols: int = DEFAULT_COLS,
    on_output: OutputCb,
    on_exit: Optional[ExitCb] = None,
    scrollback_limit: int = DEFAULT_SCROLLBACK_BYTES,
) -> PtyProcess:
    """Spawn ``argv`` on a fresh PTY and return a loop-integrated handle.

    ``argv`` is the fully-assembled command. For a sandboxed local session this
    is ``SandboxBuilder.build_command_prefix([...claude TUI argv...])`` — the
    CLI runs its interactive TUI, not ``-p``. ``env`` MUST include ``TERM``
    (the sandbox env overrides don't set it). The child becomes a new session
    leader with the PTY as its controlling terminal (established by hand for
    py3.13). Must be called from the event-loop thread.
    """
    if "TERM" not in env:
        # A TUI with no TERM renders garbage. Callers should set it; fall back
        # rather than fail.
        logger.warning("spawn_pty: TERM missing from env; defaulting to xterm-256color")
        env = {**env, "TERM": "xterm-256color"}

    # The shim execs argv; a missing binary must still fail here, not in
    # the terminal.
    _require_executable(argv, env)
    master_fd, slave_fd = pty.openpty()
    with contextlib.suppress(OSError):
        _set_winsize(master_fd, rows, cols)

    try:
        popen = subprocess.Popen(
            [sys.executable or "python3", "-I", "-S", "-c", _PTY_EXEC_SHIM,
             str(SESSION_NICE), *argv],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(master_fd)
        raise
    finally:
        # Parent keeps only the master end; the child holds its own dup'd copies.
        with contextlib.suppress(OSError):
            os.close(slave_fd)

    proc = PtyProcess(
        popen=popen,
        master_fd=master_fd,
        rows=rows,
        cols=cols,
        on_output=on_output,
        on_exit=on_exit,
        scrollback_limit=scrollback_limit,
    )
    proc._attach()
    logger.info(
        "pty spawned pid=%s (%dx%d) argv0=%s",
        popen.pid, cols, rows, argv[0] if argv else "?",
    )
    return proc


# ---------------------------------------------------------------------------
# Headless session processes (pipes), the spawn slots, the priority
# ---------------------------------------------------------------------------

_nice_prefix: list[str] | None = None


def nice_prefix() -> list[str]:
    """``["nice", "-n", "10"]`` (resolved once), or ``[]`` with one WARNING
    where ``nice`` is missing."""
    global _nice_prefix
    if _nice_prefix is None:
        nice = shutil.which("nice")
        if nice is None:
            logger.warning("nice not found: session processes start at the proxy's priority")
        _nice_prefix = [nice, "-n", str(SESSION_NICE)] if nice else []
    return _nice_prefix


def niced(argv: Sequence[str]) -> list[str]:
    """``argv`` run below the proxy's CPU priority; its children inherit it."""
    return [*nice_prefix(), *argv]


def _require_executable(argv: Sequence[str], env: dict) -> None:
    if not argv or shutil.which(argv[0], path=env.get("PATH", os.defpath)) is None:
        name = argv[0] if argv else ""
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)


def _set_autogroup_nice(pid: int) -> None:
    """Lower the new session's autogroup too: where the proxy sits in the
    root CPU group with autogroup on, each setsid'd tree is its own group and
    a task nice alone changes nothing between groups. A no-op elsewhere."""
    with contextlib.suppress(OSError):
        with open(f"/proc/{pid}/autogroup", "w") as f:
            f.write(str(SESSION_NICE))


_spawn_slots: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _spawn_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _spawn_slots.get(loop)
    if sem is None:
        for stale in [lp for lp in _spawn_slots if lp.is_closed()]:
            _spawn_slots.pop(stale, None)
        sem = _spawn_slots[loop] = asyncio.Semaphore(
            max(1, int(config.SESSION_SPAWN_CONCURRENCY)))
    return sem


@contextlib.asynccontextmanager
async def spawn_slot():
    """At most ``config.SESSION_SPAWN_CONCURRENCY`` session starts at once.
    A slot is held through the body and ``_SPAWN_SETTLE_S`` after it (the
    child's startup burst); a body that raised frees it at once."""
    sem = _spawn_semaphore()
    if sem.locked():
        loop = asyncio.get_running_loop()
        queued_at = loop.time()
        await sem.acquire()
        logger.info("session spawn waited %.1fs for a slot (%d at once)",
                    loop.time() - queued_at, int(config.SESSION_SPAWN_CONCURRENCY))
    else:
        await sem.acquire()
    try:
        yield
    except BaseException:
        sem.release()
        raise
    try:
        asyncio.get_running_loop().call_later(_SPAWN_SETTLE_S, sem.release)
    except RuntimeError:
        sem.release()


_spawn_executor: ThreadPoolExecutor | None = None
_spawn_executor_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    """The threads that fork session processes. Never shut down while the
    proxy runs: a child's parent-death signal (bwrap --die-with-parent, the
    sandbox launcher) follows the THREAD that forked it, so that thread must
    live as long as the process. A pool of its own also keeps spawns from
    queueing behind slow jobs on the default executor."""
    global _spawn_executor
    with _spawn_executor_lock:
        if _spawn_executor is None:
            _spawn_executor = ThreadPoolExecutor(
                max_workers=max(2, int(config.SESSION_SPAWN_CONCURRENCY)),
                thread_name_prefix="session-spawn")
        return _spawn_executor


def _popen_piped(argv: list[str], cwd: Optional[str], env: dict):
    popen = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=cwd, env=env, start_new_session=True,
    )
    pidfd = None
    opener = getattr(os, "pidfd_open", None)
    if opener is not None:
        try:
            pidfd = opener(popen.pid)
        except OSError:
            pidfd = None
    _set_autogroup_nice(popen.pid)
    return popen, pidfd


def _discard_child(popen: subprocess.Popen, pidfd: Optional[int]) -> None:
    """Kill a child nobody will own (a cancelled or failed spawn) and reap it
    on a daemon thread."""
    with contextlib.suppress(OSError):
        os.killpg(popen.pid, signal.SIGKILL)
    for pipe in (popen.stdin, popen.stdout, popen.stderr):
        with contextlib.suppress(OSError, ValueError):
            if pipe is not None:
                pipe.close()
    if pidfd is not None:
        with contextlib.suppress(OSError):
            os.close(pidfd)
    threading.Thread(target=popen.wait, daemon=True, name="spawn-reap").start()


class PipedProcess:
    """The part of ``asyncio.subprocess.Process`` the session layers use
    (``pid``, ``returncode``, ``stdin``/``stdout``/``stderr``, ``wait``,
    ``send_signal``/``terminate``/``kill``), over a ``Popen`` started off the
    loop."""

    def __init__(self, popen: subprocess.Popen, stdin: asyncio.StreamWriter,
                 stdout: asyncio.StreamReader, stderr: asyncio.StreamReader,
                 pidfd: Optional[int]) -> None:
        self._popen = popen
        self.pid = popen.pid
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._pidfd = pidfd
        self._exited: Optional[asyncio.Future] = None

    @property
    def returncode(self) -> Optional[int]:
        rc = self._popen.poll()
        if rc is not None:
            self._close_pidfd()
        return rc

    def _close_pidfd(self) -> None:
        if self._pidfd is not None:
            fd, self._pidfd = self._pidfd, None
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        # A ``returncode`` read that reaps the child before the pidfd reader
        # ran removes that reader: the pending ``wait()`` is released here.
        if self._exited is not None and not self._exited.done():
            self._exited.set_result(None)

    async def wait(self) -> int:
        rc = self.returncode
        if rc is not None:
            return rc
        if self._pidfd is not None:
            if self._exited is None:
                loop = asyncio.get_running_loop()
                fut = self._exited = loop.create_future()
                fd = self._pidfd

                def _readable() -> None:
                    with contextlib.suppress(Exception):
                        loop.remove_reader(fd)
                    if not fut.done():
                        fut.set_result(None)

                loop.add_reader(fd, _readable)
            await asyncio.shield(self._exited)
        while (rc := self.returncode) is None:
            await asyncio.sleep(_WAIT_POLL_S)
        return rc

    def send_signal(self, sig: int) -> None:
        self._popen.send_signal(sig)

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()


async def spawn_piped(argv: Sequence[str], *, cwd: Optional[str], env: dict,
                      limit: int) -> PipedProcess:
    """Start ``argv`` (a new session, pipes on 0/1/2) from the spawn thread
    and wire its pipes to the running loop. A cancellation or a failure after
    the fork kills the child. Raises what ``Popen`` raises (a missing binary:
    ``FileNotFoundError``)."""
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_executor(), _popen_piped, list(argv), cwd, env)
    try:
        popen, pidfd = await asyncio.shield(fut)
    except asyncio.CancelledError:
        def _drop(f: asyncio.Future) -> None:
            if not f.cancelled() and f.exception() is None:
                _discard_child(*f.result())
        fut.add_done_callback(_drop)
        raise
    try:
        stdout = asyncio.StreamReader(limit=limit, loop=loop)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stdout, loop=loop), popen.stdout)
        stderr = asyncio.StreamReader(limit=limit, loop=loop)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stderr, loop=loop), popen.stderr)
        transport, protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(loop=loop), popen.stdin)
        stdin = asyncio.StreamWriter(transport, protocol, None, loop)
    except BaseException:
        _discard_child(popen, pidfd)
        raise
    return PipedProcess(popen, stdin, stdout, stderr, pidfd)
