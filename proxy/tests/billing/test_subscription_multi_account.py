"""Multi-account subscription connect — the exchange endpoint's match rule.

Reconnecting the SAME provider account refreshes its row; connecting a
DIFFERENT account creates a second subscription. The pre-identity code
matched on (owner, layer, provider) alone, so adding a second Anthropic
account silently overwrote the first one's credential (single pill in the
UI, original tokens gone).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from api.auth import claude_oauth as claude_api
from api.auth.claude_oauth import OAuthExchangeRequest


_ACCOUNT_A = {"email_address": "a@example.com", "uuid": "uuid-a"}
_ACCOUNT_B = {"email_address": "b@example.com", "uuid": "uuid-b"}


def _token_response(account):
    return {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_in": 3600,
        "scope": "user:inference",
        "subscriptionType": "max",
        **({"account": account} if account is not None else {}),
    }


def _row(sub_id, oauth_email=""):
    return {
        "id": sub_id,
        "auth_type": "oauth",
        "provider": "anthropic",
        "oauth_email": oauth_email,
        "label": "Claude Max",
    }


def _exchange(account, existing_rows):
    """Drive the exchange endpoint with everything mocked; return the store."""
    store = MagicMock()
    store.list_subscriptions.return_value = existing_rows
    store.add_subscription.return_value = {"id": "new-sub"}
    store.get_subscription.return_value = {"id": "refreshed-sub"}

    user = SimpleNamespace(sub="user-1", role="admin")
    meta = {"user_sub": "user-1", "owner_type": "user", "code_verifier": "ver"}
    req = OAuthExchangeRequest(code="auth-code", state="st-1")

    with patch.object(claude_api, "subscription_store", store), \
         patch.object(claude_api, "_consume_state", return_value=meta), \
         patch.object(claude_api, "require_auth", lambda u: u), \
         patch.object(
             claude_api.claude_oauth, "exchange_code",
             return_value=_token_response(account),
         ):
        asyncio.run(claude_api.oauth_exchange(req, user=user))
    return store


def test_fresh_connect_creates_stamped_row():
    store = _exchange(_ACCOUNT_A, existing_rows=[])
    store.add_subscription.assert_called_once()
    assert store.add_subscription.call_args.kwargs["oauth_email"] == "a@example.com"
    store.update_credential_data.assert_not_called()


def test_same_account_reconnect_refreshes_row():
    store = _exchange(_ACCOUNT_A, existing_rows=[_row("s1", "a@example.com")])
    store.update_credential_data.assert_called_once()
    assert store.update_credential_data.call_args.args[0] == "s1"
    store.add_subscription.assert_not_called()
    # Identity restamped alongside the token refresh.
    assert store.update_subscription.call_args.kwargs["oauth_email"] == "a@example.com"


def test_different_account_creates_second_row():
    # THE bug: this used to refresh s1, clobbering account A's credential.
    store = _exchange(_ACCOUNT_B, existing_rows=[_row("s1", "a@example.com")])
    store.add_subscription.assert_called_once()
    assert store.add_subscription.call_args.kwargs["oauth_email"] == "b@example.com"
    store.update_credential_data.assert_not_called()


def test_legacy_unstamped_row_is_never_adopted():
    # A pre-identity row (oauth_email "") could hold ANY account — guessing
    # is the clobber bug, so a known-identity connect always creates fresh.
    store = _exchange(_ACCOUNT_A, existing_rows=[_row("s1", "")])
    store.add_subscription.assert_called_once()
    store.update_credential_data.assert_not_called()


def test_no_identity_falls_back_to_single_row_refresh():
    # Provider returned no account info — historic reconnect behavior so a
    # token-revocation recovery still works.
    store = _exchange(None, existing_rows=[_row("s1", "")])
    store.update_credential_data.assert_called_once()
    assert store.update_credential_data.call_args.args[0] == "s1"
    store.add_subscription.assert_not_called()


# ── owner-scoped update endpoint (per-account toggles for every role) ───────

def _update(user_role, owner_sub="user-1", **body):
    from api.admin import execution_layers as el
    from api.admin.execution_layers import (
        UpdateSubscriptionRequest, user_update_subscription,
    )
    store = MagicMock()
    store.get_subscription.return_value = {"id": "s1", "owner_sub": owner_sub}
    store.update_subscription.return_value = {"id": "s1", **body}
    user = SimpleNamespace(sub="user-1", role=user_role)
    with patch.object(el, "subscription_store", store), \
         patch.object(el, "require_auth", lambda u: u):
        result = asyncio.run(user_update_subscription(
            "claude-code-cli", "s1", UpdateSubscriptionRequest(**body), user=user,
        ))
    return store, result


def test_any_role_toggles_own_use_personal():
    store, _ = _update("member", use_personal=False)
    assert store.update_subscription.call_args.kwargs["use_personal"] is False


def test_non_admin_cannot_touch_agent_pool():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _update("creator", contribute_platform=True)
    assert e.value.status_code == 403


def test_update_requires_ownership():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _update("member", owner_sub="someone-else", use_personal=False)
    assert e.value.status_code == 404


# ── selection hooks: reconnect fan-out + live-session rebind scheduling ─────

def _exchange_with_pool(account, existing_rows):
    """Like ``_exchange`` but with the subscription pool observed too."""
    store = MagicMock()
    pool = MagicMock()
    store.list_subscriptions.return_value = existing_rows
    store.add_subscription.return_value = {"id": "new-sub"}
    store.get_subscription.return_value = {"id": "refreshed-sub"}

    user = SimpleNamespace(sub="user-1", role="admin")
    meta = {"user_sub": "user-1", "owner_type": "user", "code_verifier": "ver"}
    req = OAuthExchangeRequest(code="auth-code", state="st-1")

    with patch.object(claude_api, "subscription_store", store), \
         patch.object(claude_api, "subscription_pool", pool), \
         patch.object(claude_api, "_consume_state", return_value=meta), \
         patch.object(claude_api, "require_auth", lambda u: u), \
         patch.object(
             claude_api.claude_oauth, "exchange_code",
             return_value=_token_response(account),
         ):
        asyncio.run(claude_api.oauth_exchange(req, user=user))
    return store, pool


def test_reconnect_fans_fresh_token_to_bound_sessions():
    """The exchange rotates the grant outside the rotation chokepoint — bound
    sessions' credential files must receive the fresh token immediately."""
    _, pool = _exchange_with_pool(_ACCOUNT_A, [_row("s1", "a@example.com")])
    pool.fan_out_current_token.assert_called_once_with("s1")
    pool.schedule_rebind.assert_called_once()


def test_fresh_connect_schedules_rebind_only():
    """A newly connected account may be the replacement that sessions stuck on
    a removed subscription are waiting for — but there is no row to fan out."""
    _, pool = _exchange_with_pool(_ACCOUNT_A, existing_rows=[])
    pool.fan_out_current_token.assert_not_called()
    pool.schedule_rebind.assert_called_once()
