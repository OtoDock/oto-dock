"""Tests for core.session.interactive_session — registry, lease, idle reaper, drainer.

DB-light: spawns real `cat` processes under a PTY via pty_relay and exercises
the registry/lease/slot/idle/drainer wiring. Uses the real concurrency slot
pool (init() falls back to defaults without a DB).
"""
import asyncio
import os
import time

import pytest
import pytest_asyncio

import config  # noqa: F401  (ensures conftest path/env setup ran)
from core import concurrency
from core.session import interactive_session as isess
from core.session.session_state import get_permission_queue

_ENV = {"TERM": "xterm-256color", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _ensure_concurrency():
    # init() is idempotent enough for tests; it (re)creates the lock + limits.
    concurrency.init()


async def _register(session_id, *, argv=("cat",), chat_id="chat-1"):
    return await isess.register(
        session_id=session_id, chat_id=chat_id, agent_name="agent",
        argv=list(argv), env=dict(_ENV),
    )


async def _noop():
    return None


@pytest_asyncio.fixture(autouse=True)
async def _clean_registry():
    # pytest-asyncio gives each test a fresh event loop; rebind the module
    # locks to it (production has a single long-lived loop, so this is a
    # test-isolation concern only).
    _ensure_concurrency()
    isess._lock = None
    isess._sessions.clear()
    concurrency._sessions.clear()
    concurrency._session_added_at.clear()
    # Pin the live-RAM veto: it reads the HOST's real free memory, so on a
    # loaded dev box it denies the slot and every register() here fails.
    # The reservation-budget gate stays real — only Gate 2 is made
    # deterministic.
    real_live = concurrency._live_available_mb
    concurrency._live_available_mb = lambda: 32768
    yield
    concurrency._live_available_mb = real_live
    await isess.close_all(reason="test-teardown")
    isess._sessions.clear()
    concurrency._sessions.clear()
    concurrency._session_added_at.clear()


@pytest.mark.asyncio
class TestInteractiveSession:
    async def test_register_tracks_and_acquires_slot(self):
        s = await _register("sid-track")
        try:
            assert isess.get("sid-track") is s
            assert s.alive
            assert "sid-track" in isess.live_session_ids()
            assert "sid-track" in concurrency._sessions
        finally:
            await s.close()
        assert isess.get("sid-track") is None
        assert "sid-track" not in concurrency._sessions
        assert not s.alive

    async def test_lease_supersedes_existing(self):
        first = await _register("sid-lease")
        first_pid = first.pty.pid
        second = await _register("sid-lease")  # same id → kills the first
        try:
            assert second is not first
            assert second.pty.pid != first_pid
            assert isess.get("sid-lease") is second
            assert second.alive
            # The superseded process is torn down.
            assert first._closed
            assert first.pty.closed
        finally:
            await second.close()

    async def test_output_fanout_and_scrollback_replay(self):
        s = await _register("sid-out")
        try:
            received = bytearray()
            replay = s.add_output_listener(received.extend)
            assert isinstance(replay, (bytes, bytearray))
            s._mark_ready()  # `cat` emits no startup output → bypass the readiness gate
            s.write_input(b"ping\n")
            await asyncio.sleep(0.3)
            assert b"ping" in bytes(received)
            assert s.has_viewer
            s.remove_output_listener(next(iter(s._output_listeners)))
            assert not s.has_viewer
        finally:
            await s.close()

    async def test_input_buffered_until_ready(self):
        # Readiness gate: input written before the TUI is ready is buffered, then
        # flushed once ready (forced here via _mark_ready — `cat` emits no startup
        # output that would trigger the settle). Prevents the first prompt from
        # being sent before the CLI can accept it.
        s = await _register("sid-gate")
        try:
            received = bytearray()
            s.add_output_listener(received.extend)
            assert s._ready is False
            s.write_input(b"buffered\n")
            await asyncio.sleep(0.2)
            assert b"buffered" not in bytes(received)  # buffered, not echoed yet
            s._mark_ready()
            await asyncio.sleep(0.3)
            assert b"buffered" in bytes(received)  # flushed on ready → echoed
            assert s._ready is True
        finally:
            await s.close()

    async def test_cold_flush_submits_after_settle(self):
        # The cold first-prompt flush writes the text body immediately on ready,
        # then sends Enter only after the echo settles (deferred submit) — fixes
        # the "prompt sits unsent on turn 1" bug (a fixed short delay missed the
        # freshly-rendered TUI). A bare-Enter is sent eventually so it submits.
        s = await _register("sid-submit")
        try:
            writes: list[bytes] = []
            orig = s.pty.write
            s.pty.write = lambda b: (writes.append(bytes(b)), orig(b))[1]
            s.write_input(b"hello\r")          # buffered (not ready)
            assert all(b"hello" not in w for w in writes)
            s._mark_ready()                     # flush → body now, Enter deferred
            await asyncio.sleep(0.05)
            assert any(b"hello" in w for w in writes)   # body written immediately
            assert b"\r" not in writes                  # Enter NOT sent yet
            await asyncio.sleep(0.8)                     # echo settles → Enter fires
            assert b"\r" in writes                       # submitted
        finally:
            await s.close()

    async def test_cold_flush_retries_when_unconfirmed(self, monkeypatch):
        # Confirm-and-retry: the readiness settle is a heuristic — a flush can
        # land in a still-booting composer and be swallowed. With no transcript
        # user signal (`cat` journals nothing), the watcher must re-emit the
        # flushed body after _COLD_RETRY_S.
        monkeypatch.setattr(isess, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(isess, "_COLD_RETRY_S", 0.3)
        s = await _register("sid-retry")
        try:
            writes: list[bytes] = []
            orig = s.pty.write
            s.pty.write = lambda b: (writes.append(bytes(b)), orig(b))[1]
            s.write_input(b"hello\r")
            s._mark_ready()
            await asyncio.sleep(1.2)  # Enter fires, watcher window elapses
            assert sum(1 for w in writes if b"hello" in w) >= 2  # re-emitted
            assert s._cold_flush_attempts >= 1
        finally:
            await s.close()

    async def test_cold_flush_confirmed_turn_stops_retry(self, monkeypatch):
        # The transcript journals the user line (turn opens) before the watcher
        # window ends → the retry stands down; the body is written exactly once.
        monkeypatch.setattr(isess, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(isess, "_COLD_RETRY_S", 0.5)
        s = await _register("sid-confirmed")
        try:
            writes: list[bytes] = []
            orig = s.pty.write
            s.pty.write = lambda b: (writes.append(bytes(b)), orig(b))[1]
            s.write_input(b"hello\r")
            s._mark_ready()
            await asyncio.sleep(0.3)            # Enter fired, watcher armed
            s._apply_turn_signal("user")        # transcript confirms the turn
            assert s._cold_flush_pending == b""  # cleared on turn-open
            await asyncio.sleep(0.7)             # past the watcher window
            assert sum(1 for w in writes if b"hello" in w) == 1
            assert s._cold_flush_attempts == 0
        finally:
            await s.close()

    async def test_cold_flush_retries_exhaust_with_warning(self, monkeypatch, caplog):
        # Retries are bounded: after _COLD_RETRY_MAX unconfirmed replays the
        # watcher gives up with a WARNING — never loops, never raises.
        monkeypatch.setattr(isess, "_SUBMIT_SETTLE_S", 0.05)
        monkeypatch.setattr(isess, "_COLD_RETRY_S", 0.15)
        monkeypatch.setattr(isess, "_COLD_RETRY_MAX", 1)
        s = await _register("sid-exhaust")
        try:
            s.write_input(b"hello\r")
            with caplog.at_level("WARNING"):
                s._mark_ready()
                await asyncio.sleep(1.5)  # first Enter + retry + give-up cycle
            assert s._cold_flush_attempts == 1
            assert s._cold_flush_pending == b""
            assert "giving up" in caplog.text
        finally:
            await s.close()

    async def test_ready_settle_floored_by_ready_min(self, monkeypatch):
        # READY_MIN_S: on a fresh session the first post-render quiet is
        # usually the MCP warm — the settle must not mark ready before
        # READY_MIN_S since spawn, however early the output goes quiet.
        monkeypatch.setattr(isess, "READY_SETTLE_S", 0.05)
        monkeypatch.setattr(isess, "READY_MIN_S", 0.6)
        s = await _register("sid-readymin")
        try:
            assert s._ready is False
            s._fanout_output(b"\x1b[2J welcome frame")  # render started → settle arms
            await asyncio.sleep(0.25)   # long past the 0.05 settle, before the floor
            assert s._ready is False
            await asyncio.sleep(0.6)    # past READY_MIN_S since spawn
            assert s._ready is True
        finally:
            await s.close()

    async def test_drainer_forwards_perm_queue(self):
        s = await _register("sid-drain")
        try:
            got = asyncio.get_running_loop().create_future()
            s.on_perm_event = lambda item: (
                None if got.done() else got.set_result(item)
            )
            get_permission_queue("sid-drain").put_nowait(
                {"event_type": "file", "filename": "x.txt"}
            )
            item = await asyncio.wait_for(got, 2)
            assert item["event_type"] == "file"
        finally:
            await s.close()

    async def test_pty_exit_auto_closes_session(self):
        # A process that exits immediately → on_exit → session auto-closes.
        await _register("sid-exit", argv=("python3", "-c", "pass"))
        for _ in range(50):
            if isess.get("sid-exit") is None:
                break
            await asyncio.sleep(0.1)
        assert isess.get("sid-exit") is None
        assert "sid-exit" not in concurrency._sessions

    async def test_idle_reaper_respects_viewer(self):
        viewed = await _register("sid-viewed")
        idle = await _register("sid-idle")
        try:
            viewed.add_output_listener(lambda b: None)  # attach a viewer
            # Force both far past the timeout.
            past = time.monotonic() - 10_000
            viewed.last_activity = past
            idle.last_activity = past

            reaped = await isess.reap_idle(timeout_s=1)
            assert reaped == 1
            assert isess.get("sid-idle") is None      # no viewer → reaped
            assert isess.get("sid-viewed") is viewed   # viewer → spared
        finally:
            await viewed.close()

    async def test_close_is_idempotent(self):
        s = await _register("sid-idem")
        await s.close()
        await s.close()  # must not raise / double-release
        assert isess.get("sid-idem") is None

    async def test_close_releases_subscription_binding(self, monkeypatch):
        # Interactive sessions never pass through the engine layers'
        # close_session(), so close() itself must release the subscription
        # seat acquired at config build — otherwise active_sessions drifts
        # up until a proxy restart.
        from services.engines import subscription_pool
        released = []
        monkeypatch.setattr(
            subscription_pool.subscription_store, "decrement_active_sessions",
            released.append,
        )
        s = await _register("sid-sub")
        subscription_pool.bind_session("sid-sub", "sub-abc")
        await s.close()
        assert released == ["sub-abc"]
        assert subscription_pool.get_session_subscription("sid-sub") is None

    # -- interactive TASK completion gate ------------------------------------
    # _maybe_fire_turn_complete is the (B) gate: fire on_turn_complete ONCE when a
    # turn-end signal lands with min-turn-time elapsed AND the bg SubagentRegistry
    # empty. Built directly (no PTY needed — the method touches neither).

    async def test_turn_complete_fires_once_when_bg_empty(self):
        s = isess.InteractiveSession(session_id="tc-1", chat_id="c", agent_name="agent")
        fired = []
        s.on_turn_complete = fired.append
        s.created_at = time.monotonic() - (isess.MIN_TURN_S + 1)  # past min-time
        s._maybe_fire_turn_complete("final answer")
        assert fired == ["final answer"]
        # Idempotent: the tailer fires on debounce + close + sweep — fire once.
        s._maybe_fire_turn_complete("again")
        assert fired == ["final answer"]

    async def test_turn_complete_holds_within_min_time(self):
        s = isess.InteractiveSession(session_id="tc-2", chat_id="c", agent_name="agent")
        fired = []
        s.on_turn_complete = fired.append
        # Freshly created → within MIN_TURN_S → no false-trigger on warm quiet.
        s._maybe_fire_turn_complete("too soon")
        assert fired == []

    async def test_turn_complete_holds_while_bg_subagent_pending(self):
        from core.session.session_state import get_subagent_registry
        s = isess.InteractiveSession(session_id="tc-3", chat_id="c", agent_name="agent")
        fired = []
        s.on_turn_complete = fired.append
        s.created_at = time.monotonic() - (isess.MIN_TURN_S + 1)
        reg = get_subagent_registry("tc-3")
        reg.register_spawn("task-1", "task-1")  # a bg subagent is still running
        s._maybe_fire_turn_complete("end of main turn")
        assert fired == []  # held — a follow-up turn is coming
        reg.mark_done("task-1")
        s._maybe_fire_turn_complete("end of FINAL turn")
        assert fired == ["end of FINAL turn"]

    async def test_turn_complete_no_callback_is_noop(self):
        # A chat never sets on_turn_complete → the whole path is a harmless no-op.
        s = isess.InteractiveSession(session_id="tc-4", chat_id="c", agent_name="agent")
        s.created_at = time.monotonic() - 100
        s._maybe_fire_turn_complete("whatever")  # must not raise

    # -- self-resume detection -------------------------------------------------
    # Output while the turn is CLOSED arms one short-fuse resume-check tail
    # (promptless self-resume / in-TUI question answer); an open turn or a
    # remote target never arms it.

    async def test_resume_tail_arms_only_while_turn_closed(self):
        s = await _register("sid-resume")
        try:
            s._mark_ready()
            assert not s._turn_open
            s._fanout_output(b"\x1b[2K spinner")
            assert s._resume_tail_handle is not None
            armed = s._resume_tail_handle
            # Further output while one is pending must not re-arm (single fuse).
            s._fanout_output(b"more output")
            assert s._resume_tail_handle is armed
            armed.cancel()
            s._resume_tail_handle = None
            # Open turn → the normal debounce owns freshness, no resume probe.
            s._set_turn_open(True)
            s._fanout_output(b"streaming output")
            assert s._resume_tail_handle is None
        finally:
            await s.close()

    async def test_resume_tail_never_arms_for_remote(self):
        s = await _register("sid-resume-remote")
        try:
            s._mark_ready()
            s.target = "machine-1"  # remote: forwarded lines carry turn state
            assert not s._turn_open
            s._fanout_output(b"remote output")
            assert s._resume_tail_handle is None
        finally:
            await s.close()

    async def test_resume_tail_runner_skips_open_turn(self):
        s = await _register("sid-resume-run")
        try:
            s._mark_ready()
            tailed = []
            s._tail_and_maybe_complete = lambda: tailed.append(1) or _noop()
            s._turn_open = True
            s._run_resume_tail()
            assert tailed == []
            s._turn_open = False
            s._run_resume_tail()
            await asyncio.sleep(0)
            assert tailed == [1]
        finally:
            await s.close()

    # -- question-parked injection gate ---------------------------------------

    async def test_question_parked_blocks_prompt_injection(self):
        s = await _register("sid-qpark")
        try:
            s._mark_ready()
            s.created_at = time.monotonic() - 60
            s.last_activity = time.monotonic() - 60
            assert s._prompt_gates_blocked() is None
            # Question fold: turn closes but the TUI shows the dialog — a
            # server-prompt paste would land in the question picker.
            s._apply_turn_signal("end_turn", question_pending=True)
            assert s._prompt_gates_blocked() == "question_parked"
            # Answer + continuation reopens: unparked (turn_open gate again).
            s._apply_turn_signal("tool_use")
            assert s._prompt_gates_blocked() == "turn_open"
            # Real turn end: unparked and clear.
            s._apply_turn_signal("end_turn")
            s.last_activity = time.monotonic() - 60
            assert s._prompt_gates_blocked() is None
        finally:
            await s.close()

    # -- composer hold while question-parked -----------------------------------

    async def test_composer_send_held_while_question_parked(self):
        s = await _register("sid-qhold")
        try:
            s._mark_ready()
            s.created_at = time.monotonic() - 60
            s._apply_turn_signal("end_turn", question_pending=True)
            paste = b"\x1b[200~hello\nworld\x1b[201~\r"
            # Composer send while parked → held in the prompt queue (typed
            # through, it would land in the picker's notes + answer it).
            s.deliver_dashboard_input(paste, composer=True)
            assert [i["text"] for i in s._prompt_queue] == ["hello\nworld"]
            # Raw terminal keystrokes (answering the picker) pass through.
            s.deliver_dashboard_input(b"\r")
            assert len(s._prompt_queue) == 1
            # Unparked → composer sends type through again, nothing queued.
            s._prompt_queue.clear()
            s._apply_turn_signal("end_turn")
            s.deliver_dashboard_input(paste, composer=True)
            assert len(s._prompt_queue) == 0
        finally:
            await s.close()

    # -- baked TUI theme -----------------------------------------------------
    # Viewers render their xterm with the session's seeded theme (a dark-seeded
    # TUI in a light xterm paints white-on-white); the field normalizes to the
    # two values Claude's seed accepts and defaults dark.

    async def test_tui_theme_normalizes(self):
        s = isess.InteractiveSession(session_id="th-1", chat_id="c", agent_name="a",
                                     tui_theme="Light-daltonized")
        assert s.tui_theme == "light"
        s2 = isess.InteractiveSession(session_id="th-2", chat_id="c", agent_name="a")
        assert s2.tui_theme == "dark"
        s3 = isess.InteractiveSession(session_id="th-3", chat_id="c", agent_name="a",
                                      tui_theme="")
        assert s3.tui_theme == "dark"


@pytest.mark.asyncio
class TestPostBatchEffects:
    """_post_batch_effects: the per-batch chat_rows nudge + the compaction
    turn-end. Built directly (no PTY — the method touches neither)."""

    async def test_persisted_rows_broadcast_chat_rows(self, monkeypatch):
        from services.notifications import notification_manager as nm
        calls = []
        monkeypatch.setattr(nm, "broadcast_chat_rows",
                            lambda sub, cid, agent="": calls.append((sub, cid, agent)))
        s = isess.InteractiveSession(session_id="pb-1", chat_id="c-pb",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"  # skip the DB lookup
        s._post_batch_effects({"persisted": 3})
        assert calls == [("u1", "c-pb", "agent")]
        # No rows → no nudge.
        s._post_batch_effects({"persisted": 0})
        assert len(calls) == 1

    async def test_meeting_chats_never_nudge(self, monkeypatch):
        from services.notifications import notification_manager as nm
        calls = []
        monkeypatch.setattr(nm, "broadcast_chat_rows",
                            lambda *a, **k: calls.append(a))
        s = isess.InteractiveSession(session_id="pb-2", chat_id="meeting-x",
                                     agent_name="agent", user_sub="u1")
        s._post_batch_effects({"persisted": 2})
        assert calls == []

    async def test_compaction_while_idle_runs_turn_end(self, monkeypatch):
        s = isess.InteractiveSession(session_id="pb-3", chat_id="c-pb3",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        ends, pings = [], []
        monkeypatch.setattr(s, "_turn_end_effects", lambda: ends.append(1))
        monkeypatch.setattr(
            s, "_fire_turn_notification",
            lambda question=False, compacted=False: pings.append(compacted))
        s._post_batch_effects({"persisted": 1, "compacted": True})
        assert ends == [1]
        assert pings == [True]

    async def test_compaction_mid_turn_is_silent(self, monkeypatch):
        # Auto-compact inside an open turn: the turn continues — no close,
        # no ping (the real end_turn fires the normal completion later).
        s = isess.InteractiveSession(session_id="pb-4", chat_id="c-pb4",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        s._turn_open = True
        ends, pings = [], []
        monkeypatch.setattr(s, "_turn_end_effects", lambda: ends.append(1))
        monkeypatch.setattr(
            s, "_fire_turn_notification",
            lambda question=False, compacted=False: pings.append(compacted))
        s._post_batch_effects({"persisted": 1, "compacted": True})
        assert ends == []
        assert pings == []

    async def test_manual_compaction_closes_stale_open_turn(self, monkeypatch):
        # Manual /compact completes only with the CLI idle at the prompt — an
        # open turn when the manual boundary lands is stale state (nothing
        # else ever closes it: the command record + reseed are all
        # signal-filtered). The boundary closes it + fires the compacted ping.
        s = isess.InteractiveSession(session_id="pb-5", chat_id="c-pb5",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        s._turn_open = True
        ends, pings = [], []
        monkeypatch.setattr(s, "_turn_end_effects", lambda: ends.append(1))
        monkeypatch.setattr(
            s, "_fire_turn_notification",
            lambda question=False, compacted=False: pings.append(compacted))
        s._post_batch_effects({"persisted": 1, "compacted": True,
                               "compact_trigger": "manual"})
        assert s._turn_open is False
        assert ends == [1]          # via the _set_turn_open(False) transition
        assert pings == [True]

    async def test_manual_compaction_yields_to_newer_prompt(self, monkeypatch):
        # A prompt landing right after the boundary in the SAME batch wins:
        # the batch's open signal means a real turn is running — never close.
        s = isess.InteractiveSession(session_id="pb-6", chat_id="c-pb6",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        s._turn_open = True
        ends, pings = [], []
        monkeypatch.setattr(s, "_turn_end_effects", lambda: ends.append(1))
        monkeypatch.setattr(
            s, "_fire_turn_notification",
            lambda question=False, compacted=False: pings.append(compacted))
        s._post_batch_effects({"persisted": 1, "compacted": True,
                               "compact_trigger": "manual",
                               "last_signal": "user"})
        assert s._turn_open is True
        assert ends == []
        assert pings == []

    async def test_triggerless_boundary_never_closes_open_turn(self, monkeypatch):
        # Old-CLI boundaries without compactMetadata (no trigger) stay
        # conservative: an open turn is left alone (only "manual" closes).
        s = isess.InteractiveSession(session_id="pb-7", chat_id="c-pb7",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        s._turn_open = True
        ends, pings = [], []
        monkeypatch.setattr(s, "_turn_end_effects", lambda: ends.append(1))
        monkeypatch.setattr(
            s, "_fire_turn_notification",
            lambda question=False, compacted=False: pings.append(compacted))
        s._post_batch_effects({"persisted": 1, "compacted": True,
                               "compact_trigger": ""})
        assert s._turn_open is True
        assert ends == []
        assert pings == []

    async def test_codex_compact_batch_pings_once_compacted_worded(
            self, monkeypatch):
        # Codex manual /compact: task_complete rides the same batch as the
        # compacted item — _post_batch_effects defers, and the turn-complete
        # ping fires ONCE with compacted wording (was: "compacted" AND
        # "finished", a double ping).
        import time as _time
        s = isess.InteractiveSession(session_id="pb-8", chat_id="c-pb8",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        s.created_at = _time.monotonic() - 9999  # clear the min-turn gate
        pings = []
        monkeypatch.setattr(s, "_turn_end_effects", lambda: None)
        monkeypatch.setattr(
            s, "_fire_turn_notification",
            lambda question=False, compacted=False: pings.append(compacted))
        batch = {"persisted": 1, "compacted": True, "turn_complete": True,
                 "last_signal": "end_turn", "last_message": ""}
        s._apply_turn_signal(batch.get("last_signal"))
        s._post_batch_effects(batch)
        assert pings == []  # deferred to the turn-complete path
        s._maybe_fire_turn_complete("", batch["persisted"],
                                    compacted=batch["compacted"])
        assert pings == [True]  # one ping, compacted wording


class TestDelegateWakeReplayOnRegister:
    """Durable delegate-wake replay: a wake stored while the chat was dead is
    claimed at the next interactive registration and queued as a server
    prompt, so a manual re-warm delivers the missed result on its own."""

    @pytest.mark.asyncio
    async def test_pending_wake_queued_on_register(self, temp_db):
        from storage import database as task_store
        task_store.create_chat("chat-wake", "user-1", "agent")
        task_store.append_pending_delegate_wake("chat-wake", "REPLAYED WAKE")

        s = await _register("sid-wake", chat_id="chat-wake")
        try:
            # Deterministically still queued: the fresh session's too_young /
            # readiness gates hold the drain at this point.
            queued = [item["text"] for item in s._prompt_queue]
            assert queued == ["REPLAYED WAKE"]
            # Claimed atomically — the store is empty now.
            assert task_store.claim_pending_delegate_wake("chat-wake") == []
        finally:
            await s.close(reason="test-end")

    @pytest.mark.asyncio
    async def test_no_wake_no_queue(self, temp_db):
        from storage import database as task_store
        task_store.create_chat("chat-nowake", "user-1", "agent")
        s = await _register("sid-nowake", chat_id="chat-nowake")
        try:
            assert len(s._prompt_queue) == 0
        finally:
            await s.close(reason="test-end")
