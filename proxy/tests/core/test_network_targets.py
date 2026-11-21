"""Tests for the homelab MCP network-target enumeration + the sandbox egress
carve-out it feeds (part of the always-on sandbox-isolation work).
"""

import pytest

from services.mcp import mcp_egress, mcp_registry
from services.mcp.mcp_egress import _extract_host_port
from services.mcp.mcp_registry import (
    McpManifest,
    NetworkTargetDecl,
    ServerConfig,
    enumerate_mcp_network_targets,
    network_access_enabled,
)

# ---------------------------------------------------------------------------
# _extract_host_port — URL + schemeless host parsing
# ---------------------------------------------------------------------------

class TestExtractHostPort:
    def test_full_url(self):
        assert _extract_host_port("http://192.168.1.10:9090", None, 9090) == ("192.168.1.10", 9090)

    def test_url_uses_default_port_when_absent(self):
        assert _extract_host_port("http://prom.lan", None, 9090) == ("prom.lan", 9090)

    def test_schemeless_bare_host(self):
        assert _extract_host_port("192.168.1.50", None, 443) == ("192.168.1.50", 443)

    def test_schemeless_host_port(self):
        assert _extract_host_port("nas.lan:8443", None, 443) == ("nas.lan", 8443)

    def test_port_key_value_used(self):
        assert _extract_host_port("10.0.0.1", "8443", 443) == ("10.0.0.1", 8443)

    def test_empty_is_none(self):
        assert _extract_host_port("", None, 9090) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manifest(name, targets, *, default=True, placement="any"):
    m = McpManifest.__new__(McpManifest)
    m.name = name
    m.server = ServerConfig(runtime="node", transport="stdio")
    m.network_targets = targets
    m.network_access_default = default
    m.placement = placement
    m.requires_capability = None
    return m


# ---------------------------------------------------------------------------
# network_access_enabled — manifest default + admin override
# ---------------------------------------------------------------------------

class TestNetworkAccessEnabled:
    def test_no_targets_is_false(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values", lambda n: {})
        assert network_access_enabled(_manifest("x", [])) is False

    def test_manifest_default_on(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values", lambda n: {})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL")], default=True)
        assert network_access_enabled(m) is True

    def test_manifest_default_off(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values", lambda n: {})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL")], default=False)
        assert network_access_enabled(m) is False

    def test_admin_override_off(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values",
                            lambda n: {"_network_access": "false"})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL")], default=True)
        assert network_access_enabled(m) is False

    def test_admin_override_on(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values",
                            lambda n: {"_network_access": "true"})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL")], default=False)
        assert network_access_enabled(m) is True


# ---------------------------------------------------------------------------
# enumerate_mcp_network_targets — per source
# ---------------------------------------------------------------------------

