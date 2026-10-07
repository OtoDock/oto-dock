"""Vendor-webhook subscription orchestration.

Owns the create / delete / renew flow that talks to the vendor's webhook
API. Pure storage lives in ``storage/automation/webhook_subscription_store.py``;
this module wires storage to:

* OAuth token resolution (``oauth_account_store._read_oauth_token``)
* Manifest template substitution (``_render_template``)
* httpx calls to vendor APIs

Cleanup hooks (``cleanup_user_subscriptions`` etc.) wrap storage rows in
best-effort vendor DELETE — orphan-tolerant when the vendor rejects.

The dispatcher (``services.webhooks.webhook_dispatcher``) is the receive side and
doesn't go through this module — it reads subscription rows directly and
calls ``record_event_received`` after fan-out.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

import config
from services.mcp import mcp_registry
from services.oauth.oauth_account_store import (
    MCP_AUTHORIZATION_FLOW,
    _read_oauth_token,
    account_token_path,
    get_token_dir,
)
from services.webhooks import vendor_http
from services.webhooks.webhook_template import (  # noqa: F401  (re-exported for callers/tests)
    _TOKEN_RE,
    _build_substitutions,
    _substitute_string,
    _substitute_value,
    _walk_dot_path,
)
from storage.identity import credential_store
from storage import database as task_store
from storage.automation import webhook_subscription_store

logger = logging.getLogger("claude-proxy.subscription-manager")


class SubscriptionError(Exception):
    """Base for subscription orchestration errors. Carries an HTTP-ish
    ``status`` so the API layer can map cleanly."""

    def __init__(self, message: str, *, status: int = 500, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class SubscriptionPermissionError(SubscriptionError):
    def __init__(self, message: str, detail: Any = None):
        super().__init__(message, status=403, detail=detail)


class SubscriptionScopeError(SubscriptionError):
    """Raised when the bound OAuth account lacks one or more required scopes."""

    def __init__(self, message: str, *, required_scopes: list[str], detail: Any = None):
        super().__init__(message, status=400, detail=detail)
        self.required_scopes = required_scopes


class VendorAPIError(SubscriptionError):
    """Raised when the vendor's webhook API returns a non-expected status.

    Surfaces as 422 (not 502): Cloudflare-class edges replace origin
    502/504 bodies with their own branded HTML error page, which would
    hide the vendor's actual rejection from the dashboard. 422 carries
    the vendor_status/vendor_body detail through untouched.
    """

    def __init__(self, message: str, *, vendor_status: int, vendor_body: str = ""):
        super().__init__(message, status=422, detail={"vendor_status": vendor_status,
                                                       "vendor_body": vendor_body[:500]})
        self.vendor_status = vendor_status
        self.vendor_body = vendor_body


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_subscription(
    *,
    user_sub: str,
    scope: str,
    agent: str | None,
    mcp_name: str,
    account_label: str,
    vendor_target: str,
    selected_events: list[str],
    selected_subevents: dict[str, list[str]] | None = None,
    caller_is_admin: bool = False,
    target_kind: str = "",
) -> dict:
    """End-to-end subscription create.

    Flow:
      1. Load manifest, validate ``selected_events`` are in event_catalog,
         resolve the target kind (``vendor_target_spec.target_kinds``; the
         flat spec when the manifest declares none).
      2. Cross-check ``required_scopes`` — the events' plus the kind's —
         against bound OAuth account's granted scopes → raise
         ``SubscriptionScopeError`` if missing.
      3. Insert DB row with status='creating' + freshly-minted signing secret.
      4. Resolve OAuth token; render registration.create template.
      5. POST to vendor with 30s timeout.
      6. On success: UPDATE row to status='active', vendor_subscription_id,
         expires_at (when manifest declares lifetime_seconds).
      7. On vendor failure: UPDATE row to status='failed', last_error;
         raise ``VendorAPIError``.

    Returns the row dict (signing_secret_enc stripped). Raises one of the
    ``SubscriptionError`` subclasses on failure.
    """
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None:
        raise SubscriptionError(f"unknown MCP: {mcp_name}", status=404)
    webhooks_block = (manifest.credentials.webhooks or {}) if manifest.credentials else {}
    if not webhooks_block.get("available", False):
        raise SubscriptionError(
            f"MCP {mcp_name!r} does not declare webhook receiver support", status=400,
        )

    provider_id = webhooks_block.get("provider_id", "")
    catalog = webhooks_block.get("event_catalog", [])

    # Service-scope subscriptions require an explicit agent binding — there is
    # no platform service-account fallback. Reject early with a clear error.
    if scope == "service":
        if not agent:
            raise SubscriptionError(
                "service-scope subscription requires an agent", status=400,
            )
        from services.oauth import credential_resolver
        ref = credential_resolver.pick_account(mcp_name, agent)
        if ref is None:
            raise SubscriptionError(
                f"no account bound to agent {agent!r} for {mcp_name} — a "
                f"manager must bind one in Agent Settings first",
                status=400,
            )
        # Every later step opens the bound owner's token dir at the request's
        # label (scopes, extra, target tokens, effective mode): a label other
        # than the binding's would read another of that user's accounts.
        if account_label != ref.label:
            raise SubscriptionError(
                f"account {account_label!r} is not the service account bound "
                f"to agent {agent!r} for {mcp_name}: subscribe with the bound "
                f"account {ref.label!r}",
                status=400,
                detail={"bound_account_label": ref.label},
            )

    # 1. Validate selected_events are in catalog.
    catalog_keys = {entry["key"] for entry in catalog}
    invalid = [e for e in (selected_events or []) if e not in catalog_keys]
    if invalid:
        raise SubscriptionError(
            f"unknown event keys: {invalid} (valid: {sorted(catalog_keys)})",
            status=400,
        )
    if not selected_events:
        raise SubscriptionError(
            "selected_events must be a non-empty list", status=400,
        )

    # 1b. Per-event gating flags (general manifest concept):
    #   * admin_only — workspace-wide event streams (a personal subscription
    #     would tap the whole company). Requires an ADMIN creating a
    #     SERVICE-scope subscription (service rows resolve credentials from
    #     the platform `_service` store, which holds only admin-created
    #     accounts — see api/mcp/credentials.py / credential_resolver).
    #   * delivery: "bot" — the event only reaches bot-install credentials
    #     (xoxb class); the bot-install OAuth flow isn't supported yet, so
    #     until an account carries extra.token_kind == "bot" this refuses
    #     with a clear message.
    by_key = {e.get("key"): e for e in catalog if isinstance(e, dict)}
    gated = [by_key.get(evk) or {} for evk in selected_events]
    for evk, entry in zip(selected_events, gated):
        if entry.get("admin_only") and not (caller_is_admin and scope == "service"):
            raise SubscriptionPermissionError(
                f"event {evk!r} is admin-only: it requires an admin creating "
                f"an agent-scope (service) subscription on a platform "
                f"service account",
            )
    if any(e.get("delivery", "user") == "bot" for e in gated):
        bot_extra = _resolve_account_extra(
            provider_id=provider_id, scope=scope,
            owner=user_sub, agent=agent, mcp_name=mcp_name,
            account_label=account_label,
        )
        if bot_extra.get("token_kind") != "bot":
            bot_keys = [k for k, e in zip(selected_events, gated)
                        if e.get("delivery", "user") == "bot"]
            raise SubscriptionError(
                f"events {bot_keys} are delivered to the company bot and need "
                f"a bot-install credential — the bot-install flow is not yet "
                f"available on this account",
                status=400,
            )

    # 2. Cross-check required_scopes against the bound account. Skipped
    # for PATs (granted_scopes returns None — we trust the user's PAT
    # scope selection; the vendor surfaces the real permission problem
    # via 403 on the subscribe call if scopes are insufficient).
    kind_entry = resolve_target_kind(webhooks_block, target_kind)
    required_scopes = _gather_required_scopes(selected_events, catalog)
    required_scopes |= {
        s for s in kind_entry.get("required_scopes") or [] if isinstance(s, str) and s
    }
    granted_scopes = _read_granted_scopes(
        scope=scope, owner=user_sub, agent=agent,
        provider_id=provider_id, mcp_name=mcp_name, account_label=account_label,
    )
    if granted_scopes is not None:
        missing = sorted(set(required_scopes) - granted_scopes)
        if missing:
            raise SubscriptionScopeError(
                f"OAuth account is missing required scopes: {missing}",
                required_scopes=missing,
                detail={"granted": sorted(granted_scopes),
                        "needed": sorted(required_scopes)},
            )

    # Normalize common vendor_target sloppiness. GitHub repo URLs often get
    # pasted with `.git` suffix (clone URL form); GitHub's API needs the
    # plain `owner/repo` shape. Strip transparently per-provider.
    if provider_id == "github" and vendor_target.endswith(".git"):
        vendor_target = vendor_target[:-4]

    # Resolve ``${account.extra.*}`` tokens in vendor_target BEFORE row insert.
    # MS Graph's resource path needs the bound user's GUID (object_id claim);
    # the manifest's static_list options carry that as a template token. We
    # store the resolved string so downstream code (delete, renew, dispatcher
    # debugging) doesn't have to re-resolve at every step.
    if "${" in vendor_target:
        account_extra = _resolve_account_extra(
            provider_id=provider_id, scope=scope,
            owner=user_sub, agent=agent, mcp_name=mcp_name,
            account_label=account_label,
        )
        # Use a tiny inline substitution rather than _build_substitutions
        # (no row exists yet at this point). Only the account.extra.*
        # namespace is in scope for vendor_target tokens.
        def _vt_repl(m: re.Match) -> str:
            key = m.group(1)
            if key.startswith("account.extra."):
                sub_key = key[len("account.extra."):]
                val = account_extra.get(sub_key, "")
                return str(val) if not isinstance(val, (dict, list)) else json.dumps(val)
            # Unknown tokens render empty — vendor's API call will surface
            # the resulting invalid path as a proper error.
            return ""
        vendor_target = _TOKEN_RE.sub(_vt_repl, vendor_target)

    # With target kinds the API is the check that must refuse a repository
    # string sent as an organization (the modal's regex is advisory). A
    # manifest without kinds keeps the client-side check alone: MS Graph's
    # `${account.extra.*}` targets never matched a pattern here.
    if (webhooks_block.get("vendor_target_spec") or {}).get("target_kinds"):
        pattern = kind_entry.get("validation_regex") or ""
        if pattern and not re.search(pattern, vendor_target):
            raise SubscriptionError(
                f"{kind_entry.get('label') or 'target'}: {vendor_target!r} does not "
                f"look like {kind_entry.get('placeholder') or 'the expected form'}",
                status=400,
            )

    # Effective registration mode: hosted installs with a relay-capable
    # manifest + a relay-exchanged account deliver events VIA the relay
    # (zero vendor-console steps). Register with the relay BEFORE the row
    # insert so an unsupported/failed registration never leaves a half-made
    # relay row behind.
    reg_block = webhooks_block.get("registration", {})
    effective_mode = _effective_registration_mode(
        webhooks_block=webhooks_block, scope=scope,
        owner=user_sub, agent=agent, mcp_name=mcp_name,
        account_label=account_label,
    )
    if effective_mode == "relay":
        from services.billing import relay_client
        try:
            await _relay_register_events(provider_id=provider_id)
        except relay_client.RelayError as e:
            if e.code == "events_provider_unsupported":
                # The relay has no event support for this provider (yet) —
                # fall back to the manifest's own mode.
                effective_mode = reg_block.get("mode", "manual")
            else:
                # 422, not 502 — edge proxies (Cloudflare) replace origin
                # 502 bodies with their own HTML page, hiding the message.
                raise SubscriptionError(
                    relay_client.relay_error_message(e.code), status=422,
                )
        except SubscriptionError:
            raise
        except Exception as e:
            raise SubscriptionError(
                f"relay events registration failed: {e}", status=502,
            ) from e
    delivery_mode = "relay" if effective_mode == "relay" else "vendor"

    # 3. Generate signing secret + insert row. Per-vendor-secret model
    # routes via manifest.signature.per_subscription_secret. Relay rows
    # store "" — forwards verify against the install-global forward secret.
    sig_block = webhooks_block.get("signature", {})
    per_sub_secret = sig_block.get("per_subscription_secret", False)
    uv_kind = (webhooks_block.get("url_verification") or {}).get("kind", "none")
    if per_sub_secret and delivery_mode != "relay" \
            and uv_kind != "verification_token_capture":
        signing_secret = secrets.token_urlsafe(32)
    else:
        # Platform-wide secret lives in infra_credentials; the row stores
        # "" and the dispatcher resolves at verify time. Token-capture vendors
        # (Notion) DICTATE the secret — the row starts empty and the
        # dispatcher's capture fills it on the vendor's setup POST.
        signing_secret = ""

    # Optional manifest-declared lifetime.
    lifetime_seconds = (webhooks_block.get("registration") or {}).get("lifetime_seconds")
    expires_at = None
    if lifetime_seconds:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(lifetime_seconds))
        ).isoformat().replace("+00:00", "Z")

    row = webhook_subscription_store.create_subscription(
        scope=scope,
        owner=user_sub if scope == "user" else "",
        agent=agent if scope == "service" else None,
        mcp_name=mcp_name,
        provider_id=provider_id,
        account_label=account_label,
        vendor_target=vendor_target,
        selected_events=selected_events,
        selected_subevents=selected_subevents or {},
        signing_secret=signing_secret,
        created_by=user_sub,
        expires_at=expires_at,
        delivery_mode=delivery_mode,
        target_kind=target_kind,
    )

    if effective_mode == "relay":
        # Relay delivery: no vendor call — the relay forwards events for the
        # account's (workspace, user) binding as soon as they arrive; the row
        # is ready to receive immediately.
        webhook_subscription_store.update_subscription_status(
            row["id"], webhook_subscription_store.ACTIVE, clear_last_error=True,
        )
    elif effective_mode == "auto":
        try:
            vendor_id = await _vendor_create(
                row=row,
                webhooks_block=webhooks_block,
                signing_secret=signing_secret,
                selected_events=selected_events,
                selected_subevents=selected_subevents or {},
                scope=scope,
                owner=user_sub,
                agent=agent,
                expires_at_iso8601=expires_at or "",
            )
        except VendorAPIError as e:
            webhook_subscription_store.update_subscription_status(
                row["id"], webhook_subscription_store.FAILED,
                last_error=f"vendor {e.vendor_status}: {e.vendor_body[:200]}",
            )
            # GitHub answers 404, not 403, when the token lacks the org
            # permission or the account is not an owner: bare, the message
            # reads like a typo in the name. The kind's help text is the
            # manifest author's own explanation of what the target needs.
            hint = str(kind_entry.get("help_text") or "").strip()
            if e.vendor_status in (403, 404) and hint:
                raise VendorAPIError(
                    f"{e} ({hint})", vendor_status=e.vendor_status, vendor_body=e.vendor_body,
                ) from e
            raise
        except Exception as e:
            webhook_subscription_store.update_subscription_status(
                row["id"], webhook_subscription_store.FAILED, last_error=f"{type(e).__name__}: {e}",
            )
            # 500, not 502 — edge proxies (Cloudflare) replace origin 502
            # bodies with their own HTML page, hiding the message.
            raise SubscriptionError(
                f"unexpected error during vendor create: {e}",
                status=500,
            ) from e

        webhook_subscription_store.update_subscription_status(
            row["id"], webhook_subscription_store.ACTIVE,
            vendor_subscription_id=vendor_id,
            clear_last_error=True,
        )
    else:
        # Manual mode — no vendor API call (Slack/Zoom have no public
        # auto-register endpoint for v1). Flip to 'active' immediately so
        # the dispatcher accepts inbound events as soon as the admin pastes
        # the webhook URL into the vendor's app dashboard. We trust the
        # admin to complete the out-of-band step; the row reflecting
        # 'active' represents "ready to receive" rather than "vendor
        # confirmed". The dashboard SubscriptionsPanel surfaces the URL
        # (and the per-subscription secret if applicable) for paste.
        webhook_subscription_store.update_subscription_status(
            row["id"], webhook_subscription_store.ACTIVE, clear_last_error=True,
        )

    return webhook_subscription_store.get_subscription(row["id"]) or row


def resolve_effective_registration_mode(
    *, mcp_name: str, scope: str, owner: str, agent: str | None = None,
    account_label: str,
) -> str:
    """The registration mode subscribe will actually use ('relay' | 'auto' |
    'manual') — consumed by the catalog endpoint so the UI shows the right
    copy before the user subscribes."""
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None:
        return "manual"
    webhooks_block = (manifest.credentials.webhooks or {}) if manifest.credentials else {}
    return _effective_registration_mode(
        webhooks_block=webhooks_block, scope=scope, owner=owner,
        agent=agent, mcp_name=mcp_name, account_label=account_label,
    )


def _effective_registration_mode(
    *, webhooks_block: dict, scope: str, owner: str, agent: str | None,
    mcp_name: str, account_label: str,
) -> str:
    """'relay' when ALL of: the OtoDock relay is configured, the manifest
    declares ``workspace_id_path`` (the fan-in key), and the bound account was
    relay-exchanged (``extra.via_relay`` — only relay-exchanged accounts have
    a routing binding on the relay). Self-managed accounts keep the
    manifest's own mode."""
    manifest_mode = (webhooks_block.get("registration") or {}).get("mode", "manual")
    if not webhooks_block.get("workspace_id_path"):
        return manifest_mode
    from services.billing import relay_client
    if not relay_client.is_available():
        return manifest_mode
    try:
        extra = _resolve_account_extra(
            provider_id=webhooks_block.get("provider_id", ""),
            scope=scope, owner=owner, agent=agent, mcp_name=mcp_name,
            account_label=account_label,
        )
    except Exception:
        return manifest_mode
    if not extra.get("via_relay"):
        return manifest_mode
    return "relay"


