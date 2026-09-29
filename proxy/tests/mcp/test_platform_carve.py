"""The sandbox carve never reaches a platform service in compose mode: the
document-sidecar plane (``OTODOCK_INTERNAL_SUBNET``) is carved only for the
platform's own file-tools, and the phone daemon's address is never carved
(it holds the phone secret the proxy sends and an unauthenticated
AudioSocket)."""

from __future__ import annotations

import pytest

import config
from core.config import deployment
from services.mcp import mcp_registry
from services.mcp.mcp_registry import McpManifest, NetworkTargetDecl, ServerConfig


def _homelab(name: str = "prom") -> McpManifest:
    m = McpManifest.__new__(McpManifest)
    m.name = name
    m.server = ServerConfig(runtime="node", transport="stdio")
    m.network_targets = [NetworkTargetDecl("config", "URL", port_default=9090)]
    m.network_access_default = True
    m.placement = "any"
    m.requires_capability = None
    m.category = "community"
    m.mcp_dir = config.MCPS_DIR / "community" / name
    return m


def _docker(name: str, service: str, category: str) -> McpManifest:
    m = McpManifest.__new__(McpManifest)
    m.name = name
    m.server = ServerConfig(runtime="docker", transport="http", port=8932,
                            service_name=service)
    m.network_targets = []
    m.network_access_default = True
    m.placement = "any"
    m.requires_capability = None
    m.category = "core" if category == "custom" else "community"
    m.mcp_dir = config.MCPS_DIR / category / name
    return m


@pytest.fixture
def compose(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCPS_DIR", tmp_path / "mcps")
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(mcp_registry, "_platform_targets_cache", {})
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@otodock-postgres:5432/x")
    monkeypatch.setattr(config, "DOCKER_SOCKET_PROXY_HOST", "docker-socket-proxy")
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://otodock-phone:9093")
    monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda mm: True)
    monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
    monkeypatch.setattr(mcp_registry, "_is_local_host_ip", lambda ip: False)
    dns: dict[str, list[str]] = {
        "otodock-postgres": ["10.203.0.2"],
        "docker-socket-proxy": ["10.202.0.2"],
        "otodock-phone": ["10.200.0.7"],
    }
    monkeypatch.setattr(mcp_registry, "_resolve_to_ips", lambda h: list(dns.get(h, [])))
    return dns


def _egress(monkeypatch, manifests, targets=()):
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: list(manifests))
    monkeypatch.setattr(mcp_registry, "enumerate_mcp_network_targets",
                        lambda mm, a, **k: list(targets))
    return mcp_registry.resolve_sandbox_egress("pa")[1]


def test_a_homelab_target_on_the_sidecar_plane_is_not_carved(monkeypatch, compose):
    compose["target.lan"] = ["10.204.0.7", "10.204.0.200", "10.200.0.20"]
    allow = _egress(monkeypatch, [_homelab()], [("target.lan", 9090)])
    assert allow == ["10.200.0.20"]


def test_the_platform_file_tools_is_carved_on_the_sidecar_plane(monkeypatch, compose):
    compose["file-tools"] = ["10.204.0.3"]
    allow = _egress(monkeypatch, [_docker("file-tools", "file-tools", "custom")])
    assert allow == ["10.204.0.3"]


def test_a_community_docker_mcp_on_the_sidecar_plane_is_not_carved(monkeypatch, compose):
    compose["lookalike"] = ["10.204.0.4"]
    allow = _egress(monkeypatch, [_docker("lookalike", "lookalike", "community")])
    assert allow == []


def test_a_community_docker_mcp_on_the_shared_network_is_carved(monkeypatch, compose):
    compose["camoufox"] = ["10.200.0.30"]
    allow = _egress(monkeypatch, [_docker("camoufox", "camoufox", "community")])
    assert allow == ["10.200.0.30"]


def test_the_phone_daemon_address_is_not_carved(monkeypatch, compose):
    compose["target.lan"] = ["10.200.0.7", "10.200.0.20"]
    assert _egress(monkeypatch, [_homelab()], [("target.lan", 9090)]) == ["10.200.0.20"]
    # a Docker MCP whose service name resolves to the phone is refused too
    compose["phone-alias"] = ["10.200.0.7"]
    assert _egress(monkeypatch, [_docker("x", "phone-alias", "community")]) == []


def test_the_phone_address_is_read_again_when_the_daemon_moves(monkeypatch, compose):
    compose["target.lan"] = ["10.200.0.7", "10.200.0.8"]
    assert _egress(monkeypatch, [_homelab()], [("target.lan", 9090)]) == ["10.200.0.8"]
    compose["otodock-phone"] = ["10.200.0.8"]  # the phone container was recreated
    assert _egress(monkeypatch, [_homelab()], [("target.lan", 9090)]) == ["10.200.0.7"]


def test_the_sidecar_plane_follows_its_config_key(monkeypatch, compose):
    real_cfg = config._cfg
    monkeypatch.setattr(config, "_cfg", lambda k, d="": (
        "10.99.0.0/24" if k == "OTODOCK_INTERNAL_SUBNET" else real_cfg(k, d)))
    compose["target.lan"] = ["10.99.0.5", "10.204.0.5"]
    assert _egress(monkeypatch, [_homelab()], [("target.lan", 9090)]) == ["10.204.0.5"]


def test_bare_metal_keeps_its_rules(monkeypatch, compose):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
    compose["target.lan"] = ["10.204.0.7"]
    assert _egress(monkeypatch, [_homelab()], [("target.lan", 9090)]) == ["10.204.0.7"]
