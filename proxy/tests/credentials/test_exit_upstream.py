"""The credential exit behind a machine's own outgoing proxy
(``core/credentials/exit_relay.py``, the relay every machine's satellite
vendors byte for byte): a plain tunnel through the proxy's ``CONNECT``, an
ended host's forward through it by the host's name, the proxy's own
credential and never the session's, a target the machine bypasses dialled
direct, a proxy that refuses, a target refused before the proxy is asked,
and an engine's host ended for a tool's own proxy user alone. Then the
address parser, the variables a session gets behind a proxy, and the block
the platform sends its own compute machine at the sign-in.

The proxy is a stand-in each test starts on loopback
(``exit_standin.UpstreamProxy``): it records every head it reads and opens
its tunnels to loopback alone. The origins are TLS servers on loopback with
leaves the test's authority signed, under ``.test`` names this process alone
resolves.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import socket
import ssl
import uuid
from types import SimpleNamespace

import httpx
import pytest

from core.credentials import credential_exit, exit_authority, exit_relay
from core.credentials.mcp_gateway import ExitCredential
from tests._timing import bound
from tests.credentials.exit_standin import VALUE, Origin, ProxiedBackend, UpstreamProxy

NAME = "api.vendor.test"
PLAIN = "plain.vendor.test"
ENGINE = "engine.test"
ENGINE_API = f"api.{ENGINE}"
PROXY_USER = "standin-proxy-user"
PROXY_PASS = "standin-proxy-pass-5e1f"


@pytest.fixture
def stand_in_names(monkeypatch):
    """Every ``.test`` name resolves to loopback, whatever asks for one."""
    real = socket.getaddrinfo

    def _resolve(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if isinstance(name, str) and name.endswith(".test"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", int(port or 0)))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


@pytest.fixture
def ca(tmp_path, stand_in_names):
    """The test's authority, which signs the origins' leaves and the relay's,
    and the bundle a caller trusts."""
    authority = exit_authority.ExitAuthority(tmp_path / "authority")
    bundle = tmp_path / "bundle.pem"
    authority.write_bundle(bundle)
    return authority, bundle


class _Echo:
    """A plain TCP server on loopback for the tunnels: it greets each
    connection, then answers every piece it reads in capitals. What it read
    is kept."""

    def __init__(self) -> None:
        self.read = bytearray()
        self.connections = 0
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def __aenter__(self) -> _Echo:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self.server is not None:
            self.server.close()
            self.server.abort_clients()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            writer.write(b"ready\n")
            await writer.drain()
            while data := await reader.read(65536):
                self.read.extend(data)
                writer.write(data.upper())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()


@contextlib.asynccontextmanager
async def _relayed(ca, *, up: exit_relay.Upstream | None = None, origin_name: str = NAME,
                   ended: tuple[str, ...] = (NAME,), **backend):
    """The relay over a ``ProxiedBackend`` and an origin under
    ``origin_name``, all stopped after."""
    authority, bundle = ca
    origin = Origin(authority, origin_name)
    await origin.start()
    held = ProxiedBackend(authority, origin, up=up, ended=ended, **backend)
    relay = exit_relay.Relay(held)
    port = await relay.start()
    try:
        yield SimpleNamespace(backend=held, origin=origin, relay=relay, port=port, bundle=bundle)
    finally:
        await relay.stop()
        await held.aclose()
        await origin.stop()


def _caller(port: int, sid: str, bundle, *, user: str = exit_relay.PROXY_USER) -> httpx.AsyncClient:
    """A caller whose ``HTTPS_PROXY`` names the relay, under ``user``."""
    return httpx.AsyncClient(proxy=f"http://{user}:{sid}@127.0.0.1:{port}",
                             verify=ssl.create_default_context(cafile=str(bundle)), follow_redirects=False,
                             trust_env=False, timeout=30.0)


async def _connect(port: int, sid: str, target: str, *, user: str = exit_relay.PROXY_USER):
    """A raw ``CONNECT`` to the relay: the streams and the relay's head."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    auth = base64.b64encode(f"{user}:{sid}".encode()).decode()
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
                 .encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), bound(10))
    return reader, writer, head


