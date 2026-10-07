"""Dispatcher pipeline — catalog-key canonicalization (finding B) + the
selected_events gate (finding A), exercised through
``_process_subscription_events`` with stores monkeypatched.

Live repros covered:
  * A stray ``function_executed_success`` event fired an all-events trigger
    because selected_events was never consulted at dispatch.
  * A trigger filtering ``event_type: message.channels`` could never match —
    payloads carry the raw ``event.type="message"``.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth import webhook_providers  # noqa: E402
from auth.webhook_providers.generic import GenericWebhookProvider  # noqa: E402
from api.events import webhook_body  # noqa: E402
from services.webhooks import webhook_dispatcher  # noqa: E402
from services.webhooks.event_normalizer import resolve_catalog_keys  # noqa: E402
from storage.identity import credential_store # noqa: E402
from storage.automation import trigger_store # noqa: E402
from storage.automation import webhook_subscription_store # noqa: E402

_CATALOG = [
    {
        "key": "message.channels", "label": "Channel messages",
        "match": {
            "event_type": "message",
            "conditions": {"body.event.channel_type": ["channel"]},
        },
    },
    {
        "key": "message.im", "label": "DMs",
        "match": {
            "event_type": "message",
            "conditions": {"body.event.channel_type": "im"},
        },
    },
    {"key": "reaction_added", "label": "Reactions"},
]

_WEBHOOKS_BLOCK = {
    "available": True,
    "provider_id": "slack",
    "event_catalog": _CATALOG,
    "payload_normalization": {
        "event_type_path": "body.event.type",
        "actor": {"id_path": "body.event.user"},
        "target": {"type": "team", "id_path": "body.team_id"},
    },
    "event_id_field": "body.event_id",
}


def _slack_body(etype: str, *, channel_type: str = "", event_id: str = "Ev1") -> dict:
    event: dict = {"type": etype, "user": "U1"}
    if channel_type:
        event["channel_type"] = channel_type
    return {
        "type": "event_callback", "team_id": "T1",
        "event_id": event_id, "event": event,
    }


def _row(sub_id: str, selected: list[str]) -> dict:
    return {
        "id": sub_id, "provider_id": "slack", "mcp_name": "slack-mcp",
        "status": "active", "selected_events": json.dumps(selected),
    }


def _process(row: dict, body: dict, monkeypatch, *, triggers=None, fired_log=None):
    """Run _process_subscription_events with the stores stubbed."""
    monkeypatch.setattr(
        webhook_subscription_store, "record_event_received", lambda sid: None)
    monkeypatch.setattr(
        trigger_store, "list_triggers", lambda **kw: triggers or [])

    async def fake_fan_out(*, triggers, body, event, trigger_source):
        if fired_log is not None:
            fired_log.append((event.event_type, [t.get("id") for t in triggers]))
        return (len(triggers), [])

    monkeypatch.setattr(webhook_dispatcher, "_fan_out_fire", fake_fan_out)
    provider = GenericWebhookProvider(provider_id="slack")
    return asyncio.run(webhook_dispatcher._process_subscription_events(
        row=row, provider=provider, webhooks_block=_WEBHOOKS_BLOCK,
        parsed_body=body, headers_lc={},
    ))


# --- finding A: the selected_events gate ---------------------------------------

def test_stray_event_ignored_by_selected_events_gate(monkeypatch):
    """LIVE REPRO: subscription selects reaction_added only; an all-events
    trigger exists; a stray function_executed_success arrives → ignored,
    nothing fires."""
    fired: list = []
    trigger = {"id": "t-all", "subscription_id": "sub-A", "event_filter": "{}"}
    out = _process(
        _row("sub-A", ["reaction_added"]),
        _slack_body("function_executed_success", event_id="Ev-stray"),
        monkeypatch, triggers=[trigger], fired_log=fired,
    )
    assert out["status"] == "ignored" and out["fired"] == 0
    assert fired == []


def test_selected_event_passes_gate_and_fires(monkeypatch):
    fired: list = []
    trigger = {"id": "t-all", "subscription_id": "sub-B", "event_filter": "{}"}
    out = _process(
        _row("sub-B", ["reaction_added"]),
        _slack_body("reaction_added", event_id="Ev-ok"),
        monkeypatch, triggers=[trigger], fired_log=fired,
    )
    assert out["status"] == "ok" and out["fired"] == 1
    assert fired == [("reaction_added", ["t-all"])]


def test_empty_selection_means_no_gate(monkeypatch):
    """Subscriptions without selected_events keep legacy behavior."""
    fired: list = []
    trigger = {"id": "t-all", "subscription_id": "sub-C", "event_filter": "{}"}
    out = _process(
        _row("sub-C", []),
        _slack_body("anything_at_all", event_id="Ev-any"),
        monkeypatch, triggers=[trigger], fired_log=fired,
    )
    assert out["fired"] == 1


# --- finding B: canonicalization to catalog keys -------------------------------

def test_channel_message_canonicalizes_and_matches_catalog_filter(monkeypatch):
    """LIVE GAP: a trigger filtering event_type=message.channels must match a
    real channel message (raw event.type='message' + channel_type=channel)."""
    fired: list = []
    trigger = {
        "id": "t-chan", "subscription_id": "sub-D",
        "event_filter": json.dumps({"event_type": "message.channels"}),
    }
    out = _process(
        _row("sub-D", ["message.channels"]),
        _slack_body("message", channel_type="channel", event_id="Ev-msg1"),
        monkeypatch, triggers=[trigger], fired_log=fired,
    )
    assert out["status"] == "ok" and out["fired"] == 1
    assert fired == [("message.channels", ["t-chan"])]
    assert out["event_type"] == "message.channels"  # canonical in the response


def test_im_payload_ignored_when_only_channels_selected(monkeypatch):
    fired: list = []
    trigger = {"id": "t-chan", "subscription_id": "sub-E", "event_filter": "{}"}
    out = _process(
        _row("sub-E", ["message.channels"]),
        _slack_body("message", channel_type="im", event_id="Ev-msg2"),
        monkeypatch, triggers=[trigger], fired_log=fired,
    )
    assert out["status"] == "ignored" and out["fired"] == 0
    assert fired == []


def test_plain_key_event_keeps_raw_type(monkeypatch):
    """Catalog entries without match blocks behave exactly as before."""
    fired: list = []
    trigger = {
        "id": "t-react", "subscription_id": "sub-F",
        "event_filter": json.dumps({"event_type": "reaction_added"}),
    }
    out = _process(
        _row("sub-F", ["reaction_added"]),
        _slack_body("reaction_added", event_id="Ev-r2"),
        monkeypatch, triggers=[trigger], fired_log=fired,
    )
    assert out["fired"] == 1


# --- resolve_catalog_keys (pure) ------------------------------------------------

def test_resolve_catalog_keys_first_match_wins():
    body = _slack_body("message", channel_type="channel")
    keys = resolve_catalog_keys(
        body=body, headers={}, raw_event_type="message", event_catalog=_CATALOG)
    assert keys == ["message.channels"]


def test_resolve_catalog_keys_any_of_list_and_str():
    im = _slack_body("message", channel_type="im")
    assert resolve_catalog_keys(
        body=im, headers={}, raw_event_type="message",
        event_catalog=_CATALOG) == ["message.im"]
    group = _slack_body("message", channel_type="mpim")
    assert resolve_catalog_keys(
        body=group, headers={}, raw_event_type="message",
        event_catalog=_CATALOG) == []


def test_resolve_catalog_keys_plain_key_and_no_match():
    body = _slack_body("reaction_added")
    assert resolve_catalog_keys(
        body=body, headers={}, raw_event_type="reaction_added",
        event_catalog=_CATALOG) == ["reaction_added"]
    assert resolve_catalog_keys(
        body=body, headers={}, raw_event_type="totally_unknown",
        event_catalog=_CATALOG) == []


# --- relay-forwarded ingest ------------------------------------------------------

import hashlib
import hmac as hmac_mod
import time
from types import SimpleNamespace

from services.billing import relay_client

_FORWARD_SECRET = "fwd-secret"


def _forward_headers(body: bytes, *, ts: str | None = None,
                     secret: str = _FORWARD_SECRET,
                     provider: str = "slack") -> dict:
    ts = str(int(time.time())) if ts is None else ts
    sig = "v0=" + hmac_mod.new(
        secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256,
    ).hexdigest()
    return {
        "x-otodock-event-signature": sig,
        "x-otodock-event-timestamp": ts,
        "x-otodock-event-provider": provider,
    }


def _setup_relay_env(monkeypatch, rows: list[dict]) -> list[str]:
    """Manifest + forward secret + subscription rows stubbed; returns the
    list that records which subscription ids got processed."""
    webhook_dispatcher.reset_caches()
    manifest = SimpleNamespace(credentials=SimpleNamespace(
        webhooks={**_WEBHOOKS_BLOCK, "workspace_id_path": "body.team_id"},
        oauth=None,
    ))
    monkeypatch.setattr(
        webhook_dispatcher.mcp_registry, "get_all_manifests",
        lambda: {"slack-mcp": manifest})
    monkeypatch.setattr(
        credential_store, "get_infra_credentials",
        lambda slug: (
            {relay_client.EVENTS_FORWARD_SECRET_KEY: _FORWARD_SECRET}
            if slug == relay_client.EVENTS_FORWARD_SECRET_SLUG else {}
        ))

    def fake_list(**kw):
        return [
            r for r in rows
            if r.get("vendor_target") == kw.get("vendor_target")
            and r.get("delivery_mode") == kw.get("delivery_mode")
            and r.get("provider_id") == kw.get("provider_id")
        ]

    monkeypatch.setattr(
        webhook_subscription_store, "list_subscriptions", fake_list)
    processed: list[str] = []

    async def fake_process(*, row, provider, webhooks_block, parsed_body,
                           headers_lc):
        processed.append(row["id"])
        return {"status": "ok", "fired": 1, "event_type": "reaction_added"}

    monkeypatch.setattr(
        webhook_dispatcher, "_process_subscription_events", fake_process)
    return processed


def _relay_rows() -> list[dict]:
    base = {
        "provider_id": "slack", "mcp_name": "slack-mcp", "status": "active",
        "delivery_mode": "relay", "selected_events": "[]",
    }
    return [
        {**base, "id": "s-relay-1", "vendor_target": "T1"},
        {**base, "id": "s-relay-2", "vendor_target": "T1"},
        {**base, "id": "s-other-team", "vendor_target": "T2"},
        {**base, "id": "s-vendor-mode", "vendor_target": "T1",
         "delivery_mode": "vendor"},
    ]


def test_relay_ingest_fans_into_matching_relay_rows(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    body = json.dumps({"team_id": "T1", "event": {"type": "reaction_added"}}).encode()
    status, resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="slack", raw_body=body, headers=_forward_headers(body)))
    assert status == 200 and resp["fired"] == 2
    # Same-team relay rows only — the vendor-mode row and the T2 row stay
    # untouched (vendor_target equality = the mis-route defense).
    assert sorted(processed) == ["s-relay-1", "s-relay-2"]


def test_relay_ingest_tampered_body_401(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    body = json.dumps({"team_id": "T1", "event": {"type": "reaction_added"}}).encode()
    headers = _forward_headers(body)
    status, resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="slack", raw_body=body + b" ", headers=headers))
    assert status == 401 and processed == []


def test_relay_ingest_stale_timestamp_401(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    body = json.dumps({"team_id": "T1", "event": {"type": "reaction_added"}}).encode()
    stale = str(int(time.time()) - 3600)
    status, _resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="slack", raw_body=body,
        headers=_forward_headers(body, ts=stale)))
    assert status == 401 and processed == []


def test_relay_ingest_unknown_team_no_subscriptions(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    body = json.dumps({"team_id": "T_UNKNOWN", "event": {"type": "x"}}).encode()
    status, resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="slack", raw_body=body, headers=_forward_headers(body)))
    assert status == 200 and resp["status"] == "no_subscriptions"
    assert processed == []


def test_relay_ingest_no_workspace_id_ignored(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    body = json.dumps({"event": {"type": "x"}}).encode()
    status, resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="slack", raw_body=body, headers=_forward_headers(body)))
    assert status == 200 and resp["status"] == "ignored"
    assert processed == []


def test_relay_ingest_provider_header_mismatch_400(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    body = json.dumps({"team_id": "T1"}).encode()
    status, _resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="slack", raw_body=body,
        headers=_forward_headers(body, provider="github")))
    assert status == 400 and processed == []


def test_relay_ingest_missing_local_secret_401(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    monkeypatch.setattr(
        credential_store, "get_infra_credentials", lambda slug: {})
    body = json.dumps({"team_id": "T1"}).encode()
    status, _resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="slack", raw_body=body, headers=_forward_headers(body)))
    assert status == 401 and processed == []


# --- notion: verification-token capture + ts-less per-subscription signature ----

_NOTION_BLOCK = {
    "available": True,
    "provider_id": "notion",
    "signature": {
        "algorithm": "hmac-sha256",
        "header": "X-Notion-Signature",
        "prefix": "sha256=",
        "per_subscription_secret": True,
    },
    "url_verification": {
        "kind": "verification_token_capture",
        "request_field": "verification_token",
        "request_source": "body",
        "response_field": "ok",
        "response_content_type": "application/json",
    },
    "event_catalog": [
        {"key": "comment.created", "label": "Comments"},
        {"key": "page.content_updated", "label": "Page edits"},
    ],
    "payload_normalization": {
        "event_type_path": "body.type",
        "actor": {"id_path": "body.authors.0.id"},
        "subject": {"type_path": "body.entity.type", "id_path": "body.entity.id"},
    },
    "event_id_field": "body.id",
    "workspace_id_path": "body.workspace_id",
}

_NOTION_TOKEN = "ntn-verification-token"


def _notion_event_body(etype: str = "comment.created", event_id: str = "nev-1") -> dict:
    return {
        "id": event_id, "type": etype, "workspace_id": "WS1",
        "authors": [{"id": "u-alice", "type": "person"}],
        "entity": {"id": "page-1", "type": "page"},
        "attempt_number": 1,
    }


def _notion_sig_headers(body: bytes, secret: str = _NOTION_TOKEN) -> dict:
    sig = "sha256=" + hmac_mod.new(
        secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Notion-Signature": sig}


def _setup_notion_sub(monkeypatch, *, row_secret: str):
    """Stub the store + manifest around dispatch_webhook for one notion row.
    Returns (stored_secrets, processed) recorders."""
    webhook_dispatcher.reset_caches()
    row = {
        "id": "nsub-1", "provider_id": "notion", "mcp_name": "notion-mcp",
        "status": "active", "selected_events": "[]",
        "delivery_mode": "vendor",
    }
    manifest = SimpleNamespace(credentials=SimpleNamespace(
        webhooks=_NOTION_BLOCK, oauth=None))
    monkeypatch.setattr(
        webhook_dispatcher.mcp_registry, "get_manifest",
        lambda name: manifest if name == "notion-mcp" else None)
    # notion has no hardcoded provider class — seed the lazy manifest cache
    # (the real path builds it from a loaded manifest).
    monkeypatch.setitem(
        webhook_providers._MANIFEST_CACHE, "notion",
        GenericWebhookProvider(provider_id="notion"))
    monkeypatch.setattr(
        webhook_subscription_store, "get_subscription",
        lambda sid: row if sid == "nsub-1" else None)
    secret_holder = {"value": row_secret}
    monkeypatch.setattr(
        webhook_subscription_store, "get_signing_secret",
        lambda sid: secret_holder["value"])
    stored: list[str] = []

    def fake_update(sid, secret):
        stored.append(secret)
        secret_holder["value"] = secret

    monkeypatch.setattr(
        webhook_subscription_store, "update_signing_secret", fake_update)
    monkeypatch.setattr(
        webhook_subscription_store, "record_event_received", lambda sid: None)
    monkeypatch.setattr(
        trigger_store, "list_triggers", lambda **kw: [])
    return stored


def _dispatch_notion(body: bytes, headers: dict):
    return asyncio.run(webhook_dispatcher.dispatch_webhook(
        provider_id="notion", subscription_id="nsub-1",
        raw_body=body, headers=headers, query_params={},
    ))


def test_notion_token_post_captured_once(monkeypatch):
    """The vendor's UNSIGNED setup POST fills an empty row secret + ACKs;
    a re-delivery (secret now present) ACKs WITHOUT overwriting."""
    stored = _setup_notion_sub(monkeypatch, row_secret="")
    body = json.dumps({"verification_token": _NOTION_TOKEN}).encode()
    status, resp, _ = _dispatch_notion(body, {})
    assert status == 200 and resp == {"ok": True}
    assert stored == [_NOTION_TOKEN]

    body2 = json.dumps({"verification_token": "tok-second"}).encode()
    status2, resp2, _ = _dispatch_notion(body2, {})
    assert status2 == 200 and resp2 == {"ok": True}
    assert stored == [_NOTION_TOKEN]  # never overwritten


def test_notion_event_verifies_with_captured_secret(monkeypatch):
    """Post-capture: a signed delivery passes the ts-less sha256= scheme and
    reaches the pipeline."""
    _setup_notion_sub(monkeypatch, row_secret=_NOTION_TOKEN)
    body = json.dumps(_notion_event_body()).encode()
    status, resp, _ = _dispatch_notion(body, _notion_sig_headers(body))
    assert status == 200
    assert resp["status"] in ("ok", "no_triggers")


def test_notion_event_before_capture_401(monkeypatch):
    _setup_notion_sub(monkeypatch, row_secret="")
    body = json.dumps(_notion_event_body()).encode()
    status, _resp, _ = _dispatch_notion(body, _notion_sig_headers(body))
    assert status == 401


def test_notion_tampered_event_401(monkeypatch):
    _setup_notion_sub(monkeypatch, row_secret=_NOTION_TOKEN)
    body = json.dumps(_notion_event_body()).encode()
    headers = _notion_sig_headers(body)
    status, _resp, _ = _dispatch_notion(body + b" ", headers)
    assert status == 401


def test_notion_unsigned_non_token_body_401(monkeypatch):
    """Only token-POST-shaped bodies bypass signature verification."""
    _setup_notion_sub(monkeypatch, row_secret=_NOTION_TOKEN)
    body = json.dumps(_notion_event_body()).encode()
    status, _resp, _ = _dispatch_notion(body, {})
    assert status == 401


# --- notion: relay fan-in (workspace-scoped) -------------------------------------

def _setup_notion_relay_env(monkeypatch, rows: list[dict]) -> list[str]:
    webhook_dispatcher.reset_caches()
    manifest = SimpleNamespace(credentials=SimpleNamespace(
        webhooks=_NOTION_BLOCK, oauth=None))
    monkeypatch.setattr(
        webhook_dispatcher.mcp_registry, "get_all_manifests",
        lambda: {"notion-mcp": manifest})
    monkeypatch.setitem(
        webhook_providers._MANIFEST_CACHE, "notion",
        GenericWebhookProvider(provider_id="notion"))
    monkeypatch.setattr(
        credential_store, "get_infra_credentials",
        lambda slug: (
            {relay_client.EVENTS_FORWARD_SECRET_KEY: _FORWARD_SECRET}
            if slug == relay_client.EVENTS_FORWARD_SECRET_SLUG else {}
        ))

    def fake_list(**kw):
        return [
            r for r in rows
            if r.get("vendor_target") == kw.get("vendor_target")
            and r.get("delivery_mode") == kw.get("delivery_mode")
            and r.get("provider_id") == kw.get("provider_id")
        ]

    monkeypatch.setattr(
        webhook_subscription_store, "list_subscriptions", fake_list)
    processed: list[str] = []

    async def fake_process(*, row, provider, webhooks_block, parsed_body,
                           headers_lc):
        processed.append(row["id"])
        return {"status": "ok", "fired": 1, "event_type": "comment.created"}

    monkeypatch.setattr(
        webhook_dispatcher, "_process_subscription_events", fake_process)
    return processed


def test_notion_relay_ingest_fans_in_by_workspace(monkeypatch):
    base = {
        "provider_id": "notion", "mcp_name": "notion-mcp", "status": "active",
        "delivery_mode": "relay", "selected_events": "[]",
    }
    processed = _setup_notion_relay_env(monkeypatch, [
        {**base, "id": "n-relay-1", "vendor_target": "WS1"},
        {**base, "id": "n-other-ws", "vendor_target": "WS2"},
        {**base, "id": "n-vendor-mode", "vendor_target": "WS1",
         "delivery_mode": "vendor"},
    ])
    body = json.dumps(_notion_event_body()).encode()
    status, resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="notion", raw_body=body,
        headers=_forward_headers(body, provider="notion")))
    assert status == 200 and resp["fired"] == 1
    assert processed == ["n-relay-1"]


# --- zoom: relay fan-in keyed on the NESTED body.payload.account_id path --------

_ZOOM_BLOCK = {
    "available": True,
    "provider_id": "zoom",
    "workspace_id_path": "body.payload.account_id",
}


def _zoom_event_body(account_id: str = "ACC1") -> dict:
    return {
        "event": "meeting.started",
        "payload": {"account_id": account_id, "object": {
            "uuid": "mtg-1", "host_id": "zu-alice", "topic": "Standup"}},
    }


def _setup_zoom_relay_env(monkeypatch, rows: list[dict]) -> list[str]:
    webhook_dispatcher.reset_caches()
    manifest = SimpleNamespace(credentials=SimpleNamespace(
        webhooks=_ZOOM_BLOCK, oauth=None))
    monkeypatch.setattr(
        webhook_dispatcher.mcp_registry, "get_all_manifests",
        lambda: {"zoom-mcp": manifest})
    monkeypatch.setitem(
        webhook_providers._MANIFEST_CACHE, "zoom",
        GenericWebhookProvider(provider_id="zoom"))
    monkeypatch.setattr(
        credential_store, "get_infra_credentials",
        lambda slug: (
            {relay_client.EVENTS_FORWARD_SECRET_KEY: _FORWARD_SECRET}
            if slug == relay_client.EVENTS_FORWARD_SECRET_SLUG else {}
        ))

    def fake_list(**kw):
        return [
            r for r in rows
            if r.get("vendor_target") == kw.get("vendor_target")
            and r.get("delivery_mode") == kw.get("delivery_mode")
            and r.get("provider_id") == kw.get("provider_id")
        ]

    monkeypatch.setattr(
        webhook_subscription_store, "list_subscriptions", fake_list)
    processed: list[str] = []

    async def fake_process(*, row, provider, webhooks_block, parsed_body,
                           headers_lc):
        processed.append(row["id"])
        return {"status": "ok", "fired": 1, "event_type": "meeting.started"}

    monkeypatch.setattr(
        webhook_dispatcher, "_process_subscription_events", fake_process)
    return processed


def test_zoom_relay_ingest_fans_in_by_account_id(monkeypatch):
    """Zoom's fan-in key is two levels deep (body.payload.account_id) — confirm
    walk_path extracts it and only the matching relay-mode row processes."""
    base = {
        "provider_id": "zoom", "mcp_name": "zoom-mcp", "status": "active",
        "delivery_mode": "relay", "selected_events": "[]",
    }
    processed = _setup_zoom_relay_env(monkeypatch, [
        {**base, "id": "z-relay-1", "vendor_target": "ACC1"},
        {**base, "id": "z-other-acct", "vendor_target": "ACC2"},
        {**base, "id": "z-vendor-mode", "vendor_target": "ACC1",
         "delivery_mode": "vendor"},
    ])
    body = json.dumps(_zoom_event_body()).encode()
    status, resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="zoom", raw_body=body,
        headers=_forward_headers(body, provider="zoom")))
    assert status == 200 and resp["fired"] == 1
    assert processed == ["z-relay-1"]


def test_zoom_relay_ingest_no_account_id_ignored(monkeypatch):
    processed = _setup_zoom_relay_env(monkeypatch, [
        {"provider_id": "zoom", "mcp_name": "zoom-mcp", "status": "active",
         "delivery_mode": "relay", "selected_events": "[]",
         "id": "z1", "vendor_target": "ACC1"},
    ])
    body = json.dumps({"event": "x", "payload": {"object": {}}}).encode()
    status, resp, _ = asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
        provider_id="zoom", raw_body=body,
        headers=_forward_headers(body, provider="zoom")))
    assert status == 200 and resp.get("reason") == "no_workspace_id"
    assert processed == []


# --- the Zoom handshake cannot sign a firing payload ---------------------------

def test_zoom_handshake_signs_nothing_the_dispatcher_fires(monkeypatch):
    """Through the public route with a Zoom-class manifest: a handshake whose
    plainToken is shaped like a signed payload is not answered, and under a
    template that signs the bare body the only strings the handshake still
    signs are ones the dispatcher never fires (a body that is not a JSON
    object fires nothing)."""
    import time
    import uuid
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.events import webhooks as webhooks_api

    secret = "zoom-secret-token"
    block = {
        "available": True, "provider_id": "zoom",
        "signature": {
            "algorithm": "hmac-sha256", "header": "x-zm-signature", "prefix": "",
            "version_prefix": "v0=", "timestamp_header": "x-zm-request-timestamp",
            "timestamp_format": "unix", "signed_payload_template": "{body}",
            "max_age_seconds": 300, "per_subscription_secret": False,
            "secret_credential_key": "ZOOM_WEBHOOK_SECRET_TOKEN",
        },
        "url_verification": {
            "kind": "zoom_endpoint_validation", "request_field": "plainToken",
            "request_source": "body", "response_field": "encryptedToken",
            "response_content_type": "application/json",
        },
        "event_catalog": [{"key": "meeting.started", "label": "Meeting started"}],
        "payload_normalization": {"event_type_path": "body.event"},
        "event_id_field": "headers.x-zm-request-id",
    }
    sub = str(uuid.uuid4())
    row = {"id": sub, "provider_id": "zoom", "mcp_name": "zoom-mcp", "status": "active",
           "delivery_mode": "vendor", "selected_events": json.dumps(["meeting.started"])}
    manifest = SimpleNamespace(credentials=SimpleNamespace(
        webhooks=block, oauth={"app_credential": "zoom-oauth-app"}))
    monkeypatch.setattr(webhook_subscription_store, "get_subscription",
                        lambda sid: row if sid == sub else None)
    monkeypatch.setattr(webhook_subscription_store, "record_event_received", lambda sid: None)
    monkeypatch.setattr(webhook_subscription_store, "update_subscription_status",
                        lambda *a, **k: None)
    monkeypatch.setattr(webhook_dispatcher.mcp_registry, "get_manifest",
                        lambda name: manifest if name == "zoom-mcp" else None)
    monkeypatch.setattr(credential_store, "get_infra_credentials",
                        lambda slug: {"ZOOM_WEBHOOK_SECRET_TOKEN": secret}
                        if slug == "zoom-oauth-app" else {})
    monkeypatch.setattr(trigger_store, "list_triggers", lambda **kw: [
        {"id": "t-1", "subscription_id": sub, "event_filter": "{}"}])
    fired: list = []

    async def fake_fan_out(*, triggers, body, event, trigger_source):
        fired.append(body)
        return (len(triggers), [])
    monkeypatch.setattr(webhook_dispatcher, "_fan_out_fire", fake_fan_out)

    app = FastAPI()
    app.include_router(webhooks_api.router)
    client = TestClient(app)
    ts = str(int(time.time()))
    event_body = json.dumps({"event": "meeting.started", "payload": {}}, separators=(",", ":"))

    # A payload-shaped token is not answered: the request falls through to
    # signature verification, which refuses it.
    r = client.post(f"/v1/webhooks/zoom/{sub}", json={
        "event": "endpoint.url_validation", "payload": {"plainToken": event_body}})
    assert r.status_code != 200 or "encryptedToken" not in r.text
    # A token inside the alphabet is answered, but what it signs is never a
    # JSON object, so the signed body fires nothing.
    r = client.post(f"/v1/webhooks/zoom/{sub}", json={
        "event": "endpoint.url_validation", "payload": {"plainToken": "1234567890"}})
    assert r.status_code == 200
    sig = r.json()["encryptedToken"]
    r = client.post(f"/v1/webhooks/zoom/{sub}", content="1234567890",
                    headers={"content-type": "application/json",
                             "x-zm-request-timestamp": ts, "x-zm-signature": "v0=" + sig,
                             "x-zm-request-id": "r1"})
    assert fired == []
    assert r.status_code != 200 or r.json().get("fired", 0) == 0
    # A real event with a wrong signature stays refused.
    r = client.post(f"/v1/webhooks/zoom/{sub}", content=event_body,
                    headers={"content-type": "application/json",
                             "x-zm-request-timestamp": ts, "x-zm-signature": "v0=" + sig,
                             "x-zm-request-id": "r2"})
    assert r.status_code == 401 and fired == []


# --- the receive path off the loop ------------------------------------------

import uuid  # noqa: E402

from auth.webhook_providers.base import NormalizedEvent  # noqa: E402

_L5_PROVIDER = "l5vendor"
_L5_SECRET = "l5-signing-secret"
_L5_BLOCK = {
    "available": True, "provider_id": _L5_PROVIDER,
    "signature": {
        "algorithm": "hmac-sha256", "header": "x-l5-signature",
        "version_prefix": "v0=", "timestamp_header": "x-l5-timestamp",
        "timestamp_format": "unix", "signed_payload_template": "v0:{timestamp}:{body}",
        "max_age_seconds": 300, "per_subscription_secret": True,
    },
    "url_verification": {
        "kind": "verification_token_capture", "request_field": "verification_token",
        "response_field": "ok",
    },
    "event_catalog": [{"key": "thing.changed", "label": "Things"}],
    "payload_normalization": {"event_type_path": "body.type"},
    "event_id_field": "body.id",
}


class _BatchProvider(GenericWebhookProvider):
    """One event per ``items[]`` entry (the shape of a batched vendor)."""

    def normalize_payload_batch(self, *, body, headers, manifest_block):
        return [NormalizedEvent(event_type=i.get("type", ""), vendor_event_id=i.get("id", ""))
                for i in (body.get("items") or [body])]


def _l5_sign(body: bytes, secret: str = _L5_SECRET) -> dict:
    ts = str(int(time.time()))
    mac = hmac_mod.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"x-l5-timestamp": ts, "x-l5-signature": "v0=" + mac}


def _l5_manifest(monkeypatch, mcp_name: str = "l5-mcp"):
    manifest = SimpleNamespace(credentials=SimpleNamespace(webhooks=_L5_BLOCK, oauth=None))
    monkeypatch.setattr(webhook_dispatcher.mcp_registry, "get_manifest",
                        lambda name: manifest if name == mcp_name else None)
    monkeypatch.setitem(webhook_providers._MANIFEST_CACHE, _L5_PROVIDER,
                        _BatchProvider(provider_id=_L5_PROVIDER))


def _l5_stub_row(monkeypatch, *, secret: str = _L5_SECRET, status: str = "active") -> str:
    """A stubbed active row with a per-subscription secret; returns its id."""
    webhook_dispatcher.reset_caches()
    _l5_manifest(monkeypatch)
    sid = str(uuid.uuid4())
    row = {"id": sid, "provider_id": _L5_PROVIDER, "mcp_name": "l5-mcp", "status": status,
           "selected_events": "[]", "delivery_mode": "vendor"}
    monkeypatch.setattr(webhook_subscription_store, "get_subscription",
                        lambda s: row if s == sid else None)
    monkeypatch.setattr(webhook_subscription_store, "get_signing_secret", lambda s: secret)
    monkeypatch.setattr(webhook_subscription_store, "record_event_received", lambda s: None)
    return sid


def _l5_dispatch(sid: str, body: bytes, headers: dict):
    return asyncio.run(webhook_dispatcher.dispatch_webhook(
        provider_id=_L5_PROVIDER, subscription_id=sid, raw_body=body,
        headers=headers, query_params={}))


def test_batch_scans_the_subscription_triggers_once(monkeypatch):
    sid = _l5_stub_row(monkeypatch)
    calls: list[dict] = []

    def fake_list(**kw):
        calls.append(kw)
        return [{"id": "t-1", "subscription_id": sid, "event_filter": "{}"}]
    monkeypatch.setattr(trigger_store, "list_triggers", fake_list)
    fired: list[int] = []

    async def fake_fan_out(*, triggers, body, event, trigger_source):
        fired.append(len(triggers))
        return (len(triggers), [])
    monkeypatch.setattr(webhook_dispatcher, "_fan_out_fire", fake_fan_out)

    body = json.dumps({"items": [{"type": "thing.changed", "id": f"e{i}"} for i in range(10)]}).encode()
    status, resp, _ = _l5_dispatch(sid, body, _l5_sign(body))
    assert status == 200 and resp["fired"] == 10 and fired == [1] * 10
    assert len(calls) == 1 and calls[0] == {"enabled_only": True, "subscription_id": sid}
    # A batch of nothing but repeats (a vendor retry) costs no query at all.
    status, resp, _ = _l5_dispatch(sid, body, _l5_sign(body))
    assert status == 200 and resp["status"] == "duplicate"
    assert len(calls) == 1


def test_refused_signature_writes_nothing_and_notes_once_a_minute(monkeypatch):
    sid = _l5_stub_row(monkeypatch)
    monkeypatch.setattr(webhook_subscription_store, "update_subscription_status",
                        lambda *a, **k: pytest.fail("a refused signature ran a status write"))
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(webhook_subscription_store, "note_last_error",
                        lambda s, text: notes.append((s, text)) or True, raising=False)
    body = json.dumps({"type": "thing.changed", "id": "e1"}).encode()

    for _ in range(5):
        status, _resp, _ = _l5_dispatch(sid, body, _l5_sign(body, "wrong"))
        assert status == 401
    status, _resp, _ = _l5_dispatch(sid, body, {})  # no signature header at all
    assert status == 401
    assert notes == [(sid, "signature: signature_mismatch (1 refused)")]

    # Past the window, the next refusal writes the aggregate since the note.
    count, last = webhook_dispatcher._signature_failures[sid]
    webhook_dispatcher._signature_failures[sid] = (count, last - 61)
    status, _resp, _ = _l5_dispatch(sid, body, _l5_sign(body, "wrong"))
    assert status == 401
    assert notes[-1] == (sid, "signature: signature_mismatch (6 refused)")
    assert len(notes) == 2


def test_receive_store_work_leaves_the_loop(temp_db, loop_db_guard, monkeypatch):
    """A real row and a real trigger: a valid event and a refused one are
    dispatched with the loop-thread guard armed; the trigger fires (the fan-out
    itself is stubbed, its reads are other packages') and the counters land."""
    webhook_dispatcher.reset_caches()
    _l5_manifest(monkeypatch)
    row = webhook_subscription_store.create_subscription(
        scope="user", owner="user-admin", agent=None, mcp_name="l5-mcp",
        provider_id=_L5_PROVIDER, account_label="", vendor_target="ws-1",
        selected_events=[], selected_subevents={}, signing_secret=_L5_SECRET,
        created_by="user-admin")
    webhook_subscription_store.update_subscription_status(row["id"], "active")
    trigger_store.create_trigger(slug="l5-t", name="t", scope="user", agent="a1",
                                 created_by="user-admin", subscription_id=row["id"],
                                 event_filter={})
    fired: list[str] = []

    async def fake_fan_out(*, triggers, body, event, trigger_source):
        fired.extend(t["id"] for t in triggers)
        return (len(triggers), [])
    monkeypatch.setattr(webhook_dispatcher, "_fan_out_fire", fake_fan_out)
    body = json.dumps({"type": "thing.changed", "id": "e1"}).encode()

    async def scenario():
        with loop_db_guard.active():
            ok = await webhook_dispatcher.dispatch_webhook(
                provider_id=_L5_PROVIDER, subscription_id=row["id"], raw_body=body,
                headers=_l5_sign(body), query_params={})
            refused = await webhook_dispatcher.dispatch_webhook(
                provider_id=_L5_PROVIDER, subscription_id=row["id"], raw_body=body,
                headers=_l5_sign(body, "wrong"), query_params={})
            unknown = await webhook_dispatcher.dispatch_webhook(
                provider_id=_L5_PROVIDER, subscription_id=str(uuid.uuid4()), raw_body=body,
                headers={}, query_params={})
        after = await asyncio.to_thread(webhook_subscription_store.get_subscription, row["id"])
        return ok, refused, unknown, after

    ok, refused, unknown, after = asyncio.run(scenario())
    assert ok[0] == 200 and ok[1]["fired"] == 1 and len(fired) == 1
    assert refused[0] == 401 and unknown[0] == 404
    assert after["event_count"] == 1
    assert after["last_error"] == "signature: signature_mismatch (1 refused)"
    assert after["status"] == "active"


def test_large_unsigned_body_is_verified_before_it_is_parsed(monkeypatch):
    """Only a body small enough to be a handshake is looked at before the
    signature check; a bigger one carrying the handshake field is refused."""
    sid = _l5_stub_row(monkeypatch, secret="")
    stored: list[str] = []
    monkeypatch.setattr(webhook_subscription_store, "update_signing_secret",
                        lambda s, secret: stored.append(secret))
    big = json.dumps({"verification_token": "tok-1", "pad": "x" * (17 * 1024)}).encode()
    status, _resp, _ = _l5_dispatch(sid, big, {})
    assert status == 401 and stored == []
    small = json.dumps({"verification_token": "tok-1"}).encode()
    status, resp, _ = _l5_dispatch(sid, small, {})
    assert status == 200 and resp == {"ok": True} and stored == ["tok-1"]


def test_preauth_gate_refuses_when_saturated(monkeypatch):
    sid = _l5_stub_row(monkeypatch)
    monkeypatch.setattr(webhook_dispatcher, "_PREAUTH_MAX_WAITING", 0, raising=False)
    body = json.dumps({"type": "thing.changed", "id": "e1"}).encode()
    status, resp, headers = _l5_dispatch(sid, body, _l5_sign(body))
    assert status == 503 and resp == {"error": "busy"}
    assert headers.get("retry-after") == "5"


def test_relay_forward_secret_is_cached_and_rechecked_on_refusal(monkeypatch):
    processed = _setup_relay_env(monkeypatch, _relay_rows())
    reads: list[str] = []
    secret = {"value": _FORWARD_SECRET}
    monkeypatch.setattr(
        credential_store, "get_infra_credentials",
        lambda slug: (reads.append(slug) or {relay_client.EVENTS_FORWARD_SECRET_KEY: secret["value"]})
        if slug == relay_client.EVENTS_FORWARD_SECRET_SLUG else {})
    body = json.dumps({"team_id": "T1", "event": {"type": "reaction_added"}}).encode()

    def forward(headers):
        return asyncio.run(webhook_dispatcher.dispatch_relay_webhook(
            provider_id="slack", raw_body=body, headers=headers))

    assert forward(_forward_headers(body))[0] == 200
    assert forward(_forward_headers(body))[0] == 200
    assert len(reads) == 1 and len(processed) == 4
    # A refusal against a value read moments ago does not re-read.
    assert forward(_forward_headers(body, secret="wrong"))[0] == 401
    assert len(reads) == 1
    # The relay rotated the secret: once the cached value is old enough, a
    # refusal re-reads once and the event signed with the new secret passes.
    secret["value"] = "rotated"
    value, at = webhook_dispatcher._relay_secret
    webhook_dispatcher._relay_secret = (value, at - 6)
    assert forward(_forward_headers(body, secret="rotated"))[0] == 200
    assert len(reads) == 2 and len(processed) == 6


# ── the receive routes before the dispatcher ─────────


def _receive_request(chunks, *, ip="198.51.100.60", delay=0.0, path="/v1/webhooks/relay/slack",
                     headers=(), server=("testserver", 80)):
    from starlette.requests import Request
    pending = list(chunks)

    async def receive():
        if delay:
            await asyncio.sleep(delay)
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    return Request({"type": "http", "method": "POST", "path": path,
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
                    "client": (ip, 1234), "server": server, "scheme": "http",
                    "query_string": b""}, receive)


_NOTHING_IN_FLIGHT = {"reads": 0, "bytes": 0, "small_bytes": 0, "clients": 0}


@pytest.fixture
def _receivers(monkeypatch):
    import config
    from api.events import webhooks
    from auth import lan_check, rate_limiter
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    lan_check.reset_state()
    rate_limiter._attempts.clear()
    calls = []

    async def fake_relay(*, provider_id, raw_body, headers):
        calls.append(len(raw_body))
        return 401, {"error": "forward signature verification failed"}, {"content-type": "application/json"}

    monkeypatch.setattr(webhook_dispatcher, "dispatch_relay_webhook", fake_relay)
    yield webhooks, calls
    rate_limiter._attempts.clear()


def test_a_body_over_the_cap_is_refused_413_and_never_dispatched(_receivers, monkeypatch):
    import config
    webhooks, calls = _receivers
    monkeypatch.setattr(config, "MAX_WEBHOOK_BODY_BYTES", 1024)
    r = asyncio.run(webhooks.receive_relay_webhook("slack", _receive_request([b"x" * 600] * 4)))
    assert r.status_code == 413 and r.headers["connection"] == "close" and calls == []


def test_a_stalled_body_is_refused_408(_receivers, monkeypatch):
    webhooks, calls = _receivers
    monkeypatch.setattr(webhook_body, "_CHUNK_GAP_S", 0.05)
    r = asyncio.run(webhooks.receive_relay_webhook(
        "slack", _receive_request([b"{", b"}"], delay=0.2)))
    assert r.status_code == 408 and calls == []


def test_the_read_bounds_answer_503_before_reading(_receivers, monkeypatch):
    webhooks, calls = _receivers
    monkeypatch.setattr(webhook_body, "_READS_PER_CLIENT", 1)

    async def scenario():
        slow = asyncio.create_task(webhooks.receive_relay_webhook(
            "slack", _receive_request([b"{", b"}"], delay=0.1)))
        await asyncio.sleep(0.02)
        second = await webhooks.receive_relay_webhook("slack", _receive_request([b"{}"]))
        other = await webhooks.receive_relay_webhook("slack", _receive_request([b"{}"], ip="198.51.100.61"))
        return second, other, await slow

    second, other, first = asyncio.run(scenario())
    assert second.status_code == 503 and second.headers["retry-after"] == "5"
    assert other.status_code == 401 and first.status_code == 401


def test_the_read_bounds_cover_the_body_read_only(_receivers, monkeypatch):
    """A burst of deliveries from one address (the relay forwards every
    vendor from one) is refused only while its bodies are being read: a
    slow dispatch holds no read slot."""
    webhooks, _calls = _receivers
    in_dispatch = {"now": 0, "most": 0}

    async def slow_relay(*, provider_id, raw_body, headers):
        in_dispatch["now"] += 1
        in_dispatch["most"] = max(in_dispatch["most"], in_dispatch["now"])
        await asyncio.sleep(0.2)
        in_dispatch["now"] -= 1
        return 200, {"status": "ok"}, {"content-type": "application/json"}

    monkeypatch.setattr(webhook_dispatcher, "dispatch_relay_webhook", slow_relay)

    async def scenario():
        deliveries = []
        for _ in range(3 * webhook_body._READS_PER_CLIENT):
            deliveries.append(asyncio.create_task(
                webhooks.receive_relay_webhook("slack", _receive_request([b"{}"]))))
            await asyncio.sleep(0.01)
        return [r.status_code for r in await asyncio.gather(*deliveries)]

    codes = asyncio.run(scenario())
    assert codes == [200] * (3 * webhook_body._READS_PER_CLIENT)
    assert in_dispatch["most"] > webhook_body._READS_PER_CLIENT
    assert webhook_body.in_flight() == _NOTHING_IN_FLIGHT


def test_a_refused_read_gives_its_slot_back(_receivers, monkeypatch):
    webhooks, _calls = _receivers
    monkeypatch.setattr(webhook_body, "_CHUNK_GAP_S", 0.05)
    r = asyncio.run(webhooks.receive_relay_webhook(
        "slack", _receive_request([b"{", b"}"], delay=0.2)))
    assert r.status_code == 408
    assert webhook_body.in_flight() == _NOTHING_IN_FLIGHT


def test_only_preauth_refusals_count_against_a_distinct_address(_receivers, monkeypatch):
    import config
    webhooks, calls = _receivers
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "webhook_receive_ip",
                        {"max": 3, "window": 60, "base_block": 60, "max_block": 600})
    codes = [asyncio.run(webhooks.receive_relay_webhook("slack", _receive_request([b"{}"]))).status_code
             for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]

    async def gone(*, provider_id, subscription_id, raw_body, headers, query_params, http_method):
        return 404, {"error": "unknown subscription"}, {"content-type": "application/json"}

    monkeypatch.setattr(webhook_dispatcher, "dispatch_webhook", gone)
    codes = [asyncio.run(webhooks.receive_webhook(
        "github", str(uuid.uuid4()), _receive_request([b"{}"], ip="198.51.100.62",
                                                      path="/v1/webhooks/github/x"))).status_code
             for _ in range(5)]
    assert codes == [404] * 5