class TestEnumerateTargets:
    def test_config_source(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values",
                            lambda n: {"PROMETHEUS_URL": "http://192.168.1.10:9090"})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        assert enumerate_mcp_network_targets(m, "pa") == [("192.168.1.10", 9090)]

    def test_instance_source_field_values_nesting(self, monkeypatch):
        # Values live under the nested ["field_values"] sub-dict.
        monkeypatch.setattr(
            mcp_registry.mcp_store, "get_mcp_instances_for_agent",
            lambda n, a: [{"field_values": {"UNIFI_HOST": "10.0.0.1", "UNIFI_PORT": "8443"}}],
        )
        m = _manifest("unifi", [NetworkTargetDecl("instance", "UNIFI_HOST",
                                                  port_key="UNIFI_PORT", port_default=443)])
        assert enumerate_mcp_network_targets(m, "pa") == [("10.0.0.1", 8443)]

    def test_instance_multi_host_ssh(self, monkeypatch):
        monkeypatch.setattr(
            mcp_registry.mcp_store, "get_mcp_instances_for_agent",
            lambda n, a: [
                {"field_values": {"host": "192.168.1.5", "port": "22"}},
                {"field_values": {"host": "192.168.1.6", "port": "2222"}},
            ],
        )
        m = _manifest("ssh", [NetworkTargetDecl("instance", "host", port_key="port", port_default=22)])
        assert enumerate_mcp_network_targets(m, "pa") == [
            ("192.168.1.5", 22), ("192.168.1.6", 2222),
        ]

    def test_per_user_credential_source(self, monkeypatch):
        class _Ref:
            label = "default"
            owner_sub = "u1"
        from services.oauth import credential_resolver
        from storage.identity import credential_store
        monkeypatch.setattr(credential_resolver, "pick_account", lambda *a, **k: _Ref())
        monkeypatch.setattr(credential_store, "get_user_credentials",
                            lambda u, n, l: {"NEXTCLOUD_URL": "https://cloud.lan"})
        m = _manifest("nextcloud", [NetworkTargetDecl("per_user_credential", "NEXTCLOUD_URL", port_default=443)])
        assert enumerate_mcp_network_targets(m, "pa", user_sub="u1") == [("cloud.lan", 443)]

    def test_per_user_credential_needs_user_sub(self, monkeypatch):
        m = _manifest("nextcloud", [NetworkTargetDecl("per_user_credential", "NEXTCLOUD_URL")])
        # No user_sub → nothing resolved (agent-scope session).
        assert enumerate_mcp_network_targets(m, "pa", user_sub="") == []

    def test_no_targets_returns_empty(self):
        assert enumerate_mcp_network_targets(_manifest("x", []), "pa") == []

    def test_missing_value_is_skipped(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values", lambda n: {})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL")])
        assert enumerate_mcp_network_targets(m, "pa") == []


# ---------------------------------------------------------------------------
# resolve_sandbox_egress — homelab carve-out path
# ---------------------------------------------------------------------------

class TestEgressHomelabCarve:
    def test_homelab_target_carved_when_enabled(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets",
                            lambda mm, a, **k: [("192.168.1.10", 9090)])
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert "192.168.1.10" in allow

    def test_homelab_target_skipped_when_toggle_off(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: False)
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert allow == []

    def test_satellite_only_excluded(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("dev", [NetworkTargetDecl("config", "X", port_default=1)], placement="satellite_only")
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert allow == []

    def test_public_target_not_carved(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("nc", [NetworkTargetDecl("config", "NEXTCLOUD_URL", port_default=443)])
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        # A public hostname literal that won't resolve to a private IP.
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets",
                            lambda mm, a, **k: [("8.8.8.8", 443)])
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert "8.8.8.8" not in allow  # public → already reachable, not carved

    def test_host_self_target_forwarded_not_carved(self, monkeypatch):
        """A service on the proxy host itself (T1), addressed by a host IP, is
        loopback-spliced (-T port), NOT route-carved — that IP is local in the
        netns. (The MCP's URL is rewritten to 127.0.0.1 separately.)"""
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets",
                            lambda mm, a, **k: [("192.168.0.50", 9090)])
        monkeypatch.setattr(mcp_egress, "_resolve_to_ips", lambda h: ["192.168.0.50"])
        monkeypatch.setattr(mcp_egress, "_is_local_host_ip", lambda ip: ip == "192.168.0.50")
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert "9090" in forwards          # loopback -T splice
        assert "192.168.0.50" not in allow  # NOT route-carved (it's local)


class TestHostSelfRewrite:
    def test_host_self_url_rewritten_to_loopback(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "_resolve_to_ips", lambda h: ["192.168.0.50"])
        monkeypatch.setattr(mcp_egress, "_is_local_host_ip", lambda ip: ip == "192.168.0.50")
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://192.168.0.50:9090"}
        mcp_egress._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://127.0.0.1:9090"

    def test_remote_url_not_rewritten(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "_resolve_to_ips", lambda h: ["192.168.30.12"])
        monkeypatch.setattr(mcp_egress, "_is_local_host_ip", lambda ip: False)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://192.168.30.12:9090"}
        mcp_egress._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://192.168.30.12:9090"  # remote → untouched

    def test_ipv6_loopback_target_dials_ipv4(self, monkeypatch):
        """The sandbox has no IPv6 loopback: a host service reached through
        the loopback splice is dialed on 127.0.0.1."""
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://[::1]:9090/api"}
        mcp_egress._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://127.0.0.1:9090/api"
        assert mcp_registry.loopback_if_host_self("http://[::1]:8080/v1") == "http://127.0.0.1:8080/v1"
        assert mcp_registry.loopback_if_host_self("http://localhost:8080/v1") == "http://localhost:8080/v1"
        assert mcp_egress._ipv4_loopback("::1") == "127.0.0.1"

    def test_no_rewrite_in_t2(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)  # T2
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://192.168.0.50:9090"}
        mcp_egress._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://192.168.0.50:9090"  # T2 → no rewrite