async def _answer(port: int, sid: str, target: str) -> bytes:
    """The relay's whole answer to a ``CONNECT`` it refuses."""
    reader, writer, head = await _connect(port, sid, target)
    try:
        return head + await asyncio.wait_for(reader.read(), bound(10))
    finally:
        writer.close()


async def _tunnelled(port: int, sid: str, target: str) -> bytes:
    """One exchange through a tunnel to ``_Echo``: its greeting and its
    answer to one line."""
    reader, writer, head = await _connect(port, sid, target)
    try:
        assert head.startswith(b"HTTP/1.1 200"), head
        greeting = await asyncio.wait_for(reader.readuntil(b"\n"), bound(5))
        writer.write(b"ping through the tunnel\n")
        await writer.drain()
        return greeting + await asyncio.wait_for(reader.readuntil(b"\n"), bound(5))
    finally:
        writer.close()


def _asked(backend: ProxiedBackend, way: str) -> list[tuple[str, int]]:
    return [(host, port) for kind, host, port in backend.ways if kind == way]


# ---------------------------------------------------------------------------
# Through the machine's own proxy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_tunnel_goes_through_the_proxys_connect(ca):
    async with UpstreamProxy() as proxy, _Echo() as echo, _relayed(ca, up=exit_relay.parse_upstream(proxy.url)) as ex:
        entry = ex.backend.hold()
        exchanged = await _tunnelled(ex.port, entry.sid, f"{PLAIN}:{echo.port}")
    assert exchanged == b"ready\nPING THROUGH THE TUNNEL\n"
    assert bytes(echo.read) == b"ping through the tunnel\n"
    (head,) = proxy.heads
    assert (head.method, head.target) == ("CONNECT", f"{PLAIN}:{echo.port}")
    assert head.headers["host"] == f"{PLAIN}:{echo.port}" and "proxy-authorization" not in head.headers
    # Both ways passed the proxy's tunnel, and the relay dialled nothing itself.
    assert bytes(proxy.toward) == b"ping through the tunnel\n"
    assert bytes(proxy.back) == b"ready\nPING THROUGH THE TUNNEL\n"
    assert _asked(ex.backend, "addresses") == [] and _asked(ex.backend, "admits") == [(PLAIN, echo.port)]
    assert ex.backend.order == []


