"""GitHub webhook provider — fits ``GenericWebhookProvider`` entirely.

GitHub webhooks use HMAC-SHA256 of the raw body, ``X-Hub-Signature-256``
header with ``sha256=`` prefix, NO timestamp/replay window, and no URL
verification handshake (the create call's 201 response is the only "I'm
listening" signal). Per-repo signing secrets stored in
``webhook_subscriptions.signing_secret_enc``.

This subclass exists as documentation that we've evaluated GitHub's quirks
and confirmed generic handles them. Future quirks (re-redelivery handling,
``ping`` event filtering, GitHub App-style webhooks with different signing)
get added here instead of polluting ``generic.py``.
"""

from __future__ import annotations

from auth.webhook_providers.generic import GenericWebhookProvider


class GitHubWebhookProvider(GenericWebhookProvider):
    def __init__(self):
        super().__init__(provider_id="github")
