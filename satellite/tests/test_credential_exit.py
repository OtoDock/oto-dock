"""The credential exit on a machine (``satellite/sessions/credential_exit.py``
over the vendored relay): the listener and what it reports, a door from the
payload's exit block, the value read from the gateway's table and landing in
the declared header alone, admission by the session's secret, the compute
machine's dial rule, the heal after an accept failure, the three OS rows, the
render's bundle bind.

The vendor is a TLS server on loopback with a leaf an authority of the test's
own signed; the relay's forward trusts it through ``_upstream_verify``.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import h11
import httpx
import pytest
import pytest_asyncio

from satellite import config as satconfig
from satellite._vendored import (
    exit_authority,
    exit_relay,
    outbound_url,
    sandbox_render as render,
)
from satellite.config import SatelliteConfig
from satellite.sessions import credential_exit as cx, sandbox_host
from satellite.transport import mcp_gateway as gw
from satellite.transport.http_tunnel import make_loop_exception_handler

SECRET_VALUE = "sk-vendor-value-0123456789abcdef"
VENDOR = "localhost"


class _Vendor:
    """One HTTP/1.1 server over TLS: what it was sent, by request."""

    def __init__(self, authority: exit_authority.ExitAuthority) -> None:
        self.seen: list[dict] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.authority = authority
        # Set, every answer is a 302 to it.
        self.redirect = ""

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0,
                                                 ssl=self.authority.leaf_context(VENDOR))
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            if hasattr(self.server, "abort_clients"):  # 3.13+; the satellite's floor is 3.10
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
            payload = json.dumps({"ok": True, "n": len(self.seen)}).encode()
            status, extra = (302, [(b"location", self.redirect.encode())]) if self.redirect else (200, [])
            answer = conn.send(h11.Response(status_code=status, headers=[
                (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *extra]))
            answer += conn.send(h11.Data(data=payload)) + conn.send(h11.EndOfMessage())
            writer.write(answer)
            await writer.drain()
        finally:
            writer.close()


@pytest.fixture
def roots(monkeypatch):
    """A short OtoDock root for the machine, a fresh module state, the
    listener stopped after."""
    root = Path(tempfile.mkdtemp(prefix="oto-exit-"))
    monkeypatch.setattr(cx, "otodock_dir", lambda: root / ".oto-dock")
    monkeypatch.setattr(sandbox_host, "otodock_dir", lambda: root / ".oto-dock")
    monkeypatch.setattr(cx, "_state", cx._State())
    # No test installs packages into the venv it runs in.
    monkeypatch.setattr(cx, "_install_exit_lock", lambda: False)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY",
                 "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield root
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if cx._state.relay is not None and not loop.is_closed():
            loop.run_until_complete(cx.stop_listener())
    except RuntimeError:
        pass
    shutil.rmtree(root, ignore_errors=True)


@pytest_asyncio.fixture
async def vendor(roots, monkeypatch):
    authority = exit_authority.ExitAuthority(roots / "vendor-ca")
    v = _Vendor(authority)
    await v.start()
    monkeypatch.setattr(cx, "_upstream_verify", str(roots / "vendor-ca" / exit_authority.CERT_NAME))
    yield v
    await v.stop()


def _block(placeholder: str, *, hosts=(VENDOR,), header="Authorization", prefix="Bearer ",
           secret: str | None = None, token_hash: str = "hash-1", carves=()) -> dict:
    return {"secret": secret or secrets.token_hex(16), "token_hash": token_hash, "carves": list(carves),
            "entries": [{"placeholder": placeholder, "header": header, "prefix": prefix, "key": "PROBE_KEY",
                         "hosts": list(hosts)}]}


def _push(table: gw.GatewayTable, token_hash: str, placeholder: str, value: str = SECRET_VALUE, **over) -> None:
    assert table.push({"type": "mcp_gateway_token", "session_id": "s", "token_hash": token_hash,
                       "mcp": placeholder, "header": "Authorization", "value": value,
                       "upstream": f"https://{VENDOR}", "expires_in": 600, **over})


def _client(port: int, secret: str, bundle: Path) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=f"http://oto:{secret}@127.0.0.1:{port}", verify=str(bundle),
                             follow_redirects=False, timeout=10.0)


async def _door(table: gw.GatewayTable, *, compute: bool = False, **over) -> tuple[str, str, dict, int]:
    """A listener and one session's door with a pushed value: the session
    id, its placeholder, its block and the exit's port."""
    port = await cx.start_listener(table, compute=compute)
    assert port > 0
    sid, placeholder = str(uuid.uuid4()), exit_relay.new_placeholder()
    block = _block(placeholder, **over)
    cx.start(sid, {"exit": block})
    _push(table, block["token_hash"], placeholder)
    return sid, placeholder, block, port


# --- the listener and what it reports -----------------------------------------


@pytest.mark.asyncio
async def test_the_listener_starts_and_reports_its_port(roots):
    table = gw.GatewayTable()
    assert cx.state() == {} and cx.port() == 0 and cx.render_values() == (0, "")
    port = await cx.start_listener(table, compute=False)
    assert port > 0
    assert cx.state() == {"exit_port": port, "exit_ok": True, "exit_why": "", "exit_upstream": False}
    assert cx.port() == port
    # No authority and no bundle until a session's door: a flag-off machine holds no key.
    assert not (roots / ".oto-dock" / cx.AUTHORITY_DIRNAME).exists()
    assert cx.render_values() == (0, "")
    # Everything answers 407 while no door exists.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    await writer.drain()
    answer = await reader.read()
    writer.close()
    assert answer.startswith(b"HTTP/1.1 407")


@pytest.mark.asyncio
@pytest.mark.parametrize("variable, through", [("HTTPS_PROXY", True), ("https_proxy", True), ("ALL_PROXY", True),
                                                ("HTTP_PROXY", False), ("http_proxy", False)])
async def test_behind_its_own_proxy_a_machine_runs_its_exit_through_it(roots, monkeypatch, variable, through, caplog):
    """An outgoing proxy for https (or for all) is the exit's way out, and
    the platform reads it as ``exit_upstream``; a plain http proxy alone
    carries none of the exit's encrypted connections."""
    caplog.set_level(logging.INFO, logger="satellite.credential-exit")
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "http://user:pw@corp.example:3128")
    port = await cx.start_listener(gw.GatewayTable(), compute=False)
    assert port > 0
    assert cx.state() == {"exit_port": port, "exit_ok": True, "exit_why": "", "exit_upstream": through}
    assert "pw" not in caplog.text and "user:" not in caplog.text


@pytest.mark.asyncio
async def test_a_socks_proxy_leaves_the_listener_up_and_says_why(roots, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="satellite.credential-exit")
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://corp.example:1080")
    port = await cx.start_listener(gw.GatewayTable(), compute=False)
    assert port > 0
    assert cx.state() == {"exit_port": port, "exit_ok": False, "exit_why": "socks", "exit_upstream": False}
    assert "SOCKS" in caplog.text


@pytest.mark.asyncio
async def test_a_machine_without_the_packages_runs_no_exit_and_starts_its_sessions(roots, monkeypatch, caplog):
    import satellite._vendored as vendored
    monkeypatch.setitem(sys.modules, "satellite._vendored.exit_relay", None)
    monkeypatch.delattr(vendored, "exit_relay")
    # The fixture's install fails, as on a host with no wheel for them.
    assert await cx.start_listener(gw.GatewayTable(), compute=False) == 0
    for _ in range(50):
        if "runs no exit" in caplog.text:
            break
        await asyncio.sleep(0.02)
    assert cx.state() == {"exit_ok": False, "exit_why": "packages"} and "could not be installed" in caplog.text
    assert await cx.start_listener(gw.GatewayTable(), compute=True) == 0
    assert "package is missing" in caplog.text and cx.state()["exit_why"] == "packages"
    # A block that still arrives opens no door and raises nothing.
    cx.start("sid", {"exit": _block("oto-exit-" + "0" * 32)})
    assert cx._state.doors == {} and "runs no exit" in caplog.text


@pytest.mark.asyncio
async def test_a_machine_updated_without_the_packages_installs_them_and_then_listens(roots, monkeypatch, caplog):
    """The update to this version is run by the previous one, which knows
    only requirements.txt: the new satellite installs the exit's own lock
    itself, in the background so its boot (and the update's boot guard) is
    never held, and listens once it has the packages; the next heartbeat
    carries the port. A compute machine never installs: its image or its
    setup does, and its venv is read-only there."""
    caplog.set_level(logging.INFO, logger="satellite.credential-exit")
    import satellite._vendored as vendored
    real = vendored.exit_relay
    monkeypatch.setitem(sys.modules, "satellite._vendored.exit_relay", None)
    monkeypatch.delattr(vendored, "exit_relay")
    installed: list[int] = []

    def install() -> bool:
        installed.append(1)
        sys.modules["satellite._vendored.exit_relay"] = real
        vendored.exit_relay = real
        return True
    monkeypatch.setattr(cx, "_install_exit_lock", install)
    table = gw.GatewayTable()
    assert await cx.start_listener(table, compute=True) == 0
    await asyncio.sleep(0.05)  # an install, had one been spawned, would have run by now
    assert installed == [] and cx.state() == {"exit_ok": False, "exit_why": "packages"}
    told: list[int] = []

    async def heartbeat_now() -> None:
        told.append(1)
    cx._state.install_tried = False
    assert await cx.start_listener(table, compute=False, on_ready=heartbeat_now) == 0
    # While the packages install, the machine says so: a start waits for it.
    assert cx.state() == {"exit_ok": False, "exit_why": "installing"}
    for _ in range(100):
        if cx.port() and told:
            break
        await asyncio.sleep(0.02)
    assert installed == [1] and cx.port() > 0 and cx.state()["exit_ok"] is True and cx.state()["exit_why"] == ""
    # The platform hears of the listener at once, not at the next heartbeat.
    assert told == [1]
    assert "installing" in caplog.text


