"""Background work between turns: the lines a sub-agent writes while the main
agent is idle, the chat monitor that waits for it, and what a page rebuilt
from rows is told still runs.

Frames are the shapes Claude Code 2.1.296 and 2.1.312 write (recorded against
a scripted API, every stdout line and hook payload logged): a
sub-agent's own lines carry its spawn's ``parent_tool_use_id``, its end comes
as ``task_updated{completed}``, ``task_notification`` and a
``background_tasks_changed`` snapshot even while the main session is idle,
and a command it started names it in ``parent_task_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core.events import bg_command_state, pump_bg_monitors, wake_capture
from core.events.bg_command_state import get_bg_command_registry
from core.layers.cli.translator import FRAME_BACKGROUND, FRAME_MAIN, FRAME_NEUTRAL
from core.session import (
    background_frames,
    background_leash,
    session_events,
    session_state,
)
from core.session.background_frames import (
    log_between_turns_census,
    resolve_background_frame,
    running_background,
)
from core.session.session_state import get_permission_queue, get_subagent_registry
from tests._paths import PROXY_DIR
from ws import wire_events as wire


def _sys(subtype: str, **kw) -> dict:
    return {"type": "system", "subtype": subtype, **kw}


@pytest.fixture
def chat_frames(monkeypatch):
    """The frames pushed to chats (no pump runs between turns: the push
    answers False and the broadcast is recorded)."""
    pushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(session_state, "_push_pump_event_fn", lambda cid, ev: False)
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda cid, frame, **kw: pushed.append((cid, frame)))
    return pushed


@pytest.fixture
def sid():
    name = "s-bgf"
    yield name
    session_state._subagent_registries.pop(name, None)
    bg_command_state._bg_command_registries.pop(name, None)
    wake_capture.forget_session(name)


def _agent(sid: str, task_id="a1", tuid="tuA1", chat="chat-bgf", desc="long reader",
           background=True):
    reg = get_subagent_registry(sid)
    reg.chat_id = chat
    reg.register_spawn(task_id, tuid, label=f'"{desc}" [{task_id}]', description=desc,
                       background=background)
    return reg


# ---------------------------------------------------------------------------
# resolve_background_frame
# ---------------------------------------------------------------------------

def test_a_sub_agents_stdout_end_clears_its_badge_and_the_hook_is_a_duplicate(sid, chat_frames):
    reg = _agent(sid)
    kind = resolve_background_frame(sid, _sys(
        "task_notification", task_id="a1", tool_use_id="tuA1", status="completed"))
    assert kind == FRAME_BACKGROUND
    assert not reg.has_pending
    assert chat_frames == [("chat-bgf", {"type": wire.BG_AGENT_DONE, "tool_use_id": "tuA1"})]
    # The stop hook that comes after (or a satellite that posts it late).
    assert session_events.subagent_stop(sid, "a1") is False


def test_a_terminal_task_update_ends_the_agent_too(sid, chat_frames):
    reg = _agent(sid)
    resolve_background_frame(sid, _sys("task_updated", task_id="a1", patch={"status": "completed"}))
    assert not reg.has_pending
    # A non-terminal patch ends nothing.
    reg2 = _agent(sid, task_id="a2", tuid="tuA2")
    resolve_background_frame(sid, _sys("task_updated", task_id="a2", patch={"is_backgrounded": True}))
    assert reg2.has_pending


def test_an_unknown_task_end_resolves_nothing(sid, chat_frames):
    _agent(sid)
    resolve_background_frame(sid, _sys("task_notification", task_id="bX", status="completed"))
    assert get_subagent_registry(sid).has_pending
    assert chat_frames == []


class _ModuleTime:
    """A module's ``time`` with its own monotonic clock (the event loop keeps
    the real one)."""

    def __init__(self, monotonic) -> None:
        self.monotonic = monotonic

    def __getattr__(self, name: str):
        return getattr(time, name)


def test_a_sub_agents_lines_and_frames_stamp_it_alive(sid, monkeypatch):
    reg = _agent(sid)
    clock = iter([100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr(session_state, "time", _ModuleTime(lambda: next(clock)))
    line = {"type": "assistant", "parent_tool_use_id": "tuA1", "message": {"content": []}}
    assert resolve_background_frame(sid, line) == FRAME_BACKGROUND
    assert reg.last_seen["a1"] == 100.0
    resolve_background_frame(sid, _sys("task_progress", task_id="a1"))
    assert reg.last_seen["a1"] == 200.0
    resolve_background_frame(sid, _sys("task_started", task_id="bS", parent_task_id="a1",
                                       owned_by_subagent=True, task_type="local_bash"))
    assert reg.last_seen["a1"] == 300.0
    # A sub-agent's command is never the main agent's background command.
    assert get_bg_command_registry(sid).pending_count == 0


def test_the_frame_kinds(sid):
    assert resolve_background_frame(sid, {"type": "assistant", "message": {}}) == FRAME_MAIN
    assert resolve_background_frame(sid, {"type": "result"}) == FRAME_MAIN
    assert resolve_background_frame(sid, {"type": "stream_event", "event": {}}) == FRAME_MAIN
    assert resolve_background_frame(sid, _sys("status", status="requesting")) == FRAME_NEUTRAL
    assert resolve_background_frame(sid, {"type": "rate_limit_event"}) == FRAME_NEUTRAL
    assert resolve_background_frame(sid, _sys("background_tasks_changed", tasks=[])) == FRAME_BACKGROUND
    assert resolve_background_frame(sid, "not a frame") == FRAME_NEUTRAL  # type: ignore[arg-type]


def test_the_snapshot_records_the_commands_a_sub_agent_still_runs(sid, chat_frames):
    reg = _agent(sid)
    resolve_background_frame(sid, _sys("background_tasks_changed", tasks=[
        {"task_id": "a1", "task_type": "local_agent"},
        {"task_id": "bOwn", "task_type": "local_bash", "parent_task_id": "a1"},
    ]))
    assert reg.owned_alive == {"bOwn"}
    assert reg.has_pending
    # The agent reported; its command still runs (2.1.312, a command outliving its agent).
    resolve_background_frame(sid, _sys("background_tasks_changed", tasks=[
        {"task_id": "bOwn", "task_type": "local_bash", "parent_task_id": "a1"}]))
    assert not reg.has_pending and reg.owned_alive == {"bOwn"}
    resolve_background_frame(sid, _sys("background_tasks_changed", tasks=[]))
    assert reg.owned_alive == set()


def test_running_background_names_the_pending_spawns_of_the_chats(sid):
    reg = _agent(sid)
    _agent(sid, task_id="a2", tuid="tuA2", desc="short worker")
    reg.mark_done("a2")
    bgreg = get_bg_command_registry(sid)
    bgreg.chat_id = "chat-bgf"
    bgreg.register_spawn("b1", "tuB1", label="b1 — sleep 150", description="wait a while")
    other = "s-bgf-other"
    try:
        _agent(other, task_id="z1", tuid="tuZ1", chat="chat-other")
        assert running_background(["chat-bgf"]) == {
            "agents": [{"id": "tuA1", "description": "long reader"}],
            "commands": [{"id": "tuB1", "description": "wait a while"}],
        }
        both = running_background(["chat-bgf", "chat-other", ""])
        assert {a["id"] for a in both["agents"]} == {"tuA1", "tuZ1"}
        assert running_background(["chat-none"]) == {"agents": [], "commands": []}
    finally:
        session_state._subagent_registries.pop(other, None)


def test_running_background_names_background_spawns_only(sid, monkeypatch):
    # A foreground agent ends inside its turn; a terminal's registry is its
    # tailer's, whose ends send no done frame.
    _agent(sid)
    _agent(sid, task_id="aF", tuid="tuF", desc="helper", background=False)
    assert [a["id"] for a in running_background(["chat-bgf"])["agents"]] == ["tuA1"]
    from core.session import interactive_session
    monkeypatch.setattr(interactive_session, "get", lambda s: object() if s == sid else None)
    assert running_background(["chat-bgf"])["agents"] == []


def test_the_census_warns_only_for_the_main_agents_lines(caplog):
    caplog.set_level(logging.INFO, logger="otodock")
    log_between_turns_census("Remote session s1", {"background": 675})
    log_between_turns_census("Remote session s2", {"background": 3, "assistant": 2, "result": 1})
    log_between_turns_census("Remote session s3", {"wake_captured": 1})
    # The operator's 675-line drain: background lines and four rate limits.
    log_between_turns_census("Remote session s4", {"background": 671, "neutral": 4})
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (logging.INFO, "Remote session s1: read 675 lines of background work between turns "
                          "(types={'background': 675})") in records
    assert any(lvl == logging.WARNING and msg.startswith("Remote session s2: drained 6 stale")
               for lvl, msg in records)
    assert not any("s3" in msg for _, msg in records)
    assert [lvl for lvl, msg in records if msg.startswith("Remote session s4")] == [logging.INFO]


def test_a_stale_line_is_named_for_the_census_and_seen_by_the_engine_prompts(sid, monkeypatch):
    from core.session import engine_prompts
    seen: list[str] = []
    monkeypatch.setattr(engine_prompts, "note_frame", lambda s, d, **kw: seen.append(d.get("type")))
    _agent(sid)
    assert background_frames.read_stale_line(sid, {"type": "rate_limit_event"}) == "neutral"
    assert background_frames.read_stale_line(sid, {"type": "control_cancel_request"}) == "neutral"
    assert background_frames.read_stale_line(
        sid, {"type": "assistant", "parent_tool_use_id": "tuA1", "message": {}}) == "background"
    assert background_frames.read_stale_line(sid, {"type": "assistant", "message": {}}) == "assistant"
    assert seen == ["rate_limit_event", "control_cancel_request", "assistant", "assistant"]


def test_a_drain_counts_the_work_it_ended(sid, chat_frames):
    _agent(sid)
    assert background_frames.resolve_background_work(sid, _sys("task_progress", task_id="a1")) is False
    assert background_frames.resolve_background_work(sid, _sys(
        "task_notification", task_id="a1", status="completed")) is True


# ---------------------------------------------------------------------------
# The hook routes
# ---------------------------------------------------------------------------

def test_the_stop_hook_reaches_a_page_that_never_registered_the_session(sid, chat_frames):
    from app import app
    reg = _agent(sid)
    client = TestClient(app)
    with patch("api.hooks.lifecycle.verify_session_match_async", new_callable=AsyncMock), \
         patch("api.hooks.lifecycle.routing.resolve_hook_chat_id", new_callable=AsyncMock,
               return_value="chat-bgf"):
        resp = client.post("/v1/hooks/subagent", json={"session_id": sid, "agent_id": "a1"},
                           headers={"Authorization": "Bearer dummy"})
    assert resp.json() == {"status": "ok"}
    assert not reg.has_pending
    assert chat_frames == [("chat-bgf", {"type": wire.BG_AGENT_DONE, "tool_use_id": "tuA1"})]


def test_a_sub_agents_tool_result_is_recorded_never_drawn(sid, monkeypatch):
    from app import app
    reg = _agent(sid)
    reg.last_seen["a1"] = 0.0
    recorded: list[tuple] = []
    monkeypatch.setattr(session_events, "post_tool",
                        lambda s, name, **kw: recorded.append((s, name, kw.get("tool_use_id"))))
    client = TestClient(app)
    try:
        with patch("api.hooks.lifecycle.verify_session_match_async", new_callable=AsyncMock):
            resp = client.post("/v1/hooks/tool-result", json={
                "session_id": sid, "tool_name": "Bash", "tool_use_id": "tuSubBash",
                "summary": "ls", "result_content": "x", "agent_id": "a1"},
                headers={"Authorization": "Bearer dummy"})
        assert resp.json() == {"status": "ok"}
        assert recorded == [(sid, "Bash", "tuSubBash")]
        assert get_permission_queue(sid).empty()
        assert reg.last_seen["a1"] > 0.0
    finally:
        session_state._permission_emitters.pop(sid, None)


def _tracker_post(status_codes: list[int]) -> tuple[int, int]:
    """Run subagent_tracker.py against a stub that answers ``status_codes`` in
    turn; returns (posts received, the script's exit code)."""
    codes = list(status_codes)
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            code = codes.pop(0) if codes else 200
            self.send_response(code)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    script = PROXY_DIR / "hooks" / "subagent_tracker.py"
    env = {**os.environ, "PROXY_URL": f"http://127.0.0.1:{server.server_address[1]}",
           "PROXY_API_KEY": "k", "OTO_SESSION_ID": "s-track"}
    env.pop("OTO_HOOK_NO_FORWARD", None)
    env.pop("OTO_INTERACTIVE", None)
    try:
        proc = subprocess.run([sys.executable, "-I", str(script)], input=json.dumps(
            {"agent_id": "a9", "agent_type": "general-purpose"}), text=True, env=env, timeout=30)
    finally:
        server.shutdown()
    assert all(p["agent_id"] == "a9" and p["session_id"] == "s-track" for p in seen)
    return len(seen), proc.returncode


def test_the_tracker_posts_again_after_a_reconnecting_tunnels_503():
    assert _tracker_post([503, 200]) == (2, 0)


def test_the_tracker_gives_up_after_three_refusals_and_never_on_a_4xx():
    assert _tracker_post([503, 503, 503, 503]) == (3, 0)
    assert _tracker_post([401]) == (1, 0)


# ---------------------------------------------------------------------------
# The agent monitor waits as long as the agent lives
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


class _IdleLayer:
    """An idle session the agent monitor polls: each drain stands for a
    2.5 s slice of the session's life (``step``), then runs ``on_drain``."""

    def __init__(self, clock: _Clock, on_drain, step: float = 2.5) -> None:
        self.clock, self.on_drain, self.step = clock, on_drain, step
        self.drains = 0

    async def is_session_alive(self, session_id: str) -> bool:
        return True

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        self.drains += 1
        self.clock.now += self.step
        return bool(self.on_drain(self.clock.now))


@pytest.fixture
def monitor_env(monkeypatch, sid):
    clock = _Clock()
    monkeypatch.setattr(pump_bg_monitors, "time", _ModuleTime(clock.monotonic))
    monkeypatch.setattr(session_state, "time", _ModuleTime(clock.monotonic))
    monkeypatch.setattr(pump_bg_monitors, "AGENT_POLL_S", 0.001)
    monkeypatch.setattr(pump_bg_monitors.task_store, "get_chat", lambda cid: {})
    monkeypatch.setattr(background_leash, "background_work_ceiling", lambda: 1800.0)
    pushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(session_state, "_push_pump_event_fn", lambda cid, ev: False)
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda cid, frame, **kw: pushed.append((cid, frame)))
    queue: asyncio.Queue = asyncio.Queue()
    session_state._dashboard_notify_queues[sid] = queue
    yield clock, queue, pushed
    session_state._dashboard_notify_queues.pop(sid, None)


