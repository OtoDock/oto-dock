"""Turn protocol on /ws/phone: turn-id stamping, abort, and overlap draining.

The handler is driven directly with a duck-typed WebSocket and a fake
execution layer + pump, so these tests pin the PROTOCOL (frames echo the
chat's turn id; "abort" cancels the in-flight producer; a chat that arrives
while a previous turn is still streaming queues behind the session lock)
without exercising the real layers or DB persistence.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

import ws.phone as ws_phone
from core.events.common_events import PRODUCER_DONE, TEXT, CommonEvent
from core.execution_layer import ResumeVerdict

_DISCONNECT = object()


class FakeWebSocket:
    """Duck-typed FastAPI WebSocket: scripted client messages, captured sends."""

    def __init__(self):
        self.headers = {"authorization": "Bearer test-key"}
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._sent_event = asyncio.Event()

    async def accept(self):
        pass

    async def close(self, code=1000, reason=""):
        pass

    async def receive_text(self) -> str:
        item = await self._incoming.get()
        if item is _DISCONNECT:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect(1000)
        return item

    async def send_json(self, payload: dict):
        self.sent.append(payload)
        self._sent_event.set()

    def push(self, msg: dict):
        self._incoming.put_nowait(json.dumps(msg))

    def disconnect(self):
        self._incoming.put_nowait(_DISCONNECT)

    async def wait_for_frame(self, predicate, timeout=5.0):
        """Wait until a sent frame matches ``predicate``; returns it."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for frame in self.sent:
                if predicate(frame):
                    return frame
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"no frame matched within {timeout}s; sent={self.sent}"
                )
            self._sent_event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._sent_event.wait(), timeout=remaining)


class FakeLayer:
    """Execution layer stub: per-session lock + scripted/gated generation."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self.gate: asyncio.Event | None = None  # holds generation open when set
        self.cancelled: list[str] = []
        self.prompts: list[str] = []
        # Graceful-abort knob: None = no abort() attribute behavior (falsy →
        # hard fallback); True = the layer interrupts the turn itself.
        self.abort_result: bool | None = None
        self.aborted: list[str] = []
        # Dead-process heal knobs (test_dead_process_respawns_in_place).
        self.process_dead = False
        self.resume_verdict = ResumeVerdict.RESUMABLE
        self.rebuilds_from_rows = False
        self.resume_checked: list[str] = []
        self.prepared: list[str] = []
        self.started: list[tuple[str, object]] = []

    def session_lock(self, session_id: str):
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def is_session_alive(self, session_id: str) -> bool:
        return True

    async def is_session_process_dead(self, session_id: str) -> bool:
        return self.process_dead

    async def can_resume_session(self, session_id: str, *, agent_name="",
                                 username="", external_home="") -> ResumeVerdict:
        self.resume_checked.append(session_id)
        return self.resume_verdict

    def capabilities_for(self, session_id: str):
        return SimpleNamespace(behaviour=SimpleNamespace(rebuilds_history_from_db=self.rebuilds_from_rows))

    async def prepare_resume(self, session_id: str) -> None:
        self.prepared.append(session_id)

    async def start_session(self, session_id: str, config) -> None:
        self.started.append((session_id, config))
        self.process_dead = False  # respawn revives the process

    async def close_session(self, session_id: str) -> None:
        pass

    async def abort(self, session_id: str) -> bool:
        self.aborted.append(session_id)
        if self.abort_result and self.gate is not None:
            # A real graceful interrupt makes the engine CLOSE the turn —
            # release the gate so the producer drains to completion.
            self.gate.set()
        return bool(self.abort_result)

    async def send_message(self, session_id: str, prompt: str, **kwargs):
        self.prompts.append(prompt)
        try:
            yield CommonEvent(TEXT, {"content": f"reply-to:{prompt}"})
            if self.gate is not None:
                await self.gate.wait()
            yield CommonEvent(TEXT, {"content": " tail"})
        except asyncio.CancelledError:
            self.cancelled.append(prompt)
            raise


class FakePump:
    """Minimal ChatStreamPump: event_queue → attached ws queue."""

    def __init__(self, *, chat_id, session_id, producer, event_queue,
                 perm_queue, scope, source_type):
        self.event_queue = event_queue
        self._ws_queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def attach(self) -> asyncio.Queue:
        self._ws_queue = asyncio.Queue()
        return self._ws_queue

    def start(self):
        async def _pump():
            while True:
                ev = await self.event_queue.get()
                if ev.type == PRODUCER_DONE:
                    self._ws_queue.put_nowait({"pump_type": "all_done"})
                    return
                if ev.type == "error":
                    self._ws_queue.put_nowait({
                        "pump_type": "error",
                        "message": ev.data.get("message", ""),
                    })
                    return
                self._ws_queue.put_nowait({
                    "pump_type": "ws_event",
                    "event": {"type": ev.type, **ev.data},
                })
        self._task = asyncio.create_task(_pump())
        return self._task


class _FakeAgentCfg:
    execution_path = "direct-llm"
    mcp_config_path = "/tmp/phone-mcp.json"
    execution_target = "local"

    def __init__(self):
        self.chat_id = ""
        self.extra_env: dict = {}


@pytest.fixture
def phone_ws_env(monkeypatch):
    """Patch the handler's collaborators; returns (websocket, layer)."""
    layer = FakeLayer()

    monkeypatch.setattr(ws_phone.config, "is_master_key", lambda k: k == "test-key")
    monkeypatch.setattr(ws_phone.config, "get_cli_model", lambda name: "model-x")
    monkeypatch.setattr(ws_phone, "resolve_phone_execution_target",
                        lambda name, **kw: None)
    monkeypatch.setattr(ws_phone, "get_execution_layer",
                        lambda name, execution_target=None, **kw: layer)
    # No pre-warmed session is held anywhere unless a test says so.
    monkeypatch.setattr(ws_phone, "live_session", lambda sid: None)
    # Identity resolution is DB-backed (routes, agents, users) — stub it to
    # the id-less external principal; a reuse request passes through as-is.
    from core.session import external_identity
    from services.phone.phone_identity import RouteIdentity
    monkeypatch.setattr(ws_phone.phone_route_store, "get_route", lambda rid: None)
    monkeypatch.setattr(
        ws_phone, "resolve_route_identity",
        lambda route, **kw: RouteIdentity(
            mode="shared", role="viewer",
            external=external_identity.resolve("phone", "", session_id=kw["session_id"], shared=True),
        ),
    )
    monkeypatch.setattr(ws_phone, "_validated_reuse_sid", lambda sid, agent: sid or "")

    async def _fake_build(**kwargs):
        return _FakeAgentCfg()
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _fake_build)

    import core.concurrency as concurrency
    layer.admissions = []  # the kwargs of every acquire the handler made

    async def _fake_acquire(session_id, **kw):
        layer.admissions.append(kw)
        return True
    monkeypatch.setattr(concurrency, "acquire_chat_slot", _fake_acquire)
    layer.rehomed = []  # what a heal asked of its slot, and after how many starts
    layer.rehome_answer = True

    async def _fake_rehome(session_id, **kw):
        layer.rehomed.append({**kw, "after_starts": len(layer.started)})
        return layer.rehome_answer
    monkeypatch.setattr(concurrency, "rehome_slot", _fake_rehome)
    monkeypatch.setattr(concurrency, "release_chat_slot", lambda sid: None)

    monkeypatch.setattr(ws_phone.task_store, "create_chat",
                        lambda *a, **kw: None)
    monkeypatch.setattr(ws_phone.task_store, "update_chat",
                        lambda *a, **kw: None)
    # The conversation's record, kept per chat id for the tests that read it.
    from storage.chat import conversation_store
    layer.records = {}
    monkeypatch.setattr(conversation_store, "create",
                        lambda chat_id, agent, **kw: layer.records.setdefault(chat_id, dict(kw)))
    layer.persisted = []  # (chat_id, role, content) rows the handler wrote
    monkeypatch.setattr(ws_phone.task_store, "add_chat_message",
                        lambda *a, **kw: layer.persisted.append(a))

    monkeypatch.setattr(ws_phone, "ChatStreamPump", FakePump)
    monkeypatch.setattr(ws_phone, "_active_pumps", {})

    return FakeWebSocket(), layer


