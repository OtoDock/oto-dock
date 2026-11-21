"""A session's SSH agent socket on this machine: the proxy signs.

A start payload may carry ``ssh_agent``: ``{"public_keys": {"<key name>":
"<OpenSSH public key line>"}}``. This module opens a unix socket for the
session under the satellite's run dir and relays every agent protocol
message a client writes to ``POST /v1/hooks/ssh-agent`` with the session's
token as the bearer; the answer goes back framed the same way. No private
key reaches this machine: the proxy judges and signs each message. The
public lines land as ``<key name>.pub`` under the session's secrets dir
(the folder ``session_files`` wipes at close), so ``ssh -i <key>.pub`` with
``IdentitiesOnly`` names the key the agent is asked for.

The socket's name is the proxy's rule (``core/credentials/ssh_signer
.socket_name``): the session id's hex characters, the first twelve, plus
``.sock``. A compute sandbox binds ``<socket_dir()>/<name>`` at
``/tmp/oto-ssh-agent.sock`` (``RenderHost.ssh_agent_dir``), so the socket
must be open before the render; the plan's environment already names the
sandbox's path, and the session merges this module's variables with
setdefault semantics (the payload's values win).

The server is a thread per socket and a thread per client (blocking reads
and a blocking POST, as ``session_files`` posts), so the session's loop
never waits on an agent client; ``start`` and ``stop`` are plain functions
the sessions run off the loop.

On Windows the endpoint is a named pipe of a random name instead
(``ssh_agent_pipe``, its transport), served by the same relay
(``_relay``): Windows' own OpenSSH client reads it from ``SSH_AUTH_SOCK``.
Git for Windows' own ssh cannot, so the session also gets ``OTO_SSH``, the
system's ssh, and ``GIT_SSH`` naming it when the machine names none of its
own (``_windows_ssh_env``).
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import shutil
import socket
import socketserver
import struct
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from .. import config as satellite_config
from ..config import otodock_dir
from . import session_files

logger = logging.getLogger("satellite.ssh_agent")

#: The proxy's cap on one agent message, either way.
MAX_MESSAGE_BYTES = 256 * 1024
#: The longest socket path bound: ``sun_path``'s portable room with margin,
#: the proxy's bound too.
MAX_SOCKET_PATH_BYTES = 100
#: SSH_AGENT_FAILURE as one framed message: the answer to any client when
#: the proxy did not answer.
FAILURE_FRAME = b"\x00\x00\x00\x01\x05"

RUN_DIRNAME = "run"
SOCKET_DIRNAME = "ssh-agent"
KEY_DIRNAME = "ssh"

_SHORT_ID_LEN = 12
_NOT_HEX = re.compile(r"[^0-9a-f]")
_KEY_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_POST_TIMEOUT_S = 20
_POLL_INTERVAL_S = 0.2

# PROXY_URL is this machine's own loopback tunnel: the post never goes
# through a proxy (urllib's default opener takes the environment's and, on
# Windows, Internet Options', whose <local> exemption never covers 127.0.0.1).
# A module opener, never a process-wide one: the vendor gateway may need one.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Client(Protocol):
    """An agent client on either transport: a unix socket, or
    ``ssh_agent_pipe.PipeConnection``."""

    def recv(self, n: int, /) -> bytes: ...
    def sendall(self, data: bytes, /) -> Any: ...
    def shutdown(self, how: int, /) -> None: ...


@dataclass
class _Door:
    session_id: str
    proxy_url: str
    token: str
    env: dict[str, str] = field(default_factory=dict)
    path: Path | None = None
    #: ``(st_dev, st_ino)`` of the socket file this door bound at ``path``.
    identity: tuple[int, int] | None = None
    #: ``_AgentServer`` for a unix socket, ``ssh_agent_pipe.PipeServer`` for a pipe.
    server: Any = None
    thread: threading.Thread | None = None
    clients: set[_Client] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Set once ``start`` is through with the door, open or refused.
    ready: threading.Event = field(default_factory=threading.Event)


_doors: dict[str, _Door] = {}
_doors_lock = threading.Lock()
#: Held around a socket file's bind and around its removal. A start binds
#: only while its door is still the session's and no other door holds the
#: path, both read under this lock, and a door removes only the file it
#: bound. Taken before ``_doors_lock``, never inside it.
_paths_lock = threading.Lock()


def socket_dir() -> Path:
    """The folder of this machine's session sockets: the compute render
    names it as ``RenderHost.ssh_agent_dir``."""
    return otodock_dir() / RUN_DIRNAME / SOCKET_DIRNAME


def socket_name(session_id: str) -> str:
    """``<sid12>.sock``: the session id's hex characters, the first twelve;
    ``""`` when the id has none. The proxy's rule, byte for byte."""
    short = _NOT_HEX.sub("", session_id)[:_SHORT_ID_LEN]
    return f"{short}.sock" if short else ""


