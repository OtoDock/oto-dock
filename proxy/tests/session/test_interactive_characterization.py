"""Characterization (golden-master) suite for the terminal session:
``core/session/interactive_session.py``, its mixins (viewers and fan-out; input, submit and cold flush; the prompt queue and
inject; turn signals, tails, title and turn complete) and the module registry
with its idle reaper.

A session runs on a fake PTY: what the PTY received, what its viewers got, the
statuses and notices, the ledger's traffic and the registry's answers are
recorded step by step and compared with a golden. The boundaries are faked:
the PTY (the world's own, handed to the registration through
``tests/_terminal.register``, and to a machine's start on ``remote_pty``), the
notification and title services on their modules, the transcript tailer on its
module, the satellite's connection manager. The session's clock is the session
itself: its timers go through ``session._loop``, which the fake spawn replaces
with one the test fires by hand, and its idle and quiet ages are set through
``last_activity`` / ``created_at``. No global of the module under test is
patched and none of its locks is reset.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest

import config
from auth.session_token import create_session_token, validate_session_token
from core import concurrency
from core.background import running
from core.remote import remote_pty, satellite_connection
from core.session import (
    interactive_session as isess,
    session_delivery,
    session_events,
    session_state,
    transcript_tailer,
)
from core.session.session_delivery import DeliveryOutcome
from services import title_generator
from services.delegation import lane_status
from services.knowledge import library_projector
from services.notifications import notification_manager
from storage import database as task_store
from tests._terminal import register
from tests.fixtures.characterization.golden_master import check_golden
from tests.fixtures.ws_dashboard_harness import make_test_agent, run_ws_scenario

_LONG_AGO = 3600
_YEARS = 10 * 365 * 24 * 3600


def _text(data) -> str:
    return bytes(data).decode("utf-8", "backslashreplace")


class _Timer:
    def __init__(self, delay, fn, args):
        self.delay, self.fn, self.args = delay, fn, args
        self.cancelled = self.fired = False

    @property
    def name(self) -> str:
        return getattr(self.fn, "__name__", repr(self.fn))

    def cancel(self) -> None:
        self.cancelled = True


class _SessionLoop:
    """The session's ``_loop``: its timers are recorded and fired by the test,
    everything else is the running loop's."""

    def __init__(self, world, real):
        self._world, self._real = world, real
        self.timers: list[_Timer] = []

    def call_later(self, delay, fn, *args) -> _Timer:
        timer = _Timer(delay, fn, args)
        self.timers.append(timer)
        # The readiness settle's delay is floored by the time since the spawn.
        self._world.note("timers", [timer.name, None if timer.name == "_mark_ready" else delay])
        return timer

    def create_task(self, coro, **kw):
        task = self._real.create_task(coro, **kw)
        if coro.__name__ != "_drain_loop":
            self._world.background.append(task)
        return task

    def run_in_executor(self, executor, fn, *args):
        future = self._real.run_in_executor(executor, fn, *args)
        self._world.background.append(future)
        return future

    def __getattr__(self, name):
        return getattr(self._real, name)

    def pending(self) -> list[str]:
        return sorted(t.name for t in self.timers if not t.cancelled and not t.fired)


class _Pty:
    """The fake PTY: what it received, and the output and exit it feeds back."""

    pid = 4242

    def __init__(self, world, on_output, on_exit):
        self._world, self.on_output, self.on_exit = world, on_output, on_exit
        self.closed = False
        self.ring = b""

    def write(self, data) -> None:
        self._world.note("pty", _text(data))

    def resize(self, rows, cols) -> None:
        self._world.note("pty_resize", [rows, cols])

    def scrollback(self) -> bytes:
        return self.ring

    def close(self, *, signal_child=True) -> None:
        self.closed = True
        self._world.note("pty_close", {"signal_child": signal_child})


class _ConnectionManager:
    """The satellite side: what is asked and sent."""

    def __init__(self, world):
        self._world = world
        self.os = "linux"
        self.in_grace: set[str] = set()

    def satellite_os(self, machine_id):
        return self.os

    def is_pty_in_grace(self, machine_id):
        return machine_id in self.in_grace

    async def send_fire_and_forget(self, machine_id, msg):
        self._world.last_inject_id = msg["inject_id"]
        self._world.note("satellite", {**msg, "inject_id": "<inject>", "machine": machine_id})


