"""Multi-account subscription connect — the exchange endpoint's match rule.

Reconnecting the SAME provider account refreshes its row; connecting a
DIFFERENT account creates a second subscription. The pre-identity code
matched on (owner, layer, provider) alone, so adding a second Anthropic
account silently overwrote the first one's credential (single pill in the
UI, original tokens gone). The response says whether a row was created, the
identity comes from the token, then the profile, and never from a guess.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

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


_NO_PROFILE = object()


def _no_profile_call(_token):
    raise AssertionError("the profile endpoint must not be read for this exchange")


def _exchange(account, existing_rows, extra_token_fields=None, *,
              profile=_NO_PROFILE, blobs=None, add_raises=None):
    """Drive the exchange endpoint with everything mocked; return
    ``(store, result)``. ``profile`` is what the profile endpoint answers
    (``None`` = unreachable); by default reading it is an error, so a test
    that expects no network says so. ``blobs`` maps sub id → credential data
    for the uuid match."""
    store = MagicMock()
    store.list_subscriptions.return_value = existing_rows
    store.add_subscription.return_value = {"id": "new-sub"}
    if add_raises is not None:
        store.add_subscription.side_effect = add_raises
    store.get_subscription.return_value = {"id": "refreshed-sub"}
    store.get_credential_data.side_effect = lambda sid: (blobs or {}).get(sid, {})
    store.SubscriptionExists = claude_api.subscription_store.SubscriptionExists

    user = SimpleNamespace(sub="user-1", role="admin")
    meta = {"user_sub": "user-1", "owner_type": "user", "code_verifier": "ver"}
    req = OAuthExchangeRequest(code="auth-code", state="st-1")

    token_response = {**_token_response(account), **(extra_token_fields or {})}
    fetch = _no_profile_call if profile is _NO_PROFILE else (lambda _t: profile)
    with patch.object(claude_api, "subscription_store", store), \
         patch.object(claude_api.claude_oauth, "fetch_profile", fetch), \
         patch.object(claude_api, "_consume_state", return_value=meta), \
         patch.object(claude_api, "require_human", lambda u: u), \
         patch.object(
             claude_api.claude_oauth, "exchange_code",
             return_value=token_response,
         ):
        result = asyncio.run(claude_api.oauth_exchange(req, user=user))
    store.result = result
    return store


def test_fresh_connect_creates_stamped_row():
    store = _exchange(_ACCOUNT_A, existing_rows=[])
    store.add_subscription.assert_called_once()
    assert store.add_subscription.call_args.kwargs["oauth_email"] == "a@example.com"
    store.update_credential_data.assert_not_called()
    assert store.result["created"] is True
    assert store.result["previous_status"] is None
    blob = store.add_subscription.call_args.kwargs["credential_data"]["oauth_token"]
    assert blob["accountUuid"] == "uuid-a"


def test_same_account_reconnect_refreshes_row():
    store = _exchange(_ACCOUNT_A, existing_rows=[_row("s1", "a@example.com")])
    store.update_credential_data.assert_called_once()
    assert store.update_credential_data.call_args.args[0] == "s1"
    store.add_subscription.assert_not_called()
    # Neither the label nor the identity is restamped on a match: a renamed
    # pill keeps its name.
    assert store.update_subscription.call_args.kwargs == {"status": "active"}
    assert store.result["created"] is False
    assert store.result["previous_status"] is None
    assert store.result["subscription"] == {"id": "refreshed-sub"}


def test_reconnect_reports_the_previous_status():
    store = _exchange(_ACCOUNT_A, existing_rows=[{**_row("s1", "a@example.com"), "status": "active"}])
    assert store.result == {**store.result, "created": False, "previous_status": "active"}


def test_email_is_stored_lower_case_and_matched_case_insensitively():
    store = _exchange({"email_address": "A@Example.com", "uuid": "uuid-a"}, existing_rows=[])
    assert store.add_subscription.call_args.kwargs["oauth_email"] == "a@example.com"
    # A row stamped with another spelling is the same account; its spelling
    # is kept (the unique index is case-sensitive).
    store = _exchange({"email_address": "A@Example.com", "uuid": "uuid-a"},
                      existing_rows=[_row("s1", "a@Example.COM")])
    store.update_credential_data.assert_called_once()
    store.add_subscription.assert_not_called()
    assert "oauth_email" not in store.update_subscription.call_args.kwargs


def test_uuid_in_the_blob_is_a_second_match_key():
    # The token carries only the uuid; the row was stamped with the email
    # but its blob holds the uuid.
    store = _exchange(
        {"uuid": "uuid-a"}, existing_rows=[_row("s1", "a@example.com")],
        blobs={"s1": {"oauth_token": {"accountUuid": "uuid-a"}}},
    )
    store.update_credential_data.assert_called_once()
    assert store.update_credential_data.call_args.args[0] == "s1"
    store.add_subscription.assert_not_called()


def test_identity_from_the_profile_when_the_token_has_none():
    profile = {"account": {"uuid": "uuid-p", "email": "P@example.com",
                           "has_claude_max": True}, "organization": {}}
    store = _exchange(None, existing_rows=[], profile=profile)
    assert store.add_subscription.call_args.kwargs["oauth_email"] == "p@example.com"
    blob = store.add_subscription.call_args.kwargs["credential_data"]["oauth_token"]
    assert blob["accountUuid"] == "uuid-p"
    assert store.result["created"] is True


def test_profile_without_identity_is_refused():
    with pytest.raises(HTTPException) as exc:
        _exchange(None, existing_rows=[_row("s1", "")], profile={"account": {}})
    assert exc.value.status_code == 400
    assert "did not return the account identity" in exc.value.detail


def test_unreachable_profile_is_refused_distinctly():
    with pytest.raises(HTTPException) as exc:
        _exchange(None, existing_rows=[_row("s1", "")], profile=None)
    assert exc.value.status_code == 400
    assert "Could not reach Claude" in exc.value.detail


def test_race_on_insert_is_a_409_naming_the_account():
    exists = claude_api.subscription_store.SubscriptionExists("dup")
    with pytest.raises(HTTPException) as exc:
        _exchange(_ACCOUNT_A, existing_rows=[], add_raises=exists)
    assert exc.value.status_code == 409
    assert "a@example.com" in exc.value.detail


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


def test_no_identity_never_refreshes_the_first_row():
    # No identity anywhere: refusal, not the historic "refresh the first
    # row" (that put a second account's tokens under the first one's name).
    with pytest.raises(HTTPException):
        _exchange(None, existing_rows=[_row("s1", "")], profile={"account": {}})


def test_reconnect_expired_row_revives_to_active():
    row = {**_row("s1", "a@example.com"), "status": "expired"}
    store = _exchange(_ACCOUNT_A, existing_rows=[row])
    store.update_credential_data.assert_called_once()
    assert store.update_subscription.call_args.kwargs["status"] == "active"
    store.add_subscription.assert_not_called()


def test_reconnect_disabled_row_stays_disabled():
    # Reconnect refreshes the credential but must NOT override the admin's
    # disable — and matching the row at all (vs. forking a second ACTIVE one)
    # is the point of the include_disabled match list.
    row = {**_row("s1", "a@example.com"), "status": "disabled"}
    store = _exchange(_ACCOUNT_A, existing_rows=[row])
    store.update_credential_data.assert_called_once()
    assert store.update_subscription.call_args.kwargs["status"] == "disabled"
    store.add_subscription.assert_not_called()


def test_reconnect_clears_refresh_backoff():
    from services.engines import subscription_pool as sp
    sp._refresh_backoff["s1"] = (0.0, 3)
    try:
        _exchange(_ACCOUNT_A, existing_rows=[_row("s1", "a@example.com")])
        assert "s1" not in sp._refresh_backoff
    finally:
        sp._refresh_backoff.pop("s1", None)


def test_exchange_stores_grant_expiry():
    import time as _time
    store = _exchange(
        _ACCOUNT_A, existing_rows=[],
        extra_token_fields={"refresh_token_expires_in": 14 * 86400},
    )
    blob = store.add_subscription.call_args.kwargs["credential_data"]["oauth_token"]
    expected_ms = (_time.time() + 14 * 86400) * 1000
    assert abs(blob["refreshTokenExpiresAt"] - expected_ms) < 10_000


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
         patch.object(claude_api, "require_human", lambda u: u), \
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