def key_dir(session_id: str) -> Path:
    """Where the session's public key files land: under the secrets dir
    ``session_files`` keeps for the session."""
    return session_files._session_dir(session_id) / KEY_DIRNAME


def merge_env(env: dict[str, str], additions: dict[str, str], config: dict) -> None:
    """Land :func:`start`'s additions in a spawn env. A variable the payload's
    own ``env`` names keeps the payload's value (a compute plan names the
    sandbox's paths); a value this machine's environment carried, a user's
    own agent, never wins over the session's."""
    named = config.get("env") or {}
    for var, value in additions.items():
        if var not in named:
            env[var] = value


def start(session_id: str, config: dict) -> dict[str, str]:
    """Open the session's socket (a pipe on Windows) and write its public
    keys; the env additions (``SSH_AUTH_SOCK`` while the endpoint is open,
    ``OTO_SSH_KEY_DIR`` when keys landed, on Windows ``OTO_SSH`` and
    ``GIT_SSH`` with the pipe). ``{}`` when the payload carries no ``ssh_agent``.
    Idempotent per session id: a second call answers the first's env.
    Never raises on a refused socket, a failed write or a Windows layer
    that fails: the session starts without the agent and the log says why."""
    spec = config.get("ssh_agent")
    if not isinstance(spec, dict):
        return {}
    with _doors_lock:
        door = _doors.get(session_id)
        opener = door is None
        if opener:
            door = _Door(session_id=session_id, proxy_url=_relay_url(config),
                         token=str((config.get("env") or {}).get("PROXY_API_KEY") or ""))
            _doors[session_id] = door
    if not opener:
        door.ready.wait()
        return dict(door.env)
    try:
        keys = _write_public_keys(session_id, spec.get("public_keys"))
        if keys is not None:
            door.env["OTO_SSH_KEY_DIR"] = str(keys)
        if not satellite_config.HOST.posix:
            name = _open_pipe(door)
            if name is not None:
                door.env["SSH_AUTH_SOCK"] = name
                door.env.update(_windows_ssh_env())
        else:
            path = _open(door)
            if path is not None:
                door.env["SSH_AUTH_SOCK"] = str(path)
    finally:
        door.ready.set()
    with _doors_lock:
        current = _doors.get(session_id)
    if current is not door:
        # Stopped while it was opening: the socket must not outlive the session.
        _shut(door)
        return {}
    return dict(door.env)


def stop(session_id: str) -> None:
    """Close the session's server, unlink its socket (a pipe's name goes
    with its last handle) and remove its public key files; nothing to do
    for a session this module never saw."""
    with _doors_lock:
        door = _doors.pop(session_id, None)
    if door is not None:
        door.ready.wait()
        _shut(door)
    shutil.rmtree(key_dir(session_id), ignore_errors=True)


def _relay_url(config: dict) -> str:
    """Where this machine posts the session's messages: its own link port
    for a compute session (the payload's ``PROXY_URL`` is the sandbox's
    view of the proxy port), else the payload's ``PROXY_URL`` (the proxy
    rewrote it to this machine's loopback tunnel)."""
    port = int(config.get("tunnel_port") or 0)
    if port > 0:
        return f"http://127.0.0.1:{port}"
    return str((config.get("env") or {}).get("PROXY_URL") or "")


def _plain_basename(name: object) -> bool:
    """A key name that is one plain file name: letters, digits, dot, dash
    and underscore, not starting with a dot (so no dot dir and no hidden
    file), as the admin's key names are spelled."""
    return isinstance(name, str) and _KEY_NAME_RE.fullmatch(name) is not None


