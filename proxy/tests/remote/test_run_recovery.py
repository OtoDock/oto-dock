"""Mode C — proxy-restart run recovery (services.scheduler.run_recovery).

Covers the startup park/fail split, the sessions_alive adopt/fail routing,
the deadline sweeper, and recovery eligibility — without a live satellite
(the connection manager + remote layer are faked).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import background
from services.scheduler import continue_pass, cut_turns, run_recovery
from storage import database as task_store
from storage.chat import cut_records, open_turns


@pytest.fixture(autouse=True)
def _clean_parked():
    run_recovery._parked.clear()
    cut_turns._parked.clear()
    yield
    run_recovery._parked.clear()
    cut_turns._parked.clear()
    # A recovery pump's end arms the chat's queue on the test's own loop; the
    # task set is module state, so a later test's gather over it must not
    # meet this loop's leftovers.
    from core.events import input_queue
    input_queue._tasks.clear()


@pytest.fixture
def kicked(monkeypatch):
    """The continue pass is asked for, not run: these tests stop at the
    record the report leaves it."""
    asked: list[int] = []
    monkeypatch.setattr(continue_pass, "kick", lambda: asked.append(1))
    return asked


def _record(chat_id: str) -> dict:
    with task_store.get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_cut_turns WHERE chat_id=%s ORDER BY id DESC LIMIT 1",
                           (chat_id,)).fetchone()
    return dict(row) if row else {}


def _mk_remote_run(temp_db, *, target="machine-1",
                   exec_path="claude-code-cli", status="running"):
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    chat_id = f"task-{run_id}"
    session_id = uuid.uuid4().hex
    task_store.create_run(run_id, "task-x", "pa", "manual", None, "do it")
    task_store.create_chat(chat_id, "user-1", "pa", "default",
                           model="m", execution_path=exec_path)
    task_store.update_chat(chat_id, session_id=session_id,
                           execution_target=target)
    task_store.update_run(run_id, status=status, chat_id=chat_id,
                          session_id=session_id)
    return run_id, chat_id, session_id


class TestDeferOrphanedRuns:
    def test_remote_cli_parked_local_failed(self, temp_db):
        remote_id, _, remote_sid = _mk_remote_run(temp_db)
        local_id, local_chat, _ = _mk_remote_run(temp_db, target="local")

        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 1 and failed == 0
        assert remote_sid in run_recovery._parked
        assert task_store.get_run(remote_id)["status"] == "running"
        # At a boot that takes nothing up the local run has a record the pass
        # gives the card, on this boot or the next.
        record = cut_records.unsettled_of(local_chat)
        assert (record["run_id"], record["cause"]) == (local_id, cut_records.CAUSE_NOT_TAKEN_UP)

    def test_codex_remote_is_parked_too(self, temp_db):
        # From 2.0 the machine keeps a Codex turn's notifications as it keeps
        # a Claude turn's events: its run waits for the machine's report.
        run_id, chat_id, sid = _mk_remote_run(temp_db, exec_path="codex-cli")
        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 1 and failed == 0
        assert sid in run_recovery._parked
        assert task_store.get_run(run_id)["status"] == "running"

    def test_empty_exec_path_resolves_to_agent_default_and_parks(self, temp_db):
        # Delegate worker chats never stamp execution_path (empty = agent
        # default). Eligibility must resolve the EFFECTIVE path — the literal
        # comparison silently excluded every delegate lane from Mode C, so a
        # deploy-restart mid-turn dropped the round's transcript (failed
        # "Proxy shutting down", turn blocks never flushed).
        run_id, _, sid = _mk_remote_run(temp_db, exec_path="")
        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 1 and failed == 0
        assert sid in run_recovery._parked
        assert task_store.get_run(run_id)["status"] == "running"

    def test_empty_exec_path_direct_default_agent_not_parked(self, temp_db):
        # The resolution consults the AGENT default — a Direct LLM agent's
        # empty-path chat stays out of Mode C (it runs on the platform).
        from storage.agents import agent_store
        agent_store.create_agent("direct-ag", "Direct Agent",
                                 created_by="user-1",
                                 execution_path="direct-llm")
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        chat_id = f"task-{run_id}"
        sid = uuid.uuid4().hex
        task_store.create_run(run_id, "task-x", "direct-ag", "manual", None, "x")
        task_store.create_chat(chat_id, "user-1", "direct-ag", "default",
                               model="m", execution_path="")
        task_store.update_chat(chat_id, session_id=sid,
                               execution_target="machine-1")
        task_store.update_run(run_id, status="running", chat_id=chat_id,
                              session_id=sid)
        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 0 and failed == 0
        assert sid not in run_recovery._parked


def _told(monkeypatch) -> list[str]:
    """The run ids the recovery reports."""
    from services.scheduler import scheduler
    said: list[str] = []

    async def _deliver(task, final, output, **kw):
        said.append(kw.get("run_id"))
    monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
    monkeypatch.setattr(task_store, "get_dynamic_task", lambda task_id: {"id": task_id})
    monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row)
    monkeypatch.setattr(run_recovery, "_REPORT_SPACING_S", 0.0)
    return said


def _listing(sid: str):
    """A complete report of machine-1 that lists ``sid`` beside what it replays."""
    from core.remote import satellite_link
    return satellite_link.AliveReport(
        machine_id="machine-1", headless=frozenset({sid}), terminals=frozenset(), complete=True,
        sessions=(), other_sessions=({"session_id": sid},), signed_in_at=time.monotonic() + 1, report_id="r1")


async def _settled() -> None:
    for owner in ("run_recovery", "cut_turns", "continue_pass"):
        for task in background.running(owner):
            await task


class TestARunOnAMachineWithNoReplay:
    """Codex: its run is not parked for a replay, and its record waits for a
    report that may never come."""

    def _cut(self, temp_db, *, catches_up: bool) -> tuple[str, str, str]:
        run_id, chat_id, sid = _mk_remote_run(temp_db, exec_path="codex-cli")
        open_turns.open_turn(chat_id, "tok", sid, kind="task", run_id=run_id, machine_id="machine-1")
        cut_turns.record_at_boot(clean_stop=False, catches_up=catches_up)
        run_recovery.defer_orphaned_runs(catches_up=catches_up)
        assert cut_records.unsettled_of(chat_id)["state"] == cut_records.WAITING
        return run_id, chat_id, sid

    @pytest.mark.parametrize("catches_up", [True, False])
    def test_a_machine_that_never_reports_ends_it_at_the_deadline_with_one_report(
            self, temp_db, monkeypatch, catches_up):
        told = _told(monkeypatch)
        run_id, _, sid = self._cut(temp_db, catches_up=catches_up)
        assert run_recovery._parked[sid]["run_id"] == run_id
        run_recovery._parked[sid]["deadline"] = 0.0
        asyncio.run(run_recovery.sweep_expired())
        assert task_store.get_run(run_id)["status"] == "failed" and told == [run_id]
        assert sid not in run_recovery._parked

    def test_a_late_report_after_the_deadline_tells_nobody_again(self, temp_db, monkeypatch):
        told = _told(monkeypatch)
        run_id, chat_id, sid = self._cut(temp_db, catches_up=True)
        run_recovery._parked[sid]["deadline"] = 0.0
        asyncio.run(run_recovery.sweep_expired())

        async def scene():
            await run_recovery.on_alive_report(_listing(sid))
            await _settled()
        asyncio.run(scene())
        assert told == [run_id] and cut_records.unsettled_of(chat_id) is None

    def test_a_report_that_lists_it_leaves_no_deadline_to_end_it_again(self, temp_db, monkeypatch):
        """The card says send a message, and a person's turn flips the row back
        to running: a deadline still counting would fail it under that turn."""
        told = _told(monkeypatch)
        run_id, chat_id, sid = self._cut(temp_db, catches_up=True)

        async def scene():
            await run_recovery.on_alive_report(_listing(sid))
            await _settled()
        asyncio.run(scene())
        assert told == [run_id] and sid not in run_recovery._parked
        assert task_store.update_latest_run_status_for_chat(
            chat_id, "running", only_from=("failed",)) == run_id
        asyncio.run(run_recovery.sweep_expired())
        assert told == [run_id] and task_store.get_run(run_id)["status"] == "running"


class TestARunAPersonsTurnFlippedBack:
    """A finished run's row reads ``running`` while a person's turn in its chat
    runs, and keeps its reason and end time."""

    def test_cut_on_a_machine_it_is_not_reported_again(self, temp_db, monkeypatch):
        told = _told(monkeypatch)
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        asyncio.run(run_recovery._fail_and_report(run_id, chat_id, run_recovery.RUN_CUT))
        assert told == [run_id]
        assert task_store.update_latest_run_status_for_chat(
            chat_id, "running", only_from=("failed",)) == run_id
        assert task_store.get_run(run_id)["error_message"] == run_recovery.RUN_CUT
        open_turns.open_turn(chat_id, "tok3", sid, kind="dashboard", person="user-1")

        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        parked, failed = run_recovery.defer_orphaned_runs(catches_up=True)
        assert (parked, failed) == (0, 1) and run_recovery._parked == {}

        async def scene():
            await run_recovery.report_failed_at_boot()
            await _settled()
        asyncio.run(scene())
        assert told == [run_id]
        assert task_store.get_run(run_id)["status"] == "failed"
        # The person's turn itself still waits for its machine, as a plain chat's.
        assert cut_turns.waiting(sid)


class TestRecoveryReadsTheSessionsOwnRecord:
    """Recovery keys a run on the machine its session's OWN record names,
    never its chat's pin alone (the compute machine keeps the pin ``local``
    while its session runs on it): the security index, then the open turn,
    then the pin only for a session nothing records."""

    @staticmethod
    def _record_on(monkeypatch, sid: str, kind: str, machine_id: str = "") -> None:
        from auth.path_policy import SecurityContext
        from core import placement
        from core.session import session_state
        ctx = SecurityContext(role="manager", username="u", agent="pa", is_admin_agent=False,
                              placement=placement.PlacementCapabilities(kind=kind, machine_id=machine_id))
        monkeypatch.setitem(session_state._session_security, sid, ctx)

    def test_a_run_pinned_local_whose_session_is_on_a_machine_is_parked_for_it(self, temp_db, monkeypatch):
        from core import placement
        run_id, chat_id, sid = _mk_remote_run(temp_db, target="local")
        self._record_on(monkeypatch, sid, placement.KIND_COMPUTE, "c-1")
        assert run_recovery.is_recovery_eligible(chat_id) is True
        parked, _failed = run_recovery.defer_orphaned_runs()
        assert parked == 1 and run_recovery._parked[sid]["machine_id"] == "c-1"
        assert task_store.get_run(run_id)["status"] == "running"

    def test_a_run_between_turns_pinned_to_a_machine_whose_session_ran_here_is_not_waited_for(self, temp_db, monkeypatch):
        """A round that fell back to the platform behind a machine's pin: its
        process died with the proxy, so no machine will ever report it."""
        from core import placement
        _run_id, chat_id, sid = _mk_remote_run(temp_db, target="machine-1")
        self._record_on(monkeypatch, sid, placement.KIND_LOCAL)
        assert run_recovery.is_recovery_eligible(chat_id) is False
        parked, _failed = run_recovery.defer_orphaned_runs()
        assert parked == 0 and sid not in run_recovery._parked

    def test_a_run_cut_mid_turn_behind_a_machines_pin_is_not_waited_for_either(self, temp_db, monkeypatch):
        """The same run with its open-turn row, as a running run nearly always
        has, whose row names no machine since its context named none: the boot's
        record and the park give one answer, so no machine is waited for."""
        from core import placement
        run_id, chat_id, sid = _mk_remote_run(temp_db, target="machine-1")
        self._record_on(monkeypatch, sid, placement.KIND_LOCAL)
        open_turns.open_turn(chat_id, "tok-1", sid, run_id=run_id, machine_id="")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        parked, _failed = run_recovery.defer_orphaned_runs()
        record = cut_records.unsettled_of(chat_id)
        assert (record["state"], record["machine_id"]) == (cut_records.PENDING, "")
        assert parked == 0 and sid not in run_recovery._parked and not cut_turns.waiting(sid)

    def test_a_turn_whose_row_names_no_machine_waits_for_the_one_its_record_names(self, temp_db, monkeypatch):
        """A row with no machine reads the session's own record, not the pin:
        a pin on the platform's place with a session on a machine waits for it."""
        from core import placement
        run_id, chat_id, sid = _mk_remote_run(temp_db, target="local")
        self._record_on(monkeypatch, sid, placement.KIND_COMPUTE, "c-1")
        open_turns.open_turn(chat_id, "tok-1", sid, run_id=run_id, machine_id="")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        record = cut_records.unsettled_of(chat_id)
        assert (record["state"], record["machine_id"]) == (cut_records.WAITING, "c-1")

    def test_a_row_no_record_explains_still_falls_to_the_chats_pin(self, temp_db):
        """A row an older release left, a session with no context: a chat on a
        machine waits for it rather than be continued beside it."""
        run_id, chat_id, sid = _mk_remote_run(temp_db, target="machine-1")
        open_turns.open_turn(chat_id, "tok-1", sid, run_id=run_id, machine_id="")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        record = cut_records.unsettled_of(chat_id)
        assert (record["state"], record["machine_id"]) == (cut_records.WAITING, "machine-1")

    def test_the_open_turn_names_the_machine_once_the_index_is_gone(self, temp_db):
        from core.session import session_state
        _run_id, chat_id, sid = _mk_remote_run(temp_db, target="local")
        open_turns.open_turn(chat_id, "tok-1", session_id=sid, machine_id="machine-9")
        assert session_state.placed_on(sid, chat_id) == "machine-9"
        assert run_recovery.is_recovery_eligible(chat_id) is True

    def test_a_record_of_a_machine_with_no_id_is_not_the_proxy(self, temp_db, monkeypatch):
        """A remote kind with no machine id (a meeting participant's context has
        that shape) names no machine, as ``_placed_here`` reads it: the open
        turn, then the pin decide, as 1.9.1 did."""
        from core import placement
        from core.session import session_state
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        self._record_on(monkeypatch, sid, placement.KIND_ADMIN_REMOTE)
        assert run_recovery._placed_here(session_state.get_session_security(sid), "machine-1") is True
        assert session_state.placed_on(sid, chat_id) is None
        assert run_recovery.is_recovery_eligible(chat_id) is True
        open_turns.open_turn(chat_id, "tok-1", sid, run_id=run_id, machine_id="machine-9")
        assert session_state.placed_on(sid, chat_id) == "machine-9"

    def test_another_sessions_open_turn_says_nothing_of_this_one(self, temp_db):
        from core.session import session_state
        run_id, chat_id, sid = _mk_remote_run(temp_db, target="local")
        open_turns.open_turn(chat_id, "tok-1", "another-session", run_id=run_id, machine_id="machine-9")
        assert session_state.placed_on(sid, chat_id) is None
        assert session_state.placed_on("another-session", chat_id) == "machine-9"
        assert run_recovery.is_recovery_eligible(chat_id) is False

    def test_a_session_nothing_records_falls_back_to_the_pin(self, temp_db):
        from core.session import session_state
        _run_id, chat_id, sid = _mk_remote_run(temp_db, target="machine-1")
        assert session_state.placed_on(sid, chat_id) is None
        assert run_recovery.is_recovery_eligible(chat_id) is True
        _run_id, chat_id, sid = _mk_remote_run(temp_db, target="local")
        assert run_recovery.is_recovery_eligible(chat_id) is False


