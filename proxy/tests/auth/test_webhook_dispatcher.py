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

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth import webhook_providers  # noqa: E402
from auth.webhook_providers.generic import GenericWebhookProvider  # noqa: E402
from services.webhooks import webhook_dispatcher  # noqa: E402
from services.webhooks.event_normalizer import resolve_catalog_keys  # noqa: E402
from storage import credential_store, trigger_store, webhook_subscription_store  # noqa: E402

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
        trigger_store, "list_triggers", lambda enabled_only=True: triggers or [])

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
        trigger_store, "list_triggers", lambda enabled_only=True: [])
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
