"""Provider windows: the two engines' normalizers, the effective reading and
the pool's questions (``services/engines/subscription_windows.py`` and the
engines' ``usage`` modules). No DB: the store is mocked where ``record``
touches it. The vendor shapes are cut from the 2026-09-11 T1 probe bodies
and the headless-stream fixture (``tests/fixtures/cli_wake/probe-race.jsonl``).

Every reading carries the engine's declared window SPECS (engine-contract
lane, phase 3b): the two CLI engines declare the same ``five_hour`` session
window and ``seven_day`` quota window, so the fixtures below use that pair.
The vendor parsers live in the engine packages (phase 3c-ii); the generic
module only settles, compares and stores what they produce.

Run: cd proxy && python -m pytest tests/billing/test_subscription_windows_normalize.py -v
"""

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core.execution_layer import WindowSpec  # noqa: E402
from core.layers.cli import usage as claude_usage  # noqa: E402
from core.layers.codex import usage as codex_usage  # noqa: E402
from core.session.session_manager import get_layer_by_path  # noqa: E402
from services.engines import subscription_windows as sw  # noqa: E402

NOW = datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc)
WEEK_RESET = "2026-09-16T05:59:59.755375+00:00"

SPECS = {
    "five_hour": WindowSpec("five_hour", 5 * 3600, "session", "session"),
    "seven_day": WindowSpec("seven_day", 7 * 86400, "quota", "weekly"),
}


def _W(five=None, seven=None, **kw):
    """A reading over the two CLI-engine windows."""
    windows = {}
    if five is not None:
        windows["five_hour"] = five
    if seven is not None:
        windows["seven_day"] = seven
    return sw.Windows(specs=SPECS, windows=windows, **kw)


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


class TestClaudeNormalizers:
    def test_usage(self):
        w = claude_usage.from_usage(CLAUDE_USAGE, SPECS)
        assert w.source == "poll"
        five, seven = w.windows["five_hour"], w.windows["seven_day"]
        assert five.pct == 0.0 and five.resets_at is None
        assert seven.pct == 55.0
        assert seven.resets_at == datetime(2026, 9, 16, 5, 59, 59, 755375, tzinfo=timezone.utc)
        fable = w.scoped_for("fable")
        assert fable.label == "Fable" and fable.pct == 100.0 and fable.active
        assert w.reached == "scoped:fable"

    def test_usage_per_model_fields(self):
        payload = dict(CLAUDE_USAGE, limits=[],
                       seven_day_opus={"utilization": 70.0, "resets_at": WEEK_RESET})
        w = claude_usage.from_usage(payload, SPECS)
        assert w.scoped_for("opus").pct == 70.0 and not w.scoped_for("opus").active
        assert w.reached == ""

    def test_event(self):
        w = claude_usage.from_event(CLAUDE_EVENT, SPECS)
        assert w.source == "claude_event"
        assert round(w.windows["five_hour"].pct, 6) == 14.0
        assert w.windows["five_hour"].resets_at == datetime.fromtimestamp(1787796000, tz=timezone.utc)
        assert round(w.windows["seven_day"].pct, 6) == 3.0
        assert w.reached == ""
        rejected = dict(CLAUDE_EVENT, status="rejected", rateLimitType="seven_day")
        assert claude_usage.from_event(rejected, SPECS).reached == "seven_day"
        rejected = dict(CLAUDE_EVENT, status="rejected", rateLimitType="seven_day_opus")
        assert claude_usage.from_event(rejected, SPECS).reached == "scoped:opus"

    def test_event_above_the_cap(self):
        info = {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 1.2, "resetsAt": 1787796000}}}
        w = claude_usage.from_event(info, SPECS)
        assert round(w.windows["five_hour"].pct, 6) == 120.0 and "seven_day" not in w.windows

    def test_unrecognised_shapes(self):
        assert claude_usage.from_usage({"error": "x"}, SPECS) is None
        assert claude_usage.from_usage("nope", SPECS) is None
        assert claude_usage.from_event({"status": "allowed"}, SPECS) is None
        assert claude_usage.from_event(CLAUDE_EVENT, {}) is None    # nothing declared

    def test_model_family_is_the_scope_key(self):
        assert claude_usage.model_family("claude-fable-5-1") == "fable"
        assert claude_usage.model_family("claude-opus-5") == "opus"
        assert claude_usage.model_family("Claude Sonnet 5 (1M)") == "sonnet"
        assert claude_usage.model_family("gpt-5.6-sol") == ""
        assert claude_usage.model_family("") == ""
        # The engines answer the same question through the contract.
        assert get_layer_by_path("claude-code-cli").usage_scope_key("claude-fable-5-1") == "fable"
        assert get_layer_by_path("codex-cli").usage_scope_key("gpt-5.6-sol") == ""
        assert get_layer_by_path("direct-llm").usage_scope_key("claude-opus-5") == ""

    def test_the_engine_parses_against_its_own_declaration(self):
        w = get_layer_by_path("claude-code-cli").parse_usage(CLAUDE_USAGE)
        assert w.windows["seven_day"].pct == 55.0 and w.specs == sw.window_specs("claude-code-cli")
        assert get_layer_by_path("direct-llm").parse_usage(CLAUDE_USAGE) is None


