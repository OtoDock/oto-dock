"""Duplex bridge lifecycle — dial-back claims, teardown anchor rule, budget.

Handler-level tests with duck-typed WebSockets (the pattern of
``tests/phone/test_ws_phone_turns.py``): no real sockets, no daemon. These
pin the LIFECYCLE — every exit path funnels through one idempotent
``_teardown`` that kills the budget timer, aborts an in-flight attached
turn, and unregisters — because leaks in a duplex system live exactly here.
"""

import asyncio
import json

import pytest

import ws.duplex as ws_duplex
import ws.duplex_attach as duplex_attach


class FakeWebSocket:
    def __init__(self, key="test-key"):
        self.headers = {"authorization": f"Bearer {key}"}
        self.sent: list = []
        self.closed_code = None
        self.accepted = False
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed_code = code

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def receive(self):
        return await self._incoming.get()

    async def receive_text(self):
        msg = await self._incoming.get()
        return msg["text"]

    def push_json(self, payload):
        self._incoming.put_nowait({"type": "websocket.receive",
                                   "text": json.dumps(payload)})

    def push_bytes(self, data):
        self._incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def disconnect(self):
        self._incoming.put_nowait({"type": "websocket.disconnect"})


@pytest.fixture(autouse=True)
def _clean_registries(monkeypatch):
    ws_duplex._pending.clear()
    ws_duplex._active.clear()
    duplex_attach._states.clear()
    monkeypatch.setattr("config.is_master_key", lambda k: k == "test-key")
    yield
    ws_duplex._pending.clear()
    ws_duplex._active.clear()
    duplex_attach._states.clear()


def _bridge(**kw):
    kwargs = dict(duplex_id="dx-1", sub="user-a", chat_id="chat-1",
                  max_seconds=1800, browser_ws=FakeWebSocket())
    kwargs.update(kw)
    return ws_duplex.DuplexBridge(**kwargs)


# --- dial-back claims -------------------------------------------------------

@pytest.mark.asyncio
async def test_engine_orphan_dialback_refused():
    ws = FakeWebSocket()
    await ws_duplex.ws_duplex_engine_handler(ws, "unknown-id")
    assert ws.closed_code == 4404
    assert not ws.accepted


@pytest.mark.asyncio
async def test_engine_bad_key_refused():
    ws = FakeWebSocket(key="wrong")
    await ws_duplex.ws_duplex_engine_handler(ws, "dx-1")
    assert ws.closed_code == 4001


@pytest.mark.asyncio
async def test_engine_duplicate_dialback_refused():
    bridge = _bridge()
    bridge.engine_ws = FakeWebSocket()  # already claimed
    ws_duplex._pending["dx-1"] = bridge
    dup = FakeWebSocket()
    await ws_duplex.ws_duplex_engine_handler(dup, "dx-1")
    assert dup.closed_code == 4409


@pytest.mark.asyncio
async def test_engine_claim_moves_pending_to_active_and_wakes_browser():
    bridge = _bridge()
    ws_duplex._pending["dx-1"] = bridge
    engine = FakeWebSocket()
    task = asyncio.create_task(ws_duplex.ws_duplex_engine_handler(engine, "dx-1"))
    await asyncio.wait_for(bridge.engine_ready.wait(), timeout=2)
    assert bridge.engine_ws is engine
    assert "dx-1" in ws_duplex._active and "dx-1" not in ws_duplex._pending
    engine.disconnect()
    await asyncio.wait_for(task, timeout=2)
    assert bridge.closed  # engine death tears the whole session down


# --- open_failed ------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_failed_wakes_waiter_with_reason():
    bridge = _bridge()
    ws_duplex._pending["dx-1"] = bridge
    ws_duplex.on_open_failed("dx-1", "no capacity")
    assert bridge.open_failed_reason == "no capacity"
    assert bridge.engine_ready.is_set()


# --- teardown anchor rule ---------------------------------------------------

