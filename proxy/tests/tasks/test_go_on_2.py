"""A turn its machine cuts while a holder runs it again (an adoption's replay,
the continue pass's continue) goes back to the pass from the holder's end, and a
run no engine drives is stopped by a Stop and ended by the sweep.

These tests run the real adoption
(``on_sessions_alive``, ``_recover_session``), the real report reader
(``machine_sessions.on_alive_report``), the real ``_finalize_run``, the real
pass and the real ``reopen_run``. The fakes are the machine (the remote layer:
its adoption reads its session's queue) and the task round's engine."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core import background
from core.events import stream_pump, turn_ending
from core.events.common_events import DONE, ERROR, TEXT, CommonEvent
from core.execution_layer import ResumeVerdict
from core.remote import satellite_link
from core.remote.remote_resume import REPLAY_BEGIN
from core.session import session_state
from services.infra import platform_marks
from services.remote import machine_sessions
from services.scheduler import (
    continue_pass,
    cut_turns,
    delivery,
    run_recovery,
    scheduler,
    shared,
    stranded_runs,
)
from storage import database as task_store
from storage.automation import db_tasks, run_status
from storage.chat import cut_records, open_turns
from tests.remote.test_run_recovery import (  # noqa: F401  # placed: a fixture
    _FakeLayer,
    placed,
)
from tests.tasks.test_run_stream_terminal import _drive, _instant_slot, _Layer, _settle

MACHINE = "machine-1"


@pytest.fixture(autouse=True)
def _a_boot_that_catches_up(monkeypatch):
    monkeypatch.setattr(platform_marks, "_boot", platform_marks.Boot(
        stamped=True, down_since=datetime.now(timezone.utc) - timedelta(minutes=5)))
    monkeypatch.setattr(run_recovery, "_REPORT_SPACING_S", 0.0)
    run_recovery._failed_at_boot.clear()
    run_recovery._parked.clear()
    cut_turns._parked.clear()
    cut_turns._held.clear()
    shared._left_for_reopen.clear()
    yield
    run_recovery._failed_at_boot.clear()
    run_recovery._parked.clear()
    cut_turns._parked.clear()
    cut_turns._held.clear()
    shared._left_for_reopen.clear()
    shared._shutting_down = False
    stream_pump._platform_ending = False


@pytest.fixture
def results(monkeypatch):
    """Every result a run hands its delegator."""
    got: list[dict] = []

    async def _deliver(task, final_status, output, **kw):
        got.append({"status": final_status, "output": output, **kw})
    monkeypatch.setattr(delivery, "_deliver_task_result", _deliver)
    monkeypatch.setattr(scheduler, "_deliver_task_result", _deliver)
    return got


@pytest.fixture
def kicked(monkeypatch):
    """The pass is asked for, then run by the test (``_pass``)."""
    asked: list[int] = []
    monkeypatch.setattr(continue_pass, "kick", lambda: asked.append(1))
    return asked


class _Info:
    """What the remote layer holds for an adopted session."""

    def __init__(self, sid: str):
        self.session_id = sid
        self.machine_id = MACHINE
        self.turn_active = True
        self.cli_dead = False
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.created_at = time.monotonic()


class _Machine(_FakeLayer):
    """The machine, faked at the remote layer. Its adoption registers the
    session and replays the turn: a begin, half the work, then whatever its
    session's queue says. The lost marker the report's reader queues is the
    lost ending a machine gives, a ``None`` the stream's end. ``held``: the
    replay reads its queue only once ``hold`` is set."""

    def __init__(self, *, held: bool = False, ends: str = "lost"):
        super().__init__()
        self.replaying = asyncio.Event()
        self.hold = asyncio.Event()
        if not held:
            self.hold.set()
        # "lost": the lost marker ends the replay lost. "done": the turn's
        # end was ahead of it. "never": the replay never began (a machine
        # that cut it before its begin: the satellite sends nothing more).
        self.ends = ends

    def session_ids_on(self, machine_id):
        return [sid for sid, i in self._sessions.items()
                if getattr(i, "machine_id", "") == machine_id]

    def owns_session(self, session_id):
        return session_id in self._sessions

    async def adopt_session(self, *, machine_id, session_id, agent_name, execution_path,
                            command_id, use_native_permissions=False, mode="", token_floor=0,
                            **kw):
        self.adopted.append(session_id)
        info = _Info(session_id)
        self._sessions[session_id] = info
        if self.ends != "never":
            yield CommonEvent(type=REPLAY_BEGIN, data={"truncated": False})
            yield CommonEvent(type=TEXT, data={"content": "Half the work. "})
        self.replaying.set()
        await self.hold.wait()
        frame = await info.event_queue.get()
        info.turn_active = False
        if frame is None or self.ends != "lost":
            yield CommonEvent(type=DONE, data={})
            return
        ending = turn_ending.TurnEnding(reason=turn_ending.LOST, detail=frame.get("message", ""))
        yield CommonEvent(type=ERROR, data={"message": frame.get("message", ""),
                                            "ending": ending.as_dict()})

    async def close_session(self, session_id):
        info = self._sessions.pop(session_id, None)
        self.closed.append(session_id)
        if info is not None:
            info.event_queue.put_nowait(None)


class _Rounds(_Layer):
    """The engine of a reopened round: it finishes."""

    def __init__(self):
        super().__init__(hold=False)
        self.prompts: list[str] = []

    async def send_message(self, session_id, prompt, **kw):
        self.prompts.append(prompt)
        yield CommonEvent(type=TEXT, data={"content": "and the rest."})
        yield CommonEvent(type=DONE, data={})

    async def can_resume_session(self, *a, **k):
        return ResumeVerdict.RESUMABLE


def _the_platform(monkeypatch, machine: _Machine, rounds: _Rounds) -> None:
    """The remote layer is the machine, a task round runs on ``rounds``
    (``_drive``'s builders and slot, with no first run)."""
    from types import SimpleNamespace

    import core.session.session_manager as sm
    from core import concurrency
    from core.config import task_config_builder
    from services.remote import remote_status
    from tests._compute import serve

    async def _cfg(agent, task_def, sid, **kw):
        return SimpleNamespace(placement_sub=None, execution_target="local",
                               execution_path="claude-code-cli", interactive=False, resume=False,
                               extra_env={}, chat_id="")

    monkeypatch.setattr(sm, "_get_remote_layer", lambda: machine)
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", _cfg)
    monkeypatch.setattr(task_config_builder, "resolve_task_identity",
                        lambda *a, **k: task_config_builder.TaskIdentity("", "manager", "agent", None))
    monkeypatch.setattr(sm, "get_execution_layer", lambda *a, **k: rounds)
    monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *a, **k: ("local", None))
    serve(monkeypatch)
    monkeypatch.setattr(concurrency, "task_slot", _instant_slot)
    monkeypatch.setattr("configuration.models.get_cli_model", lambda agent, layer=None: "test-model")
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
    monkeypatch.setattr(machine_sessions, "_TURN_END_WAIT_S", 5.0)


