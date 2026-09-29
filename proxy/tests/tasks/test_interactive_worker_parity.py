"""Interactive delegated-worker parity — the run-lifecycle half of delegation.

A delegated task that RUNS interactive must complete exactly like a headless
one: `_run_interactive_task` awaits the tailer's turn-end signal (registered
BEFORE the cold prompt is injected so no turn can complete unobserved), the
tailer has already persisted the turns to chat_messages, and the shared
`_collect_task_output` + `_deliver_task_result` produce an identical payload.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/tasks/test_interactive_worker_parity.py -q
"""
from __future__ import annotations

import asyncio

import pytest

from core.session import interactive_session
from services.scheduler import interactive, scheduler
from storage import database as task_store


class _FakeInteractiveSession:
    """The slice `_run_interactive_task` drives: the completion callback slot,
    the cold-prompt injection, and liveness."""

    def __init__(self):
        self.on_turn_complete = None
        self.alive = True
        self.had_viewer = False              # real sessions track viewer attach
        self.events: list[str] = []          # call-order recorder
        self.submitted: list[str] = []

    def submit_prompt(self, text: str) -> None:
        self.events.append("submit")
        self.submitted.append(text)

    def set_callback_marker(self):
        # queried via property below; kept simple
        pass


@pytest.fixture()
def fake_isess(monkeypatch):
    fake = _FakeInteractiveSession()
    monkeypatch.setitem(interactive_session._sessions, "sid-worker", fake)

    # Record the registration order: setting on_turn_complete must precede the
    # prompt injection (a turn that completes instantly must not be missed).
    orig_setattr = _FakeInteractiveSession.__setattr__

    def _tracking_setattr(self, name, value):
        if name == "on_turn_complete" and value is not None and hasattr(self, "events"):
            self.events.append("callback_registered")
        orig_setattr(self, name, value)

    monkeypatch.setattr(_FakeInteractiveSession, "__setattr__", _tracking_setattr)
    yield fake
    interactive_session._sessions.pop("sid-worker", None)


def test_turn_end_completes_the_run(fake_isess):
    async def _run():
        task = asyncio.create_task(scheduler._run_interactive_task(
            "sid-worker", "chat-w", "analyze the repo", False,
        ))
        await asyncio.sleep(0.05)
        # Cold prompt injected via the PTY flush, AFTER the callback was armed.
        assert fake_isess.events == ["callback_registered", "submit"]
        assert fake_isess.submitted == ["analyze the repo"]
        assert not task.done()               # awaiting the turn-end signal
        # The tailer reports turn-end (bg-empty + min-turn gates live in
        # interactive_session; the scheduler just gets the callback).
        fake_isess.on_turn_complete("final answer text")
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())


def test_argv_prompt_is_not_reinjected(fake_isess):
    # Codex fresh delivers the cold prompt via the launch argv — injecting it
    # again would run the prompt twice.
    async def _run():
        task = asyncio.create_task(scheduler._run_interactive_task(
            "sid-worker", "chat-w", "argv prompt", True,
        ))
        await asyncio.sleep(0.05)
        assert fake_isess.submitted == []
        fake_isess.on_turn_complete("done")
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())


def test_unregistered_session_raises(temp_db):
    async def _run():
        with pytest.raises(RuntimeError, match="not registered"):
            await scheduler._run_interactive_task("sid-ghost", "chat-g", "p", False)

    asyncio.run(_run())


def test_dead_pty_fails_fast(fake_isess):
    # CLI crash / idle reap mid-run: the watcher must fail within one liveness
    # poll (~5s), not hang until the 2h max-time backstop.
    async def _run():
        fake_isess.alive = False
        with pytest.raises(RuntimeError, match="ended before completing"):
            await asyncio.wait_for(scheduler._run_interactive_task(
                "sid-worker", "chat-w", "p", False,
            ), timeout=15)

    asyncio.run(_run())


def test_delivery_payload_matches_headless(temp_db):
    """The parity core: output collection is the SAME function over the same
    chat_messages rows, whether the pump (headless) or the tailer (interactive)
    persisted them — so `_deliver_task_result` receives an identical payload."""
    task_store.create_chat("chat-headless", "u", "pa")
    task_store.create_chat("chat-interactive", "u", "pa")
    # Headless: the pump persists assistant messages turn by turn.
    task_store.add_chat_message("chat-headless", "user", "do the thing")
    task_store.add_chat_message("chat-headless", "assistant", "Working on it.")
    task_store.add_chat_message("chat-headless", "assistant", "Here is the result.")
    # Interactive: the transcript tailer backfills the same conversation.
    task_store.add_chat_message("chat-interactive", "user", "do the thing")
    task_store.add_chat_message("chat-interactive", "assistant", "Working on it.")
    task_store.add_chat_message("chat-interactive", "assistant", "Here is the result.")

    headless = scheduler._collect_task_output("chat-headless")
    interactive = scheduler._collect_task_output("chat-interactive")
    assert headless == interactive == "Working on it.\n\nHere is the result."


