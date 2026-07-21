"""Perm-queue routing on the pump: blocking prompt types must never drop.

Codex ``request_user_input`` holds the daemon turn open until the dashboard
answers, so a ``question_prompt`` that never reaches the blocking-prompt gate
hangs the turn forever (no card, no turn-end, mid-turn messages undeliverable
until Stop). Regression for the missing ``_handle_perm_event`` branch.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_pump_perm_routing.py -q
"""

import asyncio

import pytest

from core.events import stream_pump
from core.events.stream_pump import ChatStreamPump, _pending_permissions


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )
    pump._ws_queue = asyncio.Queue()
    return pump


def _question_item(request_id: str) -> dict:
    return {
        "event_type": "question_prompt",
        "request_id": request_id,
        "tool_name": "request_user_input",
        "tool_input": {"questions": [{"question": "Delete the keypair file?"}]},
    }


@pytest.mark.asyncio
async def test_question_prompt_reaches_blocking_gate(temp_db, monkeypatch):
    temp_db.create_chat("qp1", "user-admin", "a1")
    fired: list[dict] = []

    async def _fake_ephemeral(*a, **kw):
        fired.append(kw)

    monkeypatch.setattr(
        stream_pump.notification_manager, "fire_ephemeral", _fake_ephemeral
    )
    pump = _mk_pump("qp1")
    try:
        await pump._handle_perm_event(_question_item("req-q1"))
        # The question is the ACTIVE blocking prompt, stored for reconnect
        # replay, and forwarded as the question card frame.
        assert pump._permission_active["request_id"] == "req-q1"
        assert _pending_permissions[pump.session_id]["request_id"] == "req-q1"
        frame = pump._ws_queue.get_nowait()
        assert frame["pump_type"] == "perm_question_prompt"
        assert frame["perm_data"]["request_id"] == "req-q1"
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_question_prompt_respects_one_prompt_gate(temp_db, monkeypatch):
    temp_db.create_chat("qp2", "user-admin", "a1")

    async def _fake_ephemeral(*a, **kw):
        pass

    monkeypatch.setattr(
        stream_pump.notification_manager, "fire_ephemeral", _fake_ephemeral
    )
    pump = _mk_pump("qp2")
    try:
        await pump._handle_perm_event(
            {"event_type": "permission_prompt", "request_id": "req-p1",
             "tool_name": "Bash", "tool_input": {"command": "rm x"}}
        )
        await pump._handle_perm_event(_question_item("req-q2"))
        # Permission stays active; the question buffers behind it...
        assert pump._permission_active["request_id"] == "req-p1"
        assert [p["request_id"] for p in pump._permission_buffer] == ["req-q2"]
        # ...and advances into the active slot once the permission resolves.
        await pump.resolve_active_permission()
        assert pump._permission_active["request_id"] == "req-q2"
        assert _pending_permissions[pump.session_id]["request_id"] == "req-q2"
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()
