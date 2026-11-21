"""A session's SSH agent endpoint on a Windows computer: a named pipe.

``ssh_agent`` owns the endpoint on every system (``start``, ``stop``, the
relay to the proxy); this module is its Windows transport, the counterpart
of the unix socket server there. Windows' own OpenSSH client opens the
path ``SSH_AUTH_SOCK`` names with ``CreateFileW`` (any pipe name of at most
107 bytes, its ``sun_path``), so a pipe per session serves ``ssh``,
``scp`` and ``sftp`` from ``System32\\OpenSSH``. Git for Windows' own ssh
reads only an emulated unix socket and is not served.

A pipe has no folder around it: its own access list is the fence.

- The access list names the satellite's own user alone, protected, with
  that user as the owner (``security_descriptor``). No other account opens
  the pipe or adds an instance to it.
- ``PIPE_REJECT_REMOTE_CLIENTS``: a client from another machine is refused.
- The first instance is created with ``FILE_FLAG_FIRST_PIPE_INSTANCE``, so
  a name another program holds fails the create, and every next instance
  is created while the previous one is still open: the name never has zero
  instances of ours while the pipe takes clients. A next create that fails
  ends the taking of clients (the clients in hand are still served), and
  the name is then free once the clients it still serves leave.
- The name is random per start (``pipe_name``). Windows lists pipe names
  to every local program, so it is no secret: the access list fences it.
- No handle is inheritable: no child of a session holds one.
- At the stop a sticky stop per server and per connection ends every
  operation, the clients are cut at once, and the thread that owns a handle
  is the only one that closes it. At a crash the system closes the handles,
  and the name goes with the last one: nothing is left on disk.

Two parts. ``PipeServer`` and ``PipeConnection`` are the loop and its
order; they compute every mode and the access list and hand them to the
thin layer, ``Win32Pipes``, the only code here that calls Windows. The
tests run the loop on a stand-in for that layer; ``Win32Pipes`` is built
on Windows alone (``api``), and this module is imported only there.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import secrets
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("satellite.ssh_agent")

PIPE_PREFIX = "\\\\.\\pipe\\oto-ssh-agent-"
#: Windows OpenSSH copies ``SSH_AUTH_SOCK`` into a 108-byte ``sun_path``.
MAX_PIPE_NAME_BYTES = 107

PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
BUFFER_BYTES = 64 * 1024

#: How long ``server_close`` waits for the client threads: one may still be
#: waiting on the proxy's answer, cut already and writing nothing.
_CLIENT_JOIN_S = 2.0
#: How long ``shutdown`` waits for the accept loop to end.
_SHUTDOWN_WAIT_S = 5.0


def pipe_name() -> str:
    """A fresh name: 128 random bits, nothing of the session's id (every
    local account can list pipe names)."""
    return PIPE_PREFIX + secrets.token_hex(16)


def open_mode(first: bool) -> int:
    """The open mode of an instance: duplex, overlapped, and the first one
    refuses a name that already has an instance."""
    mode = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED
    return mode | FILE_FLAG_FIRST_PIPE_INSTANCE if first else mode


def pipe_mode() -> int:
    """A byte stream, as Windows OpenSSH reads and writes it, and no client
    from another machine."""
    return PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS


def security_descriptor(sid: str) -> str:
    """The pipe's security descriptor as SDDL: owned by ``sid`` (the token's
    user, not its default owner, which an elevated token makes the
    Administrators group) and one protected allow entry for that user."""
    return f"O:{sid}D:P(A;;GA;;;{sid})"


class PipeConnection:
    """One client of the pipe: the calls the relay makes on a unix socket
    (``recv``, ``sendall``, ``shutdown``). ``shutdown`` may come from any
    thread; ``close`` comes from the thread that owns the connection, after
    its last operation ended."""

    def __init__(self, api: Any, handle: Any) -> None:
        self._api = api
        self._handle = handle
        self._stop = api.stopper()
        self._lock = threading.Lock()
        self._closed = False

    def recv(self, n: int) -> bytes:
        """Up to ``n`` bytes; ``b""`` at the end of the stream or the stop."""
        if self._stop.is_set():
            return b""
        return self._api.read(self._handle, n, self._stop)

    def sendall(self, data: bytes) -> None:
        """Every byte of ``data``, over as many writes as the pipe takes;
        ``BrokenPipeError`` once the stop is set or the client is gone."""
        view = memoryview(data)
        while view:
            if self._stop.is_set():
                raise BrokenPipeError("the agent endpoint was stopped")
            written = self._api.write(self._handle, bytes(view), self._stop)
            view = view[written:]

    def shutdown(self, how: int = 2) -> None:
        """Set the stop and cut the client: nothing is read or written on
        this connection after it, from any thread."""
        with self._lock:
            if self._closed:
                return
            self._stop.set()
            self._api.disconnect(self._handle)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            self._api.close(self._handle)
            self._stop.close()


class PipeServer:
    """The accept loop of one session's pipe, in the shape ``socketserver``
    gives the unix door (``serve_forever``, ``shutdown``, ``server_close``).
    The first instance is created here, so a held name fails before any
    thread starts."""

    def __init__(self, name: str, api: Any, on_client: Callable[[PipeConnection], None]) -> None:
        self.name = name
        self._api = api
        self._on_client = on_client
        self._sddl = security_descriptor(api.user_sid())
        self._stop = api.stopper()
        self._lock = threading.Lock()
        self._clients: list[tuple[threading.Thread, PipeConnection]] = []
        self._started = False
        self._closed = False
        self._done = threading.Event()
        try:
            self._listening: Any = api.create(name, open_mode(True), pipe_mode(), self._sddl)
        except BaseException:
            self._stop.close()
            raise

    def serve_forever(self, poll_interval: float | None = None) -> None:
        """Accept clients until the stop. Each connected instance goes to a
        thread of its own only after the next instance exists. A loop that
        starts after ``server_close`` returns at once: the close has let the
        instance and the stop go."""
        with self._lock:
            if self._closed:
                return
            self._started = True
        try:
            while not self._stop.is_set():
                handle = self._listening
                if not self._api.connect(handle, self._stop):
                    return
                try:
                    following = self._api.create(self.name, open_mode(False), pipe_mode(), self._sddl)
                except OSError as exc:
                    logger.warning("ssh-agent: %s: no next pipe instance, the pipe takes no new client: %s",
                                   self.name, exc)
                    following = None
                with self._lock:
                    self._listening = following
                self._hand_off(PipeConnection(self._api, handle))
                if following is None:
                    return
        finally:
            with self._lock:
                handle, self._listening = self._listening, None
            if handle is not None:
                self._api.close(handle)
            self._done.set()

    def shutdown(self) -> None:
        """Stop accepting and wait for the loop to end; a pending wait for a
        client ends at once."""
        self._stop.set()
        with self._lock:
            started = self._started
        if started and not self._done.wait(_SHUTDOWN_WAIT_S):
            logger.warning("ssh-agent: %s: the pipe's accept loop did not end in time", self.name)

    def server_close(self) -> None:
        """Cut every client, wait for their threads, and close the listening
        instance when no loop runs to close it."""
        self._stop.set()
        with self._lock:
            self._closed = True
            clients = list(self._clients)
            started = self._started
            handle = None if started else self._listening
            if not started:
                self._listening = None
        for _thread, conn in clients:
            conn.shutdown()
        if handle is not None:
            self._api.close(handle)
        waiting = False
        for thread, _conn in clients:
            thread.join(timeout=_CLIENT_JOIN_S)
            waiting = waiting or thread.is_alive()
        if waiting:
            logger.warning("ssh-agent: %s: a client of the pipe still waits on the proxy", self.name)
        # The stop is let go only when no loop can wait on it any more.
        if not started or self._done.is_set():
            self._stop.close()

    def _hand_off(self, conn: PipeConnection) -> None:
        thread = threading.Thread(target=self._serve_client, args=(conn,),
                                  name=f"ssh-agent-pipe:{self.name[-8:]}", daemon=True)
        with self._lock:
            self._clients.append((thread, conn))
            stopping = self._stop.is_set()
        if stopping:
            conn.shutdown()
        thread.start()

    def _serve_client(self, conn: PipeConnection) -> None:
        try:
            self._on_client(conn)
        finally:
            conn.close()
            with self._lock:
                self._clients = [(t, c) for t, c in self._clients if c is not conn]


@functools.cache
def api() -> Win32Pipes:
    """The thin layer, built once per process on Windows."""
    return Win32Pipes()


ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
SDDL_REVISION_1 = 1
#: The end of a client's stream, whichever way Windows says it.
_GONE = (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED)


class _Stopped(Exception):
    """An operation the stop cancelled."""


class _Win32Stop:
    """A manual-reset event: sticky once set, waited on with every
    operation of its server or connection."""

    def __init__(self, layer: Win32Pipes) -> None:
        self._layer = layer
        self.handle = layer.new_event()

    def set(self) -> None:
        self._layer.k32.SetEvent(self.handle)

    def is_set(self) -> bool:
        return bool(self._layer.k32.WaitForSingleObject(self.handle, 0) == WAIT_OBJECT_0)

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle:
            self._layer.k32.CloseHandle(handle)


class Win32Pipes:
    """Every call into Windows the pipe makes: ctypes on kernel32 and
    advapi32, each declared with its argument and result types, loaded with
    ``use_last_error``. Overlapped I/O, one event per operation: the wait is
    on that event and the stop together; a stop cancels the operation and
    waits for its final status before the OVERLAPPED, the event and the
    buffer are let go."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        self._ctypes = ctypes
        self._w = w

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                        ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", w.BOOL)]

        self.OVERLAPPED = OVERLAPPED
        self.SECURITY_ATTRIBUTES = SECURITY_ATTRIBUTES
        self.invalid = ctypes.c_void_p(-1).value
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        lp_ov = ctypes.POINTER(OVERLAPPED)
        lp_dword = ctypes.POINTER(w.DWORD)
        self._declare(k32.CreateNamedPipeW, w.HANDLE,
                      [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
                       ctypes.POINTER(SECURITY_ATTRIBUTES)])
        self._declare(k32.ConnectNamedPipe, w.BOOL, [w.HANDLE, lp_ov])
        self._declare(k32.DisconnectNamedPipe, w.BOOL, [w.HANDLE])
        self._declare(k32.CreateEventW, w.HANDLE, [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR])
        self._declare(k32.SetEvent, w.BOOL, [w.HANDLE])
        self._declare(k32.WaitForSingleObject, w.DWORD, [w.HANDLE, w.DWORD])
        self._declare(k32.WaitForMultipleObjects, w.DWORD,
                      [w.DWORD, ctypes.POINTER(w.HANDLE), w.BOOL, w.DWORD])
        self._declare(k32.ReadFile, w.BOOL, [w.HANDLE, ctypes.c_void_p, w.DWORD, lp_dword, lp_ov])
        self._declare(k32.WriteFile, w.BOOL, [w.HANDLE, ctypes.c_void_p, w.DWORD, lp_dword, lp_ov])
        self._declare(k32.GetOverlappedResult, w.BOOL, [w.HANDLE, lp_ov, lp_dword, w.BOOL])
        self._declare(k32.CancelIoEx, w.BOOL, [w.HANDLE, lp_ov])
        self._declare(k32.CloseHandle, w.BOOL, [w.HANDLE])
        self._declare(k32.GetCurrentProcess, w.HANDLE, [])
        self._declare(k32.LocalFree, ctypes.c_void_p, [ctypes.c_void_p])
        self._declare(adv.OpenProcessToken, w.BOOL, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)])
        self._declare(adv.GetTokenInformation, w.BOOL,
                      [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, lp_dword])
        self._declare(adv.ConvertSidToStringSidW, w.BOOL, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
        self._declare(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW, w.BOOL,
                      [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.ULONG)])
        self.k32 = k32
        self.adv = adv

    @staticmethod
    def _declare(fn: Any, restype: Any, argtypes: list[Any]) -> None:
        fn.restype = restype
        fn.argtypes = argtypes

    def _error(self, what: str, code: int | None = None) -> OSError:
        """The failure of ``what``: ``code``, or the last error, read here
        right after the call (any later call may set another)."""
        if code is None:
            code = self._ctypes.get_last_error()
        return OSError(None, f"{what}: {self._ctypes.FormatError(code).strip()}", None, code)  # type: ignore[attr-defined]

    def new_event(self) -> Any:
        handle = self.k32.CreateEventW(None, True, False, None)
        if not handle:
            raise self._error("CreateEventW")
        return handle

    def stopper(self) -> _Win32Stop:
        return _Win32Stop(self)

    def user_sid(self) -> str:
        """The process token's user, as a SID string."""
        ctypes, w = self._ctypes, self._w
        token = w.HANDLE()
        if not self.adv.OpenProcessToken(self.k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            raise self._error("OpenProcessToken")
        try:
            size = w.DWORD(0)
            self.adv.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
            if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not size.value:
                raise self._error("GetTokenInformation")
            buf = ctypes.create_string_buffer(size.value)
            if not self.adv.GetTokenInformation(token, TOKEN_USER_CLASS, buf, size, ctypes.byref(size)):
                raise self._error("GetTokenInformation")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the PSID.
            sid = ctypes.c_void_p.from_buffer(buf).value
            text = ctypes.c_void_p()
            if not self.adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise self._error("ConvertSidToStringSidW")
            try:
                return str(ctypes.wstring_at(text.value))
            finally:
                self.k32.LocalFree(text)
        finally:
            self.k32.CloseHandle(token)

    def create(self, name: str, mode: int, pmode: int, sddl: str) -> Any:
        """One instance of ``name`` with ``sddl`` as its security descriptor,
        never inheritable."""
        ctypes = self._ctypes
        descriptor = ctypes.c_void_p()
        if not self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None):
            raise self._error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            attributes = self.SECURITY_ATTRIBUTES(ctypes.sizeof(self.SECURITY_ATTRIBUTES), descriptor, False)
            handle = self.k32.CreateNamedPipeW(name, mode, pmode, PIPE_UNLIMITED_INSTANCES,
                                               BUFFER_BYTES, BUFFER_BYTES, 0, ctypes.byref(attributes))
            code = ctypes.get_last_error()
        finally:
            self.k32.LocalFree(descriptor)
        if handle is None or handle == self.invalid:
            raise self._error("CreateNamedPipeW", code)
        return handle

    def _final(self, handle: Any, overlapped: Any, stop: _Win32Stop) -> int:
        """Wait for a pending operation or the stop, then its final status:
        the bytes it moved. ``_Stopped`` when the stop cancelled it."""
        ctypes, w = self._ctypes, self._w
        handles = (w.HANDLE * 2)(overlapped.hEvent, stop.handle)
        if self.k32.WaitForMultipleObjects(2, handles, False, INFINITE) != WAIT_OBJECT_0:
            self.k32.CancelIoEx(handle, ctypes.byref(overlapped))
        moved = w.DWORD(0)
        if self.k32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(moved), True):
            return int(moved.value)
        code = ctypes.get_last_error()
        if code == ERROR_OPERATION_ABORTED:
            raise _Stopped
        raise OSError(None, "the pipe operation failed", None, code)

    def _operation(self) -> Any:
        overlapped = self.OVERLAPPED()
        overlapped.hEvent = self.new_event()
        return overlapped

    def connect(self, handle: Any, stop: _Win32Stop) -> bool:
        """Wait for a client on the instance; False when the stop ended the
        wait. A client that came before the call, or came and left, is a
        connection too (its first read ends the stream)."""
        ctypes = self._ctypes
        overlapped = self._operation()
        try:
            if self.k32.ConnectNamedPipe(handle, ctypes.byref(overlapped)):
                return True
            code = ctypes.get_last_error()
            if code in (ERROR_PIPE_CONNECTED, ERROR_NO_DATA):
                return True
            if code != ERROR_IO_PENDING:
                raise OSError(None, "ConnectNamedPipe failed", None, code)
            try:
                self._final(handle, overlapped, stop)
            except _Stopped:
                return False
            return True
        finally:
            self.k32.CloseHandle(overlapped.hEvent)

    def read(self, handle: Any, n: int, stop: _Win32Stop) -> bytes:
        """Up to ``n`` bytes; ``b""`` at the end of the stream or the stop."""
        ctypes = self._ctypes
        buf = ctypes.create_string_buffer(n)
        overlapped = self._operation()
        try:
            if self.k32.ReadFile(handle, buf, n, None, ctypes.byref(overlapped)):
                moved = self._w.DWORD(0)
                self.k32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(moved), False)
                return buf.raw[:moved.value]
            code = ctypes.get_last_error()
            if code in _GONE:
                return b""
            if code != ERROR_IO_PENDING:
                raise OSError(None, "ReadFile failed", None, code)
            try:
                return buf.raw[:self._final(handle, overlapped, stop)]
            except _Stopped:
                return b""
            except OSError as exc:
                if exc.winerror in _GONE:  # type: ignore[attr-defined]
                    return b""
                raise
        finally:
            self.k32.CloseHandle(overlapped.hEvent)

    def write(self, handle: Any, data: bytes, stop: _Win32Stop) -> int:
        """Write ``data``; the bytes written. ``BrokenPipeError`` when the
        client is gone or the stop ended the write."""
        ctypes = self._ctypes
        buf = ctypes.create_string_buffer(data, len(data))
        overlapped = self._operation()
        try:
            if self.k32.WriteFile(handle, buf, len(data), None, ctypes.byref(overlapped)):
                moved = self._w.DWORD(0)
                self.k32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(moved), False)
                return int(moved.value)
            code = ctypes.get_last_error()
            if code in _GONE:
                raise BrokenPipeError("the client is gone")
            if code != ERROR_IO_PENDING:
                raise OSError(None, "WriteFile failed", None, code)
            try:
                return self._final(handle, overlapped, stop)
            except _Stopped:
                raise BrokenPipeError("the agent endpoint was stopped") from None
            except OSError as exc:
                if exc.winerror in _GONE:  # type: ignore[attr-defined]
                    raise BrokenPipeError("the client is gone") from None
                raise
        finally:
            self.k32.CloseHandle(overlapped.hEvent)

    def disconnect(self, handle: Any) -> None:
        with contextlib.suppress(OSError):
            self.k32.DisconnectNamedPipe(handle)

    def close(self, handle: Any) -> None:
        with contextlib.suppress(OSError):
            self.k32.CloseHandle(handle)
