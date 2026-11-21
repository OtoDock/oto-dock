"""Tests for core.session.interactive_session — registry, lease, idle reaper, drainer.

DB-light: each session is registered on a stand-in PTY that prints back what
it reads (``tests/_terminal.py``) and exercises the registry/lease/slot/idle/
drainer wiring. Uses the real concurrency slot pool (init() falls back to
defaults without a DB).
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

import config  # (also ensures conftest path/env setup ran)
from core import concurrency
from core.session import (
    interactive_input,
    interactive_output,
    interactive_session as isess,
)
from core.session.session_state import get_permission_queue
from tests._terminal import register


def _ensure_concurrency():
    # init() is idempotent enough for tests; it (re)creates the lock + limits.
    concurrency.init()


async def _register(session_id, *, chat_id="chat-1", **program):
    return await register(
        session_id=session_id, chat_id=chat_id, agent_name="agent", **program,
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
            s._mark_ready()  # the stand-in prints no frame of its own → bypass the readiness gate
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
        # flushed once ready (forced here via _mark_ready — the stand-in prints no
        # frame of its own that would trigger the settle). Prevents the first prompt from
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
        # user signal (the stand-in journals nothing), the watcher must re-emit the
        # flushed body after _COLD_RETRY_S.
        monkeypatch.setattr(interactive_input, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(interactive_output, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(interactive_input, "_COLD_RETRY_S", 0.3)
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
        monkeypatch.setattr(interactive_input, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(interactive_output, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(interactive_input, "_COLD_RETRY_S", 0.5)
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

    async def test_cold_flush_hook_activity_stops_retry(self, monkeypatch):
        # Second confirmation source: a hook of this session fired after the
        # cold Enter (PreToolUse for the turn's first tool) — the turn runs
        # even though no transcript line arrived (late forward, or a CLI that
        # journals nothing). No re-emit, no breadcrumb (T1 2026-09-10: the
        # replay landed in the open AskUserQuestion dialog).
        monkeypatch.setattr(interactive_input, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(interactive_output, "_SUBMIT_SETTLE_S", 0.1)
        monkeypatch.setattr(interactive_input, "_COLD_RETRY_S", 0.5)
        captured = []
        from core.session import transcript_tool_events as tte
        monkeypatch.setattr(
            tte, "persist_event",
            lambda store, chat_id, block: captured.append((chat_id, block)),
        )
        from core.session.session_state import record_hook_activity
        s = await _register("sid-hook-confirmed")
        try:
            writes: list[bytes] = []
            orig = s.pty.write
            s.pty.write = lambda b: (writes.append(bytes(b)), orig(b))[1]
            s.write_input(b"please use your question tool and ask me one thing\r")
            s._mark_ready()
            await asyncio.sleep(0.3)            # Enter fired, watcher armed
            record_hook_activity("sid-hook-confirmed")  # the turn's first tool
            await asyncio.sleep(0.7)             # past the watcher window
            assert sum(1 for w in writes if b"question tool" in w) == 1
            assert s._cold_flush_attempts == 0
            assert s._cold_flush_pending == b""
            assert captured == []                # no breadcrumb either
        finally:
            await s.close()

    async def test_cold_flush_retries_exhaust_with_warning(self, monkeypatch, caplog):
        # Retries are bounded: after _COLD_RETRY_MAX unconfirmed replays the
        # watcher gives up with a WARNING — never loops, never raises.
        monkeypatch.setattr(interactive_input, "_SUBMIT_SETTLE_S", 0.05)
        monkeypatch.setattr(interactive_output, "_SUBMIT_SETTLE_S", 0.05)
        monkeypatch.setattr(interactive_input, "_COLD_RETRY_S", 0.15)
        monkeypatch.setattr(interactive_input, "_COLD_RETRY_MAX", 1)
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

    async def test_abandoned_cold_flush_persists_breadcrumb(self, monkeypatch):
        # Retry exhaustion is a DEFINITIVE loss (no turn ever opened with the
        # flushed text) — the bytes must land in the chat as an
        # undelivered_input system event before being dropped (field loss
        # 2026-08-18: a prompt typed into the CLI's resume-from-summary
        # picker vanished with no DB trace).
        monkeypatch.setattr(interactive_input, "_SUBMIT_SETTLE_S", 0.05)
        monkeypatch.setattr(interactive_output, "_SUBMIT_SETTLE_S", 0.05)
        monkeypatch.setattr(interactive_input, "_COLD_RETRY_S", 0.15)
        monkeypatch.setattr(interactive_input, "_COLD_RETRY_MAX", 1)
        captured = []
        from core.session import transcript_tool_events as tte
        monkeypatch.setattr(
            tte, "persist_event",
            lambda store, chat_id, block: captured.append((chat_id, block)),
        )
        prompt = b"please redeploy the internal install and verify the card\r"
        s = await _register("sid-breadcrumb")
        try:
            s.write_input(prompt)
            s._mark_ready()
            await asyncio.sleep(1.5)
            assert captured, "breadcrumb never persisted"
            chat_id, block = captured[0]
            assert chat_id == "chat-1"
            assert block["subtype"] == "undelivered_input"
            assert "redeploy the internal install" in block["message"]
        finally:
            await s.close()

    async def test_user_input_cancel_persists_breadcrumb(self, monkeypatch):
        # A user keypress during the cold-submit settle window cancels the
        # pending Enter — the unconfirmed flush text gets a breadcrumb (in
        # the dialog-swallowed case it is the ONLY surviving copy).
        monkeypatch.setattr(interactive_input, "_SUBMIT_SETTLE_S", 5.0)  # keep Enter pending
        monkeypatch.setattr(interactive_output, "_SUBMIT_SETTLE_S", 5.0)
        captured = []
        from core.session import transcript_tool_events as tte
        monkeypatch.setattr(
            tte, "persist_event",
            lambda store, chat_id, block: captured.append(block),
        )
        prompt = b"connect the analytics account and run the first GA4 report\r"
        s = await _register("sid-cancel-crumb")
        try:
            s.write_input(prompt)
            s._mark_ready()
            await asyncio.sleep(0.1)
            s.write_input(b"x")  # user drives → cancel branch
            await asyncio.sleep(0.2)
            assert captured and captured[0]["subtype"] == "undelivered_input"
            assert "GA4 report" in captured[0]["message"]
            assert s._cold_flush_pending == b""
        finally:
            await s.close()

    async def test_breadcrumb_skips_short_residue(self, monkeypatch):
        # Key residue (arrows, a stray Enter, short edits) must never become
        # a chat row — the breadcrumb is length-gated.
        monkeypatch.setattr(interactive_input, "_SUBMIT_SETTLE_S", 5.0)
        monkeypatch.setattr(interactive_output, "_SUBMIT_SETTLE_S", 5.0)
        captured = []
        from core.session import transcript_tool_events as tte
        monkeypatch.setattr(
            tte, "persist_event",
            lambda store, chat_id, block: captured.append(block),
        )
        s = await _register("sid-short-crumb")
        try:
            s.write_input(b"ok\r")
            s._mark_ready()
            await asyncio.sleep(0.1)
            s.write_input(b"x")
            await asyncio.sleep(0.2)
            assert captured == []
        finally:
            await s.close()

    def test_breadcrumb_text_strips_terminal_framing(self):
        raw = (b"\x1b[200~line one\nline two\x1b[201~\r"
               b"\x1b[1;2H\x1b]0;title\x07")
        assert isess._breadcrumb_text(raw) == "line one\nline two"

    async def test_ready_settle_floored_by_ready_min(self, monkeypatch):
        # READY_MIN_S: on a fresh session the first post-render quiet is
        # usually the MCP warm — the settle must not mark ready before
        # READY_MIN_S since spawn, however early the output goes quiet.
        monkeypatch.setattr(interactive_output, "READY_SETTLE_S", 0.05)
        monkeypatch.setattr(interactive_output, "READY_MIN_S", 0.6)
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
        # A program that ends by itself → on_exit → session auto-closes.
        await _register("sid-exit", exit_code=0)
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

    async def test_idle_reaper_spares_question_parked_under_cap(self):
        # Parked on an unanswered AskUserQuestion: idle by every byte measure
        # but waiting on a person, so spared like a held prompt: until the
        # session's token ends (24 h after the terminal started, less a
        # minute), at most session_state.PROMPT_WAIT_S. Far past a few idle
        # windows; this harness env carries no token, so the terminal's start
        # plus the token life stands in.
        s = await _register("sid-qpark-spare")
        try:
            s._apply_turn_signal("end_turn", question_pending=True)
            s.last_activity = time.monotonic() - 100 * 100  # 100 idle windows
            assert await isess.reap_idle(timeout_s=100) == 0
            assert isess.get("sid-qpark-spare") is s
        finally:
            await s.close()

    async def test_idle_reaper_reaps_question_parked_over_cap(self):
        # Past the prompt's wait an abandoned question no longer holds the
        # slot (revival-on-answer covers a late reply).
        from core.session import session_state
        s = await _register("sid-qpark-cap")
        s._apply_turn_signal("end_turn", question_pending=True)
        s.last_activity = time.monotonic() - (session_state.PROMPT_WAIT_S + 60)
        assert await isess.reap_idle(timeout_s=100) == 1
        assert isess.get("sid-qpark-cap") is None

    async def test_idle_reaper_spares_a_prompt_waiting_on_a_person(self):
        # Parity with the CLI, Codex, Direct and remote reapers: a prompt the
        # platform holds for this session keeps it; the prompt's own wait
        # bounds the spare.
        from core.session import session_state
        s = await _register("sid-prompt-spare")
        try:
            s.last_activity = time.monotonic() - 10_000
            session_state._session_permission_requests["sid-prompt-spare"] = {"r-a1"}
            session_state._permission_events["r-a1"] = asyncio.Event()
            assert await isess.reap_idle(timeout_s=100) == 0
            assert isess.get("sid-prompt-spare") is s
            session_state._permission_events.pop("r-a1", None)
            assert await isess.reap_idle(timeout_s=100) == 1
            assert isess.get("sid-prompt-spare") is None
        finally:
            session_state._permission_events.pop("r-a1", None)
            session_state._session_permission_requests.pop("sid-prompt-spare", None)
            if isess.get("sid-prompt-spare") is not None:
                await s.close()

    async def test_a_parked_terminal_ends_with_its_session_token(self):
        # The token in the spawn env ends within the margin: an answer now
        # would let the turn go on with every later hook refused, so the park
        # does not spare it, however short its idle time.
        from auth.session_token import create_session_token
        token = create_session_token(
            "sid-qpark-token", "agent",
            issued_at=int(time.time()) - 24 * 3600 + 30)
        s = await register(
            session_id="sid-qpark-token", chat_id="chat-1", agent_name="agent",
            env={"PROXY_API_KEY": token})
        s._apply_turn_signal("end_turn", question_pending=True)
        s.last_activity = time.monotonic() - 150  # > timeout, < 2x timeout
        assert await isess.reap_idle(timeout_s=100) == 1
        assert isess.get("sid-qpark-token") is None

    async def test_idle_reaper_spares_open_turn(self):
        # A byte-quiet stretch of a long unviewed turn can outlast the idle
        # window — an open turn is spared; closing it reaps as before.
        s = await _register("sid-turn-spare")
        try:
            s._turn_open = True
            s.last_activity = time.monotonic() - 100  # > timeout, < turn ceiling
            assert await isess.reap_idle(timeout_s=1) == 0
            assert isess.get("sid-turn-spare") is s
            s._turn_open = False
            assert await isess.reap_idle(timeout_s=1) == 1
            assert isess.get("sid-turn-spare") is None
        finally:
            if isess.get("sid-turn-spare") is not None:
                await s.close()

    async def test_idle_reaper_reaps_open_turn_past_turn_ceiling(self):
        # A turn cannot legitimately outlive the per-turn ceiling
        # (config.get_session_timeout()), so a STUCK _turn_open flag must not
        # make the session immortal.
        import config as _config
        s = await _register("sid-turn-ceiling")
        s._turn_open = True
        s.last_activity = time.monotonic() - (_config.get_session_timeout() + 60)
        assert await isess.reap_idle(timeout_s=1) == 1
        assert isess.get("sid-turn-ceiling") is None

    async def test_idle_reaper_spares_recent_hook_activity(self):
        # PTY output stalled (satellite WS-down frames drop silently) but tool
        # hooks keep firing for the session id → the CLI is working, spare it.
        # Hook activity older than the window no longer spares.
        from core.session import session_state
        s = await _register("sid-hook-spare")
        try:
            s.last_activity = time.monotonic() - 10_000
            session_state.record_hook_activity("sid-hook-spare")
            assert await isess.reap_idle(timeout_s=1) == 0
            assert isess.get("sid-hook-spare") is s
            session_state._session_hook_activity["sid-hook-spare"] = (
                time.monotonic() - 5_000)  # stale — outside the window
            assert await isess.reap_idle(timeout_s=1) == 1
            assert isess.get("sid-hook-spare") is None
        finally:
            session_state._session_hook_activity.pop("sid-hook-spare", None)
            if isess.get("sid-hook-spare") is not None:
                await s.close()

    # -- background work a Claude terminal's Stop reports -----------------------
    async def test_reported_background_work_spares_up_to_the_ceiling(
            self, monkeypatch, caplog):
        # The turn ended with a background command running and nobody
        # watching: the Stop's report spares the terminal,
        # counted from the last hook (the Stop), up to the headless path's
        # ceiling; past it the terminal goes, and the log says why.
        from core.session import session_state
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = await _register("sid-bg-spare")
        try:
            s.last_activity = time.monotonic() - 10_000
            session_state._session_hook_activity["sid-bg-spare"] = time.monotonic() - 500
            isess.note_background_report("sid-bg-spare", ["shell"])
            with caplog.at_level("INFO", logger="otodock"):
                assert await isess.reap_idle(timeout_s=100) == 0
            assert isess.get("sid-bg-spare") is s
            assert "sparing idle session (background-pending" in caplog.text
            session_state._session_hook_activity["sid-bg-spare"] = time.monotonic() - 1_500
            caplog.clear()
            with caplog.at_level("INFO", logger="otodock"):
                assert await isess.reap_idle(timeout_s=100) == 1
            assert isess.get("sid-bg-spare") is None
            assert "its background work outlived the ceiling" in caplog.text
        finally:
            session_state._session_hook_activity.pop("sid-bg-spare", None)
            if isess.get("sid-bg-spare") is not None:
                await s.close()

    async def test_a_stop_listing_no_work_ends_the_background_spare(
            self, monkeypatch, caplog):
        # The job ended: the CLI's own wake turn stops with an empty list,
        # and the terminal is reaped as one with no background work.
        from core.session import session_state
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = await _register("sid-bg-ended")
        try:
            s.last_activity = time.monotonic() - 10_000
            session_state._session_hook_activity["sid-bg-ended"] = time.monotonic() - 500
            isess.note_background_report("sid-bg-ended", ["shell", "subagent"])
            assert isess.reap_spare_reason(s, 100) == "background-pending"
            isess.note_background_report("sid-bg-ended", [])
            with caplog.at_level("INFO", logger="otodock"):
                assert await isess.reap_idle(timeout_s=100) == 1
            assert "outlived the ceiling" not in caplog.text
        finally:
            session_state._session_hook_activity.pop("sid-bg-ended", None)
            if isess.get("sid-bg-ended") is not None:
                await s.close()

    async def test_housekeeping_and_unknown_kinds_spare_nothing(self, monkeypatch):
        # The CLI's own housekeeping ends with no wake and no Stop, and a
        # teammate or a cloud session has no end this terminal waits on: only
        # the agent's own work spares, and a report of housekeeping alone
        # clears an earlier one.
        from core.session import session_state
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = await _register("sid-bg-kinds")
        try:
            session_state._session_hook_activity["sid-bg-kinds"] = time.monotonic() - 500
            isess.note_background_report("sid-bg-kinds", [
                "dream", "auto-mode scan", "memory import", "teammate",
                "cloud session", "post_turn_memory", "a kind from a later CLI"])
            assert isess.reap_spare_reason(s, 100) == ""
            for kind in ("shell", "subagent", "workflow", "monitor", "MCP task"):
                isess.note_background_report("sid-bg-kinds", [kind])
                assert isess.reap_spare_reason(s, 100) == "background-pending", kind
            isess.note_background_report("sid-bg-kinds", ["dream"])
            assert isess.reap_spare_reason(s, 100) == ""
        finally:
            session_state._session_hook_activity.pop("sid-bg-kinds", None)
            await s.close()

    async def test_the_last_hook_moves_the_background_ceiling(self, monkeypatch):
        # A sub-agent's tool calls fire hooks: work that shows itself is
        # counted from its newest sign, not from the turn that started it.
        from core.session import session_state
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = await _register("sid-bg-hook")
        try:
            s.last_activity = time.monotonic() - 10_000
            isess.note_background_report("sid-bg-hook", ["subagent"])
            session_state._session_hook_activity["sid-bg-hook"] = time.monotonic() - 1_500
            assert isess.reap_spare_reason(s, 100) == ""
            session_state._session_hook_activity["sid-bg-hook"] = time.monotonic() - 900
            assert isess.reap_spare_reason(s, 100) == "background-pending"
        finally:
            session_state._session_hook_activity.pop("sid-bg-hook", None)
            await s.close()

    async def test_terminals_with_no_report_are_reaped_as_before(self):
        # A Claude terminal whose Stop listed nothing, and a Codex terminal
        # (whose Stop carries no list, so its script sends none), are reaped
        # as before; a report for a session that is not a terminal is a no-op.
        from core.session import session_state
        plain = await _register("sid-bg-none")
        codex = await register(
            session_id="sid-bg-codex", chat_id="chat-2", agent_name="agent",
            execution_path="codex-cli")
        try:
            for sid in ("sid-bg-none", "sid-bg-codex"):
                session_state._session_hook_activity[sid] = time.monotonic() - 500
            isess.note_background_report("sid-bg-none", [])
            isess.note_background_report("sid-gone", ["shell"])  # no such terminal
            assert isess.get("sid-gone") is None
            plain.last_activity = codex.last_activity = time.monotonic() - 10_000
            assert await isess.reap_idle(timeout_s=100) == 2
        finally:
            for sid in ("sid-bg-none", "sid-bg-codex"):
                session_state._session_hook_activity.pop(sid, None)

    async def test_each_spare_reason_is_named(self, monkeypatch):
        from core.session import session_state
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = await _register("sid-reasons")
        try:
            s.last_activity = time.monotonic() - 1_000
            assert isess.reap_spare_reason(s, 100) == ""
            isess.note_background_report("sid-reasons", ["shell"])
            session_state._session_hook_activity["sid-reasons"] = time.monotonic() - 500
            assert isess.reap_spare_reason(s, 100) == "background-pending"
            session_state.record_hook_activity("sid-reasons")
            assert isess.reap_spare_reason(s, 100) == "hook-activity"
            s._turn_open = True
            assert isess.reap_spare_reason(s, 100) == "turn-open"
            monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: True)
            assert isess.reap_spare_reason(s, 100) == "prompt-pending"
            s._apply_turn_signal("end_turn", question_pending=True)
            assert isess.reap_spare_reason(s, 100) == "question-parked"
        finally:
            session_state._session_hook_activity.pop("sid-reasons", None)
            await s.close()

    async def test_a_spare_is_logged_once_per_stretch_over_the_timeout(
            self, monkeypatch, caplog):
        from core.session import session_state
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = await _register("sid-bg-log")
        try:
            session_state._session_hook_activity["sid-bg-log"] = time.monotonic() - 500
            isess.note_background_report("sid-bg-log", ["shell"])
            with caplog.at_level("INFO", logger="otodock"):
                s.last_activity = time.monotonic() - 10_000
                await isess.reap_idle(timeout_s=100)
                await isess.reap_idle(timeout_s=100)
                s.last_activity = time.monotonic()  # a turn, under the timeout
                await isess.reap_idle(timeout_s=100)
                s.last_activity = time.monotonic() - 10_000
                await isess.reap_idle(timeout_s=100)
            assert caplog.text.count("sparing idle session (background-pending") == 2
        finally:
            session_state._session_hook_activity.pop("sid-bg-log", None)
            await s.close()

    async def test_a_viewer_coming_and_going_keeps_the_report_and_its_ceiling(
            self, monkeypatch, caplog):
        # While viewed the reaper skips it, and the viewer moves neither the
        # report nor the hook clock: the ceiling stays where the work left it,
        # and the spare line logs again for the new stretch over the timeout.
        from core.session import session_state
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = await _register("sid-bg-viewer")
        try:
            stamped = time.monotonic() - 500
            session_state._session_hook_activity["sid-bg-viewer"] = stamped
            isess.note_background_report("sid-bg-viewer", ["shell"])
            s.last_activity = time.monotonic() - 10_000
            with caplog.at_level("INFO", logger="otodock"):
                assert await isess.reap_idle(timeout_s=100) == 0
                viewer = lambda b: None
                s.add_output_listener(viewer)
                assert await isess.reap_idle(timeout_s=100) == 0
                s.remove_output_listener(viewer)
                s.last_activity = time.monotonic() - 10_000
                assert await isess.reap_idle(timeout_s=100) == 0
            assert s._background_reported
            assert session_state.get_hook_activity("sid-bg-viewer") == stamped
            assert caplog.text.count("sparing idle session (background-pending") == 2
            session_state._session_hook_activity["sid-bg-viewer"] = time.monotonic() - 1_500
            assert await isess.reap_idle(timeout_s=100) == 1
        finally:
            session_state._session_hook_activity.pop("sid-bg-viewer", None)
            if isess.get("sid-bg-viewer") is not None:
                await s.close()

    # -- what counts as activity ------------------------------------------------

    async def test_output_outside_a_turn_is_not_activity(self):
        # A TUI redraws on its own between turns (a config reload, a status
        # line): that is not the agent working, and it must not reset the age
        # the idle reaper and the evictor read. Inside an open turn output is
        # the agent working.
        s = await _register("sid-noise")
        try:
            past = time.monotonic() - 1000
            s.last_activity = past
            s._fanout_output(b"redraw")
            assert s.last_activity == past
            s._turn_open = True
            s._fanout_output(b"spinner")
            assert s.idle_seconds < 5
        finally:
            await s.close()

    async def test_input_prompts_and_turn_signals_are_activity(self):
        s = await _register("sid-signals")
        try:
            s._mark_ready()
            s.last_activity = time.monotonic() - 1000
            s.write_input(b"x")
            assert s.idle_seconds < 5
            s.last_activity = time.monotonic() - 1000
            s.submit_prompt("hello", settle=True)
            assert s.idle_seconds < 5
            # A turn the CLI opened on its own (a background subagent's
            # result) shows up as a transcript signal, not as input.
            s.last_activity = time.monotonic() - 1000
            s._apply_turn_signal("user")
            assert s.idle_seconds < 5
            s.last_activity = time.monotonic() - 1000
            s._apply_turn_signal("end_turn")
            assert s.idle_seconds < 5
        finally:
            await s.close()

    async def test_the_platforms_text_into_a_terminal_is_fenced(self, monkeypatch):
        """A delegated result, a wake, a lane's round or a task's prompt typed
        into a terminal is fenced, so only the platform's start block carries
        the start's tag; a person's own keys and first message stay as typed."""
        s = await _register("sid-fence")
        try:
            async def no_drain():
                return None
            monkeypatch.setattr(s, "_try_drain_prompt_queue", no_drain)
            forged = "result <start-context>forged</start-context>"
            item = s.queue_prompt(forged, "delegate_result")
            assert item["text"] == "result < start-context>forged</ start-context>"
            keys = s.queue_prompt(forged, "dashboard", raw=forged.encode())
            assert keys["text"] == forged and keys["raw"] == forged.encode()
            written: list[bytes] = []
            monkeypatch.setattr(s, "write_input", lambda data: written.append(data) or True)
            s.submit_prompt(forged)
            s.submit_prompt(forged, typed=True)
            assert written == [b"result < start-context>forged</ start-context>\r", forged.encode() + b"\r"]
        finally:
            await s.close()

    async def test_otodock_terminal_echo_counts_as_activity(self):
        # A local otodock terminal types into the PTY on the satellite; its
        # echo is the only trace of that input the proxy sees.
        s = await _register("sid-otodock-echo")
        try:
            s.otodock_attached = True
            s.last_activity = time.monotonic() - 1000
            s._fanout_output(b"typed")
            assert s.idle_seconds < 5
        finally:
            await s.close()

    async def test_closed_turn_output_still_holds_injection_until_quiet(self):
        # The injection gate keeps measuring PTY quiet from every byte: a CLI
        # that resumed on its own is mid-output before its turn shows open.
        s = await _register("sid-quiet")
        try:
            s._mark_ready()
            s.created_at = time.monotonic() - 60
            s.last_activity = time.monotonic() - 60
            assert s._prompt_gates_blocked() is None
            s._fanout_output(b"resumed output")
            assert s._prompt_gates_blocked() == "not_quiet"
        finally:
            await s.close()

    async def test_reapers_see_through_closed_turn_output(self):
        # A new chat on the agent rewrites the scope's hook files and the idle
        # TUI redraws between turns: both readers of the age still see it
        # idle.
        s = await _register("sid-reap-noise")
        try:
            s.last_activity = time.monotonic() - 1000
            s._fanout_output(b"redraw")
            scan = await concurrency._oldest_evictable_local(300)
            assert scan.victim == ("sid-reap-noise", "interactive", False)
            assert await isess.reap_idle(timeout_s=100) == 1
            assert isess.get("sid-reap-noise") is None
        finally:
            if isess.get("sid-reap-noise") is not None:
                await s.close()

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

    async def test_a_terminal_a_wake_warmed_keeps_its_finished_ping(self, monkeypatch):
        # The re-warm leaves its callback on the chat's terminal, for this turn
        # and every later one. Only a task's terminal stays silent.
        from services.notifications import notification_manager as nm
        pings = []
        monkeypatch.setattr(nm, "broadcast_chat_status", lambda *a, **kw: None)
        monkeypatch.setattr(nm, "agent_label", lambda a: a)

        async def _ping(user_sub, **kw):
            pings.append(kw.get("title", ""))
        monkeypatch.setattr(nm, "fire_ephemeral", _ping)
        s = isess.InteractiveSession(session_id="woken-1", chat_id="c", agent_name="agent", user_sub="u1")
        s.on_turn_complete = lambda message: None
        s._fire_turn_notification()
        await asyncio.sleep(0.05)
        assert pings == []
        s.chat_terminal_rewarmed = True
        for _ in range(2):
            s._fire_turn_notification()
            await asyncio.sleep(0.05)
        assert len(pings) == 2

    # -- hook-driven park on a native dialog ----------------------------------

    async def test_park_on_native_dialog_closes_turn_and_pings_once(self, monkeypatch):
        # The permission hook saw ExitPlanMode / AskUserQuestion on a
        # human-driven session: the turn parks NOW (ready broadcast, hold,
        # one "needs your input" ping). The transcript fold that may follow
        # for the same dialog pings nothing; the answer's reopen resets.
        from services.notifications import notification_manager as nm
        statuses, pings = [], []
        monkeypatch.setattr(nm, "broadcast_chat_status",
                            lambda owner, cid, status, agent="": statuses.append(status))
        monkeypatch.setattr(nm, "agent_label", lambda a: a)

        async def _ping(user_sub, **kw):
            pings.append(kw.get("title", ""))
        monkeypatch.setattr(nm, "fire_ephemeral", _ping)
        s = isess.InteractiveSession(session_id="park-1", chat_id="c", agent_name="agent",
                                     user_sub="u1")
        s.created_at = time.monotonic() - (isess.MIN_TURN_S + 1)
        s._title_armed = False
        s._turn_open = True
        s.park_on_native_dialog("ExitPlanMode")
        await asyncio.sleep(0.05)
        assert s.turn_open is False
        assert s.question_parked is True
        assert "ready" in statuses
        assert len(pings) == 1 and "needs your input" in pings[0]
        # The CLI journals the tool_use line later → the fold runs for the
        # same dialog: no second ping.
        s._apply_turn_signal("end_turn", question_pending=True)
        s._maybe_fire_turn_complete("", persisted=1, question=True)
        await asyncio.sleep(0.05)
        assert len(pings) == 1
        # The answer's continuation reopens: unparked, and a LATER dialog
        # (a real fold, no hook park) pings again.
        s._apply_turn_signal("tool_use")
        assert s.question_parked is False and s._native_park_pinged is False
        s._apply_turn_signal("end_turn", question_pending=True)
        s._maybe_fire_turn_complete("", persisted=1, question=True)
        await asyncio.sleep(0.05)
        assert len(pings) == 2

    async def test_a_batch_journaled_before_the_park_does_not_reopen_it(self, monkeypatch):
        # A tail batch read before the hook parked the turn (an earlier
        # call's tool_use line, journaled within the same poll) and folded
        # after it must not reopen the parked turn: the composer hold would
        # drop and a send would paste into the dialog. A line from after the
        # park (the answer) reopens as always; a batch or a park with no
        # time folds as before.
        from services.notifications import notification_manager as nm
        monkeypatch.setattr(nm, "broadcast_chat_status", lambda *a, **k: None)
        monkeypatch.setattr(nm, "agent_label", lambda a: a)

        async def _ping(user_sub, **kw):
            return None
        monkeypatch.setattr(nm, "fire_ephemeral", _ping)
        s = isess.InteractiveSession(session_id="park-stale", chat_id="c", agent_name="agent",
                                     user_sub="u1")
        s.created_at = time.monotonic() - (isess.MIN_TURN_S + 1)
        s._title_armed = False
        s._turn_open = True
        s.park_on_native_dialog("AskUserQuestion", sent_at=1000.0)
        s._apply_turn_signal("tool_use", signal_at=999.2)
        assert s.question_parked is True and s.turn_open is False
        s._apply_turn_signal("user", signal_at=999.9)
        assert s.question_parked is True and s.turn_open is False
        s._apply_turn_signal("tool_use", signal_at=1003.5)
        assert s.question_parked is False and s.turn_open is True
        # No time on either side: the fold of before.
        s.park_on_native_dialog("AskUserQuestion")
        s._apply_turn_signal("tool_use", signal_at=1.0)
        assert s.question_parked is False
        s.park_on_native_dialog("AskUserQuestion", sent_at=2000.0)
        s._apply_turn_signal("tool_use")
        assert s.question_parked is False

    async def test_the_receiver_hands_the_gates_stamp_to_the_park(self, monkeypatch):
        from api.hooks import permission as hp
        got = []

        class _Sess:
            def park_on_native_dialog(self, tool, *, sent_at=None):
                got.append((tool, sent_at))
        monkeypatch.setattr(isess, "get", lambda sid: _Sess())
        assert hp.HookPermissionRequest(session_id="s", tool_name="t", hook_sent_at=5.5).hook_sent_at == 5.5
        token = hp._hook_sent_at.set(77.0)
        try:
            hp._park_interactive_on_dialog("sid", "AskUserQuestion")
        finally:
            hp._hook_sent_at.reset(token)
        hp._park_interactive_on_dialog("sid", "ExitPlanMode")
        assert got == [("AskUserQuestion", 77.0), ("ExitPlanMode", None)]

    async def test_every_fold_site_passes_the_signal_time(self, monkeypatch):
        s = isess.InteractiveSession(session_id="park-sites", chat_id="c", agent_name="agent",
                                     user_sub="u1")
        seen = []
        monkeypatch.setattr(s, "_apply_turn_signal",
                            lambda sig, question_pending=False, signal_at=None: seen.append(signal_at))
        monkeypatch.setattr(s, "_post_batch_effects", lambda r: None)
        s._fold_tail_result({"last_signal": "tool_use", "last_signal_at": 12.5})
        assert seen == [12.5]

    async def test_park_writes_no_chat_row(self, monkeypatch):
        # The chat rows stay the tailers' job (a hook-written card was tried
        # and dropped: it could not be answered from the history and the
        # late-arriving reply text rendered below it). The park only flips
        # the live state and pings.
        from core.session import transcript_tool_events as tte
        from services.notifications import notification_manager as nm
        rows = []
        monkeypatch.setattr(nm, "broadcast_chat_status", lambda *a, **k: None)
        monkeypatch.setattr(nm, "agent_label", lambda a: a)

        async def _ping(user_sub, **kw):
            return None
        monkeypatch.setattr(nm, "fire_ephemeral", _ping)
        monkeypatch.setattr(tte, "persist_event",
                            lambda store, chat_id, block: rows.append(block))
        s = isess.InteractiveSession(session_id="park-3", chat_id="c", agent_name="agent",
                                     user_sub="u1")
        s.created_at = time.monotonic() - (isess.MIN_TURN_S + 1)
        s._title_armed = False
        for tool in ("AskUserQuestion", "ExitPlanMode", "request_user_input"):
            s.park_on_native_dialog(tool)
        await asyncio.sleep(0.05)
        assert rows == []
        assert s.question_parked is True

    async def test_chained_native_dialogs_each_ping(self, monkeypatch):
        # The CLI journals late and the model chains a plan approval onto a
        # question: the second hook park lands before any tail consumed the
        # first park's dedupe flag. Every hook park is a fresh wait on the
        # human, so each one pings.
        from services.notifications import notification_manager as nm
        pings = []
        monkeypatch.setattr(nm, "broadcast_chat_status", lambda owner, cid, status, agent="": None)
        monkeypatch.setattr(nm, "agent_label", lambda a: a)

        async def _ping(user_sub, **kw):
            pings.append(kw.get("title", ""))
        monkeypatch.setattr(nm, "fire_ephemeral", _ping)
        s = isess.InteractiveSession(session_id="park-3", chat_id="c", agent_name="agent",
                                     user_sub="u1")
        s.created_at = time.monotonic() - (isess.MIN_TURN_S + 1)
        s._title_armed = False
        s._turn_open = True
        s.park_on_native_dialog("AskUserQuestion")
        await asyncio.sleep(0.05)
        s.park_on_native_dialog("ExitPlanMode")
        await asyncio.sleep(0.05)
        assert len(pings) == 2
        # The tailer's fold for the last dialog still pings nothing.
        s._maybe_fire_turn_complete("", persisted=1, question=True)
        await asyncio.sleep(0.05)
        assert len(pings) == 2

    async def test_park_on_native_dialog_without_open_turn_still_clears(self, monkeypatch):
        # A late transcript never opened the turn: the close-side effects
        # (ready broadcast) must still run so the dot clears.
        from services.notifications import notification_manager as nm
        statuses = []
        monkeypatch.setattr(nm, "broadcast_chat_status",
                            lambda owner, cid, status, agent="": statuses.append(status))
        monkeypatch.setattr(nm, "agent_label", lambda a: a)

        async def _ping(user_sub, **kw):
            return None
        monkeypatch.setattr(nm, "fire_ephemeral", _ping)
        s = isess.InteractiveSession(session_id="park-2", chat_id="c", agent_name="agent",
                                     user_sub="u1")
        s.created_at = time.monotonic() - (isess.MIN_TURN_S + 1)
        s._title_armed = False
        s.park_on_native_dialog("AskUserQuestion")
        await asyncio.sleep(0.05)
        assert "ready" in statuses
        assert s.question_parked is True

    async def test_close_for_a_deleted_chat_broadcasts_no_ready(self, monkeypatch):
        # DELETE /v1/chats/{id} closes the live terminal before the row goes:
        # the close-side ready frame and unread stamp would outlive the chat.
        from services.notifications import notification_manager as nm
        statuses = []
        monkeypatch.setattr(nm, "broadcast_chat_status",
                            lambda owner, cid, status, agent="": statuses.append(status))
        s = await _register("sid-deleted")
        s._set_turn_open(True)
        assert statuses == ["streaming"]
        await s.close(reason="chat_deleted")
        assert statuses == ["streaming"]
        assert s._turn_open is False

    async def test_close_for_any_other_reason_still_clears_the_dot(self, monkeypatch):
        from services.notifications import notification_manager as nm
        statuses = []
        monkeypatch.setattr(nm, "broadcast_chat_status",
                            lambda owner, cid, status, agent="": statuses.append(status))
        s = await _register("sid-reaped")
        s._set_turn_open(True)
        await s.close(reason="idle")
        assert statuses == ["streaming", "ready"]

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

    # -- mid-turn periodic tail (local incremental persistence) ---------------

    async def test_midturn_tail_arms_on_open_and_cancels_on_close(self):
        s = await _register("sid-midturn")
        try:
            assert s._midturn_tail_handle is None
            s._set_turn_open(True)
            assert s._midturn_tail_handle is not None
            s._set_turn_open(False)
            assert s._midturn_tail_handle is None
        finally:
            await s.close()

    async def test_midturn_tail_never_arms_for_remote(self):
        s = await _register("sid-midturn-remote")
        try:
            s.target = "machine-1"  # remote: satellite polling already persists mid-turn
            s._set_turn_open(True)
            assert s._midturn_tail_handle is None
        finally:
            await s.close()

    async def test_midturn_tail_runner_tails_and_rearms_while_open(self):
        s = await _register("sid-midturn-run")
        try:
            tailed = []
            s._tail_and_maybe_complete = lambda: tailed.append(1) or _noop()
            s._turn_open = True
            s._run_midturn_tail()
            await asyncio.sleep(0)
            assert tailed == [1]
            assert s._midturn_tail_handle is not None  # cycle re-armed
            s._midturn_tail_handle.cancel()
            s._midturn_tail_handle = None
            # Turn closed → the runner stands down without tailing or re-arming.
            s._turn_open = False
            s._run_midturn_tail()
            await asyncio.sleep(0)
            assert tailed == [1]
            assert s._midturn_tail_handle is None
        finally:
            await s.close()

    async def test_midturn_tail_skips_beat_while_previous_inflight(self):
        s = await _register("sid-midturn-busy")
        try:
            tailed = []
            s._tail_and_maybe_complete = lambda: tailed.append(1) or _noop()
            s._turn_open = True
            s._midturn_tail_task = asyncio.get_running_loop().create_task(
                asyncio.sleep(30))  # previous tail still off-thread
            s._run_midturn_tail()
            await asyncio.sleep(0)
            assert tailed == []                        # beat skipped
            assert s._midturn_tail_handle is not None  # but the cycle continues
            s._midturn_tail_handle.cancel()
            s._midturn_tail_handle = None
            s._midturn_tail_task.cancel()
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

    async def test_every_chat_id_shape_nudges(self, monkeypatch):
        """The rows nudge keys on the chat id being set, never on its shape —
        an interactive session's chat is always a real chat (a meeting spawns
        headless participants; core-seams phase 4 dropped the dead ``meeting-``
        guard)."""
        from services.notifications import notification_manager as nm
        calls = []
        monkeypatch.setattr(nm, "broadcast_chat_rows",
                            lambda *a, **k: calls.append(a))
        s = isess.InteractiveSession(session_id="pb-2", chat_id="meeting-x",
                                     agent_name="agent", user_sub="u1")
        s._post_batch_effects({"persisted": 2})
        assert len(calls) == 1

    async def test_a_batch_reaches_the_duplex_feed_through_its_listener(self, monkeypatch):
        """Core never imports the socket package: the duplex feed registers
        itself as a batch listener when ``ws.duplex_attach`` is imported, and
        the listener reads ``on_interactive_batch`` at call time."""
        from services.notifications import notification_manager as nm
        from ws import duplex_attach
        assert duplex_attach._feed_listener in isess._batch_listeners
        fed = []
        monkeypatch.setattr(nm, "broadcast_chat_rows", lambda *a, **k: None)
        monkeypatch.setattr(duplex_attach, "on_interactive_batch",
                            lambda chat_id, **kw: fed.append((chat_id, kw)))
        s = isess.InteractiveSession(session_id="pb-feed", chat_id="c-feed",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        s._post_batch_effects({"persisted": 2})
        assert fed == [("c-feed", {"persisted": 2, "turn_open": s._turn_open})]

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

    async def test_a_compaction_the_tailer_sees_reaches_the_starts_record(self, monkeypatch):
        """A terminal's compaction is recorded where the tailer sees it: a hook
        that answered the ``compact`` source keeps what it served, one that
        did not leaves the conversation holding nothing (``start_context``)."""
        from core.session import start_context
        seen = []
        monkeypatch.setattr(start_context, "compacted", seen.append)
        monkeypatch.setattr(isess.InteractiveSession, "_turn_end_effects", lambda self: None)
        monkeypatch.setattr(isess.InteractiveSession, "_fire_turn_notification",
                            lambda self, question=False, compacted=False: None)
        s = isess.InteractiveSession(session_id="pb-sc", chat_id="c-pbsc",
                                     agent_name="agent", user_sub="u1")
        s._chat_owner_sub = "u1"
        s._post_batch_effects({"persisted": 0})
        s._post_batch_effects({"persisted": 1, "compacted": True})
        assert seen == ["pb-sc"]

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
    async def test_a_meeting_note_stays_for_the_next_typed_message(self, temp_db):
        """A terminal's start replays the wakes only: a note waits for the
        person's next message, never queued as a turn of its own."""
        from storage import database as task_store
        task_store.create_chat("chat-note", "user-1", "agent")
        task_store.append_pending_delegate_wake("chat-note", "A WAKE")
        task_store.append_pending_delegate_wake("chat-note", "A NOTE", note=True)

        s = await _register("sid-note", chat_id="chat-note")
        try:
            assert [item["text"] for item in s._prompt_queue] == ["A WAKE"]
            assert task_store.claim_pending_notes("chat-note") == ["A NOTE"]
        finally:
            await s.close(reason="test-end")

    @staticmethod
    def _lane_chat():
        """A Shared-only agent's lane chat whose round u-alice pays, holding
        a stored result."""
        from auth import roles
        from core.session.visibility import shared_chat_owner
        from services.scheduler import task_kinds
        from storage import database as task_store
        from storage.agents import agent_store
        agent_store.create_agent("is-shared", "Shared", default_scope="agent", collaborative=False)
        for sub in ("u-alice", "u-bob"):
            task_store.upsert_user(sub, f"{sub}@t.io", sub, "member")
            task_store.add_user_agent(sub, "is-shared", roles.EDITOR, sub)
        task_store.create_chat("chat-lane", shared_chat_owner("is-shared"), "is-shared", "auto",
                               origin="delegated")
        task_store.create_run("r-lane", "task-x", "is-shared", "manual", None, "go", task_kinds.DELEGATE,
                              "agent", "u-alice", spender_sub="u-alice")
        task_store.update_run("r-lane", chat_id="chat-lane", started_at="2026-11-13T06:00:00")
        task_store.append_pending_delegate_wake("chat-lane", "THE LANE'S RESULT")

    @pytest.mark.asyncio
    async def test_a_lanes_wake_waits_for_a_terminal_that_serves_the_lane(self, temp_db):
        """A terminal starts on a lane's chat before its seat is bound: the
        stored result is typed in only when the payer its config was built
        with serves the lane's person, never a viewer's."""
        from storage import database as task_store
        self._lane_chat()
        viewer = await register(session_id="sid-viewer", chat_id="chat-lane", agent_name="is-shared",
                                payer="u-bob")
        try:
            assert len(viewer._prompt_queue) == 0
        finally:
            await viewer.close(reason="test-end")
        own = await register(session_id="sid-own", chat_id="chat-lane", agent_name="is-shared",
                             payer="u-alice")
        try:
            assert [item["text"] for item in own._prompt_queue] == ["THE LANE'S RESULT"]
            assert task_store.claim_pending_delegate_wake("chat-lane") == []
        finally:
            await own.close(reason="test-end")

    @pytest.mark.asyncio
    async def test_a_terminal_taken_back_asks_the_gate_by_its_binding(self, temp_db, monkeypatch):
        """The adoption's tail after a restart: a terminal bound to a viewer
        leaves the lane's result stored; one bound to the lane's person takes
        it."""
        from core import concurrency as _concurrency
        from core.session import session_manager
        from services.engines import subscription_pool as sp
        from storage import database as task_store
        self._lane_chat()

        async def _slot(*_a, **_k):
            return True
        monkeypatch.setattr(_concurrency, "acquire_chat_slot", _slot)
        monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: None)
        for sid, person, taken in (("sid-back-viewer", "u-bob", []), ("sid-back-own", "u-alice", ["W"])):
            sp.bind_session(sid, "sub-1", layer="claude-code-cli", user_sub=person)
            typed = []
            sess = SimpleNamespace(session_id=sid, chat_id="chat-lane", alive=True, target="m-1", user_sub=person,
                                   agent_name="is-shared", execution_path="claude-code-cli", payer=None,
                                   queue_prompt=lambda text, source, _typed=typed, **_k: _typed.append("W"))
            await isess._finish_adoption(sess)
            assert typed == taken
        assert task_store.claim_pending_delegate_wake("chat-lane") == []

    @pytest.mark.asyncio
    async def test_no_wake_no_queue(self, temp_db):
        from storage import database as task_store
        task_store.create_chat("chat-nowake", "user-1", "agent")
        s = await _register("sid-nowake", chat_id="chat-nowake")
        try:
            assert len(s._prompt_queue) == 0
        finally:
            await s.close(reason="test-end")


