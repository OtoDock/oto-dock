"""Delegate result delivery robustness.

- The delegate_result event (source of truth) is persisted EXACTLY ONCE, even
  when the LLM-echo delivery fails (dead session) — the result is never lost and
  the badge always completes.
- A failed delivery must NOT save an assistant message (the old "No conversation
  found" bug).
- task_id rides on the event for stable spawn↔result correlation.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/tasks/test_delegate_delivery.py -q
"""

from __future__ import annotations

import asyncio
import json

from services.scheduler import scheduler
from services.scheduler.scheduler import TaskDefinition
from storage import database as task_store


def _task() -> TaskDefinition:
    return TaskDefinition(id="task-1", name="sub", agent="pa", prompt="p", scope="agent")


def _delegate_events(chat_id):
    return [m for m in task_store.get_chat_messages(chat_id)
            if m.get("event_type") == "delegate_result"]


def _assistant_msgs(chat_id):
    return [m for m in task_store.get_chat_messages(chat_id)
            if m.get("role") == "assistant"]


class TestDelegateDelivery:
    def test_failed_delivery_persists_result_once_no_echo(self, temp_db, monkeypatch):
        task_store.create_chat("chat-x", "user-1", "pa")

        async def _fail(*a, **k):
            return None
        monkeypatch.setattr(scheduler, "_deliver_via_persistent", _fail)
        monkeypatch.setattr(scheduler, "_deliver_via_oneshot", _fail)

        asyncio.run(scheduler._do_deliver(
            "sess-1", "pa", "echo prompt", _task(),
            chat_id="chat-x", output_text="THE RESULT",
        ))

        evts = _delegate_events("chat-x")
        assert len(evts) == 1                       # persisted exactly once
        data = json.loads(evts[0]["event_data"])
        assert data["output_text"] == "THE RESULT"
        assert data["task_id"] == "task-1"          # stable correlation key
        assert _assistant_msgs("chat-x") == []      # no error echo saved

    def test_notify_queue_payload_carries_origin(self, temp_db, monkeypatch):
        """Path 1 (WS connected): the task_result_prompt payload MUST carry the
        delegating session_id + chat_id so the dashboard handler runs the
        synthesis turn on the ORIGINATING chat (chat-scoped server turns) — not
        whatever chat the socket happens to be viewing. Regression guard for the
        contamination fix."""
        from core.session.session_state import _dashboard_notify_queues
        task_store.create_chat("chat-z", "user-1", "pa")
        # push_pump_event (live-only UI nudge) is a safe no-op here — its hook
        # (_push_pump_event_fn) is unset in tests, so it just returns False.
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-z"] = q
        try:
            asyncio.run(scheduler._do_deliver(
                "sess-z", "pa", "echo prompt", _task(),
                chat_id="chat-z", output_text="THE RESULT",
            ))
            assert not q.empty(), "expected a task_result_prompt on the notify queue"
            payload = q.get_nowait()
            assert payload["type"] == "task_result_prompt"
            assert payload["session_id"] == "sess-z"   # routes to the right session
            assert payload["chat_id"] == "chat-z"        # routes to the right chat
            assert payload["result_prompt"] == "echo prompt"
        finally:
            _dashboard_notify_queues.pop("sess-z", None)

    def test_successful_delivery_persists_result_and_echo(self, temp_db, monkeypatch):
        task_store.create_chat("chat-y", "user-1", "pa")

        async def _ok(*a, **k):
            return "ECHO RESPONSE"

        async def _none(*a, **k):
            return None
        monkeypatch.setattr(scheduler, "_deliver_via_persistent", _ok)
        monkeypatch.setattr(scheduler, "_deliver_via_oneshot", _none)

        asyncio.run(scheduler._do_deliver(
            "sess-2", "pa", "echo prompt", _task(),
            chat_id="chat-y", output_text="THE RESULT",
        ))

        assert len(_delegate_events("chat-y")) == 1   # result persisted once
        echoes = _assistant_msgs("chat-y")
        assert len(echoes) == 1                        # echo saved on real response
        assert echoes[0]["content"] == "ECHO RESPONSE"

    def test_notify_payload_carries_status(self, temp_db, monkeypatch):
        from core.session.session_state import _dashboard_notify_queues
        task_store.create_chat("chat-s", "user-1", "pa")
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-s"] = q
        try:
            asyncio.run(scheduler._do_deliver(
                "sess-s", "pa", "echo prompt", _task(),
                chat_id="chat-s", output_text="PARTIAL",
                status="user_interrupted",
            ))
            payload = q.get_nowait()
            assert payload["status"] == "user_interrupted"
        finally:
            _dashboard_notify_queues.pop("sess-s", None)


