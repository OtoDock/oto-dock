"""Webhook provider registry — resolves provider_id → WebhookProvider instance.

Vendor-webhook receiver framework. Parallel to ``oauth_providers/``
but for INBOUND HTTP requests from vendors (Slack signing, GitHub HMAC,
MS Graph validationToken, etc.).

Construction strategy:
  1. Hardcoded Python subclasses for vendors with signature/handshake quirks
     that don't fit the generic manifest-driven path. Today: github (stub),
     slack, microsoft, zoom — all subclass ``GenericWebhookProvider`` and
     override only what they need.
  2. Manifest-driven ``GenericWebhookProvider`` for any provider_id seen in a
     loaded manifest's ``credentials.webhooks`` block that doesn't have a
     hardcoded class. Built lazily on first ``get_provider()`` call.

Callers:
  ``provider = get_provider("github")`` → ``GitHubWebhookProvider``
  ``provider = get_provider("linear")`` → ``GenericWebhookProvider`` (from manifest)

Raises ``KeyError`` if the provider_id is unknown AND no manifest declares it.

This module is independent of the OAuth provider registry — the two systems
serve different layers (OUTBOUND auth vs INBOUND signature verification).
A vendor MCP typically declares both (oauth + webhooks) with matching
provider_id; the cross-check happens in ``mcp_registry._parse_manifest``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auth.webhook_providers.base import WebhookProvider

logger = logging.getLogger("claude-proxy.webhook-providers")


_HARDCODED: dict[str, "WebhookProvider"] = {}
_MANIFEST_CACHE: dict[str, "WebhookProvider"] = {}


def _register_hardcoded() -> None:
    """Import + register hardcoded provider classes.

    Each vendor lives in its own module under ``auth/webhook_providers/``.
    Stubs for slack/linear/microsoft/zoom sit alongside the
    fully-implemented GitHub provider; each vendor's overrides get fleshed
    out as those credentials become testable.
    """
    from auth.webhook_providers.github import GitHubWebhookProvider
    from auth.webhook_providers.slack import SlackWebhookProvider
    from auth.webhook_providers.linear import LinearWebhookProvider
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    from auth.webhook_providers.zoom import ZoomWebhookProvider
    _HARDCODED["github"] = GitHubWebhookProvider()
    _HARDCODED["slack"] = SlackWebhookProvider()
    _HARDCODED["linear"] = LinearWebhookProvider()
    _HARDCODED["microsoft"] = MicrosoftWebhookProvider()
    _HARDCODED["zoom"] = ZoomWebhookProvider()


_register_hardcoded()


def clear_manifest_cache() -> None:
    """Drop cached manifest-derived providers.

    Call after ``mcp_registry.scan_manifests()`` if a manifest's webhook
    config changed. Manifest URLs are mostly static so this is defensive.
    """
    _MANIFEST_CACHE.clear()


def get_provider(provider_id: str) -> "WebhookProvider":
    """Resolve a provider by id.

    Lookup order: hardcoded singletons → manifest-derived cache → manifest
    scan + cache. Raises ``KeyError`` if no provider matches.
    """
    if provider_id in _HARDCODED:
        return _HARDCODED[provider_id]
    if provider_id in _MANIFEST_CACHE:
        return _MANIFEST_CACHE[provider_id]
    p = _build_from_manifest(provider_id)
    if p is not None:
        _MANIFEST_CACHE[provider_id] = p
        return p
    raise KeyError(
        f"Webhook provider '{provider_id}' has no hardcoded class and no "
        f"manifest declares it. Add a credentials.webhooks block to a "
        f"community MCP's manifest (with provider_id={provider_id!r}) or "
        f"write a class in auth/webhook_providers/."
    )


def list_provider_ids() -> list[str]:
    """All known provider_ids (hardcoded + currently-cached manifest-derived).

    Note: manifest-derived providers may not appear until ``get_provider``
    has been called for them once (lazy build).
    """
    return sorted(set(_HARDCODED) | set(_MANIFEST_CACHE))


def _build_from_manifest(provider_id: str) -> "WebhookProvider | None":
    """Scan loaded manifests for the first one declaring this provider_id.

    Returns a ``GenericWebhookProvider`` bound to that manifest, or None if
    no manifest declares this provider.
    """
    from services.mcp import mcp_registry
    from auth.webhook_providers.generic import GenericWebhookProvider

    for manifest in mcp_registry.get_all_manifests().values():
        webhooks = manifest.credentials.webhooks if manifest.credentials else None
        if not webhooks or not webhooks.get("available", False):
            continue
        if webhooks.get("provider_id") != provider_id:
            continue
        return GenericWebhookProvider(provider_id=provider_id)
    return None