@pytest.mark.asyncio
async def test_an_ended_hosts_request_goes_through_the_proxy_and_the_proxy_reads_none_of_it(ca):
    async with UpstreamProxy() as proxy, _relayed(ca, up=exit_relay.parse_upstream(proxy.url)) as ex:
        entry = ex.backend.hold(hosts=(NAME,))
        async with _caller(ex.port, entry.sid, ex.bundle) as client:
            r = await client.post(f"https://{NAME}:{ex.origin.port}/items", json={"a": 1},
                                  headers={"Authorization": f"Bearer {entry.placeholder}"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    (seen,) = ex.origin.seen
    assert seen.headers["authorization"] == f"Bearer {VALUE}" and seen.complete
    assert seen.headers["host"] == f"{NAME}:{ex.origin.port}" and "proxy-authorization" not in seen.headers
    # The forward named the host to the proxy: no address was asked or dialled.
    (head,) = proxy.heads
    assert (head.method, head.target) == ("CONNECT", f"{NAME}:{ex.origin.port}")
    assert _asked(ex.backend, "addresses") == [] and _asked(ex.backend, "admits") == [(NAME, ex.origin.port)]
    # What the proxy read and passed holds neither the placeholder nor the value.
    assert proxy.toward and proxy.back
    record = repr(proxy.heads).encode() + bytes(proxy.toward) + bytes(proxy.back)
    assert entry.placeholder.encode() not in record and VALUE.encode() not in record


@pytest.mark.asyncio
async def test_the_proxys_own_credential_reaches_it_and_the_sessions_never_does(ca, caplog):
    caplog.set_level(logging.DEBUG)
    async with UpstreamProxy() as proxy, _Echo() as echo:
        up = exit_relay.parse_upstream(f"http://{PROXY_USER}:{PROXY_PASS}@127.0.0.1:{proxy.port}")
        async with _relayed(ca, up=up) as ex:
            entry = ex.backend.hold(hosts=(NAME,))
            exchanged = await _tunnelled(ex.port, entry.sid, f"{PLAIN}:{echo.port}")
            async with _caller(ex.port, entry.sid, ex.bundle) as client:
                r = await client.get(f"https://{NAME}:{ex.origin.port}/items",
                                     headers={"Authorization": f"Bearer {entry.placeholder}"})
            proxy.answer = "407"
            refused = await _answer(ex.port, entry.sid, f"{PLAIN}:{echo.port}")
    own = "Basic " + base64.b64encode(f"{PROXY_USER}:{PROXY_PASS}".encode()).decode()
    session = "Basic " + base64.b64encode(f"{exit_relay.PROXY_USER}:{entry.sid}".encode()).decode()
    assert up is not None and up.authorization == own and PROXY_PASS not in up.url
    assert exchanged.endswith(b"PING THROUGH THE TUNNEL\n") and r.status_code == 200
    assert refused.startswith(b"HTTP/1.1 502")
    # The tunnel's, the forward's and the refused one's heads: each the proxy's own credential.
    assert len(proxy.heads) == 3
    for head in proxy.heads:
        assert head.headers["proxy-authorization"] == own
        assert entry.sid not in repr(head) and session not in repr(head)
    (seen,) = ex.origin.seen
    assert "proxy-authorization" not in seen.headers and PROXY_PASS not in repr(seen)
    # The exit's own log says what the proxy did and never names it: not its
    # address, not its credential (the pages promise neither reaches a log;
    # httpx's own debug trace is the library's, off in the satellite's log).
    exits = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpcore", "httpx")))
    assert "the machine's proxy answered 407" in exits and f":{proxy.port}" not in exits
    for text in (PROXY_PASS, PROXY_USER, own.split()[1], session.split()[1], entry.sid):
        assert text not in caplog.text


@pytest.mark.asyncio
async def test_a_target_the_machine_bypasses_is_dialled_direct_and_the_proxy_records_nothing(ca):
    async with UpstreamProxy() as proxy, _Echo() as echo:
        async with _relayed(ca, up=exit_relay.parse_upstream(proxy.url), bypass=(PLAIN, NAME)) as ex:
            entry = ex.backend.hold(hosts=(NAME,))
            exchanged = await _tunnelled(ex.port, entry.sid, f"{PLAIN}:{echo.port}")
            async with _caller(ex.port, entry.sid, ex.bundle) as client:
                r = await client.get(f"https://{NAME}:{ex.origin.port}/items",
                                     headers={"Authorization": f"Bearer {entry.placeholder}"})
            assert proxy.heads == [] and proxy.connections == 0
            # Judged by the addresses it may be dialled at; the proxy's judge is not asked.
            assert _asked(ex.backend, "addresses") == [(PLAIN, echo.port), (NAME, ex.origin.port)]
            assert _asked(ex.backend, "admits") == []
            # A name the machine does not bypass goes through the proxy.
            other = await _tunnelled(ex.port, entry.sid, f"other.vendor.test:{echo.port}")
    assert exchanged == other == b"ready\nPING THROUGH THE TUNNEL\n"
    assert r.status_code == 200 and ex.origin.seen[0].headers["authorization"] == f"Bearer {VALUE}"
    assert [(h.method, h.target) for h in proxy.heads] == [("CONNECT", f"other.vendor.test:{echo.port}")]
    assert echo.connections == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["407", "403", "close", "gone"])
async def test_a_proxy_that_refuses_or_ends_the_connection_gives_the_caller_502(ca, answer):
    """The tunnel's caller reads the relay's own sentence, nothing of the
    proxy's answer; the forward's caller reads a 502 too; neither target is
    dialled another way."""
    async with UpstreamProxy(answer="200" if answer == "gone" else answer) as proxy, _Echo() as echo:
        up = exit_relay.parse_upstream(proxy.url)
        if answer == "gone":
            await proxy.stop()
        async with _relayed(ca, up=up) as ex:
            entry = ex.backend.hold(hosts=(NAME,))
            tunnel = await _answer(ex.port, entry.sid, f"{PLAIN}:{echo.port}")
            async with _caller(ex.port, entry.sid, ex.bundle) as client:
                forward = await client.get(f"https://{NAME}:{ex.origin.port}/items",
                                           headers={"Authorization": f"Bearer {entry.placeholder}"})
    _head, _, body = tunnel.partition(b"\r\n\r\n")
    assert tunnel.startswith(b"HTTP/1.1 502") and body.decode() == exit_relay.REFUSAL_UPSTREAM + "\n"
    assert b"standin" not in tunnel
    assert forward.status_code == 502 and b"standin" not in forward.content
    assert len(proxy.heads) == (0 if answer == "gone" else 2)
    assert echo.connections == 0 and ex.origin.seen == []
    assert _asked(ex.backend, "addresses") == []


@pytest.mark.asyncio
async def test_a_target_the_backend_does_not_admit_is_refused_before_the_proxy_is_dialled(ca):
    async with UpstreamProxy() as proxy, _Echo() as echo:
        async with _relayed(ca, up=exit_relay.parse_upstream(proxy.url), admits=False) as ex:
            entry = ex.backend.hold(hosts=(NAME,))
            tunnel = await _answer(ex.port, entry.sid, f"{PLAIN}:{echo.port}")
            async with _caller(ex.port, entry.sid, ex.bundle) as client:
                forward = await client.get(f"https://{NAME}:{ex.origin.port}/items",
                                           headers={"Authorization": f"Bearer {entry.placeholder}"})
    assert tunnel.startswith(b"HTTP/1.1 403") and exit_relay.REFUSAL_WHERE.encode() in tunnel
    assert forward.status_code == 403 and exit_relay.REFUSAL_WHERE in forward.text
    assert proxy.heads == [] and proxy.connections == 0
    assert echo.connections == 0 and ex.origin.seen == []
    assert _asked(ex.backend, "addresses") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("through", [True, False], ids=["through-a-proxy", "direct"])
async def test_an_engines_host_is_ended_for_a_tools_own_proxy_user_alone(ca, monkeypatch, through):
    """The session holds a placeholder for a host under an engine's domain.
    The engine's own connection to it (user ``oto``) is a plain tunnel:
    nothing is read and no value is asked. A tool's connection under its own
    user is ended, the value in its header."""
    monkeypatch.setattr(exit_relay, "ENGINE_HOSTS", (ENGINE,))
    assert exit_relay.under_engine(ENGINE_API)
    async with UpstreamProxy() as proxy:
        up = exit_relay.parse_upstream(proxy.url) if through else None
        async with _relayed(ca, up=up, origin_name=ENGINE_API, ended=(ENGINE_API,)) as ex:
            entry = ex.backend.hold(hosts=(ENGINE_API,))
            url = f"https://{ENGINE_API}:{ex.origin.port}/v1/images"
            headers = {"Authorization": f"Bearer {entry.placeholder}"}
            async with _caller(ex.port, entry.sid, ex.bundle) as client:
                engine = await client.get(url, headers=headers)
            asked_for_the_engine = list(ex.backend.order)
            async with _caller(ex.port, entry.sid, ex.bundle, user=exit_relay.TOOL_PROXY_USER) as client:
                tool = await client.get(url, headers=headers)
    assert engine.status_code == tool.status_code == 200
    first, second = ex.origin.seen
    assert first.headers["authorization"] == f"Bearer {entry.placeholder}" and asked_for_the_engine == []
    assert second.headers["authorization"] == f"Bearer {VALUE}" and ex.backend.order == ["value"]
    targets = [(h.method, h.target) for h in proxy.heads]
    assert targets == ([("CONNECT", f"{ENGINE_API}:{ex.origin.port}")] * 2 if through else [])


def test_an_engines_domain_and_every_name_under_it_are_the_engines():
    for host in ("anthropic.com", "api.anthropic.com", "API.OpenAI.com.", "chatgpt.com", "x.y.claude.com"):
        assert exit_relay.under_engine(host), host
    for host in ("notanthropic.com", "openai.com.example.net", "api.vendor.test", "claude.ai"):
        assert not exit_relay.under_engine(host), host
    assert exit_relay.PROXY_USERS == (exit_relay.PROXY_USER, exit_relay.TOOL_PROXY_USER) == ("oto", "oto-tool")


# ---------------------------------------------------------------------------
# The proxy's address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url, scheme, host, port", [
    ("http://proxy.corp.test:3128", "http", "proxy.corp.test", 3128),
    ("http://proxy.corp.test", "http", "proxy.corp.test", 80),
    ("https://proxy.corp.test:8443", "https", "proxy.corp.test", 8443),
    ("https://proxy.corp.test", "https", "proxy.corp.test", 443),
    ("proxy.corp.test:3128", "http", "proxy.corp.test", 3128),
    ("  HTTP://Proxy.Corp.Test:3128/  ", "http", "proxy.corp.test", 3128),
    ("http://[fd00::5]:3128", "http", "fd00::5", 3128),
])
def test_an_http_or_https_proxy_address_is_read(url, scheme, host, port):
    up = exit_relay.parse_upstream(url)
    assert up is not None and (up.scheme, up.host, up.port, up.authorization) == (scheme, host, port, "")
    named = f"[{host}]" if ":" in host else host
    assert up.url == f"{scheme}://{named}:{port}"


