"""The Docker deployment-topology (T1 bare-metal / T2 Docker-Compose)
URL-host seam.

Every test pins the deployment via ``config.RUNNING_IN_DOCKER`` (+ ``OTODOCK_CLOUD``)
with monkeypatch so it auto-restores. The recurring assertion is that **T1 is
byte-for-byte unchanged** (localhost / host.docker.internal, no DOCKER_HOST) — the
live native install must be unaffected — while **T2** swaps to service-DNS and the
socket-proxy daemon. The security-critical cases are the "proxy-local sidecar"
checks: in T2 a Docker MCP's bearer must still be recognised as proxy-terminable
(lifted into the broker, tunnel-routed) even though its host is a service name.
"""

import pytest

import config
from core.config import deployment


# --------------------------------------------------------------------------- #
# Fake manifests (the deployment helpers duck-type on .name / .server.*).
# --------------------------------------------------------------------------- #
class _Srv:
    def __init__(self, runtime="docker", service_name="", port=8932):
        self.runtime = runtime
        self.service_name = service_name
        self.port = port


class _Manifest:
    def __init__(self, name="file-tools", **srv):
        self.name = name
        self.server = _Srv(**srv)


@pytest.fixture
def t1(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)


@pytest.fixture
def t2(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)


# --------------------------------------------------------------------------- #
# Mode resolution
# --------------------------------------------------------------------------- #
def test_mode_t1(t1):
    assert deployment.current_mode() == deployment.MANAGED_LOCAL
    assert deployment.in_docker_compose() is False


def test_mode_t2(t2):
    assert deployment.current_mode() == deployment.MANAGED_SOCKPROX
    assert deployment.in_docker_compose() is True


@pytest.mark.parametrize("in_docker, mode", [
    (True, deployment.MANAGED_SOCKPROX), (False, deployment.MANAGED_LOCAL)])
def test_hosted_flag_is_no_topology(monkeypatch, in_docker, mode):
    # A hosted install keeps its Docker tools inside it: the flag carries the
    # hosted rules, the topology follows RUNNING_IN_DOCKER alone.
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", in_docker)
    assert deployment.current_mode() == mode
    assert deployment.in_docker_compose() is in_docker
    assert not hasattr(deployment, "EXTERNAL_POOL")


# --------------------------------------------------------------------------- #
# Host resolution
# --------------------------------------------------------------------------- #
def test_docker_mcp_host_t1(t1):
    assert deployment.docker_mcp_host(_Manifest()) == "localhost"


def test_docker_mcp_host_t2_defaults_to_name(t2):
    assert deployment.docker_mcp_host(_Manifest(name="camoufox")) == "camoufox"


def test_docker_mcp_host_t2_service_name_override(t2):
    m = _Manifest(name="file-tools", service_name="custom-dns")
    assert deployment.docker_mcp_host(m) == "custom-dns"


def test_proxy_callback_host_t1(t1):
    assert deployment.proxy_callback_host() == "host.docker.internal"


def test_proxy_callback_host_t2(t2):
    assert deployment.proxy_callback_host() == config.PROXY_SERVICE_NAME


def test_docker_subprocess_env_t1_empty(t1):
    assert deployment.docker_subprocess_env() == {}


def test_docker_subprocess_env_t2_sets_docker_host(t2):
    env = deployment.docker_subprocess_env()
    assert env == {
        "DOCKER_HOST": f"tcp://{config.DOCKER_SOCKET_PROXY_HOST}:{config.DOCKER_SOCKET_PROXY_PORT}"
    }


# --------------------------------------------------------------------------- #
# is_proxy_local_mcp_host — the security-critical classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", ""])
def test_loopback_always_proxy_local(t1, host):
    # Loopback is proxy-local in every topology, manifest or not.
    assert deployment.is_proxy_local_mcp_host(host, None) is True
    assert deployment.is_proxy_local_mcp_host(host, _Manifest()) is True


def test_service_dns_not_proxy_local_in_t1(t1):
    # On bare-metal a bare service name is just some host — NOT proxy-local.
    assert deployment.is_proxy_local_mcp_host("file-tools", _Manifest()) is False


def test_service_dns_proxy_local_in_t2(t2):
    # In T2 the Docker MCP's own service name IS proxy-local (so its bearer is
    # lifted into the broker + the URL is tunnel-routed).
    assert deployment.is_proxy_local_mcp_host("file-tools", _Manifest()) is True


def test_other_service_name_not_proxy_local_in_t2(t2):
    # A host that is NOT this manifest's service name is not proxy-local for it.
    assert deployment.is_proxy_local_mcp_host("some-other", _Manifest(name="file-tools")) is False