@pytest.mark.asyncio
class TestSpawnCollisionGuard:
    """5a spawn-collision guards (incident 2026-08-06): the warmup spawn
    funnel must never start a second runtime for a session id a LIVE
    interactive session already owns (the headless pump twin), and a composer
    `chat` frame landing while the layer's liveness check says dead must be
    delivered to the live PTY instead of re-warming. Exercised by direct
    method calls on stub connections — the full `chat`-frame WS flow is
    pinned by tests/session/test_ws_dashboard_*."""

    async def test_warmup_funnel_refuses_spawn_over_live_interactive(
            self, temp_db, caplog):
        import ws.dashboard  # noqa: F401  (assembles the controller mixins first)
        from core.remote.remote_execution import RemoteExecutionLayer
        from storage import remote_store
        from tests._compute import COMPUTE
        from ws.dashboard_warmup import WarmupController

        # A terminal of the platform's place runs on a compute machine: its
        # record's target is that machine's id, and the layer that answers
        # for it is the machines' one (the row says it is a compute machine).
        remote_store.create_remote_machine(COMPUTE["id"], COMPUTE["name"], None,
                                           pairing_scope="compute")
        live = await register(
            session_id="sid-collide", chat_id="chat-collide", agent_name="agent",
            target=COMPUTE["id"],
        )
        try:
            conn = WarmupController()
            conn.user_sub = "user-guard"
            conn.user = {"username": "guard", "role": "manager"}
            with caplog.at_level("WARNING", logger="otodock"):
                res = await conn._create_or_resume_session(
                    "sid-collide", "agent", "default", resume=True,
                    adopt=False,
                )
            assert res.session_id == "sid-collide"
            assert res.interactive is True
            assert res.execution_target == live.target == COMPUTE["id"]
            assert isinstance(res.layer, RemoteExecutionLayer)
            assert "refusing headless spawn" in caplog.text
            # Reused, not superseded — the live session keeps its PTY.
            assert live.alive
            assert isess.get("sid-collide") is live
        finally:
            await live.close()

    async def test_chat_send_delivered_to_live_interactive(self, temp_db):
        import ws.dashboard  # noqa: F401  (assembles the controller mixins first)
        from storage import database as task_store
        from ws.dashboard_chat import ChatController

        task_store.create_chat("chat-deliver", "user-d", "agent",
                               model="test-model")
        live = await _register("sid-deliver", chat_id="chat-deliver")
        try:
            conn = ChatController()
            conn.user_sub = "user-d"
            conn.user = {"username": "duser", "role": "manager"}
            conn.chat_id = "chat-deliver"
            conn.agent_name = "agent"
            conn.session_id = "sid-deliver"
            # The connection's own default (``DashboardConnection``): this
            # sender drives the chat.
            conn._view_only = False
            sent = []

            async def _send(frame):
                sent.append(frame)

            conn._send = _send
            handled = await conn._deliver_chat_to_live_interactive(
                {"text": "hello twin"})
            assert handled == "typed"
            # The client is told the session is interactive (it sent `chat`
            # believing it dead) — the re-attach frame shape.
            ready = [f for f in sent if f.get("type") == "warmup_ready"]
            assert ready and ready[0]["interactive"] is True
            assert ready[0]["session_id"] == "sid-deliver"
            # The prompt reached the PTY (buffered — the TUI is not ready yet).
            assert b"hello twin" in b"".join(live._input_buffer)
            # The user row persisted at send-time, attributed to the sender.
            rows = task_store.get_chat_messages("chat-deliver")
            assert any(r["role"] == "user" and r["content"] == "hello twin"
                       and r["author_sub"] == "user-d" for r in rows)
        finally:
            await live.close()

    async def test_chat_send_refused_attachments_answers_with_error(self, temp_db):
        """A photo the shared-workspace gate refuses never reaches the PTY:
        the error frame answers the send, nothing is typed, no row lands."""
        import ws.dashboard  # noqa: F401  (assembles the controller mixins first)
        from storage import database as task_store
        from ws.dashboard_chat import ChatController
        from ws.dashboard_chat_support import AttachmentsRefused

        task_store.create_chat("chat-refuse", "user-v", "agent",
                               model="test-model")
        live = await _register("sid-refuse", chat_id="chat-refuse")
        try:
            conn = ChatController()
            conn.user_sub = "user-v"
            conn.user = {"username": "vuser", "role": "member"}
            conn.chat_id = "chat-refuse"
            conn.agent_name = "agent"
            conn.session_id = "sid-refuse"
            sent = []

            async def _send(frame):
                sent.append(frame)

            async def _send_error(text):
                sent.append({"type": "error", "message": text})

            async def _refuse(*a, **k):
                raise AttachmentsRefused(
                    "Photos in this chat land in the agent's shared workspace")

            conn._send = _send
            conn._send_error = _send_error
            conn._process_attachments = _refuse
            handled = await conn._deliver_chat_to_live_interactive(
                {"text": "look", "images": [{"data": "x", "name": "a.png"}]})
            assert handled == "typed"
            assert [f["type"] for f in sent] == ["error"]
            assert "shared workspace" in sent[0]["message"]
            assert b"".join(live._input_buffer) == b""
            assert task_store.get_chat_messages("chat-refuse") == []
        finally:
            await live.close()