class TestCodexNormalizers:
    def test_usage(self):
        w = codex_usage.from_usage(CODEX_USAGE, SPECS)
        assert w.plan == "plus"
        assert w.windows["five_hour"].pct == 0.0
        assert w.windows["five_hour"].resets_at == datetime.fromtimestamp(1789114918, tz=timezone.utc)
        assert w.windows["seven_day"].pct == 8.0
        assert w.windows["seven_day"].resets_at == datetime.fromtimestamp(1789447223, tz=timezone.utc)
        assert w.reached == ""

    def test_usage_reached(self):
        payload = dict(CODEX_USAGE)
        payload["rate_limit"] = dict(CODEX_USAGE["rate_limit"], limit_reached=True,
                                     rate_limit_reached_type="secondary")
        assert codex_usage.from_usage(payload, SPECS).reached == "seven_day"
        payload["rate_limit"] = dict(payload["rate_limit"], rate_limit_reached_type=None)
        # No type: the fullest window is the one that stopped the account.
        assert codex_usage.from_usage(payload, SPECS).reached == "seven_day"

    def test_usage_without_reset_at_uses_reset_after(self):
        payload = dict(CODEX_USAGE)
        payload["rate_limit"] = {"allowed": True, "limit_reached": False,
                                 "primary_window": {"used_percent": 5, "limit_window_seconds": 18000,
                                                    "reset_after_seconds": 3600}}
        w = codex_usage.from_usage(payload, SPECS)
        five = w.windows["five_hour"]
        assert five.resets_at is not None
        assert abs((five.resets_at - w.observed_at).total_seconds() - 3600) < 1

    def test_snapshot_both_casings(self):
        camel = codex_usage.from_snapshot(CODEX_SNAPSHOT_CAMEL, SPECS)
        assert camel.source == "codex_event" and camel.plan == "plus"
        assert camel.windows["five_hour"].pct == 12.0 and camel.windows["seven_day"].pct == 40.0
        assert camel.reached == ""
        snake = codex_usage.from_snapshot(CODEX_SNAPSHOT_SNAKE, SPECS)
        assert snake.windows["five_hour"].pct == 3.0 and snake.windows["seven_day"].pct == 100.0
        assert snake.reached == "seven_day"
        # The older camelCase spelling of the length is accepted too.
        alt = codex_usage.from_snapshot({"primary": {"usedPercent": 5, "windowMinutes": 300,
                                                     "resetsAt": 1789114918}}, SPECS)
        assert alt.windows["five_hour"].pct == 5.0 and "seven_day" not in alt.windows

    def test_length_maps_to_the_declared_window_by_threshold(self):
        # The boundary the parser always used: a report up to 6 h is the
        # session window, anything longer the quota window; a length no
        # declared window covers goes to the longest.
        by = lambda s: sw.spec_for_length(SPECS, s).key  # noqa: E731
        assert by(18000) == "five_hour" and by(6 * 3600) == "five_hour"
        assert by(6 * 3600 + 1) == "seven_day" and by(24 * 3600) == "seven_day"
        assert by(604800) == "seven_day" and by(30 * 86400) == "seven_day"
        assert sw.spec_for_length(SPECS, None) is None
        assert sw.spec_for_length({}, 18000) is None

    def test_unrecognised_shapes(self):
        assert codex_usage.from_usage({"plan_type": "plus"}, SPECS) is None
        assert codex_usage.from_snapshot({"primary": "bad"}, SPECS) is None
        assert codex_usage.from_snapshot({"primary": {"usedPercent": 1}}, SPECS) is None  # no length
        assert codex_usage.from_usage(CODEX_USAGE, {}) is None                        # nothing declared