@pytest.mark.parametrize("compute", [False, True])
@pytest.mark.parametrize("why", ["installing", "packages"])
def test_a_sign_in_goes_on_while_the_exits_packages_are_missing(monkeypatch, compute, why):
    """The sign-in hands the exit the platform's egress block, which reads
    whether a proxy keeps the exit off. Without the exit's packages (still
    installing after an update, or a host with no build of one) the sign-in
    goes on and the reason stands: the machine never stays away for it."""
    import satellite._vendored as vendored
    monkeypatch.setitem(sys.modules, "satellite._vendored.exit_relay", None)
    monkeypatch.delattr(vendored, "exit_relay")
    monkeypatch.setattr(cx._state, "why", why)
    block = {"https": "http://proxy.corp.test:3128", "no_proxy": "localhost"}
    cx.set_platform_egress(block, compute=compute)
    cx.set_platform_egress(None, compute=compute)
    assert cx.state() == {"exit_ok": False, "exit_why": why} and cx.upstream_on() is False


@pytest.mark.asyncio
async def test_a_failed_install_leaves_the_machine_without_an_exit_once(roots, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="satellite.credential-exit")
    import satellite._vendored as vendored
    monkeypatch.setitem(sys.modules, "satellite._vendored.exit_relay", None)
    monkeypatch.delattr(vendored, "exit_relay")
    tries: list[int] = []
    monkeypatch.setattr(cx, "_install_exit_lock", lambda: tries.append(1) or False)
    table = gw.GatewayTable()
    assert await cx.start_listener(table, compute=False) == 0
    for _ in range(50):
        if "stay off" in caplog.text:
            break
        await asyncio.sleep(0.02)
    assert tries == [1] and cx.state() == {"exit_ok": False, "exit_why": "packages"} and "stay off" in caplog.text
    # A second start (a heal, a later boot path) never installs again.
    assert await cx.start_listener(table, compute=False) == 0
    await asyncio.sleep(0.05)
    assert tries == [1]


# --- a door, a value, the header --------------------------------------------------


@pytest.mark.asyncio
async def test_the_pushed_value_lands_in_the_declared_header_alone(roots, vendor):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    bundle = cx.bundle_path()
    assert bundle.is_file() and bundle.read_text().count("BEGIN CERTIFICATE") >= 2
    assert cx.render_values() == (port, str(bundle))
    async with _client(port, block["secret"], bundle) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/items", json={"a": 1},
                              headers={"Authorization": f"Bearer {placeholder}", "X-Other": "plain"})
    assert r.status_code == 200 and r.json()["ok"] is True
    seen = vendor.seen[-1]
    assert seen["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    assert seen["headers"]["x-other"] == "plain" and json.loads(seen["body"]) == {"a": 1}
    assert seen["headers"]["host"] == f"{VENDOR}:{vendor.port}"
    assert placeholder not in json.dumps(seen) and "proxy-authorization" not in seen["headers"]
    # The placeholder anywhere else refuses the request before any dial.
    async with _client(port, block["secret"], bundle) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items?key={placeholder}",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 400 and exit_relay.REFUSAL_PLACE in r.text
    assert len(vendor.seen) == 1
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_redirect_is_passed_back_and_never_followed(roots, vendor):
    """A 3xx from the declared host goes back to the tool as it came: the
    exit dials nothing else, so the value never reaches the Location. The
    header is a custom one, since an HTTP client following a redirect drops
    Authorization by itself and would hide the rule."""
    other = _Vendor(exit_authority.ExitAuthority(roots / "vendor-ca"))
    await other.start()
    try:
        vendor.redirect = f"https://{VENDOR}:{other.port}/next"
        table = gw.GatewayTable()
        port = await cx.start_listener(table, compute=False)
        sid, placeholder = str(uuid.uuid4()), exit_relay.new_placeholder()
        block = _block(placeholder, header="X-Api-Key", prefix="")
        cx.start(sid, {"exit": block})
        _push(table, block["token_hash"], placeholder, header="X-Api-Key")
        async with _client(port, block["secret"], cx.bundle_path()) as client:
            r = await client.get(f"https://{VENDOR}:{vendor.port}/start", headers={"X-Api-Key": placeholder})
        assert r.status_code == 302 and r.headers["location"] == vendor.redirect
        assert vendor.seen[-1]["headers"]["x-api-key"] == SECRET_VALUE
        assert other.seen == []
        cx.stop(sid)
    finally:
        await other.stop()


@pytest.mark.asyncio
async def test_a_door_with_no_entry_is_a_plain_tunnel_and_makes_the_bundle(roots, vendor):
    """A compute session whose tools declare nothing for the exit still has
    the exit in its sandbox's variables: its door has no entry, the bundle
    the render binds exists, and an HTTPS request goes through untouched."""
    table = gw.GatewayTable()
    port = await cx.start_listener(table, compute=False)
    assert cx.render_values() == (0, "")
    sid = str(uuid.uuid4())
    block = {"secret": secrets.token_hex(16), "token_hash": "hash-empty", "carves": [], "entries": []}
    cx.start(sid, {"exit": block})
    bundle = cx.bundle_path()
    assert bundle.is_file() and cx.render_values() == (port, str(bundle))
    vendor_ca = roots / "vendor-ca" / exit_authority.CERT_NAME  # a raw tunnel: the client meets the vendor itself
    async with _client(port, block["secret"], vendor_ca) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/plain", headers={"Authorization": "Bearer mine"})
    assert r.status_code == 200 and vendor.seen[-1]["headers"]["authorization"] == "Bearer mine"
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_wrong_secret_is_refused_before_anything_is_read(roots, vendor):
    """Admission is the session's own secret: another string, or another
    session's, opens nothing of this session."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    bundle = cx.bundle_path()
    async with _client(port, "not-the-secret", bundle) as client:
        with pytest.raises(httpx.ProxyError, match="407"):
            await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert vendor.seen == []
    # A second session's secret admits that session alone: this session's
    # placeholder is a string nobody can use through it (a raw tunnel, since
    # the second session holds no entry for the vendor's host).
    other = str(uuid.uuid4())
    cx.start(other, {"exit": _block("oto-exit-" + "f" * 32, hosts=("api.example.test",), secret="other-secret",
                                     token_hash="hash-2")})
    vendor_ca = roots / "vendor-ca" / exit_authority.CERT_NAME  # a raw tunnel: the client meets the vendor itself
    async with _client(port, "other-secret", vendor_ca) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200
    assert vendor.seen[-1]["headers"]["authorization"] == f"Bearer {placeholder}"


@pytest.mark.asyncio
async def test_no_value_refuses_and_a_revoked_one_says_why(roots, vendor, monkeypatch):
    monkeypatch.setattr(cx, "_PUSH_WAIT_S", 0.2)
    table = gw.GatewayTable()
    port = await cx.start_listener(table, compute=False)
    sid, placeholder = str(uuid.uuid4()), exit_relay.new_placeholder()
    block = _block(placeholder)
    cx.start(sid, {"exit": block})
    bundle = cx.bundle_path()
    async with _client(port, block["secret"], bundle) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 403 and cx.REFUSAL_NO_VALUE in r.text
    _push(table, block["token_hash"], placeholder, value="-", expires_in=0, kind="needs_reconnect", reason="x")
    async with _client(port, block["secret"], bundle) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 403 and "disconnected" in r.text
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_a_second_start_replaces_the_door_and_stop_drops_it(roots, vendor):
    """A terminal re-opened under its id: the new block's secret is the
    admitted one, the old refused; the stop closes the door."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    bundle = cx.bundle_path()
    again = _block(placeholder, secret="second-secret", token_hash=block["token_hash"])
    cx.start(sid, {"exit": again})
    async with _client(port, "second-secret", bundle) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200 and vendor.seen[-1]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    async with _client(port, block["secret"], bundle) as client:
        with pytest.raises(httpx.ProxyError, match="407"):
            await client.get(f"https://{VENDOR}:{vendor.port}/items")
    cx.stop(sid)
    async with _client(port, "second-secret", bundle) as client:
        with pytest.raises(httpx.ProxyError, match="407"):
            await client.get(f"https://{VENDOR}:{vendor.port}/items")
    cx.stop(sid)  # a second stop is nothing


@pytest.mark.asyncio
async def test_the_same_block_again_keeps_the_door_and_its_client(roots, vendor):
    """Direct LLM on compute opens the session's block once per tool: a
    tool opened mid-run must not cut another tool's request in flight, so
    the same secret and token hash update the door in place and keep its
    connection pool."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200
    door = cx._state.doors[sid]
    pool = door.client
    assert pool is not None
    more = exit_relay.new_placeholder()
    again = {**block, "entries": [*block["entries"], {**block["entries"][0], "placeholder": more}]}
    cx.start(sid, {"exit": again})
    assert cx._state.doors[sid] is door and door.client is pool and set(door.entries) == {placeholder, more}
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_block_with_another_token_hash_replaces_the_door(roots, vendor):
    """The same secret under a new token hash is a new session token (a
    session re-minted after a restart): its values are pushed under the new
    hash, so the door is a new one reading them, never the old one updated."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    door = cx._state.doors[sid]
    cx.start(sid, {"exit": {**block, "token_hash": block["token_hash"] + "-new"}})
    again = cx._state.doors[sid]
    assert again is not door and again.token_hash == block["token_hash"] + "-new"
    cx.stop(sid)


# --- the compute machine's dial rule -----------------------------------------------


@pytest.mark.asyncio
async def test_on_compute_a_private_target_outside_the_carves_is_refused(roots):
    """The exit dials from outside the sandbox's namespace, so it applies the
    shim's rule: the host's loopback, the metadata address and a private
    address stay closed; a carved address passes."""
    table = gw.GatewayTable()
    port = await cx.start_listener(table, compute=True)
    sid, placeholder = str(uuid.uuid4()), exit_relay.new_placeholder()
    block = _block(placeholder, hosts=("api.example.test",), carves=("192.168.250.251",))
    cx.start(sid, {"exit": block})
    auth = base64.b64encode(f"oto:{block['secret']}".encode()).decode()
    for target in ("192.168.250.250:443", "127.0.0.1:443", "169.254.169.254:80", "[::1]:443"):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
                     .encode())
        await writer.drain()
        answer = await reader.read()
        writer.close()
        assert answer.startswith(b"HTTP/1.1 403") and exit_relay.REFUSAL_WHERE.encode() in answer, target
    backend = cx._Backend()
    assert await backend.addresses(sid, "192.168.250.251", 443) == ["192.168.250.251"]
    assert await backend.addresses(sid, "192.168.250.250", 443) is None
    assert await backend.addresses(sid, "127.0.0.1", 443) is None
    # The forward pins the dial to the checked address and keeps the host as the SNI.
    url, extensions = backend.forward_url(sid, "api.example.test", 443, "/x", ["93.184.216.34"])
    assert url == "https://93.184.216.34:443/x" and extensions == {"sni_hostname": "api.example.test"}
    url6, _ = backend.forward_url(sid, "api.example.test", 443, "/x", ["2606:4700::1111"])
    assert url6 == "https://[2606:4700::1111]:443/x"


