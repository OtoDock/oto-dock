"""The usage endpoints a user owns (api/billing/usage.py): the own-API-key
dollar cap rows (``user_self``) and the pool cap of their own accounts. A
caller only ever touches their own rows; the platform pool cap is admin-only.

Run: cd proxy && python -m pytest tests/billing/test_usage_api.py -v
"""

import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from storage import database as task_store


@pytest.fixture
def client(temp_db):
    from fastapi.testclient import TestClient
    from app import app
    from auth.providers import UserContext, get_current_user

    state = {"user": UserContext(sub="user-viewer", email="v@t.com", name="V", role="member")}

    async def _current():
        return state["user"]

    app.dependency_overrides[get_current_user] = _current
    c = TestClient(app)
    c.as_user = lambda sub, role="member", **kw: state.__setitem__(  # type: ignore[attr-defined]
        "user", UserContext(sub=sub, email=f"{sub}@t.com", name=sub, role=role, **kw))
    try:
        yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


class TestMyLimits:
    def test_round_trip_is_scoped_to_the_caller(self, client):
        assert client.get("/v1/usage/me/limits").json() == {"limits": []}
        r = client.put("/v1/usage/me/limits", json={"period": "monthly", "cost_limit_usd": 12.5})
        assert r.status_code == 200
        rows = client.get("/v1/usage/me/limits").json()["limits"]
        assert [(x["limit_type"], x["target"], x["period"], x["cost_limit_usd"]) for x in rows] == [
            ("user_self", "user-viewer", "monthly", 12.5)]
        assert rows[0]["updated_by"] == "user-viewer"
        # Another user sees nothing of it and cannot remove it.
        client.as_user("user-viewer2")
        assert client.get("/v1/usage/me/limits").json() == {"limits": []}
        assert client.post("/v1/usage/me/limits/delete", json={"period": "monthly"}).status_code == 404
        assert task_store.get_usage_limit("user_self", "user-viewer", "monthly")["cost_limit_usd"] == 12.5

    def test_null_and_delete_clear_the_row(self, client):
        client.put("/v1/usage/me/limits", json={"period": "weekly", "cost_limit_usd": 3})
        client.put("/v1/usage/me/limits", json={"period": "monthly", "cost_limit_usd": 30})
        assert client.put("/v1/usage/me/limits", json={"period": "weekly", "cost_limit_usd": None}).status_code == 200
        assert task_store.get_usage_limit("user_self", "user-viewer", "weekly") is None
        assert client.post("/v1/usage/me/limits/delete", json={"period": "monthly"}).status_code == 200
        assert client.get("/v1/usage/me/limits").json() == {"limits": []}

    def test_validation(self, client):
        assert client.put("/v1/usage/me/limits", json={"period": "daily", "cost_limit_usd": 1}).status_code == 400
        assert client.put("/v1/usage/me/limits", json={"period": "weekly", "cost_limit_usd": -1}).status_code == 400
        # Zero is a valid cap ("nothing on my keys").
        assert client.put("/v1/usage/me/limits", json={"period": "weekly", "cost_limit_usd": 0}).status_code == 200

    def test_summary_and_check_carry_the_self_budget(self, client):
        client.put("/v1/usage/me/limits", json={"period": "monthly", "cost_limit_usd": 4})
        s = client.get("/v1/usage/me").json()
        assert s["self_limits"]["monthly"]["limit"] == 4.0
        assert s["self_limits"]["weekly"]["limit"] is None
        c = client.get("/v1/usage/me/check").json()
        assert c["periods"]["self"]["monthly"]["limit"] == 4.0
        assert c["periods"]["self"]["weekly"] is None
        # Admin listing keeps returning every row, the new type included.
        client.as_user("user-admin", "admin")
        rows = client.get("/v1/admin/usage/limits").json()["limits"]
        assert [r["limit_type"] for r in rows] == ["user_self"]


def _oauth(owner, *, layer="claude-code-cli", pooled=False):
    from storage.billing import subscription_store
    provider = "openai" if layer == "codex-cli" else "anthropic"
    return subscription_store.add_subscription(
        layer, provider, "oauth", owner_sub=owner, contribute_platform=pooled,
        oauth_email=f"{owner}@{layer}.test",
    )["id"]


def _sample(sub_id, seven):
    from datetime import datetime, timezone
    from storage.billing import subscription_store
    assert subscription_store.insert_window_sample(
        sub_id, observed_at=datetime.now(timezone.utc).isoformat(), source="poll",
        five_hour_pct=1.0, five_hour_resets_at="2099-01-01T00:00:00+00:00",
        seven_day_pct=seven, seven_day_resets_at="2099-01-02T00:00:00+00:00",
    )


