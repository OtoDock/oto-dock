"""FacebookOAuthProvider tests.

Covers:
  * Provider registration (`get_provider("facebook")`).
  * Authorize URL: COMMA-joined scopes (Meta documents commas only) + core
    params + `extra` layering (room for an FLB `config_id`).
  * normalize_token_response stores the access token in the refresh slot
    (Meta issues no refresh_token — the token re-exchanges itself), on both
    the self-managed and the hosted (relay re-normalize) paths.
  * exchange_code upgrades short-lived → long-lived immediately
    (fb_exchange_token), FAIL-OPEN on upgrade errors.
  * refresh speaks grant_type=fb_exchange_token and ROTATES — the new token
    becomes the next refresh credential.
  * Nested Meta errors ({"error": {"code": 190}}) surface as RuntimeError
    with ``invalid_grant`` so the refresh worker marks the grant dead.
  * Userinfo /me?fields=id,name,email with the NAME fallback label.
  * Revoke = DELETE /me/permissions, best-effort.
  * The refresh worker's per-provider threshold override
    (credentials.oauth.refresh.min_remaining_seconds — facebook manifests
    declare 7 days; 5 minutes at day 60 dies on any proxy downtime).

HTTP is mocked via httpx.AsyncClient.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from auth.oauth_providers import get_provider
from auth.oauth_providers.facebook import FacebookOAuthProvider

pytestmark = pytest.mark.asyncio


@pytest.fixture
def provider() -> FacebookOAuthProvider:
    return FacebookOAuthProvider()


def _resp(payload: dict, status: int = 200):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=payload)
    r.text = ""
    return r


def _client(post_responses=None, get_response=None, delete_response=None):
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=None)
    if post_responses is not None:
        c.post = AsyncMock(side_effect=list(post_responses))
    if get_response is not None:
        c.get = AsyncMock(return_value=get_response)
    if delete_response is not None:
        c.delete = AsyncMock(return_value=delete_response)
    return c


# ---------------------------------------------------------------------------
# Registration + metadata
# ---------------------------------------------------------------------------


class TestFacebookRegistration:
    def test_facebook_resolves_to_subclass(self):
        p = get_provider("facebook")
        assert isinstance(p, FacebookOAuthProvider)
        assert p.provider_id == "facebook"

    def test_facebook_metadata(self, provider):
        assert provider.authorization_url == (
            "https://www.facebook.com/v26.0/dialog/oauth"
        )
        assert provider.token_url == (
            "https://graph.facebook.com/v26.0/oauth/access_token"
        )
        assert provider.revoke_url == (
            "https://graph.facebook.com/v26.0/me/permissions"
        )
        assert provider.userinfo_url == "https://graph.facebook.com/v26.0/me"
        assert provider.userinfo_id_field == "id"
        assert provider.flow == "authorization_code"
        assert provider.supports_incremental_auth is False


# ---------------------------------------------------------------------------
# build_auth_url — comma scopes
# ---------------------------------------------------------------------------


class TestFacebookAuthUrl:
    async def test_scopes_are_comma_joined(self, provider):
        url = await provider.build_auth_url(
            state="st", scopes=["pages_show_list", "ads_read", "instagram_basic"],
            redirect_uri="https://x/cb", client_id="cid",
        )
        assert "scope=pages_show_list%2Cads_read%2Cinstagram_basic" in url
        assert "scope=pages_show_list+ads_read" not in url

    async def test_core_params_present(self, provider):
        url = await provider.build_auth_url(
            state="st-1", scopes=["public_profile"],
            redirect_uri="https://x/cb", client_id="cid-1",
        )
        assert url.startswith("https://www.facebook.com/v26.0/dialog/oauth?")
        assert "client_id=cid-1" in url
        assert "response_type=code" in url
        assert "state=st-1" in url
        assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in url

    async def test_extra_params_layer_on(self, provider):
        # Room for a Facebook-Login-for-Business config_id should the dialog
        # ever demand configurations instead of classic scopes.
        url = await provider.build_auth_url(
            state="st", scopes=["public_profile"],
            redirect_uri="https://x/cb", client_id="cid",
            extra={"config_id": "1234567890"},
        )
        assert "config_id=1234567890" in url


# ---------------------------------------------------------------------------
# normalize_token_response — token doubles as the refresh credential
# ---------------------------------------------------------------------------


class TestFacebookNormalize:
    def test_access_token_fills_refresh_slot(self, provider):
        ts = provider.normalize_token_response(
            {"access_token": "LL-1", "token_type": "bearer",
             "expires_in": 5183944, "via_relay": True},
        )
        assert ts.access_token == "LL-1"
        assert ts.refresh_token == "LL-1"
        assert ts.expires_in == 5183944

    def test_explicit_refresh_token_respected(self, provider):
        ts = provider.normalize_token_response(
            {"access_token": "AT", "refresh_token": "RT"},
        )
        assert ts.refresh_token == "RT"

    def test_no_access_token_means_no_fake_refresh(self, provider):
        ts = provider.normalize_token_response({"token_type": "bearer"})
        assert ts.refresh_token == ""


# ---------------------------------------------------------------------------
# exchange_code — immediate long-lived upgrade
# ---------------------------------------------------------------------------


class TestFacebookExchange:
    async def test_exchange_upgrades_to_long_lived(self, provider):
        client = _client(post_responses=[
            _resp({"access_token": "SL-1", "token_type": "bearer",
                   "expires_in": 5000}),
            _resp({"access_token": "LL-1", "token_type": "bearer",
                   "expires_in": 5183944}),
        ])
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            ts = await provider.exchange_code(
                code="CODE", redirect_uri="https://x/cb",
                client_id="cid", client_secret="sec",
            )
        assert client.post.await_count == 2
        first = client.post.await_args_list[0].kwargs["data"]
        second = client.post.await_args_list[1].kwargs["data"]
        assert first["grant_type"] == "authorization_code"
        assert second["grant_type"] == "fb_exchange_token"
        assert second["fb_exchange_token"] == "SL-1"
        assert ts.access_token == "LL-1"
        assert ts.refresh_token == "LL-1"
        assert ts.expires_in == 5183944

    async def test_upgrade_failure_ships_short_lived(self, provider):
        client = _client(post_responses=[
            _resp({"access_token": "SL-1", "token_type": "bearer",
                   "expires_in": 5000}),
            _resp({"error": {"message": "boom", "type": "OAuthException",
                             "code": 2}}, status=500),
        ])
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            ts = await provider.exchange_code(
                code="CODE", redirect_uri="https://x/cb",
                client_id="cid", client_secret="sec",
            )
        assert ts.access_token == "SL-1"
        assert ts.refresh_token == "SL-1"   # worker upgrades within the hour

    async def test_exchange_error_raises(self, provider):
        client = _client(post_responses=[
            _resp({"error": {"message": "bad code", "type": "OAuthException",
                             "code": 100}}, status=400),
        ])
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            with pytest.raises(RuntimeError, match="bad code"):
                await provider.exchange_code(
                    code="X", redirect_uri="https://x/cb",
                    client_id="cid", client_secret="sec",
                )


# ---------------------------------------------------------------------------
# refresh — fb_exchange_token + rotation + permanent-error classification
# ---------------------------------------------------------------------------


class TestFacebookRefresh:
    async def test_refresh_speaks_fb_exchange_token_and_rotates(self, provider):
        client = _client(post_responses=[
            _resp({"access_token": "LL-2", "token_type": "bearer",
                   "expires_in": 5183944}),
        ])
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            ts = await provider.refresh(
                refresh_token="LL-1", client_id="cid", client_secret="sec",
            )
        data = client.post.await_args_list[0].kwargs["data"]
        assert data["grant_type"] == "fb_exchange_token"
        assert data["fb_exchange_token"] == "LL-1"
        assert "refresh_token" not in data
        assert ts.access_token == "LL-2"
        assert ts.refresh_token == "LL-2"   # rotation — never the stale LL-1

    async def test_code_190_marks_grant_dead(self, provider):
        """Meta code 190 = invalid/expired token. The message must trip the
        refresh worker's permanent-failure check, or a dead grant would
        back off and retry forever instead of waiting for a reconnect."""
        from services.oauth.oauth_refresh_worker import (
            _is_permanent_refresh_error,
        )

        client = _client(post_responses=[
            _resp({"error": {"message": "Session has expired",
                             "type": "OAuthException", "code": 190,
                             "error_subcode": 463}}, status=400),
        ])
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            with pytest.raises(RuntimeError) as exc:
                await provider.refresh(
                    refresh_token="LL-dead", client_id="cid",
                    client_secret="sec",
                )
        assert "invalid_grant" in str(exc.value)
        assert _is_permanent_refresh_error(exc.value) is True

    async def test_other_codes_stay_transient(self, provider):
        from services.oauth.oauth_refresh_worker import (
            _is_permanent_refresh_error,
        )

        client = _client(post_responses=[
            _resp({"error": {"message": "Unsupported request",
                             "type": "GraphMethodException", "code": 100}},
                  status=400),
        ])
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            with pytest.raises(RuntimeError) as exc:
                await provider.refresh(
                    refresh_token="LL-1", client_id="cid", client_secret="sec",
                )
        assert "invalid_grant" not in str(exc.value)
        assert _is_permanent_refresh_error(exc.value) is False


# ---------------------------------------------------------------------------
# fetch_userinfo — email → name fallback label
# ---------------------------------------------------------------------------


class TestFacebookUserinfo:
    async def test_userinfo_with_email(self, provider):
        client = _client(get_response=_resp(
            {"id": "1020304050", "name": "Dimi Tsis", "email": "d@example.com"},
        ))
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            info = await provider.fetch_userinfo(access_token="AT")
        assert info.email == "d@example.com"
        assert info.name == "Dimi Tsis"
        assert info.account_id == "1020304050"
        assert client.get.await_args.kwargs["params"] == {
            "fields": "id,name,email",
        }

    async def test_userinfo_email_absent_falls_back_to_name(self, provider):
        """`email` needs the email permission and Meta often omits it — the
        profile NAME substitutes so the account label (and token filename)
        stays human-readable instead of a numeric Graph id."""
        client = _client(get_response=_resp(
            {"id": "1020304050", "name": "Dimi Tsis"},
        ))
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            info = await provider.fetch_userinfo(access_token="AT")
        assert info.email == "Dimi Tsis"
        assert info.account_id == "1020304050"

    async def test_userinfo_error_raises(self, provider):
        client = _client(get_response=_resp(
            {"error": {"message": "bad token", "type": "OAuthException",
                       "code": 190}}, status=400,
        ))
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            with pytest.raises(RuntimeError, match="invalid_grant"):
                await provider.fetch_userinfo(access_token="dead")


# ---------------------------------------------------------------------------
# revoke — DELETE /me/permissions, best-effort
# ---------------------------------------------------------------------------


class TestFacebookRevoke:
    async def test_revoke_success(self, provider):
        client = _client(delete_response=_resp({"success": True}))
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            ok = await provider.revoke(
                token="AT", client_id="cid", client_secret="sec",
            )
        assert ok is True
        args = client.delete.await_args
        assert args.args[0] == "https://graph.facebook.com/v26.0/me/permissions"
        assert args.kwargs["params"] == {"access_token": "AT"}

    async def test_revoke_failure_returns_false(self, provider):
        client = _client(delete_response=_resp({}, status=400))
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            ok = await provider.revoke(
                token="AT", client_id="cid", client_secret="sec",
            )
        assert ok is False

    async def test_revoke_exception_returns_false(self, provider):
        client = _client()
        client.delete = AsyncMock(side_effect=OSError("net down"))
        with patch("auth.oauth_providers.facebook.httpx.AsyncClient",
                   return_value=client):
            ok = await provider.revoke(
                token="AT", client_id="cid", client_secret="sec",
            )
        assert ok is False


# ---------------------------------------------------------------------------
# refresh worker — per-provider threshold override (manifest min_remaining)
# ---------------------------------------------------------------------------


class TestRefreshThresholdOverride:
    def test_manifest_min_remaining_raises_threshold(self, monkeypatch):
        from services.mcp import mcp_registry
        from services.oauth import oauth_refresh_worker as w

        m = MagicMock()
        m.credentials.oauth = {
            "provider_id": "facebook",
            "refresh": {"strategy": "lazy", "min_remaining_seconds": 604800},
        }
        monkeypatch.setattr(
            mcp_registry, "get_mcps_by_provider", lambda pid: [m],
        )
        assert w._refresh_threshold_for("facebook") == 604800

    def test_no_manifests_keeps_default(self, monkeypatch):
        from services.mcp import mcp_registry
        from services.oauth import oauth_refresh_worker as w

        monkeypatch.setattr(
            mcp_registry, "get_mcps_by_provider", lambda pid: [],
        )
        assert w._refresh_threshold_for("nope") == w._REFRESH_THRESHOLD_SECONDS

    def test_declared_but_small_values_floor_at_default(self, monkeypatch):
        from services.mcp import mcp_registry
        from services.oauth import oauth_refresh_worker as w

        m = MagicMock()
        m.credentials.oauth = {
            "provider_id": "google",
            "refresh": {"strategy": "lazy", "min_remaining_seconds": 30},
        }
        monkeypatch.setattr(
            mcp_registry, "get_mcps_by_provider", lambda pid: [m],
        )
        assert w._refresh_threshold_for("google") == w._REFRESH_THRESHOLD_SECONDS
