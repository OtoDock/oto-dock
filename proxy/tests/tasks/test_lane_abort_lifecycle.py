"""Graceful abort must keep a delegated/task lane's session warm.

The 2026-07-20 incident: dashboard Stop on a delegated worker ran the
graceful path end-to-end (soft interrupt, turn closed, CLI + subagent
children survived) — then the run's turn-end branch closed the session off
``last_turn_aborted`` alone, killing the warm CLI and its 8 in-flight
subagents. The close now consults ``last_abort_graceful`` (stamped by every
dashboard abort site alongside the aborted flag): graceful → keep warm for
the idle reaper, exactly like a dashboard chat; hard/crash/PTY → close as
before.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.scheduler.scheduler import _post_run_session_action


# --- The close decision -----------------------------------------------------

def test_completed_run_never_closes():
    assert _post_run_session_action({"last_turn_aborted": False}) == (
        "completed", False)
    assert _post_run_session_action(None) == ("completed", False)


def test_graceful_abort_keeps_session_warm():
    row = {"last_turn_aborted": True, "last_abort_graceful": True}
    assert _post_run_session_action(row) == ("user_interrupted", True)


def test_hard_abort_closes():
    row = {"last_turn_aborted": True, "last_abort_graceful": False}
    assert _post_run_session_action(row) == ("user_interrupted", False)
    # PTY stops / crashes never stamp the graceful flag at all.
    assert _post_run_session_action({"last_turn_aborted": True}) == (
        "user_interrupted", False)


def test_stale_graceful_without_abort_is_ignored():
    # Step 1 clears both flags per round; even if a stale graceful=True
    # survived, it must not matter without last_turn_aborted.
    row = {"last_turn_aborted": False, "last_abort_graceful": True}
    assert _post_run_session_action(row) == ("completed", False)


# --- Subscription seat accounting on re-bind --------------------------------

def _bind(session_id, sub_id, monkeypatch, calls):
    from services.engines import subscription_pool as pool
    monkeypatch.setattr(
        pool.subscription_store, "upsert_session_binding",
        lambda *a, **k: None)
    monkeypatch.setattr(
        pool.subscription_store, "decrement_active_sessions",
        calls.append)
    pool.bind_session(session_id, sub_id)


def test_fresh_bind_releases_nothing(monkeypatch):
    from services.engines import subscription_pool as pool
    calls: list[str] = []
    sid = "seat-fresh-1"
    pool._session_subscriptions.pop(sid, None)
    try:
        _bind(sid, "sub-A", monkeypatch, calls)
        assert calls == []
    finally:
        pool._session_subscriptions.pop(sid, None)


@pytest.mark.parametrize("second_sub", ["sub-A", "sub-B"])
def test_rebind_releases_replaced_seat(monkeypatch, second_sub):
    """A warm continue round acquires a FRESH seat before re-binding — the
    replaced binding's seat must be released, same-sub included (two
    increments, one binding, one eventual release = +1 leak per round)."""
    from services.engines import subscription_pool as pool
    calls: list[str] = []
    sid = "seat-rebind-1"
    pool._session_subscriptions.pop(sid, None)
    try:
        _bind(sid, "sub-A", monkeypatch, calls)
        _bind(sid, second_sub, monkeypatch, calls)
        assert calls == ["sub-A"]
        assert pool._session_subscriptions[sid] == second_sub
    finally:
        pool._session_subscriptions.pop(sid, None)


# --- Idle-reaper leash (pending subagents / recent hooks) -------------------

def test_reaper_leash_pending_subagents(temp_db):
    from core.remote.remote_execution import _idle_session_has_pending_work
    from core.session.session_state import (
        get_subagent_registry, reset_subagent_registry,
    )
    sid = "leash-sess-1"
    reset_subagent_registry(sid)
    now = time.monotonic()
    assert _idle_session_has_pending_work(sid, now, 900.0) is False
    reg = get_subagent_registry(sid)
    reg.register_spawn("task1", "tu1")
    assert _idle_session_has_pending_work(sid, now, 900.0) is True
    reg.mark_done("task1")
    assert _idle_session_has_pending_work(sid, now, 900.0) is False


def test_reaper_leash_fresh_boot_no_hook_activity(temp_db):
    """The 0 that get_hook_activity returns for a hook-less session must not
    read as 'active at boot': with now < idle_timeout (a host up less than
    the timeout — every fresh CI VM and freshly booted server),
    `now - 0 <= idle_timeout` held the leash and suppressed reaping."""
    from core.remote.remote_execution import _idle_session_has_pending_work
    from core.session.session_state import reset_subagent_registry
    sid = "leash-sess-fresh-boot"
    reset_subagent_registry(sid)
    assert _idle_session_has_pending_work(sid, 300.0, 900.0) is False


def test_reaper_leash_recent_hook_activity(temp_db, monkeypatch):
    from core.remote import remote_execution as rex
    from core.session import session_state
    sid = "leash-sess-2"
    now = time.monotonic()
    monkeypatch.setattr(session_state, "get_hook_activity",
                        lambda s: now - 10.0)
    assert rex._idle_session_has_pending_work(sid, now, 900.0) is True
    monkeypatch.setattr(session_state, "get_hook_activity",
                        lambda s: now - 5000.0)
    assert rex._idle_session_has_pending_work(sid, now, 900.0) is False


# --- Chat delete closes the live session ------------------------------------

@pytest.mark.asyncio
async def test_delete_chat_closes_remote_session(monkeypatch):
    from api.agents import chats as chats_mod
    from core.session import session_manager

    closed: list[str] = []
    remote = MagicMock()
    remote._sessions = {"sess-del-1": object()}
    remote.close_session = AsyncMock(side_effect=closed.append)
    monkeypatch.setattr(session_manager, "_remote_layer", remote)

    await chats_mod._close_chat_session(
        {"session_id": "sess-del-1", "agent": "pa"})
    assert closed == ["sess-del-1"]


@pytest.mark.asyncio
async def test_delete_chat_noop_without_live_session(monkeypatch):
    from api.agents import chats as chats_mod
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "_remote_layer", None)
    # No registry holds the session — must be a silent no-op.
    await chats_mod._close_chat_session(
        {"session_id": "sess-del-absent", "agent": "pa"})
    await chats_mod._close_chat_session({"session_id": "", "agent": "pa"})


# --- A2: warm-session reuse decision ----------------------------------------

class _Info:
    def __init__(self, machine_id="m-1", alive=True, cli_dead=False,
                 turn_active=False):
        self.machine_id = machine_id
        self.alive = alive
        self.cli_dead = cli_dead
        self.turn_active = turn_active


class _Cfg:
    def __init__(self, execution_target="m-1"):
        self.execution_target = execution_target


def _mk_remote(monkeypatch, sid, info, *, proc_dead=False, is_task=True):
    from core.session import session_manager, session_state as _state
    remote = MagicMock()
    remote._sessions = {sid: info} if info else {}
    remote.is_session_process_dead = AsyncMock(return_value=proc_dead)
    monkeypatch.setattr(session_manager, "_remote_layer", remote)
    if is_task:
        _state._sessions[sid] = {"is_task": True}
    else:
        _state._sessions.pop(sid, None)
    return remote


@pytest.mark.asyncio
async def test_reuse_warm_remote_task_session(monkeypatch):
    from services.scheduler.scheduler import _try_reuse_warm_session
    sid = "reuse-sess-1"
    remote = _mk_remote(monkeypatch, sid, _Info())
    assert await _try_reuse_warm_session(remote, _Cfg(), sid, "run-1") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "no_info", "not_alive", "cli_dead", "turn_active",
    "machine_mismatch", "not_task", "proc_dead", "local_layer",
])
async def test_reuse_vetoes(monkeypatch, case):
    from services.scheduler.scheduler import _try_reuse_warm_session
    sid = f"reuse-veto-{case}"
    info = _Info()
    kwargs = dict(proc_dead=False, is_task=True)
    cfg = _Cfg()
    if case == "no_info":
        info = None
    elif case == "not_alive":
        info.alive = False
    elif case == "cli_dead":
        info.cli_dead = True
    elif case == "turn_active":
        info.turn_active = True
    elif case == "machine_mismatch":
        cfg = _Cfg(execution_target="m-OTHER")
    elif case == "not_task":
        kwargs["is_task"] = False
    elif case == "proc_dead":
        kwargs["proc_dead"] = True
    remote = _mk_remote(monkeypatch, sid, info, **kwargs)
    layer = MagicMock() if case == "local_layer" else remote
    assert await _try_reuse_warm_session(layer, cfg, sid, "run-1") is False


# --- A2: run-start lane gate (wait healthy, reap wedged, veto reuse) --------

def test_lane_pump_wedged_states(monkeypatch):
    from core.session import session_manager
    from services.scheduler.scheduler import _lane_pump_wedged
    sid = "wedge-sess-1"
    pump = MagicMock()
    pump.session_id = sid

    remote = MagicMock()
    remote._sessions = {sid: _Info()}
    remote.remote_stream_severed = MagicMock(return_value=False)
    monkeypatch.setattr(session_manager, "_remote_layer", remote)
    assert _lane_pump_wedged(pump) is False          # healthy remote
    remote.remote_stream_severed = MagicMock(return_value=True)
    assert _lane_pump_wedged(pump) is True           # severed stream
    remote.remote_stream_severed = MagicMock(return_value=False)
    remote._sessions[sid].cli_dead = True
    assert _lane_pump_wedged(pump) is True           # dead CLI
    remote._sessions = {}
    monkeypatch.setattr(session_manager, "_remote_layer", None)
    assert _lane_pump_wedged(pump) is True           # no backing session


@pytest.mark.asyncio
async def test_settle_prior_lane_waits_healthy_and_reaps_wedged(monkeypatch):
    from core.events.stream_pump import _active_pumps
    from services.scheduler import scheduler as sched

    waited: list[str] = []
    reaped: list[str] = []

    async def _fake_wait(chat_id, **kw):
        waited.append(chat_id)
        # The healthy turn finishes while we wait.
        _active_pumps.pop(chat_id, None)

    async def _fake_reap(chat_id, run_id):
        reaped.append(chat_id)
        _active_pumps.pop(chat_id, None)

    monkeypatch.setattr(sched, "_await_lane_quiescence", _fake_wait)
    monkeypatch.setattr(sched, "_reap_prior_lane_pump", _fake_reap)

    # No pump at all → nothing happens.
    assert await sched._settle_prior_lane("lane-none", "run-1") is False
    assert waited == [] and reaped == []

    # Healthy live pump → waited out, never reaped, reuse stays allowed.
    pump = MagicMock()
    pump.session_id = "settle-sess-1"
    pump.is_done = False
    _active_pumps["lane-healthy"] = pump
    monkeypatch.setattr(sched, "_lane_pump_wedged", lambda p: False)
    assert await sched._settle_prior_lane("lane-healthy", "run-1") is False
    assert waited == ["lane-healthy"] and reaped == []

    # Wedged pump → reaped immediately, reuse vetoed.
    _active_pumps["lane-wedged"] = pump
    monkeypatch.setattr(sched, "_lane_pump_wedged", lambda p: True)
    assert await sched._settle_prior_lane("lane-wedged", "run-1") is True
    assert reaped == ["lane-wedged"]
