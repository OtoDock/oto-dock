"""A live session moves to another account only by a means its engine takes.

An engine declares whether it takes another account's sign-in file under a live session
(``AuthProfile.sign_in_in_place``). Claude Code does: it uses a rewritten ``.credentials.json`` at its next call.
Codex 0.166 does not: a live session keeps its token until the vendor refuses it, then refuses an ``auth.json`` of
another account ("signed in to another account"), and the turn fails. So the pool never moves a live session of
such an engine by rewriting its file, and a scope pinned to an account by a live session of it keeps the pin.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.engine_records import AuthProfile
from core.session.session_manager import get_layer_by_path
from services.engines import (
    pool_acquire,
    pool_moves,
    subscription_pool as sp,
    token_fanout as tf,
)
from services.engines.pool_moves import Cause
from tests._pool_store import pool_store

_FAR_MS = int((time.time() + 8 * 3600) * 1000)
_CODEX_SCOPE = "local:/agents/dev/workspace/.codex"
_CLAUDE_SCOPE = "local:/agents/dev/workspace/.claude"


def _reset():
    for d in (sp._session_subscriptions, sp._session_binding_ctx, sp._session_scope_keys, sp._scope_recent,
              sp._session_bound_at,
              sp._session_token_expiry, sp._issued_token_expiry, sp._throttled_until, sp._throttled_hard,
              sp._window_rests, sp._scoped_rests, sp._scope_rebalance_last, sp._refresh_backoff,
              sp._session_moved_at):
        d.clear()


def _row(sid, layer):
    return {"id": sid, "layer": layer, "provider": "openai" if layer == "codex-cli" else "anthropic",
            "auth_type": "oauth", "owner_sub": "", "is_primary": 0, "status": "active", "active_sessions": 0}


def _store(mock_store, layer, *, cons_a=500.0, cons_b=5.0, listed=("sub-a", "sub-b")):
    rows = {"sub-a": _row("sub-a", layer), "sub-b": _row("sub-b", layer)}
    mock_store.list_platform_pool.return_value = [rows[s] for s in listed]
    mock_store.list_personal.return_value = []
    mock_store.get_subscription.side_effect = rows.get
    mock_store.get_subscription_consumption.side_effect = lambda sid, since: {"sub-a": cons_a, "sub-b": cons_b}[sid]
    mock_store.get_credential_data.return_value = {
        "oauth_token": {"accessToken": "tok", "expiresAt": _FAR_MS},
        "codex_auth_blob": {"auth_mode": "chatgpt", "tokens": {"id_token": "idt", "account_id": "acct"}},
    }


def _bind(layer, scope, session_id="sess-1", sub="sub-a"):
    sp._session_subscriptions[session_id] = sub
    sp._session_binding_ctx[session_id] = (layer, "")
    sp._session_scope_keys[session_id] = scope


def _lands(sids, *, layer=None, payload=None, on_written=None, expected_sub_id=None):
    for s in sids:
        on_written(s)


def test_the_engines_declare_whether_they_take_a_sign_in_in_place():
    assert AuthProfile().sign_in_in_place is False
    assert get_layer_by_path("claude-code-cli").capabilities.auth.sign_in_in_place is True
    assert get_layer_by_path("codex-cli").capabilities.auth.sign_in_in_place is False


@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="codex-cli", host_dir="/x"))
@pool_store()
def test_a_codex_scope_is_never_moved_by_a_rewritten_sign_in(mock_store, _target, fan):
    _reset()
    _store(mock_store, "codex-cli")
    _bind("codex-cli", _CODEX_SCOPE)
    sp.mark_subscription_throttled("sess-1")
    with patch.object(pool_moves, "restart_when_idle") as restart, \
            patch("storage.database.get_chat_by_session", return_value={"id": "chat-1", "model": ""}):
        assert sp.rebalance_scopes(reason="test") == 0
    fan.assert_not_called()
    assert sp._session_subscriptions["sess-1"] == "sub-a"
    mock_store.update_session_binding_sub.assert_not_called()
    assert restart.call_args.args[0] == ["chat-1"]


@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_a_claude_scope_still_moves_in_place(mock_store, _target, fan):
    _reset()
    _store(mock_store, "claude-code-cli")
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    sp.mark_subscription_throttled("sess-1")
    assert sp.rebalance_scopes(reason="test") == 1
    assert fan.call_count == 1
    assert sp._session_subscriptions["sess-1"] == "sub-b"


@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="codex-cli", host_dir="/x"))
@pool_store()
def test_a_delisted_codex_account_rewrites_no_sign_in_file(mock_store, _target, fan):
    _reset()
    _store(mock_store, "codex-cli", listed=("sub-b",))
    _bind("codex-cli", _CODEX_SCOPE)
    assert sp.rebind_delisted_sessions(reason="test") == 0
    fan.assert_not_called()
    assert sp._session_subscriptions["sess-1"] == "sub-a"


@pool_store()
def test_a_codex_pin_holds_while_its_account_is_out_for_the_model(mock_store):
    _reset()
    _store(mock_store, "codex-cli", cons_a=0.0, cons_b=0.0)
    _bind("codex-cli", _CODEX_SCOPE)
    sp._throttled_until["sub-a"] = time.time() + 600
    with patch.object(pool_acquire, "_out_for", side_effect=lambda row, model: row["id"] == "sub-a"), \
            patch.object(pool_acquire, "schedule_rebalance") as rebalance:
        got = sp.acquire_subscription("codex-cli", None, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    assert got.subscription_id == "sub-a"
    rebalance.assert_not_called()


@pool_store()
def test_a_claude_pin_still_yields_when_its_account_is_out_for_the_model(mock_store):
    _reset()
    _store(mock_store, "claude-code-cli", cons_a=0.0, cons_b=0.0)
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    sp._throttled_until["sub-a"] = time.time() + 600
    with patch.object(pool_acquire, "_out_for", side_effect=lambda row, model: row["id"] == "sub-a"), \
            patch.object(pool_acquire, "schedule_rebalance") as rebalance:
        got = sp.acquire_subscription("claude-code-cli", None, sticky_scope=_CLAUDE_SCOPE, model="claude-fable-5-2")
    assert got.subscription_id == "sub-b"
    rebalance.assert_called_once()


# ---------------------------------------------------------------------------
# The folder guard: a start never writes its sign-in under live sessions of
# another account when its engine cannot change a sign-in in place
# ---------------------------------------------------------------------------

_PERSON = "local:alice"


def _personal(mock_store, layer):
    """The platform's two accounts and one of the person's own."""
    rows = {"sub-a": _row("sub-a", layer), "sub-b": _row("sub-b", layer),
            "sub-p": {**_row("sub-p", layer), "owner_sub": _PERSON}}
    mock_store.list_platform_pool.return_value = [rows["sub-a"], rows["sub-b"]]
    mock_store.list_personal.return_value = [rows["sub-p"]]
    mock_store.get_subscription.side_effect = rows.get
    mock_store.get_subscription_consumption.return_value = 5.0
    mock_store.get_credential_data.return_value = {
        "oauth_token": {"accessToken": "tok", "expiresAt": _FAR_MS},
        "codex_auth_blob": {"auth_mode": "chatgpt", "tokens": {"id_token": "idt", "account_id": "acct"}},
    }


@pool_store()
def test_a_persons_codex_start_under_the_agents_live_codex_work_is_refused(mock_store):
    _reset()
    _personal(mock_store, "codex-cli")
    _bind("codex-cli", _CODEX_SCOPE)          # the agent's own task, live on sub-a
    with patch.object(pool_acquire, "release_unbound_seat") as released:
        with pytest.raises(sp.NoSubscriptionError) as e:
            sp.acquire_subscription("codex-cli", _PERSON, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    assert e.value.reason == "folder_busy"
    assert "cannot change its sign-in while they run" in str(e.value)
    released.assert_called_once_with("sub-p")


@pool_store()
def test_the_agents_codex_start_under_a_persons_live_lane_is_refused(mock_store):
    _reset()
    _personal(mock_store, "codex-cli")
    _bind("codex-cli", _CODEX_SCOPE, sub="sub-p")
    with patch.object(pool_acquire, "release_unbound_seat"):
        with pytest.raises(sp.NoSubscriptionError):
            sp.acquire_subscription("codex-cli", None, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")


@pool_store()
def test_a_codex_start_with_no_live_session_in_the_folder_goes_ahead(mock_store):
    _reset()
    _personal(mock_store, "codex-cli")
    _bind("codex-cli", "local:/agents/dev/other/.codex")
    got = sp.acquire_subscription("codex-cli", _PERSON, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    assert got.subscription_id == "sub-p"


@pool_store()
def test_a_claude_start_still_takes_the_folder_its_sessions_reread(mock_store):
    _reset()
    _personal(mock_store, "claude-code-cli")
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    got = sp.acquire_subscription("claude-code-cli", _PERSON, sticky_scope=_CLAUDE_SCOPE,
                                  model="claude-fable-5-2")
    assert got.subscription_id == "sub-p"


@pool_store()
def test_a_move_of_a_live_session_is_never_refused_by_the_guard(mock_store):
    _reset()
    _personal(mock_store, "codex-cli")
    _bind("codex-cli", _CODEX_SCOPE)
    got = sp.acquire_subscription("codex-cli", _PERSON, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol",
                                  enforce_caps=False)
    assert got.subscription_id == "sub-p"


@pool_store()
def test_a_live_account_the_vendor_refuses_does_not_hold_the_folder(mock_store):
    _reset()
    _personal(mock_store, "codex-cli")
    _bind("codex-cli", _CODEX_SCOPE)
    sp._throttled_hard.add("sub-a")
    sp._throttled_until["sub-a"] = time.time() + 600
    got = sp.acquire_subscription("codex-cli", _PERSON, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    assert got.subscription_id == "sub-p"


@pool_store()
def test_a_start_in_flight_holds_the_folder(mock_store):
    """The agent's own Codex task has acquired (its claim stands until the
    layer binds) and not yet bound; a person's Codex lane starting in the same
    folder is refused, never written beside it."""
    _reset()
    _personal(mock_store, "codex-cli")
    first = sp.acquire_subscription("codex-cli", None, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    assert first.subscription_id in ("sub-a", "sub-b")
    with patch.object(pool_acquire, "release_unbound_seat"):
        with pytest.raises(sp.NoSubscriptionError) as e:
            sp.acquire_subscription("codex-cli", _PERSON, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    assert e.value.reason == "folder_busy"


@pool_store()
def test_a_start_slower_than_three_minutes_still_holds_the_folder(mock_store):
    """A Codex start on a local model may take three minutes for its start
    command alone: its claim, 181 s old, still holds the folder, and only a
    leak older than the claim's lifetime frees it."""
    from services.engines import pool_state
    _reset()
    _personal(mock_store, "codex-cli")
    first = sp.acquire_subscription("codex-cli", None, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    sub, at = sp._scope_recent[_CODEX_SCOPE]
    assert sub == first.subscription_id
    sp._scope_recent[_CODEX_SCOPE] = (sub, at - 181)
    with patch.object(pool_acquire, "release_unbound_seat"):
        with pytest.raises(sp.NoSubscriptionError):
            sp.acquire_subscription("codex-cli", _PERSON, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")
    assert pool_state._SCOPE_RECENT_TTL_S >= 600


@pool_store()
def test_a_survivor_not_yet_taken_back_after_a_restart_holds_the_folder(mock_store):
    """Just after a platform restart a Codex session of the agent survived on
    its machine (its stored binding names sub-a) and is not taken back yet: a
    person's Codex start in the same folder is refused."""
    from services.engines import pool_bindings
    _reset()
    _personal(mock_store, "codex-cli")
    mock_store.get_scope_binding.return_value = "sub-a"
    with patch.object(pool_bindings, "_BOOT_MONOTONIC", time.monotonic()), \
            patch.object(pool_acquire, "release_unbound_seat"):
        assert pool_bindings._sticky_subscription_id(_CODEX_SCOPE) == "sub-a"
        with pytest.raises(sp.NoSubscriptionError):
            sp.acquire_subscription("codex-cli", _PERSON, sticky_scope=_CODEX_SCOPE, model="gpt-6.1-sol")


# ---------------------------------------------------------------------------
# The causes, the marks and the walk down three accounts
# ---------------------------------------------------------------------------

_WEEK = 7 * 86400


def _reading(*, week_pct=10.0, week_reset_h=72.0, five_pct=5.0, scoped=None):
    now = datetime.now(timezone.utc)
    return {"observed_at": now.isoformat(), "source": "poll",
            "five_hour_pct": five_pct, "five_hour_resets_at": (now + timedelta(hours=2)).isoformat(),
            "seven_day_pct": week_pct, "seven_day_resets_at": (now + timedelta(hours=week_reset_h)).isoformat(),
            "data": {"scoped": scoped or [], "reached": "", "plan": "max"}}


def _three(mock_store, layer="claude-code-cli"):
    rows = {s: _row(s, layer) for s in ("sub-a", "sub-b", "sub-c")}
    mock_store.list_platform_pool.return_value = list(rows.values())
    mock_store.list_personal.return_value = []
    mock_store.get_subscription.side_effect = rows.get
    mock_store.get_subscription_consumption.return_value = 0.0
    mock_store.get_credential_data.return_value = {"oauth_token": {"accessToken": "tok", "expiresAt": _FAR_MS}}
    return rows


def _readings(mock_store, by_sub):
    mock_store.latest_window_samples.side_effect = lambda ids, source=None: {
        sid: by_sub[sid] for sid in ids if sid in by_sub}


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_no_move_between_two_accounts_past_their_mark(mock_store, _target, fan, _on):
    _reset()
    _store(mock_store, "claude-code-cli")
    _readings(mock_store, {"sub-a": _reading(week_pct=98.5), "sub-b": _reading(week_pct=99.0)})
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    assert sp.rebalance_scopes(reason="test") == 0
    fan.assert_not_called()
    assert sp._session_subscriptions["sess-1"] == "sub-a"


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_three_accounts_walk_to_their_last_percent(mock_store, _target, fan, _on):
    """Every account past its mark: the session stays on each until the vendor refuses it, then moves to the next
    that is not refused, the soonest reset first. A reset brings new work back."""
    _reset()
    _three(mock_store)
    readings = {"sub-a": _reading(week_pct=98.5, week_reset_h=10),
                "sub-b": _reading(week_pct=99.0, week_reset_h=20),
                "sub-c": _reading(week_pct=99.5, week_reset_h=30)}
    _readings(mock_store, readings)
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    assert sp.rebalance_scopes(reason="mark") == 0                 # all past the mark: no move
    sp.mark_subscription_throttled("sess-1")                       # sub-a refuses
    assert sp.rebalance_scopes(reason="limit") == 1
    assert sp._session_subscriptions["sess-1"] == "sub-b"          # the soonest reset of the rest
    sp.mark_subscription_throttled("sess-1")                       # sub-b refuses
    assert sp.rebalance_scopes(reason="limit") == 1
    assert sp._session_subscriptions["sess-1"] == "sub-c"
    assert sp.rebalance_scopes(reason="mark") == 0                 # sub-c past its mark, nowhere under one
    # sub-a's week resets: its rest has ended and its reading has room again, so new work goes back to it.
    readings["sub-a"] = _reading(week_pct=1.0, week_reset_h=7 * 24)
    sp._throttled_until["sub-a"] = time.time() - 1
    got = sp.acquire_subscription("claude-code-cli", None, model="claude-opus-5-5")
    assert got.subscription_id == "sub-a"


@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_after_a_refusal_a_past_mark_account_still_serves(mock_store, _target, fan):
    _reset()
    _store(mock_store, "claude-code-cli")
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    sp.mark_subscription_throttled("sess-1")
    with patch("services.engines.subscription_windows.is_enabled", return_value=True):
        _readings(mock_store, {"sub-b": _reading(week_pct=98.5)})
        assert sp.rebalance_scopes(reason="limit") == 1
    assert sp._session_subscriptions["sess-1"] == "sub-b"


@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_a_refusal_never_moves_onto_an_account_refused_for_the_model(mock_store, _target, fan):
    _reset()
    _store(mock_store, "claude-code-cli")
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    sp.mark_subscription_throttled("sess-1")
    sp._throttled_until["sub-b"] = time.time() + 900
    sp._throttled_hard.add("sub-b")
    assert sp.rebalance_scopes(reason="limit") == 0
    fan.assert_not_called()


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_a_user_scope_move_never_lands_on_an_admin_sign_in(mock_store, _target, fan, _on):
    _reset()
    own = _row("own-a", "claude-code-cli") | {"owner_sub": "u-1"}
    admin = _row("admin-b", "claude-code-cli") | {"owner_sub": "admin"}
    mock_store.list_personal.return_value = [own]
    mock_store.list_platform_pool.return_value = [admin]
    mock_store.get_user_allow_platform_auth.return_value = True
    mock_store.get_subscription.side_effect = {"own-a": own, "admin-b": admin}.get
    mock_store.get_subscription_consumption.return_value = 0.0
    mock_store.get_credential_data.return_value = {"oauth_token": {"accessToken": "tok", "expiresAt": _FAR_MS}}
    sp._session_subscriptions["sess-u"] = "own-a"
    sp._session_binding_ctx["sess-u"] = ("claude-code-cli", "u-1")
    sp._session_scope_keys["sess-u"] = _CLAUDE_SCOPE
    sp.mark_subscription_throttled("sess-u")
    assert sp.rebalance_scopes(reason="limit") == 0
    assert sp._session_subscriptions["sess-u"] == "own-a"
    fan.assert_not_called()


# ---------------------------------------------------------------------------
# The return, the model per session, the line
# ---------------------------------------------------------------------------

def _chat(model="", idle_s=0.0, cid="chat-1"):
    at = (datetime.now(timezone.utc) - timedelta(seconds=idle_s)).isoformat()
    return {"id": cid, "model": model, "updated_at": at}


def _no_turn(open_=False):
    from services.remote import chat_move
    return patch.object(chat_move, "open_turn", return_value=open_)


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_a_cold_claude_session_returns_to_the_account_that_resets_first(mock_store, _target, fan, _on):
    _reset()
    _store(mock_store, "claude-code-cli")
    _readings(mock_store, {"sub-a": _reading(week_reset_h=100), "sub-b": _reading(week_reset_h=10)})
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    with patch("storage.database.get_chat_by_session", return_value=_chat(idle_s=600)), _no_turn():
        assert sp.rebalance_scopes(reason="tick") == 0           # warm: never moved for a better account
    with patch("storage.database.get_chat_by_session", return_value=_chat(idle_s=2 * 3600)), _no_turn(), \
            patch.object(pool_moves, "note") as note:
        assert sp.rebalance_scopes(reason="tick") == 1           # cold: back to the account that resets first
    assert sp._session_subscriptions["sess-1"] == "sub-b"
    assert "reset sooner" in note.call_args.args[1]


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@pool_store()
def test_a_cold_codex_session_is_never_closed_to_go_back(mock_store, _on):
    _reset()
    _store(mock_store, "codex-cli")
    _readings(mock_store, {"sub-a": _reading(week_reset_h=100), "sub-b": _reading(week_reset_h=10)})
    _bind("codex-cli", _CODEX_SCOPE)
    with patch("storage.database.get_chat_by_session", return_value=_chat(idle_s=6 * 3600)), \
            patch.object(pool_moves, "restart_when_idle") as restart:
        assert sp.rebalance_scopes(reason="tick") == 0
    restart.assert_not_called()


@pool_store()
def test_a_shared_codex_folder_stays_off_its_mark_until_a_refusal(mock_store):
    _reset()
    _store(mock_store, "codex-cli")
    _bind("codex-cli", _CODEX_SCOPE, "s1")
    _bind("codex-cli", _CODEX_SCOPE, "s2")
    with patch.object(pool_moves, "past_mark", return_value=True), \
            patch.object(pool_moves, "restart_when_idle") as restart, \
            patch("storage.database.get_chat_by_session", side_effect=lambda sid: _chat(cid=f"chat-{sid}")):
        assert sp.rebalance_scopes(reason="mark") == 0
        restart.assert_not_called()
        sp.mark_subscription_throttled("s1")
        sp.rebalance_scopes(reason="limit")
    assert sorted(restart.call_args.args[0]) == ["chat-s1", "chat-s2"]


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_a_group_is_judged_by_each_sessions_model(mock_store, _target, fan, _on):
    """Two chats of one folder on two models: the Fable window of sub-a is full, so the folder leaves it, and only
    for an account whose Fable window has room."""
    _reset()
    _store(mock_store, "claude-code-cli")
    fable_full = [{"key": "fable", "label": "Fable", "pct": 99.0,
                   "resets_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(), "active": True}]
    _readings(mock_store, {"sub-a": _reading(scoped=fable_full), "sub-b": _reading()})
    _bind("claude-code-cli", _CLAUDE_SCOPE, "s-opus")
    _bind("claude-code-cli", _CLAUDE_SCOPE, "s-fable")
    chats = {"s-opus": _chat("claude-opus-5-5", cid="c-opus"), "s-fable": _chat("claude-fable-5-2", cid="c-fable")}
    with patch("storage.database.get_chat_by_session", side_effect=chats.get), \
            patch.object(pool_moves, "note") as note:
        assert sp.rebalance_scopes(reason="mark") == 2
    assert {sp._session_subscriptions[s] for s in chats} == {"sub-b"}
    assert {c.args[0] for c in note.call_args_list} == {"c-opus", "c-fable"}
    assert "close to its Fable limit" in note.call_args.args[1]


def test_a_session_with_no_chat_is_judged_by_its_bound_model():
    _reset()
    sp._session_models["s-call"] = "claude-fable-5-2"
    with patch("storage.database.get_chat_by_session", return_value=None):
        facts = sp._scope_facts(["s-call"])
    assert facts.models == frozenset({"claude-fable-5-2"})
    assert facts.chats == {}
    sp._session_models.clear()


@pool_store()
def test_bind_records_and_release_drops_the_model(mock_store):
    _reset()
    sp.bind_session("s-m", "sub-a", layer="claude-code-cli", user_sub="", scope_key=_CLAUDE_SCOPE,
                    model="claude-opus-5-5")
    assert sp._session_models["s-m"] == "claude-opus-5-5"
    assert mock_store.upsert_session_binding.call_args.kwargs["model"] == "claude-opus-5-5"
    sp.release_subscription("s-m")
    assert "s-m" not in sp._session_models


def test_the_lines_say_what_happened():
    assert pool_moves.line(Cause.LIMIT, "Work Max", "Home Max", "weekly", in_place=True) == (
        "This chat now runs on Home Max. Work Max reached its weekly limit.")
    assert pool_moves.line(Cause.NEAR, "A", "B", "Fable", in_place=True) == (
        "This chat now runs on B. A is close to its Fable limit.")
    assert pool_moves.line(Cause.LIMIT, "A", "", "five-hour", in_place=False) == (
        "A reached its five-hour limit. This chat starts again on another account at its next message.")
    assert pool_moves.line(Cause.NEAR, "", "", "weekly", in_place=True) == (
        "This chat now runs on another account. The account it used is close to its weekly limit.")
    assert pool_moves.line(Cause.DELISTED, "", "", "", in_place=False) == (
        "The account it used is no longer offered to this chat. This chat starts again on another account at its "
        "next message.")


@pool_store()
def test_a_line_names_an_account_only_to_its_owner_and_never_by_its_email(mock_store):
    mock_store.get_subscription.side_effect = {
        "sub-own": {"id": "sub-own", "owner_sub": "alice", "label": "Work Max", "oauth_email": "alice@example.com"},
        "sub-admin": {"id": "sub-admin", "owner_sub": "admin", "label": "Admin Max",
                      "oauth_email": "admin@example.com"},
        "sub-bare": {"id": "sub-bare", "owner_sub": "alice", "label": "", "oauth_email": "alice@example.com"},
    }.get
    assert pool_moves.account_label("sub-own", "alice") == "Work Max"
    assert pool_moves.account_label("sub-admin", "alice") == ""        # someone else's: "another account"
    assert pool_moves.account_label("sub-admin", "") == ""             # an agent's chat, read by its members
    assert pool_moves.account_label("sub-bare", "alice") == ""         # no label: never the email


# ---------------------------------------------------------------------------
# The restart: the terminal's input box, and the reload's idle closer
# ---------------------------------------------------------------------------

class _Terminal:
    def __init__(self):
        self.holds_unsent_text = True
        self.submit_settling = False


def test_a_restart_waits_on_text_typed_in_the_terminal_and_tells_once():
    term = _Terminal()
    now = [1000.0]
    with patch("core.session.interactive_session.find_live_for_chat", return_value=term), \
            patch.object(pool_moves, "note") as note:
        hold = pool_moves.terminal_hold("chat-t", "waits", clock=lambda: now[0])
        assert hold() is True
        now[0] += pool_moves.TERMINAL_HOLD_NOTICE_S - 1
        assert hold() is True
        note.assert_not_called()
        now[0] += 2
        assert hold() is True
        now[0] += 3600
        assert hold() is True
        note.assert_called_once_with("chat-t", "waits", waits=True)
        term.holds_unsent_text = False
        assert hold() is False


def test_a_dashboard_draft_is_never_read():
    with patch("core.session.interactive_session.find_live_for_chat", return_value=None):
        assert pool_moves.terminal_hold("chat-h", "waits")() is False


def test_the_idle_close_waits_on_the_hold_then_writes_the_line():
    from core.session import chat_rebuild, session_state
    from services.remote import chat_move, chat_reload
    holds = [True, True, False]
    closed: list[str] = []
    after: list[str] = []

    async def _close(chat, **kw):
        closed.append(kw["reason"])
        return True

    async def _after():
        after.append("line")

    async def _run():
        with patch("storage.database.get_chat", return_value={"id": "c-1", "session_id": "s-1"}), \
                patch.object(session_state, "session_is_held", return_value=True), \
                patch.object(chat_move, "open_turn", return_value=False), \
                patch.object(chat_move, "TURN_POLL_S", 0.0), \
                patch.object(chat_rebuild, "close_chat_session", side_effect=_close):
            await chat_reload._close_when_idle("c-1", reason="account", hold=lambda: holds.pop(0),
                                               on_close=_after)

    asyncio.run(_run())
    assert closed == ["account"]
    assert after == ["line"]
    assert holds == []


# ---------------------------------------------------------------------------
# The fan-out's lock per folder
# ---------------------------------------------------------------------------

def test_a_rotation_never_writes_over_a_move_that_landed_first(tmp_path):
    """A move to sub-b and a rotation of sub-a reach one folder at once: the move holds the folder's lock across its
    write and its binding swap, so the rotation, taken next, sees the new binding and writes nothing."""
    _reset()
    sp._session_subscriptions["s-f"] = "sub-a"
    tf.register_session_target("s-f", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path)))
    started, release = threading.Event(), threading.Event()
    writes: list[str] = []
    real_write = tf.write_credential_file

    def _slow_write(path, name, payload):
        writes.append(payload["claudeAiOauth"]["accessToken"])
        if payload["claudeAiOauth"]["accessToken"] == "move-b":
            started.set()
            release.wait(5)
        real_write(path, name, payload)

    def _swap(sid):
        sp._session_subscriptions[sid] = "sub-b"

    def _payload(tok):
        return {"claudeAiOauth": {"accessToken": tok, "refreshToken": "", "expiresAt": _FAR_MS}}

    try:
        with patch.object(tf, "write_credential_file", side_effect=_slow_write):
            mover = threading.Thread(target=tf.fan_out, args=(["s-f"],), kwargs={
                "layer": "claude-code-cli", "payload": _payload("move-b"), "on_written": _swap,
                "expected_sub_id": "sub-a"})
            mover.start()
            assert started.wait(5)
            rotator = threading.Thread(target=tf.fan_out, args=(["s-f"],), kwargs={
                "layer": "claude-code-cli", "payload": _payload("rotated-a"), "on_written": lambda sid: None,
                "expected_sub_id": "sub-a"})
            rotator.start()
            release.set()
            mover.join(5)
            rotator.join(5)
        assert writes == ["move-b"]
    finally:
        tf.unregister_session_target("s-f")
        _reset()


def test_a_refused_account_is_left_out_of_the_replacement():
    _reset()
    sp._throttled_until["sub-b"] = time.time() + 900
    sp._throttled_hard.add("sub-b")
    rows = [_row("sub-a", "claude-code-cli"), _row("sub-b", "claude-code-cli"), _row("sub-c", "claude-code-cli")]
    out = pool_moves.excluded(Cause.LIMIT, "sub-a", rows, "claude-code-cli", frozenset({""}))
    assert out == frozenset({"sub-a", "sub-b"})
    assert pool_moves.excluded(Cause.NEAR, "sub-a", rows, "claude-code-cli", frozenset({""})) == frozenset({"sub-a"})
    _reset()


def test_a_refused_account_is_never_accepted():
    _reset()
    sp._throttled_until["sub-b"] = time.time() + 900
    sp._throttled_hard.add("sub-b")
    row = _row("sub-b", "claude-code-cli")
    assert pool_moves.accepts(Cause.LIMIT, row, frozenset({""})) is False
    assert pool_moves.accepts(Cause.DELISTED, row, frozenset({""})) is False
    assert pool_moves.accepts(Cause.LIMIT, _row("sub-c", "claude-code-cli"), frozenset({""})) is True
    _reset()


# ---------------------------------------------------------------------------
# The return waits for a closed turn and a known, sooner reset; one closer per chat
# ---------------------------------------------------------------------------

def _cold_return_state(mock_store, readings):
    _reset()
    _store(mock_store, "claude-code-cli")
    _readings(mock_store, readings)
    _bind("claude-code-cli", _CLAUDE_SCOPE)


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_the_return_never_moves_a_session_whose_turn_is_open(mock_store, _target, fan, _on):
    _cold_return_state(mock_store, {"sub-a": _reading(week_reset_h=100), "sub-b": _reading(week_reset_h=10)})
    with patch("storage.database.get_chat_by_session", return_value=_chat(idle_s=2 * 3600)), _no_turn(True):
        assert sp.rebalance_scopes(reason="tick") == 0
    fan.assert_not_called()
    assert sp._session_subscriptions["sess-1"] == "sub-a"


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_the_return_waits_while_a_session_without_a_chat_shares_the_folder(mock_store, _target, fan, _on):
    _cold_return_state(mock_store, {"sub-a": _reading(week_reset_h=100), "sub-b": _reading(week_reset_h=10)})
    _bind("claude-code-cli", _CLAUDE_SCOPE, session_id="sess-call")
    chats = {"sess-1": _chat(idle_s=2 * 3600)}
    with patch("storage.database.get_chat_by_session", side_effect=chats.get), _no_turn():
        assert sp.rebalance_scopes(reason="tick") == 0
    fan.assert_not_called()


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_the_return_never_follows_spending_alone(mock_store, _target, fan, _on):
    # No reading of either account: the pick falls to the spend, which never moves a live session.
    _cold_return_state(mock_store, {})
    with patch("storage.database.get_chat_by_session", return_value=_chat(idle_s=2 * 3600)), _no_turn():
        assert sp.rebalance_scopes(reason="tick") == 0
    # Only the new account is read: the old one's reset is unknown, so neither is "sooner".
    _readings(mock_store, {"sub-b": _reading(week_reset_h=10)})
    with patch("storage.database.get_chat_by_session", return_value=_chat(idle_s=2 * 3600)), _no_turn():
        assert sp.rebalance_scopes(reason="tick") == 0
    # The same reset on both: no account resets sooner.
    same = _reading(week_reset_h=50)
    _readings(mock_store, {"sub-a": same, "sub-b": dict(same)})
    sp._scope_rebalance_last.clear()
    with patch("storage.database.get_chat_by_session", return_value=_chat(idle_s=2 * 3600)), _no_turn():
        assert sp.rebalance_scopes(reason="tick") == 0
    fan.assert_not_called()
    assert sp._session_subscriptions["sess-1"] == "sub-a"


def test_one_closer_waits_per_chat_and_the_notice_comes_once():
    """Each pass that finds the group again asks for its close: the closer already waiting keeps its hold, so the
    terminal's clock runs on and the person is told once, at a refusal too."""
    from core.session import session_state
    from services.remote import chat_move, chat_reload
    term = _Terminal()
    told: list[str] = []

    async def _run():
        def loop_note(cid, msg, waits=False):
            told.append((msg, waits))
        with patch("storage.database.get_chat", return_value={"id": "c-9", "session_id": "s-9"}), \
                patch.object(session_state, "session_is_held", return_value=True), \
                patch.object(chat_move, "open_turn", return_value=False), \
                patch.object(chat_move, "TURN_POLL_S", 0.01), \
                patch("core.session.interactive_session.find_live_for_chat", return_value=term), \
                patch.object(pool_moves, "TERMINAL_HOLD_NOTICE_S", 0.2), \
                patch.object(pool_moves, "note", side_effect=loop_note):
            for _ in range(7):
                pool_moves.restart_when_idle(["c-9"], "line")
                await asyncio.sleep(0.1)
            assert chat_reload.close_waiting("c-9", "account")
            # A waiting close for another reason is replaced by the pool's.
            chat_reload._idle_closes.pop("c-9").cancel()
            chat_reload.close_when_idle("c-9", reason="team-account")
            assert not chat_reload.close_waiting("c-9", "account")
            pool_moves.restart_when_idle(["c-9"], "line")
            assert chat_reload.close_waiting("c-9", "account")
            chat_reload._idle_closes.pop("c-9").cancel()
            await asyncio.sleep(0)

    asyncio.run(_run())
    assert told == [("This chat moves to another account when the text in its terminal is sent or cleared.", True)]


def test_a_terminal_attached_through_the_command_holds_its_text():
    from core.session.interactive_queue import InteractiveQueueMixin
    term = type("_Attached", (InteractiveQueueMixin,), {})()
    term._composer_dirty = False
    term.otodock_attached = True
    assert term.holds_unsent_text is True
    term.otodock_attached = False
    assert term.holds_unsent_text is False
    term._composer_dirty = True
    assert term.holds_unsent_text is True
    with patch("core.session.interactive_session.find_live_for_chat", return_value=term):
        assert pool_moves.terminal_hold("chat-a", "waits")() is True


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_a_mark_move_never_lands_on_an_account_the_pool_has_not_read(mock_store, _target, fan, _on):
    _reset()
    _store(mock_store, "claude-code-cli")
    _readings(mock_store, {"sub-a": _reading(week_pct=98.5)})
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    with patch("storage.database.get_chat_by_session", return_value=_chat()), _no_turn():
        assert sp.rebalance_scopes(reason="tick") == 0
    fan.assert_not_called()
    # At a refusal, an account with no reading serves.
    sp.mark_subscription_throttled("sess-1")
    with patch("storage.database.get_chat_by_session", return_value=_chat()), _no_turn():
        assert sp.rebalance_scopes(reason="limit") == 1
    assert sp._session_subscriptions["sess-1"] == "sub-b"


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_two_chats_with_homes_of_their_own_move_apart(mock_store, _target, fan, _on):
    """The same two chats, each in a home of its own in that folder (``scope_key_for``): sub-a is past its Fable
    mark, so the Fable chat leaves it alone and the Opus chat keeps its account."""
    _reset()
    _store(mock_store, "claude-code-cli")
    fable_full = [{"key": "fable", "label": "Fable", "pct": 99.0,
                   "resets_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(), "active": True}]
    _readings(mock_store, {"sub-a": _reading(scoped=fable_full), "sub-b": _reading()})
    store = _CLAUDE_SCOPE.split(":", 1)[1]
    for sid in ("s-opus", "s-fable"):
        _bind("claude-code-cli", sp.scope_key_for("local", store, f"{store}/own/{sid}"), sid)
    chats = {"s-opus": _chat("claude-opus-5-5", cid="c-opus"), "s-fable": _chat("claude-fable-5-2", cid="c-fable")}
    with patch("storage.database.get_chat_by_session", side_effect=chats.get), _no_turn(), \
            patch.object(pool_moves, "note") as note:
        assert sp.rebalance_scopes(reason="mark") == 1
    assert (sp._session_subscriptions["s-fable"], sp._session_subscriptions["s-opus"]) == ("sub-b", "sub-a")
    assert [c.args[0] for c in note.call_args_list] == ["c-fable"]


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_a_group_of_two_models_finds_the_account_that_serves_both(mock_store, _target, fan, _on):
    """A Fable chat and an Opus chat share a folder on sub-a, past its Fable mark. sub-b resets first but is past
    its Opus mark; sub-c serves both. The group moves to sub-c."""
    _reset()
    _three(mock_store)

    def fam(key, pct):
        reset = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        return {"key": key, "label": key.capitalize(), "pct": pct, "resets_at": reset, "active": False}
    _readings(mock_store, {"sub-a": _reading(week_reset_h=50, scoped=[fam("fable", 98.5)]),
                           "sub-b": _reading(week_reset_h=10, scoped=[fam("opus", 99.0)]),
                           "sub-c": _reading(week_reset_h=90)})
    _bind("claude-code-cli", _CLAUDE_SCOPE, session_id="s-fable")
    _bind("claude-code-cli", _CLAUDE_SCOPE, session_id="s-opus")
    chats = {"s-fable": _chat(model="claude-fable-5-2", cid="c-f"), "s-opus": _chat(model="claude-opus-5-5", cid="c-o")}
    with patch("storage.database.get_chat_by_session", side_effect=chats.get), _no_turn(), \
            patch.object(pool_moves, "note"):
        assert sp.rebalance_scopes(reason="tick") == 2
    assert sp._session_subscriptions["s-fable"] == sp._session_subscriptions["s-opus"] == "sub-c"


def test_a_codex_folder_never_takes_the_refused_account_back_over_its_newcomer(tmp_path):
    """After a refusal, chat X restarted on sub-b in the folder where chat Y still waits on the refused sub-a (its
    terminal holds text). A rotation of sub-a writes nothing there: the folder carries the newer account, and the
    sessions left on sub-a are being closed. Claude takes any account in place, so its folder is written."""
    _reset()
    for layer, written in (("codex-cli", []), ("claude-code-cli", ["s-y"])):
        folder = tmp_path / layer
        folder.mkdir()
        sp._session_subscriptions.update({"s-y": "sub-a", "s-x": "sub-b"})
        sp._session_bound_at.update({"s-y": 1000.0, "s-x": 2000.0})
        for sid in ("s-y", "s-x"):
            tf.register_session_target(sid, tf.CredentialFileTarget(layer=layer, host_dir=str(folder)))
        got: list[str] = []
        payload = {"claudeAiOauth": {"accessToken": "rotated-a", "refreshToken": "", "expiresAt": _FAR_MS},
                   "tokens": {"access_token": "rotated-a"}, "auth_mode": "chatgpt"}
        try:
            tf.fan_out(["s-y"], layer=layer, payload=payload, on_written=got.append, expected_sub_id="sub-a")
        finally:
            for sid in ("s-y", "s-x"):
                tf.unregister_session_target(sid)
        assert got == written, layer
        assert (folder / tf.credential_file_spec(layer).filename).exists() == bool(written), layer
    # With no neighbour on another account, the rotation lands as before.
    folder = tmp_path / "alone"
    folder.mkdir()
    tf.register_session_target("s-y", tf.CredentialFileTarget(layer="codex-cli", host_dir=str(folder)))
    got = []
    try:
        tf.fan_out(["s-y"], layer="codex-cli", payload={"tokens": {"access_token": "t"}, "auth_mode": "chatgpt"},
                   on_written=got.append, expected_sub_id="sub-a")
    finally:
        tf.unregister_session_target("s-y")
        _reset()
    assert got == ["s-y"]


def test_a_folders_lock_goes_with_its_last_session(tmp_path):
    tf.register_session_target("s-1", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path)))
    tf.register_session_target("s-2", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path)))
    tf._dir_lock(str(tmp_path))
    tf._push_locks[("m-1", "dev", "users/a/.claude")] = asyncio.Lock()
    tf.register_session_target("s-3", tf.CredentialFileTarget(layer="claude-code-cli", machine_id="m-1",
                                                               agent_name="dev", dir_relative="users/a/.claude"))
    tf.unregister_session_target("s-1")
    assert str(tmp_path) in tf._dir_locks
    tf.unregister_session_target("s-2")
    assert str(tmp_path) not in tf._dir_locks
    tf.unregister_session_target("s-3")
    assert ("m-1", "dev", "users/a/.claude") not in tf._push_locks


