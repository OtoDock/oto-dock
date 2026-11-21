"""Characterization (golden-master) suite for the dashboard connection's warmup
(``ws/dashboard_warmup.py``) and its spawn funnel in
``ws/dashboard_warmup_spawn.py`` (``_create_or_resume_session``,
``_resume_dead_session_for_chat``, ``_give_reused_session_the_chats_mode``,
``_reapply_deferred_after_warmup``).

Every test drives the real handler through the dashboard harness and records
what the code does today: the frames sent, the calls the fake execution layer
and the admission ledger saw, the chat rows written. Fakes sit at the harness's
seams only; nothing global of the warmup module is patched.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import uuid
from types import SimpleNamespace

import pytest

import config
from core import concurrency, placement
from core.events import chat_writer
from core.events.common_events import DONE, TEXT, CommonEvent
from core.execution_layer import MachineReach, ResumeVerdict
from core.session import (
    interactive_session,
    prewarm_session_registry,
    session_manager,
    session_state,
)
from services.engines.subscription_pool import NoSubscriptionError
from services.remote import remote_status
from storage import database as task_store
from storage.agents import agent_store
from storage.billing import subscription_store
from storage.pg import get_conn
from tests import _engines
from tests.fixtures.characterization.golden_master import check_golden
from tests.fixtures.ws_dashboard_harness import (
    HARNESS_COMPUTE,
    TEST_MODEL,
    FakeDashboardWebSocket,
    FakeExecutionLayer,
    FakeInteractiveSession,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    set_username,
    stub_dashboard_seams,
)

_STARTUP_FRAMES = 4
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NOW = re.compile(r"\[Current time: [^\]]*\]")
_VOLATILE_KEYS = {"build_id", "version", "started_at", "message_ids"}


class _Golden:
    """Replaces what varies between runs: a uuid by its order of appearance, a
    time stamp, this box's paths."""

    def __init__(self):
        self.ids: dict[str, str] = {}

    def _id(self, match) -> str:
        return self.ids.setdefault(match.group(0), f"<id{len(self.ids) + 1}>")

    def __call__(self, value):
        if isinstance(value, dict):
            return {k: ("<volatile>" if k in _VOLATILE_KEYS
                        else "<ADMISSION_QUEUE_WAIT_S>" if k == "queue_wait_s" and v == config.ADMISSION_QUEUE_WAIT_S
                        else self(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        if isinstance(value, str):
            value = _UUID.sub(self._id, value)
            value = _NOW.sub("[Current time: <now>]", value)
            return value.replace(str(config.AGENTS_DIR), "<agents>").replace(tempfile.gettempdir(), "<tmp>")
        return value


def _config_of(sid: str, cfg) -> dict:
    context = cfg.security_context
    return {
        "session_id": sid, "agent": cfg.agent_name, "user_sub": cfg.user_sub, "chat_id": cfg.chat_id,
        "client_type": cfg.client_type, "mode": cfg.permission_mode, "model": cfg.model,
        "resume": cfg.resume, "resume_handle": cfg.resume_handle,
        "execution_path": cfg.execution_path, "execution_target": cfg.execution_target,
        "fallback_reason": cfg.fallback_reason, "interactive": cfg.interactive,
        "interactive_theme": cfg.interactive_theme,
        "interactive_first_prompt": cfg.interactive_first_prompt,
        "role": getattr(context, "role", None), "username": getattr(context, "username", None),
    }


def _chat_of(cid: str) -> dict:
    row = task_store.get_chat(cid) or {}
    return {
        **{k: row.get(k) for k in (
            "agent", "user_sub", "session_id", "model", "permission_mode", "execution_path",
            "execution_target", "execution_mode", "tui_theme", "pending_history_seed", "title")},
        "rows": [[m["role"], m["content"]] for m in task_store.get_chat_messages(cid)],
    }


def _frames_of(ws) -> list:
    """What the socket sent past its connect frames. A pong is the test's own
    barrier. The dispatcher's unread count follows each warmup from its own
    thread hop, racing what the spawn tail sends, so those frames are kept
    last, in their number."""
    frames = [f for f in ws.sent[_STARTUP_FRAMES:] if f["type"] != "pong"]
    return ([f for f in frames if f["type"] != "notification_count"]
            + [f for f in frames if f["type"] == "notification_count"])


class _World:
    """One test's fake layer and ledger, and what they saw as one golden value."""

    def __init__(self, monkeypatch, layer=None):
        self.layer = layer or FakeExecutionLayer()
        self.slots = stub_dashboard_seams(monkeypatch, self.layer)
        self.chats: list[str] = []
        self.sockets: list[list] = []

    def chat(self, slug, *, session_id=None, mode="ask", model=TEST_MODEL,
             user_sub="user-admin", **columns) -> str:
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, user_sub, slug, mode, model=model, execution_path="claude-code-cli")
        if session_id or columns:
            task_store.update_chat(cid, **({"session_id": session_id} if session_id else {}), **columns)
        self.chats.append(cid)
        return cid

    def keep(self, ws) -> list:
        self.sockets.append(_frames_of(ws))
        return self.sockets[-1]

    def seen(self, extra=None) -> dict:
        layer = self.layer
        return _Golden()({
            "frames": self.sockets[0] if len(self.sockets) == 1 else self.sockets,
            "chats": [_chat_of(cid) for cid in self.chats],
            "layer": {
                "started": [_config_of(sid, cfg) for sid, cfg in layer.started],
                "messages": layer.messages, "mode_changes": layer.mode_changes,
                "model_changes": layer.model_changes, "closed": layer.closed_sessions,
                "prepared_resume": layer.prepared_resume, "reconnect_waits": layer.reconnect_waits,
                "forgotten_probes": layer.forgotten_probes,
            },
            "slots": {"acquired": self.slots.acquired, "released": self.slots.released},
            **(extra or {}),
        })

    def run(self, drive) -> None:
        """Runs ``drive(ws)`` on a dashboard socket and keeps what it sent."""
        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                await drive(ws)
                ws.client_send({"type": "close"})
            self.keep(ws)
        run_ws_scenario(scenario, timeout=30.0)


async def _until(ws, kind: str, timeout: float = 5.0) -> dict:
    while True:
        try:
            frame = await ws.next_frame(timeout)
        except asyncio.TimeoutError as e:
            raise AssertionError(f"no {kind} frame; sent: {ws.sent[_STARTUP_FRAMES:]}") from e
        if frame["type"] == kind:
            return frame


async def _barrier(ws) -> None:
    ws.client_send({"type": "ping"})
    await _until(ws, "pong")


async def _finished(*coroutine_names: str) -> None:
    """The socket's background work of these names has run to its end (the
    spawn tail goes on after its last frame, a pre-warm answers off the
    dispatcher)."""
    me = asyncio.current_task()
    while True:
        running = [t for t in asyncio.all_tasks() if t is not me and not t.done()
                   and getattr(t.get_coro(), "__name__", "") in coroutine_names]
        if not running:
            return
        await asyncio.wait(running, timeout=5)


async def _spawned_warmup(ws, layer, msg: dict, end: str = "warmup_ready") -> dict:
    """A warmup run to the end of its tail. The spawn waits for the dispatcher's
    own frame (one that fails before it starts does not, and its frame may come
    first); the frame ``end`` is returned."""
    layer.start_gate = asyncio.Event()
    ws.client_send({"type": "warmup", **msg})
    ended, counted = None, False
    while ended is None or not counted:
        try:
            frame = await ws.next_frame(5.0)
        except asyncio.TimeoutError as e:
            raise AssertionError(f"no {end} frame; sent: {ws.sent[_STARTUP_FRAMES:]}") from e
        if frame["type"] == "notification_count":
            counted = True
            layer.start_gate.set()
        elif frame["type"] == end:
            ended = frame
    await _finished("_spawn_tail")
    layer.start_gate = None
    return ended


async def _inline_warmup(ws, msg: dict) -> None:
    ws.client_send({"type": "warmup", **msg})
    await _until(ws, "notification_count")


@pytest.fixture
def world(temp_db, monkeypatch):
    from services.scheduler import shared
    monkeypatch.setattr(shared, "_shutting_down", False)
    prewarm_session_registry._entries.clear()
    yield _World(monkeypatch)
    prewarm_session_registry._entries.clear()


def _a_turn() -> list:
    return [CommonEvent(type=TEXT, data={"content": "Hello there!"}), CommonEvent(type=DONE, data={})]


# ---------------------------------------------------------------------------
# A warmup that mints a chat.
# ---------------------------------------------------------------------------

def test_a_warmup_mints_a_chat(world):
    slug = make_test_agent("warm-mint")

    async def drive(ws):
        minted = await _spawned_warmup(ws, world.layer, {"agent": slug})
        world.chats.append(minted["chat_id"])
        await chat_writer.drain(minted["chat_id"], timeout=5)
        # An unknown id, the frame's picks, a light page, a model the engine does not serve.
        minted = await _spawned_warmup(ws, world.layer, {
            "agent": slug, "chat_id": "no-such-chat", "permission_mode": "dontAsk",
            "execution_mode": "headless", "theme": "light", "model": "foreign-model"})
        world.chats.append(minted["chat_id"])
        await chat_writer.drain(minted["chat_id"], timeout=5)
    world.run(drive)
    check_golden("warmup/mint", world.seen())


def test_a_warmup_with_text_mints_a_chat_and_runs_its_first_turn(world):
    slug = make_test_agent("warm-mint-turn")
    set_username("user-admin", "admin")
    world.layer.turn_events = _a_turn()

    async def drive(ws):
        minted = await _spawned_warmup(ws, world.layer, {"agent": slug, "text": "Say hello"})
        world.chats.append(minted["chat_id"])
        await _until(ws, "turn_complete")
        await chat_writer.drain(minted["chat_id"], timeout=5)
    world.run(drive)
    check_golden("warmup/mint-first-turn", world.seen())


def test_warmup_refusals_before_any_spawn(world):
    """The gates ahead of the spawn: no agent, no access, a deleted agent, the
    editor tier of a Shared-only agent, the chat row's agent winning over the
    frame's stale one."""
    slug = make_test_agent("warm-refused")
    shared = make_test_agent("warm-refused-shared", collaborative=False, default_scope="agent")
    other = make_test_agent("warm-refused-other")
    denied = make_test_agent("warm-refused-denied")
    task_store.set_user_agents("user-viewer", [slug, shared], "user-admin",
                               agent_roles={slug: "editor", shared: "viewer"})
    denied_chat = world.chat(denied, user_sub="user-viewer", session_id=str(uuid.uuid4()))
    world.chats.remove(denied_chat)
    rebound = world.chat(other)
    world.layer.alive.add("sid-rebound")
    task_store.update_chat(rebound, session_id="sid-rebound")
    session_state.set_session_mode("sid-rebound", "plan")

    async def scenario():
        cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                name="Viewer User", role="member")
        async with dashboard_connection(cookie) as ws:
            await drain_startup(ws)
            for msg in ({"type": "warmup"},
                        {"type": "warmup", "agent": other},
                        {"type": "warmup", "agent": "no-such-agent"},
                        {"type": "warmup", "agent": shared},
                        {"type": "warmup", "agent": slug, "chat_id": denied_chat}):
                ws.client_send(msg)
                await _until(ws, "notification_count")
            ws.client_send({"type": "close"})
        world.keep(ws)
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            await _inline_warmup(ws, {"agent": "no-such-agent"})
            await _inline_warmup(ws, {"agent": slug, "chat_id": rebound})
            ws.client_send({"type": "close"})
        world.keep(ws)
    run_ws_scenario(scenario)
    check_golden("warmup/refusals", world.seen())


# ---------------------------------------------------------------------------
# A warmup of an existing chat that reuses a live session.
# ---------------------------------------------------------------------------

def test_a_warmup_reuses_a_live_session(world):
    slug = make_test_agent("warm-live")
    set_username("user-admin", "admin")
    live = str(uuid.uuid4())
    world.layer.alive.add(live)
    session_state.set_session_mode(live, "autoReview")
    cid = world.chat(slug, session_id=live, mode="autoReview")
    world.layer.turn_events = _a_turn()

    async def drive(ws):
        await _inline_warmup(ws, {"agent": slug, "chat_id": cid})
        await _barrier(ws)
        # The cold send on the same live session: its turn runs inline.
        ws.client_send({"type": "warmup", "agent": slug, "chat_id": cid, "text": "again"})
        await _until(ws, "turn_complete")
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    check_golden("warmup/reuse-live", world.seen())


def test_a_warmup_reattaches_a_live_terminal(world, monkeypatch):
    slug = make_test_agent("warm-terminal")
    idle, busy = str(uuid.uuid4()), str(uuid.uuid4())
    chat_idle, chat_busy = world.chat(slug, session_id=idle), world.chat(slug, session_id=busy)
    session_state.set_session_mode(idle, "ask")
    session_state.set_session_mode(busy, "plan")
    monkeypatch.setitem(interactive_session._sessions, idle, FakeInteractiveSession(idle, chat_idle))
    term = FakeInteractiveSession(busy, chat_busy)
    monkeypatch.setitem(interactive_session._sessions, busy, term)

    async def drive(ws):
        await _inline_warmup(ws, {"agent": slug, "chat_id": chat_idle})
        term._turn_open = True
        await _inline_warmup(ws, {"agent": slug, "chat_id": chat_busy})
    world.run(drive)
    check_golden("warmup/reuse-terminal", world.seen())


def test_a_warmup_reuses_a_session_whose_machine_is_coming_back(world):
    slug = make_test_agent("warm-grace")
    held, back = str(uuid.uuid4()), str(uuid.uuid4())
    chat_held, chat_back = world.chat(slug, session_id=held), world.chat(slug, session_id=back)
    world.layer.grace_held.add(held)        # still reconnecting past the wait
    world.layer.reconnecting[back] = 0.0    # back within the wait

    async def drive(ws):
        await _inline_warmup(ws, {"agent": slug, "chat_id": chat_held})
        await _inline_warmup(ws, {"agent": slug, "chat_id": chat_back})
    world.run(drive)
    check_golden("warmup/reuse-grace", world.seen())


def test_a_reuse_the_ledger_refuses(world, monkeypatch):
    slug = make_test_agent("warm-reuse-denied")
    live, term = str(uuid.uuid4()), str(uuid.uuid4())
    world.layer.alive.add(live)
    chat_live, chat_term = world.chat(slug, session_id=live), world.chat(slug, session_id=term)
    monkeypatch.setitem(interactive_session._sessions, term, FakeInteractiveSession(term, chat_term))

    async def _refuse(sid, **kwargs):
        return concurrency.Admission(False, "busy", "The server is busy.")

    monkeypatch.setattr(concurrency, "acquire_chat_slot", _refuse)

    async def drive(ws):
        await _inline_warmup(ws, {"agent": slug, "chat_id": chat_live})
        await _inline_warmup(ws, {"agent": slug, "chat_id": chat_term})
    world.run(drive)
    check_golden("warmup/reuse-refused", world.seen())


# ---------------------------------------------------------------------------
# A warmup that resumes a dead session.
# ---------------------------------------------------------------------------

def test_a_warmup_resumes_a_dead_session(world):
    slug = make_test_agent("warm-resume")
    set_username("user-admin", "admin")
    dead = str(uuid.uuid4())
    world.layer.resumable.add(dead)
    cid = world.chat(slug, session_id=dead, mode="plan", codex_thread_id="thread-kept")
    world.layer.turn_events = _a_turn()

    async def drive(ws):
        await _spawned_warmup(ws, world.layer, {"agent": slug, "chat_id": cid, "text": "go on"})
        await _until(ws, "turn_complete")
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    check_golden("warmup/resume-dead", world.seen())


def test_a_warmup_starts_fresh_when_the_dead_session_has_nothing_to_resume(world):
    slug = make_test_agent("warm-fresh")
    cid = world.chat(slug, session_id=str(uuid.uuid4()))

    async def drive(ws):
        await _spawned_warmup(ws, world.layer, {"agent": slug, "chat_id": cid, "execution_mode": "headless"})
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    check_golden("warmup/fresh-dead", world.seen())


@pytest.mark.parametrize("scope", ["agent", "user"])
def test_a_task_chat_rewarms_in_the_tasks_identity(world, scope):
    """A ``task-{run}`` chat rebuilds in its run's scope, never the viewer's."""
    slug = make_test_agent(f"warm-task-{scope}")
    set_username("user-admin", "admin")
    run_id = f"run-{scope}-golden"
    task_store.create_run(run_id, "dyn-x", slug, "manual", None, "go", "one-time", scope, "user-admin")
    cid = f"task-{run_id}"
    dead = str(uuid.uuid4())
    world.layer.resumable.add(dead)
    task_store.create_chat(cid, "user-admin", slug, "dontAsk", model=TEST_MODEL, execution_path="claude-code-cli")
    task_store.update_chat(cid, session_id=dead)
    world.chats.append(cid)

    async def drive(ws):
        await _spawned_warmup(ws, world.layer, {"agent": slug, "chat_id": cid})
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    check_golden(f"warmup/task-chat-{scope}", world.seen())


class _UndecidedLayer(FakeExecutionLayer):
    """A session whose conversation no probe can answer for: its machine away,
    or its files unreadable on this server's disk."""

    def __init__(self, reach):
        super().__init__()
        self.reach = reach

    async def can_resume_session(self, sid, **kw):
        return ResumeVerdict.UNKNOWN

    def resume_machine_reach(self, sid, **kw):
        return self.reach

    def resume_probe_future(self, sid, **kw):
        return None


@pytest.mark.parametrize("reach", [MachineReach.AWAY, MachineReach.LOCAL], ids=["away", "local"])
def test_a_warmup_refused_when_the_probe_has_no_answer(temp_db, monkeypatch, reach):
    world = _World(monkeypatch, _UndecidedLayer(reach))
    slug = make_test_agent(f"warm-undecided-{reach.name.lower()}")
    dead = str(uuid.uuid4())
    cid = world.chat(slug, session_id=dead)

    async def drive(ws):
        await _spawned_warmup(ws, world.layer, {"agent": slug, "chat_id": cid, "text": "hello"},
                              end="warmup_failed")
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    check_golden(f"warmup/undecided-{reach.name.lower()}", world.seen())


# ---------------------------------------------------------------------------
# ``_resume_dead_session_for_chat``: a send finds the session dead.
# ---------------------------------------------------------------------------

async def _send_on_dead(ws, world, slug, *, resumable: bool) -> None:
    minted = await _spawned_warmup(ws, world.layer, {"agent": slug})
    cid, sid = minted["chat_id"], minted["session_id"]
    world.chats.append(cid)
    await chat_writer.drain(cid, timeout=5)
    world.layer.alive.discard(sid)
    world.layer.dead_processes.add(sid)
    if resumable:
        world.layer.resumable.add(sid)
    ws.client_send({"type": "chat", "text": "are you there", "chat_id": cid})
    await _until(ws, "turn_complete")
    await chat_writer.drain(cid, timeout=5)


@pytest.mark.parametrize("resumable", [True, False], ids=["resumable", "fresh"])
def test_a_send_resumes_a_dead_session(world, resumable):
    slug = make_test_agent(f"warm-send-{'resume' if resumable else 'fresh'}")
    set_username("user-admin", "admin")
    world.layer.turn_events = _a_turn()

    async def drive(ws):
        await _send_on_dead(ws, world, slug, resumable=resumable)
    world.run(drive)
    check_golden(f"warmup/send-on-dead-{'resumable' if resumable else 'fresh'}", world.seen())


@pytest.mark.parametrize("resumable", [True, False], ids=["resumable", "fresh"])
def test_a_queued_turn_finds_its_session_dead(world, monkeypatch, resumable):
    """The chat's queue delivers as its author, headless: the dead session is
    warmed behind the socket (``_warm_dead_target``), the socket's own view stays."""
    from core.events import input_queue
    from core.events.common_events import TurnInput
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.0)
    slug = make_test_agent(f"warm-queued-{'resume' if resumable else 'fresh'}")
    set_username("user-admin", "admin")
    world.layer.turn_events = _a_turn()

    async def drive(ws):
        minted = await _spawned_warmup(ws, world.layer, {"agent": slug})
        cid, sid = minted["chat_id"], minted["session_id"]
        world.chats.append(cid)
        await chat_writer.drain(cid, timeout=5)
        world.layer.alive.discard(sid)
        world.layer.dead_processes.add(sid)
        if resumable:
            world.layer.resumable.add(sid)
        queue = await input_queue.loaded(cid)
        await queue.add("q-dead", "user-admin", TurnInput("typed while it was gone"))
        assert await input_queue.deliver(cid)
        while (await _until(ws, "chat_status"))["status"] != "ready":
            pass
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    check_golden(f"warmup/queued-on-dead-{'resumable' if resumable else 'fresh'}", world.seen())


