"""Tests for the homelab MCP network-target enumeration + the sandbox egress
carve-out it feeds (part of the always-on sandbox-isolation work).
"""

from services.mcp import mcp_registry
from services.mcp.mcp_registry import (
    McpManifest, ServerConfig, NetworkTargetDecl,
    _extract_host_port, enumerate_mcp_network_targets, network_access_enabled,
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
        from services.oauth import credential_resolver
        from storage import credential_store
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
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "enumerate_mcp_network_targets",
                            lambda mm, a, **k: [("192.168.1.10", 9090)])
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert "192.168.1.10" in allow

    def test_homelab_target_skipped_when_toggle_off(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: False)
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert allow == []

    def test_satellite_only_excluded(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("dev", [NetworkTargetDecl("config", "X", port_default=1)], placement="satellite_only")
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert allow == []

    def test_public_target_not_carved(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        m = _manifest("nc", [NetworkTargetDecl("config", "NEXTCLOUD_URL", port_default=443)])
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
        # A public hostname literal that won't resolve to a private IP.
        monkeypatch.setattr(mcp_registry, "enumerate_mcp_network_targets",
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
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [m])
        monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "enumerate_mcp_network_targets",
                            lambda mm, a, **k: [("192.168.0.50", 9090)])
        monkeypatch.setattr(mcp_registry, "_resolve_to_ips", lambda h: ["192.168.0.50"])
        monkeypatch.setattr(mcp_registry, "_is_local_host_ip", lambda ip: ip == "192.168.0.50")
        forwards, allow = mcp_registry.resolve_sandbox_egress("pa")
        assert "9090" in forwards          # loopback -T splice
        assert "192.168.0.50" not in allow  # NOT route-carved (it's local)


class TestHostSelfRewrite:
    def test_host_self_url_rewritten_to_loopback(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "_resolve_to_ips", lambda h: ["192.168.0.50"])
        monkeypatch.setattr(mcp_registry, "_is_local_host_ip", lambda ip: ip == "192.168.0.50")
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://192.168.0.50:9090"}
        mcp_registry._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://127.0.0.1:9090"

    def test_remote_url_not_rewritten(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
        monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
        monkeypatch.setattr(mcp_registry, "_resolve_to_ips", lambda h: ["192.168.30.12"])
        monkeypatch.setattr(mcp_registry, "_is_local_host_ip", lambda ip: False)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://192.168.30.12:9090"}
        mcp_registry._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://192.168.30.12:9090"  # remote → untouched

    def test_no_rewrite_in_t2(self, monkeypatch):
        from core.config import deployment
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)  # T2
        monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda mm: True)
        m = _manifest("prom", [NetworkTargetDecl("config", "PROMETHEUS_URL", port_default=9090)])
        env = {"PROMETHEUS_URL": "http://192.168.0.50:9090"}
        mcp_registry._rewrite_host_self_targets_to_loopback(m, env, "pa")
        assert env["PROMETHEUS_URL"] == "http://192.168.0.50:9090"  # T2 → no rewrite