def _a_lane_in_its_turn(placed) -> tuple[str, str, str, str]:  # noqa: F811  # the fixture's function
    """A delegated run in a turn on the machine when the platform stopped:
    its task row, its run row, its chat on the machine, its open-turn row.
    Returns ``(run_id, task_id, chat_id, session_id)``."""
    run_id, task_id = f"run-{uuid.uuid4().hex[:10]}", f"t-{uuid.uuid4().hex[:8]}"
    chat_id, sid = f"task-{run_id}", str(uuid.uuid4())
    db_tasks.create_dynamic_task(task_id, "pa", "count", "count to four", "cli", "one_time",
                                 None, None, 0, 600, None, scope="agent",
                                 notification_mode="none", on_complete_agent="boss")
    shared._dynamic_task_ids.add(task_id)
    task_store.create_run(run_id, task_id, "pa", "manual", None, "count to four",
                          "one-time", "agent", None)
    task_store.create_chat(chat_id, "user-1", "pa", "default", model="m",
                           execution_path="claude-code-cli")
    task_store.update_chat(chat_id, session_id=sid, execution_target=MACHINE)
    task_store.update_run(run_id, status=run_status.RUNNING, chat_id=chat_id, session_id=sid,
                          started_at=datetime.now(timezone.utc).isoformat())
    task_store.add_chat_message(chat_id, "user", "count to four")
    open_turns.open_turn(chat_id, "tok-1", sid, kind="task", run_id=run_id)
    placed(sid, MACHINE)
    return run_id, task_id, chat_id, sid


def _report(*headless: str, signed_in_at: float | None = None) -> satellite_link.AliveReport:
    return satellite_link.AliveReport(
        machine_id=MACHINE, headless=frozenset(headless), terminals=frozenset(), complete=True,
        sessions=(), other_sessions=(),
        signed_in_at=time.monotonic() if signed_in_at is None else signed_in_at,
        report_id=uuid.uuid4().hex)


async def _signs_in(*sessions: str) -> None:
    """A sign-in that lists ``sessions`` alive with a turn, as the link runs
    it: the adoption, then the report's hooks in their order."""
    report = _report(*sessions)
    await run_recovery.on_sessions_alive(MACHINE, [
        {"session_id": sid, "turn_active": True, "command_id": "c1"} for sid in sessions], [])
    await run_recovery.on_alive_report(report)
    await machine_sessions.on_alive_report(report)


async def _pass() -> int:
    taken = await continue_pass.run_pending()
    for task in background.running("continue_pass"):
        await task
    return taken