# ---------------------------------------------------------------------------
# The platform's own control plane is never carved or spliced
# ---------------------------------------------------------------------------

class TestPlatformRefusal:
    """The carve never re-opens a route into the platform's control-plane
    networks or to a platform service address, and the bare-metal splice
    never opens a platform listener's port."""

    def _homelab(self, monkeypatch, ips, *, compose, port=9090, local_ips=()):
        import config
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: compose)
        monkeypatch.setattr(mcp_egress, "_platform_targets_cache", {})
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@dbhost:5432/x")
        monkeypatch.setattr(config, "DOCKER_SOCKET_PROXY_HOST", "docker-socket-proxy")
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=port)])
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets",
                            lambda mm, a, **k: [("target.lan", port)])
        monkeypatch.setattr(mcp_egress, "_resolve_to_ips", lambda h: list(ips.get(h, [])))
        monkeypatch.setattr(mcp_egress, "_is_local_host_ip", lambda ip: ip in local_ips)
        return mcp_registry.resolve_sandbox_egress("pa")

    def test_compose_control_plane_networks_are_not_carved(self, monkeypatch):
        forwards, allow = self._homelab(monkeypatch, {
            "target.lan": ["10.203.0.5", "10.202.0.8", "10.204.0.7", "10.200.0.20"],
        }, compose=True)
        assert allow == ["10.200.0.20"]

    def test_compose_service_addresses_are_not_carved(self, monkeypatch):
        forwards, allow = self._homelab(monkeypatch, {
            "dbhost": ["10.200.0.4"], "docker-socket-proxy": ["10.200.0.5"],
            "target.lan": ["10.200.0.4", "10.200.0.5", "10.200.0.6"],
        }, compose=True)
        assert allow == ["10.200.0.6"]

    def test_the_proxy_container_own_addresses_are_not_carved(self, monkeypatch):
        forwards, allow = self._homelab(monkeypatch, {
            "target.lan": ["10.200.0.9", "10.200.0.10"],
        }, compose=True, local_ips=("10.200.0.9",))
        assert allow == ["10.200.0.10"]

    @pytest.mark.parametrize("compose", [True, False])
    def test_loopback_and_mapped_forms_are_never_carved(self, monkeypatch, compose):
        forwards, allow = self._homelab(monkeypatch, {
            "target.lan": ["127.0.0.1", "::1", "::ffff:10.203.0.5", "::ffff:127.0.0.1"],
        }, compose=compose)
        # A mapped form is judged as its IPv4 address: a control-plane
        # address in compose mode, an ordinary private one on bare metal.
        assert allow == ([] if compose else ["::ffff:10.203.0.5"])

    def test_bare_metal_does_not_apply_the_compose_networks(self, monkeypatch):
        forwards, allow = self._homelab(monkeypatch, {
            "target.lan": ["10.203.0.5"],
        }, compose=False)
        assert allow == ["10.203.0.5"]

    def test_bare_metal_never_splices_a_platform_listener_port(self, monkeypatch):
        import config
        # A homelab target that names the proxy host itself on the database
        # port is a splice request for the local Postgres: refused.
        monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://127.0.0.1:9093")
        forwards, _allow = self._homelab(monkeypatch, {
            "dbhost": ["127.0.0.1"], "target.lan": ["192.168.0.50"],
        }, compose=False, port=5432, local_ips=("192.168.0.50",))
        assert "5432" not in forwards
        for port in (9093, 9092):
            forwards, _allow = self._homelab(monkeypatch, {
                "dbhost": ["127.0.0.1"], "target.lan": ["192.168.0.50"],
            }, compose=False, port=port, local_ips=("192.168.0.50",))
            assert str(port) not in forwards
        forwards, _allow = self._homelab(monkeypatch, {
            "dbhost": ["127.0.0.1"], "target.lan": ["192.168.0.50"],
        }, compose=False, port=9090, local_ips=("192.168.0.50",))
        assert "9090" in forwards

    def test_bare_metal_never_splices_the_public_urls_port_on_this_host(self, monkeypatch):
        import config
        # An edge on this host fronts the proxy on the public URL's port.
        monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://otodock.lan")
        forwards, _ = self._homelab(monkeypatch, {
            "otodock.lan": ["192.168.0.50"], "target.lan": ["192.168.0.50"],
        }, compose=False, port=443, local_ips=("192.168.0.50",))
        assert "443" not in forwards

    def test_an_explicit_public_port_is_refused_too(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://otodock.lan:8443")
        forwards, _ = self._homelab(monkeypatch, {
            "otodock.lan": ["192.168.0.50"], "target.lan": ["192.168.0.50"],
        }, compose=False, port=8443, local_ips=("192.168.0.50",))
        assert "8443" not in forwards

    def test_a_public_host_elsewhere_keeps_its_port_spliceable(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://otodock.example.com")
        forwards, _ = self._homelab(monkeypatch, {
            "otodock.example.com": ["203.0.113.7"], "target.lan": ["192.168.0.50"],
        }, compose=False, port=443, local_ips=("192.168.0.50",))
        assert "443" in forwards

    def test_a_target_on_the_proxy_port_is_refused_and_the_hook_forward_stays(
            self, monkeypatch, caplog):
        import config
        forwards, _ = self._homelab(monkeypatch, {"target.lan": ["192.168.0.50"]},
                                    compose=False, port=config.PORT,
                                    local_ips=("192.168.0.50",))
        assert forwards == [str(config.PORT)]
        assert "platform listener" in caplog.text


# ---------------------------------------------------------------------------
# A hosted install (OTODOCK_CLOUD) opens no private address by these paths
# ---------------------------------------------------------------------------

class TestHostedInstall:
    """Each path fails first without the flag: the broken form of every
    check below is the same call with the flag off."""

    @pytest.fixture
    def hosted(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
        return config

    def test_the_approval_reads_off_whatever_is_stored(self, monkeypatch, hosted):
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values",
                            lambda n: {"_network_access": "true"})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL")], default=True)
        assert network_access_enabled(m) is False
        assert mcp_egress.network_access_of(m, {"_network_access": "true"}) is False
        assert mcp_egress.network_access_of(m, {}) is False
        monkeypatch.setattr(hosted, "OTODOCK_CLOUD", False)
        assert network_access_enabled(m) is True, "the broken form: on without the flag"

    def test_no_homelab_carve_with_the_approval_stored_on(self, monkeypatch, hosted):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
        monkeypatch.setattr(mcp_egress, "_platform_targets_cache", {})
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values",
                            lambda n: {"_network_access": "true"})
        m = _manifest("ssh", [NetworkTargetDecl("config", "HOSTS", port_default=22)])
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets",
                            lambda mm, a, **k: [("192.168.180.11", 22)])
        _forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert "192.168.180.11" not in allow
        monkeypatch.setattr(hosted, "OTODOCK_CLOUD", False)
        _forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert "192.168.180.11" in allow, "the broken form: carved without the flag"

    def test_no_extra_target_such_as_a_local_model(self, monkeypatch, hosted):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [])
        extra = ["http://192.168.1.20:11434", "http://localhost:11434"]
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa", extra_targets=extra)
        assert allow == [] and "11434" not in forwards
        monkeypatch.setattr(hosted, "OTODOCK_CLOUD", False)
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa", extra_targets=extra)
        assert "192.168.1.20" in allow and "11434" in forwards

    def test_no_host_self_rewrite(self, monkeypatch, hosted):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_registry.mcp_store, "get_mcp_config_values",
                            lambda n: {"_network_access": "true"})
        monkeypatch.setattr(mcp_egress, "_resolve_to_ips", lambda h: ["192.168.0.50"])
        monkeypatch.setattr(mcp_egress, "_is_local_host_ip", lambda ip: ip == "192.168.0.50")
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://192.168.0.50:9090"}
        mcp_egress._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://192.168.0.50:9090"

    def test_a_docker_tool_is_never_carved_whatever_its_name_resolves_to(self, monkeypatch, hosted):
        """A manifest picks its service name; the sandbox carves none, hosted or
        not: a session reaches the tool through the credential gateway."""
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
        monkeypatch.setattr(mcp_egress, "_platform_targets_cache", {})
        tool = McpManifest.__new__(McpManifest)
        tool.name, tool.network_targets, tool.placement = "cam", [], "any"
        tool.requires_capability, tool.mcp_dir = None, None
        tool.server = ServerConfig(runtime="docker", transport="http", port=8931,
                                   service_name="cam")
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [tool])
        ips = {"cam": ["10.200.0.31", "192.168.1.9"]}
        monkeypatch.setattr(mcp_egress, "_resolve_to_ips", lambda h: list(ips.get(h, [])))
        assert mcp_registry.resolve_sandbox_egress("pa")[1] == []
        monkeypatch.setattr(hosted, "OTODOCK_CLOUD", False)
        assert mcp_registry.resolve_sandbox_egress("pa")[1] == []


