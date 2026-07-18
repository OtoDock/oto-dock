"""Tests for the T2 Docker-MCP compose rewrite (services/mcp/compose_rewrite.py).

The pure ``transform_compose_dict`` is exercised directly; ``ensure_pull_compose``
is exercised with a tmp compose file + a monkeypatched deployment mode so the
T1-no-op / T2-rewrite / idempotency / no-image-error paths are all covered
without a real Docker daemon.

Run standalone (one file at a time — concurrent pytest deadlocks on schema-init):
    proxy/venv/bin/python -m pytest tests/mcp/test_compose_rewrite.py -x
"""

from types import SimpleNamespace

import pytest
import yaml

import config
from core.config import deployment
from services.mcp import compose_rewrite


# --- a camoufox-shaped build-from-context compose (the real shape we rewrite) ---
CAMOUFOX_COMPOSE = {
    "services": {
        "camoufox": {
            "build": {"context": ".", "additional_contexts": {"shared": "../../_shared"}},
            "container_name": "camoufox-mcp",
            "restart": "always",
            "shm_size": "2gb",
            "environment": ["OTO_MCP_SUPPRESS_SERVER_REQUESTS=1"],
            "ports": ["127.0.0.1:8931:8931"],
            "volumes": ["./screenshots:/screenshots"],
            "healthcheck": {"test": ["CMD", "python3", "/app/healthprobe.py"]},
        }
    }
}


def _transform(data, **over):
    kwargs = dict(
        image="ghcr.io/otodock/camoufox:0.0.55",
        service_name="camoufox",
        container_name="otodock-mcp-camoufox",
        network_name="otodock",
        mcp_name="camoufox",
    )
    kwargs.update(over)
    return compose_rewrite.transform_compose_dict(data, **kwargs)


# --------------------------------------------------------------------------- #
# transform_compose_dict — pure
# --------------------------------------------------------------------------- #

def test_build_dropped_and_image_set():
    out = _transform(CAMOUFOX_COMPOSE)
    svc = out["services"]["camoufox"]
    assert "build" not in svc
    assert svc["image"] == "ghcr.io/otodock/camoufox:0.0.55"


def test_container_name_and_ports_stripped():
    svc = _transform(CAMOUFOX_COMPOSE)["services"]["camoufox"]
    assert svc["container_name"] == "otodock-mcp-camoufox"
    assert "ports" not in svc  # reached over the shared net, not a host port


def test_network_alias_is_service_name():
    out = _transform(CAMOUFOX_COMPOSE)
    svc = out["services"]["camoufox"]
    assert svc["networks"] == {"otodock": {"aliases": ["camoufox"]}}
    # external network declared at the top level, mapped to the real name
    assert out["networks"] == {"otodock": {"external": True, "name": "otodock"}}


def test_service_name_alias_distinct_from_key():
    """The DNS alias tracks server.service_name, not the compose service key."""
    data = {"services": {"app": {"build": ".", "ports": ["8931:8931"]}}}
    out = _transform(data, service_name="camoufox")
    assert out["services"]["app"]["networks"] == {"otodock": {"aliases": ["camoufox"]}}


def test_relative_bind_becomes_named_volume():
    out = _transform(CAMOUFOX_COMPOSE)
    svc = out["services"]["camoufox"]
    assert svc["volumes"] == [f"otodock-{config.INSTALL_ID}-mcp-camoufox-screenshots:/screenshots"]
    # and the named volume is declared at the top level (default driver)
    assert f"otodock-{config.INSTALL_ID}-mcp-camoufox-screenshots" in out["volumes"]
    assert out["volumes"][f"otodock-{config.INSTALL_ID}-mcp-camoufox-screenshots"] is None


def test_absolute_bind_with_mode_becomes_named_volume():
    data = {"services": {"m": {"build": ".", "volumes": ["/var/data:/data:rw"]}}}
    svc = _transform(data, service_name="m", mcp_name="m")["services"]["m"]
    assert svc["volumes"] == [f"otodock-{config.INSTALL_ID}-mcp-m-data:/data:rw"]


