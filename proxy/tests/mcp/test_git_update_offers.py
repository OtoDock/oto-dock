"""Update offers for git-sourced MCPs, a PyPI source's extras the manifest hash
cannot see, and the catalog's trust tier.

A git+ MCP is offered like a container: a newer catalog version, a changed
manifest or ref under the same version, an install ahead noted, never moved
back. Its Update converges through the catalog install; the weekly job builds
one only when the catalog pins a full commit (``pinned_commit`` on the row,
``test_release_age.py``)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from services.community import community_catalog
from services.mcp import mcp_updater

GIT = "git+https://projects.blender.org/lab/blender_mcp.git@{ref}#subdirectory=mcp"


def _git_manifest(tmp_path: Path, version: str, ref: str, raw_hash_extra: str = "") -> NS:
    d = tmp_path / "blender"
    d.mkdir(exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "name": "blender", "version": version, "extra": raw_hash_extra,
        "server": {"runtime": "python", "source": GIT.format(ref=ref)},
    }))
    return NS(name="blender", category="community", version=version, mcp_dir=str(d),
              server=NS(runtime="python", source=GIT.format(ref=ref), image="",
                        version_constraint=""))


@pytest.fixture
def check(monkeypatch):
    """Run ``detect_available_updates`` over one installed manifest and one
    catalog entry; returns the blender row (or None)."""
    async def _run(manifest, entry):
        monkeypatch.setattr(mcp_updater.mcp_registry, "get_all_manifests",
                            lambda: {manifest.name: manifest})

        async def _registry():
            return {"mcps": [entry]}
        monkeypatch.setattr(community_catalog, "fetch_registry", _registry)

        async def _skills():
            return {"skills": []}
        monkeypatch.setattr(community_catalog, "fetch_skills_registry", _skills)

        async def _persist(results, source_changes, unchanged, registry_ok):
            return {"updates": results, "checked": 0, "checked_at": ""}
        monkeypatch.setattr(mcp_updater, "_persist_check", _persist)

        from services.community import mcp_source_swap

        async def _changes(manifests, catalog):
            return {}, set()
        monkeypatch.setattr(mcp_source_swap, "detect_changes", _changes)
        out = await mcp_updater.detect_available_updates()
        return out["updates"].get(manifest.name)
    return _run


def _hash_of(manifest: NS) -> str:
    return community_catalog.normalized_manifest_hash(
        json.loads((Path(manifest.mcp_dir) / "manifest.json").read_text()))


@pytest.mark.asyncio
async def test_a_newer_catalog_version_is_offered(tmp_path, check):
    m = _git_manifest(tmp_path, "1.0.2", "aaa")
    row = await check(m, {"name": "blender", "version": "1.0.3", "runtime": "python",
                          "source": GIT.format(ref="bbb"), "manifest_hash": _hash_of(m)})
    assert row == {"current": "1.0.2", "latest": "1.0.3", "registry": "git",
                   "package": "blender", "reason": "package",
                   "pinned_commit": False, "catalog_ref": "bbb"}


COMMIT = "2cea8d566dde07fbac28a61d698909d69724e853"


@pytest.mark.asyncio
async def test_a_row_says_whether_the_catalog_pins_a_commit(tmp_path, check):
    m = _git_manifest(tmp_path, "1.0.2", "v1.0.2")
    row = await check(m, {"name": "blender", "version": "1.0.3", "runtime": "python",
                          "source": GIT.format(ref=COMMIT), "manifest_hash": _hash_of(m)})
    assert row["pinned_commit"] is True and row["catalog_ref"] == COMMIT


@pytest.mark.asyncio
async def test_a_held_git_row_says_so(tmp_path, check):
    """The hold marker reaches a git offer like a package offer: the weekly
    job skips the MCP, and the row's note says held, not building."""
    m = _git_manifest(tmp_path, "1.0.2", "aaa")
    (Path(m.mcp_dir) / mcp_updater.HOLD_MARKER).write_text("")
    row = await check(m, {"name": "blender", "version": "1.0.3", "runtime": "python",
                          "source": GIT.format(ref=COMMIT), "manifest_hash": _hash_of(m)})
    assert row["held"] is True and row["pinned_commit"] is True