class TestIsRecoveryEligible:
    def test_empty_exec_path_remote_chat_is_eligible(self, temp_db):
        # Mirrors the shutdown guard: a delegate lane (execution_target set,
        # execution_path empty → resolves to claude-code-cli) must be LEFT
        # RUNNING at graceful shutdown for satellite re-adopt.
        _, chat_id, _ = _mk_remote_run(temp_db, exec_path="")
        assert run_recovery.is_recovery_eligible(chat_id) is True

    def test_local_chat_not_eligible(self, temp_db):
        _, chat_id, _ = _mk_remote_run(temp_db, target="local")
        assert run_recovery.is_recovery_eligible(chat_id) is False


class _FakeLayer:
    def __init__(self):
        self._sessions = {}
        self.adopted = []
        self.idle_adopted = []
        self.idle_kwargs: dict[str, dict] = {}
        self.adopt_kwargs: dict[str, dict] = {}
        self.closed = []
        self.unadopted_closed = []
        self.incarnations: dict[str, str] = {}
        self.dropped: list[str] = []
        self.modes: dict[str, tuple[str, int]] = {}
        self.machines: dict[str, str] = {}
        self.severed: set[str] = set()
        self.spawning: set[str] = set()

    def owns_session(self, session_id):
        return session_id in self._sessions

    def is_spawning(self, session_id):
        return session_id in self.spawning

    async def prepare_resume(self, session_id):
        self._sessions.pop(session_id, None)
        self.machines.pop(session_id, None)
        self.severed.discard(session_id)
        self.dropped.append(session_id)

    async def adopt_session(self, *, machine_id, session_id, agent_name,
                            execution_path, command_id,
                            use_native_permissions=False, mode="", token_floor=0, **kw):
        from core.events.common_events import DONE, TEXT, CommonEvent
        self.adopted.append(session_id)
        self.adopt_kwargs[session_id] = {"execution_path": execution_path, **kw}
        self.modes[session_id] = (mode, token_floor)
        yield CommonEvent(type=TEXT, data={"content": "recovered answer"})
        yield CommonEvent(type=DONE, data={})

    async def adopt_idle_session(self, *, machine_id, session_id, agent_name,
                                 execution_path, use_native_permissions=False,
                                 mode="", token_floor=0, **kw):
        self.idle_adopted.append(session_id)
        self.idle_kwargs[session_id] = {"execution_path": execution_path, **kw}
        self.modes[session_id] = (mode, token_floor)
        self._sessions[session_id] = object()
        self.machines[session_id] = machine_id
        return True

    def session_ids_on(self, machine_id):
        return [sid for sid, m in self.machines.items() if m == machine_id]

    def remote_stream_severed(self, session_id):
        return session_id in self.severed

    async def close_session(self, session_id):
        self._sessions.pop(session_id, None)
        self.closed.append(session_id)

    async def close_unadopted(self, machine_id, session_id, incarnation=""):
        self.unadopted_closed.append(session_id)
        self.incarnations[session_id] = incarnation


@pytest.fixture
def placed(temp_db):
    """Give a session the security context the index would reload for it,
    placed on a machine, with the mode and floor the index kept; its person
    is a platform admin unless ``platform_role`` says otherwise."""
    from auth.path_policy import SecurityContext
    from core import placement
    from core.session import session_state
    sids: list[str] = []

    def _place(sid, machine="machine-1", *, mode="", floor=0, platform_role="admin"):
        sids.append(sid)
        sub = f"sub-{sid[:8]}"
        task_store.upsert_user(sub, f"{sub}@x.test", f"Person {sid[:8]}", platform_role)
        # The person existed before any session of theirs: a token minted
        # before the users row is a re-created person's and never current.
        with task_store.get_conn() as conn:
            conn.execute("UPDATE users SET created_at=%s WHERE sub=%s", ("2026-01-01T00:00:00+00:00", sub))
            conn.commit()
        session_state._session_security[sid] = SecurityContext(
            role="manager", username=task_store.get_username_by_sub(sub), agent="pa",
            is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind="remote", machine_id=machine))
        if mode or floor:
            session_state._reloaded_state[sid] = (mode, floor)

    yield _place
    for sid in sids:
        session_state._session_security.pop(sid, None)
        session_state._reloaded_state.pop(sid, None)
        session_state._starting.pop(sid, None)


async def _reported(machine_id: str, sessions: list[dict], other_sessions: list[dict] | None = None,
                    *, complete: bool | None = None) -> None:
    """One sign-in as ``satellite_link`` runs it: the adoption to its end, then
    the report. Complete when both lists came, unless the test says."""
    from core.remote import satellite_link
    await run_recovery.on_sessions_alive(machine_id, sessions, other_sessions)
    others = other_sessions or []
    await run_recovery.on_alive_report(satellite_link.AliveReport(
        machine_id=machine_id,
        headless=frozenset(e.get("session_id", "") for e in [*sessions, *others]),
        terminals=frozenset(),
        complete=other_sessions is not None if complete is None else complete,
        sessions=tuple(sessions),
        other_sessions=None if other_sessions is None else tuple(other_sessions),
        signed_in_at=time.monotonic(),
        report_id="report-1",
    ))


class TestOnSessionsAlive:
    @pytest.mark.asyncio
    async def test_reported_run_adopted_and_completed(self, temp_db,
                                                      monkeypatch, placed):
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        run_recovery.defer_orphaned_runs()

        layer = _FakeLayer()
        monkeypatch.setattr(
            "core.session.session_manager._get_remote_layer", lambda: layer)
        # _recover_session imports these lazily; patch at source module.
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1",
        }])
        # The adoption task runs on the loop — let it finish.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if task_store.get_run(run_id)["status"] != "running":
                break
        assert layer.adopted == [sid]
        run = task_store.get_run(run_id)
        assert run["status"] == "completed"
        assert sid not in run_recovery._parked

    @pytest.mark.asyncio
    async def test_a_parked_run_a_complete_report_lacks_goes_to_the_pass(self, temp_db, kicked):
        """Only a complete report (both lists) says a session is gone. Then
        the run stays live and its record is the continue pass's, which
        reopens it: never failed here, never on a report that is not
        complete."""
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(chat_id, "tok", sid, kind="task", run_id=run_id)
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True)
        await _reported("machine-1", [])
        assert task_store.get_run(run_id)["status"] == "running"
        assert sid in run_recovery._parked and _record(chat_id)["state"] == cut_records.WAITING
        assert kicked == []
        await _reported("machine-1", [], [])
        assert task_store.get_run(run_id)["status"] == "running"
        assert sid not in run_recovery._parked and sid not in cut_turns._parked
        record = _record(chat_id)
        assert (record["state"], record["run_id"], record["machine_id"]) == (
            cut_records.PENDING, run_id, "machine-1")
        assert kicked == [1] and open_turns.list_open() == []

    @pytest.mark.asyncio
    async def test_idle_session_with_chat_re_leashed(self, temp_db,
                                                     monkeypatch, placed):
        """An idle (no turn) session with a chat row must be re-REGISTERED
        so the idle reaper can leash it — restart-orphaned idle sessions
        previously lived forever satellite-side and ate the machine's
        capacity ("at capacity — too many active sessions" on every new
        spawn) — in the mode and with the floor the index kept."""
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default", floor=1790000000)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False, "agent_slug": "pa",
        }])
        assert layer.idle_adopted == [sid]
        assert layer.closed == []          # has a chat → stays resumable
        assert layer.adopted == []         # no turn → no recovery pump
        assert layer.modes[sid] == ("default", 1790000000)

    @pytest.mark.asyncio
    async def test_idle_session_without_chat_closed(self, temp_db,
                                                    monkeypatch, placed):
        """A reported idle session with NO chat row is junk: its process is
        closed on the satellite and the context the index kept goes."""
        from core.session import session_state
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        sid = uuid.uuid4().hex
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == []
        assert layer.unadopted_closed == [sid]
        assert session_state.get_session_security(sid) is None

    @pytest.mark.asyncio
    async def test_a_session_closed_on_purpose_is_not_revived(self, temp_db, monkeypatch):
        """A close drops the session's context before it returns (an idle reap,
        offboarding, a sign-out, a close the offline satellite never got): a
        reported session without one is closed on the satellite, never
        re-leashed, and its token is never live again."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        assert not session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_a_context_of_another_machine_is_not_taken_by_this_one(
            self, temp_db, monkeypatch, placed):
        """Two satellites reporting one id: the machine the context names keeps
        it; the other's process is closed and the context is left alone."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, machine="machine-2")
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        assert session_state.get_session_security(sid) is not None
        assert sid not in session_state._starting

    @pytest.mark.asyncio
    async def test_an_older_index_falls_back_to_the_chats_mode(self, temp_db, monkeypatch, placed):
        """An index written before modes were persisted: a chat session comes
        back in its chat row's mode, never in the default ``auto``; an
        unattended (task) session in ``auto``."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        task_store.update_chat(chat_id, permission_mode="plan")
        placed(sid)
        session_state._sessions[sid] = {"client_type": "dashboard"}
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        try:
            await run_recovery.on_sessions_alive("machine-1", [{
                "session_id": sid, "turn_active": False,
            }])
            assert layer.modes[sid] == ("plan", 0)
            assert run_recovery._readopt_state("machine-1", sid, {"permission_mode": ""}) == ("ask", 0)
            session_state._sessions[sid] = {"client_type": "task"}
            assert run_recovery._readopt_state("machine-1", sid, {"permission_mode": "default"}) == ("auto", 0)
        finally:
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_turn_that_finished_during_the_restart_is_replayed(
            self, temp_db, monkeypatch, placed):
        """The shutdown marks a turn it left running; after the restart the
        satellite's retained turn is replayed into the chat even when it
        finished meanwhile (it reports no turn in flight)."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default")
        session_state.mark_recover_pending(sid)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False, "command_id": "c9",
            "buffered_events": 12,
        }])
        for _ in range(50):
            await asyncio.sleep(0.02)
            if layer.adopted:
                break
        assert layer.adopted == [sid] and layer.idle_adopted == []
        assert layer.modes[sid] == ("default", 0)
        assert session_state.take_recover_pending(sid) is False

    @pytest.mark.asyncio
    async def test_idle_session_already_registered_skipped(self, temp_db,
                                                           monkeypatch, placed):
        """A reconnect blip reports sessions the proxy still tracks (grace)
        — no double registration."""
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        layer = _FakeLayer()
        layer._sessions[sid] = object()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == []
        assert layer.closed == []