def _write_public_keys(session_id: str, public_keys: object) -> Path | None:
    """Write ``<name>.pub`` for every valid entry; the folder, or None when
    there is nothing to write or the folder could not be made."""
    if not isinstance(public_keys, dict) or not public_keys:
        return None
    sid = session_id[:8]
    folder = key_dir(session_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("ssh-agent: %s: no key dir at %s: %s", sid, folder, exc)
        return None
    _chmod_quiet(folder.parent, 0o700)
    _chmod_quiet(folder, 0o700)
    for name, line in public_keys.items():
        text = line.strip() if isinstance(line, str) else ""
        if not _plain_basename(name) or not text or "\n" in text or "\r" in text:
            logger.warning("ssh-agent: %s: refusing public key %r", sid, name)
            continue
        dest = folder / f"{name}.pub"
        try:
            dest.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("ssh-agent: %s: public key %s not written: %s", sid, dest, exc)
            continue
        _chmod_quiet(dest, 0o600)
    return folder


def _open(door: _Door) -> Path | None:
    """Bind and serve the session's socket; its path, or None with the
    reason logged."""
    sid = door.session_id[:8]
    if not door.proxy_url or not door.token:
        logger.warning("ssh-agent: %s: no socket, the payload names no proxy URL or session token", sid)
        return None
    name = socket_name(door.session_id)
    if not name:
        logger.warning("ssh-agent: %s: no socket, the session id has no hex characters", sid)
        return None
    path = socket_dir() / name
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        logger.warning("ssh-agent: %s: no socket, %s exceeds %d bytes", sid, path, MAX_SOCKET_PATH_BYTES)
        return None
    try:
        run = path.parent.parent
        run.mkdir(parents=True, exist_ok=True)
        os.chmod(run, 0o700)
        path.parent.mkdir(exist_ok=True)
        os.chmod(path.parent, 0o700)
        with _paths_lock:
            with _doors_lock:
                stopped = _doors.get(door.session_id) is not door
                held = any(d is not door and d.path == path for d in _doors.values())
            if stopped:
                # Stopped while it was opening: it binds nothing, so it
                # removes no file a newer door of the session bound.
                logger.info("ssh-agent: %s: no socket, the session was stopped while it opened", sid)
                return None
            if held:
                logger.warning("ssh-agent: %s: no socket, another session holds %s", sid, path)
                return None
            # A file at the path is a leftover: of a satellite that died with
            # its sessions, or of a door of this session that is shutting,
            # which removes only its own file.
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            server = _AgentServer(str(path), door)
            os.chmod(path, 0o600)
            st = os.lstat(path)
            door.path = path
            door.identity = (st.st_dev, st.st_ino)
    except OSError as exc:
        logger.warning("ssh-agent: %s: no socket at %s: %s", sid, path, exc)
        return None
    _serve(door, server, str(path))
    return path


def _open_pipe(door: _Door) -> str | None:
    """Create and serve the session's pipe (``ssh_agent_pipe``); its name,
    or None with the reason logged. The name is random per start: a pipe
    of another program under it fails the create."""
    sid = door.session_id[:8]
    if not door.proxy_url or not door.token:
        logger.warning("ssh-agent: %s: no pipe, the payload names no proxy URL or session token", sid)
        return None
    name = "a pipe not yet named"
    try:
        from . import ssh_agent_pipe
        name = ssh_agent_pipe.pipe_name()
        server = ssh_agent_pipe.PipeServer(name, ssh_agent_pipe.api(), lambda conn: _relay(door, conn))
    except Exception as exc:  # noqa: BLE001  # the Windows layer runs on Windows alone: any fault of it costs the endpoint, never the session's start
        logger.warning("ssh-agent: %s: no pipe at %s: %s: %s", sid, name, type(exc).__name__, exc)
        return None
    _serve(door, server, name)
    return name


def _serve(door: _Door, server: Any, where: str) -> None:
    """Run ``server`` on a thread of the door's own."""
    sid = door.session_id[:8]
    door.server = server
    door.thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": _POLL_INTERVAL_S},
        name=f"ssh-agent:{sid}", daemon=True,
    )
    door.thread.start()
    logger.info("ssh-agent: %s: serving %s", sid, where)