@pytest.mark.parametrize("value, kept", [
    ("camoufox", "camoufox"), ("file-tools", "file-tools"), ("", ""),
    ("192.168.1.5", ""), ("nas.lan", ""), ("a_b", ""), ("-x", ""), (5, ""),
    ("2852039166", ""), ("0xa9fea9fe", ""), ("0", ""), ("08", ""), ("0x7f000001", ""),
    ("localhost", ""), ("LOCALHOST", ""), ("1password", "1password"), ("deadbeef", "deadbeef"),
    ("ip6-localhost", ""), ("ip6-loopback", ""), ("ip6-allnodes", ""), ("ip6-tools", "ip6-tools"),
])
@pytest.mark.parametrize("cloud", [False, True])
def test_a_service_name_is_one_dns_label(value, kept, cloud, monkeypatch):
    import config
    from services.mcp.mcp_manifest_parse import _service_name
    monkeypatch.setattr(config, "OTODOCK_CLOUD", cloud)
    assert _service_name(value, "m") == kept


@pytest.mark.parametrize("name, refused", [
    ("2852039166", True), ("0xa9fea9fe", True), ("0", True), ("007", True), ("08", True),
    ("0177.1", True), ("10.1", True), ("169.254.169.254", True), ("::1", True),
    ("localhost", True), ("a.localhost", True), ("localhost.", True),
    ("ip6-localhost", True), ("ip6-loopback", True), ("IP6-LOOPBACK", True), ("ip6-localnet", True),
    ("ip6-mcastprefix", True), ("ip6-allnodes", True), ("ip6-allrouters", True),
    ("camoufox", False), ("1password", False), ("deadbeef", False), ("0x", False),
    ("ip6-tools", False),
])
@pytest.mark.parametrize("cloud", [False, True])
def test_a_tool_name_the_resolver_reads_as_an_address_is_refused(name, refused, cloud, monkeypatch):
    """glibc's getaddrinfo reads ``2852039166`` and ``0xa9fea9fe`` as
    169.254.169.254 without asking DNS: a Docker tool dialled by such a name
    reaches the cloud metadata address. Refused on every install, at the
    manifest's label rule and at the install's name rule."""
    import config
    from core.config import deployment
    from services.community import community_installer as ci
    monkeypatch.setattr(config, "OTODOCK_CLOUD", cloud)
    assert deployment.address_like(name) is refused
    if all(c.isalnum() or c in "-_" for c in name) and name[:1].isalnum():
        assert ci._is_safe_name(name) is (not refused)
        errors = ci._validate_manifest({
            "name": name, "label": "t", "description": "d", "version": "1",
            "category": "community",
            "server": {"runtime": "docker", "transport": "http", "port": 3000}})
        assert any("reads as a network address" in e for e in errors) is refused


