"""core.session.session_delivery — rung selection + the one-shot dual-writer guard.

The ladder must prefer a live interactive PTY over everything (the WS handler's
synthesis turn dead-ends for PTY sessions; a --resume one-shot would fork the
live TUI's transcript), keep the WS rung's persistence asymmetry (the dashboard
handler persists, the module doesn't), persist exactly once on every other
path, and refuse the one-shot while any live PTY / in-flight warmup holds the
chat.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/session/test_session_delivery.py -q
"""
import asyncio
import contextlib
import time

# The ws rung has a LIVENESS GATE: queue presence alone does not route, the
# target session must be alive. Tests that exercise the ws route plant a live
# session the platform holds alongside their notify queue.
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.session import (
    interactive_session as isess,
    session_delivery as delivery,
    warmup_registry,
)
from core.session.session_delivery import deliver_prompt, oneshot_inflight
from core.session.session_state import _dashboard_notify_queues, set_pump_callbacks
from storage import database as task_store


@contextmanager
def _alive_session(sid):
    """A live session in the pool the platform holds itself (Direct LLM's):
    on no machine, so it is on its chat's place whatever the chat's pin."""
    from core.layers.direct import session as _direct
    _direct._direct_sessions[sid] = SimpleNamespace(mcp_manager=None)
    try:
        yield
    finally:
        _direct._direct_sessions.pop(sid, None)


class _FakeISession:
    """The slice of InteractiveSession the ladder touches: registry identity,
    liveness, and queue_prompt. Registered straight into the module registry so
    find_live_for_chat/get see it."""

    def __init__(self, session_id, chat_id, *, alive=True, accept=True):
        self.session_id = session_id
        self.chat_id = chat_id
        self.alive = alive
        self.created_at = time.monotonic()
        self.accept = accept
        self.queued: list[dict] = []

    def queue_prompt(self, text, source, **context):
        if not self.accept:
            return False
        self.queued.append({"text": text, "source": source, **context})
        return True


def _install(fake):
    isess._sessions[fake.session_id] = fake


def _cleanup():
    isess._sessions.clear()
    set_pump_callbacks(None, None, None)