class TestTheDatabaseDecidesATakeback:
    """A crash can leave a closed session's record in the file (a close answers
    at 30 s with its removal queued): a Stop that closed a run's session and a
    sign out everywhere after its start never come back on it."""

    @staticmethod
    def _stopped(chat_id: str, sid: str, *, settled_at: str | None = None, kind: str = "task") -> None:
        from storage.chat import cut_records
        made = cut_records.add_settled(chat_id, cause=cut_records.CAUSE_STOP,
                                       state=cut_records.DROPPED,
                                       reason=cut_records.STOPPED_BY_PERSON, session_id=sid,
                                       kind=kind)
        if settled_at:
            from storage.pg import get_conn
            with get_conn() as conn:
                conn.execute("UPDATE chat_cut_turns SET settled_at=%s WHERE id=%s",
                             (settled_at, made["id"]))
                conn.commit()

    @staticmethod
    def _layer(monkeypatch):
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        return layer

    @pytest.mark.asyncio
    @pytest.mark.parametrize("turn_active", [False, True])
    async def test_a_session_a_stop_closed_never_comes_back_on_its_file(
            self, temp_db, monkeypatch, placed, turn_active):
        _, chat_id, sid = _mk_remote_run(temp_db, status="cancelled")
        placed(sid, mode="auto", floor=int(time.time()) - 60)
        self._stopped(chat_id, sid)
        assert run_recovery.lost_standing("machine-1", {sid: {}}) == {sid: run_recovery.LOST_STOPPED}
        layer = self._layer(monkeypatch)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": turn_active, "command_id": "c1"}])
        assert sid in layer.unadopted_closed
        assert layer.idle_adopted == [] and sid not in layer.adopted

    @pytest.mark.asyncio
    async def test_a_stop_before_the_session_started_or_one_that_closed_nothing_keeps_it(
            self, temp_db, monkeypatch, placed):
        """A record the Stop settled before this session's start is an older
        life of the id, and a run stopped with no record of its own closed no
        session (a later round of its lane may run on it)."""
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="auto", floor=int(time.time()))
        self._stopped(chat_id, sid, settled_at="2026-01-01T00:00:00+00:00")
        _, _chat2, kept = _mk_remote_run(temp_db, status="cancelled")
        placed(kept, mode="auto", floor=int(time.time()))
        # A person's terminal the stopped run drove: a Stop never closes it.
        _, chat3, terminal = _mk_remote_run(temp_db, status="cancelled")
        placed(terminal, mode="auto", floor=int(time.time()) - 60)
        self._stopped(chat3, terminal, kind="terminal")
        assert run_recovery.lost_standing("machine-1", {sid: {}, kept: {}, terminal: {}}) == {}

    @pytest.mark.asyncio
    async def test_a_sign_out_everywhere_after_the_start_declines_it(
            self, temp_db, monkeypatch, placed):
        from storage.identity import db_users
        _, _chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default", floor=int(time.time()) - 120)
        db_users.bump_token_epoch(f"sub-{sid[:8]}")
        assert run_recovery.lost_standing("machine-1", {sid: {}}) == {sid: run_recovery.LOST_SIGNED_OUT}
        layer = self._layer(monkeypatch)
        await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
        assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]

    def test_a_start_after_the_sign_out_or_an_unknown_start_is_kept(self, temp_db, placed):
        from storage.identity import db_users
        _, _c1, after = _mk_remote_run(temp_db, status="completed")
        _, _c2, unknown = _mk_remote_run(temp_db, status="completed")
        placed(after, mode="default", floor=int(time.time()) + 5)
        placed(unknown, mode="default")
        db_users.bump_token_epoch(f"sub-{after[:8]}")
        db_users.bump_token_epoch(f"sub-{unknown[:8]}")
        assert run_recovery.lost_standing("machine-1", {after: {}, unknown: {}}) == {}

    @pytest.mark.asyncio
    async def test_a_parked_run_whose_person_signed_out_is_not_adopted(
            self, temp_db, monkeypatch, placed):
        from storage.identity import db_users
        run_id, _chat_id, sid = _mk_remote_run(temp_db)
        placed(sid, mode="auto", floor=int(time.time()) - 120)
        run_recovery.defer_orphaned_runs()
        db_users.bump_token_epoch(f"sub-{sid[:8]}")
        layer = self._layer(monkeypatch)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1"}])
        assert sid in layer.unadopted_closed and sid not in layer.adopted
        assert task_store.get_run(run_id)["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_active", [True, False])
async def test_a_stop_whose_removal_a_crash_cut_is_not_taken_back(
        temp_db, monkeypatch, tmp_path, turn_active):
    """The narrow pass's case end to end (LOOP-WRITES' review, place 1): a
    person stops a waiting run, the close answers at its cap with the index's
    write still queued, the process dies before it lands, and the next boot
    reads the file with the session's record in it. The machine's report
    meets the database's own mark and the session is closed, not taken back."""
    import json
    import shutil
    import threading

    from auth.path_policy import SecurityContext
    from core import file_commit, placement
    from core.session import session_state
    from services.scheduler import stranded_runs
    from storage.chat import cut_records
    run_recovery._parked.clear()
    index = tmp_path / "security_index.json"
    monkeypatch.setattr(session_state, "_SECURITY_INDEX", index)
    monkeypatch.setattr(session_state, "_session_security", {})
    monkeypatch.setattr(session_state, "_session_security_ts", {})
    session_state._security_writer.reset_for_tests()
    session_state.reset_liveness_for_tests()
    run_id, chat_id, sid = _mk_remote_run(temp_db)
    cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                    session_id=sid, kind="task", run_id=run_id, machine_id="machine-1",
                    turn_token="tok-w")
    sub = f"sub-{sid[:8]}"
    task_store.upsert_user(sub, f"{sub}@x.test", f"Person {sid[:8]}", "admin")
    session_state._session_security[sid] = SecurityContext(
        role="manager", username=task_store.get_username_by_sub(sub), agent="pa",
        is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="machine-1"))
    session_state._session_security_ts[sid] = time.time()
    session_state._reloaded_state[sid] = ("auto", int(time.time()) - 60)
    session_state._save_session_security()
    assert await session_state.security_index_written()

    monkeypatch.setattr(run_recovery, "report_stopped", lambda _run: asyncio.sleep(0))
    monkeypatch.setattr(session_state, "SECURITY_WRITE_WAIT_S", 0.3)
    release = threading.Event()
    real = file_commit.atomic_replace

    def held(p, blob, *, fsync):
        if p == index:
            assert release.wait(20)
        return real(p, blob, fsync=fsync)
    monkeypatch.setattr(file_commit, "atomic_replace", held)
    assert await stranded_runs.stop(run_id) is True
    assert sid in json.loads(index.read_text())  # the removal did not land

    # The crash: the next boot reads the file as it is.
    boot = tmp_path / "boot" / "security_index.json"
    boot.parent.mkdir()
    shutil.copy2(index, boot)
    release.set()
    session_state._security_writer.reset_for_tests()
    monkeypatch.setattr(session_state, "_SECURITY_INDEX", boot)
    monkeypatch.setattr(session_state, "_session_security", {})
    monkeypatch.setattr(session_state, "_session_security_ts", {})
    session_state._reloaded_state.pop(sid, None)
    session_state.reset_liveness_for_tests()
    run_recovery._parked.clear()
    session_state.load_session_security()
    assert session_state.get_session_security(sid) is not None
    run_recovery.defer_orphaned_runs()
    layer = _FakeLayer()
    import core.session.session_manager as sm
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    await run_recovery.on_sessions_alive("machine-1", [{
        "session_id": sid, "turn_active": turn_active, "command_id": "c1",
        "execution_path": "claude-code-cli"}])
    await asyncio.sleep(0.2)
    assert layer.unadopted_closed == [sid]
    assert layer.adopted == [] and layer.idle_adopted == []
    assert task_store.get_run(run_id)["status"] == "cancelled"