@pytest.mark.asyncio
async def test_teardown_is_idempotent_and_resumes_inflight():
    """Exiting spoken mode is NOT Stop (operator decision 2026-08-14): an
    in-flight attached turn keeps producing into the chat — teardown queues
    a mode-exit system note instead of aborting, exactly once."""
    from types import SimpleNamespace

    bridge = _bridge()
    ws_duplex._active["dx-1"] = bridge
    aborts = []

    async def _abort():
        aborts.append(1)

    bridge.abort_inflight_turn = _abort
    bridge.budget_task = asyncio.create_task(asyncio.sleep(60))
    st = duplex_attach._AttachState()
    st.pump = SimpleNamespace(system_queue=[])
    duplex_attach._states["dx-1"] = st

    await ws_duplex._teardown(bridge, "test")
    await ws_duplex._teardown(bridge, "again")  # second call: no-op

    assert aborts == []  # the turn was left running
    assert len(st.pump.system_queue) == 1  # …with the mode-exit note queued
    assert "exited spoken phone mode" in st.pump.system_queue[0]
    assert "dx-1" not in ws_duplex._active
    assert "dx-1" not in duplex_attach._states
    assert bridge.closed
    # end frames reached both sides exactly once
    ends = [f for f in bridge.browser_ws.sent
            if isinstance(f, dict) and f.get("type") == "end"]
    assert len(ends) == 1 and ends[0]["reason"] == "test"


@pytest.mark.asyncio
async def test_budget_expiry_tears_down():
    bridge = _bridge(max_seconds=1)
    ws_duplex._active["dx-1"] = bridge
    bridge.budget_task = asyncio.create_task(ws_duplex._budget_loop(bridge))
    await asyncio.sleep(1.2)
    assert bridge.closed
    ends = [f for f in bridge.browser_ws.sent
            if isinstance(f, dict) and f.get("type") == "end"]
    assert ends and ends[0]["reason"] == "budget"


# --- audio pass-through -----------------------------------------------------

@pytest.mark.asyncio
async def test_engine_binary_frames_reach_browser():
    bridge = _bridge()
    ws_duplex._pending["dx-1"] = bridge
    engine = FakeWebSocket()
    task = asyncio.create_task(ws_duplex.ws_duplex_engine_handler(engine, "dx-1"))
    await asyncio.wait_for(bridge.engine_ready.wait(), timeout=2)
    engine.push_bytes(b"\x01\x02")
    engine.push_json({"type": "state", "state": "speaking"})
    engine.disconnect()
    await asyncio.wait_for(task, timeout=2)
    assert b"\x01\x02" in bridge.browser_ws.sent
    assert {"type": "state", "state": "speaking"} in [
        f for f in bridge.browser_ws.sent if isinstance(f, dict)]


@pytest.mark.asyncio
async def test_engine_caption_frames_reach_browser():
    """All three caption channels pass the relay whitelist: `interim`
    (overlay partial), `interim_final` (accumulate chunk — R4.6), and
    `final` (dispatched utterance). Unknown engine frames are dropped."""
    bridge = _bridge()
    ws_duplex._pending["dx-1"] = bridge
    engine = FakeWebSocket()
    task = asyncio.create_task(ws_duplex.ws_duplex_engine_handler(engine, "dx-1"))
    await asyncio.wait_for(bridge.engine_ready.wait(), timeout=2)
    engine.push_json({"type": "interim", "text": "hey do"})
    engine.push_json({"type": "interim_final", "text": "hey, do you copy?"})
    engine.push_json({"type": "final", "text": "hey, do you copy? over"})
    engine.push_json({"type": "bogus", "text": "never forwarded"})
    engine.disconnect()
    await asyncio.wait_for(task, timeout=2)
    frames = [f for f in bridge.browser_ws.sent if isinstance(f, dict)]
    assert {"type": "interim", "text": "hey do"} in frames
    assert {"type": "interim_final", "text": "hey, do you copy?"} in frames
    assert {"type": "final", "text": "hey, do you copy? over"} in frames
    assert not any(f.get("type") == "bogus" for f in frames)


@pytest.mark.asyncio
async def test_engine_flush_frame_reaches_browser():
    """The barge-in flush frame passes the relay whitelist — without it the
    browser plays out its whole scheduled lead (250 ms–1 s) after a
    VAD-detected barge-in."""
    bridge = _bridge()
    ws_duplex._pending["dx-1"] = bridge
    engine = FakeWebSocket()
    task = asyncio.create_task(ws_duplex.ws_duplex_engine_handler(engine, "dx-1"))
    await asyncio.wait_for(bridge.engine_ready.wait(), timeout=2)
    engine.push_json({"type": "flush"})
    engine.disconnect()
    await asyncio.wait_for(task, timeout=2)
    frames = [f for f in bridge.browser_ws.sent if isinstance(f, dict)]
    assert {"type": "flush"} in frames


# --- attach prompt building -------------------------------------------------

def test_prompt_context_injected_once(temp_db):
    from storage import database as task_store
    task_store.set_platform_setting("chat_duplex_context", "SPOKEN RULES")
    st = duplex_attach._AttachState(execution_path="claude-code-cli")
    first = duplex_attach._build_prompt(st, "hello", None)
    assert first.startswith("<system-reminder>\nSPOKEN RULES\n</system-reminder>")
    assert first.endswith("hello")
    second = duplex_attach._build_prompt(st, "again", None)
    assert second == "again"


