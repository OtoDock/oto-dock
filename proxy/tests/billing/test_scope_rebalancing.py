"""Scope-level rebalancing + sticky-pin liveness (subscription_pool).

Covers the subscription_pool scope-rebalancing invariants:
- drift trigger ($100 floor / 3× ratio on the 5h window) moves a whole scope
  onto the coldest eligible account over the fan-out rails, ack-gated;
- reactive trigger: a REAL provider limit (full cooldown class) moves the
  scope even below the drift floor; the 10s overload nudge never does;
- per-scope cooldown suppresses repeat moves; no-candidate cases no-op;
- replacements that are themselves throttled are refused (rebalance only);
- sticky lookup deletes GHOST persisted rows (dead session, past boot grace)
  and keeps rows of live sessions / everything during the boot grace window;
- mark_subscription_throttled read-through + hard-class tracking;
- throttle_from_cli_error classifies CLI-reported error text only.
"""
import time
from unittest.mock import MagicMock, patch

from services.engines import subscription_pool as sp
from services.engines import token_fanout as tf


_FAR_MS = int((time.time() + 8 * 3600) * 1000)
_SCOPE = "local:/agents/dev/workspace/.claude"


def _reset():
    sp._session_subscriptions.clear()
    sp._session_binding_ctx.clear()
    sp._session_scope_keys.clear()
    sp._scope_recent.clear()
    sp._session_token_expiry.clear()
    sp._issued_token_expiry.clear()
    sp._throttled_until.clear()
    sp._throttled_hard.clear()
    sp._scope_rebalance_last.clear()
    sp._refresh_backoff.clear()


def _row(sid, **o):
    base = {"id": sid, "layer": "claude-code-cli", "provider": "anthropic",
            "auth_type": "oauth", "owner_sub": "", "is_primary": 0,
            "status": "active", "active_sessions": 0}
    base.update(o)
    return base


def _bind(session_id="sess-1", sub="sub-a", layer="claude-code-cli",
          scope_sub="", scope_key=_SCOPE):
    sp._session_subscriptions[session_id] = sub
    sp._session_binding_ctx[session_id] = (layer, scope_sub)
    sp._session_scope_keys[session_id] = scope_key


def _store_two_subs(mock_store, cons_a=500.0, cons_b=5.0):
    """A platform pool of sub-a (pinned/hot) + sub-b (cold), OAuth creds."""
    rows = {"sub-a": _row("sub-a"), "sub-b": _row("sub-b")}
    mock_store.list_platform_pool.return_value = list(rows.values())
    mock_store.list_personal.return_value = []
    mock_store.get_subscription.side_effect = lambda sid: rows.get(sid)
    mock_store.get_subscription_consumption.side_effect = (
        lambda sid, since: {"sub-a": cons_a, "sub-b": cons_b}[sid])
    mock_store.get_credential_data.return_value = {
        "oauth_token": {"accessToken": "tok", "expiresAt": _FAR_MS}}
    return rows


def _fan_out_lands(sids, *, claude_blob=None, codex_auth=None,
                   on_written=None, expected_sub_id=None):
    for s in sids:
        on_written(s)