def _connection(user_sub="user-admin", agent=""):
    from ws.dashboard import DashboardConnection
    conn = DashboardConnection(FakeDashboardWebSocket(session_cookie()), user_sub=user_sub,
                               user=task_store.get_user(user_sub))
    conn.agent_name, conn.chat_id, conn.session_id, conn.layer = agent, None, None, None
    conn.session_execution_target, conn.session_fallback_reason = "", None
    return conn


def test_the_engines_resume_answer_is_asked_off_the_loop(world, monkeypatch):
    """Whether an interactive spawn resumes is the engine's answer, which can
    search a folder on disk (Codex's rollouts): asked in a worker thread."""
    import threading
    slug = make_test_agent("warm-resume-thread")
    sid = str(uuid.uuid4())
    cid = world.chat(slug, session_id=sid)
    threads: list = []
    real = world.layer.resumes_interactive
    monkeypatch.setattr(world.layer, "resumes_interactive",
                        lambda cfg: threads.append(threading.current_thread()) or real(cfg))

    async def scenario():
        conn = _connection(agent=slug)
        await conn._resume_dead_session_for_chat(sid, cid, world.layer, adopt=False)
        await chat_writer.drain(cid, timeout=5)
    run_ws_scenario(scenario)
    assert threads and all(t is not threading.main_thread() for t in threads)