@pytest.mark.parametrize("shared_by", ["hop_without_xff", "gateway", "internal_listener"])
def test_no_per_address_throttle_for_a_shared_address(_receivers, monkeypatch, shared_by):
    """An address every client shares (a trusted proxy that sends no
    X-Forwarded-For, the container's gateway, the internal listener) is
    never counted, and nothing lands on the loopback's key."""
    import config
    from auth import lan_check, rate_limiter
    webhooks, calls = _receivers
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "webhook_receive_ip",
                        {"max": 2, "window": 60, "base_block": 60, "max_block": 600})
    kw: dict = {"ip": "10.200.0.1"}
    if shared_by == "hop_without_xff":
        monkeypatch.setattr(config, "TRUSTED_PROXIES", ["10.200.0.1"])
        kw["headers"] = [("X-Forwarded-Proto", "https")]
    elif shared_by == "gateway":
        monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
        monkeypatch.setattr(lan_check, "_docker_gateway", lambda: "10.200.0.1")
        kw["headers"] = [("X-Forwarded-For", "203.0.113.9")]
    else:
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 45123)
        kw = {"ip": "127.0.0.1", "server": ("127.0.0.1", 45123)}
    codes = [asyncio.run(webhooks.receive_relay_webhook(
        "slack", _receive_request([b"{}"], **kw))).status_code for _ in range(5)]
    assert codes == [401] * 5
    assert not [k for k in rate_limiter._attempts if k[0] == "webhook_receive_ip"]