@pytest.mark.parametrize("url, pair", [
    ("http://standin-user:standin-pass@proxy.corp.test:3128", "standin-user:standin-pass"),
    ("https://standin-user:p%40ss%3Aw0rd@proxy.corp.test", "standin-user:p@ss:w0rd"),
    ("http://standin-user@proxy.corp.test:3128", "standin-user:"),
])
def test_the_user_part_becomes_the_proxys_basic_credential_and_leaves_its_address(url, pair):
    up = exit_relay.parse_upstream(url)
    assert up is not None and up.authorization == "Basic " + base64.b64encode(pair.encode()).decode()
    assert up.url == f"{up.scheme}://proxy.corp.test:{up.port}"


@pytest.mark.parametrize("url, kind", [
    ("socks5://proxy.corp.test:1080", "socks"),
    ("socks5h://standin-user:standin-pass@proxy.corp.test:1080", "socks"),
    ("SOCKS4://proxy.corp.test:1080", "socks"),
    ("socks://proxy.corp.test", "socks"),
    ("ftp://proxy.corp.test:21", "scheme"),
    ("file:///etc/proxy", "scheme"),
    ("http://", "scheme"),
    ("http://proxy.corp.test:notaport", "scheme"),
    ("http://proxy.corp.test:99999", "scheme"),
])
def test_an_address_the_relay_does_not_speak_is_refused_with_its_kind(url, kind):
    with pytest.raises(exit_relay.UpstreamRefused) as refused:
        exit_relay.parse_upstream(url)
    assert refused.value.kind == kind and isinstance(refused.value, ValueError)
    assert "standin-pass" not in str(refused.value)