@pytest.mark.asyncio
async def test_a_ref_moved_under_the_same_version_is_an_integration_update(tmp_path, check):
    m = _git_manifest(tmp_path, "1.0.3", "aaa")
    row = await check(m, {"name": "blender", "version": "1.0.3", "runtime": "python",
                          "source": GIT.format(ref="bbb"), "manifest_hash": _hash_of(m)})
    assert row["reason"] == "manifest" and row["registry"] == "git"


@pytest.mark.asyncio
async def test_a_changed_manifest_under_the_same_version_is_offered(tmp_path, check):
    m = _git_manifest(tmp_path, "1.0.3", "aaa")
    row = await check(m, {"name": "blender", "version": "1.0.3", "runtime": "python",
                          "source": GIT.format(ref="aaa"), "manifest_hash": "something-else"})
    assert row["reason"] == "manifest"


@pytest.mark.asyncio
async def test_the_same_version_ref_and_manifest_offer_nothing(tmp_path, check):
    m = _git_manifest(tmp_path, "1.0.3", "aaa")
    assert await check(m, {"name": "blender", "version": "1.0.3", "runtime": "python",
                           "source": GIT.format(ref="aaa"), "manifest_hash": _hash_of(m)}) is None


@pytest.mark.asyncio
async def test_an_install_ahead_is_noted_never_offered(tmp_path, check):
    m = _git_manifest(tmp_path, "1.1.0", "ccc")
    row = await check(m, {"name": "blender", "version": "1.0.3", "runtime": "python",
                          "source": GIT.format(ref="bbb"), "manifest_hash": "x"})
    assert row["reason"] == "ahead"


@pytest.mark.asyncio
async def test_update_one_converges_a_git_mcp_through_the_catalog(tmp_path, monkeypatch):
    m = _git_manifest(tmp_path, "1.0.2", "aaa")
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_manifest", lambda n: m)
    calls = []

    async def _install(name, *, install_version=None, progress_cb=None):
        calls.append((name, install_version))
        return {"status": "updated", "name": name, "version": "1.0.3"}
    from services.community import community_installer
    monkeypatch.setattr(community_installer, "install_from_catalog", _install)
    monkeypatch.setattr(mcp_updater, "_forget_check_result", lambda n: calls.append(("forget", n)))
    result = await mcp_updater.update_one("blender")
    assert result["version"] == "1.0.3"
    assert calls == [("blender", None), ("forget", "blender")]


# ---------------------------------------------------------------------------
# What the hash cannot see: a ref, a PyPI source's extras
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("installed, catalog, differs", [
    (GIT.format(ref="a"), GIT.format(ref="b"), True),
    (GIT.format(ref="a"), GIT.format(ref="a"), False),
    ("pypi:x[a]@1.0", "pypi:x[a,b]", True),
    ("pypi:x[A_b]@1.0", "pypi:x[a-b]", False),
    ("pypi:x[a,b]@1.0", "pypi:x[b,a]", False),
    ("pypi:x@1.0", "pypi:x[documents]", True),
    ("pypi:x[documents]@1.0", "pypi:x", True),
    ("pypi:x@1.0", "pypi:x", False),
    ("npm:x@1.0", "npm:x", False),
    ("pypi:x@1.0", "pypi:x[-bad]", False),
])
def test_source_details(installed, catalog, differs):
    assert community_catalog.source_detail_differs(installed, catalog) is differs