async def _relay_register_events(*, provider_id: str) -> None:
    """Ensure this install's event forwarding is registered with the relay for
    ``provider_id`` and the forward secret is held locally
    (``infra_credentials['otodock-relay'].EVENTS_FORWARD_SECRET`` — the relay
    ingest route verifies forwards against it). Sends ``rotate_secret=True``
    whenever no local secret exists (first registration or DB restore)."""
    from services.billing import relay_client

    if not config.DASHBOARD_PUBLIC_URL:
        raise SubscriptionError(
            "DASHBOARD_PUBLIC_URL must be set for hosted event delivery "
            "(the relay needs a reachable URL to forward events to)",
            status=400,
        )
    events_url = (
        f"{config.DASHBOARD_PUBLIC_URL.rstrip('/')}/v1/webhooks/relay/{provider_id}"
    )
    have_secret = bool(
        (credential_store.get_infra_credentials(
            relay_client.EVENTS_FORWARD_SECRET_SLUG) or {})
        .get(relay_client.EVENTS_FORWARD_SECRET_KEY)
    )
    out = await relay_client.events_register(
        provider_id=provider_id, events_url=events_url, enabled=True,
        rotate_secret=not have_secret,
    )
    fresh = (out or {}).get("forward_secret")
    if fresh:
        credential_store.set_infra_credentials(
            relay_client.EVENTS_FORWARD_SECRET_SLUG,
            {relay_client.EVENTS_FORWARD_SECRET_KEY: fresh},
        )


