"""Microsoft Graph webhook provider.

MS Graph doesn't HMAC-sign payloads. Verification is via ``clientState``
echo: we set the value at subscription create time (using our per-
subscription signing secret), Graph echoes it back in every notification,
and we ``hmac.compare_digest`` on receipt.

Two structural differences from the generic provider:

* **No header-based signature.** Manifest declares
  ``signature.algorithm = "client_state_echo"`` so the validator skips
  the non-empty-header rule. The generic ``verify_signature`` rejects
  ``client_state_echo`` with ``unsupported_algorithm`` — this subclass
  IS the verification path.

* **Batched notifications.** A single inbound POST can carry many
  events in ``body.value[]``. We override ``normalize_payload_batch``
  to yield one ``NormalizedEvent`` per item. The dispatcher loops over
  the list for dedup + fan-out.

URL validation handshake (`ms_graph_validation_token`), event-id
extraction, and the singular ``normalize_payload`` (used as a fallback)
are all inherited from ``GenericWebhookProvider``.
"""

from __future__ import annotations

import hmac
import json
import logging

from auth.webhook_providers.base import NormalizedEvent, VerifyResult
from auth.webhook_providers.generic import GenericWebhookProvider
from services.webhooks.event_normalizer import normalize_event

logger = logging.getLogger("claude-proxy.webhook-providers.microsoft")


class MicrosoftWebhookProvider(GenericWebhookProvider):
    def __init__(self):
        super().__init__(provider_id="microsoft")

    # ------------------------------------------------------------------
    # Signature verification — clientState echo across all batch items
    # ------------------------------------------------------------------

    def verify_signature(
        self,
        *,
        raw_body: bytes,
        headers: dict[str, str],
        signing_secret: str,
        manifest_sig_block: dict,
    ) -> VerifyResult:
        """Compare ``value[].clientState`` against the stored signing secret.

        Every item in the batch MUST carry the same clientState (Graph
        guarantees this — one subscription per request). A single
        mismatch fails the whole request: MS Graph retries failed
        requests for up to 4 hours, so a transient mismatch from a stale
        request lands in last_error for the dashboard to surface.

        Returns ``ok=True`` only when the body is well-formed JSON with
        a non-empty ``value`` array AND every item's clientState matches.
        """
        try:
            body = json.loads(raw_body.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            return VerifyResult(False, reason="malformed_body")
        if not isinstance(body, dict):
            return VerifyResult(False, reason="malformed_body")
        items = body.get("value")
        if not isinstance(items, list) or not items:
            return VerifyResult(False, reason="no_value_array")
        if not signing_secret:
            # Defensive: empty signing_secret means the row's secret
            # was never set OR Fernet decryption failed. Don't accept
            # anything (an attacker forging an empty clientState would
            # otherwise pass).
            return VerifyResult(False, reason="missing_secret")
        for item in items:
            if not isinstance(item, dict):
                return VerifyResult(False, reason="client_state_mismatch")
            cs = item.get("clientState", "")
            if not isinstance(cs, str):
                return VerifyResult(False, reason="client_state_mismatch")
            if not hmac.compare_digest(cs, signing_secret):
                return VerifyResult(False, reason="client_state_mismatch")
        return VerifyResult(True)

    # ------------------------------------------------------------------
    # Batched normalization — one NormalizedEvent per value[] item
    # ------------------------------------------------------------------

    def normalize_payload_batch(
        self,
        *,
        body: dict,
        headers: dict[str, str],
        manifest_block: dict,
    ) -> list[NormalizedEvent]:
        """Yield one NormalizedEvent per item in body.value[].

        Each item is its own logical event — distinct ``changeType``,
        ``resourceData``, and ``id`` (delivery uuid). The manifest's
        ``payload_normalization`` paths are written against a SINGLE
        item (not the batched wrapper), so we walk each item directly.
        """
        items = body.get("value") if isinstance(body, dict) else []
        if not isinstance(items, list):
            items = []
        norm_block = manifest_block.get("payload_normalization") or {}
        out: list[NormalizedEvent] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            # Per-item event id for dedup. MS Graph change notifications have
            # NO top-level per-delivery id — the changed item's id lives in
            # ``resourceData.id``. Keying on ``{resourceData.id}:{changeType}``
            # dedups true redeliveries (Graph resends the same item+change on
            # our non-2xx) while still letting "created" and "updated" for the
            # same item each fire. The old ``{subscriptionId}:{idx}`` fallback
            # collapsed to ``{subId}:0`` for every single-item notification, so
            # everything after the first on a subscription was wrongly deduped
            # for the whole 10-min ring window.
            rdata = item.get("resourceData")
            rid = rdata.get("id") if isinstance(rdata, dict) else ""
            change = str(item.get("changeType", "") or "")
            event_id = (
                str(item.get("id") or "")
                or (f"{rid}:{change}" if rid else "")
                or f"{item.get('subscriptionId', '')}:{idx}"
            )
            ev = normalize_event(
                body=item,
                headers=headers,
                manifest_block=norm_block,
                vendor_event_id=event_id,
            )
            # Canonicalize the per-item type to its catalog key. Graph items
            # carry changeType ("created"/"updated") as the raw type, while
            # the catalog (subscription gate + trigger event_filters) speaks
            # subscription names ("calendar_events"). The dispatcher's
            # request-level resolve_catalog_keys can't see per-item context
            # in a batched body, so the mapping happens here, keyed on the
            # item's `resource` string ("Users/{id}/Events/{id}" matches
            # entry.resource_contains "/events"). First catalog match wins;
            # no match keeps the raw type (the gate then ignores it).
            resource = str(item.get("resource", "") or "").lower()
            matched_key = ""
            if resource:
                for entry in manifest_block.get("event_catalog") or []:
                    if not isinstance(entry, dict):
                        continue
                    rc = str(entry.get("resource_contains", "") or "").lower()
                    if rc and rc in resource and entry.get("key"):
                        matched_key = str(entry["key"])
                        ev.event_type = matched_key
                        break
            if not matched_key:
                # No catalog entry pairs with this resource — the event keeps
                # its raw changeType and the dispatcher's selected-events gate
                # will ignore it. Log it (no payload, resource path only) so an
                # unmapped resource surfaces in ops instead of silently
                # vanishing.
                logger.warning(
                    "microsoft webhook: unmapped resource %r (changeType=%s) "
                    "— event will be gated out; add a resource_contains entry",
                    resource, change,
                )
            out.append(ev)
        return out
