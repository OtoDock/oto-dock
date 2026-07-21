"""Tests for usage tracking and limits system.

Covers: recording, aggregation, limit resolution, enforcement edge cases,
period calculations, and database CRUD.

Run: cd proxy && python -m pytest tests/billing/test_usage.py -v
"""

from datetime import datetime, timezone, timedelta
from calendar import monthrange

from storage import database as task_store
from services.billing import usage_service


# ---------------------------------------------------------------------------
# Helper: record user-scoped usage PAID BY A BORROWED PLATFORM CREDENTIAL.
# User/role limits gate only platform-paid usage (the platform-auth budget), so
# enforcement tests must feed borrowed usage — usage on a user's OWN sub or with
# no subscription is "unattributed" and never counts. See storage/database.py
# `basis=` classification + tests/billing/test_usage_payment_split.py.
# ---------------------------------------------------------------------------

_BORROWED_SUB_ID = "borrowed-test-sub"


def _ensure_borrowed_sub():
    """Idempotently create one admin-owned (borrowed) api_key subscription."""
    now = datetime.now(timezone.utc).isoformat()
    with task_store.get_conn() as conn:
        conn.execute(
            "INSERT INTO execution_layer_subscriptions"
            " (id, layer, provider, auth_type, owner_sub, use_personal,"
            "  contribute_platform, label, is_primary, credential_data_enc,"
            "  oauth_email, active_sessions, status, created_at, updated_at)"
            " VALUES (%s,'codex-cli','openai','api_key','admin',FALSE,TRUE,'',"
            "  FALSE,'','',0,'active',%s,%s) ON CONFLICT (id) DO NOTHING",
            (_BORROWED_SUB_ID, now, now),
        )
        conn.commit()
    return _BORROWED_SUB_ID