async def _maybe_disable_relay_events(provider_id: str) -> None:
    """Best-effort: after the LAST relay-mode subscription for a provider is
    deleted, tell the relay to stop forwarding (the secret is kept so a
    re-subscribe re-enables without rotation)."""
    if not provider_id:
        return
    remaining = webhook_subscription_store.list_subscriptions(
        provider_id=provider_id, delivery_mode="relay",
    )
    if remaining:
        return
    from services.billing import relay_client
    try:
        events_url = (
            f"{(config.DASHBOARD_PUBLIC_URL or '').rstrip('/')}"
            f"/v1/webhooks/relay/{provider_id}"
        )
        await relay_client.events_register(
            provider_id=provider_id, events_url=events_url, enabled=False,
        )
    except Exception as e:
        logger.info("relay events disable skipped (%s): %s", provider_id, e)


async def delete_subscription(*, subscription_id: str) -> tuple[bool, bool]:
    """Delete a subscription. Best-effort vendor DELETE first.

    Authorization happens at the API layer (_can_manage_subscription) —
    this function performs no actor checks.
    Returns ``(deleted, vendor_detached)``: whether the row went, and
    whether nothing is left registered at the vendor. A vendor failure is
    logged and does not block the DB delete (orphan-tolerant by design),
    but it is reported — a hook left behind keeps posting to a dead URL
    until a human removes it in the vendor's settings.
    Relay-delivered rows have no vendor-side registration; deleting the
    LAST relay row for a provider best-effort disables relay forwarding
    (the forward secret is kept for cheap re-enable).
    """
    row = webhook_subscription_store.get_subscription(subscription_id)
    if not row:
        return False, True
    if row.get("delivery_mode") == "relay":
        deleted = webhook_subscription_store.delete_subscription(subscription_id)
        if deleted:
            await _maybe_disable_relay_events(row.get("provider_id", ""))
        return deleted, True
    try:
        detached = await _vendor_delete(row) is not False
    except Exception as e:
        detached = False
        logger.warning(
            "vendor delete failed for subscription %s (provider=%s): %s. "
            "Proceeding with DB delete (orphan-tolerant).",
            subscription_id, row.get("provider_id"), e,
        )
    return webhook_subscription_store.delete_subscription(subscription_id), detached


