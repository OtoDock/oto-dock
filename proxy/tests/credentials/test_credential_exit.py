"""The credential exit (``core/credentials/credential_exit.py``, the
platform's half of a machine's exit, and ``exit_relay.Relay``, which every
machine's satellite runs): a session's placeholder is swapped for the value
in its header alone, toward an approved host alone, and the exit widens
nothing the sandbox's namespace does not reach.

The relay runs over a compute machine's exit as ``exit_standin.MachineExit``
stands it in: a session is admitted by the secret of the door its start gave
it, its values are the platform's resolution, its judged requests the
platform's judge, and a target is dialled by the sandbox's rule with the
session's carves. The vendor is a TLS server on loopback with a leaf the
exit's own authority signed; the dial rule, which refuses loopback whatever
the carves say, is replaced for the vendor tests and tested on its own.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import ssl
import threading
import time
import uuid
from types import SimpleNamespace

import h11
import httpx
import pytest
import pytest_asyncio

import config
from core.credentials import (
    credential_exit as exit_mod,
    exit_authority,
    exit_relay,
    mcp_broker,
    mcp_gateway,
)
from core.credentials.mcp_broker import SecretBundle
from core.credentials.mcp_gateway import ExitCredential
from tests._timing import bound
from tests.credentials.exit_standin import MachineExit

SECRET = "sk-vendor-value-0123456789abcdef"
VENDOR = "localhost"


class _Vendor:
    """One HTTP/1.1 server over TLS: what it was sent, by request."""

    def __init__(self, ca: exit_authority.ExitAuthority) -> None:
        self.ca = ca
        self.seen: list[dict] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self.ca.leaf_context(VENDOR))
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            # The exit's pool keeps its connection to the vendor alive; a
            # TLS close would wait 30 s for the close notice nobody sends.
            self.server.abort_clients()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER)
        request = None
        body = b""
        try:
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    data = await reader.read(65536)
                    conn.receive_data(data)
                    if not data:
                        break
                    continue
                if isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    body += bytes(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    break
            if request is None:
                return
            headers = {k.decode().lower(): v.decode() for k, v in request.headers}
            self.seen.append({"method": request.method.decode(), "target": request.target.decode(),
                              "headers": headers, "body": body.decode("utf-8", "replace")})
            if request.target == b"/redirect":
                answer = conn.send(h11.Response(status_code=302, headers=[
                    (b"location", b"https://127.0.0.1:1/elsewhere"), (b"content-length", b"0")]))
                answer += conn.send(h11.EndOfMessage())
            else:
                payload = json.dumps({"ok": True, "n": len(self.seen)}).encode()
                answer = conn.send(h11.Response(status_code=200, headers=[
                    (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]))
                answer += conn.send(h11.Data(data=payload)) + conn.send(h11.EndOfMessage())
            writer.write(answer)
            await writer.drain()
        finally:
            writer.close()


@pytest.fixture
def ca(tmp_path) -> exit_authority.ExitAuthority:
    """The exit's authority: it signs the vendor's leaf and every leaf the
    exit ends a connection with."""
    return exit_authority.ExitAuthority(tmp_path / "authority")


@pytest.fixture
def authority(ca, tmp_path):
    """The bundle a session's clients trust: the system's roots, then the
    exit's authority."""
    bundle = tmp_path / "exit-ca.pem"
    ca.write_bundle(bundle)
    return bundle


@pytest_asyncio.fixture
async def vendor(ca):
    v = _Vendor(ca)
    await v.start()
    yield v
    await v.stop()


def _approve_every_host(monkeypatch) -> None:
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: True)
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda upstream: None)


def _loopback_passes(address: str, carves: frozenset[str]) -> bool:
    """The dial rule of the vendor tests: the vendor sits on loopback, which
    the sandbox's rule never lets through."""
    return False


@pytest_asyncio.fixture
async def machine(ca, monkeypatch):
    _approve_every_host(monkeypatch)
    exit_ = MachineExit(ca, dial_refused=_loopback_passes)
    await exit_.start()
    yield exit_
    await exit_.stop()
    mcp_broker._store.clear()