class _World:
    """One test's boundaries and the steps it recorded."""

    def __init__(self, monkeypatch):
        self.fresh: dict[str, list] = defaultdict(list)
        self.steps: list[dict] = []
        self.background: list = []
        self.loops: dict[str, _SessionLoop] = {}
        self.ptys: dict[str, _Pty] = {}
        self.bg_pending: set[str] = set()
        self.admission = concurrency.Admission(True)
        self.spawn_error: Exception | None = None
        self.outcome = DeliveryOutcome("pump", chat_id="chat-handed-back")
        self.manager = _ConnectionManager(self)
        self.last_inject_id = ""

        monkeypatch.setattr(remote_pty, "spawn_remote_pty", self._spawn_remote_pty)
        monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: self.manager)
        monkeypatch.setattr(concurrency, "acquire_chat_slot", self._acquire)
        monkeypatch.setattr(concurrency, "release_chat_slot", lambda sid: self.note("slot_released", sid))
        for name in ("broadcast_chat_status", "broadcast_chat_rows", "broadcast_chat_read"):
            monkeypatch.setattr(notification_manager, name, self._recorder(name))
        monkeypatch.setattr(notification_manager, "fire_ephemeral", self._ephemeral)
        monkeypatch.setattr(title_generator, "request_chat_title", self._title)
        monkeypatch.setattr(session_events, "turn_end", self._turn_end)
        monkeypatch.setattr(library_projector, "schedule_reconcile_for_agent",
                            lambda agent: self.note("library_reconcile", agent))
        monkeypatch.setattr(session_delivery, "deliver_prompt", self._deliver)
        monkeypatch.setattr(session_state, "get_subagent_registry",
                            lambda sid: SimpleNamespace(has_pending=sid in self.bg_pending))

    # -- the boundaries ------------------------------------------------------
    def note(self, channel: str, value) -> None:
        self.fresh[channel].append(value)

    def _recorder(self, name):
        def _record(owner, chat_id, *args, **kwargs):
            self.note(name, [owner, chat_id, *args, kwargs])
        return _record

    def _adopt(self, on_output, on_exit) -> _Pty:
        session = on_output.__self__
        loop = self.loops[session.session_id] = _SessionLoop(self, session._loop)
        session._loop = loop
        pty = self.ptys[session.session_id] = _Pty(self, on_output, on_exit)
        return pty

    def _spawn_pty(self, *, rows, cols, on_output, on_exit, scrollback_limit):
        if self.spawn_error:
            raise self.spawn_error
        self.note("spawn", {"rows": rows, "cols": cols, "scrollback_limit": scrollback_limit})
        return self._adopt(on_output, on_exit)

    async def _spawn_remote_pty(self, *, on_output, on_exit, config_payload, **kw):
        self.note("spawn_remote", {**kw, "config_payload": sorted(config_payload)})
        return self._adopt(on_output, on_exit)

    async def _acquire(self, sid, **kwargs):
        self.note("slot_acquired", [sid, kwargs])
        return self.admission

    async def _ephemeral(self, user_sub, *, title, body, chat_id=None, **kw):
        self.note("ephemeral", {"user_sub": user_sub, "title": title, "body": body, "chat_id": chat_id, **kw})

    async def _title(self, chat_id, assistant_excerpt=""):
        self.note("title", [chat_id, assistant_excerpt])

    async def _turn_end(self, session_id, **kw):
        self.note("turn_end", [session_id, kw])

    async def _deliver(self, chat_id, text, **kw):
        self.note("handed_back", {"chat_id": chat_id, "text": text, **{
            k: (v.__name__ if callable(v) else v) for k, v in kw.items()}})
        return self.outcome

    # -- driving -------------------------------------------------------------
    async def register(self, sid, chat_id="", agent="agent", *, user_sub="user-admin", **kw) -> isess.InteractiveSession:
        return await register(session_id=sid, chat_id=chat_id, agent_name=agent, user_sub=user_sub,
                              role="manager", username="admin", pty=self._spawn_pty, **kw)

    async def settle(self) -> None:
        """What the sessions spawned has run to its end (drains, tails, notices,
        the turn-end stamp)."""
        loop = asyncio.get_running_loop()

        def _pending() -> list:
            spawned = [t for t in running("interactive_session") if t.get_loop() is loop]
            return spawned + [b for b in self.background if not b.done()]

        while True:
            pending = _pending()
            if pending:
                await asyncio.wait(pending, timeout=5)
                continue
            await asyncio.sleep(0)
            if not _pending():
                return

    async def fire(self, session, name: str):
        """A timer of the session comes due, the oldest of that name."""
        loop = self.loops[session.session_id]
        timer = next(t for t in loop.timers if t.name == name and not t.cancelled and not t.fired)
        timer.fired = True
        timer.fn(*timer.args)
        await self.settle()

    def viewer(self, session, name="viewer", *, driving=True) -> bytes:
        """Attaches a viewer that records what it gets; returns its replay."""
        kwargs = {"on_close": lambda s, reason: self.note(f"{name}_closed", [s.session_id, reason])}
        if driving:
            kwargs["on_evict"] = lambda reason: self.note(f"{name}_evicted", reason)
            session.on_status = lambda state: self.note(f"{name}_status", state)
            session.on_perm_event = lambda item: self.note(f"{name}_perm", _item(item))
            session.on_close = lambda s, reason: self.note(f"{name}_on_close", [s.session_id, reason])
        return session.add_output_listener(lambda data: self.note(f"{name}_output", _text(data)), **kwargs)

    def step(self, name: str, *sessions, **answers) -> None:
        entry: dict = {"step": name, **{k: v for k, v in self.fresh.items() if v}, **answers}
        if sessions:
            entry["state"] = {s.session_id: _state_of(s, self.loops.get(s.session_id)) for s in sessions}
        self.fresh = defaultdict(list)
        self.steps.append(entry)

    def run(self, scenario) -> None:
        async def _main():
            try:
                await scenario()
            finally:
                await isess.close_all(reason="test-end")
                await self.settle()
        run_ws_scenario(_main, timeout=60.0)


def _item(item: dict) -> dict:
    return {k: ("<row>" if k == "db_message_id" else v) for k, v in item.items()}


def _state_of(s, loop) -> dict:
    return {
        "alive": s.alive, "closing": s.closing, "ready": s._ready, "turn_open": s.turn_open,
        "question_parked": s.question_parked, "composer_dirty": s._composer_dirty,
        "submitted_once": s._submitted_once, "has_viewer": s.has_viewer, "had_viewer": s.had_viewer,
        "otodock_attached": s.otodock_attached, "exit_code": s.exit_code,
        "queue": [q["text"] for q in s._prompt_queue], "held": s._last_held_reason,
        "input_buffer": [_text(b) for b in s._input_buffer],
        "cold_flush": {"pending": _text(s._cold_flush_pending), "attempts": s._cold_flush_attempts},
        "title_armed": s._title_armed, "timers": loop.pending() if loop else [],
    }


def _idle(s) -> None:
    """Ready, past its youth and past the quiet window."""
    s._mark_ready()
    s.created_at = time.monotonic() - _LONG_AGO
    s.last_activity = time.monotonic() - _LONG_AGO
    s._cancel_deferred_submit()


def _registry(chat_id: str, *session_ids) -> dict:
    found = isess.find_live_for_chat(chat_id)
    return {
        "get": {sid: (isess.get(sid).session_id if isess.get(sid) else None) for sid in session_ids},
        "find_live_for_chat": found.session_id if found else None,
        "live_session_ids": sorted(isess.live_session_ids()),
        "live_local": sorted(isess.live_session_ids(local_only=True)),
        "streaming_chat_ids": sorted({cid for sid, cid in lane_status.working_sessions().items() if isess.get(sid)}),
    }


def _rows(chat_id: str) -> list:
    return [[m["role"], m["content"], m.get("event_type"), m.get("event_data")]
            for m in task_store.get_chat_messages(chat_id)]


@pytest.fixture
def world(temp_db, monkeypatch):
    """The registry starts empty for this test's questions and is given back."""
    saved = dict(isess._sessions)
    isess._sessions.clear()
    yield _World(monkeypatch)
    isess._sessions.clear()
    isess._sessions.update(saved)


