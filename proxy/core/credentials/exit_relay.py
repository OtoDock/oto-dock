"""The credential exit's relay: the proxy at a session's exit that adds a
credential's value on the way out, so the session holds a placeholder. One
implementation for every machine's satellite, which vendors this module byte
for byte and supplies a backend of its own
(``satellite/sessions/credential_exit.py``); the platform keeps it in its own
tree beside the code that gives each session its door.

A session's clients reach the relay as an HTTPS proxy (``HTTPS_PROXY`` in
the session's environment names a loopback port, ``session_env``) and trust
its authority through the usual variables. Every ``CONNECT`` carries the
session's own credential in ``Proxy-Authorization`` (the secret the
machine's exit was told for the session); without one nothing is dialled
(407). What the relay does with a ``CONNECT``:

* **Where it may go.** The backend answers the addresses the target may be
  dialled at (``addresses``: the sandbox shim's rule on a compute machine,
  every address on a paired computer) and the relay dials those alone.
* **Through the machine's own proxy.** Where the backend names an outgoing
  proxy for the target (``upstream``), the relay dials that proxy instead:
  a plain tunnel through its ``CONNECT``, a forward through it by the host's
  name. The target is still judged first (``upstream_admits``), and the
  proxy's own credential is the backend's, never the session's.
* **An engine's host** is ended only for a connection a tool makes with its
  own proxy user (``TOOL_PROXY_USER``): every other connection to such a
  host is a plain tunnel, so an engine's own traffic is never read.
* **What it ends.** A target that is an approved host of one of the
  session's placeholders (``takes_host``) is ended here: the client side is
  upgraded to TLS with a leaf the backend's authority signed for that host,
  HTTP/1.1 alone, and the requests inside are read. Every other ``CONNECT``
  is a plain tunnel of bytes to a checked address, nothing read.
* **The decrypted request** is judged on its own ``Host``: it must name the
  ``CONNECT`` target (a default port left out) and be approved for the
  placeholder the request carries; the body is read whole, up to the cap,
  before anything is dialled. A placeholder found in the request target, in
  any header but the credential's own, or in the body refuses the request:
  the value goes only into the configured header. In that header the exact
  placeholder is replaced by the value the backend gives now (``value_of``;
  a ``Basic`` scheme is decoded and re-encoded around it). A placeholder of
  another session, or none, is forwarded as it came: a random string nobody
  can use.
* **A body of any size** passes for an entry that asks for it
  (``Entry.streams``), when it is chunked or announces more than
  ``STREAM_FROM``: it is forwarded as it arrives and searched as it passes,
  with the bytes a placeholder could still begin in held back, so no byte
  of a found placeholder is sent on and the request is cut before its end.
  The head, with the value in its own header, has then reached the approved
  host: that is the one difference from a buffered body.
* **A judged entry** (``Entry.judged``): the backend is asked about each
  request that carries its placeholder (``Backend.judge``) after the head's
  own checks and before the value is asked. Its word refuses the request,
  asks for the body, replaces it, or lets it go. What it judges by is the
  backend's own: nothing of a vendor is in this file.
* **What is held.** A buffered body counts against its session's share and
  the relay's budget (``SESSION_BUFFER``, ``BUFFER_BUDGET``) while its
  request lives; a streamed body holds one piece and the carry.
* **The forward** is an ``httpx`` request through the backend's client, the
  host set as the request named it, no redirects, the certificate verified;
  the answer streams back. A 3xx is passed back as the vendor sent it and
  never followed: the client's next request is judged on its own host.
* **Nothing in a log** but the session's first eight characters, the host,
  the credential's key and a refusal's reason.

Plain ``http://`` requests never carry a value and are refused: only
``HTTPS_PROXY`` names the relay, so they take the session's own path.

Stdlib, ``h11`` and ``httpx`` alone, no platform import, 3.10 syntax: the
satellite runs this file on its own floor.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import secrets
import socket
import ssl
import sys
import time
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import h11
import httpx

PLACEHOLDER_PREFIX = "oto-exit-"
PLACEHOLDER_RE = re.compile(rb"oto-exit-[0-9a-f]{32}")
#: The request body cap of an ended connection: the gateway's too
#: (``mcp_gateway.MAX_BODY_BYTES`` reads this one).
MAX_BODY_BYTES = 16 * 1024 * 1024
#: From this announced length, and for a body of unknown length, a request
#: of an entry that streams is forwarded as it arrives.
STREAM_FROM = 1024 * 1024
#: The buffered bodies held at one time: one session's, and the relay's.
SESSION_BUFFER = 64 * 1024 * 1024
BUFFER_BUDGET = 256 * 1024 * 1024
_HEAD_BYTES = 64 * 1024
_CHUNK = 64 * 1024
_HEAD_TIMEOUT_S = 60.0
_CONNECT_TIMEOUT_S = 10.0
#: The bound on an outgoing proxy's answer to the relay's ``CONNECT``.
_UPSTREAM_HEAD_S = 30.0
# A placeholder's length less one: the bytes of a streamed body in which a
# placeholder could still begin, held back until the next piece is searched.
_HELD_BACK = len(PLACEHOLDER_PREFIX) + 32 - 1
# How long a refused caller's unread body is read and dropped, so it reads
# the answer and not a reset.
_DROP_S = 5.0
#: The engines' own domains, their sign-in's among them. A session keeps
#: them direct, so an engine's own traffic never passes the exit, except
#: behind the machine's own proxy, where it passes as a plain tunnel
#: (``session_env``'s ``engines_direct``). A tool whose entry names a host
#: below one reaches that host through the exit as ``TOOL_PROXY_USER``.
ENGINE_HOSTS = ("anthropic.com", "claude.com", "openai.com", "chatgpt.com")
#: The environment the session gets (``session_env``): every client that
#: reads a proxy variable or a trust variable is pointed at the exit and its
#: bundle; ``NODE_USE_ENV_PROXY`` makes Node's own fetch read the proxy.
ENV_NAMES = (
    "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO", "NODE_USE_ENV_PROXY",
)
PROXY_USER = "oto"
#: The proxy user a tool's own ``HTTPS_PROXY`` names when one of its entries
#: declares a host under an engine's domain: the same session secret, and
#: the one connection to such a host the relay ends.
TOOL_PROXY_USER = "oto-tool"
PROXY_USERS = (PROXY_USER, TOOL_PROXY_USER)
#: Where a machine's satellite keeps its exit's trust bundle, under the
#: OtoDock folder it reports (``capabilities.otodock_dir``): the proxy
#: composes the path a machine session's trust variables name.
BUNDLE_RELPATH = "credential-exit/bundle.pem"
# The request headers never forwarded (h11 names them in lower-case bytes):
# the hop-by-hop set, the client's own host and length, which the forward
# sets again.
_HOP_BY_HOP = frozenset({
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"proxy-connection",
    b"te", b"trailer", b"trailers", b"transfer-encoding", b"upgrade", b"host", b"content-length",
})
_RESPONSE_DROP = frozenset({
    "connection", "keep-alive", "transfer-encoding", "content-length", "trailer", "upgrade",
})

REFUSAL_PLACE = "The credential may be sent only in its header"
REFUSAL_HOST = "The request names another host than the connection"
REFUSAL_PLAIN = "The exit carries encrypted connections alone"
REFUSAL_WHERE = "The exit does not reach that address"
REFUSAL_TOKEN = "The exit takes a session's own token"
REFUSAL_LARGE = "The request body is larger than the exit takes"
REFUSAL_BUSY = "The exit holds too many large requests right now. Try again in a moment."
REFUSAL_FRAMING = "The request gives its body's length in two ways"
REFUSAL_UNCHECKED = "The request could not be checked, so it was refused"
REFUSAL_CUT = "The host ended the request before its body was sent"
REFUSAL_UPSTREAM = "The proxy did not open the connection"
_PLAIN_TEXT = "text/plain; charset=utf-8"
_JSON = "application/json; charset=utf-8"


class Judged(Exception):
    """A request the relay refuses with an HTTP status and one sentence, as
    plain text or, with another ``content_type``, as a JSON message."""

    def __init__(self, status: int, detail: str, content_type: str = "") -> None:
        super().__init__(detail)
        self.status, self.detail, self.content_type = status, detail, content_type


@dataclass(frozen=True)
class Judgement:
    """A backend's word on one request of a judged entry. ``status`` with
    ``detail`` refuses it (``content_type`` as ``Judged`` takes it).
    ``wants_body`` asks for the body, read whole up to that many bytes,
    and a second ask with it. Else the request goes: with ``body`` in the
    place of the one read, and with the answer's ``Link`` header rewritten
    (``link``: a pattern and what replaces each match)."""

    status: int = 0
    detail: str = ""
    content_type: str = ""
    wants_body: int = 0
    body: bytes | None = None
    link: tuple[str, str] | None = None


class Entry(Protocol):
    """One placeholder a session holds, as the backend knows it."""

    @property
    def placeholder(self) -> str: ...
    @property
    def header(self) -> str: ...
    @property
    def prefix(self) -> str: ...
    @property
    def key(self) -> str: ...
    @property
    def hosts(self) -> tuple[str, ...]: ...
    @property
    def streams(self) -> bool:
        """A large body, or one of unknown length, passes as it arrives."""
        ...
    @property
    def judged(self) -> bool:
        """The backend is asked about each request (``Backend.judge``)."""
        ...


@dataclass(frozen=True)
class Upstream:
    """An outgoing proxy of the machine's own that a target's connection
    goes through: its scheme (``http`` or ``https``), host and port, and the
    ``Proxy-Authorization`` value its own credential makes (empty for none;
    never logged)."""

    scheme: str
    host: str
    port: int
    authorization: str = field(default="", repr=False)

    @property
    def url(self) -> str:
        """The proxy's address with no credential, as a key may name it (never
        a log: the pages promise the address reaches none)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


