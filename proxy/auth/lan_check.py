"""The client address of a request, and the local-network rule for
``local_only`` accounts.

One resolver reads the ASGI scope for HTTP and WebSocket alike. uvicorn's own
forwarded-header rewrite is off (``app.py``): the ASGI shim there calls
``stamp_scope`` once per connection, which keeps the socket peer in
``scope["otodock.peer"]``, runs the misconfiguration detector, puts the
resolved client in ``scope["client"]`` (the access log and every handler see
it) and, from a hop, the scheme its ``X-Forwarded-Proto`` names in
``scope["scheme"]``. ``get_client_ip``, ``auth_bucket_key``,
``trusted_forwarded_proto`` and ``check_local_auth_allowed`` resolve again
from the peer, so they give the same answer with or without the shim (tests
drive the app directly).

A request on the internal listener is not a person on the local network:
sandboxes, app servers, scripts and the satellite tunnel arrive there from
127.0.0.1, so it never passes the local-network rule and its per-address auth
buckets are one bucket of their own (``INTERNAL_LISTENER_KEY``), apart from a
person on the host's own loopback.
"""

import ipaddress
import logging
import time
from dataclasses import dataclass, replace
from typing import Literal

from starlette.requests import HTTPConnection

import config

logger = logging.getLogger("claude-proxy")

# The request headers an edge adds. Only X-Forwarded-For ever resolves a
# client; the others only tell the detector that an edge is in front.
_XFF = b"x-forwarded-for"
_EDGE_SIGNALS = frozenset({
    _XFF, b"forwarded", b"x-real-ip", b"x-forwarded-proto", b"x-forwarded-host",
})
_LOOPBACK_HOPS = ("127.0.0.1", "::1")
# The X-Forwarded-Proto values a hop's scheme is read from, by security.
_FORWARDED_PROTOS = {"http": "http", "https": "https", "ws": "http", "wss": "https"}

# The detector's memory: one log line per peer per hour, the last hour's
# peers listed for the admin.
_WARN_EVERY_S = 3600.0
_WARN_PEERS_MAX = 64

# The key every per-address auth bucket uses for the internal listener. The
# resolver yields an address or the socket peer for a client, never this.
INTERNAL_LISTENER_KEY = "internal-listener"

Case = Literal["", "untrusted_forwarder", "edge_without_xff"]


@dataclass(frozen=True)
class ClientAddress:
    """``client``: the resolved address. ``case``: an edge misconfiguration
    seen on THIS request (never a flag a request can set for others).
    ``shared``: the address is not one distinct outside client (a hop, the
    Docker gateway, the internal listener): a per-address bucket keyed on it
    is one bucket for everyone behind it. It depends on the peer alone: a
    private peer that is not a hop is one client whatever headers it sends.
    ``internal``: the request came in on the internal listener (sandboxes
    and the satellite tunnel), which is not the local network."""
    client: str
    peer: str
    case: Case = ""
    shared: bool = False
    internal: bool = False

    @property
    def bucket_key(self) -> str:
        """The key of a per-address bucket (``auth_bucket_key``): read with
        ``shared`` from one resolution, so a check and its record, or a
        reservation and its release, never disagree."""
        return INTERNAL_LISTENER_KEY if self.internal else self.client


def _ip_in_trusted(ip_str: str) -> bool:
    """True if ``ip_str`` is one of the configured trusted reverse-proxy hops
    (``config.TRUSTED_PROXIES``: plain IPs or CIDRs), read live."""
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


def _is_loopback(ip_str: str) -> bool:
    try:
        return ipaddress.ip_address(ip_str).is_loopback
    except ValueError:
        return False


# --- the effective hops -----------------------------------------------------

_hops_key: tuple | None = None
_hops: tuple = ()


