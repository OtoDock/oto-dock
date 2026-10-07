"""A manifest whose MCP server names its own authorization server
(``credentials.oauth.authorization_server``): what the parser, the credential
schema, the provider registry and the installer's manifest validation make
of it, and that every shipped manifest still scans.
"""

from __future__ import annotations

import json

import pytest

from services.community import community_installer
from services.mcp import mcp_manifest_parse, mcp_registry
from tests._paths import REPO_ROOT


def _manifest_data(name="notion-hosted-mcp", provider_id="notion-hosted", block=True):
    oauth = {
        "provider_id": provider_id,
        "flows": ["authorization_code_pkce"],
        "bearer_required": True,
        "proposed_hosts": ["mcp.notion.com"],
        "services": [{"key": "default", "label": "Workspace", "description": "d",
                      "scopes": ["default"]}],
    }
    if block:
        oauth["authorization_server"] = {
            "registration": "dynamic", "confidential": False,
            "identity": {"label_field": "workspace_id", "display_field": "email_domain"},
        }
    else:
        oauth["authorization_url"] = "https://api.notion.com/v1/oauth/authorize"
        oauth["token_url"] = "https://api.notion.com/v1/oauth/token"
        oauth["app_credential"] = "notion-oauth-app"
    return {
        "name": name, "label": "Notion (hosted)", "description": "d", "version": "1.0.0",
        "category": "community",
        "server": {"transport": "streamable_http", "url_template": "https://mcp.notion.com/mcp",
                   "source": "remote:mcp.notion.com"},
        "credentials": {"type": "per_user", "label": "Notion Account", "oauth": oauth},
    }


def _write(tmp_path, data):
    folder = tmp_path / data["name"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(data))
    return folder / "manifest.json"


@pytest.fixture
def registry(monkeypatch):
    """A registry of exactly the manifests a test installs."""
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    from auth import oauth_providers
    oauth_providers.clear_manifest_cache()
    yield mcp_registry._manifests
    oauth_providers.clear_manifest_cache()


def _install(registry, tmp_path, data):
    m = mcp_manifest_parse._parse_manifest(_write(tmp_path, data))
    assert m is not None
    registry[m.name] = m
    return m


class TestParseAndSchema:
    def test_the_block_parses_and_reaches_the_schema(self, registry, tmp_path):
        _install(registry, tmp_path, _manifest_data())
        schema = mcp_registry.get_credential_schema("notion-hosted-mcp")
        meta = schema["oauth_meta"]["authorization_server"]
        assert meta == {
            "registration": "dynamic", "issuer": "", "resource_host": "mcp.notion.com",
            "confidential": False, "accepts_app_tokens": False,
        }
        assert schema["oauth_meta"]["flows"] == ["authorization_code_pkce"]

    def test_a_manifest_without_the_block_has_no_meta(self, registry, tmp_path):
        _install(registry, tmp_path, _manifest_data(block=False))
        schema = mcp_registry.get_credential_schema("notion-hosted-mcp")
        assert "authorization_server" not in schema["oauth_meta"]

    def test_a_broken_block_is_refused_at_parse(self, tmp_path):
        data = _manifest_data()
        data["credentials"]["oauth"]["flows"] = ["authorization_code"]
        with pytest.raises(ValueError, match="authorization_code_pkce"):
            mcp_manifest_parse._parse_manifest(_write(tmp_path, data))


class TestProviderRegistry:
    def test_a_provider_is_built_without_urls(self, registry, tmp_path):
        """The flow discovers the endpoints; the provider carries the identity
        settings and nothing URL-bound."""
        _install(registry, tmp_path, _manifest_data())
        from auth.oauth_providers import get_provider
        p = get_provider("notion-hosted")
        assert p.provider_id == "notion-hosted"
        assert p.authorization_url == "" and p.token_url == ""

    def test_hardcoded_ids_are_named(self):
        from auth.oauth_providers import hardcoded_provider_ids
        assert {"google", "slack", "microsoft", "zoom", "facebook"} <= hardcoded_provider_ids()


class TestOneProviderOneMechanism:
    def test_conflict_with_an_installed_app_provider(self, registry, tmp_path):
        _install(registry, tmp_path, _manifest_data(name="notion-mcp", provider_id="notion", block=False))
        incoming = _manifest_data(name="notion-hosted-mcp", provider_id="notion")
        reason = mcp_registry.authorization_server_conflict(
            "notion-hosted-mcp", incoming["credentials"]["oauth"],
        )
        assert "used by the installed MCP 'notion-mcp'" in reason
        assert "admin's OAuth app" in reason
        errors = community_installer._validate_manifest(incoming)
        assert any("one provider id issues its tokens one way" in e for e in errors)

    def test_conflict_the_other_way_round(self, registry, tmp_path):
        _install(registry, tmp_path, _manifest_data(name="notion-hosted-mcp", provider_id="notion"))
        incoming = _manifest_data(name="notion-mcp", provider_id="notion", block=False)
        reason = mcp_registry.authorization_server_conflict("notion-mcp", incoming["credentials"]["oauth"])
        assert "names its own authorization server" in reason

    def test_no_conflict_for_the_same_name_or_mechanism(self, registry, tmp_path):
        _install(registry, tmp_path, _manifest_data())
        same = _manifest_data()
        assert mcp_registry.authorization_server_conflict("notion-hosted-mcp", same["credentials"]["oauth"]) == ""
        twin = _manifest_data(name="notion-hosted-2")
        assert mcp_registry.authorization_server_conflict("notion-hosted-2", twin["credentials"]["oauth"]) == ""
        assert community_installer._validate_manifest(twin) == []


class TestShippedManifestsStillScan:
    @pytest.mark.parametrize("tree", [
        REPO_ROOT / "mcps" / "community",
        REPO_ROOT / "mcps" / "custom",
        REPO_ROOT.parent / "otodock-community-mcps-prep",
    ])
    def test_every_manifest_parses(self, tree):
        """The host rule applies to manifests with the block only: the
        templated hosts of github-mcp and m365-mcp keep parsing."""
        if not tree.is_dir():
            pytest.skip(f"{tree} is not in this checkout")
        paths = sorted(p for p in tree.glob("*/manifest.json"))
        if not paths:
            pytest.skip(f"no manifests under {tree}")
        for path in paths:
            assert mcp_manifest_parse._parse_manifest(path) is not None, path


class TestPackageCheck:
    def test_the_authoring_check_accepts_a_package_with_the_block(self, tmp_path):
        """``validate_mcp_package`` runs ``community_installer.check_package``;
        a hosted-server package with the declaration is valid."""
        root = tmp_path / "notion-hosted-mcp"
        root.mkdir()
        data = _manifest_data()
        data["author_url"] = "https://developers.notion.com/guides/mcp/overview"
        (root / "manifest.json").write_text(json.dumps(data))
        (root / "README.md").write_text("# Notion (hosted)\n")
        out = community_installer.check_package(root)
        assert out["ok"] is True, out["errors"]
        assert out["summary"]["runtime"] == "remote" and out["summary"]["host"] == "mcp.notion.com"

    def test_the_authoring_check_names_a_broken_block(self, tmp_path):
        root = tmp_path / "notion-hosted-mcp"
        root.mkdir()
        data = _manifest_data()
        data["credentials"]["oauth"]["env_injection"] = ["NOTION_TOKEN"]
        (root / "manifest.json").write_text(json.dumps(data))
        out = community_installer.check_package(root)
        assert out["ok"] is False
        assert any("env_injection cannot be declared beside authorization_server" in e for e in out["errors"])
