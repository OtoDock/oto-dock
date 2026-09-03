"""otodock-CLI — the thin local client.

Run ON a satellite machine, inside the folder you want to work in::

    otodock claude [agent]       # interactive Claude TUI as the otodock agent
    otodock codex  [agent]       # interactive Codex TUI

(The satellite installs a PATH ``otodock`` wrapper that execs
``python -m satellite.otodock_cli`` — ``bin/otodock`` on Unix, ``bin/otodock.cmd``
on Windows — so you can also run it the long way directly.)

It connects to the satellite's local control channel (an AF_UNIX socket on
Unix/macOS, a named pipe on Windows), asks the platform to open an interactive
session AS the otodock agent in the CURRENT directory, and relays the PTY to THIS
terminal in raw mode. Terminal bytes never leave the machine; the transcript
persists to the platform and the chat is resumable from the dashboard.

Detach without ending the session: press ``Ctrl-] q`` (or just close the terminal
— the session keeps running and is resumable from the dashboard). The agent's own
Ctrl-C / Esc are forwarded to the CLI, not handled here.
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import json
import os
import re
import signal
import sys
import threading
# NOTE: ``termios``/``tty`` are POSIX-only and imported lazily inside
# _PosixTerminal; ``ctypes`` is imported lazily inside _WindowsTerminal. Nothing
# platform-specific loads at module import, so the client imports on both OSes.

try:  # package import (the normal case: python -m satellite.otodock_cli)
    from .otodock_proto import (
        read_frame, encode, encode_json,
        T_OPEN, T_INPUT, T_RESIZE, T_LIST,
        T_OPENED, T_ERROR, T_OUTPUT, T_EXIT, T_LISTED,
    )
    from .local_socket import (
        socket_path, named_pipe_path, set_pipe_byte_mode,
        verify_pipe_server_identity,
    )
    from .terminal_queries import TERMINAL_MODE_RESET
except ImportError:  # pragma: no cover - standalone fallback
    from otodock_proto import (  # type: ignore
        read_frame, encode, encode_json,
        T_OPEN, T_INPUT, T_RESIZE, T_LIST,
        T_OPENED, T_ERROR, T_OUTPUT, T_EXIT, T_LISTED,
    )
    from local_socket import (  # type: ignore
        socket_path, named_pipe_path, set_pipe_byte_mode,
        verify_pipe_server_identity,
    )
    from terminal_queries import TERMINAL_MODE_RESET  # type: ignore

_GS = 0x1d  # Ctrl-] — the detach escape lead-in (then 'q')

_EXEC_PATHS = {"claude": "claude-code-cli", "codex": "codex-cli"}

# Terminal device-query sequences (Primary DA ``ESC[…c`` + DSR ``ESC[…n``). On
# Windows the satellite's OUTER ConPTY emits a DA query into the PTY output; if we
# relay it to the local console, PowerShell ANSWERS it and that reply rides stdin
# back into the CLI → stray bytes in the TUI → cursor jumps + erratic/duplicated
# redraws (worse while scrolling). We strip them from the output ON WINDOWS so the
# local console never answers — the same fix the dashboard applies proxy-side
# (`remote_pty._TERMINAL_QUERY_RE`). NOT stripped on Unix: there the local
# terminal correctly answering the CLI's OWN queries is the right behavior (exactly
# like running the CLI directly). Only query/report CSIs end in c/n — rendering
# sequences (SGR ``m``, cursor ``H``/``A``…, erase ``J``/``K``, scroll ``r``/``S``)
# are untouched.
_TERMINAL_QUERY_RE = re.compile(rb"\x1b\[[0-9;>=?]*[cn]")


def _term_size() -> tuple[int, int]:
    try:
        sz = os.get_terminal_size(sys.stdout.fileno())
        return sz.lines, sz.columns
    except OSError:
        return 24, 80


async def _connect():
    """Open a connection to the local control channel, or print a hint + return
    None. AF_UNIX on Unix/macOS, a named pipe on Windows."""
    if sys.platform == "win32":
        return await _connect_windows()
    path = socket_path()
    try:
        return await asyncio.open_unix_connection(str(path))
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        print(
            "otodock: cannot reach the local satellite "
            f"(no control socket at {path}). Is the otodock satellite running?",
            file=sys.stderr,
        )
        return None


async def _connect_windows():
    """Windows: connect to the satellite's named pipe and adapt it to a
    ``(reader, writer)`` pair. ``create_pipe_connection`` returns
    ``(transport, protocol)`` (NOT a reader/writer like ``open_unix_connection``),
    so we build the StreamReader/StreamWriter the way ``open_connection`` does.
    Bounded by ``wait_for`` because ``connect_pipe`` retries ``ERROR_PIPE_BUSY``
    forever — a wedged satellite must not hang the client."""
    pipe = named_pipe_path()
    loop = asyncio.get_event_loop()
    try:
        reader = asyncio.StreamReader(loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        transport, _ = await asyncio.wait_for(
            loop.create_pipe_connection(lambda: protocol, pipe), timeout=5.0
        )
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        print(
            "otodock: cannot reach the local satellite "
            f"(no control pipe at {pipe}). Is the otodock satellite running?",
            file=sys.stderr,
        )
        return None
    # The pipe name is deterministic and first-come — verify the SERVER
    # actually runs as this user before typing anything into it (a local
    # squatter could otherwise harvest every keystroke). Fail-closed.
    if not verify_pipe_server_identity(transport):
        print(
            "otodock: refusing the control pipe — the process serving it is "
            "not running as your user (or could not be verified). If the "
            "satellite IS running, another local process may be squatting "
            "its pipe name.",
            file=sys.stderr,
        )
        transport.close()
        return None
    # Match the server: byte read mode (stdlib pipes are message-mode).
    set_pipe_byte_mode(transport)
    return reader, writer


async def _pick_resume_chat(agent: str, execution_path: str) -> "tuple[str, str] | None":
    """--resume: fetch the owner's resumable chats for this agent/CLI and let the
    user pick one. Returns ``(chat_id, title)``, or None to cancel."""
    conn = await _connect()
    if conn is None:
        return None
    reader, writer = conn
    writer.write(encode_json(T_LIST, {"agent": agent, "execution_path": execution_path}))
    await writer.drain()
    frame = await read_frame(reader)
    writer.close()
    if frame is None:
        print("otodock: no response from the satellite", file=sys.stderr)
        return None
    ftype, payload = frame
    if ftype == T_ERROR:
        print(f"otodock: {(json.loads(payload or b'{}') or {}).get('reason', 'error')}",
              file=sys.stderr)
        return None
    if ftype != T_LISTED:
        print("otodock: unexpected response from satellite", file=sys.stderr)
        return None
    chats = (json.loads(payload or b"{}") or {}).get("chats", [])
    if not chats:
        print(f"otodock: no resumable {execution_path.split('-')[0]} chats for "
              f"agent '{agent or '(default)'}'.", file=sys.stderr)
        return None
    # Titles derive from session CONTENT (a prompt-injected agent names its
    # own chat) — strip control chars so ANSI/OSC sequences can't rewrite
    # the picker display. Same rule the OSC tab-title path applies.
    _ctrl = re.compile(r"[\x00-\x1f\x7f]")
    print(f"Resumable chats for '{agent}':\n")
    for i, c in enumerate(chats, 1):
        title = _ctrl.sub("", c.get("title") or "(untitled)")[:120]
        origin = "local" if c.get("origin") == "otodock" else "dashboard"
        when = (c.get("updated_at") or "")[:16].replace("T", " ")
        folder = _ctrl.sub("", c.get("work_cwd") or "")
        line = f"  [{i}] {title}  · {origin} · {when}"
        if folder:
            line += f"  ({folder})"
        print(line)
    try:
        choice = input("\nResume which? [number, Enter to cancel]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice:
        return None
    try:
        idx = int(choice)
        if 1 <= idx <= len(chats):
            picked = chats[idx - 1]
            return picked.get("chat_id") or "", picked.get("title") or ""
    except ValueError:
        pass
    print("otodock: invalid selection", file=sys.stderr)
    return None


async def _run(args: argparse.Namespace) -> int:
    cli = args.cli
    execution_path = _EXEC_PATHS[cli]
    cwd = os.path.realpath(args.folder or os.getcwd())
    if not os.path.isdir(cwd):
        print(f"otodock: not a directory: {cwd}", file=sys.stderr)
        return 2

    resume_chat_id = ""
    resume_title = ""
    if args.resume:
        picked = await _pick_resume_chat(args.agent or "", execution_path)
        if not picked or not picked[0]:
            return 0  # cancelled / nothing to resume
        resume_chat_id, resume_title = picked

    conn = await _connect()
    if conn is None:
        return 2
    reader, writer = conn

    rows, cols = _term_size()
    writer.write(encode_json(T_OPEN, {
        "agent": args.agent or "",
        "execution_path": execution_path,
        "cwd": cwd,
        "model": args.model or "",
        "mode": args.mode or "default",
        "rows": rows, "cols": cols,
        # Forward the local terminal's $TERM so the remote PTY renders to match
        # the user's actual terminal (the satellite otherwise forces
        # xterm-256color). Empty → the satellite keeps its default.
        "term": os.environ.get("TERM", ""),
        "resume_chat_id": resume_chat_id,
    }))
    await writer.drain()

    # The FIRST run of an agent on a machine syncs/installs its MCP servers
    # (a pip install can take ~30s+), so opening isn't instant — tell the user
    # we're working instead of leaving a blank line.
    label = f"'{args.agent}'" if args.agent else cli
    print(f"otodock: starting {label}… (first run may install tools)", file=sys.stderr)

    # Await OPENED / ERROR before touching the terminal.
    frame = await read_frame(reader)
    if frame is None:
        print("otodock: connection closed before the session opened", file=sys.stderr)
        return 2
    ftype, payload = frame
    if ftype == T_ERROR:
        reason = (json.loads(payload or b"{}") or {}).get("reason", "unknown error")
        print(f"otodock: {reason}", file=sys.stderr)
        return 1
    if ftype != T_OPENED:
        print("otodock: unexpected response from satellite", file=sys.stderr)
        return 2

    # Tab/window title: "otodock: <agent>", plus the chat title on resume.
    # Control bytes stripped — the title rides an OSC escape verbatim.
    base_title = f"otodock: {args.agent or cli}"
    if resume_title:
        clean = re.sub(r"[\x00-\x1f\x7f]", " ", resume_title).strip()[:60]
        title = f"{base_title} — {clean}" if clean else base_title
    else:
        title = base_title

    return await _relay(reader, writer, cli=cli, agent=args.agent or "",
                        title=title)


def _feed_stdin(data, *, writer, escape_armed, finish, exit_code) -> None:
    """Shared stdin→PTY path: the ``Ctrl-] q`` detach-escape state machine +
    ``T_INPUT`` forwarding. Fed by the POSIX ``add_reader`` callback and the
    Windows reader thread alike, so the detach/relay control logic is byte-for-byte
    identical on every platform. Empty ``data`` = terminal EOF → finish."""
    if not data:
        finish(exit_code["v"])
        return
    out = bytearray()
    for b in data:
        if escape_armed["v"]:
            escape_armed["v"] = False
            if b == ord("q"):
                finish(0)  # graceful detach — session keeps running
                if out:
                    with contextlib.suppress(Exception):
                        writer.write(encode(T_INPUT, bytes(out)))
                return
            out.append(_GS)
            if b == _GS:
                escape_armed["v"] = True
            else:
                out.append(b)
        elif b == _GS:
            escape_armed["v"] = True
        else:
            out.append(b)
    if out:
        try:
            writer.write(encode(T_INPUT, bytes(out)))
        except Exception:
            finish(exit_code["v"])


class _PosixTerminal:
    """Unix/macOS terminal I/O: cbreak raw mode via ``termios``, stdin via the
    event loop's ``add_reader``, resize via ``SIGWINCH``, output via ``os.write``."""

    def __init__(self, loop, writer, escape_armed, finish, exit_code):
        import termios  # POSIX-only — lazy so the module imports on Windows too
        self._termios = termios
        self.loop = loop
        self.writer = writer
        self.escape_armed = escape_armed
        self.finish = finish
        self.exit_code = exit_code
        self.stdin_fd = sys.stdin.fileno()
        self.old_attr = termios.tcgetattr(self.stdin_fd)
        self._restored = False

    def setup(self) -> None:
        import tty
        atexit.register(self._restore)  # restore on EVERY exit path
        tty.setraw(self.stdin_fd)
        # Send the real size immediately (the PTY spawned at the 24x80 default).
        r, c = _term_size()
        self.writer.write(encode_json(T_RESIZE, {"rows": r, "cols": c}))
        with contextlib.suppress(NotImplementedError, ValueError):
            self.loop.add_signal_handler(signal.SIGWINCH, self._on_winch)
        self.loop.add_reader(self.stdin_fd, self._on_stdin)

    def _on_stdin(self) -> None:
        try:
            data = os.read(self.stdin_fd, 65536)
        except OSError:
            return
        _feed_stdin(data, writer=self.writer, escape_armed=self.escape_armed,
                    finish=self.finish, exit_code=self.exit_code)

    def _on_winch(self) -> None:
        r, c = _term_size()
        with contextlib.suppress(Exception):
            self.writer.write(encode_json(T_RESIZE, {"rows": r, "cols": c}))

    def write_output(self, payload: bytes) -> None:
        with contextlib.suppress(OSError):
            os.write(sys.stdout.fileno(), payload)

    def _restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        # The inner TUI enabled mouse tracking / bracketed paste / … on THIS
        # terminal through the mirrored output; on takeover/disconnect it never
        # gets to reset them, and a cooked-mode shell then echoes every mouse
        # report as garbage. Reset the modes ourselves; a dead terminal must
        # never mask the termios restore below.
        with contextlib.suppress(OSError):
            os.write(sys.stdout.fileno(), TERMINAL_MODE_RESET)
        with contextlib.suppress(Exception):
            self._termios.tcsetattr(
                self.stdin_fd, self._termios.TCSADRAIN, self.old_attr)
        # Drop input that piled up while modes were still on (queued mouse
        # reports would echo into the shell prompt).
        with contextlib.suppress(Exception):
            self._termios.tcflush(self.stdin_fd, self._termios.TCIFLUSH)

    def teardown(self) -> None:
        with contextlib.suppress(Exception):
            self.loop.remove_reader(self.stdin_fd)
        with contextlib.suppress(NotImplementedError, ValueError):
            self.loop.remove_signal_handler(signal.SIGWINCH)
        self._restore()