@pytest.mark.asyncio
async def test_on_a_paired_computer_every_address_passes_and_the_forward_names_the_host(roots):
    table = gw.GatewayTable()
    port = await cx.start_listener(table, compute=False)
    sid, placeholder = str(uuid.uuid4()), exit_relay.new_placeholder()
    cx.start(sid, {"exit": _block(placeholder, hosts=("api.example.test",))})
    backend = cx._Backend()
    assert await backend.addresses(sid, "nas.lan", 443) == ["nas.lan"]
    assert backend.forward_url(sid, "api.example.test", 443, "/x", ["nas.lan"]) == ("https://api.example.test/x", {})
    assert backend.forward_url(sid, "api.example.test", 8443, "/x", ["a"]) == ("https://api.example.test:8443/x", {})
    assert port > 0


@pytest.mark.asyncio
async def test_on_compute_the_value_lands_through_a_pinned_forward(roots, vendor, monkeypatch):
    """The round trip on a compute machine, the shim's rule patched open for
    the loopback vendor: the forward dials the checked address with the
    vendor's name as the SNI and the value lands."""
    monkeypatch.setattr(outbound_url, "sandbox_dial_refused", lambda *a, **k: False)
    # The vendor listens on 127.0.0.1, and the forward dials the first address
    # the name answers: where ``localhost`` answers ``::1`` first it met nothing.
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: (
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", int(port or 0)))]
        if host == VENDOR else real(host, port, *a, **k)))
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table, compute=True)
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/items", json={"a": 1},
                              headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200
    assert vendor.seen[-1]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    assert vendor.seen[-1]["headers"]["host"] == f"{VENDOR}:{vendor.port}"


# --- the heal ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_accept_failure_heals_the_exit_on_its_port(roots, caplog):
    caplog.set_level(logging.INFO, logger="satellite.credential-exit")
    table = gw.GatewayTable()
    port = await cx.start_listener(table, compute=False)
    loop = asyncio.get_running_loop()
    tunnel = SimpleNamespace(heal_soon=lambda trigger: pytest.fail("the tunnel's heal ran for the exit's socket"))
    handler = make_loop_exception_handler(tunnel, quiet_close_resets=True, exit_listener=cx)
    context = {"message": "Accept failed on a socket",
               "exception": ConnectionResetError(10054, "An existing connection was forcibly closed"),
               "socket": cx._state.relay.sock}
    handler(loop, context)
    assert cx.state() == {"exit_port": port, "exit_ok": False, "exit_why": "bind", "exit_upstream": False}
    loop._stop_serving(cx._state.relay.sock)  # what the Proactor does after the handler returns
    await cx._state.heal_task
    assert cx.state() == {"exit_port": port, "exit_ok": True, "exit_why": "", "exit_upstream": False}
    assert "stopped accepting" in caplog.text and "listening again" in caplog.text
    # The port answers again (407: no door yet).
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    await writer.drain()
    assert (await reader.read()).startswith(b"HTTP/1.1 407")
    writer.close()
    # Another socket's failure is the tunnel's.
    seen: list[str] = []
    tunnel2 = SimpleNamespace(heal_soon=seen.append)
    handler2 = make_loop_exception_handler(tunnel2, quiet_close_resets=True, exit_listener=cx)
    with socket.socket() as other:
        handler2(loop, {**context, "socket": other})
    assert seen == ["an accept failure"]


# --- the three OS rows ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ["linux", "darwin", "windows"])
async def test_the_three_os_rows_run_the_same_listener_and_authority(roots, vendor, monkeypatch, row):
    """Nothing in the exit is posix-only: the Windows row runs with the
    attributes Windows lacks removed and the mode checks off."""
    monkeypatch.setattr(satconfig, "HOST", satconfig.ROWS[row])
    if row == "windows":
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.delattr(os, "fchmod", raising=False)
        monkeypatch.setattr(exit_authority, "_POSIX", False)
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    bundle = cx.bundle_path()
    assert bundle.is_file()
    async with _client(port, block["secret"], bundle) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200 and vendor.seen[-1]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    if row == "windows":
        # The bundle was written once more at the door: no mode compare took part.
        assert cx._state.authority.write_bundle(bundle) is False


# --- the render -------------------------------------------------------------------------


def _compute_config(tmp: Path) -> SatelliteConfig:
    agents = tmp / "platform" / "agents"
    (agents / "pa" / "workspace").mkdir(parents=True, exist_ok=True)
    mcps = tmp / "platform" / "mcps"
    mcps.mkdir(parents=True, exist_ok=True)
    return SatelliteConfig(machine_id="c-1", machine_secret="s", platform_url="ws://127.0.0.1:8400/v1/satellite",
                           agents_dir=agents, mcps_dir=mcps, compute=True)


@pytest.mark.asyncio
async def test_the_compute_render_binds_the_bundle_and_refuses_without_the_exit(roots, monkeypatch):
    monkeypatch.setattr(sandbox_host, "_bwrap_has_size", lambda: True)
    monkeypatch.setattr(render, "mem_total_mb", lambda: 16000)
    monkeypatch.setattr(render, "host_resolv_has_loopback_ns", lambda *_a: False)
    cfg = _compute_config(roots)
    desc = render.SandboxDescription(
        agent="pa", binds=(), mcp_binds=(), mcps_root=str(cfg.mcps_dir), extra_binds=(), cwd="/workspace",
        proxy_port=8400, forwards=(render.PROXY_FORWARD,), exit_port=3128)
    # No listener: a description that names an exit is refused.
    with pytest.raises(render.RenderRefused, match="serves no credential exit"):
        sandbox_host.render_command(desc, cfg, tunnel_port=43210, argv=["true"])
    table = gw.GatewayTable()
    port = await cx.start_listener(table, compute=True)
    # The listener alone is not enough: the bundle is written at the first door.
    with pytest.raises(render.RenderRefused, match="serves no credential exit"):
        sandbox_host.render_command(desc, cfg, tunnel_port=43210, argv=["true"])
    cx.start("sid", {"exit": _block(exit_relay.new_placeholder())})
    argv = sandbox_host.render_command(desc, cfg, tunnel_port=43210, argv=["true"])
    forwards = [argv[i + 1] for i, a in enumerate(argv) if a == "--forward"]
    assert forwards == ["8400:43210", f"3128:{port}"]
    j = argv.index(str(cx.bundle_path()))
    assert argv[j - 1] == "--ro-bind" and argv[j + 1] == render.EXIT_BUNDLE_PATH
    # A description without an exit binds nothing, listener or not.
    plain = render.SandboxDescription(
        agent="pa", binds=(), mcp_binds=(), mcps_root=str(cfg.mcps_dir), extra_binds=(), cwd="/workspace",
        proxy_port=8400, forwards=(render.PROXY_FORWARD,))
    assert render.EXIT_BUNDLE_PATH not in sandbox_host.render_command(plain, cfg, tunnel_port=43210, argv=["true"])


def test_the_block_is_read_as_data(roots):
    """A malformed entry is dropped, an empty secret opens no door."""
    # A stand-in the fixture can stop: on 3.10 its teardown finds an open loop.
    cx._state.relay = SimpleNamespace(port=1, owns_socket=lambda s: False, stop=lambda: asyncio.sleep(0))
    cx._state.ok = True
    cx._state.loop = None
    entries = cx._entries_of([{"placeholder": "p1", "header": "Authorization", "hosts": ["a.b"]},
                              {"placeholder": "", "header": "X", "hosts": ["a.b"]},
                              {"placeholder": "p3", "header": "X", "hosts": []}, "junk"])
    assert list(entries) == ["p1"] and entries["p1"].hosts == ("a.b",) and entries["p1"].prefix == ""
    cx.start("sid", {"exit": {"secret": "", "token_hash": "h", "entries": []}})
    assert cx._state.doors == {}
    cx.start("sid", {"exit": "junk"})
    cx.start("sid", {})
    assert cx._state.doors == {}
    assert outbound_url.sandbox_dial_refused("8.8.8.8") is False
    assert isinstance(ipaddress.ip_network("10.0.0.0/8"), ipaddress.IPv4Network)