def _chat(temp_db, cid, slug, *, user_sub="user-admin") -> str:
    temp_db.create_chat(cid, user_sub, slug, "default", model="m", execution_path="claude-code-cli")
    return cid


# ---------------------------------------------------------------------------
# A terminal from its registration to its close.
# ---------------------------------------------------------------------------

def test_a_terminal_from_registration_to_close(world, temp_db):
    slug = make_test_agent("term-life")
    cid = _chat(temp_db, "chat-life", slug)
    sid = "sid-life"

    async def scenario():
        s = await world.register(sid, cid, slug)
        pty = world.ptys[sid]
        world.step("registered", s, registry=_registry(cid, sid, "sid-nobody"))

        pty.ring = b"earlier screen\x1b[6n\x1b]52;c;Y2xpcA==\x07 end"
        replay = world.viewer(s)
        world.step("a viewer attaches", s, replay=_text(replay))

        assert s.submit_prompt("first prompt") is True
        world.step("the cold prompt waits for readiness", s)

        pty.on_output(b"pre-TUI warning, no escape yet")
        world.step("plain output arms no settle", s)
        pty.on_output(b"\x1b[2Jwelcome\x1b[6n")
        world.step("the first escape arms the settle", s)
        await world.fire(s, "_mark_ready")
        world.step("ready: the body is flushed, its Enter waits", s)

        pty.on_output(b"first prompt")
        world.step("the echo pushes the Enter out", s)
        await world.fire(s, "_fire_submit")
        world.step("the Enter goes", s)

        s._apply_turn_signal("user")
        await world.settle()
        world.step("a turn signal opens the turn", s, registry=_registry(cid, sid))

        s.last_activity = time.monotonic() - _LONG_AGO
        pty.on_output(b"working")
        world.step("output in the open turn is activity", s, idle_still_old=s.idle_seconds > _LONG_AGO / 2)
        answered = s.interrupt_turn()
        world.step("Stop presses ESC in an open turn", s, answered=answered)

        s._apply_turn_signal("end_turn")
        await world.settle()
        world.step("a turn signal closes the turn", s, registry=_registry(cid, sid),
                   stamped=bool(task_store.get_chat(cid)["last_response_at"]))
        answered = s.interrupt_turn()
        world.step("Stop on a closed turn presses nothing", s, answered=answered)

        s._apply_turn_signal("end_turn")
        await world.settle()
        world.step("a second close with no open still runs the close effects", s)

        await world.fire(s, "_run_post_output_tail")
        world.step("the debounced tail reads no transcript", s)

        s.last_activity = time.monotonic() - _LONG_AGO
        pty.on_output(b"a redraw")
        world.step("a redraw between turns breaks the quiet, not the idle age", s,
                   idle_still_old=s.idle_seconds > _LONG_AGO / 2, quiet_still_old=s.quiet_seconds > _LONG_AGO / 2)

        assert s.submit_prompt("second prompt") is True
        world.step("a warm line submit: the text", s)
        await world.fire(s, "_warm_enter")
        world.step("a warm line submit: its Enter", s)

        assert s.submit_prompt("line one\r\nline two") is True
        await world.fire(s, "_warm_enter")
        world.step("a multi-line submit is a bracketed paste", s)

        answers = {
            "owner_types": s.deliver_dashboard_input(b"hi", sender_sub="user-admin"),
            "another_person_types": s.deliver_dashboard_input(b"no", sender_sub="user-viewer"),
            "a_server_input_has_no_sender": s.deliver_dashboard_input(b"!", sender_sub=""),
            "may_drive": [s.may_drive("user-admin"), s.may_drive("user-viewer"), s.may_drive("")],
            "sole_driver": s.sole_driver,
        }
        world.step("the identity gate", s, **answers)
        s.write_input(b"\x03")
        world.step("Ctrl-C clears the composer flag", s)
        replies = s.write_input(b"\x1b[?1;2c")
        mouse = s.write_input(b"\x1b[<35;70;68M")
        world.step("terminal replies are dropped, a mouse report is no typing", s,
                   replies=replies, mouse=mouse)

        s.resize(30, 100)
        world.step("a resize", s, size=[s.rows, s.cols])

        replay = world.viewer(s, "second")
        world.step("a second driving viewer evicts the first", s, replay=_text(replay))
        mirror = world.viewer(s, "mirror", driving=False)
        pty.on_output(b"to both")
        world.step("a read-only mirror attaches", s, replay=_text(mirror))
        s.notify_status("reconnecting")
        await world.settle()
        world.step("a transport status reaches the viewer", s)

        s.otodock_attached = True
        written = s.write_input(b"dropped")
        s.resize(1, 1)
        s.evict_viewer("superseded_otodock")
        await world.settle()
        world.step("a local terminal holds the input", s, written=written, size=[s.rows, s.cols])
        s.otodock_attached = False

        answered = await isess.close_session(sid, reason="closed")
        await world.settle()
        world.step("the close", s, answered=answered, registry=_registry(cid, sid))
        answered = await isess.close_session(sid)
        world.step("a second close finds nothing", answered=answered, written=s.write_input(b"x"),
                   queued=s.queue_prompt("late", "delegate_result"))

    world.run(scenario)
    check_golden("interactive/lifecycle", world.steps)


# ---------------------------------------------------------------------------
# The cold flush: a seed digest, the Enter, the confirm-and-retry.
# ---------------------------------------------------------------------------

