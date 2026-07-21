"""Unit tests for SubagentRegistry — deterministic subagent spawn/finish gating.

Pure state-machine tests (no DB, no subprocess). Covers idempotency, the
SubagentStop-before-task_started race (pending_stops), the all-done event, and
per-turn reset.
"""

from __future__ import annotations

import asyncio

from core.session.session_state import (
    SubagentRegistry, _chat_streaming_state, _dashboard_notify_queues,
    _subagent_registries, clear_session_liveness, get_subagent_registry,
    reset_subagent_registry,
)


def test_register_then_done_clears_pending():
    r = SubagentRegistry()
    r.register_spawn("ag1", "tu1")
    assert r.has_pending is True
    assert r.pending_count == 1
    assert r.tuid_for("ag1") == "tu1"
    assert r.mark_done("ag1") is True
    assert r.has_pending is False
    assert r.pending_count == 0


def test_mark_done_is_idempotent():
    r = SubagentRegistry()
    r.register_spawn("ag1", "tu1")
    assert r.mark_done("ag1") is True    # transition
    assert r.mark_done("ag1") is False   # already done — dedups WS emission
    assert r.mark_done("ag1") is False


def test_out_of_order_stop_before_spawn_reconciles():
    """SubagentStop (hook) can race ahead of task_started (stdout). buffer=True
    parks it; register_spawn then reconciles it to completed."""
    r = SubagentRegistry()
    assert r.mark_done("ag1", buffer=True) is False   # not spawned yet → parked
    assert "ag1" in r.pending_stops
    assert r.has_pending is False                      # nothing spawned yet
    r.register_spawn("ag1", "tu1")
    assert "ag1" not in r.pending_stops
    assert "ag1" in r.completed
    assert r.has_pending is False


def test_unknown_done_without_buffer_ignored():
    """task_notification backup must not pollute the gate with untracked ids
    (local_bash etc.) — no buffering, no completion."""
    r = SubagentRegistry()
    assert r.mark_done("ghost") is False
    assert "ghost" not in r.pending_stops
    assert "ghost" not in r.completed


def test_all_done_event_not_vacuous():
    """The all-done event must NOT fire when nothing was ever spawned (the
    monitor awaits it after spawns, so a vacuous fire would mis-nudge)."""
    r = SubagentRegistry()
    assert r._all_done_event.is_set() is False
    r.register_spawn("ag1", "tu1")
    assert r._all_done_event.is_set() is False
    r.mark_done("ag1")
    assert r._all_done_event.is_set() is True


def test_event_reclears_when_new_agent_spawns():
    r = SubagentRegistry()
    r.register_spawn("ag1", "tu1")
    r.mark_done("ag1")
    assert r._all_done_event.is_set() is True
    r.register_spawn("ag2", "tu2")          # new pending → not all done
    assert r._all_done_event.is_set() is False
    assert r.has_pending is True


def test_wait_all_done_returns_after_completion():
    async def scenario():
        r = SubagentRegistry()
        r.register_spawn("ag1", "tu1")
        r.register_spawn("ag2", "tu2")
        waiter = asyncio.create_task(r.wait_all_done())
        await asyncio.sleep(0)
        assert not waiter.done()
        r.mark_done("ag1")
        await asyncio.sleep(0)
        assert not waiter.done()            # one still pending
        r.mark_done("ag2")
        await asyncio.wait_for(waiter, timeout=1.0)  # now resolves
    asyncio.run(scenario())


def test_reset_drops_resolved_preserves_event_identity():
    # reset() drops fully-resolved agents + per-turn bookkeeping, but preserves
    # still-pending bg agents (covered by test_codex_subagent_turn) and the Event.
    r = SubagentRegistry()
    ev = r._all_done_event
    r.register_spawn("ag1", "tu1")
    r.mark_done("ag1")                       # resolved this turn → dropped on reset
    r.chat_id = "chat-1"
    r.workflow_tuids.add("tu-wf")
    r.reset()
    assert r.spawned == set() and r.completed == set()
    assert r.pending_stops == set() and r.workflow_tuids == set()
    assert r.task_to_tuid == {}
    assert r.chat_id == ""
    assert r._all_done_event is ev          # same Event object (no orphaned waiters)
    assert ev.is_set() is False


