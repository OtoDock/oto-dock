"""Provider windows: the normalizers, the effective reading and the pool's
questions (``services/engines/subscription_windows.py``). No DB: the store
is mocked where ``record`` touches it. The vendor shapes are cut from the
2026-09-11 T1 probe bodies and the headless-stream fixture
(``tests/fixtures/cli_wake/probe-race.jsonl``).

Run: cd proxy && python -m pytest tests/billing/test_subscription_windows_normalize.py -v
"""

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from services.engines import subscription_windows as sw  # noqa: E402

NOW = datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc)
WEEK_RESET = "2026-09-16T05:59:59.755375+00:00"

CLAUDE_USAGE = {
    "five_hour": {"utilization": 0.0, "resets_at": None, "limit_dollars": None},
    "seven_day": {"utilization": 55.0, "resets_at": WEEK_RESET},
    "seven_day_oauth_apps": None, "seven_day_opus": None, "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False},
    "limits": [
        {"kind": "session", "group": "session", "percent": 0, "severity": "normal",
         "resets_at": None, "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 55, "severity": "normal",
         "resets_at": WEEK_RESET, "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 100, "severity": "critical",
         "resets_at": "2026-09-16T05:59:59.755514+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": True},
    ],
    "spend": {"used": {"amount_minor": 0}},
}

CLAUDE_EVENT = {
    "status": "allowed", "resetsAt": 1787796000, "rateLimitType": "five_hour",
    "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
    "isUsingOverage": False,
    "unifiedWindows": {"five_hour": {"utilization": 0.14, "resetsAt": 1787796000},
                       "seven_day": {"utilization": 0.03, "resetsAt": 1788328800}},
}

CODEX_USAGE = {
    "user_id": "user-x", "account_id": "acct", "email": "x@example.com", "plan_type": "plus",
    "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": {"used_percent": 0, "limit_window_seconds": 18000,
                           "reset_after_seconds": 18000, "reset_at": 1789114918},
        "secondary_window": {"used_percent": 8, "limit_window_seconds": 604800,
                             "reset_after_seconds": 350306, "reset_at": 1789447223},
    },
    "credits": {"has_credits": False}, "spend_control": {"reached": False},
    "rate_limit_reached_type": None,
}

# The app-server's shape, captured from codex 0.153.4 on 2026-09-11
# (``windowDurationMins``, not the ``windowMinutes`` one might guess).
CODEX_SNAPSHOT_CAMEL = {
    "limitId": "codex", "limitName": None, "planType": "plus", "rateLimitReachedType": None,
    "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1789114918},
    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1789447223},
    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    "individualLimit": None, "spendControlReached": None,
}

CODEX_SNAPSHOT_SNAKE = {
    "limit_name": "codex", "plan_type": "plus", "rate_limit_reached_type": "secondary",
    "primary": {"used_percent": 3, "window_minutes": 300, "resets_at": 1789114918},
    "secondary": {"used_percent": 100, "window_minutes": 10080, "resets_at": 1789447223},
}