async def renew_subscription(subscription_id: str) -> dict | None:
    """Force-renew a subscription via the vendor's renew API.

    Reads ``registration.renew`` block + ``lifetime_seconds`` from manifest.
    Updates ``expires_at`` on success; flips to ``renew_failed`` on vendor
    rejection.
    """
    row = webhook_subscription_store.get_subscription(subscription_id)
    if not row:
        return None
    manifest = mcp_registry.get_manifest(row["mcp_name"])
    if manifest is None:
        return row
    webhooks_block = (manifest.credentials.webhooks or {}) if manifest.credentials else {}
    reg_block = webhooks_block.get("registration", {})
    renew_block = registration_call(webhooks_block, row.get("target_kind") or "", "renew")
    lifetime = reg_block.get("lifetime_seconds")
    if not renew_block or not lifetime:
        # Vendor doesn't support renewal — no-op.
        return row

    new_expires = (
        datetime.now(timezone.utc) + timedelta(seconds=int(lifetime))
    ).isoformat().replace("+00:00", "Z")

    try:
        token = _resolve_token_or_raise(
            provider_id=row["provider_id"],
            scope=row["scope"],
            owner=row["owner"] or "",
            agent=row.get("agent"),
            mcp_name=row["mcp_name"],
            account_label=row["account_label"],
        )
        account_extra = _resolve_account_extra(
            provider_id=row["provider_id"],
            scope=row["scope"],
            owner=row["owner"] or "",
            agent=row.get("agent"),
            mcp_name=row["mcp_name"],
            account_label=row["account_label"],
        )
        await _call_vendor(
            call_block=renew_block,
            row=row,
            access_token=token,
            extra_subs={"expires_at_iso8601": new_expires},
            account_extra=account_extra,
        )
    except VendorAPIError as e:
        webhook_subscription_store.update_subscription_status(
            subscription_id, webhook_subscription_store.RENEW_FAILED,
            last_error=f"vendor {e.vendor_status}: {e.vendor_body[:200]}",
        )
        raise
    except Exception as e:
        webhook_subscription_store.update_subscription_status(
            subscription_id, webhook_subscription_store.RENEW_FAILED,
            last_error=f"{type(e).__name__}: {e}",
        )
        raise SubscriptionError(f"renew failed: {e}", status=502) from e

    webhook_subscription_store.update_subscription_status(
        subscription_id, webhook_subscription_store.ACTIVE,
        expires_at=new_expires, clear_last_error=True,
    )
    return webhook_subscription_store.get_subscription(subscription_id)