class UpstreamRefused(ValueError):
    """A proxy address the relay does not speak (``socks``, another scheme,
    no host)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def parse_upstream(url: str) -> Upstream | None:
    """The ``Upstream`` an address names, None for an empty one; raises
    ``UpstreamRefused`` (its ``kind``: ``socks``, ``scheme``) for one the
    relay does not speak. An address with no scheme is read as ``http``, as
    the clients read it. The user part becomes the Basic credential."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        # The parser's own words can quote the address, its credential included.
        raise UpstreamRefused("scheme") from None
    scheme = parts.scheme.lower()
    if scheme.startswith("socks"):
        raise UpstreamRefused("socks")
    if scheme not in ("http", "https") or not parts.hostname:
        raise UpstreamRefused("scheme")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UpstreamRefused("scheme") from None
    auth = ""
    if parts.username is not None:
        from urllib.parse import unquote
        pair = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        auth = "Basic " + base64.b64encode(pair).decode("ascii")
    return Upstream(scheme=scheme, host=parts.hostname, port=port, authorization=auth)


def under_engine(host: str) -> bool:
    """Whether ``host`` is one of the engines' domains or a name under one."""
    name = host.lower().rstrip(".")
    return any(name == e or name.endswith("." + e) for e in ENGINE_HOSTS)