@pytest.mark.parametrize("url", ["", "   ", None])
def test_no_address_is_no_proxy(url):
    assert exit_relay.parse_upstream(url) is None


# ---------------------------------------------------------------------------
# A session's variables behind the machine's own proxy
# ---------------------------------------------------------------------------


def test_behind_a_proxy_the_composer_leaves_the_engines_to_the_exit():
    """The engines' hosts, bare and dotted, are not kept direct: a network
    whose only way out is the proxy would cut them. The loopback names and
    the carves stay; without a proxy the list is as before."""
    kw = {"port": 41443, "credential": "door", "bundle_path": "/exit/bundle.pem", "allow_hosts": ("10.0.0.5",),
          "direct_hosts": ("file-tools",)}
    behind = credential_exit._relay_env(**kw, upstream=True)
    plain = credential_exit._relay_env(**kw)
    assert behind["NO_PROXY"].split(",") == ["127.0.0.1", "localhost", "::1", "10.0.0.5", "file-tools"]
    assert plain["NO_PROXY"].split(",") == ["127.0.0.1", "localhost", "::1", "10.0.0.5", "file-tools",
                                            *(f".{e}" for e in exit_relay.ENGINE_HOSTS), *exit_relay.ENGINE_HOSTS]
    assert behind["no_proxy"] == behind["NO_PROXY"]
    assert {k: v for k, v in behind.items() if k.lower() != "no_proxy"} == {
        k: v for k, v in plain.items() if k.lower() != "no_proxy"}
    relay_own = exit_relay.session_env(port=1, credential="c", bundle_path="/b", engines_direct=False)
    assert relay_own["NO_PROXY"] == "127.0.0.1,localhost,::1"


