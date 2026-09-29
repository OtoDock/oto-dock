"""Weekly-window alerts (services.infra.subscription_window_alerts): 90 % as
info and 100 % as warning to the account owner, once per window instance,
re-armed by a new reset instant, scoped per-model windows included, the
owner's opt-out honoured.

Run: cd proxy && python -m pytest tests/billing/test_subscription_window_alerts.py -v
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from services.engines import subscription_windows as sw
from services.infra import subscription_window_alerts as alerts

NOW = datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=4, hours=3, seconds=41, microseconds=755375)


def _row(sub_id="s1", *, owner="user-1", label="Claude Max", email="me@example.com",
         layer="claude-code-cli", status="active", auth_type="oauth"):
    return {"id": sub_id, "status": status, "owner_sub": owner, "auth_type": auth_type,
            "layer": layer, "provider": "anthropic", "label": label, "oauth_email": email}


def _reading(seven=50.0, *, resets_at=RESET, scoped=()):
    from core.execution_layer import WindowSpec
    specs = {
        "five_hour": WindowSpec("five_hour", 5 * 3600, "session", "session"),
        "seven_day": WindowSpec("seven_day", 7 * 86400, "quota", "weekly"),
    }
    return sw.Windows(specs=specs,
                      windows={"five_hour": sw.Window(5.0, NOW + timedelta(hours=2)),
                               "seven_day": sw.Window(seven, resets_at)},
                      scoped=list(scoped), observed_at=NOW)


class _Store:
    """The alert rows, in memory, shaped like the store's helpers."""

    def __init__(self, rows):
        self.rows = rows
        self.alerts: dict[tuple, dict] = {}

    def list_subscriptions(self):
        return list(self.rows)

    def get_window_alert(self, sub_id, window_key, threshold):
        return self.alerts.get((sub_id, window_key, threshold))

    def upsert_window_alert(self, sub_id, window_key, threshold, *, resets_at, fired_at):
        self.alerts[(sub_id, window_key, threshold)] = {
            "subscription_id": sub_id, "window_key": window_key, "threshold": threshold,
            "resets_at": resets_at, "fired_at": fired_at,
        }


def _readings_for(subs, readings):
    """``latest`` takes subscription ROWS (the layer names the window specs) —
    the sweep once passed bare ids and failed on every pass in production."""
    assert all(isinstance(s, dict) and s.get("id") and s.get("layer") for s in subs), subs
    return readings


def _sweep(rows, readings, store=None, *, prefs=None, enabled=True, now=NOW):
    store = store or _Store(rows)
    fire = AsyncMock()
    alerts._last_run = 0.0
    with patch.object(alerts, "subscription_store", store), \
         patch.object(sw, "is_enabled", return_value=enabled), \
         patch.object(sw, "latest", side_effect=lambda subs, now=None: _readings_for(subs, readings)), \
         patch("storage.prefs.user_ui_prefs_store.get_prefs",
               side_effect=lambda owner: (prefs or {}).get(owner, {})), \
         patch("core.session.session_state.get_user_tz", return_value="Europe/Athens"), \
         patch("services.notifications.notification_manager.fire_notification", fire):
        fired = asyncio.run(alerts.check_window_alerts(now=now))
    return fired, fire, store


class TestThresholds:
    def test_quiet_below_ninety(self):
        fired, fire, _ = _sweep([_row()], {"s1": _reading(89.9)})
        assert fired == 0 and fire.await_count == 0

    def test_ninety_is_an_info_with_the_owner_clock(self):
        fired, fire, store = _sweep([_row()], {"s1": _reading(91.0)})
        assert fired == 1
        kw = fire.await_args.kwargs
        assert kw["severity"] == "info" and kw["scope"] == "user" and kw["target"] == "user-1"
        assert kw["title"] == "Claude Max (me@example.com): weekly limit at 90%"
        assert kw["body"].startswith("91% used. Resets Tue 12:00.")   # Athens, minute precision
        assert "other accounts" not in kw["body"]                     # a single account
        assert store.alerts[("s1", "seven_day", 90)]["resets_at"] == "2026-09-15T09:00:00+00:00"
        assert ("s1", "seven_day", 100) not in store.alerts

    def test_hundred_is_a_warning_and_stamps_both(self):
        fired, fire, store = _sweep([_row()], {"s1": _reading(100.0)})
        assert fired == 1
        kw = fire.await_args.kwargs
        assert kw["severity"] == "warning"
        assert kw["title"] == "Claude Max (me@example.com): weekly limit reached"
        assert "refuses new work on this account until Tue 12:00" in kw["body"]
        assert {k[2] for k in store.alerts} == {90, 100}

    def test_other_accounts_are_mentioned(self):
        rows = [_row("s1"), _row("s2", email="two@example.com")]
        _, fire, _ = _sweep(rows, {"s1": _reading(95.0), "s2": _reading(10.0)})
        assert fire.await_args.kwargs["body"].endswith("New work goes to your other accounts first.")


