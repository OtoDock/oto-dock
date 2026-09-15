"""``cost_billed`` on the per-turn metadata event.

The chat hides the per-turn cost line and the cost gauge when the session runs
on a subscription (oauth) or a local model (local_endpoint), and shows them on
an API key or the hosted relay. The pump stamps the flag — the one place every
cost emitter (CLI, Codex, direct, satellite, meetings) passes through — on the
WS frame AND the persisted row, so it survives a reload and a proxy restart.
Accounting (deltas, cumulative tracking, usage rows) must not move.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_pump_cost_billed.py -q
"""

import asyncio
import json

import pytest

from core.events import chat_writer
from core.events.common_events import METADATA, CommonEvent
from core.events.stream_pump import ChatStreamPump
from services.engines import subscription_pool
from storage import database as task_store


def _mk_pump(chat_id: str, session_id: str | None = None) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=session_id or f"sess-{chat_id}",
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


def _metadata_frames(q: asyncio.Queue) -> list[dict]:
    return [
        f["event"] for f in _drain(q)
        if f.get("pump_type") == "ws_event" and f["event"].get("type") == "metadata"
    ]


def _bill(monkeypatch, table: dict[str, bool]) -> dict:
    """Fake the credential lookup: session_id -> billed. Unknown sessions
    answer True (an unbound session shows its cost — today's behaviour).
    Returns a call log so caching can be asserted."""
    calls: dict = {"n": 0}

    def _lookup(sid: str) -> bool:
        calls["n"] += 1
        return table.get(sid, True)

    monkeypatch.setattr(subscription_pool, "session_cost_billed", _lookup)
    return calls


@pytest.mark.asyncio
async def test_delta_metadata_hides_cost_on_a_subscription(temp_db, monkeypatch):
    temp_db.create_chat("cb1", "user-admin", "a1")
    _bill(monkeypatch, {"sess-cb1": False})
    pump = _mk_pump("cb1")
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(type=METADATA, data={
            "cost_usd": 0.05, "cost_is_delta": True,
            "duration_ms": 1200, "input_tokens": 10,
        }))
        assert _metadata_frames(q) == [{
            "type": "metadata", "cost_usd": 0.05, "cost_billed": False,
            "duration_ms": 1200, "input_tokens": 10,
        }]
        assert pump._turn_blocks[-1]["cost_billed"] is False
        # Accounting is exactly what it was: the delta still counts in full.
        assert pump._total_cost_delta == 0.05
        assert pump._llm_cost_delta == 0.05
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_cumulative_cli_metadata_shows_cost_on_an_api_key(temp_db, monkeypatch):
    temp_db.create_chat("cb2", "user-admin", "a1")
    calls = _bill(monkeypatch, {"sess-cb2": True})
    pump = _mk_pump("cb2")
    try:
        q = pump.attach()
        # CLI reports a cumulative session total; the pump forwards the delta.
        await pump._process_event(CommonEvent(type=METADATA, data={"cost_usd": 0.30}))
        await pump._process_event(CommonEvent(type=METADATA, data={"cost_usd": 0.45}))
        frames = _metadata_frames(q)
        assert [f["cost_usd"] for f in frames] == [0.30, pytest.approx(0.15)]
        assert all(f["cost_billed"] is True for f in frames)
        assert all(b["cost_billed"] is True for b in pump._turn_blocks)
        assert pump._last_session_cost == 0.45
        assert pump._total_cost_delta == pytest.approx(0.45)
        # Resolved per event, never cached across a pump: a queued turn after
        # a direct-llm provider switch must carry the NEW binding's kind.
        assert calls["n"] == 2
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_flag_is_persisted_in_the_event_row(temp_db, monkeypatch):
    """A reload (chat_history replays the rows) and a proxy restart both read
    the flag back from event_data — the only place it lives."""
    temp_db.create_chat("cb3", "user-admin", "a1")
    temp_db.create_chat("cb4", "user-admin", "a1")
    _bill(monkeypatch, {"sess-cb3": False})  # cb4's session is unbound → True
    for cid in ("cb3", "cb4"):
        pump = _mk_pump(cid)
        try:
            pump.attach()
            await pump._process_event(CommonEvent(type=METADATA, data={
                "cost_usd": 0.02, "cost_is_delta": True,
            }))
            pump._save_turn_blocks()
            assert await chat_writer.drain(cid, timeout=5)
        finally:
            pump.producer.cancel()
    rows = {
        cid: [json.loads(r["event_data"]) for r in task_store.get_chat_messages(cid)
              if r["event_type"] == "metadata"]
        for cid in ("cb3", "cb4")
    }
    assert rows["cb3"] == [{"type": "metadata", "cost_usd": 0.02, "cost_billed": False}]
    assert rows["cb4"] == [{"type": "metadata", "cost_usd": 0.02, "cost_billed": True}]