def test_the_cold_flush_retries_and_leaves_its_breadcrumb(world, temp_db):
    slug = make_test_agent("term-cold")
    prompt = "please redeploy the internal install and verify the card"

    async def cold(sid, cid, *, seed=""):
        _chat(temp_db, cid, slug)
        s = await world.register(sid, cid, slug)
        if seed:
            s.set_pending_seed(seed)
        s.write_input(prompt.encode() + b"\r")
        await world.fire(s, "_mark_ready")
        return s

    async def scenario():
        s = await cold("sid-cold-retry", "chat-cold-retry", seed="earlier\r\nhistory")
        world.step("the flush pastes the seed, then the body", s)
        await world.fire(s, "_fire_submit")
        world.step("the Enter arms the watcher", s)
        for attempt in (1, 2):
            await world.fire(s, "_check_cold_flush")
            world.step(f"unconfirmed: replay {attempt}", s)
            await world.fire(s, "_fire_submit")
        await world.fire(s, "_check_cold_flush")
        await world.settle()
        world.step("exhausted: the breadcrumb", s, rows=_rows("chat-cold-retry"))

        s = await cold("sid-cold-turn", "chat-cold-turn")
        await world.fire(s, "_fire_submit")
        s._apply_turn_signal("user")
        await world.settle()
        world.step("the transcript confirms: the watcher stands down", s)

        s = await cold("sid-cold-hook", "chat-cold-hook")
        await world.fire(s, "_fire_submit")
        session_state.record_hook_activity("sid-cold-hook")
        await world.fire(s, "_check_cold_flush")
        world.step("a hook after the Enter confirms it", s)

        s = await cold("sid-cold-typed", "chat-cold-typed")
        s.write_input(b"x")
        await world.settle()
        world.step("the person types over the pending Enter: the breadcrumb", s,
                   rows=_rows("chat-cold-typed"))

        _chat(temp_db, "chat-cold-short", slug)
        s = await world.register("sid-cold-short", "chat-cold-short", slug)
        s.write_input(b"ok\r")
        await world.fire(s, "_mark_ready")
        s.write_input(b"x")
        await world.settle()
        world.step("short residue leaves no row", s, rows=_rows("chat-cold-short"))

        _chat(temp_db, "chat-cold-argv", slug)
        s = await world.register("sid-cold-argv", "chat-cold-argv", slug, prompt_in_argv=True)
        s.write_input(b"\x1b[I")
        world.step("a prompt in the launch argument starts ready", s)

    world.run(scenario)
    check_golden("interactive/cold-flush", world.steps)


# ---------------------------------------------------------------------------
# The prompt queue and its drain.
# ---------------------------------------------------------------------------

def test_a_queued_prompt_waits_for_the_turn_and_drains(world, temp_db):
    slug = make_test_agent("term-queue")
    cid = _chat(temp_db, "chat-queue", slug)
    sid = "sid-queue"

    async def scenario():
        s = await world.register(sid, cid, slug)
        world.viewer(s)
        _idle(s)
        s._apply_turn_signal("user")
        await world.settle()
        world.step("a turn is open", s)

        injected = []
        first = s.queue_prompt("first result", "delegate_result", chat_id=cid, agent=slug,
                               on_injected=lambda: injected.append("first"))
        second = s.queue_prompt("second result", "delegate_result", chat_id=cid, note=True)
        third = s.queue_prompt("third result", "delegate_result", chat_id=cid)
        await world.settle()
        world.step("queued while busy", s, item_keys=sorted(first), injected=list(injected))

        answers = [s.cancel_prompt(third), s.cancel_prompt(third)]
        world.step("a queued prompt is cancelled", s, answers=answers)

        s.last_activity = time.monotonic() - _LONG_AGO
        s._apply_turn_signal("end_turn")
        s.last_activity = time.monotonic() - _LONG_AGO
        await world.settle()
        world.step("the turn closes: a drain is kicked", s)
        await world.fire(s, "<lambda>")
        world.step("the drain injects the head: the body, the turn opens", s, injected=list(injected))
        await world.fire(s, "_fire_submit")
        world.step("its Enter", s)

        s.last_activity = time.monotonic() - _LONG_AGO
        await s._try_drain_prompt_queue()
        world.step("one item per turn: the next is held", s)

        s._apply_turn_signal("end_turn")
        s.last_activity = time.monotonic() - _LONG_AGO
        await world.settle()
        await world.fire(s, "<lambda>")
        await world.fire(s, "_fire_submit")
        world.step("the second turn's close drains the second", s, second_item=sorted(second))

    world.run(scenario)
    check_golden("interactive/prompt-queue", world.steps)


def test_each_gate_holds_a_queued_prompt(world, temp_db, monkeypatch):
    """The injection gates one by one, in what the drain reads."""
    slug = make_test_agent("term-gates")
    cid = _chat(temp_db, "chat-gates", slug)
    sid = "sid-gates"
    held = {}

    async def scenario():
        s = await world.register(sid, cid, slug)
        s.queue_prompt("not yet", "delegate_result", chat_id=cid)
        await world.settle()
        held["not_ready"] = s._last_held_reason

        _idle(s)
        now = time.monotonic

        async def gate(name, on, off):
            on()
            s.last_activity = now() - _LONG_AGO if name != "not_quiet" else now() + _YEARS
            await s._try_drain_prompt_queue()
            held[name] = s._last_held_reason
            off()
            s._last_held_reason = None

        def _open():
            s._turn_open = True

        def _close():
            s._turn_open = False

        def _dirty():
            s._composer_dirty, s._composer_dirty_at = True, now()

        def _clean():
            s._composer_dirty = False

        def _permission():
            session_state._session_permission_requests[sid] = {"req-1"}

        def _no_permission():
            session_state._session_permission_requests.pop(sid, None)

        def _parked():
            s._question_parked = True

        def _unparked():
            s._question_parked = False

        def _young():
            s.created_at = now() + _YEARS

        def _old():
            s.created_at = now() - _LONG_AGO

        def _pending():
            s._submit_settle_handle = s._loop.call_later(60, lambda: None)

        def _nothing():
            pass

        await gate("cold_submit_pending", _pending, s._cancel_deferred_submit)
        await gate("turn_open", _open, _close)
        await gate("composer_dirty", _dirty, _clean)
        await gate("not_quiet", _nothing, _nothing)
        await gate("too_young", _young, _old)
        await gate("bg_pending", lambda: world.bg_pending.add(sid), world.bg_pending.clear)
        await gate("permission_pending", _permission, _no_permission)
        await gate("question_parked", _parked, _unparked)
        _dirty()
        s._composer_dirty_at = now() - 1000
        await gate("a_stale_draft_expires_and_it_injects", _nothing, _clean)
        world.step("gates", s, held=held)

        # A steer into the open turn drops the turn gates, keeps the safety ones.
        s._cancel_deferred_submit()
        s._turn_open = True
        s.last_activity = now() + _YEARS
        s.queue_prompt("steer this", "delegate_result", steer=True)
        await world.settle()
        world.step("a steer goes into the open turn", s)
        s._cancel_deferred_submit()
        _dirty()
        s.queue_prompt("typed over", "delegate_result", steer=True)
        await world.settle()
        world.step("a dirty composer holds a steer too", s)
        s.cancel_prompt(s._prompt_queue[0])
        _clean()
        s._question_parked = False

        # A person's composer send while a question shows is held as a steer.
        s._apply_turn_signal("end_turn", question_pending=True)
        await world.settle()
        paste = b"\x1b[200~hello\nworld\x1b[201~\r"
        answered = s.deliver_dashboard_input(paste, composer=True, sender_sub="user-admin")
        await world.settle()
        world.step("a composer send held for the dialog", s, answered=answered)
        s.last_activity = now() + _YEARS
        s._apply_turn_signal("tool_use")
        await world.settle()
        world.step("the answer reopens the turn and types it", s)
        await world.fire(s, "_warm_enter")
        world.step("its warm Enter", s)

        # A dialog that opens between a line's text and its Enter: no Enter into it.
        s.submit_prompt("text before the dialog")
        s._apply_turn_signal("end_turn", question_pending=True)
        await world.settle()
        await world.fire(s, "_warm_enter")
        world.step("the Enter is held for the dialog", s)
        s._apply_turn_signal("tool_use")
        await world.settle()
        world.step("the unpark presses it", s)

    world.run(scenario)
    check_golden("interactive/gates", world.steps)