def _queued(queue: asyncio.Queue) -> list[dict]:
    return [queue.get_nowait() for _ in range(queue.qsize())]


@pytest.mark.asyncio
async def test_a_research_agent_past_ten_minutes_is_waited_for(monitor_env, sid, caplog):
    clock, queue, _ = monitor_env
    start = clock.now
    reg = _agent(sid)

    def life(now: float) -> bool:
        reg.note_alive("a1")            # a line of its own every slice
        if now - start >= 900:          # its stop hook, fifteen minutes in
            return session_events.subagent_stop(sid, "a1")
        return False

    layer = _IdleLayer(clock, life)
    caplog.set_level(logging.WARNING, logger="otodock")
    await pump_bg_monitors._bg_agent_monitor(layer, sid, "chat-bgf", 1)
    assert clock.now - start >= 900
    assert not any("lost hook" in r.getMessage() or "no sign of life" in r.getMessage()
                   for r in caplog.records)
    nudges = [q for q in _queued(queue) if q["type"] == wire.NOTIFY_BG_NUDGE]
    assert [n["labels"] for n in nudges] == [['"long reader" [a1]']]


@pytest.mark.asyncio
async def test_a_silent_agent_is_given_up_while_another_keeps_the_wait(monitor_env, sid, caplog):
    clock, queue, pushed = monitor_env
    start = clock.now
    reg = _agent(sid, task_id="a1", tuid="tuA1", desc="lost one")
    _agent(sid, task_id="a2", tuid="tuA2", desc="busy one")

    def life(now: float) -> bool:
        reg.note_alive("a2")
        if now - start >= 2400:
            return session_events.subagent_stop(sid, "a2")
        return False

    caplog.set_level(logging.WARNING, logger="otodock")
    await pump_bg_monitors._bg_agent_monitor(_IdleLayer(clock, life), sid, "chat-bgf", 2)
    warnings = [r.getMessage() for r in caplog.records if "no sign of life" in r.getMessage()]
    assert len(warnings) == 1
    assert '"lost one" [a1]' in warnings[0] and "1800s" in warnings[0]
    assert ("chat-bgf", {"type": wire.BG_AGENT_DONE, "tool_use_id": "tuA1"}) in pushed
    # The review names the agent that ended, never the one given up on.
    nudges = [q for q in _queued(queue) if q["type"] == wire.NOTIFY_BG_NUDGE]
    assert [n["labels"] for n in nudges] == [['"busy one" [a2]']]
    # A stop hook that arrives after all is a duplicate.
    assert session_events.subagent_stop(sid, "a1") is False


