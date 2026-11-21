"""A socket that left a chat shows nothing of it.

The page leaves a chat two ways: it opens another chat (``resume_chat``) or
its new-chat view (``leave_chat``). From then on nothing of the chat it
left reaches the socket: not the rest of its live stream, not its end, not
its result. A frame of a chat sent outside a pump's stream names its chat,
so the page can hold the same rule.

Run: cd proxy && venv/bin/pytest tests/session/test_ws_dashboard_left_chat.py -v
"""

from __future__ import annotations

import asyncio

from core.events import stream_pump
from core.session import session_state
from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
    sync_dispatch,
)
from tests.session.test_ws_dashboard_resume_controls import (
    _frames_until_pong,
    _make_chat,
)
from ws import wire_events as wire


def _live_pump(chat_id: str) -> stream_pump.ChatStreamPump:
    loop = asyncio.get_running_loop()
    pump = stream_pump.ChatStreamPump(chat_id=chat_id, session_id=f"sess-{chat_id}",
                                      producer=loop.create_task(asyncio.sleep(3600)),
                                      event_queue=asyncio.Queue(), perm_queue=None)
    stream_pump._active_pumps[chat_id] = pump
    session_state._chat_streaming_state[chat_id] = {
        "streaming": True, "session_id": pump.session_id, "live_blocks": []}
    return pump


async def _attached(pump: stream_pump.ChatStreamPump) -> None:
    for _ in range(500):
        if pump._ws_queues:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the socket never attached to the pump")


def _late_frames(pump: stream_pump.ChatStreamPump) -> None:
    """What the left chat's turn still says after the page left it: a line
    of text, then its end."""
    for q in list(pump._ws_queues):
        q.put_nowait({"pump_type": wire.PUMP_WS_EVENT,
                      "event": {"type": wire.TEXT, "content": "the left chat's words"}})
        q.put_nowait({"pump_type": wire.PUMP_ALL_DONE})


def _names(frames: list[dict], chat_id: str) -> list[dict]:
    return [f for f in frames if f.get("chat_id") == chat_id
            or "the left chat's words" in str(f) or "worker's report" in str(f)]


def _result_frame(chat_id: str | None = None) -> dict:
    frame = {"type": wire.DELEGATE_RESULT, "task_id": "t-1", "task_name": "lane",
             "agent": "worker", "output_text": "the worker's report", "status": "completed"}
    if chat_id:
        frame["chat_id"] = chat_id
    return frame


def _cleanup(*chat_ids: str) -> None:
    for cid in chat_ids:
        pump = stream_pump._active_pumps.pop(cid, None)
        if pump is not None:
            pump.producer.cancel()
        session_state._chat_streaming_state.pop(cid, None)


def test_a_socket_that_left_a_streaming_chat_for_a_new_chat_gets_none_of_it(temp_db, monkeypatch):
    """The page watched chat A's live turn, then pressed "+ New Chat": the
    socket detaches from A's stream, and A's later words and its end never
    reach it."""
    stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
    slug = make_test_agent()
    cid = _make_chat(slug, messages=(("user", "go"),))

    async def scenario():
        try:
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                pump = _live_pump(cid)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _attached(pump)
                ws.client_send({"type": "leave_chat"})
                await _frames_until_pong(ws)
                assert not pump._ws_queues, "the socket still follows the left chat's stream"
                _late_frames(pump)
                late = await _frames_until_pong(ws)
                assert _names(late, cid) == []
        finally:
            _cleanup(cid)
    run_ws_scenario(scenario)


def test_a_socket_that_moved_to_another_chat_gets_none_of_the_left_one(temp_db, monkeypatch):
    """The same for a move to chat B (``resume_chat``): A's later words and
    its end stay with A."""
    stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
    slug = make_test_agent()
    cid_a = _make_chat(slug, messages=(("user", "go"),))
    cid_b = _make_chat(slug, messages=(("user", "hi"), ("assistant", "hello")))

    async def scenario():
        try:
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                pump = _live_pump(cid_a)
                ws.client_send({"type": "resume_chat", "chat_id": cid_a})
                await _attached(pump)
                ws.client_send({"type": "resume_chat", "chat_id": cid_b})
                await _frames_until_pong(ws)
                assert not pump._ws_queues
                _late_frames(pump)
                late = await _frames_until_pong(ws)
                assert _names(late, cid_a) == []
        finally:
            _cleanup(cid_a, cid_b)
    run_ws_scenario(scenario)


def test_a_chats_result_reaches_its_own_view_named_and_no_other(temp_db, monkeypatch):
    """A result's frame broadcast for chat A (the delivery's badge frame,
    which names no chat): the socket on A sends it naming A. After the page
    left A for its new-chat view, the same broadcast sends nothing."""
    stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
    slug = make_test_agent()
    cid = _make_chat(slug, messages=(("user", "go"), ("assistant", "delegated")))

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            ws.client_send({"type": "resume_chat", "chat_id": cid})
            await _frames_until_pong(ws)
            session_state.broadcast_chat_frame(cid, _result_frame())
            on_a = await _frames_until_pong(ws)
            assert [f for f in on_a if f["type"] == wire.DELEGATE_RESULT] == [
                {**_result_frame(), "chat_id": cid}]
            ws.client_send({"type": "leave_chat"})
            await sync_dispatch(ws)
            session_state.broadcast_chat_frame(cid, _result_frame())
            left = await _frames_until_pong(ws)
            assert _names(left, cid) == []
    run_ws_scenario(scenario)


def _words_frames() -> frozenset[str]:
    """The per-chat frames that carry a chat's words: the ones whose block
    is stored, the text and the live state. The page drops one that names
    no chat (``frameIsForShownChat`` in the dashboard)."""
    return frozenset(n for n, f in wire.FRAMES.items()
                     if f.per_chat and (f.persisted or n in (wire.TEXT, wire.LIVE_STATE)))


def test_every_frame_of_a_chats_words_the_socket_sends_names_its_chat():
    """A source scan of the dashboard socket's modules: a ``_send`` of a dict
    whose type is a words frame names ``chat_id`` in the same dict. A pump's
    frames are named by the stream loop; this holds every other send."""
    import ast
    import pathlib

    names = {k: v for k, v in vars(wire).items() if isinstance(v, str)}
    words = _words_frames()
    missing: list[str] = []
    for path in sorted(pathlib.Path(wire.__file__).parent.glob("dashboard*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_send" and node.args
                    and isinstance(node.args[0], ast.Dict)):
                continue
            keys = {k.value for k in node.args[0].keys if isinstance(k, ast.Constant)}
            kind = None
            for k, v in zip(node.args[0].keys, node.args[0].values, strict=True):
                if (isinstance(k, ast.Constant) and k.value == "type"
                        and isinstance(v, ast.Attribute)):
                    kind = names.get(v.attr)
            if kind in words and "chat_id" not in keys:
                missing.append(f"{path.name}:{node.lineno} {kind}")
    assert missing == [], f"word frames sent with no chat named: {missing}"