class TestNormalizers:
    def test_claude_usage(self):
        w = sw.from_claude_usage(CLAUDE_USAGE)
        assert w.source == "poll"
        assert w.five_hour.pct == 0.0 and w.five_hour.resets_at is None
        assert w.seven_day.pct == 55.0
        assert w.seven_day.resets_at == datetime(2026, 9, 16, 5, 59, 59, 755375, tzinfo=timezone.utc)
        fable = w.scoped_for("fable")
        assert fable.label == "Fable" and fable.pct == 100.0 and fable.active
        assert w.reached == "scoped:fable"

    def test_claude_usage_per_model_fields(self):
        payload = dict(CLAUDE_USAGE, limits=[],
                       seven_day_opus={"utilization": 70.0, "resets_at": WEEK_RESET})
        w = sw.from_claude_usage(payload)
        assert w.scoped_for("opus").pct == 70.0 and not w.scoped_for("opus").active
        assert w.reached == ""

    def test_claude_event(self):
        w = sw.from_claude_event(CLAUDE_EVENT)
        assert w.source == "claude_event"
        assert round(w.five_hour.pct, 6) == 14.0
        assert w.five_hour.resets_at == datetime.fromtimestamp(1787796000, tz=timezone.utc)
        assert round(w.seven_day.pct, 6) == 3.0
        assert w.reached == ""
        rejected = dict(CLAUDE_EVENT, status="rejected", rateLimitType="seven_day")
        assert sw.from_claude_event(rejected).reached == "seven_day"
        rejected = dict(CLAUDE_EVENT, status="rejected", rateLimitType="seven_day_opus")
        assert sw.from_claude_event(rejected).reached == "scoped:opus"

    def test_claude_event_above_the_cap(self):
        info = {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 1.2, "resetsAt": 1787796000}}}
        w = sw.from_claude_event(info)
        assert round(w.five_hour.pct, 6) == 120.0 and w.seven_day is None

    def test_codex_usage(self):
        w = sw.from_codex_usage(CODEX_USAGE)
        assert w.plan == "plus"
        assert w.five_hour.pct == 0.0
        assert w.five_hour.resets_at == datetime.fromtimestamp(1789114918, tz=timezone.utc)
        assert w.seven_day.pct == 8.0
        assert w.seven_day.resets_at == datetime.fromtimestamp(1789447223, tz=timezone.utc)
        assert w.reached == ""

    def test_codex_usage_reached(self):
        payload = dict(CODEX_USAGE)
        payload["rate_limit"] = dict(CODEX_USAGE["rate_limit"], limit_reached=True,
                                     rate_limit_reached_type="secondary")
        assert sw.from_codex_usage(payload).reached == "seven_day"
        payload["rate_limit"] = dict(payload["rate_limit"], rate_limit_reached_type=None)
        # No type: the fullest window is the one that stopped the account.
        assert sw.from_codex_usage(payload).reached == "seven_day"

    def test_codex_usage_without_reset_at_uses_reset_after(self):
        payload = dict(CODEX_USAGE)
        payload["rate_limit"] = {"allowed": True, "limit_reached": False,
                                 "primary_window": {"used_percent": 5, "limit_window_seconds": 18000,
                                                    "reset_after_seconds": 3600}}
        w = sw.from_codex_usage(payload)
        assert w.five_hour.resets_at is not None
        assert abs((w.five_hour.resets_at - w.observed_at).total_seconds() - 3600) < 1

    def test_codex_snapshot_both_casings(self):
        camel = sw.from_codex_snapshot(CODEX_SNAPSHOT_CAMEL)
        assert camel.source == "codex_event" and camel.plan == "plus"
        assert camel.five_hour.pct == 12.0 and camel.seven_day.pct == 40.0
        assert camel.reached == ""
        snake = sw.from_codex_snapshot(CODEX_SNAPSHOT_SNAKE)
        assert snake.five_hour.pct == 3.0 and snake.seven_day.pct == 100.0
        assert snake.reached == "seven_day"
        # The older camelCase spelling of the length is accepted too.
        alt = sw.from_codex_snapshot({"primary": {"usedPercent": 5, "windowMinutes": 300,
                                                  "resetsAt": 1789114918}})
        assert alt.five_hour.pct == 5.0 and alt.seven_day is None

    def test_unrecognised_shapes(self):
        assert sw.from_claude_usage({"error": "x"}) is None
        assert sw.from_claude_usage("nope") is None
        assert sw.from_claude_event({"status": "allowed"}) is None
        assert sw.from_codex_usage({"plan_type": "plus"}) is None
        assert sw.from_codex_snapshot({"primary": "bad"}) is None
        assert sw.from_codex_snapshot({"primary": {"usedPercent": 1}}) is None  # no length

    def test_model_family(self):
        assert sw.model_family("claude-fable-5-1") == "fable"
        assert sw.model_family("claude-opus-5") == "opus"
        assert sw.model_family("Claude Sonnet 5 (1M)") == "sonnet"
        assert sw.model_family("gpt-5.6-sol") == ""
        assert sw.model_family("") == ""