# --- the tool relay's door -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_relayed_tools_open_carries_the_door_and_the_last_child_drops_it(roots, monkeypatch):
    """Direct LLM's tools on compute: the frame's ``exit`` block opens the
    session's door ahead of the render, and the session's last child ending
    (or the link's drop) closes it."""
    from satellite.sessions.tool_relay import ToolRelay
    from satellite.tests.test_tool_relay import _WS, _bare_child, _open_frame
    monkeypatch.setattr(sandbox_host, "_bwrap_has_size", lambda: True)
    monkeypatch.setattr(render, "mem_total_mb", lambda: 16000)
    monkeypatch.setattr(render, "host_resolv_has_loopback_ns", lambda *_a: False)
    cfg = _compute_config(roots)
    table = gw.GatewayTable()
    await cx.start_listener(table, compute=True)
    relay = ToolRelay(cfg)
    _bare_child(relay)
    ws = _WS()
    block = _block(exit_relay.new_placeholder())
    msg = _open_frame(cfg, exit=block)
    await relay.open(msg, ws)
    assert ws.ack()["status"] == "ok"
    sid = msg["session_id"]
    assert sid in cx._state.doors and cx._state.doors[sid].secret == block["secret"]
    relay.close_all("the test ends")
    await asyncio.sleep(0.05)
    assert sid not in cx._state.doors


# --- the exit's file work, off the machine's loop ------------------------------


def _record_threads(monkeypatch) -> list[tuple[str, int]]:
    """Every leaf write, bundle compare and system bundle read, with the
    thread it ran on."""
    seen: list[tuple[str, int]] = []
    import threading

    def recorded(name: str):
        real = getattr(exit_authority, name)

        def call(*args, **kwargs):
            seen.append((name, threading.get_ident()))
            return real(*args, **kwargs)
        return call

    for name in ("_write_new", "_holds", "system_bundle_text"):
        monkeypatch.setattr(exit_authority, name, recorded(name))
    return seen


@pytest.mark.asyncio
async def test_an_ended_connection_does_its_file_work_off_the_loop(roots, vendor, monkeypatch):
    """A new host's leaf (and the bundle's compare before it) is made on a
    worker thread, and a cached host's connection touches no file at all:
    the machine's loop also carries its link to the platform."""
    import threading
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    seen = _record_threads(monkeypatch)
    loop_thread = threading.get_ident()
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/one", headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200
    # The new leaf, and the bundle's compare before it.
    assert {name for name, _ in seen} >= {"_write_new", "_holds"}
    assert all(thread != loop_thread for _, thread in seen)
    # Another session toward the same host: its door is made first, then the
    # connection, which takes the cached leaf.
    sid2, placeholder2 = str(uuid.uuid4()), exit_relay.new_placeholder()
    block2 = _block(placeholder2, token_hash="hash-2")
    cx.start(sid2, {"exit": block2})
    _push(table, "hash-2", placeholder2)
    seen.clear()
    async with _client(port, block2["secret"], cx.bundle_path()) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/two", headers={"Authorization": f"Bearer {placeholder2}"})
    assert r.status_code == 200 and seen == []
    cx.stop(sid)
    cx.stop(sid2)


def test_two_first_doors_at_once_share_one_authority(roots, monkeypatch):
    import threading
    import time
    made: list[object] = []
    real_init = exit_authority.ExitAuthority.__init__

    def slow_init(self, folder):
        made.append(self)
        time.sleep(0.2)
        real_init(self, folder)

    monkeypatch.setattr(exit_authority.ExitAuthority, "__init__", slow_init)
    workers = [threading.Thread(target=cx._ensure_authority, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert len(made) == 1 and cx._state.authority is made[0]
    assert cx.bundle_path().is_file()


def test_a_first_door_imports_the_authority_before_it_takes_the_lock_the_loop_takes(roots, monkeypatch):
    """The first import of the authority loads ``cryptography`` from disk.
    While it is held, the lock the machine's loop takes at every request
    and every stop stays free."""
    import importlib.abc
    import threading

    import satellite._vendored as vendored
    name = "satellite._vendored.exit_authority"
    started, release = threading.Event(), threading.Event()
    timer = threading.Timer(5.0, release.set)
    timer.daemon = True
    timer.start()

    class _Held(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == name:
                started.set()
                release.wait(5)
            return None

    monkeypatch.delitem(sys.modules, name)
    monkeypatch.delattr(vendored, "exit_authority")
    monkeypatch.setattr(sys, "meta_path", [_Held(), *sys.meta_path])
    door = threading.Thread(target=cx._ensure_authority, daemon=True)
    door.start()
    try:
        assert started.wait(5)
        free = cx._state.lock.acquire(timeout=1)
        if free:
            cx._state.lock.release()
    finally:
        release.set()
        door.join(10)
    # The held import made a second module object: its instance goes.
    monkeypatch.setattr(cx._state, "authority", None)
    assert free


def test_an_ask_that_finds_its_leaf_made_compares_nothing(roots, monkeypatch):
    """A miss queued behind the make of the same host finds the leaf when
    its turn comes and touches no file."""
    cx._ensure_authority()
    cx._new_leaf("fresh.example")
    seen = _record_threads(monkeypatch)
    cx._new_leaf("fresh.example")
    assert seen == []


@pytest.mark.asyncio
async def test_a_machine_makes_its_leaves_one_at_a_time_on_the_exits_own_thread(roots, monkeypatch):
    """Asks for new hosts wait in the exit's own queue of one thread: a
    stalled make holds that thread alone, never the default executor's."""
    import threading
    entered: list[tuple[str, str]] = []
    held, release = threading.Event(), threading.Event()
    timer = threading.Timer(5.0, release.set)
    timer.daemon = True
    timer.start()
    real = cx._new_leaf

    def new_leaf(host: str):
        entered.append((host, threading.current_thread().name))
        if host == "a.example":
            held.set()
            release.wait(5)
        return real(host)

    monkeypatch.setattr(cx, "_new_leaf", new_leaf)
    backend = cx._Backend()
    first = asyncio.ensure_future(backend.leaf_context("a.example"))
    second = None
    try:
        assert await asyncio.to_thread(held.wait, 5)
        second = asyncio.ensure_future(backend.leaf_context("b.example"))
        await asyncio.sleep(0.3)
        assert [host for host, _ in entered] == ["a.example"]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), 10)
    assert [host for host, _ in entered] == ["a.example", "b.example"]
    assert all(thread.startswith("credential-exit") for _, thread in entered)


# --- a judged entry asks the platform, and a large body passes as it arrives ----


GO = {"status": 0, "detail": "", "content_type": "", "wants_body": 0, "body_b64": None, "link": None}


class _Platform:
    """The platform's side of the ask, on loopback over plain HTTP as this
    machine's tunnel is: what it was asked, and the answer it gives."""

    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.answers: list[tuple[int, object]] = []
        self.delay = 0.0
        self.live = self.peak = 0
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            if hasattr(self.server, "abort_clients"):
                self.server.abort_clients()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER)
        request, body = None, b""
        try:
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    data = await reader.read(65536)
                    conn.receive_data(data)
                    if not data:
                        return
                    continue
                if isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    body += bytes(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    break
            headers = {k.decode().lower(): v.decode() for k, v in request.headers}
            self.asked.append({"path": request.target.decode(), "headers": headers, "json": json.loads(body)})
            self.live += 1
            self.peak = max(self.peak, self.live)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
            finally:
                self.live -= 1
            status, answer = self.answers.pop(0) if self.answers else (200, {"verdict": dict(GO)})
            payload = json.dumps(answer).encode()
            writer.write(conn.send(h11.Response(status_code=status, headers=[
                (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                (b"connection", b"close")])) + conn.send(h11.Data(data=payload)) + conn.send(h11.EndOfMessage()))
            await writer.drain()
        finally:
            writer.close()


@pytest_asyncio.fixture
async def platform(roots):
    p = _Platform()
    await p.start()
    yield p
    await p.stop()


async def _judged_door(table: gw.GatewayTable, platform: _Platform | None, *, streams: bool = False,
                       judged: bool = True, token: str = "cli-token") -> tuple[str, str, dict, int]:
    port = await cx.start_listener(table, compute=False)
    sid, placeholder = str(uuid.uuid4()), exit_relay.new_placeholder()
    block = _block(placeholder, prefix="")
    block["entries"][0].update({"streams": streams, "judged": judged})
    config: dict = {"exit": block}
    if platform is not None:
        config.update({"tunnel_port": platform.port, "env": {"OTO_CLI_TOKEN": token}})
    cx.start(sid, config)
    _push(table, block["token_hash"], placeholder)
    return sid, placeholder, block, port


def test_the_door_reads_the_two_keys_as_plain_yes_or_no(roots):
    entries = cx._entries_of([
        {"placeholder": "p1", "header": "Authorization", "hosts": ["github.com"], "streams": True, "judged": True},
        {"placeholder": "p2", "header": "Authorization", "hosts": ["github.com"], "streams": "yes", "judged": 1},
        {"placeholder": "p3", "header": "xi-api-key", "hosts": ["api.elevenlabs.io"]},
    ])
    assert (entries["p1"].streams, entries["p1"].judged) == (True, True)
    assert (entries["p2"].streams, entries["p2"].judged) == (False, False)
    assert (entries["p3"].streams, entries["p3"].judged) == (False, False)


@pytest.mark.asyncio
async def test_a_judged_entrys_request_is_put_to_the_platform_before_the_value_is_added(roots, vendor, platform):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform)
    refusal = "This agent may reach only these GitHub repositories: a/b. c/d is not one of them."
    platform.answers = [(200, {"verdict": dict(GO)}),
                        (200, {"verdict": {**GO, "status": 403, "detail": refusal,
                                           "content_type": "application/json; charset=utf-8"}})]
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        went = await client.get(f"https://{VENDOR}:{vendor.port}/repos/a/b/issues?page=1",
                                headers={"Authorization": f"Bearer {placeholder}"})
        refused = await client.post(f"https://{VENDOR}:{vendor.port}/repos/c/d/issues", json={"title": "x"},
                                    headers={"Authorization": f"Bearer {placeholder}"})
    assert went.status_code == 200 and vendor.seen[0]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    assert refused.status_code == 403 and refused.json() == {"message": refusal}
    assert len(vendor.seen) == 1
    # The ask: the command-line credential's route, the session's command-line
    # token, the request's method, host and target, and nothing of a value.
    first, second = platform.asked
    assert first["path"] == "/v1/hooks/cli-credential" == cx.JUDGE_PATH
    assert first["headers"]["authorization"] == "Bearer cli-token"
    assert first["headers"]["x-tunnel-timeout-s"] == "10"
    assert first["json"] == {"session_id": sid, "host": VENDOR, "request": {
        "placeholder": placeholder, "method": "GET", "host": VENDOR, "target": "/repos/a/b/issues?page=1",
        "content_type": "", "content_encoding": ""}}
    assert second["json"]["request"]["method"] == "POST" and "body_b64" not in second["json"]["request"]
    assert SECRET_VALUE not in json.dumps(platform.asked)
    cx.stop(sid)


@pytest.mark.asyncio
async def test_an_entry_that_is_not_judged_asks_nothing(roots, vendor, platform):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform, judged=False)
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/any", json={},
                              headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200 and platform.asked == []
    cx.stop(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, answer", [
    (404, {"detail": "Not Found"}),
    (403, {"detail": "This session's command-line token is required"}),
    (500, {"verdict": GO}),
    (200, {"username": "x-access-token", "password": "oto-exit-" + "a" * 32, "env": [], "process_env": {}}),
    (200, {"verdict": None}),
    (200, {"verdict": {**GO, "status": "0"}}),
    (200, {"verdict": {**GO, "status": True}}),
    (200, {"verdict": {**GO, "status": 200}}),
    (200, {"verdict": {**GO, "wants_body": -1}}),
    (200, {"verdict": {**GO, "detail": None}}),
    (200, {"verdict": {**GO, "body_b64": "***"}}),
    (200, {"verdict": {**GO, "link": ["only-one"]}}),
    (200, {"verdict": {"status": 0}}),
    (200, [{"verdict": GO}]),
])
async def test_only_a_verdict_lets_a_judged_request_go(roots, vendor, platform, status, answer):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform)
    platform.answers = [(status, answer)]
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/repos/a/b",
                             headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 503 and cx.REFUSAL_NO_WORD in r.text
    assert vendor.seen == []
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_platform_that_does_not_answer_refuses_the_request(roots, vendor, platform, monkeypatch):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform)
    monkeypatch.setattr(cx, "_JUDGE_TIMEOUT_S", 0.3)
    platform.delay = 5.0
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        slow = await client.get(f"https://{VENDOR}:{vendor.port}/repos/a/b",
                                headers={"Authorization": f"Bearer {placeholder}"})
        await platform.stop()
        gone = await client.get(f"https://{VENDOR}:{vendor.port}/repos/a/b",
                                headers={"Authorization": f"Bearer {placeholder}"})
    assert slow.status_code == 503 and gone.status_code == 503 and cx.REFUSAL_NO_WORD in gone.text
    assert vendor.seen == []
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_door_with_no_way_to_ask_refuses_every_judged_request(roots, vendor, platform, caplog):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, None)
    no_way = "holds no way to ask the platform"

    async def refused() -> bool:
        """One judged request: refused, and the log says the door could not ask (no ask was tried)."""
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="satellite.credential-exit"):
            async with _client(port, block["secret"], cx.bundle_path()) as client:
                r = await client.get(f"https://{VENDOR}:{vendor.port}/repos/a/b",
                                     headers={"Authorization": f"Bearer {placeholder}"})
        return r.status_code == 503 and cx.REFUSAL_NO_WORD in r.text and no_way in caplog.text

    assert await refused() and vendor.seen == []
    # A token with no tunnel, and a tunnel with no token, are no way to ask either.
    door = cx._state.doors[sid]
    door.cli_token = "cli-token"
    assert await refused() and platform.asked == []
    door.cli_token, door.platform_url = "", f"http://127.0.0.1:{platform.port}"
    assert await refused() and platform.asked == [] and vendor.seen == []
    cx.stop(sid)


