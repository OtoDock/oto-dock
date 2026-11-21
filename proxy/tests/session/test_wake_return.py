"""A person's message after a wake: the wake already built the chat's own
session, so nothing is closed or rebuilt at their return. A session of the
agent's shared chat that serves another identity (another sender, or a wake
with nobody behind it after the agent left Shared-only) is recycled by the
send's sender check, whatever the agent's mode now.
"""

from __future__ import annotations

import asyncio

import pytest

from core.session import chat_rebuild, visibility
from storage import database as task_store
from ws import dashboard_chat_send
from ws.dashboard_chat_send import ChatSendMixin

CHAT = "chat-wake-1"
SID = "sess-wake-1"


class _Live:
    async def is_session_alive(self, _sid):
        return True

    def is_session_grace_held(self, _sid):
        return False


class _Host(ChatSendMixin):
    def __init__(self, serves: bool):
        self.user_sub = "sub-ana"
        self.agent_name = "pa"
        self.chat_id = CHAT
        self.session_id = SID
        self.layer = _Live()
        self.user = {"username": "ana"}
        self.recycled: list[str] = []
        self._serves = serves

    async def _handle_warmup(self, msg, background=False):
        raise AssertionError("nothing is warmed")

    async def _recycle_session(self, why: str) -> None:
        self.recycled.append(why)

    async def _viewed_session_payer(self) -> str:
        return ""

    def _session_serves_sender(self, sid: str, payer: str, chat: dict) -> bool:
        return self._serves


@pytest.fixture
def chat(monkeypatch):
    row = {"id": CHAT, "session_id": SID, "agent": "pa", "user_sub": "sub-ana",
           "source_type": "chat", "origin": "dashboard"}
    monkeypatch.setattr(task_store, "get_chat", lambda _cid: dict(row))
    monkeypatch.setattr(visibility, "is_shared_only", lambda _a: False)

    async def _no_close(*a, **k):
        raise AssertionError("a person's return closes nothing")
    monkeypatch.setattr(chat_rebuild, "close_chat_session", _no_close)
    return row


@pytest.mark.asyncio
async def test_a_persons_message_on_a_live_session_closes_nothing(chat):
    host = _Host(serves=True)
    assert await host._heal_viewed_session({"text": "hi"}) == dashboard_chat_send.HEAL_READY
    assert host.recycled == [] and host.session_id == SID


@pytest.mark.asyncio
async def test_a_shared_chat_session_built_for_another_is_recycled_after_the_mode_left(chat):
    """The agent left Shared-only: its shared chat's live session, built for
    nobody by a wake, never serves a person's message."""
    chat["user_sub"] = visibility.shared_chat_owner("pa")
    host = _Host(serves=False)
    await host._heal_viewed_session({"text": "hi"})
    assert len(host.recycled) == 1


@pytest.mark.asyncio
async def test_a_persons_own_chat_is_never_recycled_for_its_sender(chat):
    host = _Host(serves=False)
    await host._heal_viewed_session({"text": "hi"})
    assert host.recycled == []


def test_the_terminals_question_ends_when_its_call_ran():
    """The record is per call: another call's hook never ends it, the call's
    own PostToolUse does, and a call the engine ran without asking leaves
    nothing behind."""
    from core.session import session_state
    sid = "sess-ask"
    try:
        assert session_state.terminal_ask_since(sid) == 0.0
        session_state.note_terminal_ask(sid, "toolu_a")
        session_state.note_terminal_ask(sid, "toolu_b")
        session_state.record_hook_activity(sid)
        session_state.end_terminal_ask(sid, "toolu_b")
        assert session_state.terminal_ask_since(sid) > 0       # a still waits
        session_state.end_terminal_ask(sid, "toolu_a")
        assert session_state.terminal_ask_since(sid) == 0.0
    finally:
        session_state._session_terminal_ask.pop(sid, None)
        session_state._session_hook_activity.pop(sid, None)


def test_a_closed_session_leaves_no_question_or_hook_activity():
    """A terminal started again under the same id inherits neither."""
    from core.session import session_state
    sid = "sess-closed"
    session_state.note_terminal_ask(sid, "toolu_a")
    session_state.record_hook_activity(sid)
    session_state.cleanup_session_permission_state(sid)
    assert session_state.terminal_ask_since(sid) == 0.0
    assert session_state.get_hook_activity(sid) == 0


# -- M1: a session a wake built the unattended way is driven by nobody --------