async def _run_handler(ws):
    return asyncio.create_task(ws_phone.ws_phone_handler(ws))


async def _warmup(ws):
    ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct",
             "phone_mode": True})
    frame = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
    assert frame["type"] == "warmup_ready", frame
    return frame["data"]["session_id"]


def test_turn_frames_are_stamped_with_turn_id(phone_ws_env):
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)

        ws.push({"type": "chat", "prompt": "hello", "turn": 5})
        done = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 5)
        assert done["turn"] == 5

        texts = [f for f in ws.sent if f["type"] == "text"]
        assert texts and all(f["turn"] == 5 for f in texts)

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_abort_cancels_inflight_turn(phone_ws_env):
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)

        layer.gate = asyncio.Event()  # hold generation open after first token
        ws.push({"type": "chat", "prompt": "long question", "turn": 1})
        await ws.wait_for_frame(
            lambda f: f["type"] == "text" and f.get("turn") == 1)

        ws.push({"type": "abort", "turn": 1})
        done = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 1)
        assert done is not None
        assert layer.cancelled == ["long question"]
        # the gated tail never streamed
        tail = [f for f in ws.sent
                if f["type"] == "text" and "tail" in f["data"]["content"]]
        assert not tail

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_abort_graceful_interrupt_leaves_producer_draining(phone_ws_env):
    """When layer.abort() interrupts gracefully (CLI stdin control_request /
    Codex interrupt), the producer is NOT hard-cancelled — the engine closes
    the turn itself and the partial reply drains + persists; the daemon's
    turn-id filter drops the tail. Parity with the duplex chat attach."""
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        sid = await _warmup(ws)

        layer.gate = asyncio.Event()
        layer.abort_result = True
        ws.push({"type": "chat", "prompt": "long question", "turn": 1})
        await ws.wait_for_frame(
            lambda f: f["type"] == "text" and f.get("turn") == 1)

        ws.push({"type": "abort", "turn": 1})
        done = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 1)
        assert done is not None
        assert layer.aborted == [sid]
        assert layer.cancelled == []  # no hard cancel — the turn drained

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_interruption_note_rides_next_dispatch_raw_row_persisted(phone_ws_env):
    """After a barge-in abort on a non-direct layer, the NEXT turn's dispatch
    carries the duplex-style interruption note (with the heard-chars phrase
    when barge_in_chars rides the chat frame) while the DB user row keeps the
    RAW spoken words."""
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)

        layer.gate = asyncio.Event()
        layer.abort_result = True
        ws.push({"type": "chat", "prompt": "tell me a story", "turn": 1})
        await ws.wait_for_frame(
            lambda f: f["type"] == "text" and f.get("turn") == 1)
        ws.push({"type": "abort", "turn": 1})
        await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 1)

        ws.push({"type": "chat", "prompt": "actually the weather",
                 "turn": 2, "barge_in_chars": 17})
        await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 2)

        dispatched = layer.prompts[-1]
        assert dispatched.startswith(
            "[The user interrupted your previous spoken reply")
        assert "heard only the first 17 characters" in dispatched
        assert dispatched.endswith("actually the weather")
        # DB row: raw words only, no note.
        user_rows = [a for a in layer.persisted if a[1] == "user"]
        assert user_rows[-1][2] == "actually the weather"

        # The note is one-shot: a further turn with no new interruption
        # dispatches clean.
        ws.push({"type": "chat", "prompt": "thanks", "turn": 3})
        await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 3)
        assert layer.prompts[-1] == "thanks"

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_chat_during_active_turn_queues_behind_session_lock(phone_ws_env):
    """A follow-up chat while a turn streams (proxy-mode drain overlap) runs
    after the first completes; both keep their own turn ids."""
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)

        layer.gate = asyncio.Event()
        ws.push({"type": "chat", "prompt": "first", "turn": 1})
        await ws.wait_for_frame(
            lambda f: f["type"] == "text" and f.get("turn") == 1)

        # Client abandoned turn 1 (barge-in) and moved on — turn 2 must wait
        # for turn 1's generation to drain, not interleave into it.
        ws.push({"type": "chat", "prompt": "second", "turn": 2})
        await asyncio.sleep(0.05)
        assert layer.prompts == ["first"]  # second not started yet

        layer.gate.set()
        done2 = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 2)
        assert done2 is not None
        assert layer.prompts == ["first", "second"]

        done_turns = [f["turn"] for f in ws.sent if f["type"] == "done"]
        assert done_turns == [1, 2]

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_dead_process_respawns_in_place(phone_ws_env):
    """A turn that finds the CLI process dead (hard abort between turns,
    satellite-side death) respawns under the SAME session id — resume
    checked BEFORE prepare_resume, phone-shape config with resume set — and
    then runs normally instead of erroring into the corpse forever."""
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        sid = await _warmup(ws)
        started_at_warmup = len(layer.started)

        layer.process_dead = True
        ws.push({"type": "chat", "prompt": "still there?", "turn": 3})
        done = await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 3)
        assert done is not None

        # Healed: resumability checked, dead entry cleared, respawned on the
        # same id with resume riding the config.
        assert layer.resume_checked == [sid]
        assert layer.prepared == [sid]
        heals = layer.started[started_at_warmup:]
        assert len(heals) == 1
        heal_sid, heal_cfg = heals[0]
        assert heal_sid == sid
        assert heal_cfg.resume is True
        # And the turn actually dispatched on the healed session.
        assert layer.prompts == ["still there?"]
        errors = [f for f in ws.sent if f["type"] == "error"]
        assert not errors

        # A live process never triggers the heal machinery.
        ws.push({"type": "chat", "prompt": "next", "turn": 4})
        await ws.wait_for_frame(
            lambda f: f["type"] == "done" and f.get("turn") == 4)
        assert layer.resume_checked == [sid]  # unchanged

        ws.disconnect()
        await handler

    asyncio.run(run())


