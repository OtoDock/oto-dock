"""Zoom webhook provider — stub.

Zoom sends ``endpoint.url_validation`` events that the receiver must
respond to with ``{plainToken, encryptedToken = HMAC-SHA256(secret, plainToken).hex()}``
to prove possession of the secret. Normal event payloads are HMAC-SHA256
signed over ``v0:{timestamp}:{body}`` with the same secret —
generic provider already handles this.

The stub keeps the registry hook in place.
"""

from __future__ import annotations

from auth.webhook_providers.generic import GenericWebhookProvider


class ZoomWebhookProvider(GenericWebhookProvider):
    def __init__(self):
        super().__init__(provider_id="zoom")