class _NobodysTerminal:
    """A live terminal a wake built for nobody on the agent's shared chat."""

    def __init__(self, chat_id: str = CHAT):
        from core.session import session_state
        from core.session.interactive_output import InteractiveOutputMixin
        self.session_id, self.chat_id, self.user_sub = "sess-nobody", chat_id, ""
        self.chat_is_lane = False
        session_state._session_modes[self.session_id] = "auto"   # recorded at its start
        self.alive = True
        self.may_drive = InteractiveOutputMixin.may_drive.__get__(self)
        self.typed: list[bytes] = []

    @property
    def runs_for_nobody(self) -> bool:
        from core.session.session_state import runs_for_nobody
        return runs_for_nobody(self.session_id, self.chat_id, lane=self.chat_is_lane)

    @property
    def sole_driver(self) -> str:
        return self.user_sub


class _Door(ChatSendMixin):
    """The composer and queued doors of one member's connection."""

    def __init__(self):
        self.user_sub = "sub-member"
        self.agent_name = "pa"
        self.chat_id = CHAT
        self.session_id = ""
        self.layer = None
        self.user = {"username": "member"}
        self.read_only: list[str] = []

    def _viewer_context(self):
        return None

    async def _send_pty_read_only(self, sess) -> None:
        self.read_only.append(sess.session_id)


@pytest.mark.asyncio
async def test_the_composer_door_refuses_a_terminal_built_for_nobody(monkeypatch):
    from core.session import interactive_session
    term = _NobodysTerminal()
    monkeypatch.setattr(interactive_session, "get", lambda _sid: None)
    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda _cid: term)
    door = _Door()
    answer = await door._deliver_chat_to_live_interactive({"text": "rm everything"})
    assert answer == dashboard_chat_send.LIVE_TERMINAL_REFUSED
    assert door.read_only == ["sess-nobody"] and term.typed == []


@pytest.mark.asyncio
async def test_the_queued_door_waits_for_a_terminal_built_for_nobody(monkeypatch):
    from core.events import input_queue
    from core.session import interactive_session, visibility
    term = _NobodysTerminal()
    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda _cid: term)
    monkeypatch.setattr(task_store, "get_chat", lambda _cid: {
        "id": CHAT, "agent": "pa", "user_sub": "agent::pa", "session_id": "sess-nobody"})
    monkeypatch.setattr(visibility, "can_access_chat", lambda *a: True)
    door = _Door()
    door._closed_by_revalidation, door._held_gate = False, False
    door._revalidate_session = lambda: True

    async def _none(*a, **k):
        return ""
    door._deny_task_continue = _none
    door._sender_refusal = _none

    class _Qi:
        waiting = ""
    taken = [_Qi()]
    status, why = await door._deliver_queued(CHAT, taken)
    assert status == input_queue.DELIVERY_DEFERRED and "terminal" in why
    assert taken[0].waiting == input_queue.WAITING_TERMINAL


@pytest.mark.asyncio
async def test_a_headless_session_in_the_unattended_word_is_recycled_for_its_own_person(
        chat, monkeypatch):
    """Arm (b): the wake's own person opens the agent's shared chat. The
    session names them, but it runs in the unattended word: their message
    never rides it."""
    from core.session import session_state
    chat["user_sub"] = visibility.shared_chat_owner("pa")
    monkeypatch.setitem(session_state._session_modes, SID, "auto")
    monkeypatch.setattr(session_state, "get_session_security",
                        lambda sid: type("S", (), {"username": "ana"})())

    class _Real(_Host):
        _session_serves_sender = ChatSendMixin._session_serves_sender
    host = _Real(serves=True)
    await host._heal_viewed_session({"text": "hi"})
    assert len(host.recycled) == 1


@pytest.mark.asyncio
async def test_the_queued_turn_closes_a_shared_chat_session_built_for_another(monkeypatch):
    """The queued door's sender check runs on the agent's shared chat after
    the agent left Shared-only (review S2, mutation 2)."""
    from core.session import interactive_session, visibility
    closed: list[str] = []

    class _Layer:
        async def is_session_alive(self, _sid):
            return True

        async def close_session(self, sid):
            closed.append(sid)
            raise RuntimeError("stop here: the close is what is pinned")

    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda _cid: None)
    monkeypatch.setattr(task_store, "get_chat", lambda _cid: {
        "id": CHAT, "agent": "pa", "user_sub": "agent::pa", "session_id": SID})
    monkeypatch.setattr(visibility, "can_access_chat", lambda *a: True)
    monkeypatch.setattr(visibility, "is_shared_only", lambda _a: False)
    door = _Door()
    door._closed_by_revalidation, door._held_gate = False, False
    door._revalidate_session = lambda: True

    async def _none(*a, **k):
        return ""

    async def _layer(_cid):
        return _Layer()

    async def _payer(_sid):
        return "sub-other"
    door._deny_task_continue = _none
    door._sender_refusal = _none
    door._resolve_layer_for_chat_async = _layer
    door._session_payer = _payer
    with pytest.raises(RuntimeError, match="stop here"):
        await door._deliver_queued(CHAT, [object()])
    assert closed == [SID]