def test_existing_named_volume_is_preserved():
    data = {"services": {"m": {"build": ".", "volumes": ["mydata:/data"]}}}
    out = _transform(data, service_name="m", mcp_name="m")
    assert out["services"]["m"]["volumes"] == ["mydata:/data"]
    # a pre-existing named volume isn't auto-declared by us
    assert "mydata" not in (out.get("volumes") or {})


def test_long_form_bind_mount_converted():
    data = {"services": {"m": {"build": ".", "volumes": [
        {"type": "bind", "source": "./x", "target": "/app/x"},
    ]}}}
    svc = _transform(data, service_name="m", mcp_name="m")["services"]["m"]
    assert svc["volumes"] == [f"otodock-{config.INSTALL_ID}-mcp-m-app-x:/app/x"]


def test_environment_and_healthcheck_preserved():
    svc = _transform(CAMOUFOX_COMPOSE)["services"]["camoufox"]
    assert svc["environment"] == ["OTO_MCP_SUPPRESS_SERVER_REQUESTS=1"]
    assert svc["restart"] == "always"
    assert svc["shm_size"] == "2gb"
    assert "healthcheck" in svc


def test_input_not_mutated():
    before = yaml.safe_dump(CAMOUFOX_COMPOSE, sort_keys=True)
    _transform(CAMOUFOX_COMPOSE)
    after = yaml.safe_dump(CAMOUFOX_COMPOSE, sort_keys=True)
    assert before == after  # deep-copied, original untouched


def test_single_service_picked_without_name_match():
    data = {"services": {"whatever": {"build": "."}}}
    out = _transform(data, service_name="camoufox")  # name != key, single svc
    assert out["services"]["whatever"]["image"]


def test_raises_when_no_services():
    with pytest.raises(ValueError, match="no `services`"):
        _transform({"version": "3"})


def test_raises_on_multi_service_extra_build():
    data = {"services": {
        "camoufox": {"build": "."},
        "sidecar": {"build": "./sidecar"},
    }}
    with pytest.raises(ValueError, match="additional build-from-context"):
        _transform(data)


def test_sibling_image_service_joins_network_without_alias():
    data = {"services": {
        "camoufox": {"build": "."},
        "redis": {"image": "redis:7"},
    }}
    out = _transform(data)
    assert out["services"]["camoufox"]["networks"] == {"otodock": {"aliases": ["camoufox"]}}
    assert out["services"]["redis"]["networks"] == {"otodock": {}}


# --------------------------------------------------------------------------- #
# ensure_pull_compose — file I/O + deployment-mode gating
# --------------------------------------------------------------------------- #

def _manifest(tmp_path, *, image="ghcr.io/otodock/camoufox:0.0.55", compose=CAMOUFOX_COMPOSE):
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    return SimpleNamespace(
        name="camoufox",
        mcp_dir=tmp_path,
        server=SimpleNamespace(
            runtime="docker",
            docker_compose="docker-compose.yml",
            image=image,
            service_name="camoufox",
        ),
    )