class _BorrowedTerminal:
    """The slice a borrowed round drives on a LIVE interactive session: the
    prompt queue with its injection hook, the turn-end waiters, liveness."""

    def __init__(self, chat_id="chat-b", session_id="sid-borrowed", *, inject_now=True,
                 user_sub=""):
        self.chat_id = chat_id
        self.session_id = session_id
        self.user_sub = user_sub
        self.alive = True
        self.had_viewer = True
        self.created_at = 1.0
        self.inject_now = inject_now
        self.queued: list[dict] = []
        self.cancelled: list[dict] = []
        self.waiters: list = []

    def may_drive(self, sender_sub):
        return not self.user_sub or not sender_sub or sender_sub == self.user_sub

    def queue_prompt(self, text, source, **context):
        item = {"text": text, "source": source, **context}
        self.queued.append(item)
        if self.inject_now:
            item["on_injected"]()
        return item

    def cancel_prompt(self, item):
        self.cancelled.append(item)
        return True

    def add_turn_end_waiter(self, cb):
        self.waiters.append(cb)

    def remove_turn_end_waiter(self, cb):
        if cb in self.waiters:
            self.waiters.remove(cb)

    def end_turn(self, text="done"):
        waiters, self.waiters = self.waiters, []
        for cb in waiters:
            cb(text)


