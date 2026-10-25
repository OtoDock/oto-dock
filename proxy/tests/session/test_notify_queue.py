"""The bounded notify queue (notification_manager.NotifyQueue): what a full
queue coalesces, refuses, evicts and keeps, and the stale mark a dropped
status frame leaves for the connection's resync.
"""

import asyncio

import pytest

from services.notifications import notification_manager as nm
from ws import wire_events as wire


def _types(q: nm.NotifyQueue) -> list[str]:
    return [f.get("type") for f in q._queue]


def test_a_status_frame_replaces_the_queued_one_of_its_chat_in_place():
    q = nm.NotifyQueue(bound=10)
    q.put_nowait({"type": wire.CHAT_STATUS, "chat_id": "a", "status": "streaming"})
    q.put_nowait({"type": wire.NOTIFICATION, "delivery": {}})
    q.put_nowait({"type": wire.CHAT_STATUS, "chat_id": "a", "status": "ready"})
    q.put_nowait({"type": wire.CHAT_READ, "chat_id": "a"})
    q.put_nowait({"type": wire.CHAT_READ, "chat_id": "a"})
    assert _types(q) == [wire.CHAT_STATUS, wire.NOTIFICATION, wire.CHAT_READ]
    assert q._queue[0]["status"] == "ready"
    assert q.dropped == 0 and not q.stale


def test_progress_frames_coalesce_per_key():
    q = nm.NotifyQueue(bound=10)
    for pct in (10, 20, 30):
        q.put_nowait({"type": wire.INSTALL_PROGRESS, "machine_id": "m", "agent": "a", "pct": pct})
    q.put_nowait({"type": wire.INSTALL_PROGRESS, "machine_id": "m", "agent": "b", "pct": 5})
    q.put_nowait({"type": wire.TRANSFER_PROGRESS, "transfer_id": "t1", "done": 1})
    q.put_nowait({"type": wire.TRANSFER_PROGRESS, "transfer_id": "t1", "done": 2})
    q.put_nowait({"type": wire.SATELLITE_UPDATE_SYNC, "inflight": []})
    q.put_nowait({"type": wire.SATELLITE_UPDATE_SYNC, "inflight": [1]})
    assert [(f["type"], f.get("pct") or f.get("done") or f.get("inflight")) for f in q._queue] == [
        (wire.INSTALL_PROGRESS, 30), (wire.INSTALL_PROGRESS, 5),
        (wire.TRANSFER_PROGRESS, 2), (wire.SATELLITE_UPDATE_SYNC, [1]),
    ]


def test_a_moves_progress_replaces_the_queued_one_of_its_chat():
    """A slow tab holds one progress frame per moving chat: the newest."""
    q = nm.NotifyQueue(bound=10)
    for done in (1, 2, 3):
        q.put_nowait({"type": wire.CHAT_MOVE_PROGRESS, "chat_id": "a", "done": done})
    q.put_nowait({"type": wire.CHAT_MOVE_PROGRESS, "chat_id": "b", "done": 9})
    q.put_nowait({"type": wire.CHAT_MOVED, "chat_id": "a"})
    assert [(f["type"], f.get("done")) for f in q._queue] == [
        (wire.CHAT_MOVE_PROGRESS, 3), (wire.CHAT_MOVE_PROGRESS, 9), (wire.CHAT_MOVED, None)]


def test_a_chats_rows_frame_replaces_the_queued_one_of_its_chat():
    """An interactive chat's tail batches fire one frame each: the queue
    keeps one per chat, the held list of a streaming viewer likewise."""
    q = nm.NotifyQueue()
    q.put_nowait({"type": wire.NOTIFICATION, "delivery": {}})
    q.put_nowait({"type": wire.CHAT_ROWS, "chat_id": "c1", "n": 1})
    q.put_nowait({"type": wire.CHAT_ROWS, "chat_id": "c2"})
    q.put_nowait({"type": wire.CHAT_ROWS, "chat_id": "c1", "n": 2})
    assert _types(q) == [wire.NOTIFICATION, wire.CHAT_ROWS, wire.CHAT_ROWS]
    assert [f.get("n") for f in list(q._queue) if f.get("chat_id") == "c1"] == [2]


def test_the_broadcast_copies_the_drain_discards_are_refused():
    q = nm.NotifyQueue(bound=10)
    q.put_nowait({"type": wire.TITLE_UPDATED, "chat_id": "a", "title": "x"})
    q.put_nowait({"type": wire.ENGINE_SWITCHED, "chat_id": "a"})
    assert q.qsize() == 0 and q.dropped == 0