async def _session(machine: MachineExit, hosts=(VENDOR,), *, header="Authorization", prefix="Bearer ", cli=None,
                   carves=()) -> tuple[str, str, str]:
    """A compute session holding one placeholder; the secret its door at the
    machine's exit admits."""
    sid = str(uuid.uuid4())
    placeholder = exit_mod.new_placeholder()
    cred = ExitCredential(placeholder=placeholder, header=header, hosts=tuple(hosts), allowlist_key="probe",
                          manifest="probe", env_name="PROBE_KEY", prefix=prefix, value=SECRET, cli=cli)
    mcp_broker.provision(sid, {"probe": SecretBundle(env={"PROBE_KEY": placeholder}, exit=(cred,))})
    return sid, placeholder, await machine.open(sid, carves)


def _trusting(bundle) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(bundle))


def _client(port: int, secret: str, bundle) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=f"http://oto:{secret}@127.0.0.1:{port}", verify=_trusting(bundle),
                             follow_redirects=False, trust_env=False, timeout=10.0)


@pytest.mark.asyncio
async def test_the_value_lands_in_the_configured_header_alone(machine, vendor, authority):
    sid, placeholder, secret = await _session(machine)
    async with _client(machine.port, secret, authority) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/items", json={"a": 1},
                              headers={"Authorization": f"Bearer {placeholder}", "X-Other": "plain"})
    assert r.status_code == 200 and r.json()["ok"] is True
    seen = vendor.seen[-1]
    assert seen["headers"]["authorization"] == f"Bearer {SECRET}"
    assert seen["headers"]["x-other"] == "plain" and json.loads(seen["body"]) == {"a": 1}
    assert placeholder not in json.dumps(seen) and secret not in json.dumps(seen)
    assert "proxy-authorization" not in seen["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["target", "header", "body"])
async def test_a_placeholder_anywhere_but_its_header_refuses_the_request(machine, vendor, authority, where):
    sid, placeholder, secret = await _session(machine)
    url = f"https://{VENDOR}:{vendor.port}/items"
    headers = {"Authorization": f"Bearer {placeholder}"}
    content = b"{}"
    if where == "target":
        url += f"?key={placeholder}"
    elif where == "header":
        headers["X-Key"] = placeholder
    else:
        content = json.dumps({"token": placeholder}).encode()
    async with _client(machine.port, secret, authority) as client:
        r = await client.post(url, content=content, headers=headers)
    assert r.status_code == 400 and exit_mod.REFUSAL_PLACE in r.text
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_a_basic_header_is_decoded_swapped_and_encoded_again(machine, vendor, authority):
    """git's form: the password inside the Basic credentials."""
    sid, placeholder, secret = await _session(machine, prefix="")
    basic = base64.b64encode(f"x-access-token:{placeholder}".encode()).decode()
    async with _client(machine.port, secret, authority) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/info/refs",
                             headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200
    sent = vendor.seen[-1]["headers"]["authorization"]
    scheme, encoded = sent.split(" ", 1)
    assert scheme == "Basic" and base64.b64decode(encoded) == f"x-access-token:{SECRET}".encode()


@pytest.mark.asyncio
async def test_another_sessions_placeholder_is_forwarded_as_it_came(machine, vendor, authority):
    """A placeholder the session does not hold is a string nobody can use:
    it reaches the vendor unchanged, and the exit swaps nothing."""
    sid, placeholder, secret = await _session(machine)
    other_sid, other_placeholder, _other = await _session(machine)
    async with _client(machine.port, secret, authority) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/x",
                             headers={"Authorization": f"Bearer {other_placeholder}"})
    assert r.status_code == 200
    assert vendor.seen[-1]["headers"]["authorization"] == f"Bearer {other_placeholder}"


@pytest.mark.asyncio
async def test_another_entrys_placeholder_is_refused_toward_this_host(machine, vendor, authority):
    """A placeholder goes to its own entry's hosts alone: in its own header,
    toward a host another entry of the session declares, the request is
    refused before any dial."""
    sid, placeholder, secret = await _session(machine)
    other = exit_mod.new_placeholder()
    mcp_broker._store[sid]["other"] = SecretBundle(env={"OTHER_KEY": other}, exit=(ExitCredential(
        placeholder=other, header="Authorization", hosts=("other.example.test",), allowlist_key="other",
        manifest="other", env_name="OTHER_KEY", prefix="Bearer ", value="another-value-0123456789"),))
    async with _client(machine.port, secret, authority) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/x", headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 400 and exit_mod.REFUSAL_PLACE in r.text
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_a_redirect_passes_back_and_is_never_followed(machine, vendor, authority):
    sid, placeholder, secret = await _session(machine)
    async with _client(machine.port, secret, authority) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/redirect",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 302 and r.headers["location"] == "https://127.0.0.1:1/elsewhere"
    assert len(vendor.seen) == 1


