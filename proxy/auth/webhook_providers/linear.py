"""Linear webhook provider — stub.

Linear signs with HMAC-SHA256 over the raw body, ``Linear-Signature``
header, per-subscription signing secret. No URL handshake.
Generic provider handles everything from the manifest.

The stub keeps the registry hook in place; wiring it up is just adding the
manifest and (once tested) flipping ``manifest.webhooks.available``.
"""

from __future__ import annotations

from auth.webhook_providers.generic import GenericWebhookProvider


class LinearWebhookProvider(GenericWebhookProvider):
    def __init__(self):
        super().__init__(provider_id="linear")