def test_an_untrusted_forwarder_is_throttled_as_one_client(_receivers, monkeypatch):
    """f8: a private peer that is not a trusted proxy and sends a forwarding
    header is counted under its own address, like a request without one."""
    import config
    from auth import rate_limiter
    webhooks, calls = _receivers
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "webhook_receive_ip",
                        {"max": 2, "window": 60, "base_block": 60, "max_block": 600})
    codes = [asyncio.run(webhooks.receive_relay_webhook("slack", _receive_request(
        [b"{}"], ip="10.200.0.1", headers=[("X-Real-IP", "1.2.3.4")]))).status_code
        for _ in range(4)]
    assert codes == [401, 401, 429, 429]
    keys = {k[1] for k in rate_limiter._attempts if k[0] == "webhook_receive_ip"}
    assert keys == {"ip:10.200.0.1"}
    # Without the header the same address is the same bucket.
    r = asyncio.run(webhooks.receive_relay_webhook(
        "slack", _receive_request([b"{}"], ip="10.200.0.1")))
    assert r.status_code == 429


def test_an_untrusted_forwarder_meets_the_per_client_read_bound(_receivers, monkeypatch):
    webhooks, calls = _receivers
    monkeypatch.setattr(webhook_body, "_READS_PER_CLIENT", 1)
    forged = {"ip": "10.200.0.1", "headers": [("X-Forwarded-For", "203.0.113.9")]}

    async def scenario():
        slow = asyncio.create_task(webhooks.receive_relay_webhook(
            "slack", _receive_request([b"{", b"}"], delay=0.1, **forged)))
        await asyncio.sleep(0.02)
        assert webhook_body.in_flight()["clients"] == 1
        second = await webhooks.receive_relay_webhook(
            "slack", _receive_request([b"{}"], ip="10.200.0.1"))
        return second, await slow

    second, first = asyncio.run(scenario())
    assert second.status_code == 503 and first.status_code == 401
    assert webhook_body.in_flight() == _NOTHING_IN_FLIGHT


