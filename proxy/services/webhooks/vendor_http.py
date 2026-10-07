"""The URL check for every call the webhook layer makes to a vendor's API
(subscription create, renew and delete; event enrichment): the URL comes
from a catalog manifest's template with values substituted in, so it is
built and checked here before any request leaves.

* Values are URL-encoded where they land (``url_from_template``): nothing a
  value carries can change the scheme, the host or another part of the URL.
  ``vendor_target`` (GitHub's ``owner/repo``) keeps its slashes between
  segments, and an empty, ``.`` or ``..`` segment is refused (a client
  normalizes them away and would reach another path).
* The URL must be https, and every address its host resolves to must be
  publicly routable (``outbound_url.validate_outbound_url``). Behind an
  egress proxy that the environment names for the URL, the proxy resolves
  the name: only a literal address is judged here.
* Redirects are never followed (httpx's default, kept by the callers).

Check-then-connect: the HTTP client resolves the name again when it
connects, so a record that answers differently between the two lookups is
not caught; pinning the checked address into the transport would break an
egress proxy that resolves names itself.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

from services.infra.outbound_url import ip_blocked, validate_outbound_url

# The same token syntax as webhook_template._TOKEN_RE.
_TOKEN_RE = re.compile(r"\$\{([^}]++)\}")
#: Values that keep ``/`` between their segments.
_PATH_KEYS = frozenset({"vendor_target"})


class VendorURLRefused(ValueError):
    """The URL a template produced may not be called; the message says why."""


def _encode(key: str, value: Any) -> str:
    text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    if key not in _PATH_KEYS:
        return quote(text, safe="")
    segments = text.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise VendorURLRefused(f"the value of {key} is not a valid path")
    return "/".join(quote(seg, safe="") for seg in segments)


def url_from_template(template: str, subs: dict[str, Any]) -> str:
    """``template`` with each ``${key}`` replaced by its URL-encoded value
    (a missing key renders empty)."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        value = subs.get(key)
        return "" if value is None or value == "" else _encode(key, value)
    return _TOKEN_RE.sub(repl, template)


def _proxied(url: str) -> bool:
    """Whether the environment sends ``url`` through an HTTP proxy (the
    variables httpx honours)."""
    parts = urlsplit(url)
    proxies = urllib.request.getproxies()
    if not proxies.get(parts.scheme) and not proxies.get("all"):
        return False
    return not urllib.request.proxy_bypass(parts.hostname or "")


def _refusal(url: str) -> str | None:
    if _proxied(url):
        parts = urlsplit(url)
        if (parts.scheme or "").lower() != "https":
            return "the URL must be https"
        host = parts.hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return None if host else "the URL has no host"
        return f"the host {host!r} is a private or internal address" if ip_blocked(host) else None
    return validate_outbound_url(url, require_https=True)


async def check_url(url: str) -> None:
    """Raise ``VendorURLRefused`` when ``url`` may not be called (the
    resolver runs off the event loop)."""
    reason = await asyncio.to_thread(_refusal, url)
    if reason:
        raise VendorURLRefused(reason)
