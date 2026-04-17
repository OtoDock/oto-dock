"""WebhookProvider ABC — vendor-neutral inbound webhook contract.

A concrete provider (``GitHubWebhookProvider``, ``SlackWebhookProvider``,
etc.) implements vendor-specific signature/handshake quirks. Most providers
can use ``GenericWebhookProvider`` which reads everything from the manifest.

The provider registry (``webhook_providers/__init__.py``) returns the right
instance for a ``provider_id`` at runtime. Callers should never instantiate
concrete classes directly.

This module is parallel to ``oauth_providers/base.py`` — the two systems
serve DIFFERENT layers (outbound auth vs inbound signature verification)
and don't share base classes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class NormalizedEvent:
    """Vendor-neutral event shape returned by ``normalize_payload``.

    Drives the ``${trigger.*}`` token namespace at session-build time AND
    the ``event_filter`` matcher at fan-out time. Manifests declare per-field
    JSONPath-style paths into the inbound payload; ``normalize_payload``
    walks them and fills this struct.

    Missing paths yield empty strings (never raises). Callers can safely
    read any field without None-checks.
    """

    #: Vendor's event type (e.g. ``"pull_request"`` / ``"message.channels"``).
    #: Drives the primary ``event_filter`` key.
    event_type: str = ""

    #: Vendor-side event uuid for dedup (X-GitHub-Delivery, Slack body.event_id).
    vendor_event_id: str = ""

    #: Who triggered the event (the human / bot).
    #: Keys: id, email, name, url. All strings; missing fields = "".
    actor: dict[str, str] = field(default_factory=dict)

    #: What was acted on (the PR / issue / message). type may be a free-text
    #: vendor verb (``"opened"`` for GitHub PR actions); id/title/url identify it.
    subject: dict[str, str] = field(default_factory=dict)

    #: Where it happened (repository / channel / drive). id is the
    #: vendor-side identifier; type often a manifest constant.
    target: dict[str, str] = field(default_factory=dict)


@dataclass
class VerifyResult:
    """Result of ``verify_signature``. ``ok=False`` carries a machine-readable
    reason for ops logs + ``webhook_subscriptions.last_error`` tracking.
    """
    ok: bool
    reason: str = ""  # missing_header | malformed_header | timestamp_too_old |
                     # signature_mismatch | unsupported_algorithm | ""


class WebhookProvider(ABC):
    """Vendor-neutral inbound webhook receiver contract.

    Concrete providers are stateless — the dispatcher passes the manifest
    block + per-request raw bytes/headers in on every call. Implementations
    use the standard library for HMAC and `hmac.compare_digest` for
    constant-time comparison.
    """

    #: Manifest-level identifier (``"github"``, ``"slack"``, ``"linear"``).
    provider_id: str = ""

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    @abstractmethod
    def verify_signature(
        self,
        *,
        raw_body: bytes,
        headers: dict[str, str],
        signing_secret: str,
        manifest_sig_block: dict,
    ) -> VerifyResult:
        """Verify the inbound request's HMAC signature.

        ``raw_body`` MUST be the byte-exact original request body — for
        timestamp-binding schemes (Slack), the signed payload includes a
        prefix that wraps the body, and any re-serialization would change
        the digest. The dispatcher always passes the raw bytes from the
        ASGI request.

        ``headers`` keys are lowercased by the dispatcher (HTTP headers are
        case-insensitive).

        ``signing_secret`` is the resolved per-subscription or
        infra-credential secret (the dispatcher routes via
        ``manifest_sig_block["per_subscription_secret"]``).

        ``manifest_sig_block`` is the ``credentials.webhooks.signature``
        dict — providers read fields they care about and ignore the rest.

        Returns ``VerifyResult(ok=True)`` on success.
        """

    # ------------------------------------------------------------------
    # URL verification handshake
    # ------------------------------------------------------------------

    @abstractmethod
    async def handle_url_verification(
        self,
        *,
        request_body: dict,
        query_params: dict[str, str],
        manifest_uv_block: dict,
        signing_secret: str,
    ) -> tuple[int, str, dict[str, str]] | None:
        """Detect + respond to a vendor's URL-verification handshake.

        Called BEFORE ``verify_signature`` (most vendors send the handshake
        unsigned or sign the response separately).

        Returns ``(status_code, body, headers)`` to send back as the HTTP
        response, OR ``None`` if this request is NOT a handshake (the
        dispatcher then proceeds to signature verification + event fan-out).

        ``signing_secret`` is provided for vendors (Zoom) that derive the
        handshake response from the secret.

        Implementation is async because Zoom requires an HMAC over the
        plaintext token; the standard library implementations are still
        synchronous internally, but the async signature lets future vendors
        do IO if needed.
        """

    # ------------------------------------------------------------------
    # Event-id extraction (for idempotency dedup)
    # ------------------------------------------------------------------

    @abstractmethod
    def extract_event_id(
        self,
        *,
        body: dict,
        headers: dict[str, str],
        manifest_block: dict,
    ) -> str:
        """Pull the dedup key out of the inbound payload.

        Returns empty string when the manifest declares no ``event_id_field``
        or the path doesn't resolve. The dispatcher's dedup ring ignores
        empty keys (no dedup possible for vendors that don't tag events).
        """

    # ------------------------------------------------------------------
    # Payload normalization
    # ------------------------------------------------------------------

    @abstractmethod
    def normalize_payload(
        self,
        *,
        body: dict,
        headers: dict[str, str],
        manifest_block: dict,
    ) -> NormalizedEvent:
        """Translate the vendor's payload into a ``NormalizedEvent``.

        Reads the manifest's ``payload_normalization`` block and walks each
        declared path. Missing paths yield empty strings. The result drives
        both the ``${trigger.*}`` token namespace AND the per-trigger
        ``event_filter`` matching.
        """

    # ------------------------------------------------------------------
    # Batched payload normalization (vendors that pack multiple events
    # into one inbound POST — MS Graph's ``body.value[]`` envelope)
    # ------------------------------------------------------------------

    def normalize_payload_batch(
        self,
        *,
        body: dict,
        headers: dict[str, str],
        manifest_block: dict,
    ) -> list[NormalizedEvent]:
        """Return one ``NormalizedEvent`` per logical event in the request.

        Single-event vendors (GitHub, Slack, Linear, Zoom) inherit this
        default which wraps the singular ``normalize_payload`` in a
        one-element list. Vendors that batch (MS Graph) override directly
        and iterate ``body.value[]``.

        The dispatcher always calls THIS method (not ``normalize_payload``
        directly) so per-event dedup + fan-out works uniformly regardless
        of single vs batched delivery.
        """
        return [self.normalize_payload(
            body=body, headers=headers, manifest_block=manifest_block,
        )]
