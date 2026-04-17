"""Slack webhook provider — stub.

Slack signs with HMAC-SHA256 over ``v0:{timestamp}:{body}`` (replay
protection via ``X-Slack-Request-Timestamp``), uses one platform-wide
signing secret stored in ``infra_credentials.SLACK_SIGNING_SECRET``, and
sends a ``slack_challenge`` handshake on first POST.

The stub keeps the registry hook in place; auto-registration via
``apps.manifest.update`` can be added later (or keep mode=manual until
that's wired). Generic provider handles signature + handshake from the
manifest today.
"""

from __future__ import annotations

from auth.webhook_providers.generic import GenericWebhookProvider


class SlackWebhookProvider(GenericWebhookProvider):
    def __init__(self):
        super().__init__(provider_id="slack")