class TestRowsAndEffective:
    def test_row_round_trip(self):
        w = sw.from_claude_usage(CLAUDE_USAGE)
        row = sw.to_row(w)
        assert row["five_hour_pct"] == 0.0 and row["five_hour_resets_at"] is None
        assert row["seven_day_pct"] == 55.0
        assert row["data"]["reached"] == "scoped:fable"
        stored = dict(row, subscription_id="s", id=1)
        back = sw.from_row(stored)
        assert back.seven_day.pct == 55.0 and back.seven_day.resets_at == w.seven_day.resets_at
        assert back.scoped_for("fable").active and back.reached == "scoped:fable"
        assert back.observed_at == w.observed_at

    def test_passed_reset_reads_empty_and_clears_reached(self):
        w = sw.Windows(
            five_hour=sw.Window(95.0, NOW - timedelta(minutes=5)),
            seven_day=sw.Window(60.0, NOW + timedelta(days=2)),
            reached="five_hour", observed_at=NOW - timedelta(minutes=30),
        )
        e = sw.effective(w, NOW)
        assert e.five_hour.pct == 0.0 and e.five_hour.resets_at is None
        assert e.seven_day.pct == 60.0
        assert e.reached == ""
        assert not sw.exhausted(e)

    def test_usage_without_reset_is_unknown(self):
        w = sw.Windows(five_hour=sw.Window(50.0, None), seven_day=sw.Window(0.0, None),
                       observed_at=NOW)
        e = sw.effective(w, NOW)
        assert e.five_hour is None            # usage but no reset: unknown
        assert e.seven_day.pct == 0.0         # an idle window is a known zero

    def test_stale_sample_is_unknown_per_window(self):
        w = sw.Windows(five_hour=sw.Window(95.0, NOW + timedelta(hours=1)),
                       seven_day=sw.Window(96.0, NOW + timedelta(days=3)),
                       observed_at=NOW - timedelta(hours=6))
        e = sw.effective(w, NOW)
        assert e.five_hour is None            # older than a session window
        assert e.seven_day.pct == 96.0        # still inside a week
        assert sw.exhausted(e)

    def test_scoped_reset_clears_active(self):
        w = sw.Windows(seven_day=sw.Window(55.0, NOW + timedelta(days=5)),
                       scoped=[sw.Scoped("fable", "Fable", 100.0, NOW - timedelta(seconds=1), True)],
                       reached="scoped:fable", observed_at=NOW)
        e = sw.effective(w, NOW)
        assert e.scoped_for("fable").pct == 0.0 and not e.scoped_for("fable").active
        assert e.reached == ""

    def test_public_payload(self):
        w = sw.from_codex_usage(CODEX_USAGE)
        # Judged at the fixture's own instant: the reset epochs in
        # CODEX_USAGE are real 2026-09-11 times that pass during the day.
        p = sw.to_public(sw.effective(w, NOW))
        assert p["five_hour"] == {
            "pct": 0.0,
            "resets_at": datetime.fromtimestamp(1789114918, tz=timezone.utc).isoformat(),
        }
        assert p["seven_day"]["pct"] == 8.0 and p["scoped"] == [] and p["plan"] == "plus"


class TestPoolQuestions:
    def _w(self, five=10.0, seven=50.0, scoped=(), reached=""):
        return sw.Windows(
            five_hour=sw.Window(five, NOW + timedelta(hours=2)),
            seven_day=sw.Window(seven, NOW + timedelta(days=3)),
            scoped=list(scoped), reached=reached, observed_at=NOW,
        )

    def test_overall_thresholds(self):
        assert not sw.exhausted(self._w(89.9, 94.9))
        assert sw.exhausted(self._w(90.0, 10.0))
        assert sw.exhausted(self._w(10.0, 95.0))
        assert sw.exhausted(self._w(reached="seven_day"))
        assert sw.exhausted_overall(self._w(reached="five_hour"))

    def test_scoped_window_only_for_its_family(self):
        fable_full = sw.Scoped("fable", "Fable", 100.0, NOW + timedelta(days=5), True)
        w = self._w(scoped=[fable_full], reached="scoped:fable")
        assert not sw.exhausted_overall(w)
        assert sw.exhausted(w, "claude-fable-5-1")
        assert not sw.exhausted(w, "claude-sonnet-5")
        assert not sw.exhausted(w, "")

    def test_frees_at_is_the_earliest_exhausting_window(self):
        w = sw.Windows(five_hour=sw.Window(95.0, NOW + timedelta(hours=1)),
                       seven_day=sw.Window(99.0, NOW + timedelta(days=3)),
                       observed_at=NOW)
        assert sw.frees_at(w) == NOW + timedelta(hours=1)
        assert sw.frees_at(self._w()) is None
        fable = sw.Scoped("fable", "Fable", 100.0, NOW + timedelta(days=5), True)
        assert sw.frees_at(self._w(scoped=[fable]), "claude-fable-5-1") == NOW + timedelta(days=5)
        assert sw.weekly_reset(self._w()) == NOW + timedelta(days=3)


_STORE = "storage.billing.subscription_store"
_POOL = "services.engines.subscription_pool"