@pytest.mark.asyncio
async def test_a_host_that_is_not_approved_gets_a_tunnel_and_the_placeholder(machine, ca, authority):
    """The same vendor under a name the credential does not approve (its
    address): the connection is tunnelled, never ended, and the placeholder
    reaches it as a string."""
    sid, placeholder, secret = await _session(machine, hosts=("api.example.test",))
    by_address = _Vendor(ca)
    by_address.server = await asyncio.start_server(by_address._handle, "127.0.0.1", 0,
                                                   ssl=ca.leaf_context("127.0.0.1"))
    by_address.port = by_address.server.sockets[0].getsockname()[1]
    try:
        async with _client(machine.port, secret, authority) as client:
            r = await client.get(f"https://127.0.0.1:{by_address.port}/x",
                                 headers={"Authorization": f"Bearer {placeholder}"})
    finally:
        await by_address.stop()
    assert r.status_code == 200
    assert by_address.seen[-1]["headers"]["authorization"] == f"Bearer {placeholder}"


@pytest.mark.asyncio
async def test_the_decrypted_host_must_name_the_connections_host(machine, vendor, authority):
    """A client that tunnels to one host and asks for another inside is
    refused before anything is dialled (the SNI and the target decide
    nothing on their own)."""
    sid, placeholder, secret = await _session(machine)
    reader, writer = await asyncio.open_connection("127.0.0.1", machine.port)
    auth = base64.b64encode(f"oto:{secret}".encode()).decode()
    writer.write(f"CONNECT {VENDOR}:{vendor.port} HTTP/1.1\r\nHost: {VENDOR}:{vendor.port}\r\n"
                 f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    await writer.start_tls(_trusting(authority), server_hostname=VENDOR)
    writer.write(f"GET /x HTTP/1.1\r\nHost: other.example.test:{vendor.port}\r\n"
                 f"Authorization: Bearer {placeholder}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    answer = await reader.read()
    writer.close()
    assert answer.startswith(b"HTTP/1.1 400") and exit_mod.REFUSAL_HOST.encode() in answer
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_no_secret_a_closed_doors_secret_and_a_plain_request_are_refused(machine, vendor, authority):
    """A machine's exit admits a session by its door's secret alone: no
    credential, the secret of a door the session's stop closed and a live
    secret under another user get no tunnel; a plain request gets no value."""
    reader, writer = await asyncio.open_connection("127.0.0.1", machine.port)
    writer.write(f"CONNECT {VENDOR}:{vendor.port} HTTP/1.1\r\nHost: {VENDOR}\r\n\r\n".encode())
    await writer.drain()
    answer = await reader.read()
    writer.close()
    assert answer.startswith(b"HTTP/1.1 407") and b'proxy-authenticate: basic realm="otodock"' in answer.lower()
    closed_sid, _p, closed = await _session(machine)
    machine.close(closed_sid)
    async with _client(machine.port, closed, authority) as client:
        with pytest.raises(httpx.ProxyError):
            await client.get(f"https://{VENDOR}:{vendor.port}/x")
    sid, _p, secret = await _session(machine)
    async with httpx.AsyncClient(proxy=f"http://git:{secret}@127.0.0.1:{machine.port}", verify=_trusting(authority),
                                 trust_env=False, timeout=10.0) as client:
        with pytest.raises(httpx.ProxyError):
            await client.get(f"https://{VENDOR}:{vendor.port}/x")
    async with httpx.AsyncClient(proxy=f"http://oto:{secret}@127.0.0.1:{machine.port}", trust_env=False,
                                 timeout=10.0) as client:
        r = await client.get(f"http://{VENDOR}:{vendor.port}/x")
    assert r.status_code == 403 and exit_mod.REFUSAL_PLAIN in r.text
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_a_body_over_the_cap_is_refused_before_any_dial(machine, vendor, authority, monkeypatch):
    monkeypatch.setattr(exit_relay, "MAX_BODY_BYTES", 1024)  # the module that reads the cap
    sid, placeholder, secret = await _session(machine)
    async with _client(machine.port, secret, authority) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/x", content=b"x" * 2048,
                              headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 413
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_a_refused_value_is_a_refusal_never_the_raw_secret(machine, vendor, authority, monkeypatch):
    """The gateway's resolution refuses (the host is not approved on the
    allowlist): the request is refused and nothing is dialled."""
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: False)
    sid, placeholder, secret = await _session(machine)
    async with _client(machine.port, secret, authority) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/x", headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 403 and "not approved" in r.text
    assert SECRET not in r.text
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_the_log_carries_neither_the_value_nor_the_placeholder(machine, vendor, authority, caplog):
    sid, placeholder, secret = await _session(machine)
    with caplog.at_level(logging.DEBUG, logger="otodock.credential-exit"):
        async with _client(machine.port, secret, authority) as client:
            await client.get(f"https://{VENDOR}:{vendor.port}/x", headers={"Authorization": f"Bearer {placeholder}"})
            await client.get(f"https://{VENDOR}:{vendor.port}/y?k={placeholder}",
                             headers={"Authorization": f"Bearer {placeholder}"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    # The log names the key swapped, never its value.
    assert "the value of PROBE_KEY added" in text and exit_mod.REFUSAL_PLACE in text
    assert SECRET not in text and placeholder not in text and secret not in text


def _compute_machine():
    from core import placement
    return placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1",
                                           satellite_version="0.5.175", exit_port=41112, exit_ok=True)


def _builder(*carves: str) -> SimpleNamespace:
    return SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=carves), exit_port=0, ssh_agent="")


