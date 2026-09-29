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
from storage.identity import credential_store # noqa: E402
from storage.automation import webhook_subscription_store # noqa: E402

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
    assert ok == (True, True)
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
        subscription_id="s1")) == (True, True)


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


# --- service scope: the request's label must be the binding's ---------------------

_GH = {
    "available": True,
    "provider_id": "github",
    "event_catalog": [{"key": "push", "label": "Push"}],
    "registration": {"mode": "manual"},
}


def _service_create(monkeypatch, *, account_label: str):
    from types import SimpleNamespace
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    monkeypatch.setattr(
        mcp_registry, "get_manifest",
        lambda name: SimpleNamespace(credentials=SimpleNamespace(webhooks=dict(_GH))))
    monkeypatch.setattr(
        credential_resolver, "pick_account",
        lambda mcp, agent, **kw: credential_resolver.AccountRef(
            label="bound", owner_sub="local:owner"))
    return asyncio.run(subscription_manager.create_subscription(
        user_sub="local:co-manager", scope="service", agent="ag", mcp_name="github-mcp",
        account_label=account_label, vendor_target="o/r", selected_events=["nope"],
    ))


def test_service_scope_refuses_a_label_other_than_the_bindings(monkeypatch):
    with pytest.raises(subscription_manager.SubscriptionError) as ei:
        _service_create(monkeypatch, account_label="other")
    assert ei.value.status == 400
    assert ei.value.detail == {"bound_account_label": "bound"}
    assert "'bound'" in str(ei.value)


def test_service_scope_bound_label_passes_the_check(monkeypatch):
    # The next validation (unknown event key) is what stops this call — the
    # label check itself let it through.
    with pytest.raises(subscription_manager.SubscriptionError) as ei:
        _service_create(monkeypatch, account_label="bound")
    assert "unknown event keys" in str(ei.value)


# --- target kinds: one manifest, two ways to register ------------------------------