class TestBorrowedTerminalTurn:
    """``_run_borrowed_terminal_turn``: the follow-up is queued steer-eligible
    on the person's terminal, the waiter is armed by the injection (a turn
    end before it cannot complete the round), the round ends on the first
    turn end after it, and a cancel drops a prompt still queued."""

    def test_waits_for_the_turn_end_after_the_injection(self):
        async def _run():
            term = _BorrowedTerminal(inject_now=False)
            task = asyncio.create_task(interactive._run_borrowed_terminal_turn(term, "follow up"))
            await asyncio.sleep(0.05)
            assert [(q["text"], q["source"], q["steer"], q["chat_id"]) for q in term.queued] == [
                ("follow up", "delegate_continue", True, "chat-b")]
            assert term.waiters == []                     # not armed before the paste
            term.end_turn("the person's own turn")        # nobody listens yet
            await asyncio.sleep(0.05)
            assert not task.done()
            term.queued[0]["on_injected"]()               # the paste landed
            assert len(term.waiters) == 1
            term.end_turn("the follow-up's answer")
            await asyncio.wait_for(task, timeout=5)
            assert term.waiters == [] and term.cancelled == []

        asyncio.run(_run())

    def test_cancel_drops_a_prompt_still_queued(self):
        async def _run():
            term = _BorrowedTerminal(inject_now=False)
            task = asyncio.create_task(interactive._run_borrowed_terminal_turn(term, "follow up"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert term.cancelled == term.queued          # dropped, never pasted

        asyncio.run(_run())

    def test_cancel_after_the_injection_leaves_the_terminal_alone(self):
        async def _run():
            term = _BorrowedTerminal()
            task = asyncio.create_task(interactive._run_borrowed_terminal_turn(term, "follow up"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert term.cancelled == [] and term.waiters == []

        asyncio.run(_run())

    def test_dead_terminal_fails_fast(self):
        async def _run():
            term = _BorrowedTerminal()
            term.alive = False
            with pytest.raises(interactive._InteractiveSessionDied) as info:
                await asyncio.wait_for(
                    interactive._run_borrowed_terminal_turn(term, "p"), timeout=15)
            assert info.value.had_viewer is True

        asyncio.run(_run())

    def test_holds_serialize_rounds_on_one_chat(self):
        async def _run():
            from services.scheduler import interactive
            first = await interactive.hold_terminal("chat-h")
            second = asyncio.create_task(interactive.hold_terminal("chat-h"))
            await asyncio.sleep(0.05)
            assert not second.done()
            first.release()
            first.release()                               # idempotent
            hold = await asyncio.wait_for(second, timeout=5)
            hold.release()
            assert not interactive._terminal_locks["chat-h"].locked()

        asyncio.run(_run())


def test_a_continue_round_borrows_the_live_terminal(temp_db, monkeypatch):
    """The runner's borrowed round end to end: no config build, no layer, no
    spawn; the follow-up goes to the terminal, the tailer's rows are the
    output, the report lands, the terminal is neither closed nor stamped a
    task session, and the chat's hold is released."""
    from contextlib import asynccontextmanager
    from core import concurrency
    from core.config import task_config_builder
    from core.session import session_manager, session_state
    from services.scheduler import interactive, runner, shared
    from storage import remote_store

    chat_id = "worker-borrow"
    sid = "22222222-2222-4222-8222-222222222222"
    task_store.create_chat(chat_id, "u", "agent-x", "auto", origin="delegated",
                           execution_mode="interactive")
    task_store.update_chat(chat_id, session_id=sid)
    term = _BorrowedTerminal(chat_id=chat_id, session_id=sid)
    monkeypatch.setitem(interactive_session._sessions, sid, term)
    closed: list[str] = []

    async def _close(session_id, *, reason="closed"):
        closed.append(session_id)
        return True
    monkeypatch.setattr(interactive_session, "close_session", _close)

    async def _no_build(*a, **k):
        raise AssertionError("a borrowed round never builds a config")
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", _no_build)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
    monkeypatch.setattr(session_manager, "get_execution_layer",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no layer")))
    monkeypatch.setattr(remote_store, "resolve_execution_target", lambda *a, **k: ("local", None))

    @asynccontextmanager
    async def _instant_slot(session_id, target="", execution_path=None):
        yield
    monkeypatch.setattr(concurrency, "task_slot", _instant_slot)
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
    session_state._sessions[sid] = {"client_type": "dashboard", "is_task": True}

    task = shared.TaskDefinition(
        id="t-borrow", name="follow", agent="agent-x", prompt="also this",
        scope="agent", notification_mode="none", continue_session=sid,
        target_chat_id=chat_id,
    )
    run_id = "run-borrow"
    task_store.create_run(run_id, task.id, task.agent, "manual", None, task.prompt,
                          "one-time", task.scope, None)
    shared._active_task_ids[task.id] = run_id

    async def _run():
        t = asyncio.get_running_loop().create_task(
            runner._run_task(run_id, sid, task, task.prompt, "manual", None, 1))
        shared._running_tasks[run_id] = t
        t.add_done_callback(lambda _: shared._running_tasks.pop(run_id, None))
        for _ in range(100):
            if term.waiters:
                break
            await asyncio.sleep(0.05)
        assert term.queued and term.queued[0]["text"] == "also this"
        assert interactive._terminal_locks[chat_id].locked()
        # The tailer persists the terminal's turn, then the turn ends.
        task_store.add_chat_message(chat_id, "user", "also this")
        task_store.add_chat_message(chat_id, "assistant", "terminal answer")
        term.end_turn("terminal answer")
        await asyncio.wait_for(t, timeout=20)
        return dict(session_state._sessions.get(sid) or {})

    try:
        entry_after = asyncio.run(_run())
    finally:
        session_state._sessions.pop(sid, None)
        interactive._terminal_locks.pop(chat_id, None)
    run = task_store.get_run(run_id)
    assert run["status"] == "completed"
    assert run["output_text"] == "terminal answer"
    assert run["session_id"] == sid
    assert closed == []                                   # the person's terminal stays
    # The dashboard's session-index entry survives the round, un-stamped.
    assert entry_after == {"client_type": "dashboard"}


def test_a_continue_round_never_drives_another_persons_terminal(temp_db, monkeypatch):
    """A terminal runs as whoever warmed it: a round created by someone
    else fails with a clear reason, types nothing into it, spawns nothing
    beside it and leaves it open."""
    from contextlib import asynccontextmanager
    from core import concurrency
    from core.config import task_config_builder
    from core.session import session_manager, session_state
    from services.scheduler import interactive, runner, shared
    from storage import remote_store

    chat_id = "worker-foreign"
    sid = "33333333-3333-4333-8333-333333333333"
    task_store.create_chat(chat_id, "agent::agent-x", "agent-x", "auto",
                           execution_mode="interactive")
    task_store.update_chat(chat_id, session_id=sid)
    term = _BorrowedTerminal(chat_id=chat_id, session_id=sid, user_sub="user-bob")
    monkeypatch.setitem(interactive_session._sessions, sid, term)
    closed: list[str] = []

    async def _close(session_id, *, reason="closed"):
        closed.append(session_id)
        return True
    monkeypatch.setattr(interactive_session, "close_session", _close)

    async def _no_build(*a, **k):
        raise AssertionError("a refused round never builds a config")
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", _no_build)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity(
                            "alice", "editor", "user", "user-alice"))
    monkeypatch.setattr(session_manager, "get_execution_layer",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no layer")))
    monkeypatch.setattr(remote_store, "resolve_execution_target", lambda *a, **k: ("local", None))

    @asynccontextmanager
    async def _instant_slot(session_id, target="", execution_path=None):
        yield
    monkeypatch.setattr(concurrency, "task_slot", _instant_slot)
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

    task = shared.TaskDefinition(
        id="t-foreign", name="follow", agent="agent-x", prompt="also this",
        scope="user", created_by="user-alice", notification_mode="none",
        continue_session=sid, target_chat_id=chat_id,
    )
    run_id = "run-foreign"
    task_store.create_run(run_id, task.id, task.agent, "manual", None, task.prompt,
                          "one-time", task.scope, "user-alice")
    shared._active_task_ids[task.id] = run_id

    async def _run():
        await asyncio.wait_for(
            runner._run_task(run_id, "run-sid-foreign", task, task.prompt, "manual", None, 1),
            timeout=20)

    try:
        asyncio.run(_run())
    finally:
        interactive._terminal_locks.pop(chat_id, None)
    run = task_store.get_run(run_id)
    assert run["status"] == "failed"
    assert "belongs to another person" in (run["error_message"] or "")
    # The person's terminal: nothing typed, not closed (the run's own,
    # never-started session id is what the failure path closes).
    assert term.queued == [] and sid not in closed