@pytest.mark.asyncio
async def test_register_records_the_owner(monkeypatch):
    """A terminal's registration names its owner for the
    per-person count (never refused on the cap: the person asked for this
    chat)."""
    seen: dict = {}

    async def _acq(sid, **kw):
        seen.update(kw)
        return concurrency.Admission(True)
    monkeypatch.setattr(concurrency, "acquire_chat_slot", _acq)
    monkeypatch.setattr(concurrency, "release_chat_slot", lambda sid: None)

    s = await register(
        session_id="sid-owner", chat_id="chat-owner", agent_name="agent",
        user_sub="user-owner",
    )
    try:
        assert seen["user_sub"] == "user-owner"
        assert seen["per_user_cap"] is False
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_reap_idle_reads_the_cached_timeout(monkeypatch):
    """The reaper's default timeout comes from the cached, off-loop
    read; the setting is never read on the loop per tick."""
    from unittest.mock import AsyncMock

    from core.session import session_state
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=1))
    s = await _register("sid-cached-timeout")
    s.last_activity = time.monotonic() - 10_000
    assert await isess.reap_idle() == 1
    assert isess.get("sid-cached-timeout") is None
    session_state.cached_idle_timeout.assert_awaited_once()


@pytest.mark.asyncio
class TestClosingMark:
    async def test_close_marks_the_session_closing(self):
        from core.session import session_state
        s = await _register("sid-closing")
        assert session_state.session_is_held("sid-closing")
        await s.close(reason="idle")
        assert not session_state.session_is_held("sid-closing")
        assert session_state.session_is_live("sid-closing")
        session_state.reset_liveness_for_tests()


