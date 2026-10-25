"""SlackOAuthProvider tests.

Covers:
  * Provider registration (`get_provider("slack")`).
  * v2_user endpoints (the mcp.slack.com flow) + URL building (single
    `scope`; classic dual `user_scope` still supported via extra).
  * Response normalization for BOTH shapes: v2_user (root user token,
    authed_user = {id, scope} only — no preferred_bearer) and classic v2
    (nested user token → user_token + preferred_bearer; enterprise_id).
  * Userinfo via auth.test + users.info email enrichment with the
    handle@workspace-domain synthesis fallback.
  * Revoke best-effort (returns False, never raises).

HTTP is mocked via httpx.AsyncClient.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from auth.oauth_providers import get_provider
from auth.oauth_providers.base import TokenSet, UserInfo
from auth.oauth_providers.slack import SlackOAuthProvider


@pytest.fixture
def provider() -> SlackOAuthProvider:
    return SlackOAuthProvider()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestSlackRegistration:
    def test_slack_resolves_to_subclass(self):
        p = get_provider("slack")
        assert isinstance(p, SlackOAuthProvider)
        assert p.provider_id == "slack"

    def test_slack_metadata(self, provider):
        # The v2_user pair — the ONLY flow whose tokens mcp.slack.com accepts.
        assert provider.authorization_url == "https://slack.com/oauth/v2_user/authorize"
        assert provider.token_url == "https://slack.com/api/oauth.v2.user.access"
        assert provider.revoke_url == "https://slack.com/api/auth.revoke"
        assert provider.userinfo_url == "https://slack.com/api/auth.test"
        assert provider.flow == "authorization_code"


# ---------------------------------------------------------------------------
# build_auth_url — dual scope/user_scope, space-joined
# ---------------------------------------------------------------------------


class TestSlackAuthUrl:
    @pytest.mark.asyncio
    async def test_bot_scopes_only_omits_user_scope(self, provider):
        url = await provider.build_auth_url(
            state="st",
            scopes=["channels:read", "chat:write"],
            redirect_uri="https://x/cb",
            client_id="cid",
        )
        assert "scope=channels%3Aread+chat%3Awrite" in url
        assert "user_scope" not in url
        assert "client_id=cid" in url
        assert "state=st" in url

    @pytest.mark.asyncio
    async def test_dual_scopes_via_extra_user_scopes(self, provider):
        url = await provider.build_auth_url(
            state="st",
            scopes=["channels:read"],
            redirect_uri="https://x/cb",
            client_id="cid",
            extra={"user_scopes": ["search:read", "files:read"]},
        )
        assert "scope=channels%3Aread" in url
        assert "user_scope=search%3Aread+files%3Aread" in url

    @pytest.mark.asyncio
    async def test_user_scopes_string_form_also_supported(self, provider):
        url = await provider.build_auth_url(
            state="st",
            scopes=["channels:read"],
            redirect_uri="https://x/cb",
            client_id="cid",
            extra={"user_scopes": "search:read files:read"},
        )
        assert "user_scope=search%3Aread+files%3Aread" in url


# ---------------------------------------------------------------------------
# exchange_code + normalize_token_response
# ---------------------------------------------------------------------------


def _mock_post(json_payload: dict, status: int = 200):
    """Helper: build an httpx.AsyncClient mock returning ``json_payload``."""
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json = MagicMock(return_value=json_payload)
    mock_response.text = ""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


def _mock_post_get(json_payload: dict, status: int = 200):
    """Same as above but also wires `.get`."""
    mc = _mock_post(json_payload, status)
    mc.get = AsyncMock(return_value=mc.post.return_value)
    return mc


class TestSlackExchange:
    @pytest.mark.asyncio
    async def test_dual_token_response_normalization(self, provider):
        """v2 response with both bot + authed_user → extras carry both
        tokens and preferred_bearer=user_token."""
        mc = _mock_post({
            "ok": True,
            "access_token": "xoxb-bot-token",
            "token_type": "bot",
            "scope": "channels:read,chat:write",
            "bot_user_id": "U_BOT",
            "app_id": "A_APP",
            "team": {"id": "T_TEAM", "name": "Acme"},
            "enterprise": {"id": "E_ENT"},
            "authed_user": {
                "id": "U_USER",
                "scope": "search:read,files:read",
                "access_token": "xoxp-user-token",
                "token_type": "user",
            },
        })
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            ts = await provider.exchange_code(
                code="c", redirect_uri="r", client_id="ci", client_secret="cs",
            )
        assert isinstance(ts, TokenSet)
        assert ts.access_token == "xoxb-bot-token"
        # raw must carry all the synthesized flat keys (engine dumps them into
        # extra.*)
        assert ts.raw["team_id"] == "T_TEAM"
        assert ts.raw["team_name"] == "Acme"
        assert ts.raw["bot_user_id"] == "U_BOT"
        assert ts.raw["enterprise_id"] == "E_ENT"
        assert ts.raw["user_id"] == "U_USER"
        assert ts.raw["user_token"] == "xoxp-user-token"
        assert ts.raw["user_scope"] == "search:read,files:read"
        assert ts.raw["preferred_bearer"] == "user_token"

    @pytest.mark.asyncio
    async def test_bot_only_response_omits_preferred_bearer(self, provider):
        """No authed_user → no user_token, no preferred_bearer in extra
        (bearer injector falls back to canonical access_token)."""
        mc = _mock_post({
            "ok": True,
            "access_token": "xoxb-bot-only",
            "token_type": "bot",
            "scope": "chat:write",
            "bot_user_id": "U_BOT",
            "team": {"id": "T1", "name": "TeamOne"},
            # no authed_user
        })
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            ts = await provider.exchange_code(
                code="c", redirect_uri="r", client_id="ci", client_secret="cs",
            )
        assert ts.access_token == "xoxb-bot-only"
        assert ts.raw["team_id"] == "T1"
        assert "preferred_bearer" not in ts.raw
        assert "user_token" not in ts.raw

    @pytest.mark.asyncio
    async def test_v2_user_response_normalization(self, provider):
        """oauth.v2.user.access: the USER token is at the ROOT and
        authed_user carries only {id, scope} — user_id still flattens into
        raw, scope is taken from authed_user, and NO preferred_bearer is set
        (the canonical access_token already IS the user token)."""
        mc = _mock_post({
            "ok": True,
            "access_token": "xoxp-root-user-token",
            "token_type": "user",
            "team": {"id": "T_TEAM", "name": "Acme"},
            "authed_user": {
                "id": "U_USER",
                "scope": "search:read.public,chat:write",
            },
        })
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            ts = await provider.exchange_code(
                code="c", redirect_uri="r", client_id="ci", client_secret="cs",
            )
        assert ts.access_token == "xoxp-root-user-token"
        assert ts.scope == "search:read.public,chat:write"
        assert ts.token_type == "Bearer"
        assert ts.expires_in == 0  # no rotation → never expires
        assert ts.raw["team_id"] == "T_TEAM"
        assert ts.raw["team_name"] == "Acme"
        assert ts.raw["user_id"] == "U_USER"
        assert "preferred_bearer" not in ts.raw
        assert "user_token" not in ts.raw

    @pytest.mark.asyncio
    async def test_error_response_raises_with_slack_error_code(self, provider):
        mc = _mock_post({"ok": False, "error": "invalid_code"})
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            with pytest.raises(RuntimeError, match="invalid_code"):
                await provider.exchange_code(
                    code="c", redirect_uri="r", client_id="ci", client_secret="cs",
                )


# ---------------------------------------------------------------------------
# fetch_userinfo — auth.test
# ---------------------------------------------------------------------------


def _response(json_payload: dict, status: int = 200):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_payload)
    r.text = ""
    return r


def _mock_post_seq(*payloads: dict):
    """Client whose successive .post calls return ``payloads`` in order."""
    mc = MagicMock()
    mc.__aenter__ = AsyncMock(return_value=mc)
    mc.__aexit__ = AsyncMock(return_value=None)
    mc.post = AsyncMock(side_effect=[_response(p) for p in payloads])
    return mc


_AUTH_TEST = {
    "ok": True,
    "url": "https://acme.slack.com/",
    "user": "alice",
    "user_id": "U_ALICE",
    "team": "Acme",
    "team_id": "T_ACME",
}


class TestSlackUserinfo:
    @pytest.mark.asyncio
    async def test_userinfo_enriches_email_via_users_info(self, provider):
        """auth.test has no email — users.info profile.email fills it
        (granted with users:read.email)."""
        mc = _mock_post_seq(
            _AUTH_TEST,
            {"ok": True, "user": {"id": "U_ALICE",
                                  "profile": {"email": "alice@acme.com"}}},
        )
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            ui = await provider.fetch_userinfo(access_token="xoxp-test")
        assert isinstance(ui, UserInfo)
        assert ui.email == "alice@acme.com"
        assert ui.name == "alice"
        assert ui.account_id == "U_ALICE"

    @pytest.mark.asyncio
    async def test_userinfo_synthesizes_label_when_email_unavailable(self, provider):
        """users.info denied (missing users:read.email) → label falls back to
        handle@workspace-domain — readable AND unique across workspaces."""
        mc = _mock_post_seq(
            _AUTH_TEST,
            {"ok": False, "error": "missing_scope"},
        )
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            ui = await provider.fetch_userinfo(access_token="xoxp-test")
        assert ui.email == "alice@acme.slack.com"
        assert ui.account_id == "U_ALICE"

    @pytest.mark.asyncio
    async def test_userinfo_raises_when_not_ok(self, provider):
        mc = _mock_post({"ok": False, "error": "invalid_auth"})
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            with pytest.raises(RuntimeError, match="invalid_auth"):
                await provider.fetch_userinfo(access_token="bad")


# ---------------------------------------------------------------------------
# revoke — best-effort
# ---------------------------------------------------------------------------


class TestSlackRevoke:
    @pytest.mark.asyncio
    async def test_revoke_returns_true_on_revoked_true(self, provider):
        mc = _mock_post({"ok": True, "revoked": True})
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            ok = await provider.revoke(token="t", client_id="c", client_secret="s")
        assert ok is True

    @pytest.mark.asyncio
    async def test_revoke_returns_false_on_exception(self, provider):
        mc = MagicMock()
        mc.__aenter__ = AsyncMock(return_value=mc)
        mc.__aexit__ = AsyncMock(return_value=None)
        mc.post = AsyncMock(side_effect=httpx.ConnectError("network"))
        with patch("auth.oauth_providers.slack.httpx.AsyncClient", return_value=mc):
            ok = await provider.revoke(token="t", client_id="c", client_secret="s")
        assert ok is False