class TestReadoptionGuards:
    @pytest.mark.asyncio
    async def test_every_reported_mark_is_read_and_a_chatless_one_closed(
            self, temp_db, monkeypatch, placed):
        """The replay marks of all reported sessions are read at once, a
        parked run's too; a marked session without a chat row is closed on
        its machine with its context, not left running unreaped."""
        from core.session import session_state
        run_id, chat_id, run_sid = _mk_remote_run(temp_db)
        placed(run_sid)
        run_recovery.defer_orphaned_runs()
        session_state.mark_recover_pending(run_sid)
        orphan = uuid.uuid4().hex
        placed(orphan)
        session_state.mark_recover_pending(orphan)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [
            {"session_id": run_sid, "turn_active": True, "command_id": "c1"},
            {"session_id": orphan, "turn_active": False},
        ])
        assert session_state.take_recover_pending(run_sid) is False
        assert session_state.take_recover_pending(orphan) is False
        assert orphan in layer.unadopted_closed
        assert session_state.get_session_security(orphan) is None
        for _ in range(50):
            await asyncio.sleep(0.02)
            if task_store.get_run(run_id)["status"] != "running":
                break

    @pytest.mark.asyncio
    async def test_a_person_who_lost_the_agent_does_not_get_the_session_back(
            self, temp_db, monkeypatch, placed):
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default", platform_role="member")  # no role on the agent now
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        assert session_state.get_session_security(sid) is None
        assert not session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_a_declined_session_is_ended_on_its_machine_before_its_context_goes(
            self, temp_db, monkeypatch, placed):
        """The machine just reported, so it is told first: it ends the process
        on the word, and a record a crash then leaves in the file is harmless
        (nothing reports the session again, and the next report is judged
        again from the database). The removal needs no wait."""
        from core.session import session_state
        _, _chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default", platform_role="member")  # no role on the agent now
        order: list[str] = []
        layer = _FakeLayer()

        async def written() -> bool:
            order.append("waited")
            return True

        async def close_unadopted(machine_id, session_id, incarnation=""):
            order.append("machine told" if session_state.get_session_security(sid) is not None
                         else "machine told after the removal")
        layer.close_unadopted = close_unadopted
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        monkeypatch.setattr(session_state, "security_index_written", written)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert order == ["machine told"]
        assert session_state.get_session_security(sid) is None

    @pytest.mark.asyncio
    async def test_an_expired_context_is_not_re_adopted(self, temp_db, monkeypatch, placed):
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default")
        session_state._session_security_ts[sid] = 0.0
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        try:
            await run_recovery.on_sessions_alive("machine-1", [{
                "session_id": sid, "turn_active": False,
            }])
            assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        finally:
            session_state._session_security_ts.pop(sid, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marked", [True, False])
    async def test_a_decline_of_the_claude_list_names_the_reported_incarnation(
            self, temp_db, monkeypatch, placed, marked):
        """A session the Claude list reports without a chat row is closed on
        its machine by the incarnation the report named (the active loop for
        a marked one, the idle loop otherwise), so a decision made on this
        report never closes an object a later start put under the id."""
        from core.session import session_state
        orphan = uuid.uuid4().hex
        placed(orphan)
        if marked:
            session_state.mark_recover_pending(orphan)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        try:
            await run_recovery.on_sessions_alive("machine-1", [
                {"session_id": orphan, "turn_active": False, "incarnation": "a4-2"},
            ])
            assert layer.unadopted_closed == [orphan]
            assert layer.incarnations[orphan] == "a4-2"
        finally:
            session_state.take_recover_pending(orphan)

    @pytest.mark.asyncio
    async def test_close_unadopted_names_the_incarnation_only_when_it_has_one(self):
        """The close frame carries the incarnation it was given and no key
        otherwise, so a satellite below 0.5.138 reads the frame it always
        did."""
        from core.remote.remote_execution import RemoteExecutionLayer
        cm = MagicMock()
        cm.send_command = AsyncMock()
        layer = RemoteExecutionLayer(cm)
        await layer.close_unadopted("machine-1", "sid-1", incarnation="a4-2")
        await layer.close_unadopted("machine-1", "sid-2")
        frames = [c.args[1] for c in cm.send_command.call_args_list]
        assert frames == [
            {"type": "close_session", "session_id": "sid-1", "incarnation": "a4-2"},
            {"type": "close_session", "session_id": "sid-2"},
        ]

    def test_another_machines_decline_keeps_the_owners_mark(self, temp_db, placed):
        from core.session import session_state
        sid = uuid.uuid4().hex
        placed(sid, machine="machine-2")
        session_state.mark_starting(sid, 60)
        run_recovery._clear_readopt_mark(sid, "machine-1")
        assert sid in session_state._starting
        run_recovery._clear_readopt_mark(sid, "machine-2")
        assert sid not in session_state._starting

    def test_a_reported_session_answers_in_its_kept_mode_before_adoption(
            self, temp_db, placed):
        from core.session import session_state
        sid = uuid.uuid4().hex
        placed(sid, mode="plan", floor=1790000000)
        assert session_state.get_session_mode(sid) == "plan"


def _codex_chat(temp_db, *, target="machine-1", model="gpt-6.1-sol", exec_path="codex-cli"):
    _, chat_id, sid = _mk_remote_run(temp_db, target=target, exec_path=exec_path,
                                     status="completed")
    task_store.update_chat(chat_id, model=model)
    return chat_id, sid


def _other(sid, **over):
    entry = {"session_id": sid, "execution_path": "codex-cli", "agent_slug": "pa",
             "alive": True, "turn_active": False, "incarnation": "b1-7",
             "resume_handle": "thread-9", "model": "gpt-6.1-sol",
             "mcp_servers": [], "use_native_permissions": False}
    entry.update(over)
    return entry


class TestOtherSessions:
    """The connect report's ``other_sessions``: a satellite's idle Codex
    session the platform still has a context and a chat for is taken back;
    one that is dead, mid-turn, closed on purpose or not this machine's is
    closed there (by the incarnation the report named)."""

    @pytest.fixture
    def layer(self, monkeypatch):
        import core.session.session_manager as sm
        lyr = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: lyr)
        return lyr

    @pytest.mark.asyncio
    async def test_an_idle_codex_session_is_taken_back(self, temp_db, layer, placed, monkeypatch):
        from storage import remote_store
        monkeypatch.setattr(remote_store, "get_remote_machine",
                            lambda mid: {"id": mid, "allow_full_fs": True})
        _, sid = _codex_chat(temp_db)
        placed(sid, mode="default", floor=1790000000)

        await run_recovery.on_sessions_alive("machine-1", [], [_other(sid)])

        assert layer.idle_adopted == [sid] and layer.unadopted_closed == []
        kw = layer.idle_kwargs[sid]
        assert kw["execution_path"] == "codex-cli" and kw["resume_handle"] == "thread-9"
        assert kw["model"] == "gpt-6.1-sol" and kw["allow_full_fs"] is True
        assert layer.modes[sid] == ("default", 1790000000)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope, taken", [("compute", True), ("admin", False)])
    async def test_a_chat_on_the_platforms_place_agrees_only_with_the_platforms_own_machine(
            self, temp_db, layer, placed, monkeypatch, scope, taken):
        """The pin ``local`` agrees with a session on a compute machine (the
        machine that serves the platform's place), never with another."""
        from storage import remote_store
        monkeypatch.setattr(remote_store, "get_remote_machine",
                            lambda mid: {"id": mid, "pairing_scope": scope, "allow_full_fs": False})
        _, sid = _codex_chat(temp_db, target="local")
        placed(sid, mode="default", floor=1790000000)
        await run_recovery.on_sessions_alive("machine-1", [], [_other(sid)])
        assert (layer.idle_adopted == [sid]) is taken
        assert (layer.unadopted_closed == []) is taken

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat_model, expected", [
        ("gpt-6-astra", "gpt-6-astra"),          # the engine serves it
        ("claude-fable-5-2", "gpt-6.1-terra"),   # foreign to it: the reported one
    ])
    async def test_the_chats_model_wins_when_it_is_the_engines(
            self, temp_db, layer, placed, monkeypatch, chat_model, expected):
        from storage.billing import subscription_store
        monkeypatch.setattr(subscription_store, "list_models", lambda layer=None: [
            {"model_id": "gpt-6-astra"}, {"model_id": "gpt-6.1-terra"}] if layer == "codex-cli" else [])
        _, sid = _codex_chat(temp_db, model=chat_model)
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [], [_other(sid, model="gpt-6.1-terra")])
        assert layer.idle_kwargs[sid]["model"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("over, why", [
        ({"alive": False}, "dead"),
        ({"turn_active": True}, "mid-turn"),
        ({"resume_handle": ""}, "no thread"),
        ({"execution_path": "acme-cli"}, "unknown engine"),
        ({"execution_path": "direct-llm"}, "an engine that is not taken back"),
    ])
    async def test_one_that_cannot_come_back_is_closed_by_its_incarnation(
            self, temp_db, layer, placed, over, why):
        _, sid = _codex_chat(temp_db)
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [], [_other(sid, **over)])
        assert layer.idle_adopted == [], why
        assert layer.unadopted_closed == [sid], why
        assert layer.incarnations[sid] == "b1-7"

    @pytest.mark.asyncio
    async def test_a_chat_on_another_engine_or_machine_is_not_taken(self, temp_db, layer, placed):
        _, sid_engine = _codex_chat(temp_db, exec_path="claude-code-cli")
        _, sid_machine = _codex_chat(temp_db, target="machine-2")
        _, sid_unpinned = _codex_chat(temp_db, target="")
        for sid in (sid_engine, sid_machine, sid_unpinned):
            placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [], [
            _other(sid_engine), _other(sid_machine), _other(sid_unpinned)])
        assert layer.idle_adopted == [sid_unpinned]
        assert sorted(layer.unadopted_closed) == sorted([sid_engine, sid_machine])

    @pytest.mark.asyncio
    async def test_no_context_or_no_chat_closes_it(self, temp_db, layer, placed):
        _, sid_closed = _codex_chat(temp_db)          # no context: closed on purpose
        sid_chatless = uuid.uuid4().hex
        placed(sid_chatless)
        await run_recovery.on_sessions_alive("machine-1", [], [
            _other(sid_closed), _other(sid_chatless)])
        assert layer.idle_adopted == []
        assert sorted(layer.unadopted_closed) == sorted([sid_closed, sid_chatless])

    @pytest.mark.asyncio
    async def test_held_or_spawning_is_left_alone(self, temp_db, layer, placed):
        _, sid_held = _codex_chat(temp_db)
        _, sid_spawn = _codex_chat(temp_db)
        for sid in (sid_held, sid_spawn):
            placed(sid)
        layer._sessions[sid_held] = object()
        layer.spawning.add(sid_spawn)
        await run_recovery.on_sessions_alive("machine-1", [], [
            _other(sid_held), _other(sid_spawn)])
        assert layer.idle_adopted == [] and layer.unadopted_closed == []

    @pytest.mark.asyncio
    async def test_an_id_in_both_lists_is_the_turn_replay_lists(self, temp_db, layer, placed):
        _, sid = _codex_chat(temp_db, exec_path="claude-code-cli")
        placed(sid, mode="default")
        await run_recovery.on_sessions_alive(
            "machine-1", [{"session_id": sid, "turn_active": False, "agent_slug": "pa",
                           "execution_path": "claude-code-cli"}], [_other(sid)])
        assert layer.idle_adopted == [sid]
        assert layer.idle_kwargs[sid]["execution_path"] == "claude-code-cli"

    @pytest.mark.asyncio
    async def test_a_report_without_the_list_takes_no_codex_step(self, temp_db, layer, placed):
        _, sid = _codex_chat(temp_db)
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [])
        assert layer.idle_adopted == [] and layer.unadopted_closed == []

    def test_the_reported_codex_session_is_live_until_its_adoption(self, temp_db, placed):
        from core.session import session_state
        sid = uuid.uuid4().hex
        placed(sid)
        run_recovery._mark_reported_starting({}, "machine-1", {sid: _other(sid)})
        assert sid in session_state._starting

    def test_a_local_context_is_never_a_satellites(self, temp_db):
        from auth.path_policy import SecurityContext
        from core import placement
        local = SecurityContext(role="manager", username="u", agent="pa", is_admin_agent=False,
                                placement=placement.LOCAL_PLACEMENT)
        unnamed_remote = SecurityContext(
            role="manager", username="u", agent="pa", is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind="admin_remote", machine_id=""))
        assert run_recovery._placed_here(local, "machine-1") is False
        assert run_recovery._placed_here(unnamed_remote, "machine-1") is True


class TestSeveredRecords:
    """A machine back after its reconnect grace expired: its records whose
    stream was severed are dropped (the record only), an idle one is taken
    back with what this process holds for it, a mid-turn one is left to its
    next turn's resume (nothing replayed twice)."""

    @pytest.fixture
    def layer(self, monkeypatch):
        import core.session.session_manager as sm
        lyr = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: lyr)
        return lyr

    @pytest.mark.asyncio
    async def test_an_idle_severed_record_is_taken_back_in_its_live_state(
            self, temp_db, layer, placed):
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid)
        layer._sessions[sid] = object()
        layer.machines[sid] = "machine-1"
        layer.severed.add(sid)
        session_state._session_modes[sid] = "plan"
        session_state._session_token_floor[sid] = 1795000000
        try:
            await run_recovery.on_sessions_alive("machine-1", [{
                "session_id": sid, "turn_active": False, "agent_slug": "pa",
                "execution_path": "claude-code-cli"}])
        finally:
            session_state._session_modes.pop(sid, None)
            session_state._session_token_floor.pop(sid, None)
        assert layer.dropped == [sid]
        assert layer.idle_adopted == [sid]
        assert layer.modes[sid] == ("plan", 1795000000)

    @pytest.mark.asyncio
    async def test_a_mid_turn_severed_record_is_dropped_and_not_replayed(
            self, temp_db, layer, placed):
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default")
        layer._sessions[sid] = object()
        layer.machines[sid] = "machine-1"
        layer.severed.add(sid)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1",
            "agent_slug": "pa", "execution_path": "claude-code-cli"}])
        await asyncio.sleep(0.05)
        assert layer.dropped == [sid]
        assert layer.adopted == [] and layer.idle_adopted == []
        assert layer.unadopted_closed == []

    @pytest.mark.asyncio
    async def test_a_record_being_spawned_is_not_dropped(self, temp_db, layer):
        layer._sessions["s-sp"] = object()
        layer.machines["s-sp"] = "machine-1"
        layer.severed.add("s-sp")
        layer.spawning.add("s-sp")
        await run_recovery.on_sessions_alive("machine-1", [])
        assert layer.dropped == []


class TestAParkedRunItsAdoptionDoesNotTake:
    """The adoption took the run out of the parked set. When it then does
    not adopt the turn, nothing else ends the run: it stayed ``running``
    until the next boot, and every follow-up of its lane waited behind it."""

    @pytest.fixture
    def failed(self, monkeypatch):
        ended: list[tuple] = []

        async def _fail(run_id, chat_id, reason, **kw):
            ended.append((run_id, chat_id, reason))
        monkeypatch.setattr(run_recovery, "_fail_and_report", _fail)
        return ended

    @pytest.mark.asyncio
    async def test_a_chat_that_streams_already_fails_the_run_with_its_report(
            self, temp_db, monkeypatch, failed):
        from types import SimpleNamespace

        from core.events import stream_pump
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        stream_pump._active_pumps[chat_id] = SimpleNamespace(is_done=False, session_id="another")
        try:
            await run_recovery._recover_session(
                "machine-1", sid, {"session_id": sid}, run_id=run_id, chat_id=chat_id, agent="pa")
        finally:
            stream_pump._active_pumps.pop(chat_id, None)
        assert failed == [(run_id, chat_id, run_recovery.RUN_SESSION_CLOSED)]
        assert layer.adopted == []

    @pytest.mark.asyncio
    async def test_no_remote_layer_fails_the_run_with_its_report(self, temp_db, monkeypatch, failed):
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: None)
        await run_recovery._recover_session(
            "machine-1", sid, {"session_id": sid}, run_id=run_id, chat_id=chat_id, agent="pa")
        assert failed == [(run_id, chat_id, run_recovery.RUN_SESSION_CLOSED)]

    @pytest.mark.asyncio
    async def test_a_plain_chats_turn_fails_no_run(self, temp_db, monkeypatch, failed):
        _run_id, chat_id, sid = _mk_remote_run(temp_db)
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: None)
        await run_recovery._recover_session(
            "machine-1", sid, {"session_id": sid}, run_id=None, chat_id=chat_id, agent="pa")
        assert failed == []

    @pytest.mark.asyncio
    async def test_a_run_a_person_stopped_meanwhile_is_declined_not_replayed(
            self, temp_db, monkeypatch, placed, failed):
        """A Stop landed between the adoption's claim and its insert: the
        run reads stopped and the Stop left the session to the adoption.
        The adoption declines it. Before, it replayed the turn to its end:
        the engine went on for a run a person had stopped."""
        from core.events import stream_pump
        from core.session import session_state
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        session_state.mark_starting(sid, 30)             # the report's re-adoption mark
        assert task_store.cancel_live_run(run_id, "Interrupted by user")
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery._recover_session(
            "machine-1", sid, {"session_id": sid, "incarnation": "inc-7"},
            run_id=run_id, chat_id=chat_id, agent="pa")
        assert layer.adopted == [] and chat_id not in stream_pump._active_pumps
        # Its process on the machine ends: the object the report named.
        assert layer.unadopted_closed == [sid] and layer.incarnations[sid] == "inc-7"
        # The platform keeps nothing of it: no mark, no context.
        assert not session_state.is_starting(sid)
        assert session_state.get_session_security(sid) is None
        assert task_store.get_run(run_id)["status"] == "cancelled"
        assert failed == []

    @pytest.mark.asyncio
    async def test_a_stopped_runs_session_is_ended_on_its_machine_before_its_context_goes(
            self, temp_db, monkeypatch, placed, failed):
        from core.session import session_state
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        session_state.mark_starting(sid, 30)
        assert task_store.cancel_live_run(run_id, "Interrupted by user")
        order: list[str] = []
        layer = _FakeLayer()

        async def written() -> bool:
            order.append("waited")
            return True

        async def close_unadopted(machine_id, session_id, incarnation=""):
            order.append("machine told" if session_state.get_session_security(sid) is not None
                         else "machine told after the removal")
        layer.close_unadopted = close_unadopted
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        monkeypatch.setattr(session_state, "security_index_written", written)
        await run_recovery._recover_session(
            "machine-1", sid, {"session_id": sid}, run_id=run_id, chat_id=chat_id, agent="pa")
        assert order == ["machine told"]
        assert session_state.get_session_security(sid) is None

    @pytest.mark.asyncio
    async def test_a_live_run_is_adopted_as_before(self, temp_db, monkeypatch, placed):
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery._recover_session(
            "machine-1", sid, {"session_id": sid}, run_id=run_id, chat_id=chat_id, agent="pa")
        assert layer.adopted == [sid] and layer.unadopted_closed == []
        assert task_store.get_run(run_id)["status"] == "completed"