def test_the_base_resume_answer_is_asked_inline(world, monkeypatch):
    """An engine that keeps the base answer (the warmup's own flag) is asked
    inline: the probes' lane is only for an engine that searches its folder."""
    from core import file_commit
    from core.engine import Engine
    monkeypatch.setattr(type(world.layer), "resumes_interactive", Engine.resumes_interactive)
    lanes: list = []
    real = file_commit.run_in

    async def _run_in(lane, fn, *a, **k):
        lanes.append(lane)
        return await real(lane, fn, *a, **k)
    monkeypatch.setattr(file_commit, "run_in", _run_in)
    slug = make_test_agent("warm-resume-inline")
    sid = str(uuid.uuid4())
    cid = world.chat(slug, session_id=sid)

    async def scenario():
        conn = _connection(agent=slug)
        await conn._resume_dead_session_for_chat(sid, cid, world.layer, adopt=False)
        await chat_writer.drain(cid, timeout=5)
    run_ws_scenario(scenario)
    assert "session-probes" not in lanes


def test_a_dead_session_warmed_behind_the_viewed_chat(world):
    """``adopt=False``, a server turn on a chat this socket does not view: the
    returned spawn runs it, the connection's viewed attributes stay."""
    slug = make_test_agent("warm-behind")
    resumable, fresh, viewed = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    world.layer.resumable.update({resumable, viewed})
    chat_resume = world.chat(slug, session_id=resumable, mode="plan")
    chat_fresh = world.chat(slug, session_id=fresh)
    chat_viewed = world.chat(slug, session_id=viewed)
    out = {}

    async def scenario():
        conn = _connection(agent=slug)
        conn.chat_id, conn.session_id, conn.layer = "viewed-chat", "viewed-session", None
        for name, sid, cid in (("resumed", resumable, chat_resume), ("fresh", fresh, chat_fresh)):
            res = await conn._resume_dead_session_for_chat(sid, cid, world.layer, adopt=False)
            out[name] = {"session_id": res.session_id, "interactive": res.interactive,
                         "execution_target": res.execution_target, "fallback_reason": res.fallback_reason,
                         "first_prompt_in_argv": res.first_prompt_in_argv,
                         "layer_is_the_fake": res.layer is world.layer}
            await chat_writer.drain(cid, timeout=5)
        out["viewed"] = [conn.chat_id, conn.session_id, conn.layer is None]
        # ``adopt=True``: the viewed chat's own session; the connection takes it.
        res = await conn._resume_dead_session_for_chat(viewed, chat_viewed, world.layer, adopt=True)
        await chat_writer.drain(chat_viewed, timeout=5)
        out["viewed_adopted"] = {"result_session": res.session_id, "session_id": conn.session_id,
                                 "layer_is_the_fake": conn.layer is world.layer,
                                 "execution_target": conn.session_execution_target,
                                 "fallback_reason": conn.session_fallback_reason}
    run_ws_scenario(scenario)
    world.sockets.append([])
    check_golden("warmup/dead-behind-the-viewed-chat", world.seen({"results": out}))