def test_a_call_is_admitted_as_a_phone_session(phone_ws_env):
    """A call takes a slot of its own kind (never evicted,
    outside the per-person cap, counted by the admin gauge as a phone) and
    names its MCP config so a Direct-LLM call reserves for its stdio MCPs;
    an external caller has no owner."""
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        ws.disconnect()
        await handler

    asyncio.run(run())
    adm = layer.admissions[-1]
    assert adm["kind"] == "phone"
    assert adm["mcp_config_path"] == "/tmp/phone-mcp.json"
    assert adm.get("user_sub") is None


def test_a_healed_call_on_an_engine_that_rebuilds_from_rows_keeps_its_conversation(phone_ws_env, monkeypatch):
    """Direct LLM holds a call in memory only: healed after its tools' machine
    went, the session comes back from the chat's rows, less this turn's own,
    which the turn sends again."""
    ws, layer = phone_ws_env
    layer.resume_verdict, layer.rebuilds_from_rows = ResumeVerdict.FRESH, True
    monkeypatch.setattr(ws_phone.task_store, "add_chat_message", lambda *a, **kw: 4711)

    async def run():
        handler = await _run_handler(ws)
        sid = await _warmup(ws)
        started_at_warmup = len(layer.started)
        layer.process_dead = True
        ws.push({"type": "chat", "prompt": "still there?", "turn": 3})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 3)
        [(heal_sid, heal_cfg)] = layer.started[started_at_warmup:]
        assert heal_sid == sid and heal_cfg.resume is False
        assert heal_cfg.chat_id and heal_cfg.extra_env["_RESUME_SKIP_ROW"] == "4711"
        # The rows hold the conversation: no digest is seeded into the prompt.
        assert layer.prompts == ["still there?"]
        ws.disconnect()
        await handler

    asyncio.run(run())


