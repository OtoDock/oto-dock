"""Relay registration mode (hosted event delivery) — effective-mode
resolution, the register lifecycle (mint/rotate forward secret), and
delete behavior. Pure unit tests: stores + relay_client monkeypatched.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config  # noqa: E402
from services.billing import relay_client  # noqa: E402
from services.webhooks import subscription_manager  # noqa: E402
from storage import credential_store, webhook_subscription_store  # noqa: E402

_WB = {
    "available": True,
    "provider_id": "slack",
    "registration": {"mode": "manual", "manual_instructions_url": "https://x"},
    "workspace_id_path": "body.team_id",
}


def _mode(monkeypatch, *, block=None, relay_up=True, extra=None,
          extra_raises=False) -> str:
    monkeypatch.setattr(relay_client, "is_available", lambda: relay_up)

    def fake_extra(**kw):
        if extra_raises:
            raise RuntimeError("no account")
        return extra or {}

    monkeypatch.setattr(subscription_manager, "_resolve_account_extra", fake_extra)
    return subscription_manager._effective_registration_mode(
        webhooks_block=block if block is not None else dict(_WB),
        scope="user", owner="alice", agent=None, mcp_name="m",
        account_label="a@x.com",
    )


# --- effective-mode matrix -------------------------------------------------------

def test_effective_mode_relay_when_all_conditions(monkeypatch):
    assert _mode(monkeypatch, extra={"via_relay": True}) == "relay"


def test_effective_mode_requires_workspace_id_path(monkeypatch):
    wb = dict(_WB)
    wb.pop("workspace_id_path")
    assert _mode(monkeypatch, block=wb, extra={"via_relay": True}) == "manual"


def test_effective_mode_requires_relay_available(monkeypatch):
    assert _mode(monkeypatch, relay_up=False, extra={"via_relay": True}) == "manual"


def test_effective_mode_requires_relay_exchanged_account(monkeypatch):
    """Self-managed accounts have no routing binding on the relay — they
    keep the manifest's own (manual/auto) mode."""
    assert _mode(monkeypatch, extra={"team_id": "T1"}) == "manual"


def test_effective_mode_account_lookup_failure_falls_back(monkeypatch):
    assert _mode(monkeypatch, extra_raises=True) == "manual"


def test_resolve_effective_registration_mode_unknown_mcp(monkeypatch):
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda name: None)
    assert subscription_manager.resolve_effective_registration_mode(
        mcp_name="nope", scope="user", owner="u", account_label="a",
    ) == "manual"


# --- register lifecycle ----------------------------------------------------------

