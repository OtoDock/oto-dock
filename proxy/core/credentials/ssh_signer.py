"""The platform's one SSH signer: the SSH agent protocol, answered per session.

No private key leaves the proxy. A session gets a socket (local: the unix
socket this module serves; a machine: the satellite's socket, posted to
``/v1/hooks/ssh-agent``) and the two doors call :func:`answer` with one agent
message; nothing else calls it. The agent whose keys answer is the session's
registered SecurityContext's, never a caller's argument.

Per message, both the listing and a signature: the session is live, its
context takes the keys (``session_takes_ssh_keys``: the editor tier, never an
outside caller, never a judge, never no context) and the key is one of the
agent's authorised keys at that moment, read fresh. A signature is given
over an SSH userauth request alone, for a user the agent's ssh-hosts
instances name for that key; anything else (an SSHSIG blob, a git commit,
another method) is refused. Add, remove, lock, unlock and every extension
answer ``SSH_AGENT_FAILURE``. No failure reaches the caller: every one
answers ``SSH_AGENT_FAILURE`` and is logged with its reason. The log never
carries key material, the signed data or the signature.

:func:`answer` blocks (file reads, the registry, RSA signing): the callers run
it in a thread. Nothing here reads the feature flag; the callers decide.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import logging
import os
import re
import socket
import stat
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import config
from core import background, file_commit
from core.sandbox import sandbox_render, session_config_dir
from core.session import session_state

logger = logging.getLogger("otodock.ssh-signer")

# The socket's path inside the sandbox (the render binds the served socket there).
SANDBOX_SOCKET = sandbox_render.SSH_AGENT_SOCKET
# draft-miller-ssh-agent message numbers and flags.
SSH_AGENT_FAILURE = 5
SSH_AGENTC_REQUEST_IDENTITIES = 11
SSH_AGENT_IDENTITIES_ANSWER = 12
SSH_AGENTC_SIGN_REQUEST = 13
SSH_AGENT_SIGN_RESPONSE = 14
SSH_AGENT_RSA_SHA2_256 = 2
SSH_AGENT_RSA_SHA2_512 = 4
# RFC 4252: the one message a signature is given over.
SSH_MSG_USERAUTH_REQUEST = 50
_USERAUTH_SERVICE = b"ssh-connection"
_USERAUTH_METHOD = b"publickey"
# OpenSSH's hostbound form of the same method (the client's default toward a
# server that offers it): the request ends with the server's host key.
_USERAUTH_METHOD_HOSTBOUND = b"publickey-hostbound-v00@openssh.com"

FAILURE = bytes([SSH_AGENT_FAILURE])

# One framed agent message on the socket; a longer frame closes the connection.
MAX_MESSAGE_BYTES = 256 * 1024
# sun_path holds 108 bytes; the margin keeps a bind-mount's spelling under it.
MAX_SOCKET_PATH_BYTES = 100
_SHORT_ID_LEN = 12
_NOT_HEX = re.compile(r"[^0-9a-f]")

_Private = ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey

_EC_CURVES: dict[str, tuple[bytes, type[hashes.HashAlgorithm]]] = {
    "secp256r1": (b"ecdsa-sha2-nistp256", hashes.SHA256),
    "secp384r1": (b"ecdsa-sha2-nistp384", hashes.SHA384),
    "secp521r1": (b"ecdsa-sha2-nistp521", hashes.SHA512),
}

# The key names already warned about in this process: one line per name.
_unusable_logged: set[str] = set()


# --- SSH wire primitives ----------------------------------------------------


def _uint32(n: int) -> bytes:
    return struct.pack(">I", n)


def _string(data: bytes) -> bytes:
    return _uint32(len(data)) + data


def _mpint(n: int) -> bytes:
    """A non-negative integer as an SSH mpint: big-endian two's complement,
    a leading zero byte when the high bit is set, empty for zero."""
    if n == 0:
        return _string(b"")
    return _string(n.to_bytes((n.bit_length() + 8) // 8, "big"))


class _Reader:
    """A cursor over one message; every short read raises ValueError."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _take(self, n: int) -> bytes:
        end = self._pos + n
        if n < 0 or end > len(self._data):
            raise ValueError("short message")
        out = self._data[self._pos:end]
        self._pos = end
        return out

    def byte(self) -> int:
        return self._take(1)[0]

    def boolean(self) -> bool:
        return self.byte() != 0

    def uint32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def string(self) -> bytes:
        return self._take(self.uint32())

    @property
    def done(self) -> bool:
        return self._pos == len(self._data)


