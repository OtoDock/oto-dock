"""Incremental-authorization scope union (shared-provider token files).

Several MCPs can share one provider_id (workspace-mcp + google-analytics-mcp
→ ``google``); their email-derived account labels collide on ONE token file
under ``sessions/google-tokens/{username}/``. Two behaviors keep that safe
for providers that support incremental authorization:

  * ``oauth_start`` always sends the incremental param
    (``include_granted_scopes=true``) so any re-consent by the same vendor
    account grants the UNION of old + new scopes (provider attribute
    ``supports_incremental_auth`` — pinned here).
  * ``persist_oauth_account`` records the matching union when it overwrites
    an existing token file — readers (workspace-mcp's google.auth) pass the
    file's ``scopes`` on refresh, so a narrowed record would narrow the
    refreshed token and silently break the OTHER MCP of the provider.

Non-incremental providers keep the old record-what-was-requested behavior:
their vendors really do replace the grant, so a union would overstate it.
"""

from __future__ import annotations

import json


from auth.oauth_providers.base import OAuthProvider, TokenSet, UserInfo
from auth.oauth_providers.google import GoogleOAuthProvider
from auth.oauth_providers.generic import GenericOAuthProvider
from services.oauth import oauth_account_store


# ---------------------------------------------------------------------------
# Provider attribute contract
# ---------------------------------------------------------------------------


class TestSupportsIncrementalAuthAttribute:
    def test_google_opts_in(self):
        assert GoogleOAuthProvider.supports_incremental_auth is True

    def test_base_default_is_false(self):
        assert OAuthProvider.supports_incremental_auth is False

    def test_generic_manifest_providers_default_false(self):
        p = GenericOAuthProvider(
            provider_id="linear",
            authorization_url="https://x/auth",
            token_url="https://x/token",
        )
        assert getattr(p, "supports_incremental_auth", False) is False


# ---------------------------------------------------------------------------
# persist_oauth_account — scope union on token-file overwrite
# ---------------------------------------------------------------------------


class _FakeManifest:
    """Minimal manifest stand-in: credentials.oauth is the raw dict."""

    def __init__(self, oauth: dict):
        class _Cred:
            pass
        self.credentials = _Cred()
        self.credentials.oauth = oauth


def _persist(monkeypatch, tmp_path, *, provider_id: str, mcp_name: str,
             services: list[str], oauth_block: dict, label: str):
    """Run persist_oauth_account with everything except the token file stubbed."""
    import config as _config
    from services.mcp import mcp_registry
    from storage import credential_store
    from storage import database as task_store

    monkeypatch.setattr(_config, "SESSIONS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice")
    monkeypatch.setattr(
        mcp_registry, "get_manifest", lambda name: _FakeManifest(oauth_block),
    )
    monkeypatch.setattr(
        credential_store, "set_user_credentials", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        credential_store, "set_account_display_email", lambda *a, **k: None,
    )

    oauth_account_store.persist_oauth_account(
        user_sub="user-sub",
        mcp_name=mcp_name,
        provider_id=provider_id,
        account_label=label,
        services=services,
        token_set=TokenSet(access_token="at-new", refresh_token="rt-new"),
        userinfo=UserInfo(email="alice@example.com", account_id="sub-1"),
        client_id="cid",
        client_secret="csec",
        token_url="https://oauth2.googleapis.com/token",
    )
    token_file = tmp_path / f"{provider_id}-tokens" / "alice" / f"{label}.json"
    return json.loads(token_file.read_text())


_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
_ANALYTICS = "https://www.googleapis.com/auth/analytics.readonly"
_BASE = ["openid", "https://www.googleapis.com/auth/userinfo.email"]

_ANALYTICS_OAUTH = {
    "provider_id": "google",
    "base_scopes": list(_BASE),
    "services": [{"key": "analytics", "scopes": [_ANALYTICS]}],
    "token_format": {"schema": "generic_oauth_v1"},
}


class TestPersistScopeUnion:
    def test_google_overwrite_unions_prior_file_scopes(
        self, monkeypatch, tmp_path,
    ):
        """Second-MCP connect (same provider + label) keeps the first MCP's
        scopes in the shared file's record."""
        prior_dir = tmp_path / "google-tokens" / "alice"
        prior_dir.mkdir(parents=True)
        (prior_dir / "alice@example.com.json").write_text(json.dumps({
            "provider": "google",
            "access_token": "at-old",
            "refresh_token": "rt-old",
            "scopes": _BASE + [_GMAIL],
        }))

        data = _persist(
            monkeypatch, tmp_path,
            provider_id="google",
            mcp_name="google-analytics-mcp",
            services=["analytics"],
            oauth_block=_ANALYTICS_OAUTH,
            label="alice@example.com",
        )
        # New request slice first, prior grant appended (deduped).
        assert data["scopes"] == _BASE + [_ANALYTICS, _GMAIL]
        assert data["access_token"] == "at-new"
        assert data["refresh_token"] == "rt-new"

    def test_google_fresh_connect_records_requested_scopes_only(
        self, monkeypatch, tmp_path,
    ):
        data = _persist(
            monkeypatch, tmp_path,
            provider_id="google",
            mcp_name="google-analytics-mcp",
            services=["analytics"],
            oauth_block=_ANALYTICS_OAUTH,
            label="alice@example.com",
        )
        assert data["scopes"] == _BASE + [_ANALYTICS]

    def test_non_incremental_provider_overwrite_replaces_scopes(
        self, monkeypatch, tmp_path,
    ):
        """slack (hardcoded, no incremental auth): the vendor really replaces
        the grant, so the record must NOT union."""
        prior_dir = tmp_path / "slack-tokens" / "alice"
        prior_dir.mkdir(parents=True)
        (prior_dir / "alice@example.com.json").write_text(json.dumps({
            "provider": "slack",
            "access_token": "at-old",
            "scopes": ["channels:history"],
        }))

        data = _persist(
            monkeypatch, tmp_path,
            provider_id="slack",
            mcp_name="slack-mcp",
            services=["chat"],
            oauth_block={
                "provider_id": "slack",
                "base_scopes": [],
                "services": [{"key": "chat", "scopes": ["chat:write"]}],
            },
            label="alice@example.com",
        )
        assert data["scopes"] == ["chat:write"]

    def test_unknown_provider_id_skips_union_without_error(
        self, monkeypatch, tmp_path,
    ):
        """get_provider raising KeyError (no manifest declares the provider
        in this test env) must not break persistence."""
        data = _persist(
            monkeypatch, tmp_path,
            provider_id="no-such-provider",
            mcp_name="mystery-mcp",
            services=[],
            oauth_block={"provider_id": "no-such-provider", "base_scopes": ["s1"]},
            label="alice@example.com",
        )
        assert data["scopes"] == ["s1"]

    def test_google_corrupt_prior_file_is_ignored(self, monkeypatch, tmp_path):
        prior_dir = tmp_path / "google-tokens" / "alice"
        prior_dir.mkdir(parents=True)
        (prior_dir / "alice@example.com.json").write_text("{not json")

        data = _persist(
            monkeypatch, tmp_path,
            provider_id="google",
            mcp_name="google-analytics-mcp",
            services=["analytics"],
            oauth_block=_ANALYTICS_OAUTH,
            label="alice@example.com",
        )
        assert data["scopes"] == _BASE + [_ANALYTICS]