# ---------------------------------------------------------------------------
# The narrow pass: the folder rule by bind instant, the hold at the Enter, refusals by reading
# ---------------------------------------------------------------------------

def test_the_newcomers_own_rotation_is_written(tmp_path):
    """The folder rule counts only a neighbour bound later than the sessions written: the newcomer's own
    account rotates into its folder while the old neighbour still waits on the refused one."""
    _reset()
    folder = tmp_path / "codex"
    folder.mkdir()
    sp._session_subscriptions.update({"s-y": "sub-a", "s-x": "sub-b"})
    sp._session_bound_at.update({"s-y": 1000.0, "s-x": 2000.0})
    for sid in ("s-y", "s-x"):
        tf.register_session_target(sid, tf.CredentialFileTarget(layer="codex-cli", host_dir=str(folder)))
    got: list[str] = []
    try:
        tf.fan_out(["s-x"], layer="codex-cli", payload={"tokens": {"access_token": "rotated-b"}, "auth_mode": "chatgpt"},
                   on_written=got.append, expected_sub_id="sub-b")
    finally:
        for sid in ("s-y", "s-x"):
            tf.unregister_session_target(sid)
        _reset()
    assert got == ["s-x"]


@pool_store()
def test_a_start_in_a_folder_with_a_newcomer_takes_the_newcomers_account(mock_store):
    _reset()
    _store(mock_store, "codex-cli")
    _bind("codex-cli", _CODEX_SCOPE, session_id="s-y", sub="sub-a")
    _bind("codex-cli", _CODEX_SCOPE, session_id="s-x", sub="sub-b")
    sp._session_bound_at.update({"s-y": 1000.0, "s-x": 2000.0})
    got = sp.acquire_subscription("codex-cli", None, sticky_scope=_CODEX_SCOPE, model="")
    _reset()
    assert got.subscription_id == "sub-b"


