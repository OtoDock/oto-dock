"""The rule for the proxy's sends to the phone daemon (``PHONE_SERVER_URL``).

The daemon's API secret, Twilio's signed webhooks and call media cross that
link, so plain http goes only to a host on this machine or a private
network: an address that is not globally routable (RFC 1918, loopback,
link-local, unique-local, the shared 100.64/10 range a tailnet uses). A
single-label name (the Docker service, ``otodock-phone``) counts as
private; another name is resolved off the loop and refused only when none
of its addresses is private (a LAN host with a global IPv6 address beside
its private one stays on the LAN); a name that does not resolve in time
is left to the send, which fails on its own. https is always allowed.

Judged at send time, never at boot (a fronted install must still start);
a verdict is kept a minute and a refusal logs one ERROR an hour. The phone
daemon applies the same rule to its ``PROXY_URL`` at start.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

import config

logger = logging.getLogger("claude-proxy.phone")

_TTL_S = 60.0
_RESOLVE_TIMEOUT_S = 3.0
_LOG_EVERY_S = 3600.0

_verdicts: dict[str, tuple[float, str | None]] = {}
_logged: dict[str, float] = {}


def _routable(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_global
    except ValueError:
        return False


def judge(url: str, resolve=socket.getaddrinfo) -> str | None:
    """Why ``url`` may not carry plain http (None when it may)."""
    parts = urlsplit(url or "")
    if (parts.scheme or "").lower() != "http":
        return None
    host = parts.hostname or ""
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        if "." not in host.rstrip("."):
            return None
        try:
            addresses = sorted({info[4][0] for info in resolve(host, None)})
        except OSError:
            return None
    public = [a for a in addresses if _routable(a)]
    if not public or len(public) < len(addresses):
        return None
    return (f"PHONE_SERVER_URL is plain http to {host} ({public[0]}), an address "
            "reachable from the internet: use https, or the daemon's address on "
            "this machine or a private network")


def _forget() -> None:
    """Drop the kept verdicts (tests, a changed setting)."""
    _verdicts.clear()
    _logged.clear()


async def refusal(url: str | None = None) -> str | None:
    """``judge`` for ``url`` (default ``PHONE_SERVER_URL``), resolved in a
    worker thread with a timeout."""
    url = url if url is not None else config.PHONE_SERVER_URL
    now = time.monotonic()
    kept = _verdicts.get(url)
    if kept and now - kept[0] < _TTL_S:
        return kept[1]
    try:
        reason = await asyncio.wait_for(asyncio.to_thread(judge, url), _RESOLVE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return None
    _verdicts[url] = (now, reason)
    if reason and now - _logged.get(url, -_LOG_EVERY_S) >= _LOG_EVERY_S:
        _logged[url] = now
        logger.error("Nothing is sent to the phone daemon: %s.", reason)
    return reason