def test_the_dispatchers_503_passes_through(_receivers, monkeypatch):
    webhooks, _calls = _receivers

    async def busy(*, provider_id, raw_body, headers):
        return 503, {"error": "busy"}, {"content-type": "application/json", "retry-after": "5"}

    monkeypatch.setattr(webhook_dispatcher, "dispatch_relay_webhook", busy)
    r = asyncio.run(webhooks.receive_relay_webhook("slack", _receive_request([b"{}"])))
    assert r.status_code == 503 and r.headers["retry-after"] == "5"


def test_a_large_body_signature_check_runs_off_the_loop():
    """The HMAC over a body above 256 KB takes a worker thread; a small
    one stays on the loop, where the hop would cost more than the digest."""
    import threading

    seen: list[int] = []

    class _Provider:
        def verify_signature(self, *, raw_body: bytes, **_kw):
            seen.append(threading.get_ident())
            return "ok"

    async def scenario():
        loop_thread = threading.get_ident()
        small = await webhook_dispatcher._verified(_Provider(), raw_body=b"x" * 64, headers={})
        large = await webhook_dispatcher._verified(
            _Provider(), raw_body=b"x" * (webhook_dispatcher._VERIFY_OFF_LOOP_BYTES + 1), headers={})
        return loop_thread, small, large

    loop_thread, small, large = asyncio.run(scenario())
    assert (small, large) == ("ok", "ok")
    assert seen[0] == loop_thread and seen[1] != loop_thread


