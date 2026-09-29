"""The Claude OAuth exchange refuses a grant Claude Code cannot run on.

An Anthropic Console (API) account authorizes the same consent screen, but
the authorization server downgrades the grant: the scopes come back without
``user:inference`` and ``subscriptionType`` reads ``api_individual``. Stored
active, such a row was selected by the pool and every turn died inside the
CLI with "Not logged in" (public issue #3). The exchange now refuses it
before any store write, with a reason the connect forms display.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from api.auth import claude_oauth as claude_api
from api.auth.claude_oauth import OAuthExchangeRequest
from core.layers.cli.oauth import INFERENCE_SCOPE, grant_refusal


# The exact grant from the issue report.
_CONSOLE_SCOPES = ["org:create_api_key", "user:file_upload", "user:profile"]
_SUBSCRIPTION_SCOPES = [
    "org:create_api_key", "user:profile", "user:inference",
    "user:sessions:claude_code", "user:mcp_servers", "user:file_upload",
]


class TestGrantRefusal:
    def test_console_grant_is_refused_with_the_alternatives(self):
        reason = grant_refusal(_CONSOLE_SCOPES, "api_individual")
        assert reason
        assert "Console" in reason
        assert "user:profile" in reason and "api_individual" in reason
        assert "Not logged in" in reason
        assert "API key" in reason

    def test_subscription_grant_passes(self):
        assert grant_refusal(_SUBSCRIPTION_SCOPES, "max") == ""
        assert grant_refusal([INFERENCE_SCOPE, "user:profile"], "pro") == ""

    def test_inference_scope_alone_decides_when_scopes_are_known(self):
        # A subscription-type label never rescues a grant without inference.
        assert grant_refusal(_CONSOLE_SCOPES, "max")
        # And a Console-looking type never condemns a grant that has it.
        assert grant_refusal([INFERENCE_SCOPE], "api_individual") == ""

    def test_no_scopes_trusts_unless_console_type(self):
        assert grant_refusal([], "max") == ""
        assert grant_refusal([], "") == ""
        assert grant_refusal([], "api_individual")
        assert grant_refusal(None, "api_individual")


def _exchange(token_response, existing_rows=()):
    """Drive the exchange endpoint with everything mocked; return the store."""
    store = MagicMock()
    store.list_subscriptions.return_value = list(existing_rows)
    store.add_subscription.return_value = {"id": "new-sub"}
    store.get_subscription.return_value = {"id": "refreshed-sub"}
    pool = MagicMock()
    user = SimpleNamespace(sub="user-1", role="admin")
    meta = {"user_sub": "user-1", "owner_type": "user", "code_verifier": "ver"}
    req = OAuthExchangeRequest(code="auth-code", state="st-1")
    with patch.object(claude_api, "subscription_store", store), \
         patch.object(claude_api, "subscription_pool", pool), \
         patch.object(claude_api, "_consume_state", return_value=meta), \
         patch.object(claude_api, "require_human", lambda u: u), \
         patch.object(claude_api.claude_oauth, "exchange_code",
                      return_value=token_response):
        asyncio.run(claude_api.oauth_exchange(req, user=user))
    return store, pool


def _console_token(account=None):
    return {
        "access_token": "at-console",
        "refresh_token": "",
        "expires_in": 28800,
        "scope": " ".join(_CONSOLE_SCOPES),
        "subscriptionType": "api_individual",
        "rateLimitTier": "auto_api_evaluation",
        "account": account or {"email_address": "dev@example.com", "uuid": "u-1"},
    }


class TestExchangeRefusesConsoleGrant:
    def test_refused_with_400_and_nothing_written(self):
        with pytest.raises(HTTPException) as exc:
            _exchange(_console_token())
        assert exc.value.status_code == 400
        assert "Console" in exc.value.detail

    def test_store_and_pool_untouched(self):
        store = MagicMock()
        store.list_subscriptions.return_value = []
        pool = MagicMock()
        user = SimpleNamespace(sub="user-1", role="admin")
        meta = {"user_sub": "user-1", "owner_type": "user", "code_verifier": "ver"}
        req = OAuthExchangeRequest(code="auth-code", state="st-1")
        with patch.object(claude_api, "subscription_store", store), \
             patch.object(claude_api, "subscription_pool", pool), \
             patch.object(claude_api, "_consume_state", return_value=meta), \
             patch.object(claude_api, "require_human", lambda u: u), \
             patch.object(claude_api.claude_oauth, "exchange_code",
                          return_value=_console_token()):
            with pytest.raises(HTTPException):
                asyncio.run(claude_api.oauth_exchange(req, user=user))
        store.add_subscription.assert_not_called()
        store.update_credential_data.assert_not_called()
        store.update_subscription.assert_not_called()
        pool.schedule_rebind.assert_not_called()

    def test_reconnect_with_downgraded_grant_leaves_good_row_alone(self):
        # The same account already has a good row: a downgraded reconnect
        # must not overwrite its tokens (the refusal fires before matching).
        existing = [{
            "id": "good-row", "auth_type": "oauth", "provider": "anthropic",
            "oauth_email": "dev@example.com", "label": "Claude Max",
            "status": "active",
        }]
        store = MagicMock()
        store.list_subscriptions.return_value = existing
        user = SimpleNamespace(sub="user-1", role="admin")
        meta = {"user_sub": "user-1", "owner_type": "user", "code_verifier": "ver"}
        req = OAuthExchangeRequest(code="auth-code", state="st-1")
        with patch.object(claude_api, "subscription_store", store), \
             patch.object(claude_api, "subscription_pool", MagicMock()), \
             patch.object(claude_api, "_consume_state", return_value=meta), \
             patch.object(claude_api, "require_human", lambda u: u), \
             patch.object(claude_api.claude_oauth, "exchange_code",
                          return_value=_console_token()):
            with pytest.raises(HTTPException):
                asyncio.run(claude_api.oauth_exchange(req, user=user))
        store.update_credential_data.assert_not_called()

    def test_subscription_grant_still_connects(self):
        token = {**_console_token(), "scope": " ".join(_SUBSCRIPTION_SCOPES),
                 "subscriptionType": "max"}
        store, pool = _exchange(token)
        store.add_subscription.assert_called_once()
        pool.schedule_rebind.assert_called_once()