@pytest.mark.asyncio
async def test_the_drain_goes_on_while_a_sub_agents_own_command_runs(monitor_env, sid):
    clock, queue, _ = monitor_env
    reg = _agent(sid)
    reg.owned_alive = {"bOwn"}

    def life(now: float) -> bool:
        if reg.has_pending:
            return session_events.subagent_stop(sid, "a1")
        if layer.drains >= 4:
            reg.owned_alive = set()     # the snapshot with the command gone
        return False

    layer = _IdleLayer(clock, life)
    await pump_bg_monitors._bg_agent_monitor(layer, sid, "chat-bgf", 1)
    assert layer.drains >= 4
    assert any(q["type"] == wire.NOTIFY_BG_NUDGE for q in _queued(queue))


@pytest.mark.asyncio
async def test_a_wake_turn_read_meanwhile_reviewed_the_finished_agents(monitor_env, sid):
    clock, queue, _ = monitor_env
    _agent(sid)

    def life(now: float) -> bool:
        session_events.subagent_stop(sid, "a1")
        wake_capture.note_captured(sid)     # the CLI's own review turn
        return True

    await pump_bg_monitors._bg_agent_monitor(_IdleLayer(clock, life), sid, "chat-bgf", 1)
    assert not any(q["type"] == wire.NOTIFY_BG_NUDGE for q in _queued(queue))