def test_a_bind_and_a_move_stamp_the_instant_and_a_release_drops_it():
    _reset()
    with patch("services.engines.pool_bindings._queue_write"):
        sp.bind_session("s-b", "sub-a", layer="codex-cli", user_sub="", scope_key=_CODEX_SCOPE)
        first = sp._session_bound_at["s-b"]
        assert first > 0
        sp.release_subscription("s-b")
    assert "s-b" not in sp._session_bound_at
    _reset()


def test_the_close_waits_for_the_turn_the_enter_opens():
    """The Enter clears the input box at once and the turn it opens is known a tail later: the hold keeps the
    terminal through that gap (``submit_settling``), so it is never closed over the message just sent."""
    from core.session import chat_rebuild, session_state
    from services.remote import chat_move, chat_reload

    class _Term:
        holds_unsent_text = True
        submit_settling = False
        turn_open = False

    term = _Term()
    closed: list[float] = []

    async def _close(chat, **kw):
        closed.append(time.monotonic())
        return True

    async def _run():
        with patch("storage.database.get_chat", return_value={"id": "c-3", "session_id": "s-3"}), \
                patch.object(session_state, "session_is_held", return_value=True), \
                patch.object(chat_move, "open_turn", side_effect=lambda chat: term.turn_open), \
                patch.object(chat_move, "TURN_POLL_S", 0.02), \
                patch("core.session.interactive_session.find_live_for_chat", return_value=term), \
                patch.object(chat_rebuild, "close_chat_session", side_effect=_close), \
                patch.object(pool_moves, "note"):
            pool_moves.restart_when_idle(["c-3"], "line")
            await asyncio.sleep(0.1)
            assert closed == []
            term.holds_unsent_text = False        # the Enter clears the flag at once
            term.submit_settling = True           # and its tails are pending
            await asyncio.sleep(0.15)
            term.turn_open = True                 # the tail sees the user line
            term.submit_settling = False
            await asyncio.sleep(0.05)
            task = chat_reload._idle_closes.pop("c-3", None)
            if task is not None:
                task.cancel()
            await asyncio.sleep(0)

    asyncio.run(_run())
    assert closed == []


