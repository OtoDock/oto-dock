"""Tests for the T1 docker-MCP override + subnet allocator (compose_rewrite) and
the related docker_manager / community_installer hooks.

All docker calls are monkeypatched away — these are pure-logic tests.
"""

import types

import pytest
import yaml

import config
from core.config import deployment
from services.mcp import compose_rewrite as cr


def _mk_manifest(
    tmp_path, *, runtime="docker", image="ghcr.io/otodock/camoufox:0.0.68",
    service_name="", compose="docker-compose.yml", name="camoufox", with_base=True,
):
    mcp_dir = tmp_path / name
    mcp_dir.mkdir(exist_ok=True)
    if with_base:
        (mcp_dir / compose).write_text(yaml.safe_dump({
            "services": {
                "camoufox": {
                    "build": {"context": "."},
                    "container_name": "camoufox-mcp",
                    "ports": ["127.0.0.1:8931:8931"],
                },
            },
        }))
    server = types.SimpleNamespace(
        runtime=runtime, docker_compose=compose, service_name=service_name, image=image,
    )
    return types.SimpleNamespace(name=name, mcp_dir=mcp_dir, server=server)


@pytest.fixture
def t1(monkeypatch):
    """Put compose_rewrite in T1 mode with a fixed (docker-free) used-subnet scan."""
    monkeypatch.setattr(config, "INSTALL_ID", "testid")
    monkeypatch.setattr(config, "OTODOCK_MCP_ADDRESS_POOL", "10.201.0.0/16")
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(
        cr, "_collect_used_subnets", lambda exclude_network=None: ["172.17.0.0/16"],
    )
    monkeypatch.setattr(cr, "_picked_subnets", {})


# --------------------------------------------------------------------------- #
# allocate_mcp_subnet
# --------------------------------------------------------------------------- #

def test_allocator_cases():
    A = cr.allocate_mcp_subnet
    assert A("10.201.0.0/16", []) == "10.201.0.0/24"
    assert A("10.201.0.0/16", ["10.201.0.0/24"]) == "10.201.1.0/24"
    # a used network LARGER than /24 blocks every /24 it covers (overlap, not exact)
    assert A("10.201.0.0/16", ["10.201.0.0/20"]) == "10.201.16.0/24"
    # reuse a recorded subnet that's still free
    assert A("10.201.0.0/16", ["10.201.0.0/24"], recorded="10.201.5.0/24") == "10.201.5.0/24"
    # recorded now overlaps a used subnet → reallocate
    assert A("10.201.0.0/16", ["10.201.5.0/24"], recorded="10.201.5.0/24") == "10.201.0.0/24"
    # recorded outside the pool → ignore
    assert A("10.201.0.0/16", [], recorded="10.99.0.0/24") == "10.201.0.0/24"
    # IPv6 used subnets are filtered, not crashed on
    assert A("10.201.0.0/16", ["fd00::/64", "10.201.0.0/24"]) == "10.201.1.0/24"
    # exhaustion → None (caller omits ipam)
    assert A("10.201.0.0/24", ["10.201.0.0/24"]) is None
    # a pool that is itself a /24 (or smaller) → use the whole pool when free
    assert A("10.201.0.0/24", []) == "10.201.0.0/24"
    assert A("10.201.0.0/25", []) == "10.201.0.0/25"
    # invalid pool → None
    assert A("not-a-cidr", []) is None


# --------------------------------------------------------------------------- #
# ensure_t1_override
# --------------------------------------------------------------------------- #

def test_override_generated_on_t1(tmp_path, t1):
    m = _mk_manifest(tmp_path)
    path = cr.ensure_t1_override(m)
    assert path is not None and path.is_file()
    data = yaml.safe_load(path.read_text())
    svc = data["services"]["camoufox"]
    assert svc["container_name"] == "otodock-testid-mcp-camoufox"
    assert svc["image"] == "ghcr.io/otodock/camoufox:0.0.68"
    assert data["networks"]["default"]["ipam"]["config"][0]["subnet"] == "10.201.0.0/24"


def test_override_idempotent_and_subnet_stable(tmp_path, t1):
    m = _mk_manifest(tmp_path)
    cr.ensure_t1_override(m)
    first = (m.mcp_dir / "docker-compose.override.yml").read_text()
    # Second call reuses the recorded subnet (it's free) → byte-identical content.
    cr.ensure_t1_override(m)
    assert (m.mcp_dir / "docker-compose.override.yml").read_text() == first


def test_override_force_realloc_picks_new_subnet(tmp_path, t1, monkeypatch):
    m = _mk_manifest(tmp_path)
    cr.ensure_t1_override(m)
    # Now pretend the originally-picked /24 is taken by another stack.
    monkeypatch.setattr(
        cr, "_collect_used_subnets",
        lambda exclude_network=None: ["172.17.0.0/16", "10.201.0.0/24"],
    )
    cr.ensure_t1_override(m, force_realloc=True)
    data = yaml.safe_load((m.mcp_dir / "docker-compose.override.yml").read_text())
    assert data["networks"]["default"]["ipam"]["config"][0]["subnet"] == "10.201.1.0/24"