def test_a_steer_never_reaches_a_wakes_unattended_turn(monkeypatch):
    """A person's message while the wake's unattended turn runs waits for
    its end instead of riding it."""
    from core.session import session_state
    from ws.dashboard_dispatch import ClientMessageDispatcher

    class _Pump:
        source_type = "chat"
        session_id, chat_id = "sess-wake-run", CHAT

    class _Behaviour:
        supports_steer, attach_images_inline = True, False

    class _Caps:
        behaviour = _Behaviour()

    class _L:
        def capabilities_for(self, _sid):
            return _Caps()

    conn = type("C", (), {"session_id": "sess-wake-run", "layer": _L()})()
    possible = ClientMessageDispatcher._steer_possible.__get__(conn)
    assert possible(_Pump()) is True
    monkeypatch.setitem(session_state._session_modes, "sess-wake-run", "auto")
    assert possible(_Pump()) is False
    # A delegated lane's running turn takes its person's message.
    assert possible(_Pump(), lane=True) is True


@pytest.mark.asyncio
async def test_a_running_lane_turn_is_read_as_a_lane(monkeypatch):
    """The steer reads the chat's origin for a session in the unattended
    word: a chat-surface lane (a plain id) is a lane, a wake's chat is not."""
    from core.session import session_state
    from ws.dashboard_dispatch import ClientMessageDispatcher

    class _Pump:
        session_id, chat_id = "sess-lane-run", "7a6b5c4d-lane"
    rows = {"7a6b5c4d-lane": {"id": "7a6b5c4d-lane", "origin": "delegated"}}
    monkeypatch.setattr(task_store, "get_chat", rows.get)
    monkeypatch.setitem(session_state._session_modes, "sess-lane-run", "auto")
    assert await ClientMessageDispatcher._pump_chat_is_lane(_Pump()) is True
    rows["7a6b5c4d-lane"]["origin"] = "dashboard"
    assert await ClientMessageDispatcher._pump_chat_is_lane(_Pump()) is False


def _run_the_real_post_tool_use(tool_name: str, tool_use_id: str) -> list[dict]:
    """The PostToolUse hook script as a terminal runs it (``OTO_INTERACTIVE``
    set), against a listener that keeps what it posts."""
    import http.server
    import json
    import os
    import subprocess
    import sys
    import threading
    from pathlib import Path
    bodies: list[dict] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    script = Path(__file__).resolve().parents[2] / "hooks" / "tool_result_forwarder.py"
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.update(OTO_SESSION_ID="sess-real", PROXY_URL=f"http://127.0.0.1:{server.server_port}",
               PROXY_API_KEY="k", OTO_INTERACTIVE="1")
    try:
        subprocess.run([sys.executable, str(script)], input=json.dumps({
            "tool_name": tool_name, "tool_use_id": tool_use_id, "tool_input": {},
            "tool_response": {"content": "ok"}}), text=True, env=env, timeout=30, check=True)
    finally:
        server.shutdown()
    return bodies


