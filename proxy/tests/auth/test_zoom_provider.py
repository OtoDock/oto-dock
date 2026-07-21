"""ZoomOAuthProvider tests.

Covers:
  * Provider registration (`get_provider("zoom")`).
  * Authorization-code happy path (HTTP Basic auth on token endpoint).
  * Refresh preserves previous refresh_token when vendor omits.
  * S2S client_credentials with `account_id` in extra → forwarded as
    Zoom's non-standard `account_id` body param.
  * S2S response does NOT echo account_id back (persist site is the
    only source).
  * Userinfo via /v2/users/me.
  * Revoke best-effort.

HTTP is mocked via httpx.AsyncClient.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from auth.oauth_providers import get_provider
from auth.oauth_providers.zoom import ZoomOAuthProvider


@pytest.fixture
def provider() -> ZoomOAuthProvider:
    return ZoomOAuthProvider()


def _mock_post(json_payload: dict, status: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json = MagicMock(return_value=json_payload)
    mock_response.text = ""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


def _mock_get(json_payload: dict, status: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json = MagicMock(return_value=json_payload)
    mock_response.text = ""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestZoomRegistration:
    def test_zoom_resolves_to_subclass(self):
        p = get_provider("zoom")
        assert isinstance(p, ZoomOAuthProvider)
        assert p.provider_id == "zoom"

    def test_zoom_metadata(self, provider):
        assert provider.authorization_url == "https://zoom.us/oauth/authorize"
        assert provider.token_url == "https://zoom.us/oauth/token"
        assert provider.revoke_url == "https://zoom.us/oauth/revoke"
        assert provider.userinfo_url == "https://api.zoom.us/v2/users/me"
        assert provider.flow == "authorization_code"


# ---------------------------------------------------------------------------
# build_auth_url — granular-scope general apps: NO scope param
# ---------------------------------------------------------------------------


class TestZoomAuthUrl:
    """Zoom general apps configure GRANULAR scopes on the marketplace app;
    consent grants exactly that set and the documented authorize URL has no
    scope param. Sending the manifest's classic names (meeting:read) against
    a granular-scope app risks an invalid-scope refusal."""

    @pytest.mark.asyncio
    async def test_auth_url_carries_no_scope_param(self, provider):
        url = await provider.build_auth_url(
            state="st", scopes=["meeting:read", "meeting:write"],
            redirect_uri="https://x/cb", client_id="cid",
        )
        assert "scope=" not in url

    @pytest.mark.asyncio
    async def test_auth_url_core_params_present(self, provider):
        url = await provider.build_auth_url(
            state="st-1", scopes=["meeting:read"],
            redirect_uri="https://x/cb", client_id="cid-1",
        )
        assert url.startswith("https://zoom.us/oauth/authorize?")
        assert "client_id=cid-1" in url
        assert "response_type=code" in url
        assert "state=st-1" in url
        assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in url


# ---------------------------------------------------------------------------
# Authorization-code exchange
# ---------------------------------------------------------------------------


class TestZoomExchange:
    @pytest.mark.asyncio
    async def test_exchange_uses_basic_auth(self, provider):
        """Zoom token endpoint requires HTTP Basic for client credentials,
        not body params (OAuth 2.0 §2.3.1)."""
        mc = _mock_post({
            "access_token": "AT", "refresh_token": "RT",
            "expires_in": 3600, "scope": "meeting:read",
            "token_type": "bearer",
        })
        with patch(
            "auth.oauth_providers.zoom.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.exchange_code(
                code="c", redirect_uri="r",
                client_id="ci", client_secret="cs",
            )
        assert ts.access_token == "AT"
        assert ts.refresh_token == "RT"
        # Confirm Basic auth was passed (auth kwarg, not in body).
        call = mc.post.call_args
        assert call.kwargs["auth"] == ("ci", "cs")
        # And client_id/secret should NOT be in the body.
        body = call.kwargs["data"]
        assert "client_id" not in body
        assert "client_secret" not in body
        assert body["grant_type"] == "authorization_code"


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


class TestZoomRefresh:
    @pytest.mark.asyncio
    async def test_refresh_preserves_previous_refresh_when_omitted(self, provider):
        mc = _mock_post({
            "access_token": "AT2", "expires_in": 3600,
            "scope": "meeting:read", "token_type": "bearer",
            # No refresh_token field.
        })
        with patch(
            "auth.oauth_providers.zoom.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.refresh(
                refresh_token="old-RT", client_id="ci", client_secret="cs",
            )
        assert ts.refresh_token == "old-RT"


# ---------------------------------------------------------------------------
# S2S client_credentials — account_id flow
# ---------------------------------------------------------------------------


class TestZoomS2S:
    @pytest.mark.asyncio
    async def test_s2s_forwards_account_id_from_extra(self, provider):
        """Zoom's S2S is a non-standard client_credentials variant —
        requires `account_id` as a body param. Caller passes it via
        extra={"account_id": "..."}."""
        mc = _mock_post({
            "access_token": "S2S-AT", "expires_in": 3600,
            "scope": "meeting:read:admin", "token_type": "bearer",
        })
        with patch(
            "auth.oauth_providers.zoom.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.exchange_client_credentials(
                client_id="ci", client_secret="cs", scopes=[],
                extra={"account_id": "ACC-123"},
            )
        assert ts.access_token == "S2S-AT"
        # Confirm account_id was put in the body + grant_type is
        # Zoom-specific "account_credentials".
        call = mc.post.call_args
        body = call.kwargs["data"]
        assert body["grant_type"] == "account_credentials"
        assert body["account_id"] == "ACC-123"
        # Basic auth carries client_id/secret.
        assert call.kwargs["auth"] == ("ci", "cs")

    @pytest.mark.asyncio
    async def test_s2s_response_does_not_echo_account_id(self, provider):
        """Zoom's S2S response carries no account_id — the only source
        is the request extra. The persist site (api/auth/oauth.py::s2s_exchange)
        is responsible for injecting it into TokenSet.raw before persist."""
        mc = _mock_post({
            "access_token": "S2S-AT", "expires_in": 3600,
            "scope": "", "token_type": "bearer",
        })
        with patch(
            "auth.oauth_providers.zoom.httpx.AsyncClient",
            return_value=mc,
        ):
            ts = await provider.exchange_client_credentials(
                client_id="ci", client_secret="cs", scopes=[],
                extra={"account_id": "ACC-XYZ"},
            )
        # Provider deliberately does NOT inject account_id into raw —
        # The persist site owns this so refresh-worker
        # re-exchange doesn't accidentally clobber it with an empty value.
        assert "account_id" not in ts.raw


# ---------------------------------------------------------------------------
# fetch_userinfo
# ---------------------------------------------------------------------------


class TestZoomUserinfo:
    @pytest.mark.asyncio
    async def test_userinfo_hits_users_me(self, provider):
        mc = _mock_get({
            "id": "USR-1",
            "email": "alice@example.com",
            "display_name": "Alice Zoomer",
        })
        with patch(
            "auth.oauth_providers.zoom.httpx.AsyncClient",
            return_value=mc,
        ):
            ui = await provider.fetch_userinfo(access_token="AT")
        assert ui.email == "alice@example.com"
        assert ui.name == "Alice Zoomer"
        assert ui.account_id == "USR-1"
        call = mc.get.call_args
        assert "/v2/users/me" in call.args[0]
        assert call.kwargs["headers"]["Authorization"] == "Bearer AT"


# ---------------------------------------------------------------------------
# revoke — best-effort
# ---------------------------------------------------------------------------


class TestZoomRevoke:
    @pytest.mark.asyncio
    async def test_revoke_success(self, provider):
        mc = _mock_post({"status": "success"})
        with patch(
            "auth.oauth_providers.zoom.httpx.AsyncClient",
            return_value=mc,
        ):
            ok = await provider.revoke(
                token="AT", client_id="ci", client_secret="cs",
            )
        assert ok is True

    @pytest.mark.asyncio
    async def test_revoke_failure_returns_false_not_raises(self, provider):
        mc = _mock_post({"status": "error"}, status=400)
        with patch(
            "auth.oauth_providers.zoom.httpx.AsyncClient",
            return_value=mc,
        ):
            ok = await provider.revoke(
                token="AT", client_id="ci", client_secret="cs",
            )
        assert ok is False