def test_a_submit_settles_for_its_tails():
    from core.session.interactive_queue import InteractiveQueueMixin
    term = type("_Settling", (InteractiveQueueMixin,), {})()
    term._composer_dirty = False
    assert term.submit_settling is False
    term._submit_settle_until = time.monotonic() + 5
    assert term.submit_settling is True
    term._submit_settle_until = time.monotonic() - 1
    assert term.submit_settling is False


@patch("services.engines.subscription_windows.is_enabled", return_value=True)
@patch.object(tf, "fan_out", side_effect=_lands)
@patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
@pool_store()
def test_no_move_onto_an_account_whose_reading_says_reached(mock_store, _target, fan, _on):
    """Both accounts at their limit, refused by plain cooldowns: when the first cooldown lapses, the vendor's own
    reading still says reached, so the session does not go back to it."""
    _reset()
    _store(mock_store, "claude-code-cli")
    full = dict(_reading(week_pct=100.0, week_reset_h=30))
    full["data"] = {"scoped": [], "reached": "seven_day", "plan": "max"}
    _readings(mock_store, {"sub-a": full, "sub-b": full})
    _bind("claude-code-cli", _CLAUDE_SCOPE)
    with patch.object(pool_moves, "note"):
        sp.mark_subscription_throttled("sess-1")
        assert sp.rebalance_scopes(reason="limit") == 0
    assert sp._session_subscriptions["sess-1"] == "sub-a"


