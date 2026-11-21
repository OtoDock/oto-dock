"""Deployment-topology resolver — T1 bare-metal · T2 Docker-Compose.

Single source of truth for the host-naming differences between the two
topologies:

  * how the proxy / a local agent reaches a Docker MCP's container,
  * how a Docker MCP (or Collabora) reaches the proxy on its callbacks,
  * which Docker daemon ``services/mcp/docker_manager.py`` drives.

Everything keys off ``config.RUNNING_IN_DOCKER``. On bare-metal
(``RUNNING_IN_DOCKER`` False — the live native install) every helper returns
the historical ``localhost`` / ``host.docker.internal`` values. A hosted
install (``OTODOCK_CLOUD``) runs the compose topology with its Docker tools
inside it: the flag carries the hosted rules (no private address, Twilio
only), never a topology of its own.

The module imports only ``config`` (no MCP/registry types — manifests are
duck-typed via ``getattr``) so it stays a dependency-free leaf importable from
both ``core`` and ``services`` without cycles.
"""

from __future__ import annotations

import ipaddress
import socket

import config

# Deployment modes — the "lifecycle mode" column of the architecture table.
MANAGED_LOCAL = "managed-local"        # T1 bare-metal: local docker socket
MANAGED_SOCKPROX = "managed-sockprox"  # T2 compose: docker-socket-proxy (tcp)

# Hosts that mean "the proxy itself / its own loopback". ``""`` covers a URL
# with no host (urlparse of a bare path), treated as loopback-equivalent.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def current_mode() -> str:
    """Resolve the active deployment topology: the bare-metal/compose split
    is ``RUNNING_IN_DOCKER``, whatever ``OTODOCK_CLOUD`` says."""
    if config.RUNNING_IN_DOCKER:
        return MANAGED_SOCKPROX
    return MANAGED_LOCAL


def in_docker_compose() -> bool:
    """True only in T2 — a containerised proxy driving a socket-proxy daemon."""
    return current_mode() == MANAGED_SOCKPROX


def compute_link_allowed(peer: str) -> bool:
    """Whether a compute machine beside the platform may link from ``peer``,
    the socket's own address (never a forwarded one): in compose the compute
    service's own pinned address on its network (``OTODOCK_COMPUTE_LINK_FROM``,
    the compose file's), never any other address of the install's networks;
    on bare metal the host's loopback alone, where the compute unit dials the
    main listener. A machine on another server links by the rule every
    machine link follows (``compute_pairing.link_address``)."""
    if in_docker_compose():
        own = config.OTODOCK_COMPUTE_LINK_FROM
        return bool(own) and peer == own
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def plain_address(addr: str) -> str:
    """An address literal in its one form (an IPv4-mapped IPv6 address as its
    IPv4 address); anything that is not a literal comes back as it was."""
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return addr
    return str(getattr(ip, "ipv4_mapped", None) or ip)


def same_address(a: str, b: str) -> bool:
    """Whether two address literals name one address (``::ffff:10.0.0.5`` is
    ``10.0.0.5``); a name never matches."""
    try:
        ia, ib = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return False
    ia = getattr(ia, "ipv4_mapped", None) or ia
    ib = getattr(ib, "ipv4_mapped", None) or ib
    return ia == ib


#: The addresses the compute machines on other servers last signed in from,
#: loaded by ``compute_bootstrap.load_addresses`` at boot and after each change.
_compute_addresses: frozenset[str] = frozenset()


def set_compute_addresses(addresses) -> None:
    global _compute_addresses
    _compute_addresses = frozenset(str(a) for a in addresses if a)


def is_compute_address(addr: str) -> bool:
    """Whether ``addr`` is an address a compute machine on another server
    signed in from: a link address, never the local network a person signs
    in from."""
    return any(same_address(addr, a) for a in _compute_addresses)


def compute_address_problem(addr: str) -> str:
    """Why a compute machine on another server cannot sign in from ``addr``,
    ``""`` when it can: an address literal another server links from, never
    this host's loopback, an unspecified, multicast or link-local one, nor
    the compose service's own link address."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "Its address cannot be read."
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        return "This address cannot be another computer's."
    if config.OTODOCK_COMPUTE_LINK_FROM and same_address(addr, config.OTODOCK_COMPUTE_LINK_FROM):
        return "This address is the install's own compute machine."
    return ""


_PLAIN_SCHEMES, _TLS_SCHEMES = ("http", "ws"), ("https", "wss")
# A LAN's literals: the private IPv4 ranges, unique-local IPv6 and link-local.
# ``is_private`` also admits 6to4, NAT64 and the documentation and benchmark
# ranges, which can carry or stand for a public address.
_LAN_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"))


def link_schemes() -> tuple[str, str]:
    """The (web, socket) schemes a machine links through: ``http``/``ws``
    only for a plain-HTTP public address that names this host or a LAN
    literal.

    The pairing token, a machine's secret and its sessions' tokens cross
    this link. An install served over plain HTTP on a LAN already signs its
    admin in over that LAN, so they get the same trust. Anything else keeps
    TLS: a name is never resolved here, since this resolver is not the
    machine's, and the satellite refuses plaintext to a public host only
    after the secret has crossed (``assert_transport_secure``, the guard
    behind this rule)."""
    from urllib.parse import urlparse
    url = urlparse(config.DASHBOARD_PUBLIC_URL or "")
    host = url.hostname or ""
    if url.scheme.lower() != _PLAIN_SCHEMES[0] or not host:
        return _TLS_SCHEMES
    if is_proxy_local_mcp_host(host):
        return _PLAIN_SCHEMES
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return _TLS_SCHEMES
    if ip.is_loopback or any(ip in net for net in _LAN_NETWORKS):
        return _PLAIN_SCHEMES
    return _TLS_SCHEMES


def link_is_plain() -> bool:
    return link_schemes() == _PLAIN_SCHEMES


def bootstrap_url(public_host: str) -> str:
    """The address every machine's install command fetches its script from."""
    return f"{link_schemes()[0]}://{public_host}/v1/satellite/bootstrap"


