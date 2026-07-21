"""Tests for the execution layer subscription system.

Covers:
- PKCE generation and auth URL building
- Subscription pool acquisition priority (user > platform)
- Token refresh logic
- Config builder env var injection
- CLI hooks generation
- OAuth API state management
- Subscription store CRUD
"""

import base64
import hashlib
import json
import time
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# PKCE & Auth URL
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_generate_pkce_returns_tuple(self):
        from auth.claude_oauth import generate_pkce

        verifier, challenge = generate_pkce()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)

    def test_verifier_length(self):
        from auth.claude_oauth import generate_pkce

        verifier, _ = generate_pkce()
        # 32 bytes base64url-encoded = 43 chars (no padding)
        assert len(verifier) == 43

    def test_challenge_is_s256_of_verifier(self):
        from auth.claude_oauth import generate_pkce

        verifier, challenge = generate_pkce()
        expected_hash = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(expected_hash).rstrip(b"=").decode()
        assert challenge == expected

    def test_unique_per_call(self):
        from auth.claude_oauth import generate_pkce

        v1, c1 = generate_pkce()
        v2, c2 = generate_pkce()
        assert v1 != v2
        assert c1 != c2

    def test_build_auth_url_contains_required_params(self):
        from auth.claude_oauth import build_auth_url, CLIENT_ID

        url = build_auth_url("test-challenge", "test-state")
        assert "platform.claude.com/oauth/authorize" in url
        assert "response_type=code" in url
        assert f"client_id={CLIENT_ID}" in url
        assert "code_challenge=test-challenge" in url
        assert "code_challenge_method=S256" in url
        assert "state=test-state" in url

    def test_build_auth_url_encodes_redirect_uri(self):
        from auth.claude_oauth import build_auth_url

        url = build_auth_url("c", "s")
        # redirect_uri should be URL-encoded
        assert "redirect_uri=https%3A%2F%2F" in url

    def test_build_auth_url_encodes_scopes(self):
        from auth.claude_oauth import build_auth_url

        url = build_auth_url("c", "s")
        # Scopes should be encoded (spaces as +)
        assert "scope=org%3Acreate_api_key" in url


# ---------------------------------------------------------------------------
# Token exchange (mocked)
# ---------------------------------------------------------------------------


class TestTokenExchange:
    def test_exchange_success(self):
        from auth.claude_oauth import exchange_code

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "sk-ant-oat01-test",
            "refresh_token": "sk-ant-ort01-test",
            "expires_in": 28800,
            "scope": "user:inference user:profile",
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
        }

        with patch("httpx.post", return_value=mock_resp):
            result = exchange_code("auth-code", "verifier")
            assert result["access_token"] == "sk-ant-oat01-test"
            assert result["refresh_token"] == "sk-ant-ort01-test"
            assert result["subscriptionType"] == "max"

    def test_exchange_failure_raises(self):
        from auth.claude_oauth import exchange_code

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"error":{"type":"invalid_grant","message":"Code expired"}}'
        mock_resp.json.return_value = {"error": {"type": "invalid_grant", "message": "Code expired"}}

        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(ValueError, match="Code expired"):
                exchange_code("bad-code", "verifier")

    def test_exchange_sends_correct_headers(self):
        from auth.claude_oauth import exchange_code

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "t"}
        mock_resp.request = MagicMock()
        mock_resp.request.headers = {"content-type": "application/json"}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            exchange_code("code", "verifier")
            call_kwargs = mock_post.call_args
            headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
            assert headers["Content-Type"] == "application/json"

    def test_exchange_sends_json_body(self):
        from auth.claude_oauth import exchange_code

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "t"}
        mock_resp.request = MagicMock()
        mock_resp.request.headers = {"content-type": "application/json"}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            exchange_code("code", "verifier", state="test-state")
            call_kwargs = mock_post.call_args
            # httpx uses content= for pre-serialized body
            content = call_kwargs.kwargs.get("content")
            assert content is not None
            body = json.loads(content)
            assert body["grant_type"] == "authorization_code"
            assert body["state"] == "test-state"


# ---------------------------------------------------------------------------
# Subscription Pool
# ---------------------------------------------------------------------------


class TestDefaultExecutionLayerForCreator:
    """Auto-default engine picked for a new/installed agent.

    Rule: first of (claude-code-cli, codex-cli) connected on BOTH the platform
    pool AND the creator's own account; never direct-llm; fall back to
    claude-code-cli.
    """

    @patch("services.engines.subscription_pool.subscription_store")
    def test_claude_when_both_connected(self, mock_store):
        from services.engines.subscription_pool import default_execution_layer_for_creator
        # Platform + personal both have claude.
        mock_store.list_platform_pool.side_effect = lambda layer: [{"id": "p"}] if layer == "claude-code-cli" else []
        mock_store.list_personal.side_effect = lambda layer, sub: [{"id": "u"}] if layer == "claude-code-cli" else []
        assert default_execution_layer_for_creator("user-1") == "claude-code-cli"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_claude_wins_when_both_engines_available(self, mock_store):
        from services.engines.subscription_pool import default_execution_layer_for_creator
        # claude AND codex fully available → claude (first in priority order).
        mock_store.list_platform_pool.side_effect = lambda layer: [{"id": "p"}]
        mock_store.list_personal.side_effect = lambda layer, sub: [{"id": "u"}]
        assert default_execution_layer_for_creator("user-1") == "claude-code-cli"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_codex_when_claude_missing_personal(self, mock_store):
        from services.engines.subscription_pool import default_execution_layer_for_creator
        # Platform has both; user only has codex personally → claude fails the
        # BOTH test, codex passes.
        mock_store.list_platform_pool.side_effect = lambda layer: [{"id": "p"}]
        mock_store.list_personal.side_effect = lambda layer, sub: [{"id": "u"}] if layer == "codex-cli" else []
        assert default_execution_layer_for_creator("user-1") == "codex-cli"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_codex_when_claude_missing_platform(self, mock_store):
        from services.engines.subscription_pool import default_execution_layer_for_creator
        # User has both personally; platform only pools codex.
        mock_store.list_platform_pool.side_effect = lambda layer: [{"id": "p"}] if layer == "codex-cli" else []
        mock_store.list_personal.side_effect = lambda layer, sub: [{"id": "u"}]
        assert default_execution_layer_for_creator("user-1") == "codex-cli"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_fallback_claude_when_nothing_connected(self, mock_store):
        from services.engines.subscription_pool import default_execution_layer_for_creator
        mock_store.list_platform_pool.return_value = []
        mock_store.list_personal.return_value = []
        assert default_execution_layer_for_creator("user-1") == "claude-code-cli"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_fallback_when_only_platform_connected(self, mock_store):
        from services.engines.subscription_pool import default_execution_layer_for_creator
        # Platform has claude+codex but the creator has NO personal sub → BOTH
        # test fails for every candidate → fall back to claude (documented edge).
        mock_store.list_platform_pool.side_effect = lambda layer: [{"id": "p"}]
        mock_store.list_personal.side_effect = lambda layer, sub: []
        assert default_execution_layer_for_creator("user-1") == "claude-code-cli"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_direct_llm_is_never_queried_or_returned(self, mock_store):
        from services.engines.subscription_pool import default_execution_layer_for_creator
        # Even if direct-llm were "available" everywhere, it's not a candidate.
        mock_store.list_platform_pool.side_effect = lambda layer: [{"id": "p"}] if layer == "direct-llm" else []
        mock_store.list_personal.side_effect = lambda layer, sub: [{"id": "u"}] if layer == "direct-llm" else []
        result = default_execution_layer_for_creator("user-1")
        assert result == "claude-code-cli"  # fallback, not direct-llm
        # direct-llm is never even probed.
        probed_layers = {c.args[0] for c in mock_store.list_platform_pool.call_args_list}
        assert "direct-llm" not in probed_layers


