"""Lane MACHINE-SESSIONS: a session on a machine survives a restart and a
dropped link, and a machine does what the platform does."""

from __future__ import annotations


def test_the_auth_answer_names_the_engines_whose_turns_are_replayed():
    """A 0.5.171 satellite lists a Codex session with its turn only under a
    platform that says it replays Codex turns."""
    from ws.satellite import _auth_ok, replays_turns_of
    assert replays_turns_of() == ["claude-code-cli", "codex-cli"]
    assert _auth_ok({}, cli_pins={})["replays_turns_of"] == ["claude-code-cli", "codex-cli"]


def test_the_sign_in_answer_says_the_platform_acks_a_terminals_lines():
    """``transcript_acks``: a 0.5.197 satellite sends a terminal's lines again
    from its start at a sign-in, when the platform acked nothing of it yet,
    only under a platform that says it acks (a 1.9.1 one never does)."""
    from ws.satellite import _auth_ok
    assert _auth_ok({}, cli_pins={})["transcript_acks"] is True


def test_the_sign_in_answer_carries_the_admins_bound_on_session_starts(monkeypatch):
    """``SESSION_SPAWN_CONCURRENCY`` bounded the starts of the sessions the
    platform ran itself. They run on the install's compute machines now, so
    the number goes to each compute machine at its sign-in. A personal or an
    admin-paired computer keeps its own bound (half its CPUs, at least 2),
    and so does a compute machine with no number set."""
    import config
    from ws.satellite import _auth_ok
    compute, own, admin = ({"pairing_scope": "compute"}, {"pairing_scope": "user"},
                           {"pairing_scope": "admin"})
    monkeypatch.setattr(config, "SESSION_SPAWN_CONCURRENCY", 0)
    assert "session_starts" not in _auth_ok(compute, cli_pins={})
    monkeypatch.setattr(config, "SESSION_SPAWN_CONCURRENCY", 3)
    assert _auth_ok(compute, cli_pins={})["session_starts"] == 3
    for machine in (own, admin, {}):
        assert "session_starts" not in _auth_ok(machine, cli_pins={})


import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from auth.path_policy import SecurityContext
from core import background, placement
from core.remote import remote_pty, satellite_connection as sc, satellite_link
from core.session import interactive_session, session_state
from services.remote import machine_sessions
from services.scheduler import continue_pass, cut_turns
from storage import database as task_store
from storage.chat import cut_records, open_turns


def _report(machine_id="machine-1", *, headless=(), terminals=(), complete=True, signed_in_at=None,
            current=True):
    return satellite_link.AliveReport(
        machine_id=machine_id, headless=frozenset(headless), terminals=frozenset(terminals),
        complete=complete, sessions=(), other_sessions=(),
        signed_in_at=time.monotonic() if signed_in_at is None else signed_in_at, report_id="r",
        _current=lambda: current)


class _Info:
    def __init__(self, sid, machine_id, *, turn_active, created_before=True):
        self.session_id = sid
        self.machine_id = machine_id
        self.turn_active = turn_active
        self.event_queue = asyncio.Queue()
        self.created_at = time.monotonic() - (10.0 if created_before else -10.0)


class _Remote:
    def __init__(self):
        self._sessions = {}
        self.closed = []
        self.credentials = []
        self.reprovisioned = []

    def session_ids_on(self, machine_id):
        return [sid for sid, i in self._sessions.items() if i.machine_id == machine_id]

    def is_spawning(self, sid):
        return False

    async def close_session(self, sid):
        # The real close forgets the session's person with its context.
        self._sessions.pop(sid, None)
        session_state._session_security.pop(sid, None)
        self.closed.append(sid)

    def _restore_adopted_credentials(self, *a, **k):
        self.credentials.append((*a, k))

    async def _reprovision_gateway(self, *a):
        self.reprovisioned.append(a)

    def is_session_grace_held(self, sid):
        return False


@pytest.fixture
def remote(monkeypatch):
    import core.session.session_manager as sm
    layer = _Remote()
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    # A fake turn with no reader holds the close for the whole wait.
    monkeypatch.setattr(machine_sessions, "_TURN_END_WAIT_S", 0.5)
    return layer


@pytest.fixture
def kicked(monkeypatch):
    asked = []
    monkeypatch.setattr(continue_pass, "kick", lambda: asked.append(1))
    return asked


def _chat(temp_db, *, target="machine-1", task=False):
    sid = uuid.uuid4().hex
    chat_id = f"task-run-{uuid.uuid4().hex[:8]}" if task else f"chat-{uuid.uuid4().hex[:8]}"
    task_store.create_chat(chat_id, "user-1", "pa", "default", model="m", execution_path="claude-code-cli")
    task_store.update_chat(chat_id, session_id=sid, execution_target=target)
    return chat_id, sid