class TestRungSelection:
    def test_pty_rung_beats_notify_queue(self, temp_db):
        try:
            task_store.create_chat("chat-p1", "user-1", "pa")
            fake = _FakeISession("sid-p1", "chat-p1")
            _install(fake)
            q: asyncio.Queue = asyncio.Queue()
            _dashboard_notify_queues["sid-p1"] = q
            persists = []
            try:
                outcome = asyncio.run(deliver_prompt(
                    "chat-p1", "the result", source="delegate_result",
                    session_id="sid-p1", agent="pa",
                    notify_payload={"type": "task_result_prompt"},
                    persist_event=persists.append, role="manager",
                ))
                assert outcome.path == "pty"
                assert outcome.session_id == "sid-p1"
                assert q.empty()                       # WS rung never reached
                assert persists == ["chat-p1"]         # persisted at enqueue
                assert len(fake.queued) == 1
                item = fake.queued[0]
                assert item["text"] == "the result"
                assert item["chat_id"] == "chat-p1"    # full re-delivery context
            finally:
                _dashboard_notify_queues.pop("sid-p1", None)
        finally:
            _cleanup()

    def test_ws_rung_does_not_persist(self, temp_db):
        try:
            task_store.create_chat("chat-w1", "user-1", "pa")
            q: asyncio.Queue = asyncio.Queue()
            _dashboard_notify_queues["sid-w1"] = q
            persists = []
            try:
                with _alive_session("sid-w1"):
                    outcome = asyncio.run(deliver_prompt(
                        "chat-w1", "the result", source="delegate_result",
                        session_id="sid-w1", agent="pa",
                        notify_payload={"type": "task_result_prompt"},
                        persist_event=persists.append, role="manager",
                    ))
                assert outcome.path == "ws"
                assert persists == []                  # handler owns persistence
                payload = q.get_nowait()
                assert payload["session_id"] == "sid-w1"
                assert payload["chat_id"] == "chat-w1"
            finally:
                _dashboard_notify_queues.pop("sid-w1", None)
        finally:
            _cleanup()

    @pytest.mark.parametrize("pin, path", [
        ("m-1", "ws"),        # the chat's own machine holds it
        ("local", "none"),    # a move to the platform whose close failed
        ("m-2", "none"),      # a move to another machine whose close failed
    ])
    def test_ws_rung_serves_a_chat_its_own_machine_holds(self, temp_db, monkeypatch, pin, path):
        """The socket rung asks the layer that HOLDS the session: a chat on a
        machine is the remote layer's, which the engine's own layer never
        holds, so it used to skip the socket and take the ladder. A machine
        holding it elsewhere than the chat's place still takes the ladder:
        the handler would run the turn on the chat's place."""
        from core.session import session_manager

        class _Remote:
            def owns_session(self, sid):
                return sid == "sid-w3"

            def machine_of_session(self, sid):
                return "m-1"

            def is_session_grace_held(self, sid):
                return False

            async def is_session_alive(self, sid):
                return sid == "sid-w3"

        manager = type("M", (), {"is_connected": staticmethod(lambda mid: mid == "m-1"),
                                 "is_compute": staticmethod(lambda mid: False)})()
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: manager)
        monkeypatch.setattr(session_manager, "_remote_layer", _Remote())
        try:
            task_store.create_chat("chat-w3", "user-1", "pa")
            task_store.update_chat("chat-w3", execution_target=pin)
            q: asyncio.Queue = asyncio.Queue()
            _dashboard_notify_queues["sid-w3"] = q
            try:
                outcome = asyncio.run(deliver_prompt(
                    "chat-w3", "the result", source="delegate_result",
                    session_id="sid-w3", agent="pa",
                    notify_payload={"type": "task_result_prompt"},
                    persist_event=lambda _c: None, role="manager",
                ))
                assert (outcome.path == "ws") is (path == "ws")
                assert q.empty() is (path != "ws")
            finally:
                _dashboard_notify_queues.pop("sid-w3", None)
        finally:
            _cleanup()

    def test_a_platform_holder_behind_a_machines_pin_takes_the_socket(self, temp_db, monkeypatch):
        """The platform holds the session behind a machine's pin. It is no
        machine's "elsewhere", so the socket serves it."""
        from core.session import session_manager
        monkeypatch.setattr(session_manager, "_remote_layer", None)
        try:
            task_store.create_chat("chat-f1", "user-1", "pa")
            task_store.update_chat("chat-f1", execution_target="m-1")
            q: asyncio.Queue = asyncio.Queue()
            _dashboard_notify_queues["sid-f1"] = q
            try:
                with _alive_session("sid-f1"):
                    outcome = asyncio.run(deliver_prompt(
                        "chat-f1", "the result", source="delegate_result", session_id="sid-f1", agent="pa",
                        notify_payload={"type": "task_result_prompt"}, persist_event=lambda _c: None,
                        role="manager"))
            finally:
                _dashboard_notify_queues.pop("sid-f1", None)
            assert outcome.path == "ws" and q.get_nowait()["session_id"] == "sid-f1"
        finally:
            _cleanup()

    def test_ws_rung_skipped_for_dead_session(self, temp_db):
        """Liveness gate (2026-09-02): a notify queue whose session is dead or
        reaped must NOT take the ws route — the dashboard handler could not run
        a turn on it (the 18:56 no-resume bug). The ladder runs instead and,
        with no other rung available here, persists once and stores the
        durable wake."""
        try:
            task_store.create_chat("chat-w2", "user-1", "pa")
            q: asyncio.Queue = asyncio.Queue()
            _dashboard_notify_queues["sid-w2"] = q
            persists = []
            try:
                outcome = asyncio.run(deliver_prompt(
                    "chat-w2", "the result", source="delegate_result",
                    session_id="sid-w2", agent="pa",
                    notify_payload={"type": "task_result_prompt"},
                    persist_event=persists.append, role="manager",
                ))
                assert outcome.path != "ws"
                assert q.empty()                    # nothing routed to the socket
                assert persists == ["chat-w2"]      # ladder persisted exactly once
            finally:
                _dashboard_notify_queues.pop("sid-w2", None)
        finally:
            _cleanup()

    def test_pump_rung_persists_once(self, temp_db):
        try:
            task_store.create_chat("chat-m1", "user-1", "pa")
            pumped = []
            set_pump_callbacks(
                lambda cid, ev: True,
                lambda cid, text, system: pumped.append((cid, text, system)) or True,
            )
            persists = []
            outcome = asyncio.run(deliver_prompt(
                "chat-m1", "the result", source="delegate_result",
                session_id="sid-m1", agent="pa",
                persist_event=persists.append, role="manager",
            ))
            assert outcome.path == "pump"
            assert persists == ["chat-m1"]
            assert pumped == [("chat-m1", "the result", True)]
        finally:
            _cleanup()

    def test_failed_pty_queue_falls_through_single_persist(self, temp_db):
        # The session dies between lookup and queue → the ladder falls through
        # WITHOUT a pre-persisted event, then persists exactly once on the
        # headless path.
        try:
            task_store.create_chat("chat-f1", "user-1", "pa")
            fake = _FakeISession("sid-f1", "chat-f1", accept=False)
            _install(fake)
            persists = []

            async def _oneshot(sid, agent, text, **kw):
                return "LATE ECHO"

            outcome = asyncio.run(deliver_prompt(
                "chat-f1", "the result", source="delegate_result",
                session_id="sid-f1", agent="pa",
                persist_event=persists.append,
                oneshot_fn=_oneshot, role="manager",
            ))
            # The fake is still "alive" in the registry, so the one-shot guard
            # refuses (correct — a real racing session would own the JSONL).
            assert outcome.path == "none"
            assert persists == ["chat-f1"]             # exactly once
        finally:
            _cleanup()


