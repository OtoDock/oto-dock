"""Client-credentials (S2S) flow — generic provider implementation
(RFC 6749 §4.4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.oauth_providers.generic import GenericOAuthProvider


def _provider() -> GenericOAuthProvider:
    return GenericOAuthProvider(
        provider_id="zoom",
        authorization_url="https://zoom.us/oauth/authorize",
        token_url="https://zoom.us/oauth/token",
        flow="client_credentials",
    )


def _mock_post_response(status_code: int, json_payload: dict):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_payload

    async_client = AsyncMock()
    async_client.post = AsyncMock(return_value=response)
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__.return_value = None
    return async_client


@pytest.mark.asyncio
async def test_client_credentials_success_returns_token_set():
    """grant_type=client_credentials POST → TokenSet."""
    payload = {
        "access_token": "s2s-at-1",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "meeting:read",
    }
    with patch("auth.oauth_providers.generic.httpx.AsyncClient",
               return_value=_mock_post_response(200, payload)):
        ts = await _provider().exchange_client_credentials(
            client_id="zoom-cid",
            client_secret="zoom-csec",
            scopes=["meeting:read"],
        )
    assert ts.access_token == "s2s-at-1"
    assert ts.expires_in == 3600
    # S2S typically has no refresh token.
    assert ts.refresh_token == ""


@pytest.mark.asyncio
async def test_client_credentials_extra_params_forwarded():
    """`extra` kwargs are merged into the POST body (Zoom's account_id)."""
    captured = {}

    class _CapturePost:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def post(self, url, data=None, headers=None):
            captured["url"] = url
            captured["data"] = dict(data or {})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"access_token": "x", "expires_in": 3600}
            return resp

    with patch("auth.oauth_providers.generic.httpx.AsyncClient",
               return_value=_CapturePost()):
        await _provider().exchange_client_credentials(
            client_id="cid",
            client_secret="csec",
            scopes=[],
            extra={"account_id": "ACC-789"},
        )
    assert captured["data"]["grant_type"] == "client_credentials"
    assert captured["data"]["client_id"] == "cid"
    assert captured["data"]["client_secret"] == "csec"
    assert captured["data"]["account_id"] == "ACC-789"


@pytest.mark.asyncio
async def test_client_credentials_vendor_error_raises():
    """4xx with error payload → RuntimeError."""
    payload = {"error": "invalid_client", "error_description": "Bad creds"}
    with patch("auth.oauth_providers.generic.httpx.AsyncClient",
               return_value=_mock_post_response(401, payload)):
        with pytest.raises(RuntimeError, match="Bad creds|invalid_client"):
            await _provider().exchange_client_credentials(
                client_id="cid",
                client_secret="csec",
                scopes=[],
            )