def _rec_platform(user_sub, cost, agent="a", source_id="c",
                  provider="anthropic", model="", message_count=1):
    """Record user-scoped usage on a borrowed platform credential (gated)."""
    return task_store.insert_usage_record(
        user_sub, agent, "user", "chat", source_id, cost,
        provider=provider, model=model, source_key=_ensure_borrowed_sub(),
        message_count=message_count,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Period calculations
# ═══════════════════════════════════════════════════════════════════════════


class TestPeriodCalculations:
    def test_monthly_range_returns_valid_iso(self):
        start, end = usage_service._monthly_range()
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        assert s.day == 1
        assert s.hour == 0 and s.minute == 0 and s.second == 0
        assert e.day == 1  # 1st of next month
        assert e > s

    def test_monthly_range_covers_full_month(self):
        start, end = usage_service._monthly_range()
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        _, days = monthrange(s.year, s.month)
        assert (e - s).days == days

    def test_weekly_range_starts_on_monday(self):
        start, end = usage_service._weekly_range()
        s = datetime.fromisoformat(start)
        assert s.weekday() == 0  # Monday

    def test_weekly_range_is_7_days(self):
        start, end = usage_service._weekly_range()
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        assert (e - s).days == 7


# ═══════════════════════════════════════════════════════════════════════════
# Recording
# ═══════════════════════════════════════════════════════════════════════════


class TestRecording:
    def test_record_usage_inserts_row(self, temp_db):
        row_id = usage_service.record_usage(
            user_sub="user-viewer", agent="test-agent", scope="user",
            source_type="chat", source_id="chat-1", cost_usd=0.05,
        )
        assert row_id > 0

        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg["total_cost"] == 0.05
        assert agg["total_messages"] == 1
        assert agg["record_count"] == 1

    def test_record_usage_zero_cost_with_message_persists(self, temp_db):
        """Cache-only turns ($0 cost, 1 message) MUST persist so token stats
        and message_count survive for analytics. The filter only drops rows
        where BOTH cost and message_count are zero."""
        row_id = usage_service.record_usage(
            user_sub="user-viewer", agent="test-agent", scope="user",
            source_type="chat", source_id="chat-1", cost_usd=0.0,
            message_count=1,
        )
        assert row_id > 0
        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg["record_count"] == 1
        assert agg["total_messages"] == 1

    def test_record_usage_zero_cost_zero_messages_dropped(self, temp_db):
        """No-op rows (zero cost AND zero messages) are filtered out — these
        are typically MCP rows where the rule's amount rounded to 0."""
        row_id = usage_service.record_usage(
            user_sub="user-viewer", agent="test-agent", scope="user",
            source_type="chat", source_id="chat-1", cost_usd=0.0,
            message_count=0,
        )
        assert row_id == 0
        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg["record_count"] == 0

    def test_record_multiple_accumulates(self, temp_db):
        for i in range(5):
            usage_service.record_usage(
                user_sub="user-viewer", agent="test-agent", scope="user",
                source_type="chat", source_id=f"chat-{i}", cost_usd=0.10,
            )
        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert abs(agg["total_cost"] - 0.50) < 0.001
        assert agg["total_messages"] == 5

    def test_record_with_all_fields(self, temp_db):
        usage_service.record_usage(
            user_sub="user-viewer", agent="personal-assistant", scope="user",
            source_type="task", source_id="run-abc",
            cost_usd=1.23, input_tokens=5000, output_tokens=2000,
            cache_read=1000, cache_write=500, message_count=3,
            provider="openai", model="gpt-4o",
        )
        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg["total_cost"] == 1.23
        assert agg["total_messages"] == 3

    def test_agent_scoped_recording(self, temp_db):
        """Agent-scoped records have user_sub=None."""
        usage_service.record_usage(
            user_sub=None, agent="social-media", scope="agent",
            source_type="scheduled", source_id="run-1", cost_usd=0.50,
        )
        # Should NOT appear in user aggregation
        agg_user = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg_user["record_count"] == 0

        # Should appear in agent aggregation
        agg_agent = task_store.get_usage_aggregated(agent="social-media", scope="agent")
        assert agg_agent["total_cost"] == 0.50


# ═══════════════════════════════════════════════════════════════════════════
# Aggregation queries
# ═══════════════════════════════════════════════════════════════════════════


class TestAggregation:
    def _seed_usage(self):
        """Seed a variety of usage records."""
        for i in range(10):
            usage_service.record_usage(
                user_sub="user-viewer", agent="agent-a", scope="user",
                source_type="chat", source_id=f"c-{i}", cost_usd=1.0,
            )
        for i in range(5):
            usage_service.record_usage(
                user_sub="user-viewer", agent="agent-b", scope="user",
                source_type="chat", source_id=f"c-b-{i}", cost_usd=2.0,
            )
        for i in range(3):
            usage_service.record_usage(
                user_sub="user-manager", agent="agent-a", scope="user",
                source_type="task", source_id=f"t-{i}", cost_usd=5.0,
            )

    def test_aggregated_with_user_filter(self, temp_db):
        self._seed_usage()
        agg = task_store.get_usage_aggregated(user_sub="user-viewer", scope="user")
        assert abs(agg["total_cost"] - 20.0) < 0.01  # 10*1 + 5*2
        assert agg["total_messages"] == 15

    def test_aggregated_with_agent_filter(self, temp_db):
        self._seed_usage()
        agg = task_store.get_usage_aggregated(
            user_sub="user-viewer", agent="agent-a", scope="user"
        )
        assert abs(agg["total_cost"] - 10.0) < 0.01

    def test_aggregated_with_date_range(self, temp_db):
        self._seed_usage()
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).isoformat()
        yesterday = (now - timedelta(days=1)).isoformat()

        agg = task_store.get_usage_aggregated(
            user_sub="user-viewer", scope="user",
            start=yesterday, end=tomorrow,
        )
        assert agg["total_cost"] == 20.0

        # Future range should be empty
        future = (now + timedelta(days=10)).isoformat()
        agg2 = task_store.get_usage_aggregated(
            user_sub="user-viewer", scope="user",
            start=tomorrow, end=future,
        )
        assert agg2["total_cost"] == 0

    def test_usage_by_agent(self, temp_db):
        self._seed_usage()
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1)).isoformat()

        breakdown = task_store.get_usage_by_agent("user-viewer", start, end)
        assert len(breakdown) == 2
        # Both agents have $10 total (10*$1 and 5*$2) — order is non-deterministic on ties
        agents = {b["agent"] for b in breakdown}
        assert agents == {"agent-a", "agent-b"}
        costs = {b["agent"]: b["cost"] for b in breakdown}
        assert abs(costs["agent-a"] - 10.0) < 0.01
        assert abs(costs["agent-b"] - 10.0) < 0.01

    def test_daily_usage(self, temp_db):
        self._seed_usage()
        daily = task_store.get_usage_daily(user_sub="user-viewer", scope="user", days=7)
        assert len(daily) >= 1
        today = daily[-1]  # Most recent day
        assert today["cost"] > 0

    def test_all_users_usage(self, temp_db):
        self._seed_usage()
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1)).isoformat()

        users = task_store.get_all_users_usage(start, end)
        # Should include all seeded users (even those with 0 usage)
        assert len(users) >= 3
        viewer = next(u for u in users if u["sub"] == "user-viewer")
        assert abs(viewer["total_cost"] - 20.0) < 0.01
        manager = next(u for u in users if u["sub"] == "user-manager")
        assert abs(manager["total_cost"] - 15.0) < 0.01

    def test_all_agents_usage(self, temp_db):
        usage_service.record_usage(
            user_sub=None, agent="bot-agent", scope="agent",
            source_type="scheduled", source_id="r-1", cost_usd=3.0,
        )
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1)).isoformat()

        agents = task_store.get_all_agents_usage(start, end)
        assert len(agents) == 1
        assert agents[0]["agent"] == "bot-agent"
        assert agents[0]["total_cost"] == 3.0