_OWNED = {"task_id": "bOwn", "parent_task_id": "a1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshots", [
    # Claude Code 2.1.312, a command outliving its agent by 70 s: the empty
    # snapshot comes before the agent's re-run.
    [[_OWNED], [], [{"task_id": "a1"}], []],
    # The parent dropped from the row while the command still runs.
    [[_OWNED], [{"task_id": "bOwn"}], [{"task_id": "a1"}], []],
], ids=["recorded", "parent-dropped"])
async def test_a_sub_agents_own_command_is_read_on_through_the_wake(monitor_env, sid, snapshots):
    # The agent ends while its command runs; when the command ends the CLI
    # starts the agent again under its id, then wakes the main agent.
    clock, queue, _ = monitor_env
    _agent(sid)
    snap = lambda tasks: _sys("background_tasks_changed", tasks=tasks)
    steps = iter(
        [lambda: (resolve_background_frame(sid, snap(snapshots[0])),
                  session_events.subagent_stop(sid, "a1"), wake_capture.note_captured(sid))]
        + [lambda t=t: resolve_background_frame(sid, snap(t)) for t in snapshots[1:]]
        + [lambda: None, lambda: wake_capture.note_captured(sid)]
    )

    def life(now: float) -> bool:
        next(steps, lambda: None)()
        return False

    layer = _IdleLayer(clock, life)
    await pump_bg_monitors._bg_agent_monitor(layer, sid, "chat-bgf", 1)
    assert layer.drains >= len(snapshots) + 2      # read on until the second wake
    assert not any(q["type"] == wire.NOTIFY_BG_NUDGE for q in _queued(queue))
    assert get_subagent_registry(sid).owned_alive == set()


