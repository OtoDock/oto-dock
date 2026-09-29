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

import pytest

from services.scheduler import scheduler
from services.scheduler import delivery, lanes
from services.scheduler.scheduler import TaskDefinition
from storage import database as task_store


# The ws rung now has a LIVENESS GATE: queue presence alone no longer routes —
# the target session must be alive. Tests that exercise the ws route register
# a fake alive CLI session alongside their notify queue.
from contextlib import contextmanager
from types import SimpleNamespace


@contextmanager
def _alive_cli_session(sid):
    from core.layers.cli import layer as _cli_layer
    _cli_layer._persistent_sessions[sid] = SimpleNamespace(is_alive=True)
    try:
        yield
    finally:
        _cli_layer._persistent_sessions.pop(sid, None)


def _task() -> TaskDefinition:
    return TaskDefinition(id="task-1", name="sub", agent="pa", prompt="p", scope="agent")


@pytest.fixture(autouse=True)
def ledger(monkeypatch):
    """The admission ledger a one-shot wake reserves in: room for ten
    1000 MB sessions, a fresh condition (it binds to this test's loop)."""
    import config
    import core.concurrency as C
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "ADMISSION_WAKE_WAIT_S", 0.3)
    for name, value in (("_sessions", {}), ("_session_est", {}), ("_session_added_at", {}),
                        ("_session_owner", {}), ("_line", []), ("_reserved_mb", 0),
                        ("_parked_tasks", 0), ("_budget_mb", 10_000), ("_floor_mb", 100)):
        monkeypatch.setattr(C, name, value)
    monkeypatch.setattr(C, "_cond", asyncio.Condition())
    monkeypatch.setattr(C, "_live_available_mb", lambda: 100_000)
    return C


def _delegate_events(chat_id):
    return [m for m in task_store.get_chat_messages(chat_id)
            if m.get("event_type") == "delegate_result"]


def _assistant_msgs(chat_id):
    return [m for m in task_store.get_chat_messages(chat_id)
            if m.get("role") == "assistant"]


class TestDelegateOutputPreview:
    """The delegate_result bubble payload: bounded, tail-keeping, marked.

    The old silent [:2000] head cut always amputated the worker's final
    summary (the deliverable lives at the END of a lane narration) — the
    2026-09-02 "cut mid-word at 'Linke'" bug."""

    def test_under_cap_unchanged(self):
        assert scheduler._delegate_output_preview("short result") == "short result"

    def test_over_cap_keeps_tail_with_marker(self):
        text = "HEAD-NARRATION " + ("x" * scheduler._DELEGATE_PREVIEW_CAP) + " FINAL-SUMMARY"
        preview = scheduler._delegate_output_preview(text)
        assert preview.startswith("… [output truncated")
        assert preview.endswith(" FINAL-SUMMARY")
        assert "HEAD-NARRATION" not in preview
        # bounded: cap + the marker line
        assert len(preview) <= scheduler._DELEGATE_PREVIEW_CAP + 120


class TestDelegateDelivery:
    def test_failed_delivery_persists_result_once_no_echo(self, temp_db, monkeypatch):
        _owner()
        task_store.create_chat("chat-x", "user-1", "pa")

        async def _fail(*a, **k):
            return None
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _fail)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _fail)

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
        _owner()
        task_store.create_chat("chat-z", "user-1", "pa")
        # push_pump_event (live-only UI nudge) is a safe no-op here — its hook
        # (_push_pump_event_fn) is unset in tests, so it just returns False.
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-z"] = q
        try:
            with _alive_cli_session("sess-z"):
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
        _owner()
        task_store.create_chat("chat-y", "user-1", "pa")

        async def _ok(*a, **k):
            return "ECHO RESPONSE"

        async def _none(*a, **k):
            return None
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _ok)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _none)

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
        _owner()
        task_store.create_chat("chat-s", "user-1", "pa")
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-s"] = q
        try:
            with _alive_cli_session("sess-s"):
                asyncio.run(scheduler._do_deliver(
                    "sess-s", "pa", "echo prompt", _task(),
                    chat_id="chat-s", output_text="PARTIAL",
                    status="user_interrupted",
                ))
            payload = q.get_nowait()
            assert payload["status"] == "user_interrupted"
        finally:
            _dashboard_notify_queues.pop("sess-s", None)