class TestSubscriptionPool:
    """Tests for subscription_pool.acquire_subscription priority logic."""

    def _make_sub(self, **overrides):
        base = {
            "id": "sub-1",
            "layer": "claude-code-cli",
            "provider": "anthropic",
            "auth_type": "api_key",
            "owner_sub": "",
            "is_primary": 1,
            "status": "active",
            "active_sessions": 0,
        }
        base.update(overrides)
        return base

    @patch("services.engines.subscription_pool.subscription_store")
    def test_user_subscription_takes_priority(self, mock_store):
        from services.engines.subscription_pool import acquire_subscription, _session_subscriptions
        _session_subscriptions.clear()

        user_sub = self._make_sub(id="user-sub", owner_sub="user-1")
        mock_store.list_personal.return_value = [user_sub]
        mock_store.list_platform_pool.return_value = []
        mock_store.get_credential_data.return_value = {"api_key": "sk-user"}

        handle = acquire_subscription("claude-code-cli", "user-1")
        assert handle is not None
        assert handle.subscription_id == "user-sub"
        assert handle.api_key == "sk-user"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_platform_pool_when_no_user_sub(self, mock_store):
        from services.engines.subscription_pool import acquire_subscription, _session_subscriptions
        _session_subscriptions.clear()

        # No personal subs; a borrowable (api_key) sub is in the platform pool.
        mock_store.list_personal.return_value = []
        mock_store.list_platform_pool.return_value = [self._make_sub(id="plat-1")]
        mock_store.get_user_allow_platform_auth.return_value = True
        mock_store.get_credential_data.return_value = {"api_key": "sk-plat"}

        handle = acquire_subscription("claude-code-cli", "user-1")
        assert handle is not None
        assert handle.subscription_id == "plat-1"
        mock_store.increment_active_sessions.assert_called_once_with("plat-1")

    @patch("services.engines.subscription_pool.subscription_store")
    def test_platform_denied_when_user_disabled(self, mock_store):
        from services.engines.subscription_pool import acquire_subscription, _session_subscriptions
        _session_subscriptions.clear()

        mock_store.list_personal.return_value = []
        mock_store.get_user_allow_platform_auth.return_value = False

        handle = acquire_subscription("claude-code-cli", "user-1")
        assert handle is None

    @patch("services.engines.subscription_pool.subscription_store")
    def test_none_when_no_active_subs(self, mock_store):
        from services.engines.subscription_pool import acquire_subscription, _session_subscriptions
        _session_subscriptions.clear()

        # Personal empty; platform pool empty (disabled subs aren't returned by it).
        mock_store.list_personal.return_value = []
        mock_store.list_platform_pool.return_value = []
        mock_store.get_user_allow_platform_auth.return_value = True

        handle = acquire_subscription("claude-code-cli", "user-1")
        assert handle is None

    @patch("services.engines.subscription_pool.subscription_store")
    def test_user_cannot_borrow_admin_oauth(self, mock_store):
        """The core ToS guarantee: a user with no personal sub never receives an
        admin OAuth subscription from the pool (only borrowable API types)."""
        from services.engines.subscription_pool import acquire_subscription, _session_subscriptions
        _session_subscriptions.clear()

        mock_store.list_personal.return_value = []
        # Pool holds ONLY an admin OAuth sub — not borrowable.
        mock_store.list_platform_pool.return_value = [
            self._make_sub(id="admin-oauth", auth_type="oauth", owner_sub="admin-1")
        ]
        mock_store.get_user_allow_platform_auth.return_value = True
        mock_store.get_credential_data.return_value = {
            "oauth_token": {"accessToken": "tok", "expiresAt": 0}
        }

        handle = acquire_subscription("claude-code-cli", "user-1")
        assert handle is None
        mock_store.increment_active_sessions.assert_not_called()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_agent_scope_may_use_oauth_pool(self, mock_store):
        """Agent-scope (user_sub None) uses the full pool, OAuth included."""
        from services.engines.subscription_pool import acquire_subscription, _session_subscriptions
        _session_subscriptions.clear()

        future_ms = int((time.time() + 7200) * 1000)
        mock_store.list_platform_pool.return_value = [
            self._make_sub(id="admin-oauth", auth_type="oauth", owner_sub="admin-1")
        ]
        mock_store.get_credential_data.return_value = {
            "oauth_token": {"accessToken": "agent-tok", "expiresAt": future_ms}
        }

        handle = acquire_subscription("claude-code-cli", None)
        assert handle is not None
        assert handle.subscription_id == "admin-oauth"
        assert handle.oauth_access_token == "agent-tok"
        mock_store.list_personal.assert_not_called()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_empty_user_sub_is_agent_scope(self, mock_store):
        """user_sub='' must be treated as agent-scope, never matching owner_sub='' infra."""
        from services.engines.subscription_pool import acquire_subscription, _session_subscriptions
        _session_subscriptions.clear()

        mock_store.list_platform_pool.return_value = []
        handle = acquire_subscription("claude-code-cli", "")
        assert handle is None
        # personal lookup never runs for a blank sub (it's agent-scope)
        mock_store.list_personal.assert_not_called()

    def test_release_decrements(self):
        from services.engines.subscription_pool import bind_session, release_subscription, _session_subscriptions
        _session_subscriptions.clear()

        with patch("services.engines.subscription_pool.subscription_store") as mock_store:
            bind_session("sess-1", "sub-1")
            assert _session_subscriptions["sess-1"] == "sub-1"

            release_subscription("sess-1")
            mock_store.decrement_active_sessions.assert_called_once_with("sub-1")
            assert "sess-1" not in _session_subscriptions

    def test_release_noop_for_unknown_session(self):
        from services.engines.subscription_pool import release_subscription, _session_subscriptions
        _session_subscriptions.clear()

        with patch("services.engines.subscription_pool.subscription_store") as mock_store:
            release_subscription("unknown")
            mock_store.decrement_active_sessions.assert_not_called()


# ---------------------------------------------------------------------------
# Blocked-message reason classification
# ---------------------------------------------------------------------------


class TestUserScopeBlockReason:
    """The reason classification behind the 'no subscription' dashboard message
    (subscription_pool.user_scope_block_reason + NoSubscriptionError)."""

    @patch("services.engines.subscription_pool.subscription_store")
    def test_auth_off(self, mock_store):
        from services.engines.subscription_pool import user_scope_block_reason
        mock_store.list_personal.return_value = []  # no own sub in these scenarios
        mock_store.get_user_allow_platform_auth.return_value = False
        assert user_scope_block_reason("claude-code-cli", "u") == "auth_off"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_no_pool(self, mock_store):
        from services.engines.subscription_pool import user_scope_block_reason
        mock_store.list_personal.return_value = []  # no own sub in these scenarios
        mock_store.get_user_allow_platform_auth.return_value = True
        mock_store.list_platform_pool.return_value = []
        assert user_scope_block_reason("claude-code-cli", "u") == "no_pool"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_admin_oauth_only(self, mock_store):
        from services.engines.subscription_pool import user_scope_block_reason
        mock_store.list_personal.return_value = []  # no own sub in these scenarios
        mock_store.get_user_allow_platform_auth.return_value = True
        mock_store.list_platform_pool.return_value = [{"auth_type": "oauth"}]
        assert user_scope_block_reason("claude-code-cli", "u") == "admin_oauth_only"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_borrowable_present_returns_none(self, mock_store):
        from services.engines.subscription_pool import user_scope_block_reason
        mock_store.list_personal.return_value = []  # no own sub in these scenarios
        mock_store.get_user_allow_platform_auth.return_value = True
        mock_store.list_platform_pool.return_value = [{"auth_type": "api_key"}]
        assert user_scope_block_reason("claude-code-cli", "u") == "none"

    def test_error_carries_reason_and_friendly_message(self):
        from services.engines.subscription_pool import NoSubscriptionError
        for reason in ("auth_off", "admin_oauth_only", "no_pool", "none"):
            err = NoSubscriptionError(reason)
            assert err.reason == reason
            assert "Settings" in str(err)


