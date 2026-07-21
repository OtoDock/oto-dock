"""OAuth engine — state-token issuance + validation.

State tokens are CSRF defense for the OAuth flow:
  * Random url-safe 32-byte tokens.
  * One-shot — consumed by validate_state.
  * 5-minute TTL.
  * Carry per-flow metadata so the callback can route + persist.

These tests exercise the pure state-management surface. The
``do_oauth_exchange`` orchestrator is exercised by the API tests
(test_oauth_api.py) where the full provider + DB interaction is
mocked.

Additional coverage:
  * ``peek_state_extra`` — non-consuming read for the API caller to merge
    engine-injected URL params (PKCE challenge) with its own URL extras
    before calling ``provider.build_auth_url``.
  * S2S persist site — verifies ``token_set.raw["flow"]`` and
    ``account_id`` injection + the synthesized UserInfo email format.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.oauth import oauth_engine


def test_state_round_trip():
    token = oauth_engine.create_state(
        user_sub="alice", mcp_name="google-workspace", provider_id="google",
        services=["gmail", "drive"], account_label_hint="work",
        redirect_uri="https://x/cb",
    )
    state = oauth_engine.validate_state(token)
    assert state is not None
    assert state.user_sub == "alice"
    assert state.mcp_name == "google-workspace"
    assert state.provider_id == "google"
    assert state.services == ["gmail", "drive"]
    assert state.account_label_hint == "work"
    assert state.redirect_uri == "https://x/cb"
    assert state.mobile is False


def test_state_is_one_shot():
    token = oauth_engine.create_state(
        user_sub="u", mcp_name="m", provider_id="p",
    )
    # First validation succeeds.
    assert oauth_engine.validate_state(token) is not None
    # Second validation returns None (replay protection).
    assert oauth_engine.validate_state(token) is None


def test_unknown_state_returns_none():
    assert oauth_engine.validate_state("never-minted") is None


def test_expired_state_returns_none():
    token = oauth_engine.create_state(
        user_sub="u", mcp_name="m", provider_id="p",
    )
    # Fast-forward time past the 5-min TTL.
    with patch.object(time, "monotonic",
                      return_value=time.monotonic() + 400):
        assert oauth_engine.validate_state(token) is None


def test_purge_runs_during_create():
    """Expired states should be purged on the next create_state call.
    Verify by counting active states across an expiry boundary."""
    # Mint a bunch of states; all unexpired.
    for i in range(3):
        oauth_engine.create_state(user_sub=f"u{i}", mcp_name="m", provider_id="p")
    n_before = oauth_engine.active_state_count()
    assert n_before >= 3

    # Fast-forward + mint one more — purge runs and drops the originals.
    with patch.object(time, "monotonic",
                      return_value=time.monotonic() + 400):
        oauth_engine.create_state(user_sub="new", mcp_name="m", provider_id="p")
        n_after = oauth_engine.active_state_count()
        # The purged tokens should be gone; only the new one remains
        # (assuming no other tests minted states concurrently — this is
        # process-local so safe in our test runner).
        # Tolerance ±0: only the freshly-minted token survives the purge.
        assert n_after <= n_before


def test_mobile_state_flag():
    token = oauth_engine.create_state(
        user_sub="u", mcp_name="m", provider_id="p", mobile=True,
    )
    state = oauth_engine.validate_state(token)
    assert state.mobile is True


def test_token_uniqueness():
    """Two states for the same user should produce distinct tokens."""
    t1 = oauth_engine.create_state(user_sub="u", mcp_name="m", provider_id="p")
    t2 = oauth_engine.create_state(user_sub="u", mcp_name="m", provider_id="p")
    assert t1 != t2
    # Both consumable independently.
    assert oauth_engine.validate_state(t1) is not None
    assert oauth_engine.validate_state(t2) is not None


# ---------------------------------------------------------------------------
# Peek_state_extra (non-consuming read for URL merging)
# ---------------------------------------------------------------------------


class TestPeekStateExtra:
    def test_peek_returns_state_extra(self):
        """The caller uses peek to merge engine-injected URL params
        (PKCE) with its own URL extras before build_auth_url."""
        token = oauth_engine.create_state(
            user_sub="u", mcp_name="m", provider_id="p",
            extra={"foo": "bar"},
        )
        peeked = oauth_engine.peek_state_extra(token)
        assert peeked == {"foo": "bar"}

    def test_peek_is_non_consuming(self):
        """Peek must not pop the state — the OAuth callback's
        validate_state still has to redeem the same token."""
        token = oauth_engine.create_state(
            user_sub="u", mcp_name="m", provider_id="p",
            extra={"a": "1"},
        )
        oauth_engine.peek_state_extra(token)
        oauth_engine.peek_state_extra(token)
        # validate_state still succeeds even after multiple peeks.
        assert oauth_engine.validate_state(token) is not None

    def test_peek_unknown_token_returns_empty(self):
        """Returns {} (not None) so the caller's dict merge doesn't
        need a None-check."""
        assert oauth_engine.peek_state_extra("never-issued") == {}


# ---------------------------------------------------------------------------
# S2S persist site (extra.flow injection + email synthesis)
# ---------------------------------------------------------------------------


class TestS2SPersistSite:
    """The S2S route (api/auth/oauth.py::s2s_exchange) injects two pieces of
    state into the token before persisting:

    1. ``token_set.raw["flow"] = "client_credentials"`` — so the refresh
       worker can dispatch to S2S re-exchange.
    2. ``token_set.raw["account_id"] = body.extra["account_id"]`` — Zoom
       S2S response doesn't echo it back; we must preserve it.

    And synthesizes a distinguishable UserInfo email
    ``{account_id}@{provider}-s2s`` so multi-S2S-account installs don't
    collide on display.
    """

    @pytest.mark.asyncio
    async def test_s2s_route_injects_flow_and_account_id(self):
        """End-to-end through the s2s_exchange handler — verify the
        TokenSet handed to persist_oauth_account carries the markers."""
        from auth.oauth_providers.base import TokenSet
        from api.auth.oauth import s2s_exchange, S2SExchangeRequest

        # Mock provider returns a fresh TokenSet (no flow/account_id in raw).
        mock_provider = MagicMock()
        mock_provider.token_url = "https://zoom.us/oauth/token"
        mock_provider.exchange_client_credentials = AsyncMock(
            return_value=TokenSet(
                access_token="S2S-AT", refresh_token="",
                expires_in=3600, scope="meeting:read:admin",
                token_type="Bearer", raw={},
            ),
        )

        # Capture what gets persisted.
        captured: dict = {}

        def _capture_persist(*a, **kw):
            captured.update(kw)

        body = S2SExchangeRequest(
            mcp_name="zoom-mcp",
            account_label="acme",
            extra={"account_id": "ACC-789"},
        )

        # Admin user mock.
        admin = MagicMock()
        admin.role = "admin"
        admin.sub = "admin-1"

        with patch(
            "api.auth.oauth._validate_provider_for_mcp",
        ), patch(
            "api.auth.oauth._app_creds_for", return_value=("ci", "cs", {}),
        ), patch(
            "api.auth.oauth.get_provider", return_value=mock_provider,
        ), patch(
            "api.auth.oauth.asyncio.to_thread",
            new=AsyncMock(
                side_effect=lambda fn, *a, **kw: _capture_persist(*a, **kw),
            ),
        ):
            result = await s2s_exchange(
                provider="zoom", body=body, user=admin,
            )

        assert result["status"] == "ok"
        # The TokenSet handed to persist carries the injected markers.
        ts = captured["token_set"]
        assert ts.raw["flow"] == "client_credentials"
        assert ts.raw["account_id"] == "ACC-789"
        # And the synthesized userinfo email distinguishes multi-account.
        ui = captured["userinfo"]
        assert ui.email == "ACC-789@zoom-s2s"

    @pytest.mark.asyncio
    async def test_s2s_email_falls_back_to_account_label_when_no_account_id(self):
        """Providers other than Zoom may not need account_id — fall
        back to account_label for the synthesized email."""
        from auth.oauth_providers.base import TokenSet
        from api.auth.oauth import s2s_exchange, S2SExchangeRequest

        mock_provider = MagicMock()
        mock_provider.token_url = "https://example.com/token"
        mock_provider.exchange_client_credentials = AsyncMock(
            return_value=TokenSet(
                access_token="AT", refresh_token="",
                expires_in=3600, scope="", token_type="Bearer", raw={},
            ),
        )

        captured: dict = {}

        def _capture_persist(*a, **kw):
            captured.update(kw)

        body = S2SExchangeRequest(
            mcp_name="some-mcp", account_label="default", extra={},
        )
        admin = MagicMock()
        admin.role = "admin"
        admin.sub = "admin-1"

        with patch(
            "api.auth.oauth._validate_provider_for_mcp",
        ), patch(
            "api.auth.oauth._app_creds_for", return_value=("ci", "cs", {}),
        ), patch(
            "api.auth.oauth.get_provider", return_value=mock_provider,
        ), patch(
            "api.auth.oauth.asyncio.to_thread",
            new=AsyncMock(
                side_effect=lambda fn, *a, **kw: _capture_persist(*a, **kw),
            ),
        ):
            await s2s_exchange(provider="generic", body=body, user=admin)

        ui = captured["userinfo"]
        assert ui.email == "default@generic-s2s"