@pytest.mark.asyncio
async def test_the_dial_rule_takes_the_carves_the_session_registered():
    """A compute machine's exit dials by the sandbox's rule
    (``outbound_url.sandbox_dial_refused``) with the carves of the session's
    door: a carve passes, its neighbours and the host's own addresses never,
    a carved on-link address passes where the rest of its subnet does not; a
    carve never opens loopback or the metadata address; a second register
    replaces the carves, and a forgotten session has none."""
    from services.infra import outbound_url
    sid = str(uuid.uuid4())
    onlink = (ipaddress.ip_network("93.184.216.0/24"),)

    def owns(ip) -> bool:
        return str(ip) in ("151.101.1.69", "2a04:4e42::323")

    def refused(address: str) -> bool:
        block = exit_mod.machine_block(sid, {}, "") or {"carves": []}
        return outbound_url.sandbox_dial_refused(address, carves=block["carves"], onlink=onlink, owns=owns)

    try:
        await exit_mod.attach_sandbox(sid, _builder("192.168.1.5", "10.9.0.3", "93.184.216.34"), {}, "",
                                      sandbox_config_dir="/workspace/.claude", machine=_compute_machine())
        assert refused("151.101.1.69") and refused("2a04:4e42::323")
        assert refused("93.184.216.35") and not refused("93.184.216.34")
        assert not refused("192.168.1.5") and not refused("10.9.0.3") and not refused("8.8.8.8")
        assert refused("192.168.1.6") and refused("10.9.0.4") and refused("172.17.0.1")
        exit_mod.register(sid, ["127.0.0.1", "169.254.169.254"])
        assert refused("127.0.0.1") and refused("169.254.169.254") and refused("192.168.1.5")
    finally:
        exit_mod.forget(sid)
    # A session nobody registered: public alone.
    assert exit_mod.machine_block(sid, {}, "") is None
    assert not refused("8.8.8.8") and refused("192.168.1.5")


@pytest.mark.asyncio
async def test_a_private_target_outside_the_carves_is_refused_at_the_exit(ca, monkeypatch):
    """The sandbox's own rule on the connect, with the session's carves: the
    LAN the namespace blackholes stays closed through the exit too, and a
    carve never opens loopback or the metadata address."""
    _approve_every_host(monkeypatch)
    machine = MachineExit(ca)
    await machine.start()
    try:
        sid, placeholder, secret = await _session(machine, hosts=("api.example.test",),
                                                  carves=("127.0.0.1", "169.254.169.254"))
        assert machine.doors[sid]["carves"] == ["127.0.0.1", "169.254.169.254"]
        auth = base64.b64encode(f"oto:{secret}".encode()).decode()
        for target in ("192.168.250.250:443", "127.0.0.1:443", "169.254.169.254:80"):
            reader, writer = await asyncio.open_connection("127.0.0.1", machine.port)
            writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
                         .encode())
            await writer.drain()
            answer = await reader.read()
            writer.close()
            assert answer.startswith(b"HTTP/1.1 403") and exit_mod.REFUSAL_WHERE.encode() in answer, target
    finally:
        await machine.stop()
        mcp_broker._store.clear()