@pytest.mark.asyncio
async def test_the_wait_for_a_free_ask_counts_against_the_asks_own_time(roots, vendor, platform, monkeypatch):
    """One ask at a time for this door, each a second at the platform, inside
    a bound of 1.6 seconds: the first is answered, the second waited a second
    for its turn and is refused at the bound, never answered late."""
    monkeypatch.setattr(cx, "_JUDGE_ASKS", 1)
    monkeypatch.setattr(cx, "_JUDGE_TIMEOUT_S", 1.6)
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform)
    platform.delay = 1.0
    platform.answers = [(200, {"verdict": {**GO, "status": 403, "detail": "No."}})] * 2

    async def one():
        async with _client(port, block["secret"], cx.bundle_path()) as client:
            return (await client.get(f"https://{VENDOR}:{vendor.port}/repos/a/b",
                                     headers={"Authorization": f"Bearer {placeholder}"})).status_code

    assert sorted(await asyncio.gather(one(), one())) == [403, 503]
    assert vendor.seen == []
    cx.stop(sid)


def test_a_door_updated_in_place_keeps_its_token_and_takes_a_new_one(roots):
    cx._state.relay = SimpleNamespace(port=1)
    sid, placeholder = str(uuid.uuid4()), exit_relay.new_placeholder()
    block = _block(placeholder)
    cx.start(sid, {"exit": block, "tunnel_port": 41000, "env": {"OTO_CLI_TOKEN": "first"}})
    door = cx._state.doors[sid]
    assert (door.platform_url, door.cli_token) == ("http://127.0.0.1:41000", "first")
    # A relayed tool's open carries the block alone: nothing is cleared.
    cx.start(sid, {"exit": block})
    assert cx._state.doors[sid] is door and (door.platform_url, door.cli_token) == ("http://127.0.0.1:41000", "first")
    # A start that names them again replaces them.
    cx.start(sid, {"exit": block, "env": {"PROXY_URL": "http://127.0.0.1:42000", "OTO_CLI_TOKEN": "second"}})
    assert (door.platform_url, door.cli_token) == ("http://127.0.0.1:42000", "second")
    cx.stop(sid)
    cx._state.relay = None


@pytest.mark.asyncio
async def test_a_body_the_platform_asks_for_is_sent_and_its_answer_goes_on(roots, vendor, platform):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform)
    rebuilt = b'{"query": "{viewer{login}}"}'
    platform.answers = [(200, {"verdict": {**GO, "wants_body": 4096}}),
                        (200, {"verdict": {**GO, "body_b64": base64.b64encode(rebuilt).decode(),
                                           "link": ["https://x/\\d+/", "https://x/a/b/"]}})]
    sent = b'{"query": "{viewer{login}}", "more": 1}'
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/graphql", content=sent,
                              headers={"Authorization": f"Bearer {placeholder}", "Content-Type": "application/json"})
    assert r.status_code == 200 and vendor.seen[0]["body"] == rebuilt.decode()
    assert "body_b64" not in platform.asked[0]["json"]["request"]
    second = platform.asked[1]["json"]["request"]
    assert base64.b64decode(second["body_b64"]) == sent and second["content_type"] == "application/json"
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_doors_asks_are_bounded(roots, vendor, platform):
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform)
    platform.delay = 0.2
    # Every ask is answered with a refusal, so nothing but the asks is in play.
    platform.answers = [(200, {"verdict": {**GO, "status": 403, "detail": "No."}})] * 20

    async def one():
        async with _client(port, block["secret"], cx.bundle_path()) as client:
            return (await client.get(f"https://{VENDOR}:{vendor.port}/repos/a/b",
                                     headers={"Authorization": f"Bearer {placeholder}"})).status_code

    statuses = await asyncio.gather(*(one() for _ in range(20)))
    assert statuses == [403] * 20 and len(platform.asked) == 20
    assert platform.peak == cx._JUDGE_ASKS == 8
    assert vendor.seen == []
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_body_past_the_cap_passes_a_machines_exit_for_an_entry_that_streams(roots, vendor):
    table = gw.GatewayTable()
    size = exit_relay.MAX_BODY_BYTES + 2 * 1024 * 1024

    async def pieces():
        for _ in range(size // 65536):
            yield b"p" * 65536

    sid, placeholder, block, port = await _judged_door(table, None, streams=True, judged=False)
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/a/b.git/git-receive-pack", content=pieces(),
                              headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 200 and len(vendor.seen[0]["body"]) == size
    assert vendor.seen[0]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    cx.stop(sid)
    # The same body for an entry that does not stream keeps the cap.
    sid, placeholder, block, port = await _judged_door(table, None, streams=False, judged=False)
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/upload", content=pieces(),
                              headers={"Authorization": f"Bearer {placeholder}"})
    assert r.status_code == 413 and len(vendor.seen) == 1
    cx.stop(sid)


# --- where a value may go: the entry's own hosts, and an address on the host ----


