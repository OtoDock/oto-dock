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
    ours to re-stamp; the name stays while the service still joins the
    shared network like a rewritten one."""
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    pull_form = {
        "services": {
            "camoufox": {
                "image": "ghcr.io/otodock/camoufox:0.0.55",
                "container_name": "camoufox-mcp",
            }
        },
    }
    m = _manifest(tmp_path, compose=pull_form)
    assert compose_rewrite.ensure_pull_compose(m) is True
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    svc = written["services"]["camoufox"]
    assert svc["container_name"] == "camoufox-mcp"
    assert svc["networks"] == {"otodock": {"aliases": ["camoufox"]}}
    assert compose_rewrite.ensure_pull_compose(m) is False


# --------------------------------------------------------------------------- #
# agents-tree mount → shared external platform volume
# --------------------------------------------------------------------------- #

# The video-tools shape: the catalog contract for "works on agent files" is a
# bind sourced from `${HOST_AGENTS_DIR}` (see mcps/community/video-tools).
VIDEO_TOOLS_COMPOSE = {
    "services": {
        "video-tools": {
            "build": ".",
            "container_name": "video-tools-mcp",
            "ports": ["127.0.0.1:8933:8933"],
            "volumes": ["${HOST_AGENTS_DIR:-../../../agents}:/agents:rw"],
            "env_file": ".env",
        }
    }
}


def test_agents_bind_maps_to_shared_external_volume():
    out = _transform(
        VIDEO_TOOLS_COMPOSE, service_name="video-tools", mcp_name="video-tools",
    )
    svc = out["services"]["video-tools"]
    assert svc["volumes"] == [f"{config.OTODOCK_AGENTS_VOLUME}:/agents:rw"]
    # attached external — the platform compose owns the volume's lifecycle
    assert out["volumes"][config.OTODOCK_AGENTS_VOLUME] == {
        "external": True, "name": config.OTODOCK_AGENTS_VOLUME,
    }


def test_agents_bind_without_default_or_mode():
    data = {"services": {"m": {"build": ".", "volumes": ["${HOST_AGENTS_DIR}:/agents"]}}}
    svc = _transform(data, service_name="m", mcp_name="m")["services"]["m"]
    assert svc["volumes"] == [f"{config.OTODOCK_AGENTS_VOLUME}:/agents"]


def test_agents_bind_long_form_read_only():
    data = {"services": {"m": {"build": ".", "volumes": [
        {
            "type": "bind",
            "source": "${HOST_AGENTS_DIR:-../../agents}",
            "target": "/agents",
            "read_only": True,
        },
    ]}}}
    svc = _transform(data, service_name="m", mcp_name="m")["services"]["m"]
    assert svc["volumes"] == [f"{config.OTODOCK_AGENTS_VOLUME}:/agents:ro"]


@pytest.mark.parametrize("vol", [
    "${OTHER_DIR:-./x}:/x",
    "${UNSET:-/}:/host",
    "$HOME:/host",
    # a subpath of the agents tree cannot be mapped onto the named volume
    "${HOST_AGENTS_DIR}/sub:/agents",
])
def test_other_interpolated_source_is_refused(vol):
    """A source compose would interpolate resolves as a host bind on the
    daemon host; only the whole agents-tree form is mapped."""
    data = {"services": {"m": {"build": ".", "volumes": [vol]}}}
    with pytest.raises(ValueError, match="interpolat"):
        _transform(data, service_name="m", mcp_name="m")


def test_ensure_heals_agents_bind_in_pull_form(tmp_path, monkeypatch):
    """A compose already rewritten to pull form BEFORE the shared-volume
    mapping existed still carries the `${HOST_AGENTS_DIR}` bind — on T2 that
    binds an empty host dir. ensure_pull_compose heals it in place."""
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    pull_form = {
        "services": {
            "video-tools": {
                "image": "ghcr.io/otodock/video-tools:0.1.0",
                "container_name": f"otodock-{config.INSTALL_ID}-mcp-video-tools",
                "volumes": ["${HOST_AGENTS_DIR:-../../../agents}:/agents:rw"],
                "networks": {"otodock": {"aliases": ["video-tools"]}},
            }
        },
        "networks": {"otodock": {"external": True, "name": "otodock"}},
    }
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(pull_form))
    m = SimpleNamespace(
        name="video-tools",
        mcp_dir=tmp_path,
        server=SimpleNamespace(
            runtime="docker",
            docker_compose="docker-compose.yml",
            image="ghcr.io/otodock/video-tools:0.1.0",
            service_name="video-tools",
        ),
    )

    assert compose_rewrite.ensure_pull_compose(m) is True
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    svc = written["services"]["video-tools"]
    assert svc["volumes"] == [f"{config.OTODOCK_AGENTS_VOLUME}:/agents:rw"]
    assert written["volumes"][config.OTODOCK_AGENTS_VOLUME] == {
        "external": True, "name": config.OTODOCK_AGENTS_VOLUME,
    }
    # untouched: identity stamp + network shape
    assert svc["container_name"] == f"otodock-{config.INSTALL_ID}-mcp-video-tools"
    assert written["networks"] == {"otodock": {"external": True, "name": "otodock"}}
    # second call: healed → idempotent no-op
    assert compose_rewrite.ensure_pull_compose(m) is False


# --------------------------------------------------------------------------- #
# reserved platform-name refusals
# --------------------------------------------------------------------------- #

def _compose_one(svc_key, svc):
    return {"services": {svc_key: svc}}


def test_reject_reserved_service_key():
    data = _compose_one("otodock-proxy", {"build": {"context": "."}})
    with pytest.raises(ValueError, match="reserved platform service name"):
        _transform(data, service_name="otodock-proxy", mcp_name="evil")


def test_reject_reserved_container_name_on_sibling():
    # a second, image-based service the target-only rewrite never renames
    data = {
        "services": {
            "camoufox": {"build": {"context": "."}},
            "helper": {"image": "x", "container_name": "otodock-postgres"},
        }
    }
    with pytest.raises(ValueError, match="reserved platform service name"):
        _transform(data)


def test_reject_reserved_container_name_case_insensitive():
    data = _compose_one("camoufox", {"build": {"context": "."}, "container_name": "OtoDock-Proxy"})
    with pytest.raises(ValueError, match="reserved platform service name"):
        _transform(data)


def test_reject_reserved_network_alias_pull_form(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    evil = {
        "services": {
            "camoufox": {
                "image": "ghcr.io/otodock/camoufox:0.0.55",
                "networks": {"otodock": {"aliases": ["docker-socket-proxy"]}},
            }
        }
    }
    m = _manifest(tmp_path, compose=evil)
    with pytest.raises(ValueError, match="reserved platform service name"):
        compose_rewrite.ensure_pull_compose(m)


def test_reject_reserved_service_name_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    m = _manifest(tmp_path)
    m.server.service_name = "otodock-proxy"
    with pytest.raises(ValueError, match="reserved platform service name"):
        compose_rewrite.ensure_pull_compose(m)


def test_legit_names_still_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    m = _manifest(tmp_path)  # service_name "camoufox", not reserved
    assert compose_rewrite.ensure_pull_compose(m) is True


# --------------------------------------------------------------------------- #
# platform volume refusals
# --------------------------------------------------------------------------- #

def test_reject_external_volume_declaration():
    # a manifest attaching an existing volume by name reaches the Postgres
    # socket (trusted by the image), the heap files, or every agent tree
    data = {
        "services": {"camoufox": {"build": ".", "volumes": ["sock:/var/run/postgresql"]}},
        "volumes": {"sock": {"external": True, "name": "otodock-pgsock"}},
    }
    with pytest.raises(ValueError, match="external or named volume"):
        _transform(data)


def test_reject_named_volume_declaration_without_external():
    data = {
        "services": {"camoufox": {"build": ".", "volumes": ["d:/d"]}},
        "volumes": {"d": {"name": "otodock-pgdata"}},
    }
    with pytest.raises(ValueError, match="external or named volume"):
        _transform(data)


@pytest.mark.parametrize(
    "src", ["otodock-pgdata", "OtoDock-PgSock", "otodock-ssh-keys", "otodock-mcps"],
)
def test_reject_platform_volume_source_short_form(src):
    data = {"services": {"camoufox": {"build": ".", "volumes": [f"{src}:/mnt"]}}}
    with pytest.raises(ValueError, match="platform volume"):
        _transform(data)


def test_reject_platform_volume_source_long_form_on_sibling():
    data = {"services": {
        "camoufox": {"build": "."},
        "helper": {"image": "x", "volumes": [
            {"type": "volume", "source": "otodock-sessions", "target": "/s"},
        ]},
    }}
    with pytest.raises(ValueError, match="platform volume"):
        _transform(data)


def test_reject_external_volume_pull_form(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    evil = {
        "services": {"camoufox": {
            "image": "ghcr.io/otodock/camoufox:0.0.55",
            "volumes": ["x:/var/run/postgresql"],
        }},
        "volumes": {"x": {"external": True, "name": "otodock-pgsock"}},
    }
    m = _manifest(tmp_path, compose=evil)
    with pytest.raises(ValueError, match="external or named volume"):
        compose_rewrite.ensure_pull_compose(m)


def test_per_mcp_volume_declaration_still_passes():
    data = {
        "services": {"camoufox": {"build": ".", "volumes": ["mydata:/data"]}},
        "volumes": {"mydata": None, "cache": {"driver": "local"}},
    }
    out = _transform(data)
    assert out["services"]["camoufox"]["volumes"] == ["mydata:/data"]
    assert out["volumes"]["cache"] == {"driver": "local"}


# --------------------------------------------------------------------------- #
# the pull-form path hardens every shape the build-form path does
# --------------------------------------------------------------------------- #

def _pull_manifest(tmp_path, compose, *, name="camoufox", service_name="camoufox"):
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    return SimpleNamespace(
        name=name,
        mcp_dir=tmp_path,
        server=SimpleNamespace(
            runtime="docker",
            docker_compose="docker-compose.yml",
            image="ghcr.io/otodock/camoufox:0.0.55",
            service_name=service_name,
        ),
    )


@pytest.fixture
def t2(monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")


def test_pull_form_with_no_recognizable_target_is_refused(tmp_path, t2):
    """Two image services, neither keyed nor aliased by the service name:
    the shape is refused, never left for `compose up` verbatim."""
    evil = {"services": {
        "a": {"image": "x", "privileged": True, "volumes": ["/:/host"]},
        "b": {"image": "y"},
    }}
    m = _pull_manifest(tmp_path, evil)
    original = (tmp_path / "docker-compose.yml").read_text()
    with pytest.raises(ValueError, match="cannot determine"):
        compose_rewrite.ensure_pull_compose(m)
    assert (tmp_path / "docker-compose.yml").read_text() == original


def test_pull_form_probe_shape_is_refused(tmp_path, t2):
    evil = {
        "services": {
            "a": {
                "image": "x", "privileged": True,
                "volumes": ["/:/host", "sock:/var/run/postgresql"],
                "networks": {"otodock": {"aliases": ["otodock-proxy"]}},
            },
            "b": {"image": "y"},
        },
        "volumes": {"sock": {"external": True, "name": "otodock-pgsock"}},
    }
    with pytest.raises(ValueError):
        compose_rewrite.ensure_pull_compose(_pull_manifest(tmp_path, evil))


@pytest.mark.parametrize("shape", [{}, {"services": {}}, {"services": {"a": None}}, ["a"]])
def test_pull_form_without_a_services_mapping_is_refused(tmp_path, t2, shape):
    with pytest.raises(ValueError):
        compose_rewrite.ensure_pull_compose(_pull_manifest(tmp_path, shape))


def test_pull_form_target_found_by_its_alias_and_every_service_hardened(tmp_path, t2):
    compose = {"services": {
        "app": {
            "image": "x", "privileged": True, "ports": ["8931:8931"],
            "volumes": ["./data:/data"],
            "networks": {"otodock": {"aliases": ["camoufox"]}},
        },
        "helper": {"image": "y", "cap_add": ["SYS_ADMIN"], "volumes": ["/:/host"]},
    }}
    m = _pull_manifest(tmp_path, compose)
    assert compose_rewrite.ensure_pull_compose(m) is True
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    app, helper = written["services"]["app"], written["services"]["helper"]
    assert "privileged" not in app and "ports" not in app
    assert "cap_add" not in helper
    assert helper["volumes"] == [f"otodock-{config.INSTALL_ID}-mcp-camoufox-host:/host"]
    assert app["networks"] == {"otodock": {"aliases": ["camoufox"]}}
    assert helper["networks"] == {"otodock": {}}
    assert compose_rewrite.ensure_pull_compose(m) is False


def test_pull_form_own_networks_are_replaced(tmp_path, t2):
    """A pull-form service cannot keep a network of its own choosing: the
    internal socket-proxy and data planes are named predictably."""
    evil = {
        "services": {"camoufox": {
            "image": "x",
            "networks": {"sp": {}, "data": {"ipv4_address": "10.203.0.9"}},
        }},
        "networks": {
            "sp": {"external": True, "name": "otodock_socketproxy"},
            "data": {"external": True, "name": "otodock_otodock-data"},
        },
    }
    m = _pull_manifest(tmp_path, evil)
    assert compose_rewrite.ensure_pull_compose(m) is True
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert written["services"]["camoufox"]["networks"] == {"otodock": {"aliases": ["camoufox"]}}
    assert written["networks"] == {"otodock": {"external": True, "name": "otodock"}}


def test_pull_form_list_networks_are_replaced(tmp_path, t2):
    evil = {"services": {"camoufox": {"image": "x", "networks": ["otodock_socketproxy"]}}}
    m = _pull_manifest(tmp_path, evil)
    compose_rewrite.ensure_pull_compose(m)
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert written["services"]["camoufox"]["networks"] == {"otodock": {"aliases": ["camoufox"]}}


def test_pull_form_gets_the_default_bounds(tmp_path, t2, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g", raising=False)
    m = _pull_manifest(tmp_path, {"services": {"camoufox": {"image": "x"}}})
    compose_rewrite.ensure_pull_compose(m)
    svc = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())["services"]["camoufox"]
    assert svc["mem_limit"] == "2g" and svc["logging"]["driver"] == "json-file"


# --------------------------------------------------------------------------- #
# allowlist: top-level keys, file readers, interpolation, volume drivers
# --------------------------------------------------------------------------- #

def _both_paths(tmp_path, compose_extra: dict, svc_extra: dict | None = None):
    """Yield a callable per path (build-form transform, pull-form heal)."""
    build = {"services": {"camoufox": {"build": ".", **(svc_extra or {})}}, **compose_extra}
    pull = {"services": {"camoufox": {"image": "x", **(svc_extra or {})}}, **compose_extra}

    def _build():
        return _transform(build)

    def _pull():
        return compose_rewrite.ensure_pull_compose(_pull_manifest(tmp_path, pull))

    return (_build, _pull)


@pytest.mark.parametrize("key, value", [
    ("include", ["../other/compose.yml"]),
    ("configs", {"c": {"file": "/var/run/docker.sock"}}),
    ("secrets", {"s": {"file": "/opt/otodock/config.env"}}),
    ("name", "otodock"),
    ("models", {"m": {"model": "x"}}),
])
def test_unknown_top_level_keys_are_refused(tmp_path, t2, key, value):
    for run in _both_paths(tmp_path, {key: value}):
        with pytest.raises(ValueError, match="top-level"):
            run()


def test_extension_fields_and_version_are_accepted(tmp_path, t2):
    for run in _both_paths(tmp_path, {"x-note": {"a": 1}, "version": "3.8"}):
        run()


@pytest.mark.parametrize("key, value", [
    ("configs", ["c"]),
    ("secrets", ["s"]),
    ("volumes_from", ["otodock-otodock-postgres-1"]),
    ("extends", {"file": "../x/compose.yml", "service": "s"}),
    ("extends", {"service": "other"}),
    ("label_file", "/opt/otodock/config.env"),
    ("use_api_socket", True),
    ("provider", {"type": "model"}),
    ("post_start", [{"command": "id", "privileged": True}]),
    ("pre_stop", [{"command": "id"}]),
])
def test_service_file_and_daemon_readers_are_refused(tmp_path, t2, key, value):
    for run in _both_paths(tmp_path, {}, {key: value}):
        with pytest.raises(ValueError, match="refusing"):
            run()


@pytest.mark.parametrize("value", [
    "/opt/otodock/config.env", "../x/.env", "config.env", ".env.local",
    ["/opt/otodock/config.env"], [".env", "/etc/shadow"],
    [{"path": "/opt/otodock/config.env"}], {"path": ".env"},
])
def test_env_file_other_than_the_mcp_own_is_refused(tmp_path, t2, value):
    for run in _both_paths(tmp_path, {}, {"env_file": value}):
        with pytest.raises(ValueError, match="env_file"):
            run()


@pytest.mark.parametrize("value", [
    ".env", "./.env", [".env"], [{"path": ".env", "required": False}],
])
def test_env_file_of_the_mcp_own_env_is_accepted(tmp_path, t2, value):
    for run in _both_paths(tmp_path, {}, {"env_file": value}):
        run()


@pytest.mark.parametrize("labels", [
    {"com.docker.compose.project": "otodock"},
    ["com.docker.compose.service=otodock-proxy"],
])
def test_compose_owned_labels_are_refused(tmp_path, t2, labels):
    for run in _both_paths(tmp_path, {}, {"labels": labels}):
        with pytest.raises(ValueError, match="label"):
            run()


@pytest.mark.parametrize("svc_extra", [
    {"environment": ["DB=${DATABASE_URL}"]},
    {"environment": {"K": "$JWT_SECRET"}},
    {"command": ["sh", "-c", "echo ${PROXY_API_KEY:-x}"]},
    {"healthcheck": {"test": ["CMD", "curl", "http://x/$$$JWT_SECRET"]}},
    {"deploy": {"resources": {"limits": {"memory": "${X:-1g}"}}}},
    {"mem_limit": "${DATABASE_URL}x"},
    {"mem_limit": "${X:?unset}"},
])
def test_interpolation_is_refused(tmp_path, t2, svc_extra):
    for run in _both_paths(tmp_path, {}, svc_extra):
        with pytest.raises(ValueError, match="interpolat"):
            run()


def test_interpolated_image_from_the_manifest_is_refused():
    with pytest.raises(ValueError, match="interpolat"):
        _transform(CAMOUFOX_COMPOSE, image="evil.example/${JWT_SECRET}:1")


def test_escaped_dollar_is_a_literal(tmp_path, t2):
    for run in _both_paths(tmp_path, {}, {"command": ["sh", "-c", "echo $$HOME"]}):
        run()


@pytest.mark.parametrize("key, value", [
    ("mem_limit", "${OTODOCK_CAMOUFOX_MEM_LIMIT:-3g}"),
    ("memswap_limit", "${OTODOCK_CAMOUFOX_MEM_LIMIT:-3g}"),
    ("shm_size", "${SHM:-2gb}"),
    ("mem_limit", "${OTODOCK_VIDEOTOOLS_MEM_LIMIT}"),
])
def test_a_whole_size_value_may_interpolate(tmp_path, t2, key, value):
    """A size parses as bytes or fails the load, so no string can reach the
    container through it; the catalog's per-install memory knobs use it."""
    for run in _both_paths(tmp_path, {}, {key: value}):
        run()


