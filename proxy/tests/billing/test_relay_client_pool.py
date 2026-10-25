"""The relay client's HTTP clients: one per loop, its SSL context built once
off the loop and carried by a checked transport, instead of a new client and
a fresh context per call; the session mint on the sync checked client. Both
connect only to addresses outside this machine's own address space."""

import asyncio
import ssl

import httpx
import pytest

import config
from services.billing import relay_client
from services.infra import pinned_http

pytestmark = pytest.mark.asyncio


class _Response:
    status_code = 200

    def json(self):
        return {"ok": True, "token": "tok"}

    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _direct(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


async def test_relay_posts_share_one_client_per_loop(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.test")
    built: list[dict] = []
    posted: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            built.append(kwargs)

        async def post(self, url, json=None):
            posted.append(url)
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    on_loop: list[bool] = []
    ctx = ssl.create_default_context()

    def build_context():
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return ctx

    monkeypatch.setattr(relay_client, "_build_ssl_context", build_context)
    monkeypatch.setattr(relay_client, "_ssl_context", None)
    relay_client.reset_relay_client_state()

    assert await relay_client._relay_post("/a", {}) == {"ok": True, "token": "tok"}
    assert await relay_client._relay_post("/b", {}) == {"ok": True, "token": "tok"}
    assert posted == ["https://relay.test/a", "https://relay.test/b"]
    assert len(built) == 1 and on_loop == [False]
    # The context rides on the checked transport: a client's own verify is
    # ignored once a transport is given.
    transport = built[0]["transport"]
    assert isinstance(transport, pinned_http.CheckedTransport)
    assert transport.ssl_context is ctx and transport.policy is pinned_http.control_plane
    assert "verify" not in built[0]
    relay_client.reset_relay_client_state()


async def test_the_session_mint_posts_through_the_sync_checked_client(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.test/")
    monkeypatch.setattr(relay_client, "_require_relay", lambda: None)
    monkeypatch.setattr(relay_client, "is_connected", lambda: True)
    monkeypatch.setattr(relay_client, "_link_identity", lambda: {"account_token": "acct"})
    relay_client._session_token_cache.clear()
    built: list[dict] = []
    posted: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            built.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None):
            posted.append(url)
            return _Response()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert relay_client.mint_session_token("user-1") == "tok"
    assert posted == ["https://relay.test/v1/relay/session/mint"]
    transport = built[0]["transport"]
    assert isinstance(transport, pinned_http.CheckedSyncTransport)
    assert transport.policy is pinned_http.control_plane
    relay_client._session_token_cache.clear()


async def test_a_relay_on_loopback_is_refused_at_the_dial_and_fails_open(monkeypatch):
    """The named behaviour change: a base that resolves to this machine's
    own address space never connects, and the call reads as the transient
    outage the module promises."""
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.test")
    monkeypatch.setattr(pinned_http, "_getaddrinfo", lambda host, port: [
        (2, 1, 6, "", ("127.0.0.1", port))])
    monkeypatch.setattr(relay_client, "_ssl_context", ssl.create_default_context())
    relay_client.reset_relay_client_state()
    with pytest.raises(httpx.ConnectError, match=r"may not reach \(127\.0\.0\.1\)"):
        await relay_client._relay_post("/v1/relay/ping", {})
    monkeypatch.setattr(relay_client, "_require_relay", lambda: None)
    monkeypatch.setattr(relay_client, "is_connected", lambda: True)
    monkeypatch.setattr(relay_client, "_link_identity", lambda: {"account_token": "acct"})
    relay_client._session_token_cache.clear()
    with pytest.raises(relay_client.RelayNotConfigured, match="not answering"):
        relay_client.mint_session_token("user-1")
    relay_client.reset_relay_client_state()