def test_a_healed_call_that_resumes_needs_no_rows(phone_ws_env, monkeypatch):
    ws, layer = phone_ws_env
    monkeypatch.setattr(ws_phone.task_store, "add_chat_message", lambda *a, **kw: 4711)

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        started_at_warmup = len(layer.started)
        layer.process_dead = True
        ws.push({"type": "chat", "prompt": "still there?", "turn": 3})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 3)
        [(_sid, heal_cfg)] = layer.started[started_at_warmup:]
        assert heal_cfg.resume is True and heal_cfg.chat_id == "" and heal_cfg.extra_env == {}
        assert layer.rehomed == []      # its slot was never in question
        ws.disconnect()
        await handler

    asyncio.run(run())


def _heal_a_call(ws, layer, monkeypatch, **before_the_heal):
    """A warmed call on an engine that rebuilds from its rows, healed once; what
    the rebuilt config carries is set after the warm-up. The frames of the turn."""
    layer.resume_verdict, layer.rebuilds_from_rows = ResumeVerdict.FRESH, True
    monkeypatch.setattr(ws_phone.task_store, "add_chat_message", lambda *a, **kw: 4711)

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        layer.started_at_warmup = len(layer.started)
        for name, value in before_the_heal.items():
            setattr(_FakeAgentCfg, name, value)
        layer.process_dead = True
        ws.push({"type": "chat", "prompt": "still there?", "turn": 3})
        await ws.wait_for_frame(lambda f: f["type"] in ("done", "error") and f.get("turn") == 3)
        ws.disconnect()
        await handler

    try:
        asyncio.run(run())
    finally:
        for name in before_the_heal:
            delattr(_FakeAgentCfg, name)


def test_a_healed_call_reserves_on_the_box_of_its_rebuilt_session(phone_ws_env, monkeypatch):
    """The reaper may have released its slot and its tools may land elsewhere
    than the slot names. Asked once the rebuilt session is started, whose
    record is the one admission reads."""
    from core import placement
    ws, layer = phone_ws_env
    compute = SimpleNamespace(placement=placement.from_machine(
        placement.KIND_COMPUTE, {"id": "compute-1", "pairing_scope": "compute"}))
    _heal_a_call(ws, layer, monkeypatch, security_context=compute)
    [asked] = layer.rehomed
    assert asked["kind"] == "phone" and asked["target"] == "compute-1"
    assert asked["execution_path"] == "direct-llm" and asked["mcp_config_path"] == "/tmp/phone-mcp.json"
    assert asked["after_starts"] == layer.started_at_warmup + 1


def test_a_healed_call_with_no_compute_placement_asks_admission_with_its_own_word(phone_ws_env, monkeypatch):
    """A rebuilt config that names no compute machine cannot come out of the
    real builder (it refuses). If one did, the slot would be asked with the
    word admission refuses, never taken on the platform's own box."""
    from core import placement
    ws, layer = phone_ws_env
    _heal_a_call(ws, layer, monkeypatch)
    assert layer.rehomed[0]["target"] == placement.NO_COMPUTE


def test_a_refused_reservation_leaves_the_healed_call_running(phone_ws_env, monkeypatch):
    """A caller is on the line: no room for a slot is the platform's bookkeeping
    and never ends the turn."""
    from core.concurrency import Admission
    ws, layer = phone_ws_env
    layer.rehome_answer = Admission(False, "busy", "Too many active sessions.")
    _heal_a_call(ws, layer, monkeypatch)
    assert len(layer.rehomed) == 1
    assert layer.prompts == ["still there?"]
    assert not [f for f in ws.sent if f["type"] == "error"]
    assert [f for f in ws.sent if f["type"] == "done" and f.get("turn") == 3]


def test_a_call_with_its_tools_on_a_compute_machine_reserves_on_its_box(phone_ws_env, monkeypatch):
    """A Direct LLM call's tools run on the compute machine: the slot names
    that machine, not the platform."""
    from core import placement
    ws, layer = phone_ws_env
    _FakeAgentCfg.security_context = SimpleNamespace(placement=placement.from_machine(
        placement.KIND_COMPUTE, {"id": "compute-1", "pairing_scope": "compute"}))

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        ws.disconnect()
        await handler

    try:
        asyncio.run(run())
    finally:
        del _FakeAgentCfg.security_context
    assert layer.admissions[-1]["target"] == "compute-1"


def test_a_user_route_call_is_owned_by_its_tied_user(phone_ws_env, monkeypatch):
    ws, layer = phone_ws_env
    from services.phone.phone_identity import RouteIdentity
    monkeypatch.setattr(
        ws_phone, "resolve_route_identity",
        lambda route, **kw: RouteIdentity(
            mode="user", role="viewer", external=None,
            user={"sub": "user-tied", "username": "tied"}, user_role="editor",
        ),
    )

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        ws.disconnect()
        await handler

    asyncio.run(run())
    assert layer.admissions[-1]["user_sub"] == "user-tied"