class TestSweepExpired:
    @pytest.mark.asyncio
    async def test_expired_deadline_fails_run(self, temp_db):
        run_id, _, sid = _mk_remote_run(temp_db)
        run_recovery.defer_orphaned_runs()
        run_recovery._parked[sid]["deadline"] = 0.0  # already expired
        await run_recovery.sweep_expired()
        assert task_store.get_run(run_id)["status"] == "failed"
        assert task_store.get_run(run_id)["error_message"] == run_recovery.RUN_MACHINE_NOT_BACK
        assert sid not in run_recovery._parked


class TestARestartCutsALane:
    """A lane in flight across an unclean restart: parked while its machine
    may still replay it (the chat's turn stays open, so a move waits), then
    failed when its machine reports no such session or the deadline lapses,
    and its delegator told that the restart cut it. A lane its machine kept
    (a clean restart) comes back instead; a run another path ended, or a
    lane that ran again since, is not reported."""

    @pytest.fixture
    def delivered(self, monkeypatch):
        from services.scheduler import scheduler
        got: list[tuple] = []

        async def _deliver(task, final_status, output, **kw):
            got.append((task, final_status, output, kw))
        monkeypatch.setattr(task_store, "get_dynamic_task", lambda tid: {"id": tid} if tid else None)
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
        monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
        return got

    @staticmethod
    def _lane(temp_db):
        from datetime import datetime, timezone
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        before = task_store.add_chat_messages_batch(chat_id, [("assistant", "an earlier round", "", "")])
        task_store.update_run(run_id, started_at=datetime.now(timezone.utc).isoformat())
        task_store.add_chat_messages_batch(chat_id, [("assistant", "half a report", "", "")])
        return run_id, chat_id, sid, before

    @staticmethod
    def _turn_open(chat_id: str) -> bool:
        from services.remote import chat_move
        return chat_move.open_turn(task_store.get_chat(chat_id))

    def _cut(self, delivered, run_id, chat_id, before) -> None:
        from core.events import turn_ending
        assert task_store.get_run(run_id)["status"] == "failed"
        assert self._turn_open(chat_id) is False
        [(task, status, output, kw)] = delivered
        assert (task, status, output) == ("task-x", "failed", "")
        assert kw["ending"].reason == turn_ending.RESTARTED
        assert (kw["worker_chat_id"], kw["output_cursor"], kw["run_id"]) == (chat_id, before, run_id)
        assert kw["turn_end_cursor"] > before
        assert kw["prompt_text"] == "do it"

    @pytest.mark.asyncio
    async def test_a_lane_its_machine_lost_is_the_passs_to_reopen(self, temp_db, delivered, kicked):
        """The machine came back without the lane's session: the run is not
        failed and its delegator hears nothing. Its record goes to the
        continue pass, which reopens the run."""
        run_id, chat_id, _, _before = self._lane(temp_db)
        run_recovery.defer_orphaned_runs(catches_up=True)
        assert self._turn_open(chat_id) is True
        await _reported("machine-1", [], [])
        assert task_store.get_run(run_id)["status"] == "running"
        assert delivered == [] and kicked == [1]
        assert _record(chat_id)["state"] == cut_records.PENDING

    @pytest.mark.asyncio
    async def test_a_lane_whose_machine_never_came_back_is_cut_at_the_deadline(self, temp_db, delivered):
        run_id, chat_id, sid, before = self._lane(temp_db)
        run_recovery.defer_orphaned_runs()
        assert self._turn_open(chat_id) is True
        run_recovery._parked[sid]["deadline"] = 0.0
        await run_recovery.sweep_expired()
        self._cut(delivered, run_id, chat_id, before)
        assert task_store.get_run(run_id)["error_message"] == run_recovery.RUN_MACHINE_NOT_BACK

    @pytest.mark.asyncio
    async def test_a_lane_its_machine_kept_comes_back_and_is_not_reported_cut(
            self, temp_db, delivered, monkeypatch, placed):
        run_id, chat_id, sid, before = self._lane(temp_db)
        placed(sid)
        run_recovery.defer_orphaned_runs()
        assert self._turn_open(chat_id) is True
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1"}])
        await _until(lambda: task_store.get_run(run_id)["status"] != "running")
        assert layer.adopted == [sid]
        assert task_store.get_run(run_id)["status"] == "completed"
        await _until(lambda: bool(delivered))
        [(_task, status, output, kw)] = delivered
        assert status == "completed" and kw["ending"] is None
        assert (kw["worker_chat_id"], kw["output_cursor"], kw["prompt_text"]) == (chat_id, before, "do it")
        assert "an earlier round" not in output

    @pytest.mark.asyncio
    async def test_a_run_already_ended_is_not_reported_and_a_lane_that_ran_again_wakes_nobody(
            self, temp_db, delivered, kicked):
        from datetime import datetime, timezone
        ended, _, ended_sid, _ = self._lane(temp_db)
        again, again_chat, _, _ = self._lane(temp_db)
        run_recovery.defer_orphaned_runs(catches_up=True)
        task_store.update_run(ended, status="completed")
        newer = f"run-{uuid.uuid4().hex[:10]}"
        task_store.create_run(newer, "task-x", "pa", "manual", None, "go on")
        task_store.update_run(newer, status="running", chat_id=again_chat,
                              started_at=datetime.now(timezone.utc).isoformat())
        run_recovery._parked[ended_sid]["deadline"] = 0.0
        await run_recovery.sweep_expired()
        await run_recovery.on_sessions_alive("machine-1", [], [])
        assert task_store.get_run(ended)["status"] == "completed"
        # The lane ran again: its reopen fails the old run, and its caller's
        # card is told the round was cut, cancelled (nothing failed), while
        # nobody is woken.
        assert await run_recovery.reopen_run(task_store.get_run(again), _record(again_chat)) is False
        assert task_store.get_run(again)["status"] == "failed"
        [(_task, status, output, kw)] = delivered
        assert status == "cancelled" and kw["wake"] is False and kw["lane_chat_id"] == again_chat
        assert "worker_chat_id" not in kw and output == run_recovery.CUT_ROUND_WENT_ON
        assert output == "A restart cut this round. The next round of this lane carries the work on."

    @pytest.mark.asyncio
    async def test_a_lane_a_waiting_round_continues_is_reported_without_a_wake(self, temp_db, delivered):
        """The delegator continued the lane inside the window: its round waits
        at the lane gate, with no start and no chat on its run yet."""
        from services.scheduler import task_kinds
        run_id, chat_id, sid, _ = self._lane(temp_db)
        run_recovery.defer_orphaned_runs()
        task_store.create_dynamic_task("task-again", "pa", "again", "go on", "auto", "one_time",
                                       None, None, None, 600, "user-1", target_chat_id=chat_id)
        waiting = f"run-{uuid.uuid4().hex[:10]}"
        task_store.create_run(waiting, "task-again", "pa", "manual", None, "go on",
                              task_type=task_kinds.RUN_DELEGATE)
        assert (task_store.get_run(waiting)["chat_id"] or "") == ""
        run_recovery._parked[sid]["deadline"] = 0.0
        await run_recovery.sweep_expired()
        assert task_store.get_run(run_id)["status"] == "failed"
        # The round the delegator queued goes on: the cut one is reported to
        # its card, cancelled, and wakes nobody. The run's own row stays failed.
        [(_task, status, output, kw)] = delivered
        assert status == "cancelled" and kw["wake"] is False and kw["lane_chat_id"] == chat_id
        assert kw["ending"].reason == "restarted" and "worker_chat_id" not in kw

    def test_a_restart_counts_as_a_cut_without_a_report(self):
        from core.events import turn_ending
        from services.scheduler import lanes
        assert lanes.report_missing(turn_ending.RESTARTED, "failed", "half a report", [], []) is True
        assert lanes.report_missing(turn_ending.RESTARTED, "failed", "Blocked on: a key", [], []) is False
        assert "the platform restarted" in lanes.missing_report_note(turn_ending.RESTARTED, "c1")


def _closing_rows(chat_id: str) -> list[dict]:
    import json
    return [json.loads(r["event_data"]) for r in task_store.get_chat_messages(chat_id)
            if r.get("event_type") == "system"
            and json.loads(r["event_data"]).get("subtype") == "turn_ended"]


async def _until(predicate, tries: int = 100) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.02)


class _ScriptedLayer(_FakeLayer):
    """A fake whose adoption yields ``script``: the replay's own markers."""

    def __init__(self, script):
        super().__init__()
        self.script = script

    async def adopt_session(self, *, machine_id, session_id, agent_name,
                            execution_path, command_id,
                            use_native_permissions=False, mode="", token_floor=0, **kw):
        self.adopted.append(session_id)
        for event in self.script:
            yield event


def _early_rows(chat_id: str, token: str, sid: str) -> None:
    """The cut turn's record with one row its checkpoint saved."""
    from storage.pg import get_conn
    open_turns.open_turn(chat_id, token, sid)
    first = task_store.add_chat_messages_batch(chat_id, [("assistant", "saved early", "", "")])
    with get_conn() as conn:
        open_turns.note_range_in(conn, chat_id, token, first - 1, first)
        conn.commit()


def _assistant(chat_id: str) -> list[str]:
    return [r["content"] for r in task_store.get_chat_messages(chat_id) if r["role"] == "assistant"]