class TestASessionTheMachineLost:
    """A complete report that lacks a session the platform held before the
    sign-in: its turn is ended first, the session is closed, and the chat
    gets one record for the pass."""

    @pytest.mark.asyncio
    async def test_an_open_turn_is_ended_closed_and_recorded_once(self, temp_db, remote, kicked):
        from core.events import stream_pump
        chat_id, sid = _chat(temp_db)
        info = _Info(sid, "machine-1", turn_active=True)
        remote._sessions[sid] = info
        ended = asyncio.Event()

        async def pump_body():
            await ended.wait()
        pump = SimpleNamespace(_task=asyncio.ensure_future(pump_body()), is_done=False, session_id=sid)
        stream_pump._active_pumps[chat_id] = pump
        session_state._record_session_use(sid, client_type="", agent="pa")
        task_store.upsert_user("sub-carol", "carol@x.test", "Carol", "member")
        username = task_store.get_username_by_sub("sub-carol") or ""
        person = task_store.get_user_sub_by_username(username) or ""
        _placed(sid, "machine-1", username=username)
        try:
            # The pump ends when the lost marker and the end reach its queue.
            async def end_when_told():
                err = await info.event_queue.get()
                assert err["type"] == "error" and err["durable_marker"] is True
                assert await info.event_queue.get() is None
                ended.set()
            watcher = asyncio.ensure_future(end_when_told())
            await machine_sessions.on_alive_report(_report())
            await watcher
            # Its lost ending writes no card: the pass carries the turn on.
            assert chat_id in stream_pump._pass_takes
            assert remote.closed == [sid]
            record = cut_records.unsettled_of(chat_id)
            assert record is not None and record["cause"] == cut_records.CAUSE_MACHINE
            assert record["kind"] == "dashboard" and record["session_id"] == sid
            # Its person, read before the close forgot it: the pass continues as them.
            assert person and record["person"] == person
            assert kicked == [1]
            # A second report changes nothing: the record stands, the session is gone.
            await machine_sessions.on_alive_report(_report())
            assert remote.closed == [sid]
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_the_record_comes_before_the_close_and_the_continue_waits_for_it(
            self, temp_db, remote, monkeypatch):
        """The record is written before the close, so a follow-up at the
        lane gate never finds the chat quiet with no record. The pass's
        continue starts the chat's session again under the same id: its
        start waits for the close (``session_closing``), whose tail clears
        that id's context, so the new context is the one that stays."""
        from core.session import session_closing
        task_store.upsert_user("sub-carol", "carol@x.test", "Carol", "member")
        username = task_store.get_username_by_sub("sub-carol") or ""
        chat_id, sid = _chat(temp_db)
        remote._sessions[sid] = _Info(sid, "machine-1", turn_active=True)
        _placed(sid, "machine-1", username=username)
        session_state._record_session_use(sid, client_type="dashboard", agent="pa")
        seen_at_close: list[bool] = []

        async def close(session_id):
            # The real close: the record out of the registry at once, the id
            # held until the tail has cleared what is kept under it.
            remote._sessions.pop(session_id, None)
            with session_closing.closing(session_id):
                seen_at_close.append(cut_records.unsettled_of(chat_id) is not None)
                await asyncio.sleep(0.05)       # the machine's ack
                session_state._session_security.pop(session_id, None)
                remote.closed.append(session_id)
        monkeypatch.setattr(remote, "close_session", close)
        started: list[int] = []

        async def _the_continue():
            await session_closing.wait_closed(sid)
            _placed(sid, "machine-1", username=username)
            started.append(1)

        monkeypatch.setattr(continue_pass, "kick",
                            lambda: background.spawn(_the_continue(), name="t", owner="t"))
        try:
            await machine_sessions.on_alive_report(_report())
            for _ in range(20):
                if started:
                    break
                await asyncio.sleep(0.02)
            assert seen_at_close == [True], "the close began before the record was written"
            assert started == [1] and remote.closed == [sid]
            assert session_state.get_session_security(sid) is not None
            record = cut_records.unsettled_of(chat_id)
            assert record["person"] == "sub-carol" and record["kind"] == "dashboard"
        finally:
            session_state._session_security.pop(sid, None)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_runs_turn_is_recorded_with_the_runs_name_in_a_tasks_chat_too(
            self, temp_db, remote, kicked):
        """The pump names the run that drives the turn: the record carries
        it, so the pass reopens the run. Before, a task's chat got no record
        and the run failed by its own reading of the lost ending."""
        from core.events import stream_pump
        chat_id, sid = _chat(temp_db, task=True)
        task_store.create_run("run-lost", "task-x", "pa", "manual", None, "do it")
        task_store.update_run("run-lost", status="running", chat_id=chat_id, session_id=sid)
        info = _Info(sid, "machine-1", turn_active=True)
        info.cli_dead = False
        remote._sessions[sid] = info
        ended = asyncio.Event()

        async def pump_body():
            await ended.wait()
        pump = SimpleNamespace(_task=asyncio.ensure_future(pump_body()), is_done=False,
                               session_id=sid, run_id="run-lost")
        stream_pump._active_pumps[chat_id] = pump
        session_state._record_session_use(sid, client_type="task", agent="pa")
        _placed(sid, "machine-1")
        try:
            async def end_when_told():
                await info.event_queue.get()
                await info.event_queue.get()
                ended.set()
            watcher = asyncio.ensure_future(end_when_told())
            await machine_sessions.on_alive_report(_report())
            await watcher
            record = cut_records.unsettled_of(chat_id)
            assert record is not None and record["run_id"] == "run-lost"
            assert record["cause"] == cut_records.CAUSE_MACHINE and record["kind"] == "task"
            # The machine is the one whose sign-in made the report, by its link's
            # own id: never a name a frame or a record carries.
            assert record["machine_id"] == "machine-1"
            # Its machine said the session is gone: no round rides it warm.
            assert info.cli_dead is True
            assert task_store.get_run("run-lost")["status"] == "running"
            assert kicked == [1] and remote.closed == [sid]
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
            stream_pump._pass_takes.discard(chat_id)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_pump_of_another_session_names_no_run(self, temp_db, remote, kicked):
        from core.events import stream_pump
        chat_id, sid = _chat(temp_db)
        info = _Info(sid, "machine-1", turn_active=True)
        remote._sessions[sid] = info

        async def pump_body():
            await asyncio.sleep(0.05)
        stream_pump._active_pumps[chat_id] = SimpleNamespace(
            _task=asyncio.ensure_future(pump_body()), is_done=False,
            session_id="another-session", run_id="run-other")
        try:
            assert await machine_sessions._end_turn(remote, info, chat_id) == machine_sessions.TurnEnded()
            # No mark either: that pump reads its own endings and keeps its card.
            assert chat_id not in stream_pump._pass_takes
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
            stream_pump._pass_takes.discard(chat_id)

    @pytest.mark.asyncio
    async def test_a_start_that_took_the_id_during_the_record_is_not_closed(
            self, temp_db, remote, kicked):
        """The lost turn has ended and the record is being written. A start
        puts its own session under the id in that moment. The close is of
        the record the report found: closed by id, the new session went
        with it."""
        from core.events import stream_pump
        chat_id, sid = _chat(temp_db)
        info = _Info(sid, "machine-1", turn_active=True)
        remote._sessions[sid] = info
        ended = asyncio.Event()
        started = _Info(sid, "machine-1", turn_active=False, created_before=False)

        async def pump_body():
            await ended.wait()
        stream_pump._active_pumps[chat_id] = SimpleNamespace(
            _task=asyncio.ensure_future(pump_body()), is_done=False, session_id=sid, run_id="")
        session_state._record_session_use(sid, client_type="dashboard", agent="pa")
        _placed(sid, "machine-1")

        async def end_then_start():
            await info.event_queue.get()
            await info.event_queue.get()
            remote._sessions[sid] = started      # a start replaced the lost session
            ended.set()
        watcher = asyncio.ensure_future(end_then_start())
        try:
            await machine_sessions.on_alive_report(_report())
            await watcher
            assert remote.closed == []
            assert remote._sessions.get(sid) is started
            # Only the lost one is marked dead, and its turn is still recorded.
            assert info.cli_dead is True and not getattr(started, "cli_dead", False)
            assert cut_records.unsettled_of(chat_id) is not None
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
            stream_pump._pass_takes.discard(chat_id)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_store_error_under_the_record_never_leaves_a_left_run_live(
            self, temp_db, remote, monkeypatch):
        """The run's own task already left it for the pass. With no record
        nothing would end it until the next boot: it fails with its report,
        the session is still closed, and the error is not swallowed."""
        import psycopg

        from core.events import stream_pump
        from services.scheduler import run_recovery
        chat_id, sid = _chat(temp_db, task=True)
        task_store.create_run("run-store", "task-x", "pa", "manual", None, "do it")
        task_store.update_run("run-store", status="running", chat_id=chat_id, session_id=sid)
        info = _Info(sid, "machine-1", turn_active=True)
        info.cli_dead = False
        remote._sessions[sid] = info

        async def pump_body():
            await info.event_queue.get()
        stream_pump._active_pumps[chat_id] = SimpleNamespace(
            _task=asyncio.ensure_future(pump_body()), is_done=False, session_id=sid,
            run_id="run-store")
        _placed(sid, "machine-1")

        async def _down(*a, **kw):
            raise psycopg.OperationalError("the store is away")
        monkeypatch.setattr(continue_pass, "session_gone", _down)
        ended: list[tuple] = []

        async def _fail(run_id, chat, reason, **kw):
            ended.append((run_id, chat, reason, kw.get("cause")))
        monkeypatch.setattr(run_recovery, "_fail_and_report", _fail)
        try:
            with pytest.raises(psycopg.OperationalError):
                await machine_sessions.on_alive_report(_report())
            assert ended == [("run-store", chat_id, run_recovery.RUN_CUT_MACHINE,
                              cut_records.CAUSE_MACHINE)]
            assert remote.closed == [sid]
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
            stream_pump._pass_takes.discard(chat_id)
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_machine_still_away_ends_the_turn_lost_with_its_own_detail_and_no_pass(
            self, temp_db, remote):
        """The reaper's entry: the bound ran out and the machine is not
        back. The turn ends for its reader, with its card: nothing continues it."""
        from core.events import stream_pump, turn_ending
        from core.layers.cli.remote import _satellite_error_events
        chat_id, sid = _chat(temp_db)
        info = _Info(sid, "machine-1", turn_active=True)
        remote._sessions[sid] = info
        got: list = []

        async def reader():
            got.append(await info.event_queue.get())
            got.append(await info.event_queue.get())
            info.turn_active = False
        watcher = asyncio.ensure_future(reader())
        await machine_sessions.end_away_turn(remote, info)
        await watcher
        assert got[0]["durable_marker"] is True and got[0]["detail"] == turn_ending.AWAY
        assert got[1] is None
        assert chat_id not in stream_pump._pass_takes
        ending = turn_ending.from_dict(_satellite_error_events(got[0])[0].data["ending"])
        assert (ending.reason, ending.detail) == (turn_ending.LOST, turn_ending.AWAY)
        assert "away too long" in ending.line()

    @pytest.mark.asyncio
    async def test_a_turn_with_no_running_pump_leaves_no_mark(self, temp_db, remote, kicked):
        """Only a running pump discards the mark at its end: with none, the
        chat's later lost ending would write no card."""
        from core.events import stream_pump
        chat_id, sid = _chat(temp_db)
        remote._sessions[sid] = _Info(sid, "machine-1", turn_active=True)
        session_state._record_session_use(sid, client_type="dashboard", agent="pa")
        try:
            await machine_sessions.on_alive_report(_report())
            assert remote.closed == [sid] and chat_id not in stream_pump._pass_takes
        finally:
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_an_idle_session_is_closed_and_not_recorded(self, temp_db, remote, kicked):
        chat_id, sid = _chat(temp_db)
        remote._sessions[sid] = _Info(sid, "machine-1", turn_active=False)
        await machine_sessions.on_alive_report(_report())
        assert remote.closed == [sid]
        assert cut_records.unsettled_of(chat_id) is None and kicked == []

    @pytest.mark.asyncio
    async def test_what_the_report_lists_a_later_start_and_an_incomplete_report_are_left(
            self, temp_db, remote, kicked):
        _, listed = _chat(temp_db)
        _, late = _chat(temp_db)
        _, other = _chat(temp_db, target="machine-2")
        remote._sessions[listed] = _Info(listed, "machine-1", turn_active=True)
        remote._sessions[late] = _Info(late, "machine-1", turn_active=True, created_before=False)
        remote._sessions[other] = _Info(other, "machine-2", turn_active=True)
        await machine_sessions.on_alive_report(_report(headless=[listed]))
        assert remote.closed == []
        _, gone = _chat(temp_db)
        remote._sessions[gone] = _Info(gone, "machine-1", turn_active=False)
        await machine_sessions.on_alive_report(_report(complete=False))
        await machine_sessions.on_alive_report(_report(current=False))
        assert remote.closed == []

    @pytest.mark.asyncio
    async def test_a_task_chats_session_is_closed_and_left_to_its_run(self, temp_db, remote, kicked):
        chat_id, sid = _chat(temp_db, task=True)
        remote._sessions[sid] = _Info(sid, "machine-1", turn_active=True)
        await machine_sessions.on_alive_report(_report())
        assert remote.closed == [sid]
        assert cut_records.unsettled_of(chat_id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["a meeting participant", "a task's run"])
    async def test_a_session_with_no_pump_has_its_open_turn_ended_before_the_close(
            self, temp_db, remote, kicked, kind):
        """A meeting participant's session has no chat, and a task's run (a
        wrap-up, a judge) has no pump: the open turn gets the lost marker and
        the end like a chat's, and the close waits for the turn's own stream
        to read them (the close takes the engine's state, so a turn still
        reading would wait on it for ever). No record either way."""
        sid = uuid.uuid4().hex if kind == "a meeting participant" else _chat(temp_db, task=True)[1]
        info = _Info(sid, "machine-1", turn_active=True)
        remote._sessions[sid] = info
        read: list = []

        async def the_turn():
            read.append(await info.event_queue.get())
            read.append(await info.event_queue.get())
            await asyncio.sleep(0.05)
            info.turn_active = False
        turn = asyncio.ensure_future(the_turn())
        real_close = remote.close_session

        async def close(session_id):
            assert info.turn_active is False, "closed under a turn still reading"
            await real_close(session_id)
        remote.close_session = close
        await machine_sessions.on_alive_report(_report())
        await turn
        assert read[0]["type"] == "error" and read[0]["durable_marker"] is True and read[1] is None
        assert remote.closed == [sid] and kicked == []

    @pytest.mark.asyncio
    async def test_a_turn_that_never_reads_its_end_holds_the_close_only_for_the_bound(
            self, temp_db, remote, kicked, monkeypatch):
        sid = uuid.uuid4().hex
        remote._sessions[sid] = _Info(sid, "machine-1", turn_active=True)
        monkeypatch.setattr(machine_sessions, "_TURN_END_WAIT_S", 0.2)
        started = time.monotonic()
        await machine_sessions.on_alive_report(_report())
        assert remote.closed == [sid] and 0.2 <= time.monotonic() - started < 2.0

    @pytest.mark.asyncio
    async def test_an_unsettled_record_of_the_chat_stands(self, temp_db, kicked):
        chat_id, sid = _chat(temp_db)
        first = cut_records.add(chat_id, cause=cut_records.CAUSE_CRASH, state=cut_records.WAITING,
                                session_id="older", machine_id="machine-1", kind="dashboard")
        assert await machine_sessions.machine_lost_session(chat_id, sid, "machine-1") is False
        assert cut_records.unsettled_of(chat_id)["id"] == first["id"] and kicked == []

    @pytest.mark.asyncio
    async def test_a_session_with_no_recorded_kind_takes_its_chats(self, temp_db, kicked):
        chat_id, sid = _chat(temp_db)
        assert session_state.get_session_client_type(sid) == ""
        try:
            assert await machine_sessions.machine_lost_session(chat_id, sid, "machine-1") is True
            assert cut_records.unsettled_of(chat_id)["kind"] == "dashboard"
            # The record takes the chat's kind, the closed session's state is left alone.
            assert session_state.get_session_client_type(sid) == ""
        finally:
            session_state._sessions.pop(sid, None)


@pytest.fixture
def _registry():
    from core import concurrency
    concurrency.init()
    real_live = concurrency._live_available_mb
    concurrency._live_available_mb = lambda: 32768
    interactive_session._lock = None
    yield
    concurrency._live_available_mb = real_live


def _placed(sid, machine_id, *, username="alice", role="manager", kind="remote"):
    session_state._session_security[sid] = SecurityContext(
        role=role, username=username, agent="pa", is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind=kind, machine_id=machine_id))
    session_state._session_security_ts[sid] = time.time()