def test_ensure_noop_on_t1(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
    m = _manifest(tmp_path)
    original = (tmp_path / "docker-compose.yml").read_text()
    assert compose_rewrite.ensure_pull_compose(m) is False
    assert (tmp_path / "docker-compose.yml").read_text() == original  # untouched


def test_ensure_t2_rewrites_then_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock-testnet")
    m = _manifest(tmp_path)

    assert compose_rewrite.ensure_pull_compose(m) is True
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    svc = written["services"]["camoufox"]
    assert svc["image"] == "ghcr.io/otodock/camoufox:0.0.55"
    assert "build" not in svc
    assert written["networks"] == {"otodock": {"external": True, "name": "otodock-testnet"}}

    # second call: already pull-form → no-op
    assert compose_rewrite.ensure_pull_compose(m) is False


def test_ensure_t2_raises_without_image(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    m = _manifest(tmp_path, image="")
    with pytest.raises(ValueError, match="no pre-built image"):
        compose_rewrite.ensure_pull_compose(m)


def test_ensure_skips_non_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    m = _manifest(tmp_path)
    m.server.runtime = "node"
    assert compose_rewrite.ensure_pull_compose(m) is False


def test_ensure_header_written(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    m = _manifest(tmp_path)
    compose_rewrite.ensure_pull_compose(m)
    assert (tmp_path / "docker-compose.yml").read_text().startswith("# AUTO-GENERATED")


# --------------------------------------------------------------------------- #
# default resource bounds (mem_limit / memswap_limit / logging)
# --------------------------------------------------------------------------- #

def test_default_bounds_injected_when_absent(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g", raising=False)
    out = _transform(CAMOUFOX_COMPOSE)
    svc = out["services"]["camoufox"]
    assert svc["mem_limit"] == "2g"
    assert svc["memswap_limit"] == "2g"
    assert svc["logging"] == {
        "driver": "json-file", "options": {"max-size": "10m", "max-file": "5"},
    }


def test_declared_limits_win(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g", raising=False)
    data = {"services": {"camoufox": {
        "build": ".",
        "mem_limit": "3g",
        "memswap_limit": "3g",
        "logging": {"driver": "json-file", "options": {"max-size": "50m"}},
    }}}
    svc = _transform(data)["services"]["camoufox"]
    assert svc["mem_limit"] == "3g"
    assert svc["memswap_limit"] == "3g"
    assert svc["logging"]["options"] == {"max-size": "50m"}


def test_deploy_memory_limit_suppresses_mem_injection(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g", raising=False)
    data = {"services": {"camoufox": {
        "build": ".",
        "deploy": {"resources": {"limits": {"memory": "4g"}}},
    }}}
    svc = _transform(data)["services"]["camoufox"]
    assert "mem_limit" not in svc
    assert "memswap_limit" not in svc
    assert "logging" in svc  # log rotation still injected


def test_mem_injection_disabled_by_knob(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "0", raising=False)
    svc = _transform(CAMOUFOX_COMPOSE)["services"]["camoufox"]
    assert "mem_limit" not in svc
    assert "memswap_limit" not in svc
    assert "logging" in svc


def test_sibling_service_also_gets_default_bounds(monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g", raising=False)
    data = {"services": {
        "camoufox": {"build": "."},
        "redis": {"image": "redis:7"},
    }}
    out = _transform(data)
    assert out["services"]["redis"]["mem_limit"] == "2g"
    assert out["services"]["redis"]["logging"]["driver"] == "json-file"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


def test_ensure_restamps_stale_install_id(tmp_path, monkeypatch):
    """A pull-form compose stamped by a PREVIOUS install identity (rotated
    config.env → new INSTALL_ID) gets its container_name re-stamped instead
    of colliding with the old generation's container forever."""
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    pull_form = {
        "services": {
            "camoufox": {
                "image": "ghcr.io/otodock/camoufox:0.0.55",
                "container_name": "otodock-deadbeef-mcp-camoufox",
                "networks": {"otodock": {"aliases": ["camoufox"]}},
            }
        },
        "networks": {"otodock": {"external": True, "name": "otodock"}},
    }
    m = _manifest(tmp_path, compose=pull_form)

    assert compose_rewrite.ensure_pull_compose(m) is True
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert written["services"]["camoufox"]["container_name"] == (
        f"otodock-{config.INSTALL_ID}-mcp-camoufox"
    )
    # second call: correctly stamped → idempotent no-op
    assert compose_rewrite.ensure_pull_compose(m) is False


def test_ensure_leaves_non_otodock_container_name(tmp_path, monkeypatch):
    """A pull-form compose whose container_name is not otodock-shaped is not
    ours to re-stamp — leave it untouched."""
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    pull_form = {
        "services": {
            "camoufox": {
                "image": "ghcr.io/otodock/camoufox:0.0.55",
                "container_name": "camoufox-mcp",
            }
        },
    }
    m = _manifest(tmp_path, compose=pull_form)
    original = (tmp_path / "docker-compose.yml").read_text()
    assert compose_rewrite.ensure_pull_compose(m) is False
    assert (tmp_path / "docker-compose.yml").read_text() == original
