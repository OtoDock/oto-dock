"""Delegation wake recovery.

- A wake whose delivery fails on EVERY rung is stored durably on the parent
  chat (``chats.pending_delegate_wake``) and the terminal badge frame is
  broadcast chat-scoped regardless of the delivery rung.
- The stored wake is claimed atomically (exactly one claimer) and injected on
  the chat's next turn/warmup.
- An interactive-pinned parent routes the one-shot to the interactive
  re-warm (``_rewarm_interactive_and_wake``); '' / '-p' chats keep the
  headless echo path unchanged.
- ``submit_prompt(settle=True)`` arms the settle Enter (echo-quiet + max
  backstop) instead of the warm fixed-gap Enter that large pastes lose.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/tasks/test_delegate_wake_recovery.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from services.scheduler import scheduler
from services.scheduler.scheduler import TaskDefinition
from storage import database as task_store


def _task() -> TaskDefinition:
    return TaskDefinition(id="task-1", name="sub", agent="pa", prompt="p", scope="agent")


async def _fail(*a, **k):
    return None


class TestPendingWakeStore:
    def test_append_and_claim_roundtrip(self, temp_db):
        task_store.create_chat("chat-w", "user-1", "pa")
        assert task_store.append_pending_delegate_wake("chat-w", "wake ONE")
        assert task_store.append_pending_delegate_wake("chat-w", "wake TWO")
        assert task_store.claim_pending_delegate_wake("chat-w") == [
            "wake ONE", "wake TWO",
        ]
        # Claimed exactly once — the second claimer gets nothing.
        assert task_store.claim_pending_delegate_wake("chat-w") == []

    def test_claim_empty_and_missing(self, temp_db):
        task_store.create_chat("chat-e", "user-1", "pa")
        assert task_store.claim_pending_delegate_wake("chat-e") == []
        assert task_store.claim_pending_delegate_wake("no-such-chat") == []

    def test_append_missing_chat_is_noop(self, temp_db):
        assert not task_store.append_pending_delegate_wake("no-such-chat", "w")


class TestFailedDeliveryRecovery:
    def test_failed_delivery_stores_wake(self, temp_db, monkeypatch):
        task_store.create_chat("chat-f", "user-1", "pa")
        monkeypatch.setattr(scheduler, "_deliver_via_persistent", _fail)
        monkeypatch.setattr(scheduler, "_deliver_via_oneshot", _fail)

        asyncio.run(scheduler._do_deliver(
            "sess-f", "pa", "THE WAKE PROMPT", _task(),
            chat_id="chat-f", output_text="OUT",
        ))

        assert task_store.claim_pending_delegate_wake("chat-f") == [
            "THE WAKE PROMPT",
        ]

    def test_failed_delivery_broadcasts_badge_frame(self, temp_db, monkeypatch):
        """The terminal delegate_result frame reaches a viewer's notify queue
        even when the wake delivery lands on the none rung — the queue here is
        a BYSTANDER socket (registered under an unrelated session id), which
        rung 2 can't select, so only the chat-scoped broadcast explains it."""
        from core.session.session_state import _dashboard_notify_queues
        task_store.create_chat("chat-b", "user-1", "pa")
        monkeypatch.setattr(scheduler, "_deliver_via_persistent", _fail)
        monkeypatch.setattr(scheduler, "_deliver_via_oneshot", _fail)
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-bystander"] = q
        try:
            asyncio.run(scheduler._do_deliver(
                "sess-b", "pa", "wake", _task(),
                chat_id="chat-b", output_text="OUT", status="failed",
            ))
            frames = []
            while not q.empty():
                frames.append(q.get_nowait())
            ui = [f for f in frames if f.get("type") == "chat_ui_frame"]
            assert len(ui) == 1
            assert ui[0]["chat_id"] == "chat-b"
            assert ui[0]["frame"]["type"] == "delegate_result"
            assert ui[0]["frame"]["task_id"] == "task-1"
            assert ui[0]["frame"]["status"] == "failed"
        finally:
            _dashboard_notify_queues.pop("sess-bystander", None)

    def test_ws_delivery_stores_no_wake(self, temp_db, monkeypatch):
        """A wake that DID deliver (ws rung) must not be double-stored."""
        from core.session.session_state import _dashboard_notify_queues
        task_store.create_chat("chat-ok", "user-1", "pa")
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-ok"] = q
        try:
            asyncio.run(scheduler._do_deliver(
                "sess-ok", "pa", "wake", _task(),
                chat_id="chat-ok", output_text="OUT",
            ))
            assert task_store.claim_pending_delegate_wake("chat-ok") == []
        finally:
            _dashboard_notify_queues.pop("sess-ok", None)