class TestTheTerminalClaim:
    """A terminal the machine still runs and the platform does not hold is
    taken back as its chat's terminal, only for the chat on this machine
    whose context is placed here, as the registered person; anything else
    is refused and gets no token."""

    @pytest.fixture(autouse=True)
    def _machine(self, temp_db, remote, _registry):
        from storage import remote_store
        task_store.upsert_user("sub-alice", "alice@x.test", "Alice", "admin")
        # Alice existed before her sessions: a token minted before the users
        # row is a re-created person's and is never current.
        with task_store.get_conn() as conn:
            conn.execute("UPDATE users SET created_at=%s WHERE sub=%s",
                         ("2026-01-01T00:00:00+00:00", "sub-alice"))
            conn.commit()
        remote_store.create_remote_machine(machine_id="machine-1", name="m1", registered_by="sub-alice",
                                           pairing_scope="admin")
        remote_store.create_remote_machine(machine_id="machine-u", name="mu", registered_by="sub-bob",
                                           pairing_scope="user")
        remote_store.create_remote_machine(machine_id="compute-1", name="Compute", registered_by=None,
                                           pairing_scope=placement.PAIRING_COMPUTE)
        self.remote = remote
        yield
        for sid in list(interactive_session._sessions):
            sess = interactive_session.get(sid)
            if sess is not None:
                sess._closed = True
                if sess._drainer is not None:
                    sess._drainer.cancel()
            interactive_session._sessions.pop(sid, None)
        remote_pty._remote_ptys.clear()
        remote_pty._away_since.clear()
        for key in list(session_state._session_security):
            if key.startswith("claim-"):
                session_state._session_security.pop(key, None)
                session_state._session_security_ts.pop(key, None)

    @pytest_asyncio.fixture(autouse=True)
    async def _adoptions_finish(self):
        """An adoption's tail (the slot, the wakes, the credentials) runs in a
        task of its own: each test waits for its own before the next."""
        yield
        pending = [t for t in background.running("interactive_session") if t.get_name().endswith(":adopt_remote")]
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        assert not [t for t in pending if not t.done()]

    def _chat_on(self, machine_id, *, token=""):
        sid = "claim-" + uuid.uuid4().hex
        chat_id = f"chat-{uuid.uuid4().hex[:8]}"
        task_store.create_chat(chat_id, "sub-alice", "pa", "default", model="m",
                               execution_path="claude-code-cli")
        task_store.update_chat(chat_id, session_id=sid, execution_target=machine_id)
        if token:
            open_turns.open_turn(chat_id, token, sid, kind=cut_turns.KIND_TERMINAL)
        return chat_id, sid

    @pytest.mark.asyncio
    async def test_a_terminal_taken_back_refills_its_credentials_once(self, monkeypatch):
        """The proxy held the session's credentials in memory: the terminal
        taken back after a restart refills them from its descriptor, the same
        call a headless take-back makes, before the slot and the wakes."""
        calls: list[tuple[str, str]] = []

        async def _reprovision(session_id, machine_id):
            calls.append((session_id, machine_id))

        monkeypatch.setattr(self.remote, "_reprovision_gateway", _reprovision)
        chat_id, sid = self._chat_on("machine-1")
        _placed(sid, "machine-1")
        from core.remote import satellite_link
        assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
        # Its tail binds later, in a task of its own: on its way until then.
        assert satellite_link._adopting.get("machine-1") == {sid}
        for _ in range(200):
            if calls and not satellite_link._adopting.get("machine-1"):
                break
            await asyncio.sleep(0.01)
        assert calls == [(sid, "machine-1")]
        assert interactive_session.get(sid).chat_id == chat_id
        assert not satellite_link._adopting.get("machine-1")

    @pytest.mark.parametrize("alive", [True, False])
    @pytest.mark.asyncio
    async def test_only_a_live_terminal_is_refilled(self, alive):
        """A terminal closed during the adoption's hops: a refill would
        provision a dead session the gateway's tick keeps pushing. The rest of
        the tail still runs."""
        chat_id, sid = self._chat_on("machine-1")
        sess = SimpleNamespace(alive=alive, session_id=sid, chat_id=chat_id, target="machine-1", user_sub="sub-alice",
                               agent_name="pa", execution_path="claude-code-cli", payer=None,
                               queue_prompt=lambda *a, **k: None)
        await interactive_session._finish_adoption(sess)
        assert self.remote.reprovisioned == ([(sid, "machine-1")] if alive else [])
        assert [a[0] for a in self.remote.credentials] == [sid]

    @pytest.mark.asyncio
    async def test_a_terminal_taken_back_refills_a_real_descriptor(self, monkeypatch, tmp_path):
        """The whole path: a terminal's descriptor names a gateway and a
        command-line credential; after the claim the broker holds both, so
        git's credential hook and the machine's gateway answer."""
        import json as _json

        from core.credentials import mcp_broker, mcp_gateway
        from core.credentials.mcp_gateway import (
            CliCredential,
            GatewayCredential,
            TokenRef,
        )
        from core.remote import mcp_gateway_push as push
        from core.remote.remote_resume import RemoteResumeMixin
        from tests.remote.test_mcp_gateway_push import _allow, _Cm

        monkeypatch.setattr(self.remote, "_reprovision_gateway",
                            RemoteResumeMixin._reprovision_gateway)
        cm = _Cm()
        monkeypatch.setattr(push, "_manager", lambda: cm)
        monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
        (tmp_path / "acct.json").write_text(_json.dumps({"access_token": "at-9", "expires_at": "", "extra": {}}))
        ref = TokenRef(str(tmp_path), "acct", "")
        chat_id, sid = self._chat_on("machine-1")
        _placed(sid, "machine-1")
        mcp_gateway.write_descriptor(
            sid, {"github-mcp": GatewayCredential(upstream="https://mcp.example.com", path="/mcp",
                                                  allowlist_key=_allow(), token_ref=ref)},
            cli={"github-mcp": CliCredential(host="github.com", token_ref=ref,
                                             env_names=("GH_TOKEN",), manifest="github-mcp")},
            machine_id="machine-1", token_hash="h" * 64, agent="pa", user_sub="sub-alice",
            task_scope="user")
        try:
            assert mcp_broker.bundles_of(sid) == {}
            assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
            for _ in range(300):
                if mcp_broker.bundles_of(sid):
                    break
                await asyncio.sleep(0.01)
            bundle = mcp_broker.get(sid, "github-mcp")
            assert bundle.gateway is not None and bundle.cli.host == "github.com"
            assert mcp_broker.cli_credential(sid, "github.com") is not None
            for _ in range(200):
                if cm.sent:
                    break
                await asyncio.sleep(0.01)
            assert [m["mcp"] for _, m in cm.sent] == ["github-mcp"]
        finally:
            mcp_broker.purge_session(sid)

    @pytest.mark.asyncio
    async def test_a_terminal_with_no_descriptor_still_comes_back(self, monkeypatch):
        """A session with no descriptor, as a headless take-back: nothing to
        refill, the adoption completes."""
        from core.remote.remote_resume import RemoteResumeMixin
        monkeypatch.setattr(self.remote, "_reprovision_gateway",
                            RemoteResumeMixin._reprovision_gateway)
        chat_id, sid = self._chat_on("machine-1")
        _placed(sid, "machine-1")
        assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
        for _ in range(100):
            if self.remote.credentials:
                break
            await asyncio.sleep(0.01)
        assert interactive_session.get(sid) is not None and self.remote.credentials

    @pytest.mark.asyncio
    async def test_a_terminal_whose_account_lost_its_token_still_comes_back(self, monkeypatch, tmp_path):
        """A descriptor naming an account that needs reconnecting: the broker
        is filled and nothing is pushed, as for a headless session, and the
        adoption completes."""
        from core.credentials import mcp_broker, mcp_gateway
        from core.credentials.mcp_gateway import GatewayCredential, TokenRef
        from core.remote import mcp_gateway_push as push
        from core.remote.remote_resume import RemoteResumeMixin
        from tests.remote.test_mcp_gateway_push import _allow, _Cm

        monkeypatch.setattr(self.remote, "_reprovision_gateway",
                            RemoteResumeMixin._reprovision_gateway)
        cm = _Cm()
        monkeypatch.setattr(push, "_manager", lambda: cm)
        monkeypatch.setattr(mcp_gateway, "resolve_credential",
                            lambda cred: mcp_gateway.Refusal("reconnect the account"))
        chat_id, sid = self._chat_on("machine-1")
        _placed(sid, "machine-1")
        mcp_gateway.write_descriptor(
            sid, {"github-mcp": GatewayCredential(upstream="https://mcp.example.com", path="/mcp",
                                                  allowlist_key=_allow(),
                                                  token_ref=TokenRef(str(tmp_path), "gone", ""))},
            machine_id="machine-1", token_hash="h" * 64, agent="pa", user_sub="sub-alice",
            task_scope="user")
        try:
            assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
            for _ in range(300):
                if mcp_broker.bundles_of(sid):
                    break
                await asyncio.sleep(0.01)
            assert mcp_broker.get(sid, "github-mcp").gateway is not None
            assert cm.sent == [] and interactive_session.get(sid) is not None
        finally:
            mcp_broker.purge_session(sid)

    @pytest.mark.asyncio
    async def test_a_held_terminal_reconnecting_refills_nothing(self, monkeypatch):
        """A machine's link blip: the platform still holds the terminal and
        its credentials, so the reconcile that re-adopts it refills nothing."""
        from core.remote import remote_pty as rp
        monkeypatch.setattr(self.remote, "_reprovision_gateway",
                            lambda *a, **k: pytest.fail("a reprovision on a blip"))
        pty = rp.adopt_remote_pty(machine_id="machine-1", session_id="claim-held", rows=24, cols=80,
                                  on_output=lambda _b: None, on_exit=lambda *_a: None)
        readopted, exited, orphans = rp.reconcile_machine_ptys("machine-1", {"claim-held"})
        assert "claim-held" in readopted and not exited and not orphans
        assert remote_pty._remote_ptys.get(("machine-1", "claim-held")) is pty

    @pytest.mark.asyncio
    async def test_a_chats_terminal_is_taken_back_with_its_row(self):
        chat_id, sid = self._chat_on("machine-1", token="tok-1")
        _placed(sid, "machine-1")
        session_state._reloaded_state[sid] = ("plan", 1790000000)
        taken = await machine_sessions.claim_terminals("machine-1", frozenset({sid}))
        assert taken == {sid}
        sess = interactive_session.get(sid)
        assert sess is not None and sess.target == "machine-1" and sess.chat_id == chat_id
        assert sess.turn_open and sess._turn_token == "tok-1" and sess.alive
        assert remote_pty._remote_ptys[("machine-1", sid)] is sess.pty
        assert session_state.get_session_mode(sid) == "plan"
        assert session_state.get_session_client_type(sid) == "dashboard"
        for _ in range(200):
            if self.remote.credentials:
                break
            await asyncio.sleep(0.01)
        assert [a[0] for a in self.remote.credentials] == [sid]
        # The record's branch: a terminal taken back settles left, no card.
        assert cut_turns._terminal_taken_back(sid, "machine-1")
        assert not cut_turns._terminal_taken_back(sid, "machine-2")

    @pytest.mark.asyncio
    async def test_a_terminal_whose_person_signed_out_after_its_start_is_refused(self):
        """A sign out everywhere after the terminal's start: the database
        refuses it, whatever the file still says."""
        from storage.identity import db_users
        _chat_id, sid = self._chat_on("machine-1", token="tok-1")
        _placed(sid, "machine-1")
        session_state._reloaded_state[sid] = ("plan", int(time.time()) - 60)
        db_users.bump_token_epoch("sub-alice")
        assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == set()

    @pytest.mark.asyncio
    async def test_a_compute_machines_terminal_is_taken_back_with_its_row(self, monkeypatch):
        """The platform's own compute machine runs a person's terminal chat,
        pinned to the platform's place: a restart takes it back like a paired
        machine's, its turn open on its row, its record settled with no card,
        and its credential file restored as the platform's own file."""
        watched: list[str] = []
        from services.remote import chat_reload
        monkeypatch.setattr(chat_reload, "_watch_terminal", watched.append)
        chat_id, sid = self._chat_on("local", token="tok-c")
        _placed(sid, "compute-1", kind=placement.KIND_COMPUTE)
        session_state._reloaded_state[sid] = ("ask", 0)
        # The boot found the turn's row: it waits for the compute machine.
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        record = cut_records.unsettled_of(chat_id)
        assert record["state"] == cut_records.WAITING and record["machine_id"] == "compute-1"
        assert await machine_sessions.claim_terminals("compute-1", frozenset({sid})) == {sid}
        sess = interactive_session.get(sid)
        assert sess is not None and sess.target == "compute-1" and sess.chat_id == chat_id
        assert sess.turn_open and sess._turn_token == "tok-c"
        assert cut_turns._terminal_taken_back(sid, "compute-1")
        for _ in range(200):
            if self.remote.credentials:
                break
            await asyncio.sleep(0.01)
        assert self.remote.reprovisioned == [(sid, "compute-1")]
        assert self.remote.credentials == [(sid, "compute-1", "pa", "claude-code-cli", {"local_file": True})]
        assert watched == [chat_id]
        # The report settles the record: the turn goes on, no card, no continue.
        assert await cut_turns.machine_reported(_report("compute-1", terminals={sid},
                                                        signed_in_at=time.monotonic() + 1)) == []
        assert cut_records.get(record["id"])["state"] == cut_records.LEFT
        assert open_turns.get_open(chat_id)["turn_token"] == "tok-c"

    @pytest.mark.asyncio
    async def test_a_paired_machines_terminal_keeps_its_file_on_the_machine(self, monkeypatch):
        from services.remote import chat_reload
        watched: list[str] = []
        monkeypatch.setattr(chat_reload, "_watch_terminal", watched.append)
        chat_id, sid = self._chat_on("machine-1")
        _placed(sid, "machine-1")
        assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
        for _ in range(200):
            if self.remote.credentials:
                break
            await asyncio.sleep(0.01)
        assert self.remote.credentials == [(sid, "machine-1", "pa", "claude-code-cli", {"local_file": False})]
        assert watched == [chat_id]

    @pytest.mark.asyncio
    async def test_the_start_hold_ends_only_after_the_take_back_answered(self, monkeypatch):
        """The hold after a restart waits for the compute machine's first
        complete report. The terminals' half of that report is noted only
        after its reconcile ran the claims, so the terminal is the chat's
        again before any held work goes out; the claim itself never waits for
        the hold. A report missing its terminals' half ends nothing."""
        from services.remote import chat_reload, compute_bootstrap
        monkeypatch.setattr(chat_reload, "_watch_terminal", lambda _cid: None)
        cm = sc.SatelliteConnectionManager()
        monkeypatch.setattr(sc, "_connection_manager", cm)
        sent: list[dict] = []

        async def _enqueue(frame):
            sent.append(frame)
        conn = SimpleNamespace(compute=True, capabilities={}, enqueue_send=_enqueue)
        cm._connections["compute-1"] = conn
        satellite_link.reset_hooks()
        machine_sessions.register()
        compute_bootstrap.register()
        compute_bootstrap.reset_for_tests()
        compute_bootstrap._expected.add("compute-1")
        compute_bootstrap._back = asyncio.Event()
        chat_id, sid = self._chat_on("local", token="tok-h")
        _placed(sid, "compute-1", kind=placement.KIND_COMPUTE)
        try:
            cm._links.begin_report("compute-1", conn, "r1")
            await cm._frame_pty_alive("compute-1", {"session_ids": [sid], "report_id": "r1"})
            assert interactive_session.get(sid) is not None
            assert not compute_bootstrap._back.is_set()
            await cm._frame_sessions_alive("compute-1", {"sessions": [], "other_sessions": [],
                                                         "report_id": "r1"})
            await asyncio.wait_for(compute_bootstrap._back.wait(), timeout=5.0)
            assert interactive_session.get(sid).turn_open
            assert not [f for f in sent if f.get("type") == "pty_close"]

            # The other order: the sessions' half alone is no complete report.
            compute_bootstrap._expected.add("compute-1")
            compute_bootstrap._back = asyncio.Event()
            cm._links.begin_report("compute-1", conn, "r2")
            await cm._frame_sessions_alive("compute-1", {"sessions": [], "other_sessions": [],
                                                         "report_id": "r2"})
            await cm._frame_pty_alive("compute-1", {"session_ids": [sid], "report_id": "r2"})
            for _ in range(20):
                await asyncio.sleep(0)
            assert not compute_bootstrap._back.is_set()
        finally:
            satellite_link.reset_hooks()
            compute_bootstrap.reset_for_tests()

    @pytest.mark.parametrize("compute", [True, False])
    @pytest.mark.asyncio
    async def test_a_compute_terminal_takes_no_admission_at_its_adoption(self, monkeypatch, compute):
        """On a compute machine the slot of a session it kept is the
        reconciler's (``concurrency._adopt_compute_sessions``, stamped old):
        an admission here would count the running terminal's memory as new
        and could evict. A paired machine's call is unchanged (it admits at
        once, the machine counts its own)."""
        from core import concurrency
        asked: list[tuple] = []

        async def _acquire(sid, **kw):
            asked.append((sid, kw.get("target")))
            return concurrency._ADMITTED

        monkeypatch.setattr(concurrency, "acquire_chat_slot", _acquire)
        machine = "compute-1" if compute else "machine-1"
        chat_id, sid = self._chat_on("local" if compute else machine)
        _placed(sid, machine, kind=placement.KIND_COMPUTE if compute else "remote")
        sess = SimpleNamespace(alive=True, session_id=sid, chat_id=chat_id, target=machine, user_sub="sub-alice",
                               agent_name="pa", execution_path="claude-code-cli", payer=None,
                               queue_prompt=lambda *a, **k: None)
        await interactive_session._finish_adoption(sess)
        assert asked == ([] if compute else [(sid, machine)])
        assert [c[0] for c in self.remote.credentials] == [sid]

    @pytest.mark.asyncio
    async def test_a_terminal_the_machine_lost_in_mid_turn_is_continued_not_carded(self, monkeypatch):
        """The machine came back without a terminal that had a turn open: the
        reconcile ends it (the real close, which forgets the session's
        person and mode), and the record still names them, so the pass
        continues the chat instead of carding it."""
        cm = sc.SatelliteConnectionManager()
        monkeypatch.setattr(sc, "_connection_manager", cm)
        satellite_link.reset_hooks()
        satellite_link.add_terminals_lost_hook(machine_sessions.terminals_lost, name="t")
        monkeypatch.setattr(continue_pass, "kick", lambda: None)
        chat_id, sid = self._chat_on("machine-1", token="tok-lost")
        _placed(sid, "machine-1")
        session_state.register_session_state(sid, "plan", session_state.get_session_security(sid))
        try:
            assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
            sess = interactive_session.get(sid)
            assert sess.turn_open
            await cm._reconcile_ptys("machine-1", [])
            for _ in range(200):
                if cut_records.unsettled_of(chat_id) is not None:
                    break
                await asyncio.sleep(0.05)
            assert interactive_session.get(sid) is None
            assert session_state.get_session_security(sid) is None   # the close forgot it
            record = cut_records.unsettled_of(chat_id)
            assert record is not None and record["cause"] == cut_records.CAUSE_MACHINE
            assert record["person"] == "sub-alice" and record["mode"] == "plan"
            assert record["kind"] == "dashboard"
            assert continue_pass.judge(record).action == continue_pass.CONTINUE
        finally:
            satellite_link.reset_hooks()

    @pytest.mark.asyncio
    async def test_a_terminals_lines_are_acked_once_their_rows_are_persisted(self, monkeypatch):
        """The machine never sends past an acked offset again, so a batch is
        acked only once its rows are written, with the open that took it. A
        batch whose persist failed sends no ack of its own."""
        from core.layers.cli import layer as cli_layer
        chat_id, sid = self._chat_on("machine-1")
        _placed(sid, "machine-1")
        assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
        sess = interactive_session.get(sid)
        sess.pty.open_token = "aa11"
        cm = sc.get_connection_manager()
        acks: list = []

        async def send(machine_id, frame):
            acks.append((machine_id, frame))
        monkeypatch.setattr(cm, "send_fire_and_forget", send)
        tailed: list = []
        ok = {"fail": False}

        def tail_lines(session_id, chat_id_, lines):
            if ok["fail"]:
                raise RuntimeError("the store is gone")
            tailed.append(lines)
            return {"persisted": len(lines)}
        tailer = SimpleNamespace(tail_lines=tail_lines, forget=lambda sid: None)
        monkeypatch.setattr(cli_layer.CLIExecutionLayer, "transcript_tailer", lambda self: tailer)
        sess.feed_transcript_lines(["a"], ack_offset=40)
        for _ in range(100):
            if acks:
                break
            await asyncio.sleep(0.01)
        assert tailed == [["a"]]
        assert acks == [("machine-1", {"type": "transcript_ack", "session_id": sid, "offset": 40,
                                       "open_token": "aa11"})]
        ok["fail"] = True
        sess.feed_transcript_lines(["b"], ack_offset=80)
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert len(acks) == 1

    def _lost_with_a_held_close(self, monkeypatch, *, wait_s):
        """A claimed terminal with a turn open, its close held in its final
        transcript tail until ``gate`` is set: (chat id, session id, gate,
        the kicks, the connection manager)."""
        import threading

        from core.layers.cli import layer as cli_layer
        gate = threading.Event()

        def resolve_and_tail(session_id, chat_id_):
            gate.wait(10.0)
            return {}
        tailer = SimpleNamespace(resolve_and_tail=resolve_and_tail, tail_lines=lambda *a: {"persisted": 0},
                                 forget=lambda sid: None)
        monkeypatch.setattr(cli_layer.CLIExecutionLayer, "transcript_tailer", lambda self: tailer)
        monkeypatch.setattr(machine_sessions, "_TERMINAL_GONE_WAIT_S", wait_s)
        cm = sc.SatelliteConnectionManager()
        monkeypatch.setattr(sc, "_connection_manager", cm)
        satellite_link.reset_hooks()
        satellite_link.add_terminals_lost_hook(machine_sessions.terminals_lost, name="t")
        chat_id, sid = self._chat_on("machine-1", token="tok-held")
        _placed(sid, "machine-1")
        session_state.register_session_state(sid, "plan", session_state.get_session_security(sid))
        kicked: list[int] = []

        def kick():
            # The pass's continue starts the chat's session again under the id.
            _placed(sid, "machine-1")
            kicked.append(1)
        monkeypatch.setattr(continue_pass, "kick", kick)
        return chat_id, sid, gate, kicked, cm

    @pytest.mark.asyncio
    async def test_a_lost_terminal_is_recorded_only_once_its_own_close_is_done(self, monkeypatch):
        """The record kicks the pass, whose continue starts the chat's session
        again under the same id: while the lost terminal's close can still
        reach its cleanup (which clears that id's state) no record is
        written. Once it is done, one record, and the continue's context
        under the id survives."""
        chat_id, sid, gate, kicked, cm = self._lost_with_a_held_close(monkeypatch, wait_s=5.0)
        try:
            assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
            old = interactive_session.get(sid)
            await cm._reconcile_ptys("machine-1", [])
            await asyncio.sleep(0.8)
            assert interactive_session.get(sid) is old and old.closing
            assert kicked == [] and cut_records.unsettled_of(chat_id) is None
            gate.set()
            for _ in range(200):
                if kicked:
                    break
                await asyncio.sleep(0.05)
            assert kicked == [1] and interactive_session.get(sid) is None
            record = cut_records.unsettled_of(chat_id)
            assert record["person"] == "sub-alice" and record["mode"] == "plan"
            assert continue_pass.judge(record).action == continue_pass.CONTINUE
            await asyncio.sleep(0.2)
            assert session_state.get_session_security(sid) is not None   # the continue's, kept
        finally:
            gate.set()
            satellite_link.reset_hooks()

    @pytest.mark.asyncio
    async def test_a_lost_terminal_whose_close_outlives_the_bound_gets_no_record(self, monkeypatch, caplog):
        """Past the bound the close may still clear the id's state, so the
        chat gets no record (a lost turn, with a warning naming it), never
        a continue that would lose its context to that close."""
        import logging
        chat_id, sid, gate, kicked, cm = self._lost_with_a_held_close(monkeypatch, wait_s=0.4)
        try:
            assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
            with caplog.at_level(logging.WARNING, logger="otodock.machine-sessions"):
                await cm._reconcile_ptys("machine-1", [])
                await asyncio.sleep(1.0)
            assert kicked == [] and cut_records.unsettled_of(chat_id) is None
            assert chat_id[:8] in caplog.text and "no record" in caplog.text
            gate.set()
            for _ in range(100):
                if interactive_session.get(sid) is None:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            assert kicked == [] and cut_records.unsettled_of(chat_id) is None
        finally:
            gate.set()
            satellite_link.reset_hooks()

    @pytest.mark.asyncio
    async def test_the_open_turns_prompt_lands_once_when_the_machine_gives_its_line_back(self):
        """The earlier process wrote the prompt's row and its note went with it:
        the journal's line for it, held by the machine while the platform was
        away, is skipped once (found by the T1 walk)."""
        from core.session.transcript_tool_events import consume_sent_prompt
        chat_id, sid = self._chat_on("machine-1", token="tok-2")
        task_store.add_chat_message(chat_id, "user", "Run the long command.")
        _placed(sid, "machine-1")
        assert await machine_sessions.claim_terminals("machine-1", frozenset({sid})) == {sid}
        assert task_store.get_last_user_message_text(chat_id) == "Run the long command."
        stamped = "[Current time: Saturday, November 07, 2026 06:48 (6:48 AM) UTC]\n\nRun the long command."
        assert consume_sent_prompt(chat_id, stamped)
        assert not consume_sent_prompt(chat_id, stamped)

    @pytest.mark.asyncio
    async def test_a_forged_id_a_foreign_chat_and_a_task_are_refused(self):
        # Placed on another machine: another machine's terminal, never this one's.
        _, elsewhere = self._chat_on("machine-2")
        _placed(elsewhere, "machine-2")
        # A context with no machine (an older index): refused, no leniency.
        _, unplaced = self._chat_on("machine-1")
        session_state._session_security[unplaced] = SecurityContext(
            role="manager", username="alice", agent="pa", is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind="remote", machine_id=""))
        session_state._session_security_ts[unplaced] = time.time()
        # The chat runs elsewhere, whatever the context says.
        _, moved = self._chat_on("machine-2")
        _placed(moved, "machine-1")
        # A task's terminal is the run's.
        tsid = "claim-" + uuid.uuid4().hex
        task_store.create_chat(f"task-run-{uuid.uuid4().hex[:8]}", "sub-alice", "pa", "default", model="m")
        _placed(tsid, "machine-1")
        # No context at all.
        unknown = "claim-" + uuid.uuid4().hex
        taken = await machine_sessions.claim_terminals(
            "machine-1", frozenset({elsewhere, unplaced, moved, tsid, unknown}))
        assert taken == set()
        assert interactive_session._sessions == {} and self.remote.credentials == []

    @pytest.mark.asyncio
    async def test_a_user_paired_machine_takes_only_its_owners_terminal(self):
        _, sid = self._chat_on("machine-u")
        _placed(sid, "machine-u")   # alice's context on bob's machine
        assert await machine_sessions.claim_terminals("machine-u", frozenset({sid})) == set()
        task_store.upsert_user("sub-bob", "bob@x.test", "Bob", "admin")
        _, bobs = self._chat_on("machine-u")
        _placed(bobs, "machine-u", username=task_store.get_username_by_sub("sub-bob"), role="member")
        assert await machine_sessions.claim_terminals("machine-u", frozenset({bobs})) == {bobs}