# --- Keys -------------------------------------------------------------------


@dataclass(frozen=True)
class _Key:
    name: str
    blob: bytes
    private: _Private


def _public_blob(key: _Private) -> bytes:
    line = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return base64.b64decode(line.split()[1])


def fingerprint(blob: bytes) -> str:
    """OpenSSH's spelling of a public key's SHA256 fingerprint."""
    digest = base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")
    return "SHA256:" + digest.decode("ascii")


def _skip(name: str, why: str) -> None:
    if name in _unusable_logged:
        return
    _unusable_logged.add(name)
    logger.warning("ssh-signer: key %s is not usable (%s) and is never offered", name, why)


def _parse_private_key(data: bytes) -> PrivateKeyTypes:
    try:
        return serialization.load_ssh_private_key(data, password=None)
    except ValueError:
        return serialization.load_pem_private_key(data, password=None)


def _load_key(name: str, path: Path) -> _Key | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        _skip(name, f"unreadable: {type(exc).__name__}")
        return None
    try:
        private = _parse_private_key(data)
    except TypeError:
        _skip(name, "encrypted: the platform holds keys without a passphrase")
        return None
    except Exception as exc:  # noqa: BLE001  # the parsers raise several classes for one meaning: not a usable key
        _skip(name, f"not a readable private key: {type(exc).__name__}")
        return None
    if isinstance(private, ec.EllipticCurvePrivateKey):
        if private.curve.name not in _EC_CURVES:
            _skip(name, f"unsupported curve {private.curve.name}")
            return None
    elif not isinstance(private, ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey):
        _skip(name, f"unsupported key type {type(private).__name__}")
        return None
    return _Key(name=name, blob=_public_blob(private), private=private)


def _usable_keys(agent: str) -> list[_Key]:
    """The agent's authorised keys that load, by name, read fresh."""
    out: list[_Key] = []
    for name, path in sorted(session_config_dir.collect_authorized_ssh_keys(agent).items()):
        key = _load_key(name, path)
        if key is not None:
            out.append(key)
    return out


def public_lines(agent: str) -> dict[str, str]:
    """The public half of every key ``agent``'s instances authorise, by key
    name, as an OpenSSH public key line (the key name as its comment): what
    a session's key dir holds in place of the private files, so the prompt's
    ``ssh -i <name>.pub -o IdentitiesOnly=yes`` picks that agent key."""
    out: dict[str, str] = {}
    for key in _usable_keys(agent):
        algorithm = key.blob[4:4 + struct.unpack(">I", key.blob[:4])[0]].decode("ascii", "replace")
        out[key.name] = f"{algorithm} {base64.b64encode(key.blob).decode('ascii')} {key.name}\n"
    return out


def _users_for_key(agent: str, key_name: str) -> set[str]:
    """The usernames the agent's ssh-hosts instances name for ``key_name``;
    an instance without a username admits no user."""
    _, instances = session_config_dir._authorizing_ssh_instances(agent)
    users: set[str] = set()
    for inst in instances:
        fields = inst.get("field_values") or {}
        if str(fields.get("key_name") or "").strip() != key_name:
            continue
        user = str(fields.get("username") or "").strip()
        if user:
            users.add(user)
    return users


def _sign(key: _Private, data: bytes, flags: int) -> bytes:
    """The SSH signature blob: ``string algorithm, string signature``."""
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return _string(b"ssh-ed25519") + _string(key.sign(data))
    if isinstance(key, rsa.RSAPrivateKey):
        digest: hashes.HashAlgorithm
        if flags & SSH_AGENT_RSA_SHA2_256:
            algorithm, digest = b"rsa-sha2-256", hashes.SHA256()
        elif flags & SSH_AGENT_RSA_SHA2_512:
            algorithm, digest = b"rsa-sha2-512", hashes.SHA512()
        else:
            algorithm, digest = b"ssh-rsa", hashes.SHA1()
        return _string(algorithm) + _string(key.sign(data, padding.PKCS1v15(), digest))
    algorithm, hash_cls = _EC_CURVES[key.curve.name]
    r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hash_cls())))
    return _string(algorithm) + _string(_mpint(r) + _mpint(s))


# --- The protocol -----------------------------------------------------------


def _refuse(sid: str, why: str) -> bytes:
    logger.info("ssh-signer: %s refused: %s", sid, why)
    return FAILURE