@pytest.mark.parametrize("reach", [MachineReach.AWAY, MachineReach.LOCAL], ids=["away", "local"])
def test_a_send_on_a_dead_session_the_probe_cannot_answer_for(temp_db, monkeypatch, reach):
    from core.session.resume_probe import ResumeUndecided
    from ws.dashboard_chat_text import TurnDeferred
    world = _World(monkeypatch, _UndecidedLayer(reach))
    slug = make_test_agent(f"warm-send-{reach.name.lower()}")
    dead = "sid-undecided"     # a refusal names its first eight characters
    cid = world.chat(slug, session_id=dead)
    out = {}

    async def scenario():
        conn = _connection(agent=slug)
        try:
            await conn._resume_dead_session_for_chat(dead, cid, world.layer, adopt=True)
        except TurnDeferred as e:
            out.update(raised="TurnDeferred", session_id=e.session_id, unanswered=e.unanswered)
        except ResumeUndecided as e:
            out.update(raised="ResumeUndecided", session_id=e.session_id, local=e.local, message=str(e))
    run_ws_scenario(scenario)
    world.sockets.append([])
    check_golden(f"warmup/send-undecided-{reach.name.lower()}", world.seen({"result": out}))


def test_the_funnel_refuses_a_second_runtime_on_a_live_terminal(world, monkeypatch):
    sid = str(uuid.uuid4())
    term = FakeInteractiveSession(sid, "chat-term")
    # A terminal runs on the compute machine: its record names it.
    term.target = HARNESS_COMPUTE["id"]
    monkeypatch.setitem(interactive_session._sessions, sid, term)
    out = {}

    async def scenario():
        conn = _connection()
        for adopt in (False, True):
            res = await conn._create_or_resume_session(sid, "warm-term", "ask", resume=True, adopt=adopt)
            out[f"adopt={adopt}"] = {
                "session_id": res.session_id, "interactive": res.interactive,
                "execution_target": res.execution_target, "fallback_reason": res.fallback_reason,
                "viewed": [conn.session_id, conn.layer is world.layer, conn.session_execution_target]}
    run_ws_scenario(scenario)
    world.sockets.append([])
    check_golden("warmup/funnel-live-terminal", world.seen({"results": out}))