_KINDS_BLOCK = {
    "available": True, "provider_id": "github",
    "signature": {"algorithm": "hmac-sha256", "header": "X-Hub-Signature-256",
                  "per_subscription_secret": True},
    "url_verification": {"kind": "none"},
    "registration": {
        "mode": "auto",
        "create": {"method": "POST",
                   "url_template": "https://api.github.com/repos/${vendor_target}/hooks",
                   "headers": {"Authorization": "token ${account.access_token}"},
                   "response_id_path": "id", "expected_status": [201]},
        "delete": {"method": "DELETE",
                   "url_template": "https://api.github.com/repos/${vendor_target}/hooks/${vendor_subscription_id}",
                   "expected_status": [204, 404]},
    },
    "event_catalog": [{"key": "push", "label": "Commits pushed", "required_scopes": ["repo"]}],
    "payload_normalization": {"event_type_path": "headers.X-GitHub-Event"},
    "vendor_target_spec": {
        "kind": "free_text", "label": "Repository (owner/name)",
        "validation_regex": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        "target_kinds": [
            {"key": "repository", "label": "One repository",
             "validation_regex": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
            {"key": "organization", "label": "Every repository in an organization",
             "validation_regex": "^[A-Za-z0-9_.-]+$",
             "required_scopes": ["admin:org_hook"],
             "help_text": "Needs the Organization webhooks permission and owner rights on the organization.",
             "registration": {
                 "create": {"url_template": "https://api.github.com/orgs/${vendor_target}/hooks"},
                 "delete": {"url_template": "https://api.github.com/orgs/${vendor_target}/hooks/${vendor_subscription_id}"},
             }},
        ],
    },
}


def test_resolve_target_kind_and_registration_merge():
    """'' is the first kind; an override restates only the URL and inherits
    the rest; a manifest without kinds is its flat spec."""
    import copy
    wb = copy.deepcopy(_KINDS_BLOCK)
    assert subscription_manager.resolve_target_kind(wb, "")["key"] == "repository"
    assert subscription_manager.resolve_target_kind(wb, "organization")["required_scopes"] == ["admin:org_hook"]
    with pytest.raises(subscription_manager.SubscriptionError):
        subscription_manager.resolve_target_kind(wb, "team")
    org_create = subscription_manager.registration_call(wb, "organization", "create")
    assert org_create["url_template"] == "https://api.github.com/orgs/${vendor_target}/hooks"
    assert org_create["method"] == "POST" and org_create["response_id_path"] == "id"
    assert org_create["headers"] == {"Authorization": "token ${account.access_token}"}
    assert subscription_manager.registration_call(wb, "organization", "delete")["expected_status"] == [204, 404]
    assert subscription_manager.registration_call(wb, "", "create")["url_template"].startswith("https://api.github.com/repos/")
    assert subscription_manager.registration_call(wb, "repository", "renew") == {}
    del wb["vendor_target_spec"]["target_kinds"]
    assert subscription_manager.resolve_target_kind(wb, "")["label"] == "Repository (owner/name)"
    assert subscription_manager.registration_call(wb, "", "create")["url_template"].startswith("https://api.github.com/repos/")
    with pytest.raises(subscription_manager.SubscriptionError):
        subscription_manager.resolve_target_kind(wb, "organization")


def _create_kind(monkeypatch, *, target, kind, granted, calls, call_vendor=None):
    """Run create_subscription on the kinds manifest in auto mode, with the
    vendor call captured into ``calls`` and the store in memory."""
    monkeypatch.setattr(
        subscription_manager.mcp_registry, "get_manifest",
        lambda n: SimpleNamespace(credentials=SimpleNamespace(webhooks=_KINDS_BLOCK, oauth=None)))
    monkeypatch.setattr(subscription_manager, "_read_granted_scopes", lambda **kw: granted)
    monkeypatch.setattr(subscription_manager, "_resolve_account_extra", lambda **kw: {})
    monkeypatch.setattr(subscription_manager, "_resolve_token_or_raise", lambda **kw: "tok")
    monkeypatch.setattr(subscription_manager, "_effective_registration_mode", lambda **kw: "auto")
    rows: dict = {}

    def fake_store_create(**kw):
        rows["row"] = {"id": "row-1", "status": "creating", **kw}
        return rows["row"]

    monkeypatch.setattr(webhook_subscription_store, "create_subscription", fake_store_create)
    monkeypatch.setattr(webhook_subscription_store, "update_subscription_status", lambda *a, **kw: None)
    monkeypatch.setattr(webhook_subscription_store, "get_subscription", lambda sid: rows.get("row"))

    async def fake_call_vendor(*, call_block, row, access_token, extra_subs,
                               account_extra=None, body_overrides=None):
        calls.append(call_block["url_template"])
        return {"id": "hook-9"}

    monkeypatch.setattr(subscription_manager, "_call_vendor", call_vendor or fake_call_vendor)
    return asyncio.run(subscription_manager.create_subscription(
        user_sub="alice", scope="user", agent=None, mcp_name="github-mcp",
        account_label="a@x.com", vendor_target=target, selected_events=["push"],
        target_kind=kind,
    ))


def test_create_org_kind_needs_its_scope_then_registers_on_the_org(monkeypatch):
    calls: list = []
    with pytest.raises(subscription_manager.SubscriptionScopeError) as ei:
        _create_kind(monkeypatch, target="OtoDock", kind="organization",
                     granted={"repo"}, calls=calls)
    assert ei.value.required_scopes == ["admin:org_hook"]
    assert calls == []
    row = _create_kind(monkeypatch, target="OtoDock", kind="organization",
                       granted={"repo", "admin:org_hook"}, calls=calls)
    assert row["target_kind"] == "organization"
    assert calls == ["https://api.github.com/orgs/${vendor_target}/hooks"]


def test_create_repository_kind_is_the_default_and_the_old_path(monkeypatch):
    calls: list = []
    row = _create_kind(monkeypatch, target="OtoDock/oto-dock", kind="",
                       granted={"repo"}, calls=calls)
    assert row["target_kind"] == ""
    assert calls == ["https://api.github.com/repos/${vendor_target}/hooks"]


def test_create_refuses_a_target_of_the_wrong_shape(monkeypatch):
    """A repository string sent as an organization (and the reverse) stops
    at the API: no row, no vendor call."""
    calls: list = []
    for target, kind in (("OtoDock/oto-dock", "organization"), ("OtoDock", "repository")):
        with pytest.raises(subscription_manager.SubscriptionError) as ei:
            _create_kind(monkeypatch, target=target, kind=kind,
                         granted={"repo", "admin:org_hook"}, calls=calls)
        assert ei.value.status == 400
    with pytest.raises(subscription_manager.SubscriptionError):
        _create_kind(monkeypatch, target="OtoDock", kind="team",
                     granted={"repo", "admin:org_hook"}, calls=calls)
    assert calls == []


@pytest.mark.asyncio
async def test_vendor_delete_renders_the_kind_it_was_created_with(monkeypatch):
    calls: list = []

    async def fake_call_vendor(*, call_block, row, access_token, extra_subs,
                               account_extra=None, body_overrides=None):
        calls.append(call_block["url_template"])
        return {}

    monkeypatch.setattr(subscription_manager, "_call_vendor", fake_call_vendor)
    monkeypatch.setattr(subscription_manager, "_resolve_token_or_raise", lambda **kw: "tok")
    monkeypatch.setattr(subscription_manager, "_resolve_account_extra", lambda **kw: {})
    monkeypatch.setattr(
        subscription_manager.mcp_registry, "get_manifest",
        lambda n: SimpleNamespace(credentials=SimpleNamespace(webhooks=_KINDS_BLOCK, oauth=None)))
    base = {"id": "s1", "provider_id": "github", "mcp_name": "github-mcp", "scope": "user",
            "owner": "alice", "agent": None, "account_label": "a@x.com",
            "vendor_subscription_id": "hook-9", "delivery_mode": "vendor"}
    await subscription_manager._vendor_delete({**base, "vendor_target": "OtoDock", "target_kind": "organization"})
    await subscription_manager._vendor_delete({**base, "vendor_target": "OtoDock/oto-dock", "target_kind": ""})
    await subscription_manager._vendor_delete({**base, "vendor_target": "OtoDock/oto-dock"})
    assert calls == [
        "https://api.github.com/orgs/${vendor_target}/hooks/${vendor_subscription_id}",
        "https://api.github.com/repos/${vendor_target}/hooks/${vendor_subscription_id}",
        "https://api.github.com/repos/${vendor_target}/hooks/${vendor_subscription_id}",
    ]


def test_delete_reports_whether_the_vendor_let_go(monkeypatch):
    """The row goes either way (orphan-tolerant); the outcome says whether a
    registration was left behind at the vendor."""
    row = {"id": "s1", "delivery_mode": "vendor", "provider_id": "github"}
    monkeypatch.setattr(webhook_subscription_store, "get_subscription", lambda sid: row)
    monkeypatch.setattr(webhook_subscription_store, "delete_subscription", lambda sid: True)

    async def ok(r):
        return None

    async def boom(r):
        raise subscription_manager.VendorAPIError(
            "vendor 401: bad credentials", vendor_status=401, vendor_body="bad credentials")

    monkeypatch.setattr(subscription_manager, "_vendor_delete", ok)
    assert asyncio.run(subscription_manager.delete_subscription(subscription_id="s1")) == (True, True)
    monkeypatch.setattr(subscription_manager, "_vendor_delete", boom)
    assert asyncio.run(subscription_manager.delete_subscription(subscription_id="s1")) == (True, False)
    monkeypatch.setattr(webhook_subscription_store, "get_subscription", lambda sid: None)
    assert asyncio.run(subscription_manager.delete_subscription(subscription_id="s1")) == (False, True)


@pytest.mark.asyncio
async def test_vendor_delete_with_the_mcp_gone_reports_the_hook_left_behind(monkeypatch):
    monkeypatch.setattr(subscription_manager.mcp_registry, "get_manifest", lambda n: None)
    base = {"id": "s1", "provider_id": "github", "mcp_name": "github-mcp", "delivery_mode": "vendor"}
    assert await subscription_manager._vendor_delete({**base, "vendor_subscription_id": "hook-9"}) is False
    assert await subscription_manager._vendor_delete({**base, "vendor_subscription_id": None}) is True
    assert await subscription_manager._vendor_delete({**base, "delivery_mode": "relay"}) is True


def test_create_vendor_refusal_carries_the_kinds_help_text(monkeypatch):
    calls: list = []

    async def refuse(*, call_block, row, access_token, extra_subs, account_extra=None, body_overrides=None):
        raise subscription_manager.VendorAPIError(
            "vendor 404: Not Found", vendor_status=404, vendor_body='{"message":"Not Found"}')

    with pytest.raises(subscription_manager.VendorAPIError) as ei:
        _create_kind(monkeypatch, target="OtoDock", kind="organization",
                     granted={"repo", "admin:org_hook"}, calls=calls, call_vendor=refuse)
    assert ei.value.vendor_status == 404
    assert "owner rights" in str(ei.value)