@pytest.mark.asyncio
async def test_meeting_cost_resolves_the_speakers_session(temp_db, monkeypatch):
    """A meeting cost names the speaker's session: each participant runs on
    its own account, so the host chat's credential must not decide."""
    temp_db.create_chat("meeting-cb5", "user-admin", "a1")
    _bill(monkeypatch, {"sess-host": True, "sess-writer": False})
    pump = _mk_pump("meeting-cb5", session_id="sess-host")
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(type=METADATA, data={
            "cost_usd": 0.02, "_meeting_cost": True, "session_id": "sess-writer",
        }))
        await pump._process_event(CommonEvent(type=METADATA, data={
            "cost_usd": 0.03, "_meeting_cost": True, "session_id": "",
        }))
        frames = _metadata_frames(q)
        # The internal routing keys never reach the frame or the row.
        assert frames == [
            {"type": "metadata", "cost_usd": 0.02, "cost_billed": False},
            {"type": "metadata", "cost_usd": 0.03, "cost_billed": True},
        ]
        assert pump._total_cost_delta == pytest.approx(0.05)
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_lookup_runs_off_the_loop_with_the_real_store(temp_db, loop_db_guard):
    """The real lookup (binding + subscription row) is a store read; the pump
    must take it through run_db, never on the loop thread. A REAL local-model
    binding must come back False — a swallowed guard violation would fall
    back to True and pass a weaker assertion."""
    from storage.billing import subscription_store as store
    temp_db.create_chat("cb7", "user-admin", "a1")
    sub = store.add_subscription(
        layer="direct-llm", provider="ollama", auth_type="local_endpoint",
        owner_sub="user-admin", label="local",
        credential_data={"endpoint_url": "http://localhost:11434"},
    )
    subscription_pool.bind_session("sess-cb7", sub["id"], layer="direct-llm", user_sub="user-admin")
    pump = _mk_pump("cb7")
    try:
        q = pump.attach()
        with loop_db_guard.active():
            await pump._process_event(CommonEvent(type=METADATA, data={
                "cost_usd": 0.0, "cost_is_delta": True, "input_tokens": 12,
            }))
        assert _metadata_frames(q)[0]["cost_billed"] is False
        assert pump._turn_blocks[-1]["cost_billed"] is False
    finally:
        pump.producer.cancel()
        subscription_pool.release_subscription("sess-cb7")


@pytest.mark.asyncio
async def test_mcp_fee_frame_is_billed(temp_db, monkeypatch):
    """A per-tool MCP fee is money whatever the LLM runs on: the mcp_cost
    frame says so, so the gauge shows it even on a subscription chat."""
    from core.events.common_events import TOOL_RESULT, TOOL_USE
    from services.mcp import mcp_cost_engine

    temp_db.create_chat("cb8", "user-admin", "a1")
    _bill(monkeypatch, {"sess-cb8": False})
    monkeypatch.setattr(
        mcp_cost_engine, "find_costs_block_for_tool",
        lambda name: ("image-gen-mcp", "generate_image", object()),
    )
    monkeypatch.setattr(
        mcp_cost_engine, "evaluate",
        lambda *a, **k: mcp_cost_engine.CostHit(
            amount=0.04, provider="openai", model="gpt-image-1", currency="USD"),
    )
    pump = _mk_pump("cb8")
    try:
        q = pump.attach()
        tool = {"name": "mcp__image-gen-mcp__generate_image", "tool_id": "t1"}
        await pump._process_event(CommonEvent(type=TOOL_USE, data=dict(tool)))
        await pump._process_event(CommonEvent(type=TOOL_RESULT, data=dict(tool)))
        fees = [
            f["event"] for f in _drain(q)
            if f.get("pump_type") == "ws_event" and f["event"].get("type") == "mcp_cost"
        ]
        assert len(fees) == 1
        assert fees[0]["cost_usd"] == 0.04 and fees[0]["cost_billed"] is True
        assert pump._total_cost_delta == pytest.approx(0.04)
        assert pump._mcp_cost_by_key == {("openai", "gpt-image-1"): 0.04}
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_lookup_failure_shows_the_cost(temp_db, monkeypatch):
    temp_db.create_chat("cb6", "user-admin", "a1")

    def _boom(_sid):
        raise RuntimeError("store down")

    monkeypatch.setattr(subscription_pool, "session_cost_billed", _boom)
    pump = _mk_pump("cb6")
    try:
        q = pump.attach()
        await pump._process_event(CommonEvent(type=METADATA, data={
            "cost_usd": 0.01, "cost_is_delta": True,
        }))
        assert _metadata_frames(q)[0]["cost_billed"] is True
    finally:
        pump.producer.cancel()