# ---------------------------------------------------------------------------
# The funnel's refusals, on warmup_failed.
# ---------------------------------------------------------------------------

def _machine(machine_id: str, name: str, *, admin: bool, owner: str = "user-admin") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, created_at, pairing_scope) "
            "VALUES (%s, %s, %s, '2026-06-11T00:00:00+00:00', %s)",
            (machine_id, name, owner, placement.PAIRING_ADMIN if admin else "user"))
        conn.commit()


def test_a_spawn_the_funnel_refuses(world, monkeypatch):
    """What the person reads when the start cannot run: the ledger's denial, no
    account, the machine dropped or offline (their computer, the agent's, one
    deleted), a start that failed."""
    from core.config import config_builder as cb
    slug = make_test_agent("warm-spawn-refused")
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: world.layer)
    _machine("m-admin", "Rack-1", admin=True)
    _machine("m-user", "Office-PC", admin=False)
    real_start = world.layer.start_session
    real_acquire = concurrency.acquire_chat_slot
    real_env = cb.subscription_pool.resolve_subscription_env
    frames = {}

    async def _denied(sid, **kwargs):
        return concurrency.Admission(False, "busy", "The server is busy.")

    def _no_account(*a, **k):
        raise NoSubscriptionError("no_pool")

    def _start_raising(text):
        async def _start(sid, cfg):
            raise RuntimeError(text)
        return _start

    async def attempt(name, **patches):
        monkeypatch.setattr(concurrency, "acquire_chat_slot", patches.get("acquire", real_acquire))
        monkeypatch.setattr(cb.subscription_pool, "resolve_subscription_env", patches.get("env", real_env))
        world.layer.start_session = patches.get("start", real_start)
        msg = {"agent": slug}
        if "pinned" in patches:     # a chat that holds to its machine, none of them connected
            dead = str(uuid.uuid4())
            world.layer.resumable.add(dead)
            msg["chat_id"] = world.chat(slug, session_id=dead, execution_target=patches["pinned"])
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            await _spawned_warmup(ws, world.layer, msg, end="warmup_failed")
            ws.client_send({"type": "close"})
        frames[name] = _frames_of(ws)

    async def scenario():
        await attempt("ledger_denied", acquire=_denied)
        await attempt("no_account", env=_no_account)
        await attempt("start_failed", start=_start_raising("the CLI did not start"))
        await attempt("machine_dropped", start=_start_raising("Satellite 790546a4 not connected"))
        await attempt("agents_machine_offline", pinned="m-admin")
        await attempt("own_computer_offline", pinned="m-user")
        await attempt("machine_deleted", pinned="m-gone")
    run_ws_scenario(scenario, timeout=30.0)
    world.sockets.append([])
    check_golden("warmup/spawn-refused", world.seen({"attempts": frames}))


