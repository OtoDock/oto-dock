"""Standalone skill-package installer (``category: "skill"``).

Installs skill packages from the ``OtoDock/community-skills`` catalog into
``mcps/skills/<folder>`` — the skills twin of ``community_installer``, with
its OWN validation branch (deliberately not the shared MCP pipeline: a
"skill"-labeled package must never reach ``mcp_installer.install_mcp`` and
gain code execution under a lower-risk label).

Hard invariants enforced here:
- ``category == "skill"`` and ``server.runtime/transport == "none"`` —
  a skill package IS a context-only manifest, nothing more;
- non-empty ``skills[]``; every skill id passes the skill-name grammar
  (ids become filesystem path components at materialization) and its
  ``file`` exists inside the package;
- no ``.env`` anywhere (same credential-leak rule as MCP archives);
- package NAME must not collide with any installed non-skill manifest,
  and skill IDS must not collide with skills of any other installed
  provider (the registry namespace is flat);
- every skill markdown is frontmatter-SCRUBBED in place at install —
  ``allowed-tools`` etc. never reach disk (belt-and-braces with the
  scrub at materialization).

Executable content (``scripts/`` — the Agent Skills standard) is ALLOWED
since 2026-08-27 (operator decision, plan
the 1.5 skills-completion round): the agent can
already write and run arbitrary code under its normal permission tiers,
so skill scripts add provenance risk, not capability — controlled by the
admin-gated install paths, catalog curation review, the frontmatter
scrub, and the materializer's digest tamper-repair. ``has_scripts`` is
computed here and surfaced as a badge wherever packages are listed, so
the provenance is always visible. (The pre-1.5 v1 policy rejected
non-empty ``scripts/`` outright.)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import logging

from fastapi import HTTPException

import config
from services.community.community_installer import (
    ProgressCb,
    _apply_extracted_files,
    _emit,
    _existing_enabled_state,
    _extract_mcp_subfolder,
    _fetch_catalog_tarball,
    _is_safe_name,
    _rollback_extracted_files,
)
from services.mcp import mcp_registry
from services.mcp.mcp_manifest_types import SKILL_ID_MAX_LEN, SKILL_ID_RE
from storage import mcp_store

logger = logging.getLogger("claude-proxy.skills-installer")

SKILLS_TARBALL_URL = (
    "https://api.github.com/repos/OtoDock/community-skills/tarball/main"
)


def _validate_skill_package(data: dict, pkg_root: Path) -> list[str]:
    """Validation errors for a skill-package manifest + folder. Empty = valid."""
    errors: list[str] = []
    for field in ("name", "label", "description", "version", "category", "server"):
        if not data.get(field):
            errors.append(f"missing required field: {field}")
    if errors:
        return errors

    if data["category"] != "skill":
        errors.append(f"category must be 'skill', got {data['category']!r}")
    # The manifest name becomes the install FOLDER under mcps/skills/ — the
    # same grammar gate the MCP installer applies, or a crafted name
    # ("../../x", an absolute path) relocates _apply_extracted_files.
    if not _is_safe_name(data["name"]):
        errors.append(
            f"invalid package name {data['name']!r} (lowercase letters/"
            f"digits/hyphens/underscores only)"
        )
    server = data.get("server") or {}
    # Tested invariant: anything else would gain code execution under a
    # lower-risk label (plan §3).
    if server.get("runtime") != "none" or server.get("transport") != "none":
        errors.append("skill packages must declare server.runtime and "
                      "server.transport as 'none'")

    skills = data.get("skills") or []
    if not skills:
        errors.append("skill packages must declare at least one skill")
    for sk in skills:
        sid = sk.get("id", "")
        if not SKILL_ID_RE.fullmatch(sid or "") or len(sid) > SKILL_ID_MAX_LEN:
            errors.append(f"invalid skill id: {sid!r}")
            continue
        rel = sk.get("file", "")
        # Must stay a RELATIVE path resolving inside the package — an
        # absolute value survives the join (`pkg_root / "/etc/x"` IS
        # "/etc/x") and the scrub step would rewrite that file in place.
        f = (pkg_root / rel) if rel and not Path(rel).is_absolute() else None
        try:
            inside = bool(f) and f.resolve().is_relative_to(pkg_root.resolve())
        except OSError:
            inside = False
        if f is None or not inside or not f.is_file() or ".." in Path(rel).parts:
            errors.append(f"skill {sid}: file {rel!r} not found in package")

    if any(p.is_file() for p in pkg_root.rglob(".env")):
        errors.append("package must not contain .env files")
    return errors


def package_has_scripts(pkg_root: Path) -> bool:
    """True when the package bundles executable content (any non-empty
    ``scripts/`` dir — the Agent Skills convention). Surfaced as a
    provenance badge in the catalog browser / admin Skills page / request
    queue; not a rejection since 2026-08-27 (see module docstring)."""
    return any(
        sub.is_dir() and any(sub.iterdir())
        for sub in pkg_root.rglob("scripts")
    )


def _check_collisions(data: dict) -> None:
    """Flat-namespace guards: package name + every skill id (plan §3)."""
    name = data["name"]
    existing = mcp_registry.get_manifest(name)
    if existing is not None and existing.category != "skill":
        raise HTTPException(
            409,
            f"Name {name!r} is already an installed {existing.category} MCP — "
            "skill packages share the flat registry namespace",
        )
    for sk in data.get("skills") or []:
        provider = mcp_registry.find_skill_provider(sk["id"])
        if provider is not None and provider.name != name:
            raise HTTPException(
                409,
                f"Skill id {sk['id']!r} is already provided by {provider.name!r}",
            )


def _scrub_package_skills(data: dict, pkg_root: Path) -> None:
    """Frontmatter-scrub every declared skill file in place."""
    from services.mcp.skill_format import scrub_frontmatter
    for sk in data.get("skills") or []:
        f = pkg_root / sk["file"]
        f.write_text(scrub_frontmatter(f.read_text(), origin=str(f)))


async def install_skill_package_from_extracted(
    pkg_root: Path, *, progress_cb: ProgressCb = None,
) -> dict:
    """Install or update a skill package from an extracted folder."""
    manifest_path = pkg_root / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(400, "manifest.json not found in skill package")
    try:
        data = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid manifest.json: {e}")

    errors = _validate_skill_package(data, pkg_root)
    if errors:
        raise HTTPException(400, f"Invalid skill package: {'; '.join(errors)}")
    _check_collisions(data)

    await _emit(progress_cb, "validate", 30, "Package validated")
    await asyncio.to_thread(_scrub_package_skills, data, pkg_root)

    name = data["name"]
    version = data.get("version", "unknown")
    target_parent = config.MCPS_DIR / "skills"
    target_parent.mkdir(parents=True, exist_ok=True)

    existing = mcp_registry.get_manifest(name)
    is_update = existing is not None
    old_version = existing.version if existing else None
    prior_enabled = _existing_enabled_state(name) if is_update else None
    folder_name = existing.mcp_dir.name if is_update else name
    target_dir = target_parent / folder_name

    await _emit(progress_cb, "install", 60, "Applying package files")
    backup_dir: Path | None = None
    if is_update and target_dir.exists():
        backup_dir = target_dir.with_suffix(".bak")
    await asyncio.to_thread(
        _apply_extracted_files, pkg_root, target_dir, is_update, backup_dir,
    )
    try:
        await asyncio.to_thread(mcp_registry.scan_manifests)
        if is_update and prior_enabled is not None:
            await asyncio.to_thread(mcp_store.set_mcp_enabled, name, prior_enabled)
        else:
            # Fresh install → platform-enabled, like an explicit MCP install;
            # per-agent enablement stays a manager decision (Skills tab).
            await asyncio.to_thread(mcp_store.set_mcp_enabled, name, True)
    except Exception:
        if backup_dir is not None and backup_dir.exists():
            await asyncio.to_thread(
                _rollback_extracted_files, target_dir, backup_dir,
            )
            await asyncio.to_thread(mcp_registry.scan_manifests)
        raise
    finally:
        if backup_dir is not None and backup_dir.exists():
            await asyncio.to_thread(shutil.rmtree, backup_dir, True)

    await _emit(progress_cb, "done", 100, "Installed")
    has_scripts = package_has_scripts(target_dir)
    logger.info("skill package %s %s (%s -> %s%s)",
                name, "updated" if is_update else "installed",
                old_version, version,
                ", bundles scripts" if has_scripts else "")
    return {
        "status": "updated" if is_update else "installed",
        "name": name,
        "version": version,
        "old_version": old_version,
        "kind": "skill",
        "has_scripts": has_scripts,
    }


async def install_bare_skill_folder(
    skill_root: Path, *, progress_cb: ProgressCb = None,
) -> dict:
    """Install a spec-standard BARE skill folder (SKILL.md at its root, no
    package manifest) by synthesizing the package around it.

    SKILL.md IS the manifest per the Agent Skills spec — its frontmatter
    ``name`` + ``description`` are authoritative (a zip's wrapping folder
    name is not trusted). The folder is restaged into the package layout
    (``manifest.json`` + ``skills/<name>/…``) and handed to the normal
    installer, which validates, collision-checks, and scrubs as usual.
    """
    from services.mcp.skill_format import parse_frontmatter

    skill_md = skill_root / "SKILL.md"
    if not skill_md.is_file():
        raise HTTPException(400, "SKILL.md not found in skill folder")
    fm, _body = parse_frontmatter(skill_md.read_text())
    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    if not SKILL_ID_RE.fullmatch(name or "") or len(name) > SKILL_ID_MAX_LEN:
        raise HTTPException(
            400,
            "SKILL.md frontmatter must carry a spec-valid `name` (lowercase "
            f"letters/digits/hyphens, ≤{SKILL_ID_MAX_LEN} chars); got: {name!r}",
        )
    if not description:
        raise HTTPException(
            400, "SKILL.md frontmatter must carry a `description`")

    meta = fm.get("metadata") or {}
    version = (str(meta.get("version") or "").strip()
               if isinstance(meta, dict) else "")
    manifest = {
        "name": name,
        "label": name.replace("-", " ").title(),
        "description": description,
        "version": version or "1.0.0",
        "category": "skill",
        "server": {"runtime": "none", "transport": "none"},
        "skills": [{
            "id": name,
            "loading": "on_demand",
            "file": f"skills/{name}/SKILL.md",
            "description": description,
        }],
    }
    stage = Path(tempfile.mkdtemp(prefix="skill-bare-install-"))
    try:
        pkg_root = stage / name
        target = pkg_root / "skills" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # symlinks can't survive zipfile extraction (they land as regular
        # files), and the materializer refuses them anyway — plain copy.
        await asyncio.to_thread(
            shutil.copytree, skill_root, target, symlinks=False)
        (pkg_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n")
        return await install_skill_package_from_extracted(
            pkg_root, progress_cb=progress_cb)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


async def install_skill_package_from_catalog(
    name: str, *, progress_cb: ProgressCb = None,
) -> dict:
    """Download one package from ``OtoDock/community-skills`` and install it."""
    if not _is_safe_name(name):
        raise HTTPException(400, f"Invalid skill package name: {name!r}")

    from services.community import community_catalog
    try:
        registry = await community_catalog.fetch_skills_registry()
        _entry = next(
            (e for e in registry.get("skills", []) if e.get("name") == name),
            None,
        )
    except Exception:
        _entry = None
    if _entry is not None:
        community_catalog.require_platform_compat(
            name, _entry.get("platform_min_version"),
        )

    await _emit(progress_cb, "fetch", 5, "Downloading from skills catalog")
    tarball = await _fetch_catalog_tarball(url=SKILLS_TARBALL_URL)

    tmp = Path(tempfile.mkdtemp(prefix="skill-catalog-install-"))
    try:
        pkg_root = await asyncio.to_thread(
            _extract_mcp_subfolder, tarball, name, tmp,
        )
        if pkg_root is None:
            raise HTTPException(
                404,
                f"Skill package '{name}' not found in the skills catalog "
                "tarball. The catalog may have been updated since the last "
                "list refresh.",
            )
        return await install_skill_package_from_extracted(
            pkg_root, progress_cb=progress_cb,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
