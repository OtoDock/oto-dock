"""ConPTY-backed process spawning for interactive CLI sessions — SATELLITE side, **Windows**.

The Windows sibling of ``pty_relay`` (the Unix ``pty.openpty`` backend). Same
public interface — :func:`spawn_pty` returning a handle with
``write``/``resize``/``close``/``terminate``/``scrollback``/``closed``/``pid``
and ``on_output``/``on_exit`` callbacks — so ``pty_session`` can dispatch to
either backend by ``sys.platform`` and everything above the spawn seam (the
``PtySession`` orchestration, the ``pty_*`` WS frames, the proxy's entire
``RemotePtyProcess`` / ``InteractiveSession``) is identical.

Backed by **pywinpty** (``winpty.PtyProcess`` → Windows ConPTY, falling back to
the bundled winpty agent on pre-1809 builds). Unix establishes a controlling tty
and registers the master fd with the asyncio loop via ``add_reader``; Windows has
**no** ``add_reader`` on the Proactor event loop and the ConPTY read handle is a
synchronous blocking pipe, so output is pumped by a dedicated **reader thread**
that blocks on ``PtyProcess.read`` and hands each chunk back to the loop with
``loop.call_soon_threadsafe`` (the only thread-safe way to touch loop state).

pywinpty's high-level ``PtyProcess`` works in ``str`` (it owns an incremental
UTF-8 decoder, so a read never splits a multi-byte sequence). We re-encode to
UTF-8 bytes for the wire so the frame shape matches the Unix backend exactly
(terminal content is ANSI + text; the round-trip is loss-free for the control
bytes that matter and ``errors="replace"`` guards the rest). Keystrokes arrive as
bytes from the proxy and are decoded to ``str`` for ``PtyProcess.write``.

The forceful kill goes through the satellite's psutil-backed
``kill_process_tree`` (not just ``PtyProcess.terminate``) so the CLI's MCP child
processes are reaped too — Windows has no process group / ``--die-with-parent``,
so a bare ConPTY close can orphan them (the same orphan-lock class the MCP-update
fixes chase).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
from collections import deque
from typing import Awaitable, Callable, Optional, Sequence

from ..config import kill_process_tree

logger = logging.getLogger("satellite")

DEFAULT_SCROLLBACK_BYTES = 256 * 1024
DEFAULT_ROWS, DEFAULT_COLS = 24, 80
_READ_CHUNK = 65536

# on_output(rendered_bytes) / on_exit(exit_code|None) — either may be sync or
# return a coroutine (it's scheduled on the loop). Identical to pty_relay.
OutputCb = Callable[[bytes], "Optional[Awaitable[None]]"]
ExitCb = Callable[["Optional[int]"], "Optional[Awaitable[None]]"]


class WinPtyProcess:
    """A subprocess on a Windows ConPTY, integrated with the asyncio loop.

    Construct via :func:`spawn_pty`. A reader thread blocks on the ConPTY read
    handle and forwards output to ``on_output`` (and a bounded scrollback ring)
    via the loop thread. ``on_exit`` fires exactly once when the child exits or
    is killed. :meth:`close` is idempotent. Mirrors
    ``pty_relay.PtyProcess`` so the two backends are interchangeable.
    """

    def __init__(
        self,
        *,
        proc: "object",  # winpty.PtyProcess
        rows: int,
        cols: int,
        on_output: OutputCb,
        on_exit: Optional[ExitCb],
        scrollback_limit: int,
    ) -> None:
        self._proc = proc
        self.pid = proc.pid
        self.rows = rows
        self.cols = cols
        self._on_output = on_output
        self._on_exit = on_exit
        self._scrollback_limit = scrollback_limit
        self._scrollback: "deque[bytes]" = deque()
        self._scrollback_len = 0
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._exit_fired = False
        self._reader: Optional[threading.Thread] = None

    # -- attach ---------------------------------------------------------------
    def _attach(self) -> None:
        """Start the blocking reader thread (called once by spawn_pty)."""
        self._reader = threading.Thread(
            target=self._reader_loop, name=f"winpty-{self.pid}", daemon=True,
        )
        self._reader.start()

    # -- output: child -> proxy (runs on the READER THREAD) -------------------
    def _reader_loop(self) -> None:
        """Block on ConPTY reads; hand each chunk to the loop WITH backpressure.

        pywinpty's ``PtyProcess.read`` blocks until data is available and raises
        ``EOFError`` once the child has exited and the pipe is drained. Each chunk
        is forwarded onto the proxy-bound LOSSLESS lane via a coroutine run on the
        loop; this thread BLOCKS on its result, so when the lane (the WS)
        is congested/disconnected the read pauses → the ConPTY pipe fills → the
        TUI's write() blocks, instead of us dropping bytes. The 1s poll lets the
        thread notice close() and abandon a put wedged on a full lane.
        """
        try:
            while not self._closed:
                try:
                    data = self._proc.read(_READ_CHUNK)  # str, blocking
                except EOFError:
                    break
                except OSError:
                    break
                if not data:
                    # Defensive: with blocking reads this shouldn't occur while
                    # alive, but never busy-spin if a pywinpty build returns ''.
                    if not self._proc.isalive():
                        break
                    continue
                payload = data.encode("utf-8", "replace")
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        self._handle_output_async(payload), self._loop)
                except RuntimeError:
                    break  # loop gone (shutdown)
                while not self._closed:
                    try:
                        fut.result(timeout=1.0)
                        break
                    except concurrent.futures.TimeoutError:
                        continue  # lane full — keep waiting (until drained or close)
                    except Exception:
                        break  # enqueue failed (loop closing / cancelled)
        except Exception:  # never let the reader thread die silently
            logger.exception("winpty %s: reader thread error", self.pid)
        finally:
            self._loop.call_soon_threadsafe(self._on_eof)

    async def _handle_output_async(self, data: bytes) -> None:
        """On the LOOP: mirror to scrollback + forward via the backpressured lane.
        Awaited by the reader thread (run_coroutine_threadsafe) so a full lane
        stalls the thread."""
        if self._closed:
            return
        self._remember(data)
        out = self._on_output(data)
        if asyncio.iscoroutine(out):
            await out  # backpressure: blocks while the lane is full

    def _remember(self, data: bytes) -> None:
        self._scrollback.append(data)
        self._scrollback_len += len(data)
        while self._scrollback_len > self._scrollback_limit and self._scrollback:
            self._scrollback_len -= len(self._scrollback.popleft())

    def scrollback(self) -> bytes:
        """Recent rendered output, for replay to a reconnecting viewer."""
        return b"".join(self._scrollback)

    def inject_output(self, data: bytes) -> None:
        """Feed satellite-authored bytes into the output stream as if the child
        printed them (scrollback + lane) — twin of pty_relay.inject_output.
        Call on the LOOP thread, right after spawn."""
        if self._closed:
            return
        self._remember(data)
        out = self._on_output(data)
        if asyncio.iscoroutine(out):
            self._loop.create_task(out)

    # -- input: proxy -> child (runs on the LOOP thread) ----------------------
    def write(self, data: bytes) -> None:
        """Write raw bytes (keystrokes) to the ConPTY → the TUI's stdin."""
        if self._closed or not data:
            return
        try:
            self._proc.write(data.decode("utf-8", "replace"))
        except Exception as exc:
            logger.warning("winpty %s: write failed: %s", self.pid, exc)

    def resize(self, rows: int, cols: int) -> None:
        """Relay a client window resize to the ConPTY."""
        if self._closed:
            return
        self.rows, self.cols = rows, cols
        try:
            self._proc.setwinsize(rows, cols)
        except Exception as exc:
            logger.debug("winpty %s: resize failed: %s", self.pid, exc)

    # -- lifecycle ------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def _on_eof(self) -> None:
        # The child exited (reader hit EOF). Tear down without re-signalling.
        if self._closed:
            return
        self.close(signal_child=False)

    def terminate(self) -> None:
        """Public kill — lease takeover, mode toggle, or idle reap."""
        self.close(signal_child=True)

    def close(self, *, signal_child: bool = True) -> None:
        """Kill (optionally) the child tree, reap, fire on_exit. Idempotent.

        ``signal_child=False`` when the child already exited (EOF). The actual
        kill + reap runs in :meth:`_teardown` so the loop thread never blocks on
        ``kill_process_tree`` / ``wait``.
        """
        if self._closed:
            return
        self._closed = True
        self._loop.create_task(self._teardown(signal_child))

    async def _teardown(self, signal_child: bool) -> None:
        if signal_child:
            try:
                await self._loop.run_in_executor(None, self._kill_tree)
            except Exception:
                logger.exception("winpty %s: kill_tree failed", self.pid)
        code: Optional[int] = None
        try:
            code = await self._loop.run_in_executor(None, self._reap)
        except Exception:
            logger.exception("winpty %s: reap failed", self.pid)
        # Release the ConPTY handles now that the child is gone (the reader
        # thread has hit EOF). Best-effort — GC would do it eventually.
        with contextlib.suppress(Exception):
            self._proc.close(force=True)
        self._fire_exit(code)

    def _kill_tree(self) -> None:
        # Runs in a threadpool. Tree-kill so the CLI's MCP children die too
        # (no process group on Windows; PtyProcess.terminate alone can orphan
        # them and leave the venv locked — the MCP-update orphan-lock class).
        try:
            kill_process_tree(self.pid, timeout=5.0)
        except Exception as exc:
            logger.debug("winpty %s: kill_process_tree failed: %s", self.pid, exc)
        try:
            if self._proc.isalive():
                self._proc.terminate(force=True)
        except Exception:
            pass

    def _reap(self) -> Optional[int]:
        # Runs in a threadpool. The child is already dead (killed, or exited on
        # its own); wait() polls isalive and returns the exit status.
        try:
            return self._proc.wait()
        except Exception:
            return None

    def _fire_exit(self, code: Optional[int]) -> None:
        if self._exit_fired:
            return
        self._exit_fired = True
        if self._on_exit is None:
            return
        try:
            res = self._on_exit(code)
            if asyncio.iscoroutine(res):
                self._loop.create_task(res)
        except Exception:
            logger.exception("winpty %s: on_exit callback failed", self.pid)


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
) -> WinPtyProcess:
    """Spawn ``argv`` on a fresh ConPTY and return a loop-integrated handle.

    Signature mirrors ``pty_relay.spawn_pty``. ``argv`` is the fully-assembled
    command (``[claude_bin, "--model", ...]`` — the interactive TUI, no ``-p``).
    ``env`` is the COMPLETE child environment (pywinpty replaces, doesn't merge),
    curated + path-translated by the caller. Must be called from the event-loop
    thread.
    """
    # pywinpty is a Windows-only wheel (requirements.txt markers it
    # ``sys_platform == "win32"``); this module is only imported on win32 by
    # pty_session, so the import lives here to surface a clean runtime error if
    # the wheel is somehow absent rather than failing the whole module import.
    from winpty import PtyProcess

    argv = list(argv)
    if not argv:
        raise ValueError("spawn_pty: empty argv")

    # Resolve the binary against the child PATH (PATHEXT-aware → finds
    # ``claude.exe``); pywinpty's CreateProcess can't launch a bare name or a
    # .cmd shim. Fall back to the original token if which() comes up empty.
    import shutil
    resolved = shutil.which(argv[0], path=env.get("PATH"))
    if resolved:
        argv[0] = resolved

    proc = PtyProcess.spawn(
        argv,
        cwd=cwd,
        env=env,
        dimensions=(rows, cols),  # pywinpty takes (rows, cols)
    )

    handle = WinPtyProcess(
        proc=proc,
        rows=rows,
        cols=cols,
        on_output=on_output,
        on_exit=on_exit,
        scrollback_limit=scrollback_limit,
    )
    handle._attach()
    logger.info(
        "conpty spawned pid=%s (%dx%d) argv0=%s",
        proc.pid, cols, rows, argv[0],
    )
    return handle
