"""Tests for bounded auto-update (#3) + converge-to-catalog detection (#2).

Covers the pure / unit-testable pieces without real npm/PyPI/GitHub:
- ``mcp_updater.resolve_latest_in_bound`` version filtering (prereleases, the npm
  ``-0`` post-release trap, exclude-all, the unbounded fast path);
- ``community_catalog.normalized_manifest_hash`` (the cross-repo hash CONTRACT,
  pinned against a non-ASCII golden vector — the catalog generator must produce
  the identical value);
- ``community_catalog.augment_entry`` manifest-change flagging (guarded so it
  never false-flags on a missing/stale hash or a docker entry).
"""

import pytest

from services.community import community_catalog
from services.mcp import mcp_updater


# Pinned golden vector — the catalog ``scripts/generate-registry.py::_manifest_hash``
# MUST produce this exact value for the same manifest (parity is the whole point).
# Includes a non-ASCII em-dash to lock ``ensure_ascii``.
GOLDEN_MANIFEST = {
    "name": "sample-mcp", "label": "Sample", "version": "",
    "description": "Pages, blocks — and search",
    "category": "community",
    "server": {
        "runtime": "node", "source": "npm:@scope/sample-mcp",
        "version_constraint": ">=2,<3",
        "args": ["${mcp_dir}/node_modules/@scope/sample-mcp/bin/cli.mjs"],
    },
}
GOLDEN_HASH = "e85230874e11d0ff"


class TestResolveLatestInBound:
    @pytest.mark.asyncio
    async def test_unbounded_uses_latest_fast_path(self, monkeypatch):
        called = {"list": False}

        async def _latest(pkg):
            return "9.9.9"

        async def _list(pkg):
            called["list"] = True
            return ["1.0.0", "9.9.9"]

        monkeypatch.setattr(mcp_updater, "_check_npm_version", _latest)
        monkeypatch.setattr(mcp_updater, "_all_npm_versions", _list)
        assert await mcp_updater.resolve_latest_in_bound("npm", "x", "") == "9.9.9"
        assert called["list"] is False  # no packument fetch when unbounded

    @pytest.mark.asyncio
    async def test_npm_bound_skips_prereleases_and_post_trap(self, monkeypatch):
        async def _list(pkg):
            # 2.0.0-0 parses to 2.0.0.post0 (NOT a prerelease, ranks ABOVE 2.0.0);
            # the npm "-" rule must drop it. 2.0.0-rc.1 is a normal prerelease.
            return ["2.0.0", "2.0.0-0", "2.0.0-rc.1", "2.4.1", "3.0.0"]

        monkeypatch.setattr(mcp_updater, "_all_npm_versions", _list)
        assert await mcp_updater.resolve_latest_in_bound("npm", "x", ">=2,<3") == "2.4.1"

    @pytest.mark.asyncio
    async def test_pypi_bound_picks_max_in_range(self, monkeypatch):
        async def _list(pkg):
            return ["6.6.1", "7.8.1", "7.9.0", "8.0.0"]

        monkeypatch.setattr(mcp_updater, "_all_pypi_versions", _list)
        assert await mcp_updater.resolve_latest_in_bound("pypi", "x", ">=7,<8") == "7.9.0"

    @pytest.mark.asyncio
    async def test_bound_excludes_all_returns_none(self, monkeypatch):
        async def _list(pkg):
            return ["2.0.0", "3.0.0"]

        monkeypatch.setattr(mcp_updater, "_all_npm_versions", _list)
        assert await mcp_updater.resolve_latest_in_bound("npm", "x", "<1") is None

    @pytest.mark.asyncio
    async def test_bound_only_prereleases_returns_none(self, monkeypatch):
        async def _list(pkg):
            return ["2.0.0-rc.1", "2.0.0-rc.2"]

        monkeypatch.setattr(mcp_updater, "_all_npm_versions", _list)
        assert await mcp_updater.resolve_latest_in_bound("npm", "x", ">=2,<3") is None


class TestIsPkgNewer:
    def test_forward_only(self):
        assert mcp_updater._is_pkg_newer("2.1.0", "2.0.0") is True
        assert mcp_updater._is_pkg_newer("2.0.0", "2.0.0") is False
        assert mcp_updater._is_pkg_newer("1.9.0", "2.0.0") is False  # no downgrade

    def test_empty_current_flags(self):
        # An empty/unparseable installed version forces a re-pin.
        assert mcp_updater._is_pkg_newer("2.0.0", "") is True

    def test_none_target_never_flags(self):
        assert mcp_updater._is_pkg_newer(None, "2.0.0") is False