class Backend(Protocol):
    """What a host supplies the relay with. ``upstream`` and
    ``upstream_admits`` are optional: a backend without them dials every
    target directly."""

    def session_of(self, headers: Iterable[tuple[bytes, bytes]]) -> str | None:
        """The session the ``Proxy-Authorization`` names, live, else None."""
        ...

    def credential(self, sid: str, placeholder: str) -> Entry | None:
        """The entry behind ``placeholder`` when the session holds it."""
        ...

    def takes_host(self, sid: str, host: str) -> bool:
        """Whether the session holds a placeholder whose value may go to ``host``."""
        ...

    async def value_of(self, sid: str, entry: Entry, host: str) -> str:
        """The value now, with the prefix stripped; raises ``Judged``."""
        ...

    async def addresses(self, sid: str, host: str, port: int) -> list[str] | None:
        """The addresses the target may be dialled at, None when refused."""
        ...

    def client(self, sid: str) -> httpx.AsyncClient:
        """The client the forward goes through."""
        ...

    def forward_url(self, sid: str, host: str, port: int, path: str,
                    addresses: list[str]) -> tuple[str, dict[str, Any]]:
        """The URL the forward dials and the request extensions (the SNI
        when the URL names an address)."""
        ...

    async def leaf_context(self, host: str) -> ssl.SSLContext:
        """The server-side TLS context for an ended connection toward
        ``host``: a cached leaf from memory, a new one made off the loop."""
        ...

    async def judge(self, sid: str, entry: Entry, method: str, host: str, target: str,
                    about: dict[str, str], body: bytes | None) -> Judgement:
        """The word on one request of a judged entry: ``about`` is the
        body's ``content_type`` and ``content_encoding``, ``body`` None at
        the first ask and the body read at the second. ``target`` is the
        caller's own text with no number sign. The forward removes its dot
        segments, as every HTTP client does: a judge that reads the path
        refuses one that holds a dot segment."""
        ...


def host_matches(hosts: Iterable[str], host: str) -> bool:
    """Whether ``host`` is one of ``hosts``: a literal name, or one label or
    more in front of a ``*.domain`` pattern, never the domain itself."""
    name = host.lower().rstrip(".")
    for pattern in hosts:
        p = pattern.lower()
        if p.startswith("*."):
            if name.endswith(p[1:]):
                return True
        elif name == p:
            return True
    return False


def new_placeholder() -> str:
    return PLACEHOLDER_PREFIX + secrets.token_hex(16)


def placeholders(data: bytes) -> set[bytes]:
    return set(PLACEHOLDER_RE.findall(data))


def session_env(*, port: int, credential: str, bundle_path: str, allow_hosts: Iterable[str] = (),
                direct_hosts: Iterable[str] = (), engines_direct: bool = True) -> dict[str, str]:
    """The variables a session's processes get: the exit as their HTTPS
    proxy with the session's credential, the hosts that stay direct (this
    machine, the session's own carves, the names the caller adds, and the
    engines' unless ``engines_direct`` is False: behind the machine's own
    proxy an engine's traffic passes the exit as a plain tunnel), and the
    bundle every trust variable names. The carves are addresses;
    ``direct_hosts`` carries names, since a client matches ``NO_PROXY``
    against the URL's host."""
    proxy = f"http://{PROXY_USER}:{credential}@127.0.0.1:{port}"
    engines = ENGINE_HOSTS if engines_direct else ()
    direct = ["127.0.0.1", "localhost", "::1", *allow_hosts, *direct_hosts, *engines]
    no_proxy = ",".join(dict.fromkeys(d for d in direct if d))
    return {
        "HTTPS_PROXY": proxy, "https_proxy": proxy,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        "SSL_CERT_FILE": bundle_path, "REQUESTS_CA_BUNDLE": bundle_path,
        "CURL_CA_BUNDLE": bundle_path, "NODE_EXTRA_CA_CERTS": bundle_path,
        "GIT_SSL_CAINFO": bundle_path, "NODE_USE_ENV_PROXY": "1",
    }


def basic_credential(headers: Iterable[tuple[bytes, bytes]]) -> tuple[str, str] | None:
    """The ``(user, secret)`` of a ``Proxy-Authorization: Basic`` header, None
    for no or another header."""
    raw = _header(headers, b"proxy-authorization")
    if not raw:
        return None
    scheme, _, rest = raw.strip().partition(b" ")
    if scheme.lower() != b"basic":
        return None
    try:
        user, _, secret = base64.b64decode(rest.strip(), validate=True).decode("ascii").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return user, secret


def listen_socket(port: int, *, share: bool = False) -> socket.socket:
    """A listening socket on ``127.0.0.1:<port>`` (0: the OS picks).
    ``socket.create_server`` sets ``SO_REUSEADDR`` on POSIX alone, as
    asyncio does, so a plain bind never takes a port another program holds;
    ``share`` sets it where the OS does not (Windows), for a rebind onto a
    port only this relay's old connections still hold."""
    if share:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
        except OSError:
            sock.close()
            raise
    else:
        sock = socket.create_server(("127.0.0.1", port), backlog=128)
    sock.set_inheritable(False)
    return sock


@dataclass
class _Request:
    method: str
    target: str
    headers: list[tuple[bytes, bytes]]
    body: bytes = b""


@dataclass
class _Passing:
    """A streamed body's state: whether the forward began to take it (the
    head is then with the host) and whether the caller's body was read to
    its end."""

    begun: bool = False
    done: bool = False