class _FakeRemotePty:
    pid = 4242

    def __init__(self):
        self.closed = False

    def close(self, signal_child: bool = True) -> None:
        self.closed = True

    def scrollback(self) -> bytes:
        return b""

    def write(self, data: bytes) -> None:
        pass


@pytest.fixture
def _fake_satellite(monkeypatch):
    from core.remote import remote_pty, satellite_connection

    async def _spawn(**_kw):
        return _FakeRemotePty()

    class _Manager:
        def satellite_os(self, _machine_id):
            return "linux"

        def is_pty_in_grace(self, _machine_id):
            return False

        def is_compute(self, _machine_id):
            return False

        def compute_machine_ids(self):
            return frozenset()

        def machine_memory_report(self, _machine_id, compute=False):
            return None   # no report: not checked by memory

    monkeypatch.setattr(remote_pty, "spawn_remote_pty", _spawn)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _Manager)


async def _register_remote_parked(session_id: str, payload: dict):
    from storage.agents.schema import DEFAULT_EXECUTION_PATH
    s = await isess.register_remote(
        session_id=session_id, chat_id="chat-1", agent_name="agent",
        machine_id="machine-1", execution_path=DEFAULT_EXECUTION_PATH,
        config_payload=payload)
    s._apply_turn_signal("end_turn", question_pending=True)
    s.last_activity = time.monotonic() - 150  # > timeout, < 2x timeout
    return s