class TestIsDowngrade:
    def test_flags_backwards_only(self):
        assert mcp_updater._is_downgrade("0.0.69", "0.0.70") is True
        assert mcp_updater._is_downgrade("0.0.70", "0.0.70") is False
        assert mcp_updater._is_downgrade("0.0.71", "0.0.70") is False

    def test_unparseable_never_flags(self):
        # The flag drives loud warnings + hold semantics — parse-only, so an
        # odd tag never false-claims a downgrade.
        assert mcp_updater._is_downgrade("abc", "0.0.70") is False
        assert mcp_updater._is_downgrade("0.0.69", "xyz") is False

    def test_empty_never_flags(self):
        assert mcp_updater._is_downgrade(None, "1.0") is False
        assert mcp_updater._is_downgrade("1.0", "") is False


class TestIsHeld:
    def _manifest(self, mcp_dir):
        return type("M", (), {"mcp_dir": mcp_dir})()

    def test_marker_present(self, tmp_path):
        (tmp_path / mcp_updater.HOLD_MARKER).touch()
        assert mcp_updater.is_held(self._manifest(tmp_path)) is True

    def test_no_marker(self, tmp_path):
        assert mcp_updater.is_held(self._manifest(tmp_path)) is False

    def test_no_mcp_dir(self):
        assert mcp_updater.is_held(object()) is False
        assert mcp_updater.is_held(self._manifest(None)) is False


class TestNormalizedManifestHash:
    def test_golden_vector(self):
        assert community_catalog.normalized_manifest_hash(GOLDEN_MANIFEST) == GOLDEN_HASH

    def test_catalog_equals_pinned_install(self):
        import copy
        installed = copy.deepcopy(GOLDEN_MANIFEST)
        installed["version"] = "2.4.0"
        installed["server"]["source"] = "npm:@scope/sample-mcp@2.4.0"
        assert (
            community_catalog.normalized_manifest_hash(installed)
            == community_catalog.normalized_manifest_hash(GOLDEN_MANIFEST)
        )

    def test_integration_change_flips(self):
        import copy
        changed = copy.deepcopy(GOLDEN_MANIFEST)
        changed["server"]["args"] = ["${mcp_dir}/node_modules/@scope/sample-mcp/dist/cli.js"]
        assert (
            community_catalog.normalized_manifest_hash(changed)
            != community_catalog.normalized_manifest_hash(GOLDEN_MANIFEST)
        )

    def test_does_not_mutate_caller(self):
        community_catalog.normalized_manifest_hash(GOLDEN_MANIFEST)
        assert GOLDEN_MANIFEST["server"]["source"] == "npm:@scope/sample-mcp"
        assert GOLDEN_MANIFEST["version"] == ""

    def test_tolerates_absent_version_and_source(self):
        # Must not KeyError on a manifest missing the pinned fields.
        h = community_catalog.normalized_manifest_hash({"name": "x", "server": {"runtime": "node"}})
        assert len(h) == 16