def _placement(kind: str, *, upstream: bool):
    from core import placement
    if kind == "compute":
        return placement.PlacementCapabilities(
            kind=placement.KIND_COMPUTE, machine_id="c-1", label="Box", satellite_version="0.5.175",
            exit_port=41112, exit_ok=True, exit_upstream=upstream)
    return placement.PlacementCapabilities(
        kind=placement.KIND_USER_REMOTE, machine_id="m-1", label="Laptop", satellite_version="0.5.175",
        exit_port=41111, exit_ok=True, os="linux", reported_otodock_dir="/home/alice/.oto-dock",
        exit_upstream=upstream)


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream", [True, False], ids=["behind-a-proxy", "direct"])
async def test_a_machine_behind_its_own_proxy_composes_its_sessions_variables_that_way(upstream):
    builder = SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=("10.0.0.5",)), exit_port=0, ssh_agent="")
    on_compute, on_paired, sandbox = str(uuid.uuid4()), str(uuid.uuid4()), {}
    await credential_exit.attach_sandbox(on_compute, builder, sandbox, "", sandbox_config_dir="/workspace/.claude",
                                         direct_hosts=("file-tools",), machine=_placement("compute", upstream=upstream))
    paired = credential_exit.machine_env(on_paired, _placement("paired", upstream=upstream))
    for env in (sandbox, paired):
        names = env["NO_PROXY"].split(",")
        assert names[:3] == ["127.0.0.1", "localhost", "::1"] and env["no_proxy"] == env["NO_PROXY"]
        for engine in exit_relay.ENGINE_HOSTS:
            assert (engine in names) is not upstream and (f".{engine}" in names) is not upstream, env["NO_PROXY"]
    assert {"10.0.0.5", "file-tools"} <= set(sandbox["NO_PROXY"].split(","))
    assert credential_exit.env_of(on_compute) == sandbox
    # A tool toward an engine's host names its own proxy user; behind the
    # proxy the session's list keeps no engine, so there is nothing to drop.
    toward = (ExitCredential(placeholder=credential_exit.new_placeholder(), header="Authorization",
                             hosts=("api.openai.com",), allowlist_key="t", manifest="t", env_name="K", value="v"),)
    own = credential_exit.own_direct(on_compute, toward, {})
    secret = credential_exit.machine_secret(on_compute)
    assert own["HTTPS_PROXY"] == own["https_proxy"] == f"http://oto-tool:{secret}@127.0.0.1:{builder.exit_port}"
    assert ("NO_PROXY" in own) is not upstream


