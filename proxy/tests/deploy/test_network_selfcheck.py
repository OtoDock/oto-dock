"""The boot tripwire for a control plane re-added to the shared network."""
from __future__ import annotations

import io
import json
import logging

import pytest

import config
from core.config import deployment
from services.infra import network_selfcheck as nsc


def _fake_urlopen(payload):
    def _open(url, timeout=None):
        return io.BytesIO(json.dumps(payload).encode())
    return _open


def _container(service, networks):
    return {"Labels": {"com.docker.compose.service": service},
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}


def test_noop_on_bare_metal(monkeypatch, caplog):
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    # urlopen must never be called on T1
    monkeypatch.setattr(nsc.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called on T1")))
    with caplog.at_level(logging.INFO, logger="claude-proxy.network-selfcheck"):
        nsc.network_selfcheck()
    assert "skipped" in caplog.text
    assert "attached to the shared" not in caplog.text


def test_positive_detection_logs_error(monkeypatch, caplog):
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    payload = [
        _container("otodock-proxy", ["otodock", "otodock-data"]),      # legit
        _container("otodock-postgres", ["otodock-data", "otodock"]),   # LEAK
    ]
    monkeypatch.setattr(nsc.urllib.request, "urlopen", _fake_urlopen(payload))
    with caplog.at_level(logging.ERROR, logger="claude-proxy.network-selfcheck"):
        nsc.network_selfcheck()
    assert "otodock-postgres" in caplog.text
    assert "attached to the shared" in caplog.text
    # the proxy legitimately on `otodock` must NOT be flagged
    assert "otodock-proxy'" not in caplog.text


def test_clean_when_control_planes_isolated(monkeypatch, caplog):
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    payload = [
        _container("otodock-proxy", ["otodock", "socketproxy", "otodock-data"]),
        _container("otodock-postgres", ["otodock-data"]),
        _container("docker-socket-proxy", ["socketproxy"]),
        _container("some-community-mcp", ["otodock"]),
    ]
    monkeypatch.setattr(nsc.urllib.request, "urlopen", _fake_urlopen(payload))
    with caplog.at_level(logging.INFO, logger="claude-proxy.network-selfcheck"):
        nsc.network_selfcheck()
    assert "control planes are off" in caplog.text
    assert "attached to the shared" not in caplog.text


def test_inconclusive_api_error_warns_not_raises(monkeypatch, caplog):
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)

    def _boom(url, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(nsc.urllib.request, "urlopen", _boom)
    with caplog.at_level(logging.WARNING, logger="claude-proxy.network-selfcheck"):
        nsc.network_selfcheck()  # must not raise
    assert "could not query" in caplog.text


def _member(service, project, networks, name=None):
    return {"Names": [f"/{name or f'{project}-{service}-1'}"],
            "Labels": {"com.docker.compose.service": service,
                       "com.docker.compose.project": project},
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}


def _platform(project="otodock"):
    return [
        _member("otodock-proxy", project, ["otodock", f"{project}_socketproxy",
                                           f"{project}_otodock-data", f"{project}_otodock-internal"]),
        _member("otodock-postgres", project, [f"{project}_otodock-data"]),
        _member("otodock-db-init", project, [f"{project}_otodock-data"]),
        _member("docker-socket-proxy", project, [f"{project}_socketproxy"]),
        _member("file-tools", project, [f"{project}_otodock-internal"]),
    ]


def _run(monkeypatch, caplog, payload):
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    monkeypatch.setattr(nsc.urllib.request, "urlopen", _fake_urlopen(payload))
    with caplog.at_level(logging.INFO, logger="claude-proxy.network-selfcheck"):
        nsc.network_selfcheck()
    return caplog.text


def test_the_platform_members_of_the_internal_planes_are_clean(monkeypatch, caplog):
    text = _run(monkeypatch, caplog, _platform())
    assert "control planes are off" in text
    assert "internal" not in text.lower() or "only the platform" in text


@pytest.mark.parametrize("plane", ["otodock_socketproxy", "otodock_otodock-data"])
def test_a_community_container_on_an_internal_plane_is_flagged(monkeypatch, caplog, plane):
    evil = _member("camoufox", "otodock-abcd1234-mcp-camoufox", ["otodock", plane],
                   name="otodock-abcd1234-mcp-camoufox")
    text = _run(monkeypatch, caplog, [*_platform(), evil])
    assert "otodock-abcd1234-mcp-camoufox" in text
    assert plane in text
    assert "ERROR" in [r.levelname for r in caplog.records if "otodock-abcd1234" in r.message][0]


def test_a_platform_service_on_the_wrong_plane_is_flagged(monkeypatch, caplog):
    # file-tools belongs on otodock-internal only; on the data plane it
    # reaches Postgres :5432.
    stray = _member("file-tools", "otodock", ["otodock_otodock-internal", "otodock_otodock-data"],
                    name="otodock-file-tools-1")
    platform = [c for c in _platform() if c["Labels"]["com.docker.compose.service"] != "file-tools"]
    text = _run(monkeypatch, caplog, [*platform, stray])
    assert "otodock-file-tools-1" in text and "otodock_otodock-data" in text


def test_a_second_install_members_are_its_own(monkeypatch, caplog):
    text = _run(monkeypatch, caplog, [*_platform(), *_platform("otodock2")])
    assert "control planes are off" in text
    assert "ERROR" not in [r.levelname for r in caplog.records]
