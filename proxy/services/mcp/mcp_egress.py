"""Where a session's MCPs may reach on the network: the sandbox's egress rules,
the platform's own targets it refuses, and the host-self rewrites to loopback.
"""

import contextlib
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

import psycopg

import config
from core.config import deployment
from core.placement import LOCAL_PLACEMENT
from services.infra import pinned_http
from services.mcp.mcp_manifest_store import (
    get_agent_mcps,
    manifest_capability_available,
)
from services.mcp.mcp_manifest_types import McpManifest, NetworkTargetDecl
from services.mcp.mcp_session_filter import audience_admits
from services.oauth.credential_resolver import choice_kw
from storage.mcp import mcp_store

logger = logging.getLogger(__name__)


def _resolve_to_ips(host: str) -> list[str]:
    """Resolve a host (or pass through an IP literal) to its IP address(es).

    Runs in the proxy's namespace at sandbox-build time. In T2 this resolves a
    Docker service name (via the embedded DNS) and ``host.docker.internal`` (via
    the container's /etc/hosts). Returns [] on failure (fail-closed → no carve).
    """
    import ipaddress
    import socket
    host = (host or "").strip()
    if not host:
        return []
    # Not an IP literal → fall through to DNS resolution.
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return [host]  # already a literal
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos})  # type: ignore[misc]  # typeshed types getaddrinfo()[4][0] as str | int; it is a str for AF_INET and AF_INET6


def _is_carveable_ip(ip: str) -> bool:
    """Whether an egress carve-out is needed for this IP.

    Only private / link-local IPs need un-blackholing (public IPs ride NAT'd
    outbound already, so carving them is redundant). The cloud-metadata
    endpoints are NEVER carved, even if a target resolves to one.
    """
    import ipaddress
    _METADATA = {"169.254.169.254", "169.254.170.2", "fd00:ec2::254"}
    if ip in _METADATA:
        return False
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return obj.is_private or obj.is_link_local


def _is_local_host_ip(ip: str) -> bool:
    """True if ``ip`` is one of the proxy HOST's own addresses (binding succeeds).

    Such an IP is *local* inside the agent's isolated netns (pasta copies the
    host's address, so it routes to loopback) — a route carve can't reach a
    service there. Instead the loopback ``-T`` splice + a ``127.0.0.1`` rewrite
    of the MCP's target (see ``_rewrite_host_self_targets_to_loopback``) reach a
    co-located service. T1 concern only — in T2 the proxy is a container.
    """
    import socket
    if not ip:
        # An empty host would bind all interfaces — never a "local host IP".
        # Callers only pass resolved literals (_resolve_to_ips), so this is
        # defensive; it also keeps the probe from ever binding wildcard.
        return False
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


class _PlatformTargets(NamedTuple):
    """What a sandbox may never reach through a carve or a splice."""
    compose: bool
    networks: tuple            # control-plane networks (compose only)
    addresses: frozenset[str]  # platform service addresses (compose only)
    ports: frozenset[int]      # local platform listeners (never spliced)
    # The document-sidecar plane (file-tools, Collabora): carved only for a
    # platform-shipped Docker MCP (compose only).
    sidecar_networks: tuple = ()
    # Platform hosts that start after the proxy and move when their container
    # is recreated (the phone daemon): resolved at every egress computation
    # (compose only).
    late_hosts: tuple[str, ...] = ()


# The phone daemon's AudioSocket listener has no config key: the phone
# overlay publishes it on the bind address next to the HTTP API.
_AUDIOSOCKET_PORT = 9092

_platform_targets_cache: dict[bool, _PlatformTargets] = {}


def _host_is_local(host: str) -> bool:
    import ipaddress
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    for ip in _resolve_to_ips(host):
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(ip).is_loopback:
                return True
        if _is_local_host_ip(ip):
            return True
    return False


