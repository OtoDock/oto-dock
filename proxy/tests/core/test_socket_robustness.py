"""The sockets end quietly when their peer is gone, and the backgrounded
warmup keeps its bookkeeping when a phase fails: a failed seed claim is a
failed warmup that still unregisters, and a failure after a Stop never kicks
the stopped turn (``ws/socket_gone.py``, ``ws/dashboard.py``,
``ws/satellite.py``, ``ws/dashboard_warmup.py``)."""

import asyncio
import threading
from unittest.mock import AsyncMock

import psycopg
import pytest
from uvicorn.protocols.utils import ClientDisconnected

from ws import socket_gone

_CLOSED_TRANSPORT = RuntimeError(
    "unable to perform operation on <TCPTransport closed=True reading=False>; "
    "the handler is closed")


def test_the_closed_socket_errors_are_told_by_their_message():
    assert socket_gone.closed_socket_error(_CLOSED_TRANSPORT)
    assert socket_gone.closed_socket_error(
        RuntimeError('WebSocket is not connected. Need to call "accept" first.'))
    assert not socket_gone.closed_socket_error(RuntimeError("dictionary changed size"))
    assert not socket_gone.closed_socket_error(ValueError("not connected"))


class _Ws:
    """A socket whose ``accept`` or first read raises what a dying peer does."""

    def __init__(self, *, accept=None, receive=None, frame=None):
        self._accept, self._receive, self._frame = accept, receive, frame
        self.sent: list[str] = []
        self.closed: list[int] = []
        self.scope = {"type": "websocket", "client": ("10.0.0.9", 5000), "headers": []}
        self.client = ("10.0.0.9", 5000)

    async def accept(self, *a, **k):
        if self._accept is not None:
            raise self._accept

    async def receive_text(self):
        if self._receive is not None:
            raise self._receive
        if self._frame is not None:
            return self._frame
        return '{"type": "auth", "machine_id": "m1deadbeef", "machine_secret": "s", ' \
               '"capabilities": {}, "satellite_version": "0.5.150"}'

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000, reason=""):
        self.closed.append(code)


# --- the dashboard socket ---------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [ClientDisconnected(), _CLOSED_TRANSPORT])
async def test_a_dashboard_page_gone_during_the_handshake_ends_quietly(monkeypatch, exc):
    from services.notifications import notification_manager
    from storage import database as task_store
    from ws import dashboard as dash
    monkeypatch.setattr(task_store, "get_user_agent_roles", lambda sub: {})
    registered = []
    monkeypatch.setattr(notification_manager, "register_user_connection",
                        lambda *a, **k: registered.append(a))
    conn = dash.DashboardConnection(_Ws(accept=exc), user_sub="user-a",
                                    user={"sub": "user-a", "role": "member"})
    await conn.run()  # returns; nothing raises past the handler
    assert registered == []


# --- the satellite socket's auth phase ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("ws", [
    _Ws(accept=_CLOSED_TRANSPORT), _Ws(accept=ClientDisconnected()),
    _Ws(receive=ClientDisconnected()), _Ws(receive=_CLOSED_TRANSPORT),
], ids=["accept-closed-transport", "accept-client-gone", "read-client-gone",
        "read-closed-transport"])
async def test_a_satellite_gone_during_the_auth_phase_ends_quietly(ws):
    from ws import satellite
    freed = []
    await satellite._ws_satellite_handler(ws, lambda: freed.append(1))
    assert ws.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ["[" * 20000, "{not json"], ids=["deeply-nested", "not-json"])
async def test_a_first_frame_that_does_not_parse_is_refused_not_read_as_gone(frame):
    """A frame under the size cap whose parse raises (a RecursionError is a
    RuntimeError, as a closed socket's error is) is refused with 4001."""
    from ws import satellite
    ws = _Ws(frame=frame)
    await satellite._ws_satellite_handler(ws, lambda: None)
    assert ws.closed == [4001]
    assert '"status": "rejected"' in ws.sent[0]


@pytest.mark.asyncio
async def test_a_store_that_cannot_answer_tells_the_machine_to_come_back(monkeypatch):
    from storage import remote_store
    from ws import satellite

    def down(machine_id):
        raise psycopg.OperationalError("server closed the connection")
    monkeypatch.setattr(remote_store, "get_remote_machine", down)
    ws = _Ws()
    await satellite._ws_satellite_handler(ws, lambda: None)
    assert ws.closed == [1013]
    assert '"status": "rejected"' in ws.sent[0] and "uninstall" not in ws.sent[0]


@pytest.mark.asyncio
async def test_another_fault_in_the_auth_phase_still_surfaces(monkeypatch):
    from storage import remote_store
    from ws import satellite

    def bug(machine_id):
        raise KeyError("a bug")
    monkeypatch.setattr(remote_store, "get_remote_machine", bug)
    with pytest.raises(KeyError):
        await satellite._ws_satellite_handler(_Ws(), lambda: None)


# --- the backgrounded warmup ----------------------------------------------------