class _WindowsTerminal:
    """Windows console I/O. The Proactor loop has no ``add_reader`` for the
    console, so stdin is read on a blocking daemon thread and handed to the loop via
    ``call_soon_threadsafe``; there is no ``SIGWINCH``, so resize is a 500 ms size
    poll. Raw mode = VT input + a UTF-8 codepage so ``ReadFile`` yields the same
    UTF-8 VT byte stream the POSIX side gets; output goes through
    ``sys.stdout.buffer`` (WriteConsoleW honors ENABLE_VIRTUAL_TERMINAL_PROCESSING).
    All ctypes is lazy + has explicit argtypes/restype (pointer-sized HANDLEs
    truncate to a C int otherwise)."""

    _STD_INPUT_HANDLE = -10
    _STD_OUTPUT_HANDLE = -11
    _ENABLE_PROCESSED_INPUT = 0x0001
    _ENABLE_LINE_INPUT = 0x0002
    _ENABLE_ECHO_INPUT = 0x0004
    _ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    _ENABLE_PROCESSED_OUTPUT = 0x0001
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    _CP_UTF8 = 65001

    def __init__(self, loop, writer, escape_armed, finish, exit_code):
        import ctypes.wintypes
        self._ctypes = ctypes
        self._wintypes = ctypes.wintypes
        self.loop = loop
        self.writer = writer
        self.escape_armed = escape_armed
        self.finish = finish
        self.exit_code = exit_code

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k = k
        k.GetStdHandle.argtypes = [ctypes.wintypes.DWORD]
        k.GetStdHandle.restype = ctypes.wintypes.HANDLE
        k.GetConsoleMode.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
        k.GetConsoleMode.restype = ctypes.wintypes.BOOL
        k.SetConsoleMode.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
        k.SetConsoleMode.restype = ctypes.wintypes.BOOL
        k.ReadFile.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.wintypes.DWORD,
                               ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.c_void_p]
        k.ReadFile.restype = ctypes.wintypes.BOOL
        k.GetConsoleCP.restype = ctypes.wintypes.UINT
        k.GetConsoleOutputCP.restype = ctypes.wintypes.UINT
        k.SetConsoleCP.argtypes = [ctypes.wintypes.UINT]
        k.SetConsoleCP.restype = ctypes.wintypes.BOOL
        k.SetConsoleOutputCP.argtypes = [ctypes.wintypes.UINT]
        k.SetConsoleOutputCP.restype = ctypes.wintypes.BOOL

        self._hin = k.GetStdHandle(self._STD_INPUT_HANDLE & 0xFFFFFFFF)
        self._hout = k.GetStdHandle(self._STD_OUTPUT_HANDLE & 0xFFFFFFFF)
        self._in_mode = None
        self._out_mode = None
        self._old_in_cp = None
        self._old_out_cp = None
        self._restored = False
        self._stop = threading.Event()
        self._reader_thread = None
        self._poll_task = None
        self._last_size = None

    def setup(self) -> None:
        atexit.register(self._restore)
        # Best-effort restore on Ctrl-Break / Ctrl-C. With processed-input off,
        # console Ctrl-C arrives as a 0x03 byte (forwarded to the agent), not a
        # signal — but a Ctrl-Break event or os.kill still fires these. A hard
        # `taskkill /F` can't be trapped (atexit won't run either), so a force-kill
        # may leave the console raw; that is unavoidable and documented.
        for signame in ("SIGBREAK", "SIGINT"):
            sig = getattr(signal, signame, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, self._on_signal)
        self._enter_raw()
        r, c = _term_size()
        self.writer.write(encode_json(T_RESIZE, {"rows": r, "cols": c}))
        self._last_size = (r, c)
        self._reader_thread = threading.Thread(
            target=self._stdin_loop, name="otodock-stdin", daemon=True)
        self._reader_thread.start()
        self._poll_task = self.loop.create_task(self._poll_size())

    def _enter_raw(self) -> None:
        c = self._ctypes
        wt = self._wintypes
        in_mode = wt.DWORD()
        if self._k.GetConsoleMode(self._hin, c.byref(in_mode)):
            self._in_mode = in_mode.value
            new_in = (in_mode.value
                      & ~(self._ENABLE_PROCESSED_INPUT | self._ENABLE_LINE_INPUT
                          | self._ENABLE_ECHO_INPUT)
                      ) | self._ENABLE_VIRTUAL_TERMINAL_INPUT
            self._k.SetConsoleMode(self._hin, new_in)  # fail-soft
        out_mode = wt.DWORD()
        if self._k.GetConsoleMode(self._hout, c.byref(out_mode)):
            self._out_mode = out_mode.value
            new_out = (out_mode.value | self._ENABLE_PROCESSED_OUTPUT
                       | self._ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            self._k.SetConsoleMode(self._hout, new_out)  # fail-soft (older conhost)
        # UTF-8 codepages so ReadFile yields a UTF-8 VT byte stream matching the
        # remote PTY (output uses WriteConsoleW via sys.stdout.buffer, which is
        # codepage-independent, but keep them consistent).
        try:
            self._old_in_cp = self._k.GetConsoleCP()
            self._old_out_cp = self._k.GetConsoleOutputCP()
            self._k.SetConsoleCP(self._CP_UTF8)
            self._k.SetConsoleOutputCP(self._CP_UTF8)
        except Exception:
            pass

    def _stdin_loop(self) -> None:
        c = self._ctypes
        wt = self._wintypes
        buf = c.create_string_buffer(65536)
        nread = wt.DWORD(0)
        while not self._stop.is_set():
            ok = self._k.ReadFile(self._hin, buf, 65536, c.byref(nread), None)
            if not ok or nread.value == 0:
                self._handoff(b"")  # EOF / error → finish
                return
            self._handoff(buf.raw[:nread.value])

    def _handoff(self, data: bytes) -> None:
        # The thread can fire after the loop stops (it is abandoned mid-ReadFile on
        # detach), so guard against a closed loop.
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self._feed, data)

    def _feed(self, data: bytes) -> None:
        _feed_stdin(data, writer=self.writer, escape_armed=self.escape_armed,
                    finish=self.finish, exit_code=self.exit_code)

    async def _poll_size(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                sz = _term_size()
                if sz != self._last_size:
                    self._last_size = sz
                    try:
                        self.writer.write(
                            encode_json(T_RESIZE, {"rows": sz[0], "cols": sz[1]}))
                    except Exception:
                        return
        except asyncio.CancelledError:
            return

    def write_output(self, payload: bytes) -> None:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is None:
            return
        # Strip ConPTY's device-query sequences so the local console doesn't answer
        # them (the reply would inject into the CLI → cursor jumps / redraw jank).
        payload = _TERMINAL_QUERY_RE.sub(b"", payload)
        try:
            buf.write(payload)
            buf.flush()
        except (OSError, ValueError):
            pass

    def _on_signal(self, signum, frame) -> None:
        self._restore()
        with contextlib.suppress(Exception):
            self.loop.call_soon_threadsafe(self.finish, 130)

    def _restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        # Mode reset FIRST, while ENABLE_VIRTUAL_TERMINAL_PROCESSING is still
        # on — written after the console-mode restore it would print as
        # literal text on consoles whose original mode had VT off. See the
        # POSIX twin for why the reset is needed at all.
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            try:
                buf.write(TERMINAL_MODE_RESET)
                buf.flush()
            except (OSError, ValueError):
                pass
        try:
            if self._in_mode is not None:
                self._k.SetConsoleMode(self._hin, self._in_mode)
            if self._out_mode is not None:
                self._k.SetConsoleMode(self._hout, self._out_mode)
            if self._old_in_cp:
                self._k.SetConsoleCP(self._old_in_cp)
            if self._old_out_cp:
                self._k.SetConsoleOutputCP(self._old_out_cp)
        except Exception:
            pass

    def teardown(self) -> None:
        self._stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
        self._restore()
        # The stdin reader thread is a daemon blocked in ReadFile; it can't observe
        # _stop until the next keystroke, so it is intentionally abandoned — the
        # process exits cleanly anyway (daemon thread, console already restored).


async def _relay(reader, writer, *, cli: str = "", agent: str = "",
                 title: str = "") -> int:
    loop = asyncio.get_event_loop()
    exit_code = {"v": 0}
    # Exit disposition, printed AFTER the terminal is restored (see below):
    # ``reason`` non-empty when the session was TAKEN OVER (the dashboard
    # claimed it, or another otodock terminal attached); ``lost`` when the
    # control socket dropped (satellite restart/update, connection loss);
    # ``detached`` when THIS side left (Ctrl-] q / terminal EOF) — the session
    # keeps running. A clean child exit sets none of them and prints nothing.
    outcome = {"reason": "", "lost": False, "detached": False}
    done = loop.create_future()
    escape_armed = {"v": False}

    def _finish(code):
        if not done.done():
            exit_code["v"] = code if isinstance(code, int) else 1
            done.set_result(None)

    def _finish_local(code):
        outcome["detached"] = True
        _finish(code)

    term_cls = _WindowsTerminal if sys.platform == "win32" else _PosixTerminal
    term = term_cls(loop, writer, escape_armed, _finish_local, exit_code)
    term.setup()

    # Name the tab/window: with no title escape the emulator falls back to the
    # foreground process name — "python". The inner TUI may re-title later
    # (new chats do); on resume nothing else ever sets one.
    if title:
        term.write_output(b"\x1b]0;" + title.encode("utf-8", "replace") + b"\x07")

    # PTY → terminal (raw bytes); EXIT ends the session.
    async def _pump_output():
        while True:
            frame = await read_frame(reader)
            if frame is None:
                outcome["lost"] = True
                _finish(exit_code["v"])
                return
            ftype, payload = frame
            if ftype == T_OUTPUT:
                term.write_output(payload)
            elif ftype == T_EXIT:
                obj = json.loads(payload or b"{}") or {}
                code = obj.get("code", 0)
                outcome["reason"] = obj.get("reason", "") or ""
                _finish(int(code) if isinstance(code, int) else 1)
                return

    out_task = loop.create_task(_pump_output())
    try:
        await done
    finally:
        term.teardown()
        out_task.cancel()
        with contextlib.suppress(Exception):
            writer.close()

    # Printed only AFTER the terminal is restored (printing in raw mode / the
    # alt-screen buffer would be eaten on restore). Take-over reason wins;
    # a clean child exit prints nothing.
    if outcome["reason"]:
        print(f"\r\notodock: {outcome['reason']}", file=sys.stderr)
    elif outcome["lost"]:
        resume_cmd = f"otodock {cli}" + (f" {agent}" if agent else "") + " --resume"
        print(f"\r\notodock: disconnected — run `{resume_cmd}` to re-attach",
              file=sys.stderr)
    elif outcome["detached"]:
        print("\r\notodock: detached — the session keeps running",
              file=sys.stderr)

    return exit_code["v"]


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="otodock",
        description="Run an interactive Claude/Codex session as your otodock agent "
                    "in the current folder.",
    )
    parser.add_argument("cli", choices=sorted(_EXEC_PATHS.keys()),
                        help="which CLI to launch (claude | codex)")
    parser.add_argument("agent", nargs="?", default="",
                        help="agent slug (defaults to your configured agent)")
    parser.add_argument("--folder", default="",
                        help="working directory (default: current directory)")
    parser.add_argument("--model", default="", help="override the model")
    parser.add_argument("--mode", default="default",
                        help="permission mode (default | plan | acceptEdits | auto)")
    parser.add_argument("--resume", action="store_true",
                        help="pick a previous conversation to resume")
    args = parser.parse_args(argv)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("otodock: must be run in an interactive terminal", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