class TestRecording:
    def test_record_schedules_a_rebalance_on_crossing(self):
        w = sw.Windows(five_hour=sw.Window(95.0, NOW + timedelta(hours=1)),
                       seven_day=sw.Window(20.0, NOW + timedelta(days=3)), observed_at=NOW)
        with patch.object(sw, "is_enabled", return_value=True), \
             patch(f"{_STORE}.latest_window_samples", return_value={}), \
             patch(f"{_STORE}.insert_window_sample", return_value=True) as ins, \
             patch(f"{_POOL}.schedule_rebalance") as reb:
            assert sw.record("sub-1", w)
        ins.assert_called_once()
        assert ins.call_args.args == ("sub-1",)
        reb.assert_called_once_with("window exhausted")

    def test_record_schedules_a_rebalance_on_a_scoped_crossing(self):
        """A per-model window filling up moves the scopes running that
        model too (the pass judges each scope by its model)."""
        before = sw.to_row(sw.Windows(five_hour=sw.Window(5.0, NOW + timedelta(hours=1)),
                                      seven_day=sw.Window(50.0, NOW + timedelta(days=3)),
                                      scoped=[sw.Scoped("fable", "Fable", 90.0, NOW + timedelta(days=3))],
                                      observed_at=NOW - timedelta(minutes=10)))
        w = sw.Windows(five_hour=sw.Window(6.0, NOW + timedelta(hours=1)),
                       seven_day=sw.Window(51.0, NOW + timedelta(days=3)),
                       scoped=[sw.Scoped("fable", "Fable", 100.0, NOW + timedelta(days=3), active=True)],
                       reached="scoped:fable", observed_at=NOW)
        with patch.object(sw, "is_enabled", return_value=True), \
             patch(f"{_STORE}.latest_window_samples",
                   return_value={"sub-1": dict(before, subscription_id="sub-1")}), \
             patch(f"{_STORE}.insert_window_sample", return_value=True), \
             patch(f"{_POOL}.schedule_rebalance") as reb:
            assert sw.record("sub-1", w)
        reb.assert_called_once_with("window exhausted")

    def test_record_without_crossing_is_quiet(self):
        already = sw.to_row(sw.Windows(five_hour=sw.Window(96.0, NOW + timedelta(hours=1)),
                                       seven_day=sw.Window(20.0, NOW + timedelta(days=3)),
                                       observed_at=NOW - timedelta(minutes=2)))
        w = sw.Windows(five_hour=sw.Window(97.0, NOW + timedelta(hours=1)),
                       seven_day=sw.Window(21.0, NOW + timedelta(days=3)), observed_at=NOW)
        with patch.object(sw, "is_enabled", return_value=True), \
             patch(f"{_STORE}.latest_window_samples",
                   return_value={"sub-1": dict(already, subscription_id="sub-1")}), \
             patch(f"{_STORE}.insert_window_sample", return_value=True), \
             patch(f"{_POOL}.schedule_rebalance") as reb:
            assert sw.record("sub-1", w)
        reb.assert_not_called()

    def test_record_for_session_resolves_the_binding(self):
        w = sw.Windows(seven_day=sw.Window(1.0, NOW + timedelta(days=3)), observed_at=NOW)
        with patch.object(sw, "is_enabled", return_value=True), \
             patch(f"{_STORE}.latest_window_samples", return_value={}), \
             patch(f"{_STORE}.insert_window_sample", return_value=True) as ins, \
             patch(f"{_POOL}.schedule_rebalance"), \
             patch(f"{_POOL}.get_session_subscription", return_value="sub-9") as bound:
            assert sw.record_for_session("sess", w)
            bound.return_value = None
            assert not sw.record_for_session("sess", w)
            bound.return_value = "default"
            assert not sw.record_for_session("sess", w)
        assert ins.call_count == 1
        assert ins.call_args.args == ("sub-9",)

    def test_disabled_records_nothing(self):
        w = sw.Windows(seven_day=sw.Window(1.0, NOW + timedelta(days=3)), observed_at=NOW)
        with patch.object(sw, "is_enabled", return_value=False), \
             patch(f"{_STORE}.insert_window_sample") as ins:
            assert not sw.record("sub-1", w)
            assert not sw.record_for_session("sess", w)
        ins.assert_not_called()

    def test_setting_cache(self):
        sw.invalidate_setting_cache()
        with patch("storage.database.get_platform_setting", return_value="") as get:
            assert sw.is_enabled()                     # unset means on
            get.return_value = "0"
            assert sw.is_enabled()                     # cached for a minute
            sw.invalidate_setting_cache()
            assert not sw.is_enabled()
        sw.invalidate_setting_cache()


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def aclose(self):
        pass