@pool_store()
def test_two_accounts_with_one_label_are_never_named(mock_store):
    rows = {"sub-a": {"id": "sub-a", "owner_sub": "alice", "label": "Claude Max"},
            "sub-b": {"id": "sub-b", "owner_sub": "alice", "label": "Claude Max"}}
    mock_store.get_subscription.side_effect = rows.get
    old, new = pool_moves.names("sub-a", "sub-b", "alice")
    assert (old, new) == ("", "")
    assert pool_moves.line(Cause.NEAR, old, new, "weekly", in_place=True) == (
        "This chat now runs on another account. The account it used is close to its weekly limit.")
    rows["sub-b"]["label"] = "Home Max"
    assert pool_moves.names("sub-a", "sub-b", "alice") == ("Claude Max", "Home Max")


def test_a_pruned_folder_lock_is_never_waited_on(tmp_path):
    """A writer that took the folder's lock before the prune finds it gone from the map after acquiring, and
    takes the folder's current one instead."""
    folder = str(tmp_path)
    stale = tf._dir_lock(folder)
    tf._dir_locks.pop(folder, None)          # the prune, after the writer took the object
    real = tf._dir_lock
    first = iter([stale])

    def _taken_before_the_prune(d):
        return next(first, None) or real(d)
    with patch.object(tf, "_dir_lock", _taken_before_the_prune), tf._folder_lock(folder) as lock:
        assert lock is not stale and tf._dir_locks[folder] is lock
    assert not stale.locked()
    tf._dir_locks.pop(folder, None)
