"""PTY-backed process spawning for interactive CLI sessions.

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
  * The controlling terminal is established BY HAND in the forked child
    (``setsid`` + ``ioctl(TIOCSCTTY)`` + dup to 0/1/2) — ``os.login_tty`` is
    Python 3.11+, the proxy host is 3.13.
  * ``TERM`` must be present in the child env; callers include it (the sandbox
    env overrides don't set it — ``sandbox.get_env_overrides``).

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
import signal
import struct
import subprocess
import termios
from collections import deque
from typing import Awaitable, Callable, Optional, Sequence

logger = logging.getLogger("claude-proxy.pty_relay")

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
        # The child is its own session/group leader (setsid in the preexec), so
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

    master_fd, slave_fd = pty.openpty()
    with contextlib.suppress(OSError):
        _set_winsize(master_fd, rows, cols)

    def _preexec() -> None:
        # Forked child, pre-exec. Establish the controlling tty by hand
        # (os.login_tty is 3.11+; host is 3.13). ONLY async-signal-safe syscalls
        # here — no allocation/logging (fork-in-a-threaded-process rule). The
        # slave fd survives the close_fds sweep via pass_fds below.
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        for target in (0, 1, 2):
            os.dup2(slave_fd, target)
        if slave_fd > 2:
            os.close(slave_fd)

    try:
        popen = subprocess.Popen(
            list(argv),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            preexec_fn=_preexec,
            pass_fds=(slave_fd,),
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
