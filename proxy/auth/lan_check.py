"""Local network restriction for local-only accounts."""

import ipaddress
import logging

from fastapi import Request

import config

logger = logging.getLogger("claude-proxy")


def _ip_in_trusted(ip_str: str) -> bool:
    """True if ``ip_str`` is one of the configured trusted reverse-proxy hops
    (``config.TRUSTED_PROXIES`` — plain IPs or CIDRs)."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for entry in config.TRUSTED_PROXIES:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False

# RFC1918 + loopback + link-local
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]


def is_private_ip(ip_str: str) -> bool:
    """Check if an IP address is in a private/local range."""
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP, trusting ``X-Forwarded-For`` ONLY when the
    immediate socket peer is a configured trusted proxy.

    A direct client cannot spoof its IP: if ``TRUSTED_PROXIES`` is empty (the
    default) or the peer isn't one of them, the header is ignored and the socket
    peer is used. Behind a trusted proxy we walk the XFF chain right-to-left and
    return the first hop that is NOT itself a trusted proxy — the genuine client
    as seen by our edge — since a client can freely prepend fake LEFTMOST entries.
    """
    peer = request.client.host if request.client else "127.0.0.1"
    if not config.TRUSTED_PROXIES or not _ip_in_trusted(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    chain = [p.strip() for p in forwarded.split(",") if p.strip()]
    for ip in reversed(chain):
        if not _ip_in_trusted(ip):
            return ip
    return chain[0] if chain else peer


def check_local_auth_allowed(request: Request, user_row: dict) -> bool:
    """Check if a local login is allowed for this user from this IP.

    Restriction is per-user only: an account with the ``local_only`` flag may
    sign in solely from a private/LAN address. Everyone else logs in from
    anywhere (the platform is reachable from the web by default). The old
    platform-wide ``restrict_local_auth_to_lan`` knob was removed — it made
    sense for neither cloud nor self-host; admins opt individual accounts in.
    """
    if not bool(user_row.get("local_only", 0)):
        return True

    client_ip = get_client_ip(request)
    if is_private_ip(client_ip):
        return True

    logger.warning(f"Local-only account login rejected from public IP {client_ip}")
    return False
