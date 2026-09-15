"""Provider windows: the vendor's 5-hour / weekly window state per subscription.

Store half (DB-backed): samples append and coalesce, the latest-per-account
read, the since-with-baseline read, pruning, the alert rows, and the cascade
when a subscription is removed.

Run: cd proxy && python -m pytest tests/billing/test_subscription_windows.py -v
"""

import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


def _sub(owner="user-1", layer="claude-code-cli", provider="anthropic"):
    from storage.billing import subscription_store
    return subscription_store.add_subscription(layer, provider, "oauth", owner_sub=owner)


def _insert(sub_id, at, *, five=10.0, five_reset="2026-09-11T10:00:00+00:00",
            seven=55.0, seven_reset="2026-09-16T06:00:00+00:00", source="poll",
            data=None):
    from storage.billing import subscription_store
    return subscription_store.insert_window_sample(
        sub_id, observed_at=at, source=source,
        five_hour_pct=five, five_hour_resets_at=five_reset,
        seven_day_pct=seven, seven_day_resets_at=seven_reset,
        data=data,
    )


class TestSamples:
    def test_latest_per_subscription(self, temp_db):
        from storage.billing import subscription_store
        a, b = _sub("u1")["id"], _sub("u2")["id"]
        assert _insert(a, "2026-09-11T05:00:00+00:00", seven=40.0)
        assert _insert(a, "2026-09-11T06:00:00+00:00", seven=45.0)
        assert _insert(b, "2026-09-11T05:30:00+00:00", seven=8.0,
                       data={"plan": "plus", "scoped": []})

        latest = subscription_store.latest_window_samples([a, b, "missing"])
        assert set(latest) == {a, b}
        assert latest[a]["seven_day_pct"] == 45.0
        assert latest[a]["observed_at"] == "2026-09-11T06:00:00+00:00"
        assert latest[b]["data"] == {"plan": "plus", "scoped": []}
        assert subscription_store.latest_window_samples([]) == {}

    def test_identical_values_coalesce_within_a_minute(self, temp_db):
        from storage.billing import subscription_store
        a = _sub()["id"]
        assert _insert(a, "2026-09-11T05:00:00+00:00", source="claude_event")
        # Same windows 20 s later: the turn's next response, not a new observation.
        assert not _insert(a, "2026-09-11T05:00:20+00:00", source="claude_event")
        # A changed value is always written…
        assert _insert(a, "2026-09-11T05:00:30+00:00", source="claude_event", seven=56.0)
        # …and so are identical values once the minute has passed.
        assert _insert(a, "2026-09-11T05:01:31+00:00", source="claude_event", seven=56.0)
        rows = subscription_store.window_samples_since(a, "2000-01-01T00:00:00+00:00")
        assert [r["seven_day_pct"] for r in rows] == [55.0, 56.0, 56.0]

    def test_since_includes_the_baseline_before_the_window(self, temp_db):
        from storage.billing import subscription_store
        a = _sub()["id"]
        for at, pct in (("2026-09-10T01:00:00+00:00", 10.0),
                        ("2026-09-10T02:00:00+00:00", 12.0),
                        ("2026-09-11T03:00:00+00:00", 20.0),
                        ("2026-09-11T04:00:00+00:00", 25.0)):
            assert _insert(a, at, seven=pct)
        rows = subscription_store.window_samples_since(a, "2026-09-11T00:00:00+00:00")
        assert [r["seven_day_pct"] for r in rows] == [12.0, 20.0, 25.0]
        # Nothing before the window: only the in-window rows.
        rows = subscription_store.window_samples_since(a, "2026-09-10T00:00:00+00:00")
        assert [r["seven_day_pct"] for r in rows] == [10.0, 12.0, 20.0, 25.0]

    def test_prune_and_cascade(self, temp_db):
        from storage.billing import subscription_store
        a = _sub()["id"]
        assert _insert(a, "2026-09-01T00:00:00+00:00", seven=1.0)
        assert _insert(a, "2026-09-11T00:00:00+00:00", seven=2.0)
        assert subscription_store.prune_window_samples("2026-09-03T00:00:00+00:00") == 1
        assert subscription_store.latest_window_samples([a])[a]["seven_day_pct"] == 2.0
        subscription_store.upsert_window_alert(
            a, "seven_day", 90, resets_at="2026-09-16T06:00", fired_at="2026-09-11T00:00:00+00:00")
        assert subscription_store.delete_subscription(a)
        assert subscription_store.latest_window_samples([a]) == {}
        assert subscription_store.get_window_alert(a, "seven_day", 90) is None

    def test_null_windows_round_trip(self, temp_db):
        """A vendor that reports only one window (an idle Claude account has a
        session window with no reset) stores NULLs, never zeros."""
        from storage.billing import subscription_store
        a = _sub()["id"]
        assert _insert(a, "2026-09-11T05:00:00+00:00", five=None, five_reset=None)
        row = subscription_store.latest_window_samples([a])[a]
        assert row["five_hour_pct"] is None and row["five_hour_resets_at"] is None
        assert row["seven_day_pct"] == 55.0


