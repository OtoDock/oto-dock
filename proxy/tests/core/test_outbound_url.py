"""``services.infra.outbound_url`` — the outbound-URL validator (core-seams
phase 3): the address classes, the client-identical hostname encoding, the
reasons, and the two MCPs' byte copies."""

from __future__ import annotations

import socket

import pytest

from services.infra import outbound_url
from services.infra.outbound_url import encoded_host, ip_blocked, validate_outbound_url
from services.notifications import push_sender
from tests._paths import REPO_ROOT

BLOCKED = [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1",        # CGNAT — ``is_private`` is False, ``is_global`` is False
    "192.0.2.1", "198.51.100.1", "203.0.113.1",  # the documentation ranges
    "198.18.0.1", "240.0.0.1", "0.0.0.0", "224.0.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fec0::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    "nope", "",
]
PUBLIC = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111", "2001:4860:4860::8888"]


@pytest.mark.parametrize("ip", BLOCKED)
def test_blocked(ip):
    assert ip_blocked(ip), ip


@pytest.mark.parametrize("ip", PUBLIC)
def test_public(ip):
    assert not ip_blocked(ip), ip


def test_the_host_is_encoded_the_way_the_clients_encode_it():
    """httpx and requests connect to the IDNA-2008 label; the stdlib codec
    would resolve a different name for ``faß``."""
    assert encoded_host("example.com") == "example.com"
    assert encoded_host("bücher.example") == "xn--bcher-kva.example"
    assert encoded_host("faß.example") == "xn--fa-hia.example"
    with pytest.raises(ValueError):
        encoded_host("a" * 70 + ".example")


def _resolver(table: dict[str, list[str]], asked: list):
    def fake(host, port, *args, **kwargs):
        asked.append((host, port))
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]
    return fake


def test_validate_outbound_url(monkeypatch):
    asked: list = []
    monkeypatch.setattr(outbound_url.socket, "getaddrinfo", _resolver({
        "public.example": ["93.184.216.34"],
        "both.example": ["93.184.216.34", "10.0.0.5"],
        "xn--fa-hia.example": ["93.184.216.34"],
    }, asked))
    assert validate_outbound_url("https://public.example/a") is None
    assert validate_outbound_url("http://public.example/a") is None
    assert "must be https" in validate_outbound_url("http://public.example/a", require_https=True)
    assert "10.0.0.5" in validate_outbound_url("https://both.example/a")
    assert "does not resolve" in validate_outbound_url("https://nowhere.example/a")
    assert "scheme" in validate_outbound_url("ftp://public.example/a")
    assert "no host" in validate_outbound_url("https:///a")
    assert validate_outbound_url("") == "no URL"
    assert "not a valid hostname" in validate_outbound_url("https://" + "a" * 70 + ".example/")
    # An IP literal needs no resolver and is judged as itself.
    assert "127.0.0.1" in validate_outbound_url("http://127.0.0.1:8400/v1/x")
    assert "169.254.169.254" in validate_outbound_url("http://169.254.169.254/latest/")
    # The label asked of the resolver is the client's, the port the URL's.
    assert validate_outbound_url("https://faß.example/x") is None
    assert validate_outbound_url("https://public.example:8443/x") is None
    assert ("xn--fa-hia.example", 443) in asked and ("public.example", 8443) in asked
    assert ("public.example", 80) in asked
    # Userinfo does not move the host.
    assert validate_outbound_url("https://user@both.example/x")


def test_the_push_endpoint_guard_is_the_validator(monkeypatch):
    seen = []
    monkeypatch.setattr(push_sender, "validate_outbound_url",
                        lambda url, **kw: seen.append((url, kw)) or "nope")
    assert push_sender._endpoint_is_public("https://push.example/x") is False
    assert seen == [("https://push.example/x", {"require_https": True})]


def test_the_push_endpoint_guard_refuses_what_requests_reads_differently(monkeypatch):
    # requests connects to 10.0.0.5 here while urlsplit reads example.com.
    monkeypatch.setattr(push_sender, "validate_outbound_url", lambda url, **kw: None)
    assert push_sender._endpoint_is_public("https://10.0.0.5:8443\\@example.com/") is False
    assert push_sender._endpoint_is_public("https://user@push.example/x") is False
    assert push_sender._endpoint_is_public("https://push.example/x") is True


def test_a_web_push_follows_no_redirect(monkeypatch):
    import asyncio
    import config
    sent = {}

    def _webpush(**kw):
        sent["max_redirects"] = kw["requests_session"].max_redirects
    monkeypatch.setattr(push_sender, "_webpush_available", True)
    monkeypatch.setattr(push_sender, "webpush", _webpush, raising=False)
    monkeypatch.setattr(push_sender, "_endpoint_is_public", lambda e: True)
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "k")
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "p")
    sub = '{"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}}'
    assert asyncio.run(push_sender.send_web_push(sub, {"t": 1})) is True
    assert sent == {"max_redirects": 0}


def test_the_two_mcps_carry_byte_copies():
    """``transcribe-mcp`` and ``image-search-mcp`` import nothing from the
    proxy: their ``_url_guard.py`` is this module, byte for byte (the gate's
    twin rule pins the functions; this pins the files)."""
    ours = (REPO_ROOT / "proxy" / "services" / "infra" / "outbound_url.py").read_bytes()
    for mcp in ("transcribe-mcp", "image-search-mcp"):
        copy = REPO_ROOT / "mcps" / "custom" / mcp / "_url_guard.py"
        assert copy.read_bytes() == ours, f"{copy} drifted from outbound_url.py — copy it again"