class TestLaneCollection:
    """Cursor-based lane collection: assistant turns verbatim, user rows as
    [User interjected], the run's own driven prompt excluded."""

    def test_collects_after_cursor_with_interjections(self, temp_db):
        task_store.create_chat("lane-1", "user-1", "pa")
        task_store.add_chat_message("lane-1", "assistant", "OLD ROUND")
        cursor = task_store.get_last_chat_message_id("lane-1")
        own = task_store.add_chat_message("lane-1", "user", "do the work")
        task_store.add_chat_message("lane-1", "assistant", "part one")
        task_store.add_chat_message("lane-1", "user", "actually focus on X")
        task_store.add_chat_message("lane-1", "assistant", "part two")

        out = scheduler._collect_lane_output_since("lane-1", cursor, own, "do the work")
        assert "OLD ROUND" not in out
        assert "do the work" not in out
        assert out == "part one\n\n[User interjected]: actually focus on X\n\npart two"

    def test_own_prompt_excluded_by_content_match(self, temp_db):
        # Interactive runs: the tailer backfills the driven prompt as a user
        # row — no row id to skip, so the first content match is consumed.
        task_store.create_chat("lane-2", "user-1", "pa")
        task_store.add_chat_message("lane-2", "user", "do the work")
        task_store.add_chat_message("lane-2", "assistant", "answer")
        task_store.add_chat_message("lane-2", "user", "do the work")  # user echoing

        out = scheduler._collect_lane_output_since("lane-2", 0, 0, "do the work")
        assert out == "answer\n\n[User interjected]: do the work"


class TestLaneQuiescence:
    def test_quiet_lane_returns_immediately(self, temp_db):
        import time as _time
        t0 = _time.monotonic()
        asyncio.run(scheduler._await_lane_quiescence("no-such-chat"))
        assert _time.monotonic() - t0 < 0.5

    def test_waits_for_active_pump_then_settles(self, temp_db):
        from core.events.stream_pump import _active_pumps

        class _FakePump:
            is_done = False
            message_queue: list = []

        pump = _FakePump()
        _active_pumps["lane-q"] = pump

        async def _run():
            async def _finish_soon():
                await asyncio.sleep(1.2)
                pump.is_done = True
                del _active_pumps["lane-q"]
            asyncio.get_running_loop().create_task(_finish_soon())
            await scheduler._await_lane_quiescence("lane-q", settle_seconds=0.5,
                                                   ceiling_seconds=10.0)

        import time as _time
        t0 = _time.monotonic()
        try:
            asyncio.run(_run())
        finally:
            _active_pumps.pop("lane-q", None)
        elapsed = _time.monotonic() - t0
        assert 1.2 <= elapsed < 8.0  # waited for the pump, then settled

    def test_ceiling_bounds_a_stuck_lane(self, temp_db):
        from core.events.stream_pump import _active_pumps

        class _StuckPump:
            is_done = False
            message_queue: list = []

        _active_pumps["lane-c"] = _StuckPump()
        try:
            asyncio.run(scheduler._await_lane_quiescence(
                "lane-c", ceiling_seconds=2.0))
        finally:
            _active_pumps.pop("lane-c", None)