@pytest.mark.asyncio
async def test_a_remote_parked_terminal_ends_with_its_payload_token(_fake_satellite):
    # The token minted into the start payload, not the terminal's own start:
    # the payload build (and a satellite MCP install) can run minutes before.
    from auth.session_token import create_session_token
    token = create_session_token(
        "sid-rpark-token", "agent", issued_at=int(time.time()) - 24 * 3600 + 30)
    await _register_remote_parked("sid-rpark-token", {"env": {"PROXY_API_KEY": token}})
    assert await isess.reap_idle(timeout_s=100) == 1
    assert isess.get("sid-rpark-token") is None


@pytest.mark.asyncio
async def test_a_remote_parked_terminal_with_no_readable_token_uses_its_start(
        _fake_satellite, monkeypatch):
    s = await _register_remote_parked("sid-rpark-fresh", {"env": {"PROXY_API_KEY": "junk"}})
    assert await isess.reap_idle(timeout_s=100) == 0  # a day of token left
    await s.close()
    monkeypatch.setattr(isess, "_SESSION_TOKEN_LIFE_S", 30)
    await _register_remote_parked("sid-rpark-old", {})
    assert await isess.reap_idle(timeout_s=100) == 1
    assert isess.get("sid-rpark-old") is None


@pytest.mark.asyncio
async def test_a_machine_terminal_is_spared_on_the_stop_it_sends_through_the_tunnel(
        _fake_satellite, monkeypatch):
    # A machine's Stop reaches the same route through the tunnel, with the
    # script the start payload carried: no satellite change.
    from api.hooks import lifecycle
    from core.session import session_state
    from storage.agents.schema import DEFAULT_EXECUTION_PATH

    async def _pass(auth, sid):
        return None

    monkeypatch.setattr(lifecycle, "verify_session_match_async", _pass)
    monkeypatch.setattr("core.session.transcript_tailer.tail_transcript", lambda *a: {})
    monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
    s = await isess.register_remote(
        session_id="sid-machine-bg", chat_id="chat-1", agent_name="agent",
        machine_id="machine-1", execution_path=DEFAULT_EXECUTION_PATH, config_payload={})
    try:
        await lifecycle.hook_stop(lifecycle.HookStopRequest(
            session_id="sid-machine-bg", background_kinds=["shell"]), authorization="Bearer x")
        session_state._session_hook_activity["sid-machine-bg"] = time.monotonic() - 500
        s.last_activity = time.monotonic() - 10_000
        assert await isess.reap_idle(timeout_s=100) == 0
        await lifecycle.hook_stop(lifecycle.HookStopRequest(
            session_id="sid-machine-bg", background_kinds=[]), authorization="Bearer x")
        session_state._session_hook_activity["sid-machine-bg"] = time.monotonic() - 500
        assert await isess.reap_idle(timeout_s=100) == 1
        assert isess.get("sid-machine-bg") is None
    finally:
        session_state._session_hook_activity.pop("sid-machine-bg", None)
        from core.session import session_events
        session_events.cleanup_session("sid-machine-bg")


