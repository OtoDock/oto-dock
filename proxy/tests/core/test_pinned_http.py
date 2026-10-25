"""``services.infra.pinned_http``: outbound connections go only to the
addresses checked at the dial.

A fake resolver stands in for DNS (the validator's and the transport's
lookups are separate calls, as on a real host), and local listeners stand in
for the hosts, so each test proves where the socket actually went: a name
whose answer changes between the call site's check and the connect is
refused before any connection is made, and a name that passes is dialled at
the address it resolved to while SNI, the certificate check and the Host
header stay on the name."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import socket
import ssl
import threading
from types import SimpleNamespace

import httpx
import pytest
import requests

from services.infra import outbound_url, pinned_http

NAME = "vendor.example"


class FlipResolver:
    """Answers ``first`` once, then ``after`` for every later lookup: a DNS
    record that changes between the check and the connect."""

    def __init__(self, first: str, after: str) -> None:
        self.answers = [first]
        self.after = after
        self.asked: list[str] = []

    def __call__(self, host, port, *a, **kw):
        self.asked.append(host)
        addr = self.answers.pop(0) if self.answers else self.after
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (addr, port))]


@pytest.fixture
def resolver(monkeypatch):
    """Install a resolver for both lookups: the validator's (its module's
    own ``socket`` reference) and the transport's."""
    def install(fake):
        monkeypatch.setattr(outbound_url, "socket", SimpleNamespace(
            getaddrinfo=fake, IPPROTO_TCP=socket.IPPROTO_TCP))
        monkeypatch.setattr(pinned_http, "_getaddrinfo", fake)
        return fake
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    return install


class Listener:
    """A local HTTP/1.1 server (optionally TLS) counting the connections it
    accepts and recording each request's Host header and TLS server name."""

    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        self.connections = 0
        self.hosts: list[str] = []
        self.sni: list[str | None] = []
        if tls is not None:
            tls.sni_callback = lambda sock, name, ctx: self.sni.append(name)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.tls = tls
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            try:
                if self.tls is not None:
                    conn = self.tls.wrap_socket(conn, server_side=True)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.decode("latin-1").split("\r\n"):
                    if line.lower().startswith("host:"):
                        self.hosts.append(line.split(":", 1)[1].strip())
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                while len(body) < length:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    body += chunk
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            except (OSError, ssl.SSLError):
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self.sock.close()