class TestInteractiveRouting:
    def _fake_layer(self, captured: dict):
        class _FakeLayer:
            async def can_resume_session(self, sid, agent_name="", username=""):
                return True

            async def start_session(self, sid, cfg):
                captured["headless_cfg"] = cfg

            def session_lock(self, sid):
                class _Lock:
                    async def __aenter__(self):
                        return None

                    async def __aexit__(self, *a):
                        return False
                return _Lock()

            async def send_message(self, sid, prompt):
                from core.events.common_events import CommonEvent, TEXT
                yield CommonEvent(type=TEXT, data={"content": "ok"})
        return _FakeLayer()

    def _stub_env(self, monkeypatch, captured: dict):
        from storage import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True,
                                 default_scope="user")
        from core.session import session_manager
        monkeypatch.setattr(session_manager, "get_execution_layer",
                            lambda *a, **k: self._fake_layer(captured))
        from services.mcp import mcp_registry
        monkeypatch.setattr(mcp_registry, "build_session_mcp_config",
                            lambda *a, **k: (None, {}, [], {}, []))

    def test_interactive_pin_routes_to_rewarm(self, temp_db, monkeypatch):
        captured: dict = {}
        self._stub_env(monkeypatch, captured)
        task_store.create_chat("chat-i", "user-1", "pa")
        task_store.update_chat("chat-i", execution_mode="interactive")

        rewarm_calls: list[dict] = []

        async def _fake_rewarm(layer, session_id, agent, result_prompt,
                               *, chat_id, base_cfg, chat_row):
            rewarm_calls.append({
                "chat_id": chat_id, "prompt": result_prompt,
                "interactive": base_cfg.interactive, "resume": base_cfg.resume,
            })
            return ""
        monkeypatch.setattr(scheduler, "_rewarm_interactive_and_wake", _fake_rewarm)

        out = asyncio.run(scheduler._deliver_via_oneshot(
            "11111111-2222-3333-4444-555555555555", "pa", "wake!",
            user_sub=None, role="manager", chat_id="chat-i",
        ))
        assert out == ""
        assert len(rewarm_calls) == 1
        assert rewarm_calls[0]["chat_id"] == "chat-i"
        assert rewarm_calls[0]["prompt"] == "wake!"
        assert rewarm_calls[0]["resume"] is True
        # The headless spawn must NOT have run — routing happened before it.
        assert "headless_cfg" not in captured

    def test_headless_chat_keeps_echo_path(self, temp_db, monkeypatch):
        captured: dict = {}
        self._stub_env(monkeypatch, captured)
        task_store.create_chat("chat-h", "user-1", "pa")  # execution_mode ''

        echo_calls: list[str] = []

        async def _fake_echo(layer, session_id, chat_id, agent, result_prompt):
            echo_calls.append(chat_id)
            return ""
        monkeypatch.setattr(scheduler, "_run_echo_turn_pumped", _fake_echo)

        out = asyncio.run(scheduler._deliver_via_oneshot(
            "11111111-2222-3333-4444-555555555555", "pa", "wake!",
            user_sub=None, role="manager", chat_id="chat-h",
        ))
        assert out == ""
        assert echo_calls == ["chat-h"]
        assert captured["headless_cfg"].resume is True