class TestCutTurns:
    """The turns a restart cut: a record each at boot, the card for one
    nothing continues or replays, never a card for a replayed one."""

    def test_boot_moves_a_local_turn_to_the_pass_and_waits_for_a_remote_one(self, temp_db):
        task_store.create_chat("cut-local", "user-1", "pa", "default")
        open_turns.open_turn("cut-local", "tok-l", "sid-l", kind="dashboard", person="user-1")
        _, remote_chat, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(remote_chat, "tok-r", sid, kind="task")

        assert cut_turns.record_at_boot(clean_stop=True, catches_up=True) == {"waiting": 1, "pending": 1, "ended": 0}
        local = _record("cut-local")
        assert (local["state"], local["cause"], local["person"], local["kind"]) == (
            cut_records.PENDING, cut_records.CAUSE_STOP, "user-1", "dashboard")
        # The card is the pass's to write, or not: nothing lands at boot.
        assert _closing_rows("cut-local") == [] and open_turns.get_open("cut-local") is None
        remote = _record(remote_chat)
        assert (remote["state"], remote["machine_id"]) == (cut_records.WAITING, "machine-1")
        assert sid in cut_turns._parked and open_turns.get_open(remote_chat)["turn_token"] == "tok-r"
        # The next boot finds the same records and makes no second one.
        assert cut_turns.record_at_boot(clean_stop=False, catches_up=True) == {"waiting": 0, "pending": 0, "ended": 0}
        assert sid in cut_turns._parked
        with task_store.get_conn() as conn:
            assert conn.execute("SELECT count(*) AS n FROM chat_cut_turns").fetchone()["n"] == 2

    def test_a_chat_cut_again_while_the_pass_held_it_keeps_only_the_newest_record(self, temp_db):
        """The last process's record goes back to pending and the continue it
        started left a row of its own: two records would continue it twice."""
        task_store.create_chat("cut-twice", "user-1", "pa", "default")
        older = cut_records.add("cut-twice", cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                                session_id="sid-old", kind="dashboard", person="user-1")
        cut_records.claim(older["id"])
        open_turns.open_turn("cut-twice", "tok-new", "sid-new", kind="dashboard", person="user-1")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        kept = cut_records.unsettled_of("cut-twice")
        assert kept["session_id"] == "sid-new" and kept["state"] == cut_records.PENDING
        # Claimed, so it counts as the start it was.
        assert cut_records.get(older["id"])["state"] == cut_records.CONTINUED

    def test_the_waiter_of_a_turn_its_machine_kept_waits_again(self, temp_db, monkeypatch):
        from services.remote import chat_reload
        from storage.chat import pending_rebuilds
        settled: list[tuple] = []

        async def _settle(row, *, continued, person="", lives_on=False):
            settled.append((row["chat_id"], continued, lives_on))
            return []
        monkeypatch.setattr(chat_reload, "settle_after_restart", _settle)
        for chat_id in ("cut-kept", "cut-never"):
            task_store.create_chat(chat_id, "user-1", "pa", "default")
            pending_rebuilds.save(chat_id, pending_rebuilds.RELOAD, person="user-1", wait_s=600)
        asyncio.run(cut_turns.replay_settled({"record_id": 0, "chat_id": "cut-kept"}, began=True))
        asyncio.run(cut_turns.replay_settled({"record_id": 0, "chat_id": "cut-never"}, began=False))
        assert settled == [("cut-kept", False, True), ("cut-never", False, False)]
        assert pending_rebuilds.listed() == []

    def test_the_place_of_the_session_decides_before_the_chats_pin(self, temp_db):
        """The row names the machine the session ran on. A row with none (an
        older release's, a session with no context) falls back to the chat's
        pin, so a chat on a machine waits for it."""
        task_store.create_chat("cut-placed", "user-1", "pa", "default")
        open_turns.open_turn("cut-placed", "tok", "sid-p", kind="dashboard", machine_id="machine-9")
        _, pinned_chat, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(pinned_chat, "tok-old", sid)          # an older release's row
        assert cut_turns.record_at_boot(clean_stop=False, catches_up=True)["waiting"] == 2
        assert _record("cut-placed")["machine_id"] == "machine-9"
        assert (_record(pinned_chat)["machine_id"], _record(pinned_chat)["kind"]) == ("machine-1", "")

    def test_boot_drops_a_finished_engine_turn_and_a_meetings_record(self, temp_db):
        """A turn whose engine had answered and a meeting's turn leave no
        record: the first is complete, the failed meeting's own row is the
        second's. A run's turn whose engine had answered is kept: the run
        itself was cut."""
        task_store.create_chat("cut-idle", "user-1", "pa", "default")
        open_turns.open_turn("cut-idle", "tok-i", "sid-i", kind="dashboard")
        open_turns.engine_turn_done("cut-idle", "tok-i")
        _, meeting_chat, _ = _mk_remote_run(temp_db)
        open_turns.open_turn(meeting_chat, "tok-m", "")   # a meeting pump records no session
        task_store.create_chat("cut-run", "user-1", "pa", "default")
        open_turns.open_turn("cut-run", "tok-run", "sid-run", kind="task", run_id="run-x")
        open_turns.engine_turn_done("cut-run", "tok-run")
        assert cut_turns.record_at_boot(clean_stop=False, catches_up=True) == {"waiting": 0, "pending": 1, "ended": 2}
        assert _closing_rows("cut-idle") == [] and _closing_rows(meeting_chat) == []
        assert open_turns.list_open() == [] and cut_turns._parked == {}
        assert _record("cut-idle") == {} and _record("cut-run")["run_id"] == "run-x"

    @pytest.mark.asyncio
    async def test_an_adopted_turn_is_replayed_once_and_gets_no_closing_row(
            self, temp_db, monkeypatch, placed):
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        _early_rows(chat_id, "tok-cut", sid)
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        run_recovery.defer_orphaned_runs()
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1",
        }])
        await _until(lambda: task_store.get_run(run_id)["status"] != "running")
        assert _assistant(chat_id) == ["recovered answer"]
        assert _closing_rows(chat_id) == []
        assert cut_turns._parked == {}
        await _until(lambda: not open_turns.list_open())
        assert open_turns.list_open() == []
        await _until(lambda: _record(chat_id)["state"] == cut_records.LEFT)
        assert _record(chat_id)["state"] == cut_records.LEFT

    @pytest.mark.asyncio
    async def test_a_truncated_replay_keeps_the_rows_saved_early(self, temp_db, monkeypatch, placed):
        from core.events.common_events import DONE, TEXT, CommonEvent
        from core.remote.remote_resume import REPLAY_BEGIN
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        _early_rows(chat_id, "tok-cut", sid)
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        run_recovery.defer_orphaned_runs()
        layer = _ScriptedLayer([CommonEvent(type=REPLAY_BEGIN, data={"truncated": True}),
                                CommonEvent(type=TEXT, data={"content": "the tail"}),
                                CommonEvent(type=DONE, data={})])
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1"}])
        await _until(lambda: task_store.get_run(run_id)["status"] != "running")
        assert _assistant(chat_id) == ["saved early", "the tail"]
        assert _closing_rows(chat_id) == []

    @staticmethod
    def _saved_early(chat_id: str, token: str, sid: str, rows: list[tuple]) -> None:
        """The cut turn's record with the rows its checkpoints saved."""
        from storage.pg import get_conn
        open_turns.open_turn(chat_id, token, sid)
        before = task_store.get_last_chat_message_id(chat_id) or 0
        last = task_store.add_chat_messages_batch(chat_id, rows)
        with get_conn() as conn:
            open_turns.note_range_in(conn, chat_id, token, before, last)
            conn.commit()

    @staticmethod
    def _tools(chat_id: str) -> list[tuple[str, bool]]:
        import json
        out = []
        for r in task_store.get_chat_messages(chat_id):
            if r.get("event_type") == "tool":
                block = json.loads(r["event_data"])
                out.append((block.get("tool_id"), "tool_result" in block))
        return out

    @pytest.mark.asyncio
    async def test_a_truncated_replay_writes_only_what_the_chat_does_not_hold(
            self, temp_db, monkeypatch, placed):
        """The internal install's case: the machine's kept events reached back
        before the last checkpoint, so the replay wrote 42 rows the chat
        already held, and a run's report held 12 blocks twice, under a mark
        that said output was lost."""
        import json

        from core.events.common_events import (
            DONE,
            TEXT,
            TOOL_RESULT,
            TOOL_USE,
            CommonEvent,
        )
        from core.remote.remote_resume import REPLAY_BEGIN, TRUNCATED_LINE
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        held = json.dumps({"type": "tool", "name": "Bash", "tool_id": "t-held", "active": False,
                           "tool_result": "the output a hook brought before the cut"})
        self._saved_early(chat_id, "tok-cut", sid, [
            ("assistant", "the turn's start", "", ""),
            ("event", "", "tool", held),
            ("assistant", "\n\nReading the diff.", "", ""),
        ])
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        run_recovery.defer_orphaned_runs()
        layer = _ScriptedLayer([
            CommonEvent(type=REPLAY_BEGIN, data={"truncated": True, "marker": TRUNCATED_LINE}),
            CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "t-held"}),
            CommonEvent(type=TOOL_RESULT, data={"name": "", "tool_id": "t-held", "result_preview": "ok"}),
            CommonEvent(type=TEXT, data={"content": "Reading the diff."}),
            CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "t-new"}),
            CommonEvent(type=TOOL_RESULT, data={"name": "", "tool_id": "t-new", "result_preview": "ok"}),
            CommonEvent(type=TEXT, data={"content": "The tests pass."}),
            CommonEvent(type=DONE, data={})])
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1"}])
        await _until(lambda: task_store.get_run(run_id)["status"] != "running")
        assert _assistant(chat_id) == ["the turn's start", "\n\nReading the diff.", "The tests pass."]
        # The kept copy of the held tool is the one with its result.
        assert self._tools(chat_id) == [("t-held", True), ("t-new", False)]
        assert "truncated" not in (task_store.get_run(run_id)["output_text"] or "")

    @pytest.mark.asyncio
    async def test_a_truncated_replay_past_the_last_saved_row_keeps_its_mark(
            self, temp_db, monkeypatch, placed):
        from core.events.common_events import (
            DONE,
            TEXT,
            TOOL_RESULT,
            TOOL_USE,
            CommonEvent,
        )
        from core.remote.remote_resume import REPLAY_BEGIN, TRUNCATED_LINE
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        self._saved_early(chat_id, "tok-cut", sid, [("assistant", "saved early", "", "")])
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        run_recovery.defer_orphaned_runs()
        layer = _ScriptedLayer([
            CommonEvent(type=REPLAY_BEGIN, data={"truncated": True, "marker": TRUNCATED_LINE}),
            CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "t-later"}),
            CommonEvent(type=TOOL_RESULT, data={"name": "", "tool_id": "t-later", "result_preview": "ok"}),
            CommonEvent(type=TEXT, data={"content": "the tail"}),
            CommonEvent(type=DONE, data={})])
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1"}])
        await _until(lambda: task_store.get_run(run_id)["status"] != "running")
        assert _assistant(chat_id) == ["saved early", TRUNCATED_LINE, "the tail"]
        assert self._tools(chat_id) == [("t-later", False)]

    @pytest.mark.asyncio
    async def test_a_truncated_replay_of_a_turn_that_saved_nothing_shows_its_mark(
            self, temp_db, monkeypatch, placed):
        from core.events.common_events import DONE, TEXT, CommonEvent
        from core.remote.remote_resume import REPLAY_BEGIN, TRUNCATED_LINE
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        open_turns.open_turn(chat_id, "tok-cut", sid)
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        run_recovery.defer_orphaned_runs()
        layer = _ScriptedLayer([
            CommonEvent(type=REPLAY_BEGIN, data={"truncated": True, "marker": TRUNCATED_LINE}),
            CommonEvent(type=TEXT, data={"content": "the tail"}),
            CommonEvent(type=DONE, data={})])
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1"}])
        await _until(lambda: task_store.get_run(run_id)["status"] != "running")
        rows = _assistant(chat_id)
        assert len(rows) == 1 and "truncated" in rows[0] and rows[0].endswith("the tail")

    @pytest.mark.asyncio
    async def test_a_replay_that_never_begins_closes_the_turn_and_fails_its_run(
            self, temp_db, monkeypatch, placed):
        """No replay: the chat gets the closing row, and the run fails with
        the same ending instead of completing with the early rows; its
        delegator hears that the turn was cut short."""
        from core.events import turn_ending
        from core.events.common_events import DONE, CommonEvent
        from services.scheduler import scheduler
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        _early_rows(chat_id, "tok-cut", sid)
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        run_recovery.defer_orphaned_runs()
        layer = _ScriptedLayer([CommonEvent(type=DONE, data={})])
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        delivered: list[tuple] = []

        async def _deliver(task, final_status, output, **kw):
            delivered.append((task, final_status, output, kw.get("ending")))
        monkeypatch.setattr(task_store, "get_dynamic_task", lambda tid: {"id": tid} if tid else None)
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
        monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1"}])
        await _until(lambda: task_store.get_run(run_id)["status"] != "running")
        assert _assistant(chat_id) == ["saved early"]
        assert [c["reason"] for c in _closing_rows(chat_id)] == ["restarted"]
        run = task_store.get_run(run_id)
        assert run["status"] == "failed"
        assert run["error_message"] == "Cut short when the platform restarted."
        await _until(lambda: bool(delivered))
        task, final_status, output, ending = delivered[0]
        assert (task, final_status, output) == ("task-x", "failed", "saved early")
        assert ending.reason == turn_ending.RESTARTED
        assert _record(chat_id)["state"] == cut_records.DROPPED

    @pytest.mark.asyncio
    async def test_a_cut_turn_its_machine_holds_idle_is_replayed(self, temp_db, monkeypatch, placed):
        """The turn finished on the satellite while the platform was down:
        reported idle and unmarked, it is still replayed (the satellite kept
        it), never closed."""
        task_store.create_chat("cut-plain", "user-1", "pa", "default", model="m",
                               execution_path="claude-code-cli")
        sid = uuid.uuid4().hex
        task_store.update_chat("cut-plain", session_id=sid, execution_target="machine-1")
        placed(sid)
        _early_rows("cut-plain", "tok-cut", sid)
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False, "command_id": "c1"}])
        await _until(lambda: _assistant("cut-plain") == ["recovered answer"])
        assert layer.adopted == [sid] and layer.idle_adopted == []
        assert _closing_rows("cut-plain") == []

    @pytest.mark.asyncio
    async def test_a_session_the_report_lists_and_nothing_replays_gets_the_card(self, temp_db, kicked):
        """Listed, and the adoption did not replay it (it closed the session
        there, or took it back idle): the chat gets the card, as before the
        pass. The pass is not asked: two engines never write one chat."""
        _, chat_id, sid = _mk_remote_run(temp_db, exec_path="codex-cli")
        open_turns.open_turn(chat_id, "tok-cut", sid, kind="dashboard")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        await _reported("machine-1", [], [
            {"session_id": sid, "alive": True, "turn_active": True, "execution_path": "codex-cli"}])
        assert [c["reason"] for c in _closing_rows(chat_id)] == ["restarted"]
        assert cut_turns._parked == {} and open_turns.list_open() == []
        assert _record(chat_id)["state"] == cut_records.DROPPED and kicked == []

    @pytest.mark.asyncio
    async def test_a_session_a_complete_report_lacks_is_the_passs(self, temp_db, kicked):
        """Not listed in a complete report: the session is gone, so the chat
        is continued. No card here, the open-turn row goes, the record is
        pending."""
        _, chat_id, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(chat_id, "tok-cut", sid, kind="dashboard", person="user-1")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        await _reported("machine-1", [], [])
        assert _closing_rows(chat_id) == []
        assert cut_turns._parked == {} and open_turns.list_open() == []
        assert _record(chat_id)["state"] == cut_records.PENDING and kicked == [1]

    @pytest.mark.asyncio
    async def test_a_report_that_is_not_complete_decides_nothing(self, temp_db, kicked):
        _, chat_id, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(chat_id, "tok-cut", sid, kind="dashboard")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        await _reported("machine-1", [])
        assert _closing_rows(chat_id) == [] and sid in cut_turns._parked
        assert _record(chat_id)["state"] == cut_records.WAITING and kicked == []
        # Both lists came, and the report still is not one to judge by: a
        # pair sent late, a list that failed on the machine, a satellite
        # older than 0.5.170.
        await _reported("machine-1", [], [], complete=False)
        assert sid in cut_turns._parked and kicked == []

    @pytest.mark.asyncio
    async def test_only_what_was_parked_before_the_sign_in_is_judged(self, temp_db, kicked):
        from core.remote import satellite_link
        _, chat_id, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(chat_id, "tok-cut", sid, kind="dashboard", person="user-1")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)

        def report(**over):
            return satellite_link.AliveReport(**{
                "machine_id": "machine-1", "headless": frozenset(), "terminals": frozenset(), "complete": True,
                "sessions": (), "other_sessions": (), "signed_in_at": time.monotonic(), "report_id": "r",
                **over})
        await run_recovery.on_alive_report(report(signed_in_at=0.0))
        assert sid in cut_turns._parked and kicked == []
        await run_recovery.on_alive_report(report(_current=lambda: False))
        assert sid in cut_turns._parked and kicked == []
        assert _record(chat_id)["state"] == cut_records.WAITING
        await run_recovery.on_alive_report(report())
        assert _record(chat_id)["state"] == cut_records.PENDING and kicked == [1]

    def test_the_pass_reads_each_machines_report_once(self, temp_db, monkeypatch):
        from core.remote import satellite_link
        monkeypatch.setattr(satellite_link, "_alive_hooks", [])
        manager = SimpleNamespace(set_sessions_alive_callback=lambda cb: None)
        run_recovery.register(manager)
        run_recovery.register(manager)
        assert satellite_link._alive_hooks == [("cut_turns", run_recovery.on_alive_report)]

    @pytest.mark.asyncio
    async def test_a_terminals_turn_on_a_machine_gets_the_card(self, temp_db, kicked):
        """A terminal is not in the report's lists, so the report cannot say
        it is gone: its chat gets the card, never a second terminal."""
        _, chat_id, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(chat_id, "tok-cut", sid, kind=cut_turns.KIND_TERMINAL)
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        await _reported("machine-1", [], [])
        assert [c["reason"] for c in _closing_rows(chat_id)] == ["restarted"]
        assert _record(chat_id)["state"] == cut_records.DROPPED and kicked == []

    @pytest.mark.asyncio
    async def test_a_turn_no_report_reaches_is_told_once_and_keeps_waiting(self, temp_db):
        """Never a timer: a machine that has not come back keeps its chats
        waiting. After a while the chat is told why, once."""
        _, chat_id, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(chat_id, "tok-cut", sid, kind="dashboard")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        await run_recovery.sweep_expired()
        assert _closing_rows(chat_id) == []
        cut_turns._parked[sid]["since"] -= cut_turns.WAITING_LINE_S + 1
        await run_recovery.sweep_expired()
        await run_recovery.sweep_expired()
        lines = [json.loads(r["event_data"]) for r in task_store.get_chat_messages(chat_id)
                 if r.get("event_type") == "system"]
        assert [(c["subtype"], c["message"]) for c in lines] == [("turn_waiting", cut_turns.WAITING_LINE)]
        assert sid in cut_turns._parked and _record(chat_id)["state"] == cut_records.WAITING
        assert open_turns.get_open(chat_id)["turn_token"] == "tok-cut"
        # Nothing is decided yet: no abort flag shapes the person's next send.
        assert not task_store.get_chat(chat_id).get("last_abort_graceful")

    @pytest.mark.asyncio
    async def test_the_card_still_follows_the_waiting_line(self, temp_db, kicked):
        """The line is not the turn's own card: read as one, the card would be
        swallowed and the chat would end on the line for ever."""
        _, chat_id, sid = _mk_remote_run(temp_db, exec_path="codex-cli")
        open_turns.open_turn(chat_id, "tok-cut", sid, kind="dashboard")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        cut_turns._parked[sid]["since"] -= cut_turns.WAITING_LINE_S + 1
        await run_recovery.sweep_expired()
        await _reported("machine-1", [], [
            {"session_id": sid, "alive": True, "turn_active": True, "execution_path": "codex-cli"}])
        assert [c["reason"] for c in _closing_rows(chat_id)] == ["restarted"]

    def test_the_waiting_line_is_true_for_a_machine_back_and_too_old_to_report(self):
        """It may be read by a person whose machine is back: it must not say it
        continues "when the machine is back"."""
        assert "when the machine" not in cut_turns.WAITING_LINE
        # Every machine reports since the 0.5.171 floor: the line names no older one.
        assert "within a day" in cut_turns.WAITING_LINE and "older version" not in cut_turns.WAITING_LINE
        assert "Send a message" in cut_turns.WAITING_LINE

    @pytest.mark.asyncio
    async def test_a_turn_a_new_one_replaced_gets_no_closing_row(self, temp_db, kicked):
        _, chat_id, sid = _mk_remote_run(temp_db)
        open_turns.open_turn(chat_id, "tok-cut", sid, kind="dashboard")
        cut_turns.record_at_boot(clean_stop=False, catches_up=True)
        open_turns.open_turn(chat_id, "tok-new", sid)   # a person's turn started first
        await run_recovery.on_sessions_alive("machine-1", [], [])
        assert _closing_rows(chat_id) == []
        assert [r["turn_token"] for r in open_turns.list_open()] == ["tok-new"]


