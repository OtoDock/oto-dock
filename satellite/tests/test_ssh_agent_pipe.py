"""Tests for satellite/sessions/ssh_agent_pipe.py: the Windows transport of
a session's SSH agent endpoint.

The loop and its order run here on a stand-in for the thin Windows layer
(``pipe_standin``). The tests at the end call Windows itself and run on a
Windows computer only; the door's tests are in test_ssh_agent.py.
"""

import contextlib
import re
import sys
import threading
import time

import pytest

from satellite.sessions import ssh_agent_pipe as pipe
from satellite.tests.pipe_standin import OTHER_USER, USER, StandinPipes

NAME = pipe.PIPE_PREFIX + "0" * 32


def _echo(conn):
    while True:
        data = conn.recv(5)
        if not data:
            return
        conn.sendall(data)


@contextlib.contextmanager
def _serving(pipes, on_client=_echo, name=NAME):
    server = pipe.PipeServer(name, pipes, on_client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the condition never held")


# ---------------------------------------------------------------------------
# what the pipe is created with
# ---------------------------------------------------------------------------

def test_the_name_is_random_and_fits_windows_opensshs_path():
    names = {pipe.pipe_name() for _ in range(50)}
    assert len(names) == 50
    for name in names:
        assert re.fullmatch(r"\\\\\.\\pipe\\oto-ssh-agent-[0-9a-f]{32}", name)
        assert len(name.encode()) <= pipe.MAX_PIPE_NAME_BYTES


def test_every_instance_is_the_users_alone_bytes_and_local_clients_only():
    pipes = StandinPipes()
    with _serving(pipes):
        for _ in range(3):
            client = pipes.open_client(NAME)
            client.sendall(b"hello")
            assert client.recv(5) == b"hello"
            client.close()
        _wait(lambda: len(pipes.creates) >= 4)
    sddl = f"O:{USER}D:P(A;;GA;;;{USER})"
    modes = [(mode, pmode, text) for _name, mode, pmode, text in pipes.creates]
    first, *rest = modes
    assert first == (pipe.PIPE_ACCESS_DUPLEX | pipe.FILE_FLAG_OVERLAPPED | pipe.FILE_FLAG_FIRST_PIPE_INSTANCE,
                     pipe.PIPE_REJECT_REMOTE_CLIENTS, sddl)
    assert rest and all(m == (pipe.PIPE_ACCESS_DUPLEX | pipe.FILE_FLAG_OVERLAPPED,
                              pipe.PIPE_REJECT_REMOTE_CLIENTS, sddl) for m in rest)


def test_another_account_and_another_machine_are_refused():
    pipes = StandinPipes()
    with _serving(pipes):
        with pytest.raises(PermissionError):
            pipes.open_client(NAME, user=OTHER_USER)
        with pytest.raises(PermissionError):
            pipes.open_client(NAME, remote=True)
        client = pipes.open_client(NAME)
        client.sendall(b"ok")
        assert client.recv(2) == b"ok"
        client.close()


def test_a_name_another_program_holds_fails_the_first_create():
    pipes = StandinPipes()
    held = pipes.create(NAME, pipe.open_mode(True), pipe.pipe_mode(), "O:S-1-5-21-9D:P(A;;GA;;;WD)")
    with pytest.raises(PermissionError):
        pipe.PipeServer(NAME, pipes, _echo)
    # The other program's pipe is untouched, and the refused server holds nothing.
    assert pipes.open_instances(NAME) == 1 and not held.closed
    assert all(stop.closed for stop in pipes.stops)


# ---------------------------------------------------------------------------
# the order of the instances
# ---------------------------------------------------------------------------

def test_the_name_never_has_zero_instances_while_it_is_served():
    """Each client leaves at once and the next create is slow: only a next
    instance made before the hand-off keeps the name held throughout."""
    pipes = StandinPipes()
    real_create = pipes.create

    def slow_next(name, mode, pmode, sddl):
        if not mode & pipe.FILE_FLAG_FIRST_PIPE_INSTANCE:
            time.sleep(0.05)
        return real_create(name, mode, pmode, sddl)

    pipes.create = slow_next
    with _serving(pipes):
        for _ in range(5):
            pipes.wait_listening(NAME)
            pipes.open_client(NAME).close()
        _wait(lambda: len(pipes.creates) >= 6)
        served = list(pipes.counts[NAME])
    assert served[0] == 1 and min(served) >= 1, served
    assert pipes.open_instances(NAME) == 0 and not pipes.double_closes


def test_a_client_that_came_before_the_wait_is_served():
    pipes = StandinPipes()
    server = pipe.PipeServer(NAME, pipes, _echo)
    client = pipes.open_client(NAME)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client.sendall(b"early")
        assert client.recv(5) == b"early"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_failed_next_create_takes_no_new_client_and_serves_the_one_it_has(caplog):
    pipes = StandinPipes()
    pipes.fail_next_create = False
    with _serving(pipes) as server:
        pipes.fail_next_create = True
        with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
            client = pipes.open_client(NAME)
            client.sendall(b"still")
            assert client.recv(5) == b"still"
        _wait(lambda: "no next pipe instance" in caplog.text)
        with pytest.raises(OSError):
            pipes.open_client(NAME)
        client.close()
        assert server.name == NAME


def test_a_partial_write_is_completed():
    pipes = StandinPipes()
    pipes.write_limit = 2
    with _serving(pipes):
        client = pipes.open_client(NAME)
        client.sendall(b"hello")
        got = b""
        while len(got) < 5:
            got += client.recv(5)
        assert got == b"hello"
        client.close()


# ---------------------------------------------------------------------------
# the stop
# ---------------------------------------------------------------------------

def test_the_stop_cuts_every_client_closes_every_instance_and_frees_the_name():
    pipes = StandinPipes()
    with _serving(pipes):
        client = pipes.open_client(NAME)
        client.sendall(b"hello")
        assert client.recv(5) == b"hello"
        pipes.wait_listening(NAME)
    assert client.recv(1) == b""
    client.close()
    assert pipes.open_instances(NAME) == 0 and NAME not in pipes.names
    with pytest.raises(FileNotFoundError):
        pipes.open_client(NAME)
    assert all(stop.closed for stop in pipes.stops) and not pipes.double_closes


def test_a_stop_set_before_the_wait_ends_it():
    pipes = StandinPipes()
    server = pipe.PipeServer(NAME, pipes, _echo)
    server.shutdown()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    server.server_close()
    assert pipes.open_instances(NAME) == 0 and not pipes.double_closes


def test_a_loop_that_starts_after_the_close_touches_nothing():
    pipes = StandinPipes()
    server = pipe.PipeServer(NAME, pipes, _echo)
    server.shutdown()
    server.server_close()
    calls = []
    pipes.connect = lambda *a: calls.append("connect") or False
    stop = pipes.stops[0]
    stop.is_set = lambda: calls.append("is_set") or True
    server.serve_forever()
    assert calls == [] and pipes.open_instances(NAME) == 0 and not pipes.double_closes


def test_a_loop_that_outlives_the_shutdown_keeps_its_stop(monkeypatch):
    """A loop that still runs when the close comes keeps the server's stop:
    the close lets it go only when no loop can read it any more."""
    pipes = StandinPipes()
    monkeypatch.setattr(pipe, "_SHUTDOWN_WAIT_S", 0.05)
    entered, release = threading.Event(), threading.Event()
    real_connect = pipes.connect

    def held_connect(instance, stop):
        connected = real_connect(instance, stop)
        entered.set()
        release.wait(5)
        return connected

    pipes.connect = held_connect
    server = pipe.PipeServer(NAME, pipes, _echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.shutdown()
    assert entered.wait(5)
    server.server_close()
    assert not pipes.stops[0].closed
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert pipes.open_instances(NAME) == 0 and not pipes.double_closes


def test_a_server_that_never_served_closes_its_first_instance():
    pipes = StandinPipes()
    server = pipe.PipeServer(NAME, pipes, _echo)
    server.shutdown()
    server.server_close()
    assert pipes.open_instances(NAME) == 0 and not pipes.double_closes
    assert all(stop.closed for stop in pipes.stops)


def test_a_connection_after_its_shutdown_reads_and_writes_nothing():
    pipes = StandinPipes()
    calls = []
    pipes.read = lambda *a: calls.append("read") or b"x"
    pipes.write = lambda *a: calls.append("write") or 1
    pipes.disconnect = lambda *a: calls.append("disconnect")
    instance = pipes.create(NAME, pipe.open_mode(True), pipe.pipe_mode(), "")
    conn = pipe.PipeConnection(pipes, instance)
    conn.shutdown()
    assert conn.recv(4) == b""
    with pytest.raises(BrokenPipeError):
        conn.sendall(b"answer")
    assert calls == ["disconnect"]
    conn.close()
    conn.shutdown()
    conn.close()
    assert calls == ["disconnect"] and not pipes.double_closes


def test_a_client_thread_waiting_on_its_answer_writes_nothing_after_the_stop():
    pipes = StandinPipes()
    asked, release, answered = threading.Event(), threading.Event(), []

    def slow(conn):
        conn.recv(5)
        asked.set()
        release.wait(10)
        try:
            conn.sendall(b"late!")
        except OSError:
            return
        answered.append(True)

    server = pipe.PipeServer(NAME, pipes, slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = pipes.open_client(NAME)
    client.sendall(b"hello")
    assert asked.wait(5)
    server.shutdown()
    closer = threading.Thread(target=server.server_close)
    closer.start()
    # Cut at once, while its thread still waits on the answer: well before
    # that thread could end on its own and close the instance.
    client.settimeout(1.0)
    assert client.recv(5) == b""
    assert not release.is_set()
    release.set()
    closer.join(timeout=5)
    thread.join(timeout=5)
    assert answered == []
    client.close()
    assert pipes.open_instances(NAME) == 0 and not pipes.double_closes


def test_a_client_accepted_while_the_stop_is_set_is_cut():
    """A client that arrives as the stop is being set is handed off cut:
    its first read ends the stream and it gets nothing written."""
    pipes = StandinPipes()
    seen = {}
    real_connect = pipes.connect

    def connect_then_stop(instance, stop):
        connected = real_connect(instance, stop)
        pipes.stops[0].set()
        return connected

    def handler(conn):
        seen["read"] = conn.recv(5)
        try:
            conn.sendall(b"late!")
        except BrokenPipeError:
            seen["write"] = "refused"

    pipes.connect = connect_then_stop
    server = pipe.PipeServer(NAME, pipes, handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = pipes.open_client(NAME)
    # The bytes a connection that was not cut would read. The cut may come
    # first, and then the write meets it.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        client.sendall(b"hello")
    thread.join(timeout=5)
    server.shutdown()
    server.server_close()
    assert seen == {"read": b"", "write": "refused"}
    client.settimeout(1.0)
    # Cut: the end of the stream, or a reset when its unread bytes were dropped.
    with contextlib.suppress(ConnectionResetError):
        assert client.recv(5) == b""
    client.close()


def test_a_failed_create_reports_its_own_error_not_a_later_calls():
    """The thin layer reads the last error right after CreateNamedPipeW:
    freeing the descriptor afterwards may set another one."""
    import ctypes as real

    class SecurityAttributes(real.Structure):
        _fields_ = [("nLength", real.c_uint32), ("lpSecurityDescriptor", real.c_void_p),
                    ("bInheritHandle", real.c_int)]

    last = {"code": 0}

    class FakeCtypes:
        c_void_p = real.c_void_p
        byref = staticmethod(real.byref)
        sizeof = staticmethod(real.sizeof)
        get_last_error = staticmethod(lambda: last["code"])
        FormatError = staticmethod(lambda code: f"Windows error {code}")

    class Advapi:
        def ConvertStringSecurityDescriptorToSecurityDescriptorW(self, sddl, revision, out, size):
            return 1

    layer = object.__new__(pipe.Win32Pipes)
    invalid = real.c_void_p(-1).value

    class Kernel:
        def CreateNamedPipeW(self, *args):
            last["code"] = pipe.ERROR_ACCESS_DENIED
            return invalid

        def LocalFree(self, pointer):
            last["code"] = 0

    layer._ctypes, layer.SECURITY_ATTRIBUTES, layer.invalid = FakeCtypes, SecurityAttributes, invalid
    layer.adv, layer.k32 = Advapi(), Kernel()
    with pytest.raises(OSError) as refused:
        layer.create(NAME, pipe.open_mode(True), pipe.pipe_mode(), pipe.security_descriptor(USER))
    assert "Windows error 5" in str(refused.value)


# ---------------------------------------------------------------------------
# Windows itself: these run on a Windows computer only
# ---------------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="calls Windows itself")


@windows_only
def test_on_windows_the_pipe_reads_back_with_the_user_alone_and_no_inheritance():
    import ctypes
    from ctypes import wintypes as w

    layer = pipe.Win32Pipes()
    sid = layer.user_sid()
    name = pipe.pipe_name()
    handle = layer.create(name, pipe.open_mode(True), pipe.pipe_mode(), pipe.security_descriptor(sid))
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv.GetSecurityInfo.restype = w.DWORD
        adv.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        k32.GetHandleInformation.restype = w.BOOL
        k32.GetHandleInformation.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k32.LocalFree.restype = ctypes.c_void_p
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        owner_dacl = 0x1 | 0x4
        descriptor = ctypes.c_void_p()
        assert adv.GetSecurityInfo(handle, 6, owner_dacl, None, None, None, None, ctypes.byref(descriptor)) == 0
        text = ctypes.c_void_p()
        assert adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, owner_dacl,
                                                                        ctypes.byref(text), None)
        sddl = ctypes.wstring_at(text.value)
        k32.LocalFree(text)
        k32.LocalFree(descriptor)
        assert sddl.startswith(f"O:{sid}D:P")
        assert re.findall(r"\(A;[^)]*;([^;)]+)\)", sddl) == [sid]
        flags = w.DWORD()
        assert k32.GetHandleInformation(handle, ctypes.byref(flags))
        assert not flags.value & 0x1
        with pytest.raises(OSError) as refused:
            layer.create(name, pipe.open_mode(True), pipe.pipe_mode(), pipe.security_descriptor(sid))
        assert refused.value.winerror == 5
    finally:
        layer.close(handle)


@windows_only
def test_on_windows_a_client_is_served_and_the_name_is_gone_after_the_stop():
    layer = pipe.Win32Pipes()
    name = pipe.pipe_name()
    server = pipe.PipeServer(name, layer, _echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with open(name, "r+b", buffering=0) as client:
            client.write(b"hello")
            assert client.read(5) == b"hello"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    with pytest.raises(OSError):
        open(name, "r+b", buffering=0)