# ---------------------------------------------------------------------------
# The platform's proxy for the install's own compute machine
# ---------------------------------------------------------------------------


BLOCK = {"https": "http://proxy.corp.test:3128", "no_proxy": "internal.corp.test", "trust": [], "cert_dir": []}


@pytest.mark.asyncio
async def test_the_platforms_proxy_goes_to_the_installs_own_compute_machine_alone(monkeypatch):
    from core import placement
    from services.mcp import mcp_egress
    from ws import satellite as sat_ws
    asked: list[int] = []
    monkeypatch.setattr(mcp_egress, "exit_upstream_block", lambda: asked.append(1) or dict(BLOCK))
    own = {"id": "compute-0123456789ab", "pairing_scope": "compute", "compute_address": ""}
    elsewhere = {"id": "compute-0123456789cd", "pairing_scope": "compute", "compute_address": "192.168.50.7"}
    assert placement.compute_own_copy(elsewhere) and not placement.compute_own_copy(own)
    assert await sat_ws._egress_proxy_for(own) == BLOCK and asked == [1]
    for machine in (elsewhere, {"id": "m-1", "pairing_scope": "user"}, {"id": "m-2", "pairing_scope": "admin"}, {}):
        assert await sat_ws._egress_proxy_for(machine) is None, machine
    assert asked == [1]
    assert sat_ws._auth_ok(own, cli_pins={}, egress_proxy=dict(BLOCK))["egress_proxy"] == BLOCK
    assert "egress_proxy" not in sat_ws._auth_ok(own, cli_pins={})
    assert "egress_proxy" not in sat_ws._auth_ok(own, cli_pins={}, egress_proxy=None)


def test_the_block_names_the_platforms_https_proxy_and_its_list(monkeypatch, tmp_path):
    import config
    from services.mcp import mcp_egress, mcp_installer
    env: dict[str, str] = {"PATH": "/usr/bin"}
    monkeypatch.setattr(mcp_installer, "_install_env", lambda extra=None: dict(env))
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    assert mcp_egress.exit_upstream_block() is None
    # A plain http proxy alone carries none of the exit's connections.
    env["HTTP_PROXY"] = "http://plain.corp.test:3128"
    assert mcp_egress.exit_upstream_block() is None
    env.update({"https_proxy": "http://proxy.corp.test:3128", "NO_PROXY": "internal.corp.test,.lan"})
    assert mcp_egress.exit_upstream_block() == {"https": "http://proxy.corp.test:3128",
                                                "no_proxy": "internal.corp.test,.lan", "trust": [], "cert_dir": []}
    del env["https_proxy"]
    env["ALL_PROXY"] = "http://all.corp.test:3128"
    assert mcp_egress.exit_upstream_block()["https"] == "http://all.corp.test:3128"
    # The trust files the builds get ride with it.
    pem = tmp_path / "corp.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\nc3RhbmRpbg==\n-----END CERTIFICATE-----\n")
    env["SSL_CERT_FILE"] = str(pem)
    assert mcp_egress.exit_upstream_block()["trust"] == [
        {"env": "SSL_CERT_FILE", "name": "corp.pem", "data_b64": base64.b64encode(pem.read_bytes()).decode()}]
    # A hosted install's transports never take a proxy.
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    assert mcp_egress.exit_upstream_block() is None


def test_a_connect_target_outside_ascii_names_no_host():
    """The relay reads a tunnel's target as strict ASCII: anything else is
    no target at all, and the relay answers it 400 before any dial."""
    assert exit_relay._split_target(b"api.example.com:443") == ("api.example.com", 443)
    assert exit_relay._split_target(b"[::1]:8443") == ("::1", 8443)
    assert exit_relay._split_target("hôst.example:443".encode()) is None
    assert exit_relay._split_target(b"api.example.com:\xff443") is None