class TestRewarmInteractiveAndWake:
    class _FakeIsess:
        def __init__(self, *, opens_on_submit=True):
            self.alive = True
            self.has_viewer = False
            self.turn_open = False
            self.opens_on_submit = opens_on_submit
            self.on_turn_complete = None
            self.submitted: list[tuple[str, bool]] = []

        def submit_prompt(self, text, *, settle=False):
            self.submitted.append((text, settle))
            if not self.opens_on_submit:
                return  # the paste never landed — turn_open stays False
            # A landed submit opens the turn (transcript-derived) and the
            # tailer fires turn-complete once the wake turn ends.
            self.turn_open = True
            if self.on_turn_complete:
                self.on_turn_complete("done")

    def _run(self, monkeypatch, isess, *, start_raises=False):
        from core.session import interactive_session
        from core.execution_layer import AgentConfig

        closed: list[str] = []

        class _FakeLayer:
            async def start_session(self, sid, cfg):
                if start_raises:
                    raise RuntimeError("spawn failed")

        monkeypatch.setattr(interactive_session, "get", lambda sid: isess)
        monkeypatch.setattr(scheduler, "_WAKE_TURN_OPEN_S", 1.0)

        async def _fake_close(sid, reason=""):
            closed.append(reason)
        monkeypatch.setattr(interactive_session, "close_session", _fake_close)

        cfg = AgentConfig(agent_name="pa", user_sub="", system_prompt="",
                          mcp_config_path="", permission_mode="auto",
                          client_type="", resume=True)
        chat_row = {"execution_mode": "interactive", "tui_theme": "dark"}
        out = asyncio.run(scheduler._rewarm_interactive_and_wake(
            _FakeLayer(), "sess-r", "pa", "WAKE",
            chat_id="chat-r", base_cfg=cfg, chat_row=chat_row,
        ))
        return out, cfg, closed

    def test_wake_submitted_settle_terminal_left_to_reaper(self, temp_db, monkeypatch):
        isess = self._FakeIsess()
        out, cfg, closed = self._run(monkeypatch, isess)
        assert out == ""
        assert cfg.interactive is True
        assert cfg.interactive_theme == "dark"
        # Chat binding — the tailer persistence, turn signals and the
        # dashboard's pty_attach guard all key on it.
        assert cfg.chat_id == "chat-r"
        assert isess.submitted == [("WAKE", True)]
        # The finished terminal stays up for the idle reaper's window so a
        # user opening the chat shortly after still attaches to it.
        assert closed == []

    def test_viewer_attached_keeps_session_live(self, temp_db, monkeypatch):
        isess = self._FakeIsess()
        isess.has_viewer = True
        out, _cfg, closed = self._run(monkeypatch, isess)
        assert out == ""
        assert closed == []  # the live terminal belongs to the viewer now

    def test_unopened_turn_stores_for_replay(self, temp_db, monkeypatch):
        # The paste never submits: after one bare-Enter nudge the wake is NOT
        # counted delivered — None routes it to the durable pending store.
        isess = self._FakeIsess(opens_on_submit=False)
        out, _cfg, closed = self._run(monkeypatch, isess)
        assert out is None
        assert isess.submitted == [("WAKE", True), ("", True)]
        assert closed == ["delegate_wake_unsubmitted"]

    def test_spawn_failure_returns_none(self, temp_db, monkeypatch):
        isess = self._FakeIsess()
        out, _cfg, closed = self._run(monkeypatch, isess, start_raises=True)
        assert out is None
        assert isess.submitted == []


class TestSettleSubmit:
    class _FakePty:
        closed = False

        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

    @pytest.mark.asyncio
    async def test_settle_arms_deferred_enter(self):
        from core.session import interactive_session as isess_mod
        s = isess_mod.InteractiveSession(
            session_id="st-1", chat_id="c", agent_name="agent")
        s.pty = self._FakePty()
        s._ready = True
        s._submitted_once = True  # warm session — the bug's precondition

        s.submit_prompt("line one\nline two", settle=True)

        joined = b"".join(s.pty.writes)
        assert b"line one\nline two" in joined
        # The body went out WITHOUT an immediate trailing Enter …
        assert not joined.endswith(b"\r")
        # … because the Enter is armed on the settle machinery instead.
        assert s._submit_settle_handle is not None
        assert s._submit_max_handle is not None
        s._cancel_deferred_submit()

    @pytest.mark.asyncio
    async def test_plain_submit_unchanged_for_user_path(self):
        from core.session import interactive_session as isess_mod
        s = isess_mod.InteractiveSession(
            session_id="st-2", chat_id="c", agent_name="agent")
        s.pty = self._FakePty()
        s._ready = True
        s._submitted_once = True

        s.submit_prompt("hello", settle=False)

        # Body written; the fixed-gap Enter is scheduled (not the settle arm).
        assert any(b"hello" in w for w in s.pty.writes)
        assert s._submit_settle_handle is None
        s._cancel_deferred_submit()