class TestSteerRung:
    """Rung 3a: a live pump + a steer-capable engine takes the prompt INTO the
    running turn (exactly-once on accept — never also queued); rejection falls
    back to the post-turn pump queue."""

    class _Pump:
        session_id = "sid-st1"
        is_done = False
        source_type = "chat"

    class _Layer:
        def __init__(self, accept):
            self.accept = accept
            self.calls = []

        async def steer(self, sid, text):
            self.calls.append((sid, text))
            return self.accept

    def _arm(self, monkeypatch, accept):
        from core.events import stream_pump
        from core.session import session_delivery
        layer = self._Layer(accept)
        stream_pump._active_pumps["chat-st1"] = self._Pump()

        # The chat's OWN layer (remote-aware); the rows here carry no target,
        # so the resolver's answer is scripted.
        async def _chat_layer(chat):
            return layer
        monkeypatch.setattr(session_delivery, "chat_layer", _chat_layer)
        return layer

    def _disarm(self):
        from core.events import stream_pump
        from core.session import sibling_awareness
        stream_pump._active_pumps.pop("chat-st1", None)
        # The sibling-awareness snapshot is TTL-cached (2s): a snapshot built
        # while the fake pump was live would leak a "chat-st1 (generating)"
        # prelude into a LATER test's deliver_prompt in the same worker.
        sibling_awareness._snapshot = {"ts": 0.0, "lanes": {}, "tasks": []}
        _cleanup()

    def test_steer_accept_beats_pump_queue(self, temp_db, monkeypatch):
        try:
            task_store.create_chat("chat-st1", "user-1", "pa")
            layer = self._arm(monkeypatch, accept=True)
            pumped = []
            set_pump_callbacks(
                lambda cid, ev: True,
                lambda cid, text, system: pumped.append((cid, text)) or True,
            )
            persists = []
            outcome = asyncio.run(deliver_prompt(
                "chat-st1", "steer me", source="delegate_result",
                session_id="sid-st1", agent="pa",
                persist_event=persists.append, role="manager",
            ))
            assert outcome.path == "steer"
            assert outcome.session_id == "sid-st1"
            assert layer.calls == [("sid-st1", "steer me")]
            assert persists == ["chat-st1"]   # exactly once, before the steer
            assert pumped == []               # accepted steer never also queues
        finally:
            self._disarm()

    def test_steer_reject_falls_back_to_pump_queue(self, temp_db, monkeypatch):
        try:
            task_store.create_chat("chat-st1", "user-1", "pa")
            layer = self._arm(monkeypatch, accept=False)
            pumped = []
            set_pump_callbacks(
                lambda cid, ev: True,
                lambda cid, text, system: pumped.append((cid, text)) or True,
            )
            persists = []
            outcome = asyncio.run(deliver_prompt(
                "chat-st1", "steer me", source="delegate_result",
                session_id="sid-st1", agent="pa",
                persist_event=persists.append, role="manager",
            ))
            assert outcome.path == "pump"
            assert layer.calls == [("sid-st1", "steer me")]
            assert persists == ["chat-st1"]   # still exactly once
            assert pumped == [("chat-st1", "steer me")]
        finally:
            self._disarm()

    def test_done_pump_skips_steer(self, temp_db, monkeypatch):
        try:
            task_store.create_chat("chat-st1", "user-1", "pa")
            layer = self._arm(monkeypatch, accept=True)
            from core.events import stream_pump
            stream_pump._active_pumps["chat-st1"].is_done = True
            outcome = asyncio.run(deliver_prompt(
                "chat-st1", "steer me", source="delegate_result",
                session_id="sid-st1", agent="pa", role="manager",
            ))
            assert layer.calls == []
            assert outcome.path in ("none", "pump")
        finally:
            self._Pump.is_done = False
            self._disarm()

    def test_meeting_pump_keeps_the_queue(self, temp_db, monkeypatch):
        # Only a chat's own turn or a task lane takes a steer — a meeting
        # turn's pump keeps the post-turn queue, like the dashboard branch.
        try:
            task_store.create_chat("chat-st1", "user-1", "pa")
            layer = self._arm(monkeypatch, accept=True)
            from core.events import stream_pump
            stream_pump._active_pumps["chat-st1"].source_type = "meeting"
            pumped = []
            set_pump_callbacks(
                lambda cid, ev: True,
                lambda cid, text, system: pumped.append((cid, text)) or True,
            )
            outcome = asyncio.run(deliver_prompt(
                "chat-st1", "steer me", source="delegate_result",
                session_id="sid-st1", agent="pa", role="manager",
            ))
            assert layer.calls == []
            assert outcome.path == "pump"
            assert pumped == [("chat-st1", "steer me")]
        finally:
            self._Pump.source_type = "chat"
            self._disarm()

    def test_unresolvable_layer_falls_back_to_pump_queue(self, temp_db,
                                                         monkeypatch):
        # A chat whose layer cannot be resolved (its target machine is gone)
        # is queued, never dropped.
        try:
            task_store.create_chat("chat-st1", "user-1", "pa")
            from core.events import stream_pump
            from core.session import session_delivery
            stream_pump._active_pumps["chat-st1"] = self._Pump()

            async def _none(chat):
                return None
            monkeypatch.setattr(session_delivery, "chat_layer", _none)
            pumped = []
            set_pump_callbacks(
                lambda cid, ev: True,
                lambda cid, text, system: pumped.append((cid, text)) or True,
            )
            outcome = asyncio.run(deliver_prompt(
                "chat-st1", "steer me", source="delegate_result",
                session_id="sid-st1", agent="pa", role="manager",
            ))
            assert outcome.path == "pump"
            assert pumped == [("chat-st1", "steer me")]
        finally:
            self._disarm()

    def test_chat_layer_resolves_the_pinned_target(self, temp_db, monkeypatch):
        # A chat pinned to a satellite is steered through its REMOTE layer:
        # the resolver receives the row's path and target with the owner's
        # role (an engine's own object holds no session).
        from core.session import session_delivery, session_manager
        seen = {}

        def _get_execution_layer(agent, execution_path="", user_sub=None,
                                 role="manager", execution_target=""):
            seen.update(agent=agent, execution_path=execution_path,
                        user_sub=user_sub, role=role,
                        execution_target=execution_target)
            return "the-remote-layer"
        monkeypatch.setattr(session_manager, "get_execution_layer",
                            _get_execution_layer)
        from auth import providers as _providers
        monkeypatch.setattr(_providers, "acting_role_of",
                            lambda sub, agent, **k: "editor")
        layer = asyncio.run(session_delivery.chat_layer({
            "id": "chat-r1", "agent": "pa", "user_sub": "user-1",
            "execution_path": "codex-cli", "execution_target": "machine-7",
        }))
        assert layer == "the-remote-layer"
        assert seen == {
            "agent": "pa", "execution_path": "codex-cli", "user_sub": "user-1",
            "role": "editor", "execution_target": "machine-7",
        }