def _ignore(*_a):
    return None


class TestTheWindowEndsOnlyTheWaiting:
    @pytest.mark.asyncio
    async def test_an_away_machines_terminals_wait_and_are_swept_after_a_day(self):
        cm = sc.SatelliteConnectionManager()
        exits = []
        try:
            rp = remote_pty.adopt_remote_pty(machine_id="m-away", session_id="t1", rows=24, cols=80,
                                             on_output=_ignore, on_exit=exits.append)
            remote_pty.machine_away("m-away")
            assert cm.is_pty_in_grace("m-away")
            assert remote_pty.sweep_away(now=time.monotonic()) == 0
            assert exits == [] and not rp.closed
            assert remote_pty.sweep_away(now=time.monotonic() + remote_pty.TERMINAL_AWAY_MAX_S + 1) == 1
            assert exits == [None] and rp.closed
            assert not cm.is_pty_in_grace("m-away")
        finally:
            remote_pty._remote_ptys.clear()
            remote_pty._away_since.clear()

    @pytest.mark.asyncio
    async def test_the_reconcile_feeds_the_held_lines_to_a_claimed_terminal_and_names_the_lost(
            self, monkeypatch):
        cm = sc.SatelliteConnectionManager()
        monkeypatch.setattr(sc, "_connection_manager", cm)
        satellite_link.reset_hooks()
        fed, lost = [], []
        claimed = SimpleNamespace(target="m-1", feed_transcript_lines=fed.extend, chat_id="c1", turn_open=False)
        gone = SimpleNamespace(target="m-1", chat_id="c-gone", turn_open=True, feed_transcript_lines=list)
        live = {"t-gone": gone}
        monkeypatch.setattr(interactive_session, "get", live.get)

        async def claim(machine_id, ids):
            live["t-new"] = claimed
            return {"t-new"}

        async def on_lost(machine_id, pairs):
            lost.extend(pairs)
        satellite_link.add_terminal_claim(claim, name="t")
        satellite_link.add_terminals_lost_hook(on_lost, name="t")
        try:
            # Lines for a terminal nobody holds yet are kept per machine, bounded.
            await cm._frame_transcript_lines("m-1", {"session_id": "t-new", "lines": ["a", "b"]})
            await cm._frame_transcript_lines("m-1", {"session_id": "t-other", "lines": ["x"]})
            assert cm._held_lines["m-1"].lines == {("terminal", "t-new"): ["a", "b"], ("terminal", "t-other"): ["x"]}
            # A held terminal with a turn open that the machine no longer runs.
            exits = []
            remote_pty.adopt_remote_pty(machine_id="m-1", session_id="t-gone", rows=24, cols=80,
                                        on_output=_ignore, on_exit=exits.append)
            remote_pty.machine_away("m-1")
            await cm._reconcile_ptys("m-1", ["t-new"])
            for _ in range(10):
                await asyncio.sleep(0)
            assert fed == ["a", "b"]
            # A terminal the report does not name waits for the window's end.
            assert cm._held_lines["m-1"].lines == {("terminal", "t-other"): ["x"]}
            assert exits == [None] and [(t.session_id, t.chat_id) for t in lost] == [("t-gone", "c-gone")]
            assert not remote_pty.is_machine_away("m-1")
        finally:
            satellite_link.reset_hooks()
            remote_pty._remote_ptys.clear()
            remote_pty._away_since.clear()

    @pytest.mark.asyncio
    async def test_a_terminal_taken_back_keeps_its_open_and_an_orphans_close_names_it(self, monkeypatch):
        """The machine's report names each terminal's open: the one a claim
        takes back hears only that open's exit, and the close of an orphan
        names it, so a terminal started again under the id is never closed."""
        cm = sc.SatelliteConnectionManager()
        monkeypatch.setattr(sc, "_connection_manager", cm)
        satellite_link.reset_hooks()
        sent: list = []

        async def send(machine_id, frame):
            sent.append(frame)
        monkeypatch.setattr(cm, "send_fire_and_forget", send)
        exits: list = []

        async def claim(machine_id, ids):
            remote_pty.adopt_remote_pty(machine_id=machine_id, session_id="t-mine", rows=24, cols=80,
                                        on_output=_ignore, on_exit=exits.append)
            return {"t-mine"}
        satellite_link.add_terminal_claim(claim, name="t")
        monkeypatch.setattr(interactive_session, "get", lambda sid: None)
        try:
            await cm._reconcile_ptys("m-1", ["t-mine", "t-orphan"],
                                     {"t-mine": "aa11", "t-orphan": "bb22", "t-elsewhere": "cc33"})
            assert remote_pty._remote_ptys[("m-1", "t-mine")].open_token == "aa11"
            assert sent == [{"type": "pty_close", "session_id": "t-orphan", "open_token": "bb22"}]
            remote_pty.feed_exit("m-1", "t-mine", 0, "zz99")
            assert exits == []
            remote_pty.feed_exit("m-1", "t-mine", 0, "aa11")
            assert exits == [0]
            # A report with no opens (a machine below 0.5.171 never signs in now,
            # a test's stand-in) closes by id as before.
            sent.clear()
            await cm._reconcile_ptys("m-1", ["t-plain"])
            assert sent == [{"type": "pty_close", "session_id": "t-plain"}]
        finally:
            satellite_link.reset_hooks()
            remote_pty._remote_ptys.clear()
            remote_pty._reported_opens.clear()

    @pytest.mark.asyncio
    async def test_the_frame_hands_its_offset_to_the_terminal_and_acks_nothing_itself(self, monkeypatch):
        """The ack waits for the rows (``_persist_forwarded_lines``): the read
        loop only passes the frame's offset on."""
        cm = sc.SatelliteConnectionManager()
        sent: list = []

        async def send(machine_id, frame):
            sent.append((machine_id, frame))
        monkeypatch.setattr(cm, "send_fire_and_forget", send)
        fed: list = []
        sess = SimpleNamespace(target="m-1", feed_transcript_lines=lambda lines, ack_offset=None:
                               fed.append((lines, ack_offset)))
        monkeypatch.setattr(interactive_session, "get", lambda sid: sess if sid == "t-1" else None)
        await cm._frame_transcript_lines("m-1", {"session_id": "t-1", "lines": ["x"], "offset": 120})
        # Another machine's lines for it go nowhere, a frame with no offset passes none.
        await cm._frame_transcript_lines("m-2", {"session_id": "t-1", "lines": ["y"], "offset": 9})
        await cm._frame_transcript_lines("m-1", {"session_id": "t-1", "lines": ["z"], "offset": True})
        assert fed == [(["x"], 120), (["z"], None)] and sent == []

    @pytest.mark.asyncio
    async def test_the_hold_is_bounded(self):
        """A terminal's lines share the machine's bounds with its sessions'."""
        from core.remote import satellite_grace as sg
        cm = sc.SatelliteConnectionManager()
        cm.hold_transcript_lines("m", "t1", ["x"] * (sg._HELD_LINES_EACH + 5))
        assert len(cm._held_lines["m"].lines[(sg.TERMINAL, "t1")]) == sg._HELD_LINES_EACH
        big = "x" * (sg._HELD_SIZE // 4)
        for i in range(6):
            cm.hold_transcript_lines("m", f"t{i + 2}", [big])
        assert cm._held_lines["m"].size <= sg._HELD_SIZE and len(cm._held_lines["m"].lines) == 4
        cm._held_lines["m"].end()


class TestTheDrainDoesNotWaitForAnAwayMachine:
    def test_a_held_pump_and_an_away_terminal_are_not_open_work(self, monkeypatch):
        import core.session.session_manager as sm
        import startup_drain
        from core.events import stream_pump
        held = {"s-held"}
        monkeypatch.setattr(sm, "_get_remote_layer",
                            lambda: SimpleNamespace(is_session_grace_held=lambda sid: sid in held))
        pumps = {
            "c1": SimpleNamespace(is_done=False, source_type="chat", session_id="s-held"),
            "c2": SimpleNamespace(is_done=False, source_type="chat", session_id="s-live"),
        }
        monkeypatch.setattr(stream_pump, "_active_pumps", pumps)
        monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: False)
        assert startup_drain._turns() == 1
        terms = {
            "t-away": SimpleNamespace(alive=True, turn_open=True, question_parked=False, session_id="t-away",
                                      is_remote=True, target="m-away"),
            "t-here": SimpleNamespace(alive=True, turn_open=True, question_parked=False, session_id="t-here",
                                      is_remote=True, target="m-here"),
        }
        monkeypatch.setattr(interactive_session, "_sessions", terms)
        remote_pty.machine_away("m-away")
        try:
            assert startup_drain._terminals() == 1
        finally:
            remote_pty._away_since.clear()


