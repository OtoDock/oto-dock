"""The persisted per-session cost baseline (2026-09-24, the CLI-upgrade lane).

Claude Code reports a cumulative ``total_cost_usd`` per session and, since
2.1.277, a ``-p --resume`` continues from the total the previous process saved.
The pump bills per turn as ``total - last seen total``; the last seen total
lived in process memory only, so the first resumed turn after a proxy restart
would have been billed the whole history. The pair
``chats.engine_cost_total`` / ``chats.engine_cost_session_id`` is that baseline
made durable — keyed by session id exactly like the in-memory map, so a fresh
session on the same chat still starts at 0.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_pump_cost_baseline.py -q
"""

import asyncio

import pytest

from core.events import chat_writer
from core.events import stream_pump as sp
from core.events.common_events import METADATA, CommonEvent
from core.events.stream_pump import ChatStreamPump
from services.engines import subscription_pool
from storage import database as task_store


def _mk_pump(chat_id: str, session_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id, session_id=session_id, producer=producer,
        event_queue=asyncio.Queue(), perm_queue=None,
    )


def _metadata_costs(q: asyncio.Queue) -> list[float]:
    out = []
    while True:
        try:
            f = q.get_nowait()
        except asyncio.QueueEmpty:
            return out
        if f.get("pump_type") == "ws_event" and f["event"].get("type") == "metadata":
            out.append(f["event"]["cost_usd"])


@pytest.fixture(autouse=True)
def _no_billing_lookup(monkeypatch):
    monkeypatch.setattr(subscription_pool, "session_cost_billed", lambda sid: True)
    # A clean in-memory map per test: the proxy-restart case is "no entry".
    sp._session_cumulative_cost.clear()
    yield
    sp._session_cumulative_cost.clear()


def _row(**kw) -> dict:
    return {"engine_cost_total": 0.0, "engine_cost_session_id": "", **kw}


@pytest.mark.asyncio
async def test_seed_applies_to_the_same_session_only():
    pump = _mk_pump("cb-seed", "sess-1")
    try:
        # Another session's total on the row: a fresh session starts at 0.
        pump._seed_cost_baseline(_row(engine_cost_total=3.20, engine_cost_session_id="sess-0"))
        assert pump._last_session_cost == 0.0 and "sess-1" not in sp._session_cumulative_cost
        # The same session's total: the baseline the previous process held.
        pump._seed_cost_baseline(_row(engine_cost_total=3.20, engine_cost_session_id="sess-1"))
        assert pump._last_session_cost == 3.20
        assert sp._session_cumulative_cost["sess-1"] == 3.20
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_seed_never_overwrites_a_baseline_the_turn_already_recorded():
    pump = _mk_pump("cb-late", "sess-2")
    try:
        sp._session_cumulative_cost["sess-2"] = 5.0  # the METADATA branch landed first
        pump._seed_cost_baseline(_row(engine_cost_total=3.20, engine_cost_session_id="sess-2"))
        assert sp._session_cumulative_cost["sess-2"] == 5.0
        assert pump._last_session_cost == 0.0  # untouched; the branch owns it
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_seed_tolerates_a_missing_row_and_a_zero_total():
    pump = _mk_pump("cb-empty", "sess-3")
    try:
        pump._seed_cost_baseline({})
        pump._seed_cost_baseline(_row(engine_cost_total=0, engine_cost_session_id="sess-3"))
        pump._seed_cost_baseline(_row(engine_cost_total=None, engine_cost_session_id="sess-3"))
        assert pump._last_session_cost == 0.0 and "sess-3" not in sp._session_cumulative_cost
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_resumed_session_bills_only_its_new_turn_and_persists_the_pair(temp_db):
    temp_db.create_chat("cb-resume", "user-admin", "a1")
    # What the previous proxy process left on the row at its last turn.
    task_store.update_chat("cb-resume", engine_cost_total=3.20, engine_cost_session_id="sess-cb-resume")
    pump = _mk_pump("cb-resume", "sess-cb-resume")
    try:
        q = pump.attach()
        pump._seed_cost_baseline(task_store.get_chat("cb-resume") or {})
        # The CLI resumed from its saved total: 3.20 + a 0.05 turn.
        await pump._process_event(CommonEvent(type=METADATA, data={"cost_usd": 3.25}))
        assert _metadata_costs(q) == [pytest.approx(0.05)]
        assert pump._total_cost_delta == pytest.approx(0.05)
        assert await chat_writer.drain("cb-resume", timeout=5)
        row = task_store.get_chat("cb-resume")
        assert row["engine_cost_total"] == pytest.approx(3.25)
        assert row["engine_cost_session_id"] == "sess-cb-resume"
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_cli_that_could_not_save_its_total_resets_the_baseline(temp_db):
    temp_db.create_chat("cb-reset", "user-admin", "a1")
    task_store.update_chat("cb-reset", engine_cost_total=3.20, engine_cost_session_id="sess-cb-reset")
    pump = _mk_pump("cb-reset", "sess-cb-reset")
    try:
        q = pump.attach()
        pump._seed_cost_baseline(task_store.get_chat("cb-reset") or {})
        # SIGKILL before the save: the CLI starts again from 0.
        await pump._process_event(CommonEvent(type=METADATA, data={"cost_usd": 0.05}))
        assert _metadata_costs(q) == [pytest.approx(0.05)]
        assert await chat_writer.drain("cb-reset", timeout=5)
        assert task_store.get_chat("cb-reset")["engine_cost_total"] == pytest.approx(0.05)
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_fresh_session_on_the_same_chat_starts_at_zero(temp_db):
    temp_db.create_chat("cb-fresh", "user-admin", "a1")
    task_store.update_chat("cb-fresh", engine_cost_total=3.20, engine_cost_session_id="sess-old")
    pump = _mk_pump("cb-fresh", "sess-new")  # an engine switch or a rebuilt history
    try:
        q = pump.attach()
        pump._seed_cost_baseline(task_store.get_chat("cb-fresh") or {})
        await pump._process_event(CommonEvent(type=METADATA, data={"cost_usd": 4.00}))
        assert _metadata_costs(q) == [pytest.approx(4.00)]
        assert await chat_writer.drain("cb-fresh", timeout=5)
        row = task_store.get_chat("cb-fresh")
        assert row["engine_cost_session_id"] == "sess-new"
        assert row["engine_cost_total"] == pytest.approx(4.00)
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_delta_engines_never_touch_the_pair(temp_db):
    temp_db.create_chat("cb-delta", "user-admin", "a1")
    pump = _mk_pump("cb-delta", "sess-cb-delta")
    try:
        pump.attach()
        await pump._process_event(CommonEvent(type=METADATA, data={"cost_usd": 0.02, "cost_is_delta": True}))
        pump._save_turn_blocks()
        assert await chat_writer.drain("cb-delta", timeout=5)
        row = task_store.get_chat("cb-delta")
        assert row["engine_cost_total"] == 0 and row["engine_cost_session_id"] == ""
    finally:
        pump.producer.cancel()