_CLAUDE_SUB = {"id": "sub-claude", "layer": "claude-code-cli", "auth_type": "oauth", "status": "active"}
_CODEX_SUB = {"id": "sub-codex", "layer": "codex-cli", "auth_type": "oauth", "status": "active"}
_CLAUDE_CRED = {"oauth_token": {"accessToken": "sk-ant-oat01-x", "refreshToken": "r"}}
_CODEX_CRED = {"oauth_token": {"accessToken": "eyJ.jwt"},
               "codex_auth_blob": {"tokens": {"access_token": "eyJ.jwt", "account_id": "acct-1"}}}


class TestPollRequests:
    def test_claude_headers(self):
        url, headers = sw.poll_request(_CLAUDE_SUB, _CLAUDE_CRED)
        assert url == sw.CLAUDE_USAGE_URL
        assert headers["Authorization"] == "Bearer sk-ant-oat01-x"
        assert headers["anthropic-beta"] == "oauth-2025-04-20"
        assert headers["User-Agent"].startswith("OtoDock/")

    def test_codex_headers(self):
        url, headers = sw.poll_request(_CODEX_SUB, _CODEX_CRED)
        assert url == sw.CODEX_USAGE_URL
        assert headers["Authorization"] == "Bearer eyJ.jwt"
        assert headers["ChatGPT-Account-Id"] == "acct-1"
        assert "anthropic-beta" not in headers

    def test_missing_token_or_other_layer(self):
        assert sw.poll_request(_CLAUDE_SUB, {}) is None
        assert sw.poll_request({"id": "x", "layer": "direct-llm"}, _CLAUDE_CRED) is None


class TestPollOne:
    def setup_method(self):
        sw.reset_poll_state()

    def _run(self, sub, cred, client, *, now=1000.0):
        with patch(f"{_STORE}.get_credential_data", return_value=cred), \
             patch.object(sw, "record", return_value=True) as rec:
            ok = asyncio.run(sw.poll_one(sub, client, now=now))
        return ok, rec

    def test_success_records_and_clears_backoff(self):
        client = _Client(_Resp(200, CLAUDE_USAGE))
        ok, rec = self._run(_CLAUDE_SUB, _CLAUDE_CRED, client)
        assert ok
        rec.assert_called_once()
        sub_id, windows = rec.call_args.args
        assert sub_id == "sub-claude" and windows.seven_day.pct == 55.0
        assert not sw._backed_off("sub-claude", 1000.0)

    def test_401_backs_off_without_refreshing(self):
        client = _Client(_Resp(401), _Resp(200, CLAUDE_USAGE))
        ok, rec = self._run(_CLAUDE_SUB, _CLAUDE_CRED, client, now=1000.0)
        assert not ok and rec.call_count == 0
        # Inside the backoff: not even a request.
        ok, rec = self._run(_CLAUDE_SUB, _CLAUDE_CRED, client, now=1030.0)
        assert not ok and len(client.calls) == 1
        # After it: asked again, and success clears the state.
        ok, rec = self._run(_CLAUDE_SUB, _CLAUDE_CRED, client, now=1000.0 + sw._BACKOFF_MIN_S)
        assert ok and len(client.calls) == 2

    def test_backoff_doubles_to_the_cap(self):
        client = _Client(*[RuntimeError("boom")] * 8)
        now = 0.0
        waits = []
        for _ in range(7):
            self._run(_CODEX_SUB, _CODEX_CRED, client, now=now)
            waits.append(sw._backoff["sub-codex"][1])
            now = sw._backoff["sub-codex"][0]
        assert waits[0] == sw._BACKOFF_MIN_S
        assert waits[-1] == sw._BACKOFF_MAX_S
        assert waits == sorted(waits)

    def test_unrecognised_shape_backs_off_and_warns_once(self):
        client = _Client(_Resp(200, {"error": "gone"}), _Resp(200, {"error": "gone"}))
        with patch.object(sw.logger, "warning") as warn:
            ok, rec = self._run(_CLAUDE_SUB, _CLAUDE_CRED, client, now=1000.0)
            assert not ok and rec.call_count == 0 and warn.call_count == 1
            self._run(_CLAUDE_SUB, _CLAUDE_CRED, client, now=1000.0 + sw._BACKOFF_MIN_S)
            assert warn.call_count == 1          # once an hour, not per attempt

    def test_credential_without_token_backs_off(self):
        client = _Client(_Resp(200, CLAUDE_USAGE))
        ok, rec = self._run(_CLAUDE_SUB, {}, client)
        assert not ok and client.calls == [] and sw._backed_off("sub-claude", 1000.0)


