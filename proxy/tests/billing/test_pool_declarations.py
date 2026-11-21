"""A third engine fits the pool by its declaration alone (rule 6).

The engine here is declared and nothing else: a five-hour session window, a
weekly quota and a monthly quota, all of the whole account, the shape Kimi
Code publishes. It has no layer code of its own and no vendor name the pool
could read. Selection, the drain order, the rests and a move by restart all
take it as they take the two engines the platform runs. The session home it
would also declare comes with the homes themselves.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.execution_layer import (
    ExecutionLayer,
    LayerCapabilities,
    UsageProfile,
    WindowSpec,
)
from core.session import session_manager
from services.engines import (
    pool_moves,
    subscription_pool as sp,
    subscription_windows as sw,
)
from tests._pool_store import pool_store

_LAYER = "declared-engine"
_SCOPE = "local:/agents/dev/workspace/.declared"
_CAPS = LayerCapabilities(
    name=_LAYER,
    display_name="A declared engine",
    usage=UsageProfile(windows=(
        WindowSpec(key="five_hour", length_s=5 * 3600, role="session", label="session"),
        WindowSpec(key="weekly", length_s=7 * 86400, role="quota", label="weekly"),
        WindowSpec(key="monthly", length_s=30 * 86400, role="quota", label="monthly"),
    )),
)


class _Declared(ExecutionLayer):
    @property
    def capabilities(self) -> LayerCapabilities:
        return _CAPS


_Declared.__abstractmethods__ = frozenset()


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    monkeypatch.setitem(session_manager._ENGINES, _LAYER, _Declared())
    for d in (sp._session_subscriptions, sp._session_binding_ctx, sp._session_scope_keys, sp._scope_recent,
              sp._session_token_expiry, sp._issued_token_expiry, sp._throttled_until, sp._throttled_hard,
              sp._window_rests, sp._scoped_rests, sp._scope_rebalance_last, sp._session_moved_at):
        d.clear()
    with patch.object(sw, "is_enabled", return_value=True):
        yield
    for d in (sp._session_subscriptions, sp._session_binding_ctx, sp._session_scope_keys, sp._throttled_until,
              sp._throttled_hard, sp._window_rests, sp._scope_rebalance_last):
        d.clear()


def _row(sid: str) -> dict:
    return {"id": sid, "layer": _LAYER, "provider": "vendor", "auth_type": "oauth", "owner_sub": "",
            "is_primary": 0, "status": "active", "active_sessions": 0}


def _reading(*, week=(10.0, 48), month=(10.0, 600), five=5.0) -> dict:
    now = datetime.now(timezone.utc)
    return {"observed_at": now.isoformat(), "source": "poll",
            "five_hour_pct": five, "five_hour_resets_at": (now + timedelta(hours=2)).isoformat(),
            "seven_day_pct": None, "seven_day_resets_at": None,
            "data": {"scoped": [], "reached": "", "plan": "",
                     "windows": {"weekly": {"pct": week[0], "resets_at": (now + timedelta(hours=week[1])).isoformat()},
                                 "monthly": {"pct": month[0],
                                             "resets_at": (now + timedelta(hours=month[1])).isoformat()}}}}


def _store(mock_store, readings: dict) -> None:
    rows = {s: _row(s) for s in readings}
    mock_store.list_platform_pool.return_value = list(rows.values())
    mock_store.list_personal.return_value = []
    mock_store.get_subscription.side_effect = rows.get
    mock_store.get_subscription_consumption.return_value = 0.0
    mock_store.get_credential_data.return_value = {"oauth_token": {"accessToken": "tok", "expiresAt": 0}}
    mock_store.latest_window_samples.side_effect = lambda ids, source=None: {
        s: readings[s] for s in ids if s in readings}


def test_the_declaration_is_read_and_nothing_else():
    specs = sw.window_specs(_LAYER)
    assert list(sw.account_specs(specs)) == ["five_hour", "weekly", "monthly"]
    assert sw.quota_spec(specs).key == "weekly"


@pool_store()
def test_selection_drains_the_quota_that_frees_first(mock_store):
    # a: its week frees in 2 days, its month in 25; b: its month frees tomorrow.
    _store(mock_store, {"sub-a": _reading(week=(30.0, 48), month=(40.0, 600)),
                        "sub-b": _reading(week=(30.0, 120), month=(40.0, 24))})
    got = sp.acquire_subscription(_LAYER, None, model="m-1")
    assert got.subscription_id == "sub-b"


@pool_store()
def test_a_monthly_window_at_its_mark_is_a_master_limit(mock_store):
    _store(mock_store, {"sub-a": _reading(week=(30.0, 48), month=(40.0, 600)),
                        "sub-b": _reading(week=(30.0, 120), month=(98.5, 24))})
    assert sp.acquire_subscription(_LAYER, None, model="m-1").subscription_id == "sub-a"
    reading = sw.latest_readings(mock_store, [_row("sub-b")])["sub-b"]
    assert sw.exhausted(reading) and sw.exhausted_overall(reading)


def test_the_soonest_reset_with_room_is_the_drain_key():
    now = datetime.now(timezone.utc)
    specs = sw.window_specs(_LAYER)
    w = sw.Windows(specs=specs, windows={
        "weekly": sw.Window(98.5, now + timedelta(hours=10)),
        "monthly": sw.Window(20.0, now + timedelta(hours=30))})
    assert sw.quota_reset(w) == now + timedelta(hours=30)          # the week has no room
    w.windows["weekly"] = sw.Window(20.0, now + timedelta(hours=10))
    assert sw.quota_reset(w) == now + timedelta(hours=10)
    w.windows["monthly"] = sw.Window(99.0, now + timedelta(hours=30))
    w.windows["weekly"] = sw.Window(99.0, now + timedelta(hours=10))
    assert sw.quota_reset(w) == now + timedelta(hours=10)          # none has room: the soonest of all


@pool_store()
def test_a_limit_on_the_monthly_window_rests_the_account_until_its_reset(mock_store):
    _store(mock_store, {"sub-a": _reading()})
    sp._session_subscriptions["s1"] = "sub-a"
    sp._session_binding_ctx["s1"] = (_LAYER, "")
    reset = datetime.now(timezone.utc) + timedelta(days=9)
    ending = SimpleNamespace(reason="limit", window="monthly", resets_at=reset.isoformat())
    with patch("services.engines.pool_limits.schedule_rebalance"), \
         patch.object(sw, "request_poll"):
        from services.engines import pool_limits
        assert pool_limits._rest_until_reset("s1", ending)
    assert sp._window_rests["sub-a"][0] == "monthly"
    assert abs(sp._throttled_until["sub-a"] - reset.timestamp()) < 5


@pool_store()
def test_a_move_off_a_refused_account_is_a_restart(mock_store):
    _store(mock_store, {"sub-a": _reading(), "sub-b": _reading()})
    sp._session_subscriptions["s1"] = "sub-a"
    sp._session_binding_ctx["s1"] = (_LAYER, "")
    sp._session_scope_keys["s1"] = _SCOPE
    sp._throttled_until["sub-a"] = time.time() + 3600
    sp._throttled_hard.add("sub-a")
    from services.engines import token_fanout as tf
    with patch.object(tf, "session_target", return_value=tf.CredentialFileTarget(layer=_LAYER, host_dir="/x")), \
         patch.object(tf, "fan_out") as fan, \
         patch.object(pool_moves, "restart_when_idle") as restart, \
         patch("storage.database.get_chat_by_session", return_value={"id": "chat-1", "model": "m-1"}):
        assert sp.rebalance_scopes(reason="test") == 0
    fan.assert_not_called()
    assert restart.call_args.args[0] == ["chat-1"]
    assert "starts again on another account" in restart.call_args.args[1]