def _cert(tmp_path, name: str):
    """A self-signed certificate for ``name``: (server context, CA file)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_file = tmp_path / f"{name}.pem"
    key_file = tmp_path / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(cert_file, key_file)
    return server, str(cert_file)


# --- the answer changes between the check and the connect --------------------

def test_a_vendor_call_whose_answer_turns_private_is_refused_before_it_connects(resolver):
    """The real vendor-call path: ``check_url`` resolves the name to a public
    address and passes; the dial resolves it again, gets the listener's
    loopback address, and refuses without connecting."""
    from services.webhooks import vendor_http
    target = Listener()
    fake = resolver(FlipResolver("8.8.8.8", "127.0.0.1"))
    url = f"https://{NAME}:{target.port}/hooks"

    async def call():
        await vendor_http.check_url(url)
        async with vendor_http.client(url, timeout=5) as client:
            await client.post(url, json={})
    with pytest.raises(httpx.ConnectError, match=r"may not reach \(127\.0\.0\.1\)"):
        asyncio.run(call())
    assert fake.asked == [NAME, NAME]
    assert target.connections == 0
    target.close()


def test_every_answer_must_pass_and_none_is_dialled_otherwise(resolver):
    target = Listener()

    def two(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    resolver(two)

    async def call():
        async with httpx.AsyncClient(transport=pinned_http.CheckedTransport(
                pinned_http.public_only)) as client:
            await client.get(f"http://{NAME}:{target.port}/")
    with pytest.raises(httpx.ConnectError, match="127.0.0.1"):
        asyncio.run(call())
    assert target.connections == 0
    target.close()


def test_a_name_that_does_not_resolve_is_a_connect_error(resolver):
    def none(host, port, *a, **kw):
        raise socket.gaierror("no such name")
    resolver(none)

    async def call():
        async with pinned_http.checked_client(f"https://{NAME}/") as client:
            await client.get(f"https://{NAME}/")
    with pytest.raises(httpx.ConnectError, match="no such name"):
        asyncio.run(call())


# --- what passes is dialled at the address it resolved to ---------------------

def test_a_passing_name_is_dialled_at_its_checked_address_with_its_own_host(resolver):
    """The listener's loopback stands in for a public address (the policy
    here admits it): the socket lands where the dial-time lookup said, and
    the Host header names the host, not the address."""
    target = Listener()
    resolver(FlipResolver("127.0.0.1", "127.0.0.1"))

    async def call():
        async with httpx.AsyncClient(transport=pinned_http.CheckedTransport(lambda a: False)) as c:
            return await c.get(f"http://{NAME}:{target.port}/x")
    assert asyncio.run(call()).text == "ok"
    assert target.connections == 1 and target.hosts == [f"{NAME}:{target.port}"]
    target.close()


def test_tls_is_verified_for_the_name_while_the_address_is_dialled(resolver, tmp_path):
    server, ca = _cert(tmp_path, NAME)
    target = Listener(server)
    resolver(lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    trust = ssl.create_default_context(cafile=ca)

    async def call(name):
        async with httpx.AsyncClient(transport=pinned_http.CheckedTransport(
                lambda a: False, verify=trust)) as c:
            return await c.get(f"https://{name}:{target.port}/x")
    assert asyncio.run(call(NAME)).text == "ok"
    assert target.sni == [NAME]
    # The certificate names vendor.example; another name dialled at the same
    # address is not accepted on it.
    with pytest.raises(httpx.ConnectError):
        asyncio.run(call("other.example"))
    target.close()


# --- requests (Web Push) -------------------------------------------------------

def test_the_requests_adapter_refuses_a_private_answer_before_connecting(resolver):
    target = Listener()
    resolver(FlipResolver("127.0.0.1", "127.0.0.1"))
    session = requests.Session()
    session.mount("https://", pinned_http.PublicHTTPSAdapter())
    with pytest.raises(requests.exceptions.ConnectionError, match="may not reach"):
        session.post(f"https://{NAME}:{target.port}/push", data=b"x", timeout=5)
    assert target.connections == 0
    target.close()


def test_the_requests_adapter_dials_the_checked_address_for_the_name(resolver, tmp_path, monkeypatch):
    server, ca = _cert(tmp_path, NAME)
    target = Listener(server)
    resolver(lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    monkeypatch.setattr(pinned_http, "public_only", lambda a: False)
    session = requests.Session()
    session.mount("https://", pinned_http.PublicHTTPSAdapter())
    resp = session.post(f"https://{NAME}:{target.port}/push", data=b"x", timeout=5, verify=ca)
    assert resp.text == "ok"
    assert target.sni == [NAME] and target.hosts == [f"{NAME}:{target.port}"]
    target.close()


# --- the sites and the egress proxy ---------------------------------------------

def test_a_proxied_url_keeps_the_plain_client(monkeypatch):
    """Behind an egress proxy the proxy resolves the name, and a custom
    transport would bypass it: the plain client is kept."""
    from api.apps import app_egress
    from services.webhooks import vendor_http
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    direct = vendor_http.client(f"https://{NAME}/", timeout=5)
    assert isinstance(direct._transport, pinned_http.CheckedTransport)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    behind = vendor_http.client(f"https://{NAME}/", timeout=5)
    assert not isinstance(behind._transport, pinned_http.CheckedTransport)
    monkeypatch.setattr(app_egress, "_client", None)
    monkeypatch.setattr(app_egress, "_proxied_client", None)
    assert not isinstance(app_egress._http(f"https://{NAME}/")._transport,
                          pinned_http.CheckedTransport)
    monkeypatch.delenv("HTTPS_PROXY")
    assert isinstance(app_egress._http(f"https://{NAME}/")._transport,
                      pinned_http.CheckedTransport)
    asyncio.run(direct.aclose())
    asyncio.run(behind.aclose())


def test_the_web_push_session_dials_checked_addresses_and_follows_no_redirect():
    from services.notifications import push_sender
    session = push_sender._no_redirect_session()
    assert session.max_redirects == 0
    adapter = session.get_adapter("https://push.example/x")
    assert type(adapter).__qualname__.endswith("Adapter")
    assert type(adapter).__module__ == pinned_http.__name__


def test_the_public_policy_is_the_validators():
    for addr in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00::1", "100.64.0.1"):
        assert pinned_http.public_only(addr), addr
    assert not pinned_http.public_only("8.8.8.8")
    assert not pinned_http.public_only(str(ipaddress.ip_address("2606:4700::1111")))


# --- several addresses, one connect timeout -------------------------------------

def test_a_silent_first_address_leaves_time_for_the_next():
    """The connect timeout is shared: the first of two addresses gets half
    of it, and a timeout there moves on to the second."""
    tried: list[tuple[str, float]] = []

    class _Inner:
        async def connect_tcp(self, host, port, timeout=None, **kw):
            tried.append((host, timeout))
            if host == "192.0.2.1":
                raise pinned_http.httpcore.ConnectTimeout("silent")
            return "stream"
    backend = pinned_http._CheckedBackend(lambda a: False)
    backend._inner = _Inner()
    pinned_http._getaddrinfo, saved = (lambda h, p: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", p)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.7", p))]), pinned_http._getaddrinfo
    try:
        assert asyncio.run(backend.connect_tcp(NAME, 443, timeout=10)) == "stream"
    finally:
        pinned_http._getaddrinfo = saved
    assert [h for h, _ in tried] == ["192.0.2.1", "198.51.100.7"]
    assert 4 < tried[0][1] <= 5 and tried[1][1] > 4


def test_the_requests_adapter_tries_the_next_address_on_a_timeout(resolver, monkeypatch,
                                                                  tmp_path):
    from urllib3.util import connection as u3conn
    server, ca = _cert(tmp_path, NAME)
    target = Listener(server)
    resolver(lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    monkeypatch.setattr(pinned_http, "public_only", lambda a: False)
    real = u3conn.create_connection
    tried = []

    def dial(address, timeout, **kw):
        tried.append((address[0], timeout))
        if address[0] == "192.0.2.1":
            raise TimeoutError("silent")
        return real(address, timeout, **kw)
    monkeypatch.setattr(u3conn, "create_connection", dial)
    session = requests.Session()
    session.mount("https://", pinned_http.PublicHTTPSAdapter())
    assert session.get(f"https://{NAME}:{target.port}/", timeout=6, verify=ca).text == "ok"
    assert [a for a, _ in tried] == ["192.0.2.1", "127.0.0.1"]
    assert tried[0][1] <= 3.01 and target.connections == 1
    target.close()


def test_the_proxy_decision_is_httpx_own(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "vendor.example:8443,.skip.example")
    assert pinned_http.proxied(f"https://{NAME}/x")
    assert not pinned_http.proxied(f"https://{NAME}:8443/x")
    assert not pinned_http.proxied("https://a.skip.example/x")
    assert not pinned_http.proxied("http://plain.example/x")
    monkeypatch.setenv("NO_PROXY", "*")
    assert not pinned_http.proxied(f"https://{NAME}/x")


# --- the control-plane policy, the verify fix and the sync twin -----------------

def test_the_control_plane_policy_refuses_this_machine_and_lets_the_lan_through():
    refused = ("127.0.0.1", "::1", "169.254.169.254", "fd00:ec2::254", "fe80::1", "fe80::1%eth0",
               "0.0.0.0", "0.0.0.5", "224.0.0.1", "240.0.0.1", "::ffff:127.0.0.1", "not-an-address")
    for addr in refused:
        assert pinned_http.control_plane(addr), addr
    for addr in ("10.0.0.5", "192.168.1.20", "fd00::1", "8.8.8.8", "2606:4700::1111"):
        assert not pinned_http.control_plane(addr), addr


def test_a_refusal_is_logged_with_the_host_and_the_address(resolver, caplog):
    resolver(FlipResolver("127.0.0.1", "127.0.0.1"))
    with caplog.at_level("WARNING", logger=pinned_http.__name__):
        with pytest.raises(pinned_http.Refused):
            pinned_http.checked_addresses(NAME, 443, pinned_http.control_plane)
    assert f"the host {NAME!r} resolves to 127.0.0.1" in caplog.text


def test_checked_client_hands_verify_to_the_transport(resolver, tmp_path):
    """A client's ``verify`` is ignored once a transport is given, so the
    context must reach the transport: the TLS listener's test CA is trusted
    through it."""
    server, ca = _cert(tmp_path, NAME)
    target = Listener(server)
    resolver(lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    trust = ssl.create_default_context(cafile=ca)

    async def call():
        async with pinned_http.checked_client(f"https://{NAME}/", lambda a: False,
                                              verify=trust, timeout=5) as c:
            assert c._transport.ssl_context is trust
            return await c.get(f"https://{NAME}:{target.port}/x")
    assert asyncio.run(call()).text == "ok"
    assert target.sni == [NAME]
    target.close()


def test_the_sync_client_refuses_a_rebound_answer_before_connecting(resolver):
    target = Listener()
    fake = resolver(FlipResolver("8.8.8.8", "127.0.0.1"))
    url = f"http://{NAME}:{target.port}/mint"
    # The caller's pre-check sees the public answer; the dial gets loopback.
    assert pinned_http.checked_addresses(NAME, target.port, pinned_http.control_plane) == ["8.8.8.8"]
    with pinned_http.checked_sync_client(url, pinned_http.control_plane, timeout=5) as client:
        with pytest.raises(httpx.ConnectError, match=r"may not reach \(127\.0\.0\.1\)"):
            client.post(url, json={})
    assert fake.asked == [NAME, NAME] and target.connections == 0
    target.close()


def test_the_sync_client_dials_the_checked_address_and_keeps_the_name(resolver, tmp_path):
    server, ca = _cert(tmp_path, NAME)
    target = Listener(server)
    resolver(lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    trust = ssl.create_default_context(cafile=ca)
    with pinned_http.checked_sync_client(f"https://{NAME}/", lambda a: False,
                                         verify=trust, timeout=5) as client:
        assert isinstance(client._transport, pinned_http.CheckedSyncTransport)
        assert client._transport.ssl_context is trust
        assert client.get(f"https://{NAME}:{target.port}/x").text == "ok"
    assert target.connections == 1 and target.sni == [NAME]
    assert target.hosts == [f"{NAME}:{target.port}"]
    target.close()


def test_the_sync_backend_tries_the_next_address_on_a_timeout():
    tried: list[tuple[str, float]] = []

    class _Inner:
        def connect_tcp(self, host, port, timeout=None, **kw):
            tried.append((host, timeout))
            if host == "192.0.2.1":
                raise pinned_http.httpcore.ConnectTimeout("silent")
            return "stream"
    backend = pinned_http._CheckedSyncBackend(lambda a: False)
    backend._inner = _Inner()
    pinned_http._getaddrinfo, saved = (lambda h, p: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", p)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.7", p))]), pinned_http._getaddrinfo
    try:
        assert backend.connect_tcp(NAME, 443, timeout=10) == "stream"
    finally:
        pinned_http._getaddrinfo = saved
    assert [h for h, _ in tried] == ["192.0.2.1", "198.51.100.7"]
    assert 4 < tried[0][1] <= 5 and tried[1][1] > 4


def test_a_proxied_url_keeps_the_plain_sync_client(monkeypatch):
    for var in ("NO_PROXY", "no_proxy", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    behind = pinned_http.checked_sync_client(f"https://{NAME}/", pinned_http.control_plane, timeout=5)
    assert not isinstance(behind._transport, pinned_http.CheckedSyncTransport)
    behind.close()
    monkeypatch.delenv("HTTPS_PROXY")
    direct = pinned_http.checked_sync_client(f"https://{NAME}/", pinned_http.control_plane, timeout=5)
    assert isinstance(direct._transport, pinned_http.CheckedSyncTransport)
    assert direct._transport.policy is pinned_http.control_plane
    direct.close()


# --- the admin-endpoint policies, the pool and the sites ----------------

def _answers(*addrs):
    def fake(host, port, *a, **kw):
        return [(socket.AF_INET6 if ":" in a_ else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (a_, port)) for a_ in addrs]
    return fake


def test_the_model_server_policy_lets_this_machine_and_the_lan_through():
    refused = ("169.254.169.254", "169.254.10.1", "fd00:ec2::254", "fe80::1", "224.0.0.1",
               "240.0.0.1", "0.0.0.5", "::ffff:169.254.169.254", "not-an-address")
    for addr in refused:
        assert pinned_http.model_server(addr), addr
    for addr in ("127.0.0.1", "::1", "0.0.0.0", "::", "::ffff:127.0.0.1", "10.0.0.5",
                 "192.168.1.20", "fd00::1", "8.8.8.8", "2606:4700::1111"):
        assert not pinned_http.model_server(addr), addr


def test_the_private_network_policy_refuses_the_public_internet():
    for addr in ("8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8", "not-an-address"):
        assert pinned_http.private_network(addr), addr
    for addr in ("127.0.0.1", "10.0.0.5", "192.168.1.20", "100.64.0.1", "fd00::1",
                 "fe80::1%eth0", "169.254.1.1", "::1"):
        assert not pinned_http.private_network(addr), addr


def test_the_admin_endpoint_policies_drop_a_refused_answer_and_dial_the_rest(resolver):
    """A LAN host with a global IPv6 address beside its private one, a name
    answering the metadata address beside a LAN one: the dial keeps what
    the policy admits; only an answer with nothing left is refused. The
    other two policies still refuse the whole answer."""
    resolver(_answers("2606:4700::1111", "192.168.1.20"))
    assert pinned_http.checked_addresses(NAME, 80, pinned_http.private_network) == ["192.168.1.20"]
    resolver(_answers("169.254.169.254", "10.0.0.5"))
    assert pinned_http.checked_addresses(NAME, 80, pinned_http.model_server) == ["10.0.0.5"]
    resolver(_answers("8.8.8.8", "2606:4700::1111"))
    with pytest.raises(pinned_http.Refused, match="only to addresses"):
        pinned_http.checked_addresses(NAME, 80, pinned_http.private_network)
    resolver(_answers("8.8.8.8", "127.0.0.1"))
    with pytest.raises(pinned_http.Refused):
        pinned_http.checked_addresses(NAME, 80, pinned_http.public_only)


def test_a_model_server_name_that_turns_to_the_metadata_address_reaches_nothing(resolver):
    """The Ollama model list (no pre-check): the dial resolves the admin's
    name to the cloud metadata address and connects to nothing."""
    from core.layers.providers.openai_compat_adapter import OllamaAdapter
    seen: list = []
    real = pinned_http.checked_client

    def recording(url, policy=pinned_http.public_only, **kw):
        seen.append(policy)
        return real(url, policy, **kw)
    resolver(_answers("169.254.169.254"))
    target = Listener()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pinned_http, "checked_client", recording)
        models = asyncio.run(OllamaAdapter().list_available_models(
            "", f"http://{NAME}:{target.port}/v1"))
    assert models == [] and seen == [pinned_http.model_server]
    assert target.connections == 0
    target.close()


def test_a_daemon_name_that_turns_public_after_the_check_reaches_nothing(resolver, monkeypatch):
    """The phone daemon over plain http: ``daemon_url.judge`` passes on the
    private answer, the dial gets a public one and refuses; https keeps the
    plain client (no rule applies)."""
    from services.phone import daemon_url
    url = f"http://{NAME}:9093"
    fake = resolver(FlipResolver("10.0.0.5", "8.8.8.8"))
    assert daemon_url.judge(url, resolve=fake) is None

    async def call():
        async with daemon_url.client(url, timeout=5) as c:
            await c.get(f"{url}/health")
    with pytest.raises(httpx.ConnectError, match=r"8\.8\.8\.8"):
        asyncio.run(call())
    plain = daemon_url.client("https://phone.example", timeout=5)
    assert not isinstance(plain._transport, pinned_http.CheckedTransport)
    asyncio.run(plain.aclose())


def test_the_daemon_media_socket_dials_a_private_address_only(resolver):
    from services.phone import daemon_url
    target = Listener()
    resolver(_answers("127.0.0.1"))
    sock = asyncio.run(daemon_url.ws_socket(f"ws://{NAME}:{target.port}/v1/twilio/media/1", 5))
    assert sock is not None and sock.getpeername()[0] == "127.0.0.1"
    sock.close()
    resolver(_answers("8.8.8.8"))
    with pytest.raises(pinned_http.Refused):
        asyncio.run(daemon_url.ws_socket(f"ws://{NAME}:{target.port}/x", 5))
    assert asyncio.run(daemon_url.ws_socket("wss://phone.example/x", 5)) is None
    target.close()


def test_the_pool_checks_every_dial_the_environment_does_not_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    pool = pinned_http.checked_pool(pinned_http.public_only)
    assert isinstance(pool._transport_for_url(httpx.URL(f"https://{NAME}/")),
                      pinned_http.CheckedTransport)
    assert pool._transport.policy is pinned_http.public_only
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", ".skip.example")
    pool2 = pinned_http.checked_pool(pinned_http.public_only)
    proxied = pool2._transport_for_url(httpx.URL(f"https://{NAME}/"))
    assert not isinstance(proxied, pinned_http.CheckedTransport)
    assert isinstance(pool2._transport_for_url(httpx.URL("https://a.skip.example/")),
                      pinned_http.CheckedTransport)
    assert isinstance(pool2._transport_for_url(httpx.URL("http://plain.example/")),
                      pinned_http.CheckedTransport)
    asyncio.run(pool.aclose())
    asyncio.run(pool2.aclose())


def test_each_site_builds_its_client_with_its_policy(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    from auth.oauth_providers import microsoft
    from services.community import community_catalog
    from services.engines import subscription_windows
    from services.notifications import push_sender
    clients = [
        (subscription_windows._new_client(), pinned_http.public_only),
        (microsoft._client("https://login.microsoftonline.com/x", 30), pinned_http.public_only),
        (community_catalog.http_client(timeout=5), pinned_http.public_only),
    ]
    for client, policy in clients:
        assert isinstance(client._transport, pinned_http.CheckedTransport)
        assert client._transport.policy is policy
        asyncio.run(client.aclose())
    # A catalog pointed at a DEV/TEST override keeps the plain client.
    monkeypatch.setenv("OTODOCK_MCPS_CATALOG_REGISTRY_URL", "http://127.0.0.1:8000/registry.json")
    plain = community_catalog.http_client(timeout=5)
    assert not isinstance(plain._transport, pinned_http.CheckedTransport)
    asyncio.run(plain.aclose())
    # FCM: the module's own SSL context reaches the checked transport.
    ctx = ssl.create_default_context()
    monkeypatch.setattr(push_sender, "_ssl_context", ctx)
    push_sender.reset_fcm_state()
    fcm = asyncio.run(push_sender._fcm_http_client())
    assert fcm._transport.policy is pinned_http.public_only and fcm._transport.ssl_context is ctx
    push_sender.reset_fcm_state()


def test_the_twilio_api_and_the_geocode_hint_ask_through_public_only(monkeypatch):
    from api.hooks import lifecycle
    from services.phone.phone_adapters import twilio
    seen: list = []

    class _Stop(Exception):
        pass

    def recording(url, policy=pinned_http.public_only, **kw):
        seen.append((url.split("/")[2], policy))
        raise _Stop()
    monkeypatch.setattr(pinned_http, "checked_client", recording)

    async def _key(*a, **k):
        return "k"
    import storage.pg as pg
    monkeypatch.setattr(pg, "run_db", _key)
    assert asyncio.run(lifecycle._address_hint("alpha", 37.9, 23.7)) == ""
    adapter = twilio.TwilioAdapter.__new__(twilio.TwilioAdapter)
    monkeypatch.setattr(adapter, "_credentials", lambda: ("AC1", "tok"), raising=False)
    with pytest.raises(_Stop):
        asyncio.run(adapter._request("GET", "/Calls.json"))
    assert seen == [("maps.googleapis.com", pinned_http.public_only),
                    ("api.twilio.com", pinned_http.public_only)]


def test_the_gateway_lookup_is_best_effort_behind_an_egress_proxy(monkeypatch):
    """A name the proxy resolves and this machine cannot passes; an answer in
    the platform's own space and ``localhost`` are refused there as well."""
    from core.credentials import mcp_gateway
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "https_proxy",
                "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")

    def unresolvable(*a, **k):
        raise socket.gaierror("no such name")
    for fake, host, refused in (
            (unresolvable, "mcp.vendor.example", False),
            (_answers("127.0.0.1"), "mcp.vendor.example", True),
            (_answers("8.8.8.8"), "localhost", True),
            (_answers("8.8.8.8"), "api.localhost", True)):
        mcp_gateway.forget_memos()
        monkeypatch.setattr(socket, "getaddrinfo", fake)
        verdict = mcp_gateway._egress_refusal(f"https://{host}")
        assert (verdict is not None) is refused, (host, verdict)
    # Not proxied, a name that does not resolve is still refused.
    monkeypatch.delenv("HTTPS_PROXY")
    mcp_gateway.forget_memos()
    monkeypatch.setattr(socket, "getaddrinfo", unresolvable)
    assert "does not resolve" in mcp_gateway._egress_refusal("https://mcp.vendor.example")
    mcp_gateway.forget_memos()


