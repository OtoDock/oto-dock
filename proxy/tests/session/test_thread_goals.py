"""Codex thread-goal flow — pump GOAL_UPDATE handling + chat restore.

The translator mapping (thread/goal/updated|cleared → GOAL_UPDATE) is locked
in test_parity_extended.py; this module covers the pump half — the live_state
mirror, the goal_update WS frame, the change-gated chats.thread_goal
write-through (cleared must NULL the column even in a pump that never saw the
goal being set) — and _build_chat_restore shipping restore.goal.
"""

import asyncio
import json

import pytest

from core.events import stream_pump
from core.events.common_events import GOAL_UPDATE, CommonEvent
from core.events.stream_pump import ChatStreamPump
from storage import database as task_store

GOAL_EVENT = {
    "objective": "Ship the release",
    "status": "active",
    "token_budget": 500000,
    "tokens_used": 12345,
    "time_used_seconds": 61,
    "cleared": False,
}


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )


def _seed_live(chat_id: str, session_id: str) -> dict:
    live = {
        "streaming": True,
        "session_id": session_id,
        "live_blocks": [],
        "active_tools": [],
        "active_agents": [],
        "todos": [],
        "goal": None,
    }
    stream_pump._chat_streaming_state[chat_id] = live
    return live


def _drain(q: asyncio.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


@pytest.mark.asyncio
async def test_goal_update_forwards_mirrors_and_persists(temp_db):
    temp_db.create_chat("gc1", "user-admin", "a1")
    pump = _mk_pump("gc1")
    live = _seed_live("gc1", pump.session_id)
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(GOAL_UPDATE, dict(GOAL_EVENT)))

        frames = _drain(q)
        assert len(frames) == 1 and frames[0]["pump_type"] == "ws_event"
        goal = frames[0]["event"]
        assert goal["type"] == "goal_update"
        assert goal["goal"] == {
            "objective": "Ship the release", "status": "active",
            "token_budget": 500000, "tokens_used": 12345,
            "time_used_seconds": 61,
        }
        assert live["goal"] == goal["goal"]
        # No turn block — panel-only state (reloads restore from the column).
        assert pump._turn_blocks == []

        assert await stream_pump.chat_writer.drain("gc1", timeout=5)
        stored = json.loads(task_store.get_chat("gc1")["thread_goal"])
        assert stored == goal["goal"]
    finally:
        stream_pump._chat_streaming_state.pop("gc1", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_goal_update_db_write_is_change_gated(temp_db):
    """An identical repeat forwards to WS but skips the redundant DB write
    (each update_chat bumps updated_at, which reorders the chat list)."""
    temp_db.create_chat("gc2", "user-admin", "a1")
    pump = _mk_pump("gc2")
    _seed_live("gc2", pump.session_id)
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(GOAL_UPDATE, dict(GOAL_EVENT)))
        assert await stream_pump.chat_writer.drain("gc2", timeout=5)
        updated_at = task_store.get_chat("gc2")["updated_at"]

        await pump._process_event(CommonEvent(GOAL_UPDATE, dict(GOAL_EVENT)))
        assert await stream_pump.chat_writer.drain("gc2", timeout=5)
        assert task_store.get_chat("gc2")["updated_at"] == updated_at
        assert len(_drain(q)) == 2  # both still forwarded live

        progressed = dict(GOAL_EVENT, tokens_used=20000)
        await pump._process_event(CommonEvent(GOAL_UPDATE, progressed))
        assert await stream_pump.chat_writer.drain("gc2", timeout=5)
        row = task_store.get_chat("gc2")
        assert row["updated_at"] != updated_at
        assert json.loads(row["thread_goal"])["tokens_used"] == 20000
    finally:
        stream_pump._chat_streaming_state.pop("gc2", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_goal_cleared_nulls_column_across_pumps(temp_db):
    """cleared must NULL chats.thread_goal even when THIS pump never saw the
    goal being set (it was written by a previous turn's pump)."""
    temp_db.create_chat("gc3", "user-admin", "a1")
    task_store.update_chat("gc3", thread_goal=json.dumps(
        {"objective": "old", "token_budget": None,
         "tokens_used": 1, "time_used_seconds": 2}))

    pump = _mk_pump("gc3")
    live = _seed_live("gc3", pump.session_id)
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(GOAL_UPDATE, {"cleared": True}))

        frames = _drain(q)
        assert frames[0]["event"] == {"type": "goal_update", "goal": None}
        assert live["goal"] is None
        assert await stream_pump.chat_writer.drain("gc3", timeout=5)
        assert task_store.get_chat("gc3")["thread_goal"] is None
    finally:
        stream_pump._chat_streaming_state.pop("gc3", None)
        pump.producer.cancel()


def test_goal_straggler_applies_out_of_band(temp_db, monkeypatch):
    """Codex accounts goal progress AT TURN STOP — the completion update can
    land after turn/completed with no consumer/pump (live-observed: a goal
    "marked complete" stayed active in the DB). The notification routers hand
    such stragglers to apply_goal_events_oob: chats.thread_goal updates and
    the owner's connections get a goal_update broadcast."""
    import json as _json

    from core.layers.codex import CodexEvent, CodexEventTranslator
    from core.layers.codex.goals import apply_goal_events_oob
    from services.notifications import notification_manager

    temp_db.create_chat("gc5", "user-admin", "a1")
    task_store.update_chat("gc5", session_id="sess-gc5")

    sent = []
    monkeypatch.setattr(notification_manager, "broadcast_goal_update",
                        lambda u, c, g: sent.append((u, c, g)))

    t = CodexEventTranslator()
    events = t.translate(CodexEvent(type="thread/goal/updated", data={
        "threadId": "th",
        "goal": {"objective": "Ship it", "status": "complete",
                 "tokenBudget": 1000, "tokensUsed": 900, "timeUsedSeconds": 30},
    }))
    apply_goal_events_oob("sess-gc5", events)

    stored = _json.loads(task_store.get_chat("gc5")["thread_goal"])
    assert stored["status"] == "complete"
    assert sent == [("user-admin", "gc5", stored)]

    # A cleared straggler NULLs the column and broadcasts a null goal.
    events = t.translate(CodexEvent(type="thread/goal/cleared", data={"threadId": "th"}))
    apply_goal_events_oob("sess-gc5", events)
    assert task_store.get_chat("gc5")["thread_goal"] is None
    assert sent[-1] == ("user-admin", "gc5", None)

    # Non-goal events and unknown sessions are no-ops.
    apply_goal_events_oob("sess-unknown", events)
    assert len(sent) == 2


def test_chat_restore_ships_goal(temp_db):
    from ws.dashboard import _build_chat_restore

    temp_db.create_chat("gc4", "user-admin", "a1")
    assert _build_chat_restore("gc4")["goal"] is None

    goal = {"objective": "Ship it", "token_budget": None,
            "tokens_used": 5, "time_used_seconds": 9}
    task_store.update_chat("gc4", thread_goal=json.dumps(goal))
    assert _build_chat_restore("gc4")["goal"] == goal

    # Garbage in the column must not break the chat_history frame.
    task_store.update_chat("gc4", thread_goal="{not json")
    assert _build_chat_restore("gc4")["goal"] is None
