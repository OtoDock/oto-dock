"""Boot tripwire: the DB and docker-socket-proxy control planes must not sit on
the shared ``otodock`` network.

The compose split puts Postgres on ``otodock-data`` and the
docker-socket-proxy on ``socketproxy``, both internal and joined only by the
proxy, so no sidecar or community MCP can reach 5432 or the Docker API. This
check catches an operator override (a hand-edited compose, a stale generated
artifact) that re-added either to ``OTODOCK_NETWORK``. It is a tripwire, not the
guarantee: the split itself is. So it NEVER blocks boot: it logs.

It also flags any container on the internal ``socketproxy`` or
``otodock-data`` plane that does not belong there (only the proxy, the control
plane itself and, on the data plane, ``otodock-db-init`` do): a community MCP
attached to one reaches the Docker API or Postgres directly.

Only meaningful in the Docker-Compose topology (T2): there the proxy drives the
daemon through the socket proxy, and we ask it which network each control-plane
container is on. On bare metal (T1) there is no socket proxy and Postgres is a
loopback service, so it is a clean no-op.
"""
from __future__ import annotations

import json
import logging
import urllib.request

import config
from core.config import deployment

logger = logging.getLogger("claude-proxy.network-selfcheck")

# Identify the control-plane containers by their compose service LABEL, never a
# name substring (a substring like "otodock" would also match otodock-proxy,
# which legitimately joins the shared network).
_CONTROL_SERVICES = frozenset({"otodock-postgres", "docker-socket-proxy"})
_TIMEOUT_S = 3.0

# The internal planes and the compose services that belong on each. Compose
# names a project network ``<project>_<key>``; the platform file pins its
# project to ``otodock``, and a second install on the daemon carries its own
# project label, so its planes and members are judged as its own.
_PLANE_MEMBERS = {
    "socketproxy": frozenset({"docker-socket-proxy", "otodock-proxy"}),
    "otodock-data": frozenset({"otodock-postgres", "otodock-db-init", "otodock-proxy"}),
}
_PLATFORM_PROJECT = "otodock"
_PLATFORM_SERVICE_LABELS = _CONTROL_SERVICES | {"otodock-proxy"}


def _stray_plane_members(containers: list) -> list[str]:
    """``name (network)`` for each container attached to an internal plane
    it does not belong on."""
    projects = {_PLATFORM_PROJECT}
    for c in containers:
        labels = c.get("Labels") or {}
        if labels.get("com.docker.compose.service") in _PLATFORM_SERVICE_LABELS:
            project = labels.get("com.docker.compose.project")
            if isinstance(project, str) and project:
                projects.add(project)
    planes = {
        f"{project}_{plane}": (project, members)
        for project in projects
        for plane, members in _PLANE_MEMBERS.items()
    }
    stray: list[str] = []
    for c in containers:
        labels = c.get("Labels") or {}
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        for net in nets:
            if net not in planes:
                continue
            project, members = planes[net]
            if (
                labels.get("com.docker.compose.project") == project
                and labels.get("com.docker.compose.service") in members
            ):
                continue
            names = c.get("Names") or [c.get("Id") or "?"]
            stray.append(f"{str(names[0]).lstrip('/')} ({net})")
    return sorted(stray)


def network_selfcheck() -> None:
    """Warn loudly if a control plane is on ``OTODOCK_NETWORK``. Never raises."""
    if deployment.current_mode() != deployment.MANAGED_SOCKPROX:
        logger.info("network self-check: not a Docker-Compose deployment; skipped")
        return

    net = config.OTODOCK_NETWORK
    url = (
        f"http://{config.DOCKER_SOCKET_PROXY_HOST}:{config.DOCKER_SOCKET_PROXY_PORT}"
        "/containers/json?all=true"
    )
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:  # noqa: S310 (fixed internal host)
            containers = json.load(resp)
    except Exception as e:
        # Inconclusive (socket proxy not ready yet, API error, timeout): the
        # split is the guarantee, so a transient hiccup must never brick boot.
        logger.warning(
            "network self-check: could not query the docker socket proxy (%s); "
            "skipped. The compose network split is the real guarantee; this is "
            "only a tripwire.", e,
        )
        return

    if not isinstance(containers, list):
        containers = []
    leaked: set[str] = set()
    for c in containers:
        svc = (c.get("Labels") or {}).get("com.docker.compose.service")
        if svc in _CONTROL_SERVICES:
            nets = ((c.get("NetworkSettings") or {}).get("Networks") or {})
            if net in nets:
                leaked.add(svc)

    if leaked:
        logger.error(
            "network self-check: %s attached to the shared %r network; a sidecar "
            "or community MCP can now reach the control plane (Postgres :5432 / the "
            "docker socket proxy :2375). Remove it from %r; only otodock-proxy "
            "should bridge the socketproxy / otodock-data networks.",
            sorted(leaked), net, net,
        )
    else:
        logger.info(
            "network self-check: control planes are off the shared %r network", net,
        )

    stray = _stray_plane_members(containers)
    if stray:
        logger.error(
            "network self-check: %s attached to an internal platform plane, from "
            "where it reaches the docker socket proxy :2375 or Postgres :5432. Only "
            "otodock-proxy and docker-socket-proxy belong on socketproxy, and only "
            "otodock-proxy, otodock-postgres and otodock-db-init on otodock-data; "
            "remove the container from the plane.", stray,
        )