# ---------------------------------------------------------------------------
# Cleanup wrappers — called by user/agent/account-delete handlers
# ---------------------------------------------------------------------------

async def cleanup_user_subscriptions(user_sub: str) -> int:
    """Delete all user-scope subscriptions for a user. Returns count deleted."""
    rows = webhook_subscription_store.cleanup_user_subscriptions(user_sub)
    return await _cleanup_rows(rows)


async def cleanup_agent_subscriptions(agent: str) -> int:
    rows = webhook_subscription_store.cleanup_agent_subscriptions(agent)
    return await _cleanup_rows(rows)


async def cleanup_account_subscriptions(
    *, scope: str, owner: str, mcp_name: str, account_label: str,
    agent: str | None = None, token_owner_sub: str = "",
) -> int:
    """``token_owner_sub`` names the lender of a service binding that no
    longer stands (the offboarding subscriber): the vendor delete then reads
    the account's token through that person directly, where the binding
    lookup would answer nothing."""
    rows = webhook_subscription_store.cleanup_account_subscriptions(
        scope=scope, owner=owner, mcp_name=mcp_name, account_label=account_label,
        agent=agent,
    )
    return await _cleanup_rows(rows, token_owner_sub=token_owner_sub)


async def _cleanup_rows(rows: list[dict], *, token_owner_sub: str = "") -> int:
    """Best-effort vendor DELETE + DB DELETE for each row."""
    count = 0
    for row in rows:
        try:
            await _vendor_delete(row, token_owner_sub=token_owner_sub)
        except Exception as e:
            logger.warning(
                "cleanup: vendor delete failed for subscription %s (provider=%s): %s",
                row["id"], row.get("provider_id"), e,
            )
        if webhook_subscription_store.delete_subscription(row["id"]):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Internal: vendor calls + template substitution
# ---------------------------------------------------------------------------