def test_a_close_hands_the_queue_back(world, temp_db):
    slug = make_test_agent("term-handback")
    outcomes = []

    async def scenario():
        for sid, cid in (("sid-back", "chat-back"), ("sid-drop", "chat-drop"), ("sid-capped", "chat-capped")):
            _chat(temp_db, cid, slug)
            s = await world.register(sid, cid, slug)
            s._turn_open = True
        back, drop, capped = isess.get("sid-back"), isess.get("sid-drop"), isess.get("sid-capped")
        back.queue_prompt("undelivered result", "delegate_result", chat_id="chat-back", agent=slug,
                          role="editor", user_sub="user-admin", hops=0, on_outcome=lambda o: outcomes.append(o.path))
        drop.queue_prompt("dropped result", "delegate_result", chat_id="chat-drop", hops=0)
        capped.queue_prompt("looping result", "delegate_result", chat_id="chat-capped", hops=2)
        await world.settle()
        await back.close(reason="closed")
        await world.settle()
        world.step("a close hands it back with no PTY or socket rung", back, outcomes=list(outcomes))
        await isess.close_session("sid-drop", reason="offboarded", drop_queued=True)
        await world.settle()
        world.step("drop_queued hands nothing back", drop)
        await capped.close(reason="closed")
        await world.settle()
        world.step("past two handbacks it is dropped", capped)

    world.run(scenario)
    check_golden("interactive/handback", world.steps)


# ---------------------------------------------------------------------------
# Turn signals, tails, the title, the turn's end.
# ---------------------------------------------------------------------------

# The batch listener stays registered for the process (registration is
# idempotent and has no removal): it hears only while a test holds its list.
_heard: list = [None]


def _note_batch(chat_id, *, persisted, turn_open) -> None:
    if _heard[0] is not None:
        _heard[0].append([chat_id, persisted, turn_open])


def test_tails_fold_into_the_turn_end(world, temp_db, monkeypatch):
    slug = make_test_agent("term-tails")
    cid = _chat(temp_db, "chat-tails", slug)
    sid = "sid-tails"
    batches: list[dict] = []
    monkeypatch.setattr(transcript_tailer, "resolve_and_tail",
                        lambda session_id, chat_id: batches.pop(0) if batches else {"persisted": 0})
    listened: list = []

    async def tail(name, s, batch):
        batches.append(batch)
        await s._tail_and_maybe_complete()
        await world.settle()
        world.step(name, s)

    async def swept(name, s, batch):
        batches.append(batch)
        await isess.tail_live_sessions()
        await world.settle()
        world.step(name, s)

    async def scenario():
        s = await world.register(sid, cid, slug)
        _idle(s)
        _heard[0] = listened
        isess.add_batch_listener(_note_batch)
        waited: list[str] = []
        s.add_turn_end_waiter(waited.append)
        try:
            await tail("a batch with no signal", s, {"persisted": 0, "reason": "no_transcript"})
            await tail("the user line opens the turn", s, {"persisted": 1, "last_signal": "user"})
            batches.append({"persisted": 3, "last_signal": "tool_use", "tool_rows": 3})
            await world.fire(s, "_run_midturn_tail")
            world.step("rows while open: the mid-turn tail, armed again", s)
            await tail("the turn ends: title, notice, waiter", s, {
                "persisted": 2, "last_signal": "end_turn", "turn_complete": True, "last_message": "the answer"})
            world.step("the waiter fired once", s, waited=list(waited))

            await tail("a tail with nothing new", s, {"persisted": 0, "last_signal": "end_turn",
                                                      "turn_complete": True, "last_message": "again"})
            await swept("the sweep's backstop opens the next turn", s, {"persisted": 1, "last_signal": "user"})
            await swept("the sweep's backstop reports its end", s, {
                "persisted": 1, "last_signal": "end_turn", "turn_complete": True, "last_message": "swept"})
            s.on_turn_complete = lambda message: world.note("task_complete", message)
            await tail("a task's completion fires once", s, {
                "persisted": 1, "last_signal": "end_turn", "turn_complete": True, "last_message": "task done"})
            await tail("and no second time", s, {
                "persisted": 1, "last_signal": "end_turn", "turn_complete": True, "last_message": "task again"})
            s.on_turn_complete = None

            s.add_turn_end_waiter(waited.append)
            await tail("a batch that reopens keeps the waiter", s, {
                "persisted": 1, "last_signal": "user", "turn_complete": True, "last_message": "reply"})
            world.bg_pending.add(sid)
            await tail("a background subagent holds the end", s, {
                "persisted": 1, "last_signal": "end_turn", "turn_complete": True, "last_message": "held"})
            world.bg_pending.clear()
            await tail("the final turn's end", s, {
                "persisted": 1, "last_signal": "end_turn", "turn_complete": True, "last_message": "final"})
            world.step("the waiter's message", s, waited=list(waited))

            await tail("a question folds as parked", s, {
                "persisted": 1, "last_signal": "end_turn", "question_pending": True, "last_message": "?"})
            s.last_activity = time.monotonic() - _LONG_AGO
            answered = s.queue_prompt("while parked", "delegate_result", chat_id=cid)
            await world.settle()
            world.step("a prompt waits behind the dialog", s, queued=bool(answered))
            s.cancel_prompt(answered)
            await tail("the answer unparks", s, {"persisted": 1, "last_signal": "tool_use"})

            s.park_on_native_dialog("AskUserQuestion", sent_at=1000.0)
            await world.settle()
            world.step("a hook parks on the native dialog", s, parked_at=s._parked_at)
            await tail("a line journaled before the park cannot reopen", s, {
                "persisted": 1, "last_signal": "user", "last_signal_at": 999.0})
            await tail("the fold of the dialog the hook pinged is silent", s, {
                "persisted": 1, "last_signal": "end_turn", "question_pending": True, "last_message": "?"})
            await tail("a line after the park reopens", s, {
                "persisted": 1, "last_signal": "user", "last_signal_at": 1001.0})

            await tail("a manual compact closes the open turn", s, {
                "persisted": 1, "compacted": True, "compact_trigger": "manual"})
            await tail("a compact while the session is idle is the turn's end", s, {"persisted": 1, "compacted": True})
            await tail("the engine's compact turn: one ping, compacted", s, {
                "persisted": 1, "compacted": True, "turn_complete": True, "last_signal": "end_turn"})
            s._apply_turn_signal("user")
            await tail("an auto compact mid-turn is no end", s, {
                "persisted": 1, "compacted": True, "compact_trigger": "auto"})
            s._apply_turn_signal("end_turn")
            await world.settle()
            world.step("the listener's batches", s, listened=list(listened))
        finally:
            _heard[0] = None

    world.run(scenario)
    check_golden("interactive/turn-end", world.steps)