def _entitled_agent(session_id: str) -> tuple[str, str]:
    """``(agent, "")`` when the session may use its agent's keys now, else
    ``("", reason)``."""
    if not session_state.session_is_live(session_id):
        return "", "session not live"
    ctx = session_state.get_session_security(session_id)
    if not session_config_dir.session_takes_ssh_keys(ctx):
        return "", "the session's context does not take the agent's keys"
    agent = getattr(ctx, "agent", "")
    if not isinstance(agent, str) or not agent:
        return "", "the session's context names no agent"
    return agent, ""


def _userauth_user(data: bytes, key_blob: bytes) -> tuple[str, str]:
    """``(user, "")`` when ``data`` is exactly an SSH_MSG_USERAUTH_REQUEST
    of the publickey method (plain or hostbound) over ``key_blob``, else
    ``("", reason)``."""
    r = _Reader(data)
    try:
        r.string()
        kind = r.byte()
        user = r.string()
        service = r.string()
        method = r.string()
        signed = r.boolean()
        r.string()
        blob = r.string()
        if method == _USERAUTH_METHOD_HOSTBOUND:
            r.string()
    except ValueError:
        return "", "data is not a userauth request"
    if kind != SSH_MSG_USERAUTH_REQUEST:
        return "", f"data is not a userauth request (message {kind})"
    if service != _USERAUTH_SERVICE or method not in (_USERAUTH_METHOD, _USERAUTH_METHOD_HOSTBOUND):
        return "", "data is a userauth request of another service or method"
    if not signed:
        return "", "data is a userauth query, not a signed request"
    if blob != key_blob:
        return "", "data names another key than the request"
    if not r.done:
        return "", "data carries bytes after the userauth request"
    try:
        return user.decode("utf-8"), ""
    except UnicodeDecodeError:
        return "", "data names a user that is not UTF-8"


def _identities(session_id: str) -> bytes:
    sid = session_id[:8]
    agent, why = _entitled_agent(session_id)
    if why:
        return _refuse(sid, f"identities: {why}")
    keys = _usable_keys(agent)
    body = b"".join(_string(k.blob) + _string(k.name.encode("utf-8")) for k in keys)
    return bytes([SSH_AGENT_IDENTITIES_ANSWER]) + _uint32(len(keys)) + body


def _signature(session_id: str, body: bytes) -> bytes:
    sid = session_id[:8]
    r = _Reader(body)
    try:
        key_blob = r.string()
        data = r.string()
        flags = r.uint32()
    except ValueError:
        return _refuse(sid, "sign: malformed request")
    if not r.done:
        return _refuse(sid, "sign: bytes after the request")
    agent, why = _entitled_agent(session_id)
    if why:
        return _refuse(sid, f"sign: {why}")
    wanted = fingerprint(key_blob)
    key = next((k for k in _usable_keys(agent) if k.blob == key_blob), None)
    if key is None:
        return _refuse(sid, f"sign: {wanted} is not a key agent {agent} is authorised for")
    user, why = _userauth_user(data, key_blob)
    if why:
        return _refuse(sid, f"sign: {key.name} {wanted}: {why}")
    if user not in _users_for_key(agent, key.name):
        return _refuse(sid, f"sign: {key.name} {wanted}: no instance names user {user[:64]!r}")
    signature = _sign(key.private, data, flags)
    logger.info("ssh-signer: %s signed as agent %s with %s %s for user %s (%d bytes)",
                sid, agent, key.name, wanted, user, len(data))
    return bytes([SSH_AGENT_SIGN_RESPONSE]) + _string(signature)


def answer(session_id: str, message: bytes) -> bytes:
    """One agent message in (the payload after its length prefix), one
    payload out, never an exception. Blocking: run it in a thread."""
    try:
        if not message:
            return _refuse(session_id[:8], "empty message")
        kind = message[0]
        if kind == SSH_AGENTC_REQUEST_IDENTITIES:
            return _identities(session_id)
        if kind == SSH_AGENTC_SIGN_REQUEST:
            return _signature(session_id, message[1:])
        return _refuse(session_id[:8], f"message type {kind} is not served")
    except Exception:
        logger.exception("ssh-signer: %s failed to answer a message", session_id[:8])
        return FAILURE


# --- The local socket door --------------------------------------------------


@dataclass
class _Door:
    path: Path
    loop: asyncio.AbstractEventLoop
    server: asyncio.Server | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    # The open connections, closed with the door: uvloop's ``Server`` has no
    # ``close_clients``, and ``wait_closed`` waits for every connection.
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    # The bound socket's ``(st_dev, st_ino)``: the one name ``_unbind`` removes.
    identity: tuple[int, int] = (0, 0)
    # Set when the start was cancelled: a bind that has not begun never does.
    abandoned: threading.Event = field(default_factory=threading.Event)