@pytest.mark.parametrize("spec", [
    {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/"}},
    {"driver_opts": {"type": "nfs"}},
    {"driver": "rclone"},
    "sock",
])
def test_volume_declaration_with_driver_options_is_refused(tmp_path, t2, spec):
    for run in _both_paths(tmp_path, {"volumes": {"d": spec}}, {"volumes": ["d:/d"]}):
        with pytest.raises(ValueError, match="volume"):
            run()


@pytest.mark.parametrize("vol", [
    {"type": "npipe", "source": "x", "target": "/x"},
    {"type": "cluster", "source": "x", "target": "/x"},
    {"source": "x", "target": "/x"},
])
def test_unknown_long_form_mount_types_are_refused(tmp_path, t2, vol):
    for run in _both_paths(tmp_path, {}, {"volumes": [vol]}):
        with pytest.raises(ValueError, match="mount"):
            run()


# --------------------------------------------------------------------------- #
# the released catalog shapes still pass, and a healed file is stable
# --------------------------------------------------------------------------- #

M365_COMPOSE = {"services": {"m365-mcp": {
    "build": ".", "container_name": "m365-mcp", "restart": "always",
    "ports": ["127.0.0.1:3000:3000"], "env_file": ".env",
}}}

CAMOUFOX_RELEASED = {"services": {"camoufox": {
    "build": ".", "container_name": "camoufox-mcp", "restart": "always",
    "shm_size": "2gb",
    "mem_limit": "${OTODOCK_CAMOUFOX_MEM_LIMIT:-3g}",
    "memswap_limit": "${OTODOCK_CAMOUFOX_MEM_LIMIT:-3g}",
    "ports": ["127.0.0.1:8931:8931"], "volumes": ["./screenshots:/screenshots"],
    "healthcheck": {"test": ["CMD", "python3", "/app/healthprobe.py"],
                    "interval": "90s", "timeout": "35s", "retries": 2,
                    "start_period": "60s"},
}}}

VIDEO_TOOLS_RELEASED = {"services": {"video-tools": {
    "build": ".", "container_name": "video-tools-mcp", "restart": "always",
    "mem_limit": "${OTODOCK_VIDEOTOOLS_MEM_LIMIT:-4g}",
    "memswap_limit": "${OTODOCK_VIDEOTOOLS_MEM_LIMIT:-4g}",
    "ports": ["127.0.0.1:8933:8933"],
    "volumes": ["${HOST_AGENTS_DIR:-../../../agents}:/agents:rw"],
    "env_file": ".env",
    "extra_hosts": ["host.docker.internal:host-gateway"],
    "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; "
                             "urllib.request.urlopen('http://127.0.0.1:8933/health', timeout=3)"]},
}}}