@dataclass
class Relay:
    """One listener on the loopback, serving the backend's sessions."""

    backend: Backend
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("otodock.credential-exit"))
    server: asyncio.AbstractServer | None = None
    sock: socket.socket | None = None
    port: int = 0
    tasks: set[asyncio.Task] = field(default_factory=set)
    # The buffered bodies held now, by session and in all.
    held: dict[str, int] = field(default_factory=dict)
    held_all: int = 0

    async def start(self, port: int = 0) -> int:
        """Listen on ``127.0.0.1`` (a port the kernel picks unless ``port``)
        and return the port."""
        if self.server is not None:
            return self.port
        sock = listen_socket(port)
        self.server = await asyncio.start_server(self._serve, sock=sock, limit=_HEAD_BYTES)
        self.sock = sock
        self.port = sock.getsockname()[1]
        return self.port

    async def rebind(self) -> bool:
        """Listen again on the port this relay had (the sessions' environments
        name it), after its socket stopped accepting; False when the port
        cannot be taken again."""
        port = self.port
        await self._close_server()
        try:
            sock = listen_socket(port, share=sys.platform == "win32")
        except OSError as e:
            self.logger.warning("credential exit: 127.0.0.1:%s cannot be bound again: %s", port, e)
            return False
        self.server = await asyncio.start_server(self._serve, sock=sock, limit=_HEAD_BYTES)
        self.sock = sock
        self.port = sock.getsockname()[1]
        return True

    def listening(self) -> bool:
        """Whether the listening socket is still open under the server."""
        return self.server is not None and self.sock is not None and self.sock.fileno() != -1

    def owns_socket(self, sock: object) -> bool:
        """Whether ``sock`` (a loop's report) is this relay's listening socket."""
        mine = self.sock
        if mine is None or not hasattr(sock, "fileno"):
            return False
        try:
            return mine.fileno() != -1 and sock.fileno() == mine.fileno()  # type: ignore[union-attr]
        except (OSError, ValueError):
            return False

    async def _close_server(self) -> None:
        server, self.server = self.server, None
        for task in list(self.tasks):
            task.cancel()
        if server is not None and self.sock is not None and self.sock.fileno() != -1:
            server.close()
            # Every connection is one of the tasks cancelled above, and its own
            # ``finally`` aborts the transport: uvloop's ``Server`` has no
            # ``abort_clients``, and ``wait_closed`` waits for every connection.
            await server.wait_closed()
        # A socket the loop closed under the server (the Proactor after a
        # failed accept) has nothing left to stop, and the server's own close
        # would wait for a wake-up that never comes: the connections are the
        # tasks cancelled above.
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None

    async def stop(self) -> None:
        """Stop listening, end every connection the relay holds, give the
        port back."""
        await self._close_server()
        self.port = 0

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)
        try:
            await self._connection(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, TimeoutError):
            pass
        except h11.RemoteProtocolError as e:
            # A caller that ends inside a body, or sends what is no HTTP: its
            # own doing and an ordinary end. The error's text can quote the
            # caller's bytes, so the log names its kind alone.
            self.logger.debug("credential exit: a caller's request ended early or could not be read (%s)",
                              type(e).__name__)
        except Exception as e:  # noqa: BLE001  # the connection's last handler: its kind is logged, never its text
            # The error's kind and where it rose, never its words: an error's
            # own text can quote what it read, a credential included.
            rose = traceback.extract_tb(e.__traceback__)[-1] if e.__traceback__ is not None else None
            self.logger.error("credential exit: a connection failed (%s at %s:%s)", type(e).__name__,
                              rose.filename.rsplit("/", 1)[-1] if rose else "?", rose.lineno if rose else "?")
        finally:
            if task is not None:
                self.tasks.discard(task)
            # Every answer was drained: nothing waits for a TLS close notice.
            with contextlib.suppress(Exception):
                writer.transport.abort()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=_HEAD_BYTES)
        event = await _next_event(conn, reader, _HEAD_TIMEOUT_S)
        if not isinstance(event, h11.Request):
            return
        sid = self.backend.session_of(event.headers)
        if sid is None:
            await _refuse(conn, writer, 407, REFUSAL_TOKEN, [(b"proxy-authenticate", b'Basic realm="OtoDock"')])
            return
        if event.method != b"CONNECT":
            self.logger.info("credential exit: a plain request refused for %s", sid[:8])
            await _refuse(conn, writer, 403, REFUSAL_PLAIN)
            return
        target = _split_target(event.target)
        if target is None:
            await _refuse(conn, writer, 400, "The connection names no host and port")
            return
        host, port = target
        user = (basic_credential(event.headers) or ("", ""))[0]
        # An engine's host is read only for a tool's own connection: the
        # engine's traffic to it passes as it came.
        if self.backend.takes_host(sid, host) and (user == TOOL_PROXY_USER or not under_engine(host)):
            await self._ended(sid, host, port, conn, reader, writer)
        else:
            await self._tunnel(sid, host, port, conn, reader, writer)

    async def _addresses(self, sid: str, host: str, port: int) -> list[str] | None:
        try:
            return await self.backend.addresses(sid, host, port)
        except (socket.gaierror, OSError):
            return None

    async def _upstream(self, sid: str, host: str, port: int) -> Upstream | None:
        """The machine's own proxy for this target, None to dial it directly."""
        ask = getattr(self.backend, "upstream", None)
        return await ask(sid, host, port) if ask is not None else None

    async def _admits(self, sid: str, host: str, port: int) -> bool:
        """Whether a target the machine's proxy will resolve may be asked of it."""
        ask = getattr(self.backend, "upstream_admits", None)
        return await ask(sid, host, port) if ask is not None else True

    async def _tunnel(self, sid: str, host: str, port: int, conn: h11.Connection,
                      reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A plain tunnel: bytes both ways to a checked address, or through
        the machine's own proxy, nothing read."""
        up = await self._upstream(sid, host, port)
        upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        early = b""
        if up is not None:
            if not await self._admits(sid, host, port):
                self.logger.info("credential exit: %s refused for %s (%s)", host, sid[:8], REFUSAL_WHERE)
                await _refuse(conn, writer, 403, REFUSAL_WHERE)
                return
            opened = await _open_through(up, host, port, self.logger)
            if opened is None:
                await _refuse(conn, writer, 502, REFUSAL_UPSTREAM)
                return
            upstream, early = (opened[0], opened[1]), opened[2]
        else:
            addresses = await self._addresses(sid, host, port)
            if not addresses:
                self.logger.info("credential exit: %s refused for %s (%s)", host, sid[:8], REFUSAL_WHERE)
                await _refuse(conn, writer, 403, REFUSAL_WHERE)
                return
            for address in addresses:
                try:
                    upstream = await asyncio.wait_for(asyncio.open_connection(address, port), _CONNECT_TIMEOUT_S)
                    break
                except (OSError, asyncio.TimeoutError, TimeoutError):
                    continue
            if upstream is None:
                await _refuse(conn, writer, 502, "The host did not answer")
                return
        up_reader, up_writer = upstream
        try:
            await _send(conn, writer, h11.Response(status_code=200, reason=b"Connection Established", headers=[]))
            if early:
                writer.write(early)
                await writer.drain()
            rest = conn.trailing_data[0]
            if rest:
                up_writer.write(rest)
                await up_writer.drain()
            await asyncio.gather(_copy(reader, up_writer), _copy(up_reader, writer))
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _ended(self, sid: str, host: str, port: int, conn: h11.Connection,
                     reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """An approved host: the connection ends here and its requests are
        judged and forwarded one by one."""
        # Nothing reaches the plain reader from here on: the client's first
        # TLS bytes can arrive while a new leaf is made, and a loop's own
        # upgrade does not hand over what the reader already took. The
        # upgrade resumes the reading itself.
        cast("asyncio.Transport", writer.transport).pause_reading()
        await _send(conn, writer, h11.Response(status_code=200, reason=b"Connection Established", headers=[]))
        if conn.trailing_data[0]:
            return  # a client that speaks before the tunnel is up is no TLS client
        try:
            # A new leaf is disk work the backend does off the loop: a make
            # that fails or outlasts the bound ends this connection alone.
            context = await asyncio.wait_for(self.backend.leaf_context(host), _HEAD_TIMEOUT_S)
            await asyncio.wait_for(_start_tls(writer, context), _HEAD_TIMEOUT_S)
        except (ssl.SSLError, OSError, asyncio.TimeoutError, TimeoutError, ValueError) as e:
            self.logger.debug("credential exit: the TLS handshake with %s's session failed: %s", sid[:8], e)
            return
        inner = h11.Connection(h11.SERVER, max_incomplete_event_size=_HEAD_BYTES)
        while True:
            event = await _next_event(inner, reader, _HEAD_TIMEOUT_S)
            if isinstance(event, h11.ConnectionClosed) or event is h11.PAUSED:
                return
            if not isinstance(event, h11.Request):
                return
            try:
                kept = await self._request(sid, host, port, event, inner, reader, writer)
            except Judged as judged:
                self.logger.info("credential exit: %s %s refused for %s: %s", event.method.decode("ascii"), host,
                                 sid[:8], judged.detail)
                await _refuse(inner, writer, judged.status, judged.detail, content_type=judged.content_type,
                              as_json=_accepts_json(event.headers))
                kept = False
            if not kept or inner.our_state is h11.MUST_CLOSE or inner.their_state is h11.MUST_CLOSE:
                await _drop_rest(inner, reader)
                return
            try:
                inner.start_next_cycle()
            except h11.LocalProtocolError:
                return

    async def _request(self, sid: str, host: str, port: int, first: h11.Request, conn: h11.Connection,
                       reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """One request of an ended connection: its head checked and judged,
        its body read whole or passed as it arrives, the forward. False when
        the connection must end after it; raises ``Judged``."""
        req = _Request(first.method.decode("ascii"), first.target.decode("latin-1"), list(first.headers))
        if _host_of(req.headers, port) != host:
            raise Judged(400, REFUSAL_HOST)
        if _header(req.headers, b"upgrade") is not None:
            raise Judged(501, "The exit carries no upgraded connection")
        # The forward's address is parsed again, and that parser ends a target
        # at a number sign: what was checked and judged would not be what is
        # sent.
        if not req.target.startswith("/") or "#" in req.target:
            raise Judged(400, "The request names no path")
        # The name was read out of this header and the header is sent on as it
        # came, so it is searched as a body is.
        self._search(sid, _header(req.headers, b"host") or b"")
        mine = self._placed(sid, host, req)
        entries = list({id(e): e for e in mine.values()}.values())
        judges = [e for e in entries if getattr(e, "judged", False)]
        # Every value of each header, joined as HTTP joins a repeated one: a
        # judge reads a body by its type, and two types are no type it knows.
        about = {"content_type": _joined(req.headers, b"content-type"),
                 "content_encoding": _joined(req.headers, b"content-encoding")}
        wants, link = 0, None
        for entry in judges:
            word = await self.backend.judge(sid, entry, req.method, host, req.target, about, None)
            if word.status:
                raise Judged(word.status, word.detail, word.content_type)
            wants = max(wants, word.wants_body)
            link = word.link or link
        chunked = _header(req.headers, b"transfer-encoding") is not None
        length = _length(req.headers)
        taken = [0]
        try:
            if wants:
                req.body = await self._read_body(sid, conn, reader, writer, min(wants, MAX_BODY_BYTES), length,
                                                 taken)
                for entry in judges:
                    word = await self.backend.judge(sid, entry, req.method, host, req.target, about, req.body)
                    if word.status or word.wants_body:
                        raise Judged(word.status or 403, word.detail or REFUSAL_UNCHECKED, word.content_type)
                    if word.body is not None:
                        req.body = word.body
                    link = word.link or link
            elif any(getattr(e, "streams", False) for e in entries) and (chunked or (length or 0) > STREAM_FROM):
                if chunked and length is not None:
                    raise Judged(400, REFUSAL_FRAMING)
                return await self._forward(sid, host, port, req, mine, conn, writer, link=link,
                                           passing=_Passing(), reader=reader, length=length)
            else:
                req.body = await self._read_body(sid, conn, reader, writer, MAX_BODY_BYTES, length, taken)
            self._search(sid, req.body)
            return await self._forward(sid, host, port, req, mine, conn, writer, link=link)
        finally:
            self._give(sid, taken[0])

    def _take(self, sid: str, size: int) -> bool:
        """Count ``size`` buffered bytes against the session's share and the
        relay's budget; False when either would be passed."""
        if self.held_all + size > BUFFER_BUDGET or self.held.get(sid, 0) + size > SESSION_BUFFER:
            return False
        self.held[sid] = self.held.get(sid, 0) + size
        self.held_all += size
        return True

    def _give(self, sid: str, size: int) -> None:
        if not size:
            return
        left = self.held.get(sid, 0) - size
        if left > 0:
            self.held[sid] = left
        else:
            self.held.pop(sid, None)
        self.held_all -= size

    async def _read_body(self, sid: str, conn: h11.Connection, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter, cap: int, length: int | None, taken: list[int]) -> bytes:
        """The whole body, up to ``cap`` (413 past it, at once for a length
        the head announces) and inside the budget (503 past it); what it
        took of the budget is added to ``taken`` for the caller to give
        back."""
        if length is not None and length > cap:
            raise Judged(413, REFUSAL_LARGE)
        if conn.they_are_waiting_for_100_continue:
            await _send(conn, writer, h11.InformationalResponse(status_code=100, headers=[]))
        chunks: list[bytes] = []
        total = 0
        while True:
            event = await _next_event(conn, reader, _HEAD_TIMEOUT_S)
            if isinstance(event, h11.Data):
                size = len(event.data)
                total += size
                if total > cap:
                    raise Judged(413, REFUSAL_LARGE)
                if not self._take(sid, size):
                    raise Judged(503, REFUSAL_BUSY)
                taken[0] += size
                chunks.append(bytes(event.data))
            elif isinstance(event, h11.EndOfMessage):
                return b"".join(chunks)
            else:
                raise ConnectionError("closed")

    def _placed(self, sid: str, host: str, req: _Request) -> dict[bytes, Entry]:
        """The session's placeholders in the request's head, each with its
        entry: none in the target, and each in its credential's own header
        toward an approved host; raises ``Judged``."""
        in_target = placeholders(req.target.encode("latin-1"))
        found = set(in_target)
        for k, v in req.headers:
            if k.lower() in _HOP_BY_HOP:
                continue
            found |= placeholders(v)
            basic = _basic_parts(v) if k.lower() == b"authorization" else None
            if basic is not None:
                found |= placeholders(basic[1])
        mine = {p: c for p in found if (c := self.backend.credential(sid, p.decode())) is not None}
        if in_target & set(mine):
            raise Judged(400, REFUSAL_PLACE)
        for k, v in req.headers:
            if k.lower() in _HOP_BY_HOP:
                continue
            basic = _basic_parts(v) if k.lower() == b"authorization" else None
            for p in placeholders(basic[1] if basic is not None else v) & set(mine):
                cred = mine[p]
                if cred.header.lower().encode() != k.lower() or not host_matches(cred.hosts, host):
                    raise Judged(400, REFUSAL_PLACE)
        return mine

    def _search(self, sid: str, data: bytes) -> None:
        """Refuse a body, or a piece of one, that holds a placeholder of the
        session: the value goes only into its header."""
        for p in placeholders(data):
            if self.backend.credential(sid, p.decode()) is not None:
                raise Judged(400, REFUSAL_PLACE)

    async def _swap(self, sid: str, host: str, req: _Request,
                    mine: dict[bytes, Entry]) -> tuple[list[tuple[bytes, bytes]], str]:
        """The request's headers with the session's placeholders (``_placed``)
        swapped for their values, and the keys swapped (for the log)."""
        out: list[tuple[bytes, bytes]] = []
        keys: list[str] = []
        for k, v in req.headers:
            if k.lower() in _HOP_BY_HOP:
                continue
            basic = _basic_parts(v) if k.lower() == b"authorization" else None
            text = basic[1] if basic is not None else v
            here = placeholders(text) & set(mine)
            if not here:
                out.append((k, v))
                continue
            for p in here:
                cred = mine[p]
                value = await self.backend.value_of(sid, cred, host)
                text = text.replace(p, value.encode("utf-8"))
                keys.append(cred.key)
            out.append((k, basic[0] + b" " + base64.b64encode(text) if basic is not None else text))
        return out, ",".join(sorted(set(keys)))

    async def _passed(self, sid: str, conn: h11.Connection, reader: asyncio.StreamReader, passing: _Passing):
        """A streamed body, piece by piece as the caller sends it. Each
        piece is joined to the bytes not yet sent and the join searched; all
        but its last ``_HELD_BACK`` bytes go on, so no byte of a placeholder
        that is found leaves, however small the pieces."""
        carry = b""
        passing.begun = True
        while True:
            event = await _next_event(conn, reader, _HEAD_TIMEOUT_S)
            if isinstance(event, h11.Data):
                data = carry + bytes(event.data)
                self._search(sid, data)
                if len(data) > _HELD_BACK:
                    yield data[:-_HELD_BACK]
                    carry = data[-_HELD_BACK:]
                else:
                    carry = data
            elif isinstance(event, h11.EndOfMessage):
                if carry:
                    yield carry
                passing.done = True
                return
            else:
                raise ConnectionError("closed")

    async def _forward(self, sid: str, host: str, port: int, req: _Request, mine: dict[bytes, Entry],
                       conn: h11.Connection, writer: asyncio.StreamWriter, *,
                       link: tuple[str, str] | None = None, passing: _Passing | None = None,
                       reader: asyncio.StreamReader | None = None, length: int | None = None) -> bool:
        """Send the request on and its answer back. With ``passing`` the body
        is the caller's own, passed as it arrives (``_passed``) under the
        caller's framing: its length when the head announced one, else
        chunked. False when the connection must end: the host answered
        before the caller's body did."""
        headers, keys = await self._swap(sid, host, req, mine)
        up = await self._upstream(sid, host, port)
        if up is not None:
            # The machine's own proxy resolves the name: the URL names the
            # host, and the target was judged instead of pinned.
            if not await self._admits(sid, host, port):
                raise Judged(403, REFUSAL_WHERE)
            url: str = (f"https://{host}:{port}{req.target}" if port != 443 else f"https://{host}{req.target}")
            extensions: dict[str, Any] = {}
            client = cast("Any", self.backend).client(sid, up)
        else:
            addresses = await self._addresses(sid, host, port)
            if not addresses:
                raise Judged(403, REFUSAL_WHERE)
            url, extensions = self.backend.forward_url(sid, host, port, req.target, addresses)
            client = self.backend.client(sid)
        # The host as the request named it, whatever the URL dials.
        headers.append((b"host", _header(req.headers, b"host") or host.encode("ascii")))
        content: Any = req.body
        if passing is not None and reader is not None:
            # The relay answers the caller's expectation itself.
            headers = [(k, v) for k, v in headers if k.lower() != b"expect"]
            if length is not None:
                headers.append((b"content-length", str(length).encode("ascii")))
            if conn.they_are_waiting_for_100_continue:
                await _send(conn, writer, h11.InformationalResponse(status_code=100, headers=[]))
            content = self._passed(sid, conn, reader, passing)
        request = client.build_request(req.method, url, headers=headers, content=content, extensions=extensions)
        started = time.monotonic()
        cut = passing is not None
        try:
            try:
                upstream = await client.send(request, stream=True)
            except httpx.ProxyError as e:
                # The machine's own proxy did not open the connection.
                self.logger.info("credential exit: the proxy refused a request of %s to %s: %s", sid[:8], host,
                                 type(e).__name__)
                raise Judged(502, REFUSAL_UPSTREAM) from None
            except httpx.HTTPError as e:
                if passing is not None and passing.begun and not passing.done:
                    # The host took the head and ended the connection inside
                    # the body: whatever it answered went with the connection.
                    self.logger.info("credential exit: %s ended a request of %s before its body was sent: %s",
                                     host, sid[:8], type(e).__name__)
                    raise Judged(502, REFUSAL_CUT) from None
                self.logger.info("credential exit: %s did not answer for %s: %s", host, sid[:8], type(e).__name__)
                raise Judged(502, "The host did not answer") from None
            cut = passing is not None and not passing.done
        finally:
            if passing is not None and cut:
                # A body the forward left before its end is closed here: the
                # caller's remaining bytes are dropped by the connection.
                with contextlib.suppress(Exception):
                    await content.aclose()
        if keys:
            self.logger.info("credential exit: %s %s for %s, the value of %s added", req.method, host, sid[:8], keys)
        try:
            out = [(k.encode("latin-1"), _linked(k, v, link).encode("latin-1"))
                   for k, v in upstream.headers.multi_items() if k.lower() not in _RESPONSE_DROP]
            if cut:
                out.append((b"connection", b"close"))
            await _send(conn, writer, h11.Response(status_code=upstream.status_code, headers=out))
            if req.method != "HEAD" and upstream.status_code not in (204, 304):
                async for chunk in upstream.aiter_raw():
                    if chunk:
                        await _send(conn, writer, h11.Data(data=chunk))
            await _send(conn, writer, h11.EndOfMessage())
        finally:
            await upstream.aclose()
            self.logger.debug("credential exit: %s %s for %s in %.0f ms", req.method, host, sid[:8],
                              (time.monotonic() - started) * 1000)
        return not cut


def _proxy_context() -> ssl.SSLContext:
    """The check of an ``https://`` proxy's own certificate: the machine's
    system store."""
    return ssl.create_default_context()


async def _open_through(up: Upstream, host: str, port: int, logger: logging.Logger
                        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes] | None:
    """A connection to ``host:port`` through the machine's own proxy: its
    ``CONNECT`` with the proxy's own credential, the answer read with a
    bound; the streams and any bytes the proxy sent after its head, or None
    (logged with the proxy's status, never its address, its body or a
    credential)."""
    context = _proxy_context() if up.scheme == "https" else None
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(up.host, up.port, ssl=context,
                                    server_hostname=up.host if context is not None else None),
            _CONNECT_TIMEOUT_S)
    except (OSError, ssl.SSLError, asyncio.TimeoutError, TimeoutError) as exc:
        logger.info("credential exit: the machine's proxy did not answer for %s: %s", host, type(exc).__name__)
        return None
    client = h11.Connection(h11.CLIENT, max_incomplete_event_size=_HEAD_BYTES)
    status = 0
    opened = False
    try:
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        head = [(b"host", authority.encode("ascii"))]
        if up.authorization:
            head.append((b"proxy-authorization", up.authorization.encode("ascii")))
        up_writer.write(client.send(h11.Request(method=b"CONNECT", target=authority.encode("ascii"), headers=head)))
        await up_writer.drain()
        while True:
            event = client.next_event()
            if event is h11.NEED_DATA:
                data = await asyncio.wait_for(up_reader.read(_CHUNK), _UPSTREAM_HEAD_S)
                client.receive_data(data)
                if not data:
                    break
                continue
            if isinstance(event, h11.InformationalResponse):
                continue
            if isinstance(event, h11.Response):
                status = event.status_code
            break
        if 200 <= status < 300:
            opened = True
            return up_reader, up_writer, client.trailing_data[0]
        logger.info("credential exit: the machine's proxy answered %s for %s", status or "nothing", host)
        return None
    except (OSError, ValueError, h11.ProtocolError, asyncio.TimeoutError, TimeoutError) as exc:
        logger.info("credential exit: the machine's proxy failed the connection to %s: %s", host,
                    type(exc).__name__)
        return None
    finally:
        # Every way out but an open tunnel closes the proxy's connection.
        if not opened:
            with contextlib.suppress(Exception):
                up_writer.close()


async def _start_tls(writer: asyncio.StreamWriter, context: ssl.SSLContext) -> None:
    """The server side of the TLS upgrade. 3.11 and later have
    ``StreamWriter.start_tls``; the satellite's 3.10 floor does not, so the
    loop's ``start_tls`` is called as 3.11 does it: the writer's and the
    protocol's transport become the TLS one, the reader keeps the raw
    transport for its flow control. A short shutdown wait: a client that
    drops the connection without a close notice must not hold the handler
    for the default 30 s."""
    if sys.version_info >= (3, 11):
        await writer.start_tls(context, ssl_shutdown_timeout=1.0)
        return
    loop = asyncio.get_running_loop()
    protocol = writer.transport.get_protocol()
    await writer.drain()
    transport = await loop.start_tls(writer.transport, protocol, context, server_side=True)
    writer._transport = transport  # type: ignore[attr-defined]  # 3.11's start_tls does the same
    protocol._stream_writer = writer  # type: ignore[attr-defined]
    protocol._transport = transport  # type: ignore[attr-defined]
    protocol._over_ssl = True  # type: ignore[attr-defined]


async def _next_event(conn: h11.Connection, reader: asyncio.StreamReader, timeout: float | None):
    """The next h11 event, feeding the connection from ``reader``."""
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            data = await asyncio.wait_for(reader.read(_CHUNK), timeout) if timeout else await reader.read(_CHUNK)
            conn.receive_data(data)
            if not data and conn.next_event() is h11.NEED_DATA:
                raise ConnectionError("closed")
            continue
        return event


async def _send(conn: h11.Connection, writer: asyncio.StreamWriter, *events) -> None:
    for event in events:
        data = conn.send(event)
        if data:
            writer.write(data)
    await writer.drain()


async def _refuse(conn: h11.Connection, writer: asyncio.StreamWriter, status: int, detail: str,
                  extra: Iterable[tuple[bytes, bytes]] = (), *, content_type: str = "",
                  as_json: bool = False) -> None:
    """Answer a refusal and ask for the connection's end: the sentence as
    plain text, or as ``{"message": …}`` under the refusal's own
    ``content_type`` when it names one that is no text, else under JSON when
    the request accepts JSON (``as_json``): a client of an API shows a JSON
    message and nothing of a plain text."""
    content_type = content_type or (_JSON if as_json else _PLAIN_TEXT)
    if not content_type.startswith("text/"):
        body = json.dumps({"message": detail}).encode()
    else:
        body = (detail + "\n").encode()
    headers = [(b"content-type", content_type.encode("latin-1")),
               (b"content-length", str(len(body)).encode()), (b"connection", b"close"), *extra]
    with contextlib.suppress(h11.LocalProtocolError):
        await _send(conn, writer, h11.Response(status_code=status, headers=headers), h11.Data(data=body),
                    h11.EndOfMessage())


async def _drop_rest(conn: h11.Connection, reader: asyncio.StreamReader) -> None:
    """Read and drop what a caller still sends of a request the relay
    answered before its end, for a short bound: a connection ended under a
    caller that still writes reads as a reset, never as the answer."""
    if conn.their_state is not h11.SEND_BODY:
        return
    loop = asyncio.get_running_loop()
    end = loop.time() + _DROP_S
    with contextlib.suppress(ConnectionError, asyncio.TimeoutError, TimeoutError, OSError):
        while (left := end - loop.time()) > 0:
            if not await asyncio.wait_for(reader.read(_CHUNK), left):
                return


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


def _joined(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str:
    """Every value of the header ``name``, joined with a comma."""
    return b", ".join(v for k, v in headers if k.lower() == name).decode("latin-1")


def _accepts_json(headers: Iterable[tuple[bytes, bytes]]) -> bool:
    """Whether the request's ``Accept`` names a JSON type."""
    return any(k.lower() == b"accept" and b"json" in v.lower() for k, v in headers)


def _length(headers: Iterable[tuple[bytes, bytes]]) -> int | None:
    """The body's length as the head announces it, None when it names none."""
    raw = _header(headers, b"content-length")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _linked(name: str, value: str, link: tuple[str, str] | None) -> str:
    """An answer's header value, the ``Link`` header rewritten as a judge
    asked (a pattern and what replaces each match)."""
    if link is None or name.lower() != "link":
        return value
    try:
        return re.sub(link[0], lambda _m: link[1], value)
    except re.error:
        return value


def _header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def _split_target(target: bytes) -> tuple[str, int] | None:
    """``host:port`` of a CONNECT target (an IPv6 literal in brackets); None
    for one that is not plain ASCII."""
    try:
        text = target.decode("ascii")
        parts = urlsplit("//" + text)
        host, port = parts.hostname, parts.port
    except ValueError:  # UnicodeDecodeError among them
        return None
    if not host or not port:
        return None
    return host.lower().rstrip("."), port


def _host_of(headers: Iterable[tuple[bytes, bytes]], port: int) -> str:
    raw = _header(headers, b"host") or b""
    text = raw.decode("latin-1").strip()
    try:
        parts = urlsplit("//" + text)
        host = (parts.hostname or "").lower().rstrip(".")
        given = parts.port
    except ValueError:
        return ""
    if given is not None and given != port:
        return ""
    return host


def _basic_parts(value: bytes) -> tuple[bytes, bytes] | None:
    scheme, _, rest = value.strip().partition(b" ")
    if scheme.lower() != b"basic":
        return None
    try:
        return scheme, base64.b64decode(rest.strip(), validate=True)
    except ValueError:
        return None


def self_hash() -> str:
    """SHA256 of this module's source, for the satellite's drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


__all__ = [
    "BUFFER_BUDGET", "BUNDLE_RELPATH", "ENGINE_HOSTS", "ENV_NAMES", "MAX_BODY_BYTES", "PLACEHOLDER_PREFIX",
    "PLACEHOLDER_RE", "PROXY_USER", "PROXY_USERS", "REFUSAL_BUSY", "REFUSAL_CUT", "REFUSAL_FRAMING", "REFUSAL_HOST",
    "REFUSAL_LARGE", "REFUSAL_PLACE", "REFUSAL_PLAIN", "REFUSAL_TOKEN", "REFUSAL_UPSTREAM", "REFUSAL_WHERE",
    "SESSION_BUFFER", "STREAM_FROM", "TOOL_PROXY_USER", "Backend", "Entry", "Judged", "Judgement", "Relay",
    "Upstream", "UpstreamRefused", "basic_credential", "host_matches", "listen_socket", "new_placeholder",
    "parse_upstream", "placeholders", "self_hash", "session_env", "under_engine",
]
