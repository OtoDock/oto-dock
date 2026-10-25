"""The rule for the proxy's sends to the phone daemon (``PHONE_SERVER_URL``).

The daemon's API secret, Twilio's signed webhooks and call media cross that
link, so plain http goes only to a host on this machine or a private
network: an address that is not globally routable (RFC 1918, loopback,
link-local, unique-local, the shared 100.64/10 range a tailnet uses). A
name, a single-label one (the Docker service, ``otodock-phone``) included,
is resolved off the loop and refused only when none of its addresses is
private (a LAN host with a global IPv6 address beside its private one stays
on the LAN); a name that does not resolve in time is left to the send,
which fails on its own. https is always allowed. The sends hold the same
rule at the connect (``client``, ``ws_socket``), so a refusal there has
this module's sentence behind it.

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

import httpx

import config

logger = logging.getLogger("otodock.phone")

_TTL_S = 60.0
# The schemes the plain-text rule is about (``judge``, ``client``, ``ws_socket``).
_PLAIN_HTTP = "http"
_PLAIN_WS = "ws"
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
    if (parts.scheme or "").lower() != _PLAIN_HTTP:
        return None
    host = parts.hostname or ""
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
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


def client(url: str, **kwargs) -> httpx.AsyncClient:
    """The client of a send to the daemon at ``url``: over plain http it
    dials only an address on this machine or a private network, the rule
    ``judge`` checks before the send, held at the connect
    (``pinned_http.private_network``); over https the plain client (TLS names
    the daemon, and no rule applies)."""
    from services.infra import pinned_http
    if (urlsplit(url).scheme or "").lower() != _PLAIN_HTTP:
        return httpx.AsyncClient(**kwargs)
    return pinned_http.checked_client(url, pinned_http.private_network, **kwargs)


async def ws_socket(url: str, timeout: float) -> socket.socket | None:
    """For a plain ``ws://`` daemon URL the WebSocket client would dial
    directly (its own proxy rule, ``websockets.proxy.get_proxy``: the
    ``ws``, ``socks``, ``https`` and ``http`` proxies of the environment less
    its bypass list), a socket connected to an address of its host on this
    machine or a private network, for the client's ``sock``; None otherwise
    (the client dials through its proxy, which resolves the name). Raises
    ``pinned_http.Refused`` (an ``OSError``) when the host has no such
    address, ``TimeoutError`` when the resolve and the connect outlast
    ``timeout``."""
    from websockets.proxy import get_proxy
    from websockets.uri import parse_uri

    from services.infra import pinned_http
    parts = urlsplit(url)
    if (parts.scheme or "").lower() != _PLAIN_WS or get_proxy(parse_uri(url)) is not None:
        return None
    return await asyncio.wait_for(asyncio.to_thread(
        pinned_http.checked_socket, parts.hostname or "", parts.port or 80,
        pinned_http.private_network, timeout), timeout)


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