def test_two_projects_picking_before_either_network_exists_never_collide(tmp_path, t1):
    """The boot starts the projects concurrently: the second pick sees the
    first one although docker has no network for it yet."""
    a = _mk_manifest(tmp_path, name="camoufox")
    b = _mk_manifest(tmp_path, name="video-tools")

    def subnet(m):
        return yaml.safe_load(cr.ensure_t1_override(m).read_text())["networks"]["default"]["ipam"]["config"][0]["subnet"]

    first, second = subnet(a), subnet(b)
    assert first != second
    # A project's own earlier pick is no obstacle to itself.
    assert subnet(a) == first


def test_override_imageless_has_no_image_key(tmp_path, t1):
    m = _mk_manifest(tmp_path, image="")
    path = cr.ensure_t1_override(m)
    svc = yaml.safe_load(path.read_text())["services"]["camoufox"]
    assert "image" not in svc
    assert svc["container_name"] == "otodock-testid-mcp-camoufox"


def test_override_carries_default_bounds(tmp_path, t1, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g", raising=False)
    m = _mk_manifest(tmp_path)  # base compose declares no limits
    svc = yaml.safe_load(cr.ensure_t1_override(m).read_text())["services"]["camoufox"]
    assert svc["mem_limit"] == "2g"
    assert svc["memswap_limit"] == "2g"
    assert svc["logging"] == {
        "driver": "json-file", "options": {"max-size": "10m", "max-file": "5"},
    }


def test_override_respects_base_declared_limits(tmp_path, t1, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_MCP_DEFAULT_MEM_LIMIT", "2g", raising=False)
    m = _mk_manifest(tmp_path, with_base=False)
    (m.mcp_dir / "docker-compose.yml").write_text(yaml.safe_dump({
        "services": {
            "camoufox": {
                "build": {"context": "."},
                "mem_limit": "3g",
                "memswap_limit": "3g",
                "logging": {"driver": "json-file", "options": {"max-size": "50m"}},
            },
        },
    }))
    svc = yaml.safe_load(cr.ensure_t1_override(m).read_text())["services"]["camoufox"]
    # camoufox-style explicit limits win — the override must not shadow them.
    assert "mem_limit" not in svc
    assert "memswap_limit" not in svc
    assert "logging" not in svc


def test_override_noop_on_t2(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    m = _mk_manifest(tmp_path)
    assert cr.ensure_t1_override(m) is None
    assert not (m.mcp_dir / "docker-compose.override.yml").exists()


def test_override_noop_for_non_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    m = _mk_manifest(tmp_path, runtime="python")
    assert cr.ensure_t1_override(m) is None


# --------------------------------------------------------------------------- #
# docker_manager._compose_cmd — override only when present
# --------------------------------------------------------------------------- #

def test_compose_cmd_appends_override_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INSTALL_ID", "testid")
    from services.mcp import docker_manager as dm
    m = _mk_manifest(tmp_path)
    override = m.mcp_dir / "docker-compose.override.yml"

    cmd = dm._compose_cmd(m, "ps")
    assert str(override) not in cmd
    assert cmd[-1] == "ps"

    override.write_text("services: {}\n")
    cmd2 = dm._compose_cmd(m, "ps")
    assert "-f" in cmd2 and str(override) in cmd2
    assert cmd2[-1] == "ps"
    # base compose still present + first
    assert str(m.mcp_dir / "docker-compose.yml") in cmd2


# --------------------------------------------------------------------------- #
# community_installer — override preserved across update
# --------------------------------------------------------------------------- #

def test_override_preserved_across_update(tmp_path):
    from services.community import community_installer as ci
    target = tmp_path / "mcp"
    target.mkdir()
    (target / "manifest.json").write_text("old")
    (target / "docker-compose.override.yml").write_text("# pinned subnet\nnetworks: {}\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text("new")  # tarball has no override
    backup = target.with_suffix(".bak")

    ci._apply_extracted_files(src, target, is_update=True, backup_dir=backup)

    assert (target / "manifest.json").read_text() == "new"          # replaced
    assert (target / "docker-compose.override.yml").is_file()       # preserved
    assert "pinned subnet" in (target / "docker-compose.override.yml").read_text()


@pytest.mark.parametrize("svc", [
    {"deploy": ["replicas: 2"]},
    {"deploy": {"resources": "small"}},
    {"deploy": {"resources": {"limits": None}}},
])
def test_a_deploy_block_of_another_shape_reads_as_no_limit(svc, monkeypatch):
    """A catalog compose is the author's YAML: a `deploy` that is not a
    mapping at some level declares no memory limit, never a crash."""
    monkeypatch.setattr(cr, "_default_mem_limit", lambda: "2g")
    out = cr._missing_default_bounds(svc)
    assert out["mem_limit"] == out["memswap_limit"] == "2g"


def test_a_deploy_memory_limit_still_wins(monkeypatch):
    monkeypatch.setattr(cr, "_default_mem_limit", lambda: "2g")
    out = cr._missing_default_bounds({"deploy": {"resources": {"limits": {"memory": "512m"}}}})
    assert "mem_limit" not in out
