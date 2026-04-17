"""GenericWebhookProvider — manifest-driven inbound webhook receiver.

Reads the vendor MCP's ``credentials.webhooks`` manifest block to implement
all four ``WebhookProvider`` ABC methods without vendor-specific code.
GitHub/Linear/most-third-parties fit this completely; vendors with novel
handshakes (Slack, MS Graph, Zoom) subclass and override only what differs.

The dispatcher passes the raw request bytes + lowercased headers + manifest
block on every call. This class is stateless.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime

from services.webhooks.event_normalizer import normalize_event, walk_path

from auth.webhook_providers.base import (
    NormalizedEvent,
    VerifyResult,
    WebhookProvider,
)

logger = logging.getLogger("claude-proxy.webhook-providers.generic")


_ALGORITHMS = {
    "hmac-sha1": hashlib.sha1,
    "hmac-sha256": hashlib.sha256,
    "hmac-sha512": hashlib.sha512,
}


class GenericWebhookProvider(WebhookProvider):
    """Manifest-driven receiver. Use directly for clean OAuth-style vendors;
    subclass to override per-vendor quirks.
    """

    def __init__(self, *, provider_id: str = ""):
        self.provider_id = provider_id

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def verify_signature(
        self,
        *,
        raw_body: bytes,
        headers: dict[str, str],
        signing_secret: str,
        manifest_sig_block: dict,
    ) -> VerifyResult:
        algo_name = manifest_sig_block.get("algorithm", "hmac-sha256")
        algo = _ALGORITHMS.get(algo_name)
        if algo is None:
            return VerifyResult(False, reason="unsupported_algorithm")

        sig_header_name = manifest_sig_block.get("header", "").lower()
        if not sig_header_name:
            return VerifyResult(False, reason="missing_header")
        sig_header_val = headers.get(sig_header_name, "")
        if not sig_header_val:
            return VerifyResult(False, reason="missing_header")

        # Strip optional prefix (e.g., GitHub `sha256=`).
        prefix = manifest_sig_block.get("prefix", "")
        if prefix and sig_header_val.startswith(prefix):
            sig_value = sig_header_val[len(prefix):]
        else:
            sig_value = sig_header_val

        # Slack-style version prefix appears INSIDE the digest portion
        # (e.g., header = "v0=abc..."). Some vendors use both prefix +
        # version_prefix; manifests declare both.
        version_prefix = manifest_sig_block.get("version_prefix", "")
        if version_prefix and sig_value.startswith(version_prefix):
            sig_value = sig_value[len(version_prefix):]

        # Timestamp window check (replay protection — Slack, Zoom).
        timestamp = ""
        ts_header_name = manifest_sig_block.get("timestamp_header", "").lower()
        if ts_header_name:
            timestamp = headers.get(ts_header_name, "")
            if not timestamp:
                return VerifyResult(False, reason="missing_header")
            max_age = manifest_sig_block.get("max_age_seconds", 300)
            ts_format = manifest_sig_block.get("timestamp_format", "unix")
            try:
                ts_unix = _parse_timestamp(timestamp, ts_format)
            except (ValueError, OverflowError):
                return VerifyResult(False, reason="malformed_header")
            if max_age > 0 and abs(time.time() - ts_unix) > max_age:
                return VerifyResult(False, reason="timestamp_too_old")

        # Build the signed payload per manifest template.
        # Default template = body only (GitHub). Slack: `v0:{timestamp}:{body}`.
        payload_template = manifest_sig_block.get("signed_payload_template", "{body}")
        body_str = raw_body.decode("utf-8", errors="replace")
        signed_payload = (
            payload_template
            .replace("{body}", body_str)
            .replace("{timestamp}", timestamp)
        )

        # Compute HMAC and compare.
        secret_bytes = (signing_secret or "").encode("utf-8")
        if not secret_bytes:
            # Empty signing secret is a config error (manifest or admin oversight).
            # We reject rather than accept (a vendor that signs with "" would let
            # anyone forge requests).
            return VerifyResult(False, reason="signature_mismatch")
        expected = hmac.new(
            secret_bytes,
            signed_payload.encode("utf-8"),
            algo,
        ).hexdigest()

        if not hmac.compare_digest(expected, sig_value):
            return VerifyResult(False, reason="signature_mismatch")
        return VerifyResult(True)

    # ------------------------------------------------------------------
    # URL handshake — manifest's `url_verification.kind` discriminator
    # ------------------------------------------------------------------

    async def handle_url_verification(
        self,
        *,
        request_body: dict,
        query_params: dict[str, str],
        manifest_uv_block: dict,
        signing_secret: str,
    ) -> tuple[int, str, dict[str, str]] | None:
        kind = manifest_uv_block.get("kind", "none")

        if kind == "none":
            return None

        if kind == "slack_challenge":
            # Slack sends `{"type": "url_verification", "challenge": "abc..."}`.
            # Detect by presence of the challenge field — not by type=='url_verification'
            # because Slack also uses the body shape for event payloads.
            if not isinstance(request_body, dict):
                return None
            challenge = request_body.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                return None
            content_type = manifest_uv_block.get(
                "response_content_type", "application/json"
            )
            response_field = manifest_uv_block.get("response_field", "challenge")
            if response_field == "plain_text":
                return (200, challenge, {"content-type": "text/plain"})
            payload = {response_field: challenge}
            return (200, json.dumps(payload), {"content-type": content_type})

        if kind == "ms_graph_validation_token":
            # MS Graph: GET (or POST) with ?validationToken=xyz. Reply 200
            # text/plain with the token value, within 10 seconds.
            param = manifest_uv_block.get("request_field", "validationToken")
            token = query_params.get(param, "")
            if not token:
                return None
            return (200, token, {"content-type": "text/plain"})

        if kind == "zoom_endpoint_validation":
            # Zoom: body shape `{"event": "endpoint.url_validation",
            # "payload": {"plainToken": "..."}}`. Respond with
            # `{plainToken, encryptedToken}` where encryptedToken =
            # HMAC-SHA256(secret, plainToken).hex().
            if not isinstance(request_body, dict):
                return None
            if request_body.get("event") != "endpoint.url_validation":
                return None
            payload = request_body.get("payload") or {}
            plain = payload.get("plainToken", "")
            if not plain:
                return None
            secret_bytes = (signing_secret or "").encode("utf-8")
            encrypted = hmac.new(
                secret_bytes,
                plain.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            content_type = manifest_uv_block.get(
                "response_content_type", "application/json"
            )
            return (
                200,
                json.dumps({"plainToken": plain, "encryptedToken": encrypted}),
                {"content-type": content_type},
            )

        # Unknown kind — caller already validated the manifest, so this
        # shouldn't happen. Defensive.
        logger.warning("Unknown url_verification.kind=%r for provider=%s",
                       kind, self.provider_id)
        return None

    # ------------------------------------------------------------------
    # Event-id extraction
    # ------------------------------------------------------------------

    def extract_event_id(
        self,
        *,
        body: dict,
        headers: dict[str, str],
        manifest_block: dict,
    ) -> str:
        path = manifest_block.get("event_id_field", "")
        if not path:
            return ""
        return walk_path(body=body, headers=headers, path=path)

    # ------------------------------------------------------------------
    # Payload normalization
    # ------------------------------------------------------------------

    def normalize_payload(
        self,
        *,
        body: dict,
        headers: dict[str, str],
        manifest_block: dict,
    ) -> NormalizedEvent:
        norm_block = manifest_block.get("payload_normalization") or {}
        event_id = self.extract_event_id(
            body=body, headers=headers, manifest_block=manifest_block,
        )
        return normalize_event(
            body=body,
            headers=headers,
            manifest_block=norm_block,
            vendor_event_id=event_id,
        )


def _parse_timestamp(value: str, fmt: str) -> float:
    """Parse a vendor's timestamp string into a Unix-seconds float."""
    if fmt == "unix":
        return float(value)
    if fmt == "unix_ms":
        return float(value) / 1000.0
    if fmt == "iso8601":
        # Accept both `Z`-suffixed and offset-bearing variants.
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    raise ValueError(f"unsupported timestamp_format: {fmt!r}")