async def _until(predicate, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("not reached in time")
        await asyncio.sleep(0.02)


def _unsettled(chat_id: str) -> list[dict]:
    return [r for r in cut_records.in_state(*cut_records.UNSETTLED) if r["chat_id"] == chat_id]


async def _replays_settle() -> None:
    for task in background.running("run_recovery"):
        await asyncio.wait_for(task, 10)


class TestTheNight:
    """A restart takes the lane's turn back, then the machine's update cuts
    the replay: the turn goes back to the pass, which reopens the run."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("order", ["the replay ends first", "the report reader goes first"])
    async def test_the_lane_goes_on_by_itself_and_reports_once(
            self, temp_db, monkeypatch, placed, results, kicked, order):  # noqa: F811
        reader_first = order == "the report reader goes first"
        machine, rounds = _Machine(held=reader_first), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        run_id, _task_id, chat_id, sid = _a_lane_in_its_turn(placed)
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
        assert _unsettled(chat_id)[0]["state"] == cut_records.WAITING

        await _signs_in(sid)
        await asyncio.wait_for(machine.replaying.wait(), 10)
        assert run_id in run_recovery._driving       # the stranded sweep leaves it alone
        if reader_first:
            # The order where the reader goes first: its wait gives up and it
            # reads the store before the replay has read its lost marker.
            monkeypatch.setattr(machine_sessions, "_TURN_END_WAIT_S", 0.05)

        # "Update now": the satellite comes back without the session.
        await machine_sessions.on_alive_report(_report())
        machine.hold.set()
        await _replays_settle()

        assert machine.closed == [sid]
        records = _unsettled(chat_id)
        assert len(records) == 1, records
        record = records[0]
        assert (record["state"], record["run_id"], record["cause"], record["attempts"]) == (
            cut_records.PENDING, run_id, cut_records.CAUSE_MACHINE, 1)
        assert kicked
        assert task_store.get_run(run_id)["status"] == run_status.RUNNING and results == []

        assert await _pass() == 1
        await asyncio.wait_for(shared._running_tasks[run_id], 10)
        assert rounds.prompts and rounds.prompts[0].startswith(continue_pass.MACHINE_WORDS[:40])
        row = task_store.get_run(run_id)
        assert row["status"] == run_status.COMPLETED and not row.get("error_message")
        assert [r["status"] for r in results] == [run_status.COMPLETED]
        assert _unsettled(chat_id) == []
        assert not [r for r in task_store.get_chat_messages(chat_id)
                    if '"turn_ended"' in (r.get("event_data") or "")]
        assert cut_turns._held == {} and run_id not in run_recovery._driving

    @pytest.mark.asyncio
    async def test_a_reader_that_reads_after_the_reopen_writes_no_second_record(
            self, temp_db, monkeypatch, placed, results, kicked):  # noqa: F811
        """The other order of the race between the two ends: the replay's end hands the
        record back and the pass reopens the run before the report's reader
        would read the store. A record the reader wrote then would name the
        live reopened run, and the next pass would fail it."""
        machine, rounds = _Machine(), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        run_id, _task_id, chat_id, sid = _a_lane_in_its_turn(placed)
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
        released, asked = asyncio.Event(), []
        real = machine_sessions.machine_lost_session

        async def _late(*a, **k):
            asked.append(a)
            await released.wait()
            return await real(*a, **k)
        monkeypatch.setattr(machine_sessions, "machine_lost_session", _late)
        await _signs_in(sid)
        await asyncio.wait_for(machine.replaying.wait(), 10)
        reader = asyncio.ensure_future(machine_sessions.on_alive_report(_report()))
        await _replays_settle()
        assert await _pass() == 1
        await asyncio.wait_for(shared._running_tasks[run_id], 10)
        released.set()
        await asyncio.wait_for(reader, 10)
        assert asked == []
        assert _unsettled(chat_id) == []
        assert task_store.get_run(run_id)["status"] == run_status.COMPLETED
        assert [r["status"] for r in results] == [run_status.COMPLETED]


class TestTheHolders:
    def test_an_old_take_never_lets_go_of_a_newer_takes_hold(self):
        record = {"id": 9001, "chat_id": "chat-a"}
        first = cut_turns.hold(record)
        second = cut_turns.hold(record)
        cut_turns.let_go(first)
        assert cut_turns.held(9001)
        cut_turns.let_go(second)
        assert not cut_turns.held(9001)

    def test_the_pass_holds_only_a_continues_pump_of_its_own_chat(self):
        from types import SimpleNamespace
        cut_turns.hold({"id": 9002, "chat_id": "chat-a"})
        other_chat = SimpleNamespace(chat_id="chat-b", driven_by="continue")
        a_persons_turn = SimpleNamespace(chat_id="chat-a", driven_by="")
        assert cut_turns.holder_of(other_chat) == 0
        assert cut_turns.holder_of(a_persons_turn) == 0
        continued = SimpleNamespace(chat_id="chat-a", driven_by="continue")
        assert cut_turns.holder_of(continued) == 9002
        assert cut_turns.holder_of(continued) == 9002        # the same pump, found again


def _a_persons_chat_in_its_turn(placed) -> tuple[str, str]:  # noqa: F811  # the fixture's function
    """A person's chat on the machine, in a turn when the platform stopped."""
    from auth import roles
    from tests.tasks.test_continue_pass import PERSON, _role
    _role(PERSON, roles.EDITOR)
    chat_id, sid = f"chat-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())
    task_store.create_chat(chat_id, PERSON, "pa", "default", model="m",
                           execution_path="claude-code-cli")
    task_store.update_chat(chat_id, session_id=sid, execution_target=MACHINE)
    task_store.add_chat_message(chat_id, "user", "plan the week")
    open_turns.open_turn(chat_id, "tok-1", sid, kind="dashboard", person=PERSON)
    placed(sid, MACHINE)
    return chat_id, sid


class TestTheNightOnAPersonsChat:
    @pytest.mark.asyncio
    async def test_the_chat_is_continued_with_the_machines_words(
            self, temp_db, monkeypatch, placed, kicked):  # noqa: F811
        from tests.tasks.test_continue_pass import _fast  # noqa: F401
        machine, rounds = _Machine(), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        monkeypatch.setattr(continue_pass, "_PASS_SPACING_S", 0.0)
        monkeypatch.setattr(continue_pass, "_START_POLL_S", 0.01)
        chat_id, sid = _a_persons_chat_in_its_turn(placed)
        session_state.mark_recover_pending(sid)
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        await _signs_in(sid)
        await asyncio.wait_for(machine.replaying.wait(), 10)
        await machine_sessions.on_alive_report(_report())
        await _replays_settle()
        (record,) = _unsettled(chat_id)
        assert (record["state"], record["cause"], record["attempts"]) == (
            cut_records.PENDING, cut_records.CAUSE_MACHINE, 1)
        # Its turn ended with no card: the pass carries it on.
        assert not [r for r in task_store.get_chat_messages(chat_id)
                    if '"turn_ended"' in (r.get("event_data") or "")]
        continued: list[str] = []

        async def _rung(chat_id, text, **kw):
            from core.session.session_delivery import DeliveryOutcome
            continued.append(text)
            return DeliveryOutcome("oneshot", chat_id=chat_id)
        from core.session import chat_rebuild
        monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", _rung)
        assert await _pass() == 1
        assert len(continued) == 1 and continued[0].startswith(continue_pass.MACHINE_WORDS)
        assert cut_records.get(record["id"])["state"] == cut_records.CONTINUED


class TestAHeldTurnThatEndedItsOwnWay:
    """The report's reader found the replay's pump, and the pump ended by
    its own means: the holder settles as it always did, nothing goes back."""

    @pytest.mark.asyncio
    async def test_a_turn_whose_end_was_ahead_of_the_lost_marker_completes(
            self, temp_db, monkeypatch, placed, results, kicked):  # noqa: F811
        machine, rounds = _Machine(ends="done"), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        run_id, _task_id, chat_id, sid = _a_lane_in_its_turn(placed)
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
        await _signs_in(sid)
        await asyncio.wait_for(machine.replaying.wait(), 10)
        await machine_sessions.on_alive_report(_report())
        await _replays_settle()
        assert _unsettled(chat_id) == [] and not kicked
        assert task_store.get_run(run_id)["status"] == run_status.COMPLETED
        assert [r["status"] for r in results] == [run_status.COMPLETED]
        assert cut_turns._held == {}

    @pytest.mark.asyncio
    async def test_a_replay_its_machine_cut_before_its_begin_fails_with_one_report(
            self, temp_db, monkeypatch, placed, results, kicked):  # noqa: F811
        machine, rounds = _Machine(ends="never"), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        run_id, _task_id, chat_id, sid = _a_lane_in_its_turn(placed)
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
        await _signs_in(sid)
        await asyncio.wait_for(machine.replaying.wait(), 10)
        await machine_sessions.on_alive_report(_report())
        await _replays_settle()
        assert _unsettled(chat_id) == [] and not kicked
        row = task_store.get_run(run_id)
        assert row["status"] == run_status.FAILED
        assert [r["status"] for r in results] == [run_status.FAILED]
        assert cut_turns._held == {}


class TestAnAdoptionThatFails:
    @pytest.mark.asyncio
    async def test_it_closes_its_claim_and_fails_its_run_with_one_report(
            self, temp_db, monkeypatch, placed, results, kicked):  # noqa: F811
        machine, rounds = _Machine(), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        run_id, _task_id, chat_id, sid = _a_lane_in_its_turn(placed)
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)

        def _broken(entry, pump):
            raise RuntimeError("a fault nobody planned for")
        monkeypatch.setattr(cut_turns, "drives", _broken)
        await _signs_in(sid)
        await _replays_settle()
        assert cut_turns._held == {} and run_id not in run_recovery._driving
        assert _unsettled(chat_id) == []
        row = task_store.get_run(run_id)
        assert row["status"] == run_status.FAILED
        assert row["error_message"] == run_recovery.RUN_SESSION_CLOSED
        assert len(results) == 1