def _hop_networks() -> tuple:
    """``TRUSTED_PROXIES`` plus loopback on bare metal. In a container a
    loopback peer is a sandbox, the satellite tunnel or an in-container
    process, never an edge: loopback is not a hop there, and a configured
    entry that covers loopback is dropped (one ERROR). Parsed once per
    distinct configuration."""
    global _hops_key, _hops
    key = (tuple(config.TRUSTED_PROXIES), bool(config.RUNNING_IN_DOCKER))
    if key == _hops_key:
        return _hops
    nets = []
    for entry in config.TRUSTED_PROXIES:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if config.RUNNING_IN_DOCKER and any(
                ipaddress.ip_address(lo) in net for lo in _LOOPBACK_HOPS
                if ipaddress.ip_address(lo).version == net.version):
            logger.error(
                "TRUSTED_PROXY entry %s covers loopback, which inside a container "
                "is a sandbox or a local process, never your reverse proxy: the "
                "entry is ignored. Set TRUSTED_PROXY to the address your reverse "
                "proxy presents to the container.", entry)
            continue
        nets.append(net)
    if not config.RUNNING_IN_DOCKER:
        nets.extend(ipaddress.ip_network(lo) for lo in _LOOPBACK_HOPS)
    _hops_key, _hops = key, tuple(nets)
    return _hops


def _is_hop(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in _hop_networks())


# --- the container's default gateway ----------------------------------------

_gateway_read = False
_gateway = ""


def _docker_gateway() -> str:
    """The container's default gateway (``/proc/net/route``), read once. Host
    traffic, IPv6 and hairpin connections to a published port arrive from it
    with no forwarding header."""
    global _gateway_read, _gateway
    if _gateway_read:
        return _gateway
    _gateway_read = True
    try:
        with open("/proc/net/route") as f:
            next(f, None)
            for line in f:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    _gateway = str(ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1]))
                    break
    except (OSError, ValueError):
        _gateway = ""
    return _gateway


# --- parsing ----------------------------------------------------------------

def _unmap(ip_str: str) -> str:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return ip_str
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped) if mapped else str(addr)


def _entry_ip(entry: str) -> str | None:
    """One X-Forwarded-For entry as an IP, its port stripped (``v4:port``,
    ``[v6]:port``); None when it is not an address."""
    e = entry.strip()
    if e.startswith("["):
        end = e.find("]")
        if end < 0:
            return None
        e = e[1:end]
    elif e.count(":") == 1:
        e = e.split(":", 1)[0]
    try:
        return _unmap(str(ipaddress.ip_address(e)))
    except ValueError:
        return None


def _peer_of(scope) -> str:
    peer = scope.get("otodock.peer")
    if peer is None:
        client = scope.get("client")
        peer = client[0] if client else ""
    return _unmap(peer or "")


def _on_internal_listener(scope) -> bool:
    port = config.INTERNAL_LISTENER_PORT
    server = scope.get("server")
    return bool(port) and bool(server) and len(server) > 1 and server[1] == port


def resolve(scope) -> ClientAddress:
    """The client address of one request. Never raises: an address that
    cannot be read yields the peer."""
    peer = _peer_of(scope)
    try:
        if _on_internal_listener(scope):
            return ClientAddress(peer, peer, "", shared=True, internal=True)
        xff: list[bytes] = []
        signals = False
        for name, value in scope.get("headers") or ():
            if name == _XFF:
                xff.append(value)
            if name in _EDGE_SIGNALS:
                signals = True
        if _is_hop(peer):
            if not xff:
                case: Case = "edge_without_xff" if signals else ""
                return ClientAddress(peer, peer, case, shared=True)
            chain = [p for v in xff for p in v.decode("latin-1").split(",") if p.strip()]
            for entry in reversed(chain):
                ip = _entry_ip(entry)
                if ip is None:
                    return ClientAddress(peer, peer, "", shared=True)
                if not _is_hop(ip):
                    return _outside(ip, peer)
            first = _entry_ip(chain[0]) if chain else None
            return ClientAddress(first or peer, peer, "", shared=True)
        if signals and is_private_ip(peer) and not (
                config.RUNNING_IN_DOCKER and _is_loopback(peer)):
            # The headers name the case (the detector, the local-only
            # refusal), never the bucket: the peer is shared exactly as it
            # would be without them.
            return replace(_outside(peer, peer), case="untrusted_forwarder")
        return _outside(peer, peer)
    except Exception:  # a resolver that raised would drop the request
        logger.debug("client address unreadable; using the peer %s", peer, exc_info=True)
        return ClientAddress(peer, peer, "", shared=True)