def test_the_title_fires_once_from_its_first_trigger(world, temp_db, monkeypatch):
    from services.title_generator import TITLE_CHAR_THRESHOLD, TITLE_TOOL_THRESHOLD
    slug = make_test_agent("term-title")
    batches: list[dict] = []
    monkeypatch.setattr(transcript_tailer, "resolve_and_tail",
                        lambda session_id, chat_id: batches.pop(0) if batches else {"persisted": 0})
    temp_db.create_chat("chat-titled", "user-admin", slug, "default", model="m")
    temp_db.update_chat("chat-titled", title_generated=True)
    for cid in ("chat-title-chars", "chat-title-tools", "chat-title-timer", "chat-title-young"):
        _chat(temp_db, cid, slug)

    async def tail(s, batch):
        batches.append(batch)
        await s._tail_and_maybe_complete()
        await world.settle()

    async def closed(s):
        await s.close(reason="closed")
        await world.settle()
        world.step("closed", s)

    async def scenario():
        s = await world.register("sid-title-chars", "chat-title-chars", slug)
        _idle(s)
        await tail(s, {"persisted": 1, "assistant_chars": TITLE_CHAR_THRESHOLD - 1})
        world.step("below the characters' threshold", s)
        await tail(s, {"persisted": 1, "assistant_chars": 1})
        world.step("the characters' threshold", s)
        await tail(s, {"persisted": 1, "assistant_chars": TITLE_CHAR_THRESHOLD})
        world.step("once", s)
        await closed(s)

        s = await world.register("sid-title-tools", "chat-title-tools", slug)
        _idle(s)
        await tail(s, {"persisted": 1, "tool_rows": TITLE_TOOL_THRESHOLD})
        world.step("the tools' threshold", s)
        await closed(s)

        s = await world.register("sid-title-timer", "chat-title-timer", slug)
        _idle(s)
        s._apply_turn_signal("user")
        await world.settle()
        await world.fire(s, "_fire_title_timer")
        world.step("the early timer of the first open", s)
        await closed(s)

        s = await world.register("sid-title-young", "chat-title-young", slug)
        s._mark_ready()
        s.last_activity = time.monotonic()
        s.created_at = time.monotonic() + _YEARS
        await tail(s, {"persisted": 1, "last_signal": "end_turn", "turn_complete": True, "last_message": "m"})
        world.step("a young terminal titles but gives no notice", s)
        await closed(s)

        s = await world.register("sid-titled", "chat-titled", slug)
        _idle(s)
        await tail(s, {"persisted": 1, "last_signal": "end_turn", "turn_complete": True, "last_message": "m",
                       "assistant_chars": TITLE_CHAR_THRESHOLD})
        world.step("a titled chat never asks again", s)

    world.run(scenario)
    check_golden("interactive/title", world.steps)


# ---------------------------------------------------------------------------
# The drainer: the permission queue to the viewer.
# ---------------------------------------------------------------------------

def test_the_drainer_forwards_and_persists(world, temp_db):
    from ws import wire_events as wire
    slug = make_test_agent("term-drain")
    cid = _chat(temp_db, "chat-drain", slug)
    sid = "sid-drain"

    async def drained(queue, channel, count):
        for _ in range(500):
            if queue.empty() and len(world.fresh[channel]) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"no {count} on {channel}: {dict(world.fresh)}")

    async def scenario():
        s = await world.register(sid, cid, slug)
        queue = session_state.get_permission_queue(sid)
        item = {"event_type": "permission_prompt", "request_id": "r-1", "tool_name": "Bash"}
        queue.put_nowait(dict(item))
        await asyncio.sleep(0)
        for _ in range(100):
            if queue.empty():
                break
            await asyncio.sleep(0.01)
        world.step("no viewer yet: the item is dropped", s, rows=_rows(cid))

        world.viewer(s)
        queue.put_nowait(dict(item))
        queue.put_nowait({"event_type": wire.ITEM_SETTINGS_CARD, "setting": "s.key", "label": "A setting",
                          "catalog_version": 3})
        queue.put_nowait({"event_type": wire.ITEM_ACTION_CARD, "card_id": "c-1", "title": "T", "text": "body",
                          "note": "n", "buttons": [{"label": "OK"}], "options": []})
        queue.put_nowait({"event_type": wire.ITEM_LIST_CARD, "kind": "tasks", "filters": {"a": 1},
                          "title": "L", "person": "user-admin"})
        queue.put_nowait({"event_type": "url", "url": "https://example.invalid/report", "title": "Report"})
        queue.put_nowait({"event_type": "image_generating", "prompt_preview": "a cat", "model": "m"})
        await drained(queue, "viewer_perm", 6)
        world.step("the viewer gets them, the cards persisted first", s,
                   rows=[[r[0], r[2], r[3]] for r in _rows(cid)])

        answers = {
            "settings": isess.settings_card_from_perm_item({"setting": "k", "label": "l"}),
            "action": isess.action_card_from_perm_item({"card_id": "c", "title": "t"}),
            "list": isess.list_card_from_perm_item({"kind": "k"}),
            "not_replayable": isess.persist_drained_artifact(cid, {"event_type": "permission_prompt"}),
            "no_chat": isess.persist_drained_artifact("", {"event_type": wire.ITEM_ACTION_CARD}),
            "card_items": sorted(isess.CARD_ITEMS),
        }
        world.step("the card helpers", s, **answers)

    world.run(scenario)
    check_golden("interactive/drainer", world.steps)