class TestTheWakeCaptureOnAMachine:
    """A self-wake bracket read between turns on a machine lives as long
    as its session can still write a frame."""

    def _info(self, attached=True):
        from core.remote.remote_bg_commands import queue_frame_reader, stream_alive
        from core.remote.remote_session_info import RemoteSessionInfo
        info = RemoteSessionInfo(session_id="s-wake", machine_id="m-1", agent_name="a",
                                 execution_path="claude-code-cli", event_queue=asyncio.Queue())
        cm = SimpleNamespace(is_session_stream_attached=lambda mid, sid: attached)
        return info, queue_frame_reader(info), stream_alive(info, cm)

    def test_the_session_is_alive_while_open_running_and_reachable(self):
        info, _read, alive = self._info()
        assert alive()
        info.cli_dead = True
        assert not alive()
        info.cli_dead = False
        info.alive = False
        assert not alive()
        _info2, _read2, alive2 = self._info(attached=False)
        assert not alive2()

    @pytest.mark.asyncio
    async def test_the_end_sentinel_answers_at_once_and_marks_the_cli_dead(self):
        info, read, alive = self._info()
        info.event_queue.put_nowait(None)
        t0 = time.monotonic()
        assert await read(30.0) is None
        assert time.monotonic() - t0 < 1.0
        assert info.cli_dead and not alive()

    @pytest.mark.asyncio
    async def test_a_silent_tool_of_a_live_session_keeps_the_bracket_open(self, monkeypatch):
        """No frame for longer than the gap, then the result: the bracket
        closes on the result (no chat row here: the frames are consumed)."""
        from core.events import wake_capture
        monkeypatch.setattr(wake_capture, "WAKE_FRAME_GAP_S", 0.05)
        info, read, alive = self._info()
        init = {"type": "system", "subtype": "init", "session_id": "s-wake"}

        async def _late():
            await asyncio.sleep(0.2)
            info.event_queue.put_nowait({"type": "result", "subtype": "success", "result": "ok"})
        feeder = asyncio.ensure_future(_late())
        with_alive = await wake_capture.capture_wake_turn("s-wake", init, read, source="t", alive=alive)
        await feeder
        assert with_alive is False and info.event_queue.empty()
        # Without the liveness the same gap ends the bracket and leaves the result.
        feeder = asyncio.ensure_future(_late())
        await wake_capture.capture_wake_turn("s-wake", init, read, source="t")
        await feeder
        assert not info.event_queue.empty()