@pytest.mark.parametrize("name, compose", [
    ("m365-mcp", M365_COMPOSE),
    ("camoufox", CAMOUFOX_RELEASED),
    ("video-tools", VIDEO_TOOLS_RELEASED),
])
def test_released_catalog_composes_rewrite_and_stay_stable(tmp_path, t2, name, compose):
    m = _pull_manifest(tmp_path, compose, name=name, service_name=name)
    assert compose_rewrite.ensure_pull_compose(m) is True
    first = (tmp_path / "docker-compose.yml").read_text()
    # the healed pull-form file is a fixed point on every later start
    assert compose_rewrite.ensure_pull_compose(m) is False
    assert (tmp_path / "docker-compose.yml").read_text() == first


# --------------------------------------------------------------------------- #
# every platform service name the proxy dials is reserved
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reserved", [
    "otodock-phone", "file-tools", "collabora", "otodock-db-init",
    "otodock-agents-init", "OtoDock-Phone", "otodock-otodock-proxy-1",
    "otodock-otodock-phone-1", "otodock-file-tools-2", "collabora-agent",
])
def test_platform_service_names_are_reserved(reserved):
    with pytest.raises(ValueError, match="reserved platform service name"):
        _transform(CAMOUFOX_COMPOSE, service_name=reserved)
    data = {"services": {"camoufox": {"build": ".", "networks": {
        "otodock": {"aliases": [reserved]}}}}}
    with pytest.raises(ValueError, match="reserved platform service name"):
        _transform(data)