# ---------------------------------------------------------------------------
# A deferred mode or model re-applied after a warmup.
# ---------------------------------------------------------------------------

def _two_models(monkeypatch) -> None:
    monkeypatch.setattr(subscription_store, "list_models", lambda layer=None: [
        {"model_id": TEST_MODEL, "enabled": True}, {"model_id": "other-model", "enabled": True}])


def test_picks_made_before_the_warmup_are_consumed_by_the_mint(world, monkeypatch):
    _two_models(monkeypatch)
    slug = make_test_agent("warm-picks-before")

    async def drive(ws):
        ws.client_send({"type": "mode_change", "mode": "dontAsk"})
        ws.client_send({"type": "model_change", "model": "other-model"})
        await _until(ws, "model_changed")
        minted = await _spawned_warmup(ws, world.layer, {"agent": slug})
        world.chats.append(minted["chat_id"])
        await _barrier(ws)
        await chat_writer.drain(minted["chat_id"], timeout=5)
    world.run(drive)
    check_golden("warmup/deferred-before", world.seen())


@pytest.mark.parametrize("naming_the_chat", [False, True], ids=["new-chat-page", "the-warming-chat"])
def test_picks_made_during_the_spawn_are_applied_after_it(world, monkeypatch, naming_the_chat):
    """A pick while the backgrounded spawn runs: echoed at once, then re-applied
    on the new session (``_reapply_deferred_after_warmup``) after warmup_ready."""
    _two_models(monkeypatch)
    slug = make_test_agent(f"warm-picks-{'chat' if naming_the_chat else 'page'}")

    async def drive(ws):
        world.layer.start_gate = asyncio.Event()
        ws.client_send({"type": "warmup", "agent": slug})
        cid = (await _until(ws, "warmup_started"))["chat_id"]
        world.chats.append(cid)
        await _until(ws, "notification_count")
        named = {"chat_id": cid} if naming_the_chat else {}
        ws.client_send({"type": "mode_change", "mode": "plan", **named})
        await _until(ws, "mode_changed")
        ws.client_send({"type": "model_change", "model": "other-model", **named})
        await _until(ws, "model_changed")
        world.layer.start_gate.set()
        await _until(ws, "warmup_ready")
        await _until(ws, "mode_changed")
        await _until(ws, "model_changed")
        await _barrier(ws)
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    name = "chat" if naming_the_chat else "page"
    check_golden(f"warmup/deferred-during-{name}", world.seen())