class TestPollDue:
    def setup_method(self):
        sw.reset_poll_state()

    def _run(self, rows, latest, client, *, enabled=True, air_gapped=False):
        now = datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc)
        with patch.object(sw, "is_enabled", return_value=enabled), \
             patch.object(sw, "_air_gapped", return_value=air_gapped), \
             patch(f"{_STORE}.list_subscriptions", return_value=rows), \
             patch(f"{_STORE}.latest_window_samples", return_value=latest), \
             patch(f"{_STORE}.prune_window_samples", return_value=0) as prune, \
             patch(f"{_STORE}.get_credential_data",
                   side_effect=lambda sid: _CODEX_CRED if "codex" in sid else _CLAUDE_CRED), \
             patch.object(sw, "record", return_value=True):
            polled = asyncio.run(sw.poll_due(client=client, now=now))
        return polled, prune

    def test_polls_only_stale_active_oauth_rows(self):
        rows = [
            _CLAUDE_SUB,                                              # no sample: due
            _CODEX_SUB,                                               # fresh sample: not due
            dict(_CLAUDE_SUB, id="sub-expired", status="expired"),    # never
            {"id": "sub-key", "layer": "claude-code-cli", "auth_type": "api_key", "status": "active"},
            dict(_CLAUDE_SUB, id="sub-old"),                          # old sample: due
        ]
        latest = {
            "sub-codex": {"observed_at": "2026-09-11T05:58:00+00:00"},   # 2 min: fresh
            "sub-old": {"observed_at": "2026-09-11T05:55:00+00:00"},     # 5 min: a tick old, due
        }
        client = _Client(_Resp(200, CLAUDE_USAGE), _Resp(200, CLAUDE_USAGE))
        polled, prune = self._run(rows, latest, client)
        assert polled == 2
        assert [h["Authorization"] for _, h in client.calls] == ["Bearer sk-ant-oat01-x"] * 2
        prune.assert_called_once()
        assert prune.call_args.args[0].startswith("2026-09-03T06:00:00")

    def test_disabled_or_air_gapped_never_asks(self):
        client = _Client(_Resp(200, CLAUDE_USAGE))
        assert self._run([_CLAUDE_SUB], {}, client, enabled=False)[0] == 0
        assert self._run([_CLAUDE_SUB], {}, client, air_gapped=True)[0] == 0
        assert client.calls == []

    def test_schedule_poll_without_a_loop_is_quiet(self):
        with patch.object(sw, "is_enabled", return_value=True):
            sw.schedule_poll("sub-claude")   # no running loop: nothing to do, no error


class TestReachedFromPercent:
    """``is_active`` is the vendor's "watch this one" (true on a session
    window at 85 % on 2026-09-11), not a refusal: ``reached`` needs 100 %."""

    def test_active_below_full_is_a_spill_mark_not_reached(self):
        payload = dict(CLAUDE_USAGE, five_hour={"utilization": 85.0, "resets_at": WEEK_RESET})
        payload["limits"] = [
            {"kind": "session", "group": "session", "percent": 85, "severity": "warning",
             "resets_at": WEEK_RESET, "scope": None, "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 60, "severity": "normal",
             "resets_at": WEEK_RESET,
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
             "is_active": True},
        ]
        w = sw.from_claude_usage(payload)
        assert w.reached == ""
        assert w.scoped_for("fable").active and w.scoped_for("fable").pct == 60.0
        # The flag is display-only: a Fable window at 60 % serves Fable
        # (2026-09-11: it sat on a 61 % window and, read as exhaustion, made
        # the pool believe both accounts were out of Fable).
        assert not sw.exhausted(w, "claude-fable-5-1")
        assert sw.frees_at(w, "claude-fable-5-1") is None
        assert not sw.exhausted_any(w)
        # The account is not exhausted overall either (85 < the 90 % spill mark).
        assert not sw.exhausted_overall(w)

    def test_full_windows_are_reached_with_or_without_limits(self):
        payload = dict(CLAUDE_USAGE, five_hour={"utilization": 100.0, "resets_at": WEEK_RESET},
                       limits=[])
        assert sw.from_claude_usage(payload).reached == "five_hour"
        payload = dict(CLAUDE_USAGE, seven_day={"utilization": 100.0, "resets_at": WEEK_RESET},
                       limits=[])
        assert sw.from_claude_usage(payload).reached == "seven_day"
        # The limits[] entry at 100 % names the window too.
        payload = dict(CLAUDE_USAGE, limits=[
            {"kind": "session", "group": "session", "percent": 100, "severity": "critical",
             "resets_at": WEEK_RESET, "scope": None, "is_active": True}])
        assert sw.from_claude_usage(payload).reached == "five_hour"

    def test_exhausted_any_covers_scoped_windows(self):
        w = sw.from_claude_usage(CLAUDE_USAGE)      # Fable 100 %, overall fine
        assert sw.exhausted_any(w) and not sw.exhausted_overall(w)
        payload = dict(CLAUDE_USAGE, limits=[])
        assert not sw.exhausted_any(sw.from_claude_usage(payload))


