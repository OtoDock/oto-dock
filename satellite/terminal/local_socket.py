"""otodock-CLI — the satellite's local control socket.

A user runs ``otodock claude|codex`` ON this machine; the thin client connects to
this AF_UNIX socket (a Windows build would use a named pipe) and asks for an interactive
session AS the machine's otodock agent. We:

  1. Accept only the OS user that runs the satellite — the socket lives ``0600`` in
     a ``0700`` per-user dir, and on Linux we additionally verify the peer's
     ``SO_PEERCRED`` uid == our uid.
  2. Read the OPEN frame and ask the proxy to open the session
     (``ws_client.request_local_session`` — identity is re-derived proxy-side from
     the MACHINE owner; the frame's fields are untrusted input).
  3. On success, attach to the spawned PTY (``session_manager``) and relay bytes
     both ways until the client detaches or the CLI exits.

Terminal bytes stay 100% local (this socket ↔ the PTY on the same host); only the
transcript flows to the platform via the normal forwarding.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import struct
import sys
from pathlib import Path

from .otodock_proto import (
    read_frame, encode, encode_json,
    T_OPEN, T_INPUT, T_RESIZE, T_DETACH, T_LIST,
    T_OPENED, T_ERROR, T_OUTPUT, T_EXIT, T_LISTED,
)

logger = logging.getLogger("satellite")

# Bounded output buffer per connection (frames). Local + fast, so overflow only
# happens if the terminal client truly wedges — then we drop with a log rather
# than stall the PTY read (which would also stall the proxy mirror).
_OUT_QUEUE_MAX = 4096


def socket_path() -> Path:
    """The local control socket path — **deterministic and identical for the daemon
    and the client**. Keyed on ``otodock_dir()`` (``~/.oto-dock`` on Unix,
    ``~/OtoDock`` on Windows), NOT ``$XDG_RUNTIME_DIR``: that var differs between a
    systemd service and an interactive shell, which would put the daemon's socket
    and the client's lookup in different places. The ``run/`` dir is locked to
    ``0700`` (owner-only) — never a world-writable dir like ``/tmp``."""
    from ..config import otodock_dir
    d = otodock_dir() / "run"
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d / "otodock.sock"


def named_pipe_path() -> str:
    """The Windows named-pipe equivalent of :func:`socket_path` — **deterministic
    and identical for the daemon and the client**. Keyed on ``otodock_dir()`` so
    it's per-install unique; the SHA-256 hex is lowercase + stable, and pipe names
    are case-insensitive and capped at 256 chars (we use ~25). We hash the plain
    ``otodock_dir()`` string (not ``.resolve()``) to avoid drive-letter/8.3
    casing nondeterminism between a service and an interactive shell."""
    import hashlib
    from ..config import otodock_dir
    h = hashlib.sha256(str(otodock_dir()).lower().encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\otodock-{h}"


def set_pipe_byte_mode(endpoint) -> None:
    """Switch a Proactor named-pipe to **byte** read mode (Windows only).

    The stdlib's ``start_serving_pipe``/``create_pipe_connection`` create pipes in
    MESSAGE mode (``PIPE_READMODE_MESSAGE``), where a ``ReadFile`` smaller than the
    pending message fails with ``ERROR_MORE_DATA`` and the Proactor tears the
    transport down. But our framing is a byte stream (``read_frame`` uses
    ``readexactly``) and a single ``T_OUTPUT`` frame can exceed the transport's
    64 KiB read buffer (PTY chunks are 64 KiB). Byte mode makes ``ReadFile`` return
    whatever is available — exactly the AF_UNIX byte-stream semantics. The first
    frame each way is small (OPEN/OPENED), so flipping right after connect/accept,
    while a message-mode read may already be pending, is safe.

    ``endpoint`` is anything with ``get_extra_info('pipe')`` (a ``StreamWriter`` on
    the server, the transport on the client). Best-effort: the default pipe mode
    only bites on large frames, so a failure here is logged, not fatal."""
    try:
        import ctypes.wintypes
        pipe = endpoint.get_extra_info("pipe")
        if pipe is None:
            return
        handle = pipe.fileno()
        PIPE_READMODE_BYTE = 0x00000000  # | PIPE_WAIT (0x0)
        mode = ctypes.wintypes.DWORD(PIPE_READMODE_BYTE)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetNamedPipeHandleState.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(ctypes.wintypes.DWORD),
            ctypes.POINTER(ctypes.wintypes.DWORD),
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        kernel32.SetNamedPipeHandleState.restype = ctypes.wintypes.BOOL
        kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)
    except Exception:
        logger.debug("otodock pipe byte-mode switch skipped", exc_info=True)


def _pipe_peer_sid_verdict(handle, *, server_pid: bool) -> "bool | None":
    """Compare the pipe PEER's token-user SID with ours.

    ``server_pid=False`` resolves the CLIENT's pid (the daemon vetting a
    connecting terminal via ``GetNamedPipeClientProcessId``);
    ``server_pid=True`` resolves the SERVER's pid (the ``otodock`` client
    vetting who actually serves the deterministic pipe name — pipe names are
    first-come, so a local squatter could pre-create it, impersonate the
    satellite, and harvest every keystroke typed into the "AI session").

    Returns ``True`` (same user), ``False`` (POSITIVE mismatch), or ``None``
    (could not determine — an API failure). Callers pick the failure posture:
    the server stays fail-open on ``None`` (the default pipe DACL is its
    backstop), the client MUST fail closed (a squatter can deliberately make
    its own process unopenable to force the inconclusive path). Explicit
    ``argtypes``/``restype`` are mandatory: pointer-sized HANDLEs truncate to
    a C ``int`` otherwise."""
    try:
        import ctypes.wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        kernel32.GetNamedPipeClientProcessId.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
        kernel32.GetNamedPipeClientProcessId.restype = ctypes.wintypes.BOOL
        kernel32.GetNamedPipeServerProcessId.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
        kernel32.GetNamedPipeServerProcessId.restype = ctypes.wintypes.BOOL
        kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL
        advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi32.EqualSid.restype = ctypes.wintypes.BOOL

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        TOKEN_QUERY = 0x0008
        TokenUser = 1

        def _token_user(proc_handle):
            """Return (psid_ptr, backing_buffer) for proc_handle's token user, or
            None. The PSID points INTO backing_buffer, which must outlive EqualSid."""
            tok = ctypes.wintypes.HANDLE()
            if not advapi32.OpenProcessToken(proc_handle, TOKEN_QUERY, ctypes.byref(tok)):
                return None
            try:
                size = ctypes.wintypes.DWORD(0)
                # First call is EXPECTED to fail with ERROR_INSUFFICIENT_BUFFER,
                # returning the needed size — not a real error.
                advapi32.GetTokenInformation(tok, TokenUser, None, 0, ctypes.byref(size))
                if size.value == 0:
                    return None
                buf = ctypes.create_string_buffer(size.value)
                if not advapi32.GetTokenInformation(
                        tok, TokenUser, buf, size, ctypes.byref(size)):
                    return None
                # TOKEN_USER { SID_AND_ATTRIBUTES User } — the first pointer-sized
                # field is the PSID.
                psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                return psid, buf
            finally:
                kernel32.CloseHandle(tok)

        pid = ctypes.wintypes.DWORD(0)
        resolve_pid = (
            kernel32.GetNamedPipeServerProcessId if server_pid
            else kernel32.GetNamedPipeClientProcessId
        )
        if not resolve_pid(handle, ctypes.byref(pid)):
            return None

        peer_proc = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not peer_proc:
            return None
        try:
            peer = _token_user(peer_proc)
        finally:
            kernel32.CloseHandle(peer_proc)
        if peer is None:
            return None
        peer_sid, _peer_buf = peer

        # GetCurrentProcess() is a pseudo-handle (-1) — must NOT be closed.
        ours = _token_user(kernel32.GetCurrentProcess())
        if ours is None:
            return None
        our_sid, _our_buf = ours

        return bool(advapi32.EqualSid(peer_sid, our_sid))
    except Exception:
        logger.debug("otodock pipe peer check inconclusive", exc_info=True)
        return None


def _check_peer_windows(writer: "asyncio.StreamWriter") -> bool:
    """SO_PEERCRED analog for the daemon side: the CONNECTING process's
    token-user SID must equal ours. Fail-open on an inconclusive check — the
    default pipe DACL already excludes other standard users, so this is
    defense-in-depth; reject only on a *positive* SID mismatch."""
    pipe = writer.get_extra_info("pipe")
    if pipe is None:
        return True
    return _pipe_peer_sid_verdict(pipe.fileno(), server_pid=False) is not False


def verify_pipe_server_identity(transport) -> bool:
    """Client-side counterpart: ``True`` only when the process SERVING the
    pipe provably runs as this user. FAIL-CLOSED on an inconclusive check —
    the pipe name is deterministic and first-come, and a squatting server can
    deliberately make itself unopenable, so "couldn't verify" must read as
    "not the satellite"."""
    try:
        pipe = transport.get_extra_info("pipe")
        if pipe is None:
            return False
        return _pipe_peer_sid_verdict(pipe.fileno(), server_pid=True) is True
    except Exception:
        logger.debug("otodock pipe server-identity check failed", exc_info=True)
        return False


class _LocalConn:
    """One connected `otodock` terminal: an output queue fed by the PTY tee, drained
    to the socket by a writer task; input/resize forwarded to the PTY session."""

    def __init__(self, session_id: str, writer: "asyncio.StreamWriter"):
        self.session_id = session_id
        self._writer = writer
        self._out_q: asyncio.Queue = asyncio.Queue(maxsize=_OUT_QUEUE_MAX)
        self._session = None
        self._exited = False
        self._closed = False
        self._writer_task: "asyncio.Task | None" = None

    # --- wired by session_manager._wire_local_conn ---
    def bind_session(self, session) -> None:
        self._session = session

    def write_input(self, data: bytes) -> None:
        s = self._session
        if s is not None:
            try:
                # Local-typing line state for the server-prompt injection gates
                # — THIS path only (proxy pty_input writes aren't local typing).
                s.note_local_input(data)
                s.write(data)
            except Exception:
                logger.debug("otodock input write failed", exc_info=True)

    def resize(self, rows: int, cols: int) -> None:
        s = self._session
        if s is not None:
            try:
                s.resize(rows, cols)
            except Exception:
                logger.debug("otodock resize failed", exc_info=True)

    # --- PTY tee (called from BasePtySession._on_output / _on_exit, same loop) ---
    def feed_output(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            self._out_q.put_nowait(("out", data))
        except asyncio.QueueFull:
            logger.warning(
                "otodock local terminal buffer full — dropping %d bytes", len(data)
            )

    def feed_exit(self, code, reason: str = "") -> None:
        # ``reason`` (dual-control): non-empty ONLY on a TAKE-OVER (the
        # dashboard claimed the session, or another otodock terminal attached) —
        # the client prints it before returning to the shell. Empty for a normal
        # CLI exit (the child died) so a clean quit prints nothing.
        if self._exited or self._closed:
            return
        self._exited = True
        try:
            self._out_q.put_nowait(("exit", (code, reason)))
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._out_q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._out_q.put_nowait(("exit", (code, reason)))

    def start_writer(self) -> None:
        self._writer_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                kind, payload = await self._out_q.get()
                if kind == "out":
                    self._writer.write(encode(T_OUTPUT, payload))
                    await self._writer.drain()
                else:  # exit — payload is (code, reason)
                    code, reason = payload if isinstance(payload, tuple) else (payload, "")
                    code = code if isinstance(code, int) else 1
                    self._writer.write(encode_json(T_EXIT, {"code": code, "reason": reason or ""}))
                    await self._writer.drain()
                    return
        except (ConnectionError, asyncio.CancelledError, BrokenPipeError):
            return
        except Exception:
            logger.debug("otodock conn drain error", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        if self._writer_task is not None:
            self._writer_task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()


class LocalControlServer:
    """Listens on the local control socket and brokers `otodock` sessions."""

    def __init__(self, ws_client, session_manager):
        self.ws = ws_client
        self.sm = session_manager
        self._server: "asyncio.AbstractServer | None" = None
        self._path: "Path | None" = None
        # Windows: the list of PipeServers from start_serving_pipe (Unix: stays None).
        self._pipe_servers: "list | None" = None

    async def start(self) -> None:
        if sys.platform == "win32":
            await self._start_windows()
            return
        self._path = socket_path()
        # Stale-socket handling: if a live satellite is already accepting, refuse
        # (don't steal its socket); else the file is stale → unlink and rebind.
        if self._path.exists():
            if await self._probe_live(self._path):
                logger.warning(
                    "otodock local control socket already in use (another "
                    "satellite instance?) — not starting a second listener"
                )
                self._path = None
                return
            with contextlib.suppress(OSError):
                self._path.unlink()
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_conn, path=str(self._path)
            )
            os.chmod(self._path, 0o600)
        except Exception:
            logger.exception("otodock local control socket failed to start")
            self._server = None
            self._path = None
            return
        logger.info("otodock local control socket listening at %s", self._path)

    async def _start_windows(self) -> None:
        """Windows: serve on a named pipe via the Proactor loop (the satellite runs
        the default ProactorEventLoop). The per-connection ``proto_factory`` mints a
        FRESH StreamReader + StreamReaderProtocol so concurrent `otodock` sessions
        can't cross-wire; the ``client_connected_cb`` hands the adapted
        ``(reader, writer)`` to the transport-agnostic ``_handle_conn`` unchanged."""
        pipe = named_pipe_path()
        # Single-instance guard, mirroring the AF_UNIX refuse-on-live: if another
        # satellite already answers, don't start a second listener.
        if await self._probe_live_windows(pipe):
            logger.warning(
                "otodock local control pipe already in use (another satellite "
                "instance?) — not starting a second listener"
            )
            return
        try:
            loop = asyncio.get_event_loop()

            def proto_factory():
                reader = asyncio.StreamReader(loop=loop)
                return asyncio.StreamReaderProtocol(
                    reader,
                    lambda r, w: loop.create_task(self._handle_conn(r, w)),
                    loop=loop,
                )

            self._pipe_servers = await loop.start_serving_pipe(proto_factory, pipe)
        except Exception:
            logger.exception("otodock local control pipe failed to start")
            self._pipe_servers = None
            return
        logger.info("otodock local control pipe listening at %s", pipe)

    @staticmethod
    async def _probe_live_windows(pipe: str) -> bool:
        """True if a satellite is already serving ``pipe``. Bounded by ``wait_for``
        because ``connect_pipe`` retries ``ERROR_PIPE_BUSY`` forever — a busy/wedged
        peer must never block our boot (treat a timeout as 'assume live, refuse')."""
        loop = asyncio.get_event_loop()

        async def _try() -> bool:
            transport, _ = await loop.create_pipe_connection(asyncio.Protocol, pipe)
            transport.close()
            return True

        try:
            return await asyncio.wait_for(_try(), timeout=1.0)
        except asyncio.TimeoutError:
            return True  # busy peer → assume live, don't block startup
        except (FileNotFoundError, OSError):
            return False  # no pipe → not live

    async def stop(self) -> None:
        if self._pipe_servers is not None:
            # PipeServer is a plain object (no wait_closed); close() cancels the
            # accept future + closes free instances. In-flight _handle_conn tasks
            # own their connected pipes and finish on their own (same as AF_UNIX).
            for s in self._pipe_servers:
                with contextlib.suppress(Exception):
                    s.close()
            self._pipe_servers = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self._path is not None:
            with contextlib.suppress(OSError):
                self._path.unlink()
            self._path = None

    @staticmethod
    async def _probe_live(path: Path) -> bool:
        try:
            _, w = await asyncio.open_unix_connection(str(path))
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()
            return True
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            return False

    def _check_peer(self, writer: "asyncio.StreamWriter") -> bool:
        """Defense-in-depth on top of the 0600/0700 file perms: on Linux verify the
        connecting process's uid == ours via SO_PEERCRED, on Windows the peer's
        token-user SID == ours via the named-pipe client PID. Best-effort elsewhere."""
        if sys.platform == "win32":
            return _check_peer_windows(writer)
        sock = writer.get_extra_info("socket")
        if sock is None or not sys.platform.startswith("linux"):
            return True  # rely on the socket-file permissions
        try:
            creds = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", creds)
            return uid == os.getuid()
        except Exception:
            return True

    async def _handle_conn(
        self, reader: "asyncio.StreamReader", writer: "asyncio.StreamWriter"
    ) -> None:
        session_id = ""
        conn: "_LocalConn | None" = None
        try:
            if sys.platform == "win32":
                # Match the AF_UNIX byte stream: stdlib pipes are message-mode,
                # which would tear down on the first >64 KiB output frame.
                set_pipe_byte_mode(writer)
            if not self._check_peer(writer):
                logger.warning("otodock local socket: rejected peer (identity mismatch)")
                return
            frame = await read_frame(reader)
            if frame is None:
                return
            ftype, payload = frame
            if ftype not in (T_OPEN, T_LIST):
                await self._reply_error(writer, "expected an OPEN or LIST frame")
                return
            try:
                args = json.loads(payload or b"{}")
            except Exception:
                await self._reply_error(writer, "malformed request payload")
                return

            # --resume: a one-shot list request/response (no session).
            if ftype == T_LIST:
                try:
                    resp = await self.ws.request_local_list(args)
                except asyncio.TimeoutError:
                    await self._reply_error(writer, "the platform did not respond")
                    return
                if resp.get("type") == "local_session_error":
                    await self._reply_error(writer, resp.get("reason") or "list failed")
                    return
                writer.write(encode_json(T_LISTED, {"chats": resp.get("chats", [])}))
                await writer.drain()
                return

            try:
                resp = await self.ws.request_local_session(args)
            except asyncio.TimeoutError:
                await self._reply_error(
                    writer, "the platform did not respond — please try again"
                )
                return
            except Exception:
                logger.exception("otodock request_local_session failed")
                await self._reply_error(
                    writer, "internal error contacting the platform"
                )
                return

            if resp.get("type") == "local_session_error":
                await self._reply_error(
                    writer, resp.get("reason") or "could not open the session"
                )
                return
            session_id = resp.get("session_id") or ""
            chat_id = resp.get("chat_id") or ""
            # Dual-control: True = the proxy wants us to ATTACH to an
            # already-live PTY (no pty_open is coming). If it raced away (satellite
            # restart / reap / supersede between the proxy's lock-checked decision
            # and now), the PTY is absent here → wiring would silently HANG the
            # terminal. Error the client cleanly instead, AND tell the proxy the
            # attach failed so it clears otodock_attached + un-gates the dashboard
            # (the proxy already marked it + evicted the dashboard viewer).
            attach = bool(resp.get("attach"))
            if not session_id:
                await self._reply_error(writer, "the platform returned no session")
                return
            if attach and session_id not in self.sm.pty_sessions:
                try:
                    await self.ws.send_local_detached(session_id)
                except Exception as e:
                    logger.debug("send_local_detached (attach-miss): %s", e)
                await self._reply_error(
                    writer, "that session is no longer running — run `otodock --resume` again"
                )
                return

            # OPENED first; thereafter the conn's writer task is the sole socket
            # writer (no concurrent writes with the reader loop below).
            writer.write(encode_json(T_OPENED, {"session_id": session_id, "chat_id": chat_id}))
            await writer.drain()
            conn = _LocalConn(session_id, writer)
            conn.start_writer()
            self.sm.attach_local_conn(session_id, conn, attach=attach)

            # Reader loop: client → PTY (input / resize / graceful detach).
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    break  # client disconnected
                ftype, payload = frame
                if ftype == T_INPUT:
                    conn.write_input(payload)
                elif ftype == T_RESIZE:
                    try:
                        obj = json.loads(payload or b"{}")
                        conn.resize(int(obj.get("rows", 24)), int(obj.get("cols", 80)))
                    except Exception:
                        pass
                elif ftype == T_DETACH:
                    break
                # unknown frame types ignored (forward-compat)
        except Exception:
            logger.debug("otodock local conn error", exc_info=True)
        finally:
            if conn is not None:
                await conn.close()
                # Orphan lifecycle: the terminal is gone, but the PTY + the
                # proxy mirror keep running — the chat stays dashboard-resumable.
                # CONN-IDENTITY guard (dual-control): detach_local_conn returns
                # False when THIS conn was already SUPERSEDED (a newer otodock
                # terminal attached, or the dashboard kicked it) — then we must NOT
                # detach the new controller's relay NOR send local_session_detached
                # (which would clear the new controller's otodock_attached). Only the
                # conn that is still the live one tears down + notifies the proxy.
                if self.sm.detach_local_conn(session_id, conn):
                    try:
                        await self.ws.send_local_detached(session_id)
                    except Exception as e:
                        logger.debug("send_local_detached (conn teardown): %s", e)
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _reply_error(writer: "asyncio.StreamWriter", reason: str) -> None:
        try:
            writer.write(encode_json(T_ERROR, {"reason": reason}))
            await writer.drain()
        except Exception:
            pass