class TestDriftRebalance:
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_hammered_scope_moves_to_cold_account(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store, cons_a=500.0, cons_b=5.0)
        _bind()
        moved = sp.rebalance_scopes(reason="test")
        assert moved == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"
        mock_store.update_session_binding_sub.assert_called_once_with("sess-1", "sub-b")
        assert _SCOPE in sp._scope_rebalance_last
        assert mock_fan.call_count == 1

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_below_floor_stays(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store, cons_a=sp.DRIFT_ABS_FLOOR_USD - 1, cons_b=0.0)
        _bind()
        assert sp.rebalance_scopes(reason="test") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_within_ratio_stays(self, mock_store, _t, mock_fan):
        # 300 vs 150: above the floor but not 3× apart — roughly even, no move.
        _reset()
        _store_two_subs(mock_store, cons_a=300.0, cons_b=150.0)
        _bind()
        assert sp.rebalance_scopes(reason="test") == 0
        mock_fan.assert_not_called()

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_cooldown_suppresses_second_move(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store)
        _bind()
        assert sp.rebalance_scopes(reason="test") == 1
        # Scope now hot on sub-b too — numbers say move again, cooldown says no.
        mock_store.get_subscription_consumption.side_effect = (
            lambda sid, since: {"sub-a": 5.0, "sub-b": 500.0}[sid])
        assert sp.rebalance_scopes(reason="test") == 0
        assert mock_fan.call_count == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_recent_spawn_claim_defers_move(self, mock_store, _t, mock_fan):
        # A spawn just claimed the scope (acquire→bind window): the move is
        # deferred WITHOUT stamping the cooldown, so the next pass retries.
        _reset()
        _store_two_subs(mock_store)
        _bind()
        sp._scope_recent[_SCOPE] = ("sub-a", time.time())
        assert sp.rebalance_scopes(reason="test") == 0
        mock_fan.assert_not_called()
        assert _SCOPE not in sp._scope_rebalance_last
        # Claim expired → the same numbers now move the scope.
        sp._scope_recent[_SCOPE] = ("sub-a", time.time() - sp._SCOPE_RECENT_TTL_S - 1)
        assert sp.rebalance_scopes(reason="test") == 1

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_sole_account_never_moves(self, mock_store, _t, mock_fan):
        _reset()
        rows = {"sub-a": _row("sub-a")}
        mock_store.list_platform_pool.return_value = list(rows.values())
        mock_store.list_personal.return_value = []
        mock_store.get_subscription.side_effect = lambda sid: rows.get(sid)
        mock_store.get_subscription_consumption.return_value = 5000.0
        _bind()
        assert sp.rebalance_scopes(reason="test") == 0
        mock_fan.assert_not_called()

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_fileless_and_unstamped_sessions_never_move(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store)
        # direct-llm style: no scope key; plus an unstamped binding.
        sp._session_subscriptions["nokey"] = "sub-a"
        sp._session_binding_ctx["nokey"] = ("claude-code-cli", "")
        sp._session_subscriptions["noctx"] = "sub-a"
        sp._session_scope_keys["noctx"] = _SCOPE
        assert sp.rebalance_scopes(reason="test") == 0
        mock_fan.assert_not_called()


