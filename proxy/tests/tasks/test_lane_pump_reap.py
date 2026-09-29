"""A new round firing on a continued worker lane must not orphan the prior
round's open pump.

The observed loss: a delegate ``continue_id`` round registered its pump over a
wedged prior-round pump in ``_active_pumps`` (the prior turn's end never
arrived — severed satellite stream), so the zombie's unflushed ``_turn_blocks``
— the prior round's whole transcript — never reached ``chat_messages``. The
dashboard then showed only the new round. ``_reap_prior_lane_pump`` aborts the
prior pump and awaits its teardown so the pump's ``finally`` persists the
partial turn BEFORE the new round's cursor and prompt land.
"""

import asyncio

import pytest

from core.events.common_events import CommonEvent, PRODUCER_DONE
from core.events.stream_pump import ChatStreamPump, _active_pumps
from services.scheduler.scheduler import _reap_prior_lane_pump
from storage import database as task_store

pytestmark = pytest.mark.asyncio


def _mk_open_pump(chat_id: str) -> ChatStreamPump:
    """A pump whose producer never ends on its own but honors the real
    producer contract: its finally emits PRODUCER_DONE so the pump loop can
    unwind after an abort()."""
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _produce_forever():
        try:
            await asyncio.sleep(3600)
        finally:
            event_queue.put_nowait(CommonEvent(type=PRODUCER_DONE, data={}))

    producer = asyncio.get_event_loop().create_task(_produce_forever())
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=event_queue,
        perm_queue=None,
    )


async def test_reap_persists_prior_rounds_blocks(temp_db):
    # A plain (non-task) id: the reap itself is id-shape agnostic, and a
    # task-shaped id would run the run-status sync (a no-op without a run row).
    cid = "lane-reap-1"
    task_store.create_chat(cid, "user-1", "pa")
    pump = _mk_open_pump(cid)
    _active_pumps[cid] = pump
    pump.start()
    await asyncio.sleep(0)  # let the pump loop start
    pump._turn_blocks.append({"type": "text", "content": "round-1 report"})

    await _reap_prior_lane_pump(cid, "run-next")

    msgs = task_store.get_chat_messages(cid)
    assert any(
        m["role"] == "assistant" and "round-1 report" in (m.get("content") or "")
        for m in msgs
    ), msgs
    # The lane is clean — the new round's pump registration won't be clobbered
    # by a late zombie teardown.
    assert cid not in _active_pumps


async def test_reap_noop_without_open_pump(temp_db):
    cid = "lane-reap-2"
    task_store.create_chat(cid, "user-1", "pa")
    await _reap_prior_lane_pump(cid, "run-next")  # no pump — no crash
    assert cid not in _active_pumps


async def test_reap_noop_on_finished_pump(temp_db):
    cid = "lane-reap-3"
    task_store.create_chat(cid, "user-1", "pa")
    pump = _mk_open_pump(cid)
    pump._done = True  # already finished — nothing to reap
    _active_pumps[cid] = pump
    try:
        await _reap_prior_lane_pump(cid, "run-next")
        assert _active_pumps.get(cid) is pump  # left alone
    finally:
        _active_pumps.pop(cid, None)
        pump.producer.cancel()
