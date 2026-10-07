"""The proxy sends nothing to the phone daemon over plain http to an
address reachable from the internet (``services/phone/daemon_url.py``):
the daemon's secret, Twilio's signed webhooks and call media cross that
link. Judged at send time; a slow or failing resolver never refuses."""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import pytest
from fastapi import HTTPException

import config
from api.phone import phone_relay
from services.phone import daemon_url


@pytest.fixture(autouse=True)
def _fresh():
    daemon_url._forget()
    yield
    daemon_url._forget()


def _resolver(*addresses):
    def resolve(host, port):
        if not addresses:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]
    return resolve


@pytest.mark.parametrize("url", [
    "https://phone.example.com", "http://127.0.0.1:9093", "http://otodock-phone:9093",
    "http://192.168.1.20:9093", "http://100.100.1.2:9093", "http://[fd12::1]:9093",
])
def test_allowed(url):
    assert daemon_url.judge(url, resolve=_resolver("8.8.8.8")) is None


def test_a_public_address_or_name_over_http_is_refused():
    assert "PHONE_SERVER_URL" in daemon_url.judge("http://8.8.8.8:9093")
    url = "http://phone.example.com:9093"
    assert daemon_url.judge(url, resolve=_resolver("8.8.4.4", "2001:4860::8844"))
    assert daemon_url.judge(url, resolve=_resolver("10.0.0.2", "2a02:587::2")) is None
    assert daemon_url.judge(url, resolve=_resolver("10.0.0.2")) is None
    assert daemon_url.judge(url, resolve=_resolver()) is None


def test_the_verdict_is_kept_and_one_error_logged(monkeypatch, caplog):
    asked = []
    monkeypatch.setattr(daemon_url, "judge", lambda url: asked.append(url) or "refused")
    with caplog.at_level(logging.ERROR, logger="claude-proxy.phone"):
        for _ in range(3):
            assert asyncio.run(daemon_url.refusal("http://x.example:9093")) == "refused"
    assert len(asked) == 1
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_a_slow_resolver_never_refuses(monkeypatch):
    monkeypatch.setattr(daemon_url, "_RESOLVE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(daemon_url, "judge", lambda url: time.sleep(0.3) or "refused")
    assert asyncio.run(daemon_url.refusal("http://slow.example:9093")) is None
    assert daemon_url._verdicts == {}


def test_the_relay_sends_nothing_on_a_refused_url(monkeypatch):
    sent = []

    class _Client:
        def __init__(self, *a, **kw):
            sent.append(kw)
    monkeypatch.setattr(phone_relay.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://8.8.8.8:9093")
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._relay("GET", "/api/calls/c1"))
    assert e.value.status_code == 502 and "https" in e.value.detail
    assert sent == []


def test_the_twilio_relay_forwards_nothing_on_a_refused_url(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.phone import twilio_relay

    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://8.8.8.8:9093")
    monkeypatch.setattr(twilio_relay.httpx, "AsyncClient",
                        lambda *a, **kw: pytest.fail("the relay dialled the daemon"))
    app = FastAPI()
    app.include_router(twilio_relay.router)
    r = TestClient(app).post("/v1/twilio/inbound/1", content=b"CallSid=CA1",
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 502
