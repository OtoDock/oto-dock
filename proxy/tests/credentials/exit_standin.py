"""A stand-in for the relay's tests (``core/credentials/exit_relay.py``): a
backend of the tests' own, a vendor on loopback that counts what it is sent
without holding it, and a raw caller that frames its own body. The relay is
the real one; nothing leaves loopback.

``MachineExit`` is a second backend, for the tests of the platform's half of
a machine's exit (``core/credentials/credential_exit.py``): the relay as a
compute machine's satellite runs it, every answer but the door's own taken
from the platform.

``UpstreamProxy`` stands in for a machine's own outgoing proxy, ``Origin``
for a vendor under a name of the test's own, and ``ProxiedBackend`` is the
first backend behind such a proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import socket
import ssl
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import h11
import httpx

from core.credentials import exit_authority, exit_relay

HOST = "localhost"
VALUE = "standin-value-0123456789abcdef"


@dataclass(frozen=True)
class Entry:
    placeholder: str
    sid: str
    header: str = "Authorization"
    prefix: str = ""
    key: str = "STANDIN_KEY"
    hosts: tuple[str, ...] = (HOST,)
    streams: bool = False
    judged: bool = False


@dataclass
class Seen:
    """One request as the vendor received it: its head, how many body bytes
    and their digest, and whether it arrived to its end."""

    method: str
    target: str
    headers: dict[str, str]
    size: int = 0
    digest: str = ""
    complete: bool = False
    only: bytes = b""


class Vendor:
    """An HTTP/1.1 server over TLS on loopback. ``answer`` builds the answer
    to a complete request; ``early`` (a status) answers after the head and
    closes; ``hold`` keeps every answer until it is set."""

    def __init__(self, authority: exit_authority.ExitAuthority) -> None:
        self.authority = authority
        self.seen: list[Seen] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.early = 0
        self.hold: asyncio.Event | None = None
        self.extra: list[tuple[bytes, bytes]] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0,
                                                 ssl=self.authority.leaf_context(HOST))
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server.abort_clients()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER)
        try:
            while True:
                seen: Seen | None = None
                digest = hashlib.sha256()
                kinds: set[int] = set()
                while True:
                    event = conn.next_event()
                    if event is h11.NEED_DATA:
                        data = await reader.read(65536)
                        conn.receive_data(data)
                        if not data:
                            return
                        continue
                    if isinstance(event, h11.ConnectionClosed):
                        return
                    if isinstance(event, h11.Request):
                        seen = Seen(event.method.decode(), event.target.decode(),
                                    {k.decode().lower(): v.decode() for k, v in event.headers})
                        self.seen.append(seen)
                        if self.early:
                            writer.write(conn.send(h11.Response(status_code=self.early, headers=[
                                (b"content-length", b"0"), (b"connection", b"close")])))
                            writer.write(conn.send(h11.EndOfMessage()))
                            await writer.drain()
                            return
                    elif isinstance(event, h11.Data) and seen is not None:
                        seen.size += len(event.data)
                        digest.update(event.data)
                        kinds.update(bytes(event.data))
                    elif isinstance(event, h11.EndOfMessage) and seen is not None:
                        seen.complete, seen.digest, seen.only = True, digest.hexdigest(), bytes(sorted(kinds))
                        break
                    if seen is not None:
                        seen.only = bytes(sorted(kinds))
                if self.hold is not None:
                    await self.hold.wait()
                payload = b'{"ok": true}'
                writer.write(conn.send(h11.Response(status_code=200, headers=[
                    (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                    *self.extra])))
                writer.write(conn.send(h11.Data(data=payload)) + conn.send(h11.EndOfMessage()))
                await writer.drain()
                if conn.our_state is h11.MUST_CLOSE:
                    return
                conn.start_next_cycle()
        except (ConnectionError, h11.ProtocolError):
            return
        finally:
            writer.close()


@dataclass
class Backend:
    """The relay's backend for one test: sessions are admitted by their id
    as the proxy secret, every forward goes to the vendor on loopback."""

    authority: exit_authority.ExitAuthority
    vendor: Vendor
    entries: dict[str, Entry] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    asked: list[tuple] = field(default_factory=list)
    words: list = field(default_factory=list)
    http: httpx.AsyncClient | None = None

    def hold(self, *, streams: bool = False, judged: bool = False, sid: str = "",
             hosts: tuple[str, ...] = (HOST,)) -> Entry:
        """A session's new placeholder and its entry."""
        entry = Entry(placeholder=exit_relay.new_placeholder(), sid=sid or str(uuid.uuid4()),
                      streams=streams, judged=judged, hosts=hosts)
        self.entries[entry.placeholder] = entry
        return entry

    def session_of(self, headers) -> str | None:
        parsed = exit_relay.basic_credential(headers)
        if parsed is None or parsed[0] != exit_relay.PROXY_USER:
            return None
        return parsed[1] if any(e.sid == parsed[1] for e in self.entries.values()) else None

    def credential(self, sid: str, placeholder: str):
        entry = self.entries.get(placeholder)
        return entry if entry is not None and entry.sid == sid else None

    def takes_host(self, sid: str, host: str) -> bool:
        return host == HOST

    async def value_of(self, sid: str, entry, host: str) -> str:
        self.order.append("value")
        return VALUE

    async def addresses(self, sid: str, host: str, port: int) -> list[str] | None:
        return ["127.0.0.1"]

    def client(self, sid: str) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(
                verify=str(self.authority.folder / exit_authority.CERT_NAME), follow_redirects=False,
                trust_env=False, timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0))
        return self.http

    def forward_url(self, sid: str, host: str, port: int, path: str, addresses: list[str]):
        return f"https://127.0.0.1:{self.vendor.port}{path}", {"sni_hostname": HOST}

    async def leaf_context(self, host: str) -> ssl.SSLContext:
        return self.authority.leaf_context(host)

    async def judge(self, sid: str, entry, method: str, host: str, target: str, about: dict,
                    body: bytes | None):
        self.order.append("judge")
        self.asked.append((entry.placeholder, method, host, target, dict(about), body))
        return self.words.pop(0) if self.words else exit_relay.Judgement()


