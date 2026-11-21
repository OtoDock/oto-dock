"""The pool's decisions as they stand, recorded before the pool was split into pieces.

One table of states and the account each gives, through the pool's public names only, so the same table holds
before and after a move of its code. A change of any row is a change of behaviour, never of structure.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.events import turn_ending
from services.engines import pool_limits, subscription_pool as sp
from tests._pool_store import pool_store

CLAUDE = "claude-code-cli"


def _row(sid, *, layer="direct-llm", auth="api_key", owner="", provider="anthropic"):
    return {"id": sid, "layer": layer, "provider": provider, "auth_type": auth, "owner_sub": owner,
            "is_primary": 0, "status": "active", "active_sessions": 0, "use_personal": True,
            "contribute_platform": True}


@pytest.fixture
def pool():
    sp._session_subscriptions.clear()
    sp._session_scope_keys.clear()
    sp._scope_recent.clear()
    sp._session_binding_ctx.clear()
    sp._throttled_until.clear()
    sp._throttled_hard.clear()
    sp._window_rests.clear()
    sp._scoped_rests.clear()
    with pool_store() as store:
        store.get_credential_data.return_value = {"api_key": "k"}
        store.get_subscription_consumption.return_value = 0.0
        store.list_personal.return_value = []
        store.get_scope_binding.return_value = None
        store.list_scope_bindings.return_value = []
        yield store
    for d in (sp._session_subscriptions, sp._session_scope_keys, sp._scope_recent, sp._session_binding_ctx,
              sp._throttled_until, sp._throttled_hard, sp._window_rests, sp._scoped_rests):
        d.clear()


def test_the_least_consumed_account_goes_first(pool):
    pool.list_platform_pool.return_value = [_row("a"), _row("b"), _row("c")]
    pool.get_subscription_consumption.side_effect = lambda sid, since: {"a": 3.0, "b": 1.0, "c": 2.0}[sid]
    assert sp.acquire_subscription("direct-llm", None, provider="anthropic").subscription_id == "b"


def test_a_resting_account_is_passed_over(pool):
    pool.list_platform_pool.return_value = [_row("a"), _row("b")]
    pool.get_subscription_consumption.side_effect = lambda sid, since: {"a": 0.0, "b": 5.0}[sid]
    sp._throttled_until["a"] = time.time() + 600
    assert sp.acquire_subscription("direct-llm", None, provider="anthropic").subscription_id == "b"


def test_when_every_account_rests_the_one_that_frees_first_is_used(pool):
    pool.list_platform_pool.return_value = [_row("a"), _row("b")]
    sp._throttled_until["a"] = time.time() + 900
    sp._throttled_until["b"] = time.time() + 300
    assert sp.acquire_subscription("direct-llm", None, provider="anthropic").subscription_id == "b"


def test_a_scope_keeps_the_account_its_live_session_holds(pool):
    pool.list_platform_pool.return_value = [_row("a", layer=CLAUDE), _row("b", layer=CLAUDE)]
    pool.get_subscription_consumption.side_effect = lambda sid, since: {"a": 9.0, "b": 0.0}[sid]
    scope = sp.credential_scope_key("", "/agents/x/workspace/.claude")
    sp.bind_session("live-1", "a", layer=CLAUDE, user_sub="", scope_key=scope)
    got = sp.acquire_subscription(CLAUDE, None, sticky_scope=scope)
    assert got.subscription_id == "a"
    other = sp.acquire_subscription(CLAUDE, None, sticky_scope=sp.credential_scope_key("", "/agents/y/workspace/.claude"))
    assert other.subscription_id == "b"


def test_a_models_rest_leaves_the_account_to_other_models(pool):
    pool.list_platform_pool.return_value = [_row("a", layer=CLAUDE), _row("b", layer=CLAUDE)]
    pool.get_subscription_consumption.side_effect = lambda sid, since: {"a": 0.0, "b": 5.0}[sid]
    family = sp._scope_key(CLAUDE, "claude-fable-5-2")
    assert family
    sp._scoped_rests[("a", family)] = (time.time() + 600, time.time())
    assert sp.acquire_subscription(CLAUDE, None, model="claude-fable-5-2").subscription_id == "b"
    assert sp.acquire_subscription(CLAUDE, None, model="claude-opus-5-5").subscription_id == "a"


def test_a_person_never_borrows_an_admin_sign_in(pool):
    pool.list_platform_pool.return_value = [_row("admin-oauth", layer=CLAUDE, auth="oauth", owner="admin"),
                                            _row("admin-key", layer=CLAUDE, auth="api_key", owner="admin")]
    pool.get_user_allow_platform_auth.return_value = True
    assert sp.acquire_subscription(CLAUDE, "member-1").subscription_id == "admin-key"
    pool.list_platform_pool.return_value = [_row("admin-oauth", layer=CLAUDE, auth="oauth", owner="admin")]
    assert sp.acquire_subscription(CLAUDE, "member-1") is None


def test_a_typed_limit_on_a_models_window_rests_the_account_for_that_model_only(pool):
    pool.list_platform_pool.return_value = [_row("a", layer=CLAUDE)]
    sp.bind_session("s-limit", "a", layer=CLAUDE, user_sub="")
    reset = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    ending = turn_ending.TurnEnding(reason=turn_ending.LIMIT, resets_at=reset, window="scoped:fable")
    with patch.object(pool_limits, "schedule_rebalance"):
        sp.rest_after_limit("s-limit", ending, "usage limit reached")
    assert ("a", "fable") in sp._scoped_rests
    assert "a" not in sp._throttled_until