# --- the webhook caps: a checked sender may send more -------------------------


class _Webhooks:
    signature = {"algorithm": "hmac-sha256", "header": "x-hub-signature-256"}


def _vendor(monkeypatch, *, provider="github", signature=True, early=None):
    """The vendor route with its subscription context and dispatch faked;
    returns (webhooks module, dispatched body sizes, refusal notes)."""
    from api.events import webhooks
    dispatched, notes = [], []
    block = {"signature": _Webhooks.signature} if signature else {}
    ctx = webhook_dispatcher.ReceiveContext(
        row={"id": "sub-1", "provider_id": provider, "status": "active"},
        manifest=type("M", (), {"credentials": type("C", (), {"webhooks": block})()})(),
        signing_secret="s")

    async def load(provider_id, subscription_id):
        return (None, early) if early else (ctx, None)

    async def dispatch(**kw):
        assert kw["context"] is ctx
        dispatched.append(len(kw["raw_body"]))
        return 200, {"status": "ok"}, {"content-type": "application/json"}

    async def note(row, cap):
        notes.append(cap)

    monkeypatch.setattr(webhook_dispatcher, "load_receive_context", load)
    monkeypatch.setattr(webhook_dispatcher, "dispatch_webhook", dispatch)
    monkeypatch.setattr(webhook_dispatcher, "note_body_refusal", note)
    return webhooks, dispatched, notes


