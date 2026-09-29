"""The MCP runtime at its readers (core-seams phase 9): the catalog's
converge signal, the skill package invariant, the satellite sync's skip set
and the deployment's proxy-local host test, each asking the runtime's
facts instead of comparing the word.

Run: venv/bin/python -m pytest tests/mcp/test_mcp_runtime.py -q
"""

from __future__ import annotations

from types import SimpleNamespace

from services.community import community_catalog
from services.mcp import mcp_manifest_types as mt


def test_the_converge_signal_applies_per_runtime():
    applies = community_catalog.manifest_hash_signal_applies
    assert applies({"runtime": "none"})                       # a skill package reinstalls from its own
    assert applies({"runtime": "python", "source": "pypi:x"})
    assert applies({"runtime": "node", "source": "npm:x"})
    assert applies({"runtime": "docker", "source": "ghcr:x"})
    assert not applies({"runtime": "python", "source": "git+https://x"})  # no converge path
    assert not applies({"runtime": "hosted"})                  # not a runtime
    assert not applies({"runtime": ""}) and not applies({})


def test_a_skill_package_must_have_no_process(tmp_path):
    from services.community import skills_installer
    # The invariant is spelled through the runtime's ``process`` fact: a
    # package declaring a runtime with a process is refused, ``none`` passes
    # the runtime half of the check.
    base = {"name": "sk", "label": "Sk", "description": "d", "version": "1.0.0", "category": "skill",
            "skills": []}
    refused = skills_installer._validate_skill_package(
        {**base, "server": {"runtime": "python", "transport": "none"}}, tmp_path)
    assert any("server.runtime" in e for e in refused)
    unknown = skills_installer._validate_skill_package(
        {**base, "server": {"runtime": "hosted", "transport": "none"}}, tmp_path)
    assert any("server.runtime" in e for e in unknown)
    ok = skills_installer._validate_skill_package(
        {**base, "server": {"runtime": "none", "transport": "none"}}, tmp_path)
    assert not any("server.runtime" in e for e in ok)


def test_the_sync_skip_set_is_what_does_not_install_on_a_host():
    # Docker lives on the platform host, a context-only MCP nowhere: neither
    # ships to a satellite; python and node do.
    for word, ships in (("python", True), ("node", True), ("docker", False), ("none", False)):
        assert mt.installs_on_host(SimpleNamespace(runtime=word)) is ships, word


def test_the_deployment_host_test_asks_the_container_fact(monkeypatch):
    from core.config import deployment
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(deployment, "docker_mcp_host", lambda _m: "svc-x")
    docker = SimpleNamespace(server=SimpleNamespace(runtime="docker"))
    stdio = SimpleNamespace(server=SimpleNamespace(runtime="python"))
    assert deployment.is_proxy_local_mcp_host("svc-x", docker)
    assert not deployment.is_proxy_local_mcp_host("svc-x", stdio)
    assert not deployment.is_proxy_local_mcp_host("svc-x", SimpleNamespace(server=None))
