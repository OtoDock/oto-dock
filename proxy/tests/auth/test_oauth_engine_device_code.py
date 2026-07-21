"""Device-code flow — generic provider implementation (RFC 8628).

Tests use respx-style HTTP mocks against httpx.AsyncClient to exercise
the generic provider's start/poll endpoints without hitting any vendor.
"""

from __future__ import annotations

from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from auth.oauth_providers.generic import GenericOAuthProvider


def _provider() -> GenericOAuthProvider:
    return GenericOAuthProvider(
        provider_id="microsoft",
        authorization_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        device_authorization_url="https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
        flow="device_code",
    )


def _mock_post_response(status_code: int, json_payload: dict):
    """Build a mock httpx.AsyncClient that returns one POST response."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_payload

    async_client = AsyncMock()
    async_client.post = AsyncMock(return_value=response)
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__.return_value = None
    return async_client


@pytest.mark.asyncio
async def test_start_device_code_returns_vendor_payload():
    """Generic provider POSTs to device_authorization_url and returns
    the vendor's start payload verbatim."""
    payload = {
        "device_code": "DC-123",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 900,
        "interval": 5,
    }
    with patch("auth.oauth_providers.generic.httpx.AsyncClient",
               return_value=_mock_post_response(200, payload)):
        result = await _provider().start_device_code(
            scopes=["https://graph.microsoft.com/Mail.Read"],
            client_id="msft-client",
        )
    assert result == payload


@pytest.mark.asyncio
async def test_poll_device_code_pending_returns_none():
    """authorization_pending → None (caller waits + retries)."""
    payload = {"error": "authorization_pending"}
    with patch("auth.oauth_providers.generic.httpx.AsyncClient",
               return_value=_mock_post_response(400, payload)):
        result = await _provider().poll_device_code(
            device_code="DC-123",
            client_id="msft-client",
            client_secret="csec",
        )
    assert result is None


@pytest.mark.asyncio
async def test_poll_device_code_success_returns_token_set():
    """200 + token payload → TokenSet."""
    payload = {
        "access_token": "ms-at-1",
        "refresh_token": "ms-rt-1",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "https://graph.microsoft.com/Mail.Read",
    }
    with patch("auth.oauth_providers.generic.httpx.AsyncClient",
               return_value=_mock_post_response(200, payload)):
        ts = await _provider().poll_device_code(
            device_code="DC-123",
            client_id="msft-client",
            client_secret="csec",
        )
    assert ts is not None
    assert ts.access_token == "ms-at-1"
    assert ts.refresh_token == "ms-rt-1"
    assert ts.expires_in == 3600


@pytest.mark.asyncio
async def test_poll_device_code_terminal_failure_raises():
    """expired_token, access_denied, invalid_grant → RuntimeError (caller
    surfaces to user)."""
    payload = {"error": "expired_token", "error_description": "Code expired"}
    with patch("auth.oauth_providers.generic.httpx.AsyncClient",
               return_value=_mock_post_response(400, payload)):
        with pytest.raises(RuntimeError, match="expired"):
            await _provider().poll_device_code(
                device_code="DC-123",
                client_id="msft-client",
                client_secret="csec",
            )


# ---------------------------------------------------------------------------
# Route guard — device-code start refuses hosted (via OtoDock) mode
# ---------------------------------------------------------------------------


def _fake_manifest():
    m = MagicMock()
    m.credentials.oauth = {
        "provider_id": "microsoft",
        "services": [{"key": "mail", "scopes": ["x"]}],
    }
    return m


def _dashboard_user():
    from auth.providers import UserContext
    return UserContext(sub="u1", email="u@example.com", name="U", role="admin")


@pytest.mark.asyncio
async def test_device_code_start_refuses_hosted_mode(monkeypatch):
    """Hosted OAuth brokers only the browser auth-code flow — the relay
    holds no device-code state and the install has no local app credential.
    The start route must 400 with a clear message instead of falling
    through to the raw 'credentials not configured' 500."""
    from fastapi import HTTPException
    from api.auth import oauth as oauth_api

    monkeypatch.setattr(
        oauth_api.mcp_registry, "get_manifest", lambda name: _fake_manifest(),
    )
    monkeypatch.setattr(
        oauth_api.relay_client, "hosted_oauth_active",
        lambda mcp_name, manifest, flow="": True,
    )
    body = oauth_api.DeviceCodeStartRequest(mcp_name="m365-mcp", services=["mail"])
    with pytest.raises(HTTPException) as ei:
        await oauth_api.device_code_start("microsoft", body, _dashboard_user())
    assert ei.value.status_code == 400
    assert "hosted" in ei.value.detail.lower()
    assert "self-managed" in ei.value.detail.lower()


@pytest.mark.asyncio
async def test_device_code_start_passes_guard_when_self_managed(monkeypatch):
    """Self-managed installs proceed past the guard into the normal
    app-credential resolution (stubbed with a sentinel to prove control
    flow reached it)."""
    from fastapi import HTTPException
    from api.auth import oauth as oauth_api

    monkeypatch.setattr(
        oauth_api.mcp_registry, "get_manifest", lambda name: _fake_manifest(),
    )
    monkeypatch.setattr(
        oauth_api.relay_client, "hosted_oauth_active",
        lambda mcp_name, manifest, flow="": False,
    )
    sentinel = HTTPException(500, "reached app-credential resolution")

    def _fake_app_creds(provider, mcp_name, flow=""):
        raise sentinel

    monkeypatch.setattr(oauth_api, "_app_creds_for", _fake_app_creds)
    body = oauth_api.DeviceCodeStartRequest(mcp_name="m365-mcp", services=["mail"])
    with pytest.raises(HTTPException) as ei:
        await oauth_api.device_code_start("microsoft", body, _dashboard_user())
    assert ei.value is sentinel  # got PAST the hosted guard
