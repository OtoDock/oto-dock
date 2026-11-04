"""Turn-counter memory-capture nudge tests (services/memory_nudge)."""

from __future__ import annotations

from services.memory import memory_nudge
from storage.agents import memory_store


def _reset_module():
    memory_nudge._counters.clear()


def test_nudge_fires_on_threshold_and_rearms(temp_db):
    _reset_module()
    memory_store.update_settings(nudge_turns=3)
    sid = "s-nudge-1"
    assert memory_nudge.maybe_nudge(sid) is None
    assert memory_nudge.maybe_nudge(sid) is None
    out = memory_nudge.maybe_nudge(sid)
    assert out == memory_nudge.NUDGE_TEXT
    # Counter reset → re-arms after another N turns.
    assert memory_nudge.maybe_nudge(sid) is None
    assert memory_nudge.maybe_nudge(sid) is None
    assert memory_nudge.maybe_nudge(sid) == memory_nudge.NUDGE_TEXT


def test_memory_call_resets_counter(temp_db):
    _reset_module()
    memory_store.update_settings(nudge_turns=3)
    sid = "s-nudge-2"
    memory_nudge.maybe_nudge(sid)
    memory_nudge.maybe_nudge(sid)
    memory_nudge.record_memory_call(sid)  # agent saved a memory
    assert memory_nudge.maybe_nudge(sid) is None  # back to 1, not 3
    assert memory_nudge.maybe_nudge(sid) is None
    assert memory_nudge.maybe_nudge(sid) == memory_nudge.NUDGE_TEXT


def test_zero_disables(temp_db):
    _reset_module()
    memory_store.update_settings(nudge_turns=0)
    sid = "s-nudge-3"
    for _ in range(20):
        assert memory_nudge.maybe_nudge(sid) is None
    # Disabled mode doesn't even track sessions.
    assert sid not in memory_nudge._counters


def test_forget_drops_session(temp_db):
    _reset_module()
    memory_store.update_settings(nudge_turns=5)
    memory_nudge.maybe_nudge("s-gone")
    memory_nudge.forget("s-gone")
    assert "s-gone" not in memory_nudge._counters


def test_prune_caps_tracked_sessions(temp_db, monkeypatch):
    _reset_module()
    memory_store.update_settings(nudge_turns=99)
    monkeypatch.setattr(memory_nudge, "_MAX_TRACKED_SESSIONS", 5)
    for i in range(12):
        memory_nudge.maybe_nudge(f"s-{i}")
    assert len(memory_nudge._counters) == 5
    # Oldest evicted, newest kept.
    assert "s-0" not in memory_nudge._counters
    assert "s-11" in memory_nudge._counters


def test_sessions_count_independently(temp_db):
    _reset_module()
    memory_store.update_settings(nudge_turns=2)
    assert memory_nudge.maybe_nudge("a") is None
    assert memory_nudge.maybe_nudge("b") is None
    assert memory_nudge.maybe_nudge("a") == memory_nudge.NUDGE_TEXT
    assert memory_nudge.maybe_nudge("b") == memory_nudge.NUDGE_TEXT


# ---------------------------------------------------------------------------
# The arm after a compaction
# ---------------------------------------------------------------------------

def test_a_compaction_arms_the_next_message_once(temp_db):
    _reset_module()
    memory_nudge._armed.clear()
    memory_store.update_settings(nudge_turns=10)
    sid = "s-armed"
    assert memory_nudge.maybe_nudge(sid) is None
    memory_nudge.arm(sid)
    memory_nudge.arm(sid)  # a second completed phase re-arms harmlessly
    assert memory_nudge.maybe_nudge(sid) == memory_nudge.NUDGE_TEXT
    assert memory_nudge.maybe_nudge(sid) is None
    assert sid not in memory_nudge._armed and memory_nudge._counters[sid] == 1


def test_the_arm_respects_the_off_switch_and_forget(temp_db):
    _reset_module()
    memory_nudge._armed.clear()
    memory_store.update_settings(nudge_turns=0)
    memory_nudge.arm("s-off")
    assert memory_nudge.maybe_nudge("s-off") is None
    assert "s-off" not in memory_nudge._armed
    memory_store.update_settings(nudge_turns=5)
    memory_nudge.arm("s-gone")
    memory_nudge.forget("s-gone")
    assert "s-gone" not in memory_nudge._armed


def test_the_arm_has_its_own_bound(temp_db, monkeypatch):
    _reset_module()
    memory_nudge._armed.clear()
    monkeypatch.setattr(memory_nudge, "_MAX_TRACKED_SESSIONS", 3)
    for i in range(8):
        memory_nudge.arm(f"task-{i}")  # a task session nobody nudges
    assert list(memory_nudge._armed) == ["task-5", "task-6", "task-7"]