@pytest.mark.asyncio
async def test_a_terminals_question_ends_with_its_result_not_its_hook(monkeypatch):
    """In a terminal the PostToolUse hook stands down: a native tool posts
    nothing, a tool server's call a memory-only ping with no call id, and
    neither ends the record. The tailer's result of the call does
    (``session_events.post_tool``), and the turn's end drops what is left."""
    import json

    import storage.database as db
    from api.hooks import lifecycle
    from core.session import (
        session_state,
        transcript_tailer as T,
        transcript_tool_events as TE,
    )
    sid, cid = "sess-real", "chat-real"
    assert _run_the_real_post_tool_use("Write", "toolu_w") == []
    [ping] = _run_the_real_post_tool_use("mcp__memory-mcp__memory", "toolu_m")
    assert ping.get("memory_only") is True and not ping.get("tool_use_id")

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(lifecycle, "verify_session_match_async", _ok)
    for name in ("toolu_w", "toolu_m", "toolu_left"):
        session_state.note_terminal_ask(sid, name)
    try:
        await lifecycle.hook_tool_result(lifecycle.HookToolResultRequest(**ping),
                                         authorization="Bearer k")
        assert session_state.terminal_ask_since(sid) > 0      # the hook ended nothing
        for name, value in (("add_chat_message", lambda *a, **k: None),
                            ("rebuild_chat_search", lambda c: None),
                            ("get_chat", lambda c: {"user_sub": "u", "title": "t"}),
                            ("update_chat", lambda c, **k: None),
                            ("set_chat_title_if_unset", lambda c, t: False)):
            monkeypatch.setattr(db, name, value)
        T._offsets.clear()
        T._tool_events.clear()
        T._attach_ts.clear()
        TE._sent_prompts.clear()
        stamp = "2026-11-12T00:10:0{}.000Z"
        calls = json.dumps({"type": "assistant", "uuid": "a1", "timestamp": stamp.format(1),
                            "message": {"role": "assistant", "stop_reason": "tool_use", "content": [
                                {"type": "tool_use", "id": t, "name": n, "input": {}}
                                for t, n in (("toolu_w", "Write"), ("toolu_m", "mcp__memory-mcp__memory"))]}})
        results = json.dumps({"type": "user", "uuid": "r1", "timestamp": stamp.format(2),
                              "message": {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": t, "content": "ok"}
                                  for t in ("toolu_w", "toolu_m")]}})
        T.tail_lines(sid, cid, [calls, results])
        assert set(session_state._session_terminal_ask[sid]) == {"toolu_left"}
        session_state.clear_terminal_asks(sid)                 # what the turn's end calls
        assert session_state.terminal_ask_since(sid) == 0.0
    finally:
        session_state._session_terminal_ask.pop(sid, None)


@pytest.mark.asyncio
async def test_a_terminals_turn_end_drops_its_questions():
    """A real terminal's turn end (the fold of an end_turn signal) drops the
    calls the tailer never answered (a sub-agent's spawn)."""
    from core.session import interactive_session as I, session_state
    s = I.InteractiveSession(session_id="sess-end", chat_id="", agent_name="a",
                             user_sub="u", role="manager", username="u")
    session_state.note_terminal_ask("sess-end", "toolu_spawn")
    s._apply_turn_signal("tool_use")
    s._apply_turn_signal("end_turn")
    assert session_state.terminal_ask_since("sess-end") == 0.0


def test_the_spawn_closes_a_headless_wake_for_nobody_on_a_shared_chat(temp_db, monkeypatch):
    from core.execution_layer import AgentConfig
    from core.session import chat_rebuild
    from services.scheduler import wake_session
    closed: list[str] = []

    class _Layer:
        async def start_session(self, sid, cfg):
            pass

        async def close_session(self, sid):
            closed.append(sid)

    async def _cfg(*a, **k):
        return AgentConfig(agent_name="pa", user_sub="", system_prompt="", mcp_config_path="",
                           permission_mode="auto", client_type="", resume=True)

    async def _echo(*a, **k):
        return ""
    monkeypatch.setattr(wake_session, "_wake_built_config", _cfg)
    monkeypatch.setattr(wake_session, "_chat_built_config", _cfg)
    monkeypatch.setattr(wake_session, "_run_echo_turn_pumped", _echo)
    monkeypatch.setattr(wake_session, "_wake_superseded", lambda *a: False)
    from core.session import background_leash
    from services.scheduler import wake_fresh
    pending = {"n": 0}
    monkeypatch.setattr(background_leash, "background_pending_count", lambda sid: pending["n"])
    monkeypatch.setattr(wake_fresh, "run_echo", _echo)
    cases = (("agent::pa", chat_rebuild.AS_WAKE, False, 0, ["s-w"]),
             ("agent::pa", chat_rebuild.AS_WAKE, True, 0, ["s-w"]),     # a fresh start
             ("agent::pa", chat_rebuild.AS_WAKE, False, 1, []),          # background work runs on
             ("agent::pa", chat_rebuild.AS_CHAT, False, 0, []),          # a member's own session
             ("user-1", chat_rebuild.AS_WAKE, False, 0, []),
             ("user-1", chat_rebuild.AS_CHAT, False, 0, []))
    for owner, build, fresh, bg, want in cases:
        closed.clear()
        pending["n"] = bg
        row = {"id": "chat-w", "agent": "pa", "user_sub": owner, "execution_mode": "headless"}
        started, _ = asyncio.run(wake_session._spawn_wake_session(
            _Layer(), "s-w", "pa", "WAKE", user_sub=None if build == chat_rebuild.AS_WAKE else "user-1",
            role="manager", chat_id="chat-w", chat_row=row, exec_path="claude-code-cli",
            target="local", username="", fresh=fresh,
            permission_mode="auto" if build == chat_rebuild.AS_WAKE else "ask",
            client_type="" if build == chat_rebuild.AS_WAKE else "dashboard", build=build))
        assert started and closed == want, (owner, build, fresh, bg)

    async def _raises(*a, **k):
        raise RuntimeError("the echo broke")
    monkeypatch.setattr(wake_session, "_run_echo_turn_pumped", _raises)
    closed.clear()
    row = {"id": "chat-w", "agent": "pa", "user_sub": "agent::pa", "execution_mode": "headless"}
    with pytest.raises(RuntimeError):
        asyncio.run(wake_session._spawn_wake_session(
            _Layer(), "s-w", "pa", "WAKE", user_sub=None, role="manager", chat_id="chat-w",
            chat_row=row, exec_path="claude-code-cli", target="local", username="",
            permission_mode="auto", client_type="", build=chat_rebuild.AS_WAKE))
    assert closed == ["s-w"]                      # closed even when its echo raised