def test_a_healed_user_route_call_stays_owned_by_its_tied_user(phone_ws_env, monkeypatch):
    """A slot the reaper released is taken again for the same person: the
    count of what a person holds reads it."""
    from services.phone.phone_identity import RouteIdentity
    ws, layer = phone_ws_env
    monkeypatch.setattr(
        ws_phone, "resolve_route_identity",
        lambda route, **kw: RouteIdentity(
            mode="user", role="viewer", external=None,
            user={"sub": "user-tied", "username": "tied"}, user_role="editor",
        ),
    )
    _heal_a_call(ws, layer, monkeypatch)
    assert layer.rehomed[0]["user_sub"] == "user-tied"


def test_the_user_row_and_the_title_ride_the_chat_writer(phone_ws_env, monkeypatch):
    """A call's user row and first title land on the chat's writer
    lane, off the loop, before the turn runs."""
    ws, layer = phone_ws_env
    from core.events import chat_writer
    labels: list[str] = []
    orig = chat_writer.submit

    def _submit(chat_id, job, *, label=""):
        labels.append(label)
        return orig(chat_id, job, label=label)
    monkeypatch.setattr(chat_writer, "submit", _submit)

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        ws.push({"type": "chat", "prompt": "hello there", "turn": 1})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        ws.disconnect()
        await handler

    asyncio.run(run())
    assert labels[:2] == ["phone_user_row", "phone_title"]
    assert [a for a in layer.persisted if a[1] == "user"][-1][2] == "hello there"


def test_a_turn_waits_for_another_starter_and_says_when_the_chat_stays_busy(
        phone_ws_env, monkeypatch):
    """The chat's turn starters take turns: a caller's turn waits a moment
    for another starter bringing the chat a turn (a stored wake's replay),
    then answers busy, with no row and no dispatch."""
    from core.events import input_queue
    ws, layer = phone_ws_env
    monkeypatch.setattr(ws_phone, "_CLAIM_WAIT_S", 0.2)

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        ws.push({"type": "chat", "prompt": "first", "turn": 1})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        chat_id = layer.persisted[0][0]
        q = input_queue.get(chat_id)
        held = q.try_claim()
        assert held is not None
        ws.push({"type": "chat", "prompt": "second", "turn": 2})
        err = await ws.wait_for_frame(lambda f: f["type"] == "error" and f.get("turn") == 2)
        assert "busy" in err["data"]["message"]
        assert layer.prompts == ["first"]
        assert [r[2] for r in layer.persisted] == ["first"]
        q.release(held)
        ws.push({"type": "chat", "prompt": "third", "turn": 3})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 3)
        assert layer.prompts == ["first", "third"]
        assert not q.claimed
        ws.disconnect()
        await handler
        input_queue._registry.pop(chat_id, None)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The layer follows the built config (a call on the compute machine)
# ---------------------------------------------------------------------------

def _on_compute(machine: str = "compute-1"):
    from core import placement
    return SimpleNamespace(placement=placement.from_machine(
        placement.KIND_COMPUTE, {"id": machine, "pairing_scope": "compute"}))


@contextlib.contextmanager
def _built_as(**attrs):
    """What every config the handler builds carries, for one block."""
    missing = object()
    before = {name: _FakeAgentCfg.__dict__.get(name, missing) for name in attrs}
    for name, value in attrs.items():
        setattr(_FakeAgentCfg, name, value)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is missing:
                delattr(_FakeAgentCfg, name)
            else:
                setattr(_FakeAgentCfg, name, value)


def _two_places(monkeypatch, layer):
    """The compute machine's layer beside the fixture's own, the platform's:
    the handler gets it only for a config placed there. Returns it with every
    ask of a layer, in order."""
    compute = FakeLayer()
    asks: list[dict] = []

    def _pick(name, execution_target=None, **kw):
        asks.append({"target": execution_target, **kw})
        return compute if kw.get("placement_built") is not None else layer
    monkeypatch.setattr(ws_phone, "get_execution_layer", _pick)
    return compute, asks


def test_a_call_built_for_the_compute_machine_runs_on_its_layer(phone_ws_env, monkeypatch):
    """A Claude Code or Codex call whose config the builder placed on the
    compute machine starts, runs its turns and is admitted there: the layer
    is read off the built config, never picked from the target before it."""
    ws, layer = phone_ws_env
    compute, asks = _two_places(monkeypatch, layer)
    where = _on_compute()
    order: list[str] = []
    built = ws_phone.build_phone_agent_config

    async def _build(**kw):
        order.append("build")
        return await built(**kw)
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _build)
    seen: dict = {}

    async def run():
        handler = await _run_handler(ws)
        seen["sid"] = await _warmup(ws)
        order.append("ready")
        ws.push({"type": "chat", "prompt": "hello", "turn": 1})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        ws.disconnect()
        await handler

    with _built_as(execution_path="claude-code-cli", security_context=where):
        asyncio.run(run())
    assert [sid for sid, _cfg in compute.started] == [seen["sid"]] and layer.started == []
    assert compute.prompts == ["hello"] and layer.prompts == []
    assert asks == [{"target": None, "placement_built": where.placement}]
    assert order == ["build", "ready"]
    assert layer.admissions[-1]["target"] == "compute-1"