class TestDedup:
    def test_same_instance_fires_once_then_the_next_mark(self):
        store = _Store([_row()])
        fired, _, _ = _sweep([_row()], {"s1": _reading(92.0)}, store)
        assert fired == 1
        fired, _, _ = _sweep([_row()], {"s1": _reading(96.0)}, store)
        assert fired == 0                                   # same window, same mark
        fired, fire, _ = _sweep([_row()], {"s1": _reading(100.0)}, store)
        assert fired == 1 and fire.await_args.kwargs["severity"] == "warning"
        fired, _, _ = _sweep([_row()], {"s1": _reading(100.0)}, store)
        assert fired == 0

    def test_source_jitter_does_not_rearm(self):
        store = _Store([_row()])
        _sweep([_row()], {"s1": _reading(92.0, resets_at=RESET)}, store)
        # The stream event reports the same instant at whole seconds.
        fired, _, _ = _sweep([_row()], {"s1": _reading(93.0, resets_at=RESET.replace(microsecond=0))}, store)
        assert fired == 0

    def test_new_reset_instant_rearms(self):
        store = _Store([_row()])
        _sweep([_row()], {"s1": _reading(92.0)}, store)
        later = RESET + timedelta(days=7)
        fired, _, _ = _sweep([_row()], {"s1": _reading(92.0, resets_at=later)}, store,
                             now=NOW + timedelta(days=7))
        assert fired == 1
        assert store.alerts[("s1", "seven_day", 90)]["resets_at"].startswith("2026-09-22T09:00")

    def test_throttled_between_passes(self):
        store = _Store([_row()])
        _sweep([_row()], {"s1": _reading(92.0)}, store)
        fire = AsyncMock()
        # A second pass inside the interval does nothing at all.
        with patch.object(alerts, "subscription_store", store), \
             patch("services.notifications.notification_manager.fire_notification", fire):
            assert asyncio.run(alerts.check_window_alerts(now=NOW)) == 0
        alerts._last_run = 0.0


class TestScopedAndSkips:
    def test_scoped_window_has_its_own_pair(self):
        fable = sw.Scoped("fable", "Fable", 100.0, RESET, True)
        fired, fire, store = _sweep([_row()], {"s1": _reading(55.0, scoped=[fable])})
        assert fired == 1
        kw = fire.await_args.kwargs
        assert kw["title"] == "Claude Max (me@example.com): Fable weekly limit reached"
        assert "refuses Fable on this account until Tue 12:00" in kw["body"]
        assert "other models stay available" in kw["body"]
        assert ("s1", "scoped:fable", 100) in store.alerts
        assert ("s1", "seven_day", 90) not in store.alerts

    def test_owner_opt_out(self):
        fired, fire, _ = _sweep([_row()], {"s1": _reading(100.0)},
                                prefs={"user-1": {"subscription_usage_alerts": False}})
        assert fired == 0 and fire.await_count == 0
        # An explicit True (or an absent key) keeps the alerts on.
        fired, _, _ = _sweep([_row()], {"s1": _reading(100.0)},
                             prefs={"user-1": {"subscription_usage_alerts": True}})
        assert fired == 1

    def test_ownerless_expired_and_keys_are_skipped(self):
        rows = [_row("s1", owner=""), _row("s2", status="expired"),
                _row("s3", auth_type="api_key")]
        fired, _, _ = _sweep(rows, {"s1": _reading(100.0), "s2": _reading(100.0),
                                    "s3": _reading(100.0)})
        assert fired == 0

    def test_disabled_setting_is_silent(self):
        fired, _, _ = _sweep([_row()], {"s1": _reading(100.0)}, enabled=False)
        assert fired == 0

    def test_window_without_a_reset_instant_is_skipped(self):
        fired, _, _ = _sweep([_row()], {"s1": _reading(100.0, resets_at=None)})
        assert fired == 0


class TestMinuteBoundaryJitter:
    def test_instants_straddling_a_minute_are_one_window(self):
        """The vendor's instant moved 05:59:59.8 → 06:00:00.4 → 05:59:59.9
        across three polls on 2026-09-11 and the same "limit reached" fired
        three times in an hour: a minute-precision key is not a window
        identity, a distance is."""
        from datetime import datetime, timezone
        a = datetime(2026, 9, 16, 5, 59, 59, 803975, tzinfo=timezone.utc)
        b = datetime(2026, 9, 16, 6, 0, 0, 373101, tzinfo=timezone.utc)
        c = datetime(2026, 9, 16, 5, 59, 59, 905910, tzinfo=timezone.utc)
        store = _Store([_row()])
        fired, _, _ = _sweep([_row()], {"s1": _reading(100.0, resets_at=a)}, store)
        assert fired == 1
        for instant in (b, c, b):
            fired, _, _ = _sweep([_row()], {"s1": _reading(100.0, resets_at=instant)}, store)
            assert fired == 0, instant
        # A week later is a new window and fires again.
        later = datetime(2026, 9, 23, 6, 0, 0, 120000, tzinfo=timezone.utc)
        fired, _, _ = _sweep([_row()], {"s1": _reading(100.0, resets_at=later)}, store)
        assert fired == 1