class TestEchoOffTheLoop:
    def test_echo_row_rides_the_chat_writer(self, temp_db, monkeypatch):
        # The assistant echo is written on the chat's lane,
        # on the DB executor, never with a store call on the event loop.
        import threading
        from core.events import chat_writer
        _owner()
        task_store.create_chat("chat-w1", "user-1", "pa")
        labels: list[str] = []
        real_submit = chat_writer.submit

        def _submit(chat_id, job, *, label=""):
            labels.append(label)
            return real_submit(chat_id, job, label=label)
        monkeypatch.setattr(chat_writer, "submit", _submit)
        threads: list[bool] = []
        real_add = task_store.add_chat_message

        def _add(chat_id, role, content, **kw):
            threads.append(threading.current_thread() is threading.main_thread())
            return real_add(chat_id, role, content, **kw)
        monkeypatch.setattr(task_store, "add_chat_message", _add)

        async def _text(*a, **k):
            return "ECHO RESPONSE"

        async def _none(*a, **k):
            return None
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _text)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _none)
        asyncio.run(scheduler._do_deliver("sess-w1", "pa", "echo prompt", _task(),
                                          chat_id="chat-w1", output_text="R"))
        # The event row too: on the lane, ahead of the echo, off the loop.
        assert labels == ["delegate_result", "delegate_echo"] and threads == [False, False]
        assert len(_delegate_events("chat-w1")) == 1
        assert [m["content"] for m in _assistant_msgs("chat-w1")] == ["ECHO RESPONSE"]

    def test_a_continuation_wake_row_rides_the_chat_writer(self, temp_db, monkeypatch):
        import threading
        from core.events import chat_writer
        from core.session import session_delivery
        from services.scheduler import firing
        _owner()
        task_store.create_chat("chat-w2", "user-1", "pa")
        labels: list[str] = []
        real_submit = chat_writer.submit

        def _submit(chat_id, job, *, label=""):
            labels.append(label)
            return real_submit(chat_id, job, label=label)
        monkeypatch.setattr(chat_writer, "submit", _submit)
        threads: list[bool] = []
        real_add = task_store.add_chat_message

        def _add(chat_id, role, content, **kw):
            threads.append(threading.current_thread() is threading.main_thread())
            return real_add(chat_id, role, content, **kw)
        monkeypatch.setattr(task_store, "add_chat_message", _add)

        async def _ladder(chat_id, text, **kw):
            kw["persist_event"](chat_id)
            return session_delivery.DeliveryOutcome("pty", chat_id=chat_id)
        monkeypatch.setattr(session_delivery, "deliver_prompt", _ladder)
        cont = TaskDefinition(id="cont-w2", name="later", agent="pa", prompt="CHECK BACK",
                              scope="user", created_by="user-1", target_chat_id="chat-w2")
        asyncio.run(firing._fire_continuation(cont))
        assert labels[0] == "schedule_wake" and threads == [False]
        assert [m["event_type"] for m in task_store.get_chat_messages("chat-w2")] == ["schedule_wake"]
        # The coalescing cursor already counts the wake's own row.
        last = task_store.get_last_chat_message_id("chat-w2")
        assert firing._continuation_cursors["chat-w2"] == last
        firing._continuation_cursors.pop("chat-w2", None)


def _grant(sub: str, agent: str, role: str) -> None:
    from storage.agents import agent_store
    from storage.identity import db_users
    if not agent_store.get_agent(agent):
        agent_store.create_agent(agent, agent.upper(), collaborative=True, default_scope="user")
    db_users.add_user_agent(sub, agent, role, "user-admin")


def _revoke(sub: str, agent: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM user_agents WHERE sub=%s AND agent=%s", (sub, agent))
        conn.commit()


def _owner(sub: str = "user-1", agent: str = "pa") -> None:
    """The chat's owner as a person holding the agent: a person's own chat
    is woken as them, and only while they hold it."""
    from datetime import datetime, timezone
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login) "
            "VALUES (%s, %s, %s, 'member', %s, %s) ON CONFLICT DO NOTHING",
            (sub, f"{sub}@test.com", sub, now, now))
        conn.commit()
    _grant(sub, agent, "editor")


def _user_task(created_by: str) -> TaskDefinition:
    return TaskDefinition(id="task-u1", name="sub", agent="worker", prompt="p",
                          scope="user", created_by=created_by)


