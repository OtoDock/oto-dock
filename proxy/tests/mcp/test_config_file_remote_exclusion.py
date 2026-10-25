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

from core import placement
from tests.mcp.test_mcp_broker_activation import (
    _FakeManifest,
    _stub_assembly,
)


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


def test_the_config_file_is_written_whole(monkeypatch, tmp_path):
    """The file is written beside itself and renamed into place: a reader
    (the Direct manager's read-back, right after the session's own builder
    wrote the same path) meets the old file or the new one, never a
    partial one, and no temp file stays behind."""
    import json

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

    path, *_ = mcp_registry.build_session_mcp_config(
        "agent", None, placement=placement.LOCAL_PLACEMENT, session_id="s-whole",
    )

    assert path is not None and path.is_file()
    assert "mcpServers" in json.loads(path.read_text())
    assert not list(path.parent.glob("*.tmp"))


def test_two_writers_of_one_path_never_tear_it_or_fail(tmp_path):
    """Each writer has a temp file of its own beside the target: two threads
    writing one path at once never raise, and the file is always one whole
    text or the other."""
    import threading

    from services.mcp import mcp_registry
    path = tmp_path / "cfg.json"
    texts = ["A" * 20000, "B" * 20000]
    errors: list[BaseException] = []

    def writer(text: str) -> None:
        try:
            for _ in range(200):
                mcp_registry._write_whole(path, text)
                assert path.read_text() in texts
        except BaseException as exc:  # noqa: BLE001  # the thread's failure is the test's finding
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in texts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert path.read_text() in texts
    assert not list(tmp_path.glob("*.tmp"))
