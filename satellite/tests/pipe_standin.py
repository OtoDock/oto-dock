"""A stand-in for ``ssh_agent_pipe.Win32Pipes``: a pipe namespace in memory
with the Windows rules the pipe's design leans on, so the transport's loop
and the door run on Linux.

- A create with ``FILE_FLAG_FIRST_PIPE_INSTANCE`` fails while the name has an
  instance; a name whose last instance closed is free for anyone.
- A client open fails with "not found" when the name has no instance, busy
  when no instance listens, refused when the descriptor recorded at the
  pipe's first create names no allow entry for the client's account, and
  refused for a client from another machine when the pipe mode holds
  ``PIPE_REJECT_REMOTE_CLIENTS``.
- A client may arrive before ``connect`` is called.
- A stop ends a pending wait or read, and a stop set before the operation
  ends it too.

It records every create's arguments and the instance count of each name
after every create and close, and the data runs over a socket pair, so the
relay's bytes are real. ``write_limit`` makes every write partial.
"""

from __future__ import annotations

import contextlib
import re
import select
import socket
import threading
import time

from satellite.sessions import ssh_agent_pipe as pipe

USER = "S-1-5-21-1004336348-1177238915-682003330-1001"
OTHER_USER = "S-1-5-21-1004336348-1177238915-682003330-1002"
_ALLOW = re.compile(r"\(A;[^;]*;[^;]*;[^;]*;[^;]*;([^)]+)\)")
_POLL_S = 0.01


class Stop:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.closed = False
        self.double_closes = 0

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def close(self) -> None:
        if self.closed:
            self.double_closes += 1
        self.closed = True


class _Pipe:
    def __init__(self, sddl: str, pmode: int) -> None:
        self.sddl = sddl
        self.pmode = pmode
        self.instances: list[Instance] = []

    def allows(self, user: str) -> bool:
        if not self.sddl.startswith(f"O:{USER}D:P"):
            # Not the protected list owned by the user: a default or an
            # inherited list may admit other accounts.
            return True
        return any(sid in (user, "WD", "AU", "BU") for sid in _ALLOW.findall(self.sddl))


class Instance:
    def __init__(self, name: str, owner: _Pipe) -> None:
        self.name = name
        self.pipe = owner
        self.state = "listening"
        self.sock: socket.socket | None = None
        self.closed = False


class StandinPipes:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.names: dict[str, _Pipe] = {}
        self.creates: list[tuple[str, int, int, str]] = []
        self.counts: dict[str, list[int]] = {}
        self.write_limit = 0
        self.stops: list[Stop] = []
        self.handles: list[Instance] = []
        self.fail_next_create = False
        self.double_closes: list[str] = []

    # -- the thin layer's calls ---------------------------------------------

    def user_sid(self) -> str:
        return USER

    def stopper(self) -> Stop:
        stop = Stop()
        self.stops.append(stop)
        return stop

    def create(self, name: str, mode: int, pmode: int, sddl: str) -> Instance:
        with self.cond:
            self.creates.append((name, mode, pmode, sddl))
            if self.fail_next_create:
                self.fail_next_create = False
                raise OSError(None, "CreateNamedPipeW: Insufficient system resources exist", None, 1450)
            owner = self.names.get(name)
            if mode & pipe.FILE_FLAG_FIRST_PIPE_INSTANCE and owner is not None and owner.instances:
                raise PermissionError(None, "CreateNamedPipeW: Access is denied", None, 5)
            if owner is None or not owner.instances:
                owner = _Pipe(sddl, pmode)
                self.names[name] = owner
            instance = Instance(name, owner)
            owner.instances.append(instance)
            self.handles.append(instance)
            self._count(name)
            self.cond.notify_all()
            return instance

    def connect(self, instance: Instance, stop: Stop) -> bool:
        with self.cond:
            while instance.state == "listening":
                if stop.is_set():
                    return False
                self.cond.wait(_POLL_S)
            return True

    def read(self, instance: Instance, n: int, stop: Stop) -> bytes:
        while True:
            if stop.is_set() or instance.sock is None or instance.state != "connected":
                return b""
            try:
                ready, _, _ = select.select([instance.sock], [], [], _POLL_S)
            except (OSError, ValueError):
                return b""
            if ready:
                try:
                    return instance.sock.recv(n)
                except OSError:
                    return b""

    def write(self, instance: Instance, data: bytes, stop: Stop) -> int:
        if stop.is_set() or instance.sock is None or instance.state != "connected":
            raise BrokenPipeError("the client is gone")
        chunk = data[: self.write_limit] if self.write_limit else data
        try:
            instance.sock.sendall(chunk)
        except OSError as exc:
            raise BrokenPipeError("the client is gone") from exc
        return len(chunk)

    def disconnect(self, instance: Instance) -> None:
        with self.cond:
            instance.state = "disconnected"
            if instance.sock is not None:
                with contextlib.suppress(OSError):
                    instance.sock.shutdown(socket.SHUT_RDWR)
            self.cond.notify_all()

    def close(self, instance: Instance) -> None:
        with self.cond:
            if instance.closed:
                self.double_closes.append(instance.name)
                return
            instance.closed = True
            instance.state = "closed"
            if instance.sock is not None:
                instance.sock.close()
            owner = instance.pipe
            owner.instances.remove(instance)
            if not owner.instances and self.names.get(instance.name) is owner:
                del self.names[instance.name]
            self._count(instance.name)
            self.cond.notify_all()

    # -- a client, as Windows OpenSSH opens the name ----------------------------

    def open_client(self, name: str, *, user: str = USER, remote: bool = False) -> socket.socket:
        with self.cond:
            owner = self.names.get(name)
            if owner is None or not owner.instances:
                raise FileNotFoundError(2, "The system cannot find the file specified", name)
            if remote and owner.pmode & pipe.PIPE_REJECT_REMOTE_CLIENTS:
                raise PermissionError(5, "Access is denied", name)
            if not owner.allows(user):
                raise PermissionError(5, "Access is denied", name)
            listening = [i for i in owner.instances if i.state == "listening"]
            if not listening:
                raise OSError(231, "All pipe instances are busy", name)
            instance = listening[0]
            ours, theirs = socket.socketpair()
            instance.sock = ours
            instance.state = "connected"
            self.cond.notify_all()
        theirs.settimeout(5)
        return theirs

    # -- what the tests read -----------------------------------------------------

    def _count(self, name: str) -> None:
        owner = self.names.get(name)
        self.counts.setdefault(name, []).append(len(owner.instances) if owner else 0)

    def open_instances(self, name: str) -> int:
        with self.cond:
            owner = self.names.get(name)
            return len(owner.instances) if owner else 0

    def wait_listening(self, name: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while time.monotonic() < deadline:
                owner = self.names.get(name)
                if owner and any(i.state == "listening" for i in owner.instances):
                    return
                self.cond.wait(_POLL_S)
        raise AssertionError(f"no instance of {name} listens")