class TestRowsAndEffective:
    def test_row_round_trip(self):
        w = claude_usage.from_usage(CLAUDE_USAGE, SPECS)
        row = sw.to_row(w)
        assert row["five_hour_pct"] == 0.0 and row["five_hour_resets_at"] is None
        assert row["seven_day_pct"] == 55.0
        assert row["data"]["reached"] == "scoped:fable"
        assert "windows" not in row["data"]          # both keys are column-backed
        stored = dict(row, subscription_id="s", id=1)
        back = sw.from_row(stored, SPECS)
        assert back.windows["seven_day"].pct == 55.0
        assert back.windows["seven_day"].resets_at == w.windows["seven_day"].resets_at
        assert back.scoped_for("fable").active and back.reached == "scoped:fable"
        assert back.observed_at == w.observed_at

    def test_undeclared_key_rides_in_the_sample_data(self):
        # A fourth engine's window under a key the sample table has no
        # column for is stored in the JSONB ``data`` and read back from it —
        # no migration for a monthly quota.
        specs = {"monthly": WindowSpec("monthly", 30 * 86400, "quota", "monthly")}
        reset = NOW + timedelta(days=12)
        w = sw.Windows(specs=specs, windows={"monthly": sw.Window(42.0, reset)}, observed_at=NOW)
        row = sw.to_row(w)
        assert row["five_hour_pct"] is None and row["seven_day_pct"] is None
        assert row["data"]["windows"] == {"monthly": {"pct": 42.0, "resets_at": reset.isoformat()}}
        back = sw.from_row(dict(row, subscription_id="s", id=1), specs)
        assert back.windows["monthly"].pct == 42.0 and back.windows["monthly"].resets_at == reset
        assert sw.sample_window(row, "monthly").pct == 42.0
        assert sw.sample_window(row, "seven_day") is None
        assert sw.quota_reset(back) == reset
        assert sw.to_public(back)["monthly"]["pct"] == 42.0

    def test_passed_reset_reads_empty_and_clears_reached(self):
        w = _W(
            five=sw.Window(95.0, NOW - timedelta(minutes=5)),
            seven=sw.Window(60.0, NOW + timedelta(days=2)),
            reached="five_hour", observed_at=NOW - timedelta(minutes=30),
        )
        e = sw.effective(w, NOW)
        assert e.windows["five_hour"].pct == 0.0 and e.windows["five_hour"].resets_at is None
        assert e.windows["seven_day"].pct == 60.0
        assert e.reached == ""
        assert e.specs == SPECS
        assert not sw.exhausted(e)

    def test_usage_without_reset_is_unknown(self):
        w = _W(five=sw.Window(50.0, None), seven=sw.Window(0.0, None), observed_at=NOW)
        e = sw.effective(w, NOW)
        assert "five_hour" not in e.windows          # usage but no reset: unknown
        assert e.windows["seven_day"].pct == 0.0     # an idle window is a known zero

    def test_stale_sample_is_unknown_per_window(self):
        w = _W(five=sw.Window(95.0, NOW + timedelta(hours=1)),
               seven=sw.Window(96.0, NOW + timedelta(days=3)),
               observed_at=NOW - timedelta(hours=6))
        e = sw.effective(w, NOW)
        assert "five_hour" not in e.windows          # older than a session window
        assert e.windows["seven_day"].pct == 96.0    # still inside a week
        assert sw.exhausted(e)

    def test_scoped_reset_clears_active(self):
        w = _W(seven=sw.Window(55.0, NOW + timedelta(days=5)),
               scoped=[sw.Scoped("fable", "Fable", 100.0, NOW - timedelta(seconds=1), True)],
               reached="scoped:fable", observed_at=NOW)
        e = sw.effective(w, NOW)
        assert e.scoped_for("fable").pct == 0.0 and not e.scoped_for("fable").active
        assert e.reached == ""

    def test_public_payload(self):
        w = codex_usage.from_usage(CODEX_USAGE, SPECS)
        # Judged at the fixture's own instant: the reset epochs in
        # CODEX_USAGE are real 2026-09-11 times that pass during the day.
        p = sw.to_public(sw.effective(w, NOW))
        assert p["five_hour"] == {
            "pct": 0.0,
            "resets_at": datetime.fromtimestamp(1789114918, tz=timezone.utc).isoformat(),
        }
        assert p["seven_day"]["pct"] == 8.0 and p["scoped"] == [] and p["plan"] == "plus"
        # The dashboard's SubscriptionWindows shape, key for key.
        assert list(p) == ["five_hour", "seven_day", "scoped", "reached", "plan",
                           "observed_at", "source"]
        # A declared window the reading lacks is null, not absent.
        assert sw.to_public(_W(observed_at=NOW))["five_hour"] is None