def _platform_targets() -> _PlatformTargets:
    """Computed once per process and mode (this runs on the loop at session
    start): the control-plane networks, the document-sidecar plane and the
    database and socket-proxy addresses in compose mode, the phone daemon's
    host to resolve per computation, and the ports of the platform listeners
    on this host and the ports that front the proxy, which the bare-metal
    loopback splice must never open."""
    from urllib.parse import urlsplit
    compose = deployment.in_docker_compose()
    cached = _platform_targets_cache.get(compose)
    if cached is not None:
        return cached
    networks: list = []
    sidecar_networks: list = []
    late_hosts: list[str] = []
    addresses: set[str] = set()
    ports: set[int] = set()
    try:
        from psycopg.conninfo import conninfo_to_dict
        db = conninfo_to_dict(config.DATABASE_URL)
    except (ImportError, psycopg.Error):
        db = {}
    db_hosts = [h.strip() for h in str(db.get("host") or "localhost").split(",") if h.strip()]
    try:
        db_port = int(db.get("port") or 5432)
    except (TypeError, ValueError):
        db_port = 5432
    phone = urlsplit(config.PHONE_SERVER_URL or "")
    if compose:
        # The compose interpolation variables never reach the container's
        # environment; ``config.env`` is the bind-mounted ``.env`` there.
        for key, default in (("OTODOCK_SOCKETPROXY_SUBNET", "10.202.0.0/24"),
                             ("OTODOCK_DATA_SUBNET", "10.203.0.0/24")):
            networks.append(pinned_http.subnet_setting(key, default))
        sidecar_networks.append(pinned_http.subnet_setting("OTODOCK_INTERNAL_SUBNET", "10.204.0.0/24"))
        for host in (*db_hosts, config.DOCKER_SOCKET_PROXY_HOST):
            addresses.update(_resolve_to_ips(host))
        if phone.hostname:
            late_hosts.append(phone.hostname)
        ports.add(int(config.DOCKER_SOCKET_PROXY_PORT))
    if any(_host_is_local(h) for h in db_hosts):
        ports.add(db_port)
    if phone.hostname and _host_is_local(phone.hostname):
        ports.add(phone.port or 9093)
        ports.add(_AUDIOSOCKET_PORT)
    if not compose:
        # The ports that front the proxy on this host: its own listener
        # (the hook forward already lands the sandbox's 127.0.0.1:PORT on
        # the internal listener) and, when the public URL's host is this
        # host, the public URL's port (an edge here forwards to the main
        # listener, where a loopback hop's X-Forwarded-For is walked).
        ports.add(int(config.PORT))
        pub = urlsplit(config.DASHBOARD_PUBLIC_URL or "")
        if pub.hostname and _host_is_local(pub.hostname):
            with contextlib.suppress(ValueError):
                ports.add(pub.port or (443 if pub.scheme == "https" else 80))
    targets = _PlatformTargets(
        compose, tuple(networks), frozenset(addresses), frozenset(ports),
        tuple(sidecar_networks), tuple(late_hosts),
    )
    _platform_targets_cache[compose] = targets
    return targets