# The names a container's /etc/hosts gives this machine (Docker writes every
# one of them; ip6-localhost and ip6-loopback resolve to ::1).
_HOSTS_NAMES = frozenset({
    "localhost", "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
    "ip6-allnodes", "ip6-allrouters",
})


def address_like(name: str) -> bool:
    """A host name the resolver answers without DNS: ``localhost`` (and its
    subdomains) and the other names ``/etc/hosts`` gives this machine, an
    address, or any numeric form ``inet_aton`` reads (all digits, ``0x``
    hex, octal, a dotted or partly dotted number, which glibc's
    ``getaddrinfo`` turns into an address, ``2852039166`` into
    169.254.169.254). A Docker tool is dialled by its name, so its name may
    never be one."""
    host = name.strip().lower().rstrip(".")
    if host in _HOSTS_NAMES or host.endswith(".localhost") or host.isdigit():
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
    except (OSError, ValueError):
        return False
    return True


def mcp_service_name(manifest) -> str:
    """The service-DNS name the proxy uses to reach this Docker MCP in T2.

    ``server.service_name`` when declared, else the manifest's canonical
    ``name``. The T2 compose rewrite guarantees this name resolves on the
    shared network (it is added as a network alias), so the name is stable
    regardless of the compose service key or ``container_name``.
    """
    srv = getattr(manifest, "server", None)
    declared = getattr(srv, "service_name", "") if srv is not None else ""
    return declared or getattr(manifest, "name", "")


def docker_mcp_host(manifest) -> str:
    """DNS host for the proxy/agent → Docker MCP hop.

    T1: ``localhost`` — the container publishes a loopback port on the host.
    T2: the service-DNS name — a sibling container on the shared network (no
    published host port).
    """
    if in_docker_compose():
        return mcp_service_name(manifest)
    return "localhost"


def proxy_callback_host() -> str:
    """Host a Docker MCP / Collabora uses to reach the proxy on its callbacks.

    T1: ``host.docker.internal`` — the host gateway, declared via the compose
    ``extra_hosts`` so a container can reach a host-process proxy.
    T2: the proxy's own service-DNS name on the shared network (the proxy is a
    sibling container, not on the host).
    """
    if in_docker_compose():
        return config.PROXY_SERVICE_NAME
    return "host.docker.internal"


def is_proxy_local_mcp_host(host: str, manifest=None) -> bool:
    """True if ``host`` is a Docker MCP running as a proxy-side sibling.

    These MCPs are *proxy-terminable*: the proxy can reach them on a private
    hop, so (a) their vendor bearer is lifted into the in-memory broker and
    replaced with a sentinel in the on-disk config, (b) on remote
    sessions their URL is rewritten to the satellite tunnel rather than dialled
    directly, and (c) the OAuth bearer allowlist treats them as the canonical
    loopback host. Vendor MCPs on an external public host (slack/linear/zoom)
    are NOT proxy-local — they must reach the internet directly.

    T1: the host is loopback (``localhost`` / ``127.0.0.1`` / ``::1``).
    T2: the host is the service-DNS name of a ``runtime: docker`` MCP. The
    manifest is required to make that determination — a bare service name is
    indistinguishable from an arbitrary external host without it. When
    ``manifest`` is omitted only the loopback test applies, which is the safe
    T1-equivalent default.
    """
    if host in _LOOPBACK_HOSTS:
        return True
    if manifest is not None and in_docker_compose():
        # a stdlib leaf; reached at call time
        from services.mcp import mcp_manifest_types as _mt
        srv = getattr(manifest, "server", None)
        if _mt.is_container(srv) and host == docker_mcp_host(manifest):
            return True
    return False


def docker_subprocess_env() -> dict[str, str]:
    """Extra env for ``docker_manager``'s ``docker compose`` subprocesses.

    T2 points ``DOCKER_HOST`` at the socket-proxy — the containerised proxy has
    no local ``/var/run/docker.sock`` and reaches the daemon over tcp through
    the read-restricted Tecnativa proxy. T1 returns ``{}`` so the subprocess
    talks to the host's local socket exactly as before (no env override).
    """
    if in_docker_compose():
        host = config.DOCKER_SOCKET_PROXY_HOST
        port = config.DOCKER_SOCKET_PROXY_PORT
        return {"DOCKER_HOST": f"tcp://{host}:{port}"}
    return {}