class TestHeadlessChokepointReplay:
    """A wake stored while the chat was dead rides the chat's NEXT headless
    turn — prepended ahead of the user's prompt at the _start_new_stream
    chokepoint (same claim shape as the history seed)."""

    def test_pending_wake_prepended_to_next_turn(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE
        from tests.fixtures.ws_dashboard_harness import (
            FakeExecutionLayer, dashboard_connection, drain_startup,
            make_test_agent, run_ws_scenario, session_cookie, set_username,
            stub_dashboard_seams, warm_new_chat,
        )

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"text": "ok"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)
                task_store.append_pending_delegate_wake(chat_id, "PENDING WAKE X")
                ws.client_send({"type": "chat", "text": "user message"})
                for _ in range(40):
                    frame = await ws.next_frame()
                    if frame.get("type") == "done":
                        return
                raise AssertionError("turn never completed")

        run_ws_scenario(scenario)
        sent_prompt = layer.messages[-1][1]
        assert "PENDING WAKE X" in sent_prompt
        assert sent_prompt.index("PENDING WAKE X") < sent_prompt.index("user message")


class TestRedeliverPendingWakes:
    """Startup / satellite-reconnect sweep (``scheduler.redeliver_pending_wakes``)."""

    def _fake_ladder(self, monkeypatch, outcomes: list, path="oneshot"):
        from core.session import session_delivery

        async def _fake(chat_id, text, **kw):
            outcomes.append((chat_id, text, kw.get("user_sub"), kw.get("role")))
            return session_delivery.DeliveryOutcome(
                path=path, response=None, chat_id=chat_id, session_id="",
            )
        monkeypatch.setattr(session_delivery, "deliver_prompt", _fake)

    def test_sweep_claims_and_delivers_all_wakes_joined(self, temp_db, monkeypatch):
        task_store.create_chat("chat-s1", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-s1", "W1")
        task_store.append_pending_delegate_wake("chat-s1", "W2")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)

        woken = asyncio.run(scheduler.redeliver_pending_wakes())
        assert woken == 1
        assert delivered[0][0] == "chat-s1"
        assert delivered[0][1] == "W1\n\nW2"
        # Claimed — nothing left for a second sweep.
        assert task_store.claim_pending_delegate_wake("chat-s1") == []

    def test_failed_delivery_repersists_original_wakes(self, temp_db, monkeypatch):
        task_store.create_chat("chat-s2", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-s2", "A")
        task_store.append_pending_delegate_wake("chat-s2", "B")
        self._fake_ladder(monkeypatch, [], path="none")

        woken = asyncio.run(scheduler.redeliver_pending_wakes())
        assert woken == 0
        assert task_store.claim_pending_delegate_wake("chat-s2") == ["A", "B"]

    def test_parked_mode_c_chat_is_skipped(self, temp_db, monkeypatch):
        from services.scheduler import run_recovery
        task_store.create_chat("chat-s3", "user-1", "pa")
        task_store.update_chat("chat-s3", session_id="sid-parked")
        task_store.append_pending_delegate_wake("chat-s3", "W")
        run_recovery._parked["sid-parked"] = {"chat_id": "chat-s3"}
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)
        try:
            woken = asyncio.run(scheduler.redeliver_pending_wakes())
        finally:
            run_recovery._parked.clear()
        assert woken == 0 and delivered == []
        # Wake untouched — the post-adopt machine pass claims it later.
        assert task_store.claim_pending_delegate_wake("chat-s3") == ["W"]

    def test_machine_scope_targets_only_that_machines_chats(self, temp_db, monkeypatch):
        task_store.create_chat("chat-m1", "user-1", "pa")
        task_store.update_chat("chat-m1", execution_target="mach-A")
        task_store.append_pending_delegate_wake("chat-m1", "WA")
        task_store.create_chat("chat-m2", "user-1", "pa")
        task_store.update_chat("chat-m2", execution_target="mach-B")
        task_store.append_pending_delegate_wake("chat-m2", "WB")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)

        woken = asyncio.run(scheduler.redeliver_pending_wakes(machine_id="mach-A"))
        assert woken == 1
        assert [d[0] for d in delivered] == ["chat-m1"]
        assert task_store.claim_pending_delegate_wake("chat-m2") == ["WB"]

    def test_task_owner_chats_deliver_agent_scoped(self, temp_db, monkeypatch):
        task_store.create_chat("chat-t1", "task::sub-x", "pa")
        task_store.append_pending_delegate_wake("chat-t1", "W")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)

        asyncio.run(scheduler.redeliver_pending_wakes())
        assert delivered[0][2] is None  # user_sub
        assert delivered[0][3] == "manager"  # role

    def test_raising_delivery_repersists_and_continues(self, temp_db, monkeypatch):
        # Live-hit on T1 (2026-07-19): a corrupt user config.toml made the
        # oneshot spawn raise — the sweep died and the CLAIMED wake was lost.
        from core.session import session_delivery
        task_store.create_chat("chat-x1", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-x1", "LOST?")
        task_store.create_chat("chat-x2", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-x2", "NEXT")
        calls: list = []

        async def _boom_then_ok(chat_id, text, **kw):
            calls.append(chat_id)
            if chat_id == "chat-x1":
                raise RuntimeError("spawn failed")
            return session_delivery.DeliveryOutcome(
                path="oneshot", response=None, chat_id=chat_id, session_id="",
            )
        monkeypatch.setattr(session_delivery, "deliver_prompt", _boom_then_ok)

        woken = asyncio.run(scheduler.redeliver_pending_wakes())
        assert woken == 1
        assert set(calls) == {"chat-x1", "chat-x2"}
        assert task_store.claim_pending_delegate_wake("chat-x1") == ["LOST?"]
        assert task_store.claim_pending_delegate_wake("chat-x2") == []

    def test_oneshot_resolves_chat_pinned_layer(self, temp_db, monkeypatch):
        # Live-hit on T1 (2026-07-19): agent default codex + chat pinned
        # claude-code-cli → the oneshot resolved the CODEX layer, whose
        # resumability pre-check refused the claude session every time.
        from core.session import session_manager
        from storage import remote_store

        task_store.create_chat("chat-l1", "user-1", "pa")
        task_store.update_chat("chat-l1", execution_path="claude-code-cli")
        seen = {}

        class _FakeLayer:
            async def can_resume_session(self, sid, agent_name="", username=""):
                return False

        def _gel(agent, execution_path="", **kw):
            seen["path"] = execution_path
            return _FakeLayer()

        monkeypatch.setattr(session_manager, "get_execution_layer", _gel)
        monkeypatch.setattr(remote_store, "resolve_execution_target",
                            lambda agent, user_sub, role: ("local", ""))

        res = asyncio.run(scheduler._deliver_via_oneshot(
            "sess-l1", "pa", "hello", user_sub=None, role="manager",
            chat_id="chat-l1",
        ))
        assert res is None
        assert seen["path"] == "claude-code-cli"

    def test_oneshot_carries_chat_pinned_model(self, temp_db, monkeypatch):
        # Same T1 live-hit family: empty cfg.model falls back to the AGENT
        # default model, which belongs to the other layer → API 400.
        from core.session import session_manager
        from storage import remote_store

        task_store.create_chat("chat-m5", "user-1", "pa")
        task_store.update_chat("chat-m5", execution_path="claude-code-cli",
                               model="claude-sonnet-5")
        seen = {}

        class _FakeLayer:
            async def can_resume_session(self, sid, agent_name="", username=""):
                return True

            async def start_session(self, sid, cfg):
                seen["model"] = cfg.model
                raise RuntimeError("stop-after-cfg")

        monkeypatch.setattr(session_manager, "get_execution_layer",
                            lambda agent, **kw: _FakeLayer())
        monkeypatch.setattr(remote_store, "resolve_execution_target",
                            lambda agent, user_sub, role: ("local", ""))

        with pytest.raises(RuntimeError, match="stop-after-cfg"):
            asyncio.run(scheduler._deliver_via_oneshot(
                "sess-m5", "pa", "hello", user_sub=None, role="manager",
                chat_id="chat-m5",
            ))
        assert seen["model"] == "claude-sonnet-5"