@pytest.mark.asyncio
async def test_a_placeholder_toward_a_host_its_own_entry_does_not_name_is_refused(roots, vendor):
    """Two entries of one session: the connection's host is the first's, so
    it is ended and read. The second's placeholder may go to its own hosts
    alone, and no value is read for it here."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    other = exit_relay.new_placeholder()
    both = {**block, "entries": [*block["entries"], {
        "placeholder": other, "header": "Authorization", "prefix": "Bearer ", "key": "OTHER_KEY",
        "hosts": ["elsewhere.test"]}]}
    cx.start(sid, {"exit": both})
    _push(table, block["token_hash"], other, "sk-other-value-0123456789")
    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.get(f"https://{VENDOR}:{vendor.port}/items",
                             headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 400 and exit_relay.REFUSAL_PLACE in r.text
    assert vendor.seen == []
    cx.stop(sid)


async def _raw_answer(port: int, secret: str, vendor_port: int, head: str) -> bytes:
    """The first bytes of the answer to one request a caller frames itself,
    inside the tunnel toward the vendor's name."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    auth = base64.b64encode(f"oto:{secret}".encode()).decode()
    writer.write(f"CONNECT {VENDOR}:{vendor_port} HTTP/1.1\r\nHost: {VENDOR}:{vendor_port}\r\n"
                 f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
    await writer.drain()
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    import ssl as _ssl
    transport = writer.transport
    protocol = transport.get_protocol()
    context = _ssl.create_default_context(cafile=str(cx.bundle_path()))
    tls = await asyncio.get_running_loop().start_tls(transport, protocol, context, server_hostname=VENDOR)
    tls.write(head.encode())
    answer = await asyncio.wait_for(reader.read(4096), 5)
    tls.abort()
    return answer


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["*x", "x/y", "@elsewhere.test/x", "/a/b#/c"])
async def test_a_target_that_is_no_path_is_refused_on_a_machine(roots, vendor, platform, target):
    """A paired computer's forward names the host and the target with no
    pinned address behind it, so the target must be a path. A number sign
    ends an address for the forward's parser, so what the platform judged
    would not be what is sent: the platform is not asked."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform)
    answer = await _raw_answer(port, block["secret"], vendor.port,
                               f"GET {target} HTTP/1.1\r\nHost: {VENDOR}:{vendor.port}\r\n"
                               f"Authorization: Bearer {placeholder}\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400") and b"The request names no path" in answer
    assert vendor.seen == [] and platform.asked == []
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_placeholder_inside_the_host_header_is_refused_on_a_machine(roots, vendor):
    """The ``Host`` header is sent on as the caller wrote it, so it is
    searched as a body is."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _door(table)
    answer = await _raw_answer(port, block["secret"], vendor.port,
                               f"GET /x HTTP/1.1\r\nHost: {placeholder}@{VENDOR}:{vendor.port}\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400") and exit_relay.REFUSAL_PLACE.encode() in answer
    assert vendor.seen == []
    cx.stop(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", [
    "Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
    f"Content-Length: {2 * 1024 * 1024}\r\nExpect: 100-continue\r\n\r\n",
])
async def test_a_placeholder_inside_the_host_header_of_a_request_that_would_stream_is_refused_on_a_machine(
        roots, vendor, framing):
    """The Host header is searched before the body's path is chosen."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, None, streams=True, judged=False)
    answer = await _raw_answer(port, block["secret"], vendor.port,
                               f"POST /upload HTTP/1.1\r\nHost: {placeholder}@{VENDOR}:{vendor.port}\r\n"
                               f"Authorization: Bearer {placeholder}\r\n{framing}")
    assert answer.startswith(b"HTTP/1.1 400") and exit_relay.REFUSAL_PLACE.encode() in answer
    assert vendor.seen == []
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_large_body_with_no_placeholder_in_its_head_keeps_the_cap_on_a_machine(roots, vendor):
    """Only a request whose head carries the placeholder of an entry that
    streams takes the larger path."""
    table = gw.GatewayTable()
    sid, _placeholder, block, port = await _judged_door(table, None, streams=True, judged=False)
    answer = await _raw_answer(port, block["secret"], vendor.port,
                               f"POST /upload HTTP/1.1\r\nHost: {VENDOR}:{vendor.port}\r\n"
                               f"Content-Length: {exit_relay.MAX_BODY_BYTES + 1}\r\nExpect: 100-continue\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 413") and exit_relay.REFUSAL_LARGE.encode() in answer
    assert vendor.seen == []
    cx.stop(sid)


@pytest.mark.asyncio
async def test_a_chunked_body_the_platform_asks_for_is_read_and_judged_though_the_entry_streams(
        roots, vendor, platform):
    """A body the platform asked for is read whole and judged before
    anything else: an entry that streams passes a chunked body as it
    arrives only when the platform asked for none."""
    table = gw.GatewayTable()
    sid, placeholder, block, port = await _judged_door(table, platform, streams=True)
    platform.answers = [(200, {"verdict": {**GO, "wants_body": 4096}}),
                        (200, {"verdict": {**GO, "status": 403, "detail": "No."}})]

    async def pieces():
        yield b'{"query": '
        yield b'"mutation{x}"}'

    async with _client(port, block["secret"], cx.bundle_path()) as client:
        r = await client.post(f"https://{VENDOR}:{vendor.port}/graphql", content=pieces(),
                              headers={"Authorization": f"Bearer {placeholder}", "Content-Type": "application/json"})
    assert r.status_code == 403 and vendor.seen == []
    assert base64.b64decode(platform.asked[1]["json"]["request"]["body_b64"]) == b'{"query": "mutation{x}"}'
    cx.stop(sid)


# --- behind the machine's own outgoing proxy ------------------------------------------


PROXY_USER = "standin-proxy-user"
PROXY_PASS = "standin-proxy-pass-5e1f"
PROXIED = "api.vendor.test"


class _UpstreamProxy:
    """A machine's own outgoing proxy on loopback, started by the test that
    needs it. Every request head it reads is recorded (method, target,
    headers in lower case), and it answers as ``answer`` says: ``200`` opens
    a tunnel to ``127.0.0.1`` at the target's port, so nothing leaves this
    machine; ``407`` and ``403`` refuse; ``close`` ends the connection
    unanswered. What passed its tunnels is kept, both ways."""

    def __init__(self, answer: str = "200") -> None:
        self.answer = answer
        self.heads: list[tuple[str, str, dict[str, str]]] = []
        self.passed = bytearray()
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            if hasattr(self.server, "abort_clients"):
                self.server.abort_clients()
            await self.server.wait_closed()

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                self.passed.extend(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            import contextlib
            with contextlib.suppress(OSError, RuntimeError):
                writer.write_eof()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        origin = None
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            line, *lines = raw.decode("latin-1").rstrip("\r\n").split("\r\n")
            method, target, _version = line.split(" ", 2)
            headers = {}
            for text in lines:
                name, _, value = text.partition(":")
                headers[name.strip().lower()] = value.strip()
            self.heads.append((method, target, headers))
            if self.answer == "close":
                return
            if self.answer != "200":
                writer.write(f"HTTP/1.1 {self.answer} No\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
                await writer.drain()
                return
            origin_reader, origin = await asyncio.open_connection("127.0.0.1", int(target.rsplit(":", 1)[1]))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(self._pipe(reader, origin), self._pipe(origin_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return
        finally:
            if origin is not None:
                origin.close()
            writer.close()


class _NamedVendor(_Vendor):
    """``_Vendor`` under a name of the test's own."""

    def __init__(self, authority: exit_authority.ExitAuthority, name: str) -> None:
        super().__init__(authority)
        self.name = name

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0,
                                                 ssl=self.authority.leaf_context(self.name))
        self.port = self.server.sockets[0].getsockname()[1]


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _pem(folder: Path) -> str:
    """The certificate of an authority of the test's own, as PEM text."""
    return exit_authority.ExitAuthority(folder).load_or_make().cert_pem.decode()


def _resolver(monkeypatch, answers: dict[str, list[str]]) -> list[str]:
    """``socket.getaddrinfo`` answering ``answers`` for the ``.test`` names
    (no answer: the name is unknown), every other name as before; the
    ``.test`` names asked, in order."""
    real = socket.getaddrinfo
    asked: list[str] = []

    def resolve(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if not name.endswith(".test"):
            return real(host, port, *args, **kwargs)
        asked.append(name)
        if name not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (a, int(port or 0))) for a in answers[name]]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return asked