def test_a_deferred_pick_on_the_reapply_edges(world):
    """The re-apply's other answers: a foreign model dropped without a word, an
    engine that refuses the change, a session that died meanwhile, another
    chat's pick left waiting, a chat's pick whose row holds another mode since
    (a terminal's Shift+Tab lowered it: dropped). A chat's pick wrote its row
    when it was made (``row``)."""
    slug = make_test_agent("warm-reapply-edges")
    world.layer.alive.update({"sid-edge", "sid-refused"})
    cid = world.chat(slug, session_id="sid-edge", mode="ask")
    refused = world.chat(slug, session_id="sid-refused", mode="ask")
    out = {}

    async def _refuse(sid, value):
        raise RuntimeError("control channel closed")

    async def scenario():
        conn = _connection(agent=slug)
        sent = []

        async def _send(frame):
            sent.append(frame)
        conn._send = _send
        conn.layer = world.layer

        async def reapply(name, chat_id, session_id, mode="", model="", deferred_for="", row="", **kw):
            sent.clear()
            if row:
                task_store.update_chat(chat_id, permission_mode=row)
            conn.chat_id, conn.session_id = chat_id, session_id
            conn.deferred_mode, conn.deferred_model, conn.deferred_for = mode, model, deferred_for
            await conn._reapply_deferred_after_warmup(kw.pop("for_chat", chat_id), **kw)
            await chat_writer.drain(chat_id, timeout=5)
            out[name] = {"sent": list(sent), "left": [conn.deferred_mode, conn.deferred_model, conn.deferred_for],
                         "row": {k: _chat_of(chat_id)[k] for k in ("permission_mode", "model")}}

        await reapply("foreign_model_dropped", cid, "sid-edge", model="foreign-model", from_warmup=True)
        await reapply("own_chat_pick", cid, "sid-edge", mode="plan", deferred_for=cid, row="plan")
        await reapply("own_chat_pick_the_row_moved_since", cid, "sid-edge", mode="dontAsk",
                      deferred_for=cid, row="ask")
        await reapply("new_chat_pick_on_a_turn_start", cid, "sid-edge", mode="dontAsk")
        await reapply("new_chat_pick_on_a_warmup", cid, "sid-edge", mode="dontAsk", from_warmup=True)
        await reapply("another_chats_pick", cid, "sid-edge", mode="ask", deferred_for="another-chat",
                      from_warmup=True)
        await reapply("not_the_bound_chat", cid, "sid-edge", mode="ask", deferred_for=cid, for_chat="another-chat")
        await reapply("session_dead", cid, "sid-gone", mode="ask", deferred_for=cid)
        world.layer.change_mode = _refuse
        world.layer.change_model = _refuse
        await reapply("engine_refuses", refused, "sid-refused", mode="plan", model=TEST_MODEL,
                      deferred_for=refused, row="plan")
    run_ws_scenario(scenario)
    world.sockets.append([])
    check_golden("warmup/deferred-edges", world.seen({"reapplied": out}))


# ---------------------------------------------------------------------------
# A pre-warm.
# ---------------------------------------------------------------------------

def test_a_prewarm_spawns_is_reused_and_replaced(world, monkeypatch):
    """The eager pre-warm, and the chats that take a pre-warmed session: the
    one this page made (its row's mode reaches it), a detached one claimed by
    its key."""
    _two_models(monkeypatch)
    slug = make_test_agent("warm-prewarm")

    async def pre_warm(ws, **msg):
        ws.client_send({"type": "pre_warmup", **msg})
        await _barrier(ws)
        await _finished("_handle_pre_warmup")

    async def drive(ws):
        await pre_warm(ws)
        await pre_warm(ws, agent="no-such-agent")
        await pre_warm(ws, agent=slug, permission_mode="ask")
        await pre_warm(ws, agent=slug, permission_mode="ask")       # alive and matching: the same one
        await pre_warm(ws, agent=slug, model="other-model")         # another model: closed and replaced
        # The chat that takes it: the row's mode reaches the reused session.
        minted = await _spawned_warmup(ws, world.layer, {
            "agent": slug, "model": "other-model", "permission_mode": "dontAsk"})
        world.chats.append(minted["chat_id"])
        await chat_writer.drain(minted["chat_id"], timeout=5)
        # A pre-warm no socket remembers, claimed by its key; a dead one is closed.
        for detached, alive in (("sid-detached-dead", False), ("sid-detached", True)):
            if alive:
                world.layer.alive.add(detached)
            # A pre-warm runs on the compute machine, as its record says.
            session_state._session_security[detached] = SimpleNamespace(
                placement=placement.from_machine(placement.KIND_COMPUTE, HARNESS_COMPUTE))
            await prewarm_session_registry.register(
                detached, agent=slug, role="admin", user_sub="user-admin",
                exec_path="claude-code-cli", model="other-model")
            minted = await _spawned_warmup(ws, world.layer, {
                "agent": slug, "model": "other-model", "permission_mode": "plan"})
            world.chats.append(minted["chat_id"])
            await chat_writer.drain(minted["chat_id"], timeout=5)
    world.run(drive)
    check_golden("warmup/prewarm", world.seen())


