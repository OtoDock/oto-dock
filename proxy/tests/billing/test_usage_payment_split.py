"""Tests for the platform-paid vs self-paid usage split.

User/role spending limits are a PLATFORM-AUTH budget: they gate only usage paid
by borrowed platform credentials (api_key / relay / local_endpoint owned by
someone other than the user), never a user's OWN subscription. Classification is
done at query time by joining usage_records.source_key -> the serving
subscription's id (no migration). See storage/database.py `basis=` +
`_PLATFORM_PAID_PRED` / `_SELF_PAID_PRED`.

Run: cd proxy && python -m pytest tests/billing/test_usage_payment_split.py -v
"""

import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

_WIDE_START = "2000-01-01T00:00:00+00:00"
_WIDE_END = "2999-01-01T00:00:00+00:00"


def _usage(db, user_sub, source_key, cost, scope="user", agent="a1"):
    db.insert_usage_record(
        user_sub, agent, scope, "chat", "c", cost,
        provider="p", source_key=source_key, model="m",
    )


class TestPaymentClassification:
    def test_own_vs_borrowed_split(self, temp_db):
        """A user mixing their OWN claude sub with the admin's borrowed codex API
        + hosted relay: self=own only, platform=borrowed only, 'default'=neither."""
        from storage import database as db, subscription_store

        own = subscription_store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub="user-1")
        admin_api = subscription_store.add_subscription(
            "codex-cli", "openai", "api_key", owner_sub="admin", contribute_platform=True)
        relay = subscription_store.add_subscription(
            "direct-llm", "openai", "relay", owner_sub="")  # owner-less platform infra

        _usage(db, "user-1", own["id"], 1.00)        # self
        _usage(db, "user-1", admin_api["id"], 2.00)  # platform (borrowed api_key)
        _usage(db, "user-1", relay["id"], 0.50)      # platform (hosted relay)
        _usage(db, "user-1", "default", 0.10)        # unattributed

        total = db.get_usage_aggregated(user_sub="user-1", scope="user")["total_cost"]
        platform = db.get_usage_aggregated(user_sub="user-1", scope="user", basis="platform")["total_cost"]
        own_paid = db.get_usage_aggregated(user_sub="user-1", scope="user", basis="self")["total_cost"]

        assert round(total, 2) == 3.60
        assert round(platform, 2) == 2.50   # admin api + relay
        assert round(own_paid, 2) == 1.00   # own oauth
        assert round(total - platform - own_paid, 2) == 0.10  # 'default' = unattributed

    def test_own_api_key_is_self_paid(self, temp_db):
        """A user's OWN api_key (owner_sub == user) is self-paid, not platform."""
        from storage import database as db, subscription_store

        own_key = subscription_store.add_subscription(
            "codex-cli", "openai", "api_key", owner_sub="user-1")
        _usage(db, "user-1", own_key["id"], 4.00)

        platform = db.get_usage_aggregated(user_sub="user-1", scope="user", basis="platform")["total_cost"]
        own_paid = db.get_usage_aggregated(user_sub="user-1", scope="user", basis="self")["total_cost"]
        assert round(platform, 2) == 0.0
        assert round(own_paid, 2) == 4.0

    def test_deleted_sub_becomes_unattributed(self, temp_db):
        """A source_key pointing at no subscription (deleted / legacy) → neither."""
        from storage import database as db
        _usage(db, "user-1", "no-such-sub-id", 9.00)
        platform = db.get_usage_aggregated(user_sub="user-1", scope="user", basis="platform")["total_cost"]
        own_paid = db.get_usage_aggregated(user_sub="user-1", scope="user", basis="self")["total_cost"]
        total = db.get_usage_aggregated(user_sub="user-1", scope="user")["total_cost"]
        assert round(total, 2) == 9.0
        assert round(platform, 2) == 0.0 and round(own_paid, 2) == 0.0


class TestUserLimitGating:
    def test_own_sub_user_never_blocked(self, temp_db):
        """Heavy own-subscription usage never trips the platform-auth budget."""
        from storage import database as db, subscription_store
        from services.billing import usage_service

        own = subscription_store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub="user-1")
        _usage(db, "user-1", own["id"], 100.0)
        db.upsert_usage_limit("role_default", "member", "monthly", 5.0, "admin")

        res = usage_service.check_user_limit("user-1", "member")
        assert res["allowed"] is True

    def test_borrowed_usage_blocks(self, temp_db):
        """Borrowed platform-credential usage over the limit blocks."""
        from storage import database as db, subscription_store
        from services.billing import usage_service

        admin_api = subscription_store.add_subscription(
            "codex-cli", "openai", "api_key", owner_sub="admin", contribute_platform=True)
        _usage(db, "user-2", admin_api["id"], 10.0)
        db.upsert_usage_limit("role_default", "member", "monthly", 5.0, "admin")

        res = usage_service.check_user_limit("user-2", "member")
        assert res["allowed"] is False
        assert res["periods"]["monthly"]["percent"] >= 100


class TestAdminUsersSplit:
    def test_split_columns(self, temp_db):
        """get_all_users_usage returns total + platform + self per user."""
        from storage import database as db, subscription_store

        db.upsert_user("user-1", "u1@x.test", "U1", "member")
        own = subscription_store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub="user-1")
        admin_api = subscription_store.add_subscription(
            "codex-cli", "openai", "api_key", owner_sub="admin", contribute_platform=True)
        _usage(db, "user-1", own["id"], 3.0)
        _usage(db, "user-1", admin_api["id"], 7.0)

        rows = db.get_all_users_usage(_WIDE_START, _WIDE_END)
        u1 = next(r for r in rows if r["sub"] == "user-1")
        assert round(u1["total_cost"], 2) == 10.0
        assert round(u1["platform_cost"], 2) == 7.0
        assert round(u1["self_cost"], 2) == 3.0


class TestNullSafety:
    def test_agent_scope_null_user_sub(self, temp_db):
        """A scope='agent' row with NULL user_sub must not crash the basis join."""
        from storage import database as db, subscription_store

        admin_api = subscription_store.add_subscription(
            "codex-cli", "openai", "api_key", owner_sub="admin", contribute_platform=True)
        db.insert_usage_record(None, "a1", "agent", "task", "t1", 5.0,
                               provider="p", source_key=admin_api["id"], model="m")

        # NULL user_sub with IS DISTINCT FROM → borrowed → platform; no crash.
        plat = db.get_usage_aggregated(scope="agent", basis="platform")["total_cost"]
        own = db.get_usage_aggregated(scope="agent", basis="self")["total_cost"]
        assert round(plat, 2) == 5.0
        assert round(own, 2) == 0.0

    def test_user_with_no_usage_zeroes(self, temp_db):
        """A user with no usage rows yields zeroed split columns (LEFT JOIN NULLs)."""
        from storage import database as db
        db.upsert_user("user-x", "x@x.test", "X", "member")
        rows = db.get_all_users_usage(_WIDE_START, _WIDE_END)
        ux = next(r for r in rows if r["sub"] == "user-x")
        assert ux["total_cost"] == 0 and ux["platform_cost"] == 0 and ux["self_cost"] == 0
