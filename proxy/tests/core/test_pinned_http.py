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