# -- the end of a wake's session for nobody (A) --------------------------------


def _rewarm(monkeypatch, isess, **kw):
    from tests.tasks.test_delegate_wake_recovery import TestRewarmInteractiveAndWake
    return TestRewarmInteractiveAndWake()._run(monkeypatch, isess, ends_with_turn=True, **kw)


def _fake(**kw):
    from tests.tasks.test_delegate_wake_recovery import TestRewarmInteractiveAndWake
    return TestRewarmInteractiveAndWake._FakeIsess(**kw)


def test_a_terminal_built_for_nobody_on_a_shared_chat_ends_with_its_turn(temp_db, monkeypatch):
    # Watched or not, it closes: a person's next message warms their own.
    isess = _fake()
    isess.has_viewer = True
    out, _cfg, closed = _rewarm(monkeypatch, isess)
    assert out == "" and closed == ["wake_turn_done"]


def test_a_turn_faster_than_the_minimum_still_ends_the_terminal(temp_db, monkeypatch):
    """A turn that ends inside the minimum turn age never calls back: two
    looks with the turn over end the wait, and the terminal closes."""
    from services.scheduler import wake_session
    monkeypatch.setattr(wake_session, "_WAKE_POLL_S", 0.01)
    isess = _fake()

    def _submit(text, *, settle=False):
        isess.submitted.append((text, settle))
        isess.turn_open = True
        return True
    isess.submit_prompt = _submit
    looks = {"n": 0}

    class _Event:
        async def wait(self):
            looks["n"] += 1
            if looks["n"] > 1:
                isess.turn_open = False          # the turn ended, no callback
            await asyncio.sleep(1)
    monkeypatch.setattr(wake_session.asyncio, "Event", _Event)
    out, _cfg, closed = _rewarm(monkeypatch, isess)
    assert out == "" and closed == ["wake_turn_done"]


def test_a_parked_question_holds_the_woken_terminal(temp_db, monkeypatch):
    """A question the turn parks on (a card waiting for its person) is no
    end of the turn: the looks count only once it is answered."""
    from services.scheduler import wake_session
    monkeypatch.setattr(wake_session, "_WAKE_POLL_S", 0.01)
    isess = _fake()

    def _submit(text, *, settle=False):
        isess.turn_open = True
        return True
    isess.submit_prompt = _submit
    looks = {"n": 0}

    class _Event:
        async def wait(self):
            looks["n"] += 1
            if looks["n"] == 2:
                isess.turn_open, isess.question_parked = False, True
            if looks["n"] == 6:
                isess.question_parked = False      # its person answered
            await asyncio.sleep(1)
    monkeypatch.setattr(wake_session.asyncio, "Event", _Event)
    out, _cfg, closed = _rewarm(monkeypatch, isess)
    assert out == "" and closed == ["wake_turn_done"] and looks["n"] >= 7