_doors: dict[str, _Door] = {}
_hook_registered = False
# The binds and the removals of the sockets' names, one at a time: a removal
# of an old life's name never lands on a socket a new life bound there.
_names_lock = threading.Lock()


def socket_dir() -> Path:
    return config.SESSIONS_DIR / "ssh-agent"


def socket_name(session_id: str) -> str:
    """``<sid12>.sock``: the session id's hex characters, the first twelve;
    ``""`` when the id has none."""
    short = _NOT_HEX.sub("", session_id)[:_SHORT_ID_LEN]
    return f"{short}.sock" if short else ""


async def _connection(session_id: str, door: _Door, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
    door.writers.add(writer)
    try:
        while True:
            length = struct.unpack(">I", await reader.readexactly(4))[0]
            if length == 0 or length > MAX_MESSAGE_BYTES:
                break
            payload = await reader.readexactly(length)
            writer.write(_string(await asyncio.to_thread(answer, session_id, payload)))
            await writer.drain()
    except (asyncio.IncompleteReadError, OSError):
        pass
    finally:
        door.writers.discard(writer)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


class _Abandoned(Exception):
    """A bind whose start was cancelled before it began."""


def _bind(path: Path, abandoned: threading.Event) -> tuple[socket.socket, tuple[int, int]]:
    """The session's socket bound at ``path``, with its ``(st_dev,
    st_ino)``. Blocking: on the file-commit pool. The folder is made and set
    0700, a socket an earlier process left at the name is removed (a socket
    alone, a link never followed), and the socket is set 0600 before it
    listens. A start cancelled before the bind began (``abandoned``) binds
    nothing: its late bind would take the name from the next life's socket."""
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with _names_lock:
            if abandoned.is_set():
                raise _Abandoned(path)
            with contextlib.suppress(FileNotFoundError):
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    os.unlink(path)
            sock.bind(str(path))
            os.chmod(path, 0o600)
            st = os.lstat(path)
    except BaseException:
        sock.close()
        raise
    return sock, (st.st_dev, st.st_ino)


def _unbind(path: Path, identity: tuple[int, int]) -> None:
    """Remove ``path`` while it still holds the socket ``_bind`` made there.
    Blocking: on the file-commit pool."""
    with _names_lock, contextlib.suppress(FileNotFoundError):
        st = os.lstat(path)
        if (st.st_dev, st.st_ino) == identity:
            os.unlink(path)


def _release(sock: socket.socket, path: Path, identity: tuple[int, int]) -> None:
    """A bound socket no server took: its name removed while the socket
    still holds it, then the socket closed. Blocking: on the file-commit
    pool."""
    try:
        _unbind(path, identity)
    finally:
        sock.close()


def _drop_late(path: Path, bound: asyncio.Future) -> None:
    """A bind that landed after its start was cancelled: its name removed,
    then its socket closed."""
    if bound.cancelled() or bound.exception() is not None:
        return
    sock, identity = bound.result()
    file_commit.behind(_release, sock, path, identity, name="ssh-agent socket")


def _drop(session_id: str, door: _Door) -> None:
    """A start that ended without a server: the door goes and its waiters
    are told."""
    if _doors.get(session_id) is door:
        _doors.pop(session_id)
    door.ready.set()


async def _open(session_id: str, door: _Door) -> asyncio.Server:
    """The door's socket bound on the pool, then served on the loop. The
    server never removes the name itself (``cleanup_socket``: on 3.13 both
    uvloop and asyncio would stat it at the start and remove it at the
    close, on the loop): ``_shut`` does, on the pool."""
    bound = asyncio.ensure_future(file_commit.run(_bind, door.path, door.abandoned))
    try:
        sock, door.identity = await asyncio.shield(bound)
    except asyncio.CancelledError:
        door.abandoned.set()
        bound.add_done_callback(functools.partial(_drop_late, door.path))
        raise
    try:
        return await asyncio.start_unix_server(
            lambda r, w: _connection(session_id, door, r, w), sock=sock, cleanup_socket=False)
    except BaseException:
        # Closed here, on the loop, before its name goes: a cancel inside the
        # server's last tick can leave the loop serving this socket with no
        # one holding the server, so a pool thread must not close it.
        sock.close()
        file_commit.behind(_unbind, door.path, door.identity, name="ssh-agent socket")
        raise


async def serve(session_id: str) -> str | None:
    """Serve the session's agent socket; its path, or None when refused.
    Idempotent per session id: a second call answers the first's path."""
    door = _doors.get(session_id)
    if door is not None:
        await door.ready.wait()
        return str(door.path) if door.server is not None else None
    sid = session_id[:8]
    name = socket_name(session_id)
    if not name:
        logger.warning("ssh-signer: %s: no socket, the session id has no hex characters", sid)
        return None
    path = socket_dir() / name
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        logger.warning("ssh-signer: %s: no socket, %s exceeds %d bytes",
                       sid, path, MAX_SOCKET_PATH_BYTES)
        return None
    if any(d.path == path for d in _doors.values()):
        logger.warning("ssh-signer: %s: no socket, another session holds %s", sid, path)
        return None
    door = _Door(path=path, loop=asyncio.get_running_loop())
    _doors[session_id] = door
    try:
        server = await _open(session_id, door)
    except _Abandoned:
        _drop(session_id, door)
        return None
    except OSError as exc:
        _drop(session_id, door)
        logger.warning("ssh-signer: %s: no socket at %s: %s", sid, path, exc)
        return None
    except BaseException:
        _drop(session_id, door)
        raise
    door.server = server
    door.ready.set()
    if _doors.get(session_id) is not door:
        # Closed while it was opening: the socket must not outlive the session.
        await _shut(door)
        return None
    return str(path)


async def attach_sandbox(session_id: str, ctx, builder, extra_env: dict[str, str],
                         *, compute: bool) -> None:
    """The socket half of a session's start: a session that
    takes the agent's keys (``session_takes_ssh_keys``) gets its agent
    socket named on the sandbox description (``SandboxBuilder.ssh_agent``,
    bound by the render at ``SANDBOX_SOCKET``) and ``SSH_AUTH_SOCK`` in its
    environment. A local sandbox's socket is served here (``serve``); a
    compute machine's satellite opens one of the same name and forwards
    each message to ``POST /v1/hooks/ssh-agent`` (every satellite at the
    platform's floor does). Below the editor tier nothing is added."""
    from core import placement
    if not session_config_dir.session_takes_ssh_keys(ctx):
        return
    if compute and placement.compute_of(getattr(ctx, "placement", None)) is None:
        return
    name = socket_name(session_id)
    if not name:
        return
    if not compute and await serve(session_id) is None:
        return
    builder.ssh_agent = name
    extra_env["SSH_AUTH_SOCK"] = SANDBOX_SOCKET


async def _shut(door: _Door) -> None:
    # Taken once: a second shut of the door (``close`` and an opening
    # ``serve`` both shut it) compares nothing after the listener closed.
    server, door.server = door.server, None
    if server is None:
        return
    # The name first, on the pool: the open listener keeps its inode, so
    # what goes is this door's own socket or nothing.
    try:
        with contextlib.suppress(OSError):
            await file_commit.run(_unbind, door.path, door.identity)
    finally:
        server.close()
        for writer in list(door.writers):
            writer.close()
    await server.wait_closed()


async def close(session_id: str) -> None:
    """Close the session's socket and unlink the path this module created
    for it; nothing when the session has none."""
    door = _doors.pop(session_id, None)
    if door is None:
        return
    # A bind that has not begun never does: the next life may bind the
    # name first, and a late bind would take that socket for a leftover.
    door.abandoned.set()
    await door.ready.wait()
    await _shut(door)


def _spawn_close(session_id: str) -> None:
    background.spawn(close(session_id), name=session_id[:8], owner="ssh-signer")


def forget(session_id: str) -> None:
    """The purge hook's entry: schedule :func:`close` on the loop that serves
    the session's socket, from the loop's thread or another."""
    door = _doors.get(session_id)
    if door is None:
        return
    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is door.loop:
        _spawn_close(session_id)
    elif not door.loop.is_closed():
        door.loop.call_soon_threadsafe(_spawn_close, session_id)


def register_purge_hook() -> None:
    """Called once at boot: a purged session loses its socket."""
    global _hook_registered
    if _hook_registered:
        return
    from core.credentials import mcp_gateway
    mcp_gateway.add_purge_hook(forget)
    _hook_registered = True


def sweep_at_boot() -> int:
    """Remove every socket an earlier process left; how many went."""
    folder = socket_dir()
    if not folder.is_dir():
        return 0
    removed = 0
    for leftover in folder.glob("*.sock"):
        with contextlib.suppress(OSError):
            leftover.unlink()
            removed += 1
    return removed