def caller(port: int, sid: str, bundle: Path) -> httpx.AsyncClient:
    """A caller that reaches the vendor's name through the relay."""
    return httpx.AsyncClient(proxy=f"http://{exit_relay.PROXY_USER}:{sid}@127.0.0.1:{port}", verify=str(bundle),
                             follow_redirects=False, trust_env=False, timeout=30.0)


async def raw_caller(port: int, sid: str, bundle: Path, target_port: int):
    """A TLS connection to the vendor's name through the relay, for a caller
    that writes its own bytes: ``(reader, writer)`` after the tunnel is up."""
    import base64
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    auth = base64.b64encode(f"{exit_relay.PROXY_USER}:{sid}".encode()).decode()
    writer.write(f"CONNECT {HOST}:{target_port} HTTP/1.1\r\nHost: {HOST}:{target_port}\r\n"
                 f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head
    context = ssl.create_default_context(cafile=str(bundle))
    await writer.start_tls(context, server_hostname=HOST)
    return reader, writer


async def answer_of(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    """The status, headers and body of one answer on a raw connection."""
    conn = h11.Connection(h11.CLIENT)
    conn.send(h11.Request(method="GET", target="/", headers=[("host", HOST)]))
    conn.send(h11.EndOfMessage())
    status, headers, body = 0, {}, b""
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            data = await reader.read(65536)
            conn.receive_data(data)
            continue
        if isinstance(event, h11.Response):
            status = event.status_code
            headers = {k.decode().lower(): v.decode() for k, v in event.headers}
        elif isinstance(event, h11.Data):
            body += bytes(event.data)
        elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
            return status, headers, body


@dataclass
class MachineExit:
    """A compute machine's exit over the platform's half of it, as the
    machine's satellite asks that half. A session is admitted by the secret
    of the door its start gave it (``credential_exit.machine_block``, the
    secret in ``Proxy-Authorization: Basic oto:<secret>``); its placeholders
    are the broker's and their values ``credential_exit.exit_resolution``'s;
    a judged entry is judged by ``credential_exit.judge_request``; a target
    is dialled at the addresses the sandbox's rule lets through with the
    door's carves (``outbound_url.sandbox_dial_refused``), the forward pinned
    to the first of them; a new leaf is made on the exit's own thread.
    ``dial_refused`` takes the rule's place for a test whose vendor sits on
    loopback, which the rule never lets through."""

    authority: exit_authority.ExitAuthority
    dial_refused: Callable[[str, frozenset[str]], bool] | None = None
    onlink: tuple = ()
    doors: dict[str, dict] = field(default_factory=dict)
    opened: list[str] = field(default_factory=list)
    clients: dict[str, httpx.AsyncClient] = field(default_factory=dict)
    relay: exit_relay.Relay | None = None
    port: int = 0
    pool: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="credential-exit"))

    async def start(self) -> int:
        """The relay over this backend on loopback; its port."""
        from core.credentials import credential_exit
        self.relay = exit_relay.Relay(self, logger=credential_exit.logger)
        self.port = await self.relay.start()
        return self.port

    async def stop(self) -> None:
        """The relay stopped, every client closed, and every session this
        exit opened forgotten at the platform."""
        from core.credentials import credential_exit
        if self.relay is not None:
            await self.relay.stop()
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)
        for sid in self.opened:
            credential_exit.forget(sid)
        self.doors.clear()

    async def open(self, sid: str, carves: Iterable[str] = (), *, direct_hosts: Iterable[str] = ()) -> str:
        """A compute session's start: the platform's half of its exit
        attached (``attach_sandbox``) and the door its start payload carries
        given to this exit; the secret the session's ``HTTPS_PROXY`` names."""
        from core import placement
        from core.credentials import credential_exit, mcp_broker
        machine = placement.PlacementCapabilities(
            kind=placement.KIND_COMPUTE, machine_id="c-standin", exit_port=41112, exit_ok=True,
            satellite_version="0.5.196")
        builder = SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=tuple(carves)), exit_port=0, ssh_agent="")
        env: dict[str, str] = {}
        await credential_exit.attach_sandbox(sid, builder, env, "", sandbox_config_dir="/workspace/.claude",
                                             direct_hosts=direct_hosts, machine=machine)
        block = credential_exit.machine_block(sid, mcp_broker.bundles_of(sid), "")
        assert block is not None and env["HTTPS_PROXY"].startswith(f"http://{exit_relay.PROXY_USER}:{block['secret']}@")
        self.opened.append(sid)
        self.doors[sid] = block
        return block["secret"]

    def close(self, sid: str) -> None:
        """The session's stop on the machine: its door goes."""
        self.doors.pop(sid, None)

    def session_of(self, headers) -> str | None:
        parsed = exit_relay.basic_credential(headers)
        if parsed is None or parsed[0] != exit_relay.PROXY_USER:
            return None
        for sid, door in list(self.doors.items()):
            if hmac.compare_digest(parsed[1].encode("utf-8"), str(door["secret"]).encode("utf-8")):
                return sid
        return None

    def credential(self, sid: str, placeholder: str):
        from core.credentials import mcp_broker
        return mcp_broker.exit_credential(sid, placeholder)

    def takes_host(self, sid: str, host: str) -> bool:
        from core.credentials import mcp_broker
        return mcp_broker.exit_takes_host(sid, host)

    async def value_of(self, sid: str, entry, host: str) -> str:
        from core.credentials import credential_exit
        value, _expires_in = await asyncio.to_thread(credential_exit.exit_resolution, entry, host)
        return value

    async def addresses(self, sid: str, host: str, port: int) -> list[str] | None:
        from services.infra import outbound_url
        door = self.doors.get(sid)
        carves = frozenset(door["carves"] if door is not None else ())
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        # IPv4 first: the vendors listen on 127.0.0.1 alone.
        found = sorted(dict.fromkeys(str(info[4][0]) for info in infos), key=lambda address: ":" in address)

        def refused(address: str) -> bool:
            if self.dial_refused is not None:
                return self.dial_refused(address, carves)
            return outbound_url.sandbox_dial_refused(address, carves=carves, onlink=self.onlink)
        return [address for address in found if not refused(address)] or None

    def client(self, sid: str) -> httpx.AsyncClient:
        if sid not in self.clients:
            trust = ssl.create_default_context(cafile=str(self.authority.folder / exit_authority.CERT_NAME))
            self.clients[sid] = httpx.AsyncClient(
                verify=trust, follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0))
        return self.clients[sid]

    def forward_url(self, sid: str, host: str, port: int, path: str, addresses: list[str]):
        address = addresses[0]
        literal = f"[{address}]" if ":" in address else address
        return f"https://{literal}:{port}{path}", {"sni_hostname": host}

    async def leaf_context(self, host: str) -> ssl.SSLContext:
        cached = self.authority.cached_context(host)
        if cached is not None:
            return cached
        return await asyncio.get_running_loop().run_in_executor(self.pool, self.authority.leaf_context, host)

    async def judge(self, sid: str, entry, method: str, host: str, target: str, about: dict,
                    body: bytes | None):
        from core.credentials import credential_exit
        return credential_exit.judge_request(sid, entry.placeholder, method, host, target, about, body)