class TestLaneFinalization:
    """_deliver_task_result with worker_chat_id: quiescence → abort re-check →
    cursor re-collection, then the template substitution incl. {{chat_id}}."""

    def _lane_task(self, chat_id: str) -> TaskDefinition:
        return TaskDefinition(
            id="dyn-lane1", name="lane", agent="pa", prompt="do the work",
            scope="agent", target_chat_id=chat_id or None,
            on_complete_agent="pa",
            on_complete_prompt="s={{status}} chat={{chat_id}} out={{output}}",
            on_complete_session_id="sess-lane",
        )

    def _deliver(self, monkeypatch, task, status, output, **lane_kw):
        captured: dict = {}
        done = asyncio.Event()

        async def _fake_do_deliver(session_id, agent, result_prompt, t, **kw):
            captured.update(kw, result_prompt=result_prompt)
            done.set()

        monkeypatch.setattr(scheduler, "_do_deliver", _fake_do_deliver)

        async def _run():
            await scheduler._deliver_task_result(task, status, output, **lane_kw)
            await asyncio.wait_for(done.wait(), timeout=5)

        asyncio.run(_run())
        return captured

    def test_recollects_and_substitutes_chat_id(self, temp_db, monkeypatch):
        task_store.create_chat("lane-f1", "user-1", "pa")
        cursor = task_store.get_last_chat_message_id("lane-f1")
        own = task_store.add_chat_message("lane-f1", "user", "do the work")
        task_store.add_chat_message("lane-f1", "assistant", "the answer")
        task_store.add_chat_message("lane-f1", "user", "also check Y")

        got = self._deliver(
            monkeypatch, self._lane_task("lane-f1"), "completed", "stale",
            worker_chat_id="lane-f1", output_cursor=cursor,
            prompt_row_id=own, prompt_text="do the work",
        )
        assert got["status"] == "completed"
        assert got["output_text"] == "the answer\n\n[User interjected]: also check Y"
        assert "chat=lane-f1" in got["result_prompt"]
        assert "s=completed" in got["result_prompt"]

    def test_abort_flag_flips_status_to_user_interrupted(self, temp_db, monkeypatch):
        task_store.create_chat("lane-f2", "user-1", "pa")
        task_store.update_chat("lane-f2", last_turn_aborted=True)
        task_store.add_chat_message("lane-f2", "assistant", "partial")

        got = self._deliver(
            monkeypatch, self._lane_task("lane-f2"), "completed", "",
            worker_chat_id="lane-f2",
        )
        assert got["status"] == "user_interrupted"
        assert got["output_text"] == "partial"
        assert "s=user_interrupted" in got["result_prompt"]

    def test_no_lane_kwargs_delivers_unchanged(self, temp_db, monkeypatch):
        got = self._deliver(
            monkeypatch, self._lane_task(""), "failed", "boom",
        )
        assert got["status"] == "failed"
        assert got["output_text"] == "boom"
        assert "chat= " in got["result_prompt"]  # substitutes to empty


class TestUserCancelStamping:
    def test_cancel_run_stamps_user_cancelled(self, temp_db):
        async def _run():
            async def _sleeper():
                await asyncio.sleep(30)
            t = asyncio.get_running_loop().create_task(_sleeper())
            scheduler._running_tasks["run-uc1"] = t
            try:
                assert await scheduler.cancel_run("run-uc1") is True
                assert "run-uc1" in scheduler._user_cancelled_runs
            finally:
                scheduler._running_tasks.pop("run-uc1", None)
                scheduler._user_cancelled_runs.discard("run-uc1")
                t.cancel()

        asyncio.run(_run())

    def test_interactive_death_carries_had_viewer(self):
        exc = scheduler._InteractiveSessionDied("dead", had_viewer=True)
        assert exc.had_viewer is True
        assert isinstance(exc, RuntimeError)


class TestPlatformCancelStamping:
    """Platform-initiated interrupts must be distinguishable from user
    cancels on the runs page: failed + reason vs cancelled."""

    def test_platform_cancel_run_notes_reason_not_user(self, temp_db):
        async def _run():
            async def _sleeper():
                await asyncio.sleep(30)
            t = asyncio.get_running_loop().create_task(_sleeper())
            scheduler._running_tasks["run-pc1"] = t
            try:
                assert scheduler.platform_cancel_run(
                    "run-pc1", "reaped by platform: stalled") is True
                assert scheduler._platform_interrupts["run-pc1"] == (
                    "reaped by platform: stalled")
                assert "run-pc1" not in scheduler._user_cancelled_runs
            finally:
                scheduler._running_tasks.pop("run-pc1", None)
                scheduler._platform_interrupts.pop("run-pc1", None)
                t.cancel()
        asyncio.run(_run())

    def test_platform_cancel_run_without_task_returns_false(self, temp_db):
        assert scheduler.platform_cancel_run("run-none", "why") is False
        assert "run-none" not in scheduler._platform_interrupts


