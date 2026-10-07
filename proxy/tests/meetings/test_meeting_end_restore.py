"""A meeting's own end never revives its indicator through the post-turn
re-send.

The orchestrator writes ``concluded`` only after the meeting's pump task
has ended and the participants' sessions are closed (the ``finally`` after
``await pump._task`` in services/meetings/meeting_orchestrator.py), while
the dashboard re-sends the chat's history as soon as the pump's
``all_done`` arrives (ws/dashboard_chat_stream.py, the post-turn branch).
The row is still live at that moment, so a restore read from the row alone
brought the meeting back as active right after ``meeting_concluded`` had
cleared the indicator. The restore reads the end from the chat itself: the
pump persists the ``meeting_concluded`` row before ``all_done``.
"""

import asyncio
import json
import uuid

from core.events import stream_pump
from core.events.common_events import PRODUCER_DONE, SYSTEM, CommonEvent
from core.session import session_kind
from storage import database as task_store
from storage.chat import meeting_status
from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer, dashboard_connection, drain_startup, make_test_agent,
    run_ws_scenario, session_cookie, stub_dashboard_seams,
)
import ws.dashboard  # noqa: F401  (assembles the mixins first)
from ws import wire_events as wire
from ws.dashboard import _build_chat_restore


async def _until(ws, ftype: str, *, limit: int = 16) -> dict:
    seen = []
    for _ in range(limit):
        frame = await ws.next_frame()
        if frame.get("type") == ftype:
            return frame
        seen.append(frame.get("type"))
    raise AssertionError(f"no {ftype!r} frame; saw {seen}")


def _live_meeting(chat_id: str, slug: str) -> str:
    mid = f"mtg-end-{uuid.uuid4().hex[:8]}"
    task_store.create_meeting(mid, "topic", json.dumps([slug]), slug, "directed", 10,
                              chat_id, None, None, "user", "user-admin")
    task_store.update_meeting(mid, status=meeting_status.ACTIVE)
    return mid


def test_the_post_turn_resend_of_a_finished_meeting_reports_no_meeting(temp_db, monkeypatch):
    stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
    slug = make_test_agent()
    cid = f"chat-mtg-end-{uuid.uuid4().hex[:8]}"
    task_store.create_chat(cid, "user-admin", slug)
    mid = _live_meeting(cid, slug)

    async def scenario():
        eq: asyncio.Queue = asyncio.Queue()
        ends = asyncio.Event()

        async def _produce():
            await ends.wait()
            await eq.put(CommonEvent(type=SYSTEM, data={
                "subtype": wire.SUBTYPE_MEETING_CONCLUDED, "meeting_id": mid,
                "total_turns": 2, "cost_usd": 0.0,
            }))
            await eq.put(CommonEvent(type=PRODUCER_DONE, data={}))

        try:
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                pump = stream_pump.ChatStreamPump(
                    chat_id=cid, session_id=f"mtg-{mid}",
                    producer=asyncio.create_task(_produce()), event_queue=eq, perm_queue=None,
                    source_type=session_kind.MEETING.source_type,
                )
                stream_pump._active_pumps[cid] = pump
                pump.start()
                ws.client_send({"type": "client_info", "platform": "web", "history_deltas": True})
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                full = await _until(ws, "chat_history")
                assert full["restore"]["meeting"]["active"] is True
                await _until(ws, "live_state")
                ends.set()
                await _until(ws, "done")
                resend = await _until(ws, "chat_history_delta")
                # The orchestrator has not written the end yet: the row is live.
                assert task_store.get_active_meeting_for_chat(cid) is not None
                assert resend["restore"]["meeting"] is None
                # The orchestrator's own write ends the loop's wait for a next pump.
                task_store.update_meeting(mid, status=meeting_status.CONCLUDED)
                ws.client_send({"type": "close"})
        finally:
            stream_pump._active_pumps.pop(cid, None)
            stream_pump._chat_streaming_state.pop(cid, None)
    run_ws_scenario(scenario)


def test_a_live_meeting_with_no_end_in_the_chat_stays_active(temp_db):
    slug = make_test_agent()
    cid = f"chat-mtg-live-{uuid.uuid4().hex[:8]}"
    task_store.create_chat(cid, "user-admin", slug)
    mid = _live_meeting(cid, slug)
    assert _build_chat_restore(cid)["meeting"]["active"] is True
    # Another meeting's end in the same chat does not end this one.
    task_store.add_chat_message(cid, "event", "", event_type="system", event_data=json.dumps({
        "type": "system", "subtype": wire.SUBTYPE_MEETING_CONCLUDED, "meeting_id": "mtg-older",
    }))
    assert _build_chat_restore(cid)["meeting"]["active"] is True
    task_store.add_chat_message(cid, "event", "", event_type="system", event_data=json.dumps({
        "type": "system", "subtype": wire.SUBTYPE_MEETING_CONCLUDED, "meeting_id": mid,
    }))
    assert _build_chat_restore(cid)["meeting"] is None
