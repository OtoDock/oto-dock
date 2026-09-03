"""Early LLM-title triggers — the pump's tool-count fire and the interactive
funnel's batch-counter/timer fires.

The headless pump fires at _TITLE_CHAR_THRESHOLD chars of first-response text
OR _TITLE_TOOL_THRESHOLD tool calls (subagent spawns count) OR PRODUCER_DONE;
interactive sessions accumulate the tailers' per-batch counters
(assistant_chars / tool_rows) and add a one-shot ~30s latency-cap timer armed
at the first turn-open of an untitled chat. All paths funnel into
request_chat_title, whose atomic claim owns exactly-once.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_title_early_triggers.py -q
"""

import asyncio

import pytest

from core.events.common_events import TOOL_USE, CommonEvent
from core.events.stream_pump import ChatStreamPump, _TITLE_TOOL_THRESHOLD
from core.session.interactive_session import InteractiveSession

pytestmark = pytest.mark.asyncio

_title_calls: list[tuple[str, str]] = []


@pytest.fixture(autouse=True)
def _patch_title_request(monkeypatch):
    _title_calls.clear()

    async def _fake_request(chat_id, *, assistant_excerpt=""):
        _title_calls.append((chat_id, assistant_excerpt))
    from services import title_generator as tg
    monkeypatch.setattr(tg, "request_chat_title", _fake_request)


async def _drain_tasks():
    for _ in range(10):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Pump: the tool-count trigger
# ---------------------------------------------------------------------------

def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
        event_queue=asyncio.Queue(), perm_queue=None,
    )


def _tool_event(name="Bash", i=0) -> CommonEvent:
    return CommonEvent(type=TOOL_USE,
                       data={"name": name, "tool_id": f"tool-{i}"})


async def test_pump_fires_at_tool_threshold(temp_db):
    temp_db.create_chat("tp1", "user-1", "agent-x")
    pump = _mk_pump("tp1")
    try:
        for i in range(_TITLE_TOOL_THRESHOLD - 1):
            await pump._process_event(_tool_event(i=i))
        assert _title_calls == []  # below threshold
        await pump._process_event(_tool_event(i=99))
        await _drain_tasks()
        assert [c[0] for c in _title_calls] == ["tp1"]
        assert pump._title_armed is False  # disarmed — no double fire
        await pump._process_event(_tool_event(i=100))
        await _drain_tasks()
        assert len(_title_calls) == 1
    finally:
        pump.producer.cancel()


async def test_pump_spawns_count_toward_threshold(temp_db):
    # Agent/Task spawns are in _SKIP_TOOL_PERSIST but still signal a long
    # working turn — counted at branch top, before the persist skip.
    temp_db.create_chat("tp2", "user-1", "agent-x")
    pump = _mk_pump("tp2")
    try:
        for i in range(_TITLE_TOOL_THRESHOLD):
            await pump._process_event(_tool_event(name="Task", i=i))
        await _drain_tasks()
        assert [c[0] for c in _title_calls] == ["tp2"]
    finally:
        pump.producer.cancel()


async def test_pump_titled_chat_never_arms(temp_db):
    temp_db.create_chat("tp3", "user-1", "agent-x")
    temp_db.update_chat("tp3", title_generated=True)
    pump = _mk_pump("tp3")
    try:
        assert pump._title_armed is False
        for i in range(_TITLE_TOOL_THRESHOLD + 1):
            await pump._process_event(_tool_event(i=i))
        await _drain_tasks()
        assert _title_calls == []
    finally:
        pump.producer.cancel()


# ---------------------------------------------------------------------------
# Interactive: batch-counter + timer fires
# ---------------------------------------------------------------------------

def _mk_session(*, armed=True, chat_id="chat-i1") -> InteractiveSession:
    s = InteractiveSession.__new__(InteractiveSession)
    s.session_id = "sid-early001"
    s.chat_id = chat_id
    s.agent_name = "researcher"
    s.user_sub = "user-1"
    s._loop = asyncio.get_running_loop()
    s._closing = False
    s._closed = False
    s.target = "local"
    s._midturn_tail_handle = None
    s._midturn_tail_task = None
    s._title_armed = armed
    s._title_chars = 0
    s._title_tools = 0
    s._title_timer = None
    s._title_timer_spent = False
    return s


async def test_batch_chars_threshold_fires_once():
    s = _mk_session()
    s._maybe_fire_title_early({"assistant_chars": 150, "tool_rows": 0})
    assert _title_calls == []
    s._maybe_fire_title_early({"assistant_chars": 150, "tool_rows": 0})
    await _drain_tasks()
    assert [c[0] for c in _title_calls] == ["chat-i1"]
    assert s._title_armed is False
    s._maybe_fire_title_early({"assistant_chars": 500, "tool_rows": 9})
    await _drain_tasks()
    assert len(_title_calls) == 1  # disarmed — accumulation stopped


async def test_batch_tool_rows_threshold_fires():
    s = _mk_session()
    s._maybe_fire_title_early({"assistant_chars": 0, "tool_rows": 3})
    s._maybe_fire_title_early({"assistant_chars": 0, "tool_rows": 2})
    await _drain_tasks()
    assert [c[0] for c in _title_calls] == ["chat-i1"]


async def test_timer_task_forces_tail_then_fires():
    s = _mk_session()
    tails = []

    async def _fake_tail():
        tails.append(True)
    s._tail_and_maybe_complete = _fake_tail  # type: ignore[method-assign]
    await s._title_timer_task()
    await _drain_tasks()
    assert tails == [True]  # full tail pass (signals apply) before the fire
    assert [c[0] for c in _title_calls] == ["chat-i1"]


async def test_timer_noops_when_disarmed_or_closing():
    s = _mk_session(armed=False)
    called = []

    async def _fake_tail():
        called.append(True)
    s._tail_and_maybe_complete = _fake_tail  # type: ignore[method-assign]
    await s._title_timer_task()
    s2 = _mk_session()
    s2._closing = True
    s2._tail_and_maybe_complete = _fake_tail  # type: ignore[method-assign]
    await s2._title_timer_task()
    await _drain_tasks()
    assert called == [] and _title_calls == []


async def test_turn_open_arms_timer_exactly_once(monkeypatch):
    # An interrupted first turn leaves _title_armed set; the second turn's
    # open transition must NOT re-arm the timer.
    from services.notifications import notification_manager
    monkeypatch.setattr(notification_manager, "broadcast_chat_status",
                        lambda *a, **k: None)
    s = _mk_session()
    s._turn_open = False
    s._question_parked = False
    s._chat_owner_sub = "user-1"
    s._cold_flush_pending = False
    monkeypatch.setattr(s, "_clear_cold_flush", lambda: None, raising=False)
    monkeypatch.setattr(s, "_turn_end_effects", lambda: None, raising=False)
    s._set_turn_open(True)
    first = s._title_timer
    assert first is not None and s._title_timer_spent is True
    first.cancel()
    s._title_timer = None
    s._set_turn_open(False)
    s._set_turn_open(True)
    assert s._title_timer is None  # once-only across the session