# ═══════════════════════════════════════════════════════════════════════════
# Limits CRUD
# ═══════════════════════════════════════════════════════════════════════════


class TestLimitsCRUD:
    def test_upsert_and_get(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        row = task_store.get_usage_limit("role_default", "member", "monthly")
        assert row is not None
        assert row["cost_limit_usd"] == 50.0

    def test_upsert_replaces_existing(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        task_store.upsert_usage_limit("role_default", "member", "monthly", 100.0, "admin")
        row = task_store.get_usage_limit("role_default", "member", "monthly")
        assert row["cost_limit_usd"] == 100.0

    def test_upsert_null_means_no_limit(self, temp_db):
        task_store.upsert_usage_limit("role_default", "admin", "monthly", None, "admin")
        row = task_store.get_usage_limit("role_default", "admin", "monthly")
        assert row is not None
        assert row["cost_limit_usd"] is None

    def test_delete(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        deleted = task_store.delete_usage_limit("role_default", "member", "monthly")
        assert deleted is True
        row = task_store.get_usage_limit("role_default", "member", "monthly")
        assert row is None

    def test_delete_nonexistent(self, temp_db):
        deleted = task_store.delete_usage_limit("role_default", "nobody", "monthly")
        assert deleted is False

    def test_get_all_limits(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 25.0, "admin")
        task_store.upsert_usage_limit("role_default", "member", "weekly", 8.0, "admin")
        task_store.upsert_usage_limit("role_default", "creator", "monthly", 100.0, "admin")
        task_store.upsert_usage_limit("user_override", "user-viewer", "monthly", 50.0, "admin")

        all_limits = task_store.get_usage_limits_all()
        assert len(all_limits) == 4

    def test_get_limits_for_target(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 25.0, "admin")
        task_store.upsert_usage_limit("role_default", "member", "weekly", 8.0, "admin")
        task_store.upsert_usage_limit("role_default", "creator", "monthly", 100.0, "admin")

        viewer_limits = task_store.get_usage_limits_for_target("role_default", "member")
        assert len(viewer_limits) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Limit resolution (the critical logic)
# ═══════════════════════════════════════════════════════════════════════════


class TestLimitResolution:
    """Tests for _resolve_limit and the precedence chain."""

    def test_no_limits_returns_none(self, temp_db):
        limit = usage_service._resolve_limit(
            "user_override", "user-viewer", "role_default", "member", "monthly"
        )
        assert limit is None

    def test_role_default_only(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        limit = usage_service._resolve_limit(
            "user_override", "user-viewer", "role_default", "member", "monthly"
        )
        assert limit == 50.0

    def test_user_override_takes_precedence(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        task_store.upsert_usage_limit("user_override", "user-viewer", "monthly", 200.0, "admin")
        limit = usage_service._resolve_limit(
            "user_override", "user-viewer", "role_default", "member", "monthly"
        )
        assert limit == 200.0

    def test_user_override_null_means_unlimited(self, temp_db):
        """User with explicit 'no limit' override should NOT fall through to role default."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        task_store.upsert_usage_limit("user_override", "user-viewer", "monthly", None, "admin")
        limit = usage_service._resolve_limit(
            "user_override", "user-viewer", "role_default", "member", "monthly"
        )
        # Should be None (explicit unlimited), NOT 50.0
        assert limit is None

    def test_role_default_null_means_unlimited(self, temp_db):
        task_store.upsert_usage_limit("role_default", "admin", "monthly", None, "admin")
        limit = usage_service._resolve_limit(
            "user_override", "user-admin", "role_default", "admin", "monthly"
        )
        assert limit is None

    def test_agent_limit_no_fallback(self, temp_db):
        task_store.upsert_usage_limit("agent", "social-media", "monthly", 100.0, "admin")
        limit = usage_service._resolve_limit(
            "agent", "social-media", None, None, "monthly"
        )
        assert limit == 100.0


# ═══════════════════════════════════════════════════════════════════════════
# check_user_limit (end-to-end enforcement logic)
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckUserLimit:
    def test_no_limits_always_allowed(self, temp_db):
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True
        assert result["warning"] is False
        assert result["periods"]["monthly"] is None
        assert result["periods"]["weekly"] is None

    def test_under_limit_allowed(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 100.0, "admin")
        # Use $30 (platform-paid → counts toward the budget)
        for _ in range(3):
            _rec_platform("user-viewer", 10.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True
        assert result["warning"] is False
        m = result["periods"]["monthly"]
        assert m is not None
        assert abs(m["used"] - 30.0) < 0.01
        assert m["limit"] == 100.0
        assert abs(m["percent"] - 30.0) < 0.1

    def test_at_80_percent_warning(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 100.0, "admin")
        # Use $85
        _rec_platform("user-viewer", 85.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True
        assert result["warning"] is True
        assert result["periods"]["monthly"]["percent"] == 85.0

    def test_at_100_percent_blocked(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 100.0, "admin")
        _rec_platform("user-viewer", 100.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is False

    def test_over_100_percent_blocked(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        _rec_platform("user-viewer", 75.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is False
        assert result["periods"]["monthly"]["percent"] == 150.0

    def test_zero_limit_always_blocked(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 0.0, "admin")
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is False

    def test_weekly_limit_independent(self, temp_db):
        """Weekly limit triggers independently of monthly."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 1000.0, "admin")
        task_store.upsert_usage_limit("role_default", "member", "weekly", 10.0, "admin")
        _rec_platform("user-viewer", 12.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        # Monthly is fine (12/1000 = 1.2%), but weekly is over (12/10 = 120%)
        assert result["allowed"] is False
        assert result["periods"]["monthly"]["percent"] < 2
        assert result["periods"]["weekly"]["percent"] > 100

    def test_either_period_blocks(self, temp_db):
        """If EITHER period hits 100%, the user is blocked."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 5.0, "admin")
        task_store.upsert_usage_limit("role_default", "member", "weekly", 100.0, "admin")
        _rec_platform("user-viewer", 6.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is False  # Monthly exceeded

    def test_user_override_increases_limit(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 10.0, "admin")
        task_store.upsert_usage_limit("user_override", "user-viewer", "monthly", 500.0, "admin")
        _rec_platform("user-viewer", 50.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True  # 50/500 = 10%
        assert result["periods"]["monthly"]["limit"] == 500.0

    def test_user_override_null_bypasses_role_limit(self, temp_db):
        """User with explicit 'no limit' isn't blocked even if role has a limit."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 1.0, "admin")
        task_store.upsert_usage_limit("user_override", "user-viewer", "monthly", None, "admin")
        _rec_platform("user-viewer", 9999.0)
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True
        assert result["periods"]["monthly"] is None

    def test_different_users_isolated(self, temp_db):
        """User A's usage doesn't affect User B's limit check."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        # User A spends a lot (platform-paid)
        _rec_platform("user-viewer", 100.0, source_id="c1")
        # User B hasn't spent anything
        result_b = usage_service.check_user_limit("user-viewer2", "member")
        assert result_b["allowed"] is True
        assert result_b["periods"]["monthly"]["used"] == 0

    def test_agent_scoped_not_counted_for_user(self, temp_db):
        """Agent-scoped usage doesn't count against user limits."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 10.0, "admin")
        # Record agent-scoped (no user_sub)
        usage_service.record_usage(
            user_sub=None, agent="social-media", scope="agent",
            source_type="scheduled", source_id="r1", cost_usd=100.0,
        )
        # Also record user-scoped, platform-paid
        _rec_platform("user-viewer", 5.0, source_id="c1")
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True  # Only $5 user-scoped, limit is $10
        assert result["periods"]["monthly"]["used"] == 5.0


# ═══════════════════════════════════════════════════════════════════════════
# check_agent_limit
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckAgentLimit:
    def test_no_limit_always_allowed(self, temp_db):
        result = usage_service.check_agent_limit("social-media")
        assert result["allowed"] is True

    def test_agent_limit_enforced(self, temp_db):
        task_store.upsert_usage_limit("agent", "social-media", "monthly", 50.0, "admin")
        usage_service.record_usage(
            user_sub=None, agent="social-media", scope="agent",
            source_type="scheduled", source_id="r1", cost_usd=60.0,
        )
        result = usage_service.check_agent_limit("social-media")
        assert result["allowed"] is False

    def test_user_scoped_not_counted_for_agent(self, temp_db):
        """User-scoped usage on the same agent doesn't count against agent limits."""
        task_store.upsert_usage_limit("agent", "social-media", "monthly", 10.0, "admin")
        # User-scoped (different scope)
        usage_service.record_usage(
            user_sub="user-viewer", agent="social-media", scope="user",
            source_type="chat", source_id="c1", cost_usd=100.0,
        )
        result = usage_service.check_agent_limit("social-media")
        assert result["allowed"] is True  # Only agent-scoped counts


# ═══════════════════════════════════════════════════════════════════════════
# User summary (for settings page)
# ═══════════════════════════════════════════════════════════════════════════


class TestUserSummary:
    def test_returns_all_sections(self, temp_db):
        usage_service.record_usage(
            user_sub="user-viewer", agent="agent-a", scope="user",
            source_type="chat", source_id="c1", cost_usd=5.0,
        )
        summary = usage_service.get_user_summary("user-viewer", "member")
        assert "monthly" in summary
        assert "weekly" in summary
        assert "daily_chart" in summary
        assert "agent_breakdown" in summary

    def test_monthly_shows_usage_without_limit(self, temp_db):
        # `used` reflects the platform-paid portion (what the budget gates).
        _rec_platform("user-viewer", 5.0, source_id="c1")
        summary = usage_service.get_user_summary("user-viewer", "member")
        # No limit set → limit is None, usage still shown
        assert summary["monthly"]["limit"] is None
        assert summary["monthly"]["used"] == 5.0

    def test_monthly_shows_usage_with_limit(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 100.0, "admin")
        _rec_platform("user-viewer", 30.0, source_id="c1")
        summary = usage_service.get_user_summary("user-viewer", "member")
        assert summary["monthly"]["limit"] == 100.0
        assert summary["monthly"]["used"] == 30.0
        assert summary["monthly"]["percent"] == 30.0

    def test_agent_breakdown_correct(self, temp_db):
        usage_service.record_usage(
            user_sub="user-viewer", agent="agent-a", scope="user",
            source_type="chat", source_id="c1", cost_usd=10.0,
        )
        usage_service.record_usage(
            user_sub="user-viewer", agent="agent-b", scope="user",
            source_type="chat", source_id="c2", cost_usd=20.0,
        )
        summary = usage_service.get_user_summary("user-viewer", "member")
        agents = {a["agent"]: a["cost"] for a in summary["agent_breakdown"]}
        assert agents["agent-a"] == 10.0
        assert agents["agent-b"] == 20.0


# ═══════════════════════════════════════════════════════════════════════════
# Admin overview
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminOverview:
    def test_returns_all_sections(self, temp_db):
        overview = usage_service.get_admin_overview()
        assert "totals" in overview
        assert "daily_chart" in overview
        assert "users" in overview
        assert "agents" in overview

    def test_totals_reflect_all_usage(self, temp_db):
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c1", cost_usd=10.0,
        )
        usage_service.record_usage(
            user_sub="user-manager", agent="a", scope="user",
            source_type="chat", source_id="c2", cost_usd=20.0,
        )
        overview = usage_service.get_admin_overview()
        assert overview["totals"]["cost"] == 30.0
        assert overview["totals"]["active_users"] == 2

    def test_per_user_with_limits(self, temp_db):
        # monthly_percent is driven by the platform-paid portion.
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        _rec_platform("user-viewer", 40.0, source_id="c1")
        overview = usage_service.get_admin_overview()
        viewer = next(u for u in overview["users"] if u["sub"] == "user-viewer")
        assert viewer["monthly_limit"] == 50.0
        assert viewer["monthly_percent"] == 80.0
        assert viewer["platform_cost"] == 40.0


# ═══════════════════════════════════════════════════════════════════════════
# Database: task_runs cost_usd
# ═══════════════════════════════════════════════════════════════════════════


class TestTaskRunsCost:
    def test_update_run_with_cost(self, temp_db):
        task_store.create_run("run-1", "task-1", "agent-a", "manual", None, "test prompt", "one-time")
        task_store.update_run("run-1", status="completed", cost_usd=1.234)
        run = task_store.get_run("run-1")
        assert run is not None
        assert abs(run["cost_usd"] - 1.234) < 0.001

    def test_run_cost_defaults_to_zero(self, temp_db):
        task_store.create_run("run-2", "task-1", "agent-a", "manual", None, "test", "one-time")
        run = task_store.get_run("run-2")
        assert run["cost_usd"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_very_small_limit(self, temp_db):
        task_store.upsert_usage_limit("role_default", "member", "monthly", 0.001, "admin")
        _rec_platform("user-viewer", 0.002, source_id="c1")
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is False

    def test_many_small_records(self, temp_db):
        """Lots of tiny records should sum correctly."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 1.0, "admin")
        for i in range(100):
            _rec_platform("user-viewer", 0.005, source_id=f"c-{i}")
        # 100 * 0.005 = 0.50
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True
        assert abs(result["periods"]["monthly"]["used"] - 0.50) < 0.01

    def test_mixed_providers(self, temp_db):
        """Different providers still sum for the same user."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 10.0, "admin")
        _rec_platform("user-viewer", 4.0, source_id="c1", provider="anthropic")
        _rec_platform("user-viewer", 4.0, source_id="c2", provider="openai")
        _rec_platform("user-viewer", 4.0, source_id="c3", provider="image-gen")
        # Total: $12 > $10 limit
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is False
        assert result["periods"]["monthly"]["used"] == 12.0

    def test_limit_check_with_no_usage(self, temp_db):
        """User has limit but zero usage — should be allowed."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 50.0, "admin")
        result = usage_service.check_user_limit("user-viewer", "member")
        assert result["allowed"] is True
        assert result["periods"]["monthly"]["used"] == 0
        assert result["periods"]["monthly"]["percent"] == 0

    def test_admin_no_limit_by_default(self, temp_db):
        """Admin with no configured limits should never be blocked."""
        usage_service.record_usage(
            user_sub="user-admin", agent="a", scope="user",
            source_type="chat", source_id="c1", cost_usd=99999.0,
        )
        result = usage_service.check_user_limit("user-admin", "admin")
        assert result["allowed"] is True

    def test_role_change_effect(self, temp_db):
        """If a user's role changes, the new role's limits apply."""
        task_store.upsert_usage_limit("role_default", "member", "monthly", 10.0, "admin")
        task_store.upsert_usage_limit("role_default", "creator", "monthly", 100.0, "admin")
        _rec_platform("user-viewer", 50.0, source_id="c1")
        # As viewer: blocked (50/10 = 500%)
        result_viewer = usage_service.check_user_limit("user-viewer", "member")
        assert result_viewer["allowed"] is False

        # If role changes to manager: allowed (50/100 = 50%)
        result_manager = usage_service.check_user_limit("user-viewer", "creator")
        assert result_manager["allowed"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Multi-row turn recording (manifest-driven MCP costs)
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordTurnUsage:
    def test_writes_all_rows(self, temp_db):
        rows = [
            {"user_sub": "user-viewer", "agent": "a", "scope": "user",
             "source_type": "chat", "source_id": "c1", "cost_usd": 0.05,
             "input_tokens": 1000, "output_tokens": 500, "message_count": 1,
             "provider": "anthropic", "model": "claude-opus-4-1"},
            {"user_sub": "user-viewer", "agent": "a", "scope": "user",
             "source_type": "chat", "source_id": "c1", "cost_usd": 0.134,
             "message_count": 0, "provider": "image-gen", "model": "nano-banana"},
        ]
        ids = usage_service.record_turn_usage(rows)
        assert len(ids) == 2
        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg["record_count"] == 2
        assert abs(agg["total_cost"] - (0.05 + 0.134)) < 0.0001

    def test_llm_row_persisted_at_zero_cost(self, temp_db):
        """Regression for the LLM-row-drop trap: cache-only turn ($0 LLM cost)
        must still persist token counts and message_count."""
        rows = [
            {"user_sub": "user-viewer", "agent": "a", "scope": "user",
             "source_type": "chat", "source_id": "c1", "cost_usd": 0.0,
             "input_tokens": 0, "output_tokens": 100,
             "cache_read": 5000, "cache_write": 0,
             "message_count": 1, "provider": "anthropic", "model": "claude-opus-4-1"},
        ]
        ids = usage_service.record_turn_usage(rows)
        assert len(ids) == 1
        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg["record_count"] == 1
        assert agg["total_messages"] == 1

    def test_zero_cost_mcp_row_dropped(self, temp_db):
        """MCP rows have message_count=0; if cost rounds to 0 they shouldn't pollute the table."""
        rows = [
            {"user_sub": "user-viewer", "agent": "a", "scope": "user",
             "source_type": "chat", "source_id": "c1", "cost_usd": 0.10,
             "message_count": 1, "provider": "anthropic", "model": "claude"},
            {"user_sub": "user-viewer", "agent": "a", "scope": "user",
             "source_type": "chat", "source_id": "c1", "cost_usd": 0.0,
             "message_count": 0, "provider": "image-gen", "model": "x"},
        ]
        usage_service.record_turn_usage(rows)
        agg = task_store.get_usage_aggregated(user_sub="user-viewer")
        assert agg["record_count"] == 1  # only the LLM row

    def test_empty_batch_is_noop(self, temp_db):
        assert usage_service.record_turn_usage([]) == []


# ═══════════════════════════════════════════════════════════════════════════
# Provider breakdown query + admin overview integration
# ═══════════════════════════════════════════════════════════════════════════


class TestProviderBreakdown:
    def test_user_view_sums_across_agents(self, temp_db):
        """Regression: a user's (provider, model) cost across multiple agents
        must collapse to a single breakdown row in the user view, not split
        per-agent. The agent split belongs only in the agent-scope view."""
        usage_service.record_usage(
            user_sub="user-viewer", agent="personal-assistant", scope="user",
            source_type="chat", source_id="c1", cost_usd=10.0,
            provider="anthropic", model="claude-opus-4-1",
        )
        usage_service.record_usage(
            user_sub="user-viewer", agent="social-media", scope="user",
            source_type="chat", source_id="c2", cost_usd=5.0,
            provider="anthropic", model="claude-opus-4-1",
        )
        m_start = (datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)).isoformat()
        m_end = (datetime.now(timezone.utc).replace(day=28, hour=23)).isoformat()
        rows = [r for r in task_store.get_usage_provider_breakdown("user", m_start, m_end)
                if r["user_sub"] == "user-viewer"]
        # ONE row, summed across both agents
        assert len(rows) == 1
        assert rows[0]["provider"] == "anthropic"
        assert rows[0]["model"] == "claude-opus-4-1"
        assert rows[0]["cost"] == 15.0
        assert rows[0]["agent"] is None  # agent is dropped in user view

    def test_user_breakdown_groups_by_provider_and_model(self, temp_db):
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c1", cost_usd=10.0,
            provider="anthropic", model="claude-opus-4-1",
        )
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c2", cost_usd=2.0,
            provider="anthropic", model="claude-opus-4-1",
        )
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c3", cost_usd=0.5,
            provider="image-gen", model="nano-banana",
        )
        m_start = (datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)).isoformat()
        m_end = (datetime.now(timezone.utc).replace(day=28, hour=23)).isoformat()
        rows = task_store.get_usage_provider_breakdown("user", m_start, m_end)
        by_key = {(r["provider"], r["model"]): r["cost"] for r in rows if r["user_sub"] == "user-viewer"}
        assert by_key[("anthropic", "claude-opus-4-1")] == 12.0
        assert by_key[("image-gen", "nano-banana")] == 0.5

    def test_agent_scope_isolated(self, temp_db):
        usage_service.record_usage(
            user_sub=None, agent="social-bot", scope="agent",
            source_type="scheduled", source_id="r1", cost_usd=3.0,
            provider="image-gen", model="gpt-image",
        )
        m_start = (datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)).isoformat()
        m_end = (datetime.now(timezone.utc).replace(day=28, hour=23)).isoformat()
        # Agent scope query returns it
        agent_rows = task_store.get_usage_provider_breakdown("agent", m_start, m_end)
        assert any(r["agent"] == "social-bot" and r["provider"] == "image-gen" for r in agent_rows)
        # User scope query does NOT
        user_rows = task_store.get_usage_provider_breakdown("user", m_start, m_end)
        assert not any(r.get("agent") == "social-bot" for r in user_rows)

    def test_admin_overview_includes_breakdown(self, temp_db):
        """The overview response surfaces a per-user `breakdown` list."""
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c1", cost_usd=5.0,
            provider="anthropic", model="claude-opus-4-1",
        )
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c2", cost_usd=1.5,
            provider="image-gen", model="nano-banana",
        )
        overview = usage_service.get_admin_overview()
        viewer = next(u for u in overview["users"] if u["sub"] == "user-viewer")
        assert "breakdown" in viewer
        by_key = {(b["provider"], b["model"]): b["cost"] for b in viewer["breakdown"]}
        assert by_key[("anthropic", "claude-opus-4-1")] == 5.0
        assert by_key[("image-gen", "nano-banana")] == 1.5

    def test_admin_overview_provider_and_model_totals(self, temp_db):
        """Top-level platform-wide rollups by provider and by (provider, model)
        — combines user-scope and agent-scope rows."""
        # User-scope LLM
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c1", cost_usd=10.0,
            provider="anthropic", model="claude-opus-4-1",
        )
        # User-scope MCP
        usage_service.record_usage(
            user_sub="user-viewer", agent="a", scope="user",
            source_type="chat", source_id="c2", cost_usd=0.50,
            provider="image-gen", model="nano-banana",
            message_count=0,
        )
        # Agent-scope LLM (same provider, different model)
        usage_service.record_usage(
            user_sub=None, agent="bot", scope="agent",
            source_type="scheduled", source_id="r1", cost_usd=4.0,
            provider="anthropic", model="claude-haiku-4-5",
        )
        # Agent-scope MCP (same provider, different model)
        usage_service.record_usage(
            user_sub=None, agent="bot", scope="agent",
            source_type="scheduled", source_id="r2", cost_usd=0.30,
            provider="image-gen", model="gpt-image",
            message_count=0,
        )
        overview = usage_service.get_admin_overview()
        # provider_totals collapses across model + scope
        by_provider = {p["provider"]: p["cost"] for p in overview["provider_totals"]}
        assert by_provider["anthropic"] == 14.0
        assert by_provider["image-gen"] == 0.80
        # model_totals splits by model but combines across scope
        by_model = {(m["provider"], m["model"]): m["cost"] for m in overview["model_totals"]}
        assert by_model[("anthropic", "claude-opus-4-1")] == 10.0
        assert by_model[("anthropic", "claude-haiku-4-5")] == 4.0
        assert by_model[("image-gen", "nano-banana")] == 0.50
        assert by_model[("image-gen", "gpt-image")] == 0.30
        # Sorted by cost desc
        assert overview["provider_totals"][0]["provider"] == "anthropic"
        assert overview["model_totals"][0]["model"] == "claude-opus-4-1"

    def test_admin_overview_agent_breakdown(self, temp_db):
        usage_service.record_usage(
            user_sub=None, agent="social-bot", scope="agent",
            source_type="scheduled", source_id="r1", cost_usd=3.0,
            provider="anthropic", model="claude-opus-4-1",
        )
        usage_service.record_usage(
            user_sub=None, agent="social-bot", scope="agent",
            source_type="scheduled", source_id="r2", cost_usd=0.5,
            provider="image-gen", model="gpt-image",
        )
        overview = usage_service.get_admin_overview()
        ag = next(a for a in overview["agents"] if a["agent"] == "social-bot")
        assert "breakdown" in ag
        by_provider = {(b["provider"], b["model"]): b["cost"] for b in ag["breakdown"]}
        assert by_provider[("anthropic", "claude-opus-4-1")] == 3.0
        assert by_provider[("image-gen", "gpt-image")] == 0.5
