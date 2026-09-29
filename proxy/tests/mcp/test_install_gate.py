"""The community install gate (``install_from_extracted_folder``): the one
path behind the admin zip upload, the catalog install and both updaters.

Pins the supply-chain rules: a skills[].file must be a
regular file inside the MCP folder before anything is copied; an update
never changes the installed MCP's source identity; an incoming folder
carries no repository, dependency tree or package-manager configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import config
from services.community import community_installer as ci
from services.mcp import mcp_registry


def _context_only(name: str = "weather-tools", skills: list | None = None) -> dict:
    return {
        "name": name, "label": "Weather tools",
        "description": "Forecast helpers (no code).", "version": "1.0.0",
        "category": "community",
        "server": {"runtime": "none", "transport": "none"},
        "skills": skills if skills is not None else [
            {"id": "weather-usage", "file": "skills/weather-usage/SKILL.md"},
        ],
    }


def _folder(tmp_path: Path, manifest: dict, *, files: dict[str, str] | None = None) -> Path:
    src = tmp_path / "upload" / manifest["name"]
    src.mkdir(parents=True)
    (src / "README.md").write_text("Weather helpers.\n")
    (src / "manifest.json").write_text(json.dumps(manifest))
    for rel, body in (files or {
        "skills/weather-usage/SKILL.md": "---\nname: weather-usage\ndescription: d\n---\n\nBody.\n",
    }).items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return src


@pytest.fixture
def mcps_dir(temp_db, tmp_path, monkeypatch):
    root = tmp_path / "opt" / "otodock" / "mcps"
    (root / "community").mkdir(parents=True)
    monkeypatch.setattr(config, "MCPS_DIR", root)
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    return root


async def _install(src: Path) -> dict:
    return await ci.install_from_extracted_folder(src)


# ── skills[].file at the gate ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_confined_skill_file_installs(mcps_dir, tmp_path):
    result = await _install(_folder(tmp_path, _context_only()))
    assert result["status"] == "installed"
    m = mcp_registry.get_manifest("weather-tools")
    assert [s.file for s in m.skills] == ["skills/weather-usage/SKILL.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "/opt/otodock/config.env", "/proc/self/environ",
    "../../../agents/x/knowledge/.credentials/gmail/token.json",
    "skills/../../../../etc/passwd",
])
async def test_an_unconfined_skill_file_is_refused_before_any_file_lands(mcps_dir, tmp_path, bad):
    src = _folder(tmp_path, _context_only(skills=[{"id": "weather-usage", "file": bad}]))
    with pytest.raises(HTTPException) as ei:
        await _install(src)
    assert ei.value.status_code == 400
    assert "relative path inside the MCP folder" in ei.value.detail
    assert not (mcps_dir / "community" / "weather-tools").exists()
    assert mcp_registry.get_manifest("weather-tools") is None


@pytest.mark.asyncio
async def test_a_skill_file_that_is_missing_or_a_link_out_is_refused(mcps_dir, tmp_path):
    src = _folder(tmp_path, _context_only(skills=[
        {"id": "weather-usage", "file": "skills/weather-usage/SKILL.md"},
        {"id": "weather-units", "file": "skills/units/SKILL.md"},
    ]))
    (src / "skills" / "units").mkdir(parents=True)
    (src / "skills" / "units" / "SKILL.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("outside\n")
    with pytest.raises(HTTPException) as ei:
        await _install(src)
    assert ei.value.status_code == 400
    assert "weather-units" in ei.value.detail
    assert not (mcps_dir / "community" / "weather-tools").exists()


@pytest.mark.asyncio
async def test_a_malformed_skills_block_is_a_400_not_a_500(mcps_dir, tmp_path):
    src = _folder(tmp_path, _context_only(skills=[{"id": "weather-usage"}]))
    with pytest.raises(HTTPException) as ei:
        await _install(src)
    assert ei.value.status_code == 400


# ── the source identity guard ────────────────────────────────────

@pytest.fixture
def fake_package_install(monkeypatch):
    """The package step (npm / pip) replaced by a recorder: the gate's own
    rules are under test, not the package managers."""
    calls: list[tuple[str, str]] = []

    async def _install(mcp_dir, runtime, source, **kw):
        calls.append((runtime, source))
        return ci.mcp_installer.InstallResult(
            ok=True, log="", version_hash="h", resolved_version="1.0.0",
        )

    monkeypatch.setattr(ci.mcp_installer, "install_mcp", _install)
    return calls


def _packaged(source: str, *, runtime: str = "node", name: str = "pkg-mcp") -> dict:
    return {
        "name": name, "label": "Pkg", "description": "d", "version": "",
        "category": "community",
        "server": {"runtime": runtime, "transport": "stdio", "command": "x",
                   "source": source},
        "skills": [],
    }


def _no_skills(manifest: dict) -> dict:
    return {"README.md": "Pkg.\n"} if manifest.get("skills") == [] else {}


async def _install_packaged(tmp_path, manifest, tag):
    src = tmp_path / "upload" / f"{manifest['name']}-{tag}"
    src.mkdir(parents=True)
    (src / "README.md").write_text(f"Pkg {tag}.\n")
    (src / "manifest.json").write_text(json.dumps(manifest))
    return await ci.install_from_extracted_folder(src)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime, first, second", [
    ("node", "npm:pkg", "npm:pkg@1.2.0"),
    ("node", "npm:@scope/pkg", "npm:@scope/pkg@2.0.0"),
    ("python", "pypi:Pkg_Name", "pypi:pkg-name"),
    ("python", "git+https://host/r.git@v1#subdirectory=mcp",
     "git+https://HOST/r@v2#subdirectory=mcp"),
])
async def test_the_same_source_identity_updates(mcps_dir, tmp_path, fake_package_install,
                                                runtime, first, second):
    assert (await _install_packaged(tmp_path, _packaged(first, runtime=runtime), "a"))["status"] == "installed"
    result = await _install_packaged(tmp_path, _packaged(second, runtime=runtime), "b")
    assert result["status"] == "updated"
    assert (mcps_dir / "community" / "pkg-mcp" / "README.md").read_text() == "Pkg b.\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime, first, second", [
    ("node", "npm:pkg", "npm:other"),
    ("node", "npm:pkg", "pypi:pkg"),
    ("python", "pypi:pkg", "git+https://host/r.git@v1"),
    ("python", "git+https://host/r.git#subdirectory=a", "git+https://host/r.git#subdirectory=b"),
    ("python", "git+https://host/r.git", "git+https://other/r.git"),
])
async def test_a_changed_source_identity_is_refused_before_any_file_moves(
        mcps_dir, tmp_path, fake_package_install, runtime, first, second):
    await _install_packaged(tmp_path, _packaged(first, runtime=runtime), "a")
    target = mcps_dir / "community" / "pkg-mcp"
    before = sorted(p.relative_to(target) for p in target.rglob("*"))
    calls_before = len(fake_package_install)
    with pytest.raises(HTTPException) as ei:
        await _install_packaged(tmp_path, _packaged(second, runtime=runtime), "b")
    assert ei.value.status_code == 409
    assert "Update refused" in ei.value.detail and "Uninstall it" in ei.value.detail
    assert (target / "README.md").read_text() == "Pkg a.\n"
    assert sorted(p.relative_to(target) for p in target.rglob("*")) == before
    assert not target.with_suffix(".bak").exists()
    assert len(fake_package_install) == calls_before
    assert mcp_registry.get_manifest("pkg-mcp").server.source.startswith(first.split("@")[0][:8])


@pytest.mark.asyncio
async def test_remote_identity_is_the_url_template_host(mcps_dir, tmp_path):
    def _remote(source, url):
        return {
            "name": "linear-mcp", "label": "Linear", "description": "d",
            "version": "1.0.0", "category": "community",
            "server": {"transport": "streamable_http", "url_template": url,
                       "source": source},
        }
    await _install_packaged(tmp_path, _remote("remote:mcp.linear.app", "https://mcp.linear.app/mcp"), "a")
    # A relabelled source with the same host is the same integration.
    r = await _install_packaged(tmp_path, _remote("remote:linear (hosted)", "https://MCP.linear.app/mcp"), "b")
    assert r["status"] == "updated"
    with pytest.raises(HTTPException) as ei:
        await _install_packaged(tmp_path, _remote("remote:mcp.linear.app", "https://relay.example/mcp"), "c")
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_a_context_only_mcp_gaining_a_package_source_is_refused(
        mcps_dir, tmp_path, fake_package_install):
    await _install(_folder(tmp_path, _context_only(name="pkg-mcp", skills=[])))
    with pytest.raises(HTTPException) as ei:
        await _install_packaged(tmp_path, _packaged("pypi:pkg", runtime="python"), "b")
    assert ei.value.status_code == 409
    assert fake_package_install == []


@pytest.mark.asyncio
async def test_source_build_reaches_the_installer(mcps_dir, tmp_path, monkeypatch):
    """The packages a catalog entry allows to build from source travel
    from the manifest to the install step; nothing else does."""
    seen: list[dict] = []

    async def _install(mcp_dir, runtime, source, **kw):
        seen.append(kw)
        return ci.mcp_installer.InstallResult(ok=True, log="", version_hash="h", resolved_version="1.0.0")

    monkeypatch.setattr(ci.mcp_installer, "install_mcp", _install)
    manifest = _packaged("pypi:unifi-network-mcp", runtime="python")
    manifest["server"]["source_build"] = ["antlr4-python3-runtime"]
    await _install_packaged(tmp_path, manifest, "a")
    assert seen[0]["source_build"] == ["antlr4-python3-runtime"]
    assert mcp_registry.get_manifest("pkg-mcp").server.source_build == ["antlr4-python3-runtime"]


def test_container_identity_comes_from_the_image_not_the_label():
    ident = ci.source_identity
    same = ident(runtime="docker", source="docker:camoufox + @playwright/mcp@0.0.68",
                 image="ghcr.io/otodock/camoufox:0.0.75", url_template="")
    bumped = ident(runtime="docker", source="docker:camoufox + @playwright/mcp@0.0.70",
                   image="ghcr.io/otodock/camoufox:0.0.80", url_template="")
    digest = ident(runtime="docker", source="docker:camoufox",
                   image="GHCR.io/otodock/camoufox@sha256:abcd", url_template="")
    assert same == bumped == digest == ("image", "ghcr.io/otodock/camoufox")
    assert ident(runtime="docker", source="docker:camoufox",
                 image="ghcr.io/otodock/other:0.0.75", url_template="") != same
    assert ident(runtime="docker", source="docker:x",
                 image="registry.local:5000/otodock/camoufox:1", url_template="") == \
        ("image", "registry.local:5000/otodock/camoufox")
    # T1 build-form (no image) is its own identity, not the pulled one.
    assert ident(runtime="docker", source="docker:camoufox", image="", url_template="") == ("image", "")


def test_identity_never_raises_on_odd_values():
    ident = ci.source_identity
    assert ident(runtime="node", source=42, image="", url_template="")[0] == "unknown"
    assert ident(runtime="python", source="pypi:pkg[extra]", image="", url_template="")[0] == "unknown"
    assert ident(runtime="none", source="", image="", url_template="") == ("none", "")
    assert ident(runtime=None, source=None, image=None, url_template=None)[0] in ("unknown", "none")


@pytest.mark.asyncio
async def test_a_name_shipped_with_the_platform_is_refused(mcps_dir, tmp_path):
    shipped = mcp_registry.McpManifest(
        name="weather-tools", label="w", description="", version="1", category="custom",
        server=mcp_registry.ServerConfig(runtime="none", transport="none"),
        credentials=mcp_registry.CredentialConfig(type="none"),
        config=[], env={}, agent_env={}, exclude_from=[], skills=[],
        mcp_dir=mcps_dir / "custom" / "weather-tools",
    )
    mcp_registry._manifests["weather-tools"] = shipped
    with pytest.raises(HTTPException) as ei:
        await _install(_folder(tmp_path, _context_only()))
    assert ei.value.status_code == 409
    assert not (mcps_dir / "community" / "weather-tools").exists()


# ── a clean install folder ───────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [".git", "node_modules", "venv", "lib/.git", "lib/node_modules"])
async def test_a_shipped_repository_or_dependency_tree_is_refused(mcps_dir, tmp_path, entry):
    src = _folder(tmp_path, _context_only())
    (src / entry).mkdir(parents=True)
    (src / entry / "x").write_text("x")
    with pytest.raises(HTTPException) as ei:
        await _install(src)
    assert ei.value.status_code == 400
    assert entry.rsplit("/", 1)[-1] in ei.value.detail
    assert not (mcps_dir / "community" / "weather-tools").exists()


@pytest.mark.asyncio
async def test_a_shipped_dot_git_file_is_refused(mcps_dir, tmp_path):
    src = _folder(tmp_path, _context_only())
    (src / ".git").write_text("gitdir: ../elsewhere\n")
    with pytest.raises(HTTPException) as ei:
        await _install(src)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_shipped_lock_and_package_manager_config_files_are_dropped(mcps_dir, tmp_path):
    dropped = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
               "pnpm-lock.yaml", ".npmrc", "uv.toml", "pip.conf")
    src = _folder(tmp_path, _context_only())
    for f in dropped:
        (src / f).write_text("x\n")
    assert (await _install(src))["status"] == "installed"
    target = mcps_dir / "community" / "weather-tools"
    assert (target / "README.md").is_file()
    assert not any((target / f).exists() for f in dropped)


@pytest.mark.asyncio
async def test_patches_are_catalog_owned_not_preserved(mcps_dir, tmp_path):
    assert "patches" not in ci._PRESERVE_DIRS
    src_a = _folder(tmp_path, _context_only())
    (src_a / "patches").mkdir()
    (src_a / "patches" / "old.patch").write_text("--- a\n+++ b\n")
    await _install(src_a)
    target = mcps_dir / "community" / "weather-tools"
    assert (target / "patches" / "old.patch").is_file()
    src_b = tmp_path / "upload-b" / "weather-tools"
    src_b.mkdir(parents=True)
    for p in ("README.md", "manifest.json", "skills/weather-usage/SKILL.md"):
        (src_b / p).parent.mkdir(parents=True, exist_ok=True)
        (src_b / p).write_text((src_a / p).read_text())
    assert (await ci.install_from_extracted_folder(src_b))["status"] == "updated"
    assert not (target / "patches").exists()