def _windows_ssh() -> str:
    """Windows' own OpenSSH client, spelled with forward slashes (Git Bash,
    PowerShell and git all take it), or ``""`` when it is not installed.
    The fixed system path, never a PATH lookup: PATH may find Git's own
    ssh first, or one any program placed there. A 32-bit Python looks
    through ``Sysnative``; the 64-bit programs of the session see
    ``System32``."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or "C:\\Windows"
    system = "Sysnative" if os.environ.get("PROCESSOR_ARCHITEW6432") else "System32"
    if not os.path.isfile(str(PureWindowsPath(root, system, "OpenSSH", "ssh.exe"))):
        return ""
    return PureWindowsPath(root, "System32", "OpenSSH", "ssh.exe").as_posix()


def _windows_ssh_env() -> dict[str, str]:
    """``OTO_SSH``, the ssh that reads the session's pipe, and ``GIT_SSH``
    naming it, so git over SSH runs it instead of Git's own. Git takes
    ``GIT_SSH_COMMAND``, then ``core.sshCommand``, then ``GIT_SSH``: a
    person's own choice in either of the first two still wins, and a
    ``GIT_SSH`` or ``GIT_SSH_COMMAND`` in this machine's environment holds
    the variable back."""
    ssh = _windows_ssh()
    if not ssh:
        logger.info("ssh-agent: Windows' OpenSSH client is not installed: only programs that read a pipe sign")
        return {}
    env = {"OTO_SSH": ssh}
    if not (os.environ.get("GIT_SSH") or os.environ.get("GIT_SSH_COMMAND")):
        env["GIT_SSH"] = ssh
    return env


def _shut(door: _Door) -> None:
    """Unlink the socket file this door bound, stop accepting, drop the
    clients. The server and the path are taken once: a stop racing the
    start's own shut closes nothing twice. The second caller returns at
    once, so a new door may bind the same path before the first shut ends:
    the file is removed only while it is still this door's (``_unlink_own``),
    and first, while the open server still holds its inode, so no new file
    can have taken that inode's number."""
    with door.lock:
        server, door.server = door.server, None
        path, door.path = door.path, None
        identity, door.identity = door.identity, None
    if path is not None and identity is not None:
        _unlink_own(path, identity)
    if server is not None:
        server.shutdown()
        server.server_close()
        with door.lock:
            clients = list(door.clients)
            door.clients.clear()
        for client in clients:
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
        if door.thread is not None:
            door.thread.join(timeout=2)


def _unlink_own(path: Path, identity: tuple[int, int]) -> None:
    """Remove ``path`` while it still holds the socket file this door bound
    (the proxy's ``ssh_signer._unbind`` does the same for its own sockets)."""
    with _paths_lock, contextlib.suppress(OSError):
        st = os.lstat(path)
        if (st.st_dev, st.st_ino) == identity:
            os.unlink(path)


class _AgentServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """A unix stream server with a thread per client. The clients are
    daemon threads: a hung one never holds the process at exit."""
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, door: _Door):
        self.address_family = socket.AF_UNIX
        self.door = door
        super().__init__(path, _Connection)


class _Connection(socketserver.BaseRequestHandler):
    """One unix socket client, served by the relay."""
    server: _AgentServer

    def handle(self) -> None:
        _relay(self.server.door, self.request)


def _relay(door: _Door, sock: _Client) -> None:
    """One agent client on either transport: framed messages in, each
    posted to the proxy, framed answers out. A zero or oversized length
    ends the connection, as the proxy's own server does."""
    with door.lock:
        door.clients.add(sock)
    try:
        while True:
            head = _read_exactly(sock, 4)
            if head is None:
                return
            length = struct.unpack(">I", head)[0]
            if length == 0 or length > MAX_MESSAGE_BYTES:
                return
            message = _read_exactly(sock, length)
            if message is None:
                return
            answer = _post(door.proxy_url, door.token, door.session_id, message)
            sock.sendall(struct.pack(">I", len(answer)) + answer if answer else FAILURE_FRAME)
    except OSError:
        return
    finally:
        with door.lock:
            door.clients.discard(sock)


def _read_exactly(sock: _Client, n: int) -> bytes | None:
    """``n`` bytes from the client, or None when it closed first."""
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = sock.recv(min(n - got, 65536))
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _post(proxy_url: str, token: str, session_id: str, message: bytes) -> bytes | None:
    """One agent message to the proxy's hook; its answer, or None on any
    failure (the client then gets SSH_AGENT_FAILURE)."""
    body = json.dumps({
        "session_id": session_id,
        "message_b64": base64.b64encode(message).decode("ascii"),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{proxy_url.rstrip('/')}/v1/hooks/ssh-agent",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _opener.open(req, timeout=_POST_TIMEOUT_S) as resp:
            answer = base64.b64decode(
                json.loads(resp.read().decode("utf-8"))["message_b64"], validate=True,
            )
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("ssh-agent: %s: the proxy did not answer: %s", session_id[:8], exc)
        return None
    if not answer or len(answer) > MAX_MESSAGE_BYTES:
        logger.warning("ssh-agent: %s: the proxy's answer is empty or too large", session_id[:8])
        return None
    return answer


def _chmod_quiet(path: Path, mode: int) -> None:
    """chmod that tolerates platforms where it is a no-op (Windows ACLs)."""
    with contextlib.suppress(OSError):
        os.chmod(path, mode)