def _a_continue_turn(machine: _Machine, *, ends: list[str], session_id: str):
    """The rung of the pass's continue, with a real pump: its turn runs on a
    session of the machine (``session_id``: the same id, or a new one as a
    fresh start gives) and ends as ``ends`` says, one word per continue."""
    from core.events.common_events import PRODUCER_DONE
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.session.session_delivery import DeliveryOutcome
    texts: list[str] = []
    running = asyncio.Event()

    async def _rung(chat_id, text, *, source="", **kw):
        texts.append(text)
        how = ends.pop(0)
        # A fresh start stamps its new id on the chat before its turn
        # (``wake_fresh.run_echo``).
        task_store.update_chat(chat_id, session_id=session_id)
        info = _Info(session_id)
        info.created_at = time.monotonic() - 1.0
        machine._sessions[session_id] = info
        queue: asyncio.Queue = asyncio.Queue()

        async def _produce():
            await queue.put(CommonEvent(type=TEXT, data={"content": "Going on. "}))
            if how == "lost":
                await info.event_queue.get()
                ending = turn_ending.TurnEnding(reason=turn_ending.LOST, detail="gone")
                await queue.put(CommonEvent(type=ERROR, data={"message": "gone",
                                                              "ending": ending.as_dict()}))
            else:
                await queue.put(CommonEvent(type=DONE, data={}))
            info.turn_active = False
            await queue.put(CommonEvent(type=PRODUCER_DONE, data={}))

        producer = asyncio.create_task(_produce())
        pump = ChatStreamPump(chat_id=chat_id, session_id=session_id, producer=producer,
                              event_queue=queue,
                              perm_queue=session_state.get_permission_queue(session_id))
        pump.driven_by = source
        _active_pumps[chat_id] = pump
        pump.start()
        running.set()
        await asyncio.wait({pump._task})
        machine._sessions.pop(session_id, None)
        return DeliveryOutcome("oneshot", chat_id=chat_id)
    return _rung, texts, running


class TestThePassesContinueItsMachineCut:
    """The pass holds the record (``running``) and its continue's turn runs
    on the machine. The machine's update cuts it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("same_session", [True, False])
    async def test_it_goes_back_to_the_pass_and_the_card_ends_it(
            self, temp_db, monkeypatch, placed, kicked, same_session):  # noqa: F811
        from core.session import chat_rebuild
        machine, rounds = _Machine(), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        monkeypatch.setattr(continue_pass, "_PASS_SPACING_S", 0.0)
        monkeypatch.setattr(continue_pass, "_START_POLL_S", 0.01)
        monkeypatch.setattr(continue_pass, "_running", False)
        monkeypatch.setattr(continue_pass, "_again", False)
        chat_id, sid = _a_persons_chat_in_its_turn(placed)
        open_turns.end_turn(chat_id, "tok-1")
        from tests.tasks.test_continue_pass import PERSON
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                                 kind="dashboard", person=PERSON, session_id=sid,
                                 engine_open=True, started_at="2026-01-01T00:00:00+00:00")
        on = sid if same_session else str(uuid.uuid4())
        rung, texts, running = _a_continue_turn(machine, ends=["lost", "lost", "lost"],
                                                session_id=on)
        monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", rung)
        for attempt in (1, 2, 3):
            running.clear()
            taking = asyncio.ensure_future(_pass())
            await asyncio.wait_for(running.wait(), 10)
            assert cut_turns.held(record["id"])
            await machine_sessions.on_alive_report(_report())
            await asyncio.wait_for(taking, 10)
            row = cut_records.get(record["id"])
            if attempt < 3:
                assert (row["state"], row["attempts"], row["cause"]) == (
                    cut_records.PENDING, attempt, cut_records.CAUSE_MACHINE)
            else:
                assert (row["state"], row["reason"]) == (cut_records.DROPPED,
                                                          "its machine cut it again")
            assert _unsettled(chat_id) in ([], [row])
        assert len(texts) == 3
        assert texts[0].startswith(continue_pass.CONTINUE_WORDS)
        assert texts[1].startswith(continue_pass.MACHINE_WORDS)
        assert cut_turns._held == {}
        cards = [r for r in task_store.get_chat_messages(chat_id)
                 if '"turn_ended"' in (r.get("event_data") or "")]
        assert len(cards) == 1


@pytest.fixture
def stops(temp_db, monkeypatch, results):
    """A run's task row, read by the report (``_report``)."""
    monkeypatch.setattr(task_store, "get_dynamic_task", lambda tid: {"id": tid} if tid else None)
    monkeypatch.setattr(scheduler, "_row_to_task", lambda row: row["id"])
    return results


def _stranded(*, task_type: str | None = None, completed: bool = False) -> tuple[str, str, str]:
    """A delegated run ``running`` with no task in this process and no record
    of its chat: what a second cut left before the holder's hand-back."""
    from tests.remote.test_run_recovery import _mk_remote_run
    run_id, chat_id, sid = _mk_remote_run(None)
    if task_type or completed:
        with task_store.get_conn() as conn:
            conn.execute("UPDATE task_runs SET task_type=COALESCE(%s, task_type), "
                         "completed_at=%s WHERE id=%s",
                         (task_type, datetime.now(timezone.utc).isoformat() if completed else None,
                          run_id))
            conn.commit()
    return run_id, chat_id, sid