class TestTheEngineOwnTurnOnAMachine:
    """A turn Claude Code starts by itself between turns, on a machine: read
    as it happens when background work ended inside the turn before it, a
    running turn while it streams, and recorded above the next message when
    nobody read it."""

    SID = "s-own-turn"

    def _layer(self, info):
        from unittest.mock import AsyncMock, MagicMock

        from core.remote.remote_execution import RemoteExecutionLayer
        layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
        layer._cm = MagicMock()
        layer._cm.send_fire_and_forget = AsyncMock()
        layer._cm.is_session_stream_attached = lambda mid, sid: True
        layer._cm.is_connected = lambda mid: True
        layer._cm.satellite_streams_between_turns = lambda mid: False  # part A: an older satellite
        layer._sessions = {info.session_id: info}
        return layer

    def _info(self):
        from core.layers.cli.remote import ClaudeRemoteState
        from core.layers.cli.settle import SettleController
        from core.layers.cli.translator import ClaudeCLIEventTranslator
        from core.remote.remote_session_info import RemoteSessionInfo
        info = RemoteSessionInfo(session_id=self.SID, machine_id="m-1", agent_name="a",
                                 execution_path="claude-code-cli", event_queue=asyncio.Queue())
        translator = ClaudeCLIEventTranslator(self.SID)
        info.engine_state = ClaudeRemoteState(
            translator=translator, settle=SettleController(self.SID, 0, translator))
        return info

    @staticmethod
    def _race_frames() -> list[dict]:
        import json

        from tests._paths import PROXY_DIR
        path = PROXY_DIR / "tests" / "fixtures" / "cli_wake" / "probe-race.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @pytest.mark.asyncio
    async def test_the_recorded_race_reads_the_engines_review_after_the_turn(self):
        """probe-race.jsonl replayed through a turn: the background command
        ends inside the person's turn ("4"), and the CLI reviews it with a
        turn of its own right after the result. The stop keeps the machine
        sending, the turn's end keeps that review whole, and the reader
        between turns records it instead of leaving it for the next send."""
        from core.events import wake_capture
        from core.events.bg_command_state import (
            get_bg_command_registry,
            reset_bg_command_registry,
        )
        frames = self._race_frames()
        turn, review = frames[26:39], frames[39:51]
        assert turn[-1]["type"] == "result" and review[0]["subtype"] == "init"
        info = self._info()
        layer = self._layer(info)
        reset_bg_command_registry(self.SID)
        wake_capture.forget_session(self.SID)
        get_bg_command_registry(self.SID).register_spawn("b30z979fj", "toolu_01LGut4yDYK8BayxMMPGZBef")
        info.current_send_command_id = "cmd-race"
        for frame in [*turn, *review, {"type": "_turn_ended", "command_id": "cmd-race"}]:
            info.event_queue.put_nowait(frame)
        try:
            async for _ in layer._adapter(info).stream_turn(info, layer._cm):
                pass
            stop = layer._cm.send_fire_and_forget.await_args.args[1]
            assert stop["type"] == "stop_turn" and stop["drain_bg"] is True
            assert info.carried and info.carried[0]["subtype"] == "init"
            await layer._adapter(info).drain_bg_commands(info, layer._cm, budget=0.5)
            assert wake_capture.recently_captured(self.SID)
            assert not info.carried and info.event_queue.empty()
        finally:
            reset_bg_command_registry(self.SID)
            wake_capture.forget_session(self.SID)

    @pytest.mark.asyncio
    async def test_the_engines_own_turn_is_a_running_turn_while_it_streams(self, temp_db):
        """While its pump is the chat's live turn: a running producer (a
        viewer's attach never reaps it), the stop-and-send flags of any turn,
        Stop's graceful interrupt and a queued message's interrupt. Its end
        gives the session back as it was."""
        from core.events.stream_pump import _active_pumps
        from core.remote.remote_bg_commands import capture_engine_turn
        from storage import database as task_store
        from ws import dashboard_chat_resume as resume
        cid = "chat-own-turn"
        task_store.create_chat(cid, "user-admin", "test-agent", "default",
                               model="claude-sonnet-5", execution_path="claude-code-cli")
        task_store.update_chat(cid, session_id=self.SID)
        info = self._info()
        info.current_send_command_id = "cmd-before"
        layer = self._layer(info)
        init = {"type": "system", "subtype": "init", "session_id": self.SID}
        capture = asyncio.ensure_future(capture_engine_turn(info, layer._cm, init, source="t"))
        for _ in range(100):
            if _active_pumps.get(cid) is not None:
                break
            await asyncio.sleep(0.01)
        pump = _active_pumps[cid]
        try:
            assert not pump.producer.done() and pump.records_engine_turn
            assert pump.stop_and_send == {"fired": False, "note": False}
            assert info.turn_active and info.current_send_command_id.startswith("engine-turn-")

            class _Viewer:
                def remote_stream_severed(self, sid):
                    return False

                def session_idle_seconds(self, sid):
                    return 0.1

                async def probe_session_process_dead(self, sid):
                    return False
            await resume.ChatResumeMixin._reap_wedged_pump(
                SimpleNamespace(layer=_Viewer()), cid, False, {})
            assert not pump._abort_requested

            assert await layer.interrupt_for_queued(self.SID) is True
            assert await layer.abort(self.SID) is True
            sent = [c.args[1]["type"] for c in layer._cm.send_fire_and_forget.await_args_list]
            assert sent == ["interrupt_turn", "interrupt_turn"]
        finally:
            # The CLI closes the interrupted turn with its own diagnostic.
            info.event_queue.put_nowait({
                "type": "result", "subtype": "error_during_execution", "is_error": True,
                "result": "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"})
            recorded = await asyncio.wait_for(capture, timeout=5)
        assert recorded is True
        assert not info.turn_active and info.current_send_command_id == "cmd-before"
        assert _active_pumps.get(cid) is None
        # The interruption it was asked for is no error of the engine: no
        # failed turn, so the message that interrupted it goes out next.
        endings = [r for r in task_store.get_chat_messages(cid) if "turn_ended" in (r.get("event_data") or "")]
        assert endings == []

    @pytest.mark.asyncio
    async def test_a_turn_the_turn_end_kept_is_recorded_before_the_next_message(self):
        """A turn that opened right after a result, kept by the turn's end,
        with nothing on the queue: the read before the next message records
        it, and the send's own drain finds nothing left."""
        from core.events import wake_capture
        info = self._info()
        layer = self._layer(info)
        wake_capture.forget_session(self.SID)
        info.carried = [{"type": "system", "subtype": "init", "session_id": self.SID},
                        {"type": "result", "subtype": "success", "result": "REVIEWED"}]
        try:
            await layer.record_waiting_turns(self.SID)
            assert wake_capture.recently_captured(self.SID)
            assert info.carried == [] and info.event_queue.empty()
        finally:
            wake_capture.forget_session(self.SID)

    @pytest.mark.asyncio
    async def test_the_saved_0245_lines_are_recorded_above_the_next_message(self, temp_db, caplog):
        """The operator's 02:45:07 drain, replayed from its saved census: 667
        lines waiting on the platform, one turn of the engine's own opening
        with its init after the tail of another whose opening line was lost.
        Read before the person's message is written, both turns are recorded
        above it and no line is dropped (the saved warning counted 559
        dropped stream lines and one result: that tail opens a record now)."""
        import logging

        from storage import database as task_store
        cid = "chat-own-0245"
        task_store.create_chat(cid, "user-admin", "test-agent", "default",
                               model="claude-sonnet-5", execution_path="claude-code-cli")
        task_store.update_chat(cid, session_id=self.SID)
        info = self._info()
        layer = self._layer(info)
        delta = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": "x"}}}
        said = {"type": "assistant", "message": {"role": "assistant",
                                                 "content": [{"type": "text", "text": "x"}]}}
        task = {"type": "system", "subtype": "task_progress", "task_id": "b-op"}
        tool = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_op", "content": "ok"}]}}
        status = {"type": "system", "subtype": "status"}
        ended = {"type": "result", "subtype": "success", "result": "earlier"}
        tail = ([delta, said, task, tool, status] + [delta] * 558 + [said] * 20 + [task] * 16
                + [tool] * 7 + [status] * 59 + [ended])
        review = [{"type": "system", "subtype": "init", "session_id": self.SID},
                  {"type": "assistant", "message": {"role": "assistant", "content": [
                      {"type": "text", "text": "REVIEWED"}]}},
                  {"type": "result", "subtype": "success", "result": "REVIEWED"}]
        assert len(tail) + 1 == 667
        for frame in [*tail, *review]:
            info.event_queue.put_nowait(frame)
        with caplog.at_level(logging.WARNING, logger="otodock"):
            await layer.record_waiting_turns(self.SID)
        assert "stale lines" not in caplog.text
        task_store.add_chat_message(cid, "user", "the next message", author_sub="user-admin")
        rows = task_store.get_chat_messages(cid)
        order = [(r["role"], r.get("event_type") or "", r.get("content") or "") for r in rows]
        markers = [i for i, r in enumerate(order) if r[1] == "system"]
        replies = [i for i, r in enumerate(order) if r[0] == "assistant"]
        person = next(i for i, r in enumerate(order) if r[0] == "user")
        assert len(markers) == 2 and order[replies[-1]][2] == "REVIEWED", order
        assert max(markers + replies) < person, order