@pytest.mark.asyncio
class TestFirstMessageWaitsForTools:
    """A Claude Code terminal sends its first message without the tool
    servers still connecting: the platform holds it until every stand-in
    of the session says the engine has its list, never past the bound."""

    async def test_held_until_the_last_stand_in_is_listed(self):
        from services.mcp import tool_listing
        s = await _register("sid-tools")
        try:
            tool_listing.expect_listed("sid-tools", {"memory-mcp", "checks-mcp"})
            received = bytearray()
            s.add_output_listener(received.extend)
            s.write_input(b"first\n")
            s._mark_ready()
            await asyncio.sleep(0.4)
            assert s._ready is False and b"first" not in bytes(received)
            tool_listing.note_listed("sid-tools", "memory-mcp")
            await asyncio.sleep(0.4)
            assert s._ready is False
            tool_listing.note_listed("sid-tools", "checks-mcp")
            await asyncio.sleep(0.6)
            assert s._ready is True and b"first" in bytes(received)
            assert tool_listing.pending_listed("sid-tools") == frozenset()
        finally:
            await s.close()

    async def test_goes_in_at_the_bound(self):
        from services.mcp import tool_listing
        s = await _register("sid-bound")
        try:
            tool_listing.expect_listed("sid-bound", {"slow-mcp"})
            received = bytearray()
            s.add_output_listener(received.extend)
            s.write_input(b"first\n")
            s._ready_deadline = time.monotonic() - 1
            s._mark_ready()
            await asyncio.sleep(0.4)
            assert s._ready is True and b"first" in bytes(received)
        finally:
            await s.close()

    async def test_a_session_with_nothing_to_wait_for_is_not_held(self):
        s = await _register("sid-none")
        try:
            s._mark_ready()
            assert s._ready is True
        finally:
            await s.close()

    async def test_the_wait_ends_with_the_session(self):
        from services.mcp import tool_listing
        s = await _register("sid-gone")
        tool_listing.expect_listed("sid-gone", {"memory-mcp"})
        s._mark_ready()
        await s.close()
        assert tool_listing.pending_listed("sid-gone") == frozenset()
        assert s._tools_recheck is None or s._tools_recheck.cancelled()


