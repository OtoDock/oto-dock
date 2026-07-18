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
    (dict(extra_files={"skills/pdf-processing/scripts/run.py": "print()"}),
     "scripts/"),
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