def test_module_accessors_get_and_reset():
    reset_subagent_registry("nope")          # no-op when absent — must not raise
    reg = get_subagent_registry("sess-xyz")
    assert isinstance(reg, SubagentRegistry)
    assert get_subagent_registry("sess-xyz") is reg   # same instance per session
    reg.register_spawn("ag1", "tu1")
    reg.mark_done("ag1")                      # resolved → reset drops it
    reset_subagent_registry("sess-xyz")
    assert reg.has_pending is False           # reset acted on the live instance


# ---------------------------------------------------------------------------
# clear_session_liveness — dead-session badge/registry cleanup
# ---------------------------------------------------------------------------

from core.session import session_state
from core.events.bg_command_state import (
    _bg_command_registries, get_bg_command_registry,
)


def test_clear_liveness_pops_matching_chats_and_broadcasts():
    """Only chats whose live-state entry belongs to the dead session are popped
    + announced; a chat already re-warmed onto a new session is untouched. One
    WS registered under several session ids gets the item exactly once."""
    sid = "sid-dead-1"
    _chat_streaming_state["chat-dead-1"] = {
        "session_id": sid, "active_agents": [{"active": True}],
    }
    _chat_streaming_state["chat-alive-1"] = {"session_id": "sid-alive-1"}
    q = asyncio.Queue()
    _dashboard_notify_queues["viewer-a"] = q
    _dashboard_notify_queues["viewer-b"] = q   # same socket, second session id
    try:
        clear_session_liveness(sid, reason="test")
        assert "chat-dead-1" not in _chat_streaming_state
        assert "chat-alive-1" in _chat_streaming_state
        item = q.get_nowait()
        assert item["type"] == "liveness_clear"
        assert item["chat_id"] == "chat-dead-1"
        assert item["session_id"] == sid
        assert item["reason"] == "test"
        assert q.empty()                       # deduped by queue object
    finally:
        _chat_streaming_state.pop("chat-alive-1", None)
        _dashboard_notify_queues.pop("viewer-a", None)
        _dashboard_notify_queues.pop("viewer-b", None)


def test_clear_liveness_collects_registry_chats_and_pops_registries():
    """Chats known only via the registries' chat_id are still announced, and
    BOTH per-session registries are dropped (the bg-command one previously
    leaked — nothing ever popped it)."""
    sid = "sid-dead-2"
    reg = get_subagent_registry(sid)
    reg.chat_id = "chat-reg-2"
    reg.register_spawn("ag1", "tu1")           # a pending ghost
    bgreg = get_bg_command_registry(sid)
    bgreg.chat_id = "chat-bgreg-2"
    q = asyncio.Queue()
    _dashboard_notify_queues["viewer-c"] = q
    try:
        clear_session_liveness(sid)
        assert sid not in _subagent_registries
        assert sid not in _bg_command_registries
        announced = set()
        while not q.empty():
            announced.add(q.get_nowait()["chat_id"])
        assert announced == {"chat-reg-2", "chat-bgreg-2"}
    finally:
        _dashboard_notify_queues.pop("viewer-c", None)


def test_clear_liveness_noop_without_chats():
    """A session with no live-state entries and no registry chats broadcasts
    nothing (pre-warm closes, already-cleared sessions)."""
    q = asyncio.Queue()
    _dashboard_notify_queues["viewer-d"] = q
    try:
        clear_session_liveness("sid-unknown-3")
        assert q.empty()
    finally:
        _dashboard_notify_queues.pop("viewer-d", None)


def test_cleanup_session_permission_state_clears_liveness(monkeypatch):
    """The shared close-path purge routes through clear_session_liveness, so
    the remote/interactive/meeting closes clear badges without their own call."""
    sid = "sid-dead-4"
    _chat_streaming_state["chat-cleanup-4"] = {"session_id": sid}
    q = asyncio.Queue()
    _dashboard_notify_queues["viewer-e"] = q
    monkeypatch.setattr(session_state, "_save_session_security", lambda: None)
    try:
        session_state.cleanup_session_permission_state(sid)
        assert "chat-cleanup-4" not in _chat_streaming_state
        msg = q.get_nowait()
        assert msg["type"] == "liveness_clear"
    finally:
        _chat_streaming_state.pop("chat-cleanup-4", None)
        _dashboard_notify_queues.pop("viewer-e", None)