def test_prompt_interruption_note_only_for_non_direct(temp_db):
    st = duplex_attach._AttachState(execution_path="claude-code-cli",
                                    context_injected=True)
    st.interrupted_last_turn = True
    p = duplex_attach._build_prompt(st, "next", 42)
    assert "heard only the first 42 characters" in p
    assert st.interrupted_last_turn is False

    st2 = duplex_attach._AttachState(execution_path="direct-llm",
                                     context_injected=True)
    st2.interrupted_last_turn = True
    p2 = duplex_attach._build_prompt(st2, "next", 42)
    # direct annotates its own history via the barge_in_chars kwarg
    assert "characters" not in p2 and p2 == "next"


# --- interactive row feed ---------------------------------------------------

@pytest.mark.asyncio
async def test_interactive_feed_speaks_new_assistant_rows(temp_db):
    from storage import database as task_store
    task_store.create_chat("chat-i", "user-a", "agent-a", "auto")
    old_id = task_store.add_chat_message("chat-i", "assistant", "old reply")

    bridge = _bridge(chat_id="chat-i")
    bridge.engine_ws = FakeWebSocket()
    st = duplex_attach._AttachState(
        chat_id="chat-i", interactive=True, row_cursor=old_id, active_turn=3)
    duplex_attach._states["dx-1"] = st
    duplex_attach._interactive_feeds["chat-i"] = bridge

    task_store.add_chat_message("chat-i", "user", "spoken words")
    task_store.add_chat_message("chat-i", "assistant", "fresh reply")
    task_store.add_chat_message("chat-i", "assistant", "", event_type="tool_use")

    duplex_attach.on_interactive_batch("chat-i", persisted=3, turn_open=True)
    await asyncio.sleep(0.2)
    texts = [f for f in bridge.engine_ws.sent
             if isinstance(f, dict) and f.get("type") == "text"]
    assert len(texts) == 1  # user + empty tool rows filtered, old row skipped
    assert texts[0]["data"]["content"].startswith("fresh reply")
    assert texts[0]["turn"] == 3
    assert st.active_turn == 3  # turn still open — no done yet

    # Turn close (even with nothing persisted) → done frame, turn cleared.
    duplex_attach.on_interactive_batch("chat-i", persisted=0, turn_open=False)
    await asyncio.sleep(0.2)
    dones = [f for f in bridge.engine_ws.sent
             if isinstance(f, dict) and f.get("type") == "done"]
    assert dones and dones[0]["turn"] == 3
    assert st.active_turn is None

    duplex_attach._interactive_feeds.clear()


@pytest.mark.asyncio
async def test_interactive_feed_forwards_tool_rows_as_tool_end(temp_db):
    """A persisted tool row (event_type='tool'/'task_spawn') = the tool
    already completed — the feed must forward a tool_end boundary so the
    engine finalizes the pre-tool TTS segment instead of holding it until
    the turn closes (live-hit 2026-08-10: pre-tool sentence sat silent for
    27 s while a web search ran)."""
    from storage import database as task_store
    task_store.create_chat("chat-t", "user-a", "agent-a", "auto")
    bridge = _bridge(chat_id="chat-t")
    bridge.engine_ws = FakeWebSocket()
    st = duplex_attach._AttachState(
        chat_id="chat-t", interactive=True, row_cursor=0, active_turn=5)
    duplex_attach._states["dx-1"] = st
    duplex_attach._interactive_feeds["chat-t"] = bridge

    task_store.add_chat_message("chat-t", "assistant", "One moment.")
    task_store.add_chat_message(
        "chat-t", "assistant", "", event_type="tool",
        event_data='{"type": "tool", "name": "WebSearch"}')
    task_store.add_chat_message("chat-t", "assistant", "It will be sunny.")

    duplex_attach.on_interactive_batch("chat-t", persisted=3, turn_open=True)
    await asyncio.sleep(0.2)
    frames = [(f.get("type"), f.get("turn")) for f in bridge.engine_ws.sent
              if isinstance(f, dict)]
    assert frames == [("text", 5), ("tool_end", 5), ("text", 5)]

    duplex_attach._interactive_feeds.clear()