class TestEligibility:
    def test_remote_cli_eligible(self, temp_db):
        _, chat_id, _ = _mk_remote_run(temp_db)
        assert run_recovery.is_recovery_eligible(chat_id) is True

    def test_local_not_eligible_and_codex_is(self, temp_db):
        _, local_chat, _ = _mk_remote_run(temp_db, target="local")
        _, codex_chat, _ = _mk_remote_run(temp_db, exec_path="codex-cli")
        assert run_recovery.is_recovery_eligible(local_chat) is False
        assert run_recovery.is_recovery_eligible(codex_chat) is True
        assert run_recovery.is_recovery_eligible("") is False


class TestMachineResumePass:
    """A machine's reconnect resumes what its blip left waiting: the
    background monitors of its ordinary chats' sessions, and the queued
    messages of the chats on it."""

    @staticmethod
    def _chat(chat_id: str, *, target: str = "", sid: str = "", queued: bool = False,
              **kw) -> None:
        task_store.create_chat(chat_id, "user-1", "pa", "default", **kw)
        task_store.update_chat(chat_id, session_id=sid or None, execution_target=target)
        if queued:
            task_store.enqueue_chat_input(chat_id, "q-" + chat_id, "user-1", text="waiting",
                                          cli_text="waiting", event_data="", images="",
                                          origin_conn="")

    @pytest.fixture
    def recorded(self, monkeypatch):
        import core.session.session_manager as sm
        from core.events import input_queue, pump_bg_monitors
        layer = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        armed_queues: list[str] = []
        armed_monitors: list[tuple[str, str]] = []
        monkeypatch.setattr(input_queue, "arm", armed_queues.append)
        monkeypatch.setattr(pump_bg_monitors, "arm_bg_monitors",
                            lambda lyr, sid, cid: armed_monitors.append((sid, cid)))
        return SimpleNamespace(layer=layer, queues=armed_queues, monitors=armed_monitors)

    @pytest.mark.asyncio
    async def test_queued_messages_on_the_machine_are_armed(self, temp_db, recorded):
        self._chat("c-pinned", target="machine-1", queued=True)
        self._chat("c-elsewhere", target="machine-2", queued=True)
        self._chat("c-unpinned", sid="s-unpinned", queued=True)
        self._chat("c-nothing-queued", target="machine-1")
        recorded.layer.machines["s-unpinned"] = "machine-1"

        await run_recovery._machine_resume_pass("machine-1", set())

        assert sorted(recorded.queues) == ["c-pinned", "c-unpinned"]

    @pytest.mark.asyncio
    async def test_the_chips_stop_saying_the_machine_is_reconnecting(
            self, temp_db, recorded, monkeypatch):
        from core.events import input_queue
        fanned: list[tuple[str, dict]] = []
        monkeypatch.setattr(input_queue, "fan_out",
                            lambda cid, frame, **kw: fanned.append((cid, frame)))
        self._chat("c-away", target="machine-1", queued=True)
        q = await input_queue.loaded("c-away")
        q.items[0].waiting = input_queue.WAITING_RECONNECT
        recorded.queues.clear()

        await run_recovery._machine_resume_pass("machine-1", set())

        assert q.items[0].waiting == ""
        assert [(cid, f["type"]) for cid, f in fanned] == [("c-away", "queue_snapshot")]
        assert "waiting" not in fanned[0][1]["messages"][0]
        assert recorded.queues == ["c-away"]

    @pytest.mark.asyncio
    async def test_monitors_of_chats_reported_sessions_are_armed(
            self, temp_db, recorded):
        from core.events.bg_command_state import (
            _bg_command_registries,
            get_bg_command_registry,
        )
        from services.scheduler import lanes
        sessions = {
            "s-chat": {},
            "s-task": {"source_type": "task"},
            "s-worker": {"delegate_role": "worker"},
            "s-window": {"delegate_role": "worker"},
            "s-phone": {"source_type": "phone"},
            "s-unreported": {},
            "s-severed": {},
            "s-idle": {},
        }
        lanes.hold_report("s-window")
        try:
            for sid, kw in sessions.items():
                self._chat("c-" + sid, target="machine-1", sid=sid, **kw)
                recorded.layer.machines[sid] = "machine-1"
                if sid != "s-idle":
                    get_bg_command_registry(sid).register_spawn("b-" + sid, "tu-" + sid)
            recorded.layer.severed.add("s-severed")

            await run_recovery._machine_resume_pass(
                "machine-1", set(sessions) - {"s-unreported"})
        finally:
            lanes.release_report("s-window")
            for sid in sessions:
                _bg_command_registries.pop(sid, None)

        # A worker's or a task run's chat outside a report window too; the
        # run's producer owns a session whose report is pending.
        assert sorted(recorded.monitors) == [
            ("s-chat", "c-s-chat"), ("s-task", "c-s-task"), ("s-worker", "c-s-worker")]

    @pytest.mark.asyncio
    async def test_a_deferred_queued_message_goes_out_after_the_reconnect(
            self, temp_db, monkeypatch):
        import core.session.session_manager as sm
        from core.events import input_queue
        from core.events.common_events import TurnInput
        layer = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.01)
        self._chat("c-blip", target="machine-1")
        q = await input_queue.loaded("c-blip")
        await q.add("q-blip", "user-1", TurnInput("across the blip"))
        outcomes = [input_queue.DELIVERY_DEFERRED, input_queue.DELIVERY_SENT]
        calls: list[list[str]] = []

        class _Conn:
            async def _deliver_queued(self, chat_id, taken):
                calls.append([qi.queue_id for qi in taken])
                return outcomes.pop(0), "the machine was away"
        monkeypatch.setattr(input_queue, "_driver_for", lambda author, items: _Conn())

        assert await input_queue.deliver("c-blip") is False      # the gap
        await run_recovery.on_sessions_alive("machine-1", [])    # the machine is back
        for _ in range(100):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.02)

        assert calls == [["q-blip"], ["q-blip"]]

    @pytest.mark.asyncio
    async def test_the_pass_does_not_wait_for_the_wake_replay(self, temp_db, recorded, monkeypatch):
        from services.scheduler import scheduler as _sched
        held = asyncio.Event()

        async def _slow_replay(machine_id=None, session_ids=()):
            await held.wait()
            return 0
        monkeypatch.setattr(_sched, "redeliver_pending_wakes", _slow_replay)
        self._chat("c-first", target="machine-1", queued=True)

        await run_recovery.on_sessions_alive("machine-1", [])
        for _ in range(100):
            if recorded.queues:
                break
            await asyncio.sleep(0.02)
        held.set()

        assert recorded.queues == ["c-first"]


class TestARunOnAMachineAndItsReport:
    """A run ends with one report when its machine's report settles it, and a
    report that decides nothing takes nothing from it."""

    @pytest.fixture
    def told(self, monkeypatch):
        from services.scheduler import scheduler
        said: list[str] = []

        async def _deliver(task, final, output, **kw):
            said.append(kw.get("run_id"))
        monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
        monkeypatch.setattr(task_store, "get_dynamic_task", lambda task_id: {"id": task_id})
        monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row)
        return said

    def _run_on_the_machine(self, temp_db, exec_path: str) -> tuple[str, str, str]:
        run_id, chat_id, sid = _mk_remote_run(temp_db, exec_path=exec_path)
        open_turns.open_turn(chat_id, "tok", sid, kind="task", run_id=run_id, machine_id="machine-1")
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
        return run_id, chat_id, sid

    @pytest.mark.asyncio
    async def test_a_codex_lane_its_machine_lists_fails_and_is_told_once(self, temp_db, told, kicked):
        """No replay for this engine, so the report's list settles it with the
        card. Left live, it would run on until the next boot with nobody told."""
        run_id, chat_id, sid = self._run_on_the_machine(temp_db, "codex-cli")
        assert task_store.get_run(run_id)["status"] == "running"
        await _reported("machine-1", [], [
            {"session_id": sid, "alive": True, "turn_active": True, "execution_path": "codex-cli"}])
        for task in background.running("cut_turns"):
            await task
        assert _record(chat_id)["state"] == cut_records.DROPPED and kicked == []
        run = task_store.get_run(run_id)
        assert (run["status"], run["error_message"]) == ("failed", run_recovery.RUN_CUT)
        assert told == [run_id]

    @pytest.mark.asyncio
    async def test_a_report_that_is_not_complete_leaves_a_parked_run_its_deadline(self, temp_db, kicked):
        """A satellite older than 0.5.170 sends both lists and no report id,
        and a list can fail on the machine. Neither says the session is gone."""
        run_id, chat_id, sid = self._run_on_the_machine(temp_db, "claude-code-cli")
        assert sid in run_recovery._parked
        await _reported("machine-1", [], [], complete=False)
        assert sid in run_recovery._parked and kicked == []
        assert _record(chat_id)["state"] == cut_records.WAITING
        assert task_store.get_run(run_id)["status"] == "running"
        # Its deadline still ends it.
        run_recovery._parked[sid]["deadline"] = 0.0
        await run_recovery.sweep_expired()
        assert sid not in run_recovery._parked
        assert task_store.get_run(run_id)["status"] == "failed"

    @pytest.mark.asyncio
    async def test_between_two_reports_the_run_stays_parked_under_its_run_id(self, temp_db, kicked):
        """Between the two reports the run stays parked, so the replay knows
        its run and not only its chat."""
        run_id, _chat_id, sid = self._run_on_the_machine(temp_db, "claude-code-cli")
        await _reported("machine-1", [], [], complete=False)
        assert run_recovery._parked[sid]["run_id"] == run_id

    @pytest.mark.asyncio
    async def test_a_complete_report_that_does_not_list_it_takes_it_off_the_deadline(self, temp_db, kicked):
        run_id, chat_id, sid = self._run_on_the_machine(temp_db, "claude-code-cli")
        await _reported("machine-1", [], [])
        assert sid not in run_recovery._parked
        assert _record(chat_id)["state"] == cut_records.PENDING and kicked == [1]
        assert task_store.get_run(run_id)["status"] == "running"


