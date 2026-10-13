"""Outbound HTTP that connects only to addresses it has checked.

``outbound_url.validate_outbound_url`` judges a host by resolving it, and a
plain HTTP client resolves the name again when it connects, so an answer
that changes between the two lookups (DNS rebinding) reaches an address
nobody checked. The transports here resolve once, at the dial: every
address the name gives must pass the call's policy (a refusal names the
address), and the socket connects to those addresses, in the resolver's
order, sharing the one connect timeout (a silent address leaves time for the
next). The URL keeps its hostname, so the
connection pool, SNI, the certificate check and the Host header all stay on
the name; only the TCP connect changes. The pre-checks at the call sites
stay for their refusal messages; this is the enforcement.

* ``CheckedTransport(policy)``: for ``httpx.AsyncClient(transport=...)``.
  A custom transport drops the environment's proxies, so a URL that
  ``HTTP(S)_PROXY`` covers keeps the plain client (``proxied``): the egress
  proxy resolves the name itself.
* ``PublicHTTPSAdapter``: for a ``requests`` session, ``public_only``.
  A proxied request never reaches it (requests builds its own proxy
  manager).

Redirects are not followed by any caller; a hop would be judged at its own
dial anyway. Unix sockets are refused.
"""

from __future__ import annotations

import asyncio
import socket
import time
import typing

import httpcore
import httpx
# The rules httpx itself applies to HTTP(S)_PROXY / NO_PROXY, so the pin is
# skipped exactly when the plain client would go through the proxy.
from httpx._utils import URLPattern, get_environment_proxies

from services.infra.outbound_url import ip_blocked

#: True when the call may not connect to this address.
Policy = typing.Callable[[str], bool]


def public_only(address: str) -> bool:
    """Every address that is not publicly routable (``ip_blocked``)."""
    return ip_blocked(address)


def proxied(url: str) -> bool:
    """Whether a plain ``httpx`` client sends ``url`` through the
    environment's proxy (``HTTP(S)_PROXY`` / ``ALL_PROXY`` less ``NO_PROXY``,
    matched the way httpx mounts them)."""
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    for pattern, proxy in sorted((URLPattern(k), v) for k, v in get_environment_proxies().items()):
        if pattern.matches(target):
            return proxy is not None
    return False


def _share(left: float | None, remaining: int) -> float | None:
    """One address's part of the connect time left: an even split over the
    addresses still to try, so a silent first one leaves time for the next."""
    if left is None or remaining <= 1:
        return left
    return left / remaining


class Refused(OSError):
    """The name resolves to an address this call may not reach."""


def _getaddrinfo(host: str, port: int) -> list:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def checked_addresses(host: str, port: int, policy: Policy) -> list[str]:
    """The addresses ``host`` resolves to, in the resolver's order, when
    every one passes ``policy``. Raises ``Refused`` naming the first address
    that does not, ``socket.gaierror`` when the name does not resolve.
    Blocking: run it off the event loop."""
    out: list[str] = []
    for info in _getaddrinfo(host, port):
        addr = str(info[4][0])
        if policy(addr):
            raise Refused(f"the host {host!r} resolves to an address this call may not reach ({addr})")
        if addr not in out:
            out.append(addr)
    if not out:
        raise socket.gaierror(f"the host {host!r} does not resolve")
    return out


# --- httpx ------------------------------------------------------------------

class _CheckedBackend(httpcore.AsyncNetworkBackend):
    """httpcore's backend with the dial checked: the pool hands it the URL's
    host, it connects to that host's checked addresses."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(checked_addresses, host, port, self._policy), timeout)
        except asyncio.TimeoutError:
            raise httpcore.ConnectTimeout(f"resolving {host!r} timed out")
        except OSError as e:  # Refused, or the name does not resolve
            raise httpcore.ConnectError(str(e)) from e
        last: Exception | None = None
        for i, addr in enumerate(addresses):
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                break
            try:
                return await self._inner.connect_tcp(
                    addr, port, timeout=_share(left, len(addresses) - i),
                    local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        if last is not None:
            raise last
        raise httpcore.ConnectTimeout(f"connecting to {host!r} timed out")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("a checked transport connects over TCP only")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class CheckedTransport(httpx.AsyncHTTPTransport):
    """An ``httpx`` transport whose connections go only to addresses that
    pass ``policy``, resolved at the dial. HTTP/1.1, no connect retries (a
    refusal is final). ``limits`` replace the client's, which a custom
    transport ignores."""

    def __init__(self, policy: Policy, *, verify: typing.Any = True,
                 limits: httpx.Limits | None = None) -> None:
        limits = limits or httpx.Limits(max_connections=100, max_keepalive_connections=20)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True, http2=False, retries=0,
            network_backend=_CheckedBackend(policy),
        )


def checked_client(url: str, policy: Policy = public_only, **kwargs) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for ``url``: on a ``CheckedTransport`` unless
    the environment proxies the URL (then the plain client, as before)."""
    if proxied(url):
        return httpx.AsyncClient(**kwargs)
    limits = kwargs.pop("limits", None)
    return httpx.AsyncClient(transport=CheckedTransport(policy, limits=limits), **kwargs)


# --- requests ---------------------------------------------------------------

def _requests_parts():
    """The urllib3 classes, built on first use (requests is optional for
    every caller but Web Push)."""
    import requests.adapters
    import urllib3
    from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
    from urllib3.util import connection as u3conn

    class _Connection(urllib3.connection.HTTPSConnection):
        def _new_conn(self):
            try:
                addresses = checked_addresses(self._dns_host, self.port, public_only)
            except Refused as e:
                raise NewConnectionError(self, str(e)) from e
            except socket.gaierror as e:
                raise NameResolutionError(self.host, self, e) from e
            timeout = self.timeout if isinstance(self.timeout, (int, float)) else None
            deadline = None if timeout is None else time.monotonic() + timeout
            last: OSError | None = None
            for i, addr in enumerate(addresses):
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                try:
                    return u3conn.create_connection(
                        (addr, self.port),
                        self.timeout if left is None else _share(left, len(addresses) - i),
                        source_address=self.source_address, socket_options=self.socket_options)
                except OSError as e:  # TimeoutError included: try the next address
                    last = e
            if last is None or isinstance(last, TimeoutError):
                raise ConnectTimeoutError(
                    self, f"Connection to {self.host} timed out. (connect timeout={self.timeout})")
            raise NewConnectionError(self, f"Failed to establish a new connection: {last}")

    class _Pool(urllib3.HTTPSConnectionPool):
        ConnectionCls = _Connection

    class Adapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {
                **self.poolmanager.pool_classes_by_scheme, "https": _Pool}

    return Adapter


_adapter_cls = None


def PublicHTTPSAdapter():  # noqa: N802 - reads as the class it builds
    """A ``requests`` adapter whose https connections go only to public
    addresses, resolved at the dial. Mount it on ``https://``."""
    global _adapter_cls
    if _adapter_cls is None:
        _adapter_cls = _requests_parts()
    return _adapter_cls()
