"""Post-compaction context gauge on the pump (CONTEXT_COMPACT phase "usage").

A Codex auto-compaction at/after turn end has no follow-up METADATA frame, so
without this phase the gauge — and ``chats.context_used`` — kept showing the
pre-compaction number until the NEXT turn completed (the stale-counter report
from 2026-07-19 live testing). The "usage" phase is gauge state only: it must
forward live, persist to the chat row, and never become a chip/turn block.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_compact_gauge_pump.py -q
"""

import asyncio

import pytest

from core.events.common_events import CONTEXT_COMPACT, CommonEvent
from core.events.stream_pump import ChatStreamPump
from storage import database as task_store


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )


def _drain(q: asyncio.Queue) -> list[dict]:
    frames = []
    while True:
        try:
            frames.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return frames


@pytest.mark.asyncio
async def test_usage_phase_updates_gauge_and_chat_row_without_a_chip(temp_db):
    temp_db.create_chat("cg1", "user-admin", "a1")
    pump = _mk_pump("cg1")
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(type=CONTEXT_COMPACT, data={
            "phase": "usage", "post_tokens": 8123, "context_max": 258400,
        }))
        events = [
            f["event"] for f in _drain(q) if f.get("pump_type") == "ws_event"
        ]
        assert events == [{
            "type": "context_compact", "phase": "usage",
            "post_tokens": 8123, "context_max": 258400,
        }]
        # Gauge state, not history: no turn block for the usage phase.
        assert pump._turn_blocks == []
        assert pump._context_used == 8123 and pump._context_max == 258400
        # Persisted immediately — a reopened chat shows the compacted size.
        assert task_store.get_chat("cg1")["context_used"] == 8123
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_completed_phase_still_forwards_and_records_the_chip(temp_db):
    temp_db.create_chat("cg2", "user-admin", "a1")
    pump = _mk_pump("cg2")
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(type=CONTEXT_COMPACT, data={
            "phase": "completed", "trigger": "auto",
        }))
        events = [
            f["event"] for f in _drain(q) if f.get("pump_type") == "ws_event"
        ]
        assert [e["type"] for e in events] == ["context_compact"]
        assert pump._turn_blocks == [{
            "type": "context_compact", "phase": "completed", "trigger": "auto",
        }]
        # No post_tokens on the auto chip — the chat row is untouched here.
        assert task_store.get_chat("cg2")["context_used"] == 0
    finally:
        pump.producer.cancel()