def test_a_call_with_no_compute_machine_is_refused_at_its_warmup(phone_ws_env, monkeypatch):
    """The platform runs no session itself: the call's build is refused, the
    daemon gets the one sentence at the warm-up, nothing starts anywhere, no
    slot is asked for, and a turn sent anyway ends with the same reason."""
    from core import placement
    from core.events import turn_ending
    ws, layer = phone_ws_env
    compute, asks = _two_places(monkeypatch, layer)

    async def _refused(**kw):
        raise placement.NoComputeMachine()
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _refused)

    async def run():
        handler = await _run_handler(ws)
        ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True})
        frame = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert frame == {"type": "error", "data": {"message": placement.NO_COMPUTE_SENTENCE,
                                                   "say": turn_ending.SPOKEN_REFUSED}}
        ws.push({"type": "chat", "prompt": "hello", "turn": 1})
        await ws.wait_for_frame(lambda f: f["type"] == "error" and f.get("turn") == 1
                                and f["data"]["message"] == placement.NO_COMPUTE_SENTENCE)
        ws.disconnect()
        await handler

    asyncio.run(run())
    assert layer.started == [] and compute.started == [] and asks == [] and layer.admissions == []


@pytest.mark.parametrize("why", ["a full server", "no compute machine", "a start that failed"])
def test_a_caller_whose_call_could_not_start_hears_one_sentence_at_the_first_turn(
        phone_ws_env, monkeypatch, why):
    """Admission refused the call's start, no compute machine could take it,
    or its start failed: the phone service gets the platform's reason at the
    warm-up, which it logs. The caller's first turn then ends with a frame of
    its own turn (the phone service reads a turn's frames only) that carries
    what the caller hears: one polite sentence, the same for every reason.
    The reason stays in the frame's message and is never what is spoken."""
    from core import concurrency, placement
    from core.concurrency import Admission
    from core.events import turn_ending
    ws, layer = phone_ws_env
    reason = {"a full server": "Too many active sessions. Try again in a few minutes.",
              "no compute machine": placement.NO_COMPUTE_SENTENCE,
              "a start that failed": "Warmup failed: the engine did not start"}[why]
    if why == "a full server":
        async def _refuse(session_id, **kw):
            return Admission(False, "busy", reason)
        monkeypatch.setattr(concurrency, "acquire_chat_slot", _refuse)
    elif why == "no compute machine":
        async def _refused(**kw):
            raise placement.NoComputeMachine()
        monkeypatch.setattr(ws_phone, "build_phone_agent_config", _refused)
    else:
        # The start itself fails, and fails again when the first turn tries it.
        async def _fails(sid, cfg):
            raise RuntimeError("the engine did not start")
        monkeypatch.setattr(layer, "start_session", _fails)
        layer.process_dead = True

    async def run():
        handler = await _run_handler(ws)
        ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True})
        warm = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert warm["type"] == "error" and warm["data"]["message"] == reason and "turn" not in warm
        assert warm["data"]["say"] == turn_ending.SPOKEN_REFUSED
        ws.push({"type": "chat", "prompt": "hello?", "turn": 1})
        said = await ws.wait_for_frame(lambda f: f["type"] == "error" and f is not warm)
        assert said["turn"] == 1 and said["data"]["say"] == turn_ending.SPOKEN_REFUSED
        if why != "a start that failed":
            assert said["data"]["message"] == reason
        assert not [f for f in ws.sent if f["type"] in ("text", "done")]
        ws.disconnect()
        await handler

    asyncio.run(run())
    assert layer.prompts == [], "no turn ran"
    assert turn_ending.SPOKEN_REFUSED == ("Sorry, I cannot take the call right now. "
                                          "Please call again in a few minutes.")
    for word in ("session", "memory", "compute", "OtoDock", "machine"):
        assert word not in turn_ending.SPOKEN_REFUSED


def test_a_turn_with_no_warmup_at_all_ends_as_its_own_turn(phone_ws_env):
    """A phone service that sends a turn before any warm-up: the refusal is
    a frame of that turn, so the turn ends, and it carries no line of the
    platform's (nothing was refused: the caller hears the service's own
    apology)."""
    ws, layer = phone_ws_env

    async def run():
        handler = await _run_handler(ws)
        ws.push({"type": "chat", "prompt": "hello?", "turn": 4})
        frame = await ws.wait_for_frame(lambda f: f["type"] == "error")
        assert frame == {"type": "error", "turn": 4, "data": {"message": "No session — send warmup first"}}
        ws.disconnect()
        await handler

    asyncio.run(run())


def test_a_prewarmed_call_is_taken_from_the_layer_that_holds_it(phone_ws_env, monkeypatch):
    """The session a pre-warm started is found on whichever layer holds it,
    the compute machine's included: no second start, and the turns run there."""
    from core.session.session_manager import LiveSession
    ws, layer = phone_ws_env
    compute, asks = _two_places(monkeypatch, layer)
    held: dict = {}
    monkeypatch.setattr(ws_phone, "live_session",
                        lambda sid: LiveSession(held[sid]) if sid in held else None)
    monkeypatch.setattr(ws_phone.task_store, "get_chat_by_session", lambda sid: {"id": "chat-prewarmed"})

    async def run():
        first = await _run_handler(ws)
        sid = await _warmup(ws)
        held[sid] = compute
        again = FakeWebSocket()
        second = await _run_handler(again)
        again.push({"type": "warmup", "model": "unified", "llm_mode": "direct",
                    "phone_mode": True, "session_id": sid})
        frame = await again.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert frame["type"] == "warmup_ready" and frame["data"]["session_id"] == sid
        again.push({"type": "chat", "prompt": "hello", "turn": 1})
        await again.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        # An id nothing holds starts a fresh session instead.
        gone = FakeWebSocket()
        third = await _run_handler(gone)
        gone.push({"type": "warmup", "model": "unified", "llm_mode": "direct",
                   "phone_mode": True, "session_id": "not-held-anywhere"})
        fresh = await gone.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert fresh["type"] == "warmup_ready" and fresh["data"]["session_id"] != "not-held-anywhere"
        for sock, task in ((gone, third), (again, second), (ws, first)):
            sock.disconnect()
            await task

    with _built_as(execution_path="claude-code-cli", security_context=_on_compute()):
        asyncio.run(run())
    assert len(compute.started) == 2          # the pre-warm and the fresh one, never the reuse
    assert compute.prompts == ["hello"] and layer.prompts == []
    assert len(asks) == 2                     # the reuse resolved no layer of its own