async def _vendor_create(
    *,
    row: dict,
    webhooks_block: dict,
    signing_secret: str,
    selected_events: list[str],
    selected_subevents: dict[str, list[str]],
    scope: str,
    owner: str,
    agent: str | None,
    expires_at_iso8601: str = "",
) -> str:
    """Call vendor's create-webhook API. Returns captured vendor_subscription_id.

    Raises ``VendorAPIError`` when:
      * vendor returns an unexpected HTTP status (existing behavior in ``_call_vendor``)
      * manifest declares ``success_path`` and the path resolves to a falsy
        value in the response (GraphQL-style 200-on-user-error)
      * manifest declares ``response_id_path`` and the path resolves to
        empty (we'd silently store a row that can't be deleted later)
    """
    create_block = registration_call(webhooks_block, row.get("target_kind") or "", "create")
    if not create_block:
        raise SubscriptionError(
            "manifest declares registration.mode=auto but no registration.create block",
            status=500,
        )
    token = _resolve_token_or_raise(
        provider_id=row["provider_id"],
        scope=scope,
        owner=owner,
        agent=agent,
        mcp_name=row["mcp_name"],
        account_label=row["account_label"],
    )
    account_extra = _resolve_account_extra(
        provider_id=row["provider_id"],
        scope=scope,
        owner=owner,
        agent=agent,
        mcp_name=row["mcp_name"],
        account_label=row["account_label"],
    )
    # Per-event vendor-create overrides (catalog `vendor_create_fields`),
    # merged over the substituted body_template's top-level keys. Designed
    # for paired vendors where each subscription carries ONE event; with
    # multiple selected events, later catalog entries win on conflict.
    body_overrides: dict[str, Any] = {}
    for entry in webhooks_block.get("event_catalog") or []:
        if isinstance(entry, dict) and entry.get("key") in selected_events:
            vcf = entry.get("vendor_create_fields")
            if isinstance(vcf, dict):
                body_overrides.update(vcf)

    response_body = await _call_vendor(
        call_block=create_block,
        row=row,
        access_token=token,
        extra_subs={
            "selected_events": selected_events,
            "selected_subevents": selected_subevents,
            "subscription.signing_secret": signing_secret,
            # Manifests with registration.lifetime_seconds template the
            # vendor expiry (MS Graph expirationDateTime). Missing tokens
            # render as "" — which Graph rejects — so the create path must
            # provide this exactly like the renew path does.
            "expires_at_iso8601": expires_at_iso8601,
        },
        account_extra=account_extra,
        body_overrides=body_overrides,
    )
    # GraphQL-style 200-on-error guard. When manifest declares ``success_path``,
    # the vendor returns HTTP 200 even on user errors (Linear's webhookCreate
    # mutation puts the error in errors[] but the HTTP status is still 200).
    # The success_path is a dot-path into the response that MUST resolve to
    # a truthy value; otherwise we treat the response as a failure.
    success_path = create_block.get("success_path", "")
    if success_path:
        success_value = _walk_dot_path(response_body, success_path)
        if not success_value:
            raise VendorAPIError(
                f"vendor reported failure: success_path={success_path!r} resolved to {success_value!r}",
                vendor_status=200,
                vendor_body=json.dumps(response_body)[:500] if response_body else "",
            )
    response_id_path = create_block.get("response_id_path", "")
    if not response_id_path:
        return ""
    # Walk the path into the vendor's response. Empty result when the path
    # is declared = error (we'd otherwise silently store a row that has no
    # vendor_subscription_id to delete later).
    captured = _walk_dot_path(response_body, response_id_path)
    if not captured:
        raise VendorAPIError(
            f"vendor response missing required id at path {response_id_path!r}",
            vendor_status=200,
            vendor_body=json.dumps(response_body)[:500] if response_body else "",
        )
    return str(captured)


async def _vendor_delete(row: dict, *, token_owner_sub: str = "") -> bool:
    """Call vendor's delete-webhook API. Caller wraps in best-effort try/except.

    Returns whether nothing is left registered at the vendor: True after a
    successful call or when there was nothing to delete, False when the
    manifest is gone (the MCP was uninstalled) and the vendor still holds a
    registration nobody can address any more.
    """
    if row.get("delivery_mode") == "relay":
        # Relay-delivered rows registered nothing at the vendor — covers the
        # cleanup_* sweeps too.
        return True
    manifest = mcp_registry.get_manifest(row["mcp_name"])
    if manifest is None:
        return not row.get("vendor_subscription_id")
    webhooks_block = (manifest.credentials.webhooks or {}) if manifest.credentials else {}
    delete_block = registration_call(webhooks_block, row.get("target_kind") or "", "delete")
    if not delete_block:
        # Manifest declares no delete endpoint (vendor doesn't support it
        # OR uses manual mode). Nothing to do.
        return True
    if not row.get("vendor_subscription_id"):
        # Row was never confirmed by the vendor (status='creating' that
        # never flipped). No vendor-side state to delete.
        return True
    token = _resolve_token_or_raise(
        provider_id=row["provider_id"],
        scope=row["scope"],
        owner=row["owner"] or "",
        agent=row.get("agent"),
        mcp_name=row["mcp_name"],
        account_label=row["account_label"],
        token_owner_sub=token_owner_sub,
    )
    account_extra = _resolve_account_extra(
        provider_id=row["provider_id"],
        scope=row["scope"],
        owner=row["owner"] or "",
        agent=row.get("agent"),
        mcp_name=row["mcp_name"],
        account_label=row["account_label"],
        token_owner_sub=token_owner_sub,
    )
    await _call_vendor(
        call_block=delete_block,
        row=row,
        access_token=token,
        extra_subs={},
        account_extra=account_extra,
    )
    return True