def _tail_conn(monkeypatch):
    from ws import dashboard as dash, dashboard_warmup
    conn = object.__new__(dash.DashboardConnection)
    conn._attached_warmups = {"chat-w"}
    conn._warmup_chat_id = "chat-w"
    calls: list = []
    conn._tail_failed = AsyncMock(side_effect=lambda w, run, kind, e: calls.append(("failed", kind)))
    conn._tail_first_turn = AsyncMock(side_effect=lambda w, run: calls.append(("first_turn",)))
    conn._tail_adopt = AsyncMock(side_effect=lambda w, run: calls.append(("adopt",)))
    conn._return_warmup_sends = AsyncMock(side_effect=lambda cid, reason: calls.append(("returned", reason)))
    unregistered = []

    async def _unregister(cid):
        unregistered.append(cid)
    monkeypatch.setattr(dashboard_warmup.warmup_registry, "unregister", _unregister)
    return conn, calls, unregistered


async def _args():
    from ws.dashboard_warmup_tail import _TailArgs
    stop = asyncio.Event()

    async def _beat():
        await stop.wait()
    hb = asyncio.create_task(_beat())
    return _TailArgs(
        wcid="chat-w", w_agent="a", w_layer=None, w_pinned="", w_thread="", w_role="",
        w_target_model="", w_exec_mode="", w_theme="", msg={"text": "hello"},
        permission_mode="default", chat_model="", effective_exec_path="",
        old_session_id=None, pw_task=None, heartbeat_stop=stop, hb_task=hb)


@pytest.mark.asyncio
async def test_a_failed_seed_claim_is_a_failed_warmup_that_still_unregisters(monkeypatch):
    from ws.dashboard_warmup_tail import _TailRun
    conn, calls, unregistered = _tail_conn(monkeypatch)
    conn._tail_claim_seed = AsyncMock(side_effect=psycopg.OperationalError("down"))
    conn._tail_start = AsyncMock()
    w = await _args()
    await conn._spawn_tail(w, _TailRun())
    assert ("failed", "spawn") in calls and ("first_turn",) not in calls
    assert ("returned", "turn_failed") in calls
    assert w.heartbeat_stop.is_set() and w.hb_task.done()
    assert unregistered == ["chat-w"] and conn._attached_warmups == set()
    assert conn._warmup_chat_id is None


@pytest.mark.asyncio
async def test_a_failure_after_a_stop_never_kicks_the_stopped_turn(monkeypatch):
    from core.events import input_queue
    from ws.dashboard_warmup_tail import _TailRun
    conn, calls, unregistered = _tail_conn(monkeypatch)
    released = []
    monkeypatch.setattr(input_queue, "first_turn_not_run", released.append)
    conn._tail_claim_seed = AsyncMock()

    async def _start(w, run):
        run.res = type("R", (), {"interactive": False})()
    conn._tail_start = _start

    async def _stopped_then_fails(w, run):
        run.aborted = True
        raise RuntimeError("the row stamp failed")
    conn._tail_abort_check = _stopped_then_fails
    w = await _args()
    await conn._spawn_tail(w, _TailRun())
    assert ("first_turn",) not in calls
    assert unregistered == ["chat-w"]
    assert released == ["chat-w"]  # what a teammate sent is not stranded


@pytest.mark.asyncio
async def test_the_abort_stamp_lands_before_the_queue_is_released(monkeypatch):
    """The stamp rides the chat's writer lane, off the loop, and is awaited:
    a message the release lets through starts after the row says aborted."""
    from core.events import input_queue
    from storage import database as task_store
    from ws.dashboard_warmup_tail import _TailRun
    conn, _calls, _ = _tail_conn(monkeypatch)
    conn._warmup_abort_chat = "chat-w"
    events: list = []
    monkeypatch.setattr(task_store, "update_chat",
                        lambda chat_id, **kw: events.append(("stamp", threading.get_ident())))
    monkeypatch.setattr(input_queue, "first_turn_not_run", lambda cid: events.append(("release",)))
    run = _TailRun()
    run.res = type("R", (), {"layer": AsyncMock(), "session_id": "sid-w"})()
    w = await _args()
    await conn._tail_abort_check(w, run)
    assert [e[0] for e in events] == ["stamp", "release"]
    assert events[0][1] != threading.get_ident()
    w.heartbeat_stop.set()
    await w.hb_task


@pytest.mark.asyncio
async def test_a_stop_whose_row_stamp_fails_still_releases_the_queue(monkeypatch):
    from core.events import input_queue
    from storage import database as task_store
    from ws.dashboard_warmup_tail import _TailRun
    conn, _calls, _ = _tail_conn(monkeypatch)
    conn._warmup_abort_chat = "chat-w"
    released = []
    monkeypatch.setattr(input_queue, "first_turn_not_run", released.append)

    def _stamp_fails(chat_id, **kw):
        raise psycopg.OperationalError("down")
    monkeypatch.setattr(task_store, "update_chat", _stamp_fails)
    layer = AsyncMock()
    run = _TailRun()
    run.res = type("R", (), {"layer": layer, "session_id": "sid-w"})()
    w = await _args()
    await conn._tail_abort_check(w, run)
    assert run.aborted and released == ["chat-w"]
    layer.abort.assert_awaited_once_with("sid-w")
    w.heartbeat_stop.set()
    await w.hb_task
