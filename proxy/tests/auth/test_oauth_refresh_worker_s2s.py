"""Refresh worker S2S re-exchange branch.

The refresh worker previously skipped any file with empty refresh_token,
which incidentally also skipped S2S tokens (Zoom S2S has NO refresh
token). It now adds a three-arm dispatch:

  * PAT (``extra.flow == "personal_access_token"``) → skip (never refresh).
  * S2S (``extra.flow == "client_credentials"``) → re-exchange via
    ``provider.exchange_client_credentials`` (no refresh_token needed).
  * Standard OAuth → ``provider.refresh`` (requires refresh_token).

These tests pin the dispatch logic + preservation of S2S-specific
``extra`` keys (``flow``, ``account_id``) across re-exchange writebacks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.oauth_providers.base import TokenSet
from services.oauth import oauth_refresh_worker


def _write_token_file(
    path: Path, *, extra: dict, refresh_token: str = "",
    expires_in_seconds: int = 60,
) -> None:
    """Write a generic_oauth_v1 token file with the given extra block."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "provider": "zoom",
        "account_id": "ACC-XYZ",
        "access_token": "old-AT",
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "scope": "meeting:read:admin",
        "client_id": "ci",
        "client_secret": "cs",
        "token_url": "https://zoom.us/oauth/token",
        "extra": extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


# ---------------------------------------------------------------------------
# S2S re-exchange dispatched on extra.flow == client_credentials
# ---------------------------------------------------------------------------


class TestS2SReExchange:
    @pytest.mark.asyncio
    async def test_s2s_dispatches_to_exchange_client_credentials(self, tmp_path):
        """Files marked client_credentials route to exchange_client_credentials,
        NOT to provider.refresh — and the call carries `account_id` extra."""
        f = tmp_path / "default.json"
        _write_token_file(
            f,
            extra={"flow": "client_credentials", "account_id": "ACC-XYZ"},
            refresh_token="",   # S2S has no refresh token
        )

        # Mock the provider.
        mock_provider = MagicMock()
        new_ts = TokenSet(
            access_token="new-S2S-AT", refresh_token="",
            expires_in=3600, scope="meeting:read:admin",
            token_type="Bearer", raw={},
        )
        mock_provider.exchange_client_credentials = AsyncMock(return_value=new_ts)
        mock_provider.refresh = AsyncMock(
            side_effect=AssertionError("refresh must NOT be called for S2S"),
        )
        mock_provider.token_url = "https://zoom.us/oauth/token"

        # Mock manifest lookup so writeback alias handling doesn't choke.
        mock_manifest = MagicMock()
        mock_manifest.credentials.oauth = {"token_format": {}}

        with patch(
            "auth.oauth_providers.get_provider", return_value=mock_provider,
        ), patch(
            "services.mcp.mcp_registry.get_mcps_by_provider",
            return_value=[mock_manifest],
        ):
            refreshed = await oauth_refresh_worker._maybe_refresh_token_file(
                token_file=f, username="zoom-admin",
                provider_id="zoom",
            )

        assert refreshed is True
        # exchange_client_credentials was called with account_id from extra.
        mock_provider.exchange_client_credentials.assert_called_once()
        call_kwargs = mock_provider.exchange_client_credentials.call_args.kwargs
        assert call_kwargs["client_id"] == "ci"
        assert call_kwargs["client_secret"] == "cs"
        # extra carries account_id from the file
        assert call_kwargs["extra"] == {"account_id": "ACC-XYZ"}

    @pytest.mark.asyncio
    async def test_s2s_preserves_extra_account_id_and_flow_on_writeback(
        self, tmp_path,
    ):
        """Zoom's re-exchange response carries no account_id — we must
        re-assert it on writeback, otherwise the next refresh would have
        no account_id to send and the call would fail (ditto for flow)."""
        f = tmp_path / "acme.json"
        _write_token_file(
            f,
            extra={"flow": "client_credentials", "account_id": "ACC-ACME"},
            refresh_token="",
        )

        mock_provider = MagicMock()
        # Response has NO flow/account_id (vendor doesn't echo them).
        new_ts = TokenSet(
            access_token="new-AT", refresh_token="",
            expires_in=3600, scope="", token_type="Bearer", raw={},
        )
        mock_provider.exchange_client_credentials = AsyncMock(return_value=new_ts)
        mock_provider.token_url = "https://zoom.us/oauth/token"

        mock_manifest = MagicMock()
        mock_manifest.credentials.oauth = {"token_format": {}}

        with patch(
            "auth.oauth_providers.get_provider", return_value=mock_provider,
        ), patch(
            "services.mcp.mcp_registry.get_mcps_by_provider",
            return_value=[mock_manifest],
        ):
            await oauth_refresh_worker._maybe_refresh_token_file(
                token_file=f, username="zoom-admin",
                provider_id="zoom",
            )

        # Re-read file: extra.flow and extra.account_id must survive.
        written = json.loads(f.read_text())
        assert written["access_token"] == "new-AT"
        assert written["extra"]["flow"] == "client_credentials"
        assert written["extra"]["account_id"] == "ACC-ACME"