@dataclass
class Head:
    """One request head the stand-in proxy read: its method, its target and
    its headers (the names in lower case)."""

    method: str
    target: str
    headers: dict[str, str]


class UpstreamProxy:
    """A machine's own outgoing proxy, on loopback. It records the head of
    every request it reads (``heads``) and answers as ``answer`` says:
    ``200`` opens the tunnel, to ``127.0.0.1`` at the target's port whatever
    the target names, so nothing leaves loopback; ``407`` and ``403`` refuse
    with a body of its own; ``close`` ends the connection unanswered. What
    passed its tunnels is kept, toward the target and back, for a test of
    what a proxy could read. Started and stopped by the test that needs it
    (``async with UpstreamProxy() as proxy``)."""

    def __init__(self, answer: str = "200") -> None:
        self.answer = answer
        self.heads: list[Head] = []
        self.toward = bytearray()
        self.back = bytearray()
        self.connections = 0
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    @property
    def url(self) -> str:
        """The proxy's address, with no credential."""
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> UpstreamProxy:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()
            server.abort_clients()
            await server.wait_closed()

    async def __aenter__(self) -> UpstreamProxy:
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        origin: asyncio.StreamWriter | None = None
        try:
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                return
            line, *lines = raw.decode("latin-1").rstrip("\r\n").split("\r\n")
            method, target, _version = line.split(" ", 2)
            headers: dict[str, str] = {}
            for text in lines:
                name, _, value = text.partition(":")
                headers[name.strip().lower()] = value.strip()
            self.heads.append(Head(method, target, headers))
            if self.answer == "close":
                return
            if self.answer != "200" or method != "CONNECT":
                status = {"407": b"407 Proxy Authentication Required", "403": b"403 Forbidden"}.get(
                    self.answer, b"405 Method Not Allowed")
                extra = b'Proxy-Authenticate: Basic realm="standin"\r\n' if self.answer == "407" else b""
                body = b"standin proxy refused\n"
                writer.write(b"HTTP/1.1 " + status + b"\r\n" + extra + b"Content-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
                return
            try:
                origin_reader, origin = await asyncio.open_connection("127.0.0.1", int(target.rsplit(":", 1)[1]))
            except (OSError, ValueError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, origin, self.toward), _pipe(origin_reader, writer, self.back))
        except (ConnectionError, OSError):
            return
        finally:
            if origin is not None:
                origin.close()
            writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kept: bytearray) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            kept.extend(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


class Origin(Vendor):
    """``Vendor`` under a name of the test's own: its leaf is signed for
    ``name``."""

    def __init__(self, authority: exit_authority.ExitAuthority, name: str = HOST) -> None:
        super().__init__(authority)
        self.name = name

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0,
                                                 ssl=self.authority.leaf_context(self.name))
        self.port = self.server.sockets[0].getsockname()[1]