class TestAStopOnARunNoEngineDrives:
    """The page shows such a run running: a Stop stops it, always, and says so
    in the log."""

    @pytest.mark.asyncio
    async def test_a_run_with_no_record_is_stopped_and_its_delegator_told_once(
            self, stops, monkeypatch, caplog):
        closed: list[str] = []

        class _Holds:
            async def close_session(self, sid):
                closed.append(sid)
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "find_layer_for_session", lambda sid: _Holds())
        run_id, chat_id, _sid = _stranded()
        with caplog.at_level("INFO", logger="otodock.recovery"):
            assert await scheduler.cancel_run(run_id) is True
        row = task_store.get_run(run_id)
        assert row["status"] == run_status.CANCELLED and row["error_message"] == "Interrupted by user"
        assert [r["status"] for r in stops] == [run_status.USER_INTERRUPTED]
        # Nothing says whose the session is now: a later round may run on it.
        assert closed == []
        assert _unsettled(chat_id) == []
        assert any("stopped by a person" in r.getMessage() and run_id in r.getMessage()
                   for r in caplog.records)
        # A second Stop finds nothing to stop, and says so.
        with caplog.at_level("INFO", logger="otodock.recovery"):
            assert await scheduler.cancel_run(run_id) is False
        assert any("not live" in r.getMessage() for r in caplog.records)
        assert len(stops) == 1

    @pytest.mark.asyncio
    async def test_another_runs_record_stays(self, stops):
        run_id, chat_id, _sid = _stranded()
        other = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                                session_id="another", kind="task", run_id="run-other")
        assert await scheduler.cancel_run(run_id) is True
        assert task_store.get_run(run_id)["status"] == run_status.CANCELLED
        assert cut_records.get(other["id"])["state"] == cut_records.PENDING
        assert len(stops) == 1

    @pytest.mark.asyncio
    async def test_a_task_record_of_its_session_that_names_no_run_is_the_runs(self, stops):
        run_id, chat_id, sid = _stranded()
        own = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                              session_id=sid, kind="task")
        assert await scheduler.cancel_run(run_id) is True
        assert cut_records.get(own["id"])["state"] == cut_records.DROPPED
        assert task_store.get_run(run_id)["status"] == run_status.CANCELLED

    @pytest.mark.asyncio
    async def test_a_persons_record_of_its_session_is_not_the_runs(self, stops):
        """A dashboard turn on the worker chat, cut by its machine: the
        person's own cut turn, which the Stop of the run leaves to the pass."""
        run_id, chat_id, sid = _stranded()
        persons = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE,
                                  state=cut_records.PENDING, session_id=sid, kind="dashboard")
        assert await scheduler.cancel_run(run_id) is True
        assert cut_records.get(persons["id"])["state"] == cut_records.PENDING

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["a finished run a person's turn flipped back",
                                      "an app handler's run", "a run a boot refused"])
    async def test_a_run_that_never_reopens_is_not_touched(self, stops, case, caplog):
        if case == "an app handler's run":
            run_id, _chat_id, _sid = _stranded(task_type="app")
        elif case == "a run a boot refused":
            run_id, _chat_id, _sid = _stranded()
            task_store.update_run(run_id, error_message=run_recovery.RUN_CUT)
        else:
            run_id, _chat_id, _sid = _stranded(completed=True)
        with caplog.at_level("INFO", logger="otodock.recovery"):
            assert await scheduler.cancel_run(run_id) is False
        assert task_store.get_run(run_id)["status"] == run_status.RUNNING and stops == []
        assert any(run_id in r.getMessage() for r in caplog.records)


@pytest.fixture
def ticks(monkeypatch):
    """The reaper's ticks of the stranded sweep, on a clock the test moves."""
    from types import SimpleNamespace
    clock = [1000.0]
    # The module's own clock only: the loop's must keep running.
    monkeypatch.setattr(stranded_runs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    stranded_runs._stranded_since.clear()

    async def _tick(after: float = 0.0) -> int:
        clock[0] += after
        return await stranded_runs.sweep()
    yield _tick
    stranded_runs._stranded_since.clear()


class TestTheSweepFindsARunNobodyDrives:
    """A hole nobody has thought of yet still ends with the delegator told."""

    @pytest.mark.asyncio
    async def test_it_is_ended_once_with_one_report_on_the_second_tick_past_the_grace(
            self, stops, ticks):
        run_id, chat_id, _sid = _stranded()
        assert await ticks() == 0
        assert await ticks(60) == 0                              # within the grace
        assert task_store.get_run(run_id)["status"] == run_status.RUNNING
        assert await ticks(61) == 1
        row = task_store.get_run(run_id)
        assert row["status"] == run_status.FAILED and row["error_message"] == run_recovery.RUN_CUT
        assert [r["status"] for r in stops] == [run_status.FAILED]
        assert await ticks(120) == 0 and len(stops) == 1

    @pytest.mark.asyncio
    async def test_after_its_machine_cut_the_chats_last_turn_it_says_so(self, stops, ticks):
        run_id, chat_id, sid = _stranded()
        left = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                               session_id=sid, kind="task", run_id="run-before")
        cut_records.settle(left["id"], cut_records.CONTINUED)
        await ticks()
        assert await ticks(121) == 1
        assert task_store.get_run(run_id)["error_message"] == run_recovery.RUN_CUT_MACHINE
        assert stops[0]["ending"].detail == turn_ending.MACHINE

    @pytest.mark.asyncio
    async def test_a_run_that_is_seen_again_only_later_starts_its_grace_again(self, stops, ticks):
        run_id, chat_id, sid = _stranded()
        await ticks()
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                                 session_id=sid, kind="task", run_id=run_id)
        await ticks(60)                                          # its record carries it now
        cut_records.settle(record["id"], cut_records.DROPPED)
        assert await ticks(61) == 0                               # the grace starts again here
        assert await ticks(121) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", [
        "a task drives it", "parked", "an adoption drives it", "a pending record names it",
        "a held record names it", "an app handler's run", "a run a person's turn flipped back",
        "a run a boot refused", "it drives a live terminal", "it is waiting to start"])
    async def test_what_is_carried_on_is_never_touched(self, stops, ticks, monkeypatch, case):
        run_id, chat_id, sid = _stranded(task_type="app" if case == "an app handler's run" else None,
                                         completed=case == "a run a person's turn flipped back")
        undo = []
        if case == "a task drives it":
            task = asyncio.ensure_future(asyncio.sleep(30))
            shared._running_tasks[run_id] = task
            undo.append(lambda: (shared._running_tasks.pop(run_id, None), task.cancel()))
        elif case == "parked":
            run_recovery._parked[sid] = {"run_id": run_id, "chat_id": chat_id,
                                         "machine_id": MACHINE, "agent": "pa", "deadline": 1e12}
        elif case == "an adoption drives it":
            run_recovery._driving.add(run_id)
            undo.append(lambda: run_recovery._driving.discard(run_id))
        elif case == "a pending record names it":
            cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
                            session_id=sid, kind="task", run_id=run_id)
        elif case == "a held record names it":
            record = cut_records.add(chat_id, cause=cut_records.CAUSE_MACHINE,
                                     state=cut_records.PENDING, session_id=sid, kind="task",
                                     run_id=run_id)
            cut_turns.hold(record)
            cut_records.claim(record["id"])
        elif case == "a run a boot refused":
            task_store.update_run(run_id, error_message=run_recovery.RUN_CUT)
        elif case == "it drives a live terminal":
            from types import SimpleNamespace

            from core.session import interactive_session
            monkeypatch.setattr(interactive_session, "get",
                                lambda s: SimpleNamespace(alive=True) if s == sid else None)
        elif case == "it is waiting to start":
            task_store.update_run(run_id, status=run_status.PENDING)
        try:
            await ticks()
            assert await ticks(121) == 0
            assert await ticks(121) == 0
        finally:
            for fn in undo:
                fn()
        assert stops == []
        assert task_store.get_run(run_id)["status"] in run_status.LIVE