def test_a_heal_with_no_compute_machine_ends_the_turn_with_the_sentence(phone_ws_env, monkeypatch):
    """The call's engine died and no compute machine is left to start it
    again: the rebuild is refused, the turn ends with the one sentence,
    nothing is started and the slot is not moved. The call's next turn asks
    again."""
    from core import placement
    from core.events import turn_ending
    ws, layer = phone_ws_env
    compute, _asks = _two_places(monkeypatch, layer)
    built = ws_phone.build_phone_agent_config
    state = {"warmed": False}

    async def _build(**kw):
        if state["warmed"]:
            raise placement.NoComputeMachine()
        return await built(**kw)
    monkeypatch.setattr(ws_phone, "build_phone_agent_config", _build)

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        state["warmed"] = True
        compute.process_dead = True
        ws.push({"type": "chat", "prompt": "still there?", "turn": 3})
        frame = await ws.wait_for_frame(lambda f: f["type"] in ("done", "error") and f.get("turn") == 3)
        assert frame["type"] == "error" and frame["data"]["message"] == placement.NO_COMPUTE_SENTENCE
        # What the caller hears: nothing can take the call now.
        assert frame["data"]["say"] == turn_ending.SPOKEN_REFUSED
        ws.disconnect()
        await handler

    with _built_as(execution_path="claude-code-cli", security_context=_on_compute()):
        asyncio.run(run())
    assert len(compute.started) == 1 and layer.started == [] and layer.rehomed == []
    assert compute.prompts == [] and layer.prompts == []


def test_a_healed_call_that_stays_on_the_compute_machine_keeps_its_slot(phone_ws_env, monkeypatch):
    ws, layer = phone_ws_env
    compute, _asks = _two_places(monkeypatch, layer)

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        compute.process_dead = True
        ws.push({"type": "chat", "prompt": "still there?", "turn": 3})
        await ws.wait_for_frame(lambda f: f["type"] in ("done", "error") and f.get("turn") == 3)
        ws.disconnect()
        await handler

    with _built_as(execution_path="claude-code-cli", security_context=_on_compute()):
        asyncio.run(run())
    assert len(compute.started) == 2 and layer.started == []
    assert compute.prompts == ["still there?"] and layer.rehomed == []


class _ReconnectingLayer(FakeLayer):
    """A compute machine's layer whose link dropped: its session is held in
    the reconnect grace, reads dead to a probe, and a turn sent meanwhile is
    refused at once, as the real link refuses it. Back after ``back_after``
    waits, never when that is None."""

    def __init__(self, back_after: int | None):
        super().__init__()
        self.linked = True
        self.back_after = back_after
        self.waits = 0

    def drop(self) -> None:
        self.linked = False

    def is_session_grace_held(self, session_id: str) -> bool:
        return not self.linked

    async def is_session_alive(self, session_id: str) -> bool:
        return self.linked

    async def is_session_process_dead(self, session_id: str) -> bool:
        return not self.linked or self.process_dead

    async def wait_session_reconnect(self, session_id: str, *, timeout: float = 10.0) -> bool:
        self.waits += 1
        if self.back_after is not None and self.waits >= self.back_after:
            self.linked = True
        return self.linked

    async def send_message(self, session_id: str, prompt: str, **kwargs):
        if not self.linked:
            raise RuntimeError("Satellite compute-1 not connected")
        async for event in super().send_message(session_id, prompt, **kwargs):
            yield event


def _a_call_whose_machine_drops(ws, monkeypatch, layer, compute):
    """The handler reads the session's holder as the platform does: the
    compute layer, its machine's link and its grace."""
    from core.session.session_manager import LiveSession
    monkeypatch.setattr(ws_phone, "get_execution_layer",
                        lambda name, execution_target=None, **kw: compute
                        if kw.get("placement_built") is not None else layer)
    monkeypatch.setattr(ws_phone, "live_session", lambda sid: LiveSession(
        compute, "compute-1", linked=compute.linked, grace=compute.is_session_grace_held(sid)))

    async def run():
        handler = await _run_handler(ws)
        await _warmup(ws)
        compute.drop()
        ws.push({"type": "chat", "prompt": "are you there?", "turn": 2})
        await ws.wait_for_frame(lambda f: f["type"] in ("done", "error") and f.get("turn") == 2)
        ws.disconnect()
        await handler

    with _built_as(execution_path="claude-code-cli", security_context=_on_compute()):
        asyncio.run(run())