class TestPoolQuestions:
    def _w(self, five=10.0, seven=50.0, scoped=(), reached=""):
        return _W(
            five=sw.Window(five, NOW + timedelta(hours=2)),
            seven=sw.Window(seven, NOW + timedelta(days=3)),
            scoped=list(scoped), reached=reached, observed_at=NOW,
        )

    def test_overall_thresholds_follow_the_role_spill_marks(self):
        assert sw.SPILL_PCT == {"session": 90.0, "quota": 95.0}
        assert not sw.exhausted(self._w(89.9, 94.9))
        assert sw.exhausted(self._w(90.0, 10.0))
        assert sw.exhausted(self._w(10.0, 95.0))
        assert sw.exhausted(self._w(reached="seven_day"))
        assert sw.exhausted_overall(self._w(reached="five_hour"))

    def test_scoped_window_only_for_its_key(self):
        # The pool passes the ENGINE's scope key for the spawn's model.
        fable_full = sw.Scoped("fable", "Fable", 100.0, NOW + timedelta(days=5), True)
        w = self._w(scoped=[fable_full], reached="scoped:fable")
        assert not sw.exhausted_overall(w)
        assert sw.exhausted(w, "fable")
        assert not sw.exhausted(w, "sonnet")
        assert not sw.exhausted(w, "")

    def test_frees_at_is_the_earliest_exhausting_window(self):
        w = _W(five=sw.Window(95.0, NOW + timedelta(hours=1)),
               seven=sw.Window(99.0, NOW + timedelta(days=3)),
               observed_at=NOW)
        assert sw.frees_at(w) == NOW + timedelta(hours=1)
        assert sw.frees_at(self._w()) is None
        fable = sw.Scoped("fable", "Fable", 100.0, NOW + timedelta(days=5), True)
        assert sw.frees_at(self._w(scoped=[fable]), "fable") == NOW + timedelta(days=5)
        assert sw.quota_reset(self._w()) == NOW + timedelta(days=3)
        assert sw.quota_reset(_W(observed_at=NOW)) is None


_STORE = "storage.billing.subscription_store"
_POOL = "services.engines.subscription_pool"