class TestTheGateWaitsOnlyWhileTheHolderLives:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [cut_records.WAITING, cut_records.RUNNING])
    async def test_a_record_whose_holder_is_gone_ends_the_follow_up_at_the_ceiling(
            self, temp_db, monkeypatch, state):
        from services.scheduler import lanes
        monkeypatch.setattr(lanes, "_LANE_POLL_S", 0.02)
        from tests.tasks.test_go_on import _delegate_run, _lane_chat
        chat_id = _lane_chat()
        _delegate_run("run-h1", "t-h1", chat_id, opened=False)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                                 session_id="sid-gone", kind="dashboard", machine_id=MACHINE)
        with task_store.get_conn() as conn:
            conn.execute("UPDATE chat_cut_turns SET state=%s WHERE id=%s", (state, record["id"]))
            conn.commit()
        # The ceiling is long enough that a loaded box's delay stays inside
        # the gap between one ceiling and two.
        began = time.monotonic()
        with pytest.raises(lanes.LaneNotStarted) as refused:
            await asyncio.wait_for(lanes._await_lane_turn(chat_id, "run-h1", ceiling_seconds=1.5), 10)
        assert time.monotonic() - began < 2.6, "waited twice the ceiling on a holder that is gone"
        assert str(refused.value) == lanes.lane_words(lanes.LANE_NOT_TAKEN_UP, chat_id)


@pytest.mark.asyncio
async def test_the_recovery_sweep_runs_the_stranded_sweep_first(monkeypatch):
    order: list[str] = []

    async def _stranded():
        order.append("stranded")
        return 0

    async def _pass_sweep():
        order.append("pass")
    monkeypatch.setattr(stranded_runs, "sweep", _stranded)
    monkeypatch.setattr(continue_pass, "sweep", _pass_sweep)
    await run_recovery.sweep_expired()
    assert order == ["stranded", "pass"]


class _SlowClose(_Layer):
    """A run's engine whose close takes a while: the window where the run is
    ending on a Stop."""

    def __init__(self):
        super().__init__(hold=True)
        self.closing, self.release = asyncio.Event(), asyncio.Event()

    async def close_session(self, session_id):
        self.closes += 1
        self._closed.set()
        self.closing.set()
        await self.release.wait()


class TestASecondStop:
    @pytest.mark.asyncio
    async def test_a_second_stop_while_the_run_ends_keeps_the_delegators_result(
            self, temp_db, monkeypatch):
        """The first Stop cancels the run's task. While its end closes the
        session, a second Stop (an agent calling cancel again, a second
        person) must not cancel it again: that cut its end before the
        delegator's result."""
        got = []

        async def _deliver(task, final_status, output, **kw):
            got.append(final_status)
        monkeypatch.setattr(delivery, "_deliver_task_result", _deliver)
        run_id, sid = "run-2stop", "33333333-3333-4333-8333-333333333333"
        task = shared.TaskDefinition(id="t-2stop", name="count", agent="agent-x", prompt="count",
                                     scope="agent", notification_mode="none",
                                     on_complete_agent="boss")
        layer = _SlowClose()
        t, q = _drive(monkeypatch, layer, task, run_id, sid)
        await asyncio.wait_for(q.first_text.wait(), 10)
        assert await scheduler.cancel_run(run_id) is True
        await asyncio.wait_for(layer.closing.wait(), 10)
        assert await scheduler.cancel_run(run_id) is True          # the second Stop
        assert scheduler.platform_cancel_run(run_id, "a reap") is True   # and a platform's
        layer.release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(t, 10)
        await _settle(f"task-{run_id}")
        assert temp_db.get_run(run_id)["status"] == run_status.CANCELLED
        assert got == [run_status.USER_INTERRUPTED]
        # The marks went with the frame: a later run of the id is cancelled anew.
        assert run_id not in shared._user_cancelled_runs
        assert run_id not in shared._platform_interrupts


class TestAStopThatBeatsTheHandBack:
    @pytest.mark.asyncio
    async def test_the_chats_waiting_reload_is_told(self, temp_db, monkeypatch, kicked):
        """A person's Stop settled the record a moment before the replay's end
        would hand it back: nothing goes back, and the reload the chat's person
        asked for hears that it did not happen."""
        from types import SimpleNamespace

        from services.remote import chat_reload
        from storage.chat import pending_rebuilds
        told: list[tuple] = []

        async def _settle(row, *, continued, person="", lives_on=False):
            told.append((row["chat_id"], continued, lives_on))
            return []
        monkeypatch.setattr(chat_reload, "settle_after_restart", _settle)
        chat_id, sid = f"chat-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())
        task_store.create_chat(chat_id, "user-1", "pa", "default")
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=sid, kind="task", run_id="run-x", machine_id=MACHINE)
        pending_rebuilds.save(chat_id, pending_rebuilds.RELOAD, person="user-1", wait_s=600)
        cut_turns._park(record, "tok")
        entry = cut_turns.claim(sid)
        cut_turns.drives(entry, SimpleNamespace(cut_by_machine=True))
        assert cut_records.settle(record["id"], cut_records.DROPPED, "a person stopped its run")
        await cut_turns.replay_settled(entry, began=True)
        assert told == [(chat_id, False, False)]
        assert cut_records.get(record["id"])["state"] == cut_records.DROPPED and not kicked
        assert not cut_turns.held(record["id"])