@pytest.mark.asyncio
async def test_abort_turn_interactive_sends_esc_and_notes_interruption(monkeypatch):
    """Real barge-in on a PTY chat: the abort_turn frame triggers the same
    graceful ESC the dashboard Stop button sends, and stamps the
    interruption for the next prompt's note. A stale abort (older turn id)
    must never fire — it could kill a newer turn."""
    from core.session import interactive_session

    class FakeIsess:
        def __init__(self):
            self.esc_sent = 0

        def interrupt_turn(self):
            self.esc_sent += 1
            return True

    isess = FakeIsess()
    monkeypatch.setattr(
        interactive_session, "find_live_for_chat", lambda cid: isess)

    bridge = _bridge(chat_id="chat-a")
    st = duplex_attach._AttachState(
        chat_id="chat-a", interactive=True, active_turn=4)
    duplex_attach._states[bridge.duplex_id] = st

    # Stale abort (turn 3 while 4 is active) → ignored.
    await duplex_attach.abort_turn(bridge, {"type": "abort_turn", "turn": 3})
    assert isess.esc_sent == 0
    assert st.interrupted_last_turn is False

    # Current-turn abort → ESC + interruption stamped.
    await duplex_attach.abort_turn(bridge, {"type": "abort_turn", "turn": 4})
    assert isess.esc_sent == 1
    assert st.interrupted_last_turn is True

    duplex_attach._states.clear()


@pytest.mark.asyncio
async def test_interactive_feed_noop_without_active_turn(temp_db):
    from storage import database as task_store
    task_store.create_chat("chat-j", "user-a", "agent-a", "auto")
    bridge = _bridge(chat_id="chat-j")
    bridge.engine_ws = FakeWebSocket()
    st = duplex_attach._AttachState(chat_id="chat-j", interactive=True)
    duplex_attach._states["dx-1"] = st
    duplex_attach._interactive_feeds["chat-j"] = bridge
    duplex_attach.on_interactive_batch("chat-j", persisted=2, turn_open=True)
    await asyncio.sleep(0.1)
    assert bridge.engine_ws.sent == []  # no utterance in flight — screen-only
    duplex_attach._interactive_feeds.clear()


@pytest.mark.asyncio
async def test_teardown_unregisters_interactive_feed():
    bridge = _bridge(chat_id="chat-k")
    st = duplex_attach._AttachState(chat_id="chat-k", interactive=True)
    duplex_attach._states["dx-1"] = st
    duplex_attach._interactive_feeds["chat-k"] = bridge
    await ws_duplex._teardown(bridge, "test")
    assert "chat-k" not in duplex_attach._interactive_feeds


@pytest.mark.asyncio
async def test_interactive_state_resolves_once(temp_db, monkeypatch):
    """The duplex context must ride the FIRST utterance only — a fresh state
    per utterance re-injected the whole spoken-mode prompt into the terminal
    on every turn (live-test regression)."""
    from storage import database as task_store
    task_store.create_chat("chat-r", "user-a", "agent-a", "auto")
    task_store.update_chat("chat-r", session_id="sid-r1")
    task_store.set_platform_setting("chat_duplex_context", "SPOKEN RULES")

    bridge = _bridge(chat_id="chat-r")
    bridge.engine_ws = FakeWebSocket()

    class _FakeIsess:
        prompts: list = []

        def queue_prompt(self, text, source, **ctx):
            self.prompts.append(text)
            return True

    isess = _FakeIsess()
    monkeypatch.setattr(
        "core.session.interactive_session.find_live_for_chat",
        lambda chat_id, **kw: isess)

    await duplex_attach.run_utterance(bridge, {"turn": 1, "text": "first words"})
    st1 = duplex_attach._states.get("dx-1")
    await duplex_attach.run_utterance(bridge, {"turn": 2, "text": "second words"})
    st2 = duplex_attach._states.get("dx-1")

    assert st1 is st2  # same attach state across utterances
    assert len(isess.prompts) == 2
    assert "SPOKEN RULES" in isess.prompts[0]
    assert "SPOKEN RULES" not in isess.prompts[1]  # context injected ONCE
    duplex_attach._interactive_feeds.clear()