class TestStandingGate:
    """A user-scope delegate result warms the delegating
    chat only for a person who still holds the agent."""

    def _spy_rungs(self, monkeypatch, *, answer=None, during=None):
        calls: list[dict] = []

        async def _rung(sid, agent, text, **kw):
            calls.append(kw)
            if during is not None:
                during()
            return answer
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _rung)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _rung)
        return calls

    def test_user_scope_result_for_a_person_without_the_agent_persists_event_only(
            self, temp_db, monkeypatch):
        from core.session.session_state import _dashboard_notify_queues
        _grant("user-viewer", "pa", "contributor")
        _revoke("user-viewer", "pa")
        task_store.create_chat("chat-g1", "user-viewer", "pa")
        calls = self._spy_rungs(monkeypatch)
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-bystander-g1"] = q
        try:
            asyncio.run(scheduler._do_deliver("sess-g1", "pa", "RESULT", _user_task("user-viewer"),
                                              chat_id="chat-g1", output_text="OUT"))
        finally:
            _dashboard_notify_queues.pop("sess-bystander-g1", None)
        assert calls == []                                        # nothing warmed
        assert len(_delegate_events("chat-g1")) == 1              # the result is kept
        frames = [f for f in (q.get_nowait() for _ in range(q.qsize()))
                  if f.get("type") == "chat_ui_frame"]
        assert frames and frames[0]["frame"]["type"] == "delegate_result"
        assert task_store.claim_pending_delegate_wake("chat-g1") == []

    def test_user_scope_result_for_a_person_with_the_agent_delivers_at_the_row_role(
            self, temp_db, monkeypatch):
        _grant("user-viewer", "pa", "contributor")
        task_store.create_chat("chat-g2", "user-viewer", "pa")
        calls = self._spy_rungs(monkeypatch, answer="")
        asyncio.run(scheduler._do_deliver("sess-g2", "pa", "RESULT", _user_task("user-viewer"),
                                          chat_id="chat-g2", output_text="OUT"))
        assert calls and calls[0]["user_sub"] == "user-viewer"
        assert calls[0]["role"] == "contributor"

    def test_agent_scope_result_wakes_a_persons_chat_as_its_owner(self, temp_db, monkeypatch):
        # The chat decides, not the worker's scope: a person's own chat runs
        # as them, in their tree, at their row, and only while they hold the
        # agent.
        task_store.create_chat("chat-g3", "user-viewer", "pa")
        task = TaskDefinition(id="task-a1", name="sub", agent="worker", prompt="p",
                              scope="agent", created_by="user-viewer")
        calls = self._spy_rungs(monkeypatch, answer="")
        asyncio.run(scheduler._do_deliver("sess-g3", "pa", "RESULT", task,
                                          chat_id="chat-g3", output_text="OUT"))
        assert calls == []                                        # holds no agent yet
        _grant("user-viewer", "pa", "contributor")
        asyncio.run(scheduler._do_deliver("sess-g3", "pa", "RESULT", task,
                                          chat_id="chat-g3", output_text="OUT"))
        assert calls and calls[0]["user_sub"] == "user-viewer"
        assert calls[0]["role"] == "contributor"

    def test_agent_scope_result_into_a_task_chat_is_not_gated(self, temp_db, monkeypatch):
        # The agent's own chats keep the agent identity.
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        task_store.create_chat("chat-g6", "task::pa", "pa")
        calls = self._spy_rungs(monkeypatch, answer="")
        task = TaskDefinition(id="task-a2", name="sub", agent="worker", prompt="p",
                              scope="agent", created_by="user-viewer")
        asyncio.run(scheduler._do_deliver("sess-g6", "pa", "RESULT", task,
                                          chat_id="chat-g6", output_text="OUT"))
        assert calls and calls[0]["user_sub"] is None and calls[0]["role"] == "manager"

    def test_admin_without_a_row_is_delivered(self, temp_db, monkeypatch):
        task_store.create_chat("chat-g4", "user-admin", "pa")
        calls = self._spy_rungs(monkeypatch, answer="")
        asyncio.run(scheduler._do_deliver("sess-g4", "pa", "RESULT", _user_task("user-admin"),
                                          chat_id="chat-g4", output_text="OUT"))
        assert calls and calls[0]["user_sub"] == "user-admin"
        assert calls[0]["role"] == "viewer"                       # the row reading, as before

    def test_no_durable_wake_for_a_person_who_lost_the_agent_meanwhile(
            self, temp_db, monkeypatch):
        _grant("user-viewer", "pa", "contributor")
        task_store.create_chat("chat-g5", "user-viewer", "pa")
        calls = self._spy_rungs(monkeypatch, during=lambda: _revoke("user-viewer", "pa"))
        asyncio.run(scheduler._do_deliver("sess-g5", "pa", "RESULT", _user_task("user-viewer"),
                                          chat_id="chat-g5", output_text="OUT"))
        assert calls                                              # delivery was tried
        assert task_store.claim_pending_delegate_wake("chat-g5") == []


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

    def test_own_prompt_matched_through_the_terminals_paste_wrapper(self, temp_db):
        # The Claude TUI journals a multi-line injected prompt wrapped as
        # pasted content, with its whitespace re-flowed: still the run's own
        # prompt, never an interjection.
        task_store.create_chat("lane-3", "user-1", "pa")
        task_store.add_chat_message(
            "lane-3", "user",
            '  <pasted_content id="adda"> [DELEGATED_WORK] do the\n  work\n'
            'please </pasted_content id="adda">',
        )
        task_store.add_chat_message("lane-3", "assistant", "borrowed")

        out = scheduler._collect_lane_output_since(
            "lane-3", 0, 0, "[DELEGATED_WORK] do the work\nplease")
        assert out == "borrowed"


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

        monkeypatch.setattr(delivery, "_do_deliver", _fake_do_deliver)

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

    def test_a_failed_run_carries_only_its_own_check_verdict(self, temp_db, monkeypatch):
        # A reused worker chat: an earlier run's failing verdict must not
        # ride a later run that failed for another reason.
        from storage.checks import db_checks
        task_store.create_chat("lane-f3", "user-1", "pa")
        db_checks.insert_verdict(
            agent="pa", owner="", check_name="answer", section="schema", status="fail",
            passed=False, score=None, findings=[], summary="no json", reason="",
            session_id="s", chat_id="lane-f3", run_id="", judge_run_id="",
            user_sub="user-1", round_no=1, ran_on="local", engine="", model="",
            cost_usd=0.0, duration_ms=1, script_sha256="")
        later = self._deliver(
            monkeypatch, self._lane_task("lane-f3"), "failed", "boom",
            worker_chat_id="lane-f3", run_started_at="2999-01-01T00:00:00+00:00",
        )
        assert later["verdict"] is None
        assert "did not pass" not in later["result_prompt"]
        same = self._deliver(
            monkeypatch, self._lane_task("lane-f3"), "failed", "boom",
            worker_chat_id="lane-f3", run_started_at="2000-01-01T00:00:00+00:00",
        )
        assert same["verdict"]["check"] == "answer"

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
        from storage.agents import agent_store
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
        assert ctx.placement.is_local


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
        monkeypatch.setattr(lanes, "_WATCHDOG_SLICE_S", 0.05)

        async def _go():
            turn = asyncio.create_task(asyncio.sleep(0.01))
            producer = asyncio.create_task(asyncio.sleep(0.01))
            pump = self._FakePump(turn, producer)
            await scheduler._watch_task_pump(
                self._FakeLayer(), pump, "run-w1", "task-run-w1", "s" * 8)
            assert not pump.aborted

        self._run(_go())

    def test_alive_process_below_ceiling_keeps_leash(self, monkeypatch):
        monkeypatch.setattr(lanes, "_WATCHDOG_SLICE_S", 0.02)
        monkeypatch.setattr(lanes, "_STALL_PROBE_SECS", 0.0)

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
        monkeypatch.setattr(lanes, "_WATCHDOG_SLICE_S", 0.02)
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
        monkeypatch.setattr(lanes, "_WATCHDOG_SLICE_S", 0.02)
        monkeypatch.setattr(lanes, "_STALL_PROBE_SECS", 0.0)

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

        monkeypatch.setattr(lanes, "_await_lane_quiescence", _fake_quiescence)

        captured: dict = {}
        done = asyncio.Event()

        async def _fake_do_deliver(session_id, agent, result_prompt, t, **kw):
            captured.update(kw, result_prompt=result_prompt)
            done.set()

        monkeypatch.setattr(delivery, "_do_deliver", _fake_do_deliver)

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
        _owner()
        task_store.create_chat("chat-k", "user-1", "pa")
        seen: dict = {}

        async def _capture(sid, agent, text, **k):
            seen.update(k)
            return ""
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _capture)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _capture)

        asyncio.run(scheduler._do_deliver(
            "sess-k", "pa", "echo prompt", _task(),
            chat_id="chat-k", output_text="R",
        ))
        assert seen.get("chat_id") == "chat-k"

    def test_pump_delivered_echo_not_double_saved(self, temp_db, monkeypatch):
        _owner()
        task_store.create_chat("chat-e", "user-1", "pa")

        async def _pumped(*a, **k):
            return ""  # pump persisted the turn itself

        async def _none(*a, **k):
            return None
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _pumped)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _none)

        asyncio.run(scheduler._do_deliver(
            "sess-e", "pa", "echo prompt", _task(),
            chat_id="chat-e", output_text="THE RESULT",
        ))
        assert len(_delegate_events("chat-e")) == 1
        assert _assistant_msgs("chat-e") == []  # no duplicate echo row