# ---------------------------------------------------------------------------
# The registry.
# ---------------------------------------------------------------------------

def test_the_registry_answers(world, temp_db):
    slug = make_test_agent("term-registry")
    for cid in ("chat-a", "chat-b", "chat-wake", "chat-fail"):
        _chat(temp_db, cid, slug)
    temp_db.append_pending_delegate_wake("chat-wake", "a stored delegate result")

    async def scenario():
        world.step("empty", **_registry("chat-a", "sid-a"), no_chat=isess.find_live_for_chat(""))

        a = await world.register("sid-a", "chat-a", slug)
        a2 = await world.register("sid-a2", "chat-a", slug)
        a.created_at, a2.created_at = 100.0, 200.0
        await world.register("sid-b", "chat-b", slug)
        a._turn_open = True
        world.step("three live, one streaming", **_registry("chat-a", "sid-a", "sid-a2", "sid-b"),
                   other_target=isess.find_live_for_chat("chat-a", target="m-1"))

        a2.pty.closed = True
        world.step("a dead PTY is no live one", **_registry("chat-a", "sid-a", "sid-a2"))
        a2.pty.closed = False

        first_b = isess.get("sid-b")
        again = await world.register("sid-b", "chat-b", slug)
        await world.settle()
        world.step("the lease: one process per session id", again, first_b, **_registry("chat-b", "sid-b"),
                   holds_the_new_one=isess.get("sid-b") is again)

        closed = await isess.close_for_chat("chat-a", reason="superseded")
        await world.settle()
        world.step("close_for_chat", closed=closed, nothing=await isess.close_for_chat(""),
                   **_registry("chat-a", "sid-a", "sid-a2"))

        wake = await world.register("sid-wake", "chat-wake", slug)
        await world.settle()
        world.step("a stored wake is queued at registration", wake,
                   stored=temp_db.get_chat("chat-wake").get("pending_delegate_wake"))

        ended = await world.register("sid-exit", "chat-fail", slug)
        world.ptys["sid-exit"].on_exit(3)
        await world.settle()
        world.step("the process exits", ended, **_registry("chat-fail", "sid-exit"))

        world.admission = concurrency.Admission(False, "busy", "No room.")
        try:
            await world.register("sid-refused", "chat-fail", slug)
        except isess.CapacityError as e:
            world.step("no slot", error=str(e), **_registry("chat-fail", "sid-refused"))
        world.admission = concurrency.Admission(True)

        world.spawn_error = OSError("no pty")
        try:
            await world.register("sid-nospawn", "chat-fail", slug)
        except OSError as e:
            world.step("the spawn fails: its slot goes back", error=str(e),
                       **_registry("chat-fail", "sid-nospawn"))
        world.spawn_error = None

        deleted = await world.register("sid-deleted", "chat-deleted", slug)
        deleted._turn_open = True
        await isess.close_session("sid-deleted", reason="chat_deleted")
        await world.settle()
        world.step("a deleted chat's close sends no ready frame", deleted)

        token = create_session_token("sid-token", slug, "user-admin")
        with_token = await register(session_id="sid-token", chat_id="", agent_name=slug,
                                    env={"PROXY_API_KEY": token}, pty=world._spawn_pty)
        expiry = float(validate_session_token(token)["exp"])
        without = await world.register("sid-token-none", "", slug)
        world.step("a parked question lives as long as the token in the spawn env", without,
                   holds_the_tokens_expiry=with_token.token_expires_at == expiry,
                   stands_in_a_day=abs(without.token_expires_at - time.time() - 24 * 3600) < 600)

        await isess.close_all(reason="shutdown")
        await world.settle()
        world.step("close_all", **_registry("chat-b", "sid-b", "sid-wake"))

    world.run(scenario)
    check_golden("interactive/registry", world.steps)


# ---------------------------------------------------------------------------
# The idle reaper's choices.
# ---------------------------------------------------------------------------