@pytest.mark.asyncio
async def test_the_sessions_environment_names_the_exit_and_the_bundle():
    """A compute session's variables: its machine's exit on the port spliced
    into its namespace with its door's secret, the carves and the tools'
    names direct beside the loopback names and the engines', every trust
    variable at the bundle the render binds."""
    from core.sandbox.sandbox_render import EXIT_BUNDLE_PATH
    sid = str(uuid.uuid4())
    builder = _builder("192.168.1.5")
    env: dict[str, str] = {}
    try:
        await exit_mod.attach_sandbox(sid, builder, env, "", sandbox_config_dir="/workspace/.claude",
                                      direct_hosts=["file-tools"], machine=_compute_machine())
        secret = exit_mod.machine_secret(sid)
    finally:
        exit_mod.forget(sid)
    assert set(env) == set(exit_mod.ENV_NAMES)
    assert secret and env["HTTPS_PROXY"] == f"http://oto:{secret}@127.0.0.1:{builder.exit_port}"
    assert env["https_proxy"] == env["HTTPS_PROXY"]
    # The carves are addresses and a Docker tool's URL spells its name: both
    # stay direct, as the engines' hosts do, also for a client that reads a
    # bare name as one host.
    assert env["NO_PROXY"] == ("127.0.0.1,localhost,::1,192.168.1.5,file-tools,.anthropic.com,.claude.com,"
                               ".openai.com,.chatgpt.com,anthropic.com,claude.com,openai.com,chatgpt.com")
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO"):
        assert env[name] == EXIT_BUNDLE_PATH
    assert env["NODE_USE_ENV_PROXY"] == "1"


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY",
                                      "http_proxy"])
@pytest.mark.asyncio
async def test_behind_an_egress_proxy_the_exit_still_runs(tmp_path, monkeypatch, variable):
    """An egress proxy in the install's environment turns nothing off: a
    compute machine's start still gets the exit's port and variables, and a
    paired computer's processes still get the variables of its own exit."""
    from core import placement
    monkeypatch.setenv(variable, "http://corp:3128")
    compute = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1",
                                              satellite_version="0.5.175", exit_port=41112, exit_ok=True)
    paired = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m-1",
                                             satellite_version="0.5.175", exit_port=41111, exit_ok=True,
                                             reported_otodock_dir="/home/alice/.oto-dock")
    builder = SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=()), exit_port=0, ssh_agent="")
    on_compute, on_paired, env = str(uuid.uuid4()), str(uuid.uuid4()), {}
    try:
        await exit_mod.attach_sandbox(on_compute, builder, env, tmp_path, sandbox_config_dir="/workspace/.claude",
                                      machine=compute)
        secret = exit_mod.machine_secret(on_compute)
        assert builder.exit_port == exit_mod._SANDBOX_EXIT_PORT
        assert secret and env["HTTPS_PROXY"] == f"http://oto:{secret}@127.0.0.1:{exit_mod._SANDBOX_EXIT_PORT}"
        machine = exit_mod.machine_env(on_paired, paired)
        secret = exit_mod.machine_secret(on_paired)
        assert secret and machine["HTTPS_PROXY"] == f"http://oto:{secret}@127.0.0.1:41111"
    finally:
        exit_mod.forget(on_compute)
        exit_mod.forget(on_paired)


def test_the_brokers_purge_forgets_the_session_at_the_exit():
    """Every sandboxed start records its session here: the broker's purge
    at the session's end forgets it here too."""
    sid = str(uuid.uuid4())
    exit_mod.register_purge_hook()
    exit_mod.register(sid, ["10.0.0.5"])
    assert sid in exit_mod._sessions
    mcp_broker.purge_session(sid)
    assert sid not in exit_mod._sessions


def test_a_credential_takes_its_hosts_and_wildcards():
    cred = ExitCredential(placeholder="p", header="Authorization", hosts=("api.vendor.com", "*.fal.run"),
                          allowlist_key="k", manifest="m")
    assert cred.takes_host("api.vendor.com") and cred.takes_host("API.VENDOR.COM.")
    assert cred.takes_host("queue.fal.run") and cred.takes_host("a.b.fal.run")
    assert not cred.takes_host("fal.run") and not cred.takes_host("vendor.com") and not cred.takes_host("evil-api.vendor.com")
    d = cred.descriptor()
    # The placeholder stays (a session taken back after a restart holds it);
    # the value never, and the command-line credential is the fact of one.
    assert d["placeholder"] == "p" and d["value"] == "" and d["has_value"] is False and d["cli"] is False
    gw = cred.gateway_for("api.vendor.com")
    assert gw.upstream == "https://api.vendor.com" and gw.allowlist_key == "k" and gw.header == "Authorization"