def _vendor_post(webhooks, chunks, *, declared=None, provider="github"):
    req = _receive_request(chunks, path=f"/v1/webhooks/{provider}/sub-1")
    if declared is not None:
        req.scope["headers"] = [(b"content-length", str(declared).encode())]
    return asyncio.run(webhooks.receive_webhook(provider, "sub-1", req)), req


def test_a_disconnect_is_noted_only_when_the_middleware_cut_the_body(_receivers, monkeypatch):
    """A sender that goes away is no oversized body; the middleware's cut
    (a chunked body past the outer cap) is one, and the subscription says so."""
    from starlette.requests import Request
    webhooks, _, notes = _vendor(monkeypatch)

    def gone(cut):
        async def receive():
            return {"type": "http.disconnect"}
        scope = {"type": "http", "method": "POST", "path": "/v1/webhooks/github/sub-1",
                 "headers": [], "client": ("198.51.100.70", 1), "server": ("testserver", 80),
                 "scheme": "http", "query_string": b""}
        if cut:
            scope["otodock.body_cut"] = True
        return Request(scope, receive)

    assert asyncio.run(webhooks.receive_webhook("github", "sub-1", gone(False))).status_code == 413
    assert notes == []
    assert asyncio.run(webhooks.receive_webhook("github", "sub-1", gone(True))).status_code == 413
    assert len(notes) == 1