@pytest.mark.asyncio
async def test_the_platforms_egress_is_kept_on_a_compute_machine_alone(roots, caplog):
    """The install's own compute machine takes the platform's proxy and its
    trust files at the sign-in, in memory; a paired computer reads its own
    settings and ignores the block; junk clears it; a SOCKS proxy turns the
    exit off with its reason until it goes."""
    caplog.set_level(logging.DEBUG)
    corp, other = _pem(roots / "corp-ca"), _pem(roots / "corp-ca-2")
    address = f"http://{PROXY_USER}:{PROXY_PASS}@proxy.corp.test:3128"
    block = {"https": f"  {address} ", "no_proxy": "internal.corp.test,.lan",
             "trust": [{"env": "SSL_CERT_FILE", "name": "corp.pem", "data_b64": _b64(corp)},
                       {"env": "REQUESTS_CA_BUNDLE", "name": "bad", "data_b64": "not base64 at all!"},
                       {"env": "CURL_CA_BUNDLE", "name": "text", "data_b64": _b64("no certificate here")},
                       {"env": "NODE_EXTRA_CA_CERTS", "name": "n", "data_b64": 5}, "junk", None],
             "cert_dir": [{"name": "0a1b2c3d.0", "data_b64": _b64(other)}]}
    cx.set_platform_egress(block, compute=False)
    assert (cx._state.platform_egress, cx._state.extra_trust, cx._upstream_url()) == (None, "", "")
    cx.set_platform_egress(block, compute=True)
    assert cx._state.platform_egress == {"https": address, "no_proxy": "internal.corp.test,.lan"}
    assert cx._state.extra_trust == corp.strip() + "\n" + other.strip()
    assert cx._upstream_url() == address and cx.upstream_on() is True
    assert all(PROXY_PASS not in value for value in os.environ.values())
    port = await cx.start_listener(gw.GatewayTable(), compute=True)
    assert cx.state() == {"exit_port": port, "exit_ok": True, "exit_why": "", "exit_upstream": True}
    # The bundle a session's clients read holds the platform's trust beside the exit's.
    cx.start(str(uuid.uuid4()), {"exit": _block(exit_relay.new_placeholder())})
    bundle = cx.bundle_path().read_text()
    assert corp.strip() in bundle and other.strip() in bundle
    cx.set_platform_egress({"https": f"socks5://{PROXY_USER}:{PROXY_PASS}@proxy.corp.test:1080"}, compute=True)
    assert cx.state() == {"exit_port": port, "exit_ok": False, "exit_why": "socks", "exit_upstream": False}
    for junk in (None, "junk", [], {"https": 5}, {"https": "   "}, {"no_proxy": "internal.corp.test"}):
        cx.set_platform_egress(block, compute=True)
        cx.set_platform_egress(junk, compute=True)
        assert (cx._state.platform_egress, cx._state.extra_trust) == (None, ""), junk
        assert cx.state() == {"exit_port": port, "exit_ok": True, "exit_why": "", "exit_upstream": False}, junk
    cx.set_platform_egress({"https": address, "no_proxy": ["internal.corp.test"]}, compute=True)
    assert cx._state.platform_egress == {"https": address, "no_proxy": ""}
    assert PROXY_PASS not in caplog.text and PROXY_USER not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["compute", "paired"])
async def test_through_the_machines_own_proxy_the_value_lands_and_the_proxy_reads_none_of_it(
        roots, monkeypatch, caplog, kind):
    """The install's own compute machine through the platform's proxy, a
    paired computer through its own ``HTTPS_PROXY``: the forward and the
    tunnel go through the proxy with the proxy's own credential, the value
    lands in its header, and the proxy, the log and this process's
    environment hold none of the session's secret, the value or the proxy's
    password. A proxy that refuses is the caller's refusal."""
    caplog.set_level(logging.DEBUG)
    _resolver(monkeypatch, {PROXIED: ["127.0.0.1"], "plain.vendor.test": ["127.0.0.1"]})
    # The stand-ins sit on loopback, which the sandbox's rule never lets through.
    monkeypatch.setattr(outbound_url, "sandbox_dial_refused", lambda *a, **k: False)
    vendor_ca = roots / "vendor-ca" / exit_authority.CERT_NAME
    authority = exit_authority.ExitAuthority(roots / "vendor-ca")
    monkeypatch.setattr(cx, "_upstream_verify", str(vendor_ca))
    origin, plain, proxy = _NamedVendor(authority, PROXIED), _NamedVendor(authority, "plain.vendor.test"), _UpstreamProxy()
    for server in (origin, plain, proxy):
        await server.start()
    address = f"http://{PROXY_USER}:{PROXY_PASS}@127.0.0.1:{proxy.port}"
    try:
        if kind == "compute":
            cx.set_platform_egress({"https": address, "no_proxy": ""}, compute=True)
        else:
            monkeypatch.setenv("HTTPS_PROXY", address)
        table = gw.GatewayTable()
        sid, placeholder, block, port = await _door(table, compute=kind == "compute", hosts=(PROXIED,))
        assert cx.state()["exit_upstream"] is True
        async with _client(port, block["secret"], cx.bundle_path()) as client:
            ended = await client.get(f"https://{PROXIED}:{origin.port}/items",
                                     headers={"Authorization": f"Bearer {placeholder}"})
        async with _client(port, block["secret"], vendor_ca) as client:
            tunnelled = await client.get(f"https://plain.vendor.test:{plain.port}/plain")
        proxy.answer = "407"
        async with _client(port, block["secret"], vendor_ca) as client:
            with pytest.raises(httpx.ProxyError, match="502"):
                await client.get(f"https://plain.vendor.test:{plain.port}/again")
        cx.stop(sid)
    finally:
        for server in (proxy, plain, origin):
            await server.stop()
    assert ended.status_code == 200 and tunnelled.status_code == 200
    (seen,) = origin.seen
    assert seen["headers"]["authorization"] == f"Bearer {SECRET_VALUE}" and "proxy-authorization" not in seen["headers"]
    assert [(m, t) for m, t, _h in proxy.heads] == [("CONNECT", f"{PROXIED}:{origin.port}"),
                                                    ("CONNECT", f"plain.vendor.test:{plain.port}"),
                                                    ("CONNECT", f"plain.vendor.test:{plain.port}")]
    own = "Basic " + _b64(f"{PROXY_USER}:{PROXY_PASS}")
    assert all(headers["proxy-authorization"] == own for _m, _t, headers in proxy.heads)
    assert block["secret"] not in repr(proxy.heads)
    for text in (placeholder, SECRET_VALUE, block["secret"]):
        assert text.encode() not in bytes(proxy.passed)
    for text in (PROXY_PASS, PROXY_USER, own.split()[1], block["secret"], SECRET_VALUE):
        assert text not in caplog.text
    if kind == "compute":
        assert all(PROXY_PASS not in value for value in os.environ.values())


@pytest.mark.asyncio
async def test_on_compute_a_target_the_proxy_will_resolve_is_judged_by_the_sandboxs_rule(roots, monkeypatch):
    """The proxy resolves the name, so the exit judges it first, best
    effort: an address by the sandbox's rule with the session's carves, a
    name that only means this machine never, a name by every answer this
    machine's resolver gives, a name it cannot answer admitted (the proxy
    resolves it) and not looked up again for a while."""
    asked = _resolver(monkeypatch, {"public.vendor.test": ["93.184.216.34"], "carved.vendor.test": ["10.0.0.5"],
                                    "inside.vendor.test": ["10.0.0.9"],
                                    "mixed.vendor.test": ["93.184.216.34", "127.0.0.1"],
                                    "mapped.vendor.test": ["::ffff:169.254.169.254"]})
    cx._state.compute = True
    sid = str(uuid.uuid4())
    cx._state.doors[sid] = cx._Door(session_id=sid, secret="s", token_hash="h", entries={},
                                    carves=frozenset({"10.0.0.5"}))
    backend = cx._Backend()
    for address, admitted in (("93.184.216.34", True), ("10.0.0.5", True), ("10.0.0.6", False),
                              ("127.0.0.1", False), ("169.254.169.254", False), ("0.0.0.0", False),
                              ("::1", False), ("[::1]", False), ("::ffff:127.0.0.1", False)):
        assert await backend.upstream_admits(sid, address, 443) is admitted, address
    for name in ("localhost", "x.localhost", "LOCALHOST.", "a.b.localhost"):
        assert await backend.upstream_admits(sid, name, 443) is False, name
    assert asked == []
    assert await backend.upstream_admits(sid, "public.vendor.test", 443) is True
    assert await backend.upstream_admits(sid, "carved.vendor.test", 443) is True
    for name in ("inside.vendor.test", "mixed.vendor.test", "mapped.vendor.test"):
        assert await backend.upstream_admits(sid, name, 443) is False, name
    # Another session holds no carve.
    assert await backend.upstream_admits("other", "carved.vendor.test", 443) is False
    asked.clear()
    assert await backend.upstream_admits(sid, "gone.vendor.test", 443) is True
    assert await backend.upstream_admits(sid, "Gone.Vendor.Test.", 443) is True
    assert asked == ["gone.vendor.test"] and "gone.vendor.test" in cx._state.unresolved
    # A resolved name is looked up at each ask.
    assert await backend.upstream_admits(sid, "inside.vendor.test", 443) is False
    assert await backend.upstream_admits(sid, "inside.vendor.test", 443) is False
    assert asked == ["gone.vendor.test", "inside.vendor.test", "inside.vendor.test"]
    # A paired computer has no sandbox: its sessions reach every target.
    cx._state.compute = False
    assert await backend.upstream_admits(sid, "localhost", 443) is True
    assert await backend.upstream_admits(sid, "10.0.0.6", 443) is True
    assert asked == ["gone.vendor.test", "inside.vendor.test", "inside.vendor.test"]


@pytest.mark.asyncio
async def test_on_compute_a_slow_lookup_is_bounded_and_the_name_left_to_the_proxy(roots, monkeypatch):
    import threading
    import time
    monkeypatch.setattr(cx, "_LOOKUP_S", 0.2)
    release = threading.Event()
    asked: list[str] = []

    def slow(host, port, *args, **kwargs):
        asked.append(str(host))
        release.wait(5)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    cx._state.compute = True
    backend = cx._Backend()
    try:
        started = time.monotonic()
        assert await backend.upstream_admits("sid", "slow.vendor.test", 443) is True
        assert time.monotonic() - started < 2.0
        assert await backend.upstream_admits("sid", "slow.vendor.test", 443) is True
    finally:
        release.set()
    assert asked == ["slow.vendor.test"]