class TestOneshotSecurityContext:
    """A one-shot resume rebuilds the session's SecurityContext — close/reap
    dropped the persisted one, and without a rebuilt context every hook of the
    callback turn fail-closes with "Session is no longer active"."""

    def test_oneshot_config_carries_security_context(self, temp_db, monkeypatch):
        from storage import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True,
                                 default_scope="user")

        captured: dict = {}

        class _FakeLayer:
            async def can_resume_session(self, sid, agent_name="", username=""):
                return True

            async def start_session(self, sid, cfg):
                captured["cfg"] = cfg

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

        from core.session import session_manager
        monkeypatch.setattr(session_manager, "get_execution_layer",
                            lambda *a, **k: _FakeLayer())
        from services.mcp import mcp_registry
        monkeypatch.setattr(mcp_registry, "build_session_mcp_config",
                            lambda *a, **k: (None, {}, [], {}, []))

        out = asyncio.run(scheduler._deliver_via_oneshot(
            "11111111-2222-3333-4444-555555555555", "pa", "result!",
            user_sub=None, role="manager",
        ))
        assert out == "ok"
        cfg = captured["cfg"]
        assert cfg.resume is True
        ctx = cfg.security_context
        assert ctx is not None
        assert ctx.agent == "pa"
        assert ctx.role == "manager"
        assert ctx.target_kind == "local"