def _outside(client: str, peer: str) -> ClientAddress:
    gateway = config.RUNNING_IN_DOCKER and client and client == _docker_gateway()
    return ClientAddress(client, peer, "", shared=bool(gateway) or not client)


# --- the detector -----------------------------------------------------------

_seen: dict[str, dict] = {}


def _note(peer: str, case: Case) -> None:
    now = time.time()
    entry = _seen.get(peer)
    if entry is None:
        if len(_seen) >= _WARN_PEERS_MAX:
            oldest = min(_seen, key=lambda k: _seen[k]["last_seen"])
            _seen.pop(oldest, None)
        # ``gateway``: the peer is the container's gateway, which may be
        # trusted only while the proxy's port is published on 127.0.0.1
        # (direct connections to a published port arrive from it too); the
        # admin Security tab words its advice by it.
        entry = _seen[peer] = {"peer": peer, "case": case, "first_seen": now,
                               "last_seen": now, "count": 0, "logged_at": 0.0,
                               "gateway": bool(config.RUNNING_IN_DOCKER
                                               and peer == _docker_gateway())}
    entry["case"] = case
    entry["last_seen"] = now
    entry["count"] += 1
    if now - entry["logged_at"] < _WARN_EVERY_S:
        return
    entry["logged_at"] = now
    if case == "untrusted_forwarder":
        gateway = entry["gateway"]
        where = (
            " This is the container's gateway: trust it only when the proxy's port "
            "is published on 127.0.0.1 (PROXY_BIND_IP=127.0.0.1), because direct "
            "connections to a published port arrive from it too."
            if gateway else "")
        logger.error(
            "Forwarding headers arrived from %s, which is not a trusted proxy: they "
            "are ignored, every client behind it counts as %s for the per-address "
            "limits, and local-only accounts are refused through it. If %s is the "
            "reverse proxy in front of OtoDock, set TRUSTED_PROXY=%s (that address, "
            "never a subnet) and restart.%s If it is a forward proxy or not a proxy "
            "at all, leave TRUSTED_PROXY alone.", peer, peer, peer, peer, where)
    else:
        logger.warning(
            "The trusted proxy %s forwards requests without X-Forwarded-For: every "
            "client behind it counts as %s and local-only accounts are refused "
            "through it. Make the edge append the client address (nginx: "
            "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;).",
            peer, peer)


_STARTED_AT = time.time()


def _unconfigured_edge() -> dict | None:
    """The boot check (``config.untrusted_edge``) as a row, read live and
    judged on the hops in effect (a loopback entry in a container trusts
    nothing): ``peer`` is the container's gateway, the address an edge on
    this host connects from (one on another machine connects from its own)."""
    effective = [str(net) for net in _hop_networks()] if config.RUNNING_IN_DOCKER \
        else config.TRUSTED_PROXIES
    if not config.untrusted_edge(config.RUNNING_IN_DOCKER, config.DASHBOARD_PUBLIC_URL,
                                 effective):
        return None
    gateway = _docker_gateway()
    return {"peer": gateway, "case": "no_trusted_proxy", "first_seen": _STARTED_AT,
            "last_seen": time.time(), "count": 0, "gateway": bool(gateway)}


def forwarding_warnings() -> list[dict]:
    """The edge misconfigurations seen in the last hour, newest first, then
    the boot check's row while it holds (the admin platform settings and
    the dashboard's admin banner carry them)."""
    cutoff = time.time() - _WARN_EVERY_S
    rows = [{k: v for k, v in e.items() if k != "logged_at"}
            for e in _seen.values() if e["last_seen"] >= cutoff]
    rows.sort(key=lambda e: e["last_seen"], reverse=True)
    boot = _unconfigured_edge()
    return rows + [boot] if boot else rows