def test_a_prewarm_that_is_skipped_or_fails(world, monkeypatch):
    slug = make_test_agent("warm-prewarm-skip")
    terminal = make_test_agent("warm-prewarm-term")
    agent_store.update_agent(terminal, default_execution_mode="interactive")
    real_start = world.layer.start_session
    real_acquire = concurrency.acquire_chat_slot
    frames = {}

    async def attempt(name, msg):
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            ws.client_send({"type": "pre_warmup", **msg})
            await _barrier(ws)
            await _finished("_handle_pre_warmup")
            ws.client_send({"type": "close"})
        frames[name] = _frames_of(ws)

    async def _speculative(sid, **kwargs):
        return concurrency.Admission(False, "speculative")

    async def _denied(sid, **kwargs):
        return concurrency.Admission(False, "busy", "The server is busy.")

    async def _start_fails(sid, cfg):
        raise RuntimeError("the CLI did not start")

    async def scenario():
        monkeypatch.setattr(concurrency, "prewarm_allowed", lambda *a, **kw: False)
        await attempt("no_room_for_two", {"agent": slug})
        monkeypatch.setattr(concurrency, "prewarm_allowed", lambda *a, **kw: True)
        monkeypatch.setattr(config, "get_model_layers", lambda model: ["codex-cli"])
        await attempt("model_of_another_engine", {"agent": slug, "model": "other-engine-model"})
        monkeypatch.setattr(config, "get_model_layers", lambda model: [])
        await attempt("interactive_agent", {"agent": terminal})
        monkeypatch.setattr(concurrency, "acquire_chat_slot", _speculative)
        await attempt("speculative_refused", {"agent": slug})
        monkeypatch.setattr(concurrency, "acquire_chat_slot", _denied)
        await attempt("ledger_denied", {"agent": slug})
        monkeypatch.setattr(concurrency, "acquire_chat_slot", real_acquire)
        world.layer.start_session = _start_fails
        await attempt("start_failed", {"agent": slug})
        world.layer.start_session = real_start
    run_ws_scenario(scenario, timeout=30.0)
    world.sockets.append([])
    check_golden("warmup/prewarm-skipped", world.seen({"attempts": frames}))


# ---------------------------------------------------------------------------
# A terminal spawn and a pinned chat's mismatch fields.
# ---------------------------------------------------------------------------

class _CodexLayer(FakeExecutionLayer):
    """The fake layer with the Codex descriptor: its TUI takes the prompt as a
    launch argument."""

    def __init__(self):
        super().__init__()
        from core.layers.codex.layer import _CODEX_CAPABILITIES
        self.capabilities = _CODEX_CAPABILITIES


def _registers_terminals(world, monkeypatch) -> list:
    """The layer's start registers a live terminal, as the real interactive start does."""
    terms: list = []
    real_start = world.layer.start_session

    async def _start(sid, cfg):
        await real_start(sid, cfg)
        if cfg.interactive:
            term = FakeInteractiveSession(sid, cfg.chat_id, tui_theme=cfg.interactive_theme)
            terms.append(term)
            monkeypatch.setitem(interactive_session._sessions, sid, term)
    world.layer.start_session = _start
    return terms


def test_a_warmup_spawns_a_terminal(world, monkeypatch):
    """The chat's execution mode reaches the funnel; the page's theme is baked
    and remembered; the cold text is submitted to the terminal, not kicked."""
    slug = make_test_agent("warm-spawn-term")
    terms = _registers_terminals(world, monkeypatch)

    baked = world.chat(slug, session_id=str(uuid.uuid4()), tui_theme="light", execution_mode="interactive")

    async def drive(ws):
        minted = await _spawned_warmup(ws, world.layer, {
            "agent": slug, "execution_mode": "interactive", "theme": "light", "text": "open it"})
        world.chats.append(minted["chat_id"])
        await chat_writer.drain(minted["chat_id"], timeout=5)
        # A re-warm with no page's theme: the chat's baked one, not the dark default.
        await _spawned_warmup(ws, world.layer, {"agent": slug, "chat_id": baked})
        await chat_writer.drain(baked, timeout=5)
    world.run(drive)
    submitted = [t.submitted for t in terms]
    check_golden("warmup/spawn-terminal", world.seen({"submitted": submitted}))


def test_a_codex_terminal_takes_the_cold_prompt_as_its_launch_argument(temp_db, monkeypatch):
    world = _World(monkeypatch, _CodexLayer())
    _engines.hold(monkeypatch, "codex-cli", world.layer)
    slug = make_test_agent("warm-codex-term")
    terms = _registers_terminals(world, monkeypatch)

    async def drive(ws):
        minted = await _spawned_warmup(ws, world.layer, {
            "agent": slug, "execution_path": "codex-cli", "execution_mode": "interactive", "text": "open it"})
        world.chats.append(minted["chat_id"])
        await _barrier(ws)
        await chat_writer.drain(minted["chat_id"], timeout=5)
    world.run(drive)
    check_golden("warmup/spawn-codex-terminal", world.seen({"submitted": [t.submitted for t in terms]}))


def test_a_pinned_chat_warmup_ready_names_the_mismatch(world, monkeypatch):
    slug = make_test_agent("warm-pinned")
    _machine("m-office", "Office-PC", admin=False)
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: world.layer)
    monkeypatch.setattr(remote_status, "is_reachable", lambda machine_id: True)
    dead = str(uuid.uuid4())
    world.layer.resumable.add(dead)
    cid = world.chat(slug, session_id=dead, execution_target="m-office")

    async def drive(ws):
        await _spawned_warmup(ws, world.layer, {"agent": slug, "chat_id": cid})
        await chat_writer.drain(cid, timeout=5)
    world.run(drive)
    check_golden("warmup/pinned-mismatch", world.seen())