class TestTaskStallWatchdog:
    """_watch_task_pump — the headless-turn backstop. A wedged turn used to
    hold the run "generating" forever (recovery only fired when a user
    re-opened the chat)."""

    class _FakePump:
        def __init__(self, task, producer):
            self._task = task
            self.producer = producer
            self.aborted = False

        def abort(self):
            self.aborted = True
            self._task.cancel()

    class _FakeLayer:
        def __init__(self, idle=None, severed=False, dead=False):
            self.idle = idle
            self.severed = severed
            self.dead = dead
            self.prepared = False

        def remote_stream_severed(self, sid):
            return self.severed

        def session_idle_seconds(self, sid):
            return self.idle

        async def probe_session_process_dead(self, sid):
            return self.dead

        async def prepare_resume(self, sid):
            self.prepared = True

    def _run(self, coro):
        return asyncio.run(coro)

    def test_healthy_completion_passes_through(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_WATCHDOG_SLICE_S", 0.05)

        async def _go():
            turn = asyncio.create_task(asyncio.sleep(0.01))
            producer = asyncio.create_task(asyncio.sleep(0.01))
            pump = self._FakePump(turn, producer)
            await scheduler._watch_task_pump(
                self._FakeLayer(), pump, "run-w1", "task-run-w1", "s" * 8)
            assert not pump.aborted

        self._run(_go())

    def test_alive_process_below_ceiling_keeps_leash(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_WATCHDOG_SLICE_S", 0.02)
        monkeypatch.setattr(scheduler, "_STALL_PROBE_SECS", 0.0)

        async def _go():
            hang = asyncio.get_event_loop().create_future()
            producer = asyncio.get_event_loop().create_future()
            pump = self._FakePump(asyncio.ensure_future(hang), producer)
            # Idle past the probe threshold but process alive → no reap; let
            # the turn finish on the third slice.
            layer = self._FakeLayer(idle=10.0, dead=False)

            async def _finish():
                await asyncio.sleep(0.07)
                hang.set_result(None)

            fin = asyncio.create_task(_finish())
            await scheduler._watch_task_pump(
                layer, pump, "run-w2", "task-run-w2", "s" * 8)
            await fin
            assert not pump.aborted

        self._run(_go())

    def test_hard_stale_turn_is_reaped(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_WATCHDOG_SLICE_S", 0.02)
        import config as _config
        monkeypatch.setattr(_config, "CLAUDE_TIMEOUT", 5)

        async def _go():
            hang = asyncio.get_event_loop().create_future()
            producer = asyncio.get_event_loop().create_future()
            pump = self._FakePump(asyncio.ensure_future(hang), producer)
            layer = self._FakeLayer(idle=99.0, dead=False)
            try:
                await scheduler._watch_task_pump(
                    layer, pump, "run-w3", "task-run-w3", "s" * 8)
            except scheduler._TaskTurnStalled as e:
                assert "hard ceiling" in str(e)
            else:
                raise AssertionError("expected _TaskTurnStalled")
            assert pump.aborted
            assert layer.prepared

        self._run(_go())

    def test_dead_process_past_probe_is_reaped(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_WATCHDOG_SLICE_S", 0.02)
        monkeypatch.setattr(scheduler, "_STALL_PROBE_SECS", 0.0)

        async def _go():
            hang = asyncio.get_event_loop().create_future()
            producer = asyncio.get_event_loop().create_future()
            pump = self._FakePump(asyncio.ensure_future(hang), producer)
            layer = self._FakeLayer(idle=10.0, dead=True)
            try:
                await scheduler._watch_task_pump(
                    layer, pump, "run-w4", "task-run-w4", "s" * 8)
            except scheduler._TaskTurnStalled as e:
                assert "process dead" in str(e)
            else:
                raise AssertionError("expected _TaskTurnStalled")
            assert pump.aborted

        self._run(_go())


class TestInterruptDeferral:
    """A user interrupt defers the callback: no first-probe fast path, a long
    settle window, delivery only once the lane is genuinely quiet."""

    def test_interrupt_skips_fast_path_waits_settle(self, temp_db):
        import time as _time
        t0 = _time.monotonic()
        asyncio.run(scheduler._await_lane_quiescence(
            "no-such-chat", settle_seconds=0.4, ceiling_seconds=5.0,
            immediate_quiet_ok=False,
        ))
        assert _time.monotonic() - t0 >= 0.4

    def test_interrupt_defers_until_lane_quiet(self, temp_db):
        from core.events.stream_pump import _active_pumps

        class _FakePump:
            is_done = False
            message_queue: list = []

        pump = _FakePump()
        _active_pumps["lane-i"] = pump

        async def _run():
            async def _user_round_ends():
                await asyncio.sleep(1.2)
                pump.is_done = True
                del _active_pumps["lane-i"]
            asyncio.get_running_loop().create_task(_user_round_ends())
            await scheduler._await_lane_quiescence(
                "lane-i", settle_seconds=0.3, ceiling_seconds=10.0,
                immediate_quiet_ok=False,
            )

        import time as _time
        t0 = _time.monotonic()
        try:
            asyncio.run(_run())
        finally:
            _active_pumps.pop("lane-i", None)
        assert _time.monotonic() - t0 >= 1.5  # waited out the user round + settle

    def test_finalization_uses_deferral_params_for_interrupts(self, temp_db, monkeypatch):
        task_store.create_chat("lane-i2", "user-1", "pa")
        task_store.add_chat_message("lane-i2", "assistant", "partial work")
        task_store.add_chat_message("lane-i2", "user", "actually do X instead")
        task_store.add_chat_message("lane-i2", "assistant", "did X")

        waited: dict = {}

        async def _fake_quiescence(chat_id, **kw):
            waited.update(kw, chat_id=chat_id)

        monkeypatch.setattr(scheduler, "_await_lane_quiescence", _fake_quiescence)

        captured: dict = {}
        done = asyncio.Event()

        async def _fake_do_deliver(session_id, agent, result_prompt, t, **kw):
            captured.update(kw, result_prompt=result_prompt)
            done.set()

        monkeypatch.setattr(scheduler, "_do_deliver", _fake_do_deliver)

        task = TaskDefinition(
            id="dyn-i2", name="lane", agent="pa", prompt="p", scope="agent",
            target_chat_id="lane-i2", on_complete_agent="pa",
            on_complete_prompt="s={{status}} out={{output}}",
            on_complete_session_id="sess-i2",
        )

        async def _run():
            await scheduler._deliver_task_result(
                task, "user_interrupted", "", worker_chat_id="lane-i2",
            )
            await asyncio.wait_for(done.wait(), timeout=5)

        asyncio.run(_run())
        assert waited["immediate_quiet_ok"] is False
        assert waited["settle_seconds"] == 120.0
        # The deferred callback carries the interjection AND the reply to it.
        assert "[User interjected]: actually do X instead" in captured["output_text"]
        assert "did X" in captured["output_text"]
        assert "s=user_interrupted" in captured["result_prompt"]


def test_touch_chat_bumps_sidebar_recency(temp_db):
    task_store.create_chat("touch-1", "user-1", "pa")
    before = task_store.get_chat("touch-1")["updated_at"]
    import time as _time
    _time.sleep(0.01)
    task_store.touch_chat("touch-1")
    after = task_store.get_chat("touch-1")["updated_at"]
    assert after > before


class _FakeEchoLayer:
    """Minimal layer for a pump-driven echo turn: one TEXT+DONE turn, quiet
    watchdog probes."""

    def __init__(self, text_parts=("PUMPED ", "ECHO")):
        self._text_parts = text_parts
        self._locks: dict = {}

    def session_lock(self, sid):
        return self._locks.setdefault(sid, asyncio.Lock())

    async def send_message(self, sid, prompt, settle_after_result=None):
        from core.events.common_events import CommonEvent, TEXT, DONE
        for part in self._text_parts:
            yield CommonEvent(type=TEXT, data={"content": part})
        yield CommonEvent(type=DONE, data={})

    def remote_stream_severed(self, sid):
        return False

    def session_idle_seconds(self, sid):
        return 0.0

    async def probe_session_process_dead(self, sid):
        return False

    async def wait_for_bg_subagents(self, sid, timeout=120.0):
        return

    async def drain_bg_commands(self, sid, *, budget=2.0):
        return False

    async def is_session_alive(self, sid):
        return True

    async def prepare_resume(self, sid):
        return


class TestPumpedEchoTurn:
    """The dead/idle-session echo turn runs through a headless ChatStreamPump.

    Regression for the 2026-07-13 incident (chat 75eab195): the pre-pump
    direct collection persisted the echo SILENTLY — no chat_status broadcast,
    no live pump for a viewer to attach to, no last_response_at stamp — so a
    delivered result produced zero signal on any connected client."""

    def test_echo_turn_persists_broadcasts_and_stamps(self, temp_db, monkeypatch):
        from core.events import stream_pump as sp
        from core.events.stream_pump import _active_pumps

        task_store.create_chat("chat-pump", "user-1", "pa")
        statuses: list[tuple[str, str]] = []
        monkeypatch.setattr(
            sp.notification_manager, "broadcast_chat_status",
            lambda owner, cid, status, agent="": statuses.append((cid, status)),
        )

        async def _quiet_ephemeral(*a, **k):
            return None
        monkeypatch.setattr(sp.notification_manager, "fire_ephemeral", _quiet_ephemeral)

        out = asyncio.run(scheduler._run_echo_turn_pumped(
            _FakeEchoLayer(), "sess-pump", "chat-pump", "pa", "review the result",
        ))

        assert out == ""  # pump persisted the turn — caller must not re-save
        # Feed truth: the echo landed as the pump's assistant row.
        echoes = _assistant_msgs("chat-pump")
        assert len(echoes) == 1
        assert echoes[0]["content"] == "PUMPED ECHO"
        # Unread truth: the turn end stamped the sidebar/Active-now signal.
        assert (task_store.get_chat("chat-pump") or {}).get("last_response_at")
        # Broadcast truth: connected clients were told the turn ran.
        assert ("chat-pump", "streaming") in statuses
        assert ("chat-pump", "ready") in statuses
        # The pump deregistered itself.
        assert _active_pumps.get("chat-pump") is None

    def test_refuses_when_chat_already_pumping(self, temp_db):
        from core.events.stream_pump import _active_pumps

        task_store.create_chat("chat-busy", "user-1", "pa")
        _active_pumps["chat-busy"] = object()
        try:
            out = asyncio.run(scheduler._run_echo_turn_pumped(
                _FakeEchoLayer(), "sess-b", "chat-busy", "pa", "prompt",
            ))
        finally:
            _active_pumps.pop("chat-busy", None)
        assert out is None  # never dual-pump a chat

    def test_ladder_passes_chat_id_to_rungs(self, temp_db, monkeypatch):
        task_store.create_chat("chat-k", "user-1", "pa")
        seen: dict = {}

        async def _capture(sid, agent, text, **k):
            seen.update(k)
            return ""
        monkeypatch.setattr(scheduler, "_deliver_via_persistent", _capture)
        monkeypatch.setattr(scheduler, "_deliver_via_oneshot", _capture)

        asyncio.run(scheduler._do_deliver(
            "sess-k", "pa", "echo prompt", _task(),
            chat_id="chat-k", output_text="R",
        ))
        assert seen.get("chat_id") == "chat-k"

    def test_pump_delivered_echo_not_double_saved(self, temp_db, monkeypatch):
        task_store.create_chat("chat-e", "user-1", "pa")

        async def _pumped(*a, **k):
            return ""  # pump persisted the turn itself

        async def _none(*a, **k):
            return None
        monkeypatch.setattr(scheduler, "_deliver_via_persistent", _pumped)
        monkeypatch.setattr(scheduler, "_deliver_via_oneshot", _none)

        asyncio.run(scheduler._do_deliver(
            "sess-e", "pa", "echo prompt", _task(),
            chat_id="chat-e", output_text="THE RESULT",
        ))
        assert len(_delegate_events("chat-e")) == 1
        assert _assistant_msgs("chat-e") == []  # no duplicate echo row