class TestOneshotGuard:
    def test_refused_while_live_pty_on_chat(self, temp_db):
        try:
            task_store.create_chat("chat-g1", "user-1", "pa")
            # The live PTY holds the chat under a DIFFERENT sid (mode toggles
            # re-warm the same JSONL under a new session id).
            _install(_FakeISession("sid-other", "chat-g1"))
            calls = []

            async def _oneshot(sid, agent, text, **kw):
                calls.append(sid)
                return "SHOULD NOT RUN"

            outcome = asyncio.run(deliver_prompt(
                "chat-g1", "the result", source="delegate_result",
                session_id="sid-dead", agent="pa",
                oneshot_fn=_oneshot,
                allow_pty=False, role="manager",                        # handback re-entry shape
            ))
            assert outcome.path == "none"
            assert calls == []
        finally:
            _cleanup()

    def test_refused_while_warmup_inflight(self, temp_db):
        # The warmup never completes and no PTY materializes: the recovery
        # wait (patched short) expires and the refusal stands.
        try:
            task_store.create_chat("chat-g2", "user-1", "pa")
            calls = []
            delivery._ONESHOT_WARMUP_WAIT_S = 0.2

            async def _oneshot(sid, agent, text, **kw):
                calls.append(sid)
                return "SHOULD NOT RUN"

            async def _run():
                await warmup_registry.register("chat-g2", "user-1", "pa")
                try:
                    return await deliver_prompt(
                        "chat-g2", "the result", source="delegate_result",
                        session_id="sid-g2", agent="pa",
                        oneshot_fn=_oneshot, role="manager",
                    )
                finally:
                    await warmup_registry.unregister("chat-g2")

            outcome = asyncio.run(_run())
            assert outcome.path == "none"
            assert calls == []
        finally:
            delivery._ONESHOT_WARMUP_WAIT_S = 90.0
            _cleanup()

    def test_runs_when_clear_and_hook_fires(self, temp_db):
        try:
            task_store.create_chat("chat-g3", "user-1", "pa")
            outcomes = []

            async def _oneshot(sid, agent, text, **kw):
                return "ECHO"

            outcome = asyncio.run(deliver_prompt(
                "chat-g3", "the result", source="delegate_result",
                session_id="sid-g3", agent="pa",
                oneshot_fn=_oneshot,
                on_outcome=outcomes.append, role="manager",
            ))
            assert outcome.path == "oneshot"
            assert outcome.response == "ECHO"
            assert outcomes and outcomes[0] is outcome  # hook saw the outcome
        finally:
            _cleanup()

    def test_inflight_claim_visible_during_oneshot_and_cleared_after(self, temp_db):
        """The warmup-side guard's contract: ``oneshot_inflight(chat)`` is a
        live Event while the one-shot echo runs, and gone (set) afterwards —
        including on a failing echo."""
        try:
            task_store.create_chat("chat-g4", "user-1", "pa")
            seen = []

            async def _oneshot(sid, agent, text, **kw):
                evt = oneshot_inflight("chat-g4")
                seen.append(evt is not None and not evt.is_set())
                return "ECHO"

            asyncio.run(deliver_prompt(
                "chat-g4", "the result", source="delegate_result",
                session_id="sid-g4", agent="pa", oneshot_fn=_oneshot, role="manager",
            ))
            assert seen == [True]
            assert oneshot_inflight("chat-g4") is None

            async def _boom(sid, agent, text, **kw):
                raise RuntimeError("echo died")

            with contextlib.suppress(RuntimeError):
                asyncio.run(deliver_prompt(
                    "chat-g4", "x", source="delegate_result",
                    session_id="sid-g4", agent="pa", oneshot_fn=_boom, role="manager",
                ))
            assert oneshot_inflight("chat-g4") is None  # finally released it
        finally:
            _cleanup()

    def test_blocked_oneshot_recovers_onto_warmed_pty(self, temp_db):
        """The strand fix: a one-shot refused for an in-flight warmup waits
        for it, then queues on the fresh live PTY (result arrives
        interactively) — and releases its own claim BEFORE waiting, so the
        warmup awaiting ``oneshot_inflight`` can never deadlock against it."""
        try:
            task_store.create_chat("chat-g5", "user-1", "pa")
            calls = []
            claims_during_wait = []

            async def _oneshot(sid, agent, text, **kw):
                calls.append(sid)
                return "SHOULD NOT RUN"

            async def _run():
                await warmup_registry.register("chat-g5", "user-1", "pa")
                task = asyncio.create_task(deliver_prompt(
                    "chat-g5", "the result", source="delegate_result",
                    session_id="sid-dead", agent="pa", oneshot_fn=_oneshot, role="manager",
                ))
                await asyncio.sleep(0.2)          # deliver is now waiting
                claims_during_wait.append(oneshot_inflight("chat-g5"))
                fake = _FakeISession("sid-warm", "chat-g5")
                _install(fake)                    # the warmup produced a PTY
                await warmup_registry.unregister("chat-g5")
                return await task, fake

            outcome, fake = asyncio.run(_run())
            assert outcome.path == "pty"
            assert outcome.session_id == "sid-warm"
            assert calls == []                    # echo never ran headless
            assert claims_during_wait == [None]   # claim released pre-wait
            assert len(fake.queued) == 1
            assert fake.queued[0]["text"] == "the result"
        finally:
            _cleanup()