def _is_platform_ip(
    ip: str, targets: _PlatformTargets, *,
    late_addresses: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Loopback in every mode; in compose mode also the control-plane
    networks, the document-sidecar plane, the service addresses, the late
    hosts' current addresses and the proxy container's own addresses. A
    mapped IPv4 form is judged as its IPv4 address."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_loopback or str(addr) in targets.addresses or str(addr) in late_addresses:
        return True
    if any(addr in net for net in targets.networks):
        return True
    if any(addr in net for net in targets.sidecar_networks):
        return True
    return targets.compose and _is_local_host_ip(str(addr))


def carves_targets(manifest: McpManifest) -> bool:
    """Whether a session's sandbox opens a path to this MCP's configured
    address: it declares network targets, runs wherever the session runs
    (placement ``any``), its platform feature is configured and its switch
    is on. The one test of the carve (``resolve_sandbox_egress``), the
    host-self rewrite and the placement rule (``reaches_local_address``), so
    where a session runs and what its sandbox opens never part."""
    return (bool(manifest.network_targets) and manifest.placement == "any"
            and manifest_capability_available(manifest) and network_access_enabled(manifest))


def reaches_local_address(agent_name: str) -> bool:
    """Whether a session of the agent opens a path to an address of the
    platform's own network: one of its MCPs passes ``carves_targets``. The
    platform works that path out on its own host, so such a session runs on
    a compute machine beside the platform (``remote_status.keeps_beside``).
    Per agent, whoever the session's person is. Blocking: store reads."""
    return any(carves_targets(m) for m in get_agent_mcps(agent_name, placement=LOCAL_PLACEMENT) or [])


def resolve_sandbox_egress(
    agent_name: str, *, user_sub: str = "", extra_targets: list[str] | None = None,
    account_choices: Mapping[str, str] | None = None,
    attached: Iterable[str] | None = None, own_copy: bool = False,
) -> tuple[list[str], list[str]]:
    """Egress allow-set for a local session's isolated sandbox netns.

    Returns ``(forwards, allow_hosts)`` for ``scripts/oto-sandbox-net``:
      * ``forwards`` — loopback ports spliced via pasta ``-T``: the proxy hook
        port (always — the permission gate POSTs to ``127.0.0.1:{PORT}``) plus
        each **T1** Docker-MCP's host-loopback-published port.
      * ``allow_hosts`` — routable IPs carved back out of the blackholes (the
        host's own subnet is now blocked): **T2** Docker-MCP container IPs
        (service-DNS resolved), and every homelab MCP's configured target host
        (when its ``_network_access`` toggle is on). Public targets are skipped
        (already reachable); Postgres / other siblings / the rest of the LAN
        stay blocked.

    The local placement (fail-closed): a ``satellite_only`` MCP never runs
    locally, so it contributes nothing here. Deterministic order for stable
    argv + golden tests. ``account_choices`` (the chat's choice of account
    per MCP) picks the account a per-user target is read from. ``attached``
    (the names the session's build attached) keeps the carve to those MCPs:
    a tool the session was refused gets no route. ``own_copy``: the session
    runs on a compute machine on another server, whose network the carve
    would open in place of the platform's: no tool address is opened there
    (``remote_status.keeps_beside`` keeps such an agent's sessions beside the
    platform; this holds the rule for a session that still lands there).
    """
    from urllib.parse import urlparse

    forwards: list[str] = [str(config.PORT)]
    fseen: set[str] = {str(config.PORT)}
    allow_hosts: list[str] = []
    aseen: set[str] = set()
    targets = _platform_targets()

    def _add_forward(port) -> None:
        # Judged before the dedup: the hook forward of config.PORT sits in
        # ``forwards`` already, so a target on it is refused aloud instead of
        # passing silently (the hook forward itself stays).
        s = str(port)
        try:
            refused = int(port) in targets.ports
        except (TypeError, ValueError):
            refused = True
        if refused:
            logger.warning(
                "egress for %s: port %s is a platform listener on this host; "
                "not spliced", agent_name, s,
            )
            return
        if s in fseen:
            return
        fseen.add(s)
        forwards.append(s)

    late: set[str] | None = None

    def _late_addresses() -> set[str]:
        nonlocal late
        if late is None:
            late = {ip for h in targets.late_hosts for ip in _resolve_to_ips(h)}
        return late

    def _add_allow(host: str) -> None:
        for ip in _resolve_to_ips(host):
            if ip in aseen or not _is_carveable_ip(ip):
                continue
            if _is_platform_ip(ip, targets, late_addresses=_late_addresses()):
                logger.warning(
                    "egress for %s: %s resolves to %s, a platform control-plane "
                    "address; not carved", agent_name, host, ip,
                )
                continue
            aseen.add(ip)
            allow_hosts.append(ip)

    mcps = get_agent_mcps(agent_name, placement=LOCAL_PLACEMENT) or []
    if attached is not None:
        keep = set(attached)
        mcps = [m for m in mcps if m.name in keep]
    # The person's tier decides which audience-gated MCPs carve anything:
    # a viewer's sandbox gets no route to the SSH hosts an editor's does.
    if user_sub:
        from auth import providers
        role = providers.effective_role_of(user_sub, agent_name)
        mcps = [m for m in mcps if audience_admits(getattr(m, 'audience', ''), role)]

    # A Docker tool is never carved: its entry is the credential gateway's
    # route on the proxy port (``mcp_server_resolve.gateway_entry``).

    # (The phone daemon needs no carve here anymore: phone-mcp calls the
    # proxy's /v1/phone/calls relay over the standard PROXY_URL loopback
    # forward, and the proxy dials the daemon from outside the sandbox.)

    # Homelab MCP network targets — only for MCPs that declare them, are
    #    placement=any, capability-available, and have the admin toggle on.
    for manifest in mcps:
        if not carves_targets(manifest):
            continue
        if own_copy:
            logger.warning(
                "egress for %s: MCP %r reaches a local address and this session runs on a "
                "compute machine on another server; nothing carved", agent_name, manifest.name,
            )
            continue
        is_t2 = deployment.in_docker_compose()
        for host, port in enumerate_mcp_network_targets(
            manifest, agent_name, user_sub=user_sub, **choice_kw(account_choices),
        ):
            if host in ("localhost", "127.0.0.1", "::1"):
                if is_t2:
                    # localhost inside the container ≠ the Docker host — nothing
                    # to reach. The admin must use a LAN IP or host.docker.internal.
                    logger.warning(
                        "network target for MCP %r is %r in a containerized "
                        "install — unreachable; configure a LAN IP or "
                        "host.docker.internal", manifest.name, host,
                    )
                elif port:
                    _add_forward(port)  # T1 same-host service (loopback splice)
                continue
            ips = _resolve_to_ips(host)
            # A service on the proxy host itself (T1), addressed by one of the
            # host's own IPs, is local inside the netns → reached via the loopback
            # -T splice (the MCP's target URL is rewritten to 127.0.0.1 by
            # _rewrite_host_self_targets_to_loopback). In T2 the proxy is a
            # container, so a homelab IP is never the host itself.
            if (not is_t2) and port and any(_is_local_host_ip(ip) for ip in ips):
                _add_forward(port)
                continue
            _add_allow(host)

    # 3. Layer-supplied targets (e.g. a Codex local-LLM endpoint URL the agent
    #    dials from the sandbox). Same handling as a homelab target: loopback →
    #    forward the port (T1 same-host), routable → carve as an allow-host.
    #    None on a hosted install: a local model endpoint does not exist there.
    for t in ([] if config.OTODOCK_CLOUD else (extra_targets or [])):
        if not t:
            continue
        parsed = urlparse(t if "://" in t else f"//{t}")
        host = parsed.hostname or ""
        if not host:
            continue
        if host in ("localhost", "127.0.0.1", "::1"):
            if deployment.in_docker_compose():
                logger.warning(
                    "local endpoint %r is localhost in a containerized install — "
                    "unreachable; use a LAN IP or host.docker.internal", t)
                continue
            if parsed.port:
                _add_forward(parsed.port)
        else:
            _add_allow(host)

    forwards = [forwards[0], *sorted(forwards[1:])]
    return forwards, sorted(allow_hosts)


def network_access_of(manifest: McpManifest, values: dict[str, str]) -> bool:
    """Whether this MCP's internal-network access is on, from its config
    values (``values``, the MCP's ``mcp_config_values``): the one reader of
    the approval, pure, for a caller that already holds the values.

    Only meaningful for MCPs that declare ``network_targets`` (the sandbox
    carves their targets) or ``network_exit`` (their container opens its own
    local network). The admin override lives under the ``_network_access``
    control key (mirrors ``_hosted_service_mode``); absent → the manifest
    default (``network_access_default``, on by default). Off on a hosted
    install (``OTODOCK_CLOUD``), whatever is stored: there is no local
    network of the customer's there, and the route that sets it refuses.
    """
    if not (manifest.network_targets or manifest.network_exit) or config.OTODOCK_CLOUD:
        return False
    val = values.get("_network_access")
    if val is None:
        return manifest.network_access_default
    return val == "true"


def network_access_enabled(manifest: McpManifest) -> bool:
    """``network_access_of`` with the MCP's stored values."""
    if not (manifest.network_targets or manifest.network_exit) or config.OTODOCK_CLOUD:
        return False
    return network_access_of(manifest, mcp_store.get_mcp_config_values(manifest.name))


def _extract_host_port(
    value: str, port_key_val: str | None, port_default: int | None,
) -> tuple[str, int | None] | None:
    """Parse a config value (URL or bare ``host`` / ``host:port``) → (host, port).

    Schemeless bare hosts (UNIFI_HOST, ssh ``host``) parse via a ``//`` prefix
    so ``urlparse`` populates netloc; a value with a scheme is parsed as-is.
    Port falls back to ``port_key_val`` then ``port_default``.
    """
    from urllib.parse import urlparse
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = parsed.hostname or ""
    port = parsed.port
    if not host:
        return None
    if port is None:
        if port_key_val:
            try:
                port = int(str(port_key_val).strip())
            except (TypeError, ValueError):
                port = None
        if port is None:
            port = port_default
    return host, port


def enumerate_mcp_network_targets(
    manifest: McpManifest, agent_name: str,
    *, user_sub: str = "", task_scope: str = "user",
    account_choices: Mapping[str, str] | None = None,
) -> list[tuple[str, int | None]]:
    """Resolve the (host, port) internal-network targets an MCP is wired to dial.

    Reuses the same stores the session env-build reads (admin config, per-agent
    instances, the session user's credential, infra credential) so the carved
    hosts are exactly what the MCP connects to. Returns [] when nothing is
    configured (→ no carve-out). The session user's credential is the
    account ``pick_account`` picks with ``account_choices`` (the chat's
    choice of account per MCP).
    """
    if not manifest.network_targets:
        return []
    from services.mcp import account_context
    from services.oauth import credential_resolver
    from storage.identity import credential_store

    out: list[tuple[str, int | None]] = []
    seen: set[tuple[str, int | None]] = set()

    def _add(values: dict | None, decl: NetworkTargetDecl) -> None:
        raw = (values or {}).get(decl.host_key)
        if not raw:
            return
        port_val = (values or {}).get(decl.port_key) if decl.port_key else None
        hp = _extract_host_port(raw, port_val, decl.port_default)
        if hp and hp not in seen:
            seen.add(hp)
            out.append(hp)

    for decl in manifest.network_targets:
        try:
            if decl.source == "config":
                _add(mcp_store.get_mcp_config_values(manifest.name), decl)
            elif decl.source == "instance":
                # All authorized instances — superset of the env-delivery pick,
                # and the exact set written for config_file MCPs (ssh hosts.json).
                for inst in mcp_store.get_mcp_instances_for_agent(manifest.name, agent_name):
                    _add(inst.get("field_values"), decl)
            elif decl.source == "per_user_credential":
                # The account the session's tools run on: the person's own,
                # or on a Shared-only agent the agent's (reader_scope).
                if user_sub:
                    _scope, person = account_context.reader_scope(agent_name, user_sub)
                    ref = credential_resolver.pick_account(
                        manifest.name, agent_name, user_sub=person,
                        **choice_kw(account_choices),
                    )
                    if ref:
                        _add(credential_store.get_user_credentials(
                            ref.owner_sub, manifest.name, ref.label,
                        ), decl)
            elif decl.source == "infra_credential":
                _add(credential_store.get_infra_credentials(manifest.name), decl)
        except Exception as e:  # noqa: BLE001  # a bad target must never break spawn
            logger.warning(
                "network_targets: resolving %s for MCP %s failed: %s",
                decl.host_key, manifest.name, e,
            )
    return out


def _rewrite_host_self_targets_to_loopback(
    manifest: McpManifest, env: dict, agent_name: str,
    *, user_sub: str | None = "", task_scope: str = "user",
) -> None:
    """Rewrite a homelab MCP's configured target host → 127.0.0.1 (in place) when
    it points at one of the proxy HOST's own IPs — a service co-located with the
    proxy (T1). That IP is local inside the agent's isolated netns, so it's
    reached via the loopback ``-T`` splice (``resolve_sandbox_egress`` forwards
    the port), and the agent must dial 127.0.0.1. No-op in T2 (the proxy is a
    container — homelab IPs route via the NAT carve), when the per-MCP toggle is
    off, or for non-host targets.
    """
    if deployment.in_docker_compose() or not carves_targets(manifest):
        return
    for decl in manifest.network_targets:
        val = env.get(decl.host_key)
        if not val:
            continue
        hp = _extract_host_port(val, None, None)
        if not hp:
            continue
        host = hp[0]
        if host == "::1":
            env[decl.host_key] = _ipv4_loopback(val)
            continue
        if host in ("localhost", "127.0.0.1"):
            continue
        try:
            ips = _resolve_to_ips(host)
            if ips and any(_is_local_host_ip(ip) for ip in ips):
                env[decl.host_key] = val.replace(host, "127.0.0.1", 1)
        except Exception as e:  # noqa: BLE001  # never break spawn on a bad target
            logger.warning(
                "network_targets: host-self rewrite of %s for MCP %s failed: %s",
                decl.host_key, manifest.name, e,
            )


def _ipv4_loopback(value: str) -> str:
    """``value`` with its ``::1`` host written ``127.0.0.1``: the sandbox has
    no IPv6 loopback (``scripts/oto-sandbox-net``), so a host service reached
    through the loopback splice is dialed over IPv4."""
    if value.strip() == "::1":
        return "127.0.0.1"
    return value.replace("[::1]", "127.0.0.1", 1)


def loopback_if_host_self(url: str) -> str:
    """Rewrite a URL's host → 127.0.0.1 when it is one of the proxy HOST's own
    IPs (T1) — a service co-located with the proxy, reached via the loopback
    splice. Used for the Codex local-LLM endpoint (dialed from the sandbox like
    the homelab MCPs). No-op in T2, for loopback, or for remote hosts. See
    resolve_sandbox_egress (which forwards the port).
    """
    if not url or deployment.in_docker_compose():
        return url
    from urllib.parse import urlparse
    host = urlparse(url if "://" in url else f"//{url}").hostname or ""
    if host == "::1":
        return _ipv4_loopback(url)
    if not host or host in ("localhost", "127.0.0.1"):
        return url
    with contextlib.suppress(Exception):
        if any(_is_local_host_ip(ip) for ip in _resolve_to_ips(host)):
            return url.replace(host, "127.0.0.1", 1)
    return url


# --- A tool's build on the compute machine beside the platform ---------------
#
# That machine builds the community tools in its own sandbox, in a container
# (or a unit) that holds none of the platform's environment: no proxy
# variable, no private index, no trust file. ``build_egress_block`` is what a
# ``sync_mcps`` frame carries of them (``egress``): the installer's
# allowlisted names the platform holds (``sandbox_render.BUILD_EGRESS_NAMES``
# and ``BUILD_SETTING_NAMES``: every one but a cache folder, which is each
# host's own), the bytes of the trust files and of the certificate folder
# its environment names, and the carves the
# platform's own rule resolves (``platform_build_carves``). The machine applies
# them as given and resolves nothing itself: its rule for a host with no
# platform beside it (``sandbox_render.build_carves``) would open any private
# address.

#: The most of one trust file a frame carries, and how many files.
TRUST_FILE_LIMIT = 1 << 20
TRUST_FILES_MAX = 8
#: The most certificates of a certificate folder a frame carries. Their
#: bytes together are bounded as one trust file is.
TRUST_DIR_FILES_MAX = 512
#: A value longer than this is no proxy or index address.
VALUE_LIMIT = 4096


def platform_build_carves(env: dict[str, str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(forwards, allow_hosts)`` a step needs for the egress proxy and the
    private indexes its environment names: a service on this host's loopback
    spliced by its port (never a port of the platform's own), a private
    address carved (never a platform address, never the cloud's metadata).
    Public addresses need nothing: the sandbox reaches the internet."""
    from core.sandbox import sandbox_render
    targets = _platform_targets()
    # The internal listener, bound at the start, is read live: the cached
    # targets predate it.
    refused = set(targets.ports) | {p for p in (int(config.INTERNAL_LISTENER_PORT or 0),) if p}
    forwards: list[str] = []
    allow: list[str] = []
    for host, port in sandbox_render.build_endpoints(env):
        if _host_is_local(host):
            if not deployment.in_docker_compose() and port not in refused and str(port) not in forwards:
                forwards.append(str(port))
            continue
        for ip in _resolve_to_ips(host):
            if (_is_carveable_ip(ip) and ip not in allow
                    and not _is_platform_ip(ip, targets)):
                allow.append(ip)
    return tuple(forwards), tuple(allow)


def _trust_file(name: str, value: str) -> dict | None:
    """One trust file as the frame carries it, None for a folder, a file
    that cannot be read or one past ``TRUST_FILE_LIMIT`` (logged)."""
    import base64
    path = Path(value)
    try:
        if not path.is_file() or path.stat().st_size > TRUST_FILE_LIMIT:
            raise OSError("not a file of at most 1 MB")
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("the trust file %s names (%s) is not sent to the compute machine's builds: %s",
                       name, value, exc)
        return None
    return {"env": name, "name": path.name, "data_b64": base64.b64encode(data).decode()}


def _trust_dir(value: str) -> list[dict]:
    """A certificate folder as the frame carries it: the files a TLS library
    looks up there (``sandbox_render.BUILD_TRUST_DIR_FILE``), each link
    followed to its certificate. All of them or none: ``[]`` for a folder
    that cannot be read or holds more than the bounds (logged), since a
    part of a trust store would read as the whole of it."""
    import base64
    import re

    from core.sandbox import sandbox_render
    hashed = re.compile(sandbox_render.BUILD_TRUST_DIR_FILE)
    files: list[dict] = []
    total = 0
    try:
        for entry in sorted(e for e in Path(value).iterdir() if hashed.fullmatch(e.name)):
            if not entry.is_file():
                continue
            total += entry.stat().st_size
            if len(files) >= TRUST_DIR_FILES_MAX or total > TRUST_FILE_LIMIT:
                raise OSError(f"it holds more than {TRUST_DIR_FILES_MAX} certificates or 1 MB of them")
            files.append({"name": entry.name, "data_b64": base64.b64encode(entry.read_bytes()).decode()})
    except OSError as exc:
        logger.warning("the certificate folder %s names (%s) is not sent to the compute machine's builds: %s",
                       sandbox_render.BUILD_TRUST_DIR_NAME, value, exc)
        return []
    return files


def _trust_entries(env: Mapping[str, str]) -> tuple[list[dict], list[dict]]:
    """``(trust, cert_dir)``: the trust files and the certificate folder the
    platform's environment names (``sandbox_render.BUILD_TRUST_NAMES``), as a
    frame carries them to the install's own compute machine. Blocking."""
    from core.sandbox import sandbox_render
    trust: list[dict] = []
    cert_dir: list[dict] = []
    for name in sandbox_render.BUILD_TRUST_NAMES:
        value = next((v for k, v in env.items() if k.upper() == name), "")
        if not value:
            continue
        if name == sandbox_render.BUILD_TRUST_DIR_NAME:
            cert_dir = _trust_dir(value)
            continue
        entry = _trust_file(name, value) if len(trust) < TRUST_FILES_MAX else None
        if entry is not None:
            trust.append(entry)
    return trust, cert_dir


def exit_upstream_block() -> dict | None:
    """The ``egress_proxy`` block of the install's own compute machine's
    sign-in: the platform's outgoing proxy for https (``HTTPS_PROXY``, else
    ``ALL_PROXY``), its ``NO_PROXY``, and its trust files as builds get them,
    read from the same environment as the builds' block, so the machine's
    credential exit reaches the world the way the platform does. None when
    the platform names no proxy, and on a hosted install, whose checked
    transports never take one (``pinned_http.proxied``). Blocking."""
    from services.mcp import mcp_installer
    if config.OTODOCK_CLOUD:
        return None
    env = mcp_installer._install_env()

    def named(name: str) -> str:
        return next((v for k, v in env.items() if k.upper() == name and v), "").strip()
    https = named("HTTPS_PROXY") or named("ALL_PROXY")
    if not https or len(https) > VALUE_LIMIT:
        return None
    trust, cert_dir = _trust_entries(env)
    return {"https": https, "no_proxy": named("NO_PROXY")[:VALUE_LIMIT], "trust": trust, "cert_dir": cert_dir}


def build_egress_block() -> dict | None:
    """The ``egress`` block of a ``sync_mcps`` frame, None when the platform
    names no proxy, index, setting or trust. Blocking (a name lookup per
    endpoint, a file read per trust name)."""
    from core.sandbox import sandbox_render
    from services.mcp import mcp_installer
    env = mcp_installer._install_env()
    wanted = {n.upper() for n in (*sandbox_render.BUILD_EGRESS_NAMES, *sandbox_render.BUILD_SETTING_NAMES)}
    egress_env = {k: v for k, v in env.items() if k.upper() in wanted and v and len(v) <= VALUE_LIMIT}
    trust, cert_dir = _trust_entries(env)
    if not (egress_env or trust or cert_dir):
        return None
    forwards, allow = platform_build_carves(egress_env)
    block: dict = {"env": egress_env, "trust": trust, "forwards": list(forwards), "allow_hosts": list(allow)}
    if cert_dir:
        block["cert_dir"] = cert_dir
    return block