def test_a_nat64_answer_is_judged_by_the_ipv4_inside_it():
    """An IPv6-only host's DNS64 resolver answers an IPv4-only vendor name
    under ``64:ff9b::/96``: every policy reads the IPv4 inside, so the
    vendor stays reachable and a loopback or metadata address inside is
    refused."""
    vendor, loop, meta, lan = "64:ff9b::808:808", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::a00:5"
    assert not pinned_http.public_only(vendor) and pinned_http.public_only(loop)
    assert not pinned_http.control_plane(vendor) and pinned_http.control_plane(loop)
    assert pinned_http.control_plane(meta) and not pinned_http.control_plane(lan)
    assert not pinned_http.model_server(vendor) and pinned_http.model_server(meta)
    assert not pinned_http.model_server(loop)
    assert pinned_http.private_network(vendor) and not pinned_http.private_network(lan)


def test_the_media_socket_follows_the_websocket_clients_own_proxy_rule(resolver, monkeypatch):
    """The socket is checked exactly when ``websockets`` would dial directly:
    ``ALL_PROXY`` alone is not its proxy (a checked socket), ``http_proxy``
    is (no socket: the proxy resolves the name), a bypassed host is dialled
    directly again."""
    from services.phone import daemon_url
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY",
                "all_proxy", "WS_PROXY", "ws_proxy", "SOCKS_PROXY", "socks_proxy",
                "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    target = Listener()
    resolver(_answers("127.0.0.1"))
    url = f"ws://{NAME}:{target.port}/v1/twilio/media/1"
    monkeypatch.setenv("ALL_PROXY", "http://proxy.internal:3128")
    sock = asyncio.run(daemon_url.ws_socket(url, 5))
    assert sock is not None and sock.getpeername()[0] == "127.0.0.1"
    sock.close()
    monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
    assert asyncio.run(daemon_url.ws_socket(url, 5)) is None
    monkeypatch.setenv("no_proxy", NAME)
    sock = asyncio.run(daemon_url.ws_socket(url, 5))
    assert sock is not None
    sock.close()
    target.close()


def test_the_media_socket_resolve_is_bounded_by_its_timeout(resolver, monkeypatch):
    import time as _time

    from services.phone import daemon_url
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "WS_PROXY", "ws_proxy"):
        monkeypatch.delenv(var, raising=False)

    def stalled(host, port, *a, **kw):
        _time.sleep(2)
        return _answers("127.0.0.1")(host, port)
    resolver(stalled)

    async def call() -> float:
        started = _time.monotonic()
        with pytest.raises(TimeoutError):
            await daemon_url.ws_socket(f"ws://{NAME}:9/x", 0.2)
        return _time.monotonic() - started
    # The call gives up at its timeout (the stalled resolve finishes on its
    # own thread, which asyncio.run then waits for).
    assert asyncio.run(call()) < 1.0
