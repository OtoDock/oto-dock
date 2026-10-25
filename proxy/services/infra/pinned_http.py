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

* ``CheckedTransport(policy)``: for ``httpx.AsyncClient(transport=...)``
  (``checked_client``). A custom transport drops the environment's proxies
  and the client's ``verify`` and ``limits``, so a URL that ``HTTP(S)_PROXY``
  covers keeps the plain client (``proxied``, the egress proxy resolves the
  name itself) and the two settings are built into the transport.
* ``CheckedSyncTransport(policy)``: the same for a sync ``httpx.Client``
  (``checked_sync_client``), for a caller that is synchronous.
* ``checked_pool(policy)``: one ``httpx.AsyncClient`` for several hosts,
  the checked transport for every URL the environment does not proxy and
  the proxy's own transport, mounted per pattern as httpx mounts it, for
  every URL it does.
* ``PublicHTTPSAdapter``: for a ``requests`` session, ``public_only``.
  A proxied request never reaches it (requests builds its own proxy
  manager).
* ``checked_socket(host, port, policy)``: a TCP socket connected to a
  checked address, for a client that takes a socket (a WebSocket).

Four policies, named for what the call may reach. ``public_only`` refuses
every address that is not publicly routable (app egress, the webhook vendor
calls, Web Push and FCM, the release check's default URL, a public MCP's
authorization-server metadata and registration, the engines' usage polls,
the community catalogs, Microsoft's sign-in, Twilio's API, the geocoding
hint). ``control_plane`` refuses this machine's own address space and lets
LAN addresses through for the callers whose hosts an admin chose (the
credential gateway's vendors, the relay, and every other call of the MCP
sign-in client: the MCP server's own metadata and challenge, a private
MCP's authorization server, the token, refresh and revocation calls).
``model_server`` lets this machine and the LAN through and refuses
link-local, the metadata addresses, multicast and reserved (a model server
an admin runs here or on the LAN). ``private_network`` refuses every
globally routable address (the phone daemon over plain http). The two
admin-endpoint policies drop the answers they refuse and connect to the
rest, refusing only when none remain (a LAN host with a global IPv6
address beside its private one); the other two refuse the whole answer. A
refusal is logged where it happens, with the host and the address: every
caller maps a connect error to a generic answer.

A redirect, where a caller follows one (the agents catalog's tarball), is
judged at its own dial. Unix sockets are refused.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
import typing

import httpcore
import httpx

# The rules httpx itself applies to HTTP(S)_PROXY / NO_PROXY, so the pin is
# skipped exactly when the plain client would go through the proxy.
from httpx._utils import URLPattern, get_environment_proxies

from services.infra.outbound_url import ip_blocked, ipv4_inside

logger = logging.getLogger(__name__)

#: True when the call may not connect to this address.
Policy = typing.Callable[[str], bool]

# The cloud metadata addresses no address-class test catches: the AWS IPv6
# endpoint is a ULA, private but not link-local.
_METADATA = frozenset({ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")})
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


def public_only(address: str) -> bool:
    """Every address that is not publicly routable (``ip_blocked``)."""
    return ip_blocked(address)


def control_plane(address: str) -> bool:
    """Every address in this machine's own address space: loopback,
    link-local and the metadata addresses, unspecified and the ``0.0.0.0/8``
    block, multicast and reserved, an IPv4-mapped or NAT64 IPv6 address
    judged as its IPv4 (``ipv4_inside``), and a string ``ipaddress`` cannot
    read (``getaddrinfo`` never
    yields one, and a zone id parses as link-local). A LAN address passes:
    the admin's allowlist decides those."""
    try:
        ip = ipv4_inside(ipaddress.ip_address(address))
    except ValueError:
        return True
    return (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
            or ip.is_reserved or ip in _METADATA
            or (ip.version == 4 and ip in _THIS_NETWORK))


def model_server(address: str) -> bool:
    """Link-local and the metadata addresses, multicast and reserved, the
    ``0.0.0.0/8`` block but ``0.0.0.0`` itself, and a string ``ipaddress``
    cannot read; an IPv4-mapped or NAT64 address judged as its IPv4. Loopback and the unspecified address (which Linux connects
    to loopback: a copied ``OLLAMA_HOST``) pass, and so do the LAN and the
    public internet."""
    try:
        ip = ipv4_inside(ipaddress.ip_address(address))
    except ValueError:
        return True
    if ip in _METADATA:
        return True
    if ip.is_loopback or ip.is_unspecified:
        return False
    return (ip.is_link_local or ip.is_multicast or ip.is_reserved
            or (ip.version == 4 and ip in _THIS_NETWORK))


def private_network(address: str) -> bool:
    """Every globally routable address (and a string ``ipaddress`` cannot
    read; an IPv4-mapped or NAT64 address judged as its IPv4): plain http
    stays on this machine or a private network (``services/phone/daemon_url``)."""
    try:
        ip = ipv4_inside(ipaddress.ip_address(address.split("%", 1)[0]))
    except ValueError:
        return True
    return ip.is_global


# The policies whose dial drops a refused answer and connects to the rest.
_FILTERING: frozenset[Policy] = frozenset({model_server, private_network})


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
    every one passes ``policy``; for a filtering policy, the ones that pass,
    when one does. Raises ``Refused`` naming an address that does not,
    ``socket.gaierror`` when the name does not resolve. Blocking: run it off
    the event loop."""
    out: list[str] = []
    refused = ""
    for info in _getaddrinfo(host, port):
        addr = str(info[4][0])
        if policy(addr):
            if policy in _FILTERING:
                refused = refused or addr
                continue
            logger.warning("outbound: the host %r resolves to %s, an address this call may not reach",
                           host, addr)
            raise Refused(f"the host {host!r} resolves to an address this call may not reach ({addr})")
        if addr not in out:
            out.append(addr)
    if not out and refused:
        logger.warning("outbound: the host %r resolves only to addresses this call may not "
                       "reach (%s)", host, refused)
        raise Refused(f"the host {host!r} resolves only to addresses this call may not "
                      f"reach ({refused})")
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
        except asyncio.TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"resolving {host!r} timed out") from exc
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
    refusal is final). ``verify`` and ``limits`` replace the client's, which
    a custom transport ignores; the dial policy and the SSL context built
    from ``verify`` stay readable as ``policy`` and ``ssl_context``. The base
    class's ``__init__`` only builds the pool, so it is not called (httpx
    0.28)."""

    def __init__(self, policy: Policy, *, verify: typing.Any = True,
                 limits: httpx.Limits | None = None) -> None:
        limits = limits or httpx.Limits(max_connections=100, max_keepalive_connections=20)
        self.policy = policy
        self.ssl_context = httpx.create_ssl_context(verify=verify)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self.ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True, http2=False, retries=0,
            network_backend=_CheckedBackend(policy),
        )


def checked_client(url: str, policy: Policy = public_only, **kwargs) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for ``url``: on a ``CheckedTransport`` unless
    the environment proxies the URL (then the plain client, as before, with
    its ``verify`` and ``limits`` honoured by httpx itself)."""
    if proxied(url):
        return httpx.AsyncClient(**kwargs)
    limits = kwargs.pop("limits", None)
    verify = kwargs.pop("verify", True)
    return httpx.AsyncClient(transport=CheckedTransport(policy, verify=verify, limits=limits), **kwargs)


def checked_pool(policy: Policy = public_only, **kwargs) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for any number of hosts: every URL the
    environment proxies (``HTTP(S)_PROXY`` / ``ALL_PROXY`` less
    ``NO_PROXY``) goes through its proxy, mounted per pattern as httpx
    itself mounts them, and every other dial is checked. ``verify`` and
    ``limits`` reach both kinds of transport."""
    limits = kwargs.pop("limits", None)
    verify = kwargs.pop("verify", True)
    proxy_limits = limits or httpx.Limits(max_connections=100, max_keepalive_connections=20)
    # A ``NO_PROXY`` pattern maps to None: the client's own (checked)
    # transport, ahead of the broader proxy pattern, as httpx orders them.
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {
        pattern: (httpx.AsyncHTTPTransport(proxy=proxy, verify=verify, limits=proxy_limits)
                  if proxy else None)
        for pattern, proxy in get_environment_proxies().items()
    }
    return httpx.AsyncClient(
        transport=CheckedTransport(policy, verify=verify, limits=limits), mounts=mounts, **kwargs)


def checked_socket(host: str, port: int, policy: Policy, timeout: float | None = None) -> socket.socket:
    """A TCP socket connected to an address of ``host`` that passes
    ``policy`` (tried in the resolver's order, sharing ``timeout``), for a
    client that takes a connected socket. Blocking: run it off the event
    loop. Raises ``Refused``, ``socket.gaierror`` or the last connect
    error."""
    deadline = None if timeout is None else time.monotonic() + timeout
    addresses = checked_addresses(host, port, policy)
    last: OSError | None = None
    for i, addr in enumerate(addresses):
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            break
        try:
            return socket.create_connection((addr, port), timeout=_share(left, len(addresses) - i))
        except OSError as e:
            last = e
    raise last or TimeoutError(f"connecting to {host!r} timed out")


class _CheckedSyncBackend(httpcore.NetworkBackend):
    """The sync twin of ``_CheckedBackend``: the resolve and the dial run in
    the calling thread, as the plain client's connect did."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._inner = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            addresses = checked_addresses(host, port, self._policy)
        except OSError as e:  # Refused, or the name does not resolve
            raise httpcore.ConnectError(str(e)) from e
        last: Exception | None = None
        for i, addr in enumerate(addresses):
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                break
            try:
                return self._inner.connect_tcp(
                    addr, port, timeout=_share(left, len(addresses) - i),
                    local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        if last is not None:
            raise last
        raise httpcore.ConnectTimeout(f"connecting to {host!r} timed out")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("a checked transport connects over TCP only")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class CheckedSyncTransport(httpx.HTTPTransport):
    """``CheckedTransport`` for a sync ``httpx.Client``."""

    def __init__(self, policy: Policy, *, verify: typing.Any = True,
                 limits: httpx.Limits | None = None) -> None:
        limits = limits or httpx.Limits(max_connections=100, max_keepalive_connections=20)
        self.policy = policy
        self.ssl_context = httpx.create_ssl_context(verify=verify)
        self._pool = httpcore.ConnectionPool(
            ssl_context=self.ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True, http2=False, retries=0,
            network_backend=_CheckedSyncBackend(policy),
        )


def checked_sync_client(url: str, policy: Policy = public_only, **kwargs) -> httpx.Client:
    """``checked_client`` for a sync ``httpx.Client``."""
    if proxied(url):
        return httpx.Client(**kwargs)
    limits = kwargs.pop("limits", None)
    verify = kwargs.pop("verify", True)
    return httpx.Client(transport=CheckedSyncTransport(policy, verify=verify, limits=limits), **kwargs)


# --- requests ---------------------------------------------------------------

def _requests_parts():
    """The urllib3 classes, built on first use (requests is optional for
    every caller but Web Push)."""
    import requests.adapters
    import urllib3
    from urllib3.exceptions import (
        ConnectTimeoutError,
        NameResolutionError,
        NewConnectionError,
    )
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


def PublicHTTPSAdapter():
    """A ``requests`` adapter whose https connections go only to public
    addresses, resolved at the dial. Mount it on ``https://``."""
    global _adapter_cls
    if _adapter_cls is None:
        _adapter_cls = _requests_parts()
    return _adapter_cls()