class TestRecording:
    def test_record_schedules_a_rebalance_on_crossing(self):
        w = _W(five=sw.Window(95.0, NOW + timedelta(hours=1)),
               seven=sw.Window(20.0, NOW + timedelta(days=3)), observed_at=NOW)
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
        before = sw.to_row(_W(five=sw.Window(5.0, NOW + timedelta(hours=1)),
                              seven=sw.Window(50.0, NOW + timedelta(days=3)),
                              scoped=[sw.Scoped("fable", "Fable", 90.0, NOW + timedelta(days=3))],
                              observed_at=NOW - timedelta(minutes=10)))
        w = _W(five=sw.Window(6.0, NOW + timedelta(hours=1)),
               seven=sw.Window(51.0, NOW + timedelta(days=3)),
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
        already = sw.to_row(_W(five=sw.Window(96.0, NOW + timedelta(hours=1)),
                               seven=sw.Window(20.0, NOW + timedelta(days=3)),
                               observed_at=NOW - timedelta(minutes=2)))
        w = _W(five=sw.Window(97.0, NOW + timedelta(hours=1)),
               seven=sw.Window(21.0, NOW + timedelta(days=3)), observed_at=NOW)
        with patch.object(sw, "is_enabled", return_value=True), \
             patch(f"{_STORE}.latest_window_samples",
                   return_value={"sub-1": dict(already, subscription_id="sub-1")}), \
             patch(f"{_STORE}.insert_window_sample", return_value=True), \
             patch(f"{_POOL}.schedule_rebalance") as reb:
            assert sw.record("sub-1", w)
        reb.assert_not_called()

    def test_record_for_session_resolves_the_binding(self):
        w = _W(seven=sw.Window(1.0, NOW + timedelta(days=3)), observed_at=NOW)
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

    def test_engine_owned_recorders_reach_the_session_binding(self):
        # The Claude session and the Codex translator/tailer record through
        # their own usage modules; shared code through the layer method.
        with patch.object(sw, "record_for_session", return_value=True) as rec:
            codex_usage.record_snapshot("s1", CODEX_SNAPSHOT_SNAKE, "2026-09-11T05:00:00Z")
            sid, w = rec.call_args.args
            assert sid == "s1" and w.windows["seven_day"].pct == 100.0
            assert w.observed_at == datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc)
            assert not codex_usage.record_snapshot("s1", {"primary": "bad"})
        with patch.object(sw, "record_for_session_async") as rec:
            get_layer_by_path("claude-code-cli").record_usage_event("s2", CLAUDE_EVENT)
            sid, w = rec.call_args.args
            assert sid == "s2" and round(w.windows["five_hour"].pct, 6) == 14.0
            get_layer_by_path("direct-llm").record_usage_event("s3", CLAUDE_EVENT)
            assert rec.call_count == 1

    def test_disabled_records_nothing(self):
        w = _W(seven=sw.Window(1.0, NOW + timedelta(days=3)), observed_at=NOW)
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
        assert url == claude_usage.USAGE_URL
        assert headers["Authorization"] == "Bearer sk-ant-oat01-x"
        assert headers["anthropic-beta"] == "oauth-2025-04-20"
        assert headers["User-Agent"].startswith("OtoDock/")       # the platform's, not the engine's

    def test_codex_headers(self):
        url, headers = sw.poll_request(_CODEX_SUB, _CODEX_CRED)
        assert url == codex_usage.USAGE_URL
        assert headers["Authorization"] == "Bearer eyJ.jwt"
        assert headers["ChatGPT-Account-Id"] == "acct-1"
        assert "anthropic-beta" not in headers
        assert headers["User-Agent"].startswith("OtoDock/")

    def test_missing_token_or_an_engine_without_windows(self):
        assert sw.poll_request(_CLAUDE_SUB, {}) is None
        assert sw.poll_request({"id": "x", "layer": "direct-llm"}, _CLAUDE_CRED) is None
        assert sw.poll_request({"id": "x", "layer": "acme-cli"}, _CLAUDE_CRED) is None

    def test_specs_come_from_the_engines_declaration(self):
        assert list(sw.window_specs("claude-code-cli")) == ["five_hour", "seven_day"]
        assert list(sw.window_specs("codex-cli")) == ["five_hour", "seven_day"]
        assert sw.window_specs("direct-llm") == {}
        assert sw.window_specs("acme-cli") == {}          # unregistered: nothing, no raise
        assert sw.quota_spec(sw.window_specs("codex-cli")).key == "seven_day"


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
        assert sub_id == "sub-claude" and windows.windows["seven_day"].pct == 55.0
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

    def test_polls_only_stale_active_oauth_rows_on_engines_with_windows(self):
        rows = [
            _CLAUDE_SUB,                                              # no sample: due
            _CODEX_SUB,                                               # fresh sample: not due
            dict(_CLAUDE_SUB, id="sub-expired", status="expired"),    # never
            {"id": "sub-key", "layer": "claude-code-cli", "auth_type": "api_key", "status": "active"},
            {"id": "sub-direct", "layer": "direct-llm", "auth_type": "oauth", "status": "active"},  # no windows declared
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
        w = claude_usage.from_usage(payload, SPECS)
        assert w.reached == ""
        assert w.scoped_for("fable").active and w.scoped_for("fable").pct == 60.0
        # The flag is display-only: a Fable window at 60 % serves Fable
        # (2026-09-11: it sat on a 61 % window and, read as exhaustion, made
        # the pool believe both accounts were out of Fable).
        assert not sw.exhausted(w, "fable")
        assert sw.frees_at(w, "fable") is None
        assert not sw.exhausted_any(w)
        # The account is not exhausted overall either (85 < the 90 % spill mark).
        assert not sw.exhausted_overall(w)

    def test_full_windows_are_reached_with_or_without_limits(self):
        payload = dict(CLAUDE_USAGE, five_hour={"utilization": 100.0, "resets_at": WEEK_RESET},
                       limits=[])
        assert claude_usage.from_usage(payload, SPECS).reached == "five_hour"
        payload = dict(CLAUDE_USAGE, seven_day={"utilization": 100.0, "resets_at": WEEK_RESET},
                       limits=[])
        assert claude_usage.from_usage(payload, SPECS).reached == "seven_day"
        # The limits[] entry at 100 % names the window too.
        payload = dict(CLAUDE_USAGE, limits=[
            {"kind": "session", "group": "session", "percent": 100, "severity": "critical",
             "resets_at": WEEK_RESET, "scope": None, "is_active": True}])
        assert claude_usage.from_usage(payload, SPECS).reached == "five_hour"

    def test_exhausted_any_covers_scoped_windows(self):
        w = claude_usage.from_usage(CLAUDE_USAGE, SPECS)      # Fable 100 %, overall fine
        assert sw.exhausted_any(w) and not sw.exhausted_overall(w)
        payload = dict(CLAUDE_USAGE, limits=[])
        assert not sw.exhausted_any(claude_usage.from_usage(payload, SPECS))


class TestMergedReading:
    """A stream event carries the overall windows only; the per-model
    windows come from the newest poll until the next poll. Live-observed
    2026-09-11: two headless turns hid a Fable window at 100 % for ten
    minutes and the pool sent Fable work to that account."""

    def _poll(self, at, fable=100.0):
        return sw.to_row(_W(
            five=sw.Window(3.0, at + timedelta(hours=2)),
            seven=sw.Window(56.0, at + timedelta(days=5)),
            scoped=[sw.Scoped("fable", "Fable", fable, at + timedelta(days=5), True)],
            reached="scoped:fable" if fable >= 100 else "", observed_at=at, source="poll"))

    def _event(self, at):
        return sw.to_row(_W(
            five=sw.Window(2.0, at + timedelta(hours=2)),
            seven=sw.Window(55.0, at + timedelta(days=5)),
            observed_at=at, source="claude_event"))

    def test_event_keeps_the_polls_scoped_windows(self):
        poll = self._poll(NOW - timedelta(minutes=8))
        event = self._event(NOW - timedelta(minutes=1))
        r = sw.merged_reading(SPECS, event, poll, NOW)
        assert r.source == "claude_event" and r.windows["five_hour"].pct == 2.0
        assert r.scoped_for("fable").pct == 100.0 and r.reached == "scoped:fable"
        assert sw.exhausted(r, "fable")
        # No poll at all: the event stands alone, and a poll as the newest
        # sample is taken as it is.
        assert sw.merged_reading(SPECS, event, None, NOW).scoped == []
        assert sw.merged_reading(SPECS, poll, None, NOW).scoped_for("fable").pct == 100.0

    def test_polls_scoped_windows_settle_by_their_own_reset(self):
        stale = sw.to_row(_W(
            five=sw.Window(3.0, NOW + timedelta(hours=2)),
            seven=sw.Window(56.0, NOW + timedelta(days=5)),
            scoped=[sw.Scoped("fable", "Fable", 100.0, NOW - timedelta(minutes=5), True)],
            reached="scoped:fable", observed_at=NOW - timedelta(minutes=8), source="poll"))
        r = sw.merged_reading(SPECS, self._event(NOW), stale, NOW)
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
        # Rows, not ids: the row's layer names the windows the sample is read against.
        rows = [{"id": "sub-e", "layer": "claude-code-cli"}, {"id": "sub-p", "layer": "claude-code-cli"}]
        readings = sw.latest_readings(store, rows, NOW)
        assert readings["sub-e"].scoped_for("fable").pct == 100.0
        assert readings["sub-p"].scoped_for("fable").pct == 100.0
        assert readings["sub-e"].specs == sw.window_specs("claude-code-cli")
        assert calls == [(("sub-e", "sub-p"), None), (("sub-e",), "poll")]
