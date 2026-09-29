"""Server-prompt injection queue on InteractiveSession (delegate results …).

Exercises the quiescence gates one by one against real `cat`-under-PTY
sessions (same harness as test_interactive_session.py): a queued prompt is
injected ONLY when the CLI is idle — turn closed (transcript-derived), composer
clean, output quiet, no pending cold-submit / permission — and close() hands
undelivered items back to the delivery ladder with the PTY/WS rungs excluded.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/session/test_interactive_prompt_queue.py -q
"""
import asyncio
import os
import time

import pytest
import pytest_asyncio

import config  # noqa: F401  (ensures conftest path/env setup ran)
from core import concurrency
from core.session import interactive_session as isess
from core.session.session_delivery import DeliveryOutcome

_ENV = {"TERM": "xterm-256color", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


async def _register(session_id, *, chat_id="chat-q"):
    return await isess.register(
        session_id=session_id, chat_id=chat_id, agent_name="agent",
        argv=["cat"], env=dict(_ENV),
    )


def _make_idle(s):
    """Force the session into the injectable state: ready, mature, quiet."""
    s._mark_ready()
    s.created_at = time.monotonic() - 60          # past MIN_TURN_S
    s.last_activity = time.monotonic() - 10       # output quiet
    s._cancel_deferred_submit()                   # _mark_ready flush may arm it


async def _settle_echo(s):
    """Let pending PTY echo land, then re-open the quiet + submit gates —
    `cat`'s echo of a prior write counts as output activity (correctly resets
    the quiet clock in production; here we fast-forward past it), and a first
    submit legitimately arms the deferred Enter (cancel it so each test
    isolates ITS gate)."""
    await asyncio.sleep(0.3)
    s._cancel_deferred_submit()
    s.last_activity = time.monotonic() - 10


async def _drain_and_settle(s):
    await s._try_drain_prompt_queue()
    await asyncio.sleep(0.25)                     # let the PTY echo


@pytest_asyncio.fixture(autouse=True)
async def _clean_registry():
    concurrency.init()
    isess._lock = None
    isess._sessions.clear()
    concurrency._sessions.clear()
    concurrency._session_added_at.clear()
    real_live = concurrency._live_available_mb
    concurrency._live_available_mb = lambda: 32768
    yield
    concurrency._live_available_mb = real_live
    await isess.close_all(reason="test-teardown")
    isess._sessions.clear()
    concurrency._sessions.clear()
    concurrency._session_added_at.clear()


@pytest.mark.asyncio
class TestPromptQueueGates:
    async def test_injects_when_idle(self):
        s = await _register("sid-inj")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        item = s.queue_prompt("run the report", "delegate_result")
        assert item and item["text"] == "run the report"
        await _drain_and_settle(s)
        assert b"run the report" in bytes(received)   # pasted into the PTY
        assert s._turn_open is True                    # injection opened a turn
        assert not s._prompt_queue

    async def test_turn_open_holds_until_end_turn(self):
        s = await _register("sid-open")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s._turn_open = True
        s.queue_prompt("held prompt", "delegate_result")
        await _drain_and_settle(s)
        assert b"held prompt" not in bytes(received)
        assert len(s._prompt_queue) == 1
        # The turn closes (tailer end_turn) → the drain fires and injects.
        s._apply_turn_signal("end_turn")
        s.last_activity = time.monotonic() - 10
        await _drain_and_settle(s)
        assert b"held prompt" in bytes(received)

    async def test_one_item_per_turn(self):
        s = await _register("sid-fifo")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s.queue_prompt("first item", "delegate_result")
        s.queue_prompt("second item", "delegate_result")
        await _drain_and_settle(s)
        assert b"first item" in bytes(received)
        assert b"second item" not in bytes(received)   # waits for the turn to close
        assert len(s._prompt_queue) == 1
        s._apply_turn_signal("end_turn")
        await _settle_echo(s)                          # injection #1's echo + Enter
        await _drain_and_settle(s)
        assert b"second item" in bytes(received)

    async def test_composer_dirty_holds_submit_clears(self):
        s = await _register("sid-dirty")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s.write_input(b"partial user tex")             # typing, no CR
        assert s._composer_dirty is True
        s.queue_prompt("blocked by composer", "delegate_result")
        s.last_activity = time.monotonic() - 10        # quiet, but still dirty
        await _drain_and_settle(s)
        assert b"blocked by composer" not in bytes(received)
        # The user submits → composer clears → next drain injects (after the
        # submit's own echo + deferred Enter settle, which legitimately hold).
        s.write_input(b"t\r")
        assert s._composer_dirty is False
        await _settle_echo(s)
        await _drain_and_settle(s)
        assert b"blocked by composer" in bytes(received)

    async def test_replies_and_mouse_do_not_dirty(self):
        s = await _register("sid-replies")
        _make_idle(s)
        s.write_input(b"\x1b[<35;70;68M")              # SGR mouse move
        assert s._composer_dirty is False
        s.write_input(b"\x1b[27;5R")                   # CPR reply (stripped whole)
        assert s._composer_dirty is False
        s.write_input(b"\x1b[?1;2c")                   # Primary DA reply
        assert s._composer_dirty is False

    async def test_ctrl_c_clears_dirty(self):
        s = await _register("sid-ctrlc")
        _make_idle(s)
        s.write_input(b"abandoned draft")
        assert s._composer_dirty is True
        s.write_input(b"\x03")
        assert s._composer_dirty is False

    async def test_stale_dirty_expires_and_injects(self):
        # Wheel scroll on a TUI alt-screen arrives as arrow keys — "typing"
        # with no Enter ever coming. Without the TTL this wedged queued
        # delegate results forever (observed live: depth=3, never injected).
        s = await _register("sid-dirty-ttl")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s.write_input(b"\x1b[A\x1b[A")
        assert s._composer_dirty is True
        s.queue_prompt("after ttl", "delegate_result")
        s.last_activity = time.monotonic() - 10
        await _drain_and_settle(s)
        assert b"after ttl" not in bytes(received)     # fresh dirty holds
        await _settle_echo(s)                          # scroll echo ≠ quiet reset here
        s._composer_dirty_at = time.monotonic() - 200  # past the 180s TTL
        await _drain_and_settle(s)
        assert b"after ttl" in bytes(received)

    async def test_cold_submit_pending_holds(self):
        s = await _register("sid-cold")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        # A pending cold-submit Enter (the user's first prompt in flight) —
        # injecting now would cancel it and merge the prompts.
        s._submit_settle_handle = s._loop.call_later(60, lambda: None)
        s.queue_prompt("too early", "delegate_result")
        await _drain_and_settle(s)
        assert b"too early" not in bytes(received)
        s._cancel_deferred_submit()
        await _drain_and_settle(s)
        assert b"too early" in bytes(received)

    async def test_pending_permission_holds(self):
        from core.session.session_state import _session_permission_requests
        s = await _register("sid-perm")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        _session_permission_requests["sid-perm"] = {"req-1"}
        try:
            s.queue_prompt("would answer the dialog", "delegate_result")
            await _drain_and_settle(s)
            assert b"would answer the dialog" not in bytes(received)
        finally:
            _session_permission_requests.pop("sid-perm", None)
        await _drain_and_settle(s)
        assert b"would answer the dialog" in bytes(received)

    async def test_forced_tail_flips_stale_turn_open(self, monkeypatch):
        # Permission-window regression: the 3s debounce means _turn_open can be
        # stale-False while a turn runs quietly. The pre-inject forced tail must
        # apply the transcript's REAL state before the gates decide.
        from core.session import transcript_tailer
        s = await _register("sid-stale")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        monkeypatch.setattr(
            transcript_tailer, "resolve_and_tail",
            lambda sid, cid: {"persisted": 0, "last_signal": "user"},
        )
        s.queue_prompt("mid-turn injection", "delegate_result")
        await _drain_and_settle(s)
        assert b"mid-turn injection" not in bytes(received)
        assert s._turn_open is True                    # tail flipped the flag

    async def test_otodock_attached_holds_proxy_side(self):
        s = await _register("sid-otodock")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s.otodock_attached = True
        s.queue_prompt("terminal owned", "delegate_result")
        await _drain_and_settle(s)
        assert b"terminal owned" not in bytes(received)
        # The local terminal detaches → the proxy path takes over the queue.
        s.otodock_attached = False
        await _drain_and_settle(s)
        assert b"terminal owned" in bytes(received)

    async def test_queue_prompt_on_closed_session_returns_none(self):
        s = await _register("sid-deadq")
        await s.close()
        assert s.queue_prompt("late", "delegate_result") is None

    async def test_turn_close_kicks_queue_without_backstop(self, monkeypatch):
        # A queued prompt lands right after the turn closes (the turn-end
        # effects schedule a drain past the quiet window) — NOT on the 5s
        # backstop cadence. Backstop pushed out of reach to prove the kick
        # is what delivers.
        monkeypatch.setattr(isess, "_INJECT_QUIET_S", 0.05)
        monkeypatch.setattr(isess, "_INJECT_BACKSTOP_S", 60.0)
        s = await _register("sid-kick")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s._turn_open = True
        s.queue_prompt("kicked prompt", "delegate_result")
        await asyncio.sleep(0.1)
        assert b"kicked prompt" not in bytes(received)  # held by the open turn
        s.last_activity = time.monotonic() - 10
        s._apply_turn_signal("end_turn")
        await asyncio.sleep(0.6)
        assert b"kicked prompt" in bytes(received)


@pytest.mark.asyncio
class TestCloseHandback:
    async def test_close_hands_pending_back_without_pty_ws(self, monkeypatch):
        calls = []
        outcomes = []

        async def _fake_deliver(chat_id, text, **kw):
            calls.append({"chat_id": chat_id, "text": text, **kw})
            return DeliveryOutcome("pump", chat_id=chat_id)

        from core.session import session_delivery
        monkeypatch.setattr(session_delivery, "deliver_prompt", _fake_deliver)

        s = await _register("sid-handback", chat_id="chat-hb")
        s._turn_open = True                            # never injectable
        s.queue_prompt(
            "undelivered result", "delegate_result",
            chat_id="chat-hb", agent="pa", role="manager", hops=0,
            on_outcome=outcomes.append,
        )
        await s.close()
        await asyncio.sleep(0.1)                       # handback task runs

        assert len(calls) == 1
        call = calls[0]
        assert call["chat_id"] == "chat-hb"
        assert call["text"] == "undelivered result"
        assert call["allow_pty"] is False and call["allow_ws"] is False
        assert call["hops"] == 1
        assert outcomes and outcomes[0].path == "pump"  # on_outcome completed
        assert not s._prompt_queue

    async def test_handback_hops_cap_drops_item(self, monkeypatch):
        calls = []

        async def _fake_deliver(chat_id, text, **kw):
            calls.append(text)
            return DeliveryOutcome("none")

        from core.session import session_delivery
        monkeypatch.setattr(session_delivery, "deliver_prompt", _fake_deliver)

        s = await _register("sid-hops", chat_id="chat-hops")
        s._turn_open = True
        s.queue_prompt("looping item", "delegate_result", chat_id="chat-hops", hops=2)
        await s.close()
        await asyncio.sleep(0.1)
        assert calls == []                             # dropped at the cap


@pytest.mark.asyncio
async def test_close_session_drop_queued_hands_nothing_back(monkeypatch):
    """The offboarding closer ends a session through the public argument:
    its queued server prompts are dropped, never handed back to the ladder."""
    calls = []

    async def _fake_deliver(chat_id, text, **kw):
        calls.append(text)
        return DeliveryOutcome("pump", chat_id=chat_id)

    from core.session import session_delivery
    monkeypatch.setattr(session_delivery, "deliver_prompt", _fake_deliver)

    s = await _register("sid-dropq", chat_id="chat-dropq")
    s._turn_open = True
    s.queue_prompt("queued result", "delegate_result", chat_id="chat-dropq", hops=0)
    assert await isess.close_session("sid-dropq", reason="offboarded", drop_queued=True)
    await asyncio.sleep(0.1)
    assert calls == []
    assert not s._prompt_queue


class _FakeConnMgr:
    def __init__(self, *, supports=True):
        self.supports = supports
        self.sent: list[dict] = []

    def satellite_supports_pty_inject(self, machine_id):
        return self.supports

    async def send_fire_and_forget(self, machine_id, msg):
        self.sent.append(msg)


@pytest.mark.asyncio
class TestSatelliteInjectPath:
    async def _attached_session(self, monkeypatch, *, supports=True):
        mgr = _FakeConnMgr(supports=supports)
        import core.remote.satellite_connection as sc
        monkeypatch.setattr(sc, "get_connection_manager", lambda: mgr)
        s = await _register("sid-sat", chat_id="chat-sat")
        _make_idle(s)
        s.target = "machine-1"                        # otodock sessions are remote
        s.otodock_attached = True
        return s, mgr

    async def test_sends_pty_inject_and_pops_on_ack(self, monkeypatch):
        s, mgr = await self._attached_session(monkeypatch)
        s.queue_prompt("the result", "delegate_result", chat_id="chat-sat")
        await asyncio.sleep(0.05)                     # queue_prompt's drain task
        assert len(mgr.sent) == 1
        frame = mgr.sent[0]
        assert frame["type"] == "pty_inject"
        assert frame["text"] == "the result"
        assert len(s._prompt_queue) == 1              # head stays until the ACK
        s.handle_inject_result(frame["inject_id"], True)
        assert not s._prompt_queue
        assert s._turn_open is True                   # injection opened a turn

    async def test_nack_keeps_item_queued(self, monkeypatch):
        s, mgr = await self._attached_session(monkeypatch)
        s.queue_prompt("held result", "delegate_result", chat_id="chat-sat")
        await asyncio.sleep(0.05)
        frame = mgr.sent[0]
        s.handle_inject_result(frame["inject_id"], False, "busy")
        assert len(s._prompt_queue) == 1              # retried on the backstop
        assert s._satellite_inject is None            # slot freed for the retry

    async def test_stale_result_ignored(self, monkeypatch):
        s, mgr = await self._attached_session(monkeypatch)
        s.queue_prompt("the result", "delegate_result", chat_id="chat-sat")
        await asyncio.sleep(0.05)
        s.handle_inject_result("not-the-inflight-id", True)
        assert len(s._prompt_queue) == 1              # unknown id → no pop

    async def test_old_satellite_holds_queue(self, monkeypatch):
        s, mgr = await self._attached_session(monkeypatch, supports=False)
        s.queue_prompt("the result", "delegate_result", chat_id="chat-sat")
        await asyncio.sleep(0.05)
        assert mgr.sent == []                         # frame never sent
        assert len(s._prompt_queue) == 1              # held for detach/close


@pytest.mark.asyncio
class TestSteerItems:
    """steer=True items (deliver_prompt rung 1 marks all of them): with an
    OPEN turn on a local PTY they inject mid-turn — both TUIs consume typed
    input between tool calls — bypassing turn_open/not_quiet/bg_pending while
    every safety gate (dialog, dirty composer, cold submit) still holds. With
    the turn closed the flag is inert (ordinary quiescence inject)."""

    async def test_steer_injects_into_open_turn(self):
        s = await _register("sid-steer")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s._turn_open = True
        s.last_activity = time.monotonic()            # streaming: never quiet
        assert s.queue_prompt("steer this in", "delegate_result", steer=True)
        await _drain_and_settle(s)
        assert b"steer this in" in bytes(received)
        assert s._turn_open is True                   # the turn stays open

    async def test_steer_flag_inert_between_turns(self):
        s = await _register("sid-steer-idle")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s.last_activity = time.monotonic()            # turn CLOSED, not quiet
        s.queue_prompt("wait for quiet", "delegate_result", steer=True)
        await _drain_and_settle(s)
        assert b"wait for quiet" not in bytes(received)   # ordinary inject rules
        s.last_activity = time.monotonic() - 10
        await _drain_and_settle(s)
        assert b"wait for quiet" in bytes(received)

    async def test_steer_still_blocked_by_permission_dialog(self):
        from core.session.session_state import _session_permission_requests
        s = await _register("sid-steer-perm")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s._turn_open = True
        _session_permission_requests["sid-steer-perm"] = {"req-1"}
        try:
            s.queue_prompt("would answer dialog", "delegate_result", steer=True)
            await _drain_and_settle(s)
            assert b"would answer dialog" not in bytes(received)
        finally:
            _session_permission_requests.pop("sid-steer-perm", None)
        await _drain_and_settle(s)                    # dialog resolved → steers
        assert b"would answer dialog" in bytes(received)

    async def test_steer_still_blocked_by_composer_dirty(self):
        s = await _register("sid-steer-dirty")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s._turn_open = True
        s._composer_dirty = True
        s.queue_prompt("typed-over", "delegate_result", steer=True)
        await _drain_and_settle(s)
        assert b"typed-over" not in bytes(received)
        assert len(s._prompt_queue) == 1

    async def test_steer_never_jumps_a_non_steer_head(self):
        # FIFO is never reordered: a steer item behind a normal one waits with
        # it (head eligibility decides the gate run).
        s = await _register("sid-steer-mixed")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s._turn_open = True
        s.queue_prompt("normal first", "delegate_result")
        s.queue_prompt("steer second", "delegate_result", steer=True)
        await _drain_and_settle(s)
        assert b"normal first" not in bytes(received)
        assert b"steer second" not in bytes(received)
        assert len(s._prompt_queue) == 2


@pytest.mark.asyncio
class TestInjectionHooks:
    """The hooks a borrowed-terminal delegate round drives: ``on_injected``
    fires once the prompt is in the terminal (local paste or satellite ACK),
    the one-shot turn-end waiters fire at the task callback's point with its
    gates, and a queued item can be dropped by its handle."""

    async def test_on_injected_fires_after_the_paste(self):
        s = await _register("sid-hook")
        _make_idle(s)
        fired: list[str] = []
        item = s.queue_prompt("go", "delegate_continue", on_injected=lambda: fired.append("in"))
        assert item and fired == []                      # queued, not yet pasted
        await _drain_and_settle(s)
        assert fired == ["in"] and not s._prompt_queue

    async def test_cancel_prompt_drops_only_a_queued_item(self):
        s = await _register("sid-cancel")
        _make_idle(s)
        s._turn_open = True                              # held: never injected
        item = s.queue_prompt("later", "delegate_continue")
        await _drain_and_settle(s)
        assert len(s._prompt_queue) == 1
        assert s.cancel_prompt(item) is True
        assert not s._prompt_queue
        assert s.cancel_prompt(item) is False            # already gone

    async def test_turn_end_waiters_fire_once_with_the_gates(self):
        s = await _register("sid-waiter")
        _make_idle(s)
        s._title_armed = False
        seen: list[str] = []
        s.add_turn_end_waiter(seen.append)
        # A question park is not a turn end for a waiter.
        s._maybe_fire_turn_complete("", persisted=1, question=True)
        assert seen == [] and len(s._turn_end_waiters) == 1
        s._maybe_fire_turn_complete("the answer", persisted=1)
        assert seen == ["the answer"] and s._turn_end_waiters == []
        s._maybe_fire_turn_complete("another turn", persisted=1)
        assert seen == ["the answer"]                    # one-shot
        s.add_turn_end_waiter(seen.append)
        s.remove_turn_end_waiter(seen.append)
        s._maybe_fire_turn_complete("removed", persisted=1)
        assert seen == ["the answer"]

    async def test_satellite_ack_fires_on_injected(self, monkeypatch):
        mgr = _FakeConnMgr()
        import core.remote.satellite_connection as sc
        monkeypatch.setattr(sc, "get_connection_manager", lambda: mgr)
        s = await _register("sid-hook-sat", chat_id="chat-hook-sat")
        _make_idle(s)
        s.target = "machine-1"
        s.otodock_attached = True
        fired: list[str] = []
        s.queue_prompt("remote go", "delegate_continue", chat_id="chat-hook-sat",
                       on_injected=lambda: fired.append("in"))
        await asyncio.sleep(0.05)
        frame = mgr.sent[0]
        assert fired == []                               # sent, not yet in the terminal
        s.handle_inject_result(frame["inject_id"], True)
        assert fired == ["in"] and not s._prompt_queue

    async def test_cancel_in_flight_satellite_inject_never_pops_another_item(self, monkeypatch):
        # A borrowed-terminal round is cancelled while the satellite types its
        # prompt: the ACK must not take the NEXT item off the queue and report
        # it injected — that item was never sent.
        mgr = _FakeConnMgr()
        import core.remote.satellite_connection as sc
        monkeypatch.setattr(sc, "get_connection_manager", lambda: mgr)
        s = await _register("sid-hook-sat-cancel", chat_id="chat-hook-sat-cancel")
        _make_idle(s)
        s.target = "machine-1"
        s.otodock_attached = True
        fired: list[str] = []
        first = s.queue_prompt("round prompt", "delegate_continue",
                               on_injected=lambda: fired.append("first"))
        s.queue_prompt("next prompt", "delegate_result",
                       on_injected=lambda: fired.append("next"))
        await asyncio.sleep(0.05)
        frame, = mgr.sent
        assert frame["text"] == "round prompt"
        cancelled = s.cancel_prompt(first)
        s.handle_inject_result(frame["inject_id"], True)
        assert fired == []
        assert [q["text"] for q in s._prompt_queue] == ["next prompt"]
        assert s._turn_open is True                      # the typed prompt opened a turn
        assert cancelled is False                        # it was on its way into the terminal

    async def test_cancelled_in_flight_item_is_never_re_sent(self, monkeypatch):
        # The result frame is lost after the cancel: the retry sends the next
        # item under a fresh id (a re-used id would be ACKed by the
        # satellite's dedupe without typing it).
        mgr = _FakeConnMgr()
        import core.remote.satellite_connection as sc
        monkeypatch.setattr(sc, "get_connection_manager", lambda: mgr)
        s = await _register("sid-hook-sat-lost", chat_id="chat-hook-sat-lost")
        _make_idle(s)
        s.target = "machine-1"
        s.otodock_attached = True
        first = s.queue_prompt("round prompt", "delegate_continue")
        s.queue_prompt("next prompt", "delegate_result")
        await asyncio.sleep(0.05)
        s.cancel_prompt(first)
        s._satellite_inject["sent_at"] -= 3600           # the result never came
        await s._try_satellite_inject()
        old, retry = mgr.sent
        assert retry["text"] == "next prompt" and retry["inject_id"] != old["inject_id"]
        s.handle_inject_result(retry["inject_id"], True)
        assert not s._prompt_queue

    async def test_cancel_during_the_freshness_tail_re_reads_steer_eligibility(self, monkeypatch):
        # The steer head is cancelled while the drain's pre-inject tail runs:
        # the item behind it is an ordinary one and must wait for the turn to
        # close instead of riding the old head's steer pass into it.
        from core.session import transcript_tailer
        s = await _register("sid-steer-cancel")
        received = bytearray()
        s.add_output_listener(received.extend)
        _make_idle(s)
        s._turn_open = True
        s.last_activity = time.monotonic()               # streaming: never quiet
        head = s.queue_prompt("steer head", "delegate_continue", steer=True)
        s.queue_prompt("ordinary next", "delegate_result")

        def _tail(sid, cid):
            s.cancel_prompt(head)
            return {"persisted": 0, "last_signal": "user"}

        monkeypatch.setattr(transcript_tailer, "resolve_and_tail", _tail)
        await _drain_and_settle(s)
        assert b"ordinary next" not in bytes(received)
        assert [q["text"] for q in s._prompt_queue] == ["ordinary next"]

    async def test_waiter_skips_an_end_whose_batch_opens_the_next_turn(self, monkeypatch):
        # A follow-up steered in while the person's turn wrote its final text
        # runs as the next turn: the batch carrying the person's end AND that
        # prompt must not answer the waiter with the person's reply.
        from core.session import transcript_tailer
        s = await _register("sid-waiter-reopen")
        _make_idle(s)
        s._title_armed = False
        seen: list[str] = []
        s.add_turn_end_waiter(seen.append)
        batches = iter([
            {"persisted": 2, "turn_complete": True, "last_signal": "user",
             "last_message": "the person's reply"},
            {"persisted": 1, "turn_complete": True, "last_signal": "end_turn",
             "last_message": "the follow-up's answer"},
        ])
        monkeypatch.setattr(transcript_tailer, "resolve_and_tail",
                            lambda sid, cid: next(batches))
        await s._tail_and_maybe_complete()
        assert seen == [] and s._turn_open is True
        await s._tail_and_maybe_complete()
        assert seen == ["the follow-up's answer"]

    async def test_the_drains_freshness_tail_reports_the_turn_end_it_reads(self, monkeypatch):
        # The pre-inject tail moves the cursor past its batch: a turn end in
        # it is reported there (the finished ping, the waiters) or never.
        from core.session import transcript_tailer
        s = await _register("sid-drain-end")
        _make_idle(s)
        s._title_armed = False
        seen: list[str] = []
        pings: list[bool] = []
        monkeypatch.setattr(s, "_fire_turn_notification",
                            lambda question=False, compacted=False: pings.append(question))
        s.add_turn_end_waiter(seen.append)
        monkeypatch.setattr(transcript_tailer, "resolve_and_tail", lambda sid, cid: {
            "persisted": 1, "turn_complete": True, "last_signal": "end_turn",
            "last_message": "the earlier answer"})
        s.queue_prompt("next prompt", "delegate_result")
        await _drain_and_settle(s)
        assert seen == ["the earlier answer"]
        assert pings == [False]
        assert not s._prompt_queue                       # and the prompt still went in