# ---------------------------------------------------------------------------
# PAT skip — unchanged behavior
# ---------------------------------------------------------------------------


class TestPATSkip:
    @pytest.mark.asyncio
    async def test_pat_file_is_skipped(self, tmp_path):
        """PAT tokens (zero-expiry) are never refreshed even when math
        somehow says expired."""
        f = tmp_path / "default.json"
        _write_token_file(
            f,
            extra={"flow": "personal_access_token"},
            refresh_token="",
        )

        mock_provider = MagicMock()
        mock_provider.exchange_client_credentials = AsyncMock(
            side_effect=AssertionError("PAT must NOT re-exchange"),
        )
        mock_provider.refresh = AsyncMock(
            side_effect=AssertionError("PAT must NOT refresh"),
        )

        with patch(
            "auth.oauth_providers.get_provider", return_value=mock_provider,
        ):
            refreshed = await oauth_refresh_worker._maybe_refresh_token_file(
                token_file=f, username="alice",
                provider_id="github",
            )
        assert refreshed is False
        mock_provider.exchange_client_credentials.assert_not_called()
        mock_provider.refresh.assert_not_called()


# ---------------------------------------------------------------------------
# Standard OAuth refresh — unchanged behavior (regression guard)
# ---------------------------------------------------------------------------


class TestStandardRefresh:
    @pytest.mark.asyncio
    async def test_standard_refresh_uses_provider_refresh(self, tmp_path):
        """Tokens with a refresh_token + no flow marker go through the
        existing provider.refresh path."""
        f = tmp_path / "default.json"
        _write_token_file(
            f,
            extra={"team_id": "T1"},  # no flow marker
            refresh_token="my-refresh-token",
        )

        mock_provider = MagicMock()
        new_ts = TokenSet(
            access_token="new-AT", refresh_token="new-RT",
            expires_in=3600, scope="", token_type="Bearer", raw={},
        )
        mock_provider.refresh = AsyncMock(return_value=new_ts)
        mock_provider.exchange_client_credentials = AsyncMock(
            side_effect=AssertionError("standard refresh must NOT re-exchange"),
        )
        mock_provider.token_url = "https://example.com/token"

        mock_manifest = MagicMock()
        mock_manifest.credentials.oauth = {"token_format": {}}

        with patch(
            "auth.oauth_providers.get_provider", return_value=mock_provider,
        ), patch(
            "services.mcp.mcp_registry.get_mcps_by_provider",
            return_value=[mock_manifest],
        ):
            refreshed = await oauth_refresh_worker._maybe_refresh_token_file(
                token_file=f, username="alice",
                provider_id="slack",
            )
        assert refreshed is True
        mock_provider.refresh.assert_called_once()
        # Confirm we passed the file's refresh_token (re-read inside lock).
        call_kwargs = mock_provider.refresh.call_args.kwargs
        assert call_kwargs["refresh_token"] == "my-refresh-token"