# ---------------------------------------------------------------------------
# Headroom routing + rate-limit failover
# ---------------------------------------------------------------------------


class TestHeadroomRoutingAndFailover:
    def _sub(self, sid, **o):
        base = {"id": sid, "layer": "direct-llm", "provider": "anthropic",
                "auth_type": "api_key", "owner_sub": "", "is_primary": 0,
                "status": "active", "active_sessions": 0}
        base.update(o)
        return base

    @patch("services.engines.subscription_pool.subscription_store")
    def test_routes_to_least_consumed(self, mock_store):
        from services.engines import subscription_pool as sp
        sp._session_subscriptions.clear(); sp._throttled_until.clear()
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        # sub-a has consumed more this period → sub-b (least consumed) wins.
        mock_store.get_subscription_consumption.side_effect = (
            lambda sid, since: {"sub-a": 9.0, "sub-b": 1.0}[sid]
        )
        handle = sp.acquire_subscription("direct-llm", None, provider="anthropic")
        assert handle.subscription_id == "sub-b"

    @staticmethod
    def _tiered_consumption(by_sub: dict):
        """side_effect for the two-tier key: by_sub maps sid → (5h, 7d). The
        window is identified from the `since` bound itself (the short window's
        start is within the last day; the 7-day one isn't)."""
        from datetime import datetime, timedelta, timezone
        def _consumption(sid, since):
            is_short = (datetime.fromisoformat(since)
                        > datetime.now(timezone.utc) - timedelta(days=1))
            return by_sub[sid][0 if is_short else 1]
        return _consumption

    @patch("services.engines.subscription_pool.subscription_store")
    def test_recent_burn_outranks_weekly_total(self, mock_store):
        """The 5h window is the PRIMARY key: an account hammered in the last
        hours loses to one whose big spend is days old — its provider-side
        rolling window has real headroom again. (The single 7-day key kept
        routing to the recently-hammered account after everyone's reset —
        the live-observed concentration bug.)"""
        from services.engines import subscription_pool as sp
        sp._session_subscriptions.clear(); sp._throttled_until.clear()
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        mock_store.get_subscription_consumption.side_effect = self._tiered_consumption(
            {"sub-a": (5.0, 10.0),   # busy right now, light week
             "sub-b": (0.5, 90.0)})  # heavy week, but idle for hours
        handle = sp.acquire_subscription("direct-llm", None, provider="anthropic")
        assert handle.subscription_id == "sub-b"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_weekly_total_breaks_short_window_ties(self, mock_store):
        # Post-reset (or overnight) every account reads ~0 in the 5h window —
        # the 7-day tier then spreads by weekly-cap headroom.
        from services.engines import subscription_pool as sp
        sp._session_subscriptions.clear(); sp._throttled_until.clear()
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        mock_store.get_subscription_consumption.side_effect = self._tiered_consumption(
            {"sub-a": (0.0, 50.0), "sub-b": (0.0, 2.0)})
        handle = sp.acquire_subscription("direct-llm", None, provider="anthropic")
        assert handle.subscription_id == "sub-b"


# ---------------------------------------------------------------------------
# Scope-sticky selection + persisted bindings
# ---------------------------------------------------------------------------