def test_vendor_host_never_proxy_local(t2):
    assert deployment.is_proxy_local_mcp_host("mcp.linear.app", _Manifest()) is False


def test_non_docker_manifest_service_name_not_proxy_local_in_t2(t2):
    # A python/node MCP is never a proxy-local Docker sidecar, even in T2.
    m = _Manifest(name="linear", runtime="python")
    assert deployment.is_proxy_local_mcp_host("linear", m) is False


def test_none_manifest_only_loopback_in_t2(t2):
    # Without a manifest only the loopback test applies (safe default).
    assert deployment.is_proxy_local_mcp_host("file-tools", None) is False
    assert deployment.is_proxy_local_mcp_host("localhost", None) is True


# --------------------------------------------------------------------------- #
# Integration — real manifests through resolve_server_config + the rewriters
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def registry():
    from services.mcp import mcp_registry
    mcp_registry.scan_manifests()
    return mcp_registry


def _installed_container(registry, nm):
    """The manifest when ``nm`` is installed as a container, else None.

    community/* manifests are gitignored, installed from the separate
    community-mcps catalog, which may move an entry off a container (a hosted
    server is not proxy-local); the in-repo file-tools manifest must ALWAYS
    resolve."""
    from services.mcp import mcp_manifest_types as _mt
    m = registry.get_manifest(nm)
    if m is None:
        assert nm != "file-tools", "in-repo file-tools manifest must exist"
        return None
    return m if _mt.is_container(m.server) else None


def test_resolve_server_config_localhost_in_t1(t1, registry):
    for nm, suffix in (("file-tools", "/mcp/"), ("m365-mcp", "/mcp")):
        m = _installed_container(registry, nm)
        if m is None:
            continue
        e = registry.resolve_server_config(m, "a", mcp_config_format="json")
        assert e["url"] == f"http://localhost:{m.server.port}{suffix}", nm


def test_resolve_server_config_service_dns_in_t2(t2, registry):
    cases = {
        "file-tools": "http://file-tools:8932/mcp/",
        "camoufox": "http://camoufox:8931/mcp/",
        "m365-mcp": "http://m365-mcp:3000/mcp",
    }
    for nm, expected in cases.items():
        m = _installed_container(registry, nm)
        if m is None:
            continue
        e = registry.resolve_server_config(m, "a", mcp_config_format="json")
        assert e["url"] == expected, nm


def test_proxy_url_for_docker_token_t1_t2(t1, registry, monkeypatch):
    m = registry.get_manifest("file-tools")
    t1_val = registry._resolve_template("${platform.proxy_url_for_docker}", m, "a")
    assert t1_val == f"http://host.docker.internal:{config.PORT}"
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    t2_val = registry._resolve_template("${platform.proxy_url_for_docker}", m, "a")
    assert t2_val == f"http://{config.PROXY_SERVICE_NAME}:{config.PORT}"


def test_docker_mcp_host_token(t2, registry):
    m = registry.get_manifest("file-tools")
    assert registry._resolve_template("${docker_mcp_host}", m, "a") == "file-tools"


# --------------------------------------------------------------------------- #
# Satellite tunnel upstream resolution
# --------------------------------------------------------------------------- #
def test_resolve_upstream_url_t1(t1, registry):
    from core.remote import satellite_http_tunnel as tun
    url = tun._resolve_upstream_url("/mcp/file-tools/mcp/?session_id=abc")
    assert url.startswith("http://localhost:8932/")
    # the proxy's own hooks always stay on loopback (both topologies): the
    # internal listener, or the main port when there is none
    assert tun._resolve_upstream_url("/v1/hooks/resolve-path").startswith(
        f"http://127.0.0.1:{config.INTERNAL_LISTENER_PORT or config.PORT}"
    )


def test_resolve_upstream_url_t2(t2, registry):
    from core.remote import satellite_http_tunnel as tun
    url = tun._resolve_upstream_url("/mcp/file-tools/mcp/?session_id=abc")
    assert url.startswith("http://file-tools:8932/")
    # hooks endpoint still loopback in T2 (proxy calling itself)
    assert tun._resolve_upstream_url("/v1/hooks/resolve-path").startswith(
        f"http://127.0.0.1:{config.INTERNAL_LISTENER_PORT or config.PORT}"
    )


# --------------------------------------------------------------------------- #
# Remote rewriters — docker MCP tunnel-routed (T1 & T2), vendor host untouched
# --------------------------------------------------------------------------- #
def test_remote_json_rewriter_t1(t1, registry):
    from core.remote import remote_execution as rx
    cfg = {"mcpServers": {
        "file-tools": {"type": "http", "url": "http://localhost:8932/mcp/"},
        "linear": {"type": "http", "url": "https://mcp.linear.app/mcp"},
    }}
    out = rx._rewrite_mcp_json_for_remote(cfg, 9001, session_id="sid")["mcpServers"]
    assert out["file-tools"]["url"].startswith("http://127.0.0.1:9001/mcp/file-tools/")
    assert "session_id=sid" in out["file-tools"]["url"]
    assert out["linear"]["url"] == "https://mcp.linear.app/mcp"  # untouched


