"""skills_installer + skills catalog plumbing.

Pins the skill-package validation matrix (its own branch — never the shared
MCP pipeline), the flat-namespace collision guards in BOTH installers, the
install-time frontmatter scrub, the empty-catalog degradation, and the
updater dispatch/targets for category "skill".
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from services.community import skills_installer as si
from services.mcp import mcp_registry


def _pkg(tmp_path, name="pdf-skills", *, category="skill", runtime="none",
         transport="none", skills=None, extra_files=None) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if skills is None:
        skills = [{"id": "pdf-processing", "file": "skills/pdf-processing/SKILL.md"}]
    for sk in skills:
        f = root / sk["file"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            "---\nname: %s\ndescription: d\nallowed-tools: Bash(*)\n---\n\nBody.\n"
            % sk["id"],
        )
    for rel, content in (extra_files or {}).items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    (root / "manifest.json").write_text(json.dumps({
        "name": name, "label": name, "description": "d", "version": "1.0.0",
        "category": category,
        "server": {"runtime": runtime, "transport": transport},
        "skills": skills,
    }))
    return root


# ── validation matrix ──────────────────────────────────────────────────

@pytest.mark.parametrize("mutate,expect", [
    (dict(category="community"), "category must be 'skill'"),
    (dict(runtime="node"), "runtime"),
    (dict(skills=[]), "at least one skill"),
    (dict(skills=[{"id": "../evil", "file": "skills/x.md"}]), "invalid skill id"),
    (dict(skills=[{"id": "ghost-skill", "file": "skills/missing.md"}]), "not found"),
    (dict(extra_files={".env": "SECRET=1"}), ".env"),
])
def test_validation_rejects(tmp_path, mutate, expect):
    kwargs = dict(mutate)
    # A skill entry pointing at a missing file shouldn't have the file created.
    skills = kwargs.pop("skills", None)
    if skills is not None and skills and "missing" in skills[0]["file"]:
        root = _pkg(tmp_path, skills=[
            {"id": "pdf-processing", "file": "skills/pdf-processing/SKILL.md"}])
        data = json.loads((root / "manifest.json").read_text())
        data["skills"] = skills
        (root / "manifest.json").write_text(json.dumps(data))
    else:
        root = _pkg(tmp_path, skills=skills, **kwargs)
    data = json.loads((root / "manifest.json").read_text())
    errors = si._validate_skill_package(data, root)
    assert any(expect in e for e in errors), errors


def test_validation_accepts_wellformed(tmp_path):
    root = _pkg(tmp_path)
    data = json.loads((root / "manifest.json").read_text())
    assert si._validate_skill_package(data, root) == []


@pytest.mark.parametrize("bad_name", ["../../proxy/api", "/etc", "a b", "UPPER!"])
def test_validation_rejects_unsafe_package_name(tmp_path, bad_name):
    """The manifest name becomes the install FOLDER — a traversal/absolute
    name would relocate _apply_extracted_files over platform code."""
    root = _pkg(tmp_path)
    data = json.loads((root / "manifest.json").read_text())
    data["name"] = bad_name
    errors = si._validate_skill_package(data, root)
    assert any("invalid package name" in e for e in errors), errors


def test_validation_rejects_absolute_skill_file(tmp_path):
    """An absolute skills[].file survives the pkg_root join verbatim — the
    scrub step would then rewrite that file in place, wherever it is."""
    outside = tmp_path / "outside.md"
    outside.write_text("---\nx: 1\n---\nbody")
    root = _pkg(tmp_path)
    data = json.loads((root / "manifest.json").read_text())
    data["skills"] = [{"id": "pdf-processing", "file": str(outside)}]
    errors = si._validate_skill_package(data, root)
    assert any("not found in package" in e for e in errors), errors


def test_scripts_allowed_and_flagged(tmp_path):
    """Executable scripts/ content is ALLOWED since 2026-08-27 (operator
    decision — see the module docstring) and surfaced via has_scripts."""
    root = _pkg(tmp_path, extra_files={
        "skills/pdf-processing/scripts/run.py": "print()"})
    data = json.loads((root / "manifest.json").read_text())
    assert si._validate_skill_package(data, root) == []
    assert si.package_has_scripts(root) is True
    assert si.package_has_scripts(_pkg(tmp_path, name="plain-skills")) is False
    # An EMPTY scripts/ dir is not executable content.
    empty = _pkg(tmp_path, name="empty-scripts")
    (empty / "skills/pdf-processing/scripts").mkdir(parents=True, exist_ok=True)
    assert si.package_has_scripts(empty) is False


# ── install ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_install_lands_scrubbed_in_skills_dir(temp_db, tmp_path, monkeypatch):
    import config as app_config
    monkeypatch.setattr(app_config, "MCPS_DIR", tmp_path / "mcps")
    root = _pkg(tmp_path)
    with patch.object(mcp_registry, "scan_manifests"):
        result = await si.install_skill_package_from_extracted(root)
    assert result["status"] == "installed" and result["kind"] == "skill"
    installed = tmp_path / "mcps/skills/pdf-skills/skills/pdf-processing/SKILL.md"
    text = installed.read_text()
    assert "allowed-tools" not in text        # scrubbed at install
    assert "name: pdf-processing" in text


@pytest.mark.asyncio
async def test_install_rejects_name_collision_with_mcp(temp_db, tmp_path, monkeypatch):
    from fastapi import HTTPException
    import config as app_config
    monkeypatch.setattr(app_config, "MCPS_DIR", tmp_path / "mcps")
    root = _pkg(tmp_path, name="camoufox")   # pretend an MCP has this name
    fake = type("M", (), {"category": "community", "name": "camoufox"})()
    with patch.object(mcp_registry, "get_manifest", return_value=fake):
        with pytest.raises(HTTPException) as ei:
            await si.install_skill_package_from_extracted(root)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_install_rejects_skill_id_collision(temp_db, tmp_path, monkeypatch):
    from fastapi import HTTPException
    import config as app_config
    monkeypatch.setattr(app_config, "MCPS_DIR", tmp_path / "mcps")
    root = _pkg(tmp_path)
    other = type("M", (), {"name": "other-pkg"})()
    with patch.object(mcp_registry, "get_manifest", return_value=None), \
         patch.object(mcp_registry, "find_skill_provider", return_value=other):
        with pytest.raises(HTTPException) as ei:
            await si.install_skill_package_from_extracted(root)
    assert ei.value.status_code == 409


def test_mcp_installer_rejects_cross_catalog_collisions():
    """The MCP-side mirror guards exist (plan §3: enforced in BOTH installers)."""
    import inspect
    from services.community import community_installer as ci
    src = inspect.getsource(ci.install_from_extracted_folder)
    assert "find_skill_provider" in src
    assert 'category == "skill"' in src


# ── catalog degradation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skills_registry_degrades_to_empty(monkeypatch):
    from services.community import community_catalog as cc
    monkeypatch.setattr(cc, "_skills_registry_cache", cc._CacheEntry(value=None))
    with patch.object(cc, "_http_get_json",
                      AsyncMock(side_effect=RuntimeError("net down"))):
        doc = await cc.fetch_skills_registry()
    assert doc["skills"] == []
    assert doc["catalog_unreachable"] is True


@pytest.mark.asyncio
async def test_skills_registry_serves_cache_on_failure(monkeypatch):
    from services.community import community_catalog as cc
    good = {"skills": [{"name": "pdf-skills", "version": "1.0.0"}]}
    monkeypatch.setattr(
        cc, "_skills_registry_cache",
        cc._CacheEntry(value=good, fetched_at=-10_000),
    )
    with patch.object(cc, "_http_get_json",
                      AsyncMock(side_effect=RuntimeError("net down"))):
        doc = await cc.fetch_skills_registry()
    assert doc is good


# ── updater dispatch + targets ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_one_dispatches_skill_to_skills_installer(temp_db):
    from services.mcp import mcp_updater
    fake = type("M", (), {"category": "skill", "name": "pdf-skills",
                          "server": type("S", (), {"runtime": "none"})()})()
    with patch.object(mcp_registry, "get_manifest", return_value=fake), \
         patch.object(si, "install_skill_package_from_catalog",
                      AsyncMock(return_value={"status": "updated"})) as inst:
        result = await mcp_updater.update_one("pdf-skills")
    inst.assert_awaited_once_with("pdf-skills")
    assert result["status"] == "updated"


def test_community_targets_include_skill_packages(temp_db):
    from services.mcp import mcp_updater
    skill_m = type("M", (), {"category": "skill", "name": "pdf-skills",
                             "server": type("S", (), {"runtime": "none"})()})()
    custom_m = type("M", (), {"category": "custom", "name": "tts-mcp",
                              "server": type("S", (), {"runtime": "python"})()})()
    with patch.object(mcp_registry, "get_all_manifests",
                      return_value={"pdf-skills": skill_m, "tts-mcp": custom_m}):
        names = {m.name for m in mcp_updater.community_targets()}
    assert "pdf-skills" in names
    assert "tts-mcp" not in names


# ── bare-folder (spec-standard) zip install ────────────────────────────

@pytest.mark.asyncio
async def test_bare_skill_folder_synthesizes_package(temp_db, tmp_path, monkeypatch):
    """A spec-standard bare skill folder installs with a synthesized package
    manifest — SKILL.md IS the manifest, no manifest.json needed in the zip."""
    import config as app_config
    monkeypatch.setattr(app_config, "MCPS_DIR", tmp_path / "mcps")
    bare = tmp_path / "upload" / "wrapping-folder"
    (bare / "scripts").mkdir(parents=True)
    (bare / "scripts" / "run.py").write_text("print()")
    (bare / "SKILL.md").write_text(
        "---\nname: my-cool-skill\ndescription: Does cool things.\n"
        "metadata:\n  version: \"2.1\"\nallowed-tools: Bash(*)\n---\n\nBody.\n")
    with patch.object(mcp_registry, "scan_manifests"):
        result = await si.install_bare_skill_folder(bare)
    assert result["status"] == "installed"
    assert result["name"] == "my-cool-skill"      # frontmatter, not folder name
    assert result["version"] == "2.1"             # metadata.version honored
    assert result["has_scripts"] is True
    pkg = tmp_path / "mcps/skills/my-cool-skill"
    manifest = json.loads((pkg / "manifest.json").read_text())
    assert manifest["category"] == "skill"
    assert manifest["server"] == {"runtime": "none", "transport": "none"}
    assert manifest["skills"][0]["file"] == "skills/my-cool-skill/SKILL.md"
    text = (pkg / "skills/my-cool-skill/SKILL.md").read_text()
    assert "allowed-tools" not in text            # install scrub still applies
    assert (pkg / "skills/my-cool-skill/scripts/run.py").is_file()


@pytest.mark.asyncio
async def test_bare_skill_folder_rejects_bad_name(tmp_path):
    from fastapi import HTTPException
    bare = tmp_path / "up" / "folder"
    bare.mkdir(parents=True)
    (bare / "SKILL.md").write_text(
        "---\nname: Bad Name\ndescription: d\n---\n\nBody.\n")
    with pytest.raises(HTTPException) as ei:
        await si.install_bare_skill_folder(bare)
    assert ei.value.status_code == 400
    assert "name" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_bare_skill_folder_requires_description(tmp_path):
    from fastapi import HTTPException
    bare = tmp_path / "up2" / "folder"
    bare.mkdir(parents=True)
    (bare / "SKILL.md").write_text("---\nname: fine-name\n---\n\nBody.\n")
    with pytest.raises(HTTPException) as ei:
        await si.install_bare_skill_folder(bare)
    assert ei.value.status_code == 400
    assert "description" in str(ei.value.detail)
