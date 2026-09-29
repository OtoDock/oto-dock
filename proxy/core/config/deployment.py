"""Deployment-topology resolver — T1 bare-metal · T2 Docker-Compose · T3 cloud.

Single source of truth for the host-naming differences between the three
topologies:

  * how the proxy / a local agent reaches a Docker MCP's container,
  * how a Docker MCP (or Collabora) reaches the proxy on its callbacks,
  * which Docker daemon ``services/mcp/docker_manager.py`` drives.

Everything keys off ``config.RUNNING_IN_DOCKER`` + ``config.OTODOCK_CLOUD``. On
bare-metal (``RUNNING_IN_DOCKER`` False — the live native install) every helper
returns the historical ``localhost`` / ``host.docker.internal`` values, so T1
behaviour is byte-for-byte unchanged and the live proxy is unaffected.

The module imports only ``config`` (no MCP/registry types — manifests are
duck-typed via ``getattr``) so it stays a dependency-free leaf importable from
both ``core`` and ``services`` without cycles.
"""

from __future__ import annotations

import config

# Deployment modes — the "lifecycle mode" column of the architecture table.
MANAGED_LOCAL = "managed-local"        # T1 bare-metal: local docker socket
MANAGED_SOCKPROX = "managed-sockprox"  # T2 compose: docker-socket-proxy (tcp)
EXTERNAL_POOL = "external-pool"        # T3 cloud: no local lifecycle (pooled)

# Hosts that mean "the proxy itself / its own loopback". ``""`` covers a URL
# with no host (urlparse of a bare path), treated as loopback-equivalent.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def current_mode() -> str:
    """Resolve the active deployment topology from config flags.

    OTODOCK_CLOUD wins (a cloud control plane is never a single-host compose
    install even if it happens to run containerised); otherwise the
    bare-metal/compose split is RUNNING_IN_DOCKER.
    """
    if config.OTODOCK_CLOUD:
        return EXTERNAL_POOL
    if config.RUNNING_IN_DOCKER:
        return MANAGED_SOCKPROX
    return MANAGED_LOCAL


def in_docker_compose() -> bool:
    """True only in T2 — a containerised proxy driving a socket-proxy daemon."""
    return current_mode() == MANAGED_SOCKPROX


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
        from services.mcp import mcp_manifest_types as _mt  # a stdlib leaf; reached at call time
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