class TestAnAdoptionThatFailsAfterItsHandBack:
    @pytest.mark.asyncio
    async def test_the_pass_keeps_the_record_and_the_run(self, temp_db, monkeypatch, results, kicked):
        """The replay handed its record back and the pass took it, then the
        adoption met an exception: it closes no claim it no longer holds, and
        it fails no run the pass now carries on."""
        from types import SimpleNamespace

        from tests.remote.test_run_recovery import _mk_remote_run
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        # The cut turn's row stays: a claim closed wrongly would write its
        # closing row (the card) on the chat.
        open_turns.open_turn(chat_id, "tok", sid, kind="task", run_id=run_id)
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.WAITING,
                                 session_id=sid, kind="task", run_id=run_id, machine_id=MACHINE)
        cut_turns._park(record, "tok")
        cut = cut_turns.claim(sid)
        pass_hold: list[dict] = []

        async def _turn(*a, **k):
            cut_turns.drives(cut, SimpleNamespace(cut_by_machine=True))
            await cut_turns.replay_settled(cut, began=True)
            pass_hold.append(cut_turns.hold(cut_records.get(record["id"])))
            raise RuntimeError("a fault after the hand-back")
        monkeypatch.setattr(run_recovery, "_recover_session_turn", _turn)
        await run_recovery._recover_session(MACHINE, sid, {}, run_id=run_id, chat_id=chat_id,
                                            agent="pa", cut=cut)
        assert task_store.get_run(run_id)["status"] == run_status.RUNNING and results == []
        assert cut_records.get(record["id"])["state"] == cut_records.PENDING
        assert cut_turns.holds(pass_hold[0])
        from core.events import chat_writer
        await chat_writer.drain(chat_id, timeout=5.0)
        assert open_turns.get_open(chat_id) is not None
        assert not [r for r in task_store.get_chat_messages(chat_id)
                    if '"turn_ended"' in (r.get("event_data") or "")]
        assert run_id not in run_recovery._driving


class TestAStopOfARunWaitingToStart:
    @pytest.mark.asyncio
    async def test_a_pending_run_with_no_task_answers_not_running(self, stops, caplog):
        """A fire held by a planned stop, or one the boot will start: the page
        shows it waiting, and the resume or the boot starts it."""
        run_id, _chat_id, _sid = _stranded()
        task_store.update_run(run_id, status=run_status.PENDING)
        with caplog.at_level("INFO", logger="otodock.recovery"):
            assert await scheduler.cancel_run(run_id) is False
        assert task_store.get_run(run_id)["status"] == run_status.PENDING and stops == []
        assert any("waiting to start" in r.getMessage() and run_id in r.getMessage()
                   for r in caplog.records)


