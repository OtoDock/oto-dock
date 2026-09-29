"""The vendored outbound-URL guard (``_url_guard.py``, a byte copy of the
proxy's ``services/infra/outbound_url.py``) as ``_download`` uses it: every
address the host resolves to must be public, on every redirect hop."""

import asyncio
import socket

import httpx

import _url_guard
import server


def _resolver(table):
    def fake(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]
    return fake


def test_validate_refuses_private_and_accepts_public(monkeypatch):
    monkeypatch.setattr(_url_guard.socket, "getaddrinfo", _resolver({
        "public.example": ["93.184.216.34"],
        "lan.example": ["93.184.216.34", "192.168.1.9"],
    }))
    assert _url_guard.validate_outbound_url("https://public.example/a.mp3") is None
    assert "192.168.1.9" in _url_guard.validate_outbound_url("https://lan.example/a.mp3")
    assert "127.0.0.1" in _url_guard.validate_outbound_url("http://127.0.0.1:8400/v1/x")
    assert "scheme" in _url_guard.validate_outbound_url("ftp://public.example/x")
    assert "does not resolve" in _url_guard.validate_outbound_url("https://nowhere.example/x")


def _client_factory(monkeypatch, handler):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


def test_download_refuses_a_redirect_into_the_lan(monkeypatch):
    monkeypatch.setattr(_url_guard.socket, "getaddrinfo", _resolver({"public.example": ["93.184.216.34"]}))

    def handler(request):
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError(f"fetched {request.url}")

    _client_factory(monkeypatch, handler)
    data, name, err = asyncio.run(server._download("http://public.example/a.mp3", 1))
    assert data == b"" and "169.254.169.254" in err


def test_download_follows_a_public_redirect_and_caps_the_size(monkeypatch):
    monkeypatch.setattr(_url_guard.socket, "getaddrinfo", _resolver({
        "public.example": ["93.184.216.34"], "cdn.example": ["93.184.216.35"],
    }))

    def handler(request):
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/b.mp3"})
        return httpx.Response(200, content=b"audio-bytes")

    _client_factory(monkeypatch, handler)
    data, name, err = asyncio.run(server._download("http://public.example/a.mp3", 1))
    assert (data, name, err) == (b"audio-bytes", "b.mp3", None)


def test_download_refuses_the_url_itself_first(monkeypatch):
    def handler(request):
        raise AssertionError(f"fetched {request.url}")

    _client_factory(monkeypatch, handler)
    data, name, err = asyncio.run(server._download("http://10.0.0.5/a.mp3", 1))
    assert data == b"" and "10.0.0.5" in err