@pytest.mark.asyncio
class TestDroppedTurn:
    """Claude Code stops a turn on an Escape before the model's first word
    without a transcript line, and puts the text back in its input box: the
    platform closes that turn once, and holds its own prompts meanwhile."""

    async def _open(self, sid):
        s = await _register(sid)
        s._mark_ready()
        s._turn_open = True
        closed = []
        s._set_turn_open = closed.append  # type: ignore[method-assign]
        return s, closed

    async def test_an_escape_then_quiet_closes_the_turn_once(self):
        s, closed = await self._open("sid-esc")
        try:
            s.write_input(b"\x1b")
            assert s._dropped_turn_check is not None
            s._last_io_at = time.monotonic() - 10
            s._check_dropped_turn()
            assert closed == [False]
            assert s._composer_dirty is True
        finally:
            await s.close()

    async def test_the_dropped_turn_leaves_a_card_and_a_live_frame(self, monkeypatch):
        """The person sees the turn end with no answer: a card says the
        agent stopped before it answered and the message is back in the
        terminal's input box, written once and sent to the chat's viewers.
        Nothing when output follows the Escape."""
        import json

        from core.events import chat_writer
        from core.session import session_state
        rows, frames = [], []
        monkeypatch.setattr(chat_writer, "submit", lambda cid, job, **k: rows.append((cid, job)))
        monkeypatch.setattr(session_state, "broadcast_chat_frame", lambda cid, f: frames.append((cid, f)))
        s, closed = await self._open("sid-card")
        try:
            s.write_input(b"\x1b")
            s._last_io_at = time.monotonic()
            s._check_dropped_turn()
            assert rows == [] and frames == []
            s._last_io_at = time.monotonic() - 10
            s._check_dropped_turn()
            assert closed == [False]
            assert [(cid, f["type"], f["subtype"]) for cid, f in frames] == [("chat-1", "system", "turn_dropped")]
            [(cid, job)] = rows
            assert cid == "chat-1" and json.loads(job.keywords["event_data"]) == {
                "type": "system", "subtype": "turn_dropped"}
        finally:
            await s.close()

    async def test_the_platforms_stop_closes_it_too(self):
        s, closed = await self._open("sid-stop")
        try:
            assert s.interrupt_turn() is True
            s._last_io_at = time.monotonic() - 10
            s._check_dropped_turn()
            assert closed == [False]
        finally:
            await s.close()

    async def test_output_after_the_escape_keeps_it_open(self):
        s, closed = await self._open("sid-moving")
        try:
            s.write_input(b"\x1b")
            s._last_io_at = time.monotonic()  # the spinner still moves
            s._check_dropped_turn()
            assert closed == []
        finally:
            await s.close()

    async def test_a_hook_after_the_escape_keeps_it_open(self):
        from core.session.session_state import record_hook_activity
        s, closed = await self._open("sid-hooked")
        try:
            s.write_input(b"\x1b")
            record_hook_activity("sid-hooked")
            s._last_io_at = time.monotonic() - 10
            s._check_dropped_turn()
            assert closed == []
        finally:
            await s.close()

    async def test_an_escape_before_the_turn_opened_is_looked_at_again(self):
        """The transcript journals the person's line a moment after the
        engine took it: the Escape can come before the turn is open."""
        s, closed = await self._open("sid-early")
        try:
            s._turn_open = False
            s.write_input(b"\x1b")
            s._check_dropped_turn()
            assert closed == [] and s._dropped_turn_check is not None  # looks again
            s._turn_open = True
            s._last_io_at = time.monotonic() - 10
            s._check_dropped_turn()
            assert closed == [False]
        finally:
            await s.close()

    async def test_an_escape_with_no_turn_is_let_be(self):
        s, closed = await self._open("sid-idle")
        try:
            s._turn_open = False
            s.write_input(b"\x1b")
            for _ in range(3):
                s._check_dropped_turn()
            assert closed == [] and s._dropped_turn_check is None
        finally:
            await s.close()

    async def test_no_escape_no_check(self):
        s, _closed = await self._open("sid-plain")
        try:
            s.write_input(b"x")
            assert s._dropped_turn_check is None
        finally:
            await s.close()