class TestScopeStickyAndPersistedBindings:
    def _sub(self, sid, **o):
        base = {"id": sid, "layer": "claude-code-cli", "provider": "anthropic",
                "auth_type": "api_key", "owner_sub": "", "is_primary": 0,
                "status": "active", "active_sessions": 0}
        base.update(o)
        return base

    @staticmethod
    def _reset(sp):
        sp._session_subscriptions.clear()
        sp._session_binding_ctx.clear()
        sp._session_scope_keys.clear()
        sp._scope_recent.clear()
        sp._throttled_until.clear()

    def test_credential_scope_key_shape(self):
        from services.engines import subscription_pool as sp
        assert sp.credential_scope_key("local", "/a/.claude") == "local:/a/.claude"
        assert sp.credential_scope_key("", "/a/.claude") == "local:/a/.claude"
        assert sp.credential_scope_key("m-1", "/a/.claude") == "m-1:/a/.claude"
        assert sp.credential_scope_key("local", "") == ""  # no domain, no sticky

    @patch("services.engines.subscription_pool.subscription_store")
    def test_sticky_reuses_live_scope_account_over_headroom(self, mock_store):
        """A scope with a LIVE session on sub-a must reuse sub-a even though
        sub-b has more headroom — two accounts alternating over one shared
        credential file is the last-write-wins / silent-401 hazard."""
        from services.engines import subscription_pool as sp
        self._reset(sp)
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        # Headroom says sub-b; the live scope binding says sub-a.
        mock_store.get_subscription_consumption.side_effect = (
            lambda sid, since: {"sub-a": 9.0, "sub-b": 0.0}[sid]
        )
        sp.bind_session("sess-live", "sub-a", layer="claude-code-cli",
                        user_sub="", scope_key="local:/agents/dev/.claude")
        handle = sp.acquire_subscription(
            "claude-code-cli", None, sticky_scope="local:/agents/dev/.claude")
        assert handle.subscription_id == "sub-a"
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_sticky_ignored_when_pinned_sub_delisted(self, mock_store):
        # The pinned account is no longer a candidate (unticked/deleted): the
        # spawn falls through to the normal headroom pick — never a dead pin.
        from services.engines import subscription_pool as sp
        self._reset(sp)
        mock_store.list_platform_pool.return_value = [self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        mock_store.get_subscription_consumption.side_effect = lambda sid, since: 0.0
        sp.bind_session("sess-live", "sub-gone", layer="claude-code-cli",
                        user_sub="", scope_key="local:/agents/dev/.claude")
        handle = sp.acquire_subscription(
            "claude-code-cli", None, sticky_scope="local:/agents/dev/.claude")
        assert handle.subscription_id == "sub-b"
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_sticky_covers_acquire_to_bind_race(self, mock_store):
        # Spawn A acquired but hasn't bound yet; spawn B in the same scope must
        # still land on A's account (the _scope_recent claim).
        from services.engines import subscription_pool as sp
        self._reset(sp)
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        calls = {"n": 0}
        def _consumption(sid, since):
            # First acquisition (4 calls: 2 candidates × 2 windows) prefers
            # sub-b; if B re-ran headroom it would prefer sub-a — the sticky
            # claim must prevent that flip.
            calls["n"] += 1
            first = {"sub-a": 5.0, "sub-b": 0.0}
            second = {"sub-a": 0.0, "sub-b": 5.0}
            return (first if calls["n"] <= 4 else second)[sid]
        mock_store.get_subscription_consumption.side_effect = _consumption
        h1 = sp.acquire_subscription(
            "claude-code-cli", None, sticky_scope="local:/agents/dev/.claude")
        h2 = sp.acquire_subscription(
            "claude-code-cli", None, sticky_scope="local:/agents/dev/.claude")
        assert h1.subscription_id == h2.subscription_id == "sub-b"
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_sticky_from_persisted_binding_post_restart(self, mock_store):
        # Maps empty (proxy restarted) but the store still holds the scope's
        # binding: a surviving session may hold the file — reuse its account.
        from services.engines import subscription_pool as sp
        self._reset(sp)
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        mock_store.get_subscription_consumption.side_effect = (
            lambda sid, since: {"sub-a": 9.0, "sub-b": 0.0}[sid]
        )
        mock_store.get_scope_binding.return_value = "sub-a"
        handle = sp.acquire_subscription(
            "claude-code-cli", None, sticky_scope="local:/agents/dev/.claude")
        assert handle.subscription_id == "sub-a"
        mock_store.get_scope_binding.assert_called_with("local:/agents/dev/.claude")
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_user_scope_sticky_never_returns_admin_oauth(self, mock_store):
        # The scope's pinned account is an admin OAuth sub (agent-scope session
        # bound it) — a USER-scope borrower must not get it via stickiness;
        # the borrowable filter still governs.
        from services.engines import subscription_pool as sp
        self._reset(sp)
        mock_store.list_personal.return_value = []
        mock_store.get_user_allow_platform_auth.return_value = True
        mock_store.list_platform_pool.return_value = [
            self._sub("sub-oauth", auth_type="oauth"),
            self._sub("sub-key", auth_type="api_key"),
        ]
        mock_store.get_credential_data.return_value = {"api_key": "k", "access_token": "t"}
        mock_store.get_subscription_consumption.side_effect = lambda sid, since: 0.0
        mock_store.get_scope_binding.return_value = "sub-oauth"
        handle = sp.acquire_subscription(
            "claude-code-cli", "user-1", sticky_scope="local:/agents/dev/.claude")
        assert handle.subscription_id == "sub-key"
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_sticky_reuses_throttled_account(self, mock_store):
        # The shared-file constraint dominates a resting account: sticky reuses
        # it (the provider's own error governs) instead of splitting the file.
        from services.engines import subscription_pool as sp
        self._reset(sp)
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        mock_store.get_subscription_consumption.side_effect = lambda sid, since: 0.0
        sp.bind_session("sess-live", "sub-a", layer="claude-code-cli",
                        user_sub="", scope_key="local:/agents/dev/.claude")
        sp._throttled_until["sub-a"] = time.time() + 600
        handle = sp.acquire_subscription(
            "claude-code-cli", None, sticky_scope="local:/agents/dev/.claude")
        assert handle.subscription_id == "sub-a"
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_bind_persists_and_release_deletes(self, mock_store):
        from services.engines import subscription_pool as sp
        self._reset(sp)
        sp.bind_session("sess-p", "sub-x", layer="claude-code-cli",
                        user_sub="u-1", scope_key="local:/a/.claude")
        mock_store.upsert_session_binding.assert_called_once_with(
            "sess-p", "sub-x",
            layer="claude-code-cli", user_sub="u-1", scope_key="local:/a/.claude",
        )
        assert sp._session_scope_keys["sess-p"] == "local:/a/.claude"
        sp.release_subscription("sess-p")
        mock_store.delete_session_binding.assert_called_with("sess-p")
        assert "sess-p" not in sp._session_scope_keys
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_get_session_subscription_reads_through_store(self, mock_store):
        # Post-restart usage attribution: the map is empty but the persisted
        # row still names the account (kills the source_key='default' leak).
        from services.engines import subscription_pool as sp
        self._reset(sp)
        mock_store.get_session_binding.return_value = {
            "session_id": "sess-old", "subscription_id": "sub-z",
            "layer": "claude-code-cli", "user_sub": "", "scope_key": "k",
            "bound_at": "2026-07-10T00:00:00+00:00"}
        assert sp.get_session_subscription("sess-old") == "sub-z"
        mock_store.get_session_binding.return_value = None
        assert sp.get_session_subscription("sess-unknown") is None
        # Store failure → None, never an exception on the usage path.
        mock_store.get_session_binding.side_effect = RuntimeError("db down")
        assert sp.get_session_subscription("sess-err") is None
        self._reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_throttled_sub_skipped(self, mock_store):
        from services.engines import subscription_pool as sp
        sp._session_subscriptions.clear(); sp._throttled_until.clear()
        mock_store.list_platform_pool.return_value = [self._sub("sub-a"), self._sub("sub-b")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        mock_store.get_subscription_consumption.side_effect = lambda sid, since: 0.0
        sp._throttled_until["sub-a"] = time.time() + 600  # sub-a recently hit a limit
        handle = sp.acquire_subscription("direct-llm", None, provider="anthropic")
        assert handle.subscription_id == "sub-b"
        sp._throttled_until.clear()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_mark_throttled_via_session(self, mock_store):
        from services.engines import subscription_pool as sp
        sp._session_subscriptions.clear(); sp._throttled_until.clear()
        sp.bind_session("sess-1", "sub-x")
        sp.mark_subscription_throttled("sess-1", cooldown_s=300)
        assert sp._is_throttled("sub-x") is True
        sp._throttled_until.clear()

    def test_looks_like_limit_error(self):
        from services.engines.subscription_pool import looks_like_limit_error
        assert looks_like_limit_error("Error 429: rate_limit_error")
        assert looks_like_limit_error("You have hit your usage limit")
        assert looks_like_limit_error("Overloaded")
        assert not looks_like_limit_error("connection reset by peer")
        assert not looks_like_limit_error("")

    @patch("services.engines.subscription_pool.subscription_store")
    def test_all_throttled_falls_back_not_blocked(self, mock_store):
        """When every eligible sub is resting, _select de-prioritises rather than
        hard-blocks — a single-account install still gets a handle over a transient
        529 (the provider's own retry then governs). Regression for the 529→15-min
        lockout that surfaced as a misleading 'no subscription'."""
        from services.engines import subscription_pool as sp
        sp._session_subscriptions.clear(); sp._throttled_until.clear()
        mock_store.list_platform_pool.return_value = [self._sub("sub-a")]
        mock_store.get_credential_data.return_value = {"api_key": "k"}
        mock_store.get_subscription_consumption.side_effect = lambda sid, since: 0.0
        sp._throttled_until["sub-a"] = time.time() + 600  # the only sub is resting
        handle = sp.acquire_subscription("direct-llm", None, provider="anthropic")
        assert handle is not None and handle.subscription_id == "sub-a"
        sp._throttled_until.clear()

    def test_throttle_cooldown_classifies_overload_vs_limit(self):
        """A transient 529 overload must get a SHORT cooldown, not the 15-min lockout
        a real rate/usage limit gets — so an immediate retry isn't blocked."""
        from services.engines import subscription_pool as sp
        assert sp.throttle_cooldown_for("API Error: 529 Overloaded") == sp._OVERLOAD_COOLDOWN_S
        assert sp._OVERLOAD_COOLDOWN_S <= 60 < sp._THROTTLE_COOLDOWN_S
        assert sp.throttle_cooldown_for("Error 429: rate_limit_error") == sp._THROTTLE_COOLDOWN_S
        assert sp.throttle_cooldown_for("You have hit your usage limit") == sp._THROTTLE_COOLDOWN_S
        assert sp.throttle_cooldown_for("connection reset by peer") is None
        assert sp.throttle_cooldown_for("") is None

    @patch("services.engines.subscription_pool.subscription_store")
    def test_block_reason_throttled_when_own_sub_resting(self, mock_store):
        """A user who owns a resting sub is told to retry, not to 'connect an account'."""
        from services.engines import subscription_pool as sp
        sp._throttled_until.clear()
        mock_store.list_personal.return_value = [self._sub("sub-own")]
        sp._throttled_until["sub-own"] = time.time() + 60
        assert sp.user_scope_block_reason("claude-code-cli", "user-1") == "throttled"
        assert "try again" in sp.NoSubscriptionError("throttled").args[0].lower()
        sp._throttled_until.clear()


# ---------------------------------------------------------------------------
# OAuth access token extraction + refresh
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    @patch("services.engines.subscription_pool.subscription_store")
    def test_handle_extracts_access_token_from_oauth(self, mock_store):
        from services.engines.subscription_pool import _build_handle

        future_ms = int((time.time() + 28700) * 1000)  # fresh — above the spawn runway
        mock_store.get_credential_data.return_value = {
            "oauth_token": {
                "accessToken": "sk-ant-oat01-valid",
                "refreshToken": "sk-ant-ort01-refresh",
                "expiresAt": future_ms,
            }
        }

        sub = {
            "id": "s1", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = _build_handle(sub)
        assert handle.oauth_access_token == "sk-ant-oat01-valid"
        assert handle.api_key is None

    @patch("services.engines.subscription_pool.subscription_store")
    def test_handle_extracts_api_key(self, mock_store):
        from services.engines.subscription_pool import _build_handle

        mock_store.get_credential_data.return_value = {"api_key": "sk-ant-api-test"}

        sub = {
            "id": "s1", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "api_key", "owner_type": "platform",
        }
        handle = _build_handle(sub)
        assert handle.api_key == "sk-ant-api-test"
        assert handle.oauth_access_token is None

    @patch("httpx.post")
    @patch("services.engines.subscription_pool.subscription_store")
    def test_refresh_called_when_token_expired(self, mock_store, mock_post):
        from services.engines.subscription_pool import _build_handle

        past_ms = int((time.time() - 3600) * 1000)  # 1 hour ago
        mock_store.get_credential_data.return_value = {
            "oauth_token": {
                "accessToken": "sk-ant-oat01-expired",
                "refreshToken": "sk-ant-ort01-refresh",
                "expiresAt": past_ms,
            }
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "sk-ant-ort01-new",
            "expires_in": 28800,
        }
        mock_post.return_value = mock_resp

        sub = {
            "id": "s1", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = _build_handle(sub)
        assert handle.oauth_access_token == "sk-ant-oat01-new"
        mock_store.update_credential_data.assert_called_once()

    @patch("httpx.post")
    @patch("services.engines.subscription_pool.subscription_store")
    def test_refresh_within_5min_buffer(self, mock_store, mock_post):
        from services.engines.subscription_pool import _build_handle

        # Expires in 4 minutes (within 5-min buffer)
        soon_ms = int((time.time() + 240) * 1000)
        mock_store.get_credential_data.return_value = {
            "oauth_token": {
                "accessToken": "sk-ant-oat01-expiring",
                "refreshToken": "sk-ant-ort01-refresh",
                "expiresAt": soon_ms,
            }
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "sk-ant-oat01-refreshed",
            "refresh_token": "sk-ant-ort01-refreshed",
            "expires_in": 28800,
        }
        mock_post.return_value = mock_resp

        sub = {
            "id": "s1", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = _build_handle(sub)
        assert handle.oauth_access_token == "sk-ant-oat01-refreshed"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_no_refresh_when_token_has_full_runway(self, mock_store):
        from services.engines.subscription_pool import _build_handle

        future_ms = int((time.time() + 28700) * 1000)  # above the spawn runway
        mock_store.get_credential_data.return_value = {
            "oauth_token": {
                "accessToken": "sk-ant-oat01-valid",
                "refreshToken": "sk-ant-ort01-refresh",
                "expiresAt": future_ms,
            }
        }

        sub = {
            "id": "s1", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = _build_handle(sub)
        assert handle.oauth_access_token == "sk-ant-oat01-valid"
        mock_store.update_credential_data.assert_not_called()

    @patch("httpx.post")
    @patch("services.engines.subscription_pool.subscription_store")
    def test_spawn_runway_refresh_hours_before_expiry(self, mock_store, mock_post):
        """A token with 2h left is still refreshed at acquire — sessions freeze
        the env token at spawn, so every acquire must hand out full runway."""
        from services.engines import subscription_pool as sp

        sp._refresh_backoff.clear()
        soon_ms = int((time.time() + 7200) * 1000)
        mock_store.get_credential_data.return_value = {
            "oauth_token": {
                "accessToken": "sk-ant-oat01-2h-left",
                "refreshToken": "sk-ant-ort01-refresh",
                "expiresAt": soon_ms,
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "sk-ant-oat01-runway",
            "refresh_token": "sk-ant-ort01-runway",
            "expires_in": 28800,
        }
        mock_post.return_value = mock_resp

        sub = {
            "id": "s-runway", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = sp._build_handle(sub)
        assert handle.oauth_access_token == "sk-ant-oat01-runway"
        mock_store.update_credential_data.assert_called_once()

    @patch("httpx.post")
    @patch("services.engines.subscription_pool.subscription_store")
    def test_refresh_failure_falls_back_to_valid_stored_token(self, mock_store, mock_post):
        """A proactive-refresh failure must not waste a token with hours of
        life left — the stored token is returned and the sub is NOT expired."""
        from services.engines import subscription_pool as sp

        sp._refresh_backoff.clear()
        soon_ms = int((time.time() + 7200) * 1000)
        mock_store.get_credential_data.return_value = {
            "oauth_token": {
                "accessToken": "sk-ant-oat01-2h-left",
                "refreshToken": "sk-ant-ort01-refresh",
                "expiresAt": soon_ms,
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        sub = {
            "id": "s-fallback", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = sp._build_handle(sub)
        assert handle.oauth_access_token == "sk-ant-oat01-2h-left"
        mock_store.update_credential_data.assert_not_called()
        mock_store.update_subscription.assert_not_called()
        assert sp._refresh_backoff["s-fallback"][1] == 1
        sp._refresh_backoff.clear()

    @patch("httpx.post")
    @patch("services.engines.subscription_pool.subscription_store")
    def test_refresh_failure_near_expiry_yields_no_token(self, mock_store, mock_post):
        from services.engines import subscription_pool as sp

        sp._refresh_backoff.clear()
        past_ms = int((time.time() - 60) * 1000)
        mock_store.get_credential_data.return_value = {
            "oauth_token": {
                "accessToken": "sk-ant-oat01-dead",
                "refreshToken": "sk-ant-ort01-refresh",
                "expiresAt": past_ms,
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        sub = {
            "id": "s-dead", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = sp._build_handle(sub)
        assert handle.oauth_access_token is None
        sp._refresh_backoff.clear()

    @patch("httpx.post")
    @patch("services.engines.subscription_pool.subscription_store")
    def test_single_flight_reread_skips_second_refresh(self, mock_store, mock_post):
        """If another acquisition refreshed while we waited on the lock, the
        re-read sees the fresh blob and no second refresh (rotation!) happens."""
        from services.engines import subscription_pool as sp

        sp._refresh_backoff.clear()
        soon_ms = int((time.time() + 7200) * 1000)
        fresh_ms = int((time.time() + 28700) * 1000)
        mock_store.get_credential_data.side_effect = [
            {"oauth_token": {"accessToken": "old", "refreshToken": "r1", "expiresAt": soon_ms}},
            {"oauth_token": {"accessToken": "fresh", "refreshToken": "r2", "expiresAt": fresh_ms}},
        ]

        sub = {
            "id": "s-flight", "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_type": "platform",
        }
        handle = sp._build_handle(sub)
        assert handle.oauth_access_token == "fresh"
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# OAuth API State Management
# ---------------------------------------------------------------------------


class TestOAuthState:
    def test_create_and_consume_state(self):
        from api.auth.claude_oauth import _create_state, _consume_state, _oauth_states
        _oauth_states.clear()

        state = _create_state("user-1", "platform")
        assert state in _oauth_states

        meta = _consume_state(state)
        assert meta is not None
        assert meta["user_sub"] == "user-1"
        assert meta["owner_type"] == "platform"
        assert "code_verifier" in meta
        assert "code_challenge" in meta
        # Consumed — gone
        assert state not in _oauth_states

    def test_consume_returns_none_for_unknown(self):
        from api.auth.claude_oauth import _consume_state, _oauth_states
        _oauth_states.clear()

        assert _consume_state("nonexistent") is None

    def test_consume_returns_none_for_expired(self):
        from api.auth.claude_oauth import _create_state, _consume_state, _oauth_states
        _oauth_states.clear()

        state = _create_state("user-1", "user")
        # Force expire
        _oauth_states[state]["expiry"] = time.monotonic() - 10

        assert _consume_state(state) is None

    def test_one_time_use(self):
        from api.auth.claude_oauth import _create_state, _consume_state, _oauth_states
        _oauth_states.clear()

        state = _create_state("user-1", "platform")
        assert _consume_state(state) is not None
        assert _consume_state(state) is None  # Second consume fails

    def test_pkce_challenge_matches_verifier(self):
        from api.auth.claude_oauth import _create_state, _oauth_states
        _oauth_states.clear()

        state = _create_state("user-1", "platform")
        meta = _oauth_states[state]

        # PKCE S256 (RFC 7636): code_challenge = BASE64URL(SHA256(verifier)).
        # SHA256 is the algorithm the spec mandates here — the verifier is a
        # one-time high-entropy nonce, not a stored password.
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(meta["code_verifier"].encode()).digest()
        ).rstrip(b"=").decode()
        assert meta["code_challenge"] == expected


# ---------------------------------------------------------------------------
# AgentConfig env var injection
# ---------------------------------------------------------------------------


class TestAgentConfigEnvVars:
    def test_no_subscription_credential_field(self):
        from core.execution_layer import AgentConfig
        from dataclasses import fields as dc_fields

        field_names = [f.name for f in dc_fields(AgentConfig)]
        assert "subscription_credential" not in field_names

    def test_subscription_id_field_exists(self):
        from core.execution_layer import AgentConfig

        cfg = AgentConfig(agent_name="test", subscription_id="sub-123")
        assert cfg.subscription_id == "sub-123"

    def test_extra_env_carries_api_key(self):
        from core.execution_layer import AgentConfig

        cfg = AgentConfig(
            agent_name="test",
            extra_env={"ANTHROPIC_API_KEY": "sk-test"},
        )
        assert cfg.extra_env["ANTHROPIC_API_KEY"] == "sk-test"

    def test_extra_env_carries_oauth_creds_blob(self):
        from core.execution_layer import AgentConfig

        cfg = AgentConfig(
            agent_name="test",
            extra_env={"_CLAUDE_CREDS_BLOB": '{"accessToken": "sk-ant-oat01-test"}'},
        )
        assert "sk-ant-oat01-test" in cfg.extra_env["_CLAUDE_CREDS_BLOB"]


# ---------------------------------------------------------------------------
# Selection-change rebinding — live sessions follow the account checkboxes
# ---------------------------------------------------------------------------


class TestSelectionRebind:
    """``rebind_delisted_sessions``: a session bound to a subscription that the
    owner benched (use_personal off), an admin pulled from the pool
    (contribute_platform off / disabled), or that was deleted, is re-homed onto
    the currently-selected account by rewriting its credential file in place —
    the fix for live/interactive sessions staying on a removed account until a
    proxy restart."""

    def _reset(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout as tf
        pool._session_subscriptions.clear()
        pool._session_binding_ctx.clear()
        pool._session_token_expiry.clear()
        pool._issued_token_expiry.clear()
        pool._refresh_backoff.clear()
        pool._throttled_until.clear()
        tf._targets.clear()

    def setup_method(self):
        self._reset()

    def teardown_method(self):
        self._reset()

    def _row(self, sub_id, *, layer="claude-code-cli", auth_type="oauth",
             owner_sub="u1", provider="anthropic"):
        return {
            "id": sub_id,
            "layer": layer,
            "provider": provider,
            "auth_type": auth_type,
            "owner_sub": owner_sub,
            "is_primary": 0,
            "status": "active",
            "active_sessions": 0,
        }

    def _fresh_oauth_cred(self, token):
        expires = int((time.time() + 8 * 3600) * 1000)
        return {
            "oauth_token": {
                "accessToken": token,
                "refreshToken": "rt",
                "expiresAt": expires,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
                "rateLimitTier": "",
            }
        }

    def _bind_claude_session(self, sid, sub_id, scope_sub, host_dir):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout as tf
        pool.bind_session(sid, sub_id, layer="claude-code-cli", user_sub=scope_sub)
        tf.register_session_target(
            sid, tf.CredentialFileTarget(kind="claude", host_dir=str(host_dir)))

    def test_bind_records_ctx_and_release_pops_it(self):
        from services.engines import subscription_pool as pool
        with patch.object(pool, "subscription_store") as store:
            pool.bind_session("s1", "A", layer="claude-code-cli", user_sub="u1")
            assert pool._session_binding_ctx["s1"] == ("claude-code-cli", "u1")
            pool.release_subscription("s1")
            assert "s1" not in pool._session_binding_ctx
            store.decrement_active_sessions.assert_called_once_with("A")

    def test_bind_without_ctx_is_not_tracked(self):
        from services.engines import subscription_pool as pool
        pool.bind_session("s1", "A")  # legacy signature — no acquisition ctx
        assert "s1" not in pool._session_binding_ctx
        # agent-scope is an EXPLICIT empty string, distinct from unstamped
        pool.bind_session("s2", "A", layer="claude-code-cli", user_sub="")
        assert pool._session_binding_ctx["s2"] == ("claude-code-cli", "")

    @patch("services.engines.subscription_pool.subscription_store")
    def test_selection_contains_truth_table(self, store):
        from services.engines.subscription_pool import _selection_contains
        own = self._row("A")
        pool_api = self._row("P", auth_type="api_key", owner_sub="admin-1")
        pool_oauth = self._row("Q", auth_type="oauth", owner_sub="admin-1")

        # personal row present → selected, regardless of platform auth
        store.list_personal.return_value = [own]
        assert _selection_contains("A", "claude-code-cli", "u1") is True

        # not personal + platform auth off → delisted even if pooled
        store.list_personal.return_value = []
        store.get_user_allow_platform_auth.return_value = False
        store.list_platform_pool.return_value = [pool_api]
        assert _selection_contains("P", "claude-code-cli", "u1") is False

        # borrowable API pool credential counts for user scope with auth on
        store.get_user_allow_platform_auth.return_value = True
        assert _selection_contains("P", "claude-code-cli", "u1") is True

        # an admin OAuth pool sub is NEVER part of a user-scope selection
        store.list_platform_pool.return_value = [pool_oauth]
        assert _selection_contains("Q", "claude-code-cli", "u1") is False
        # ... but IS part of the agent-scope selection
        assert _selection_contains("Q", "claude-code-cli", "") is True

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_moves_session_onto_new_personal_account(self, store, tmp_path):
        """The reported bug: bench account A, enable account B — the live
        session's credential file is rewritten with B's token, no restart."""
        from services.engines import subscription_pool as pool
        a = self._row("A")
        b = self._row("B")
        cred_b = self._fresh_oauth_cred("tok-b")
        store.list_personal.return_value = [b]          # A benched, B selected
        store.get_user_allow_platform_auth.return_value = False
        store.get_subscription.return_value = a         # old row still exists
        store.get_credential_data.return_value = cred_b

        self._bind_claude_session("s1", "A", "u1", tmp_path)
        moved = pool.rebind_delisted_sessions(reason="test")

        assert moved == 1
        assert pool.get_session_subscription("s1") == "B"
        written = json.loads((tmp_path / ".credentials.json").read_text())
        assert written["claudeAiOauth"]["accessToken"] == "tok-b"
        assert written["claudeAiOauth"]["refreshToken"] == ""  # pool = sole rotator
        assert pool._session_token_expiry["s1"] == cred_b["oauth_token"]["expiresAt"]
        # Counters: acquire's +1 cancelled, then per-session +B/-A on the ack.
        inc = [c.args[0] for c in store.increment_active_sessions.call_args_list]
        dec = [c.args[0] for c in store.decrement_active_sessions.call_args_list]
        assert inc.count("B") - dec.count("B") == 1
        assert dec.count("A") == 1

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_noop_while_subscription_still_selected(self, store, tmp_path):
        from services.engines import subscription_pool as pool
        store.list_personal.return_value = [self._row("A")]
        self._bind_claude_session("s1", "A", "u1", tmp_path)

        assert pool.rebind_delisted_sessions() == 0
        assert pool.get_session_subscription("s1") == "A"
        assert not (tmp_path / ".credentials.json").exists()
        store.increment_active_sessions.assert_not_called()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_failsoft_without_replacement(self, store, tmp_path):
        """Account deleted before its successor was connected: the session
        keeps its current credentials (nothing to swap to) and the periodic
        pass retries until a replacement appears."""
        from services.engines import subscription_pool as pool
        store.list_personal.return_value = []
        store.get_user_allow_platform_auth.return_value = False
        store.get_subscription.return_value = None      # row deleted
        self._bind_claude_session("s1", "A", "u1", tmp_path)

        assert pool.rebind_delisted_sessions(reason="delete") == 0
        assert pool.get_session_subscription("s1") == "A"
        assert not (tmp_path / ".credentials.json").exists()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_never_borrows_admin_oauth_for_user_scope(self, store, tmp_path):
        """ToS guarantee holds through rebinding: a user-scope session is left
        stuck rather than re-homed onto an admin OAuth pool subscription."""
        from services.engines import subscription_pool as pool
        admin_oauth = self._row("Q", auth_type="oauth", owner_sub="admin-1")
        store.list_personal.return_value = []
        store.get_user_allow_platform_auth.return_value = True
        store.list_platform_pool.return_value = [admin_oauth]
        store.get_subscription.return_value = self._row("A")
        self._bind_claude_session("s1", "A", "u1", tmp_path)

        assert pool.rebind_delisted_sessions() == 0
        assert pool.get_session_subscription("s1") == "A"
        assert not (tmp_path / ".credentials.json").exists()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_agent_scope_follows_platform_pool(self, store, tmp_path):
        """Admin pulls account A out of the agent pool — agent-scope sessions
        re-home onto the remaining pool account (OAuth allowed at agent scope)."""
        from services.engines import subscription_pool as pool
        b = self._row("B", owner_sub="admin-1")
        store.list_platform_pool.return_value = [b]     # A no longer pooled
        store.get_subscription.return_value = self._row("A", owner_sub="admin-1")
        store.get_credential_data.return_value = self._fresh_oauth_cred("tok-pool")
        self._bind_claude_session("s1", "A", "", tmp_path)  # "" = agent scope

        assert pool.rebind_delisted_sessions() == 1
        assert pool.get_session_subscription("s1") == "B"
        written = json.loads((tmp_path / ".credentials.json").read_text())
        assert written["claudeAiOauth"]["accessToken"] == "tok-pool"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_skips_sessions_without_credential_file(self, store):
        """Env-injected credentials (API key) are frozen at exec — nothing to
        rewrite; the session follows the new selection at its next spawn."""
        from services.engines import subscription_pool as pool
        store.list_personal.return_value = [self._row("B")]
        store.get_user_allow_platform_auth.return_value = False
        pool.bind_session("s1", "A", layer="claude-code-cli", user_sub="u1")
        # no register_session_target — API-key sessions never register

        assert pool.rebind_delisted_sessions() == 0
        assert pool.get_session_subscription("s1") == "A"
        store.increment_active_sessions.assert_not_called()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_skips_unstamped_bindings(self, store, tmp_path):
        """No acquisition context recorded — never guess the scope (a wrong
        guess could re-home a user-scope session onto an admin OAuth sub)."""
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout as tf
        store.list_personal.return_value = []
        pool.bind_session("s1", "A")  # legacy bind, no ctx
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(kind="claude", host_dir=str(tmp_path)))

        assert pool.rebind_delisted_sessions() == 0
        assert pool.get_session_subscription("s1") == "A"
        store.list_personal.assert_not_called()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_api_key_replacement_cannot_hot_swap(self, store, tmp_path):
        """The only replacement is an API key — env-frozen, undeliverable to a
        live OAuth-file session. Counters net zero, binding unchanged."""
        from services.engines import subscription_pool as pool
        b = self._row("B", auth_type="api_key")
        store.list_personal.return_value = [b]
        store.get_user_allow_platform_auth.return_value = False
        store.get_subscription.return_value = self._row("A")
        store.get_credential_data.return_value = {"api_key": "sk-b"}
        self._bind_claude_session("s1", "A", "u1", tmp_path)

        assert pool.rebind_delisted_sessions() == 0
        assert pool.get_session_subscription("s1") == "A"
        assert not (tmp_path / ".credentials.json").exists()
        inc = [c.args[0] for c in store.increment_active_sessions.call_args_list]
        dec = [c.args[0] for c in store.decrement_active_sessions.call_args_list]
        assert inc.count("B") == dec.count("B")  # acquire's seat fully undone
        assert dec.count("A") == 0

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_codex_session_rewrites_auth_json(self, store, tmp_path):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout as tf
        b = self._row("B", layer="codex-cli", provider="openai")
        cred = self._fresh_oauth_cred("tok-codex")
        cred["codex_auth_blob"] = {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "old", "refresh_token": "rt",
                       "id_token": "idt", "account_id": "acc-1"},
        }
        store.list_personal.return_value = [b]
        store.get_user_allow_platform_auth.return_value = False
        store.get_subscription.return_value = self._row(
            "A", layer="codex-cli", provider="openai")
        store.get_credential_data.return_value = cred
        pool.bind_session("s1", "A", layer="codex-cli", user_sub="u1")
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(kind="codex", host_dir=str(tmp_path)))

        assert pool.rebind_delisted_sessions() == 1
        assert pool.get_session_subscription("s1") == "B"
        auth = json.loads((tmp_path / "auth.json").read_text())
        assert auth["tokens"]["access_token"] == "tok-codex"
        assert auth["tokens"]["refresh_token"] == ""  # neutralized
        assert auth["tokens"]["account_id"] == "acc-1"

    @patch("services.engines.subscription_pool.subscription_store")
    def test_on_written_guard_ignores_released_sessions(self, store, tmp_path, monkeypatch):
        """A session that closes between the snapshot and the file write must
        not be re-inserted into the binding maps (that would leak a seat)."""
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout as tf
        store.list_personal.return_value = [self._row("B")]
        store.get_user_allow_platform_auth.return_value = False
        store.get_subscription.return_value = self._row("A")
        store.get_credential_data.return_value = self._fresh_oauth_cred("tok-b")
        self._bind_claude_session("s1", "A", "u1", tmp_path)

        def fake_fan_out(sids, *, claude_blob, codex_auth, on_written, **kw):
            with pool._session_maps_lock:  # session dies just before the write
                pool._session_subscriptions.pop("s1")
            for sid in sids:
                on_written(sid)

        monkeypatch.setattr(tf, "fan_out", fake_fan_out)
        assert pool.rebind_delisted_sessions() == 0
        assert pool.get_session_subscription("s1") is None
        dec = [c.args[0] for c in store.decrement_active_sessions.call_args_list]
        assert dec.count("A") == 0  # the swap's -A never ran for a dead session

    @patch("services.engines.subscription_pool.subscription_store")
    def test_rebind_groups_share_one_replacement(self, store, tmp_path):
        """Two sessions of the same scope re-home onto the SAME account (their
        scope-shared credential file must not flap between accounts)."""
        from services.engines import subscription_pool as pool
        store.list_personal.return_value = [self._row("B"), self._row("C")]
        store.get_user_allow_platform_auth.return_value = False
        store.get_subscription.return_value = self._row("A")
        store.get_credential_data.return_value = self._fresh_oauth_cred("tok")
        store.get_subscription_consumption.return_value = 0.0
        self._bind_claude_session("s1", "A", "u1", tmp_path / "d1")
        self._bind_claude_session("s2", "A", "u1", tmp_path / "d2")

        assert pool.rebind_delisted_sessions() == 2
        assert (pool.get_session_subscription("s1")
                == pool.get_session_subscription("s2"))

    def test_schedule_rebind_without_running_loop_is_noop(self):
        from services.engines import subscription_pool as pool
        pool.schedule_rebind("unit-test")  # no loop — must not raise

    def test_rebind_never_raises(self):
        from services.engines import subscription_pool as pool
        with patch.object(pool, "_rebind_delisted_sessions",
                          side_effect=RuntimeError("boom")):
            assert pool.rebind_delisted_sessions() == 0


# ---------------------------------------------------------------------------
# Selection-mutation endpoints trigger a live-session rebind
# ---------------------------------------------------------------------------


class TestSelectionRebindHooks:
    """Every endpoint that mutates the account selection schedules a rebind
    pass so live sessions follow the change without a proxy restart."""

    def _admin(self):
        from types import SimpleNamespace
        return SimpleNamespace(sub="u1", role="admin", is_admin=True)

    def _member(self):
        from types import SimpleNamespace
        return SimpleNamespace(sub="u1", role="member", is_admin=False)

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_user_update_schedules_rebind(self):
        import api.admin.execution_layers as api_mod
        req = api_mod.UpdateSubscriptionRequest(use_personal=False)
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool") as pool_mock:
            store.get_subscription.return_value = {"id": "A", "owner_sub": "u1"}
            store.update_subscription.return_value = {"id": "A"}
            self._run(api_mod.user_update_subscription(
                "claude-code-cli", "A", req, user=self._member()))
            pool_mock.schedule_rebind.assert_called_once()

    def test_user_delete_schedules_rebind(self):
        import api.admin.execution_layers as api_mod
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool") as pool_mock:
            store.get_subscription.return_value = {"id": "A", "owner_sub": "u1"}
            self._run(api_mod.user_delete_subscription(
                "claude-code-cli", "A", user=self._member()))
            pool_mock.schedule_rebind.assert_called_once()

    def test_admin_update_schedules_rebind(self):
        import api.admin.execution_layers as api_mod
        req = api_mod.UpdateSubscriptionRequest(contribute_platform=False)
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool") as pool_mock:
            store.get_subscription.return_value = {"id": "A", "owner_sub": "u1"}
            store.update_subscription.return_value = {"id": "A"}
            self._run(api_mod.admin_update_subscription(
                "claude-code-cli", "A", req, user=self._admin()))
            pool_mock.schedule_rebind.assert_called_once()

    def test_admin_delete_schedules_rebind(self):
        import api.admin.execution_layers as api_mod
        with patch.object(api_mod, "subscription_store") as store, \
             patch.object(api_mod, "subscription_pool") as pool_mock:
            store.get_subscription.return_value = {
                "id": "A", "owner_sub": "u1", "active_sessions": 0,
            }
            store.delete_subscription.return_value = True
            self._run(api_mod.admin_delete_subscription(
                "claude-code-cli", "A", user=self._admin()))
            pool_mock.schedule_rebind.assert_called_once()

    def test_platform_auth_toggle_schedules_rebind(self):
        import api.admin.execution_layers as api_mod
        req = api_mod.SetPlatformAuthRequest(allowed=False)
        with patch.object(api_mod, "subscription_store"), \
             patch.object(api_mod, "subscription_pool") as pool_mock:
            self._run(api_mod.admin_set_platform_auth(
                "u2", req, user=self._admin()))
            pool_mock.schedule_rebind.assert_called_once()


# ---------------------------------------------------------------------------
# Restore binding after proxy restart (Mode C re-adopt)
# ---------------------------------------------------------------------------


class TestRestoreSessionBinding:
    def _clear_maps(self):
        from services.engines import subscription_pool as sp
        with sp._session_maps_lock:
            sp._session_subscriptions.clear()
            sp._session_binding_ctx.clear()
            sp._session_scope_keys.clear()
            sp._session_token_expiry.clear()

    def test_restores_maps_and_seat_from_persisted_row(self, temp_db):
        from services.engines import subscription_pool as sp
        from storage import subscription_store as store

        sub = store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub="u1",
            credential_data={"oauth_token": {"accessToken": "at"}},
        )
        sp.bind_session("sess-r1", sub["id"], layer="claude-code-cli",
                        user_sub="u1", scope_key="local:/x")
        # Simulate restart: in-memory maps die, counters reset.
        self._clear_maps()
        store.reset_active_sessions()

        restored = sp.restore_session_binding("sess-r1")
        assert restored == sub["id"]
        with sp._session_maps_lock:
            assert sp._session_subscriptions["sess-r1"] == sub["id"]
            assert sp._session_binding_ctx["sess-r1"] == ("claude-code-cli", "u1")
            assert sp._session_scope_keys["sess-r1"] == "local:/x"
        row = store.get_subscription(sub["id"])
        assert row["active_sessions"] == 1

    def test_idempotent_when_binding_live(self, temp_db):
        from services.engines import subscription_pool as sp
        from storage import subscription_store as store

        sub = store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub="u1",
            credential_data={},
        )
        sp.bind_session("sess-r2", sub["id"])
        store.reset_active_sessions()
        # Binding already live in memory: no seat re-take, same id back.
        assert sp.restore_session_binding("sess-r2") == sub["id"]
        assert store.get_subscription(sub["id"])["active_sessions"] == 0
        self._clear_maps()

    def test_no_persisted_row_returns_none(self, temp_db):
        from services.engines import subscription_pool as sp
        self._clear_maps()
        assert sp.restore_session_binding("sess-never-bound") is None