def test_remote_json_rewriter_t2(t2, registry):
    from core.remote import remote_execution as rx
    cfg = {"mcpServers": {
        "file-tools": {"type": "http", "url": "http://file-tools:8932/mcp/"},
        "linear": {"type": "http", "url": "https://mcp.linear.app/mcp"},
    }}
    out = rx._rewrite_mcp_json_for_remote(cfg, 9001, session_id="sid")["mcpServers"]
    # service-DNS docker MCP STILL tunnel-routed in T2 (the bug this closes)
    assert out["file-tools"]["url"].startswith("http://127.0.0.1:9001/mcp/file-tools/")
    assert out["linear"]["url"] == "https://mcp.linear.app/mcp"  # untouched


def test_remote_toml_rewriter_t1(t1, registry):
    from core.remote import remote_execution as rx
    toml = (
        '[mcp_servers.file-tools]\n'
        'url = "http://localhost:8932/mcp/"\n'
        '\n'
        '[mcp_servers.linear]\n'
        'url = "https://mcp.linear.app/mcp"\n'
    )
    out = rx._rewrite_mcp_toml_for_remote(toml, 9001, session_id="sid")
    assert 'url = "http://127.0.0.1:9001/mcp/file-tools/' in out
    assert 'url = "https://mcp.linear.app/mcp"' in out  # untouched


def test_remote_toml_rewriter_t2(t2, registry):
    from core.remote import remote_execution as rx
    toml = (
        '[mcp_servers.file-tools]\n'
        'url = "http://file-tools:8932/mcp/"\n'
        '\n'
        '[mcp_servers.linear]\n'
        'url = "https://mcp.linear.app/mcp"\n'
    )
    out = rx._rewrite_mcp_toml_for_remote(toml, 9001, session_id="sid")
    assert 'url = "http://127.0.0.1:9001/mcp/file-tools/' in out
    assert 'url = "https://mcp.linear.app/mcp"' in out  # untouched


# --------------------------------------------------------------------------- #
# docker_manager subprocess env + netns forwards
# --------------------------------------------------------------------------- #
def test_compose_env_t1_no_docker_host(t1):
    from services.mcp import docker_manager
    assert "DOCKER_HOST" not in docker_manager._compose_env() or \
        docker_manager._compose_env().get("DOCKER_HOST") == __import__("os").environ.get("DOCKER_HOST")


def test_compose_env_t2_sets_docker_host(t2):
    from services.mcp import docker_manager
    assert docker_manager._compose_env()["DOCKER_HOST"] == (
        f"tcp://{config.DOCKER_SOCKET_PROXY_HOST}:{config.DOCKER_SOCKET_PROXY_PORT}"
    )


def test_egress_carves_no_docker_tool_on_either_topology(monkeypatch, registry):
    """A session reaches a Docker tool through the credential gateway's route on
    the proxy port: neither its loopback port (bare metal) nor its container's
    address (the Docker install) is carved."""
    from services.mcp import mcp_egress

    ft = registry.get_manifest("file-tools")
    monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [ft])
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    assert registry.resolve_sandbox_egress("a") == ([str(config.PORT)], [])
    from core.config import deployment
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(deployment, "docker_mcp_host", lambda m: "10.200.0.9")
    assert registry.resolve_sandbox_egress("a") == ([str(config.PORT)], [])


def test_egress_phone_mcp_needs_no_daemon_carve(monkeypatch, registry):
    from services.mcp import mcp_egress

    """phone-mcp calls the proxy's /v1/phone/calls relay over the standard
    PROXY_URL loopback forward — the egress resolver must NOT open a sandbox
    path to the phone daemon (the proxy dials it from outside the sandbox,
    and the daemon URL / telephony secret no longer appear in the MCP env)."""
    pm = registry.get_manifest("phone-mcp")
    if pm is None:
        pytest.skip("phone-mcp not present in this checkout")
    assert not (pm.agent_env or {})  # no daemon URL / telephony secret
    monkeypatch.setattr(mcp_egress, "get_agent_mcps", lambda *a, **k: [pm])

    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://192.168.110.10:9093")
    forwards, allow = registry.resolve_sandbox_egress("a")
    assert "9093" not in forwards
    assert "192.168.110.10" not in allow
    assert str(config.PORT) in forwards  # the proxy relay rides this