class TestAugmentManifestChange:
    def _entry(self, **over):
        e = {"name": "notion-mcp", "runtime": "node", "version": "", "manifest_hash": "aaaa"}
        e.update(over)
        return e

    def test_flags_when_manifest_hash_differs(self):
        out = community_catalog.augment_entry(
            self._entry(), installed_versions={"notion-mcp": "2.4.0"},
            enabled_for_agents={}, installed_manifest_hashes={"notion-mcp": "bbbb"},
        )
        assert out["update_available"] is True

    def test_no_flag_when_hash_equal(self):
        out = community_catalog.augment_entry(
            self._entry(), installed_versions={"notion-mcp": "2.4.0"},
            enabled_for_agents={}, installed_manifest_hashes={"notion-mcp": "aaaa"},
        )
        assert out["update_available"] is False

    def test_no_flag_when_catalog_hash_missing(self):
        # Stale registry.json without manifest_hash must not false-flag.
        out = community_catalog.augment_entry(
            self._entry(manifest_hash=None), installed_versions={"notion-mcp": "2.4.0"},
            enabled_for_agents={}, installed_manifest_hashes={"notion-mcp": "bbbb"},
        )
        assert out["update_available"] is False

    def test_no_flag_without_installed_hashes(self):
        # Back-compat: callers that don't pass installed_manifest_hashes (existing
        # marketplace tests) never trip the manifest-change branch.
        out = community_catalog.augment_entry(
            self._entry(), installed_versions={"notion-mcp": "2.4.0"},
            enabled_for_agents={},
        )
        assert out["update_available"] is False

    def test_docker_flagged_via_manifest_hash_under_the_same_tag(self):
        # A docker catalog edit under the same image tag is an integration
        # update too (the converge re-fetches the folder); the version signal
        # still wins when the tag moved.
        out = community_catalog.augment_entry(
            self._entry(runtime="docker", version="1.0.0", source="docker:notion"),
            installed_versions={"notion-mcp": "1.0.0"},
            enabled_for_agents={}, installed_manifest_hashes={"notion-mcp": "bbbb"},
        )
        assert out["update_available"] is True
        out = community_catalog.augment_entry(
            self._entry(runtime="docker", version="1.0.0", source="docker:notion"),
            installed_versions={"notion-mcp": "1.0.0"},
            enabled_for_agents={}, installed_manifest_hashes={"notion-mcp": "aaaa"},
        )
        assert out["update_available"] is False

    def test_git_and_remote_sources_never_flag_via_manifest_hash(self):
        # No converge path for these: a hash mismatch would badge an update
        # nobody can run.
        for source in ("git+https://example.com/x.git@v1", "remote:mcp.example.com"):
            out = community_catalog.augment_entry(
                self._entry(runtime="python", version="1.0.0", source=source),
                installed_versions={"notion-mcp": "1.0.0"},
                enabled_for_agents={}, installed_manifest_hashes={"notion-mcp": "bbbb"},
            )
            assert out["update_available"] is False, source


class TestDetectionScopedToCommunity:
    """detect_available_updates must only offer what update_one can execute:
    npm/pypi + docker converges go through the COMMUNITY catalog, so custom/
    local MCPs (which ship with the platform and update via platform releases)
    must never be offered — the browser-control wrapper tracked its wrapped
    @playwright/mcp upstream and showed a dashboard update that 404'd on click
    (observed 2026-07-09 on the internal install)."""

    def _manifests(self, tmp_path):
        from types import SimpleNamespace as NS
        return {
            "browser-control": NS(  # custom npm wrapper — must be skipped
                category="custom", version="0.0.76",
                mcp_dir=str(tmp_path / "browser"),
                server=NS(runtime="node", source="npm:@playwright/mcp@0.0.76",
                          version_constraint=""),
            ),
            "notion-mcp": NS(  # community npm — still offered
                category="community", version="1.0.0",
                mcp_dir=str(tmp_path / "notion"),
                server=NS(runtime="node", source="npm:@notionhq/notion-mcp-server",
                          version_constraint=""),
            ),
            "file-tools": NS(  # custom docker — must be skipped even if a
                category="custom", version="0.1.0",  # catalog name collides
                mcp_dir=str(tmp_path / "ft"),
                server=NS(runtime="docker", source="docker:file-tools",
                          version_constraint=""),
            ),
            "camoufox": NS(  # community docker — catalog-version converge
                category="community", version="0.0.70",
                mcp_dir=str(tmp_path / "cam"),
                server=NS(runtime="docker", source="docker:camoufox",
                          version_constraint=""),
            ),
        }

    @pytest.mark.asyncio
    async def test_custom_mcps_never_offered(self, tmp_path, monkeypatch):
        manifests = self._manifests(tmp_path)
        monkeypatch.setattr(
            mcp_updater.mcp_registry, "get_all_manifests", lambda: manifests)

        async def _fake_registry():
            return {"mcps": [
                {"name": "notion-mcp", "version": "", "version_constraint": ""},
                {"name": "camoufox", "version": "0.0.72"},
                # Adversarial: catalog entry colliding with the CUSTOM name.
                {"name": "file-tools", "version": "9.9.9"},
            ]}
        monkeypatch.setattr(community_catalog, "fetch_registry", _fake_registry)

        async def _fake_latest(registry, package, constraint):
            return "2.0.0"  # newer than every installed npm version
        monkeypatch.setattr(mcp_updater, "resolve_latest_in_bound", _fake_latest)

        out = await mcp_updater.detect_available_updates()
        updates = out["updates"]

        assert "browser-control" not in updates   # custom npm: no phantom offer
        assert "file-tools" not in updates        # custom docker: collision-proof
        assert updates["notion-mcp"]["latest"] == "2.0.0"
        assert updates["camoufox"]["latest"] == "0.0.72"
        # Only the community npm candidate was even probed.
        assert out["checked"] == 2  # notion (npm) + camoufox (docker)