def stamp_scope(scope) -> ClientAddress:
    """For the ASGI shim: keep the socket peer, feed the detector, put the
    resolved client in ``scope["client"]`` and the scheme a hop names in
    ``scope["scheme"]`` (``ws``/``wss`` on a WebSocket)."""
    client = scope.get("client")
    scope["otodock.peer"] = client[0] if client else ""
    proto = trusted_forwarded_proto(scope)
    if proto:
        if scope.get("type") == "websocket":
            scope["scheme"] = "wss" if proto == "https" else "ws"
        else:
            scope["scheme"] = proto
    resolved = resolve(scope)
    if resolved.case:
        _note(resolved.peer, resolved.case)
    elif scope.get("headers") and not _on_internal_listener(scope) and not _is_hop(resolved.peer):
        if any(name in _EDGE_SIGNALS for name, _ in scope["headers"]):
            logger.debug("Forwarding headers from the public address %s ignored", resolved.peer)
    if client:
        scope["client"] = (resolved.client, client[1])
    return resolved


# --- the request-level API --------------------------------------------------

def trusted_forwarded_host(scope) -> str:
    """The ``X-Forwarded-Host`` a trusted hop sent (its first value), or ""
    when the socket peer is not a hop or the request came in on the
    internal listener: a host header from anyone else names nothing."""
    peer = _peer_of(scope)
    if not peer or _on_internal_listener(scope) or not _is_hop(peer):
        return ""
    for name, value in scope.get("headers") or ():
        if name == b"x-forwarded-host":
            return value.decode("latin-1").split(",")[0].strip()
    return ""


def trusted_forwarded_proto(scope) -> str:
    """``http`` or ``https``, as the first ``X-Forwarded-Proto`` value a
    trusted hop sent names it (``ws``/``wss`` read alike: Traefik sends them
    on an upgrade), or "" when the socket peer is not a hop, the request
    came in on the internal listener, or the value names neither: a scheme
    from anyone else names nothing."""
    peer = _peer_of(scope)
    if not peer or _on_internal_listener(scope) or not _is_hop(peer):
        return ""
    for name, value in scope.get("headers") or ():
        if name == b"x-forwarded-proto":
            first = value.decode("latin-1").split(",")[0].strip().lower()
            return _FORWARDED_PROTOS.get(first, "")
    return ""


def client_address(conn: HTTPConnection) -> ClientAddress:
    return resolve(conn.scope)


def get_client_ip(conn: HTTPConnection) -> str:
    """The resolved client address (see ``resolve``)."""
    return resolve(conn.scope).client


def auth_bucket_key(conn: HTTPConnection) -> str:
    """The key of a per-address auth bucket (login, 2FA, passkey, forgot,
    reset, invite, webhook auth, a link's password): the resolved client, or
    ``INTERNAL_LISTENER_KEY`` on the internal listener, so sandboxes never
    spend the attempts of a person on the host's own loopback."""
    return resolve(conn.scope).bucket_key


def check_local_auth_allowed(conn: HTTPConnection, user_row: dict) -> bool:
    """Check if a local login is allowed for this user from this IP.

    Restriction is per-user only: an account with the ``local_only`` flag may
    sign in solely from a private/LAN address. It fails closed per request:
    the internal listener (a sandbox, an app server, a script or the
    satellite tunnel, all from 127.0.0.1), an edge misconfiguration seen on
    this request, or (in a container) the gateway as the client, cannot prove
    a local address.
    """
    if not bool(user_row.get("local_only", 0)):
        return True
    r = resolve(conn.scope)
    if r.internal:
        reason = ("the request came through the internal listener (an agent sandbox, "
                  "an app server or a script, or the satellite tunnel), not the local network")
    elif r.case == "untrusted_forwarder":
        reason = f"forwarding headers from the untrusted address {r.peer}"
    elif r.case == "edge_without_xff":
        reason = f"the trusted proxy {r.peer} sent no X-Forwarded-For"
    elif config.RUNNING_IN_DOCKER and r.client and r.client == _docker_gateway():
        reason = f"the client is the container's gateway {r.client}"
    elif is_private_ip(r.client):
        return True
    else:
        reason = f"the public address {r.client or '(none)'}"
    logger.warning("Local-only account login refused: %s", reason)
    return False


def reset_state() -> None:
    """Forget the detector's memory and the parsed configuration (tests)."""
    global _hops_key, _gateway_read, _gateway
    _seen.clear()
    _hops_key = None
    _gateway_read = False
    _gateway = ""
