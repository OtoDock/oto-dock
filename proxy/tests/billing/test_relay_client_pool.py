"""The relay client's HTTP client: one per loop, its SSL context built once
off the loop, instead of a new client and a fresh context per call."""

import asyncio

import httpx
import pytest

import config
from services.billing import relay_client

pytestmark = pytest.mark.asyncio


class _Response:
    status_code = 200

    def json(self):
        return {"ok": True}

    def raise_for_status(self):
        pass


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

    def build_context():
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return "ctx"

    monkeypatch.setattr(relay_client, "_build_ssl_context", build_context)
    monkeypatch.setattr(relay_client, "_ssl_context", None)
    relay_client.reset_relay_client_state()

    assert await relay_client._relay_post("/a", {}) == {"ok": True}
    assert await relay_client._relay_post("/b", {}) == {"ok": True}
    assert posted == ["https://relay.test/a", "https://relay.test/b"]
    assert len(built) == 1 and built[0]["verify"] == "ctx"
    assert on_loop == [False]
    relay_client.reset_relay_client_state()