def test_the_boot_retires_the_platforms_own_authority(tmp_path, monkeypatch):
    """An earlier release kept an authority of the platform's own exit under
    ``<PLATFORM_DATA_DIR>/credential-exit/``: the boot removes its two files,
    and the folder once nothing else is in it; a file it never made stays,
    and the folder with it; with nothing there it removes nothing."""
    monkeypatch.setattr(config, "PLATFORM_DATA_DIR", tmp_path)
    folder = tmp_path / "credential-exit"
    assert exit_mod.retire_platform_authority() == 0 and not folder.exists()
    exit_authority.ExitAuthority(folder).load_or_make()
    assert sorted(p.name for p in folder.iterdir()) == sorted([exit_authority.KEY_NAME, exit_authority.CERT_NAME])
    assert exit_mod.retire_platform_authority() == 2 and not folder.exists()
    exit_authority.ExitAuthority(folder).load_or_make()
    (folder / "notes.txt").write_text("kept", encoding="utf-8")
    assert exit_mod.retire_platform_authority() == 2
    assert [p.name for p in folder.iterdir()] == ["notes.txt"]
    assert exit_mod.retire_platform_authority() == 0
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "kept"
    # A leaf a stopped make left in the leaves folder goes, the folder too.
    (folder / "notes.txt").unlink()
    leaves = folder / exit_authority.LEAVES_DIRNAME
    leaves.mkdir()
    (leaves / ".host-1.key").write_text("left", encoding="utf-8")
    (leaves / ".host-1.pem").write_text("left", encoding="utf-8")
    assert exit_mod.retire_platform_authority() == 2 and not folder.exists()


@pytest.mark.parametrize("linked", ["folder", "leaves"])
def test_the_boot_leaves_a_linked_authority_folder_alone(tmp_path, monkeypatch, caplog, linked):
    """A link at the authority's folder or at its leaves is never followed:
    the boot removes nothing there and says so once."""
    import logging
    monkeypatch.setattr(config, "PLATFORM_DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in (exit_authority.KEY_NAME, exit_authority.CERT_NAME, ".host-1.key", ".host-1.pem"):
        (elsewhere / name).write_text("theirs", encoding="utf-8")
    folder = tmp_path / "data" / "credential-exit"
    if linked == "folder":
        folder.symlink_to(elsewhere, target_is_directory=True)
    else:
        folder.mkdir()
        (folder / exit_authority.LEAVES_DIRNAME).symlink_to(elsewhere, target_is_directory=True)
    with caplog.at_level(logging.WARNING, logger="otodock.credential-exit"):
        assert exit_mod.retire_platform_authority() == 0
    assert sorted(p.name for p in elsewhere.iterdir()) == sorted(
        [exit_authority.KEY_NAME, exit_authority.CERT_NAME, ".host-1.key", ".host-1.pem"])
    assert any("is a link" in m for m in caplog.messages)


def test_stop_ends_an_attached_connection_under_uvloop(ca):
    """A loop whose ``Server`` has no ``abort_clients`` (uvloop's): the
    relay's stop with a connection attached ends it and returns within a
    moment, and gives the port back."""
    import uvloop

    async def scenario() -> None:
        machine = MachineExit(ca)
        port = await machine.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.wait_for(machine.stop(), 5)
            assert await asyncio.wait_for(reader.read(), 5) == b""
        finally:
            writer.close()
        assert machine.relay is not None and machine.relay.port == 0

    uvloop.run(scenario())


# --- the exit's file work, off the loop ----------------------------------------

#: A declared host the exit has no leaf for yet: the vendor's leaf names
#: ``localhost``, so the forward toward this one answers 502 after a good
#: handshake on a new leaf.
FRESH = "127.0.0.1"


def _made_on(monkeypatch, *, fail_first: bool = False, hold: threading.Event | None = None) -> list[str]:
    """``_make_leaf`` recorded by the thread it ran on; the first one
    refused, or held until ``hold`` is set."""
    threads: list[str] = []
    real = exit_authority._make_leaf

    def make(authority_, host, leaves_dir):
        threads.append(threading.current_thread().name)
        if fail_first and len(threads) == 1:
            raise OSError("the disk refused the write")
        if hold is not None:
            hold.wait(10)
        return real(authority_, host, leaves_dir)

    monkeypatch.setattr(exit_authority, "_make_leaf", make)
    return threads


async def _connect_raw(port: int, secret: str, target: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    auth = base64.b64encode(f"oto:{secret}".encode()).decode()
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
                 .encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), bound(10.0))
    return reader, writer, head