def test_a_turn_waits_while_the_calls_compute_machine_reconnects(phone_ws_env, monkeypatch):
    """The compute machine's link drops between two turns and is back within
    the wait: the turn waits for it and the caller hears the answer, never the
    error line. Nothing is healed: the session lived through it."""
    ws, layer = phone_ws_env
    compute = _ReconnectingLayer(back_after=1)
    _a_call_whose_machine_drops(ws, monkeypatch, layer, compute)
    assert compute.waits == 1 and compute.prompts == ["are you there?"]
    assert not [f for f in ws.sent if f["type"] == "error"]
    assert len(compute.started) == 1 and compute.prepared == [] and layer.started == []


def test_a_machine_away_past_the_wait_fails_the_turn_and_heals_nothing(phone_ws_env, monkeypatch):
    """Still away after the wait: the session is reconnecting, not dead, so
    nothing is rebuilt in its place, and the turn fails as it reaches the link."""
    ws, layer = phone_ws_env
    compute = _ReconnectingLayer(back_after=None)
    _a_call_whose_machine_drops(ws, monkeypatch, layer, compute)
    assert compute.waits == 1 and compute.prompts == []
    assert [f for f in ws.sent if f["type"] == "error" and f.get("turn") == 2]
    assert len(compute.started) == 1 and compute.prepared == [] and layer.started == []


def test_a_healed_call_rebuilt_for_another_compute_machine_moves_its_slot_there(phone_ws_env, monkeypatch):
    """Two compute machines share the folders and the rebuilt config names
    the other one: the layer is the same, the box is not, so the slot moves.
    The heal reads its layer off the rebuilt config, as the warm-up read its
    own: where a session runs is decided by the build, once per build."""
    ws, layer = phone_ws_env
    compute, asks = _two_places(monkeypatch, layer)

    async def run():
        handler = await _run_handler(ws)
        with _built_as(execution_path="claude-code-cli", security_context=_on_compute("compute-1")):
            await _warmup(ws)
        with _built_as(execution_path="claude-code-cli", security_context=_on_compute("compute-2")):
            compute.process_dead = True
            ws.push({"type": "chat", "prompt": "still there?", "turn": 3})
            await ws.wait_for_frame(lambda f: f["type"] in ("done", "error") and f.get("turn") == 3)
        ws.disconnect()
        await handler

    asyncio.run(run())
    assert len(compute.started) == 2 and layer.started == []
    assert layer.admissions[-1]["target"] == "compute-1"
    assert [asked["target"] for asked in layer.rehomed] == ["compute-2"]
    assert compute.prompts == ["still there?"]
    assert [ask["placement_built"].machine_id for ask in asks if ask.get("placement_built") is not None][-1] == "compute-2"


def test_a_refused_layer_ends_the_warmup_and_gives_back_what_it_took(phone_ws_env, monkeypatch):
    """The target's machine is offline: the daemon hears it, the build's seat
    goes back, no slot was asked for, and the socket's end still frees the
    call's id (its slot and its caller's scratch tree)."""
    import core.concurrency as concurrency
    from core.config import config_builder
    ws, layer = phone_ws_env

    def _refuse(name, execution_target=None, **kw):
        raise RuntimeError("Agent 'unified' targets remote machine abcd1234 which is offline.")
    monkeypatch.setattr(ws_phone, "get_execution_layer", _refuse)
    seats: list[str] = []
    freed: list[str] = []
    monkeypatch.setattr(config_builder, "release_config_seat", lambda sid, cfg: seats.append(sid))
    monkeypatch.setattr(concurrency, "release_chat_slot", freed.append)

    async def run():
        handler = await _run_handler(ws)
        ws.push({"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True})
        frame = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert frame["type"] == "error" and "which is offline" in frame["data"]["message"]
        ws.push({"type": "chat", "prompt": "hello", "turn": 1})
        err = await ws.wait_for_frame(lambda f: f["type"] == "error" and f.get("turn") == 1)
        assert "which is offline" in err["data"]["message"] and err["data"]["say"]
        ws.disconnect()
        await handler

    asyncio.run(run())
    assert len(seats) == 1 and layer.admissions == [] and layer.started == []
    assert freed and set(freed) == set(seats)


def test_an_outside_calls_place_is_asked_at_its_own_role_with_no_person(phone_ws_env, monkeypatch):
    """Where the call's layer comes from when its config names no compute
    machine: the target the resolver gives the identity's own role and no
    person, which never is an admin's machine or a person's. A route tied to
    a person is asked at that person's role, for that person."""
    from services.phone.phone_identity import RouteIdentity
    ws, layer = phone_ws_env
    asked: list[dict] = []
    monkeypatch.setattr(ws_phone, "resolve_phone_execution_target",
                        lambda name, **kw: asked.append(kw))

    async def run():
        sock = FakeWebSocket()
        handler = await _run_handler(sock)
        await _warmup(sock)
        sock.disconnect()
        await handler

    asyncio.run(run())
    assert asked == [{"role": "viewer", "user_sub": None}]
    asked.clear()
    monkeypatch.setattr(
        ws_phone, "resolve_route_identity",
        lambda route, **kw: RouteIdentity(
            mode="user", role="viewer", external=None,
            user={"sub": "user-tied", "username": "tied"}, user_role="editor",
        ),
    )
    asyncio.run(run())
    assert asked == [{"role": "editor", "user_sub": "user-tied"}]
    assert layer.started and not ws.sent