class TestAlerts:
    def test_upsert_replaces_the_window_instance(self, temp_db):
        from storage.billing import subscription_store
        a = _sub()["id"]
        assert subscription_store.get_window_alert(a, "seven_day", 90) is None
        subscription_store.upsert_window_alert(
            a, "seven_day", 90, resets_at="2026-09-16T06:00", fired_at="2026-09-11T00:00:00+00:00")
        subscription_store.upsert_window_alert(
            a, "seven_day", 90, resets_at="2026-09-23T06:00", fired_at="2026-09-18T00:00:00+00:00")
        row = subscription_store.get_window_alert(a, "seven_day", 90)
        assert row["resets_at"] == "2026-09-23T06:00"
        assert row["fired_at"] == "2026-09-18T00:00:00+00:00"
        # Thresholds and windows are independent rows.
        assert subscription_store.get_window_alert(a, "seven_day", 100) is None
        assert subscription_store.get_window_alert(a, "scoped:fable", 90) is None


class TestListingsAndSetting:
    """Both AI Engines listings carry each OAuth row's effective reading, and
    the admin switch removes it (and stops every read) when off."""

    def _client(self):
        from fastapi.testclient import TestClient
        from app import app
        from auth.providers import UserContext, get_current_user

        async def _admin():
            return UserContext(sub="local:admin", email="a@t.com", name="A", role="admin")

        app.dependency_overrides[get_current_user] = _admin
        return TestClient(app), app, get_current_user

    def test_windows_on_both_listings_and_the_switch(self, temp_db):
        from services.engines import subscription_windows as sw
        from storage.billing import subscription_store
        sw.invalidate_setting_cache()
        client, app, dep = self._client()
        try:
            sub = subscription_store.add_subscription(
                "claude-code-cli", "anthropic", "oauth", owner_sub="local:admin",
                contribute_platform=True)
            key = subscription_store.add_subscription(
                "codex-cli", "openai", "api_key", owner_sub="local:admin",
                contribute_platform=True)

            def _claude_row(payload):
                layer = next(x for x in payload["layers"] if x["name"] == "claude-code-cli")
                return layer["user_subscriptions"][0]

            mine = client.get("/v1/users/me/execution-layers").json()
            assert _claude_row(mine)["windows"] is None          # no sample yet
            codex = next(x for x in mine["layers"] if x["name"] == "codex-cli")
            assert "windows" not in codex["user_subscriptions"][0]  # keys have no windows
            assert key["auth_type"] == "api_key"

            assert _insert(sub["id"], "2026-09-11T05:00:00+00:00", seven=55.0,
                           seven_reset="2999-01-01T00:00:00+00:00",
                           five=None, five_reset=None,
                           data={"scoped": [{"key": "fable", "label": "Fable", "pct": 100.0,
                                             "resets_at": "2999-01-01T00:00:00+00:00",
                                             "active": True}],
                                 "reached": "scoped:fable", "plan": "max"})
            row = _claude_row(client.get("/v1/users/me/execution-layers").json())
            assert row["windows"]["seven_day"]["pct"] == 55.0
            assert row["windows"]["five_hour"] is None
            assert row["windows"]["scoped"][0]["key"] == "fable"
            assert row["windows"]["reached"] == "scoped:fable"

            admin = client.get("/v1/admin/execution-layers").json()
            platform = next(x for x in admin["layers"] if x["name"] == "claude-code-cli")
            assert platform["subscriptions"]["platform"][0]["windows"]["seven_day"]["pct"] == 55.0

            settings = client.get("/v1/admin/platform-settings").json()
            assert settings["subscription_windows_enabled"] is True
            resp = client.put("/v1/admin/platform-settings",
                              json={"subscription_windows_enabled": False})
            assert resp.status_code == 200
            assert client.get("/v1/admin/platform-settings").json()["subscription_windows_enabled"] is False
            assert "windows" not in _claude_row(client.get("/v1/users/me/execution-layers").json())
            assert not sw.is_enabled()

            client.put("/v1/admin/platform-settings", json={"subscription_windows_enabled": True})
            assert _claude_row(client.get("/v1/users/me/execution-layers").json())["windows"]["seven_day"]["pct"] == 55.0
        finally:
            app.dependency_overrides.pop(dep, None)
            sw.invalidate_setting_cache()


class TestLatestBySource:
    def test_newest_poll_per_subscription(self, temp_db):
        from storage.billing import subscription_store
        a = _sub()["id"]
        assert _insert(a, "2026-09-11T05:00:00+00:00", seven=40.0, source="poll",
                       data={"scoped": [{"key": "fable", "label": "Fable", "pct": 100.0,
                                         "resets_at": "2026-09-16T06:00:00+00:00", "active": True}],
                             "reached": "scoped:fable", "plan": "max"})
        assert _insert(a, "2026-09-11T05:03:00+00:00", seven=41.0, source="claude_event")
        newest = subscription_store.latest_window_samples([a])[a]
        assert newest["source"] == "claude_event" and newest["data"] == {}
        poll = subscription_store.latest_window_samples([a], source="poll")[a]
        assert poll["source"] == "poll" and poll["data"]["scoped"][0]["pct"] == 100.0
        assert subscription_store.latest_window_samples([a], source="codex_event") == {}
        # The merged reading keeps the Fable window through the event.
        from services.engines import subscription_windows as sw
        r = sw.latest([a])[a]
        assert r.source == "claude_event" and r.seven_day.pct == 41.0
        assert r.scoped_for("fable").pct == 100.0 and r.reached == "scoped:fable"
