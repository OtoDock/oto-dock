"""Notify-drain staleness guard for ``chat_status`` forwards.

The per-user notify queue is only drained BETWEEN the viewed chat's turns, so
a turn-START ``streaming`` broadcast can sit queued for the whole turn and
land right after ``done`` — where the frontend's auto-attach reads it as a
fresh turn and pointlessly re-resumes the chat (a full-history reload flash
at every turn end). The drain must forward a ``streaming`` status only while
the chat still has a live turn (an active pump, or an interactive session
with an open turn); ``ready`` always forwards.
"""

import asyncio

import pytest

from core.events.stream_pump import ChatStreamPump, _active_pumps
from ws.dashboard_server_events import ServerNotificationController


class _Conn(ServerNotificationController):
    """Bare mixin host — the chat_status branch only touches ``self._send``."""

    def __init__(self):
        self.sent: list[dict] = []

    async def _send(self, data: dict):
        self.sent.append(data)


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )


def _status(chat_id: str, status: str) -> dict:
    return {"type": "chat_status", "chat_id": chat_id, "status": status}


@pytest.mark.asyncio
async def test_stale_streaming_status_is_dropped(temp_db):
    conn = _Conn()
    await conn._handle_server_notification(_status("sc-none", "streaming"))
    assert conn.sent == []  # no pump, no interactive turn — stale, dropped


@pytest.mark.asyncio
async def test_live_pump_streaming_status_forwards(temp_db):
    temp_db.create_chat("sc-live", "user-admin", "a1")
    pump = _mk_pump("sc-live")
    _active_pumps["sc-live"] = pump
    try:
        conn = _Conn()
        await conn._handle_server_notification(_status("sc-live", "streaming"))
        assert conn.sent == [_status("sc-live", "streaming")]
    finally:
        _active_pumps.pop("sc-live", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_done_pump_streaming_status_is_dropped(temp_db):
    temp_db.create_chat("sc-done", "user-admin", "a1")
    pump = _mk_pump("sc-done")
    pump._done = True
    _active_pumps["sc-done"] = pump
    try:
        conn = _Conn()
        await conn._handle_server_notification(_status("sc-done", "streaming"))
        assert conn.sent == []
    finally:
        _active_pumps.pop("sc-done", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_interactive_open_turn_streaming_forwards(temp_db, monkeypatch):
    class _Isess:
        turn_open = True

    from core.session import interactive_session

    monkeypatch.setattr(
        interactive_session, "find_live_for_chat", lambda cid: _Isess()
    )
    conn = _Conn()
    await conn._handle_server_notification(_status("sc-pty", "streaming"))
    assert conn.sent == [_status("sc-pty", "streaming")]


@pytest.mark.asyncio
async def test_ready_status_always_forwards(temp_db):
    conn = _Conn()
    await conn._handle_server_notification(_status("sc-ready", "ready"))
    assert conn.sent == [_status("sc-ready", "ready")]


@pytest.mark.asyncio
async def test_a_delegate_result_keeps_its_typed_ending(temp_db):
    """The dashboard-socket rung of a delegate result: the persisted row and
    the frame carry the worker's typed ending, as the other rungs do."""
    import json
    temp_db.create_chat("sc-deleg", "user-admin", "a1")

    class _Viewer(_Conn):
        chat_id = "sc-deleg"
        session_id = "sess-deleg"
        layer = object()

        async def _run_server_turn(self, *a, **k):
            return object()

    conn = _Viewer()
    await conn._handle_server_notification({
        "type": "task_result_prompt", "chat_id": "sc-deleg", "session_id": "sess-deleg",
        "task_id": "t-1", "task_name": "lane", "result_prompt": "p", "delegate_agent": "w",
        "output_text": "⚠ declined", "status": "failed",
        "reason": "declined", "resets_at": "",
    })
    frame = next(f for f in conn.sent if f["type"] == "delegate_result")
    assert frame["reason"] == "declined" and frame["resets_at"] == ""
    rows = [m for m in temp_db.get_chat_messages("sc-deleg") if m.get("event_type") == "delegate_result"]
    assert json.loads(rows[-1]["event_data"])["reason"] == "declined"


@pytest.mark.asyncio
async def test_a_delegate_result_whose_turn_cannot_start_is_stored_as_the_socket_person(temp_db):
    """No turn ran for the result: the prompt is stored as the chat's wake
    for the person this socket's turn runs as, at their role on the agent,
    off the loop."""
    from storage.identity import db_users
    temp_db.create_chat("sc-park", "agent::a1", "a1")
    db_users.add_user_agent("user-viewer", "a1", "editor", "user-admin")

    class _Dead(_Conn):
        chat_id = "sc-other"
        session_id = "sess-other"
        user_sub = "user-viewer"

        async def _resolve_layer_for_chat_async(self, cid):
            return object()

        async def _run_server_turn(self, *a, **k):
            return None

    await _Dead()._handle_server_notification({
        "type": "task_result_prompt", "chat_id": "sc-park", "session_id": "sess-park",
        "task_id": "t-2", "task_name": "lane", "result_prompt": "the result",
        "delegate_agent": "w", "output_text": "done", "status": "completed",
    })
    assert temp_db.claim_pending_wake_records("sc-park") == [
        {"prompt": "the result", "person": "user-viewer", "role": "editor", "by": ""}]


@pytest.mark.asyncio
async def test_a_delegate_result_keeps_its_files_and_verdict(temp_db):
    """The dashboard-socket rung rebuilds the row and the frame from the notify
    item through one helper: the worker's files, the skipped ones and the
    failing check's verdict survive it as the typed ending does."""
    import json
    temp_db.create_chat("sc-files", "user-admin", "a1")

    class _Viewer(_Conn):
        chat_id = "sc-files"
        session_id = "sess-files"
        layer = object()

        async def _run_server_turn(self, *a, **k):
            return object()

    files = [{"path": "users/u/workspace/inbox/w/r.md", "bytes": 5}]
    skipped = [{"path": "x", "reason": "symlink"}]
    verdict = {"check": "answer", "round": 1, "summary": "no"}
    conn = _Viewer()
    await conn._handle_server_notification({
        "type": "task_result_prompt", "chat_id": "sc-files", "session_id": "sess-files",
        "task_id": "t-2", "task_name": "lane", "result_prompt": "p", "delegate_agent": "w",
        "output_text": "done", "status": "completed",
        "files": files, "files_skipped": skipped, "verdict": verdict,
    })
    frame = next(f for f in conn.sent if f["type"] == "delegate_result")
    assert frame["files"] == files and frame["files_skipped"] == skipped
    assert frame["verdict"] == verdict and "reason" not in frame
    rows = [m for m in temp_db.get_chat_messages("sc-files") if m.get("event_type") == "delegate_result"]
    data = json.loads(rows[-1]["event_data"])
    assert data["files"] == files and data["verdict"] == verdict