@pytest.mark.asyncio
async def test_an_extras_change_is_an_integration_update(tmp_path, monkeypatch):
    d = tmp_path / "nextcloud"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"name": "nextcloud"}))
    m = NS(name="nextcloud", category="community", version="0.40.0", mcp_dir=str(d),
           server=NS(runtime="python", source="pypi:nextcloud-mcp-server@0.40.0",
                     version_constraint="", image=""))
    monkeypatch.setattr(mcp_updater.mcp_registry, "get_all_manifests", lambda: {m.name: m})

    async def _registry():
        return {"mcps": [{"name": "nextcloud", "version": "", "runtime": "python",
                          "source": "pypi:nextcloud-mcp-server[documents]"}]}
    monkeypatch.setattr(community_catalog, "fetch_registry", _registry)

    async def _skills():
        return {"skills": []}
    monkeypatch.setattr(community_catalog, "fetch_skills_registry", _skills)

    async def _persist(results, source_changes, unchanged, registry_ok):
        return {"updates": results}
    monkeypatch.setattr(mcp_updater, "_persist_check", _persist)

    async def _release(registry, package, constraint, now=None):
        assert package == "nextcloud-mcp-server"
        return mcp_updater.Release("0.40.0", "0.40.0", "")
    monkeypatch.setattr(mcp_updater, "resolve_release", _release)
    from services.community import mcp_source_swap

    async def _changes(manifests, catalog):
        return {}, set()
    monkeypatch.setattr(mcp_source_swap, "detect_changes", _changes)
    row = (await mcp_updater.detect_available_updates())["updates"]["nextcloud"]
    assert row["reason"] == "manifest" and row["latest"] == "0.40.0"


def test_browse_flags_an_extras_change_and_a_moved_ref(monkeypatch, tmp_path):
    local = {"nextcloud": NS(server=NS(source="pypi:nextcloud-mcp-server@0.40.0"),
                             mcp_dir=tmp_path / "nextcloud"),
             "blender": NS(server=NS(source=GIT.format(ref="aaa")), mcp_dir=tmp_path / "blender")}
    monkeypatch.setattr("services.mcp.mcp_registry.get_manifest", local.get)
    out = community_catalog.augment_entry(
        {"name": "nextcloud", "version": "", "runtime": "python",
         "source": "pypi:nextcloud-mcp-server[documents]", "manifest_hash": "h"},
        installed_versions={"nextcloud": "0.40.0"}, enabled_for_agents={},
        installed_manifest_hashes={"nextcloud": "h"})
    assert out["update_available"] is True
    out = community_catalog.augment_entry(
        {"name": "blender", "version": "1.0.3", "runtime": "python",
         "source": GIT.format(ref=COMMIT), "manifest_hash": "h"},
        installed_versions={"blender": "1.0.3"}, enabled_for_agents={},
        installed_manifest_hashes={"blender": "h"})
    assert out["update_available"] is True and out["catalog_unpinned"] is False
    # A moved ref the catalog does not pin to a commit is no button: the
    # install from the catalog would refuse it.
    out = community_catalog.augment_entry(
        {"name": "blender", "version": "1.0.3", "runtime": "python",
         "source": GIT.format(ref="bbb"), "manifest_hash": "h"},
        installed_versions={"blender": "1.0.3"}, enabled_for_agents={},
        installed_manifest_hashes={"blender": "h"})
    assert out["update_available"] is False and out["catalog_unpinned"] is True


# ---------------------------------------------------------------------------
# The trust tier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry, tier", [
    ({"trust": "first-party"}, "first-party"),
    ({"trust": "maintained"}, "maintained"),
    ({"trust": "third-party"}, "third-party"),
    ({}, "third-party"),
    ({"trust": "official"}, "third-party"),
    ({"trust": None}, "third-party"),
])
def test_trust_tier(entry, tier):
    assert community_catalog.trust_tier(entry) == tier


def test_browse_carries_the_tier():
    out = community_catalog.augment_entry(
        {"name": "x", "version": "", "runtime": "node", "source": "npm:x"},
        installed_versions={}, enabled_for_agents={})
    assert out["trust"] == "third-party"
    out = community_catalog.augment_entry(
        {"name": "x", "version": "", "runtime": "node", "source": "npm:x", "trust": "maintained"},
        installed_versions={}, enabled_for_agents={})
    assert out["trust"] == "maintained"