def test_a_full_queue_evicts_the_least_valuable_first_and_marks_stale():
    q = nm.NotifyQueue(bound=4)
    q.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": 1}})
    q.put_nowait({"type": wire.FILE_UPDATED, "agent_slug": "a", "rel_path": "f"})
    q.put_nowait({"type": wire.CHAT_STATUS, "chat_id": "c1", "status": "ready"})
    q.put_nowait({"type": wire.GOAL_UPDATE, "goal": None})
    assert q.qsize() == 4
    q.put_nowait({"type": wire.CHAT_ROWS, "chat_id": "c2"})
    # The status frame went first (it is the least valuable kind present).
    assert _types(q) == [wire.NOTIFICATION, wire.FILE_UPDATED, wire.GOAL_UPDATE, wire.CHAT_ROWS]
    assert q.stale and q.dropped == 1
    # A kept frame evicts nothing and queues past the bound.
    q.put_nowait({"type": wire.BG_AGENT_DONE, "tool_use_id": "t"})
    assert _types(q) == [wire.NOTIFICATION, wire.FILE_UPDATED, wire.GOAL_UPDATE,
                         wire.CHAT_ROWS, wire.BG_AGENT_DONE]
    assert q.dropped == 1
    # The next evictable frame costs the file update (next in the order).
    q.put_nowait({"type": wire.GOAL_UPDATE, "goal": {"x": 1}})
    assert _types(q) == [wire.NOTIFICATION, wire.GOAL_UPDATE, wire.CHAT_ROWS,
                         wire.BG_AGENT_DONE, wire.GOAL_UPDATE]
    assert q.dropped == 2


def test_kept_frames_are_never_evicted_and_queue_past_the_bound():
    q = nm.NotifyQueue(bound=2)
    q.put_nowait({"type": wire.NOTIFY_TASK_RESULT_PROMPT, "chat_id": "c"})
    q.put_nowait({"type": wire.TURN_COMPLETE, "chat_id": "c"})
    # Nothing evictable waits: a new evictable frame is dropped...
    q.put_nowait({"type": wire.FILE_UPDATED, "agent_slug": "a", "rel_path": "f"})
    assert _types(q) == [wire.NOTIFY_TASK_RESULT_PROMPT, wire.TURN_COMPLETE]
    assert q.dropped == 1 and not q.stale
    # ...and a new kept frame is queued past the bound.
    q.put_nowait({"type": wire.NOTIFY_LOCATION_REQUEST, "data": {}})
    q.put_nowait({"type": wire.INSTALL_DONE, "machine_id": "m", "agent": "a"})
    assert q.qsize() == 4


@pytest.mark.asyncio
async def test_put_never_blocks_and_get_drains_in_order():
    q = nm.NotifyQueue(bound=3)
    for i in range(6):
        await asyncio.wait_for(q.put({"type": wire.NOTIFICATION, "delivery": {"id": i}}), 0.1)
    assert q.qsize() == 6
    assert [(await q.get())["delivery"]["id"] for _ in range(6)] == list(range(6))


def test_purge_status_drops_the_status_frames_and_clears_stale():
    q = nm.NotifyQueue(bound=3)
    q.put_nowait({"type": wire.CHAT_STATUS, "chat_id": "c1", "status": "ready"})
    q.put_nowait({"type": wire.CHAT_READ, "chat_id": "c2"})
    q.put_nowait({"type": wire.NOTIFICATION, "delivery": {}})
    q.put_nowait({"type": wire.CHAT_STATUS, "chat_id": "c3", "status": "streaming"})
    assert q.stale
    assert q.purge_status() == 2
    assert _types(q) == [wire.NOTIFICATION]
    assert not q.stale


def test_a_drop_is_logged_once_a_minute(caplog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(nm.time, "monotonic", lambda: now[0])
    q = nm.NotifyQueue(bound=1)
    q.put_nowait({"type": wire.NOTIFICATION, "delivery": {}})
    with caplog.at_level("WARNING"):
        for _ in range(5):
            q.put_nowait({"type": wire.FILE_UPDATED, "agent_slug": "a", "rel_path": "f"})
        now[0] += 61
        q.put_nowait({"type": wire.FILE_UPDATED, "agent_slug": "a", "rel_path": "g"})
    lines = [r.getMessage() for r in caplog.records if "notify queue full" in r.getMessage()]
    assert len(lines) == 2
    assert "dropped 1 frame" in lines[0] and "dropped 5 frame" in lines[1]
    assert q.dropped == 6


@pytest.mark.asyncio
async def test_unget_puts_the_items_first_in_order_and_wakes_a_getter():
    q = nm.NotifyQueue(bound=4)
    q.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": "later"}})
    q.unget([{"type": wire.NOTIFY_BG_NUDGE, "count": 1},
             {"type": wire.CHAT_STATUS, "chat_id": "a", "status": "ready"}])
    assert _types(q) == [wire.NOTIFY_BG_NUDGE, wire.CHAT_STATUS, wire.NOTIFICATION]
    # Past the bound the items stay: each was counted when first queued.
    q.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": 2}})
    q.unget([{"type": wire.NOTIFY_SERVER_KICK, "chat_id": "c"}])
    assert q.qsize() == 5 and q.dropped == 0
    empty = nm.NotifyQueue(bound=4)
    getter = asyncio.ensure_future(empty.get())
    await asyncio.sleep(0)
    empty.unget([{"type": wire.NOTIFICATION, "delivery": {"id": 9}}])
    assert (await asyncio.wait_for(getter, 1))["delivery"]["id"] == 9