class TestTheOneReaderBetweenTurns:
    """Part B: a line that reaches a session's queue with no turn reading it
    starts the one reader between turns, which records a turn the engine
    started by itself as it happens, whatever background work the platform
    knew of, and reads nothing else away from a turn or a task's producer."""

    SID = "s-reader"
    CID = "chat-reader"

    def _layer(self, caps: dict | None = None, path: str = "claude-code-cli"):
        from unittest.mock import AsyncMock

        from core.remote.remote_execution import RemoteExecutionLayer
        from core.remote.remote_session_info import RemoteSessionInfo
        from core.remote.satellite_connection import (
            SatelliteConnection,
            SatelliteConnectionManager,
        )
        cm = SatelliteConnectionManager()
        conn = SatelliteConnection(machine_id="m-1", ws=None, satellite_version="0.5.195")
        conn.capabilities = dict(caps if caps is not None else {"between_turns_stream": True})
        cm._connections["m-1"] = conn
        cm.send_fire_and_forget = AsyncMock()
        cm.send_command = AsyncMock(return_value={"steered": True})
        layer = RemoteExecutionLayer(cm)
        queue = cm.create_session_queue("m-1", self.SID, path)
        info = RemoteSessionInfo(session_id=self.SID, machine_id="m-1", agent_name="a",
                                 execution_path=path, event_queue=queue, files_synced=False)
        layer._sessions[self.SID] = info
        return layer, cm, info

    @staticmethod
    def _turn(text: str, *, init: bool = True) -> list[dict]:
        lines = [{"type": "system", "subtype": "init"}] if init else []
        return lines + [
            {"type": "stream_event", "event": {"type": "message_start"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
            {"type": "result", "subtype": "success", "result": text},
        ]

    async def _send(self, cm, frames: list[dict]) -> None:
        for frame in frames:
            await cm._frame_session_event("m-1", {"session_id": self.SID, "event": frame})
            await asyncio.sleep(0)

    @staticmethod
    async def _settle(info) -> None:
        for _ in range(300):
            if (info.reader is None or info.reader.done()) and info.event_queue.empty():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("the reader did not end")

    def _chat(self):
        from storage import database as task_store
        task_store.create_chat(self.CID, "user-admin", "test-agent", "default",
                               model="claude-sonnet-5", execution_path="claude-code-cli")
        task_store.update_chat(self.CID, session_id=self.SID)
        return task_store

    @pytest.mark.asyncio
    async def test_the_operators_turns_are_recorded_live_with_no_monitor(self, temp_db):
        """The 02:42 to 02:45 shape with nothing the platform knew of: a turn
        that opens with its init, one that opens with no init (as Claude Code
        2.1.312 recorded), and another: each is recorded as it arrives, as
        the agent's own turn, in order. No monitor runs here."""
        task_store = self._chat()
        layer, cm, info = self._layer()
        await self._send(cm, self._turn("ONE"))
        await self._settle(info)
        await self._send(cm, [{"type": "system", "subtype": "task_notification", "task_id": "b-x"}])
        await self._send(cm, self._turn("TWO", init=False))
        await self._settle(info)
        await self._send(cm, self._turn("THREE"))
        await self._settle(info)
        rows = task_store.get_chat_messages(self.CID)
        said = [r["content"] for r in rows if r["role"] == "assistant"]
        markers = [r for r in rows if r.get("event_type") == "system" and "bg_wake" in (r.get("event_data") or "")]
        assert said == ["ONE", "TWO", "THREE"] and len(markers) == 3

    @pytest.mark.asyncio
    async def test_after_the_platforms_hard_stop_the_dying_turns_lines_open_nothing(self, temp_db):
        task_store = self._chat()
        layer, cm, info = self._layer()
        info.proxy_killed = True
        await self._send(cm, self._turn("DYING", init=False))
        await self._settle(info)
        assert not [r for r in task_store.get_chat_messages(self.CID) if r["role"] == "assistant"]

    @pytest.mark.asyncio
    async def test_a_sub_agents_lines_open_no_turn(self, temp_db):
        """A background sub-agent streams its own lines between turns
        (``parent_tool_use_id``): they are its work, read as background,
        never a turn of the main agent."""
        task_store = self._chat()
        layer, cm, info = self._layer()
        sub = [{**line, "parent_tool_use_id": "toolu_sub"} for line in self._turn("SUB", init=False)]
        await self._send(cm, sub)
        await self._settle(info)
        assert not [r for r in task_store.get_chat_messages(self.CID) if r["role"] == "assistant"]

    @pytest.mark.asyncio
    async def test_one_reader_at_a_time(self):
        """Lines that come while the reader waits for the session (a turn holds
        it) start no second reader."""
        layer, cm, info = self._layer()
        async with info.lock:
            await self._send(cm, [{"type": "rate_limit_event"}])
            first = info.reader
            await self._send(cm, [{"type": "rate_limit_event"}])
            assert first is not None and info.reader is first and not first.done()
        await self._settle(info)

    @pytest.mark.asyncio
    async def test_a_turn_that_opens_as_the_turn_ends_is_read_after_it(self, monkeypatch, temp_db):
        """The engine's own turn opens right after a turn's result, inside the
        window the turn's end reads: kept, and the reader that the turn's end
        wakes records it (the next message never meets it)."""
        task_store = self._chat()
        layer, cm, info = self._layer()

        async def begin_turn(_info, _settle):
            from core.layers.cli.remote import ClaudeRemoteState
            from core.layers.cli.settle import SettleController
            from core.layers.cli.translator import ClaudeCLIEventTranslator
            tr = ClaudeCLIEventTranslator(self.SID)
            _info.engine_state = ClaudeRemoteState(translator=tr, settle=SettleController(self.SID, 0, tr))
        monkeypatch.setattr(layer._adapter(info), "begin_turn", begin_turn)

        async def send_command(_machine, frame, *, command_id="", **_kw):
            if frame.get("type") == "send_message":
                for line in self._turn("MINE") + self._turn("OWN"):
                    info.event_queue.put_nowait(line)
                info.event_queue.put_nowait({"type": "_turn_ended", "command_id": command_id})
            return {}
        cm.send_command = send_command
        async for _ in layer._send_turn(self.SID, "hello"):
            pass
        await self._settle(info)
        said = [r["content"] for r in task_store.get_chat_messages(self.CID) if r["role"] == "assistant"]
        assert said == ["OWN"]

    @pytest.mark.asyncio
    async def test_the_reader_ends_when_quiet_and_keeps_no_idle_session_open(self):
        from core.events.pump_bg_monitors import hold_bg_monitors
        layer, cm, info = self._layer()
        before = info.last_activity
        layer._line_arrived(self.SID)  # nothing waits: no reader
        assert info.reader is None and info.last_activity == before
        await self._send(cm, [{"type": "rate_limit_event"}])
        await self._settle(info)
        assert info.reader.done() and info.last_activity > before
        quiet = info.last_activity
        await asyncio.sleep(0.6)
        assert info.last_activity == quiet  # nothing read, nothing kept
        # A task run's producer reads its own session: no reader.
        info.reader = None
        with hold_bg_monitors(self.SID):
            await self._send(cm, [{"type": "rate_limit_event"}])
            assert info.reader is None
        info.event_queue.get_nowait()
        # A turn reading the session: no reader either.
        info.turn_active = True
        await self._send(cm, [{"type": "rate_limit_event"}])
        assert info.reader is None

    @pytest.mark.asyncio
    async def test_an_engine_that_reads_its_own_queue_gets_no_reader(self):
        layer, cm, info = self._layer(path="codex-cli")
        await self._send(cm, [{"method": "item/started", "params": {}}])
        assert info.reader is None and not info.event_queue.empty()

    @pytest.mark.asyncio
    async def test_a_message_is_steered_into_the_engines_turn_on_a_satellite_that_takes_it(self, temp_db):
        """With the capability the steer goes into the engine's own turn, and
        when the CLI takes it after that turn's result as a turn of its own,
        the record reads it through: one record, both answers. An older
        satellite takes no steer there: the message takes stop-and-send."""
        task_store = self._chat()
        layer, cm, info = self._layer()
        await self._send(cm, self._turn("OWN")[:2])
        for _ in range(100):
            if info.engine_turn is not None:
                break
            await asyncio.sleep(0.01)
        assert await layer.steer(self.SID, "also this") is True
        assert cm.send_command.await_args.args[1]["type"] == "steer_turn"
        await self._send(cm, self._turn("OWN")[2:] + self._turn("STEERED"))
        await self._settle(info)
        rows = task_store.get_chat_messages(self.CID)
        markers = [r for r in rows if r.get("event_type") == "system" and "bg_wake" in (r.get("event_data") or "")]
        assert [r["content"] for r in rows if r["role"] == "assistant"] == ["OWN", "STEERED"]
        assert len(markers) == 1

    @pytest.mark.asyncio
    async def test_no_second_steer_once_the_engines_turn_reached_its_result(self, temp_db):
        """After the engine's own turn reached its result (its record waits a
        moment for the turn a first steer may open), a message is no steer:
        it would start a turn nobody reads."""
        self._chat()
        layer, cm, info = self._layer()
        await self._send(cm, self._turn("OWN")[:2])
        for _ in range(100):
            if info.engine_turn is not None:
                break
            await asyncio.sleep(0.01)
        assert await layer.steer(self.SID, "first") is True
        await self._send(cm, self._turn("OWN")[2:])
        for _ in range(100):
            if info.engine_turn is not None and info.engine_turn.result_seen:
                break
            await asyncio.sleep(0.01)
        assert await layer.steer(self.SID, "second") is False
        assert cm.send_command.await_count == 1
        await self._settle(info)

    @pytest.mark.asyncio
    async def test_an_older_satellite_takes_no_steer_into_the_engines_turn(self, temp_db):
        self._chat()
        layer, cm, info = self._layer(caps={})
        await self._send(cm, self._turn("OWN")[:2])
        for _ in range(100):
            if info.engine_turn is not None:
                break
            await asyncio.sleep(0.01)
        assert await layer.steer(self.SID, "also this") is False
        cm.send_command.assert_not_awaited()
        await self._send(cm, self._turn("OWN")[2:])
        await self._settle(info)

    @pytest.mark.asyncio
    async def test_a_terminal_chats_lines_never_reach_the_reader(self):
        """A terminal chat's session has no headless record: its output goes
        through its own frames (``pty_output``, ``transcript_lines``), and a
        line under its id starts no reader and records nothing."""
        layer, cm, info = self._layer()
        layer._line_arrived("pty-session")
        await cm._frame_session_event("m-1", {"session_id": "pty-session", "event": self._turn("X")[0]})
        assert "pty-session" not in layer._sessions and info.reader is None

    @staticmethod
    def _placed(monkeypatch, sid: str, machine: str = "m-1", kind: str = "") -> None:
        """The security context a session has when the platform placed it
        on ``machine`` (``kind``: another placement)."""
        from auth.path_policy import SecurityContext
        from core import placement
        from core.session import session_state
        monkeypatch.setitem(session_state._session_security, sid, SecurityContext(
            role="editor", username="", agent="a", is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind=kind or placement.KIND_ADMIN_REMOTE,
                                                      machine_id=machine)))

    @pytest.mark.asyncio
    async def test_lines_read_while_the_platform_restarted_wait_for_the_session(self, monkeypatch, temp_db):
        """After a restart the satellite sends what it read meanwhile before
        its report of live sessions: those lines wait on the machine, and
        the adoption of the idle session reads them, in their place."""
        from core.remote import satellite_grace as sg
        from core.remote.remote_execution import RemoteExecutionLayer
        task_store = self._chat()
        layer, cm, info = self._layer()
        self._placed(monkeypatch, self.SID)
        del layer._sessions[self.SID]
        cm.remove_session_queue("m-1", self.SID)
        await self._send(cm, self._turn("WHILE AWAY"))
        assert len(cm._held_lines["m-1"].lines[(sg.SESSION, self.SID)]) == 4
        monkeypatch.setattr(RemoteExecutionLayer, "_restore_adopted_credentials",
                            staticmethod(lambda *a, **k: None))

        async def _no_gateway(*a):
            return None
        monkeypatch.setattr(RemoteExecutionLayer, "_reprovision_gateway", staticmethod(_no_gateway))
        assert await layer.adopt_idle_session(machine_id="m-1", session_id=self.SID, agent_name="a",
                                              execution_path="claude-code-cli")
        await self._settle(layer._sessions[self.SID])
        assert [r["content"] for r in task_store.get_chat_messages(self.CID)
                if r["role"] == "assistant"] == ["WHILE AWAY"]
        assert cm.take_held_lines("m-1", self.SID) == []

    @pytest.mark.asyncio
    async def test_a_line_waits_only_for_a_session_the_platform_placed_on_this_machine(self, monkeypatch):
        """The adoption takes back no other session, so no other line waits:
        none for an id the platform does not know, one placed on another
        machine, or one that runs on the platform itself."""
        from core import placement
        from core.remote import satellite_grace as sg
        _layer, cm, _info = self._layer()
        self._placed(monkeypatch, "s-here")
        self._placed(monkeypatch, "s-there", machine="m-2")
        self._placed(monkeypatch, "s-local", machine="", kind=placement.KIND_LOCAL)
        for sid in ("s-here", "s-there", "s-local", "s-unknown"):
            await cm._frame_session_event("m-1", {"session_id": sid, "event": {"type": "result"}})
        assert list(cm._held_lines["m-1"].lines) == [(sg.SESSION, "s-here")]

    @pytest.mark.asyncio
    async def test_held_lines_have_bounds_the_machine_cannot_raise(self, monkeypatch):
        """However many ids and lines a machine sends, the platform holds at
        most its number of sessions, its lines a session (the first ones, a
        turn's opening among them), its lines a machine and its size."""
        from core.remote import satellite_grace as sg
        _layer, cm, _info = self._layer()
        for i in range(sg._HELD_ENTRIES + 20):
            self._placed(monkeypatch, f"s-{i}")
            await cm._frame_session_event("m-1", {"session_id": f"s-{i}", "event": {"n": 0}})
        held = cm._held_lines["m-1"]
        assert len(held.lines) == sg._HELD_ENTRIES and (sg.SESSION, f"s-{sg._HELD_ENTRIES}") not in held.lines
        for n in range(1, sg._HELD_LINES_EACH + 500):
            await cm._frame_session_event("m-1", {"session_id": "s-0", "event": {"n": n}})
        mine = held.lines[(sg.SESSION, "s-0")]
        assert len(mine) == sg._HELD_LINES_EACH and mine[0] == {"n": 0} and mine[-1] == {"n": sg._HELD_LINES_EACH - 1}
        for i in range(1, sg._HELD_LINES // sg._HELD_LINES_EACH + 1):
            for n in range(1, sg._HELD_LINES_EACH):
                await cm._frame_session_event("m-1", {"session_id": f"s-{i}", "event": {"n": n}})
        assert held.count == sg._HELD_LINES == sum(len(x) for x in held.lines.values())

    @pytest.mark.asyncio
    async def test_held_lines_stop_at_their_size(self, monkeypatch):
        from core.remote import satellite_grace as sg
        _layer, cm, _info = self._layer()
        self._placed(monkeypatch, "s-big")
        big = {"text": "x" * (sg._HELD_SIZE // 3)}
        for _ in range(3):
            await cm._frame_session_event("m-1", {"session_id": "s-big", "event": big})
        held = cm._held_lines["m-1"]
        assert len(held.lines[(sg.SESSION, "s-big")]) == 2 and held.size <= sg._HELD_SIZE

    @pytest.mark.asyncio
    async def test_the_window_ends_with_no_further_line(self, monkeypatch):
        """The held lines go when the window closes, whether or not another
        line comes, and nothing is held after it."""
        from core.remote import satellite_grace as sg
        monkeypatch.setattr(sg, "HELD_LINES_S", 0.05)
        _layer, cm, _info = self._layer()
        self._placed(monkeypatch, "s-late")
        await cm._frame_session_event("m-1", {"session_id": "s-late", "event": {"type": "result"}})
        held = cm._held_lines["m-1"]
        assert held.lines
        await asyncio.sleep(0.15)
        assert held.ended and held.lines == {}
        await cm._frame_session_event("m-1", {"session_id": "s-late", "event": {"type": "result"}})
        assert held.lines == {} and cm.take_held_lines("m-1", "s-late") == []
