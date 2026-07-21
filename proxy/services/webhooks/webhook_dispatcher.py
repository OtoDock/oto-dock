"""Inbound webhook receive flow.

Single entry-point from the public ``api/events/webhooks.py`` route. Owns:

* URL-verification handshake short-circuit
* Signature verification (per-vendor)
* Event-id dedup via an in-memory ring (10-minute TTL, single-replica)
* Payload normalization → ``NormalizedEvent``
* Fan-out: match the event against triggers WHERE subscription_id=row.id,
  call ``trigger_manager.fire_trigger`` concurrently per match
* ``record_event_received`` housekeeping on the subscription row

Returns a JSON dict the API layer wraps in a Response.

Cross-replica dedup (Redis-backed) is on the deferred list — single proxy
for v1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

from auth import webhook_providers
from auth.webhook_providers.base import NormalizedEvent
from services.mcp import mcp_registry
from services.webhooks.event_normalizer import (
    match_event_filter,
    resolve_catalog_keys,
    walk_path,
)
from storage import trigger_store, webhook_subscription_store

logger = logging.getLogger("claude-proxy.webhook-dispatcher")

# The relay's forward-signature contract — verified with the SAME generic
# HMAC verifier the vendor schemes use, just with this constant pseudo-block
# + the install's forward secret (infra_credentials['otodock-relay']). The
# vendor's own signature was verified relay-side before forwarding.
_RELAY_SIG_BLOCK = {
    "algorithm": "hmac-sha256",
    "header": "x-otodock-event-signature",
    "version_prefix": "v0=",
    "timestamp_header": "x-otodock-event-timestamp",
    "timestamp_format": "unix",
    "signed_payload_template": "v0:{timestamp}:{body}",
    "max_age_seconds": 300,
}


# Module-level dedup ring. Each entry is (event_key, monotonic_seen_at).
# ``event_key`` is f"{subscription_id}:{vendor_event_id}" so cross-subscription
# id collisions don't cause false-positive dedup. Bounded 1000 events;
# entries older than _DEDUP_TTL_SECONDS are evicted on each lookup.
_DEDUP_RING: deque[tuple[str, float]] = deque(maxlen=1000)
_DEDUP_LOCK = asyncio.Lock()
_DEDUP_TTL_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def dispatch_webhook(
    *,
    provider_id: str,
    subscription_id: str,
    raw_body: bytes,
    headers: dict[str, str],
    query_params: dict[str, str],
    http_method: str = "POST",
) -> tuple[int, dict | str, dict[str, str]]:
    """End-to-end webhook receive.

    Returns ``(status_code, body, response_headers)``:
      * On URL-verification handshake: provider-specific body + content-type
      * On normal event: ``{"status": "ok"|"duplicate"|"no_triggers"|"verify_failed",
        "fired": N, "errors": [...]}``
      * Returns 410 for status='disabled'; 404 for unknown subscription_id.

    Always returns 200 once signature passes (vendors retry on non-200; we
    don't want trigger-fire errors to cause infinite retries).
    """
    # 1. Load subscription row.
    row = webhook_subscription_store.get_subscription(subscription_id)
    if not row:
        return (404, {"error": "subscription not found"}, {"content-type": "application/json"})
    if row.get("provider_id") != provider_id:
        return (404, {"error": "provider mismatch"}, {"content-type": "application/json"})
    if row.get("status") == "disabled":
        return (410, {"error": "subscription disabled"}, {"content-type": "application/json"})
    if row.get("status") not in ("active", "renew_failed", "creating"):
        return (410, {"error": f"subscription status={row.get('status')!r}"},
                {"content-type": "application/json"})

    # 2. Resolve manifest + provider implementation.
    manifest = mcp_registry.get_manifest(row["mcp_name"])
    if manifest is None:
        return (500, {"error": "manifest not found"}, {"content-type": "application/json"})
    webhooks_block = (manifest.credentials.webhooks or {}) if manifest.credentials else {}
    if not webhooks_block.get("available", False):
        return (500, {"error": "MCP no longer declares webhook receiver support"},
                {"content-type": "application/json"})
    try:
        provider = webhook_providers.get_provider(provider_id)
    except KeyError as e:
        logger.warning("unknown webhook provider %s: %s", provider_id, e)
        return (500, {"error": f"unknown provider {provider_id}"},
                {"content-type": "application/json"})

    # Lowercase headers for case-insensitive lookups downstream.
    headers_lc = {k.lower(): v for k, v in (headers or {}).items()}

    # 3. URL-verification handshake — runs BEFORE signature verification.
    parsed_body = _safe_parse_json(raw_body)
    signing_secret = _resolve_signing_secret(
        webhooks_block=webhooks_block,
        row=row,
    )
    uv_block = webhooks_block.get("url_verification") or {"kind": "none"}
    if uv_block.get("kind") == "verification_token_capture":
        # Notion-class: the vendor's one-time UNSIGNED token POST carries the
        # permanent signing secret in-band — dispatcher-owned (it writes the
        # row), so it never reaches the provider's handshake handler.
        capture = _handle_token_capture(
            subscription_id=subscription_id, uv_block=uv_block,
            parsed_body=parsed_body, signing_secret=signing_secret,
        )
        if capture is not None:
            return capture
    else:
        try:
            handshake = await provider.handle_url_verification(
                request_body=parsed_body if isinstance(parsed_body, dict) else {},
                query_params=query_params or {},
                manifest_uv_block=uv_block,
                signing_secret=signing_secret,
            )
        except Exception as e:
            logger.exception("url verification handler raised for provider=%s sub=%s: %s",
                             provider_id, subscription_id, e)
            handshake = None
        if handshake is not None:
            status, body, resp_headers = handshake
            return (status, body, resp_headers)

    # 4. Signature verification (skip for MS Graph GET validation handshake
    # which arrives before any signed payload — already handled above).
    if http_method == "GET":
        # GET that didn't trigger a handshake = nothing to do.
        return (200, {"status": "ok"}, {"content-type": "application/json"})

    sig_block = webhooks_block.get("signature") or {}
    verify = provider.verify_signature(
        raw_body=raw_body,
        headers=headers_lc,
        signing_secret=signing_secret,
        manifest_sig_block=sig_block,
    )
    if not verify.ok:
        if row["status"] not in ("creating",):
            webhook_subscription_store.update_subscription_status(
                subscription_id, row["status"],  # no-op transition; just to write last_error
                last_error=f"signature: {verify.reason}",
            )
        return (401, {"error": "signature verification failed", "reason": verify.reason},
                {"content-type": "application/json"})

    # 5-8. Normalize → canonicalize → gate → dedup → match → fire → aggregate.
    # Shared with the relay-forwarded ingest (which verifies the relay's
    # forward signature instead of the vendor's, then fans IN here).
    response = await _process_subscription_events(
        row=row,
        provider=provider,
        webhooks_block=webhooks_block,
        parsed_body=parsed_body,
        headers_lc=headers_lc,
    )
    return (200, response, {"content-type": "application/json"})


async def dispatch_relay_webhook(
    *,
    provider_id: str,
    raw_body: bytes,
    headers: dict[str, str],
) -> tuple[int, dict, dict[str, str]]:
    """Receive a relay-FORWARDED vendor event (hosted event delivery).

    The OtoDock relay verified the vendor's signature upstream and re-signed
    the VERBATIM body with this install's forward secret. Flow:

      1. Resolve the manifest declaring ``provider_id``.
      2. Verify the forward signature (constant ``_RELAY_SIG_BLOCK`` + the
         secret in ``infra_credentials['otodock-relay']``).
      3. Extract the workspace id via the manifest's ``workspace_id_path``.
      4. Fan IN: every relay-delivered subscription whose ``vendor_target``
         equals the workspace id (the defense against a relay mis-route)
         runs the standard pipeline.

    Always 200 once the forward signature passes; 401 only on signature
    failure (the relay drops hard-4xx forwards without retrying).
    """
    json_ct = {"content-type": "application/json"}

    manifest, webhooks_block = _manifest_for_webhook_provider(provider_id)
    if manifest is None or not webhooks_block:
        return (404, {"error": f"no manifest declares provider {provider_id!r}"},
                json_ct)
    workspace_id_path = webhooks_block.get("workspace_id_path", "")
    if not workspace_id_path:
        return (404, {"error": "provider is not relay-capable on this install"},
                json_ct)

    headers_lc = {k.lower(): v for k, v in (headers or {}).items()}
    hinted = headers_lc.get("x-otodock-event-provider", provider_id)
    if hinted != provider_id:
        return (400, {"error": "provider header mismatch"}, json_ct)

    from auth.webhook_providers.generic import GenericWebhookProvider
    from services.billing import relay_client
    from storage import credential_store

    forward_secret = (
        credential_store.get_infra_credentials(
            relay_client.EVENTS_FORWARD_SECRET_SLUG) or {}
    ).get(relay_client.EVENTS_FORWARD_SECRET_KEY, "")
    verify = GenericWebhookProvider(provider_id="relay").verify_signature(
        raw_body=raw_body,
        headers=headers_lc,
        signing_secret=forward_secret,
        manifest_sig_block=_RELAY_SIG_BLOCK,
    )
    if not verify.ok:
        return (401, {"error": "forward signature verification failed",
                      "reason": verify.reason}, json_ct)

    parsed_body = _safe_parse_json(raw_body)
    workspace_id = walk_path(
        body=parsed_body, headers=headers_lc, path=workspace_id_path,
    )
    if not workspace_id:
        return (200, {"status": "ignored", "reason": "no_workspace_id"}, json_ct)

    rows = [
        r for r in webhook_subscription_store.list_subscriptions(
            provider_id=provider_id, vendor_target=workspace_id,
            delivery_mode="relay",
        )
        if r.get("status") in ("active", "renew_failed")
    ]
    if not rows:
        return (200, {"status": "no_subscriptions", "fired": 0}, json_ct)

    try:
        provider = webhook_providers.get_provider(provider_id)
    except KeyError:
        return (500, {"error": f"unknown provider {provider_id}"}, json_ct)

    total_fired = 0
    results: list[dict[str, Any]] = []
    for row in rows:
        resp = await _process_subscription_events(
            row=row, provider=provider, webhooks_block=webhooks_block,
            parsed_body=parsed_body, headers_lc=headers_lc,
        )
        total_fired += int(resp.get("fired", 0))
        results.append({"subscription_id": row["id"], **resp})
    return (200, {"status": "ok", "fired": total_fired, "results": results},
            json_ct)


def _manifest_for_webhook_provider(provider_id: str):
    """First loaded manifest declaring ``provider_id`` in an available
    webhooks block → (manifest, webhooks_block) or (None, {})."""
    for manifest in mcp_registry.get_all_manifests().values():
        webhooks = manifest.credentials.webhooks if manifest.credentials else None
        if not webhooks or not webhooks.get("available", False):
            continue
        if webhooks.get("provider_id") != provider_id:
            continue
        return manifest, webhooks
    return None, {}


async def _process_subscription_events(
    *,
    row: dict,
    provider: Any,
    webhooks_block: dict,
    parsed_body: Any,
    headers_lc: dict[str, str],
) -> dict[str, Any]:
    """Steps 5-8 of the receive flow for ONE subscription, after the
    transport-level checks (row status, manifest, vendor/relay signature)
    have passed. Returns the JSON response body (always served as 200 —
    vendors retry on non-2xx and trigger-fire errors must not loop them)."""
    subscription_id = row["id"]
    provider_id = row.get("provider_id", "")

    # 5. Normalize payload into a list of events (batched vendors yield N>1,
    # single-event vendors yield a 1-element list via the ABC default).
    try:
        events = provider.normalize_payload_batch(
            body=parsed_body if isinstance(parsed_body, dict) else {},
            headers=headers_lc,
            manifest_block=webhooks_block,
        )
    except Exception as e:
        logger.exception(
            "normalize_payload_batch raised for provider=%s sub=%s: %s",
            provider_id, subscription_id, e,
        )
        events = []

    if not events:
        # Empty batch (vendor sent value=[] or normalize raised) — record
        # the receipt but don't fan out.
        try:
            webhook_subscription_store.record_event_received(subscription_id)
        except Exception as ex:
            logger.warning(
                "record_event_received failed for sub=%s: %s",
                subscription_id, ex,
            )
        return {"status": "no_events", "fired": 0}

    # Subscription gate input: the events the user actually selected.
    selected_events = row.get("selected_events")
    if isinstance(selected_events, str):
        try:
            selected_events = json.loads(selected_events or "[]")
        except json.JSONDecodeError:
            selected_events = []
    if not isinstance(selected_events, list):
        selected_events = []

    # 6. Per-event loop: canonicalize, gate, dedup, match triggers, fire.
    per_event_results: list[dict[str, Any]] = []
    total_fired = 0
    all_errors: list[str] = []
    duplicate_count = 0
    no_trigger_count = 0
    ignored_count = 0
    body_dict = parsed_body if isinstance(parsed_body, dict) else {}
    event_catalog = webhooks_block.get("event_catalog") or []
    for event in events:
        # Canonicalize the event type to its catalog key (slack: raw
        # "message" + channel_type=channel → "message.channels") so BOTH the
        # subscription gate and trigger event_filters work in catalog
        # vocabulary. First match wins; no match keeps the raw type.
        keys = resolve_catalog_keys(
            body=body_dict, headers=headers_lc,
            raw_event_type=event.event_type, event_catalog=event_catalog,
        )
        if keys:
            event.event_type = keys[0]

        # Gate: events outside the subscription's selection are
        # acknowledged but ignored — BEFORE dedup so the ring stays clean.
        if selected_events and event.event_type not in selected_events:
            ignored_count += 1
            logger.info(
                "webhook gate: sub=%s ignored event_type=%r (selected=%s)",
                subscription_id, event.event_type, selected_events,
            )
            per_event_results.append({
                "event_type": event.event_type,
                "status": "ignored",
                "fired": 0,
            })
            continue

        if event.vendor_event_id:
            if await _is_duplicate(subscription_id, event.vendor_event_id):
                duplicate_count += 1
                per_event_results.append({
                    "event_type": event.event_type,
                    "event_id": event.vendor_event_id,
                    "status": "duplicate",
                    "fired": 0,
                })
                continue

        matching_triggers = _find_matching_triggers(subscription_id, event)
        if not matching_triggers:
            no_trigger_count += 1
            per_event_results.append({
                "event_type": event.event_type,
                "status": "no_triggers",
                "fired": 0,
            })
            continue

        # Enrichment — manifest-driven ID→name lookups, run ONLY
        # for events that will actually fire (no vendor calls for ignored /
        # untriggered events). Fail-open: raw IDs still fire fine.
        from services.webhooks import event_enrichment
        await event_enrichment.enrich_event(
            event, row=row, webhooks_block=webhooks_block,
        )

        fired, errors = await _fan_out_fire(
            triggers=matching_triggers,
            body=body_dict,
            event=event,
            trigger_source=(
                f"webhook:{provider_id}/{subscription_id}/{event.event_type}"
            ),
        )
        total_fired += fired
        all_errors.extend(errors)
        per_event_results.append({
            "event_type": event.event_type,
            "status": "ok",
            "fired": fired,
            **({"errors": errors} if errors else {}),
        })

    # 7. Record receive housekeeping (best-effort, once per inbound request).
    try:
        webhook_subscription_store.record_event_received(subscription_id)
    except Exception as e:
        logger.warning(
            "record_event_received failed for sub=%s: %s", subscription_id, e,
        )

    # 8. Aggregate response. Single-event call sites get the legacy shape
    # (top-level event_type). Batched call sites also see per-event detail
    # in the ``events`` array.
    if total_fired > 0:
        top_status: str = "ok"
    elif duplicate_count == len(events):
        top_status = "duplicate"
    elif no_trigger_count == len(events):
        top_status = "no_triggers"
    elif ignored_count == len(events):
        top_status = "ignored"
    else:
        top_status = "ok"
    response: dict[str, Any] = {
        "status": top_status,
        "fired": total_fired,
        "event_type": events[0].event_type if len(events) == 1 else "",
    }
    if len(events) > 1:
        response["events"] = per_event_results
    if all_errors:
        response["errors"] = all_errors
    return response


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_parse_json(raw: bytes) -> Any:
    """Parse JSON body. Returns empty dict on parse failure (vendors
    occasionally send empty body for handshakes)."""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, json.JSONDecodeError):
        return {}


def _handle_token_capture(
    *, subscription_id: str, uv_block: dict, parsed_body: Any,
    signing_secret: str,
) -> tuple[int, dict, dict[str, str]] | None:
    """In-band verification-token capture (``url_verification.kind:
    "verification_token_capture"``, Notion-class vendors).

    The vendor's one-time, UNSIGNED setup POST carries the permanent signing
    secret in its body — first-writer: store it only while the row holds no
    secret (a configured secret is never overwritten by an unsigned request),
    then ACK. Re-deliveries ACK without storing. The admin reveals the
    captured token via the existing per-subscription signing-secret endpoint
    to complete the vendor's Verify dialog. The token itself is NEVER logged.

    Returns None when the body isn't a token POST (normal events fall through
    to signature verification).
    """
    if not isinstance(parsed_body, dict):
        return None
    field = uv_block.get("request_field") or ""
    token = parsed_body.get(field) if field else None
    if not isinstance(token, str) or not token:
        return None
    if not signing_secret:
        webhook_subscription_store.update_signing_secret(subscription_id, token)
        logger.info(
            "webhook verification token captured for subscription %s — reveal "
            "it on the subscription card to verify at the vendor",
            subscription_id,
        )
    response_field = uv_block.get("response_field") or "ok"
    content_type = uv_block.get("response_content_type") or "application/json"
    return (200, {response_field: True}, {"content-type": content_type})


def _resolve_signing_secret(*, webhooks_block: dict, row: dict) -> str:
    """Resolve the signing secret per the manifest's per-subscription-vs-platform
    flag.

    Per-subscription: read from ``webhook_subscriptions.signing_secret_enc``
    via the store (decrypted).

    Platform-wide: read from ``infra_credentials`` using
    ``signature.secret_credential_key``. The admin app-credentials form
    stores under the manifest's ``credentials.oauth.app_credential`` slug
    (e.g. ``slack-oauth-app``) — read that bundle first, falling back to
    the MCP name for manifests without an oauth block.
    """
    sig_block = webhooks_block.get("signature", {})
    if sig_block.get("per_subscription_secret", False):
        secret = webhook_subscription_store.get_signing_secret(row["id"])
        return secret or ""
    key = sig_block.get("secret_credential_key", "")
    if not key:
        return ""
    from services.mcp import mcp_registry
    from storage import credential_store
    manifest = mcp_registry.get_manifest(row["mcp_name"])
    oauth_block = (manifest.credentials.oauth or {}) if manifest else {}
    for slug in (oauth_block.get("app_credential", ""), row["mcp_name"]):
        if not slug:
            continue
        creds = credential_store.get_infra_credentials(slug) or {}
        if creds.get(key):
            return creds[key]
    return ""


async def _is_duplicate(subscription_id: str, event_id: str) -> bool:
    """Check + record event_id in the dedup ring."""
    key = f"{subscription_id}:{event_id}"
    now = time.monotonic()
    async with _DEDUP_LOCK:
        # Evict stale entries opportunistically (TTL prune).
        while _DEDUP_RING and (now - _DEDUP_RING[0][1]) > _DEDUP_TTL_SECONDS:
            _DEDUP_RING.popleft()
        for k, _ts in _DEDUP_RING:
            if k == key:
                return True
        _DEDUP_RING.append((key, now))
        return False


def _find_matching_triggers(
    subscription_id: str, event: NormalizedEvent,
) -> list[dict]:
    """Read all enabled triggers for this subscription; return those whose
    event_filter matches the event."""
    rows = trigger_store.list_triggers(enabled_only=True)
    matches: list[dict] = []
    for row in rows:
        if row.get("subscription_id") != subscription_id:
            continue
        event_filter = row.get("event_filter")
        # Tolerate JSONB returning as str or already-dict.
        if isinstance(event_filter, str):
            try:
                event_filter = json.loads(event_filter or "{}")
            except json.JSONDecodeError:
                event_filter = {}
        if not isinstance(event_filter, dict):
            event_filter = {}
        if match_event_filter(event=event, event_filter=event_filter):
            matches.append(row)
    return matches


async def _fan_out_fire(
    *,
    triggers: list[dict],
    body: dict,
    event: NormalizedEvent,
    trigger_source: str,
) -> tuple[int, list[str]]:
    """Fire all matching triggers concurrently. Returns (fired_count, errors)."""
    # Lazy import to avoid circular: trigger_manager imports scheduler which
    # imports a lot.
    from services.scheduler import trigger_manager

    async def fire_one(trigger_row: dict) -> str | None:
        try:
            await trigger_manager.fire_trigger(
                trigger_row, body,
                trigger_source=trigger_source,
                vendor_event=event,
            )
            return None
        except Exception as e:
            return f"trigger {trigger_row.get('id')}: {type(e).__name__}: {e}"

    results = await asyncio.gather(
        *[fire_one(t) for t in triggers],
        return_exceptions=False,
    )
    errors = [r for r in results if r]
    fired = len(triggers) - len(errors)
    return (fired, errors)
