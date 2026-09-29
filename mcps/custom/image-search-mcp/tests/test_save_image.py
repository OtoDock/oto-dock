"""``save_image`` fetches an agent-chosen URL from this process: the host and
every redirect hop must answer with a public address (the vendored
``_url_guard``), redirects are followed by hand, the file lands under the
workspace."""

import asyncio
import socket

import httpx

import _url_guard
import server

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00\x18\xdd\x8d\xb1\x00\x00\x00\x00IEND\xaeB`\x82")


def _resolver(table):
    def fake(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]
    return fake


def _client_factory(monkeypatch, handler):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


def test_a_private_url_is_refused_before_any_request(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "IMAGE_WORKSPACE", str(tmp_path))
    _client_factory(monkeypatch, lambda r: (_ for _ in ()).throw(AssertionError(f"fetched {r.url}")))
    out = asyncio.run(server._tool_save_image({"url": "http://127.0.0.1:8400/v1/x.png", "dest_path": "x.png"}))
    assert "127.0.0.1" in out["error"]
    assert not list(tmp_path.rglob("*"))


def test_a_redirect_into_the_lan_is_refused_on_head_and_get(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "IMAGE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(_url_guard.socket, "getaddrinfo", _resolver({"img.example": ["93.184.216.34"]}))
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.url.host == "img.example":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
        raise AssertionError(f"fetched {request.url}")

    _client_factory(monkeypatch, handler)
    out = asyncio.run(server._tool_save_image({"url": "http://img.example/a.png", "dest_path": "a.png"}))
    assert "169.254.169.254" in out["error"]
    assert seen == [("HEAD", "http://img.example/a.png")]
    assert not list(tmp_path.rglob("*"))


def test_a_public_redirect_lands_the_image(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "IMAGE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(_url_guard.socket, "getaddrinfo", _resolver({
        "img.example": ["93.184.216.34"], "cdn.example": ["93.184.216.35"],
    }))

    def handler(request):
        if request.url.host == "img.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/real.png"})
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Type": "image/png", "Content-Length": str(len(PNG))})
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=PNG)

    _client_factory(monkeypatch, handler)
    out = asyncio.run(server._tool_save_image({"url": "http://img.example/a.png", "dest_path": "pics/a.png"}))
    assert out.get("error") is None, out
    assert out["saved_path"] == str(tmp_path / "pics" / "a.png")
    assert (tmp_path / "pics" / "a.png").read_bytes() == PNG
    assert out["size_bytes"] == len(PNG) and out["width"] == 1


def test_too_many_redirects_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "IMAGE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(_url_guard.socket, "getaddrinfo", _resolver({"img.example": ["93.184.216.34"]}))
    _client_factory(monkeypatch, lambda r: httpx.Response(302, headers={"Location": "https://img.example/again.png"}))
    out = asyncio.run(server._tool_save_image({"url": "http://img.example/a.png"}))
    assert out["error"] == "too many redirects"
