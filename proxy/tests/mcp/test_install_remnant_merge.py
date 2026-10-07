"""Fresh install onto an existing unregistered folder must merge, not crash.

A community MCP's folder can exist on disk without a registry entry — e.g.
ssh-server's preserved ``keys/`` data dir carried across a migration, or a
half-cleaned install. The fresh-install branch used a plain copytree, which
raised FileExistsError; it must merge instead, keeping preserved data dirs,
and end as an update ends (nothing else of the remnant survives). The install
backs the remnant up and restores it on failure, and never takes a folder
that holds another MCP.
"""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import config
from services.community import community_installer as ci
from services.community.community_installer import _apply_extracted_files
from services.mcp import mcp_registry


def _mk(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def test_fresh_install_merges_into_existing_remnant(tmp_path: Path):
    src = _mk(tmp_path / "src", {"manifest.json": "{}", "server.py": "new"})
    target = _mk(tmp_path / "community" / "ssh-server", {
        "keys/id_ed25519": "SECRET",       # preserved data dir remnant
        "server.py": "old",
    })

    _apply_extracted_files(src, target, is_update=False, backup_dir=None)

    assert (target / "manifest.json").is_file()
    assert (target / "server.py").read_text() == "new"          # incoming wins
    assert (target / "keys" / "id_ed25519").read_text() == "SECRET"  # kept


def test_fresh_install_plain_copy_when_target_absent(tmp_path: Path):
    src = _mk(tmp_path / "src", {"manifest.json": "{}"})
    target = tmp_path / "community" / "new-mcp"

    _apply_extracted_files(src, target, is_update=False, backup_dir=None)

    assert (target / "manifest.json").is_file()


def test_a_remnant_keeps_only_its_preserved_items(tmp_path: Path):
    """A remnant merge ends where an update ends: the preserved data and the
    incoming folder, nothing else (a hand-dropped ``patches/`` would be
    applied to the next ``node_modules``)."""
    src = _mk(tmp_path / "src", {"manifest.json": "{}", "server.js": "new"})
    target = _mk(tmp_path / "community" / "node-mcp", {
        "patches/stray.patch": "x",
        "old-helper.js": "old",
        "keys/id": "k",
        "config/settings.json": "{}",
        "docker-compose.override.yml": "pin",
    })

    _apply_extracted_files(src, target, is_update=False, backup_dir=None)

    assert not (target / "patches").exists()
    assert not (target / "old-helper.js").exists()
    assert (target / "server.js").read_text() == "new"
    assert (target / "keys" / "id").read_text() == "k"
    assert (target / "config" / "settings.json").is_file()
    assert (target / "docker-compose.override.yml").read_text() == "pin"


_PATCHED = {"node_modules/pkg/index.js": "patched", "patches/a.patch": "v1"}


@pytest.mark.parametrize("incoming", [
    {"patches/a.patch": "v2"},                           # revised bytes
    {"patches/b.patch": "v1"},                           # renamed
    {},                                                  # dropped
    {"patches/a.patch": "v1", "patches/b.patch": "v9"},  # added
], ids=["revised", "renamed", "dropped", "added"])
def test_a_changed_patch_set_drops_the_preserved_node_modules(tmp_path: Path, incoming):
    """npm keeps an unchanged package version as it is, so files an earlier
    patch changed stay changed: a different patch set reinstalls clean."""
    src = _mk(tmp_path / "src", {"manifest.json": "{}", **incoming})
    target = _mk(tmp_path / "community" / "node-mcp", {"manifest.json": "{}", **_PATCHED})
    backup = target.with_suffix(".bak")

    _apply_extracted_files(src, target, is_update=True, backup_dir=backup)

    assert not (target / "node_modules").exists()
    assert (backup / "node_modules" / "pkg" / "index.js").read_text() == "patched"


def test_an_unchanged_patch_set_keeps_node_modules(tmp_path: Path):
    src = _mk(tmp_path / "src", {"manifest.json": "{}", "patches/a.patch": "v1",
                                 "patches/notes.txt": "not a patch"})
    target = _mk(tmp_path / "community" / "node-mcp", {"manifest.json": "{}", **_PATCHED})

    _apply_extracted_files(src, target, is_update=True, backup_dir=target.with_suffix(".bak"))

    assert (target / "node_modules" / "pkg" / "index.js").read_text() == "patched"


def test_a_remnant_with_a_stray_patch_reinstalls_its_node_modules(tmp_path: Path):
    src = _mk(tmp_path / "src", {"manifest.json": "{}"})
    target = _mk(tmp_path / "community" / "node-mcp", _PATCHED)

    _apply_extracted_files(src, target, is_update=False, backup_dir=None)

    assert not (target / "node_modules").exists()
    assert not (target / "patches").exists()


def test_a_backup_that_fails_part_way_is_removed(tmp_path: Path, monkeypatch):
    """A half-written backup is never left for a rollback to put in place of
    the intact folder."""
    import shutil
    src = _mk(tmp_path / "src", {"manifest.json": "{}"})
    target = _mk(tmp_path / "community" / "node-mcp", {"a.js": "a", "b.js": "b"})
    backup = target.with_suffix(".bak")
    real = shutil.copytree

    def _partial(s, d, *a, **kw):
        if Path(d) == backup:
            Path(d).mkdir()
            (Path(d) / "a.js").write_text("a")
            raise OSError("disk full")
        return real(s, d, *a, **kw)

    monkeypatch.setattr(ci.shutil, "copytree", _partial)
    with pytest.raises(OSError):
        _apply_extracted_files(src, target, is_update=True, backup_dir=backup)
    assert not backup.exists()
    assert (target / "b.js").read_text() == "b"


# ── the install around the merge: backup, rollback, another MCP's folder ──

@pytest.fixture
def mcps_dir(tmp_path, monkeypatch):
    root = tmp_path / "opt" / "mcps"
    (root / "community").mkdir(parents=True)
    monkeypatch.setattr(config, "MCPS_DIR", root)
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    return root


def _manifest(name: str) -> dict:
    return {"name": name, "label": "n", "description": "d", "version": "1.0.0",
            "category": "community",
            "server": {"runtime": "node", "transport": "stdio", "command": "node",
                       "args": ["x.js"], "source": "npm:pkg"}}


def _incoming(tmp_path: Path, name: str, tag: str) -> Path:
    return _mk(tmp_path / "upload" / f"{name}-{tag}", {
        "manifest.json": json.dumps(_manifest(name)), "README.md": tag,
    })


def _package_install(monkeypatch, ok: bool):
    async def _install(mcp_dir, runtime, source, **kw):
        return ci.mcp_installer.InstallResult(ok=ok, log="" if ok else "npm failed",
                                              version_hash="h", resolved_version="1.0.0")
    monkeypatch.setattr(ci.mcp_installer, "install_mcp", _install)


@pytest.mark.asyncio
async def test_a_failed_install_over_a_remnant_restores_it(mcps_dir, tmp_path, monkeypatch):
    _package_install(monkeypatch, ok=False)
    remnant = _mk(mcps_dir / "community" / "node-mcp", {
        "patches/stray.patch": "x", "old-helper.js": "old", "keys/id": "k",
    })
    before = sorted(p.relative_to(remnant) for p in remnant.rglob("*"))
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(_incoming(tmp_path, "node-mcp", "a"))
    assert ei.value.status_code == 500
    assert sorted(p.relative_to(remnant) for p in remnant.rglob("*")) == before
    assert (remnant / "old-helper.js").read_text() == "old"
    assert not remnant.with_suffix(".bak").exists()


@pytest.mark.asyncio
async def test_a_folder_whose_manifest_names_another_mcp_is_refused(mcps_dir, tmp_path, monkeypatch):
    _package_install(monkeypatch, ok=True)
    other = _mk(mcps_dir / "community" / "node-mcp", {
        "manifest.json": json.dumps(_manifest("other-mcp")), "server.js": "theirs",
    })
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(_incoming(tmp_path, "node-mcp", "a"))
    assert ei.value.status_code == 409
    assert "other-mcp" in ei.value.detail
    assert (other / "server.js").read_text() == "theirs"


@pytest.mark.asyncio
async def test_the_folder_of_a_registered_mcp_is_never_taken(mcps_dir, tmp_path, monkeypatch):
    """An MCP keeps its folder when its name changes, so a folder named after
    a new install can belong to a registered MCP of another name."""
    _package_install(monkeypatch, ok=True)
    await ci.install_from_extracted_folder(_incoming(tmp_path, "renamed-mcp", "a"))
    folder = mcps_dir / "community" / "node-mcp"
    (mcps_dir / "community" / "renamed-mcp").rename(folder)
    (folder / "manifest.json").unlink()
    mcp_registry._manifests["renamed-mcp"].mcp_dir = folder
    with pytest.raises(HTTPException) as ei:
        await ci.install_from_extracted_folder(_incoming(tmp_path, "node-mcp", "b"))
    assert ei.value.status_code == 409
    assert "renamed-mcp" in ei.value.detail
    assert (folder / "README.md").read_text() == "a"
    assert mcp_registry.get_manifest("node-mcp") is None