def test_configured_platform_hosts_are_reserved(monkeypatch):
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://phone-box:9093")
    monkeypatch.setattr(config, "COLLABORA_BACKEND_URL", "http://docs-box:9980")
    for name in ("phone-box", "docs-box"):
        with pytest.raises(ValueError, match="reserved platform service name"):
            _transform(CAMOUFOX_COMPOSE, service_name=name)


def test_reserved_manifest_service_name_is_refused_on_pull_form(tmp_path, t2):
    m = _pull_manifest(tmp_path, {"services": {"x": {"image": "x"}}},
                       service_name="otodock-phone")
    with pytest.raises(ValueError, match="reserved platform service name"):
        compose_rewrite.ensure_pull_compose(m)


# --------------------------------------------------------------------------- #
# the compose file must sit inside the MCP folder
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rel", [
    "../other/docker-compose.yml", "/etc/compose.yml", "sub/../../x.yml", "",
])
def test_compose_path_outside_the_folder_is_refused(tmp_path, t2, rel):
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "docker-compose.yml").write_text(yaml.safe_dump(CAMOUFOX_COMPOSE))
    m = SimpleNamespace(
        name="camoufox", mcp_dir=mcp_dir,
        server=SimpleNamespace(runtime="docker", docker_compose=rel,
                               image="ghcr.io/x:1", service_name="camoufox"),
    )
    with pytest.raises(ValueError, match="compose file"):
        compose_rewrite.ensure_pull_compose(m)
    assert "build" in yaml.safe_load((tmp_path / "other" / "docker-compose.yml").read_text())["services"]["camoufox"]