def test_a_signed_github_delivery_may_use_the_larger_cap(_receivers, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_WEBHOOK_BODY_BYTES", 1024)
    monkeypatch.setattr(config, "MAX_WEBHOOK_SIGNED_BODY_BYTES", 8192)
    webhooks, dispatched, _ = _vendor(monkeypatch)
    r, req = _vendor_post(webhooks, [b"x" * 3000])
    assert r.status_code == 200 and dispatched == [3000]
    assert req.scope[webhook_body.SCOPE_KEY] == 8192


def test_an_unsigned_provider_keeps_the_unknown_sender_cap(_receivers, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_WEBHOOK_BODY_BYTES", 1024)
    monkeypatch.setattr(config, "MAX_WEBHOOK_SIGNED_BODY_BYTES", 8192)
    webhooks, dispatched, notes = _vendor(monkeypatch, provider="slack")
    r, req = _vendor_post(webhooks, [b"x" * 3000], provider="slack")
    assert r.status_code == 413 and dispatched == [] and notes == [1024]
    assert webhook_body.SCOPE_KEY not in req.scope


def test_a_declared_length_over_the_cap_is_refused_before_reading(_receivers, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_WEBHOOK_SIGNED_BODY_BYTES", 8192)
    monkeypatch.setattr(config, "MAX_WEBHOOK_BODY_BYTES", 1024)
    webhooks, dispatched, notes = _vendor(monkeypatch)
    r, _ = _vendor_post(webhooks, [b"x" * 10], declared=30_000)
    assert r.status_code == 413 and dispatched == [] and notes == [8192]
    assert webhook_body.in_flight() == _NOTHING_IN_FLIGHT


def test_an_unknown_subscription_is_answered_without_a_large_read(_receivers, monkeypatch):
    webhooks, dispatched, _ = _vendor(
        monkeypatch, early=(404, {"error": "subscription not found"}, {}))
    r, _ = _vendor_post(webhooks, [b"{}"])
    assert r.status_code == 404 and dispatched == []


def test_the_bytes_in_flight_are_bounded(_receivers, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_WEBHOOK_INFLIGHT_BYTES", 256 * 1024)
    monkeypatch.setattr(config, "MAX_WEBHOOK_INFLIGHT_BYTES_PER_CLIENT", 0)
    webhooks, calls = _receivers

    def declared(req):
        req.scope["headers"] = [(b"content-length", b"2")]
        return req

    async def scenario():
        # Each slow read reserves the 64 KB floor; the small tier holds half.
        slow = [asyncio.create_task(webhooks.receive_relay_webhook(
            "slack", declared(_receive_request([b"{", b"}"], delay=0.1, ip=f"198.51.100.{70 + i}"))))
            for i in range(2)]
        await asyncio.sleep(0.02)
        third = await webhooks.receive_relay_webhook(
            "slack", declared(_receive_request([b"{}"], ip="198.51.100.90")))
        return third, await asyncio.gather(*slow)

    third, firsts = asyncio.run(scenario())
    assert third.status_code == 503
    assert [r.status_code for r in firsts] == [401, 401]
    assert webhook_body.in_flight() == _NOTHING_IN_FLIGHT


def test_a_slow_large_read_gets_a_longer_deadline():
    assert webhook_body._BODY_S <= 25 * 1024 * 1024 / webhook_body._MIN_RATE <= webhook_body._BODY_MAX_S


def test_a_large_body_is_parsed_off_the_loop(monkeypatch):
    threads = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        threads.append(fn.__name__)
        return await real(fn, *a, **k)

    monkeypatch.setattr(webhook_dispatcher.asyncio, "to_thread", spy)
    big = json.dumps({"x": "y" * (2 * 1024 * 1024)}).encode()
    assert asyncio.run(webhook_dispatcher._parse_body(big))["x"].startswith("yyy")
    assert asyncio.run(webhook_dispatcher._parse_body(b'{"a": 1}')) == {"a": 1}
    assert threads == ["_safe_parse_json"]


def test_a_refused_body_is_noted_on_the_subscription(monkeypatch):
    notes = []

    async def fake_note(sid, text):
        notes.append((sid, text))

    monkeypatch.setattr(webhook_dispatcher, "run_db",
                        lambda fn, *a: fake_note(*a))
    webhook_dispatcher.reset_caches()
    asyncio.run(webhook_dispatcher.note_body_refusal(
        {"id": "sub-9", "status": "active"}, 25 * 1024 * 1024))
    assert notes == [("sub-9", "body over 25 MB refused (1 refused)")]


def test_a_body_with_no_declared_length_reserves_its_cap(_receivers, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_WEBHOOK_INFLIGHT_BYTES", 2 * 1024 * 1024)
    webhooks, _calls = _receivers
    r = asyncio.run(webhooks.receive_relay_webhook("slack", _receive_request([b"{}"])))
    assert r.status_code == 503  # 2 MB reserved > the small tier's 1 MB half