def test_the_stand_ins_of_a_claude_tool_config():
    from services.mcp import tool_listing
    servers = {
        "memory-mcp": {"command": "py", "env": {tool_listing.STANDIN_ENV: "{}"}},
        "agent-config-mcp": {"command": "py", "env": {"A": "1"}},
        "file-tools": {"url": "http://x/mcp"},
    }
    assert tool_listing.standin_servers_of(servers) == ["memory-mcp"]
    tool_listing.expect_listed("sid-x", [])
    assert tool_listing.pending_listed("sid-x") == frozenset()


@pytest.mark.asyncio
async def test_a_terminal_the_memory_guard_ended_tells_its_chat(monkeypatch):
    """The machine's guard ended the terminal's engine: the chat gets the
    headless chat's card and line, written before the viewer hears of the exit
    (its page then reloads the chat's history, which holds the card)."""
    import json

    from core.events import turn_ending
    from core.session import session_state
    from storage import database as task_store
    from ws import wire_events as wire

    order: list = []
    monkeypatch.setattr(task_store, "add_chat_message",
                        lambda chat_id, role, text, **kw: order.append(("row", chat_id, json.loads(kw["event_data"]))))
    monkeypatch.setattr(session_state, "broadcast_chat_frame", lambda chat_id, frame: order.append(("frame", chat_id)))
    s = await _register("sid-memory", chat_id="chat-memory")
    s.on_close = lambda sess, reason: order.append(("close", reason))
    s.pty.exit_reason = "memory"
    s._on_pty_exit(-9)
    for _ in range(100):
        if any(o[0] == "close" for o in order):
            break
        await asyncio.sleep(0.02)
    rows = [o for o in order if o[0] == "row"]
    assert len(rows) == 1 and rows[0][1] == "chat-memory"
    block = rows[0][2]
    line = turn_ending.TurnEnding(reason=turn_ending.EXITED, detail=turn_ending.MEMORY).line()
    assert block["subtype"] == wire.SUBTYPE_TURN_ENDED and block["message"] == line
    assert [o[0] for o in order] == ["row", "frame", "close"]



@pytest.mark.asyncio
async def test_a_terminals_home_on_the_platforms_disk_goes_at_its_close(tmp_path, monkeypatch):
    """A terminal chat on a compute machine beside the platform runs from a
    home in the shared folder: its close lets go of the renewal target, then
    removes the home, before the seat is released. A home the platform does
    not hold (a paired computer's) is not touched."""
    from core.sandbox import session_home
    from services.engines import pool_bindings, token_fanout
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    agents = tmp_path / "agents"
    store = agents / "pa" / "users" / "alice" / ".claude"
    store.mkdir(parents=True)
    home = session_home.prepare(store, sid, ("projects",))
    (home / ".credentials.json").write_text("token")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    s = await _register(sid)
    seen: list = []
    real_drop = session_home.drop

    def _drop(*a, **k):
        seen.append(("drop", token_fanout.session_target(sid), pool_bindings.bound_folder(sid)))
        return real_drop(*a, **k)
    monkeypatch.setattr(session_home, "drop", _drop)
    pool_bindings._session_scope_keys[sid] = pool_bindings.scope_key_for("c-1", str(store), str(home))
    token_fanout.register_session_target(sid, token_fanout.CredentialFileTarget(
        layer="claude-code-cli", host_dir=str(home)))
    try:
        await s.close()
    finally:
        pool_bindings._session_scope_keys.pop(sid, None)
        token_fanout.unregister_session_target(sid)
    assert seen == [("drop", None, str(home))]
    assert not home.exists() and (store / "projects").is_dir()



def test_a_close_touches_no_home_the_platform_does_not_hold(tmp_path, monkeypatch):
    """The binding's folder is a home of this session under the platform's
    folders, or nothing is removed: a folder elsewhere, or another session's
    home, is left as it is."""
    from core.sandbox import session_home
    from core.sandbox.session_config_dir import close_session_home
    from services.engines import pool_bindings
    sid, other = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "11111111-2222-3333-4444-555555555555"
    agents, elsewhere = tmp_path / "agents", tmp_path / "elsewhere"
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    (elsewhere / ".claude").mkdir(parents=True)
    (agents / "pa" / "workspace" / ".claude").mkdir(parents=True)
    outside = session_home.prepare(elsewhere / ".claude", sid, ())
    inside_other = session_home.prepare(agents / "pa" / "workspace" / ".claude", other, ())
    try:
        pool_bindings._session_scope_keys[sid] = pool_bindings.scope_key_for("c-1", "", str(outside))
        assert close_session_home(sid) is False and outside.is_dir()
        # A binding that names another session's home is not trusted at all:
        # nothing in that store is removed, the session's own name there neither.
        (inside_other.parent / sid).mkdir()
        pool_bindings._session_scope_keys[sid] = pool_bindings.scope_key_for("c-1", "", str(inside_other))
        assert close_session_home(sid) is False and inside_other.is_dir() and (inside_other.parent / sid).is_dir()
    finally:
        pool_bindings._session_scope_keys.pop(sid, None)



def test_a_close_removes_a_home_whose_agents_folder_is_behind_a_link(tmp_path, monkeypatch):
    """The binding names the home at the agent's real place. An agents
    folder the operator reached through a link is the same folder: the
    close asks the agent folder's owner, never compares the two texts."""
    from core.sandbox import session_home
    from core.sandbox.session_config_dir import close_session_home
    from services.engines import pool_bindings
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    real = tmp_path / "disk" / "agents"
    store = real / "pa" / "workspace" / ".claude"
    store.mkdir(parents=True)
    (tmp_path / "agents").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    home = session_home.prepare(store, sid, ())
    pool_bindings._session_scope_keys[sid] = pool_bindings.scope_key_for("c-1", str(store), str(home))
    try:
        assert close_session_home(sid) is True and not home.exists()
    finally:
        pool_bindings._session_scope_keys.pop(sid, None)


def test_a_start_that_failed_drops_the_home_it_made(tmp_path, monkeypatch):
    """A start on the platform makes the session's home and writes its
    sign-in before the machine answers. One that fails there is bound to
    nothing, so no close finds the home: the start drops it itself. Only
    this session's own home in an agent's folder."""
    from core.sandbox import session_home
    from core.sandbox.session_config_dir import drop_unstarted_home
    sid, other = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "11111111-2222-3333-4444-555555555555"
    agents, elsewhere = tmp_path / "agents", tmp_path / "elsewhere"
    store = agents / "pa" / "workspace" / ".claude"
    store.mkdir(parents=True)
    (elsewhere / ".claude").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    home = session_home.prepare(store, sid, ())
    (home / ".credentials.json").write_text("the start's")
    sibling = session_home.prepare(store, other, ())
    outside = session_home.prepare(elsewhere / ".claude", sid, ())
    assert drop_unstarted_home(sid, str(sibling)) is False and sibling.is_dir()
    assert drop_unstarted_home(sid, str(outside)) is False and outside.is_dir()
    assert drop_unstarted_home(sid, str(store)) is False and drop_unstarted_home(sid, "") is False
    assert drop_unstarted_home(sid, str(home)) is True and not home.exists() and sibling.is_dir()


def test_a_close_while_a_start_of_its_id_runs_leaves_the_home(tmp_path, monkeypatch):
    """A terminal replaced under its id is closed inside the new start
    (``register``'s lease), after that start made the home under the same
    id: the start in flight owns it, so the old close leaves it. A close
    with no start in flight holds the id while it removes the home, so a
    start of the id waits for it."""
    from core.sandbox import session_home
    from core.sandbox.session_config_dir import close_session_home
    from core.session import session_closing, session_state
    from services.engines import pool_bindings
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    agents = tmp_path / "agents"
    store = agents / "pa" / "workspace" / ".claude"
    store.mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    home = session_home.prepare(store, sid, ())
    pool_bindings._session_scope_keys[sid] = pool_bindings.scope_key_for("c-1", str(store), str(home))
    try:
        session_state.mark_starting(sid, 60)
        assert close_session_home(sid) is False and home.is_dir()
        session_state.clear_starting(sid)
        held: list[bool] = []
        real_drop = session_home.drop
        monkeypatch.setattr(session_home, "drop",
                            lambda *a, **k: held.append(sid in session_closing._holds) or real_drop(*a, **k))
        assert close_session_home(sid) is True and held == [True] and not home.exists()
        assert sid not in session_closing._holds
    finally:
        session_state.clear_starting(sid)
        pool_bindings._session_scope_keys.pop(sid, None)