@pytest.mark.asyncio
async def test_the_platforms_no_proxy_sends_a_listed_name_direct_and_another_through_the_proxy(roots, monkeypatch):
    """The install's own compute machine reads the platform's list, never
    this process's; a paired computer reads its own."""
    monkeypatch.setenv("NO_PROXY", "api.vendor.test")
    cx.set_platform_egress({"https": "http://proxy.corp.test:3128", "no_proxy": "internal.corp.test,.lan"},
                           compute=True)
    for name, direct in (("internal.corp.test", True), ("git.internal.corp.test", True), ("nas.lan", True),
                         ("api.vendor.test", False), ("notinternal.corp.test", False)):
        assert await cx._bypassed(name) is direct, name
    backend = cx._Backend()
    assert await backend.upstream("sid", "internal.corp.test", 443) is None
    up = await backend.upstream("sid", "api.vendor.test", 443)
    assert up is not None and (up.scheme, up.host, up.port, up.authorization) == ("http", "proxy.corp.test", 3128, "")
    # No list: every name through the proxy.
    cx.set_platform_egress({"https": "http://proxy.corp.test:3128"}, compute=True)
    assert await cx._bypassed("internal.corp.test") is False
    # No platform proxy: this machine's own bypass list.
    cx.set_platform_egress(None, compute=False)
    assert await cx._bypassed("api.vendor.test") is True
    assert await cx._bypassed("internal.corp.test") is False


@pytest.mark.asyncio
async def test_a_bypass_answer_that_does_not_come_sends_the_name_through_the_proxy(roots, monkeypatch):
    """The machine's own bypass list may resolve names (Windows' on Python
    3.10 and 3.11): an answer that does not come within the bound sends the
    name through the proxy, and is kept like any other answer."""
    import threading
    import time
    import urllib.request
    release, asked = threading.Event(), []

    def slow(host):
        asked.append(host)
        release.wait(10)
        return True
    monkeypatch.setattr(urllib.request, "proxy_bypass", slow)
    monkeypatch.setattr(cx, "_LOOKUP_S", 0.2)
    cx.set_platform_egress(None, compute=False)
    try:
        started = time.monotonic()
        assert await cx._bypassed("slow.corp.test") is False
        assert time.monotonic() - started < 2.0
        assert await cx._bypassed("slow.corp.test") is False
    finally:
        release.set()
    assert asked == ["slow.corp.test"]


def test_the_forwards_check_reads_the_sessions_base_and_the_platforms_trust(roots, monkeypatch):
    """One base for every forward: the system's store as the sessions'
    bundle reads it (``exit_authority.system_bundle_text``), with the
    platform's trust files beside it when the platform sent them."""
    import ssl
    system, corp = _pem(roots / "vendor-ca"), _pem(roots / "corp-ca")
    monkeypatch.setattr(exit_authority, "system_bundle_text", lambda: system)
    monkeypatch.setattr(cx, "_upstream_verify", True)

    def held(context) -> set[bytes]:
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
        return set(context.get_ca_certs(binary_form=True))

    assert held(cx._verify()) == {ssl.PEM_cert_to_DER_cert(system)}
    cx.set_platform_egress({"https": "http://proxy.corp.test:3128",
                            "trust": [{"env": "SSL_CERT_FILE", "name": "corp.pem", "data_b64": _b64(corp)}]},
                           compute=True)
    assert held(cx._verify()) == {ssl.PEM_cert_to_DER_cert(system), ssl.PEM_cert_to_DER_cert(corp)}
    # A test's own file stands over it.
    monkeypatch.setattr(cx, "_upstream_verify", "/standin/ca.pem")
    assert cx._verify() == "/standin/ca.pem"
    cx.set_platform_egress(None, compute=True)


@pytest.mark.asyncio
async def test_the_door_admits_the_sessions_two_users_and_no_other(roots):
    """A session's processes present its door's secret as ``oto`` or, for a
    tool whose key goes to a host under an engine's domain, ``oto-tool``:
    both name the session, any other user name or secret names none."""
    table = gw.GatewayTable()
    sid, _placeholder, block, _port = await _door(table)
    backend = cx._Backend()

    def head(user: str, secret: str) -> list[tuple[bytes, bytes]]:
        token = base64.b64encode(f"{user}:{secret}".encode()).decode()
        return [(b"proxy-authorization", f"Basic {token}".encode())]

    assert backend.session_of(head("oto", block["secret"])) == sid
    assert backend.session_of(head("oto-tool", block["secret"])) == sid
    for user, secret in (("root", block["secret"]), ("oto-tools", block["secret"]), ("", block["secret"]),
                         ("oto", "not-the-secret"), ("oto-tool", "")):
        assert backend.session_of(head(user, secret)) is None, user
    assert backend.session_of([]) is None


def test_the_exits_state_never_prints_the_platforms_proxy():
    """The platform's proxy address can carry its credential: the state's
    own text leaves it out."""
    cx.set_platform_egress({"https": "http://walkuser:not-the-password@proxy.corp.test:3128"}, compute=True)
    try:
        assert "not-the-password" not in repr(cx._state) and "proxy.corp.test" not in repr(cx._state)
    finally:
        cx.set_platform_egress(None, compute=True)


def test_the_forwards_check_takes_a_store_whose_comments_name_a_root_outside_ascii(roots, monkeypatch):
    """A distribution's store may label each root in a comment line, a name
    outside ASCII among them: every certificate of it is still trusted."""
    import ssl
    system = _pem(roots / "vendor-ca")
    monkeypatch.setattr(exit_authority, "system_bundle_text",
                        lambda: "# Ünïcödé Root CA, a label line\n" + system)
    monkeypatch.setattr(cx, "_upstream_verify", True)
    context = cx._verify()
    assert set(context.get_ca_certs(binary_form=True)) == {ssl.PEM_cert_to_DER_cert(system)}


@pytest.mark.asyncio
async def test_a_doors_trust_is_built_at_its_start_and_a_new_proxy_password_a_new_client(roots, monkeypatch):
    """The forward's trust is read and built when a door opens (on a worker
    thread: reading a system's store can take a while, Windows' above all),
    never by the clients the loop makes; a proxy whose credential changed
    gets a client of its own."""
    reads: list[int] = []
    system = _pem(roots / "vendor-ca")

    def base() -> str:
        reads.append(1)
        return system

    monkeypatch.setattr(exit_authority, "system_bundle_text", base)
    monkeypatch.setattr(cx, "_upstream_verify", True)
    table = gw.GatewayTable()
    sid, _placeholder, _block, _port = await _door(table)
    assert cx._door_of(sid).verify is not None and reads
    reads.clear()
    backend = cx._Backend()
    direct = [backend.client(sid) for _ in range(3)]
    assert direct[0] is direct[1] is direct[2] and reads == []
    first = exit_relay.parse_upstream("http://walker:first@proxy.corp.test:3128")
    second = exit_relay.parse_upstream("http://walker:second@proxy.corp.test:3128")
    through = backend.client(sid, first)
    assert backend.client(sid, first) is through
    renewed = backend.client(sid, second)
    assert renewed is not through and backend.client(sid, second) is renewed and reads == []


@pytest.mark.asyncio
async def test_a_doors_text_and_the_states_hold_no_secret_and_no_proxy_address(roots, monkeypatch):
    """A door's secret, its command-line token and the machine's own proxy
    address stay out of the objects' printed form."""
    table = gw.GatewayTable()
    sid, _placeholder, block, _port = await _door(table)
    door = cx._door_of(sid)
    door.cli_token = "cli-token-not-for-print"
    assert block["secret"] not in repr(door) and "cli-token-not-for-print" not in repr(door)
    monkeypatch.setattr(cx._state, "proxies", (0.0, {"https": "http://walker:not-for-print@proxy.corp.test:3128"}))
    assert "not-for-print" not in repr(cx._state) and "proxy.corp.test" not in repr(cx._state)


@pytest.mark.asyncio
async def test_a_proxy_address_the_parser_refuses_is_dialled_around_and_never_logged(roots, monkeypatch, caplog):
    """The machine's own proxy setting names an address the standard
    library refuses: the exit dials around it, keeps answering, and no log
    record holds any part of it."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(cx, "_upstream_url", lambda: "http://walker:stand＃in@proxy.corp.test:3128")
    table = gw.GatewayTable()
    sid, _placeholder, _block, _port = await _door(table)
    assert await cx._Backend().upstream(sid, VENDOR, 443) is None
    cx._refresh_upstream_why()
    assert cx.upstream_on() is False and cx.state()["exit_why"] == ""
    assert "stand" not in caplog.text and "proxy.corp.test" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["direct", "proxy"])
async def test_the_forward_refuses_a_vendor_its_trust_does_not_hold(roots, monkeypatch, way):
    """The forward checks the vendor's certificate against its trust, on
    both ways out: a vendor signed by an authority the base does not hold
    answers 502 and is sent nothing, the value least of all."""
    _resolver(monkeypatch, {PROXIED: ["127.0.0.1"]})
    monkeypatch.setattr(outbound_url, "sandbox_dial_refused", lambda *a, **k: False)
    authority = exit_authority.ExitAuthority(roots / "vendor-ca")
    other = _pem(roots / "corp-ca")
    monkeypatch.setattr(cx, "_upstream_verify", True)
    monkeypatch.setattr(exit_authority, "system_bundle_text", lambda: other)
    origin, proxy = _NamedVendor(authority, PROXIED), _UpstreamProxy()
    for server in (origin, proxy):
        await server.start()
    try:
        if way == "proxy":
            monkeypatch.setenv("HTTPS_PROXY", f"http://{PROXY_USER}:{PROXY_PASS}@127.0.0.1:{proxy.port}")
        table = gw.GatewayTable()
        sid, placeholder, block, port = await _door(table, hosts=(PROXIED,))
        async with _client(port, block["secret"], cx.bundle_path()) as client:
            r = await client.get(f"https://{PROXIED}:{origin.port}/items",
                                 headers={"Authorization": f"Bearer {placeholder}"})
        cx.stop(sid)
    finally:
        for server in (proxy, origin):
            await server.stop()
    assert r.status_code == 502 and "did not answer" in r.text
    assert origin.seen == []