@pytest.mark.asyncio
async def test_a_head_the_relay_cannot_write_closes_the_proxys_connection():
    """The CONNECT toward the machine's proxy is built inside the guarded
    part: a target it cannot write ends in None, and the connection the relay
    opened to the proxy is closed, not left to the collector."""
    ended = asyncio.Event()
    seen: list[bytes] = []

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.read())
        ended.set()
        writer.close()

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        up = exit_relay.parse_upstream(f"http://127.0.0.1:{port}")
        got = await exit_relay._open_through(up, "hôst.example", 443, logging.getLogger("test"))
        assert got is None
        await asyncio.wait_for(ended.wait(), 5)
        assert seen == [b""]
    finally:
        server.close()
        await server.wait_closed()


def test_an_address_the_parser_refuses_keeps_its_credential_out_of_every_text(caplog):
    """An address the standard library's parser refuses is refused as a
    scheme the relay does not speak, with nothing of the address in the
    error's text, its form or its cause, and nothing in any log record."""
    address = "http://walker:stand＃in@proxy.corp.test:3128"
    caplog.set_level(logging.DEBUG)
    with pytest.raises(exit_relay.UpstreamRefused) as raised:
        exit_relay.parse_upstream(address)
    error = raised.value
    assert error.kind == "scheme"
    told = f"{error!s} {error!r} {error.args!r}"
    assert "stand" not in told and "walker" not in told and "proxy.corp.test" not in told
    assert error.__cause__ is None and error.__suppress_context__
    assert "stand" not in caplog.text and "proxy.corp.test" not in caplog.text


@pytest.mark.asyncio
async def test_a_connection_that_fails_unforeseen_logs_its_kind_and_never_its_words(caplog):
    """The relay's last handler names the error's kind and where it rose:
    an error's own text can quote what it read, and none of it is logged."""

    class Failing:
        def session_of(self, headers):
            raise ValueError("words-not-for-the-log")

    logger = logging.getLogger("test.exit-relay")
    caplog.set_level(logging.DEBUG, logger="test.exit-relay")
    relay = exit_relay.Relay(Failing(), logger=logger)
    port = await relay.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT a.example.test:443 HTTP/1.1\r\nHost: a.example.test:443\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(), 5)
        writer.close()
    finally:
        await relay.stop()
    assert "words-not-for-the-log" not in caplog.text
    assert any("ValueError" in r.getMessage() and r.exc_info is None for r in caplog.records)


@pytest.mark.asyncio
async def test_an_https_proxys_own_certificate_is_checked(tmp_path):
    """Through an ``https://`` proxy the tunnel opens only when the relay's
    trust holds the proxy's certificate: with the machine's own store alone
    (which holds no test authority) the caller gets nothing and the proxy
    reads no head."""
    authority = exit_authority.ExitAuthority(tmp_path / "proxy-ca")
    authority.load_or_make()
    heads: list[bytes] = []

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            heads.append(head)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(proxy, "127.0.0.1", 0, ssl=authority.leaf_context("localhost"))
    port = server.sockets[0].getsockname()[1]
    up = exit_relay.parse_upstream(f"https://localhost:{port}")
    log = logging.getLogger("test.https-proxy")
    try:
        assert await exit_relay._open_through(up, "a.example.test", 443, log) is None
        assert heads == []
        trusted = ssl.create_default_context(cafile=str(tmp_path / "proxy-ca" / exit_authority.CERT_NAME))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(exit_relay, "_proxy_context", lambda: trusted)
            opened = await exit_relay._open_through(up, "a.example.test", 443, log)
        assert opened is not None and heads and heads[0].startswith(b"CONNECT a.example.test:443 ")
        opened[1].close()
    finally:
        server.close()
        await server.wait_closed()