def test_the_backstop_ends_a_terminal_built_for_nobody(temp_db, monkeypatch):
    from services.scheduler import interactive, wake_session
    monkeypatch.setattr(wake_session, "_WAKE_POLL_S", 0.01)
    monkeypatch.setattr(interactive, "INTERACTIVE_TASK_MAX_S", 0.03)
    isess = _fake()

    def _submit(text, *, settle=False):
        isess.turn_open = True                   # opens and never ends
        return True
    isess.submit_prompt = _submit
    out, _cfg, closed = _rewarm(monkeypatch, isess)
    assert out == "" and closed == ["wake_turn_done"]


def test_background_work_keeps_the_woken_terminal(temp_db, monkeypatch):
    """The work the wake's turn started runs on: the terminal is left to the
    reaper, as a chat's own, and stays read-only."""
    isess = _fake()
    isess.background = True
    out, _cfg, closed = _rewarm(monkeypatch, isess)
    assert out == "" and closed == []


def test_a_session_for_nobody_is_read_from_the_kept_index(monkeypatch):
    """After a restart, before its re-adoption restores the live map, the
    mode the index kept marks the session."""
    from core.session import session_state
    monkeypatch.setitem(session_state._reloaded_state, "sess-kept", ("auto", 0))
    session_state._session_modes.pop("sess-kept", None)
    assert session_state.runs_for_nobody("sess-kept", "chat-kept") is True


@pytest.mark.asyncio
async def test_a_mode_pick_never_relabels_a_session_for_nobody(monkeypatch):
    """The mode menu on a headless session a wake built for nobody: the pick
    goes to the row, the session is closed (no turn runs), its word stays.
    A lane's chat takes the pick as any chat does."""
    from core.events import chat_writer
    from core.session import session_state
    from ws.dashboard_chat_support import ChatSupportMixin
    closed, written, sent = [], [], []

    class _Layer:
        async def close_session(self, sid):
            closed.append(sid)

    async def _submit(cid, job, *, label=""):
        written.append(cid)
    monkeypatch.setattr(chat_writer, "submit", _submit)
    rows = {"c-old": {"id": "c-old", "origin": "dashboard"}, "c-lane": {"id": "c-lane", "origin": "delegated"}}
    monkeypatch.setattr(task_store, "get_chat", rows.get)
    monkeypatch.setitem(session_state._session_modes, "s-old", "auto")
    monkeypatch.setitem(session_state._session_modes, "s-lane", "auto")

    async def _send(frame):
        sent.append(frame)
    conn = type("C", (), {"layer": _Layer(), "chat_id": "c-old", "session_id": "s-old",
                          "deferred_mode": "", "deferred_for": "", "_send": staticmethod(_send)})()
    pick = ChatSupportMixin._mode_pick_for_nobody.__get__(conn)
    assert await pick("s-old", "c-old", "dontAsk") is True
    assert closed == ["s-old"] and written == ["c-old"] and conn.session_id is None
    assert session_state._session_modes["s-old"] == "auto"
    assert await pick("s-lane", "c-lane", "dontAsk") is False


@pytest.mark.asyncio
async def test_voice_closes_a_session_for_nobody_before_its_turn(monkeypatch):
    """A spoken turn never rides a headless session a wake built for nobody:
    it is closed, and the headless resume starts the speaker's own."""
    from core.session import session_manager, session_state
    from tests._compute import serve
    from ws import duplex_attach, headless_resume
    serve(monkeypatch)
    closed: list[str] = []

    class _Layer:
        alive = True

        async def close_session(self, sid):
            closed.append(sid)
            self.alive = False

        async def is_session_process_dead(self, sid):
            return not self.alive

        async def is_session_alive(self, sid):
            return self.alive

    resumed: list[str] = []

    async def _resume(chat_id, sid, layer, *, user_sub=""):
        resumed.append(user_sub)
        return type("R", (), {"session_id": sid, "layer": layer, "resumed": True})()
    monkeypatch.setattr(headless_resume, "resume_dead_session_headless", _resume)
    monkeypatch.setattr(session_manager, "live_session", lambda sid: None)
    monkeypatch.setattr(task_store, "get_chat", lambda cid: {"id": cid, "session_id": "s-v",
                                                            "origin": "dashboard"})
    monkeypatch.setitem(session_state._session_modes, "s-v", "auto")
    st = duplex_attach._AttachState()
    st.session_id, st.layer = "s-v", _Layer()
    bridge = type("B", (), {"chat_id": "c-v", "sub": "sub-speaker", "duplex_id": "d1234567"})()
    await duplex_attach._ensure_live_session(bridge, st)
    assert closed == ["s-v"] and resumed == ["sub-speaker"]