def test_register_notify_queue_replaces_superseded_sid(temp_db):
    """A same-chat re-warm passes ``replaces`` — the dead sid's entry goes;
    sibling-chat entries and other connections' entries survive."""
    import asyncio
    from types import SimpleNamespace

    from ws.dashboard import DashboardConnection

    q_mine: asyncio.Queue = asyncio.Queue()
    q_other: asyncio.Queue = asyncio.Queue()
    conn = SimpleNamespace(session_id="sid-new", notify_queue=q_mine, _view_only=False)
    reg = DashboardConnection._register_notify_queue
    try:
        _dashboard_notify_queues["sid-old"] = q_mine   # superseded
        _dashboard_notify_queues["sid-bg"] = q_mine    # sibling bg chat
        reg(conn, replaces="sid-old")
        assert "sid-old" not in _dashboard_notify_queues
        assert _dashboard_notify_queues["sid-bg"] is q_mine
        assert _dashboard_notify_queues["sid-new"] is q_mine

        # A sid owned by ANOTHER connection's queue is never dropped.
        _dashboard_notify_queues["sid-foreign"] = q_other
        reg(conn, replaces="sid-foreign")
        assert _dashboard_notify_queues["sid-foreign"] is q_other

        # replaces == the current sid must not self-drop.
        reg(conn, replaces="sid-new")
        assert _dashboard_notify_queues["sid-new"] is q_mine
    finally:
        for k in ("sid-old", "sid-bg", "sid-new", "sid-foreign"):
            _dashboard_notify_queues.pop(k, None)
