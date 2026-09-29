"""config_file-delivery instance MCPs (ssh-server) are platform-host-only.

The generated instance file (and the SSH private keys its ``privateKey``
entries point at) lives on the proxy filesystem; the remote rewriter does not
deliver it, so on a satellite the MCP would spawn with a dangling
``--config-file`` path and fail with no visible reason. ``build_session_mcp_config``
must exclude these MCPs from remote sessions with an explicit reason
(surfaced in the prompt's "# Excluded MCPs") and keep including them locally.
"""

import sys
from types import SimpleNamespace

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from tests.mcp.test_mcp_broker_activation import (  # noqa: E402
    _FakeManifest, _stub_assembly,
)
from core import placement


def _ssh_like_manifest():
    inst = SimpleNamespace(
        delivery="config_file",
        fields=[],
        config_file_arg="--config-file",
        config_file_name="hosts.json",
        transform="ssh_hosts",
        max_instances=0,
    )
    return _FakeManifest("ssh-server", instances=inst)


def test_config_file_mcp_excluded_on_remote(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_ssh_like_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )

    _path, _env, excluded, bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m"),
    )

    assert "ssh-server" in excluded
    assert "remote machines" in excluded["ssh-server"]
    assert "ssh-server" not in bundles


def test_config_file_mcp_included_locally(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_ssh_like_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )
    hosts = tmp_path / "hosts.json"
    hosts.write_text('{"hosts": []}')
    monkeypatch.setattr(
        mcp_registry, "_generate_instance_config_file",
        lambda *a, **k: hosts,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=placement.LOCAL_PLACEMENT,
    )

    assert "ssh-server" not in excluded