class TestTheTurnListsFacts:
    """A ``sessions`` entry of a 0.5.171 satellite carries the thread, the
    model and the tools: an idle session listed there is taken back with
    them, as one in ``other_sessions`` is, and the chat's kind is recorded
    on the adoption."""

    @pytest.fixture
    def layer(self, monkeypatch):
        import core.session.session_manager as sm
        lyr = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: lyr)
        return lyr

    @pytest.mark.asyncio
    async def test_an_idle_codex_session_in_the_turn_list_is_taken_back_with_its_facts(
            self, temp_db, layer, placed, monkeypatch):
        from storage import remote_store
        monkeypatch.setattr(remote_store, "get_remote_machine",
                            lambda mid: {"id": mid, "allow_full_fs": True})
        _, sid = _codex_chat(temp_db)
        placed(sid, mode="default", floor=1790000000)
        entry = {"session_id": sid, "execution_path": "codex-cli", "agent_slug": "pa",
                 "turn_active": False, "incarnation": "b1-8", "command_id": "",
                 "buffered_events": 0, "use_native_permissions": False,
                 "resume_handle": "thread-9", "model": "gpt-6.1-sol",
                 "mcp_servers": ["memory", "display"]}

        await run_recovery.on_sessions_alive("machine-1", [entry], [])

        assert layer.idle_adopted == [sid]
        kw = layer.idle_kwargs[sid]
        assert kw["resume_handle"] == "thread-9" and kw["model"] == "gpt-6.1-sol"
        assert kw["used_mcps"] == ["memory", "display"] and kw["allow_full_fs"] is True
        assert kw["client_type"] == "task"

    @pytest.mark.asyncio
    async def test_an_entry_of_an_older_satellite_is_taken_back_with_empty_facts(
            self, temp_db, layer, placed):
        _, sid = _codex_chat(temp_db, exec_path="claude-code-cli")
        placed(sid, mode="default", floor=1790000000)
        entry = {"session_id": sid, "execution_path": "claude-code-cli", "agent_slug": "pa",
                 "turn_active": False, "incarnation": "b1-9", "command_id": "",
                 "buffered_events": 0, "use_native_permissions": False}
        await run_recovery.on_sessions_alive("machine-1", [entry], [])
        kw = layer.idle_kwargs[sid]
        assert (kw["resume_handle"], kw["used_mcps"]) == ("", ())

    def test_the_kind_of_an_adopted_session(self):
        assert run_recovery._client_type_of(None, "run-1") == "task"
        assert run_recovery._client_type_of({"id": "task-run-2", "source_type": "chat"}) == "task"
        assert run_recovery._client_type_of({"id": "c1", "source_type": "chat"}) == "dashboard"
        assert run_recovery._client_type_of({"id": "c2", "source_type": "phone"}) == "phone"


class TestACallsChatAfterARestart:
    """A call on a route tied to a person writes in their own chat. A restart
    ends the call, so the call's own session never comes back there: the
    person's next message builds the chat's own session. The person's own
    session after the call comes back as a chat's, the agent's conversation
    as today, and a call still live in this process (its machine's grace
    expired, no restart) keeps its session."""

    @pytest.fixture
    def layer(self, monkeypatch):
        import core.session.session_manager as sm
        from services.phone import call_chats
        lyr = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: lyr)
        call_chats._held.clear()
        yield lyr
        call_chats._held.clear()

    @staticmethod
    def _call_chat(placed, *, owner="user-1", recorded="phone", exec_path="claude-code-cli"):
        from core.session import session_kind, session_state
        sid, cid = uuid.uuid4().hex, str(uuid.uuid4())
        placed(sid, mode="auto")
        session_state._sessions[sid] = {"client_type": recorded}
        task_store.create_chat(cid, owner, "pa", "ask", model="m", execution_path=exec_path,
                               source_type=session_kind.PHONE.source_type)
        task_store.update_chat(cid, session_id=sid, execution_target="machine-1")
        return cid, sid

    @staticmethod
    def _forget(*sids):
        from core.session import session_state
        for sid in sids:
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_cut_call_in_a_persons_chat_is_closed_with_its_context(self, temp_db, layer, placed):
        from core.session import session_state
        _, sid = self._call_chat(placed)
        try:
            await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
            assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
            assert session_state.get_session_security(sid) is None
        finally:
            self._forget(sid)

    @pytest.mark.asyncio
    async def test_a_cut_call_turn_is_not_replayed_into_a_persons_chat(self, temp_db, layer, placed):
        from core.session import session_state
        _, sid = self._call_chat(placed)
        try:
            await run_recovery.on_sessions_alive("machine-1", [{
                "session_id": sid, "turn_active": True, "command_id": "c1"}])
            assert layer.adopted == [] and layer.unadopted_closed == [sid]
            assert session_state.get_session_security(sid) is None
        finally:
            self._forget(sid)

    @pytest.mark.asyncio
    async def test_the_persons_own_session_after_the_call_comes_back_as_a_chats(
            self, temp_db, layer, placed):
        _, sid = self._call_chat(placed, recorded="dashboard")
        try:
            await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
            assert layer.idle_adopted == [sid] and layer.unadopted_closed == []
            assert layer.idle_kwargs[sid]["client_type"] == "dashboard"
        finally:
            self._forget(sid)

    @pytest.mark.asyncio
    async def test_the_agents_conversation_comes_back_as_today(self, temp_db, layer, placed):
        _, sid = self._call_chat(placed, owner="phone")
        try:
            await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
            assert layer.idle_adopted == [sid]
            assert layer.idle_kwargs[sid]["client_type"] == "phone"
        finally:
            self._forget(sid)

    @pytest.mark.asyncio
    async def test_a_call_this_process_still_holds_keeps_its_session(self, temp_db, layer, placed):
        from services.phone import call_chats
        cid, sid = self._call_chat(placed)
        call_chats.hold(cid, sid)
        try:
            await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
            assert layer.idle_adopted == [sid]
            assert layer.idle_kwargs[sid]["client_type"] == "phone"
        finally:
            self._forget(sid)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("recorded, taken", [("phone", False), ("dashboard", True)])
    async def test_codex_takes_back_the_persons_session_never_the_calls(
            self, temp_db, layer, placed, monkeypatch, recorded, taken):
        from core.session import session_state
        from storage import remote_store
        monkeypatch.setattr(remote_store, "get_remote_machine",
                            lambda mid: {"id": mid, "allow_full_fs": False})
        _, sid = self._call_chat(placed, recorded=recorded, exec_path="codex-cli")
        try:
            await run_recovery.on_sessions_alive("machine-1", [], [_other(sid)])
            assert (layer.idle_adopted == [sid]) is taken
            assert (layer.unadopted_closed == [sid]) is not taken
            assert (session_state.get_session_security(sid) is None) is not taken
            if taken:
                assert layer.idle_kwargs[sid]["client_type"] == "dashboard"
        finally:
            self._forget(sid)


    @pytest.mark.asyncio
    @pytest.mark.parametrize("exec_path", ["claude-code-cli", "codex-cli"])
    async def test_a_cut_call_whose_kind_the_index_lost_is_closed_with_its_context(
            self, temp_db, layer, placed, monkeypatch, exec_path):
        """The session index is written two seconds behind the security index:
        a crash in that gap leaves the call's context on disk and no kind for
        its session. Only a session the index records as a chat's comes back
        in a chat a call wrote for a person."""
        from core.session import session_state
        from storage import remote_store
        monkeypatch.setattr(remote_store, "get_remote_machine",
                            lambda mid: {"id": mid, "allow_full_fs": False})
        cid, sid = self._call_chat(placed, exec_path=exec_path)
        session_state._sessions.pop(sid, None)
        try:
            if exec_path == "codex-cli":
                await run_recovery.on_sessions_alive("machine-1", [], [_other(sid)])
            else:
                await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
            assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
            assert session_state.get_session_security(sid) is None
        finally:
            self._forget(sid)


# ── a start that takes the id while a decline tells the machine keeps it ─────


def _new_life(sid: str):
    from auth.path_policy import SecurityContext
    from core import placement
    from core.session import session_state
    return SecurityContext(
        role="viewer", username=session_state._session_security[sid].username, agent="pa",
        is_admin_agent=False,
        placement=placement.PlacementCapabilities(kind="remote", machine_id="machine-1"))


@pytest.mark.asyncio
async def test_a_start_that_takes_the_id_while_a_declined_reports_machine_is_told_keeps_its_context(
        temp_db, monkeypatch, placed):
    """The decline looks again after the machine is told, as every step of
    the report does before it acts: a start of the id that began meanwhile
    owns what is kept under it."""
    from core.session import session_state
    _, _chat_id, sid = _mk_remote_run(temp_db, status="completed")
    placed(sid, mode="default", platform_role="member")   # no role on the agent now
    layer = _FakeLayer()
    new_ctx = _new_life(sid)

    async def close_unadopted(machine_id, session_id, incarnation=""):
        layer.unadopted_closed.append(session_id)
        session_state.mark_starting(sid, 900)             # RemoteSessionStartMixin.start_session
        layer.spawning.add(sid)
        session_state.register_session_state(sid, "default", new_ctx)
    layer.close_unadopted = close_unadopted
    import core.session.session_manager as sm
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    try:
        await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
        assert layer.unadopted_closed == [sid]
        kept = session_state.get_session_security(sid)   # the registration stores a stamped copy
        assert kept is not None and kept.role == "viewer"
        assert session_state._session_modes.get(sid) == "default"
    finally:
        session_state.cleanup_session_permission_state(sid)
        session_state.clear_starting(sid)


@pytest.mark.asyncio
async def test_a_start_that_ended_while_a_declined_reports_machine_is_told_keeps_its_context(
        temp_db, monkeypatch, placed):
    """The frame waits up to 15 s, long enough for a whole start: a start of the
    id that is over and holds it owns what is kept under it too."""
    from core.session import session_state
    _, _chat_id, sid = _mk_remote_run(temp_db, status="completed")
    placed(sid, mode="default", platform_role="member")
    layer = _FakeLayer()
    new_ctx = _new_life(sid)

    async def close_unadopted(machine_id, session_id, incarnation=""):
        layer.unadopted_closed.append(session_id)
        session_state.register_session_state(sid, "default", new_ctx)
        layer._sessions[sid] = object()                   # the start is over: the layer holds it
    layer.close_unadopted = close_unadopted
    import core.session.session_manager as sm
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    monkeypatch.setattr(sm, "_remote_layer", layer)
    try:
        await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": False}])
        assert layer.unadopted_closed == [sid]
        assert not session_state.is_starting(sid)
        kept = session_state.get_session_security(sid)
        assert kept is not None and kept.role == "viewer"
    finally:
        session_state.cleanup_session_permission_state(sid)


@pytest.mark.asyncio
async def test_a_start_that_takes_the_id_while_a_stopped_runs_machine_is_told_keeps_its_context(
        temp_db, monkeypatch, placed):
    from core.session import session_state
    run_id, chat_id, sid = _mk_remote_run(temp_db)
    placed(sid)
    session_state.mark_starting(sid, 30)
    assert task_store.cancel_live_run(run_id, "Interrupted by user")
    layer = _FakeLayer()
    new_ctx = _new_life(sid)

    async def close_unadopted(machine_id, session_id, incarnation=""):
        layer.unadopted_closed.append(session_id)
        session_state.mark_starting(sid, 900)
        session_state.register_session_state(sid, "default", new_ctx)
    layer.close_unadopted = close_unadopted
    import core.session.session_manager as sm
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    try:
        await run_recovery._recover_session(
            "machine-1", sid, {"session_id": sid}, run_id=run_id, chat_id=chat_id, agent="pa")
        assert layer.unadopted_closed == [sid]
        kept = session_state.get_session_security(sid)
        assert kept is not None and kept.role == "viewer"
    finally:
        session_state.cleanup_session_permission_state(sid)
        session_state.clear_starting(sid)