class TestPoolCap:
    EMPTY = {"week_pct": None, "day_pct": None, "week_usd": None, "day_usd": None}

    def test_user_round_trip_with_partial_updates(self, client):
        from services.engines import subscription_windows as sw
        sw.invalidate_setting_cache()
        assert client.get("/v1/usage/me/pool-cap").json() == {
            "caps": self.EMPTY, "on_reached": "stop", "engines": {}}
        r = client.put("/v1/usage/me/pool-cap", json={"week_pct": 50, "on_reached": "continue"})
        assert r.status_code == 200
        assert r.json()["caps"]["week_pct"] == 50 and r.json()["on_reached"] == "continue"
        # A field left out keeps its value; null clears it.
        r = client.put("/v1/usage/me/pool-cap", json={"day_usd": 2.5})
        assert r.json()["caps"] == {**self.EMPTY, "week_pct": 50, "day_usd": 2.5}
        assert r.json()["on_reached"] == "continue"
        r = client.put("/v1/usage/me/pool-cap", json={"week_pct": None})
        assert r.json()["caps"] == {**self.EMPTY, "day_usd": 2.5}
        # Clearing every field removes the row; the switch reads as default again.
        r = client.put("/v1/usage/me/pool-cap", json={"day_usd": None})
        assert r.json() == {"caps": self.EMPTY, "on_reached": "stop", "engines": {}}
        from storage.billing import subscription_store
        assert subscription_store.get_pool_cap("user", "user-viewer") is None

    def test_own_caps_change_from_a_dashboard_session_only(self, client):
        # The session token an agent subprocess holds resolves to its owner:
        # it may read the caps that bound its own spend, never lift them.
        client.put("/v1/usage/me/pool-cap", json={"week_pct": 50})
        client.put("/v1/usage/me/limits", json={"period": "weekly", "cost_limit_usd": 5})
        client.as_user("user-viewer", is_api_key=True)
        assert client.get("/v1/usage/me/pool-cap").json()["caps"]["week_pct"] == 50
        assert client.get("/v1/usage/me/limits").status_code == 200
        assert client.put("/v1/usage/me/pool-cap", json={"week_pct": None}).status_code == 403
        assert client.put("/v1/usage/me/pool-cap", json={"on_reached": "continue"}).status_code == 403
        assert client.put("/v1/usage/me/limits",
                          json={"period": "weekly", "cost_limit_usd": None}).status_code == 403
        assert client.post("/v1/usage/me/limits/delete", json={"period": "weekly"}).status_code == 403
        client.as_user("user-viewer")
        assert client.get("/v1/usage/me/pool-cap").json()["caps"]["week_pct"] == 50
        assert len(client.get("/v1/usage/me/limits").json()["limits"]) == 1

    def test_validation(self, client):
        bad = [{"week_pct": 0}, {"week_pct": 101}, {"day_pct": -5}, {"week_usd": 0},
               {"day_usd": -1}, {"on_reached": "maybe"}]
        for body in bad:
            assert client.put("/v1/usage/me/pool-cap", json=body).status_code == 400, body
        assert client.put("/v1/usage/me/pool-cap", json={"week_pct": 100, "day_pct": 14.3}).status_code == 200

    def test_engines_and_the_summary_reading(self, client):
        from services.engines import subscription_windows as sw
        sw.invalidate_setting_cache()
        a = _oauth("user-viewer")
        _sample(a, 52.0)
        client.put("/v1/usage/me/pool-cap", json={"week_pct": 50})
        r = client.get("/v1/usage/me/pool-cap").json()
        eng = r["engines"]["claude-code-cli"]
        assert eng["accounts"] == 1 and eng["readings"]["week_pct"] == 52.0
        assert eng["hits"] == ["week_pct"] and eng["allowed"] is False
        assert "codex-cli" not in r["engines"]
        assert client.get("/v1/usage/me").json()["pool"]["claude-code-cli"]["hits"] == ["week_pct"]
        # A second user's pool is their own accounts, not this one's.
        client.as_user("user-viewer2")
        assert client.get("/v1/usage/me/pool-cap").json()["engines"] == {}

    def test_platform_row_is_admin_only(self, client):
        assert client.get("/v1/admin/usage/pool-cap").status_code == 403
        assert client.put("/v1/admin/usage/pool-cap", json={"week_pct": 5}).status_code == 403
        client.as_user("user-admin", "admin")
        a = _oauth("user-admin", layer="codex-cli", pooled=True)
        r = client.put("/v1/admin/usage/pool-cap", json={"week_usd": 40, "on_reached": "continue"})
        assert r.status_code == 200
        assert r.json()["caps"]["week_usd"] == 40 and r.json()["on_reached"] == "continue"
        assert list(r.json()["engines"]) == ["codex-cli"]
        assert r.json()["engines"]["codex-cli"]["caps"]["week_usd"] == 40
        overview = client.get("/v1/admin/usage/overview").json()
        assert overview["pool"]["codex-cli"]["accounts"] == 1
        from storage.billing import subscription_store
        assert subscription_store.get_pool_cap("platform", "")["updated_by"] == "user-admin"
        assert a