def test_relay_register_requires_public_url(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    with pytest.raises(subscription_manager.SubscriptionError):
        asyncio.run(
            subscription_manager._relay_register_events(provider_id="slack"))


def test_relay_register_mints_via_rotate_when_no_local_secret(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://inst.example/")
    monkeypatch.setattr(credential_store, "get_infra_credentials", lambda slug: {})
    saved: dict = {}
    monkeypatch.setattr(
        credential_store, "set_infra_credentials",
        lambda slug, creds: saved.update({slug: creds}))
    calls: dict = {}

    async def fake_register(**kw):
        calls.update(kw)
        return {"ok": True, "enabled": True, "forward_secret": "fs-new"}

    monkeypatch.setattr(relay_client, "events_register", fake_register)
    asyncio.run(subscription_manager._relay_register_events(provider_id="slack"))
    assert calls["rotate_secret"] is True
    assert calls["events_url"] == "https://inst.example/v1/webhooks/relay/slack"
    assert saved[relay_client.EVENTS_FORWARD_SECRET_SLUG] == {
        relay_client.EVENTS_FORWARD_SECRET_KEY: "fs-new"}


def test_relay_register_keeps_existing_local_secret(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://inst.example")
    monkeypatch.setattr(
        credential_store, "get_infra_credentials",
        lambda slug: {relay_client.EVENTS_FORWARD_SECRET_KEY: "fs-old"})
    saved: dict = {}
    monkeypatch.setattr(
        credential_store, "set_infra_credentials",
        lambda slug, creds: saved.update({slug: creds}))
    calls: dict = {}

    async def fake_register(**kw):
        calls.update(kw)
        return {"ok": True, "enabled": True, "forward_secret": None}

    monkeypatch.setattr(relay_client, "events_register", fake_register)
    asyncio.run(subscription_manager._relay_register_events(provider_id="slack"))
    assert calls["rotate_secret"] is False
    assert saved == {}  # nothing rewritten


# --- delete behavior -------------------------------------------------------------

def test_delete_relay_row_skips_vendor_and_disables_when_last(monkeypatch):
    row = {"id": "s1", "delivery_mode": "relay", "provider_id": "slack"}
    monkeypatch.setattr(
        webhook_subscription_store, "get_subscription", lambda sid: row)
    monkeypatch.setattr(
        webhook_subscription_store, "delete_subscription", lambda sid: True)
    monkeypatch.setattr(
        webhook_subscription_store, "list_subscriptions", lambda **kw: [])
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://inst.example")

    async def fail_vendor_delete(r):
        raise AssertionError("vendor delete must not run for relay rows")

    monkeypatch.setattr(subscription_manager, "_vendor_delete", fail_vendor_delete)
    reg: dict = {}

    async def fake_register(**kw):
        reg.update(kw)
        return {"ok": True, "enabled": False}

    monkeypatch.setattr(relay_client, "events_register", fake_register)
    ok = asyncio.run(subscription_manager.delete_subscription(
        subscription_id="s1"))
    assert ok is True
    assert reg["enabled"] is False and reg["provider_id"] == "slack"


def test_delete_relay_row_keeps_forwarding_when_others_remain(monkeypatch):
    row = {"id": "s1", "delivery_mode": "relay", "provider_id": "slack"}
    monkeypatch.setattr(
        webhook_subscription_store, "get_subscription", lambda sid: row)
    monkeypatch.setattr(
        webhook_subscription_store, "delete_subscription", lambda sid: True)
    monkeypatch.setattr(
        webhook_subscription_store, "list_subscriptions",
        lambda **kw: [{"id": "s2", "delivery_mode": "relay"}])

    async def fail_register(**kw):
        raise AssertionError("must not disable while relay rows remain")

    monkeypatch.setattr(relay_client, "events_register", fail_register)
    assert asyncio.run(subscription_manager.delete_subscription(
        subscription_id="s1")) is True


def test_vendor_delete_noop_for_relay_rows(monkeypatch):
    def boom(name):
        raise AssertionError("manifest lookup must not run for relay rows")

    monkeypatch.setattr(subscription_manager.mcp_registry, "get_manifest", boom)
    asyncio.run(subscription_manager._vendor_delete(
        {"delivery_mode": "relay", "mcp_name": "slack-mcp"}))


# --- per-event gating: admin_only / delivery: bot --------------------------------

from types import SimpleNamespace  # noqa: E402

_GATED_CATALOG = [
    {"key": "reaction_added", "label": "Reactions"},
    {"key": "app_mention", "label": "Bot mentions",
     "delivery": "bot", "admin_only": True},
    {"key": "channel_created", "label": "Channels created", "admin_only": True},
]


def _gated_manifest():
    return SimpleNamespace(credentials=SimpleNamespace(
        webhooks={
            "available": True, "provider_id": "slack",
            "registration": {"mode": "manual",
                             "manual_instructions_url": "https://x"},
            "event_catalog": _GATED_CATALOG,
        },
        oauth=None,
    ))


def _create(monkeypatch, *, selected, scope="service", admin=True, extra=None):
    monkeypatch.setattr(
        subscription_manager.mcp_registry, "get_manifest",
        lambda n: _gated_manifest())
    monkeypatch.setattr(
        subscription_manager, "_read_granted_scopes", lambda **kw: None)
    monkeypatch.setattr(
        subscription_manager, "_resolve_account_extra", lambda **kw: extra or {})
    monkeypatch.setattr(
        subscription_manager, "_effective_registration_mode",
        lambda **kw: "manual")
    # Service-scope create requires a resolvable agent binding (no platform
    # fallback) — mock pick_account so the binding check passes.
    from services.oauth import credential_resolver
    monkeypatch.setattr(
        credential_resolver, "pick_account",
        lambda *a, **kw: SimpleNamespace(label="a@x.com", owner_sub="owner-sub"))
    monkeypatch.setattr(
        webhook_subscription_store, "create_subscription",
        lambda **kw: {"id": "row-1", **kw})
    monkeypatch.setattr(
        webhook_subscription_store, "update_subscription_status",
        lambda *a, **kw: None)
    monkeypatch.setattr(
        webhook_subscription_store, "get_subscription", lambda sid: {"id": sid})
    return asyncio.run(subscription_manager.create_subscription(
        user_sub="alice", scope=scope,
        agent="agentx" if scope == "service" else None,
        mcp_name="slack-mcp", account_label="a@x.com", vendor_target="T1",
        selected_events=selected, caller_is_admin=admin,
    ))


def test_admin_only_event_allowed_for_admin_service_scope(monkeypatch):
    row = _create(monkeypatch, selected=["channel_created"],
                  scope="service", admin=True)
    assert row["id"] == "row-1"


def test_admin_only_event_rejected_for_non_admin(monkeypatch):
    with pytest.raises(subscription_manager.SubscriptionPermissionError):
        _create(monkeypatch, selected=["channel_created"],
                scope="service", admin=False)


def test_admin_only_event_rejected_for_user_scope(monkeypatch):
    """Even an admin can't take a workspace-wide stream onto a PERSONAL
    subscription — admin_only events are service-scope only."""
    with pytest.raises(subscription_manager.SubscriptionPermissionError):
        _create(monkeypatch, selected=["channel_created"],
                scope="user", admin=True)


def test_bot_delivery_requires_bot_install_credential(monkeypatch):
    with pytest.raises(subscription_manager.SubscriptionError) as ei:
        _create(monkeypatch, selected=["app_mention"], scope="service",
                admin=True, extra={"team_id": "T1"})
    assert "bot-install" in str(ei.value)


def test_bot_delivery_passes_with_bot_token_kind(monkeypatch):
    row = _create(monkeypatch, selected=["app_mention"], scope="service",
                  admin=True, extra={"token_kind": "bot"})
    assert row["id"] == "row-1"


def test_plain_event_unaffected_by_gating(monkeypatch):
    row = _create(monkeypatch, selected=["reaction_added"],
                  scope="user", admin=False)
    assert row["id"] == "row-1"


# --- token-capture vendors (notion): the per-sub secret mint is skipped -----------

def _notion_manifest(uv_kind: str = "verification_token_capture"):
    return SimpleNamespace(credentials=SimpleNamespace(
        webhooks={
            "available": True, "provider_id": "notion",
            "signature": {
                "algorithm": "hmac-sha256", "header": "X-Notion-Signature",
                "prefix": "sha256=", "per_subscription_secret": True,
            },
            "url_verification": {
                "kind": uv_kind, "request_field": "verification_token",
                "request_source": "body", "response_field": "ok",
                "response_content_type": "application/json",
            },
            "registration": {"mode": "manual",
                             "manual_instructions_url": "https://x"},
            "event_catalog": [{"key": "comment.created", "label": "Comments"}],
            "payload_normalization": {"event_type_path": "body.type"},
            "event_id_field": "body.id",
            "workspace_id_path": "body.workspace_id",
        },
        oauth=None,
    ))


def _create_notion(monkeypatch, *, uv_kind="verification_token_capture",
                   mode="manual"):
    created: dict = {}
    monkeypatch.setattr(
        subscription_manager.mcp_registry, "get_manifest",
        lambda n: _notion_manifest(uv_kind))
    monkeypatch.setattr(
        subscription_manager, "_read_granted_scopes", lambda **kw: None)
    monkeypatch.setattr(
        subscription_manager, "_resolve_account_extra",
        lambda **kw: {"workspace_id": "WS1", "via_relay": True})
    monkeypatch.setattr(
        subscription_manager, "_effective_registration_mode",
        lambda **kw: mode)
    if mode == "relay":
        async def fake_register(**kw):
            return None
        monkeypatch.setattr(
            subscription_manager, "_relay_register_events", fake_register)

    def fake_create(**kw):
        created.update(kw)
        return {"id": "row-n", **kw}

    monkeypatch.setattr(
        webhook_subscription_store, "create_subscription", fake_create)
    monkeypatch.setattr(
        webhook_subscription_store, "update_subscription_status",
        lambda *a, **kw: None)
    asyncio.run(subscription_manager.create_subscription(
        user_sub="alice", scope="user", agent=None,
        mcp_name="notion-mcp", account_label="a@x.com", vendor_target="WS1",
        selected_events=["comment.created"],
    ))
    return created


def test_capture_kind_vendor_row_starts_with_empty_secret(monkeypatch):
    """Notion dictates the signing secret (verification_token) — the row must
    start EMPTY so the dispatcher capture can fill it."""
    created = _create_notion(monkeypatch, mode="manual")
    assert created["signing_secret"] == ""
    assert created["delivery_mode"] == "vendor"


def test_per_sub_secret_still_minted_without_capture_kind(monkeypatch):
    created = _create_notion(monkeypatch, uv_kind="none", mode="manual")
    assert created["signing_secret"]  # minted as before
    assert len(created["signing_secret"]) > 20


def test_capture_kind_relay_row_keeps_empty_secret(monkeypatch):
    created = _create_notion(monkeypatch, mode="relay")
    assert created["signing_secret"] == ""
    assert created["delivery_mode"] == "relay"


def test_effective_mode_relay_for_notion_shaped_block(monkeypatch):
    """The notion manifest qualifies for relay mode exactly like slack:
    workspace_id_path + relay up + via_relay account."""
    wb = _notion_manifest().credentials.webhooks
    assert _mode(monkeypatch, block=wb, extra={
        "workspace_id": "WS1", "via_relay": True}) == "relay"
    assert _mode(monkeypatch, block=wb, extra={
        "workspace_id": "WS1"}) == "manual"


# ---------------------------------------------------------------------------
# Create-path expiry substitution (MS Graph expirationDateTime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_create_threads_expiry_into_substitutions(monkeypatch):
    """The create path must provide ${expires_at_iso8601} exactly like the
    renew path does — missing tokens render as "" and MS Graph rejects an
    empty expirationDateTime with InvalidRequest. (m365 is the first
    auto-registration manifest with a lifetime, which is why this never
    surfaced for github/linear.)"""
    captured: dict = {}

    async def fake_call_vendor(*, call_block, row, access_token,
                               extra_subs, account_extra=None,
                               body_overrides=None):
        captured.update(extra_subs)
        return {"id": "vendor-sub-1"}

    monkeypatch.setattr(subscription_manager, "_call_vendor", fake_call_vendor)
    monkeypatch.setattr(
        subscription_manager, "_resolve_token_or_raise", lambda **kw: "tok",
    )
    monkeypatch.setattr(
        subscription_manager, "_resolve_account_extra", lambda **kw: {},
    )
    wb = {
        "registration": {
            "create": {
                "method": "POST",
                "url_template": "https://vendor.example/subscriptions",
                "response_id_path": "id",
                "expected_status": [201],
            },
            "lifetime_seconds": 86400,
        },
    }
    vendor_id = await subscription_manager._vendor_create(
        row={"id": "sub-1", "provider_id": "microsoft", "mcp_name": "m365-mcp",
             "account_label": "a@b", "vendor_target": "me/events"},
        webhooks_block=wb,
        signing_secret="sec",
        selected_events=["calendar_events"],
        selected_subevents={},
        scope="user",
        owner="u1",
        agent=None,
        expires_at_iso8601="2026-06-13T19:00:00Z",
    )
    assert vendor_id == "vendor-sub-1"
    assert captured["expires_at_iso8601"] == "2026-06-13T19:00:00Z"
    assert captured["subscription.signing_secret"] == "sec"


@pytest.mark.asyncio
async def test_vendor_create_applies_event_body_overrides(monkeypatch):
    """Catalog entries can override top-level create-body fields for their
    event (`vendor_create_fields`) — MS Graph driveItem subscriptions accept
    ONLY changeType="updated" while the shared body_template sends
    "created,updated". Events without overrides leave the body alone."""
    captured: dict = {}

    async def fake_call_vendor(*, call_block, row, access_token,
                               extra_subs, account_extra=None,
                               body_overrides=None):
        captured["overrides"] = dict(body_overrides or {})
        return {"id": "vendor-sub-2"}

    monkeypatch.setattr(subscription_manager, "_call_vendor", fake_call_vendor)
    monkeypatch.setattr(
        subscription_manager, "_resolve_token_or_raise", lambda **kw: "tok",
    )
    monkeypatch.setattr(
        subscription_manager, "_resolve_account_extra", lambda **kw: {},
    )
    wb = {
        "registration": {
            "create": {
                "method": "POST",
                "url_template": "https://vendor.example/subscriptions",
                "response_id_path": "id",
                "expected_status": [201],
            },
        },
        "event_catalog": [
            {"key": "calendar_events", "label": "Cal"},
            {"key": "drive_root", "label": "Drive",
             "vendor_create_fields": {"changeType": "updated"}},
        ],
    }
    common = dict(
        webhooks_block=wb, signing_secret="sec", selected_subevents={},
        scope="user", owner="u1", agent=None, expires_at_iso8601="2026-06-15T00:00:00Z",
    )
    await subscription_manager._vendor_create(
        row={"id": "s1", "provider_id": "microsoft", "mcp_name": "m365-mcp",
             "account_label": "a@b", "vendor_target": "me/drive/root"},
        selected_events=["drive_root"], **common,
    )
    assert captured["overrides"] == {"changeType": "updated"}

    await subscription_manager._vendor_create(
        row={"id": "s2", "provider_id": "microsoft", "mcp_name": "m365-mcp",
             "account_label": "a@b", "vendor_target": "me/events"},
        selected_events=["calendar_events"], **common,
    )
    assert captured["overrides"] == {}