async def _call_vendor(
    *,
    call_block: dict,
    row: dict,
    access_token: str,
    extra_subs: dict[str, Any],
    account_extra: dict[str, Any] | None = None,
    body_overrides: dict[str, Any] | None = None,
) -> dict:
    """Render a registration.{create|delete|renew} block and execute it.

    Returns the parsed JSON response body (or empty dict on no-body responses).
    Raises ``VendorAPIError`` on unexpected status.

    ``account_extra`` is the bound OAuth account's token-file ``extra`` dict;
    flattened under ``account.extra.<key>`` for template substitution.
    ``body_overrides`` are merged over the substituted body's TOP-LEVEL keys
    (per-event catalog ``vendor_create_fields`` — e.g. MS Graph driveItem
    subscriptions accept only changeType="updated").
    """
    subs = _build_substitutions(
        row=row,
        access_token=access_token,
        extra=extra_subs,
        account_extra=account_extra,
    )
    method = call_block["method"].upper()
    try:
        url = vendor_http.url_from_template(call_block["url_template"], subs)
        await vendor_http.check_url(url)
    except vendor_http.VendorURLRefused as e:
        raise VendorAPIError(f"vendor call refused: {e}", vendor_status=0, vendor_body="") from e
    headers = {k: _substitute_string(v, subs) for k, v in (call_block.get("headers") or {}).items()}
    body_template = call_block.get("body_template")
    body_data = _substitute_value(body_template, subs) if body_template is not None else None
    if body_overrides and isinstance(body_data, dict):
        body_data = {**body_data, **body_overrides}
    expected = set(call_block.get("expected_status", [200]))

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            req = client.build_request(
                method, url,
                headers=headers,
                json=body_data if body_data is not None else None,
            )
            resp = await client.send(req)
        except httpx.HTTPError as e:
            raise VendorAPIError(
                f"network error calling vendor: {e}",
                vendor_status=0, vendor_body=str(e),
            ) from e
    if resp.status_code not in expected:
        raise VendorAPIError(
            f"vendor returned unexpected status {resp.status_code} (expected {sorted(expected)})",
            vendor_status=resp.status_code,
            vendor_body=resp.text,
        )
    try:
        return resp.json() if resp.content else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _username_for(owner_sub: str) -> str:
    """Resolve a user_sub → filesystem username (used in token dir paths).

    Token files live at ``sessions/{provider}-tokens/{username}/{account}.json``
    where ``{username}`` is the filesystem-safe handle, NOT the user_sub
    (subs may be ``local:<uuid>`` or OIDC-issued strings unsafe for paths).
    Returns the resolved username or raises ``SubscriptionError`` — we can't
    proceed with a vendor API call if we can't find the OAuth token file.
    """
    if not owner_sub:
        raise SubscriptionError(
            "user-scope subscription requires a user_sub", status=400,
        )
    username = task_store.get_username_by_sub(owner_sub)
    if not username:
        raise SubscriptionError(
            f"could not resolve username for user_sub {owner_sub!r}", status=500,
        )
    return username


def _owner_username_for(
    *, scope: str, owner: str, agent: str | None, mcp_name: str,
    token_owner_sub: str = "",
) -> str:
    """Filesystem username whose token dir holds the bound account.

    User scope → the subscription owner. Service scope → the agent's binding
    points at a user's OWN account (no platform tier); return that user's
    username, or the named lender's when the caller knows the binding no
    longer stands. Raises when a service-scope subscription has no
    resolvable binding.
    """
    if scope == "service" and token_owner_sub:
        return _username_for(token_owner_sub)
    if scope == "service":
        from services.oauth import credential_resolver
        ref = credential_resolver.pick_account(mcp_name, agent or "")
        if ref is None:
            raise SubscriptionError(
                f"no account bound to agent {agent!r} for {mcp_name} — a "
                f"manager must bind one in Agent Settings",
                status=400,
            )
        return _username_for(ref.owner_sub)
    return _username_for(owner)


def _resolve_token_or_raise(
    *, provider_id: str, scope: str, owner: str, agent: str | None,
    mcp_name: str, account_label: str, token_owner_sub: str = "",
) -> str:
    """Resolve the bound OAuth account's access_token. Raises on miss."""
    username = _owner_username_for(
        scope=scope, owner=owner, agent=agent, mcp_name=mcp_name,
        token_owner_sub=token_owner_sub,
    )
    token_dir = get_token_dir(username=username, provider_id=provider_id)
    path = account_token_path(token_dir, account_label)
    if not path.exists():
        raise SubscriptionError(
            f"OAuth token file not found for {provider_id}/{account_label} "
            f"(owner={username!r})",
            status=400,
        )
    payload = _read_oauth_token(path)
    if not payload:
        raise SubscriptionError(
            f"OAuth token file is empty or unreadable for {provider_id}/{account_label}",
            status=400,
        )
    token = payload.get("access_token", "")
    if not token:
        raise SubscriptionError(
            f"OAuth token has no access_token field for {provider_id}/{account_label}",
            status=400,
        )
    # A token the MCP server's own authorization server issued is meant for
    # that server alone (RFC 8707): events need an account connected through
    # the relay or the vendor's app.
    extra = payload.get("extra") or {}
    if isinstance(extra, dict) and extra.get("flow") == MCP_AUTHORIZATION_FLOW:
        raise SubscriptionError(
            f"The {provider_id} account {account_label!r} signed in through the MCP "
            "server's own authorization server; its token serves the MCP server only. "
            "Events need an account connected through OtoDock or the vendor's app.",
            status=400,
        )
    return token