def test_compose_symlink_out_of_the_folder_is_refused(tmp_path, t2):
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir()
    outside = tmp_path / "outside.yml"
    outside.write_text(yaml.safe_dump(CAMOUFOX_COMPOSE))
    (mcp_dir / "docker-compose.yml").symlink_to(outside)
    m = SimpleNamespace(
        name="camoufox", mcp_dir=mcp_dir,
        server=SimpleNamespace(runtime="docker", docker_compose="docker-compose.yml",
                               image="ghcr.io/x:1", service_name="camoufox"),
    )
    with pytest.raises(ValueError, match="compose file"):
        compose_rewrite.ensure_pull_compose(m)
    assert "build" in yaml.safe_load(outside.read_text())["services"]["camoufox"]


def test_check_pull_compose_writes_nothing(tmp_path, t2):
    m = _pull_manifest(tmp_path, CAMOUFOX_COMPOSE)
    original = (tmp_path / "docker-compose.yml").read_text()
    compose_rewrite.check_pull_compose(m)
    assert (tmp_path / "docker-compose.yml").read_text() == original
    m2 = _pull_manifest(tmp_path, {"services": {"camoufox": {"build": ".", "env_file": "/x"}}})
    with pytest.raises(ValueError):
        compose_rewrite.check_pull_compose(m2)


def test_extension_anchor_merged_into_a_service_is_resolved_and_dropped(tmp_path, t2):
    text = (
        "x-common: &common\n"
        "  mem_limit: ${OTODOCK_X_MEM_LIMIT:-3g}\n"
        "  restart: always\n"
        "services:\n"
        "  camoufox:\n"
        "    <<: *common\n"
        "    image: x\n"
    )
    (tmp_path / "docker-compose.yml").write_text(text)
    m = SimpleNamespace(
        name="camoufox", mcp_dir=tmp_path,
        server=SimpleNamespace(runtime="docker", docker_compose="docker-compose.yml",
                               image="x", service_name="camoufox"),
    )
    assert compose_rewrite.ensure_pull_compose(m) is True
    written = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert "x-common" not in written
    assert written["services"]["camoufox"]["mem_limit"] == "${OTODOCK_X_MEM_LIMIT:-3g}"
    assert compose_rewrite.ensure_pull_compose(m) is False
