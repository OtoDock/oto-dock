"""Subscription pool caps (services.billing.pool_caps): the readings over a
pool of OAuth accounts, the cap row, the thresholds and the cache.

Run: cd proxy && python -m pytest tests/billing/test_pool_caps.py -v
"""

import sys
import threading
from datetime import datetime, timezone

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from services.billing import pool_caps
from services.engines import subscription_windows as sw
from storage import database as task_store
from storage.billing import subscription_store

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
RESET = "2026-09-16T06:00:00+00:00"


class _FixedNow(datetime):
    """``datetime`` whose ``now`` is NOW, for the paths that do not take a
    ``now`` argument (the cached evaluate)."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _fresh_setting(temp_db):
    sw.invalidate_setting_cache()
    yield
    sw.invalidate_setting_cache()


def _oauth(owner="user-admin", *, layer="claude-code-cli", personal=True, pooled=True):
    provider = "openai" if layer == "codex-cli" else "anthropic"
    return subscription_store.add_subscription(
        layer, provider, "oauth", owner_sub=owner, use_personal=personal,
        contribute_platform=pooled, oauth_email=f"{owner}-{layer}@x.test",
    )["id"]


def _sample(sub_id, at, seven, *, five=10.0):
    assert subscription_store.insert_window_sample(
        sub_id, observed_at=at, source="poll",
        five_hour_pct=five, five_hour_resets_at="2026-09-11T15:00:00+00:00",
        seven_day_pct=seven, seven_day_resets_at=RESET,
    )


def _spend(sub_id, cost, *, scope, user_sub=None, at=None):
    rid = task_store.insert_usage_record(
        user_sub, "agent-a", scope, "chat", "c", cost, source_key=sub_id, message_count=1,
    )
    if at:
        with task_store.get_conn() as conn:
            conn.execute("UPDATE usage_records SET created_at = %s WHERE id = %s", (at, rid))
            conn.commit()
    return rid


def _cap(scope="platform", target="", **fields):
    values = {f: None for f in pool_caps.CAP_FIELDS}
    values.update({k: v for k, v in fields.items() if k in pool_caps.CAP_FIELDS})
    return subscription_store.upsert_pool_cap(
        scope, target, on_reached=fields.get("on_reached", "stop"),
        updated_by="t", **values,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class TestStore:
    def test_cap_row_round_trip_and_clear(self, temp_db):
        row = _cap(week_pct=50, day_usd=5, on_reached="continue")
        assert row["week_pct"] == 50 and row["day_usd"] == 5
        assert row["day_pct"] is None and row["week_usd"] is None
        assert row["on_reached"] == "continue"
        assert subscription_store.get_pool_cap("platform", "")["updated_by"] == "t"
        # Writing every field empty removes the row.
        _cap()
        assert subscription_store.get_pool_cap("platform", "") is None
        assert not subscription_store.delete_pool_cap("platform", "")

    def test_user_rows_are_keyed_by_target(self, temp_db):
        _cap("user", "u1", week_pct=10)
        _cap("user", "u2", week_pct=20)
        assert subscription_store.get_pool_cap("user", "u1")["week_pct"] == 10
        assert subscription_store.get_pool_cap("user", "u2")["week_pct"] == 20
        assert subscription_store.delete_pool_cap("user", "u1")
        assert subscription_store.get_pool_cap("user", "u2")["week_pct"] == 20

    def test_pool_consumption_by_bucket(self, temp_db):
        a, b = _oauth("user-admin"), _oauth("user-manager", pooled=False)
        _spend(a, 1.0, scope="agent")
        _spend(a, 2.0, scope="user", user_sub="user-admin")
        _spend(b, 4.0, scope="user", user_sub="user-manager")
        _spend(a, 8.0, scope="agent", at="2026-01-01T00:00:00+00:00")
        since = "2026-09-01T00:00:00+00:00"
        assert subscription_store.get_pool_consumption([a, b], since, scope="agent") == 1.0
        assert subscription_store.get_pool_consumption(
            [a, b], since, scope="user", user_sub="user-admin") == 2.0
        assert subscription_store.get_pool_consumption(
            [a, b], since, scope="user", user_sub="user-manager") == 4.0
        assert subscription_store.get_pool_consumption([], since, scope="agent") == 0.0


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

class TestReadings:
    def test_week_pct_is_the_mean_over_accounts_with_a_sample(self, temp_db):
        a = _oauth("user-admin")
        b = subscription_store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub="user-admin",
            contribute_platform=True, oauth_email="second@x.test")["id"]
        _oauth("user-admin", layer="codex-cli")
        _sample(a, "2026-09-11T11:50:00+00:00", 60.0)
        _sample(b, "2026-09-11T11:55:00+00:00", 20.0)
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert s.accounts == 2
        assert s.readings["week_pct"] == 40.0
        # The other engine's pool is read on its own and has no sample.
        s2 = pool_caps.evaluate("platform", "", "codex-cli", now=NOW)
        assert s2.accounts == 1 and s2.readings["week_pct"] is None

    def test_account_without_a_sample_is_left_out_of_the_mean(self, temp_db):
        a = _oauth("user-admin")
        subscription_store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub="user-admin",
            contribute_platform=True, oauth_email="second@x.test")
        _sample(a, "2026-09-11T11:50:00+00:00", 60.0)
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert s.accounts == 2 and s.readings["week_pct"] == 60.0

    def test_day_pct_sums_positive_steps_across_a_reset(self, temp_db):
        a = _oauth("user-admin")
        # Baseline before the day, 40 → 55 (+15), the window resets (55 → 3,
        # ignored), then 3 → 9 (+6): 21 % of the week used in the last day.
        _sample(a, "2026-09-10T10:00:00+00:00", 40.0)
        _sample(a, "2026-09-10T20:00:00+00:00", 55.0)
        _sample(a, "2026-09-11T02:00:00+00:00", 3.0)
        _sample(a, "2026-09-11T11:00:00+00:00", 9.0)
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert s.readings["day_pct"] == pytest.approx(21.0)

    def test_day_pct_with_one_sample_is_zero(self, temp_db):
        a = _oauth("user-admin")
        _sample(a, "2026-09-11T11:00:00+00:00", 9.0)
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert s.readings["day_pct"] == 0.0

    def test_percent_readings_skipped_when_windows_are_off(self, temp_db):
        a = _oauth("user-admin")
        _sample(a, "2026-09-11T11:00:00+00:00", 90.0)
        task_store.set_platform_setting(sw.SETTING_KEY, "0")
        sw.invalidate_setting_cache()
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert s.readings["week_pct"] is None and s.readings["day_pct"] is None
        assert s.readings["week_usd"] == 0.0

    def test_dollars_follow_the_scope(self, temp_db):
        a = _oauth("user-admin")                      # personal AND pooled
        _spend(a, 3.0, scope="agent")
        _spend(a, 5.0, scope="user", user_sub="user-admin")
        _spend(a, 7.0, scope="user", user_sub="user-admin", at="2026-09-09T12:00:00+00:00")
        platform = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert platform.readings["week_usd"] == 3.0 and platform.readings["day_usd"] == 3.0
        user = pool_caps.evaluate("user", "user-admin", "claude-code-cli", now=NOW)
        assert user.readings["week_usd"] == 12.0 and user.readings["day_usd"] == 5.0

    def test_user_pool_is_the_owners_personal_accounts(self, temp_db):
        _oauth("user-admin")
        benched = _oauth("user-manager", personal=False, pooled=False)
        assert benched
        assert pool_caps.evaluate("user", "user-manager", "claude-code-cli", now=NOW).accounts == 0
        assert pool_caps.evaluate("user", "user-admin", "claude-code-cli", now=NOW).accounts == 1

    def test_not_applicable_paths(self, temp_db):
        # No OAuth account: nothing to cap even with a cap row.
        _cap(week_pct=1)
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert s.configured and s.accounts == 0 and s.allowed and not s.hits
        # An API key in the pool is not an account the cap reads.
        subscription_store.add_subscription(
            "claude-code-cli", "anthropic", "api_key", owner_sub="user-admin",
            contribute_platform=True, credential_data={"api_key": "k"})
        assert pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW).accounts == 0
        # An engine without OAuth accounts is never evaluated.
        s = pool_caps.evaluate("platform", "", "direct-llm", now=NOW)
        assert s.allowed and s.accounts == 0 and s.readings["week_usd"] is None
        # No cap row: readings only.
        _cap()
        _oauth("user-admin")
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert not s.configured and s.allowed and s.readings["week_usd"] == 0.0


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_first_hit_blocks_and_names_itself(self, temp_db):
        a = _oauth("user-admin")
        _sample(a, "2026-09-11T11:00:00+00:00", 52.0)
        _cap(week_pct=50, day_usd=5)
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert not s.allowed and s.hits == ["week_pct"] and s.warning
        assert s.hit_text() == "the week is at 52% of the 50% cap"
        assert s.blocked_message() == (
            "The agent pool's Claude subscription cap is reached: the week is at "
            "52% of the 50% cap. It clears as the accounts' windows reset; change "
            "the cap in Setup → Usage.")
        assert s.short_reason() == "Subscription pool cap reached (the week is at 52% of the 50% cap)"

    def test_warning_at_eighty_percent_of_a_cap(self, temp_db):
        a = _oauth("user-admin")
        _spend(a, 4.0, scope="user", user_sub="user-admin")
        _cap("user", "user-admin", week_usd=5)
        s = pool_caps.evaluate("user", "user-admin", "claude-code-cli", now=NOW)
        assert s.allowed and s.warning and not s.hits
        assert s.readings["week_usd"] == 4.0
        _spend(a, 1.0, scope="user", user_sub="user-admin")
        s = pool_caps.evaluate("user", "user-admin", "claude-code-cli", now=NOW)
        assert not s.allowed and s.hits == ["week_usd"]
        assert s.hit_text() == "the week is at $5.00 of the $5 cap"
        assert "Your ChatGPT" not in s.blocked_message()
        assert s.blocked_message(no_key=True).endswith(
            "There is no API key to continue on. Connect one in User Settings → "
            "AI Engines, or change the cap in User Settings → Usage.")

    def test_unknown_readings_never_hit(self, temp_db):
        _oauth("user-admin")
        _cap(week_pct=1, day_pct=1)
        s = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW)
        assert s.allowed and not s.warning and s.readings["week_pct"] is None

    def test_on_reached_and_public_shape(self, temp_db):
        a = _oauth("user-admin")
        _sample(a, "2026-09-11T11:00:00+00:00", 30.0)
        _cap(week_pct=25, on_reached="continue")
        p = pool_caps.evaluate("platform", "", "claude-code-cli", now=NOW).to_public()
        assert p["on_reached"] == "continue" and p["hits"] == ["week_pct"]
        assert p["caps"]["week_pct"] == 25 and p["readings"]["week_pct"] == 30.0
        assert p["layer"] == "claude-code-cli" and p["accounts"] == 1
        assert set(p) == {"scope", "layer", "configured", "caps", "readings",
                          "on_reached", "accounts", "hits", "warning", "allowed"}

    def test_engines_map_lists_pools_with_accounts(self, temp_db):
        _oauth("user-admin", layer="codex-cli")
        engines = pool_caps.evaluate_engines("platform", "")
        assert list(engines) == ["codex-cli"]
        assert pool_caps.evaluate_engines("user", "user-viewer") == {}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_cached_until_invalidated(self, temp_db, monkeypatch):
        # The cached path reads the wall clock; pin it to NOW so the sample's
        # fixed reset instant never falls into the past.
        monkeypatch.setattr(pool_caps, "datetime", _FixedNow)
        a = _oauth("user-admin")
        assert pool_caps.evaluate("platform", "", "claude-code-cli").allowed
        _sample(a, NOW.isoformat(), 99.0)
        _cap(week_pct=50)
        # Still the cached verdict.
        assert pool_caps.evaluate("platform", "", "claude-code-cli").allowed
        pool_caps.invalidate("user", "user-admin")
        assert pool_caps.evaluate("platform", "", "claude-code-cli").allowed
        pool_caps.invalidate("platform")
        assert not pool_caps.evaluate("platform", "", "claude-code-cli").allowed

    def test_user_pools_are_cached_apart(self, temp_db):
        a = _oauth("user-admin")
        _cap("user", "user-admin", week_usd=1)
        _spend(a, 2.0, scope="user", user_sub="user-admin")
        assert not pool_caps.evaluate("user", "user-admin", "claude-code-cli").allowed
        assert pool_caps.evaluate("user", "user-manager", "claude-code-cli").allowed
        pool_caps.clear_cache()
        assert not pool_caps.evaluate("user", "user-admin", "claude-code-cli").allowed

    def test_concurrent_readers_share_one_verdict(self, temp_db):
        _oauth("user-admin")
        results = []

        def _read():
            results.append(pool_caps.evaluate("platform", "", "claude-code-cli"))

        threads = [threading.Thread(target=_read) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 6 and all(r.allowed for r in results)