async def _settles(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + bound(seconds)
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@pytest.mark.asyncio
async def test_an_ended_connection_does_no_disk_work_on_the_loop(machine, vendor, authority, monkeypatch,
                                                               loop_disk_guard):
    """A cached leaf comes from memory and a new one is made on the exit's
    own thread, as a machine's exit makes it: neither holds the relay's
    loop."""
    made_on = _made_on(monkeypatch)
    sid, placeholder, secret = await _session(machine, hosts=(VENDOR, FRESH))
    async with _client(machine.port, secret, authority) as client:
        with loop_disk_guard.active():
            cached = await client.get(f"https://{VENDOR}:{vendor.port}/x",
                                      headers={"Authorization": f"Bearer {placeholder}"})
            fresh = await client.get(f"https://{FRESH}:{vendor.port}/x")
    assert cached.status_code == 200
    assert fresh.status_code == 502
    assert made_on and all(name.startswith("credential-exit") for name in made_on)
    assert machine.authority.cached_context(FRESH) is not None


@pytest.mark.asyncio
async def test_a_failed_make_ends_that_connection_alone_after_its_200(machine, vendor, authority, monkeypatch):
    made_on = _made_on(monkeypatch, fail_first=True)
    sid, placeholder, secret = await _session(machine, hosts=(VENDOR, FRESH))
    reader, writer, head = await _connect_raw(machine.port, secret, f"{FRESH}:{vendor.port}")
    try:
        assert head.startswith(b"HTTP/1.1 200")
        assert await asyncio.wait_for(reader.read(), bound(10.0)) == b""
    finally:
        writer.close()
    async with _client(machine.port, secret, authority) as client:
        again = await client.get(f"https://{FRESH}:{vendor.port}/x")
        other = await client.get(f"https://{VENDOR}:{vendor.port}/x",
                                 headers={"Authorization": f"Bearer {placeholder}"})
    assert again.status_code == 502 and other.status_code == 200
    assert len(made_on) == 2


@pytest.mark.asyncio
async def test_a_make_held_past_the_bound_ends_the_connection_and_lands_later(machine, vendor, authority,
                                                                             monkeypatch):
    monkeypatch.setattr(exit_relay, "_HEAD_TIMEOUT_S", 1.0)
    release = threading.Event()
    timer = threading.Timer(10.0, release.set)
    timer.daemon = True
    timer.start()
    made_on = _made_on(monkeypatch, hold=release)
    sid, placeholder, secret = await _session(machine, hosts=(VENDOR, FRESH))
    try:
        reader, writer, head = await _connect_raw(machine.port, secret, f"{FRESH}:{vendor.port}")
        try:
            assert head.startswith(b"HTTP/1.1 200")
            assert await asyncio.wait_for(reader.read(), bound(4.0)) == b""
        finally:
            writer.close()
        assert made_on and machine.authority.cached_context(FRESH) is None
    finally:
        release.set()
    assert await _settles(lambda: machine.authority.cached_context(FRESH) is not None)


def test_an_ended_connection_toward_a_new_host_completes_under_uvloop(ca, authority, monkeypatch):
    """uvloop's upgrade to TLS does not take over what the plain reader
    already read. The client sends its first TLS message as soon as it reads
    the 200, while a new leaf is still being made: that message must wait
    for the upgrade, never reach the plain reader."""
    import uvloop
    _approve_every_host(monkeypatch)
    real = exit_authority._make_leaf

    def slow_make(authority_, host, leaves_dir):
        time.sleep(0.2)  # the client's first TLS message arrives during the make
        return real(authority_, host, leaves_dir)

    monkeypatch.setattr(exit_authority, "_make_leaf", slow_make)
    statuses: list[int] = []

    async def scenario() -> None:
        vendor = _Vendor(ca)
        await vendor.start()
        machine = MachineExit(ca, dial_refused=_loopback_passes)
        port = await machine.start()
        try:
            sid, placeholder, secret = await _session(machine, hosts=(VENDOR, FRESH))
            async with httpx.AsyncClient(proxy=f"http://oto:{secret}@127.0.0.1:{port}", verify=_trusting(authority),
                                         follow_redirects=False, trust_env=False, timeout=bound(5.0)) as client:
                fresh = await client.get(f"https://{FRESH}:{vendor.port}/x")
                cached = await client.get(f"https://{VENDOR}:{vendor.port}/x",
                                          headers={"Authorization": f"Bearer {placeholder}"})
            statuses.extend([fresh.status_code, cached.status_code])
        finally:
            await machine.stop()
            assert vendor.server is not None
            vendor.server.close()  # uvloop's Server has no abort_clients
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(vendor.server.wait_closed(), 5)
            mcp_broker._store.clear()

    uvloop.run(scenario())
    # The new host's forward fails after a good handshake on the new leaf
    # (the vendor's certificate names localhost alone): 502, never a hang.
    assert statuses == [502, 200]


# ---------------------------------------------------------------------------
# A machine's exit asks the platform about a narrowed command-line
# credential's requests
# ---------------------------------------------------------------------------

async def _cli_session(machine: MachineExit, reach: dict | None, tmp_path) -> tuple[str, str, str]:
    """A session whose command-line credential, with ``reach``, is held as a
    placeholder toward the vendor's host."""
    from core.credentials.mcp_gateway import CliCredential, TokenRef
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": SECRET, "expires_at": "", "extra": {}}))
    cli = CliCredential(host=VENDOR, token_ref=TokenRef(str(tmp_path), "acct"), reach=reach)
    sid = str(uuid.uuid4())
    placeholder = exit_mod.new_placeholder()
    cred = ExitCredential(placeholder=placeholder, header="Authorization", hosts=(VENDOR,),
                          allowlist_key="github-mcp", manifest="github-mcp", cli=cli)
    mcp_broker.provision(sid, {"github-mcp": SecretBundle(cli=cli, exit=(cred,))})
    return sid, placeholder, await machine.open(sid)


@pytest.mark.asyncio
async def test_a_narrowed_reach_is_judged_at_a_machines_exit_before_the_token_is_added(machine, vendor,
                                                                                      authority, tmp_path):
    sid, placeholder, secret = await _cli_session(machine, {"repositories": [], "read_only": True}, tmp_path)
    async with _client(machine.port, secret, authority) as client:
        read = await client.get(f"https://{VENDOR}:{vendor.port}/acme/app/issues",
                                headers={"Authorization": f"Bearer {placeholder}"})
        write = await client.post(f"https://{VENDOR}:{vendor.port}/acme/app/issues",
                                  headers={"Authorization": f"Bearer {placeholder}"}, json={"title": "x"})
    assert read.status_code == 200 and vendor.seen[0]["headers"]["authorization"] == f"Bearer {SECRET}"
    # The stand-in host is none of GitHub's git hosts, so its refusal is a JSON message.
    assert write.status_code == 403
    assert write.json() == {"message": "This agent may only read on GitHub. This request to GitHub changes "
                                       "something, so it was refused."}
    assert len(vendor.seen) == 1


@pytest.mark.asyncio
async def test_with_nothing_chosen_a_machines_exit_asks_no_rule(machine, vendor, authority, tmp_path, monkeypatch):
    from auth import github_reach
    asked: list = []
    monkeypatch.setattr(github_reach, "judge_request", lambda *a, **k: asked.append(a) or github_reach.RequestVerdict())
    sid, placeholder, secret = await _cli_session(machine, None, tmp_path)
    async with _client(machine.port, secret, authority) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/any/repo/issues",
                              headers={"Authorization": f"Bearer {placeholder}"}, json={"title": "x"})
    assert r.status_code == 200 and asked == []
    assert vendor.seen[0]["headers"]["authorization"] == f"Bearer {SECRET}"


def test_the_boot_retires_the_old_authority_and_sweeps_private_keys_off_the_loop():
    """Every boot removes what an earlier release left: the platform's own
    exit authority and the private key files in the scopes' key folders,
    each off the loop (``startup._boot_services``)."""
    import inspect

    import startup
    source = inspect.getsource(startup._boot_services)
    assert "await asyncio.to_thread(_credential_exit.retire_platform_authority)" in source
    assert "await asyncio.to_thread(_scd.sweep_private_ssh_keys)" in source