# ---------------------------------------------------------------------------
# A tool's local address: one test of what the sandbox opens, read by the
# placement rule too (``carves_targets``, ``reaches_local_address``), and no
# address opened on a compute machine that keeps its own copy
# ---------------------------------------------------------------------------

class TestLocalAddressRule:
    def _tools(self, monkeypatch, *manifests, switch=True, available=True):
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: list(manifests))
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: available)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: switch)

    def test_a_tool_with_a_network_target_and_its_switch_on_reaches_a_local_address(self, monkeypatch):
        ssh = _manifest("ssh-hosts", [NetworkTargetDecl("instance", "host", port_key="port", port_default=22)])
        self._tools(monkeypatch, _manifest("plain", []), ssh)
        assert mcp_egress.carves_targets(ssh) is True
        assert mcp_egress.reaches_local_address("pa") is True

    @pytest.mark.parametrize("case", ["no-target", "satellite-only", "switch-off", "unavailable"])
    def test_every_test_of_the_carve_is_a_test_of_the_rule(self, monkeypatch, case):
        decl = [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)]
        m = _manifest("prom", [] if case == "no-target" else decl,
                      placement="satellite_only" if case == "satellite-only" else "any")
        self._tools(monkeypatch, m, switch=case != "switch-off", available=case != "unavailable")
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets", lambda mm, a, **k: [("192.168.1.10", 9090)])
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
        monkeypatch.setattr(mcp_egress, "_platform_targets_cache", {})
        assert mcp_egress.carves_targets(m) is False
        assert mcp_egress.reaches_local_address("pa") is False
        assert mcp_registry.resolve_sandbox_egress("pa")[1] == []

    def test_the_carve_keeps_exactly_what_the_rule_reads(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
        monkeypatch.setattr(mcp_egress, "_platform_targets_cache", {})
        decl = [NetworkTargetDecl("config", "URL", port_default=80)]
        on, off = _manifest("on", decl), _manifest("off", decl)
        monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [on, off])
        monkeypatch.setattr(mcp_egress, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_egress, "network_access_enabled", lambda mm: mm.name == "on")
        hosts = {"on": "192.168.1.11", "off": "192.168.1.12"}
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets", lambda mm, a, **k: [(hosts[mm.name], 80)])
        assert mcp_registry.resolve_sandbox_egress("pa")[1] == ["192.168.1.11"]
        assert [m.name for m in (on, off) if mcp_egress.carves_targets(m)] == ["on"]

    def test_no_tool_address_is_opened_on_a_machine_that_keeps_its_own_copy(self, monkeypatch, caplog):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
        monkeypatch.setattr(mcp_egress, "_platform_targets_cache", {})
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        self._tools(monkeypatch, m)
        monkeypatch.setattr(mcp_egress, "enumerate_mcp_network_targets", lambda mm, a, **k: [("192.168.1.10", 9090)])
        assert mcp_registry.resolve_sandbox_egress("pa")[1] == ["192.168.1.10"]
        with caplog.at_level("WARNING"):
            forwards, allow = mcp_registry.resolve_sandbox_egress(
                "pa", own_copy=True, extra_targets=["http://192.168.1.20:11434"])
        assert allow == ["192.168.1.20"], "a layer target is not this rule's"
        assert any("pa" in r.getMessage() and "prom" in r.getMessage() for r in caplog.records)

    def test_the_sandbox_config_passes_the_machines_copy_to_the_carve(self, monkeypatch, tmp_path):
        from core.sandbox import sandbox as sb
        seen = {}

        def _egress(agent, **kw):
            seen.update(kw)
            return [], []
        monkeypatch.setattr(mcp_registry, "resolve_sandbox_egress", _egress)
        sb.resolve_sandbox_config("viewer", "", "pa", False, tmp_path / "c", own_copy=True)
        assert seen.get("own_copy") is True
        seen.clear()
        sb.resolve_sandbox_config("viewer", "", "pa", False, tmp_path / "c")
        assert not seen.get("own_copy")