def test_reap_idle_chooses(world, temp_db):
    slug = make_test_agent("term-reap")
    timeout = 100

    async def scenario():
        now = time.monotonic

        async def make(sid, age=10_000, **kw):
            s = await world.register(sid, "", slug, **kw)
            s.last_activity = now() - age
            return s

        idle = await make("reap-idle")
        fresh = await make("reap-fresh", age=1)
        viewed = await make("reap-viewed")
        viewed.add_output_listener(lambda data: None)
        attached = await make("reap-otodock")
        attached.otodock_attached = True
        spared = await make("reap-prompt-pending")
        session_state._session_permission_requests["reap-prompt-pending"] = {"r-reap"}
        session_state._permission_events["r-reap"] = asyncio.Event()
        ceiling = config.get_session_timeout()
        assert ceiling > 2 * timeout
        turn = await make("reap-turn-open", age=ceiling / 2)
        turn._turn_open = True
        stuck = await make("reap-turn-stuck", age=ceiling + 60)
        stuck._turn_open = True
        await make("reap-hook-activity")
        session_state.record_hook_activity("reap-hook-activity")
        parked = await make("reap-parked")
        parked._question_parked = True
        parked_long = await make("reap-parked-past-wait", age=session_state.PROMPT_WAIT_S + 60)
        parked_long._question_parked = True
        parked_token = await make("reap-parked-token-over")
        parked_token._question_parked = True
        parked_token.token_expires_at = time.time() - 1

        world.manager.in_grace.add("m-grace")
        for sid, machine in (("reap-remote-grace", "m-grace"), ("reap-remote-gone", "m-gone")):
            remote = await isess.register_remote(
                session_id=sid, chat_id="", agent_name=slug, machine_id=machine,
                execution_path="claude-code-cli", config_payload={"env": {}}, user_sub="user-admin")
            remote.last_activity = now() - 10_000
        dead = await make("reap-dead-pty")
        dead.pty.closed = True
        world.fresh.clear()

        sessions = dict(isess._sessions)
        reaped = await isess.reap_idle(timeout_s=timeout)
        await world.settle()
        choices = {sid: {"reaped": isess.get(sid) is None, "spare": s._last_reap_spare,
                         "closed": s.closing} for sid, s in sorted(sessions.items())}
        world.step("the sweep", reaped=reaped, choices=choices,
                   slots_released=sorted(world.fresh.pop("slot_released", [])))
        second = await isess.reap_idle(timeout_s=timeout)
        world.step("a second sweep finds no more", reaped=second)
        session_state._permission_events.pop("r-reap", None)
        session_state._session_permission_requests.pop("reap-prompt-pending", None)
        third = await isess.reap_idle(timeout_s=timeout)
        world.step("the prompt's wait ended", reaped=third, gone=isess.get("reap-prompt-pending") is None,
                   spared=spared.closing, idle=idle.closing, fresh=fresh.closing)
        ancient = await make("reap-past-the-platform-timeout", age=_YEARS)
        by_default = await isess.reap_idle()
        await world.settle()
        world.step("with no timeout given: the platform's", ancient, reaped=by_default,
                   fresh_stays=fresh.closing is False, turn_stays=turn.closing is False)
        world.fresh.clear()

    world.run(scenario)
    check_golden("interactive/reap", world.steps)


# ---------------------------------------------------------------------------
# A terminal on a satellite.
# ---------------------------------------------------------------------------

def test_a_remote_terminal(world, temp_db, monkeypatch):
    slug = make_test_agent("term-remote")
    cid = _chat(temp_db, "chat-remote", slug)
    sid = "sid-remote"
    world.manager.os = "windows"
    tailed: list = []

    def _tail_lines(session_id, chat_id, lines):
        tailed.append([session_id, chat_id, lines])
        return {"persisted": len(lines), "last_signal": "end_turn", "turn_complete": True,
                "last_message": "from the satellite"}

    monkeypatch.setattr(transcript_tailer, "tail_lines", _tail_lines)

    async def scenario():
        s = await isess.register_remote(
            session_id=sid, chat_id=cid, agent_name=slug, machine_id="m-1",
            execution_path="claude-code-cli", config_payload={"env": {"X": "1"}, "argv": []},
            user_sub="user-admin", role="manager", username="admin", tui_theme="light")
        world.step("registered on its machine", s, is_remote=s.is_remote, target=s.target, theme=s.tui_theme,
                   remote_os=s.remote_os, registry=_registry(cid, sid))
        pty = world.ptys[sid]
        pty.ring = b"earlier\x1b[6n"
        replay = world.viewer(s)
        pty.on_output(b"\x1b[2Joutput\x1b[6n")
        world.step("a remote ring is stripped at its feed, not here", s, replay=_text(replay))

        s.write_input(b"cold prompt\r")
        await world.fire(s, "_mark_ready")
        await world.fire(s, "_fire_submit")
        world.step("the first Enter arms the ConPTY backstop", s)
        await world.fire(s, "_fire_submit")
        world.step("the backstop Enter, once", s)
        s._apply_turn_signal("user")
        await world.settle()
        world.step("no mid-turn tail on a satellite", s)

        s.created_at = time.monotonic() - _LONG_AGO
        s.feed_transcript_lines(['{"type": "user"}', '{"type": "assistant"}'])
        await world.settle()
        world.step("forwarded lines fold into the turn's end", s, tailed=list(tailed))
        tailed.clear()

        # The local terminal holds the input: the satellite types the queue.
        _idle(s)
        s.otodock_attached = True
        kept = s.queue_prompt("kept", "delegate_result", chat_id=cid)
        await world.settle()
        world.step("the satellite is told to type the head", s)
        s.handle_inject_result("not-the-id", True)
        world.step("a stale result changes nothing", s)
        s.handle_inject_result(world.last_inject_id, False, "busy")
        world.step("a decline keeps it queued", s)
        await s._try_drain_prompt_queue()
        world.step("the retry sends it again", s)
        sent_id = world.last_inject_id
        s._satellite_inject["sent_at"] -= _LONG_AGO
        await s._try_drain_prompt_queue()
        world.step("a lost result re-sends the same id", s, same_id=world.last_inject_id == sent_id)
        injected = []
        kept["on_injected"] = lambda: injected.append(True)
        s.handle_inject_result(world.last_inject_id, True)
        await world.settle()
        world.step("the ACK takes it and opens the turn", s, injected=injected)
        s._apply_turn_signal("end_turn")
        await world.settle()

        first = s.queue_prompt("cancelled", "delegate_result", chat_id=cid)
        await world.settle()
        cancelled = s.cancel_prompt(first)
        s.handle_inject_result(world.last_inject_id, True)
        await world.settle()
        world.step("cancelled in flight: nothing else leaves the queue", s, cancelled=cancelled)
        s._apply_turn_signal("end_turn")
        await world.settle()

    world.run(scenario)
    check_golden("interactive/remote", world.steps)


def test_a_task_chat_has_no_sole_driver(world, temp_db):
    slug = make_test_agent("term-task")
    run_id = "run-golden-term"
    temp_db.create_run(run_id, "dyn-x", slug, "manual", None, "go", "one-time", "user", "user-admin")
    cid = f"task-{run_id}"
    _chat(temp_db, cid, slug)

    async def scenario():
        s = await world.register("sid-task-term", cid, slug)
        world.step("a task's terminal", s, sole_driver=s.sole_driver,
                   may_drive=[s.may_drive("user-admin"), s.may_drive("user-viewer")],
                   input=s.deliver_dashboard_input(b"steer", sender_sub="user-viewer"))
        anyone = await world.register("sid-no-owner", "chat-no-owner", slug, user_sub="")
        world.step("no owner recorded", anyone, sole_driver=anyone.sole_driver,
                   may_drive=anyone.may_drive("user-viewer"))

    world.run(scenario)
    check_golden("interactive/identity", world.steps)