def _resolve_account_extra(
    *, provider_id: str, scope: str, owner: str, agent: str | None,
    mcp_name: str, account_label: str, token_owner_sub: str = "",
) -> dict:
    """Read the bound account's token-file ``extra`` block.

    Used to expose vendor-specific fields (Microsoft's ``object_id`` /
    ``tenant_id``, Slack's ``team_id`` / ``team_name``, Zoom's
    ``account_id``) to registration templates via the
    ``${account.extra.<key>}`` substitution namespace.

    Returns empty dict on miss — callers tolerate empty values rather
    than failing (the template will substitute "" and surface a vendor
    error on the API call if a critical field is empty).
    """
    try:
        username = _owner_username_for(
            scope=scope, owner=owner, agent=agent, mcp_name=mcp_name,
            token_owner_sub=token_owner_sub,
        )
        token_dir = get_token_dir(username=username, provider_id=provider_id)
        path = account_token_path(token_dir, account_label)
        if not path.exists():
            return {}
        payload = _read_oauth_token(path)
        if not payload:
            return {}
        extra = payload.get("extra")
        return extra if isinstance(extra, dict) else {}
    except Exception as e:
        logger.warning(
            "could not read account.extra for %s/%s: %s",
            provider_id, account_label, e,
        )
        return {}


def _read_granted_scopes(
    *,
    scope: str,
    owner: str,
    agent: str | None,
    provider_id: str,
    mcp_name: str,
    account_label: str,
) -> set[str] | None:
    """Read the bound account's granted scopes from the token file.

    Returns ``None`` when the scope set is unknowable (PAT flow — GitHub
    doesn't return the scopes a PAT was minted with at save time). The
    caller treats ``None`` as "trust the user's scope selection" and
    skips the missing-scope check; the vendor's own 401/403 on the
    subscribe call surfaces the real permission problem.

    Returns a (possibly empty) set when the scope set IS knowable but
    just happens to be empty (user genuinely connected with no scopes).
    """
    try:
        username = _owner_username_for(
            scope=scope, owner=owner, agent=agent, mcp_name=mcp_name,
        )
        token_dir = get_token_dir(username=username, provider_id=provider_id)
        path = account_token_path(token_dir, account_label)
        if not path.exists():
            return set()
        payload = _read_oauth_token(path)
        if not payload:
            return set()
        # PAT flow: scopes can't be enumerated from a saved PAT (GitHub
        # returns them only in the X-OAuth-Scopes header on a live
        # authenticated request — the OAuth engine doesn't fetch + persist
        # them today). Return None to signal "unknowable; skip the check".
        flow = (payload.get("extra") or {}).get("flow", "")
        if flow == "personal_access_token":
            return None
        scopes = payload.get("scopes", [])
        return {s for s in scopes if isinstance(s, str)}
    except Exception as e:
        logger.warning("could not read granted scopes for %s/%s: %s",
                       provider_id, account_label, e)
        return set()


def resolve_target_kind(webhooks_block: dict, key: str) -> dict:
    """The target-kind entry a subscription uses.

    A manifest without ``vendor_target_spec.target_kinds`` has one kind: the
    flat spec itself (and only the empty key names it). With kinds, the
    empty key is the first entry — every row created before kinds existed
    stores ``''`` and was registered the way the first kind registers.
    """
    spec = webhooks_block.get("vendor_target_spec") or {}
    kinds = [k for k in spec.get("target_kinds") or [] if isinstance(k, dict)]
    if not kinds:
        if key:
            raise SubscriptionError(f"unknown target kind: {key!r}", status=400)
        return spec
    if not key:
        return kinds[0]
    for entry in kinds:
        if entry.get("key") == key:
            return entry
    raise SubscriptionError(f"unknown target kind: {key!r}", status=400)


def registration_call(webhooks_block: dict, target_kind: str, call: str) -> dict:
    """The ``registration.<call>`` block a target kind runs: the top-level
    block with the kind's own keys laid over it. A kind restates only what
    differs (GitHub's organization kind: the URL) and inherits the rest."""
    base = (webhooks_block.get("registration") or {}).get(call) or {}
    if not (webhooks_block.get("vendor_target_spec") or {}).get("target_kinds"):
        return base
    override = (resolve_target_kind(webhooks_block, target_kind).get("registration") or {}).get(call)
    if not isinstance(override, dict) or not override:
        return base
    return {**base, **override}


def _gather_required_scopes(
    selected_events: list[str], event_catalog: list[dict],
) -> set[str]:
    """Union of required_scopes across all selected event_catalog entries."""
    out: set[str] = set()
    by_key = {entry["key"]: entry for entry in event_catalog}
    for evk in selected_events:
        entry = by_key.get(evk)
        if not entry:
            continue
        for s in entry.get("required_scopes", []) or []:
            if isinstance(s, str) and s:
                out.add(s)
    return out