class TestACancelAfterAPlatformCancel:
    """The run ends on a platform cancel (a reap, an offboarding, a judge's wait):
    a second cancel in its end, a person's or the platform's, keeps the
    delegator's result."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", ["a person's Stop", "a platform cancel"])
    async def test_the_delegator_still_gets_its_result(self, temp_db, monkeypatch, second):
        got = []

        async def _deliver(task, final_status, output, **kw):
            got.append(final_status)
        monkeypatch.setattr(delivery, "_deliver_task_result", _deliver)
        run_id, sid = f"run-p{uuid.uuid4().hex[:6]}", str(uuid.uuid4())
        task = shared.TaskDefinition(id=f"t-{run_id}", name="count", agent="agent-x",
                                     prompt="count", scope="agent", notification_mode="none",
                                     on_complete_agent="boss")
        layer = _SlowClose()
        t, q = _drive(monkeypatch, layer, task, run_id, sid)
        await asyncio.wait_for(q.first_text.wait(), 10)
        assert scheduler.platform_cancel_run(run_id, "a reap") is True
        await asyncio.wait_for(layer.closing.wait(), 10)
        # The platform's mark stays while the run ends: the guard reads it.
        assert (run_id in shared._user_cancelled_runs,
                run_id in shared._platform_interrupts) == (False, True)
        if second == "a person's Stop":
            assert await scheduler.cancel_run(run_id) is True
        else:
            assert scheduler.platform_cancel_run(run_id, "another reap") is True
        layer.release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(t, 10)
        await _settle(f"task-{run_id}")
        assert temp_db.get_run(run_id)["status"] == run_status.FAILED
        assert got == [run_status.CANCELLED]


class TestThePassReadsBeforeThePumpEnded:
    @pytest.mark.asyncio
    async def test_a_continue_that_returns_before_its_pumps_end(
            self, temp_db, monkeypatch, placed, kicked):  # noqa: F811
        """The continue's rung returns while its pump still runs (the stall
        watch's reap gives a pump two seconds), after the report's reader
        marked the pump for the pass."""
        import time

        from core.events import turn_ending
        from core.events.common_events import ERROR, PRODUCER_DONE, TEXT, CommonEvent
        from core.events.stream_pump import ChatStreamPump, _active_pumps
        from core.session import chat_rebuild, session_state
        from core.session.session_delivery import DeliveryOutcome
        from tests.tasks.test_continue_pass import PERSON

        machine, rounds = _Machine(), _Rounds()
        _the_platform(monkeypatch, machine, rounds)
        monkeypatch.setattr(continue_pass, "_PASS_SPACING_S", 0.0)
        monkeypatch.setattr(continue_pass, "_START_POLL_S", 0.01)
        monkeypatch.setattr(continue_pass, "_running", False)
        monkeypatch.setattr(continue_pass, "_again", False)
        monkeypatch.setattr(machine_sessions, "_TURN_END_WAIT_S", 0.2)
        chat_id, sid = _a_persons_chat_in_its_turn(placed)
        open_turns.end_turn(chat_id, "tok-1")
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=cut_records.PENDING,
                                 kind="dashboard", person=PERSON, session_id=sid,
                                 engine_open=True, started_at="2026-01-01T00:00:00+00:00")
        running, marked, go_on = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pumps: list = []

        async def _rung(chat, text, *, source="", **kw):
            task_store.update_chat(chat, session_id=sid)
            info = _Info(sid)
            info.created_at = time.monotonic() - 1.0
            machine._sessions[sid] = info
            queue: asyncio.Queue = asyncio.Queue()

            async def _produce():
                await queue.put(CommonEvent(type=TEXT, data={"content": "Going on. "}))
                await go_on.wait()                       # the pump's end is slow
                await info.event_queue.get()
                ending = turn_ending.TurnEnding(reason=turn_ending.LOST, detail="gone")
                await queue.put(CommonEvent(type=ERROR, data={"message": "gone",
                                                              "ending": ending.as_dict()}))
                info.turn_active = False
                await queue.put(CommonEvent(type=PRODUCER_DONE, data={}))

            producer = asyncio.create_task(_produce())
            pump = ChatStreamPump(chat_id=chat, session_id=sid, producer=producer,
                                  event_queue=queue,
                                  perm_queue=session_state.get_permission_queue(sid))
            pump.driven_by = source
            _active_pumps[chat] = pump
            pump.start()
            pumps.append(pump)
            running.set()
            await marked.wait()          # the reader marked the pump meanwhile
            return DeliveryOutcome("oneshot", chat_id=chat)   # the reap's early return
        monkeypatch.setattr(chat_rebuild, "rebuild_and_resume", _rung)

        taking = asyncio.ensure_future(_pass())
        await asyncio.wait_for(running.wait(), 10)
        reading = asyncio.ensure_future(machine_sessions.on_alive_report(_report()))
        await _until(lambda: chat_id in stream_pump._pass_takes)
        marked.set()
        # The pump ends 0.3 s after the rung's early return.
        asyncio.get_running_loop().call_later(0.3, go_on.set)
        await asyncio.wait_for(taking, 10)
        await asyncio.wait_for(reading, 10)
        await asyncio.wait_for(pumps[0]._task, 10)
        from core.events import chat_writer
        await chat_writer.drain(chat_id, timeout=5.0)
        row = cut_records.get(record["id"])
        cards = [r for r in task_store.get_chat_messages(chat_id)
                 if '"turn_ended"' in (r.get("event_data") or "")]
        assert pumps[0].cut_by_machine is True
        # The pass read the pump's own ending: the record goes back, the
        # turn goes on, and no card is written for it.
        assert (row["state"], row["attempts"], row["cause"]) == (
            cut_records.PENDING, 1, cut_records.CAUSE_MACHINE)
        assert cards == []


class _HeldRounds(_Rounds):
    """The reopened round's engine: its turn stays open until it is let go
    or its session is closed."""

    def __init__(self):
        super().__init__()
        self.started, self.go = asyncio.Event(), asyncio.Event()

    async def send_message(self, session_id, prompt, **kw):
        from core.events.common_events import DONE, TEXT, CommonEvent
        self.prompts.append(prompt)
        yield CommonEvent(type=TEXT, data={"content": "and "})
        self.started.set()
        waits = [asyncio.ensure_future(self.go.wait()), asyncio.ensure_future(self._closed.wait())]
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for w in waits:
            w.cancel()
        yield CommonEvent(type=TEXT, data={"content": "the rest."})
        yield CommonEvent(type=DONE, data={})


class TestAStopWhoseReadsStraddleTheReopen:
    @pytest.mark.asyncio
    async def test_the_stop_stops_the_reopened_run(
            self, temp_db, monkeypatch, placed, results, kicked):  # noqa: F811
        """The Stop looks for the run's task (none yet), then reads the store
        while the pass settles the record and launches the run: its read finds
        no record, and the run has a task by the time it decides."""
        import threading

        from services.scheduler import stranded_runs
        machine, rounds = _Machine(), _HeldRounds()
        _the_platform(monkeypatch, machine, rounds)
        run_id, _task_id, chat_id, sid = _a_lane_in_its_turn(placed)
        cut_turns.record_at_boot(clean_stop=True, catches_up=True)
        run_recovery.defer_orphaned_runs(catches_up=True, clean_stop=True)
        await _signs_in(sid)
        await asyncio.wait_for(machine.replaying.wait(), 10)
        await machine_sessions.on_alive_report(_report())
        await _replays_settle()
        assert _unsettled(chat_id)[0]["state"] == cut_records.PENDING

        entered, gate = threading.Event(), threading.Event()

        class _Records:
            def __getattr__(self, name):
                return getattr(cut_records, name)

            def unsettled_of(self, chat):
                entered.set()
                gate.wait(10)        # the read lands after the reopen's settle
                return cut_records.unsettled_of(chat)
        monkeypatch.setattr(stranded_runs, "cut_records", _Records())

        stop = asyncio.ensure_future(scheduler.cancel_run(run_id))   # no task yet
        await _until(entered.is_set)
        assert shared._running_tasks.get(run_id) is None
        assert await _pass() == 1                                    # settles, launches
        task = shared._running_tasks[run_id]
        await asyncio.wait_for(rounds.started.wait(), 10)
        gate.set()
        answered = await asyncio.wait_for(stop, 10)
        await asyncio.sleep(0.3)
        rounds.go.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        await _settle(chat_id)
        row = task_store.get_run(run_id)
        assert answered is True
        assert [r["status"] for r in results] == [run_status.USER_INTERRUPTED]
        assert row["status"] == run_status.CANCELLED


class TestAStopAtARecordNobodyHolds:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["waiting", "pending"])
    async def test_the_chats_waiting_reload_is_told(self, temp_db, monkeypatch, state):
        """The twin of the Stop that beats the hand-back: the Stop settles a
        parked or a pending record itself, and the reload the chat's person
        asked for before the restart waits on that record."""
        from services.remote import chat_reload
        from storage.chat import pending_rebuilds
        told: list[tuple] = []

        async def _settle_row(row, *, continued, person="", lives_on=False):
            told.append((row["chat_id"], continued, lives_on))
            return []
        monkeypatch.setattr(chat_reload, "settle_after_restart", _settle_row)
        chat_id, sid = f"chat-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())
        task_store.create_chat(chat_id, "user-1", "pa", "default")
        record = cut_records.add(chat_id, cause=cut_records.CAUSE_STOP, state=state,
                                 session_id=sid, kind="task", run_id="run-x",
                                 machine_id="machine-1")
        pending_rebuilds.save(chat_id, pending_rebuilds.RELOAD, person="user-1", wait_s=600)
        if state == "waiting":
            cut_turns._park(record, "tok")
        assert await cut_turns.stopped_by_person(record) is True
        assert told == [(chat_id, False, False)]


class TestAStopWhileAReplayEndsTheRun:
    @pytest.mark.asyncio
    async def test_the_run_is_left_to_its_replays_end(self, stops, caplog):
        """A replay that ended well is writing its run's end: a Stop with no
        record of its own leaves the run to that end, and says so."""
        run_id, _chat_id, _sid = _stranded()
        run_recovery._driving.add(run_id)
        try:
            with caplog.at_level("INFO", logger="otodock.recovery"):
                assert await scheduler.cancel_run(run_id) is False
        finally:
            run_recovery._driving.discard(run_id)
        assert task_store.get_run(run_id)["status"] == run_status.RUNNING and stops == []
        assert any("its replay is ending it" in r.getMessage() and run_id in r.getMessage()
                   for r in caplog.records)
