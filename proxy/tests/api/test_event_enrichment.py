"""Event enrichment (finding C) — manifest-driven ID→name lookups:
success path, fail-open matrix, caching. No DB; httpx mocked."""

from __future__ import annotations

import asyncio
import sys

import httpx

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.webhook_providers.base import NormalizedEvent  # noqa: E402
from services.webhooks import event_enrichment  # noqa: E402

_ROW = {
    "id": "sub-1", "provider_id": "slack", "scope": "user",
    "owner": "alice", "account_label": "a@x.com",
}


def _block(ttl: int = 900) -> dict:
    return {
        "enrichment": {
            "lookups": [
                {
                    "source_field": "actor.id",
                    "request": {
                        "method": "GET",
                        "url_template": "https://slack.test/api/users.info?user=${value}",
                        "headers": {"Authorization": "Bearer ${account.access_token}"},
                        "expected_status": [200],
                    },
                    "outputs": {
                        "actor.name": [
                            "body.user.profile.display_name",
                            "body.user.real_name",
                        ],
                    },
                    "ttl_seconds": ttl,
                },
            ],
        },
    }


def _event(actor_id: str = "U123") -> NormalizedEvent:
    return NormalizedEvent(
        event_type="message.channels", vendor_event_id="Ev1",
        actor={"id": actor_id}, subject={}, target={"id": "T1"},
    )


def _run(event, *, block, monkeypatch, responder, token="xoxp-tok"):
    monkeypatch.setattr(event_enrichment, "_cache", {})
    monkeypatch.setattr(
        "services.webhooks.subscription_manager._resolve_token_or_raise",
        lambda **kw: token() if callable(token) else token,
    )
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(responder)
    monkeypatch.setattr(
        event_enrichment.httpx, "AsyncClient",
        lambda **kw: real_client(transport=transport,
                                 timeout=kw.get("timeout")),
    )
    asyncio.run(event_enrichment.enrich_event(
        event, row=_ROW, webhooks_block=block))


def test_enrichment_writes_resolved_name(monkeypatch):
    calls: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer xoxp-tok"
        assert request.url.params["user"] == "U123"
        return httpx.Response(200, json={
            "ok": True,
            "user": {"real_name": "Jimmy M",
                     "profile": {"display_name": "dave"}},
        })

    ev = _event()
    _run(ev, block=_block(), monkeypatch=monkeypatch, responder=responder)
    assert ev.actor["name"] == "dave"      # first fallback path wins
    assert len(calls) == 1


def test_enrichment_fallback_path_used(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "user": {"real_name": "Jimmy M", "profile": {}},
        })

    ev = _event()
    _run(ev, block=_block(), monkeypatch=monkeypatch, responder=responder)
    assert ev.actor["name"] == "Jimmy M"


def test_enrichment_vendor_error_leaves_event_untouched(monkeypatch):
    ev = _event()
    _run(ev, block=_block(), monkeypatch=monkeypatch,
         responder=lambda r: httpx.Response(500, json={}))
    assert "name" not in ev.actor


def test_enrichment_network_error_leaves_event_untouched(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ev = _event()
    _run(ev, block=_block(), monkeypatch=monkeypatch, responder=responder)
    assert "name" not in ev.actor


def test_enrichment_missing_token_skips_lookup(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the vendor without a token")

    def raise_token():
        raise RuntimeError("no account")

    ev = _event()
    _run(ev, block=_block(), monkeypatch=monkeypatch, responder=responder,
         token=raise_token)
    assert "name" not in ev.actor


def test_enrichment_empty_source_field_skips(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the vendor for empty source")

    ev = _event(actor_id="")
    ev.actor.pop("id", None)
    _run(ev, block=_block(), monkeypatch=monkeypatch, responder=responder)
    assert "name" not in ev.actor


def test_enrichment_cache_hit_avoids_second_call(monkeypatch):
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={
            "ok": True, "user": {"profile": {"display_name": "dave"}},
        })

    monkeypatch.setattr(event_enrichment, "_cache", {})
    monkeypatch.setattr(
        "services.webhooks.subscription_manager._resolve_token_or_raise",
        lambda **kw: "xoxp-tok")
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(responder)
    monkeypatch.setattr(
        event_enrichment.httpx, "AsyncClient",
        lambda **kw: real_client(transport=transport, timeout=kw.get("timeout")))

    ev1, ev2 = _event(), _event()
    asyncio.run(event_enrichment.enrich_event(
        ev1, row=_ROW, webhooks_block=_block()))
    asyncio.run(event_enrichment.enrich_event(
        ev2, row=_ROW, webhooks_block=_block()))
    assert len(calls) == 1
    assert ev1.actor["name"] == "dave" and ev2.actor["name"] == "dave"


def test_enrichment_no_block_is_noop(monkeypatch):
    ev = _event()
    asyncio.run(event_enrichment.enrich_event(ev, row=_ROW, webhooks_block={}))
    assert "name" not in ev.actor