class TestMergedReading:
    """A stream event carries the overall windows only; the per-model
    windows come from the newest poll until the next poll. Live-observed
    2026-09-11: two headless turns hid a Fable window at 100 % for ten
    minutes and the pool sent Fable work to that account."""

    def _poll(self, at, fable=100.0):
        return sw.to_row(sw.Windows(
            five_hour=sw.Window(3.0, at + timedelta(hours=2)),
            seven_day=sw.Window(56.0, at + timedelta(days=5)),
            scoped=[sw.Scoped("fable", "Fable", fable, at + timedelta(days=5), True)],
            reached="scoped:fable" if fable >= 100 else "", observed_at=at, source="poll"))

    def _event(self, at):
        return sw.to_row(sw.Windows(
            five_hour=sw.Window(2.0, at + timedelta(hours=2)),
            seven_day=sw.Window(55.0, at + timedelta(days=5)),
            observed_at=at, source="claude_event"))

    def test_event_keeps_the_polls_scoped_windows(self):
        poll = self._poll(NOW - timedelta(minutes=8))
        event = self._event(NOW - timedelta(minutes=1))
        r = sw.merged_reading(event, poll, NOW)
        assert r.source == "claude_event" and r.five_hour.pct == 2.0
        assert r.scoped_for("fable").pct == 100.0 and r.reached == "scoped:fable"
        assert sw.exhausted(r, "claude-fable-5-1")
        # No poll at all: the event stands alone, and a poll as the newest
        # sample is taken as it is.
        assert sw.merged_reading(event, None, NOW).scoped == []
        assert sw.merged_reading(poll, None, NOW).scoped_for("fable").pct == 100.0

    def test_polls_scoped_windows_settle_by_their_own_reset(self):
        stale = sw.to_row(sw.Windows(
            five_hour=sw.Window(3.0, NOW + timedelta(hours=2)),
            seven_day=sw.Window(56.0, NOW + timedelta(days=5)),
            scoped=[sw.Scoped("fable", "Fable", 100.0, NOW - timedelta(minutes=5), True)],
            reached="scoped:fable", observed_at=NOW - timedelta(minutes=8), source="poll"))
        r = sw.merged_reading(self._event(NOW), stale, NOW)
        assert r.scoped_for("fable").pct == 0.0 and r.reached == ""

    def test_latest_readings_asks_for_polls_only_when_needed(self):
        calls = []

        class _Store:
            def latest_window_samples(self, ids, *, source=None):
                calls.append((tuple(ids), source))
                if source == "poll":
                    return {"sub-e": dict(self._poll, subscription_id="sub-e")}
                return {"sub-e": dict(self._event, subscription_id="sub-e"),
                        "sub-p": dict(self._poll, subscription_id="sub-p")}
        store = _Store()
        store._poll = self._poll(NOW - timedelta(minutes=8))
        store._event = self._event(NOW - timedelta(minutes=1))
        readings = sw.latest_readings(store, ["sub-e", "sub-p"], NOW)
        assert readings["sub-e"].scoped_for("fable").pct == 100.0
        assert readings["sub-p"].scoped_for("fable").pct == 100.0
        assert calls == [(("sub-e", "sub-p"), None), (("sub-e",), "poll")]
