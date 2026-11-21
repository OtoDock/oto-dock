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

What it does not do, stated: an HTTP client resolves the name a second
time when it connects, so a record that answers differently between the
two lookups (DNS rebinding) is not caught by this check alone. The proxy's
callers fetch through ``pinned_http``, which judges every address again at
the dial and connects only to the ones it judged; the MCP copies have no
such transport.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import subprocess
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

try:  # what httpx and requests encode hostnames with; absent → the stdlib codec
    import idna as _idna
except ImportError:  # pragma: no cover - the proxy and the MCP venvs carry it
    _idna = None  # type: ignore[assignment]  # None marks the optional module absent

#: Deprecated IPv6 site-local — ``ipaddress`` reads it as global.
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")
#: The well-known NAT64 prefix (RFC 6052): a DNS64 resolver answers an
#: IPv4-only name with that IPv4 address inside it. ``ipaddress`` reads the
#: whole block as reserved.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
#: The schemes a fetch may use; ``require_https`` narrows it to the second.
_SCHEMES = ("http", "https")
#: The cloud metadata addresses: never dialled from a sandbox's exit,
#: whatever a carve says.
METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
})


def ipv4_inside(ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                ) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The address ``ip`` stands for: the IPv4 address inside an IPv4-mapped
    IPv6 address or one under the NAT64 prefix (an IPv6-only host's answer
    for an IPv4-only name), else ``ip`` itself."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def ip_blocked(ip_str: str) -> bool:
    """True if an address is not publicly routable: private (RFC 1918,
    CGNAT, the documentation ranges), loopback, link-local (the
    cloud-metadata address included), reserved, unspecified, multicast,
    the deprecated IPv6 site-local range, and anything ``ipaddress`` does
    not call global; an IPv4-mapped or NAT64 IPv6 address is judged as
    its IPv4 (``ipv4_inside``). Unparseable → blocked."""
    try:
        ip = ipv4_inside(ipaddress.ip_address(ip_str))
    except ValueError:
        return True
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
    except (OSError, UnicodeError):
        return f"the host {host!r} does not resolve"
    if not infos:
        return f"the host {host!r} does not resolve"
    for info in infos:
        addr = info[4][0]
        # typeshed types getaddrinfo()[4][0] as str | int: a str for AF_INET and AF_INET6.
        if ip_blocked(addr):  # type: ignore[arg-type]
            return f"the host {host!r} resolves to a private or internal address ({addr})"
    return None


def host_owns(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether this host can bind ``ip``: one of its own addresses, on any
    interface, whatever listens on it."""
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((str(ip), 0))
    except OSError:
        return False
    return True


def read_onlink() -> tuple[ipaddress.IPv4Network, ...]:
    """The host's on-link IPv4 routes less link-local, as the sandbox shim's
    ``_onlink4`` reads them; empty where ``ip`` is missing (a Mac, Windows).
    Blocking (a subprocess): run it off the loop."""
    try:
        out = subprocess.run(["ip", "-o", "route", "show", "scope", "link"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    nets: list[ipaddress.IPv4Network] = []
    for line in out.splitlines():
        parts = line.split()
        if parts and "/" in parts[0] and not parts[0].startswith("169.254."):
            with contextlib.suppress(ValueError):
                net = ipaddress.ip_network(parts[0], strict=False)
                if isinstance(net, ipaddress.IPv4Network):
                    nets.append(net)
    return tuple(nets)


def sandbox_dial_refused(
    address: str, *, carves: Iterable[str] = (), onlink: Iterable[ipaddress.IPv4Network] = (),
    owns: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] = host_owns,
) -> bool:
    """The sandbox shim's rule for one address a session's exit would dial
    from outside the namespace (the compute machine's exit, through the
    vendored copy): every
    address that is not publicly routable is refused, and so are the host's
    own addresses (under pasta the host's address is the namespace's own, so
    a sandbox reaches none of them: ``owns``) and its on-link subnets
    (``onlink``, ``read_onlink``'s), but the session's own carves pass;
    loopback, the metadata addresses and the unspecified address never pass,
    whatever the carves say. An IPv4-mapped or NAT64 form stands for its
    IPv4 (a name the session controls can answer one, and the namespace
    sends the connect as IPv4). Unparseable: refused."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if ip.is_loopback or ip in METADATA_ADDRESSES or ip.is_unspecified:
        return True
    if address in set(carves):
        return False
    v4 = ipv4_inside(ip)
    if ip_blocked(address) or owns(v4):
        return True
    return v4.version == 4 and any(v4 in net for net in onlink)


#: The name RFC 6761 reserves for this machine, with every name under it.
LOOPBACK_NAME = "localhost"


def names_this_machine(host: str) -> bool:
    """``localhost`` and every ``*.localhost`` name: they resolve to
    loopback, whatever a resolver answers, so a check that lets a proxy
    resolve a name refuses these first (the gateway's and the exit's)."""
    name = host.lower().rstrip(".")
    return name == LOOPBACK_NAME or name.endswith("." + LOOPBACK_NAME)
