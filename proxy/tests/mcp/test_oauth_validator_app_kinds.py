"""The ``credentials.oauth.app_kinds`` block (storage/identity/
oauth_app_kinds.py): the validator's rules, the parse into the credential
schema and the admin list, the engine reading the active kind's admin
fields, and ``app`` reserved as a webhook provider id.
"""

from __future__ import annotations

import copy
import json

import pytest

from services.mcp import mcp_manifest_parse, mcp_registry
from services.mcp.mcp_validate_oauth import _validate_oauth_services
from services.oauth import oauth_engine
from storage.identity import oauth_app_kinds as kinds
from tests.auth.test_github_app import FIELDS, OAUTH_BLOCK, OAUTH_FIELDS

MCP = "github-kinds-mcp"


def _oauth() -> dict:
    return copy.deepcopy({
        **OAUTH_BLOCK,
        "flows": ["authorization_code", "personal_access_token"],
        "authorization_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "bearer_required": True,
        "proposed_hosts": ["api.githubcopilot.com"],
        "services": [{"key": "repos", "label": "Repositories", "description": "d", "scopes": ["repo"]}],
    })


def _manifest_data(oauth: dict | None = None) -> dict:
    return {
        "name": MCP, "label": "GitHub", "description": "d", "version": "2.1.0",
        "category": "community",
        "server": {"transport": "streamable_http", "url_template": "https://api.githubcopilot.com/mcp/",
                   "source": "remote:api.githubcopilot.com"},
        "credentials": {"type": "per_user", "label": "GitHub Account", "service_account": True,
                        "oauth": oauth or _oauth()},
    }


def _valid(oauth: dict) -> None:
    _validate_oauth_services(oauth, MCP, {"transport": "streamable_http"})


class TestValidator:
    def test_the_block_passes(self):
        _valid(_oauth())

    @pytest.mark.parametrize("change, match", [
        (lambda o: o["app_kinds"].__setitem__("key", "bad key"), "POSIX"),
        (lambda o: o["app_kinds"].__setitem__("kinds", []), "non-empty list"),
        (lambda o: o["app_kinds"]["kinds"][1].__setitem__("kind", "slack_app"), "not one of"),
        (lambda o: o["app_kinds"]["kinds"][1].__setitem__("kind", kinds.KIND_OAUTH_APP), "duplicate"),
        (lambda o: o["app_kinds"]["kinds"][1].__setitem__("label", ""), "label"),
        (lambda o: o["app_kinds"]["kinds"][1].__setitem__("mcp_token", "agent"), "mcp_token"),
        (lambda o: o["app_kinds"]["kinds"][1]["fields"][3].__setitem__("input_type", "textarea"), "input_type"),
        (lambda o: o["app_kinds"]["kinds"][1]["fields"].append({"key": "GITHUB_APP_ID", "label": "x"}), "duplicate"),
        (lambda o: o["app_kinds"]["kinds"][1]["fields"].append({"key": "GITHUB_APP_KIND", "label": "x"}), "kind key"),
        (lambda o: o["app_kinds"]["kinds"][1]["fields"].pop(3), "private_key_pem"),
        (lambda o: o["app_kinds"]["kinds"][0].__setitem__("fields", OAUTH_FIELDS[:1]), "equal app_credential_fields"),
    ])
    def test_the_rules(self, change, match):
        oauth = _oauth()
        change(oauth)
        with pytest.raises(ValueError, match=match):
            _valid(oauth)

    def test_the_webhook_secret_is_optional(self):
        oauth = _oauth()
        oauth["app_kinds"]["kinds"][1]["fields"].pop(4)
        _valid(oauth)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    from auth import oauth_providers
    oauth_providers.clear_manifest_cache()
    yield mcp_registry._manifests
    oauth_providers.clear_manifest_cache()


def _install(registry, tmp_path, data):
    folder = tmp_path / data["name"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(data))
    m = mcp_manifest_parse._parse_manifest(folder / "manifest.json")
    assert m is not None
    registry[m.name] = m
    return m


class TestParseAndSchema:
    def test_the_block_reaches_the_schema(self, registry, tmp_path):
        m = _install(registry, tmp_path, _manifest_data())
        assert m.credentials.app_kinds == _oauth()["app_kinds"]
        assert m.credentials.app_credential_fields == OAUTH_FIELDS
        schema = mcp_registry.get_credential_schema(MCP)
        assert schema["app_credential"] == "github-oauth-app"
        assert schema["app_credential_fields"] == OAUTH_FIELDS
        assert schema["app_kinds"] == _oauth()["app_kinds"]

    def test_a_manifest_without_kinds_has_none(self, registry, tmp_path):
        oauth = _oauth()
        del oauth["app_kinds"]
        m = _install(registry, tmp_path, _manifest_data(oauth))
        assert m.credentials.app_kinds is None
        assert "app_kinds" not in mcp_registry.get_credential_schema(MCP)


class TestEngine:
    def test_the_active_kinds_fields_are_read(self):
        creds = {"GITHUB_CLIENT_ID": "oauth-cid", "GITHUB_CLIENT_SECRET": "oauth-sec",
                 "GITHUB_APP_CLIENT_ID": "Iv1.cafe", "GITHUB_APP_CLIENT_SECRET": "app-sec"}
        assert oauth_engine._resolve_app_credentials(_oauth(), creds) == ("oauth-cid", "oauth-sec")
        marked = {**creds, "GITHUB_APP_KIND": kinds.KIND_GITHUB_APP}
        assert oauth_engine._resolve_app_credentials(_oauth(), marked) == ("Iv1.cafe", "app-sec")
        assert oauth_engine._resolve_app_credentials(_oauth(), marked, kinds.KIND_OAUTH_APP) == ("oauth-cid", "oauth-sec")
        assert oauth_engine._resolve_app_credentials(_oauth(), creds, kinds.KIND_GITHUB_APP) == ("Iv1.cafe", "app-sec")

    def test_a_provider_without_kinds_reads_its_legacy_fields(self):
        oauth = {"app_credential_fields": [{"key": "SLACK_CLIENT_ID", "label": "a"},
                                           {"key": "SLACK_CLIENT_SECRET", "label": "b"}]}
        creds = {"SLACK_CLIENT_ID": "i", "SLACK_CLIENT_SECRET": "s"}
        assert oauth_engine._resolve_app_credentials(oauth, creds) == ("i", "s")
        assert FIELDS[0]["key"] == "GITHUB_APP_ID"
