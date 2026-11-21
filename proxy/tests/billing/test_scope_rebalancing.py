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
- nudge_from_cli_error classifies CLI-reported error text only (an overload's brief rest).
"""
import time
from unittest.mock import patch

from services.engines import pool_bindings, subscription_pool as sp, token_fanout as tf
from tests._pool_store import pool_store

_FAR_MS = int((time.time() + 8 * 3600) * 1000)
_SCOPE = "local:/agents/dev/workspace/.claude"


def _reset():
    sp._session_subscriptions.clear()
    sp._session_binding_ctx.clear()
    sp._session_scope_keys.clear()
    sp._scope_recent.clear()
    sp._session_token_expiry.clear()
    sp._session_moved_at.clear()
    sp._issued_token_expiry.clear()
    sp._throttled_until.clear()
    sp._throttled_hard.clear()
    sp._window_rests.clear()
    sp._scoped_rests.clear()
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
    mock_store.get_subscription.side_effect = rows.get
    mock_store.get_subscription_consumption.side_effect = (
        lambda sid, since: {"sub-a": cons_a, "sub-b": cons_b}[sid])
    mock_store.get_credential_data.return_value = {
        "oauth_token": {"accessToken": "tok", "expiresAt": _FAR_MS}}
    return rows


def _fan_out_lands(sids, *, layer=None, payload=None,
                   on_written=None, expected_sub_id=None):
    for s in sids:
        on_written(s)


class TestCausesAndCooldown:
    """A live session moves for a refusal, a mark, a delisting or a cold
    return, never for spending alone."""

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_spending_alone_never_moves_a_live_session(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store, cons_a=5000.0, cons_b=0.0)
        _bind()
        assert sp.rebalance_scopes(reason="test") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()

    @patch("services.engines.subscription_windows.is_enabled", return_value=True)
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_a_second_mark_moves_the_scope_at_once(self, mock_store, _t, mock_fan, _on):
        # No cooldown at a mark: a target past its mark is never taken, so two accounts cannot trade a scope,
        # and a session that crosses a second mark leaves before the vendor refuses.
        _reset()
        _store_two_subs(mock_store)
        marks = {"sub-a": 95.0, "sub-b": 10.0}
        mock_store.latest_window_samples.side_effect = lambda ids: {
            sid: _window_row(five_hour_pct=marks[sid]) for sid in ids}
        _bind()
        assert sp.rebalance_scopes(reason="test") == 1
        assert _SCOPE not in sp._scope_rebalance_last
        marks.update({"sub-a": 10.0, "sub-b": 95.0})
        assert sp.rebalance_scopes(reason="test") == 1
        assert mock_fan.call_count == 2
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        # Both past their marks: it stays.
        marks.update({"sub-a": 96.0, "sub-b": 95.0})
        assert sp.rebalance_scopes(reason="test") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_a_refusal_is_never_held_by_the_cooldown(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store)
        _bind()
        sp._scope_rebalance_last[_SCOPE] = time.monotonic()
        sp.mark_subscription_throttled("sess-1")
        assert sp.rebalance_scopes(reason="limit") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_no_replacement_stamps_no_cooldown(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store)
        _bind()
        sp.mark_subscription_throttled("sess-1")
        sp._throttled_until["sub-b"] = time.time() + 900
        sp._throttled_hard.add("sub-b")
        assert sp.rebalance_scopes(reason="limit") == 0
        assert _SCOPE not in sp._scope_rebalance_last

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_recent_spawn_claim_defers_move(self, mock_store, _t, mock_fan):
        # A spawn just claimed the scope (acquire→bind window): the move is
        # deferred WITHOUT stamping the cooldown, so the next pass retries.
        _reset()
        _store_two_subs(mock_store)
        _bind()
        sp.mark_subscription_throttled("sess-1")
        sp._scope_recent[_SCOPE] = ("sub-a", time.time())
        assert sp.rebalance_scopes(reason="test") == 0
        mock_fan.assert_not_called()
        assert _SCOPE not in sp._scope_rebalance_last
        # Claim expired → the same refusal now moves the scope.
        sp._scope_recent[_SCOPE] = ("sub-a", time.time() - sp._SCOPE_RECENT_TTL_S - 1)
        assert sp.rebalance_scopes(reason="test") == 1

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_sole_account_never_moves(self, mock_store, _t, mock_fan):
        _reset()
        rows = {"sub-a": _row("sub-a")}
        mock_store.list_platform_pool.return_value = list(rows.values())
        mock_store.list_personal.return_value = []
        mock_store.get_subscription.side_effect = rows.get
        mock_store.get_subscription_consumption.return_value = 5000.0
        _bind()
        assert sp.rebalance_scopes(reason="test") == 0
        mock_fan.assert_not_called()

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
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
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_a_hard_limit_moves_the_scope(self, mock_store, _t, mock_fan):
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        _bind()
        sp.mark_subscription_throttled("sess-1")  # full cooldown = hard class
        assert "sub-a" in sp._throttled_hard
        assert sp.rebalance_scopes(reason="limit") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
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
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_replacement_must_not_be_throttled(self, mock_store, _t, mock_fan):
        # Both accounts resting on real limits: moving A→B is pure churn.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        _bind()
        sp.mark_subscription_throttled("sess-1")
        sp._throttled_until["sub-b"] = time.time() + 900
        sp._throttled_hard.add("sub-b")
        assert sp.rebalance_scopes(reason="limit") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_user_scope_respects_platform_auth_off(self, mock_store, _t, mock_fan):
        # User-scope group, hard-limited own account, Platform Auth OFF and no
        # other personal sub → nowhere to go; sessions stay put.
        _reset()
        rows = {"sub-a": _row("sub-a", owner_sub="user-1")}
        mock_store.list_personal.return_value = [rows["sub-a"]]
        mock_store.list_platform_pool.return_value = []
        mock_store.get_user_allow_platform_auth.return_value = False
        mock_store.get_subscription.side_effect = rows.get
        mock_store.get_subscription_consumption.return_value = 1.0
        _bind(scope_sub="user-1")
        sp.mark_subscription_throttled("sess-1")
        assert sp.rebalance_scopes(reason="limit") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()


class TestThrottleMarking:
    @pool_store()
    def test_read_through_marks_persisted_binding(self, mock_store):
        # A session that outlived a proxy restart is only in the DB mirror —
        # the tailer's limit report must still land on the right account.
        _reset()
        mock_store.get_session_binding.return_value = {"subscription_id": "sub-x"}
        sp.mark_subscription_throttled("ghost-sess")
        assert sp._is_throttled("sub-x")
        assert "sub-x" in sp._throttled_hard

    @pool_store()
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

    @pool_store()
    def test_cli_error_classifier_gates(self, mock_store):
        _reset()
        mock_store.get_session_binding.return_value = None
        sp._session_subscriptions["s"] = "sub-x"
        # 401 (the real observed outage line) is NOT a limit — no rest.
        sp.nudge_from_cli_error(
            "s", "Please run /login · API Error: 401 Invalid authentication credentials")
        assert not sp._is_throttled("sub-x")
        # Overload → brief rest, NOT the hard class.
        sp.nudge_from_cli_error("s", "API Error: 529 Overloaded")
        assert sp._is_throttled("sub-x")
        assert "sub-x" not in sp._throttled_hard
        sp._throttled_until.clear()
        # A real limit is a typed ending (``follow_ending``), never the nudge's to rest.
        sp.nudge_from_cli_error("s", "API Error: 429 rate limit exceeded")
        assert not sp._is_throttled("sub-x")
        # The vendor's brief throttle is the overload's class, whatever its words hold.
        sp.nudge_from_cli_error(
            "s", "API Error: Server is temporarily limiting requests (not your usage limit)")
        assert sp._is_throttled("sub-x") and "sub-x" not in sp._throttled_hard
        sp._throttled_until.clear()


class TestAHomesOwnRow:
    """A home has one session, so its key's stored row is the restarting
    session's own: a chat moved to another account when idle, its release cut
    by a restart, starts on the pool's pick, not the account it was leaving."""

    @pool_store()
    def test_a_homes_stored_row_pins_nothing(self, mock_store, monkeypatch):
        _reset()
        home_key = "c-1:/agents/a/users/alice/.codex/own/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        mock_store.get_scope_binding.return_value = "sub-leaving"
        mock_store.list_scope_bindings.return_value = [
            {"session_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "subscription_id": "sub-leaving"}]
        for since_boot in (0, 700):
            monkeypatch.setattr(pool_bindings, "_BOOT_MONOTONIC", time.monotonic() - since_boot)
            assert sp._sticky_subscription_id(home_key) is None
        # A live session of the key still holds it (a reload of a running chat).
        sp._session_subscriptions["0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"] = "sub-now"
        sp._session_scope_keys["0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"] = home_key
        assert sp._sticky_subscription_id(home_key) == "sub-now"


class TestStickyLiveness:
    """_sticky_subscription_id's persisted fallback: trust rows of LIVE
    sessions only (past the boot grace); ghosts get deleted on sight."""

    @pool_store()
    def test_ghost_row_deleted_and_ignored(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(pool_bindings, "_BOOT_MONOTONIC", time.monotonic() - 700)
        mock_store.list_scope_bindings.return_value = [
            {"session_id": "dead-sid", "subscription_id": "sub-old"}]
        assert sp._sticky_subscription_id(_SCOPE) is None
        mock_store.delete_session_binding.assert_called_once_with("dead-sid")

    @pool_store()
    def test_live_row_pins_scope(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(pool_bindings, "_BOOT_MONOTONIC", time.monotonic() - 700)
        # Live in the pool's own map (bound under a DIFFERENT scope lookup) —
        # the registry probe's first, cheapest source.
        sp._session_subscriptions["live-sid"] = "sub-live"
        mock_store.list_scope_bindings.return_value = [
            {"session_id": "live-sid", "subscription_id": "sub-live"}]
        assert sp._sticky_subscription_id(_SCOPE) == "sub-live"
        mock_store.delete_session_binding.assert_not_called()

    @pool_store()
    def test_ghost_skipped_falls_to_older_live_row(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(pool_bindings, "_BOOT_MONOTONIC", time.monotonic() - 700)
        sp._session_subscriptions["live-sid"] = "sub-live"
        mock_store.list_scope_bindings.return_value = [
            {"session_id": "dead-sid", "subscription_id": "sub-old"},
            {"session_id": "live-sid", "subscription_id": "sub-live"}]
        assert sp._sticky_subscription_id(_SCOPE) == "sub-live"
        mock_store.delete_session_binding.assert_called_once_with("dead-sid")

    @pool_store()
    def test_boot_grace_trusts_newest_row(self, mock_store, monkeypatch):
        # Registries still warming after a restart — no verdicts, no deletes.
        _reset()
        monkeypatch.setattr(pool_bindings, "_BOOT_MONOTONIC", time.monotonic())
        mock_store.get_scope_binding.return_value = "sub-old"
        assert sp._sticky_subscription_id(_SCOPE) == "sub-old"
        mock_store.list_scope_bindings.assert_not_called()
        mock_store.delete_session_binding.assert_not_called()


class TestASessionThatIsGone:
    """``session_gone``: what a session's home and its stored row are
    removed by. It answers yes only when it knows: a registry or a row
    that cannot be read keeps what the caller holds."""

    def _machines(self, monkeypatch, *, connected=(), reported=()):
        from core.remote import satellite_connection, satellite_link

        class _Machines:
            def is_connected(self, machine_id):
                return machine_id in connected

        monkeypatch.setattr(satellite_connection, "get_connection_manager", _Machines)
        monkeypatch.setattr(satellite_link, "reported_since_boot", lambda m: m in reported)

    @pool_store()
    def test_a_registry_that_holds_it_or_cannot_be_asked_keeps_it(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(pool_bindings, "session_registered_live", lambda sid: {"held": True, "soft": None}[sid])
        assert sp.session_gone("held") is False and sp.session_gone("soft") is False
        mock_store.get_session_binding.assert_not_called()

    @pool_store()
    def test_no_row_is_judged_by_the_registries_alone(self, mock_store, monkeypatch):
        _reset()
        monkeypatch.setattr(pool_bindings, "session_registered_live", lambda sid: False)
        mock_store.get_session_binding.return_value = None
        assert sp.session_gone("s-1") is True

    @pool_store()
    def test_a_row_that_cannot_be_read_keeps_it(self, mock_store, monkeypatch):
        import psycopg
        _reset()
        monkeypatch.setattr(pool_bindings, "session_registered_live", lambda sid: False)
        mock_store.get_session_binding.side_effect = psycopg.OperationalError("down")
        assert sp.session_gone("s-1") is False

    @pool_store()
    def test_a_row_waits_for_its_machine(self, mock_store, monkeypatch):
        """A paired computer's key names it; the platform's place is any
        compute machine. Away, or here with no report yet: it may come back."""
        import psycopg

        from storage import remote_store
        _reset()
        monkeypatch.setattr(pool_bindings, "session_registered_live", lambda sid: False)
        rows = {"paired": "m-1:/x/own/paired", "place": "local:/x/own/place"}
        mock_store.get_session_binding.side_effect = lambda sid: {"session_id": sid, "scope_key": rows[sid]}
        monkeypatch.setattr(remote_store, "list_compute_machines", lambda: [{"id": "c-1"}, {"id": "c-2"}])
        self._machines(monkeypatch, connected={"m-1", "c-1", "c-2"}, reported={"m-1", "c-1", "c-2"})
        assert sp.session_gone("paired") is True and sp.session_gone("place") is True
        self._machines(monkeypatch, connected={"c-1", "c-2"}, reported={"m-1", "c-1", "c-2"})
        assert sp.session_gone("paired") is False
        self._machines(monkeypatch, connected={"m-1", "c-1", "c-2"}, reported={"c-1"})
        assert sp.session_gone("paired") is False and sp.session_gone("place") is False

        def _down():
            raise psycopg.OperationalError("down")
        self._machines(monkeypatch, connected={"m-1", "c-1", "c-2"}, reported={"m-1", "c-1", "c-2"})
        monkeypatch.setattr(remote_store, "list_compute_machines", _down)
        assert sp.session_gone("place") is False


def _window_row(**o):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    row = {"observed_at": now.isoformat(), "source": "poll",
           "five_hour_pct": 5.0, "five_hour_resets_at": (now + timedelta(hours=2)).isoformat(),
           "seven_day_pct": 10.0, "seven_day_resets_at": (now + timedelta(days=3)).isoformat(),
           "data": {"scoped": [], "reached": "", "plan": "max"}}
    row.update(o)
    return row


class TestWindowExhaustionRebalance:
    @patch("services.engines.subscription_windows.is_enabled", return_value=True)
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_window_exhausted_scope_moves(self, mock_store, _t, mock_fan, _on):
        """The vendor's own reading (no error text needed) moves the scope."""
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        mock_store.latest_window_samples.side_effect = lambda ids: {
            sid: _window_row(five_hour_pct=95.0) if sid == "sub-a" else _window_row()
            for sid in ids}
        _bind()
        assert sp.rebalance_scopes(reason="windows") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"

    @patch("services.engines.subscription_windows.is_enabled", return_value=True)
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_replacement_must_not_be_window_exhausted(self, mock_store, _t, mock_fan, _on):
        # Every account out of window: hopping A→B is churn, the scope stays.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        mock_store.latest_window_samples.side_effect = lambda ids: {
            sid: _window_row(seven_day_pct=99.0) for sid in ids}
        _bind()
        assert sp.rebalance_scopes(reason="windows") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()


def _fable_full():
    return {"scoped": [{"key": "fable", "label": "Fable", "pct": 100.0,
                        "resets_at": "2099-01-01T00:00:00+00:00", "active": True}],
            "reached": "scoped:fable", "plan": "max"}


class TestModelAwareRebalance:
    """A scope is judged and re-homed by the model its sessions run: the
    chat rows carry it (the binding context does not). Live-observed
    2026-09-11: Fable sessions were moved onto the account whose Fable
    window was full, trading one refusal for another."""

    def _chat_model(self, model):
        return patch("storage.database.get_chat_by_session",
                     return_value={"model": model, "agent": "dev"})

    @patch("services.engines.subscription_windows.is_enabled", return_value=True)
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_full_model_window_moves_the_scope_running_that_model(self, mock_store, _t, mock_fan, _on):
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        # sub-a: overall windows fine, Fable full. sub-b: everything fine.
        mock_store.latest_window_samples.side_effect = lambda ids: {
            sid: _window_row(data=_fable_full()) if sid == "sub-a" else _window_row()
            for sid in ids}
        _bind()
        with self._chat_model("claude-fable-5-2"):
            assert sp.rebalance_scopes(reason="windows") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"
        # A Sonnet scope on the same account has nothing to flee from.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        _bind()
        with self._chat_model("claude-sonnet-5"):
            assert sp.rebalance_scopes(reason="windows") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"

    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_a_rest_for_the_scopes_model_moves_it(self, mock_store, _t, mock_fan):
        # A usage limit on sub-a's Fable window rests it for Fable only: the
        # Fable scope moves to sub-b, a Sonnet scope on sub-a stays.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        mock_store.latest_window_samples.side_effect = lambda ids: {}
        sp._scoped_rests[("sub-a", "fable")] = (time.time() + 86400, time.time())
        _bind()
        with self._chat_model("claude-fable-5-2"):
            assert sp.rebalance_scopes(reason="provider limit") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        sp._scoped_rests[("sub-a", "fable")] = (time.time() + 86400, time.time())
        _bind()
        with self._chat_model("claude-sonnet-5"):
            assert sp.rebalance_scopes(reason="provider limit") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        # A replacement resting for the scope's model is no replacement.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        sp._scoped_rests[("sub-a", "fable")] = (time.time() + 86400, time.time())
        sp._scoped_rests[("sub-b", "fable")] = (time.time() + 86400, time.time())
        _bind()
        with self._chat_model("claude-fable-5-2"):
            assert sp.rebalance_scopes(reason="provider limit") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        _reset()

    @patch("services.engines.subscription_windows.is_enabled", return_value=True)
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_replacement_must_serve_the_scopes_model(self, mock_store, _t, mock_fan, _on):
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        # sub-a: session window gone. sub-b: session fine, Fable full.
        mock_store.latest_window_samples.side_effect = lambda ids: {
            sid: _window_row(five_hour_pct=97.0) if sid == "sub-a"
            else _window_row(data=_fable_full())
            for sid in ids}
        _bind()
        with self._chat_model("claude-fable-5-2"):
            assert sp.rebalance_scopes(reason="windows") == 0
        assert sp._session_subscriptions["sess-1"] == "sub-a"
        mock_fan.assert_not_called()
        # The same accounts serve a Sonnet scope: sub-b has room for it.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        _bind()
        with self._chat_model("claude-sonnet-5"):
            assert sp.rebalance_scopes(reason="windows") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"

    @patch("services.engines.subscription_windows.is_enabled", return_value=True)
    @patch.object(tf, "fan_out", side_effect=_fan_out_lands)
    @patch.object(tf, "session_target",
                  return_value=tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x"))
    @pool_store()
    def test_recent_claim_on_another_account_does_not_defer(self, mock_store, _t, mock_fan, _on):
        # A sticky pin that yielded to exhaustion claimed the scope on sub-b:
        # the rest of the scope follows now, not a tick later.
        _reset()
        _store_two_subs(mock_store, cons_a=1.0, cons_b=0.0)
        mock_store.latest_window_samples.side_effect = lambda ids: {
            sid: _window_row(five_hour_pct=97.0) if sid == "sub-a" else _window_row()
            for sid in ids}
        _bind()
        sp._scope_recent[_SCOPE] = ("sub-b", time.time())
        with self._chat_model("claude-fable-5-2"):
            assert sp.rebalance_scopes(reason="windows") == 1
        assert sp._session_subscriptions["sess-1"] == "sub-b"