@dataclass
class ProxiedBackend(Backend):
    """``Backend`` on a machine behind its own outgoing proxy. ``up`` is the
    proxy every target goes through but the names in ``bypass``, which are
    dialled at ``127.0.0.1``; ``admits`` is the word on a target the proxy
    will resolve; ``ended`` names the hosts the session holds a placeholder
    for. Every user of ``exit_relay.PROXY_USERS`` is admitted by the
    session's id. Each ask of the way is kept in ``ways`` (``upstream``,
    ``admits``, ``addresses``, with the target). The forward through the
    proxy is an httpx client naming the proxy with its own credential, as a
    machine's is; a direct forward dials the target's port on loopback with
    the host as the SNI."""

    up: exit_relay.Upstream | None = None
    bypass: tuple[str, ...] = ()
    admits: bool = True
    ended: tuple[str, ...] = (HOST,)
    ways: list[tuple[str, str, int]] = field(default_factory=list)
    proxied: httpx.AsyncClient | None = None

    def session_of(self, headers) -> str | None:
        parsed = exit_relay.basic_credential(headers)
        if parsed is None or parsed[0] not in exit_relay.PROXY_USERS:
            return None
        return parsed[1] if any(e.sid == parsed[1] for e in self.entries.values()) else None

    def takes_host(self, sid: str, host: str) -> bool:
        return exit_relay.host_matches(self.ended, host)

    async def upstream(self, sid: str, host: str, port: int) -> exit_relay.Upstream | None:
        self.ways.append(("upstream", host, port))
        return None if host in self.bypass else self.up

    async def upstream_admits(self, sid: str, host: str, port: int) -> bool:
        self.ways.append(("admits", host, port))
        return self.admits

    async def addresses(self, sid: str, host: str, port: int) -> list[str] | None:
        self.ways.append(("addresses", host, port))
        return ["127.0.0.1"]

    def client(self, sid: str, upstream: exit_relay.Upstream | None = None) -> httpx.AsyncClient:
        if upstream is None:
            return super().client(sid)
        if self.proxied is None:
            headers = {"Proxy-Authorization": upstream.authorization} if upstream.authorization else None
            trust = ssl.create_default_context(cafile=str(self.authority.folder / exit_authority.CERT_NAME))
            self.proxied = httpx.AsyncClient(
                proxy=httpx.Proxy(upstream.url, headers=headers), verify=trust, follow_redirects=False,
                trust_env=False, timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0))
        return self.proxied

    def forward_url(self, sid: str, host: str, port: int, path: str, addresses: list[str]):
        return f"https://127.0.0.1:{port}{path}", {"sni_hostname": host}

    async def aclose(self) -> None:
        for client in (self.http, self.proxied):
            if client is not None:
                await client.aclose()