class TestReactiveRebalance:
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_hard_limit_moves_even_below_floor(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        _bind()
        sp.mark_subscription_throttled("sess-1")  # full cooldown = hard class
        assert "sub-a" in sp._throttled_hard
        assert sp.rebalance_scopes(reason="limit") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_overload_nudge_does_not_move(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        _bind()
        sp.mark_subscription_throttled("sess-1", cooldown_s=sp._OVERLOAD_COOLDOWN_S)
        assert "sub-a" not in sp._throttled_hard
        assert sp.rebalance_scopes(reason="blip") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_replacement_must_not_be_throttled(self, mock_store, _t, mock_fan):
        # Both accounts resting on real limits: moving A→B is pure churn.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        _bind()
        sp.mark_subscription_throttled("sess-1")
        sp._throttled_until["sub-b"] = time.time() + 900
        assert sp.rebalance_scopes(reason="limit") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(kind="claude", host_dir="/x"))
    @patch("services.engines.subscription_pool.subscription_store")
    def test_user_scope_respects_platform_auth_off(self, mock_store, _t, mock_fan):
        # User-scope group, hard-limited own account, Platform Auth OFF and no
        # other personal sub → nowhere to go; sessions stay put.
        _reset()
        rows = {"sub-a": _row("sub-a", owner_sub="user-1")}
        mock_store.list_personal.return_value = [rows["sub-a"]]
        mock_store.list_platform_pool.return_value = []
        mock_store.get_user_allow_platform_auth.return_value = False
        mock_store.get_subscription.side_effect = lambda sid: rows.get(sid)
        mock_store.get_subscription_consumption.return_value = 1.0
        _bind(scope_sub="user-1")
        sp.mark_subscription_throttled("sess-1")
        assert sp.rebalance_scopes(reason="limit") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()


class TestThrottleMarking:
    @patch("services.engines.subscription_pool.subscription_store")
    def test_read_through_marks_persisted_binding(self, mock_store):
        # A session that outlived a proxy restart is only in the DB mirror —
        # the tailer's limit report must still land on the right account.
        _reset()
        mock_store.get_session_binding.return_value = {"subscription_id": "sub-x"}
        sp.mark_subscription_throttled("ghost-sess")
        assert sp._is_throttled("sub-x")
        assert "sub-x" in sp._throttled_hard

    @patch("services.engines.subscription_pool.subscription_store")
    def test_unknown_session_is_noop(self, mock_store):
        _reset()
        mock_store.get_session_binding.return_value = None
        sp.mark_subscription_throttled("nobody")
        assert not sp._throttled_until

    def test_hard_flag_clears_with_expiry(self):
        _reset()
        sp._throttled_until["sub-x"] = time.time() - 1
        sp._throttled_hard.add("sub-x")
        assert not sp._is_throttled("sub-x")
        assert "sub-x" not in sp._throttled_hard

    @patch("services.engines.subscription_pool.subscription_store")
    def test_cli_error_classifier_gates(self, mock_store):
        _reset()
        mock_store.get_session_binding.return_value = None
        sp._session_subscriptions["s"] = "sub-x"
        # 401 (the real observed outage line) is NOT a limit — no rest.
        sp.throttle_from_cli_error(
            "s", "Please run /login · API Error: 401 Invalid authentication credentials")
        assert not sp._is_throttled("sub-x")
        # Overload → brief rest, NOT the hard class.
        sp.throttle_from_cli_error("s", "API Error: 529 Overloaded")
        assert sp._is_throttled("sub-x")
        assert "sub-x" not in sp._throttled_hard
        sp._throttled_until.clear()
        # Real limit → full rest + hard class.
        sp.throttle_from_cli_error("s", "API Error: 429 rate limit exceeded")
        assert sp._is_throttled("sub-x")
        assert "sub-x" in sp._throttled_hard


class TestStickyLiveness:
    """_sticky_subscription_id's persisted fallback: trust rows of LIVE
    sessions only (past the boot grace); ghosts get deleted on sight."""

    @patch("services.engines.subscription_pool.subscription_store")
    def test_ghost_row_deleted_and_ignored(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(sp, "_BOOT_MONOTONIC", time.monotonic() - 700)
        mock_store.list_scope_bindings.return_value = [
            {"session_id": "dead-sid", "subscription_id": "sub-old"}]
        assert sp._sticky_subscription_id(_SCOPE) is None
        mock_store.delete_session_binding.assert_called_once_with("dead-sid")

    @patch("services.engines.subscription_pool.subscription_store")
    def test_live_row_pins_scope(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(sp, "_BOOT_MONOTONIC", time.monotonic() - 700)
        # Live in the pool's own map (bound under a DIFFERENT scope lookup) —
        # the registry probe's first, cheapest source.
        sp._session_subscriptions["live-sid"] = "sub-live"
        mock_store.list_scope_bindings.return_value = [
            {"session_id": "live-sid", "subscription_id": "sub-live"}]
        assert sp._sticky_subscription_id(_SCOPE) == "sub-live"
        mock_store.delete_session_binding.assert_not_called()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_ghost_skipped_falls_to_older_live_row(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(sp, "_BOOT_MONOTONIC", time.monotonic() - 700)
        sp._session_subscriptions["live-sid"] = "sub-live"
        mock_store.list_scope_bindings.return_value = [
            {"session_id": "dead-sid", "subscription_id": "sub-old"},
            {"session_id": "live-sid", "subscription_id": "sub-live"}]
        assert sp._sticky_subscription_id(_SCOPE) == "sub-live"
        mock_store.delete_session_binding.assert_called_once_with("dead-sid")

    @patch("services.engines.subscription_pool.subscription_store")
    def test_boot_grace_trusts_newest_row(self, mock_store, monkeypatch):
        # Registries still warming after a restart — no verdicts, no deletes.
        _reset()
        monkeypatch.setattr(sp, "_BOOT_MONOTONIC", time.monotonic())
        mock_store.get_scope_binding.return_value = "sub-old"
        assert sp._sticky_subscription_id(_SCOPE) == "sub-old"
        mock_store.list_scope_bindings.assert_not_called()
        mock_store.delete_session_binding.assert_not_called()