@pytest.mark.asyncio
async def test_interactive_feed_ignores_preopen_close_edge(temp_db):
    """Cold-PTY race (live-hit 2026-08-12 06:07): the injected duplex
    prompt's own echoed USER row persists seconds BEFORE the CLI opens the
    turn, in a batch reporting turn_open=False. Closing on that edge sent an
    empty `done` (daemon: "TTS playback done: 0 chunks") and the real reply
    rows that followed were never forwarded — silent turn. The close edge
    must only count once the turn was seen open or output was forwarded."""
    from storage import database as task_store
    task_store.create_chat("chat-p", "user-a", "agent-a", "auto")

    bridge = _bridge(chat_id="chat-p")
    bridge.engine_ws = FakeWebSocket()
    st = duplex_attach._AttachState(
        chat_id="chat-p", interactive=True, row_cursor=0, active_turn=1)
    duplex_attach._states["dx-1"] = st
    duplex_attach._interactive_feeds["chat-p"] = bridge

    # Batch 1: the echoed user row, turn NOT open yet — must not close.
    task_store.add_chat_message("chat-p", "user", "spoken words + context")
    duplex_attach.on_interactive_batch("chat-p", persisted=1, turn_open=False)
    await asyncio.sleep(0.2)
    assert st.active_turn == 1
    assert bridge.engine_ws.sent == []  # no done, no frames

    # Batch 2: the reply streams with the turn open — forwarded normally.
    task_store.add_chat_message("chat-p", "assistant", "Sunny tomorrow.")
    duplex_attach.on_interactive_batch("chat-p", persisted=1, turn_open=True)
    await asyncio.sleep(0.2)
    texts = [f for f in bridge.engine_ws.sent
             if isinstance(f, dict) and f.get("type") == "text"]
    assert len(texts) == 1 and texts[0]["turn"] == 1

    # Batch 3: the REAL close edge → done + turn cleared.
    duplex_attach.on_interactive_batch("chat-p", persisted=0, turn_open=False)
    await asyncio.sleep(0.2)
    dones = [f for f in bridge.engine_ws.sent
             if isinstance(f, dict) and f.get("type") == "done"]
    assert dones and dones[0]["turn"] == 1
    assert st.active_turn is None
    duplex_attach._interactive_feeds.clear()


@pytest.mark.asyncio
async def test_interactive_feed_single_batch_turn_still_closes(temp_db):
    """A turn whose reply rows AND close edge arrive in one batch (fast
    turn, chunky tailer) must still forward the text and close — the
    pre-open guard keys on forwarded output, not only on a seen open."""
    from storage import database as task_store
    task_store.create_chat("chat-q", "user-a", "agent-a", "auto")

    bridge = _bridge(chat_id="chat-q")
    bridge.engine_ws = FakeWebSocket()
    st = duplex_attach._AttachState(
        chat_id="chat-q", interactive=True, row_cursor=0, active_turn=2)
    duplex_attach._states["dx-1"] = st
    duplex_attach._interactive_feeds["chat-q"] = bridge

    task_store.add_chat_message("chat-q", "assistant", "Done already.")
    duplex_attach.on_interactive_batch("chat-q", persisted=1, turn_open=False)
    await asyncio.sleep(0.2)
    kinds = [f.get("type") for f in bridge.engine_ws.sent if isinstance(f, dict)]
    assert kinds == ["text", "done"]
    assert st.active_turn is None
    duplex_attach._interactive_feeds.clear()


@pytest.mark.asyncio
async def test_abort_turn_routes_to_foreign_pump_only_while_live():
    """A duplex utterance queued onto a FOREIGN live pump (typed turn
    streaming) records it as the abort target — a barge-in must interrupt
    THAT generation (st.pump stays None by invariant, which used to drop
    the abort silently). A later abort whose target is no longer the
    chat's live pump is dropped: never kill a newer turn."""
    from core.events.stream_pump import _active_pumps

    class FakePump:
        def __init__(self):
            self.aborts = 0

        def abort(self):
            self.aborts += 1

    class FakeLayer:
        def __init__(self):
            self.aborts = 0

        async def abort(self, sid):
            self.aborts += 1
            return False  # not graceful → the hard path must hit the pump

    bridge = _bridge(chat_id="chat-f")
    layer = FakeLayer()
    foreign = FakePump()
    st = duplex_attach._AttachState(
        chat_id="chat-f", session_id="s1", layer=layer, active_turn=7)
    st.abort_target = foreign
    duplex_attach._states[bridge.duplex_id] = st
    _active_pumps["chat-f"] = foreign
    try:
        await duplex_attach.abort_turn(bridge, {"type": "abort_turn", "turn": 7})
        assert layer.aborts == 1
        assert foreign.aborts == 1
        assert st.interrupted_last_turn is True

        # The chat moved on to a NEWER pump → the stale abort is dropped.
        st.interrupted_last_turn = False
        st.abort_target = foreign
        newer = FakePump()
        _active_pumps["chat-f"] = newer
        await duplex_attach.abort_turn(bridge, {"type": "abort_turn", "turn": 7})
        assert layer.aborts == 1
        assert foreign.aborts == 1
        assert newer.aborts == 0
        assert st.interrupted_last_turn is False
    finally:
        _active_pumps.pop("chat-f", None)
