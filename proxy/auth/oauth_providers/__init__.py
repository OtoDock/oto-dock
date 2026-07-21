"""OAuth provider registry — resolves provider_id → OAuthProvider instance.

Construction strategy:
  1. Hardcoded Python subclasses for providers with quirks (Google today;
     Slack/Microsoft/Zoom S2S when added) — singletons registered at
     module import time.
  2. Manifest-driven ``GenericOAuthProvider`` for any provider_id seen in a
     loaded manifest's ``credentials.oauth`` block that doesn't have a
     hardcoded class. Built lazily on first ``get_provider()`` call from
     the manifest URLs.

Callers:
  ``provider = get_provider("google")`` → ``GoogleOAuthProvider``
  ``provider = get_provider("linear")`` → ``GenericOAuthProvider`` (from manifest)

Raises ``KeyError`` if the provider_id is unknown AND no manifest declares it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auth.oauth_providers.base import OAuthProvider

logger = logging.getLogger("claude-proxy.oauth-providers")


# Hardcoded provider singletons. Populated at import time below.
_HARDCODED: dict[str, "OAuthProvider"] = {}

# Lazy cache for manifest-derived GenericOAuthProvider instances. Cleared
# whenever manifests are rescanned (callable from mcp_registry).
_MANIFEST_CACHE: dict[str, "OAuthProvider"] = {}


def _register_hardcoded() -> None:
    """Import + register hardcoded provider classes."""
    from auth.oauth_providers.google import GoogleOAuthProvider
    from auth.oauth_providers.slack import SlackOAuthProvider
    from auth.oauth_providers.microsoft import MicrosoftOAuthProvider
    from auth.oauth_providers.zoom import ZoomOAuthProvider
    _HARDCODED["google"] = GoogleOAuthProvider()
    _HARDCODED["slack"] = SlackOAuthProvider()
    _HARDCODED["microsoft"] = MicrosoftOAuthProvider()
    _HARDCODED["zoom"] = ZoomOAuthProvider()


_register_hardcoded()


def clear_manifest_cache() -> None:
    """Drop cached manifest-derived providers.

    Call after ``mcp_registry.scan_manifests()`` if a manifest's oauth
    URLs changed. Today URLs are static per provider so this is mostly
    defensive — keep it cheap.
    """
    _MANIFEST_CACHE.clear()


def get_provider(provider_id: str) -> "OAuthProvider":
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
        f"OAuth provider '{provider_id}' has no hardcoded class and no "
        f"manifest declares it. Add it to a community MCP's manifest "
        f"credentials.oauth block or write a class in auth/oauth_providers/."
    )


def list_provider_ids() -> list[str]:
    """All known provider_ids (hardcoded + currently-cached manifest-derived).

    Note: manifest-derived providers may not appear until ``get_provider``
    has been called for them once (lazy build).
    """
    return sorted(set(_HARDCODED) | set(_MANIFEST_CACHE))


def canonical_provider_id(provider_id: str) -> str:
    """Validate ``provider_id`` and return the registry's own copy of it.

    The returned string is drawn from the registry's key set, not from the
    caller's input, so a request-derived id can be reflected into HTML or
    redirect responses without carrying request taint. Raises ``KeyError``
    for unknown ids (same contract as ``get_provider``).
    """
    get_provider(provider_id)  # KeyError if unknown; populates the lazy cache
    for known in (*_HARDCODED, *_MANIFEST_CACHE):
        if known == provider_id:
            return known
    raise KeyError(provider_id)


def _build_from_manifest(provider_id: str) -> "OAuthProvider | None":
    """Scan loaded manifests for the first one whose oauth.provider_id matches.

    Returns a ``GenericOAuthProvider`` built from that manifest's URL fields,
    or None if no manifest declares this provider.
    """
    from services.mcp import mcp_registry
    from auth.oauth_providers.generic import GenericOAuthProvider

    for manifest in mcp_registry.get_all_manifests().values():
        oauth = manifest.credentials.oauth if manifest.credentials else None
        if not oauth:
            continue
        if oauth.get("provider_id") != provider_id:
            continue
        authorization_url = oauth.get("authorization_url", "")
        token_url = oauth.get("token_url", "")
        if not authorization_url or not token_url:
            logger.warning(
                "Manifest %s declares provider_id=%s but is missing "
                "authorization_url/token_url; cannot build provider.",
                manifest.name, provider_id,
            )
            continue
        return GenericOAuthProvider(
            provider_id=provider_id,
            authorization_url=authorization_url,
            token_url=token_url,
            revoke_url=oauth.get("revoke_url", ""),
            userinfo_url=oauth.get("userinfo_url", ""),
            userinfo_email_field=oauth.get("userinfo_email_field", "email"),
            userinfo_name_field=oauth.get("userinfo_name_field", "name"),
            userinfo_id_field=oauth.get("userinfo_id_field", "sub"),
            userinfo_headers=oauth.get("userinfo_headers") or {},
            userinfo_method=oauth.get("userinfo_method", "GET"),
            userinfo_body=oauth.get("userinfo_body") or None,
            # Provider's default flow = first declared in the manifest's
            # `flows` list. Multi-flow MCPs (github-mcp) pick alternatives at
            # connect time via the dashboard's flow picker.
            flow=(oauth.get("flows") or ["authorization_code"])[0],
            device_authorization_url=oauth.get("device_authorization_url", ""),
        )
    return None