@pytest.mark.asyncio
async def test_an_agent_waiting_on_a_person_is_never_given_up(monitor_env, sid, monkeypatch, caplog):
    clock, queue, _ = monitor_env
    start = clock.now
    _agent(sid)
    monkeypatch.setattr(pump_bg_monitors, "has_pending_prompt", lambda s: s == sid)

    def life(now: float) -> bool:
        if now - start >= 2400:       # past the ceiling, the card answered
            return session_events.subagent_stop(sid, "a1")
        return False

    caplog.set_level(logging.WARNING, logger="otodock")
    await pump_bg_monitors._bg_agent_monitor(_IdleLayer(clock, life), sid, "chat-bgf", 1)
    assert not any("no sign of life" in r.getMessage() for r in caplog.records)
    nudges = [q for q in _queued(queue) if q["type"] == wire.NOTIFY_BG_NUDGE]
    assert [n["labels"] for n in nudges] == [['"long reader" [a1]']]


def test_a_between_turns_frame_reaches_every_socket_once():
    # A second tab and a view-only viewer on the chat hold no session sink
    # (the last socket to warm the session does): the done frame reaches them
    # too, and the sink's own socket once.
    from core.events.input_queue import _connections_by_user

    class _Socket:
        def __init__(self, queue: asyncio.Queue) -> None:
            self.notify_queue = queue

    sink, tab, viewer = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    session_state._dashboard_notify_queues["s-sink"] = sink
    _connections_by_user["u-bgf"] = {_Socket(q) for q in (sink, tab, viewer)}
    try:
        session_state.broadcast_chat_frame("chat-bgf", {"type": wire.BG_AGENT_DONE, "tool_use_id": "tuA1"})
        for q in (sink, tab, viewer):
            assert [i["chat_id"] for i in _queued(q)] == ["chat-bgf"]
    finally:
        session_state._dashboard_notify_queues.pop("s-sink", None)
        _connections_by_user.pop("u-bgf", None)
