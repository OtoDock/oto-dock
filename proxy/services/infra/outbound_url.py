"""The outbound-URL validator (core-seams phase 3): what the platform asks
before it fetches a destination a member or an agent chose.

``push_sender`` (the Web Push endpoint a browser registered) and the app
egress route (an app's approved vendor host) call ``validate_outbound_url``
and refuse on a reason; the two fetching MCPs — ``transcribe-mcp`` and
``image-search-mcp`` — carry a byte copy of this module as ``_url_guard.py``
(an MCP is its own process and imports nothing from the proxy; the release
gate's twin rule pins the copies and ``tests/core/test_outbound_url.py``
compares the files, so edit THIS file and copy it). Stdlib only, plus the
``idna`` package when it is installed.

What it does: the scheme must be ``https`` (or ``http`` unless the caller
requires TLS); the host is encoded the way httpx and requests encode it
(IDNA 2008 with UTS 46 through the ``idna`` package — the stdlib codec is
IDNA 2003 and can name a DIFFERENT host: ``faß.example`` becomes
``fass.example`` there and ``xn--fa-hia.example`` in the client); every
address that label resolves to must be publicly routable; an empty or
failed resolution is a refusal. A redirect is the caller's: fetch with
``follow_redirects=False`` and validate every ``Location`` again.

What it does not do, stated: the HTTP client resolves the name a second
time when it connects, so a record that answers differently between the
two lookups (DNS rebinding) is not caught here — closing that means
pinning the checked address into the transport.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

try:  # what httpx and requests encode hostnames with; absent → the stdlib codec
    import idna as _idna
except ImportError:  # pragma: no cover - the proxy and the MCP venvs carry it
    _idna = None

#: Deprecated IPv6 site-local — ``ipaddress`` reads it as global.
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")
#: The schemes a fetch may use; ``require_https`` narrows it to the second.
_SCHEMES = ("http", "https")


def ip_blocked(ip_str: str) -> bool:
    """True if an address is not publicly routable: private (RFC 1918,
    CGNAT, the documentation ranges), loopback, link-local (the
    cloud-metadata address included), reserved, unspecified, multicast,
    the deprecated IPv6 site-local range, and anything ``ipaddress`` does
    not call global; an IPv4-mapped IPv6 address is judged as its IPv4.
    Unparseable → blocked."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 6 and ip in _SITE_LOCAL_V6:
        return True
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_unspecified or ip.is_multicast or not ip.is_global
    )


def encoded_host(host: str) -> str:
    """The A-label form of a hostname — what the HTTP client puts on the
    wire. Raises ``ValueError`` for a name the codec refuses."""
    try:
        if _idna is not None:
            return _idna.encode(host, uts46=True).decode("ascii")
        return host.encode("idna").decode("ascii")
    except Exception as e:  # idna.IDNAError, UnicodeError
        raise ValueError(f"unencodable hostname {host!r}: {e}") from e


def validate_outbound_url(url: str, *, require_https: bool = False) -> str | None:
    """The reason the platform must not fetch ``url``, or None when every
    address its host resolves to is public. Blocking (a resolver call):
    run it off the event loop."""
    if not url or not isinstance(url, str):
        return "no URL"
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        port = u.port
    except ValueError:
        return "malformed URL"
    scheme = (u.scheme or "").lower()
    if require_https:
        if scheme != "https":
            return "the URL must be https"
    elif scheme not in _SCHEMES:
        return f"unsupported URL scheme {scheme!r}"
    if not host:
        return "the URL has no host"
    try:
        label = encoded_host(host)
    except ValueError:
        return f"the host {host!r} is not a valid hostname"
    if not port:
        port = 443 if scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(label, port, proto=socket.IPPROTO_TCP)
    except Exception:
        return f"the host {host!r} does not resolve"
    if not infos:
        return f"the host {host!r} does not resolve"
    for info in infos:
        addr = info[4][0]
        if ip_blocked(addr):
            return f"the host {host!r} resolves to a private or internal address ({addr})"
    return None
