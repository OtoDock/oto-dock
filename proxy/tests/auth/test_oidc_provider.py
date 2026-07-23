"""OIDC lazy endpoint re-discovery tests.

Regression coverage for the boot-race brick: config.py fetches the
.well-known/openid-configuration document once at import time, and when the
IdP is unreachable at that moment (proxy and a co-hosted IdP cold-starting
together after a power cut) the endpoint URLs stayed empty for the process
lifetime — every SSO login returned 503 "OIDC not configured" until a manual
proxy restart. ensure_oidc_discovery() now re-attempts discovery at request
time, rate-limited, with explicit env vars always winning.

HTTP is mocked via httpx.AsyncClient (patched inside oidc_provider).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

import config
from app import app
from auth.providers import oidc_provider
from auth.providers.oidc_provider import OIDCAuthProvider, ensure_oidc_discovery

client = TestClient(app)

_DISCOVERY_DOC = {
    "authorization_endpoint": "https://idp.example.com/authorize",
    "token_endpoint": "https://idp.example.com/token",
    "userinfo_endpoint": "https://idp.example.com/userinfo",
    "end_session_endpoint": "https://idp.example.com/logout",
}


def _mock_get(json_payload: dict, status: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json = MagicMock(return_value=json_payload)
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


def _mock_get_failing():
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    return mock_client


@pytest.fixture(autouse=True)
def _oidc_baseline(monkeypatch):
    """Empty-URL OIDC config with discovery enabled — the post-boot-failure
    state. monkeypatch restores the config attrs apply_oidc_discovery rebinds
    mid-test; the shared retry guard is reset on both sides of each test."""
    oidc_provider._discovery_guard["at"] = 0.0
    for attr in ("OIDC_AUTHORIZE_URL", "OIDC_TOKEN_URL",
                 "OIDC_USERINFO_URL", "OIDC_LOGOUT_URL"):
        monkeypatch.setattr(config, attr, "")
    monkeypatch.setattr(config, "OIDC_ENABLED", True)
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "test-client")
    monkeypatch.setattr(config, "OIDC_REDIRECT_URI", "https://dash.example.com/auth/callback")
    monkeypatch.setattr(
        config, "OIDC_DISCOVERY_URL",
        "https://idp.example.com/.well-known/openid-configuration",
    )
    yield
    oidc_provider._discovery_guard["at"] = 0.0


def test_login_url_none_without_discovery(monkeypatch):
    monkeypatch.setattr(config, "OIDC_DISCOVERY_URL", "")
    assert OIDCAuthProvider().get_login_url() is None


@pytest.mark.asyncio
async def test_ensure_noop_without_discovery_url(monkeypatch):
    monkeypatch.setattr(config, "OIDC_DISCOVERY_URL", "")
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
    assert mc.get.await_count == 0


@pytest.mark.asyncio
async def test_lazy_discovery_success():
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
    assert config.OIDC_AUTHORIZE_URL == "https://idp.example.com/authorize"
    assert config.OIDC_TOKEN_URL == "https://idp.example.com/token"
    assert config.OIDC_USERINFO_URL == "https://idp.example.com/userinfo"
    assert config.OIDC_LOGOUT_URL == "https://idp.example.com/logout"
    url = OIDCAuthProvider().get_login_url()
    assert url is not None
    assert url.startswith("https://idp.example.com/authorize?")
    assert "client_id=test-client" in url


@pytest.mark.asyncio
async def test_lazy_discovery_recovered_once(caplog):
    """Once recovered, further ensures are no-ops — no second fetch."""
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
        await ensure_oidc_discovery()
    assert mc.get.await_count == 1


@pytest.mark.asyncio
async def test_failure_rate_limited_then_retries(caplog):
    mc = _mock_get_failing()
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        with caplog.at_level("WARNING", logger="claude-proxy"):
            await ensure_oidc_discovery()
            await ensure_oidc_discovery()  # inside the retry window — skipped
        assert mc.get.await_count == 1
        failures = [r for r in caplog.records if "discovery retry failed" in r.message]
        assert len(failures) == 1

        # Past the retry window the next login click attempts again.
        oidc_provider._discovery_guard["at"] -= oidc_provider._DISCOVERY_RETRY_INTERVAL_S + 1
        await ensure_oidc_discovery()
        assert mc.get.await_count == 2


@pytest.mark.asyncio
async def test_explicit_env_var_wins(monkeypatch):
    monkeypatch.setattr(config, "OIDC_TOKEN_URL", "https://custom.example.com/token")
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await ensure_oidc_discovery()
    assert config.OIDC_TOKEN_URL == "https://custom.example.com/token"
    assert config.OIDC_AUTHORIZE_URL == "https://idp.example.com/authorize"


@pytest.mark.asyncio
async def test_concurrent_ensures_single_fetch():
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        await asyncio.gather(ensure_oidc_discovery(), ensure_oidc_discovery())
    assert mc.get.await_count == 1
    assert config.OIDC_AUTHORIZE_URL == "https://idp.example.com/authorize"


def test_oidc_url_503_while_idp_down():
    mc = _mock_get_failing()
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        resp = client.get("/auth/oidc-url")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "OIDC not configured"


def test_oidc_url_recovers_without_restart():
    mc = _mock_get(_DISCOVERY_DOC)
    with patch("auth.providers.oidc_provider.httpx.AsyncClient", return_value=mc):
        resp = client.get("/auth/oidc-url")
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://idp.example.com/authorize?")
